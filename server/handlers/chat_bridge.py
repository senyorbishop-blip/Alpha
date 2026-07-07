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


def _echo_req(payload: dict, out: dict) -> dict:
    """Echo the bridge's request-correlation id into a direct reply payload.

    The bridge's GameClient matches replies to requests via `_req_id`; without
    the echo every request/response command times out on the bridge side.
    """
    req_id = payload.get("_req_id") if isinstance(payload, dict) else None
    if req_id is not None:
        out["_req_id"] = str(req_id)[:64]
    return out


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
        arena_character=dict(raw.get("arena_character", None) or {}),
    )


def _find_participant_key_by_user_id(session: Session, twitch_user_id: str) -> str | None:
    """Locate an existing participant by stable Twitch user ID.

    Twitch usernames can change; the numeric user ID cannot. When a chatter
    joins under a new username, this lets us migrate their record instead of
    creating a fresh one.
    """
    uid = str(twitch_user_id or "").strip()
    if not uid:
        return None
    for key, p in session.chat_participants.items():
        stored = ""
        if isinstance(p, ChatParticipant):
            stored = p.twitch_user_id
        elif isinstance(p, dict):
            stored = str(p.get("twitch_user_id", "") or "")
        if stored and stored == uid:
            return key
    return None


def _kill_switch_check(session: Session) -> bool:
    """Return True if the kill switch is active (bridge interactions paused)."""
    return bool(getattr(session, "chat_bridge_paused", False))


def _visible_token_names(session: Session, limit: int = 8) -> list:
    names = [t.name for t in session.tokens.values() if not t.hidden]
    return [_sanitize(n) for n in names[:limit]]


def _fuzzy_match_token(session: Session, name_query: str):
    """
    Case-insensitive substring match against token names.
    Returns (token, None) on unique match, (None, error_dict) otherwise.
    The error dict carries an error_code plus name lists so the bridge can
    render its "invalid target" / "ambiguous target" chat templates.
    """
    query = name_query.strip().lower()
    if not query:
        return None, {"error_code": "no_name", "message": "No target name provided."}
    matches = [
        t for t in session.tokens.values()
        if not t.hidden and query in t.name.lower()
    ]
    if not matches:
        return None, {
            "error_code": "not_found",
            "message": f"No token matching '{_sanitize(name_query)}' was found on the map.",
            "valid_targets": _visible_token_names(session),
        }
    if len(matches) > 1:
        # Exact-name match wins over substring ambiguity ("Gob" vs "Goblin King")
        exact = [t for t in matches if t.name.lower() == query]
        if len(exact) == 1:
            return exact[0], None
        names = [_sanitize(t.name) for t in matches[:5]]
        return None, {
            "error_code": "ambiguous",
            "message": f"Ambiguous target — matches: {', '.join(names)}. Be more specific.",
            "suggestions": names,
        }
    return matches[0], None


# Item-name keywords → viewer power ids, checked in order. Items may also
# carry an explicit "power_id" field (set by loot tables or the DM) which
# always wins. The fallback is a generic 1d10 zap so every item does
# something real.
_ITEM_POWER_KEYWORDS = [
    ("fireball", "fireball"),
    ("meteor", "meteor_pop"),
    ("chain lightning", "chain_lightning"),
    ("lightning", "chain_lightning"),
    ("superior heal", "battle_blessing"),
    ("greater heal", "battle_blessing"),
    ("blessing", "battle_blessing"),
    ("heal", "healing_spark"),
    ("potion", "healing_spark"),
    ("freeze", "flash_freeze"),
    ("frost", "flash_freeze"),
    ("trip", "trip_hex"),
    ("caltrop", "trip_hex"),
    ("goo", "goo_burst"),
    ("net", "goo_burst"),
    ("smoke", "smoke_burst"),
    ("horn", "knockback"),
    ("ram", "knockback"),
    ("magic missile", "arcane_zap"),
    ("wand", "arcane_zap"),
    ("scroll", "arcane_zap"),
    ("staff", "arcane_zap"),
    ("pebble", "pebble_toss"),
    ("rock", "pebble_toss"),
]
_DEFAULT_ITEM_POWER = "arcane_zap"


