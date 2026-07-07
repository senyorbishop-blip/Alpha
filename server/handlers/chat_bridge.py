"""
server/handlers/chat_bridge.py — WebSocket handlers for the Twitch chat bridge.

The chat_bridge role is a trusted internal service account. It can:
  - Register / deregister chat participants
  - Trigger item use against a target token on a participant's behalf
  - Grant items to participants (from loot-table rolls on Twitch events)

It cannot perform any DM or player actions — enforced both here and in
ws_permissions.py.

The DM can pause/resume all chat bridge activity via dm_chat_bridge_kill_switch
(dm role only, NOT a chat_bridge message type).
"""
from __future__ import annotations

import html
import re
import time
import logging

from server.session import Session, User, ChatParticipant
from server.handlers.common import manager, save_campaign_async

logger = logging.getLogger(__name__)

# Maximum length for any user-supplied string that enters game state / overlays.
_MAX_ARG_LEN = 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(value: str, max_len: int = _MAX_ARG_LEN) -> str:
    """Strip HTML and control characters; truncate to max_len."""
    cleaned = html.escape(str(value or "").strip(), quote=False)
    # Remove any remaining angle brackets just in case
    cleaned = re.sub(r"[<>]", "", cleaned)
    return cleaned[:max_len]


def _resolve_participant(session: Session, twitch_username: str) -> ChatParticipant | None:
    key = str(twitch_username or "").strip().lower()
    raw = session.chat_participants.get(key)
    if raw is None:
        return None
    if isinstance(raw, ChatParticipant):
        return raw
    # Deserialised from plain dict (e.g. after session restore with persist=true)
    return ChatParticipant(
        twitch_username=raw.get("twitch_username", key),
        display_name=raw.get("display_name", key),
        joined_at=float(raw.get("joined_at", time.time())),
        inventory=list(raw.get("inventory", [])),
        is_active=bool(raw.get("is_active", True)),
        twitch_user_id=str(raw.get("twitch_user_id", "") or ""),
        character_name=str(raw.get("character_name", "") or ""),
        last_joined_session_id=str(raw.get("last_joined_session_id", "") or ""),
        lifetime_stats=dict(raw.get("lifetime_stats", None) or {
            "sessions_joined": 0, "items_used": 0, "damage_dealt": 0, "kills": 0,
        }),
        arena_stats=dict(raw.get("arena_stats", None) or {
            "wins": 0, "losses": 0, "arena_gold": 0,
        }),
    )


def _kill_switch_check(session: Session) -> bool:
    """Return True if the kill switch is active (bridge interactions paused)."""
    return bool(getattr(session, "chat_bridge_paused", False))


def _fuzzy_match_token(session: Session, name_query: str):
    """
    Case-insensitive substring match against token names.
    Returns (token, None) on unique match, (None, error_str) otherwise.
    """
    query = name_query.strip().lower()
    if not query:
        return None, "No target name provided."
    matches = [
        t for t in session.tokens.values()
        if not t.hidden and query in t.name.lower()
    ]
    if not matches:
        return None, f"No token matching '{_sanitize(name_query)}' was found on the map."
    if len(matches) > 1:
        names = ", ".join(t.name for t in matches[:5])
        return None, f"Ambiguous target — matches: {names}. Be more specific."
    return matches[0], None


def _first_usable_item(inventory: list) -> dict | None:
    """Return the first item that has qty > 0 and either charges or is single-use."""
    for item in inventory:
        if not isinstance(item, dict):
            continue
        qty = int(item.get("qty", 1) or 1)
        if qty <= 0:
            continue
        return item
    return None


def _consume_item(inventory: list, item: dict) -> None:
    """Decrement qty / charges; remove if depleted."""
    idx = inventory.index(item)
    charges_max = int(item.get("charges_max", 0) or 0)
    charges_cur = int(item.get("charges_current", 0) or 0)

    if charges_max > 0:
        new_charges = max(0, charges_cur - 1)
        inventory[idx] = {**item, "charges_current": new_charges}
        if new_charges <= 0:
            inventory.pop(idx)
    else:
        qty = int(item.get("qty", 1) or 1)
        new_qty = max(0, qty - 1)
        if new_qty <= 0:
            inventory.pop(idx)
        else:
            inventory[idx] = {**item, "qty": new_qty}


