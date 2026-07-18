"""Desync hardening: reaped sockets get closed, every push carries a seq.

Root cause of the mid-session player desync: a send that failed or timed out
only removed the socket from the registry — the browser still saw an OPEN
socket, its ``onclose`` never fired, auto-reconnect never ran, and the player
silently received nothing until a manual relaunch. These tests pin the server
half of the defense-in-depth fix:

  1. close-on-reap — a failed send closes the WebSocket (code 1011) so the
     client notices and reconnects;
  2. per-connection outbound ``seq`` on every push — the client's gap detector
     depends on it being contiguous per recipient and reset per socket;
  3. size-aware per-send timeout — a large-but-progressing sync over a slow
     link must not be reaped as if it were a wedged socket.
"""
import asyncio
import json

import server.connections as connections_module
from server.connections import (
    ConnectionManager,
    DEFAULT_SEND_TIMEOUT_SECONDS,
    MAX_SEND_TIMEOUT_SECONDS,
    send_timeout_seconds_for,
)


class RecordingWebSocket:
    def __init__(self, *, fail_sends=False):
        self.accepted = False
        self.sent = []
        self.close_calls = []
        self.fail_sends = fail_sends

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))

    async def send_text(self, payload):
        if self.fail_sends:
            raise RuntimeError("send failed")
        self.sent.append(payload)

    def seqs(self):
        return [json.loads(frame).get("seq") for frame in self.sent]


class WedgedCloseWebSocket(RecordingWebSocket):
    """Close never completes — mimics a wedged transport during teardown."""

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))
        await asyncio.sleep(3600)


class RaisingCloseWebSocket(RecordingWebSocket):
    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))
        raise RuntimeError("already closed")


# ── Layer 1: close on reap ───────────────────────────────────────────────────

def test_broadcast_send_failure_closes_socket_and_excludes_user_from_next_broadcast():
    async def run_case():
        manager = ConnectionManager()
        healthy = RecordingWebSocket()
        broken = RecordingWebSocket(fail_sends=True)
        await manager.connect("sess", "healthy", healthy, role="player")
        await manager.connect("sess", "broken", broken, role="player")

        await manager.broadcast("sess", {"type": "token_moved", "payload": {"x": 1}})

        # The failed socket is reaped AND closed so the client's onclose fires.
        assert not manager.is_connected("sess", "broken")
        assert broken.close_calls, "reaped socket must be explicitly closed"
        assert broken.close_calls[0][0] == 1011
        assert manager.is_connected("sess", "healthy")

        # The next broadcast excludes the reaped user entirely.
        await manager.broadcast("sess", {"type": "token_moved", "payload": {"x": 2}})
        assert len(healthy.sent) == 2
        assert not broken.sent

    asyncio.run(run_case())


def test_send_to_failure_closes_socket():
    async def run_case():
        manager = ConnectionManager()
        broken = RecordingWebSocket(fail_sends=True)
        await manager.connect("sess", "u", broken, role="player")

        ok = await manager.send_to("sess", "u", {"type": "ping"})

        assert ok is False
        assert not manager.is_connected("sess", "u")
        assert broken.close_calls and broken.close_calls[0][0] == 1011

    asyncio.run(run_case())


def test_reap_close_is_bounded_when_close_itself_wedges(monkeypatch):
    monkeypatch.setattr(connections_module, "REAP_CLOSE_TIMEOUT_SECONDS", 0.1)

    async def run_case():
        manager = ConnectionManager()
        wedged = WedgedCloseWebSocket(fail_sends=True)
        healthy = RecordingWebSocket()
        await manager.connect("sess", "wedged", wedged, role="player")
        await manager.connect("sess", "healthy", healthy, role="player")

        started = asyncio.get_running_loop().time()
        await manager.broadcast("sess", {"type": "chat_message", "payload": {}})
        elapsed = asyncio.get_running_loop().time() - started

        assert healthy.sent, "healthy recipient must still receive the frame"
        assert not manager.is_connected("sess", "wedged")
        assert wedged.close_calls, "close must at least be attempted"
        assert elapsed < 2.0, f"broadcast stalled {elapsed:.2f}s on a wedged close"

    asyncio.run(run_case())


def test_reap_close_errors_are_swallowed():
    async def run_case():
        manager = ConnectionManager()
        broken = RaisingCloseWebSocket(fail_sends=True)
        await manager.connect("sess", "u", broken, role="player")

        # Must not raise even though close() itself errors.
        await manager.broadcast("sess", {"type": "chat_message", "payload": {}})

        assert not manager.is_connected("sess", "u")
        assert broken.close_calls

    asyncio.run(run_case())


def test_reap_close_targets_failed_socket_not_a_newer_replacement():
    async def run_case():
        manager = ConnectionManager()
        new_socket = RecordingWebSocket()

        class ReconnectDuringSend(RecordingWebSocket):
            async def send_text(self, payload):
                await manager.connect("sess", "u", new_socket, role="player")
                raise RuntimeError("send failed")

        old_socket = ReconnectDuringSend()
        await manager.connect("sess", "u", old_socket, role="player")

        await manager.broadcast("sess", {"type": "state_update"})

        # The stale socket is closed; the replacement stays registered and open.
        assert any(code == 1011 for code, _ in old_socket.close_calls)
        assert not any(code == 1011 for code, _ in new_socket.close_calls)
        assert manager.get_socket("sess", "u") is new_socket

    asyncio.run(run_case())