def _power_for_item(session: Session, item: dict) -> tuple:
    """Resolve which viewer power an inventory item executes as."""
    from server.handlers.viewer_powers import _viewer_power_defs

    defs = _viewer_power_defs(session)
    explicit = str(item.get("power_id") or "").strip().lower()
    if explicit and explicit in defs:
        return explicit, defs[explicit]
    name = str(item.get("name") or "").lower()
    for keyword, power_id in _ITEM_POWER_KEYWORDS:
        if keyword in name and power_id in defs:
            return power_id, defs[power_id]
    return _DEFAULT_ITEM_POWER, defs[_DEFAULT_ITEM_POWER]


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
            "payload": _echo_req(payload, {"message": "Chat bridge is paused by the DM."}),
        })
        return

    raw_username = str(payload.get("twitch_username") or "").strip()
    raw_display = str(payload.get("display_name") or raw_username).strip()
    raw_user_id = str(payload.get("twitch_user_id") or "").strip()

    if not raw_username:
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": _echo_req(payload, {"message": "twitch_username is required."}),
        })
        return

    key = raw_username.lower()
    display = _sanitize(raw_display)

    # Stable identity: if this Twitch user ID already has a record under an
    # old username, migrate it to the new key so their character survives
    # a Twitch rename.
    if key not in session.chat_participants and raw_user_id:
        old_key = _find_participant_key_by_user_id(session, raw_user_id)
        if old_key and old_key != key:
            migrated = _resolve_participant(session, old_key)
            if migrated is not None:
                migrated.twitch_username = key
                session.chat_participants[key] = migrated
                session.chat_participants.pop(old_key, None)
                logger.info("Chat participant renamed on Twitch: %s -> %s", old_key, key)

    existing = session.chat_participants.get(key)
    if isinstance(existing, ChatParticipant) and existing.is_active:
        # Idempotent — already joined; just ack
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_join_ack",
            "payload": _echo_req(payload, {"twitch_username": key, "already_joined": True}),
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
        "payload": _echo_req(payload, {
            "twitch_username": key,
            "already_joined": False,
            "returning": participant.lifetime_stats.get("sessions_joined", 0) > 1,
            "sessions_joined": participant.lifetime_stats.get("sessions_joined", 0),
            "character_name": participant.character_name,
            "lifetime_stats": participant.lifetime_stats,
        }),
    })

    await save_campaign_async(session)


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
            "payload": _echo_req(payload, {
                "success": False, "error_code": "bridge_paused",
                "message": "Chat bridge is paused by the DM.",
            }),
        })
        return

    raw_username = str(payload.get("twitch_username") or "").strip()
    raw_target = str(payload.get("target_name") or "").strip()

    key = raw_username.lower()
    participant = _resolve_participant(session, key)

    if participant is None or not participant.is_active:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_target_result",
            "payload": _echo_req(payload, {
                "success": False, "error_code": "not_joined",
                "message": f"{_sanitize(raw_username)} has not joined — use !join first.",
            }),
        })
        return

    item = _first_usable_item(participant.inventory)
    if item is None:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_target_result",
            "payload": _echo_req(payload, {
                "success": False, "error_code": "no_item",
                "message": f"{participant.display_name} has no usable items.",
            }),
        })
        return

    token, err = _fuzzy_match_token(session, raw_target)
    if err:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_target_result",
            "payload": _echo_req(payload, {"success": False, **err}),
        })
        return

    item_name = _sanitize(str(item.get("name") or "item"))
    display = participant.display_name
    actor_name = participant.character_name or display
    target_name = _sanitize(token.name)

    # Execute the item through the REAL viewer-power engine (same code path
    # viewers use): actual dice rolls, saves, HP mutation, FX, combat sync.
    from server.handlers.viewer_powers import _resolve_viewer_power
    from server.handlers.common import _token_center, _token_map_context

    power_id, power = _power_for_item(session, item)
    cx, cy = _token_center(token)
    target_desc = {
        "token_id": token.id,
        "x": cx,
        "y": cy,
        "map_context": _token_map_context(token),
    }

    hp_before = getattr(token, "hp", None)
    try:
        affected, msg = await _resolve_viewer_power(session, actor_name, power_id, target_desc)
    except Exception:
        logger.exception("chat bridge target: power execution failed (%s)", power_id)
        affected, msg = None, "The item fizzles — something went wrong."

    if affected is None:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_target_result",
            "payload": _echo_req(payload, {
                "success": False, "error_code": "power_failed",
                "message": str(msg or "Could not use item."),
            }),
        })
        return

    hp_after = getattr(token, "hp", None)
    damage = 0
    healed = 0
    if isinstance(hp_before, (int, float)) and isinstance(hp_after, (int, float)):
        delta = int(hp_before) - int(hp_after)
        damage = max(0, delta)
        healed = max(0, -delta)
    killed = bool(
        isinstance(hp_before, (int, float)) and isinstance(hp_after, (int, float))
        and hp_before > 0 and hp_after <= 0
    )

    # Item is only consumed once the power actually resolved.
    _consume_item(participant.inventory, item)
    participant.lifetime_stats["items_used"] = (
        participant.lifetime_stats.get("items_used", 0) + 1
    )
    if damage > 0:
        participant.lifetime_stats["damage_dealt"] = (
            participant.lifetime_stats.get("damage_dealt", 0) + damage
        )
    if killed:
        participant.lifetime_stats["kills"] = (
            participant.lifetime_stats.get("kills", 0) + 1
        )

    await manager.broadcast(session.id, {
        "type": "chat_participant_action_result",
        "payload": {
            "twitch_username": key,
            "display_name": display,
            "item_name": item_name,
            "target_token_id": token.id,
            "target_name": target_name,
            "damage": damage,
            "healed": healed,
            "killed": killed,
            "remaining_items": len(participant.inventory),
        },
    })

    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_target_result",
        "payload": _echo_req(payload, {
            "success": True,
            "message": str(msg),
            "item_name": item_name,
            "target_name": target_name,
            "power_id": power_id,
            "damage": damage,
            "healed": healed,
            "killed": killed,
        }),
    })

    await save_campaign_async(session)


