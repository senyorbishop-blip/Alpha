"""P10 websocket heartbeat/reconnect policy tests."""

from __future__ import annotations

from pathlib import Path

from server.utils.ws_heartbeat_policy import (
    HeartbeatPolicy,
    heartbeat_policy_from_env,
    should_close_for_heartbeat,
    should_count_frame_as_liveness,
    should_dispatch_frame,
)

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
WS = ROOT / "client" / "static" / "js" / "core" / "ws.js"


def test_default_policy_requires_two_ping_windows_before_timeout():
    policy = HeartbeatPolicy()

    # 20s ping interval: at least two server→client pings must fit inside the
    # client's 45s no-traffic watchdog window (ws.js), so a quiet-but-healthy
    # session never trips a false client-side reconnect.
    assert policy.ping_interval_seconds == 20.0
    assert policy.timeout_seconds == 60.0
    assert policy.is_valid()
    assert policy.timeout_seconds >= policy.ping_interval_seconds * 2


def test_env_policy_clamps_timeout_to_safe_minimum(monkeypatch):
    monkeypatch.setenv("WS_HEARTBEAT_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("WS_HEARTBEAT_TIMEOUT_SECONDS", "50")

    policy = heartbeat_policy_from_env()

    assert policy.ping_interval_seconds == 45.0
    assert policy.timeout_seconds == 90.0
    assert policy.is_valid()


def test_heartbeat_close_decision_uses_strict_timeout():
    policy = HeartbeatPolicy(ping_interval_seconds=30, timeout_seconds=60)

    assert should_close_for_heartbeat(now=159.9, last_seen=100, policy=policy) is False
    assert should_close_for_heartbeat(now=160.0, last_seen=100, policy=policy) is False
    assert should_close_for_heartbeat(now=160.1, last_seen=100, policy=policy) is True


def test_valid_non_pong_frames_count_as_liveness():
    assert should_count_frame_as_liveness("token_move", decoded=True)
    assert should_count_frame_as_liveness("chat_message", decoded=True)
    assert should_count_frame_as_liveness("pong", decoded=True)
    assert not should_count_frame_as_liveness("token_move", decoded=False)
    assert not should_count_frame_as_liveness("", decoded=True)


def test_pong_refreshes_liveness_but_skips_gameplay_dispatch():
    assert should_count_frame_as_liveness("pong", decoded=True)
    assert not should_dispatch_frame("pong")
    assert should_dispatch_frame("token_move")


def test_server_receive_loop_refreshes_liveness_before_pong_skip():
    src = MAIN.read_text(encoding="utf-8")
    loop_start = src.index("while True:")
    loop_end = src.index("except WebSocketDisconnect:", loop_start)
    loop = src[loop_start:loop_end]

    update = '_last_pong["t"] = asyncio.get_running_loop().time()'
    assert update in loop
    assert loop.index(update) < loop.index('if msg_type == "pong":')
    assert loop.index("json.JSONDecodeError") < loop.index(update)


def test_server_heartbeat_timeout_stays_tolerant_not_aggressive():
    src = MAIN.read_text(encoding="utf-8")

    assert "ping_interval: float = 20" in src
    assert "pong_timeout: float = 60" in src
    assert "Heartbeat timeout" in src
    assert 'await connection_manager.send_to(session_id, user_id, {"type": "ping"})' in src


def test_client_reconnects_in_place_without_page_reload():
    src = WS.read_text(encoding="utf-8")

    assert "function scheduleReconnect()" in src
    assert "connectWS();" in src
    assert "global.location.reload" not in src
    assert "window.location.reload" not in src
    assert "location.href" not in src


def test_client_does_not_reconnect_replaced_sockets():
    src = WS.read_text(encoding="utf-8")

    assert "function wasReplacedByNewerConnection(event)" in src
    assert "replaced by a newer connection" in src.lower()
    assert "not reconnecting: socket was replaced by a newer connection" in src


def test_client_intercepts_ping_before_gameplay_dispatch():
    src = WS.read_text(encoding="utf-8")

    ping_idx = src.index("if (msg && msg.type === 'ping') {")
    pong_idx = src.index("sendPong(socket);", ping_idx)
    dispatch_idx = src.index("config.onMessage(msg);", ping_idx)
    assert ping_idx < pong_idx < dispatch_idx