def _build_participants_payload(session: Session) -> list:
    out = []
    for key, p in session.chat_participants.items():
        if isinstance(p, ChatParticipant):
            out.append({
                "twitch_username": p.twitch_username,
                "display_name": p.display_name,
                "joined_at": p.joined_at,
                "is_active": p.is_active,
                "item_count": len(p.inventory),
            })
    return out


# ---------------------------------------------------------------------------
# Chat-participant lifecycle handlers  (role: chat_bridge)
# ---------------------------------------------------------------------------

async def handle_chat_participant_join(payload: dict, session: Session, user: User):
    if _kill_switch_check(session):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Chat bridge is paused by the DM."},
        })
        return

    raw_username = str(payload.get("twitch_username") or "").strip()
    raw_display = str(payload.get("display_name") or raw_username).strip()
    raw_user_id = str(payload.get("twitch_user_id") or "").strip()

    if not raw_username:
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "twitch_username is required."},
        })
        return

    key = raw_username.lower()
    display = _sanitize(raw_display)

    existing = session.chat_participants.get(key)
    if isinstance(existing, ChatParticipant) and existing.is_active:
        # Idempotent — already joined; just ack
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_join_ack",
            "payload": {"twitch_username": key, "already_joined": True},
        })
        return

    if isinstance(existing, ChatParticipant):
        # Rejoining after leave — reactivate, preserve inventory
        existing.is_active = True
        existing.display_name = display
        if raw_user_id:
            existing.twitch_user_id = _sanitize(raw_user_id, 32)
        participant = existing
    elif isinstance(existing, dict):
        # Restored from persistence — deserialise and reactivate
        participant = _resolve_participant(session, key)
        participant.is_active = True
        participant.display_name = display
        if raw_user_id:
            participant.twitch_user_id = _sanitize(raw_user_id, 32)
        session.chat_participants[key] = participant
    else:
        participant = ChatParticipant(
            twitch_username=key,
            display_name=display,
            joined_at=time.time(),
            twitch_user_id=_sanitize(raw_user_id, 32) if raw_user_id else "",
        )
        session.chat_participants[key] = participant

    # Track per-session join count (increment once per distinct session)
    if participant.last_joined_session_id != session.id:
        participant.lifetime_stats["sessions_joined"] = (
            participant.lifetime_stats.get("sessions_joined", 0) + 1
        )
        participant.last_joined_session_id = session.id

    log_entry = session.add_log(
        f"{display} joined the tavern from Twitch chat.",
        msg_type="chat_bridge",
        user_name="Chat Bridge",
    )

    await manager.broadcast(session.id, {
        "type": "chat_participant_joined",
        "payload": {
            "twitch_username": key,
            "display_name": display,
            "joined_at": participant.joined_at,
        },
    })
    await manager.broadcast(session.id, {
        "type": "chat_participants_sync",
        "payload": {"participants": _build_participants_payload(session)},
    })
    await manager.broadcast(session.id, {
        "type": "log_entry",
        "payload": {"log": log_entry},
    })

    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_join_ack",
        "payload": {
            "twitch_username": key,
            "already_joined": False,
            "returning": participant.lifetime_stats.get("sessions_joined", 0) > 1,
            "sessions_joined": participant.lifetime_stats.get("sessions_joined", 0),
            "character_name": participant.character_name,
            "lifetime_stats": participant.lifetime_stats,
        },
    })

    await save_campaign_async(session.id)


async def handle_chat_participant_leave(payload: dict, session: Session, user: User):
    raw_username = str(payload.get("twitch_username") or "").strip()
    if not raw_username:
        return

    key = raw_username.lower()
    participant = _resolve_participant(session, key)
    if participant is None or not participant.is_active:
        return

    participant.is_active = False
    display = participant.display_name

    log_entry = session.add_log(
        f"{display} left the tavern.",
        msg_type="chat_bridge",
        user_name="Chat Bridge",
    )

    await manager.broadcast(session.id, {
        "type": "chat_participant_left",
        "payload": {"twitch_username": key, "display_name": display},
    })
    await manager.broadcast(session.id, {
        "type": "chat_participants_sync",
        "payload": {"participants": _build_participants_payload(session)},
    })
    await manager.broadcast(session.id, {
        "type": "log_entry",
        "payload": {"log": log_entry},
    })


# ---------------------------------------------------------------------------
# Item targeting  (role: chat_bridge)
# ---------------------------------------------------------------------------