# ---------------------------------------------------------------------------
# Loot grant from Twitch events  (role: chat_bridge)
# ---------------------------------------------------------------------------

async def handle_chat_bridge_loot_grant(payload: dict, session: Session, user: User):
    if _kill_switch_check(session):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_loot_grant_result",
            "payload": _echo_req(payload, {"success": False, "message": "Chat bridge is paused."}),
        })
        return

    raw_username = str(payload.get("twitch_username") or "").strip()
    raw_display = str(payload.get("display_name") or raw_username).strip()
    item_entry = payload.get("item_entry")
    trigger = _sanitize(str(payload.get("trigger") or "gift"))  # "sub"|"bits"|"raid"|etc.

    if not raw_username or not isinstance(item_entry, dict):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_loot_grant_result",
            "payload": _echo_req(payload, {"success": False, "message": "twitch_username and item_entry are required."}),
        })
        return

    item_name_raw = str(item_entry.get("name") or "").strip()
    if not item_name_raw:
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_loot_grant_result",
            "payload": _echo_req(payload, {"success": False, "message": "item_entry.name is required."}),
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
        "power_id": _sanitize(str(item_entry.get("power_id") or ""), 40),
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
        "payload": _echo_req(payload, {"success": True, "item_name": item_name, "twitch_username": key}),
    })

    await save_campaign_async(session)


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
            "payload": _echo_req(payload, {
                "twitch_username": key,
                "found": False,
                "items": [],
            }),
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
        "payload": _echo_req(payload, {
            "twitch_username": key,
            "display_name": participant.display_name,
            "found": True,
            "items": items_summary,
        }),
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
            "payload": _echo_req(payload, {"success": False, "error_code": "bridge_paused"}),
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
            "payload": _echo_req(payload, {"success": False, "error_code": "no_active_poll", "twitch_username": key}),
        })
        return

    if poll_id and poll_id != poll.get("id"):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_poll_vote_result",
            "payload": _echo_req(payload, {"success": False, "error_code": "poll_id_mismatch", "twitch_username": key}),
        })
        return

    options = poll.get("options") or []
    if not isinstance(option_index, int) or option_index < 0 or option_index >= len(options):
        await manager.send_to(session.id, user.id, {
            "type": "chat_bridge_poll_vote_result",
            "payload": _echo_req(payload, {
                "success": False,
                "error_code": "invalid_option",
                "twitch_username": key,
                "max_valid": len(options),
            }),
        })
        return

    vote_key = f"chat:{key}"
    already_voted = vote_key in (poll.get("votes") or {})
    poll.setdefault("votes", {})[vote_key] = option_index

    from server.handlers.content import _broadcast_poll_state
    await _broadcast_poll_state(session, "poll_updated")

    await manager.send_to(session.id, user.id, {
        "type": "chat_bridge_poll_vote_result",
        "payload": _echo_req(payload, {
            "success": True,
            "twitch_username": key,
            "option_index": option_index,
            "option_label": options[option_index],
            "changed": already_voted,
        }),
    })


