"""Role-aware outbound payload size diagnostics.

This module intentionally measures serialized payload sizes without logging or
returning payload contents. Diagnostics must remain safe for hidden token names,
private token notes, handouts, and character data.
"""
from __future__ import annotations

import json
import logging
from typing import Any

PAYLOAD_WARN_BYTES = 128 * 1024
PAYLOAD_ERROR_BYTES = 512 * 1024

MONITORED_MESSAGE_TYPES = frozenset({
    "state_sync",
    "authoritative_snapshot",
    "item_library_sync",
    "tokens_sync",
    "combat_state",
    "fog_state",
    "fog_delta",
    "player_inventory_sync",
    "quick_actions_sync",
    "char_profiles_sync",
})


def payload_byte_size(message: dict[str, Any]) -> int:
    """Return the UTF-8 byte size for a JSON WebSocket frame."""
    return len(json.dumps(message).encode("utf-8"))


def payload_severity(byte_size: int) -> str:
    if byte_size > PAYLOAD_ERROR_BYTES:
        return "error"
    if byte_size > PAYLOAD_WARN_BYTES:
        return "warning"
    return "debug"


def log_payload_size_diagnostic(
    logger: logging.Logger,
    *,
    session_id: str,
    recipient_user_id: str,
    recipient_role: str,
    message_type: str,
    byte_size: int,
) -> None:
    """Log metadata-only diagnostics for monitored payload types.

    Do not add payload fields here. Some monitored messages can contain hidden
    token names, token private notes, inventory contents, and DM-only text.
    """
    if message_type not in MONITORED_MESSAGE_TYPES:
        return
    log_message = (
        "[payload_size] message_type=%s session_id=%s recipient_user_id=%s "
        "recipient_role=%s byte_size=%s warn_threshold_bytes=%s error_threshold_bytes=%s"
    )
    args = (
        message_type,
        session_id,
        recipient_user_id,
        recipient_role or "unknown",
        byte_size,
        PAYLOAD_WARN_BYTES,
        PAYLOAD_ERROR_BYTES,
    )
    severity = payload_severity(byte_size)
    if severity == "error":
        logger.error(log_message, *args)
    elif severity == "warning":
        logger.warning(log_message, *args)
    else:
        logger.debug(log_message, *args)



def top_level_payload_sizes(message: dict[str, Any], *, limit: int = 8) -> list[tuple[str, int]]:
    payload = message.get("payload") if isinstance(message, dict) else None
    if not isinstance(payload, dict):
        return []
    sizes = [(str(key), payload_byte_size({"value": value})) for key, value in payload.items()]
    sizes.sort(key=lambda kv: kv[1], reverse=True)
    return sizes[:limit]


def log_top_level_payload_keys(logger: logging.Logger, *, session_id: str, recipient_user_id: str, recipient_role: str, message: dict[str, Any], byte_size: int = 0) -> None:
    message_type = str((message or {}).get("type") or "")
    if message_type not in {"state_sync", "char_profiles_sync"}:
        return
    if byte_size and byte_size <= PAYLOAD_WARN_BYTES:
        return
    top = top_level_payload_sizes(message)
    if not top:
        return
    logger.warning(
        "[payload_size_breakdown] message_type=%s session_id=%s recipient_user_id=%s recipient_role=%s top_level_bytes=%s",
        message_type,
        session_id,
        recipient_user_id,
        recipient_role or "unknown",
        ",".join(f"{key}:{size}" for key, size in top),
    )

def deflate_byte_size(message: dict[str, Any]) -> int:
    """Approximate on-the-wire size of a frame under permessage-deflate."""
    import zlib
    try:
        return len(zlib.compress(json.dumps(message).encode("utf-8"), 6))
    except Exception:
        return 0


def build_payload_size_report_for_role(session, role: str, user_id: str) -> dict[str, Any]:
    """Build a metadata-only sample payload size report for one session role."""
    state_message = {"type": "state_sync", "payload": session.to_state_dict_for_role(role, user_id)}
    snapshot_message = session.to_authoritative_snapshot_for_role(role, user_id, source="payload_size_report")
    return {
        "role": role,
        "user_id": user_id,
        "session_id": str(getattr(session, "id", "") or ""),
        "messages": {
            "state_sync": payload_byte_size(state_message),
            "state_sync_deflate": deflate_byte_size(state_message),
            "authoritative_snapshot": payload_byte_size(snapshot_message),
        },
    }


def build_payload_size_report(session) -> dict[str, Any]:
    """Build sample state/snapshot payload sizes for DM, player, and viewer users."""
    by_role: dict[str, dict[str, Any]] = {}
    for uid, user in (getattr(session, "users", {}) or {}).items():
        role = str(getattr(user, "role", "viewer") or "viewer").strip().lower() or "viewer"
        if role in {"dm", "player", "viewer"} and role not in by_role:
            by_role[role] = build_payload_size_report_for_role(session, role, uid)
    return {"session_id": str(getattr(session, "id", "") or ""), "roles": by_role}
