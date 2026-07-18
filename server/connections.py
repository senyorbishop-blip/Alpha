"""
server/connections.py — WebSocket connection registry and broadcaster
"""
import asyncio
import json
import logging
import os
import time
import uuid
import zlib
from server.payload_diagnostics import log_payload_size_diagnostic, log_top_level_payload_keys
from typing import Dict, Set, Optional
from fastapi import WebSocket

logger = logging.getLogger(__name__)

PAYLOAD_WARN_BYTES = 128 * 1024
PAYLOAD_ERROR_BYTES = 512 * 1024
SLOW_SEND_WARN_MS = 250.0

# Oversized frames (e.g. a 5 MB state_sync) otherwise log on EVERY send and on
# every reconnect. Log each (session, message_type) at most once per interval.
_PAYLOAD_LOG_INTERVAL_S = 30.0
_payload_log_last: dict[tuple[str, str], float] = {}


def _payload_log_allowed(session_id: str, message_type: str) -> bool:
    key = (session_id, message_type or "unknown")
    now = time.monotonic()
    if now - _payload_log_last.get(key, 0.0) >= _PAYLOAD_LOG_INTERVAL_S:
        _payload_log_last[key] = now
        return True
    return False

# A single send must never be allowed to block the broadcaster indefinitely.
# A half-open socket (sleeping laptop, dropped wifi, backgrounded phone, NAT
# idle-timeout) does not fail fast: once the transport buffer fills, send_text
# awaits a drain that never completes. Without a bound that one dead peer
# freezes live sync for the whole session. The timeout is generous enough for
# large initial syncs over slow-but-alive links yet well under the 60s
# heartbeat timeout, so it only ever reaps genuinely wedged sockets.
DEFAULT_SEND_TIMEOUT_SECONDS = 10.0
MIN_SEND_TIMEOUT_SECONDS = 1.0

# Large frames get proportionally more time on top of the flat base timeout.
# A flat 10s budget sized for small frames would reap a healthy-but-slow
# client mid state_sync: at ~128 KiB/s of sustained goodput (a mediocre
# residential link) a 1 MiB frame needs ~8s of pure transfer time before any
# RTT/scheduling slack. The extra allowance is capped well under the 60s
# heartbeat timeout so a wedged socket is still reaped promptly.
SEND_TIMEOUT_THROUGHPUT_FLOOR_BYTES_PER_S = 128 * 1024
MAX_SEND_TIMEOUT_SECONDS = 30.0

# Bound for the best-effort close of a reaped socket. Closing a wedged
# transport can itself hang, and the close runs inside the broadcast path,
# so it must never stall live sync for the healthy recipients.
REAP_CLOSE_TIMEOUT_SECONDS = 3.0


def send_timeout_seconds() -> float:
    raw = os.environ.get("WS_SEND_TIMEOUT_SECONDS")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_SEND_TIMEOUT_SECONDS
    try:
        value = float(str(raw).strip())
    except Exception:
        return DEFAULT_SEND_TIMEOUT_SECONDS
    return max(MIN_SEND_TIMEOUT_SECONDS, value)


def send_timeout_seconds_for(byte_size: int) -> float:
    """Per-send timeout scaled by frame size (see the throughput-floor note above).

    An operator-configured base above the cap is always respected — the cap only
    limits the size-based extension, never shrinks an explicit override.
    """
    base = send_timeout_seconds()
    try:
        extra = max(0.0, float(byte_size)) / SEND_TIMEOUT_THROUGHPUT_FLOOR_BYTES_PER_S
    except Exception:
        extra = 0.0
    return max(base, min(MAX_SEND_TIMEOUT_SECONDS, base + extra))


def _socket_is_open(websocket) -> bool:
    """Best-effort check of whether a WebSocket is still connected.

    Used only for diagnostic logging when an old socket is replaced, so it must
    never raise. Falls back to ``True`` when the socket's state is unknowable
    (e.g. test doubles without ``client_state``)."""
    try:
        from starlette.websockets import WebSocketState
        state = getattr(websocket, "client_state", None)
        if state is None:
            return True
        return state == WebSocketState.CONNECTED
    except Exception:
        return True