# ---------------------------------------------------------------------------
# Character name set  (role: chat_bridge)
# ---------------------------------------------------------------------------

async def handle_chat_participant_name_set(payload: dict, session: Session, user: User):
    """Chatter sets their in-game character name via !name <name>."""
    if _kill_switch_check(session):
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_name_result",
            "payload": _echo_req(payload, {"success": False, "error_code": "bridge_paused"}),
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
            "payload": _echo_req(payload, {"success": False, "error_code": "not_joined", "twitch_username": key}),
        })
        return

    if not raw_name:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_name_result",
            "payload": _echo_req(payload, {"success": False, "error_code": "empty_name", "twitch_username": key}),
        })
        return

    # Sanitize: allow letters, numbers, spaces, hyphens, apostrophes
    import re as _re
    cleaned = _re.sub(r"[^a-zA-Z0-9 '\-]", "", raw_name).strip()[:32]
    if not cleaned:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_name_result",
            "payload": _echo_req(payload, {"success": False, "error_code": "invalid_chars", "twitch_username": key}),
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
                "payload": _echo_req(payload, {"success": False, "error_code": "name_taken", "twitch_username": key}),
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
        "payload": _echo_req(payload, {"success": True, "twitch_username": key, "character_name": cleaned}),
    })

    await save_campaign_async(session)


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
            "payload": _echo_req(payload, {"found": False, "twitch_username": key}),
        })
        return

    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_me_result",
        "payload": _echo_req(payload, {
            "found": True,
            "twitch_username": key,
            "display_name": participant.display_name,
            "character_name": participant.character_name,
            "inventory_count": len([i for i in participant.inventory if isinstance(i, dict)]),
            "lifetime_stats": dict(participant.lifetime_stats or {}),
            "arena_stats": dict(participant.arena_stats or {}),
            "arena_character": dict(getattr(participant, "arena_character", None) or {}),
        }),
    })


# ---------------------------------------------------------------------------
# Kill switch  (role: dm — enforced in ws_permissions via DM_ADMIN_MESSAGE_TYPES)
# ---------------------------------------------------------------------------