async def handle_chat_participant_target(payload: dict, session: Session, user: User):
    if _kill_switch_check(session):
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_target_result",
            "payload": {"success": False, "message": "Chat bridge is paused by the DM."},
        })
        return

    raw_username = str(payload.get("twitch_username") or "").strip()
    raw_target = str(payload.get("target_name") or "").strip()

    key = raw_username.lower()
    participant = _resolve_participant(session, key)

    if participant is None or not participant.is_active:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_target_result",
            "payload": {"success": False, "message": f"{_sanitize(raw_username)} has not joined — use !join first."},
        })
        return

    item = _first_usable_item(participant.inventory)
    if item is None:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_target_result",
            "payload": {"success": False, "message": f"{participant.display_name} has no usable items."},
        })
        return

    token, err = _fuzzy_match_token(session, raw_target)
    if err:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_target_result",
            "payload": {"success": False, "message": err},
        })
        return

    item_name = _sanitize(str(item.get("name") or "item"))
    display = participant.display_name
    target_name = _sanitize(token.name)

    _consume_item(participant.inventory, item)
    participant.lifetime_stats["items_used"] = (
        participant.lifetime_stats.get("items_used", 0) + 1
    )

    log_entry = session.add_log(
        f"{display} used {item_name} on {target_name}!",
        msg_type="chat_bridge",
        user_name=display,
    )

    await manager.broadcast(session.id, {
        "type": "chat_participant_action_result",
        "payload": {
            "twitch_username": key,
            "display_name": display,
            "item_name": item_name,
            "target_token_id": token.id,
            "target_name": target_name,
            "remaining_items": len(participant.inventory),
        },
    })
    await manager.broadcast(session.id, {
        "type": "log_entry",
        "payload": {"log": log_entry},
    })

    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_target_result",
        "payload": {
            "success": True,
            "message": f"{display} used {item_name} on {target_name}!",
            "item_name": item_name,
            "target_name": target_name,
        },
    })

    await save_campaign_async(session.id)


# ---------------------------------------------------------------------------
# Loot grant from Twitch events  (role: chat_bridge)
# ---------------------------------------------------------------------------

async def handle_chat_bridge_loot_grant(payload: dict, session: Session, user: User):
    if _kill_switch_check(session):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_loot_grant_result",
            "payload": {"success": False, "message": "Chat bridge is paused."},
        })
        return

    raw_username = str(payload.get("twitch_username") or "").strip()
    raw_display = str(payload.get("display_name") or raw_username).strip()
    item_entry = payload.get("item_entry")
    trigger = _sanitize(str(payload.get("trigger") or "gift"))  # "sub"|"bits"|"raid"|etc.

    if not raw_username or not isinstance(item_entry, dict):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_loot_grant_result",
            "payload": {"success": False, "message": "twitch_username and item_entry are required."},
        })
        return

    item_name_raw = str(item_entry.get("name") or "").strip()
    if not item_name_raw:
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_loot_grant_result",
            "payload": {"success": False, "message": "item_entry.name is required."},
        })
        return

    key = raw_username.lower()
    display = _sanitize(raw_display)

    # Auto-join if not already present
    if key not in session.chat_participants or not session.chat_participants[key].is_active:
        session.chat_participants[key] = ChatParticipant(
            twitch_username=key,
            display_name=display,
            joined_at=time.time(),
        )
        await manager.broadcast(session.id, {
            "type": "chat_participant_joined",
            "payload": {
                "twitch_username": key,
                "display_name": display,
                "joined_at": session.chat_participants[key].joined_at,
            },
        })

    participant = _resolve_participant(session, key)

    # Build a clean item entry
    safe_item: dict = {
        "name": _sanitize(item_name_raw, 80),
        "qty": max(1, int(item_entry.get("qty", 1) or 1)),
        "notes": _sanitize(str(item_entry.get("notes") or ""), 200),
        "effect": _sanitize(str(item_entry.get("effect") or ""), 300),
        "is_magic": bool(item_entry.get("is_magic", False)),
        "charges_current": max(0, int(item_entry.get("charges_current", 0) or 0)),
        "charges_max": max(0, int(item_entry.get("charges_max", 0) or 0)),
        "source": f"twitch:{trigger}",
    }

    participant.inventory.append(safe_item)
    item_name = safe_item["name"]

    log_entry = session.add_log(
        f"{display} received {item_name} (via {trigger})!",
        msg_type="chat_bridge",
        user_name="Chat Bridge",
    )

    await manager.broadcast(session.id, {
        "type": "chat_participant_loot_received",
        "payload": {
            "twitch_username": key,
            "display_name": display,
            "item_name": item_name,
            "trigger": trigger,
        },
    })
    await manager.broadcast(session.id, {
        "type": "chat_participants_sync",
        "payload": {"participants": _build_participants_payload(session)},
    })
    await manager.broadcast(session.id, {
        "type": "log_entry",
        "payload": {"log": log_entry},
    })

    await manager.send_to(session.id, user.id, {
        "type": "chat_bridge_loot_grant_result",
        "payload": {"success": True, "item_name": item_name, "twitch_username": key},
    })

    await save_campaign_async(session.id)


