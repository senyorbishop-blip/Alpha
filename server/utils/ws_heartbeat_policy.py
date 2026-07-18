"""WebSocket heartbeat and reconnect policy helpers.

The live websocket endpoint in ``main.py`` owns the actual heartbeat loop. These
helpers make the timeout math explicit and testable so future tuning does not
reintroduce false player disconnects during heavy UI frames, combat, fog, or
map-render lag.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# 20s (down from 30s) so at least two server→client pings land inside the
# client's 45s no-traffic watchdog window (client/static/js/core/ws.js).
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 20.0
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 60.0
MIN_HEARTBEAT_INTERVAL_SECONDS = 5.0
MIN_HEARTBEAT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class HeartbeatPolicy:
    ping_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    timeout_seconds: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS

    @property
    def min_timeout_for_interval(self) -> float:
        # Timeout must allow at least one missed/delayed pong plus one extra ping
        # interval. This gives browser tabs that are busy rendering or briefly
        # background-throttled room to recover without being kept forever.
        return max(MIN_HEARTBEAT_TIMEOUT_SECONDS, self.ping_interval_seconds * 2.0)

    def is_valid(self) -> bool:
        return self.ping_interval_seconds >= MIN_HEARTBEAT_INTERVAL_SECONDS and self.timeout_seconds >= self.min_timeout_for_interval


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except Exception:
        return default
    return max(minimum, value)


def heartbeat_policy_from_env() -> HeartbeatPolicy:
    interval = _env_float("WS_HEARTBEAT_INTERVAL_SECONDS", DEFAULT_HEARTBEAT_INTERVAL_SECONDS, MIN_HEARTBEAT_INTERVAL_SECONDS)
    timeout = _env_float("WS_HEARTBEAT_TIMEOUT_SECONDS", DEFAULT_HEARTBEAT_TIMEOUT_SECONDS, MIN_HEARTBEAT_TIMEOUT_SECONDS)
    policy = HeartbeatPolicy(interval, timeout)
    if policy.is_valid():
        return policy
    return HeartbeatPolicy(interval, policy.min_timeout_for_interval)


def should_close_for_heartbeat(*, now: float, last_seen: float, policy: HeartbeatPolicy | None = None) -> bool:
    policy = policy or heartbeat_policy_from_env()
    if now < last_seen:
        return False
    return (now - last_seen) > policy.timeout_seconds


def should_count_frame_as_liveness(frame_type: str | None, *, decoded: bool) -> bool:
    """Only successfully decoded frames refresh liveness.

    Any valid message type proves the socket is alive, not just ``pong``. Invalid
    JSON or undecodable frames must not keep a dead socket alive.
    """
    if not decoded:
        return False
    return bool(str(frame_type or "").strip())


def should_dispatch_frame(frame_type: str | None) -> bool:
    """Heartbeat pongs refresh liveness but do not enter gameplay dispatch."""
    return str(frame_type or "").strip().lower() != "pong"