async def handle_dm_chat_bridge_kill_switch(payload: dict, session: Session, user: User):
    paused = bool(payload.get("paused", True))
    session.chat_bridge_paused = paused
    status = "paused" if paused else "resumed"

    # Optional arena controls piggyback on the same status broadcast so the
    # bridge learns about them over its existing chat_bridge_status handler.
    if "arena_enabled" in payload:
        session.arena_enabled = bool(payload.get("arena_enabled"))
    if "arena_quiet" in payload:
        session.arena_quiet = bool(payload.get("arena_quiet"))

    log_entry = session.add_log(
        f"Chat bridge {status} by DM.",
        msg_type="system",
        user_name=user.name,
    )

    await manager.broadcast(session.id, {
        "type": "chat_bridge_status",
        "payload": {
            "paused": paused,
            "arena_enabled": bool(getattr(session, "arena_enabled", True)),
            "arena_quiet": bool(getattr(session, "arena_quiet", False)),
        },
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

    await save_campaign_async(session)


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
        "power_id": _sanitize(str(item_entry.get("power_id") or ""), 40),
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

    await save_campaign_async(session)


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


# ---------------------------------------------------------------------------
# Arena character persistence  (role: chat_bridge — arena-only, never campaign)
# ---------------------------------------------------------------------------

_ARENA_ABILITIES = ("str", "dex", "con", "int", "wis", "cha")
_ARENA_ITEM_SLOTS = {"weapon", "armor", "trinket"}
_ARENA_MAX_ITEMS = 50


def _sanitize_arena_item(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = _sanitize(str(raw.get("name") or ""), 60)
    if not name:
        return None
    slot = str(raw.get("slot") or "").strip().lower()
    if slot not in _ARENA_ITEM_SLOTS:
        slot = "trinket"
    bonuses = {}
    raw_bonuses = raw.get("bonuses")
    if isinstance(raw_bonuses, dict):
        for stat in (*_ARENA_ABILITIES, "hp", "atk", "ac"):
            val = raw_bonuses.get(stat)
            if isinstance(val, (int, float)) and val:
                bonuses[stat] = max(-10, min(10, int(val)))
    return {
        "id": _sanitize(str(raw.get("id") or ""), 40),
        "name": name,
        "slot": slot,
        "rarity": _sanitize(str(raw.get("rarity") or "common"), 20).lower(),
        "bonuses": bonuses,
        "effect": _sanitize(str(raw.get("effect") or ""), 40),
        "cosmetic": _sanitize(str(raw.get("cosmetic") or ""), 120),
    }


def _sanitize_arena_character(raw: dict) -> dict | None:
    """Whitelist + clamp an arena character sheet sent by the bridge.

    The sheet only ever feeds back into the arena module and !me summaries —
    it can never touch campaign state — but it is still persisted, so keep it
    bounded and clean.
    """
    if not isinstance(raw, dict):
        return None

    def _num(field, lo, hi, default=0):
        val = raw.get(field)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return default
        return max(lo, min(hi, int(val)))

    abilities = {}
    raw_abilities = raw.get("abilities")
    if isinstance(raw_abilities, dict):
        for stat in _ARENA_ABILITIES:
            val = raw_abilities.get(stat)
            abilities[stat] = (
                max(1, min(30, int(val))) if isinstance(val, (int, float)) and not isinstance(val, bool) else 10
            )
    else:
        abilities = {stat: 10 for stat in _ARENA_ABILITIES}

    items = []
    raw_items = raw.get("items")
    if isinstance(raw_items, list):
        for entry in raw_items[:_ARENA_MAX_ITEMS]:
            clean = _sanitize_arena_item(entry)
            if clean:
                items.append(clean)

    equipped = {}
    raw_equipped = raw.get("equipped")
    if isinstance(raw_equipped, dict):
        for slot in _ARENA_ITEM_SLOTS:
            val = raw_equipped.get(slot)
            equipped[slot] = _sanitize(str(val), 40) if val else None
    else:
        equipped = {slot: None for slot in _ARENA_ITEM_SLOTS}

    return {
        "class": _sanitize(str(raw.get("class") or ""), 20),
        "level": _num("level", 1, 99, 1),
        "xp": _num("xp", 0, 10_000_000),
        "gold": _num("gold", 0, 10_000_000),
        "abilities": abilities,
        "wins": _num("wins", 0, 1_000_000),
        "losses": _num("losses", 0, 1_000_000),
        "streak": _num("streak", 0, 1_000_000),
        "best_streak": _num("best_streak", 0, 1_000_000),
        "last_duel_day": _sanitize(str(raw.get("last_duel_day") or ""), 10),
        "wins_since_rare": _num("wins_since_rare", 0, 1_000_000),
        "rerolled": bool(raw.get("rerolled", False)),
        "items": items,
        "equipped": equipped,
        "pending_stat_points": _num("pending_stat_points", 0, 99),
        "created_at": _num("created_at", 0, 4_102_444_800_000),
    }


async def handle_chat_participant_arena_load(payload: dict, session: Session, user: User):
    """Bridge fetches a chatter's persisted arena character (on join / first duel)."""
    raw_username = str(payload.get("twitch_username") or "").strip()
    key = raw_username.lower()
    participant = _resolve_participant(session, key)

    character = dict(getattr(participant, "arena_character", None) or {}) if participant else {}
    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_arena_load_result",
        "payload": _echo_req(payload, {
            "twitch_username": key,
            "found": bool(character),
            "arena_character": character or None,
        }),
    })


async def handle_chat_participant_arena_sync(payload: dict, session: Session, user: User):
    """Bridge persists a chatter's full arena character sheet.

    Arena-only state: this handler writes participant.arena_character and
    nothing else — campaign tokens/inventory/combat are never touched.
    """
    raw_username = str(payload.get("twitch_username") or "").strip()
    if not raw_username:
        return

    clean = _sanitize_arena_character(payload.get("arena_character"))
    if clean is None:
        await manager.send_to(session.id, user.id, {
            "type": "chat_participant_arena_sync_result",
            "payload": _echo_req(payload, {"success": False, "message": "arena_character is required."}),
        })
        return

    key = raw_username.lower()
    participant = _resolve_participant(session, key)
    if participant is None:
        # Chatters can duel without !join — create an inactive record so their
        # arena progress still persists.
        participant = ChatParticipant(
            twitch_username=key,
            display_name=_sanitize(str(payload.get("display_name") or key)),
            joined_at=time.time(),
            is_active=False,
        )
        session.chat_participants[key] = participant
    else:
        session.chat_participants[key] = participant

    participant.arena_character = clean

    # Mirror the headline numbers into arena_stats so !me and the DM roster
    # stay consistent without loading the full sheet.
    stats = participant.arena_stats if isinstance(participant.arena_stats, dict) else {}
    stats["wins"] = clean["wins"]
    stats["losses"] = clean["losses"]
    stats["arena_gold"] = clean["gold"]
    participant.arena_stats = stats

    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_arena_sync_result",
        "payload": _echo_req(payload, {"success": True, "twitch_username": key}),
    })

    await save_campaign_async(session)


