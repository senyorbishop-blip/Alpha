"""
server/handlers/camp_rest.py — Camp / rest mode for downtime scenes.

The DM can open a camp/rest scene between combats.  Players choose a
downtime activity (cook, keep watch, tell story, etc.).  Selections are
broadcast to every connected user so the table can narrate around them.
The DM may restrict which activities are on offer and can end the scene
at any time.  No heavy mechanics — this is pure atmosphere and interaction.
"""
import time
from server.session import Session, User, normalize_profile_owner_key, build_quick_actions_sync_payload, bump_character_hydration_revisions
from server.character.summon_runtime import prune_expired_temporary_summons
from server.character.profile_stub import build_char_profile_stub_list
from server.handlers.common import (
    manager, save_campaign_async,
    _apply_heal, _broadcast_token_state_sync, _sync_combatant_token_state,
)

def _profile_owner_keys_for_user(session: Session, user: User) -> list[str]:
    keys: list[str] = []
    for value in (normalize_profile_owner_key(getattr(user, "name", "")), getattr(user, "id", "")):
        key = str(value or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _active_profile_id(session: Session, user_id: str) -> str:
    return str((getattr(session, "active_char_profiles", {}) or {}).get(user_id) or "").strip()


def _safe_int(value, default=0, *, minimum=None, maximum=None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = int(default)
    if minimum is not None:
        out = max(int(minimum), out)
    if maximum is not None:
        out = min(int(maximum), out)
    return out


def _slot_max(value) -> int:
    if isinstance(value, dict):
        return _safe_int(value.get("max", value.get("total", value.get("available", value.get("current", 0)))), 0, minimum=0, maximum=99)
    return _safe_int(value, 0, minimum=0, maximum=99)


def _restore_spell_slots(native: dict, runtime: dict | None = None) -> bool:
    changed = False
    spell_state = native.get("spellState") if isinstance(native.get("spellState"), dict) else {}
    if spell_state or "spellState" in native:
        native["spellState"] = spell_state
        slots = spell_state.get("slots") if isinstance(spell_state.get("slots"), dict) else {}
        slot_maxes = spell_state.get("slotMaxes") if isinstance(spell_state.get("slotMaxes"), dict) else {}
        current = spell_state.get("slotsCurrent") if isinstance(spell_state.get("slotsCurrent"), dict) else None
        used = spell_state.get("slotsUsed") if isinstance(spell_state.get("slotsUsed"), dict) else None
        if current is not None:
            for lvl, max_raw in {**slots, **slot_maxes}.items():
                max_val = _slot_max(max_raw)
                if max_val and current.get(str(lvl)) != max_val:
                    current[str(lvl)] = max_val; changed = True
            spell_state["slotsCurrent"] = current
        elif used is not None:
            for lvl in list(used.keys()):
                if _safe_int(used.get(lvl), 0, minimum=0) != 0:
                    used[lvl] = 0; changed = True
            spell_state["slotsUsed"] = used
        else:
            # Legacy sheets commonly mutate slots directly as remaining slots.
            restored = {}
            for lvl, max_raw in slots.items():
                max_val = _slot_max(slot_maxes.get(str(lvl), max_raw) if isinstance(slot_maxes, dict) else max_raw)
                restored[str(lvl)] = max_val
                if slots.get(lvl) != max_val:
                    changed = True
            if restored:
                spell_state["slots"] = restored
    if isinstance(runtime, dict):
        spells = runtime.get("spells") if isinstance(runtime.get("spells"), dict) else {}
        slots = spells.get("slots") if isinstance(spells.get("slots"), dict) else {}
        for lvl, raw in list(slots.items()):
            max_val = _slot_max(raw)
            if isinstance(raw, dict):
                if raw.get("current") != max_val:
                    raw["current"] = max_val; changed = True
                slots[lvl] = raw
        if slots:
            spells["slots"] = slots; runtime["spells"] = spells
    return changed


def _restore_resources(runtime: dict | None, rest_type: str) -> bool:
    if not isinstance(runtime, dict):
        return False
    resources = runtime.get("resources") if isinstance(runtime.get("resources"), list) else []
    changed = False
    for res in resources:
        if not isinstance(res, dict):
            continue
        recharge = str(res.get("recharge", res.get("rechargeType", res.get("refresh", "long_rest"))) or "long_rest").lower()
        if rest_type == "short" and recharge not in {"short", "short_rest"}:
            continue
        if rest_type == "long" and recharge in {"none", "never"}:
            continue
        max_val = _safe_int(res.get("max", res.get("maximum", res.get("usesMax", 0))), 0, minimum=0, maximum=999)
        if max_val and _safe_int(res.get("current", res.get("usesCurrent", 0)), 0, minimum=0) != max_val:
            res["current"] = max_val
            if "usesCurrent" in res:
                res["usesCurrent"] = max_val
            changed = True
    if resources:
        runtime["resources"] = resources
    return changed


def _sync_profile_runtime_for_user(session: Session, user: User, *, token=None, rest_type: str, heal_amount: int = 0, hit_dice_state: dict | None = None) -> bool:
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    active_id = _active_profile_id(session, getattr(user, "id", ""))
    changed = False
    for owner_key in _profile_owner_keys_for_user(session, user):
        rows = list(profiles.get(owner_key) or [])
        bucket_changed = False
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            if active_id and str(row.get("id") or "").strip() != active_id:
                continue
            runtime = row.get("nativeRuntime") if isinstance(row.get("nativeRuntime"), dict) else {}
            native = row.get("nativeCharacter") if isinstance(row.get("nativeCharacter"), dict) else {}
            max_hp = _safe_int(getattr(token, "max_hp", row.get("hp", ((runtime.get("hp") or {}).get("max") if isinstance(runtime.get("hp"), dict) else 0))), 0, minimum=0)
            cur_hp = _safe_int(getattr(token, "hp", row.get("curhp", max_hp)), max_hp, minimum=0)
            temp_hp = _safe_int(getattr(token, "temp_hp", row.get("tempHp", 0)), 0, minimum=0)
            if max_hp:
                hp = runtime.get("hp") if isinstance(runtime.get("hp"), dict) else {}
                hp.update({"max": max_hp, "current": min(cur_hp, max_hp), "temp": temp_hp})
                runtime["hp"] = hp
                combat = runtime.get("combat") if isinstance(runtime.get("combat"), dict) else {}
                combat.update({"maxHP": max_hp, "currentHP": min(cur_hp, max_hp), "tempHP": temp_hp})
                runtime["combat"] = combat
                row.update({"curhp": min(cur_hp, max_hp), "hp": max_hp, "tempHp": temp_hp})
                for block_name in ("charBook", "charSheet"):
                    block = row.get(block_name) if isinstance(row.get(block_name), dict) else {}
                    if block_name == "charSheet":
                        sheet_hp = block.get("hp") if isinstance(block.get("hp"), dict) else {}
                        sheet_hp.update({"max": max_hp, "current": min(cur_hp, max_hp), "temp": temp_hp})
                        block["hp"] = sheet_hp
                    block.update({"maxHp": max_hp, "currentHp": min(cur_hp, max_hp), "tempHp": temp_hp})
                    row[block_name] = block
                bucket_changed = True
            if rest_type == "long" and _restore_spell_slots(native, runtime):
                bucket_changed = True
            if _restore_resources(runtime, rest_type):
                bucket_changed = True
            if hit_dice_state and isinstance(hit_dice_state, dict):
                clean_hd = {
                    "total": _safe_int(hit_dice_state.get("total"), 0, minimum=0, maximum=99),
                    "available": _safe_int(hit_dice_state.get("available"), 0, minimum=0, maximum=99),
                    "dieSize": _safe_int(hit_dice_state.get("dieSize", hit_dice_state.get("die_size")), 8, minimum=2, maximum=20),
                    "spent": _safe_int(hit_dice_state.get("spent"), 0, minimum=0, maximum=99),
                }
                clean_hd["available"] = min(clean_hd["available"], clean_hd["total"])
                clean_hd["spent"] = min(clean_hd["spent"], clean_hd["total"])
                runtime["hitDice"] = clean_hd
                row["hitDiceState"] = clean_hd
                sheet = row.get("charSheet") if isinstance(row.get("charSheet"), dict) else {}
                sheet["_hitDiceState"] = clean_hd
                row["charSheet"] = sheet
                bucket_changed = True
            row["nativeRuntime"] = runtime
            if native:
                row["nativeCharacter"] = native
            rows[idx] = row
            if active_id:
                break
        if bucket_changed:
            profiles[owner_key] = rows; changed = True
    if changed:
        session.char_profiles = profiles
    return changed


async def _broadcast_character_and_quick_actions(session: Session):
    active_user_ids = set(manager.get_session_connections(session.id).keys())
    for uid in active_user_ids:
        u = (getattr(session, "users", {}) or {}).get(uid)
        if not u or getattr(u, "role", "") != "player":
            continue
        profiles = dict(getattr(session, "char_profiles", {}) or {})
        mine = []
        for key in _profile_owner_keys_for_user(session, u):
            if isinstance(profiles.get(key), list):
                mine = profiles.get(key); break
        await manager.send_to(session.id, uid, {"type": "char_profiles_sync", "payload": {
            "profiles": build_char_profile_stub_list(mine),
            "active_profile_id": _active_profile_id(session, uid),
            "character_runtime_revision": int(getattr(session, "character_runtime_revision", 0) or 0),
            "spell_manifest_revision": int(getattr(session, "spell_manifest_revision", 0) or 0),
            "quick_actions_revision": int(getattr(session, "quick_actions_revision", 0) or 0),
        }})
        await manager.send_to(session.id, uid, {"type": "quick_actions_sync", "payload": build_quick_actions_sync_payload(session, uid)})


# Canonical activity definitions
CAMP_ACTIVITIES = {
    "cook":          {"label": "Cook",           "icon": "🍲", "description": "Prepare a hearty meal to restore spirits."},
    "keep_watch":    {"label": "Keep Watch",      "icon": "👁",  "description": "Stand guard and keep the camp safe through the night."},
    "tell_story":    {"label": "Tell Story",      "icon": "📖", "description": "Share a tale from your past around the fire."},
    "identify_item": {"label": "Identify Item",   "icon": "🔍", "description": "Study a mysterious item and uncover its properties."},
    "train":         {"label": "Train",           "icon": "⚔️",  "description": "Practice your combat or class techniques."},
    "pray":          {"label": "Pray",            "icon": "🙏", "description": "Commune with your deity or meditate in quiet."},
    "gamble":        {"label": "Gamble",          "icon": "🎲", "description": "Play dice or cards with fellow adventurers."},
    "bond_with_ally":{"label": "Bond with Ally",  "icon": "🤝", "description": "Strengthen a connection with a companion."},
}

ALL_ACTIVITY_IDS = list(CAMP_ACTIVITIES.keys())


def _safe_camp_rest(session: Session) -> dict:
    """Return the camp_rest dict, initialising if needed."""
    if not isinstance(session.camp_rest, dict):
        session.camp_rest = {
            "active": False,
            "label": "",
            "available_activities": [],
            "player_activities": {},
        }
    return session.camp_rest


async def _broadcast_camp_rest_state(session: Session):
    """Push the full camp_rest state to every connected user."""
    cr = _safe_camp_rest(session)
    payload = {
        "active": cr.get("active", False),
        "label": cr.get("label", ""),
        "available_activities": cr.get("available_activities", list(ALL_ACTIVITY_IDS)),
        "player_activities": dict(cr.get("player_activities") or {}),
        "activity_defs": CAMP_ACTIVITIES,
    }
    for uid in list(session.users.keys()):
        await manager.send_to(session.id, uid, {
            "type": "camp_rest_sync",
            "payload": payload,
        })


async def handle_camp_rest_start(payload: dict, session: Session, user: User):
    """DM opens a camp/rest scene."""
    if user.role != "dm":
        return

    label = str(payload.get("label") or "Making Camp").strip()[:120]
    raw_acts = payload.get("available_activities")
    if isinstance(raw_acts, list):
        available = [a for a in raw_acts if a in CAMP_ACTIVITIES]
    else:
        available = list(ALL_ACTIVITY_IDS)

    cr = _safe_camp_rest(session)
    cr["active"] = True
    cr["label"] = label
    cr["available_activities"] = available
    # Clear previous selections when starting a new scene
    cr["player_activities"] = {}

    session.add_log(f"Camp/rest scene started: {label}", "system", "DM")
    await _broadcast_camp_rest_state(session)
    await save_campaign_async(session)


async def handle_camp_rest_end(payload: dict, session: Session, user: User):
    """DM closes the camp/rest scene."""
    if user.role != "dm":
        return

    cr = _safe_camp_rest(session)
    cr["active"] = False
    cr["player_activities"] = {}

    session.add_log("Camp/rest scene ended.", "system", "DM")
    await _broadcast_camp_rest_state(session)
    await save_campaign_async(session)


async def handle_camp_rest_activity_select(payload: dict, session: Session, user: User):
    """A player (or the DM on behalf of a token) selects a downtime activity."""
    cr = _safe_camp_rest(session)
    if not cr.get("active"):
        await manager.send_to(session.id, user.id, {
            "type": "notification",
            "payload": {"message": "No active camp/rest scene.", "kind": "warning"},
        })
        return

    activity_id = str(payload.get("activity_id") or "").strip()
    if not activity_id or activity_id not in CAMP_ACTIVITIES:
        return

    # DM may restrict available activities
    available = cr.get("available_activities") or ALL_ACTIVITY_IDS
    if activity_id not in available:
        await manager.send_to(session.id, user.id, {
            "type": "notification",
            "payload": {"message": "That activity is not available at this camp.", "kind": "warning"},
        })
        return

    note = str(payload.get("note") or "").strip()[:200]
    player_activities = dict(cr.get("player_activities") or {})
    player_activities[user.id] = {
        "user_id": user.id,
        "user_name": user.name,
        "activity_id": activity_id,
        "note": note,
        "selected_at": time.time(),
    }
    cr["player_activities"] = player_activities

    act = CAMP_ACTIVITIES[activity_id]
    log_msg = f"{user.name} chose to {act['label'].lower()} during the rest."
    if note:
        log_msg += f' — "{note}"'
    session.add_log(log_msg, "camp_rest", user.name)

    await _broadcast_camp_rest_state(session)

    # Broadcast a chat-style log entry so everyone sees the narrative
    await manager.broadcast(session.id, {
        "type": "chat_message",
        "payload": {
            "user": user.name,
            "message": log_msg,
            "channel": "everyone",
            "msg_type": "camp_rest",
        },
    })


async def handle_camp_rest_update_activities(payload: dict, session: Session, user: User):
    """DM updates the list of available activities mid-scene."""
    if user.role != "dm":
        return

    raw_acts = payload.get("available_activities")
    if not isinstance(raw_acts, list):
        return

    available = [a for a in raw_acts if a in CAMP_ACTIVITIES]
    cr = _safe_camp_rest(session)
    cr["available_activities"] = available
    await _broadcast_camp_rest_state(session)
    await save_campaign_async(session)


async def handle_camp_rest_clear_activity(payload: dict, session: Session, user: User):
    """Remove a player's activity selection (DM can clear any; players clear their own)."""
    target_user_id = str(payload.get("user_id") or user.id)
    if user.role != "dm" and target_user_id != user.id:
        return

    cr = _safe_camp_rest(session)
    player_activities = dict(cr.get("player_activities") or {})
    player_activities.pop(target_user_id, None)
    cr["player_activities"] = player_activities
    await _broadcast_camp_rest_state(session)


async def handle_camp_rest_take_rest(payload: dict, session: Session, user: User):
    """DM triggers a short or long rest, applying HP and resource recovery."""
    if user.role != "dm":
        await manager.send_to(session.id, user.id, {
            "type": "notification",
            "payload": {"message": "Only the DM can trigger a rest.", "kind": "warning"},
        })
        return

    rest_type = str(payload.get("rest_type") or "long").strip().lower()
    if rest_type not in ("short", "long"):
        rest_type = "long"

    healed_tokens = []
    removed_temp_summon_tokens: list[str] = []
    item_recharge_updates = []

    if rest_type == "long":
        # Long rest (5e): all player tokens restored to full HP, temp HP cleared.
        # Spell slots are reset client-side when they receive the event.
        for tid, token in list(session.tokens.items()):
            owner = getattr(token, "owner_id", None)
            if not owner:
                continue  # Skip DM-owned tokens
            max_hp = getattr(token, "max_hp", None)
            if max_hp is None:
                continue
            old_hp = int(getattr(token, "hp") or 0)
            token.hp = int(max_hp)
            token.temp_hp = 0
            _sync_combatant_token_state(session, token, previous_hp=old_hp)
            owner_user = (getattr(session, "users", {}) or {}).get(str(owner))
            if owner_user:
                _sync_profile_runtime_for_user(session, owner_user, token=token, rest_type="long")
            healed_tokens.append({
                "token_id": tid,
                "name": getattr(token, "name", "Unknown"),
                "hp": token.hp,
                "max_hp": int(max_hp),
            })

        from server.handlers.inventory import refresh_item_charges_for_rest
        for uid, session_user in list((getattr(session, "users", {}) or {}).items()):
            if getattr(session_user, "role", "") != "player":
                continue
            _sync_profile_runtime_for_user(session, session_user, rest_type="long")
            updated = refresh_item_charges_for_rest(session, session_user, "long")
            if updated:
                item_recharge_updates.append({
                    "user_id": uid,
                    "user_name": getattr(session_user, "name", "Player"),
                    "items": updated,
                })

        log_msg = "🌙 Long rest — all party members restored to full HP. Spell slots refreshed."
        rest_label = "Long Rest"

    else:
        # Short rest (5e): no automatic HP recovery — players spend hit dice.
        # Broadcast the event so each client can show the hit dice UI.
        from server.handlers.inventory import refresh_item_charges_for_rest
        for uid, session_user in list((getattr(session, "users", {}) or {}).items()):
            if getattr(session_user, "role", "") != "player":
                continue
            _sync_profile_runtime_for_user(session, session_user, rest_type="short")
            updated = refresh_item_charges_for_rest(session, session_user, "short")
            if updated:
                item_recharge_updates.append({
                    "user_id": uid,
                    "user_name": getattr(session_user, "name", "Player"),
                    "items": updated,
                })

        log_msg = "☀ Short rest — party may spend hit dice to recover HP."
        rest_label = "Short Rest"

    profiles = dict(getattr(session, "char_profiles", {}) or {})
    for owner_key, rows in list(profiles.items()):
        if not isinstance(rows, list):
            continue
        bucket = list(rows)
        changed = False
        for idx, row in enumerate(bucket):
            if not isinstance(row, dict):
                continue
            native = row.get("nativeCharacter") if isinstance(row.get("nativeCharacter"), dict) else {}
            removed = prune_expired_temporary_summons(native, rest_type=rest_type)
            if not removed:
                continue
            changed = True
            row["nativeCharacter"] = native
            bucket[idx] = row
            for entry in removed:
                tok_id = str(entry.get("tokenId") or "").strip()
                if tok_id and tok_id in (session.tokens or {}):
                    session.tokens.pop(tok_id, None)
                    removed_temp_summon_tokens.append(tok_id)
                    await manager.broadcast(session.id, {"type": "token_deleted", "payload": {"token_id": tok_id}})
        if changed:
            profiles[owner_key] = bucket
    session.char_profiles = profiles

    session.add_log(log_msg, "camp_rest", "DM")
    bump_character_hydration_revisions(session, spells=True, quick_actions=True)
    await _broadcast_character_and_quick_actions(session)

    # Sync token state across all clients if HP changed
    if healed_tokens:
        await _broadcast_token_state_sync(session)

    # Broadcast rest result so clients can update spell slots / show hit dice UI
    await manager.broadcast(session.id, {
        "type": "camp_rest_rest_applied",
        "payload": {
            "rest_type": rest_type,
            "label": rest_label,
            "healed_tokens": healed_tokens,
            "item_recharge_updates": item_recharge_updates,
            "message": log_msg,
            "removed_temp_summon_tokens": removed_temp_summon_tokens,
        },
    })

    if rest_type == "long" or item_recharge_updates:
        from server.handlers.inventory import _broadcast_inventory_state
        await _broadcast_inventory_state(session)

    await manager.broadcast(session.id, {
        "type": "chat_message",
        "payload": {
            "user": "DM",
            "message": log_msg,
            "channel": "everyone",
            "msg_type": "camp_rest",
        },
    })

    await save_campaign_async(session)


async def handle_camp_rest_spend_hit_die(payload: dict, session: Session, user: User):
    """Player spends a hit die during a short rest — applies healing to an owned token."""
    cr = _safe_camp_rest(session)
    if not cr.get("active"):
        return

    heal_amount = int(payload.get("heal_amount") or 0)
    if heal_amount <= 0:
        return

    requested_token_id = str(payload.get("token_id") or "").strip()
    token = None

    if requested_token_id:
        token = (getattr(session, "tokens", {}) or {}).get(requested_token_id)
        if token is None:
            await manager.send_to(session.id, user.id, {
                "type": "notification",
                "payload": {"message": "Could not find that token to apply hit die healing.", "kind": "warning"},
            })
            return
        owner = getattr(token, "owner_id", None)
        if not owner or str(owner) != str(user.id):
            await manager.send_to(session.id, user.id, {
                "type": "notification",
                "payload": {"message": "You can only spend hit dice for one of your own tokens.", "kind": "warning"},
            })
            return
    else:
        # Legacy fallback: if the client does not specify a token, keep healing
        # the first token owned by this player to preserve existing clients.
        for tid, tok in list((getattr(session, "tokens", {}) or {}).items()):
            owner = getattr(tok, "owner_id", None)
            if owner and str(owner) == str(user.id):
                token = tok
                break

    if token is None:
        await manager.send_to(session.id, user.id, {
            "type": "notification",
            "payload": {"message": "Could not find your token to apply healing.", "kind": "warning"},
        })
        return

    actual = _apply_heal(token, heal_amount)
    _sync_combatant_token_state(session, token)
    _sync_profile_runtime_for_user(session, user, token=token, rest_type="short", heal_amount=actual)
    bump_character_hydration_revisions(session, spells=False, quick_actions=True)
    await _broadcast_token_state_sync(session)
    await _broadcast_character_and_quick_actions(session)

    token_name = getattr(token, "name", user.name)
    log_msg = f"☀ {token_name} spends a hit die and recovers {actual} HP ({token.hp}/{token.max_hp})."
    session.add_log(log_msg, "camp_rest", user.name)
    await manager.broadcast(session.id, {
        "type": "chat_message",
        "payload": {"user": user.name, "message": log_msg, "channel": "everyone", "msg_type": "camp_rest"},
    })
    # Notify the player of the result
    await manager.send_to(session.id, user.id, {
        "type": "camp_rest_hit_die_result",
        "payload": {"heal_amount": actual, "new_hp": token.hp, "max_hp": token.max_hp},
    })
    await save_campaign_async(session)


def _owned_player_token_for_rest(payload: dict, session: Session, user: User):
    """Resolve the token a player is allowed to rest."""
    requested = str(payload.get("token_id") or "").strip()
    if requested:
        token = (getattr(session, "tokens", {}) or {}).get(requested)
        if token is not None and str(getattr(token, "owner_id", "") or "") == str(user.id):
            return requested, token
        return None, None
    for tid, token in list((getattr(session, "tokens", {}) or {}).items()):
        if str(getattr(token, "owner_id", "") or "") == str(user.id):
            return tid, token
    return None, None


def _safe_rest_heal_amount(payload: dict) -> int:
    raw = payload.get("heal_amount", payload.get("healed_amount", 0))
    try:
        amount = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    if amount <= 0:
        return 0
    # Character-sheet hit-dice healing is client-rolled today; keep the server
    # authoritative by clamping to a sane per-rest amount and to the token's
    # missing HP before applying it.
    return min(amount, 500)


def _apply_rest_heal_without_overflow(token, amount: int) -> int:
    if token is None or getattr(token, "hp", None) is None:
        return 0
    try:
        current = int(getattr(token, "hp") or 0)
        max_hp = int(getattr(token, "max_hp") or 0)
    except (TypeError, ValueError):
        return 0
    if max_hp <= 0:
        return 0
    actual = min(max(0, int(amount or 0)), max(0, max_hp - current))
    token.hp = current + actual
    return actual


def _safe_optional_rest_hp(payload: dict, key: str) -> int | None:
    if key not in payload:
        return None
    try:
        value = int(payload.get(key))
    except (TypeError, ValueError):
        return None
    return max(0, min(value, 9999))


def _apply_short_rest_hp_contract(token, payload: dict) -> int:
    """Apply short-rest HP from either the new absolute contract or legacy delta.

    New clients send ``current_hp`` after rolling/spending hit dice.  Setting the
    absolute value makes the operation idempotent when an earlier token vitals
    edit reached the server before the final rest message.  Older clients only
    send ``heal_amount``/``healed_amount`` and retain the legacy additive path.
    """
    if token is None:
        return 0
    try:
        current = int(getattr(token, "hp") or 0)
        max_hp = int(getattr(token, "max_hp") or 0)
    except (TypeError, ValueError):
        return 0
    if max_hp <= 0:
        return 0
    absolute_hp = _safe_optional_rest_hp(payload, "current_hp")
    if absolute_hp is None:
        absolute_hp = _safe_optional_rest_hp(payload, "hp")
    if absolute_hp is not None:
        next_hp = min(max_hp, absolute_hp)
        token.hp = next_hp
        # Short rests do not grant or clear temp HP; preserve it unless the
        # client included the authoritative current temp value.
        temp_hp = _safe_optional_rest_hp(payload, "temp_hp")
        if temp_hp is not None:
            token.temp_hp = temp_hp
        return max(0, next_hp - current)
    return _apply_rest_heal_without_overflow(token, _safe_rest_heal_amount(payload))


async def _prune_rest_summons(session: Session, rest_type: str) -> list[str]:
    removed_temp_summon_tokens: list[str] = []
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    for owner_key, rows in list(profiles.items()):
        if not isinstance(rows, list):
            continue
        bucket = list(rows)
        changed = False
        for idx, row in enumerate(bucket):
            if not isinstance(row, dict):
                continue
            native = row.get("nativeCharacter") if isinstance(row.get("nativeCharacter"), dict) else {}
            removed = prune_expired_temporary_summons(native, rest_type=rest_type)
            if not removed:
                continue
            changed = True
            row["nativeCharacter"] = native
            bucket[idx] = row
            for entry in removed:
                tok_id = str(entry.get("tokenId") or "").strip()
                if tok_id and tok_id in (session.tokens or {}):
                    session.tokens.pop(tok_id, None)
                    removed_temp_summon_tokens.append(tok_id)
                    await manager.broadcast(session.id, {"type": "token_deleted", "payload": {"token_id": tok_id}})
        if changed:
            profiles[owner_key] = bucket
    session.char_profiles = profiles
    return removed_temp_summon_tokens


async def handle_character_self_rest(payload: dict, session: Session, user: User):
    """Server-authoritative player rest for one owned character token."""
    if user.role != "player":
        return

    rest_type = str(payload.get("rest_type") or "long").strip().lower()
    if rest_type not in ("short", "long"):
        return

    token_id, token = _owned_player_token_for_rest(payload, session, user)
    if token is None:
        await manager.send_to(session.id, user.id, {
            "type": "notification",
            "payload": {"message": "Could not find one of your tokens to rest.", "kind": "warning"},
        })
        return

    from server.handlers.inventory import refresh_item_charges_for_rest
    updated_items = refresh_item_charges_for_rest(session, user, rest_type)

    healed_amount = 0
    removed_temp_summon_tokens: list[str] = []
    if rest_type == "long":
        old_hp = int(getattr(token, "hp") or 0)
        max_hp = getattr(token, "max_hp", None)
        if max_hp is not None:
            token.hp = int(max_hp)
            healed_amount = max(0, int(token.hp or 0) - old_hp)
        token.temp_hp = 0
        _sync_combatant_token_state(session, token, previous_hp=old_hp)
        removed_temp_summon_tokens = await _prune_rest_summons(session, rest_type)
        await _broadcast_token_state_sync(session)
    else:
        heal_amount = _safe_rest_heal_amount(payload)
        has_absolute_hp = _safe_optional_rest_hp(payload, "current_hp") is not None or _safe_optional_rest_hp(payload, "hp") is not None
        if heal_amount > 0 or has_absolute_hp:
            old_hp = int(getattr(token, "hp") or 0)
            healed_amount = _apply_short_rest_hp_contract(token, payload)
            if int(getattr(token, "hp", old_hp) or 0) != old_hp or healed_amount > 0:
                _sync_combatant_token_state(session, token, previous_hp=old_hp)
                await _broadcast_token_state_sync(session)

    _sync_profile_runtime_for_user(session, user, token=token, rest_type=rest_type, heal_amount=healed_amount, hit_dice_state=payload.get("hit_dice_state") if isinstance(payload.get("hit_dice_state"), dict) else None)
    bump_character_hydration_revisions(session, spells=True, quick_actions=True)
    await _broadcast_character_and_quick_actions(session)

    char_name = str(payload.get("character_name") or getattr(token, "name", "") or user.name or "A character").strip()
    if rest_type == "long":
        log_msg = f"🌙 {char_name} completed a Long Rest."
    elif healed_amount > 0:
        log_msg = f"☀ {char_name} completed a Short Rest and recovered {healed_amount} HP."
    else:
        log_msg = f"☀ {char_name} completed a Short Rest."

    session.add_log(log_msg, "rest", user.name)

    if updated_items:
        from server.handlers.inventory import _broadcast_inventory_state
        await _broadcast_inventory_state(session)

    await manager.send_to(session.id, user.id, {
        "type": "character_rest_applied",
        "payload": {
            "rest_type": rest_type,
            "token_id": token_id,
            "hp": getattr(token, "hp", None),
            "max_hp": getattr(token, "max_hp", None),
            "temp_hp": getattr(token, "temp_hp", 0),
            "healed_amount": healed_amount,
            "items": updated_items,
            "message": log_msg,
            "removed_temp_summon_tokens": removed_temp_summon_tokens,
        },
    })

    await manager.broadcast(session.id, {
        "type": "chat_message",
        "payload": {"user": user.name, "message": log_msg, "channel": "everyone", "msg_type": "character_rest"},
    })

    await save_campaign_async(session)