class ConnectionManager:
    def __init__(self):
        self._connections: Dict[str, Dict[str, WebSocket]] = {}
        self._connection_ids: Dict[str, Dict[str, str]] = {}
        self._roles: Dict[str, Dict[str, str]] = {}
        # Per-connection outbound sequence numbers (reset whenever a user's
        # socket is (re)established). Every pushed frame carries the next value
        # so the client can detect a missed push (a reaped-then-reconnected or
        # otherwise lossy window) and repair via the request_state delta path.
        self._send_seqs: Dict[str, Dict[str, int]] = {}

    def get_session_connections(self, session_id: str) -> Dict[str, WebSocket]:
        return self._connections.get(session_id, {})

    async def connect(self, session_id: str, user_id: str, websocket: WebSocket, role: str | None = None, connection_id: str | None = None, client_socket_id: str | None = None, reason: str | None = None, user_agent: str | None = None) -> str:
        await websocket.accept()
        if session_id not in self._connections:
            self._connections[session_id] = {}
        if session_id not in self._connection_ids:
            self._connection_ids[session_id] = {}
        if session_id not in self._roles:
            self._roles[session_id] = {}
        new_connection_id = connection_id or uuid.uuid4().hex
        prior = self._connections[session_id].get(user_id)
        old_connection_id = self._connection_ids.get(session_id, {}).get(user_id)
        old_replaced = bool(prior and prior is not websocket)
        if old_replaced:
            # Temporary diagnostic logging for the reconnect-storm investigation:
            # correlate the replaced socket with the client-side client_socket_id,
            # the connect reason, and the browser so duplicate owners are traceable.
            logger.warning(
                "[ws] replacing socket session_id=%s user_id=%s role=%s "
                "old_connection_id=%s new_connection_id=%s old_socket_open=%s "
                "client_socket_id=%s reason=%s user_agent=%s "
                "old_socket_replaced=true new_socket_connected=false",
                session_id, user_id, role or "unknown",
                old_connection_id or "unknown", new_connection_id,
                _socket_is_open(prior),
                client_socket_id or "unknown", reason or "unknown", user_agent or "unknown",
            )
            try:
                await prior.close(code=1001, reason="Replaced by a newer connection")
            except Exception as exc:
                logger.warning(
                    "[ws] old socket close failed during replacement session_id=%s user_id=%s role=%s error=%s",
                    session_id, user_id, role or "unknown", exc,
                )
        self._connections[session_id][user_id] = websocket
        self._connection_ids[session_id][user_id] = new_connection_id
        self._roles[session_id][user_id] = role or "unknown"
        # Fresh socket ⇒ fresh outbound seq stream. The client resets its own
        # tracker per socket, so the first push on any connection is seq 1.
        self._send_seqs.setdefault(session_id, {})[user_id] = 0
        logger.info(
            "[ws] connected session_id=%s user_id=%s role=%s connection_id=%s "
            "client_socket_id=%s reason=%s old_socket_replaced=%s new_socket_connected=true",
            session_id, user_id, role or "unknown", new_connection_id,
            client_socket_id or "unknown", reason or "unknown", old_replaced,
        )
        return new_connection_id

    def disconnect(self, session_id: str, user_id: str, websocket: Optional[WebSocket] = None) -> bool:
        if session_id in self._connections:
            if websocket is not None:
                current = self._connections[session_id].get(user_id)
                if current is not websocket:
                    return False
            self._connections[session_id].pop(user_id, None)
            if session_id in self._connection_ids:
                self._connection_ids[session_id].pop(user_id, None)
                if not self._connection_ids[session_id]:
                    del self._connection_ids[session_id]
            if session_id in self._roles:
                self._roles[session_id].pop(user_id, None)
                if not self._roles[session_id]:
                    del self._roles[session_id]
            if session_id in self._send_seqs:
                self._send_seqs[session_id].pop(user_id, None)
                if not self._send_seqs[session_id]:
                    del self._send_seqs[session_id]
            if not self._connections[session_id]:
                del self._connections[session_id]
            return True
        return False

    def _role_for(self, session_id: str, user_id: str, session_obj=None, fallback: str | None = None) -> str:
        if fallback:
            return fallback
        if session_obj is not None:
            try:
                user = getattr(session_obj, "users", {}).get(user_id)
                role = getattr(user, "role", None)
                if role:
                    return str(role)
            except Exception:
                pass
        return self._roles.get(session_id, {}).get(user_id) or "unknown"

    def _encode_payload(self, message: dict) -> tuple[str, int]:
        payload = json.dumps(message)
        return payload, len(payload.encode("utf-8"))

    def _next_seq(self, session_id: str, user_id: str) -> int:
        seqs = self._send_seqs.setdefault(session_id, {})
        seqs[user_id] = int(seqs.get(user_id, 0)) + 1
        return seqs[user_id]

    def _stamp_seq_payload(self, payload: str, byte_size: int, seq: int) -> tuple[str, int]:
        """Splice a per-connection ``seq`` into an already-encoded frame.

        Broadcast paths intentionally ``json.dumps`` a shared payload once; the
        seq differs per recipient, so it is inserted into the encoded string
        instead of re-serializing the (potentially multi-hundred-KB) message
        for every recipient. The suffix is pure ASCII, so the UTF-8 byte size
        grows by exactly ``len(suffix) - 1`` (the replaced ``}``).
        """
        if not payload.endswith("}"):
            return payload, byte_size
        suffix = ("," if len(payload) > 2 else "") + '"seq":%d}' % seq
        return payload[:-1] + suffix, byte_size + len(suffix) - 1

    def _seq_stamped_send(
        self,
        session_id: str,
        user_id: str,
        ws: WebSocket,
        payload: str,
        byte_size: int,
        *,
        role: str,
        message_type: str,
    ):
        """Build one recipient's send coroutine with its seq stamped in."""
        seq = self._next_seq(session_id, user_id)
        stamped, stamped_size = self._stamp_seq_payload(payload, byte_size, seq)
        return self._send_payload(
            ws,
            stamped,
            session_id=session_id,
            user_id=user_id,
            role=role,
            message_type=message_type,
            byte_size=stamped_size,
        )

    async def _close_reaped(self, session_id: str, user_id: str, ws: WebSocket) -> None:
        """Best-effort close of a socket reaped after a failed/timed-out send.

        Without this the client's browser still sees an OPEN socket: its
        ``onclose`` never fires, auto-reconnect never runs, and the player
        silently receives nothing until a manual relaunch. Closing (1011 —
        internal error) makes the client notice and reconnect via the cheap
        delta path. Both the close and any error are swallowed: the socket is
        usually already dead, and a wedged close must not stall the broadcast
        that reaped it.
        """
        try:
            await asyncio.wait_for(
                ws.close(code=1011, reason="Server send failed"),
                timeout=REAP_CLOSE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.debug(
                "[ws] reaped socket close failed session_id=%s user_id=%s error=%r",
                session_id, user_id, exc,
            )

    def _log_send_diagnostic(
        self,
        *,
        session_id: str,
        user_id: str,
        role: str,
        message_type: str,
        byte_size: int,
        duration_ms: float,
    ) -> None:
        # Privacy note: intentionally log metadata only. Never include raw payload
        # fields because outbound frames may contain hidden tokens, private notes,
        # profile contents, handout text, or other player/viewer-sensitive data.
        log_args = (message_type or "unknown", session_id, user_id, role or "unknown", byte_size, duration_ms)
        log_message = (
            "[ws] outbound_send message_type=%s session_id=%s recipient_user_id=%s "
            "recipient_role=%s byte_size=%s duration_ms=%.2f"
        )
        if byte_size > PAYLOAD_WARN_BYTES:
            if _payload_log_allowed(session_id, message_type):
                if byte_size > PAYLOAD_ERROR_BYTES:
                    logger.error(log_message, *log_args)
                else:
                    logger.warning(log_message, *log_args)
                log_payload_size_diagnostic(
                    logger,
                    session_id=session_id,
                    recipient_user_id=user_id,
                    recipient_role=role or "unknown",
                    message_type=message_type or "unknown",
                    byte_size=byte_size,
                )
        else:
            logger.debug(log_message, *log_args)
        if duration_ms > SLOW_SEND_WARN_MS:
            logger.warning(
                "[ws] outbound_send_slow message_type=%s session_id=%s recipient_user_id=%s "
                "recipient_role=%s byte_size=%s duration_ms=%.2f threshold_ms=%.2f",
                message_type or "unknown",
                session_id,
                user_id,
                role or "unknown",
                byte_size,
                duration_ms,
                SLOW_SEND_WARN_MS,
            )

    async def _send_payload(
        self,
        ws: WebSocket,
        payload: str,
        *,
        session_id: str,
        user_id: str,
        role: str,
        message_type: str,
        byte_size: int,
    ) -> None:
        started = time.perf_counter()
        timed_out = False
        # Scaled by frame size so a large-but-progressing sync over a mediocre
        # link is not reaped as if it were a wedged socket.
        timeout_s = send_timeout_seconds_for(byte_size)
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=timeout_s)
        except asyncio.TimeoutError:
            # The socket is wedged (half-open TCP / unresponsive peer). Surface it
            # as a send failure so the caller reaps the connection instead of
            # letting one dead recipient stall live sync for everyone else.
            timed_out = True
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            if timed_out:
                logger.warning(
                    "[ws] outbound_send_timeout message_type=%s session_id=%s recipient_user_id=%s "
                    "recipient_role=%s byte_size=%s duration_ms=%.2f timeout_s=%.2f",
                    message_type or "unknown", session_id, user_id, role or "unknown",
                    byte_size, duration_ms, timeout_s,
                )
            self._log_send_diagnostic(
                session_id=session_id, user_id=user_id, role=role,
                message_type=message_type, byte_size=byte_size, duration_ms=duration_ms,
            )

    def _log_deflate_estimate(self, session_id: str, user_id: str, message_type: str, payload: str, byte_size: int) -> None:
        """For oversized frames, log the approximate on-the-wire size under
        permessage-deflate so raw-vs-compressed can be compared in one place.
        Only runs on frames already past the warn threshold (rate-limited by
        the caller), so the extra zlib pass costs nothing in the hot path."""
        if byte_size <= PAYLOAD_WARN_BYTES:
            return
        try:
            deflate_bytes = len(zlib.compress(payload.encode("utf-8"), 6))
        except Exception:
            return
        logger.warning(
            "[ws] outbound_send_deflate_estimate message_type=%s session_id=%s recipient_user_id=%s "
            "byte_size=%s deflate_estimate_bytes=%s ratio=%.1fx",
            message_type or "unknown", session_id, user_id, byte_size, deflate_bytes,
            (byte_size / deflate_bytes) if deflate_bytes else 0.0,
        )

    async def send_to(self, session_id: str, user_id: str, message: dict) -> bool:
        ws = self._connections.get(session_id, {}).get(user_id)
        if ws:
            try:
                payload, byte_size = self._encode_payload(message)
                if _payload_log_allowed(session_id, str(message.get("type") or "unknown") + ":breakdown"):
                    log_top_level_payload_keys(logger, session_id=session_id, recipient_user_id=user_id, recipient_role=self._role_for(session_id, user_id), message=message, byte_size=byte_size)
                    self._log_deflate_estimate(session_id, user_id, str(message.get("type") or "unknown"), payload, byte_size)
                await self._seq_stamped_send(
                    session_id,
                    user_id,
                    ws,
                    payload,
                    byte_size,
                    role=self._role_for(session_id, user_id),
                    message_type=str(message.get("type") or "unknown"),
                )
                return True
            except Exception:
                logger.warning("[ws] send_to failed session_id=%s user_id=%s message_type=%s", session_id, user_id, message.get("type"))
                self.disconnect(session_id, user_id, ws)
                # Close the dead socket so the client's onclose fires and its
                # auto-reconnect kicks in — otherwise it stays silently stale.
                await self._close_reaped(session_id, user_id, ws)
        return False

    async def _gather_sends(self, session_id: str, sends: list) -> None:
        """Deliver many sends concurrently and reap whichever sockets failed.

        ``sends`` is a list of ``(uid, ws, coroutine)`` tuples. Running them
        concurrently (rather than awaiting each in turn) is what prevents a
        single slow or wedged recipient from adding head-of-line latency to
        every other client's live sync. Each coroutine is already bounded by the
        per-send timeout in ``_send_payload``; any that fail (timeout, transport
        error) have their socket removed from the registry so the next broadcast
        skips them, and are then explicitly closed — a reaped-but-never-closed
        socket looks OPEN to the browser, so its onclose/auto-reconnect never
        fires and the player silently stops receiving updates.
        """
        if not sends:
            return
        results = await asyncio.gather(*(coro for _, _, coro in sends), return_exceptions=True)
        for (uid, ws, _), result in zip(sends, results):
            if isinstance(result, asyncio.CancelledError):
                # Preserve cancellation semantics — do not swallow a cancel.
                raise result
            if isinstance(result, BaseException):
                self.disconnect(session_id, uid, ws)
                await self._close_reaped(session_id, uid, ws)

    async def broadcast(self, session_id: str, message: dict, exclude_user: Optional[str] = None):
        connections = dict(self._connections.get(session_id, {}))
        payload, byte_size = self._encode_payload(message)
        message_type = str(message.get("type") or "unknown")
        sends = [
            (uid, ws, self._seq_stamped_send(
                session_id,
                uid,
                ws,
                payload,
                byte_size,
                role=self._role_for(session_id, uid),
                message_type=message_type,
            ))
            for uid, ws in connections.items()
            if uid != exclude_user
        ]
        await self._gather_sends(session_id, sends)

    async def broadcast_to_role(self, session_id: str, message: dict, roles: Set[str], session_obj):
        connections = dict(self._connections.get(session_id, {}))
        payload, byte_size = self._encode_payload(message)
        message_type = str(message.get("type") or "unknown")
        sends = []
        for uid, ws in connections.items():
            user = session_obj.users.get(uid)
            if user and user.role in roles:
                sends.append((uid, ws, self._seq_stamped_send(
                    session_id,
                    uid,
                    ws,
                    payload,
                    byte_size,
                    role=self._role_for(session_id, uid, session_obj=session_obj),
                    message_type=message_type,
                )))
        await self._gather_sends(session_id, sends)

    async def broadcast_filtered(self, session_id: str, message: dict,
                                 hide_hidden_tokens: bool = False, dm_id: str = None,
                                 session_obj=None):
        """Broadcast with token visibility filtering.

        Hidden tokens are replaced with a token_removed_hidden notification for
        non-DM recipients.  DM-created tokens (no owner_id) ARE visible to all
        non-DM users unless they are explicitly marked hidden — the original
        implementation incorrectly dropped ownerless tokens for non-DMs.

        NOTE: This method is currently unused; all token-specific broadcasts use
        _broadcast_token_event() in handlers/common.py which applies per-user
        visibility filtering.  Kept for potential future use.
        """
        connections = dict(self._connections.get(session_id, {}))
        dm_payload, dm_byte_size = self._encode_payload(message)
        message_type = str(message.get("type") or "unknown")
        sends = []
        for uid, ws in connections.items():
            is_dm = (uid == dm_id)
            if not is_dm and hide_hidden_tokens:
                payload_data = message.get("payload", {})
                token = payload_data.get("token") if isinstance(payload_data, dict) else None
                if token and token.get("hidden"):
                    alt_message = {"type": "token_removed_hidden", "payload": {"id": token["id"]}}
                    alt, alt_byte_size = self._encode_payload(alt_message)
                    sends.append((uid, ws, self._seq_stamped_send(
                        session_id,
                        uid,
                        ws,
                        alt,
                        alt_byte_size,
                        role=self._role_for(session_id, uid, session_obj=session_obj),
                        message_type="token_removed_hidden",
                    )))
                    continue
                # Note: tokens without owner_id are DM-created NPCs and ARE
                # visible to players (unless hidden=True, handled above).
            sends.append((uid, ws, self._seq_stamped_send(
                session_id,
                uid,
                ws,
                dm_payload,
                dm_byte_size,
                role=self._role_for(session_id, uid, session_obj=session_obj),
                message_type=message_type,
            )))
        await self._gather_sends(session_id, sends)

    def is_connected(self, session_id: str, user_id: str) -> bool:
        return user_id in self._connections.get(session_id, {})

    def get_socket(self, session_id: str, user_id: str) -> Optional[WebSocket]:
        return self._connections.get(session_id, {}).get(user_id)

    def get_connection_id(self, session_id: str, user_id: str) -> Optional[str]:
        return self._connection_ids.get(session_id, {}).get(user_id)

    def is_current_connection(self, session_id: str, user_id: str, connection_id: str) -> bool:
        return self.get_connection_id(session_id, user_id) == connection_id

    def get_active_session_ids(self) -> list:
        """Return a snapshot of all currently active session IDs."""
        return list(self._connections.keys())


manager = ConnectionManager()
