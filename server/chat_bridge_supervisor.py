"""
server/chat_bridge_supervisor.py — supervised chat-bridge child processes.

The game server owns the lifecycle of the Node chat-bridge service so the DM
never has to run `npm start` by hand:

  * When a session has a connected Twitch channel AND the chat bridge toggle
    is enabled, the supervisor spawns `node src/index.js` with
    cwd=<repo>/chat-bridge. The bridge still reads chat-bridge/.env for its
    Twitch bot credentials; GAME_SERVER_WS_URL, GAME_SESSION_ID and
    CHAT_BRIDGE_TOKEN are injected via the child environment (dotenv never
    overrides existing process env), so those never need manual syncing.
  * Unexpected exits are restarted with capped exponential backoff; after
    MAX_RETRIES consecutive failures the bridge is marked "failed" until the
    DM restarts it (or toggles the bridge / reconnects Twitch).
  * stdout/stderr are piped into the server log with a [chat-bridge] prefix
    and kept in a small per-session ring buffer for the status endpoint.
  * One bridge per session; double-spawns are prevented with a per-session
    lock, and a manually started bridge (`npm start` during development) that
    is already connected to the session suppresses spawning.

Bridge failures must never affect the game server: every supervisor entry
point swallows its own errors and only ever mutates supervisor state.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import signal
import subprocess
import time
from collections import deque
from pathlib import Path

from server.paths import REPO_ROOT

logger = logging.getLogger(__name__)

CHAT_BRIDGE_DIR = REPO_ROOT / "chat-bridge"

# The service-account user id the bridge connects with (see main.py ws endpoint).
_BRIDGE_USER_ID = "chat_bridge"

_LOG_RING_SIZE = 200      # lines kept per session
_LOG_TAIL_SIZE = 40       # lines exposed via the status endpoint


def _game_server_ws_url() -> str:
    """WebSocket URL the spawned bridge should connect back to.

    Overridable with CHAT_BRIDGE_WS_URL; defaults to the loopback address on
    the configured server port so the bridge never depends on public DNS/TLS.
    """
    override = os.environ.get("CHAT_BRIDGE_WS_URL", "").strip()
    if override:
        return override.rstrip("/")
    from server.config import load_config
    cfg = load_config(REPO_ROOT / "config.txt")
    return f"ws://127.0.0.1:{cfg.port}"


def _ensure_bridge_token() -> str:
    """Return CHAT_BRIDGE_TOKEN, generating an ephemeral one if unset.

    The generated token is stored in os.environ so the /api/chat-bridge/auth
    and /credentials endpoints accept the supervised child without any manual
    secret syncing. It only lives for this server run.
    """
    token = os.environ.get("CHAT_BRIDGE_TOKEN", "").strip()
    if not token:
        token = secrets.token_hex(32)
        os.environ["CHAT_BRIDGE_TOKEN"] = token
        logger.info(
            "[chat-bridge] CHAT_BRIDGE_TOKEN not set; generated an ephemeral "
            "token for this server run (set it in .env to keep it stable)."
        )
    return token


class _BridgeEntry:
    """Mutable per-session supervisor state."""

    __slots__ = (
        "session_id", "proc", "task", "desired", "state", "retries",
        "last_error", "last_stderr", "log", "started_at",
    )

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.desired = False
        # "stopped" | "starting" | "backoff" | "failed"
        # ("running" is derived: process alive + bridge websocket connected)
        self.state = "stopped"
        self.retries = 0
        self.last_error = ""
        self.last_stderr = ""
        self.log: deque[str] = deque(maxlen=_LOG_RING_SIZE)
        self.started_at = 0.0


class ChatBridgeSupervisor:
    MAX_RETRIES = 5
    BACKOFF_BASE_SECONDS = 1.0   # 1, 2, 4, 8, 16 (capped below)
    BACKOFF_CAP_SECONDS = 30.0
    STABLE_RUN_SECONDS = 60.0    # a run this long resets the retry counter

    def __init__(self):
        self._entries: dict[str, _BridgeEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── public API ───────────────────────────────────────────────────────────

    async def ensure_started(self, session_id: str, *, force: bool = False) -> dict:
        """Spawn the bridge for this session if it should be running.

        Idempotent and safe to fire on every DM connect / status poll: it
        no-ops when the bridge is already starting/running, when the session
        has no connected+enabled Twitch channel, or when an externally started
        bridge (manual `npm start`) is already connected. A "failed" bridge
        stays failed until force=True (explicit DM action) so status polls
        can't reanimate a crash loop.
        """
        try:
            async with self._lock(session_id):
                entry = self._entries.setdefault(session_id, _BridgeEntry(session_id))
                if entry.task and not entry.task.done() and entry.desired:
                    return self.status(session_id)
                if entry.state == "failed" and not force:
                    return self.status(session_id)
                if not self._session_wants_bridge(session_id):
                    entry.desired = False
                    return self.status(session_id)
                if self._external_bridge_connected(session_id):
                    # Development flow: a manually started bridge already owns
                    # this session — don't double-spawn.
                    entry.desired = False
                    entry.state = "stopped"
                    self._log_line(entry, "[supervisor] external bridge already connected; not spawning")
                    return self.status(session_id)
                entry.desired = True
                entry.retries = 0
                if force:
                    entry.last_error = ""
                entry.state = "starting"
                entry.task = asyncio.create_task(self._run(entry))
                return self.status(session_id)
        except Exception:
            logger.exception("[chat-bridge] ensure_started failed for session %s", session_id)
            return self.status(session_id)

    async def stop(self, session_id: str, *, reason: str = "") -> dict:
        """Terminate the supervised bridge (process tree) for this session."""
        try:
            async with self._lock(session_id):
                entry = self._entries.get(session_id)
                if entry is None:
                    return self.status(session_id)
                entry.desired = False
                if entry.proc is not None and entry.proc.returncode is None:
                    self._log_line(entry, f"[supervisor] stopping bridge{': ' + reason if reason else ''}")
                    await self._terminate_tree(entry.proc)
                task = entry.task
                if task is not None and not task.done():
                    if entry.proc is None:
                        # In backoff sleep (or pre-spawn) — cancel outright.
                        task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=8)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        task.cancel()
                    except Exception:
                        pass
                entry.state = "stopped"
                return self.status(session_id)
        except Exception:
            logger.exception("[chat-bridge] stop failed for session %s", session_id)
            return self.status(session_id)

    async def restart(self, session_id: str) -> dict:
        """DM-requested restart: full stop, then a fresh start (resets retries)."""
        await self.stop(session_id, reason="restart requested")
        return await self.ensure_started(session_id, force=True)

    async def shutdown_all(self) -> None:
        """Server shutdown: terminate every supervised bridge."""
        for session_id in list(self._entries.keys()):
            await self.stop(session_id, reason="server shutting down")

    def status(self, session_id: str) -> dict:
        """Supervisor view of the bridge for the panel / status endpoints."""
        entry = self._entries.get(session_id)
        connected = self._external_bridge_connected(session_id)
        proc_alive = bool(entry and entry.proc is not None and entry.proc.returncode is None)

        if proc_alive:
            state, managed = ("running" if connected else "starting"), True
        elif (
            entry is not None
            and entry.state in ("starting", "backoff")
            and entry.task is not None
            and not entry.task.done()
        ):
            # Spawn scheduled but the process isn't up yet, or in backoff sleep.
            state, managed = "starting", True
        elif connected:
            # A bridge is connected but we're not running one — manual `npm start`.
            state, managed = "running", False
        elif entry is not None and entry.state == "failed":
            state, managed = "failed", False
        else:
            state, managed = "stopped", False

        return {
            "state": state,
            "managed": managed,
            "connected": connected,
            "retries": entry.retries if entry else 0,
            "max_retries": self.MAX_RETRIES,
            "last_error": entry.last_error if entry else "",
            "pid": entry.proc.pid if proc_alive else None,
            "log": list(entry.log)[-_LOG_TAIL_SIZE:] if entry else [],
        }

    # ── internals ────────────────────────────────────────────────────────────

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        return lock

    def _session_wants_bridge(self, session_id: str) -> bool:
        """True when the campaign has a connected Twitch channel and the chat
        bridge toggle is enabled."""
        try:
            from server.twitch_oauth_routes import _get_twitch_row
            row = _get_twitch_row(session_id) or {}
            return bool(row.get("twitch_channel")) and bool(row.get("twitch_chat_enabled", 1))
        except Exception:
            logger.exception("[chat-bridge] could not read Twitch state for session %s", session_id)
            return False

    def _external_bridge_connected(self, session_id: str) -> bool:
        """True when any chat-bridge websocket is live for this session."""
        try:
            from server.connections import manager
            return manager.is_connected(session_id, _BRIDGE_USER_ID)
        except Exception:
            return False

    def _build_command(self) -> list[str] | None:
        """Resolve the child argv, or None when no Node.js runtime is found.

        Node is invoked directly (never via npm): on Windows npm wraps the
        real node process in a cmd shell, which breaks clean tree termination.
        """
        candidate = os.environ.get("CHAT_BRIDGE_NODE", "").strip() or "node"
        resolved = shutil.which(candidate)
        if not resolved and Path(candidate).is_file():
            resolved = candidate
        if not resolved:
            return None
        return [resolved, "src/index.js"]

    def _child_env(self, session_id: str) -> dict[str, str]:
        env = os.environ.copy()
        # dotenv.config() in the bridge does NOT override existing process env,
        # so these always win over stale values in chat-bridge/.env.
        env["GAME_SERVER_WS_URL"] = _game_server_ws_url()
        env["GAME_SESSION_ID"] = session_id
        env["CHAT_BRIDGE_TOKEN"] = _ensure_bridge_token()
        # A supervised bridge must talk to real Twitch, never the mock REPL.
        env.pop("MOCK_CHAT", None)
        return env

    def _log_line(self, entry: _BridgeEntry, line: str) -> None:
        stamped = f"{time.strftime('%H:%M:%S')} {line}"
        entry.log.append(stamped)
        logger.info("[chat-bridge][%s] %s", entry.session_id[:8], line)

    async def _pump(self, entry: _BridgeEntry, stream: asyncio.StreamReader | None, is_stderr: bool) -> None:
        if stream is None:
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                if is_stderr:
                    entry.last_stderr = line
                self._log_line(entry, line)
        except Exception:
            return

    async def _run(self, entry: _BridgeEntry) -> None:
        """Spawn + supervise loop: restart on unexpected exit with capped backoff."""
        try:
            while entry.desired:
                cmd = self._build_command()
                if cmd is None:
                    entry.state = "failed"
                    entry.last_error = (
                        "Node.js was not found on this server. Install Node.js 18+ "
                        "(https://nodejs.org) or set CHAT_BRIDGE_NODE to the node executable."
                    )
                    self._log_line(entry, f"[supervisor] {entry.last_error}")
                    return

                spawn_kwargs: dict = {}
                if os.name == "nt":
                    spawn_kwargs["creationflags"] = (
                        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                    )
                else:
                    # Own process group so termination can sweep any children.
                    spawn_kwargs["start_new_session"] = True

                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(CHAT_BRIDGE_DIR),
                        env=self._child_env(entry.session_id),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        **spawn_kwargs,
                    )
                except FileNotFoundError:
                    entry.state = "failed"
                    entry.last_error = (
                        "Node.js was not found on this server. Install Node.js 18+ "
                        "(https://nodejs.org) or set CHAT_BRIDGE_NODE to the node executable."
                    )
                    self._log_line(entry, f"[supervisor] {entry.last_error}")
                    return
                except Exception as exc:
                    entry.state = "failed"
                    entry.last_error = f"Could not spawn chat bridge: {exc}"
                    self._log_line(entry, f"[supervisor] {entry.last_error}")
                    return

                entry.proc = proc
                entry.state = "starting"
                entry.started_at = time.monotonic()
                self._log_line(entry, f"[supervisor] spawned chat bridge (pid {proc.pid})")

                pumps = [
                    asyncio.create_task(self._pump(entry, proc.stdout, is_stderr=False)),
                    asyncio.create_task(self._pump(entry, proc.stderr, is_stderr=True)),
                ]
                returncode = await proc.wait()
                await asyncio.gather(*pumps, return_exceptions=True)
                run_seconds = time.monotonic() - entry.started_at
                entry.proc = None

                if not entry.desired:
                    entry.state = "stopped"
                    return

                # Unexpected exit — schedule a restart with capped backoff.
                if run_seconds >= self.STABLE_RUN_SECONDS:
                    entry.retries = 0
                entry.retries += 1
                entry.last_error = (
                    entry.last_stderr
                    or (entry.log[-1].split(" ", 1)[-1] if entry.log else "")
                    or f"chat bridge exited with code {returncode}"
                )

                if entry.retries > self.MAX_RETRIES:
                    entry.state = "failed"
                    entry.desired = False
                    self._log_line(
                        entry,
                        f"[supervisor] bridge exited with code {returncode}; giving up after "
                        f"{self.MAX_RETRIES} restarts. Last error: {entry.last_error}",
                    )
                    return

                delay = min(
                    self.BACKOFF_CAP_SECONDS,
                    self.BACKOFF_BASE_SECONDS * (2 ** (entry.retries - 1)),
                )
                entry.state = "backoff"
                self._log_line(
                    entry,
                    f"[supervisor] bridge exited with code {returncode}; "
                    f"restart {entry.retries}/{self.MAX_RETRIES} in {delay:g}s",
                )
                await asyncio.sleep(delay)

                if not entry.desired:
                    entry.state = "stopped"
                    return
                if not self._session_wants_bridge(entry.session_id):
                    entry.desired = False
                    entry.state = "stopped"
                    return
                if self._external_bridge_connected(entry.session_id):
                    entry.desired = False
                    entry.state = "stopped"
                    self._log_line(entry, "[supervisor] external bridge connected during backoff; not respawning")
                    return
        except asyncio.CancelledError:
            entry.state = "stopped"
            raise
        except Exception:
            # Never let supervisor bugs escape into the server.
            logger.exception("[chat-bridge] supervisor loop crashed for session %s", entry.session_id)
            entry.state = "failed"
            entry.last_error = entry.last_error or "Bridge supervisor crashed; see server log."

    async def _terminate_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate the child and everything it spawned, Windows-safe.

        POSIX: SIGTERM the process group (the bridge handles SIGTERM and
        disconnects cleanly), escalating to SIGKILL. Windows: taskkill /T /F —
        node has no usable SIGTERM there, and killing only the parent would
        orphan any children.
        """
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, FileNotFoundError, OSError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=6)
            return
        except asyncio.TimeoutError:
            pass

        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=4)
        except asyncio.TimeoutError:
            logger.warning("[chat-bridge] pid %s did not exit after SIGKILL", proc.pid)


# Singleton used by routes and main.py.
bridge_supervisor = ChatBridgeSupervisor()