# ── Layer 2: per-connection seq on every push ────────────────────────────────

def test_broadcast_stamps_contiguous_seq_per_recipient():
    async def run_case():
        manager = ConnectionManager()
        a = RecordingWebSocket()
        b = RecordingWebSocket()
        await manager.connect("sess", "a", a, role="dm")
        await manager.connect("sess", "b", b, role="player")

        await manager.broadcast("sess", {"type": "token_moved", "payload": {"x": 1}})
        await manager.broadcast("sess", {"type": "token_moved", "payload": {"x": 2}})

        assert a.seqs() == [1, 2]
        assert b.seqs() == [1, 2]

    asyncio.run(run_case())


def test_targeted_send_does_not_create_gaps_for_other_recipients():
    async def run_case():
        manager = ConnectionManager()
        a = RecordingWebSocket()
        b = RecordingWebSocket()
        await manager.connect("sess", "a", a, role="dm")
        await manager.connect("sess", "b", b, role="player")

        await manager.broadcast("sess", {"type": "chat_message", "payload": {}})
        await manager.send_to("sess", "a", {"type": "private_note", "payload": {}})
        await manager.broadcast("sess", {"type": "chat_message", "payload": {}})

        # Each user's stream is independently contiguous: the targeted send to
        # "a" must not open a hole in "b"'s sequence.
        assert a.seqs() == [1, 2, 3]
        assert b.seqs() == [1, 2]

    asyncio.run(run_case())


def test_seq_resets_for_a_reconnected_socket():
    async def run_case():
        manager = ConnectionManager()
        first = RecordingWebSocket()
        await manager.connect("sess", "u", first, role="player")
        await manager.broadcast("sess", {"type": "chat_message", "payload": {}})
        await manager.broadcast("sess", {"type": "chat_message", "payload": {}})
        assert first.seqs() == [1, 2]

        second = RecordingWebSocket()
        await manager.connect("sess", "u", second, role="player")
        await manager.broadcast("sess", {"type": "chat_message", "payload": {}})

        # A fresh socket starts a fresh stream — the client resets its tracker
        # per socket, so seq 1 (not 3) is what prevents a false gap on open.
        assert second.seqs() == [1]

    asyncio.run(run_case())


def test_stamped_frame_is_valid_json_and_preserves_message_content():
    async def run_case():
        manager = ConnectionManager()
        ws = RecordingWebSocket()
        await manager.connect("sess", "u", ws, role="player")

        message = {
            "type": "chat_message",
            "payload": {"text": 'tricky "quotes" and braces }{', "n": 3},
        }
        await manager.send_to("sess", "u", message)

        decoded = json.loads(ws.sent[0])
        assert decoded["seq"] == 1
        assert decoded["type"] == "chat_message"
        assert decoded["payload"] == message["payload"]

    asyncio.run(run_case())


def test_broadcast_to_role_and_filtered_paths_also_stamp_seq():
    from types import SimpleNamespace

    async def run_case():
        manager = ConnectionManager()
        dm = RecordingWebSocket()
        player = RecordingWebSocket()
        await manager.connect("sess", "dm", dm, role="dm")
        await manager.connect("sess", "p1", player, role="player")
        session_obj = SimpleNamespace(users={
            "dm": SimpleNamespace(role="dm"),
            "p1": SimpleNamespace(role="player"),
        })

        await manager.broadcast_to_role("sess", {"type": "dm_notice", "payload": {}}, {"dm"}, session_obj)
        await manager.broadcast_filtered(
            "sess",
            {"type": "token_moved", "payload": {"token": {"id": "t1", "hidden": True}}},
            hide_hidden_tokens=True,
            dm_id="dm",
            session_obj=session_obj,
        )

        assert dm.seqs() == [1, 2]
        # The player got the redacted variant, but still with their own seq.
        player_frames = [json.loads(f) for f in player.sent]
        assert [f.get("seq") for f in player_frames] == [1]
        assert player_frames[0]["type"] == "token_removed_hidden"

    asyncio.run(run_case())


# ── Layer 2.5: size-aware per-send timeout ───────────────────────────────────

def test_send_timeout_scales_with_frame_size(monkeypatch):
    monkeypatch.delenv("WS_SEND_TIMEOUT_SECONDS", raising=False)

    tiny = send_timeout_seconds_for(64)
    one_mib = send_timeout_seconds_for(1024 * 1024)
    huge = send_timeout_seconds_for(64 * 1024 * 1024)

    assert tiny >= DEFAULT_SEND_TIMEOUT_SECONDS
    # A 1 MiB frame at the assumed 128 KiB/s throughput floor needs ~8s of pure
    # transfer time; the timeout must budget for it on top of the base.
    assert one_mib >= DEFAULT_SEND_TIMEOUT_SECONDS + 8.0
    assert one_mib <= MAX_SEND_TIMEOUT_SECONDS
    # …but never grow unbounded (must stay well under the 60s heartbeat timeout).
    assert huge == MAX_SEND_TIMEOUT_SECONDS


def test_send_timeout_respects_operator_override_above_cap(monkeypatch):
    monkeypatch.setenv("WS_SEND_TIMEOUT_SECONDS", "45")
    assert send_timeout_seconds_for(0) == 45.0
    assert send_timeout_seconds_for(64 * 1024 * 1024) == 45.0