# ---------------------------------------------------------------------------
# Inventory query  (role: chat_bridge)
# ---------------------------------------------------------------------------

async def handle_chat_participant_inventory(payload: dict, session: Session, user: User):
    raw_username = str(payload.get("twitch_username") or "").strip()
    key = raw_username.lower()
    participant = _resolve_participant(session, key)

    if participant is None or not participant.is_active:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_inventory_result",
            "payload": {
                "twitch_username": key,
                "found": False,
                "items": [],
            },
        })
        return

    items_summary = [
        {
            "name": _sanitize(str(item.get("name") or "item")),
            "qty": int(item.get("qty", 1) or 1),
            "charges_current": int(item.get("charges_current", 0) or 0),
            "charges_max": int(item.get("charges_max", 0) or 0),
        }
        for item in participant.inventory
        if isinstance(item, dict)
    ]

    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_inventory_result",
        "payload": {
            "twitch_username": key,
            "display_name": participant.display_name,
            "found": True,
            "items": items_summary,
        },
    })


# ---------------------------------------------------------------------------
# Poll voting from chat  (role: chat_bridge)
# ---------------------------------------------------------------------------

async def handle_chat_bridge_poll_vote(payload: dict, session: Session, user: User):
    """
    Chat viewer votes on the active session poll.

    The bridge sends this on behalf of any chatter who types !vote <N>.
    Chatters do not need to have joined (no !join required for voting).
    Vote key is "chat:<twitch_username>" to avoid collisions with player user_ids.
    """
    if _kill_switch_check(session):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_poll_vote_result",
            "payload": {"success": False, "error_code": "bridge_paused"},
        })
        return

    raw_username = str(payload.get("twitch_username") or "").strip()
    if not raw_username:
        return

    key = raw_username.lower()
    poll_id = str(payload.get("poll_id") or "").strip()
    option_index = payload.get("option_index")

    poll = session.active_poll
    if not poll or poll.get("closed"):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_poll_vote_result",
            "payload": {"success": False, "error_code": "no_active_poll", "twitch_username": key},
        })
        return

    if poll_id and poll_id != poll.get("id"):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_poll_vote_result",
            "payload": {"success": False, "error_code": "poll_id_mismatch", "twitch_username": key},
        })
        return

    options = poll.get("options") or []
    if not isinstance(option_index, int) or option_index < 0 or option_index >= len(options):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_poll_vote_result",
            "payload": {
                "success": False,
                "error_code": "invalid_option",
                "twitch_username": key,
                "max_valid": len(options),
            },
        })
        return

    vote_key = f"chat:{key}"
    already_voted = vote_key in (poll.get("votes") or {})
    poll.setdefault("votes", {})[vote_key] = option_index

    from server.handlers.content import _broadcast_poll_state
    await _broadcast_poll_state(session, "poll_updated")

    await manager.send_to(session.id, user.id, {
        "type": "chat_bridge_poll_vote_result",
        "payload": {
            "success": True,
            "twitch_username": key,
            "option_index": option_index,
            "option_label": options[option_index],
            "changed": already_voted,
        },
    })


# ---------------------------------------------------------------------------
# Character name set  (role: chat_bridge)
# ---------------------------------------------------------------------------

