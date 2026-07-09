"""Tests for the chat-bridge process supervisor (auto-start / backoff / stop).

The real bridge is a Node process; these tests substitute small Python child
processes via _build_command so they exercise the actual spawn / pump /
terminate / backoff machinery without needing npm dependencies.
"""
import asyncio
import os
import sys

from server.chat_bridge_supervisor import (
    ChatBridgeSupervisor,
    _ensure_bridge_token,
)

# Child that starts, prints a line, and stays up (a "healthy" bridge).
LONG_RUNNING_CMD = [
    sys.executable, "-u", "-c",
    "import time, sys; print('bridge up'); sys.stdout.flush(); time.sleep(60)",
]

# Child that prints to stderr and exits non-zero (a crashing bridge).
CRASHING_CMD = [
    sys.executable, "-u", "-c",
    "import sys; print('boom: missing creds', file=sys.stderr); sys.exit(3)",
]


def make_supervisor(*, cmd=LONG_RUNNING_CMD, wants=True, external=False,
                    max_retries=2, backoff=0.01):
    sup = ChatBridgeSupervisor()
    sup.MAX_RETRIES = max_retries
    sup.BACKOFF_BASE_SECONDS = backoff
    sup.BACKOFF_CAP_SECONDS = backoff * 4
    sup.STABLE_RUN_SECONDS = 999  # never reset retries in tests
    sup._session_wants_bridge = lambda sid: wants
    sup._external_bridge_connected = lambda sid: external
    sup._build_command = lambda: cmd
    return sup


async def _wait_for(predicate, timeout=8.0, interval=0.03):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Spawn / status / stop
# ---------------------------------------------------------------------------

def test_spawn_pipes_output_and_stop_terminates():
    async def run():
        sup = make_supervisor()
        status = await sup.ensure_started("sess1")
        assert status["state"] == "starting"
        assert status["managed"] is True

        # Child stdout lands in the ring buffer.
        assert await _wait_for(
            lambda: any("bridge up" in line for line in sup.status("sess1")["log"])
        )
        status = sup.status("sess1")
        assert status["state"] == "starting"  # alive but no bridge WS connected
        assert status["pid"]

        stopped = await sup.stop("sess1", reason="test")
        assert stopped["state"] == "stopped"
        entry = sup._entries["sess1"]
        assert entry.proc is None
        assert entry.task.done()
    asyncio.run(run())


def test_running_state_when_bridge_websocket_connected():
    async def run():
        sup = make_supervisor()
        await sup.ensure_started("sess1")
        assert await _wait_for(lambda: sup.status("sess1")["pid"] is not None)
        # Simulate the bridge's chat_bridge WS connecting to the session.
        sup._external_bridge_connected = lambda sid: True
        status = sup.status("sess1")
        assert status["state"] == "running"
        assert status["managed"] is True
        sup._external_bridge_connected = lambda sid: False
        await sup.stop("sess1")
    asyncio.run(run())


def test_double_start_does_not_double_spawn():
    async def run():
        sup = make_supervisor()
        await asyncio.gather(
            sup.ensure_started("sess1"),
            sup.ensure_started("sess1"),
        )
        assert await _wait_for(lambda: sup.status("sess1")["pid"] is not None)
        pid = sup.status("sess1")["pid"]
        await sup.ensure_started("sess1")
        assert sup.status("sess1")["pid"] == pid
        await sup.stop("sess1")
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Preconditions: toggle off, external bridge, missing node
# ---------------------------------------------------------------------------

def test_disabled_session_never_spawns():
    async def run():
        sup = make_supervisor(wants=False)
        status = await sup.ensure_started("sess1")
        assert status["state"] == "stopped"
        assert sup._entries["sess1"].task is None
    asyncio.run(run())


def test_external_bridge_suppresses_spawn():
    async def run():
        sup = make_supervisor(external=True)
        status = await sup.ensure_started("sess1")
        assert status["state"] == "running"
        assert status["managed"] is False  # manual `npm start` owns the session
        assert sup._entries["sess1"].proc is None
    asyncio.run(run())


def test_missing_node_marks_failed_with_clear_error():
    async def run():
        sup = make_supervisor(cmd=None)
        await sup.ensure_started("sess1")
        entry = sup._entries["sess1"]
        await asyncio.wait_for(entry.task, timeout=5)
        status = sup.status("sess1")
        assert status["state"] == "failed"
        assert "Node.js" in status["last_error"]
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Crash loop → capped backoff → failed; restart recovers
# ---------------------------------------------------------------------------

def test_crash_loop_backs_off_then_fails():
    async def run():
        sup = make_supervisor(cmd=CRASHING_CMD, max_retries=2)
        await sup.ensure_started("sess1")
        entry = sup._entries["sess1"]
        await asyncio.wait_for(entry.task, timeout=15)
        status = sup.status("sess1")
        assert status["state"] == "failed"
        assert status["retries"] > sup.MAX_RETRIES
        assert "boom" in status["last_error"]  # stderr line surfaced

        # A failed bridge stays failed on ordinary ensure_started (no zombie loop)…
        status = await sup.ensure_started("sess1")
        assert status["state"] == "failed"

        # …but an explicit restart resets and recovers.
        sup._build_command = lambda: LONG_RUNNING_CMD
        status = await sup.restart("sess1")
        assert status["state"] == "starting"
        assert await _wait_for(lambda: sup.status("sess1")["pid"] is not None)
        await sup.stop("sess1")
    asyncio.run(run())


def test_shutdown_all_stops_every_session():
    async def run():
        sup = make_supervisor()
        await sup.ensure_started("sess1")
        await sup.ensure_started("sess2")
        assert await _wait_for(lambda: sup.status("sess1")["pid"] and sup.status("sess2")["pid"])
        await sup.shutdown_all()
        assert sup.status("sess1")["state"] == "stopped"
        assert sup.status("sess2")["state"] == "stopped"
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Child environment injection
# ---------------------------------------------------------------------------

def test_child_env_injects_session_vars(monkeypatch):
    monkeypatch.setenv("MOCK_CHAT", "true")
    monkeypatch.setenv("CHAT_BRIDGE_TOKEN", "tok123")
    monkeypatch.setenv("CHAT_BRIDGE_WS_URL", "ws://127.0.0.1:9999")
    sup = ChatBridgeSupervisor()
    env = sup._child_env("sess42")
    assert env["GAME_SESSION_ID"] == "sess42"
    assert env["GAME_SERVER_WS_URL"] == "ws://127.0.0.1:9999"
    assert env["CHAT_BRIDGE_TOKEN"] == "tok123"
    # A supervised bridge must never inherit mock-chat mode.
    assert "MOCK_CHAT" not in env


def test_ephemeral_bridge_token_generated_when_unset(monkeypatch):
    monkeypatch.delenv("CHAT_BRIDGE_TOKEN", raising=False)
    token = _ensure_bridge_token()
    assert token
    # Stored so /api/chat-bridge/auth accepts the supervised child.
    assert os.environ["CHAT_BRIDGE_TOKEN"] == token
    assert _ensure_bridge_token() == token  # stable within the run