async def handle_chat_participant_arena_leaderboard(payload: dict, session: Session, user: User):
    """Top arena fighters for the !leaderboard command."""
    try:
        limit = max(1, min(25, int(payload.get("limit", 10) or 10)))
    except (TypeError, ValueError):
        limit = 10

    entries = []
    for key, raw in session.chat_participants.items():
        participant = _resolve_participant(session, key)
        if participant is None:
            continue
        char = dict(getattr(participant, "arena_character", None) or {})
        stats = participant.arena_stats if isinstance(participant.arena_stats, dict) else {}
        wins = int(char.get("wins", stats.get("wins", 0)) or 0)
        losses = int(char.get("losses", stats.get("losses", 0)) or 0)
        if wins <= 0 and losses <= 0:
            continue
        entries.append({
            "twitch_username": participant.twitch_username,
            "display_name": participant.display_name,
            "character_name": participant.character_name,
            "class": str(char.get("class", "") or ""),
            "level": int(char.get("level", 0) or 0),
            "wins": wins,
            "losses": losses,
        })

    entries.sort(key=lambda e: (-e["wins"], e["losses"], -e["level"]))
    await manager.send_to(session.id, user.id, {
        "type": "chat_participant_arena_leaderboard_result",
        "payload": _echo_req(payload, {"entries": entries[:limit]}),
    })


# ---------------------------------------------------------------------------
# DM chat-character tools  (role: dm — enforced via DM_ADMIN_MESSAGE_TYPES)
# ---------------------------------------------------------------------------

def _participant_detail_payload(participant: ChatParticipant) -> dict:
    char = dict(getattr(participant, "arena_character", None) or {})
    return {
        "twitch_username": participant.twitch_username,
        "twitch_user_id": participant.twitch_user_id,
        "display_name": participant.display_name,
        "character_name": participant.character_name,
        "is_active": participant.is_active,
        "joined_at": participant.joined_at,
        "inventory": [i for i in participant.inventory if isinstance(i, dict)],
        "lifetime_stats": dict(participant.lifetime_stats or {}),
        "arena_stats": dict(participant.arena_stats or {}),
        "arena_class": str(char.get("class", "") or ""),
        "arena_level": int(char.get("level", 0) or 0),
    }


async def handle_dm_chat_participants_get(payload: dict, session: Session, user: User):
    """Full chat-participant roster for the DM settings panel."""
    details = []
    for key in list(session.chat_participants.keys()):
        participant = _resolve_participant(session, key)
        if participant is not None:
            details.append(_participant_detail_payload(participant))
    details.sort(key=lambda d: (not d["is_active"], d["twitch_username"]))
    await manager.send_to(session.id, user.id, {
        "type": "dm_chat_participants_result",
        "payload": {"participants": details},
    })