async def handle_chat_participant_name_set(payload: dict, session: Session, user: User):
    """Chatter sets their in-game character name via !name <name>."""
    if _kill_switch_check(session):
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_name_result",
            "payload": {"success": False, "error_code": "bridge_paused"},
        })
        return

    raw_username = str(payload.get("twitch_username") or "").strip()
    raw_name = str(payload.get("character_name") or "").strip()
    raw_user_id = str(payload.get("twitch_user_id") or "").strip()

    key = raw_username.lower()
    participant = _resolve_participant(session, key)
    if participant is None or not participant.is_active:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_name_result",
            "payload": {"success": False, "error_code": "not_joined", "twitch_username": key},
        })
        return

    if not raw_name:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_name_result",
            "payload": {"success": False, "error_code": "empty_name", "twitch_username": key},
        })
        return

    # Sanitize: allow letters, numbers, spaces, hyphens, apostrophes
    import re as _re
    cleaned = _re.sub(r"[^a-zA-Z0-9 '\-]", "", raw_name).strip()[:32]
    if not cleaned:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_name_result",
            "payload": {"success": False, "error_code": "invalid_chars", "twitch_username": key},
        })
        return

    # Enforce uniqueness across active participants
    cleaned_lower = cleaned.lower()
    for other_key, other_p in session.chat_participants.items():
        if other_key == key:
            continue
        other_name = ""
        if isinstance(other_p, ChatParticipant):
            other_name = other_p.character_name
        elif isinstance(other_p, dict):
            other_name = str(other_p.get("character_name", "") or "")
        if other_name.lower() == cleaned_lower:
            await manager.send_to(session.id, user.id, {
                "type": "chat_participant_name_result",
                "payload": {"success": False, "error_code": "name_taken", "twitch_username": key},
            })
            return

    participant.character_name = cleaned
    if raw_user_id:
        participant.twitch_user_id = _sanitize(raw_user_id, 32)

    log_entry = session.add_log(
        f"{participant.display_name} is now known as {cleaned}.",
        msg_type="chat_bridge",
        user_name="Chat Bridge",
    )

    await manager.broadcast(session.id, {
        "type": "chat_participant_updated",
        "payload": {
            "twitch_username": key,
            "display_name": participant.display_name,
            "character_name": cleaned,
        },
    })
    await manager.broadcast(session.id, {
        "type": "log_entry",
        "payload": {"log": log_entry},
    })
    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_name_result",
        "payload": {"success": True, "twitch_username": key, "character_name": cleaned},
    })

    await save_campaign_async(session.id)


# ---------------------------------------------------------------------------
# Character summary query  (role: chat_bridge)
# ---------------------------------------------------------------------------

async def handle_chat_participant_me(payload: dict, session: Session, user: User):
    """Chatter queries their own character summary (for !me command)."""
    raw_username = str(payload.get("twitch_username") or "").strip()
    key = raw_username.lower()
    participant = _resolve_participant(session, key)

    if participant is None or not participant.is_active:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_me_result",
            "payload": {"found": False, "twitch_username": key},
        })
        return

    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_me_result",
        "payload": {
            "found": True,
            "twitch_username": key,
            "display_name": participant.display_name,
            "character_name": participant.character_name,
            "inventory_count": len([i for i in participant.inventory if isinstance(i, dict)]),
            "lifetime_stats": dict(participant.lifetime_stats or {}),
            "arena_stats": dict(participant.arena_stats or {}),
        },
    })


# ---------------------------------------------------------------------------
# Kill switch  (role: dm — enforced in ws_permissions via DM_ADMIN_MESSAGE_TYPES)
# ---------------------------------------------------------------------------

async def handle_dm_chat_bridge_kill_switch(payload: dict, session: Session, user: User):
    paused = bool(payload.get("paused", True))
    session.chat_bridge_paused = paused
    status = "paused" if paused else "resumed"

    log_entry = session.add_log(
        f"Chat bridge {status} by DM.",
        msg_type="system",
        user_name=user.name,
    )

    await manager.broadcast(session.id, {
        "type": "chat_bridge_status",
        "payload": {"paused": paused},
    })
    await manager.broadcast(session.id, {
        "type": "log_entry",
        "payload": {"log": log_entry},
    })


# ---------------------------------------------------------------------------
# Arena stats update  (role: chat_bridge — arena-only, never campaign state)
# ---------------------------------------------------------------------------