async def handle_dm_chat_participant_update(payload: dict, session: Session, user: User):
    """DM edits a chat character: rename, or remove an inventory item."""
    raw_username = str(payload.get("twitch_username") or "").strip()
    key = raw_username.lower()
    participant = _resolve_participant(session, key)
    if participant is None:
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": f"No chat participant '{_sanitize(raw_username)}'."},
        })
        return
    session.chat_participants[key] = participant

    changed = []
    if "character_name" in payload:
        cleaned = re.sub(r"[^a-zA-Z0-9 '\-]", "", str(payload.get("character_name") or "")).strip()[:32]
        participant.character_name = cleaned
        changed.append("character_name")

    remove_idx = payload.get("remove_item_index")
    if isinstance(remove_idx, int) and 0 <= remove_idx < len(participant.inventory):
        removed = participant.inventory.pop(remove_idx)
        changed.append(f"removed item '{_sanitize(str((removed or {}).get('name') or 'item'))}'")

    if not changed:
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Nothing to update."},
        })
        return

    log_entry = session.add_log(
        f"DM edited chat character {participant.display_name} ({', '.join(changed)}).",
        msg_type="chat_bridge",
        user_name=user.name,
    )
    await manager.broadcast(session.id, {
        "type": "chat_participant_updated",
        "payload": {
            "twitch_username": key,
            "display_name": participant.display_name,
            "character_name": participant.character_name,
        },
    })
    await manager.broadcast(session.id, {"type": "log_entry", "payload": {"log": log_entry}})
    await manager.send_to(session.id, user.id, {
        "type": "dm_chat_participant_update_result",
        "payload": {"success": True, "participant": _participant_detail_payload(participant)},
    })
    await save_campaign_async(session)


async def handle_dm_chat_participant_reset(payload: dict, session: Session, user: User):
    """DM resets a chat character.

    scope: "stats" | "inventory" | "arena" | "all" (default) | "remove"
    """
    raw_username = str(payload.get("twitch_username") or "").strip()
    scope = str(payload.get("scope") or "all").strip().lower()
    key = raw_username.lower()
    participant = _resolve_participant(session, key)
    if participant is None:
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": f"No chat participant '{_sanitize(raw_username)}'."},
        })
        return

    if scope == "remove":
        session.chat_participants.pop(key, None)
    else:
        session.chat_participants[key] = participant
        if scope in ("stats", "all"):
            participant.lifetime_stats = {
                "sessions_joined": 0, "items_used": 0, "damage_dealt": 0, "kills": 0,
            }
        if scope in ("inventory", "all"):
            participant.inventory = []
        if scope in ("arena", "all"):
            participant.arena_stats = {"wins": 0, "losses": 0, "arena_gold": 0}
            participant.arena_character = {}
        if scope == "all":
            participant.character_name = ""

    log_entry = session.add_log(
        f"DM reset chat character {_sanitize(raw_username)} (scope: {scope}).",
        msg_type="chat_bridge",
        user_name=user.name,
    )
    await manager.broadcast(session.id, {
        "type": "chat_participants_sync",
        "payload": {"participants": _build_participants_payload(session)},
    })
    await manager.broadcast(session.id, {"type": "log_entry", "payload": {"log": log_entry}})
    await manager.send_to(session.id, user.id, {
        "type": "dm_chat_participant_reset_result",
        "payload": {"success": True, "twitch_username": key, "scope": scope},
    })
    await save_campaign_async(session)


async def handle_dm_chat_persistence_mode(payload: dict, session: Session, user: User):
    """DM chooses what chat-participant data persists between sessions."""
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in ("everything", "stats", "nothing"):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "mode must be one of: everything, stats, nothing."},
        })
        return

    session.chat_persistence_mode = mode
    log_entry = session.add_log(
        f"Chat participant persistence set to '{mode}'.",
        msg_type="system",
        user_name=user.name,
    )
    await manager.broadcast(session.id, {"type": "log_entry", "payload": {"log": log_entry}})
    await manager.send_to(session.id, user.id, {
        "type": "dm_chat_persistence_mode_result",
        "payload": {"success": True, "mode": mode},
    })
    await save_campaign_async(session)