async def handle_chat_participant_arena_stats_update(payload: dict, session: Session, user: User):
    """
    Sync arena win/loss/gold stats from the bridge to the server.

    The arena module is isolated in the bridge — only this endpoint is called
    when a duel ends. It touches ONLY participant.arena_stats; no campaign
    tokens, inventories, or combat state are ever modified.
    """
    raw_username = str(payload.get("twitch_username") or "").strip()
    if not raw_username:
        return

    key = raw_username.lower()
    participant = _resolve_participant(session, key)
    if participant is None:
        # Auto-create a minimal record for chatters who earned stats without joining
        participant = ChatParticipant(
            twitch_username=key,
            display_name=_sanitize(str(payload.get("display_name") or key)),
            joined_at=time.time(),
            is_active=False,
        )
        session.chat_participants[key] = participant

    raw_stats = payload.get("arena_stats")
    if not isinstance(raw_stats, dict):
        return

    arena = participant.arena_stats
    if not isinstance(arena, dict):
        arena = {"wins": 0, "losses": 0, "arena_gold": 0}
        participant.arena_stats = arena

    for field in ("wins", "losses", "arena_gold"):
        val = raw_stats.get(field)
        if isinstance(val, (int, float)) and val >= 0:
            arena[field] = int(val)

    await save_campaign_async(session.id)


# ---------------------------------------------------------------------------
# DM item grant to a chat participant  (role: dm)
# ---------------------------------------------------------------------------

async def handle_dm_grant_chat_participant_item(payload: dict, session: Session, user: User):
    raw_username = str(payload.get("twitch_username") or "").strip()
    item_entry = payload.get("item_entry")

    if not raw_username or not isinstance(item_entry, dict) or not str(item_entry.get("name") or "").strip():
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "twitch_username and item_entry.name are required."},
        })
        return

    key = raw_username.lower()
    participant = _resolve_participant(session, key)
    if participant is None or not participant.is_active:
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": f"No active chat participant '{_sanitize(raw_username)}'."},
        })
        return

    safe_item: dict = {
        "name": _sanitize(str(item_entry.get("name") or ""), 80),
        "qty": max(1, int(item_entry.get("qty", 1) or 1)),
        "notes": _sanitize(str(item_entry.get("notes") or ""), 200),
        "effect": _sanitize(str(item_entry.get("effect") or ""), 300),
        "is_magic": bool(item_entry.get("is_magic", False)),
        "charges_current": max(0, int(item_entry.get("charges_current", 0) or 0)),
        "charges_max": max(0, int(item_entry.get("charges_max", 0) or 0)),
        "source": f"dm:{user.name}",
    }
    participant.inventory.append(safe_item)
    item_name = safe_item["name"]

    log_entry = session.add_log(
        f"DM granted {item_name} to {participant.display_name}.",
        msg_type="chat_bridge",
        user_name=user.name,
    )

    await manager.broadcast(session.id, {
        "type": "chat_participant_item_granted",
        "payload": {
            "twitch_username": key,
            "display_name": participant.display_name,
            "item_name": item_name,
            "granted_by": user.name,
        },
    })
    await manager.broadcast(session.id, {
        "type": "log_entry",
        "payload": {"log": log_entry},
    })
    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_item_granted",
        "payload": {"success": True, "item_name": item_name},
    })

    await save_campaign_async(session.id)


# ---------------------------------------------------------------------------
# Arena display event relay  (role: chat_bridge, read-only broadcast)
# ---------------------------------------------------------------------------

async def handle_arena_display_event(payload: dict, session: Session, user: User):
    """Relay an arena display event to all connected overlays.

    The arena module emits these for OBS overlay consumption.  They are
    display-only — no game state is modified.
    """
    event_type = str(payload.get("event_type") or "").strip()[:32]
    if not event_type:
        return

    safe_payload = {
        "event_type":        event_type,
        "challenger":        _sanitize(str(payload.get("challenger") or ""), 32),
        "opponent":          _sanitize(str(payload.get("opponent") or ""), 32),
        "challenger_hp":     int(payload.get("challenger_hp") or 0),
        "opponent_hp":       int(payload.get("opponent_hp") or 0),
        "challenger_max_hp": int(payload.get("challenger_max_hp") or 0),
        "opponent_max_hp":   int(payload.get("opponent_max_hp") or 0),
        "round":             int(payload.get("round") or 0),
        "roll":              int(payload.get("roll") or 0),
        "damage":            int(payload.get("damage") or 0),
        "winner":            _sanitize(str(payload.get("winner") or ""), 32),
        "message":           _sanitize(str(payload.get("message") or ""), 120),
    }

    await manager.broadcast(session.id, {
        "type": "arena_display_event",
        "payload": safe_payload,
    })
