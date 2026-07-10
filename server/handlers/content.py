"""
server/handlers/content.py — Journal, library, character profiles, chat, and state handlers.
"""
import asyncio
import time
import secrets
import logging

logger = logging.getLogger(__name__)
from server.session import Session, User, normalize_profile_owner_key, set_assistant_dm_permissions, assistant_dm_has_scope, grant_temp_permission, ACTIVE_PROFILE_ID_KEY_LIMIT, set_player_gold_for_user, _inventory_owner_key, bump_character_hydration_revisions, build_quick_actions_sync_payload, _profile_runtime_summary
from server.character.profile_sanitize import strip_runtime_fields
from server.character.profile_assets import sanitize_profile_for_websocket
from server.character.profile_stub import build_char_profile_stub_list, char_profile_revision
from server.quest_library import (
    build_session_quest_from_template,
    get_quest_template,
    load_builtin_quest_templates,
)
from server.prep_pack_library import (
    get_prep_pack,
    load_builtin_prep_packs,
    prep_pack_catalog_view,
    build_import_instance_id,
    import_timestamp,
)
from server.quest_progress import (
    apply_dm_override,
    apply_objective_event,
    normalize_objective_list,
    normalize_quest_payload_shape,
    normalize_quest_status,
    rebuild_progress,
)
from server.quest_progression import (
    normalize_quest_progression_fields,
    resolve_session_quest_progression,
)
from server.quest_premium_progression import build_premium_progression_snapshot
from server.faction_reputation import apply_reputation_changes_to_session
from server.living_world_events import emit_world_event, consume_world_event
from server.handlers.common import (
    manager,
    save_campaign_async,
    _safe_int,
    _broadcast_token_state_sync,
    _sync_combatant_token_state,
    build_live_state_debug_summary,
)
from server.handlers.inventory import (
    _update_encumbrance_cache,
    _broadcast_inventory_state,
    _recompute_equipment_effects,
)


def _normalize_item_library_entry(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:80]
    if not name:
        return None
    category = str(raw.get("category") or "Gear").strip()[:40] or "Gear"
    rarity = str(raw.get("rarity") or "Common").strip()[:32] or "Common"
    notes = str(raw.get("notes") or "").strip()[:240]
    price = str(raw.get("price") or "").strip()[:32]
    default_qty = _safe_int(raw.get("default_qty"), 1, minimum=1, maximum=999)
    entry_id = str(raw.get("id") or secrets.token_hex(6)).strip()[:48] or secrets.token_hex(6)
    updated_at = float(raw.get("updated_at") or time.time())
    out = {
        "id": entry_id,
        "name": name,
        "category": category,
        "rarity": rarity,
        "default_qty": default_qty,
        "notes": notes,
        "updated_at": updated_at,
    }
    if price:
        out["price"] = price

    equipment_kind = str(raw.get("equipment_kind") or "").strip().lower()
    if equipment_kind in {"armor", "shield", "weapon", "gear"}:
        out["equipment_kind"] = equipment_kind

    armor_type = str(raw.get("armor_type") or "").strip().lower()
    if armor_type in {"light", "medium", "heavy"}:
        out["armor_type"] = armor_type

    handedness = str(raw.get("handedness") or "").strip().lower()
    if handedness in {"one_handed", "two_handed", "shield"}:
        out["handedness"] = handedness

    for int_key, minimum, maximum in (
        ("base_ac", 0, 99),
        ("dex_cap", -5, 20),
        ("ac_bonus", -10, 20),
        ("strength_requirement", 0, 30),
    ):
        if raw.get(int_key) is None or str(raw.get(int_key)).strip() == "":
            continue
        try:
            out[int_key] = _safe_int(raw.get(int_key), 0, minimum=minimum, maximum=maximum)
        except Exception:
            continue

    for text_key, limit in (
        ("damage_dice", 20),
        ("damage_type", 24),
        ("versatile_damage", 20),
    ):
        val = str(raw.get(text_key) or "").strip()[:limit]
        if val:
            out[text_key] = val

    if "stealth_disadvantage" in raw:
        out["stealth_disadvantage"] = bool(raw.get("stealth_disadvantage"))

    raw_props = raw.get("weapon_properties")
    if isinstance(raw_props, list):
        cleaned_props = []
        for prop in raw_props:
            text = str(prop or "").strip()[:32]
            if text and text not in cleaned_props:
                cleaned_props.append(text)
        if cleaned_props:
            out["weapon_properties"] = cleaned_props

    return out


async def _broadcast_journal_state(session: Session):
    all_entries = list(getattr(session, "journal_entries", []) or [])
    for uid, u in session.users.items():
        visible = all_entries if u.role == "dm" else [e for e in all_entries if e.get("shared")]
        await manager.send_to(session.id, uid, {
            "type": "journal_sync",
            "payload": {"entries": visible}
        })


def _session_user_subgroup(session: Session, user_id: str) -> str:
    try:
        return session.get_user_subgroup_id(user_id)
    except Exception:
        return "main"


def _handout_targets_user(handout: dict, session: Session, user_id: str) -> bool:
    recipients = handout.get("recipients", "all")
    if recipients == "all":
        return True
    if not isinstance(recipients, list):
        return False
    subgroup_id = _session_user_subgroup(session, user_id)
    for target in recipients:
        value = str(target or "").strip()
        if not value:
            continue
        if value == user_id:
            return True
        if value.startswith("subgroup:") and str(value.split(":", 1)[1] or "").strip().lower() == subgroup_id:
            return True
    return False



async def _broadcast_item_library_state(session: Session):
    """Broadcast session item library entries plus the current SRD version."""
    from server.item_library_srd import get_srd_items_version

    entries = list(getattr(session, "item_library_entries", []) or [])
    srd_items_version = get_srd_items_version()
    for uid in session.users.keys():
        await manager.send_to(session.id, uid, {
            "type": "item_library_sync",
            "payload": {"entries": entries, "srd_items_version": srd_items_version},
        })


async def handle_srd_items_request(payload: dict, session: Session, user: User):
    """Send the full SRD item list only to the requesting user."""
    from server.item_library_srd import get_srd_items_payload

    await manager.send_to(session.id, user.id, {
        "type": "srd_items_response",
        "payload": get_srd_items_payload(),
    })


async def _broadcast_encounter_template_state(session: Session):
    templates = list(getattr(session, "encounter_templates", []) or [])
    for uid, u in session.users.items():
        visible = templates if getattr(u, "role", "") == "dm" else []
        await manager.send_to(session.id, uid, {
            "type": "encounter_templates_sync",
            "payload": {"templates": visible},
        })


def _char_profile_bucket_key(session: Session, user: User) -> str:
    owner_key = normalize_profile_owner_key(getattr(user, "name", ""))
    if owner_key:
        return owner_key
    return user.id


def _get_char_profiles_for_user(session: Session, user: User):
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    owner_key = _char_profile_bucket_key(session, user)
    mine = list(profiles.get(owner_key, []) or [])
    if not mine and user.id in profiles:
        mine = list(profiles.get(user.id, []) or [])
        if mine:
            profiles[owner_key] = mine
            profiles.pop(user.id, None)
            session.char_profiles = profiles
    return profiles, owner_key, mine




def _safe_profile_inventory_item(raw):
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:80]
    if not name:
        return None
    try:
        qty = int(raw.get("qty") or raw.get("quantity") or 1)
    except Exception:
        qty = 1
    out = {"name": name, "qty": max(1, min(9999, qty))}
    for key in ("notes", "price", "source", "id", "category", "item_type", "equipment_kind", "armor_type", "handedness", "equip_slot", "damage_dice", "damage_type", "versatile_damage"):
        value = str(raw.get(key) or "").strip()
        if value:
            out[key] = value
    for key in ("equipped", "is_container", "extradimensional", "is_devouring"):
        if key in raw:
            out[key] = bool(raw.get(key))
    for key in ("weight_lbs", "own_weight_lbs", "capacity_lbs", "volume_ft3", "base_ac", "dex_cap", "ac_bonus", "strength_requirement"):
        if raw.get(key) is None or str(raw.get(key)).strip() == "":
            continue
        try:
            out[key] = int(raw.get(key)) if key in {"base_ac", "dex_cap", "ac_bonus", "strength_requirement"} else float(raw.get(key))
        except Exception:
            continue
    if isinstance(raw.get("weapon_properties"), list):
        cleaned = [str(v or "").strip()[:32] for v in (raw.get("weapon_properties") or []) if str(v or "").strip()]
        if cleaned:
            out["weapon_properties"] = cleaned[:12]
    if isinstance(raw.get("bag_contents"), list):
        out["bag_contents"] = [entry for entry in (_safe_profile_inventory_item(v) for v in raw.get("bag_contents") or []) if entry]
    return out


def _currency_dict_to_gold_units(currency: dict) -> int:
    if not isinstance(currency, dict):
        return 0
    try:
        cp = int(currency.get("cp") or 0)
        sp = int(currency.get("sp") or 0)
        ep = int(currency.get("ep") or 0)
        gp = int(currency.get("gp") or 0)
        pp = int(currency.get("pp") or 0)
    except Exception:
        return 0
    cp = max(0, cp); sp = max(0, sp); ep = max(0, ep); gp = max(0, gp); pp = max(0, pp)
    return (pp * 1000) + (gp * 100) + (ep * 50) + (sp * 10) + cp


def _hydrate_active_profile_inventory_state(session: Session, user: User, profile: dict) -> None:
    if not isinstance(profile, dict):
        return
    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    equipment = native.get("equipment") if isinstance(native.get("equipment"), dict) else {}
    inventory_rows = equipment.get("inventory") if isinstance(equipment.get("inventory"), list) else []
    currency = equipment.get("currency") if isinstance(equipment.get("currency"), dict) else {}
    wallet_units = equipment.get("walletGoldUnits")

    normalized_inventory = [row for row in (_safe_profile_inventory_item(item) for item in inventory_rows) if row]
    inventories = dict(getattr(session, "player_inventories", {}) or {})
    owner_key = _inventory_owner_key(session, user)
    inventories[owner_key] = normalized_inventory
    session.player_inventories = inventories

    if wallet_units is None:
        wallet_units = _currency_dict_to_gold_units(currency)
    set_player_gold_for_user(session, user.id, wallet_units)

def _link_owned_tokens_to_profile(session: Session, user: User, profile: dict) -> bool:
    if not isinstance(profile, dict):
        return False
    profile_id = str(profile.get("id") or "").strip()[:ACTIVE_PROFILE_ID_KEY_LIMIT]
    if not profile_id:
        return False
    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    identity = native.get("identity") if isinstance(native.get("identity"), dict) else {}
    changed = False
    for token in (getattr(session, "tokens", {}) or {}).values():
        if str(getattr(token, "owner_id", "") or "") != str(getattr(user, "id", "") or ""):
            continue
        if getattr(token, "profile_id", "") != profile_id:
            token.profile_id = profile_id
            changed = True
        library_id = str(profile.get("libraryId") or profile.get("library_id") or profile_id).strip()[:120]
        if library_id and getattr(token, "library_id", "") != library_id:
            token.library_id = library_id
            changed = True
        character_id = str(profile.get("characterId") or profile.get("character_id") or identity.get("characterId") or identity.get("id") or "").strip()[:120]
        if character_id and getattr(token, "character_id", "") != character_id:
            token.character_id = character_id
            changed = True
    return changed


def _apply_profile_max_hp_to_linked_tokens(session: Session, profile: dict, *, owner_id: str | None = None) -> bool:
    """Apply a profile's (possibly edited) max HP to tokens linked to it.

    Sheet edits are authoritative for max HP only — current HP changes solely
    through game actions, so it is never rewritten here. If the new max drops
    below the live current HP, current is clamped down to the new max.
    When *owner_id* is given, only that user's tokens are touched.
    """
    if not isinstance(profile, dict):
        return False
    profile_id = str(profile.get("id") or "").strip()
    if not profile_id:
        return False
    max_hp = _profile_runtime_summary(profile).get("maxHp")
    if max_hp is None:
        return False
    max_hp = max(1, int(max_hp))
    changed = False
    for token in (getattr(session, "tokens", {}) or {}).values():
        if owner_id is not None and str(getattr(token, "owner_id", "") or "") != str(owner_id):
            continue
        if str(getattr(token, "profile_id", "") or "") != profile_id:
            continue
        token_changed = False
        if getattr(token, "max_hp", None) != max_hp:
            token.max_hp = max_hp
            token_changed = True
        current = getattr(token, "hp", None)
        if current is not None and int(current) > max_hp:
            token.hp = max_hp
            token_changed = True
        if token_changed:
            _sync_combatant_token_state(session, token)
            changed = True
    return changed


def _resolve_profile_level(payload: dict) -> int | None:
    """Resolve a canonical profile level while preserving legacy payload shapes."""
    if not isinstance(payload, dict):
        return None

    def _as_level(raw):
        if raw is None:
            return None
        try:
            return _safe_int(raw, 0, minimum=0, maximum=30)
        except Exception:
            return None

    direct = _as_level(payload.get("level"))
    if direct is not None:
        return direct

    char_book = payload.get("charBook") if isinstance(payload.get("charBook"), dict) else {}
    book_level = _as_level(char_book.get("level"))
    if book_level is not None:
        return book_level

    char_sheet = payload.get("charSheet") if isinstance(payload.get("charSheet"), dict) else {}
    for key in ("totalLevel", "level"):
        sheet_level = _as_level(char_sheet.get(key))
        if sheet_level is not None:
            return sheet_level

    classes = char_sheet.get("classes") if isinstance(char_sheet.get("classes"), list) else []
    if classes:
        total = 0
        seen = False
        for cls in classes:
            if not isinstance(cls, dict):
                continue
            lvl = _as_level(cls.get("level"))
            if lvl is None:
                continue
            seen = True
            total += max(0, lvl)
        if seen:
            return _safe_int(total, 0, minimum=0, maximum=30)

    return None



def _normalize_character_notes(payload: dict, existing: dict | None = None) -> dict:
    """Normalize the canonical per-character live notes widget state."""
    existing_notes = existing.get("characterNotes") if isinstance(existing, dict) and isinstance(existing.get("characterNotes"), dict) else {}
    raw = payload.get("characterNotes") if isinstance(payload.get("characterNotes"), dict) else {}
    legacy_text = payload.get("notes")
    char_book = payload.get("charBook") if isinstance(payload.get("charBook"), dict) else {}
    char_sheet = payload.get("charSheet") if isinstance(payload.get("charSheet"), dict) else {}

    def _text(key: str, *fallbacks, limit: int = 12000) -> str:
        for value in (raw.get(key), *fallbacks, existing_notes.get(key)):
            if value is None:
                continue
            return str(value)[:limit]
        return ""

    def _num_pair(key: str, defaults: tuple[int, int], limits: tuple[int, int]) -> dict:
        raw_pair = raw.get(key) if isinstance(raw.get(key), dict) else {}
        existing_pair = existing_notes.get(key) if isinstance(existing_notes.get(key), dict) else {}
        out = {}
        for axis, default, maximum in ((limits[0], defaults[0], 4096), (limits[1], defaults[1], 4096)):
            try:
                value = int(raw_pair.get(axis, existing_pair.get(axis, default)))
            except (TypeError, ValueError):
                value = default
            out[axis] = max(0, min(maximum, value))
        return out

    session_text = _text("session", char_book.get("sessionNotes"), char_sheet.get("sessionNotes"))
    private_text = _text("private", legacy_text, char_book.get("campaignNotes"), char_sheet.get("notes"))
    updated_raw = raw.get("updated_at") or existing_notes.get("updated_at") or ""
    updated_at = str(updated_raw or "")[:40]
    if not updated_at and (session_text or private_text):
        updated_at = str(time.time())

    return {
        "session": session_text,
        "private": private_text,
        "updated_at": updated_at,
        "pinned": bool(raw.get("pinned", existing_notes.get("pinned", False))),
        "minimized": bool(raw.get("minimized", existing_notes.get("minimized", False))),
        "widget_position": _num_pair("widget_position", (100, 100), ("x", "y")),
        "widget_size": _num_pair("widget_size", (320, 260), ("width", "height")),
    }

def upsert_char_profile_for_owner(session: Session, owner_key: str, payload: dict) -> dict:
    """Upsert a character profile in a specific owner bucket (legacy-compatible)."""
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    mine = list(profiles.get(owner_key, []) or [])
    profile_id = str(payload.get("id") or secrets.token_hex(6))
    now = time.time()
    resolved_level = _resolve_profile_level(payload)
    sheet_payload = payload.get("charSheet") if isinstance(payload.get("charSheet"), dict) else {}
    if resolved_level is not None:
        sheet_payload = dict(sheet_payload)
        sheet_payload["level"] = resolved_level
        sheet_payload["totalLevel"] = resolved_level

    existing = next((p for p in mine if p.get("id") == profile_id), None)
    character_notes = _normalize_character_notes(payload, existing)

    profile = {
        "id": profile_id,
        "name": str(payload.get("name", "") or "").strip()[:80] or "Character",
        "charBook": payload.get("charBook") or {},
        "updated_at": now,
        # Persist quick-panel stats so they survive reconnect
        "curhp": payload.get("curhp"),
        "hp": payload.get("hp"),
        "tempHp": payload.get("tempHp"),
        "initiative": payload.get("initiative"),
        "ac": payload.get("ac"),
        "speed": payload.get("speed"),
        "level": resolved_level,
        "passive": payload.get("passive"),
        "faction": str(payload.get("faction") or "")[:80],
        "notes": str(character_notes.get("private", ""))[:500],
        "characterNotes": character_notes,
        "classId": str(payload.get("classId") or "")[:40],
        "color": str(payload.get("color") or "")[:16],
        "accentColor": str(payload.get("accentColor") or "")[:16],
        "diceTheme": str(payload.get("diceTheme") or "")[:24],
        "portraitFrame": str(payload.get("portraitFrame") or "")[:24],
        "tagline": str(payload.get("tagline") or "")[:120],
        # Full charSheet runtime state (spellbook, slot state, etc.)
        "charSheet": sheet_payload,
    }
    for key in ("nativeCharacter", "nativeRuntime", "nativeMeta", "sourceMode", "classSummary", "importMeta"):
        if key in payload:
            profile[key] = payload.get(key)

    # Defence in depth: strip combat/UI runtime caches before this profile is
    # stored. The client already strips them, but a stale or third-party client
    # must not be able to re-bloat the persisted char_profiles field.
    strip_runtime_fields(profile)

    if existing:
        created = existing.get("created_at", now)
        # Preserve ac_from_equipment flag so equipment-based AC tracking
        # survives profile updates (prevents AC reverting to 10).
        prev_ac_from_equip = existing.get("ac_from_equipment", False)
        existing.clear()
        existing.update(profile)
        existing["created_at"] = created
        # If the user manually set AC in this upsert, mark it as non-equipment.
        # Otherwise preserve the previous equipment flag.
        if payload.get("ac") is not None:
            existing["ac_from_equipment"] = False
        else:
            existing["ac_from_equipment"] = prev_ac_from_equip
        saved_profile = existing
    else:
        profile["created_at"] = now
        mine.append(profile)
        saved_profile = profile
    profiles[owner_key] = mine
    session.char_profiles = profiles
    return saved_profile


async def _send_char_profiles(session: Session, user_id: str):
    user = session.users.get(user_id)
    profiles = []
    if user:
        all_profiles, owner_key, mine = _get_char_profiles_for_user(session, user)
        profiles = mine
    # Stubs only: the full body is never pushed. Clients holding a cached body
    # compare each stub's revision and re-fetch via char_profile_fetch when it
    # changed (see handle_char_profile_fetch).
    await manager.send_to(session.id, user_id, {"type": "char_profiles_sync", "payload": {
        "profiles": build_char_profile_stub_list(profiles),
        "active_profile_id": str((getattr(session, "active_char_profiles", {}) or {}).get(user_id) or ""),
        "character_runtime_revision": int(getattr(session, "character_runtime_revision", 0) or 0),
        "spell_manifest_revision": int(getattr(session, "spell_manifest_revision", 0) or 0),
        "quick_actions_revision": int(getattr(session, "quick_actions_revision", 0) or 0),
    }})
    if user and getattr(user, "role", "") == "player":
        await manager.send_to(session.id, user_id, {"type": "quick_actions_sync", "payload": build_quick_actions_sync_payload(session, user_id)})


def _find_char_profile_for_fetch(session: Session, user: User, profile_id: str):
    """Locate a profile this user may fetch in full.

    Access must EXACTLY mirror state-sync visibility (to_state_dict_for_role):
    the DM sees every profile in the session; a player sees only their own
    owner bucket; every other role (viewer, chat_bridge, overlay,
    assistant_dm) receives no profiles from sync and therefore may not fetch
    any. The DM check is the session role established by the authenticated
    connect — the WS counterpart of request_has_dm_access — never a raw
    dm_id/ownership comparison.
    """
    role = str(getattr(user, "role", "") or "").strip().lower()
    if role == "dm":
        for owner_key, rows in (dict(getattr(session, "char_profiles", {}) or {})).items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and str(row.get("id") or "").strip() == profile_id:
                    return row, str(owner_key)
        return None, ""
    if role == "player":
        _, owner_key, mine = _get_char_profiles_for_user(session, user)
        for row in mine:
            if isinstance(row, dict) and str(row.get("id") or "").strip() == profile_id:
                return row, owner_key
        return None, ""
    return None, ""


async def handle_char_profile_fetch(payload: dict, session: Session, user: User):
    """On-demand full-profile fetch (sync itself only carries stubs).

    Always answers with char_profile_response so a denied/missing profile
    surfaces as a visible client error instead of a silently empty sheet.
    """
    profile_id = str((payload or {}).get("id") or "").strip()[:ACTIVE_PROFILE_ID_KEY_LIMIT]

    async def _respond_error(error: str):
        await manager.send_to(session.id, user.id, {"type": "char_profile_response", "payload": {
            "ok": False, "id": profile_id, "error": error,
        }})

    if not profile_id:
        return await _respond_error("Missing profile id.")
    role = str(getattr(user, "role", "") or "").strip().lower()
    if role not in {"dm", "player"}:
        return await _respond_error("You don't have permission to load character profiles.")
    profile, owner_key = _find_char_profile_for_fetch(session, user, profile_id)
    if not isinstance(profile, dict):
        return await _respond_error("Character profile not found.")
    await manager.send_to(session.id, user.id, {"type": "char_profile_response", "payload": {
        "ok": True,
        "id": profile_id,
        "owner_key": owner_key,
        "revision": char_profile_revision(profile),
        "profile": sanitize_profile_for_websocket(profile),
    }})


async def handle_journal_upsert(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    entries = list(getattr(session, "journal_entries", []) or [])
    entry_id = str(payload.get("id") or secrets.token_hex(6))
    now = time.time()
    title = str(payload.get("title", "") or "").strip()[:120] or "Untitled Entry"
    content_md = str(payload.get("content_md", payload.get("content", "")) or "")[:12000]
    content = str(payload.get("content", content_md) or "")[:12000]
    shared = bool(payload.get("shared", payload.get("is_public", False)))
    category = str(payload.get("category", "") or "").strip()[:40]
    poi_id = str(payload.get("poi_id", "") or "").strip()[:120] or None
    is_public = bool(payload.get("is_public", shared))
    existing = next((e for e in entries if e.get("id") == entry_id), None)
    if existing:
        existing.update({
            "id": entry_id,
            "title": title,
            "content": content,
            "content_md": content_md,
            "shared": shared,
            "is_public": is_public,
            "category": category,
            "poi_id": poi_id,
            "updated_at": now,
        })
    else:
        entries.append({
            "id": entry_id,
            "title": title,
            "content": content,
            "content_md": content_md,
            "shared": shared,
            "is_public": is_public,
            "category": category,
            "poi_id": poi_id,
            "created_at": now,
            "updated_at": now,
        })
    session.journal_entries = entries
    await _broadcast_journal_state(session)
    await save_campaign_async(session)


async def handle_journal_delete(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    entry_id = str(payload.get("id") or "")
    if not entry_id:
        return
    entries = [e for e in (getattr(session, "journal_entries", []) or []) if e.get("id") != entry_id]
    session.journal_entries = entries
    await _broadcast_journal_state(session)
    await save_campaign_async(session)



async def handle_item_library_upsert(payload: dict, session: Session, user: User):
    from server.handlers.inventory import _send_inventory_action_result
    if user.role != "dm":
        return
    entries = list(getattr(session, "item_library_entries", []) or [])
    entry = _normalize_item_library_entry(payload or {})
    if not entry:
        return await _send_inventory_action_result(session, user.id, "Item library entries need at least a name.")
    existing = next((e for e in entries if str(e.get("id") or "") == entry["id"]), None)
    if existing:
        created_at = float(existing.get("created_at") or time.time())
        existing.clear(); existing.update(entry); existing["created_at"] = created_at
    else:
        entry["created_at"] = time.time()
        entries.append(entry)
    entries.sort(key=lambda e: (str(e.get("category") or "").lower(), str(e.get("name") or "").lower()))
    session.item_library_entries = entries
    await _broadcast_item_library_state(session)
    await save_campaign_async(session)


async def handle_item_library_delete(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    entry_id = str(payload.get("id") or "").strip()[:48]
    if not entry_id:
        return
    session.item_library_entries = [e for e in (getattr(session, "item_library_entries", []) or []) if str(e.get("id") or "") != entry_id]
    await _broadcast_item_library_state(session)
    await save_campaign_async(session)


async def handle_char_profile_upsert(payload: dict, session: Session, user: User):
    owner_key = _char_profile_bucket_key(session, user)
    saved_profile = upsert_char_profile_for_owner(session, owner_key, payload)
    active_profile_id = str((getattr(session, "active_char_profiles", {}) or {}).get(user.id) or "").strip()
    if active_profile_id and str(saved_profile.get("id") or "").strip() == active_profile_id:
        _hydrate_active_profile_inventory_state(session, user, saved_profile)
    bump_character_hydration_revisions(session, spells=True, quick_actions=True)
    # Refresh encumbrance so STR/size changes are reflected immediately.
    # Also recompute equipment AC so DEX changes update token.ac before broadcasting.
    # This runs BEFORE the stub push: equipment recompute can write back into
    # the stored profile, and the pushed stub revision must reflect the final
    # content or clients would invalidate their body cache on the next sync.
    _update_encumbrance_cache(session, user.id)
    _recompute_equipment_effects(session, user)
    await _send_char_profiles(session, user.id)
    await _broadcast_inventory_state(session)
    _link_owned_tokens_to_profile(session, user, saved_profile)
    _apply_profile_max_hp_to_linked_tokens(session, saved_profile, owner_id=user.id)
    await _broadcast_token_state_sync(session)
    await save_campaign_async(session)


async def handle_char_profile_select(payload: dict, session: Session, user: User):
    profile_id = str(payload.get("id") or "").strip()[:ACTIVE_PROFILE_ID_KEY_LIMIT]
    _, _, mine = _get_char_profiles_for_user(session, user)
    valid_ids = {str((entry or {}).get("id") or "").strip() for entry in mine if isinstance(entry, dict)}
    if profile_id and profile_id not in valid_ids:
        return
    active_map = dict(getattr(session, "active_char_profiles", {}) or {})
    selected_profile = None
    if profile_id:
        active_map[user.id] = profile_id
        selected_profile = next((entry for entry in mine if isinstance(entry, dict) and str(entry.get("id") or "").strip() == profile_id), None)
    else:
        active_map.pop(user.id, None)
    session.active_char_profiles = active_map
    bump_character_hydration_revisions(session, spells=True, quick_actions=True)
    logger.info("[live_state] active_profile_update %s", build_live_state_debug_summary(session, user.id, user.role, {"active_profile_id": profile_id}))
    if selected_profile:
        _hydrate_active_profile_inventory_state(session, user, selected_profile)
    _update_encumbrance_cache(session, user.id)
    _recompute_equipment_effects(session, user)
    await _broadcast_inventory_state(session)
    if selected_profile:
        _link_owned_tokens_to_profile(session, user, selected_profile)
        _apply_profile_max_hp_to_linked_tokens(session, selected_profile, owner_id=user.id)
    await _broadcast_token_state_sync(session)
    await save_campaign_async(session)


async def handle_char_profile_delete(payload: dict, session: Session, user: User):
    profiles, owner_key, mine = _get_char_profiles_for_user(session, user)
    profile_id = str(payload.get("id") or "")
    if not profile_id:
        return
    mine = [p for p in mine if p.get("id") != profile_id]
    profiles[owner_key] = mine
    session.char_profiles = profiles
    active_map = dict(getattr(session, "active_char_profiles", {}) or {})
    if str(active_map.get(user.id) or "") == profile_id:
        replacement_id = str((mine[0] or {}).get("id") or "").strip() if mine else ""
        if replacement_id:
            active_map[user.id] = replacement_id
        else:
            active_map.pop(user.id, None)
        session.active_char_profiles = active_map
        _update_encumbrance_cache(session, user.id)
        _recompute_equipment_effects(session, user)
        await _broadcast_inventory_state(session)
    await _send_char_profiles(session, user.id)
    await save_campaign_async(session)


def _dice_roll_audience(session: Session, roller: User, visibility: str = "private") -> set[str]:
    """User ids that should receive a dice roll's popup / special FX / private log.

    Visibility tiers (default ``private``):
      * ``private`` — the roller only. Nobody else sees the big dice popup.
      * ``dm``      — the roller plus every connected DM.
      * ``table``   — everyone in the session (normally delivered via
                      ``manager.broadcast``; the full id set is returned here for
                      completeness/testing).
    """
    roller_id = str(getattr(roller, "id", "") or "")
    if visibility == "table":
        audience = {str(uid) for uid in (getattr(session, "users", {}) or {}).keys()}
        audience.add(roller_id)
    elif visibility == "dm":
        audience = {roller_id}
        for uid, u in (getattr(session, "users", {}) or {}).items():
            if str(getattr(u, "role", "") or "").strip().lower() == "dm":
                audience.add(str(uid))
    else:  # private (default) — roller only
        audience = {roller_id}
    audience.discard("")
    return audience


async def handle_dice_roll(payload: dict, session: Session, user: User):
    import random
    dice_type = int(payload.get("dice_type", 20))
    quantity = _safe_int(payload.get("quantity"), 1, minimum=1, maximum=20)
    modifier = _safe_int(payload.get("modifier"), 0, minimum=-999, maximum=999)
    roll_label = str(payload.get("roll_label", "") or "").strip()[:80]
    mode = str(payload.get("mode") or "server-authoritative").strip()[:40] or "server-authoritative"
    seed = _safe_int(payload.get("seed"), random.randint(1, 999_999_999), minimum=1, maximum=999_999_999)
    roll_id = str(payload.get("roll_id") or f"roll-{secrets.token_hex(6)}").strip()[:64]
    theme = str(payload.get("theme") or "").strip()[:64]

    if dice_type not in (4, 6, 8, 10, 12, 20, 100):
        return

    rng = random.Random(seed)
    percentile_pairs = []
    if dice_type == 100:
        rolls = []
        for _ in range(quantity):
            tens = rng.randint(0, 9) * 10
            ones = rng.randint(0, 9)
            result = 100 if tens == 0 and ones == 0 else tens + ones
            rolls.append(result)
            percentile_pairs.append({"tens": tens, "ones": ones, "result": result})
        base_total = sum(rolls)
        roll_str = ", ".join(f"{pair['tens']:02d}+{pair['ones']}→{pair['result']}" for pair in percentile_pairs)
    else:
        rolls = [rng.randint(1, dice_type) for _ in range(quantity)]
        base_total = sum(rolls)
        roll_str = ", ".join(str(r) for r in rolls)

    total = base_total + modifier

    if modifier or roll_label:
        sign = "+" if modifier > 0 else ""
        suffix = f" for {roll_label}" if roll_label else ""
        message = f"{user.name} rolled {quantity}d{dice_type}{suffix}: [{roll_str}] {sign}{modifier} = {total}"
    else:
        message = f"{user.name} rolled {quantity}d{dice_type}: [{roll_str}] = {total}"

    # Classify result sound for the audio_event broadcast
    def _result_sound(dt: int, r: list) -> str:
        if dt == 20 and quantity == 1:
            if r[0] == 20:
                return "dice_nat20"
            if r[0] == 1:
                return "dice_nat1"
        midpoint = dt * 0.5
        roll_avg = sum(r) / len(r)
        return "dice_high" if roll_avg >= midpoint else "dice_low"

    sounds = {
        "roll": f"dice_roll_d{dice_type}",
        "result": _result_sound(dice_type, rolls),
    }

    # Roll popups are private by default. The roller alone sees the big dice
    # result popup / special FX. The caller can widen this with an explicit
    # visibility flag:
    #   * "dm"    — the roller plus the DM(s)
    #   * "table" — the whole table (broadcast)
    # Initiative is NOT special-cased as public anymore: the combat initiative
    # *tracker* is synchronised separately through combat_state, so the order
    # still updates for everyone while the dice popup stays private to the
    # roller. (Previously any roll whose label contained "initiative" was
    # broadcast, so every player saw each other's initiative dice popup.)
    visibility = str(payload.get("visibility") or payload.get("roll_visibility") or "private").strip().lower()
    if visibility not in ("private", "dm", "table"):
        visibility = "private"

    result_payload = {
        "user_id": user.id,
        "user_name": user.name,
        "dice_type": dice_type,
        "quantity": quantity,
        "rolls": rolls,
        "total": total,
        "modifier": modifier,
        "init_bonus": modifier,
        "roll_label": roll_label,
        "mode": mode,
        "seed": seed,
        "roll_id": roll_id,
        "theme": theme,
        "percentile_pairs": percentile_pairs,
        "sounds": sounds,
    }

    fx_type = ""
    if dice_type == 20 and quantity == 1 and len(rolls) == 1:
        fx_type = "nat20" if int(rolls[0]) == 20 else "nat1" if int(rolls[0]) == 1 else ""

    if visibility == "table":
        # Table roll: persisted to the shared session log (survives reconnect)
        # and broadcast to everyone, popup + FX included.
        log_entry = session.add_log(message, "dice", user.name)
        result_payload["log"] = log_entry
        await manager.broadcast(session.id, {"type": "dice_result", "payload": result_payload})
        if fx_type:
            await manager.broadcast(session.id, {
                "type": "dice_special_fx",
                "payload": {"user_id": user.id, "result": int(rolls[0]), "fx_type": fx_type},
            })
        return

    # Private / DM roll: the log entry is ephemeral (never persisted to the
    # shared session log, so it can't leak to other players through a later
    # state snapshot) and is delivered only to the calculated audience. The
    # special FX follow the exact same audience as the dice_result.
    private_log = {
        "id": secrets.token_hex(4),
        "timestamp": time.time(),
        "type": "dice",
        "user": user.name,
        "message": message,
        "private": True,
        "roll_user_id": user.id,
    }
    result_payload["log"] = private_log
    result_payload["private"] = True
    audience = _dice_roll_audience(session, user, visibility)
    for uid in audience:
        await manager.send_to(session.id, uid, {"type": "dice_result", "payload": result_payload})
    if fx_type:
        for uid in audience:
            await manager.send_to(session.id, uid, {
                "type": "dice_special_fx",
                "payload": {"user_id": user.id, "result": int(rolls[0]), "fx_type": fx_type},
            })


async def handle_dice_special_fx(payload: dict, session: Session, user: User):
    """Broadcast special dice FX (Nat 20 / Nat 1) to all other players in the session."""
    fx_type = str(payload.get("fx_type", ""))
    if fx_type not in ("nat20", "nat1"):
        return
    await manager.broadcast(session.id, {
        "type": "dice_special_fx",
        "payload": {
            "user_id":  str(payload.get("user_id", user.id)),
            "result":   payload.get("result"),
            "fx_type":  fx_type,
        },
    }, exclude_user=user.id)


async def handle_grant_permission(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return

    token_id = payload.get("token_id")
    target_user_id = payload.get("user_id")
    duration = int(payload.get("duration", 30))

    if grant_temp_permission(session, token_id, target_user_id, duration):
        token = session.tokens.get(token_id)
        target = session.users.get(target_user_id)
        log_entry = session.add_log(
            f"DM granted {target.name if target else '?'} temporary control of '{token.name if token else '?'}' for {duration}s.",
            "system"
        )
        await manager.broadcast(session.id, {
            "type": "permission_granted",
            "payload": {
                "token_id": token_id,
                "user_id": target_user_id,
                "duration": duration,
                "log": log_entry,
            }
        })




async def handle_assistant_dm_permissions_set(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    target_user_id = str(payload.get("user_id") or "").strip()[:64]
    target = session.users.get(target_user_id)
    if not target or target.role == "dm":
        return
    enabled = bool(payload.get("enabled", True))
    scopes = payload.get("scopes") if isinstance(payload.get("scopes"), list) else []
    token_ids = payload.get("token_ids") if isinstance(payload.get("token_ids"), list) else []
    map_contexts = payload.get("map_contexts") if isinstance(payload.get("map_contexts"), list) else []
    perms = set_assistant_dm_permissions(
        session,
        actor=user,
        target_user_id=target_user_id,
        enabled=enabled,
        scopes=scopes,
        token_ids=token_ids,
        map_contexts=map_contexts,
    )
    target.role = "assistant_dm" if enabled else "player"
    log_entry = session.add_log(
        f"DM updated assistant DM scope for {target.name}: {'enabled' if enabled else 'disabled'}.",
        "system"
    )
    await manager.broadcast(session.id, {
        "type": "assistant_dm_permissions_sync",
        "payload": {
            "user_id": target_user_id,
            "permissions": perms,
            "role": target.role,
            "users": {uid: {"id": u.id, "name": u.name, "role": u.role, "connected": u.connected} for uid, u in session.users.items()},
            "log": log_entry,
        }
    })
    await save_campaign_async(session)


async def handle_chat_message(payload: dict, session: Session, user: User):
    message = str(payload.get("message", "")).strip()[:500]
    if not message:
        return

    role = str(getattr(user, "role", "") or "").strip().lower()
    # Viewers are receive-only for chat to prevent spectator spam.
    if role == "viewer":
        return

    channel = str(payload.get("channel", "") or "").strip()

    # ── Viewer channel: only viewers + DM see these messages ──────────────────
    if channel == "viewers":
        if role != "viewer":
            return
        log_entry = session.add_log(message, "chat", user.name)
        log_entry["channel"] = "viewers"
        viewer_payload = {
            "type": "chat_message",
            "payload": {
                "user_name": user.name,
                "role": user.role,
                "message": message,
                "channel": "viewers",
                "log": log_entry,
            }
        }
        for uid, u in session.users.items():
            if str(getattr(u, "role", "") or "").strip().lower() in {"viewer", "dm"}:
                await manager.send_to(session.id, uid, viewer_payload)
        return

    # ── Whisper channel: sender + one target only ──────────────────────────────
    if channel == "whisper":
        target_user_id = str(payload.get("target_user_id", "") or "").strip()
        if not target_user_id:
            return
        target = session.users.get(target_user_id)
        if not target or target.id == user.id:
            return
        whisper_log = {
            "id": secrets.token_hex(4),
            "timestamp": time.time(),
            "type": "chat",
            "user": f"{user.name} → {target.name}",
            "message": message,
            "channel": "whisper",
            "whisper_to": target.name,
            "target_user_id": target.id,
            "target_user_name": target.name,
        }
        whisper_payload = {
            "type": "chat_message",
            "payload": {
                "user_name": user.name,
                "role": user.role,
                "message": message,
                "channel": "whisper",
                "whisper_to": target.name,
                "target_user_id": target.id,
                "target_user_name": target.name,
                "log": whisper_log,
            }
        }
        await manager.send_to(session.id, user.id, whisper_payload)
        await manager.send_to(session.id, target.id, whisper_payload)
        return

    # ── Legacy private whisper (whisper_to = user_id, dm/player only) ─────────
    whisper_to = str(payload.get("whisper_to", "") or "").strip()

    if whisper_to:
        if role not in {"dm", "player"}:
            return
        target = session.users.get(whisper_to)
        target_role = str(getattr(target, "role", "") or "").strip().lower() if target else ""
        if not target or target.id == user.id or target_role not in {"dm", "player"}:
            return

        whisper_log = {
            "id": secrets.token_hex(4),
            "timestamp": time.time(),
            "type": "chat",
            "user": f"{user.name} → {target.name}",
            "message": message,
            "private": True,
            "target_user_id": target.id,
            "target_user_name": target.name,
        }
        payload_msg = {
            "type": "chat_message",
            "payload": {
                "user_name": user.name,
                "role": user.role,
                "message": message,
                "private": True,
                "target_user_id": target.id,
                "target_user_name": target.name,
                "log": whisper_log,
            }
        }
        await manager.send_to(session.id, user.id, payload_msg)
        if target.id != user.id:
            await manager.send_to(session.id, target.id, payload_msg)
        return

    log_entry = session.add_log(message, "chat", user.name)
    await manager.broadcast(session.id, {
        "type": "chat_message",
        "payload": {
            "user_name": user.name,
            "role": user.role,
            "message": message,
            "log": log_entry,
        }
    })


async def handle_request_state(payload: dict, session: Session, user: User):
    """Send a state snapshot to the requesting user.

    Reconnecting clients include ``known_revisions`` (their live-tracked
    counters plus the ``domain_revisions`` map from their last sync); heavy
    domains whose content has not changed since are omitted from the response
    (see server/state_delta.py). Clients without ``known_revisions`` receive
    the full snapshot exactly as before.
    """
    from server.state_delta import apply_reconnect_delta

    reason = str((payload or {}).get("reason") or "").strip().lower()
    known_revisions = (payload or {}).get("known_revisions")
    now = time.monotonic()
    last_initial = float(getattr(user, "_last_initial_state_sync_at", 0.0) or 0.0)
    skip_duplicate_reconnect_state = reason == "reconnect" and last_initial and (now - last_initial) < 2.0
    if skip_duplicate_reconnect_state:
        logger.info(
            "[live_state] request_state skipped duplicate initial state_sync reason=%s session_id=%s user_id=%s age_ms=%.1f",
            reason, session.id, user.id, (now - last_initial) * 1000.0,
        )
    else:
        state = session.to_state_dict_for_role(user.role, user.id)
        apply_reconnect_delta(state, known_revisions if reason == "reconnect" else None)
        logger.info(
            "[live_state] request_state reason=%s delta=%s omitted_domains=%s summary=%s",
            reason, state.get("delta"), ",".join(state.get("omitted_domains") or []) or "none",
            build_live_state_debug_summary(session, user.id, user.role, state),
        )
        await manager.send_to(session.id, user.id, {
            "type": "state_sync",
            "payload": state
        })
    await manager.send_to(
        session.id,
        user.id,
        session.to_authoritative_snapshot_for_role(user.role, user.id, source="request_state"),
    )


async def _broadcast_session_quests(session: Session):
    session.session_quests = resolve_session_quest_progression(session)
    for uid, participant in session.users.items():
        role = str(getattr(participant, "role", "viewer") or "viewer")
        visible = session._visible_session_quests_for_role(role, uid)
        await manager.send_to(session.id, uid, {
            "type": "session_quests_sync",
            "payload": {
                "session_quests": visible,
                "quest_board_bindings": list(getattr(session, "quest_board_bindings", []) or []),
                "premium_progression": build_premium_progression_snapshot(session, role=role, user_id=uid),
            },
        })


async def _broadcast_quest_board_notice(session: Session, *, actor: User, quest: dict, scope: str, message: str, details: list[str] | None = None):
    payload = {
        "scope": scope,
        "quest_id": str((quest or {}).get("id") or ""),
        "quest_title": str((quest or {}).get("title") or "Quest")[:120],
        "message": str(message or "").strip()[:240],
        "details": list(details or [])[:8],
        "linked_handout_ids": _normalize_optional_ref_list((quest or {}).get("linked_handout_ids"), limit=16),
        "linked_map_ids": _normalize_optional_ref_list((quest or {}).get("linked_map_ids"), limit=16),
        "linked_poi_ids": _normalize_optional_ref_list((quest or {}).get("linked_poi_ids"), limit=16),
        "availability_state": str((quest or {}).get("availability_state") or ""),
        "status": str((quest or {}).get("status") or ""),
        "premium_progression": build_premium_progression_snapshot(session, role=actor.role, user_id=actor.id),
    }
    await manager.broadcast(session.id, {"type": "session_event_notice", "payload": payload})


def _normalize_quest_id_list(raw, *, limit: int = 40) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        quest_id = str(entry or "").strip()[:64]
        if not quest_id or quest_id in out:
            continue
        out.append(quest_id)
        if len(out) >= limit:
            break
    return out


def _normalize_optional_ref_list(raw, *, limit: int = 24) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        text = str(entry or "").strip()[:80]
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _normalize_reward_bundle(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    reputation_raw = raw.get("reputation")
    if isinstance(reputation_raw, list):
        reputation = []
        for row in reputation_raw[:30]:
            if not isinstance(row, dict):
                continue
            amount = _safe_int(row.get("delta"), 0, minimum=-10_000, maximum=10_000)
            faction_id = str(row.get("id") or "").strip()[:64]
            faction_name = str(row.get("name") or row.get("faction") or "").strip()[:80]
            if not (faction_id or faction_name) or not amount:
                continue
            visibility = str(row.get("visibility") or "party").strip().lower()[:16]
            if visibility not in {"party", "dm_only"}:
                visibility = "party"
            reputation.append({
                "id": faction_id,
                "name": faction_name,
                "tag": str(row.get("tag") or "").strip()[:48],
                "delta": amount,
                "visibility": visibility,
            })
    else:
        reputation = dict(reputation_raw) if isinstance(reputation_raw, dict) else {}
    bundle = {
        "distribution": "party",
        "gold": max(0, _safe_int(raw.get("gold"), 0, minimum=0, maximum=1_000_000_000)),
        "xp": max(0, _safe_int(raw.get("xp"), 0, minimum=0, maximum=1_000_000_000)),
        "reputation": reputation,
    }
    distribution = str(raw.get("distribution") or raw.get("mode") or "party").strip().lower()[:24]
    if distribution in {"personal", "player"}:
        bundle["distribution"] = "personal"
    if bundle["distribution"] == "personal":
        target_user_id = str(raw.get("target_user_id") or "").strip()[:64]
        if target_user_id:
            bundle["target_user_id"] = target_user_id

    items: list[dict] = []
    raw_items = raw.get("items")
    if isinstance(raw_items, list):
        for entry in raw_items[:60]:
            if isinstance(entry, str):
                name = str(entry or "").strip()[:80]
                if not name:
                    continue
                items.append({"name": name, "qty": 1})
                continue
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("item_id") or "").strip()[:80]
            if not name:
                continue
            item = {
                "name": name,
                "qty": max(1, _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)),
            }
            for key, limit in (
                ("item_type", 32),
                ("category", 40),
                ("notes", 240),
                ("price", 32),
                ("rarity", 32),
                ("equipment_kind", 24),
                ("armor_type", 24),
                ("damage_dice", 20),
                ("damage_type", 24),
            ):
                val = str(entry.get(key) or "").strip()[:limit]
                if val:
                    item[key] = val
            items.append(item)
    if items:
        bundle["items"] = items

    handout_ids = _normalize_optional_ref_list(raw.get("handout_unlock_ids") or raw.get("handout_ids"), limit=20)
    if handout_ids:
        bundle["handout_unlock_ids"] = handout_ids

    flags = raw.get("flags")
    if isinstance(flags, dict):
        out_flags = {}
        for key, limit in (
            ("shop_discount", 80),
            ("location_unlock", 80),
            ("service_unlock", 80),
            ("faction_reputation", 80),
        ):
            val = str(flags.get(key) or "").strip()[:limit]
            if val:
                out_flags[key] = val
        if out_flags:
            bundle["flags"] = out_flags
    return bundle


def _normalize_quest_visibility(raw: dict, session: Session) -> dict:
    vis = dict(raw or {})
    mode = str(vis.get("mode") or "party_public").strip().lower()
    if mode not in {"party_public", "private_player", "dm_only", "hidden", "viewer_public"}:
        mode = "party_public"
    roles_raw = vis.get("roles")
    if isinstance(roles_raw, list):
        roles = []
        for role in roles_raw:
            role_name = str(role or "").strip().lower()
            if role_name in {"player", "viewer", "dm"} and role_name not in roles:
                roles.append(role_name)
    else:
        roles = ["player", "viewer"] if mode == "party_public" else (["viewer"] if mode == "viewer_public" else ["player"])
    player_ids = _normalize_quest_id_list(vis.get("player_ids"), limit=20)
    if player_ids:
        connected = {str(uid) for uid, u in (session.users or {}).items() if str(getattr(u, "role", "")).lower() == "player"}
        player_ids = [uid for uid in player_ids if uid in connected] or player_ids
    hidden_objective_ids = _normalize_optional_ref_list(vis.get("hidden_objective_ids"), limit=40)
    subgroup_ids = _normalize_optional_ref_list(vis.get("subgroup_ids"), limit=16)
    subgroup_ids = [str(sid or "").strip().lower()[:48] for sid in subgroup_ids if str(sid or "").strip()]
    return {
        "mode": mode,
        "roles": roles,
        "player_ids": player_ids,
        "subgroup_ids": subgroup_ids,
        "hidden_objective_ids": hidden_objective_ids,
    }


def _normalize_session_quest_payload(payload: dict, session: Session, user: User) -> dict | None:
    now = time.time()
    quest_id = str(payload.get("id") or "").strip()[:64]
    title = str(payload.get("title") or "").strip()[:120] or "Untitled quest"
    summary = str(payload.get("summary") or "").strip()[:300]
    description = str(payload.get("description") or "").strip()[:4000]
    category = str(payload.get("category") or "general").strip()[:60] or "general"
    difficulty = str(payload.get("difficulty_tier") or payload.get("difficulty") or "Tier 1").strip()[:40] or "Tier 1"
    status = normalize_quest_status(
        str(payload.get("status") or payload.get("availability_state") or "draft"),
        fallback="draft",
    )

    meta = dict(payload.get("meta") or {})
    giver_name = str(payload.get("quest_giver") or meta.get("quest_giver") or "").strip()[:80]
    if giver_name:
        meta["quest_giver"] = giver_name
    if "board_label" in payload:
        meta["board_label"] = str(payload.get("board_label") or "").strip()[:80]
    prerequisite_text = str(payload.get("prerequisites") or meta.get("prerequisites") or "").strip()[:280]
    if prerequisite_text:
        meta["prerequisites"] = prerequisite_text
    meta["last_edited_by"] = str(user.id or "dm")[:64]

    visibility = _normalize_quest_visibility(payload.get("visibility") if isinstance(payload.get("visibility"), dict) else {}, session)
    board_visible = bool(payload.get("board_visible"))
    if board_visible and visibility.get("mode") in {"dm_only", "hidden"}:
        visibility["mode"] = "party_public"
        if "player" not in visibility["roles"]:
            visibility["roles"] = ["player", "viewer"]

    linked_handouts = _normalize_optional_ref_list(payload.get("linked_handout_ids"), limit=16)
    linked_pois = _normalize_optional_ref_list(payload.get("linked_poi_ids"), limit=16)
    linked_maps = _normalize_optional_ref_list(payload.get("linked_map_ids"), limit=16)
    linked_npcs = _normalize_optional_ref_list(payload.get("linked_npc_ids"), limit=16)
    linked_encounters = _normalize_optional_ref_list(payload.get("linked_encounter_template_ids"), limit=16)

    objective_list = normalize_objective_list(payload.get("objective_list"))
    if not objective_list and isinstance(payload.get("objective_structure"), list):
        objective_list = normalize_objective_list(
            [{"id": f"obj-{idx + 1}", "title": str(title or ""), "type": "manual"} for idx, title in enumerate(payload.get("objective_structure") or [])]
        )

    out = {
        "id": quest_id or f"sq-{secrets.token_hex(6)}",
        "title": title,
        "summary": summary,
        "description": description,
        "category": category,
        "difficulty_tier": difficulty,
        "status": status,
        "visibility": visibility,
        "linked_handout_ids": linked_handouts,
        "linked_poi_ids": linked_pois,
        "linked_map_ids": linked_maps,
        "linked_npc_ids": linked_npcs,
        "linked_encounter_template_ids": linked_encounters,
        "updated_at": now,
        "meta": meta,
    }
    out["objective_list"] = objective_list
    out["progress"] = rebuild_progress(out)
    if isinstance(payload.get("reward_bundle"), dict):
        out["reward_bundle"] = _normalize_reward_bundle(payload.get("reward_bundle") or {})
    if isinstance(payload.get("stage_list"), list):
        out["stage_list"] = list(payload.get("stage_list") or [])
    if payload.get("current_stage_id"):
        out["current_stage_id"] = str(payload.get("current_stage_id") or "").strip()[:80]
    out = normalize_quest_payload_shape(out)
    if payload.get("template_id"):
        out["template_id"] = str(payload.get("template_id") or "").strip()[:80]
    out["faction_tags"] = _normalize_optional_ref_list(payload.get("faction_tags"), limit=24)
    out["guild_tags"] = _normalize_optional_ref_list(payload.get("guild_tags"), limit=24)
    out["required_faction_tags"] = _normalize_optional_ref_list(payload.get("required_faction_tags"), limit=24)
    out["required_guild_rank_id"] = str(payload.get("required_guild_rank_id") or "").strip().lower()[:40]
    try:
        out["required_guild_rank_points"] = max(0, min(10_000, int(payload.get("required_guild_rank_points") or 0)))
    except Exception:
        out["required_guild_rank_points"] = 0
    out["prerequisite_quest_ids"] = _normalize_quest_id_list(payload.get("prerequisite_quest_ids"), limit=24)
    out["unlocks_quest_ids"] = _normalize_quest_id_list(payload.get("unlocks_quest_ids"), limit=24)
    out["hidden_until_unlocked"] = bool(payload.get("hidden_until_unlocked"))
    out["lock_visibility"] = str(payload.get("lock_visibility") or "").strip().lower()[:20]
    return normalize_quest_progression_fields(out)


async def handle_session_quest_upsert(payload: dict, session: Session, user: User):
    """Create/update a session quest with DM-controlled publish visibility and board bindings."""
    if user.role != "dm" and not assistant_dm_has_scope(session, user, "quests.manage"):
        return
    incoming = _normalize_session_quest_payload(payload or {}, session, user)
    if not incoming:
        return

    quests = list(getattr(session, "session_quests", []) or [])
    existing = next((entry for entry in quests if str(entry.get("id") or "") == incoming["id"]), None)
    if existing:
        created_at = float(existing.get("created_at") or incoming["updated_at"])
        existing.update(incoming)
        existing["created_at"] = created_at
        saved_quest = dict(existing)
    else:
        incoming["created_at"] = incoming["updated_at"]
        quests.append(incoming)
        saved_quest = dict(incoming)
    session.session_quests = quests
    session.session_quests = resolve_session_quest_progression(session)

    requested_board_ids = _normalize_optional_ref_list(payload.get("board_ids"), limit=24)
    board_bindings = list(getattr(session, "quest_board_bindings", []) or [])
    quest_id = saved_quest["id"]
    if requested_board_ids:
        normalized_bindings: list[dict] = []
        board_lookup: dict[str, dict] = {}
        for raw in board_bindings:
            board_id = str((raw or {}).get("board_id") or "").strip()[:80]
            if not board_id:
                continue
            quest_ids = _normalize_quest_id_list((raw or {}).get("quest_ids"), limit=200)
            entry = {"board_id": board_id, "quest_ids": quest_ids}
            normalized_bindings.append(entry)
            board_lookup[board_id] = entry
        for board_id in requested_board_ids:
            row = board_lookup.get(board_id)
            if not row:
                row = {"board_id": board_id, "quest_ids": []}
                normalized_bindings.append(row)
                board_lookup[board_id] = row
            if quest_id not in row["quest_ids"]:
                row["quest_ids"].append(quest_id)
        for row in normalized_bindings:
            row["quest_ids"] = [qid for qid in row.get("quest_ids", []) if qid != quest_id or row["board_id"] in requested_board_ids]
        session.quest_board_bindings = [row for row in normalized_bindings if row.get("quest_ids")]
    elif "board_ids" in payload:
        normalized_bindings = []
        for raw in board_bindings:
            board_id = str((raw or {}).get("board_id") or "").strip()[:80]
            if not board_id:
                continue
            quest_ids = [qid for qid in _normalize_quest_id_list((raw or {}).get("quest_ids"), limit=200) if qid != quest_id]
            if quest_ids:
                normalized_bindings.append({"board_id": board_id, "quest_ids": quest_ids})
        session.quest_board_bindings = normalized_bindings

    await save_campaign_async(session)
    await _broadcast_session_quests(session)
    await manager.send_to(session.id, user.id, {
        "type": "session_quest_upsert_result",
        "payload": {"ok": True, "quest": saved_quest},
    })


async def _broadcast_split_party_state(session: Session):
    split_payload = session.split_party_state()
    for uid, u in session.users.items():
        role = str(getattr(u, "role", "") or "").strip().lower()
        payload = {"split_party": split_payload}
        if role != "dm":
            payload["user_subgroup_id"] = session.get_user_subgroup_id(uid)
            payload["subgroup_map_context"] = session.get_subgroup_map_context(payload["user_subgroup_id"])
        await manager.send_to(session.id, uid, {"type": "split_party_sync", "payload": payload})


async def handle_split_party_assign(payload: dict, session: Session, user: User):
    if user.role != "dm" and not assistant_dm_has_scope(session, user, "quests.manage"):
        return
    subgroup_id = str(payload.get("subgroup_id") or "main").strip().lower()[:48] or "main"
    user_ids = payload.get("user_ids")
    if not isinstance(user_ids, list):
        return
    changed_users = []
    for raw_uid in user_ids:
        uid = str(raw_uid or "").strip()[:64]
        target = session.users.get(uid)
        if not target:
            continue
        if str(getattr(target, "role", "") or "").strip().lower() not in {"player", "assistant_dm"}:
            continue
        previous = session.get_user_subgroup_id(uid)
        next_group = session.set_user_subgroup_id(uid, subgroup_id, actor_id=user.id)
        if previous != next_group:
            changed_users.append({"user_id": uid, "from_subgroup": previous, "to_subgroup": next_group})
    if not changed_users:
        return
    session.add_log(f"{user.name} reassigned {len(changed_users)} participant(s) to subgroup '{subgroup_id}'.", "system")
    await save_campaign_async(session)
    await _broadcast_split_party_state(session)
    for row in changed_users:
        uid = row.get("user_id")
        target = session.users.get(uid)
        if not target:
            continue
        state = session.to_state_dict_for_role(target.role, uid)
        await manager.send_to(session.id, uid, {"type": "state_sync", "payload": state})


async def handle_split_party_set_context(payload: dict, session: Session, user: User):
    if user.role != "dm" and not assistant_dm_has_scope(session, user, "maps.fog"):
        return
    subgroup_id = str(payload.get("subgroup_id") or "main").strip().lower()[:48] or "main"
    map_context = str(payload.get("map_context") or "").strip()[:80] or "world"
    updated = session.set_subgroup_map_context(subgroup_id, map_context, actor_id=user.id)
    session.add_log(f"{user.name} set subgroup '{subgroup_id}' context to '{updated.get('map_context', 'world')}'.", "system")
    await save_campaign_async(session)
    await _broadcast_split_party_state(session)
    for uid, participant in session.users.items():
        if session.get_user_subgroup_id(uid) != subgroup_id:
            continue
        role = str(getattr(participant, "role", "") or "").strip().lower()
        if role == "dm":
            continue
        state = session.to_state_dict_for_role(participant.role, uid)
        await manager.send_to(session.id, uid, {"type": "state_sync", "payload": state})


def _build_session_quest_from_prep_pack_entry(entry: dict, *, pack: dict, imported_by: str) -> dict:
    now = import_timestamp()
    objective_titles = list(entry.get("objective_list") or [])
    objective_list = [
        {
            "id": f"obj-{idx + 1}",
            "title": str(text or "Objective").strip()[:160] or "Objective",
            "status": "active" if idx == 0 else "pending",
        }
        for idx, text in enumerate(objective_titles[:16])
    ]
    reward_bundle = dict(entry.get("reward_bundle") or {})
    reward_bundle["gold"] = max(0, int(reward_bundle.get("gold", 0) or 0))
    reward_bundle["xp"] = max(0, int(reward_bundle.get("xp", 0) or 0))
    reward_bundle["items"] = [str(v or "").strip()[:80] for v in (reward_bundle.get("items") or []) if str(v or "").strip()][:16]

    return normalize_quest_payload_shape({
        "id": f"sq-{secrets.token_hex(6)}",
        "template_id": "",
        "title": str(entry.get("title") or "Untitled quest")[:120],
        "summary": str(entry.get("summary") or "")[:300],
        "description": str(entry.get("description") or "")[:4000],
        "status": str(entry.get("status") or "available")[:24].lower() or "available",
        "category": str(entry.get("category") or "general")[:60],
        "difficulty_tier": str(entry.get("difficulty_tier") or "Tier 1")[:40],
        "objective_list": objective_list,
        "reward_bundle": reward_bundle,
        "visibility": {"mode": "party_public", "roles": ["player", "viewer"], "player_ids": [], "hidden_objective_ids": []},
        "linked_handout_ids": list(entry.get("linked_handout_ids") or []),
        "linked_poi_ids": list(entry.get("linked_poi_ids") or []),
        "linked_map_ids": list(entry.get("linked_map_ids") or []),
        "linked_npc_ids": list(entry.get("linked_npc_ids") or []),
        "linked_encounter_template_ids": list(entry.get("linked_encounter_template_ids") or []),
        "source_type": "prep_pack_import",
        "source_marker": str(pack.get("pack_id") or "prep_pack")[:64],
        "created_at": now,
        "updated_at": now,
        "meta": {
            "imported_by": str(imported_by or "dm")[:64],
            "prep_pack_id": str(pack.get("pack_id") or "")[:64],
            "prep_pack_name": str(pack.get("name") or "")[:120],
            "import_instance_id": build_import_instance_id(str(pack.get("pack_id") or "prep-pack")),
        },
    })


def _build_handout_from_prep_pack_entry(entry: dict, *, pack: dict, imported_by: str) -> dict:
    now = import_timestamp()
    return {
        "id": f"ho-{secrets.token_hex(6)}",
        "title": str(entry.get("title") or "Untitled Handout")[:120],
        "public_text": str(entry.get("public_text") or entry.get("content") or "")[:12000],
        "dm_secret_text": str(entry.get("dm_secret_text") or "")[:12000],
        "recipients": "all",
        "created_at": now,
        "updated_at": now,
        "source_type": "prep_pack_import",
        "source_marker": str(pack.get("pack_id") or "prep_pack")[:64],
        "meta": {
            "imported_by": str(imported_by or "dm")[:64],
            "prep_pack_id": str(pack.get("pack_id") or "")[:64],
            "prep_pack_name": str(pack.get("name") or "")[:120],
        },
    }


async def handle_prep_pack_library_list(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    packs = [prep_pack_catalog_view(pack) for pack in load_builtin_prep_packs()]
    await manager.send_to(session.id, user.id, {
        "type": "prep_pack_library_sync",
        "payload": {"packs": packs},
    })


async def handle_prep_pack_import(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    pack_id = str(payload.get("pack_id") or "").strip()
    if not pack_id:
        return
    pack = get_prep_pack(pack_id)
    if not pack:
        await manager.send_to(session.id, user.id, {
            "type": "prep_pack_import_result",
            "payload": {"ok": False, "error": "Prep pack not found.", "pack_id": pack_id},
        })
        return

    imported = {"quests": 0, "handouts": 0, "encounters": 0, "pois": 0}

    session.session_quests = list(getattr(session, "session_quests", []) or [])
    for row in list(pack.get("quests") or []):
        quest = _build_session_quest_from_prep_pack_entry(row, pack=pack, imported_by=user.id)
        session.session_quests.append(quest)
        imported["quests"] += 1
    session.session_quests = resolve_session_quest_progression(session)

    session.handouts = list(getattr(session, "handouts", []) or [])
    for row in list(pack.get("handouts") or []):
        handout = _build_handout_from_prep_pack_entry(row, pack=pack, imported_by=user.id)
        session.handouts.append(handout)
        imported["handouts"] += 1

    session.encounter_templates = list(getattr(session, "encounter_templates", []) or [])
    for row in list(pack.get("encounters") or []):
        normalized = _normalize_encounter_template(row or {})
        if not normalized:
            continue
        normalized["id"] = f"enc-{secrets.token_hex(6)}"
        normalized["source_type"] = "prep_pack_import"
        normalized["source_marker"] = str(pack.get("pack_id") or "prep_pack")[:64]
        session.encounter_templates.append(normalized)
        imported["encounters"] += 1
    session.encounter_templates.sort(key=lambda item: (str(item.get("name") or "").lower(), float(item.get("created_at") or 0.0)))

    from server.session import POI
    for row in list(pack.get("pois") or []):
        poi = POI(
            id=f"poi-{secrets.token_hex(6)}",
            x=float(row.get("x", 0) or 0),
            y=float(row.get("y", 0) or 0),
            name=str(row.get("name") or "Location")[:80],
            description=str(row.get("description") or "")[:2000],
            dm_notes=str(row.get("dm_notes") or "")[:2000],
            poi_type=str(row.get("poi_type") or "city")[:40],
            local_map_url=(str(row.get("local_map_url") or "").strip()[:400] or None),
            map_context=str(row.get("map_context") or "world")[:80] or "world",
            revealed_to_players=bool(row.get("revealed_to_players", True)),
            interactable=None,
        )
        session.pois[poi.id] = poi
        imported["pois"] += 1
        await manager.broadcast(session.id, {
            "type": "poi_created",
            "payload": {"poi": poi.to_dict(include_dm_notes=False), "poi_dm": poi.to_dict(include_dm_notes=True)},
        })

    await save_campaign_async(session)
    await _broadcast_session_quests(session)
    await _broadcast_handout_state(session)
    await _broadcast_encounter_template_state(session)
    await manager.send_to(session.id, user.id, {
        "type": "prep_pack_import_result",
        "payload": {
            "ok": True,
            "pack_id": str(pack.get("pack_id") or ""),
            "pack_name": str(pack.get("name") or ""),
            "imported": imported,
        },
    })



async def handle_quest_template_library_list(payload: dict, session: Session, user: User):
    """Send premade quest template library to DM requester."""
    if user.role != "dm":
        return
    templates = load_builtin_quest_templates()
    await manager.send_to(session.id, user.id, {
        "type": "quest_template_library_sync",
        "payload": {"templates": templates},
    })


async def handle_quest_template_import(payload: dict, session: Session, user: User):
    """Import a premade quest template as an editable session quest."""
    if user.role != "dm":
        return
    template_id = str(payload.get("template_id") or "").strip()
    if not template_id:
        return
    template = get_quest_template(template_id)
    if not template:
        await manager.send_to(session.id, user.id, {
            "type": "quest_template_import_result",
            "payload": {"ok": False, "error": "Template not found.", "template_id": template_id},
        })
        return

    imported = normalize_quest_payload_shape(build_session_quest_from_template(template, imported_by=user.id))
    title_override = str(payload.get("title") or "").strip()[:120]
    if title_override:
        imported["title"] = title_override
    summary_override = str(payload.get("summary") or "").strip()[:300]
    if summary_override:
        imported["summary"] = summary_override

    session.session_quests = list(getattr(session, "session_quests", []) or [])
    session.session_quests.append(imported)
    session.session_quests = resolve_session_quest_progression(session)

    session.quest_templates = list(getattr(session, "quest_templates", []) or [])
    if not any(str(entry.get("template_id") or entry.get("id") or "") == template_id for entry in session.quest_templates if isinstance(entry, dict)):
        session.quest_templates.append(dict(template))

    await save_campaign_async(session)
    await _broadcast_session_quests(session)
    await manager.send_to(session.id, user.id, {
        "type": "quest_template_import_result",
        "payload": {"ok": True, "quest": imported, "template_id": template_id},
    })


async def handle_session_quest_objective_event(payload: dict, session: Session, user: User):
    """Apply role-safe objective progress from live gameplay events."""
    event_payload = dict(payload or {})
    event_type = str(event_payload.get("event_type") or "").strip().lower()[:40]
    if not event_type:
        return
    if event_type == "clear_encounter" and user.role != "dm":
        return
    if user.role not in {"dm", "player", "viewer"}:
        return

    quests = list(getattr(session, "session_quests", []) or [])
    quest_id = str(event_payload.get("quest_id") or "").strip()[:64]
    changed = False
    for idx, quest in enumerate(quests):
        entry = normalize_quest_payload_shape(dict(quest or {}))
        if quest_id and str(entry.get("id") or "") != quest_id:
            continue
        if str(entry.get("status") or "") in {"completed", "failed", "expired", "hidden", "draft"}:
            continue
        if apply_objective_event(entry, event_payload):
            quests[idx] = entry
            changed = True
    if not changed:
        return

    session.session_quests = quests
    session.session_quests = resolve_session_quest_progression(session)
    await save_campaign_async(session)
    await _broadcast_session_quests(session)
    await manager.send_to(session.id, user.id, {
        "type": "session_quest_objective_result",
        "payload": {"ok": True, "event_type": event_type},
    })


def _player_has_other_active_quest(session: Session, user: User, target_quest_id: str) -> bool:
    visible = session._visible_session_quests_for_role("player", user.id)
    active_statuses = {"accepted", "active", "ready_to_turn_in", "rewards_pending"}
    for row in visible:
        quest_id = str((row or {}).get("id") or "")
        if not quest_id or quest_id == target_quest_id:
            continue
        status = str((row or {}).get("status") or "").strip().lower()
        if status in active_statuses:
            return True
    return False


def _connected_party_players(session: Session) -> list[User]:
    """Return only player-role users that are currently connected."""
    players: list[User] = []
    for participant in (session.users or {}).values():
        if str(getattr(participant, "role", "")).strip().lower() != "player":
            continue
        if not bool(getattr(participant, "connected", True)):
            continue
        players.append(participant)
    return players


async def handle_session_quest_accept(payload: dict, session: Session, user: User):
    """Player acceptance flow: enforce one active quest and auto-vote for parties."""
    if user.role != "player":
        return
    quest_id = str(payload.get("quest_id") or payload.get("id") or "").strip()[:64]
    if not quest_id:
        return

    quests = list(getattr(session, "session_quests", []) or [])
    index = next((idx for idx, row in enumerate(quests) if str((row or {}).get("id") or "") == quest_id), None)
    if index is None:
        return
    quest = normalize_quest_payload_shape(dict(quests[index] or {}))

    visible_for_player = session._visible_session_quests_for_role("player", user.id)
    if not any(str((row or {}).get("id") or "") == quest_id for row in visible_for_player):
        return

    status = str(quest.get("status") or "").strip().lower()
    if status not in {"available", "accepted", "active"}:
        await manager.send_to(session.id, user.id, {
            "type": "session_quest_accept_result",
            "payload": {"ok": False, "quest_id": quest_id, "error": "Quest is not currently available."},
        })
        return

    if _player_has_other_active_quest(session, user, quest_id):
        await manager.send_to(session.id, user.id, {
            "type": "session_quest_accept_result",
            "payload": {"ok": False, "quest_id": quest_id, "error": "You can only have one active quest at a time."},
        })
        return

    players = _connected_party_players(session)

    if len(players) <= 1:
        quest["status"] = "accepted"
        quest["accepted_at"] = time.time()
        quest["accepted_by_user_id"] = user.id
        quest["accepted_by_name"] = user.name
        quest["updated_at"] = time.time()
        quests[index] = quest
        session.session_quests = quests
        session.session_quests = resolve_session_quest_progression(session)
        accepted_event = emit_world_event(session, "quest_accepted", {
            "source": "session_quest_accept",
            "actor_user_id": user.id,
            "quest_id": quest_id,
            "summary": f"{user.name} accepted quest {str(quest.get('title') or quest_id)[:120]}",
        })
        consume_world_event(session, accepted_event, {"refresh_quest_ids": [quest_id]})
        await save_campaign_async(session)
        await _broadcast_session_quests(session)
        await _broadcast_quest_board_notice(
            session,
            actor=user,
            quest=quest,
            scope="quest_accept",
            message=f"{user.name} accepted {str(quest.get('title') or 'a quest')[:120]}.",
            details=[
                "Quest log updated.",
                "Guild board state refreshed.",
                *([f"Linked handouts: {len(list(quest.get('linked_handout_ids') or []))}"] if list(quest.get("linked_handout_ids") or []) else []),
            ],
        )
        await manager.send_to(session.id, user.id, {
            "type": "session_quest_accept_result",
            "payload": {
                "ok": True,
                "quest_id": quest_id,
                "status": "accepted",
                "quest": quest,
                "vote_required": False,
                "premium_progression": build_premium_progression_snapshot(session, role=user.role, user_id=user.id),
            },
        })
        return

    poll = session.active_poll if isinstance(session.active_poll, dict) else None
    poll_kind = str((poll or {}).get("kind") or "")
    poll_quest_id = str((poll or {}).get("quest_id") or "")
    if not poll or poll.get("closed") or poll_kind != "quest_accept" or poll_quest_id != quest_id:
        now = time.time()
        session.active_poll = {
            "id": secrets.token_hex(6),
            "kind": "quest_accept",
            "quest_id": quest_id,
            "title": "Quest Acceptance Vote",
            "question": f"Accept quest: {str(quest.get('title') or 'Quest')[:120]}?",
            "options": ["Accept", "Decline"],
            "votes": {},
            "created_at": now,
            "closes_at": now + 90,
            "closed": False,
            "results_mode": "live",
            "authority_note": "Party majority decides acceptance.",
            "closed_by": "dm",
            "closed_reason": "active",
        }
        poll = session.active_poll
        await _broadcast_poll_state(session, "poll_created")

    poll.setdefault("votes", {})[user.id] = 0
    await _broadcast_poll_state(session, "poll_updated")

    total_players = max(1, len(players))
    votes = poll.get("votes") or {}
    yes_votes = sum(1 for idx in votes.values() if idx == 0)
    no_votes = sum(1 for idx in votes.values() if idx == 1)
    pending_votes = max(0, total_players - len(votes))
    majority = (total_players // 2) + 1

    if yes_votes >= majority:
        quest["status"] = "accepted"
        quest["accepted_at"] = time.time()
        quest["accepted_by_user_id"] = user.id
        quest["accepted_by_name"] = user.name
        quest["updated_at"] = time.time()
        quests[index] = quest
        session.session_quests = quests
        session.session_quests = resolve_session_quest_progression(session)
        accepted_event = emit_world_event(session, "quest_accepted", {
            "source": "session_quest_accept_vote",
            "actor_user_id": user.id,
            "quest_id": quest_id,
            "summary": f"Party accepted quest {str(quest.get('title') or quest_id)[:120]}",
        })
        consume_world_event(session, accepted_event, {"refresh_quest_ids": [quest_id]})
        poll["closed"] = True
        poll["closed_by"] = "vote"
        poll["closed_reason"] = "quest_accepted"
        await save_campaign_async(session)
        await _broadcast_session_quests(session)
        await _broadcast_quest_board_notice(
            session,
            actor=user,
            quest=quest,
            scope="quest_accept",
            message=f"Party accepted {str(quest.get('title') or 'a quest')[:120]}.",
            details=[
                "Quest log updated.",
                "Guild board state refreshed.",
                *([f"Linked handouts: {len(list(quest.get('linked_handout_ids') or []))}"] if list(quest.get("linked_handout_ids") or []) else []),
            ],
        )
        await _broadcast_poll_state(session, "poll_closed")
        await manager.broadcast(session.id, {
            "type": "session_quest_accept_result",
            "payload": {
                "ok": True,
                "quest_id": quest_id,
                "status": "accepted",
                "quest": quest,
                "vote_required": True,
                "premium_progression": build_premium_progression_snapshot(session, role=user.role, user_id=user.id),
            },
        })
        return

    if no_votes >= majority or (yes_votes + pending_votes) < majority:
        poll["closed"] = True
        poll["closed_by"] = "vote"
        poll["closed_reason"] = "quest_declined"
        await save_campaign_async(session)
        await _broadcast_poll_state(session, "poll_closed")
        await manager.broadcast(session.id, {
            "type": "session_quest_accept_result",
            "payload": {"ok": False, "quest_id": quest_id, "status": "declined", "error": "Party vote declined this quest.", "vote_required": True},
        })
        return

    await save_campaign_async(session)
    await manager.send_to(session.id, user.id, {
        "type": "session_quest_accept_result",
        "payload": {
            "ok": True,
            "quest_id": quest_id,
            "status": "vote_pending",
            "vote_required": True,
            "message": "Vote recorded. Waiting on party majority.",
        },
    })


def _quest_reward_targets(session: Session, quest: dict, reward_bundle: dict, requested_user_id: str = "") -> list[User]:
    players = [
        participant for participant in (session.users or {}).values()
        if str(getattr(participant, "role", "")).strip().lower() == "player"
    ]
    if not players:
        return []
    distribution = str(reward_bundle.get("distribution") or "party").strip().lower()
    if distribution != "personal":
        return players
    target_user_id = str(
        requested_user_id
        or reward_bundle.get("target_user_id")
        or ((quest.get("visibility") or {}).get("player_ids") or [None])[0]
        or ""
    ).strip()
    if target_user_id:
        target = next((player for player in players if str(player.id) == target_user_id), None)
        return [target] if target else []
    return [players[0]]


async def _apply_quest_rewards(session: Session, quest: dict, reward_bundle: dict, grant_user: User, claim_user_id: str = "") -> list[str]:
    from server.handlers.inventory import (
        _add_gold_to_player,
        _add_item_to_party_stash,
        _add_item_to_player_inventory,
        _broadcast_inventory_state,
        _format_gold_units,
    )

    targets = _quest_reward_targets(session, quest, reward_bundle, claim_user_id)
    if not targets:
        return []

    summary: list[str] = []
    gold_units_total = max(0, _safe_int(reward_bundle.get("gold"), 0, minimum=0, maximum=1_000_000_000)) * 100
    if gold_units_total > 0:
        distribution = str(reward_bundle.get("distribution") or "party").strip().lower()
        if distribution == "personal":
            target = targets[0]
            updated_total = _add_gold_to_player(session, target, gold_units_total)
            summary.append(f"Gold: {_format_gold_units(gold_units_total)} to {target.name} ({_format_gold_units(updated_total)} total)")
        else:
            share = gold_units_total // len(targets)
            remainder = gold_units_total % len(targets)
            for idx, target in enumerate(targets):
                grant = share + (1 if idx < remainder else 0)
                if grant <= 0:
                    continue
                _add_gold_to_player(session, target, grant)
            summary.append(f"Gold: {_format_gold_units(gold_units_total)} split across {len(targets)} player(s)")

    for item in list(reward_bundle.get("items") or [])[:60]:
        if not isinstance(item, dict):
            continue
        qty = max(1, _safe_int(item.get("qty"), 1, minimum=1, maximum=9999))
        if str(reward_bundle.get("distribution") or "party").strip().lower() == "personal":
            target = targets[0]
            _add_item_to_player_inventory(session, target, item, qty, source_name=f"Quest: {quest.get('title') or 'Quest Reward'}")
            summary.append(f"Item: {qty}x {item.get('name') or 'Item'} to {target.name}")
        else:
            _add_item_to_party_stash(session, item, qty, source_name=f"Quest: {quest.get('title') or 'Quest Reward'}")
            summary.append(f"Item: {qty}x {item.get('name') or 'Item'} to Party Stash")

    handout_unlocks = _normalize_optional_ref_list(reward_bundle.get("handout_unlock_ids"), limit=20)
    if handout_unlocks:
        for handout_id in handout_unlocks:
            handout = next((row for row in (getattr(session, "handouts", []) or []) if str((row or {}).get("id") or "") == handout_id), None)
            if not handout:
                continue
            recipients = handout.get("recipients", "all")
            for target in targets:
                if recipients != "all" and (not isinstance(recipients, list) or target.id not in recipients):
                    continue
                await manager.send_to(session.id, target.id, {
                    "type": "handout_received",
                    "payload": {
                        "handout_id": handout_id,
                        "title": str(handout.get("title") or "Handout")[:120],
                        "public_text": str(handout.get("public_text") or handout.get("content") or "")[:12000],
                    },
                })
            handout_event = emit_world_event(session, "handout_unlocked", {
                "source": "quest_reward_bundle",
                "actor_user_id": grant_user.id,
                "quest_id": str(quest.get("id") or ""),
                "handout_id": handout_id,
                "summary": f"Handout unlocked from quest reward: {handout_id}",
            })
            consume_world_event(session, handout_event, {"unlock_handout_ids": [handout_id]})
        summary.append(f"Handouts unlocked: {len(handout_unlocks)}")

    flags = dict(reward_bundle.get("flags") or {})
    if flags:
        summary.append("Flags: " + ", ".join(sorted(flags.keys())))
    rep_applied = apply_reputation_changes_to_session(
        session,
        reward_bundle,
        source=f"quest:{str(quest.get('id') or '')}",
    )
    if rep_applied:
        reputation_event = emit_world_event(session, "faction_reputation_changed", {
            "source": "quest_reward_bundle",
            "actor_user_id": grant_user.id,
            "quest_id": str(quest.get("id") or ""),
            "summary": f"Faction reputation changed from quest rewards: {len(rep_applied)} faction(s)",
            "meta": {"changes": rep_applied[:20]},
        })
        consume_world_event(session, reputation_event, {
            "summary_messages": [reputation_event.get("summary") or "Faction reputation changed."],
        })
    for row in rep_applied[:6]:
        summary.append(
            f"Faction: {row['name']} {'+' if int(row.get('delta') or 0) > 0 else ''}{int(row.get('delta') or 0)} "
            f"(now {int(row.get('reputation') or 0)}, {row.get('tier_label') or row.get('tier')})"
        )

    if summary:
        await _broadcast_inventory_state(session)
        await manager.broadcast(session.id, {
            "type": "session_event_notice",
            "payload": {
                "scope": "quest_reward",
                "quest_id": str(quest.get("id") or ""),
                "quest_title": str(quest.get("title") or "Quest")[:120],
                "message": f"{grant_user.name} granted quest rewards.",
                "details": summary[:12],
            },
        })
    return summary


async def handle_session_quest_turn_in(payload: dict, session: Session, user: User):
    """Resolve quest turn-in and optionally grant rewards without replacing existing inventory/economy flow."""
    if user.role not in {"dm", "player"}:
        return
    quest_id = str(payload.get("quest_id") or payload.get("id") or "").strip()[:64]
    if not quest_id:
        return
    quests = list(getattr(session, "session_quests", []) or [])
    index = next((idx for idx, row in enumerate(quests) if str((row or {}).get("id") or "") == quest_id), None)
    if index is None:
        return
    quest = normalize_quest_payload_shape(dict(quests[index] or {}))
    if user.role != "dm":
        visible_for_player = session._visible_session_quests_for_role("player", user.id)
        if not any(str((row or {}).get("id") or "") == quest_id for row in visible_for_player):
            return

    status = str(quest.get("status") or "")
    normalized_status = status.strip().lower()
    if user.role == "player":
        if normalized_status not in {"accepted", "active", "ready_to_turn_in", "completed", "rewards_pending", "rewards_granted"}:
            await manager.send_to(session.id, user.id, {
                "type": "session_quest_turn_in_result",
                "payload": {"ok": False, "quest_id": quest_id, "error": "Quest cannot be submitted right now."},
            })
            return
        outcome = str(payload.get("outcome") or "completed").strip().lower()
        if outcome not in {"completed", "failed", "not_completed"}:
            outcome = "completed"
        quest_reward_state = dict(quest.get("reward_state") or {})
        quest_reward_state.update({
            "status": "awaiting_dm_approval",
            "requested_outcome": outcome,
            "requested_at": time.time(),
            "requested_by_user_id": user.id,
            "requested_by_name": user.name,
        })
        quest["reward_state"] = quest_reward_state
        quest["turn_in_request"] = {
            "status": "pending",
            "outcome": outcome,
            "requested_at": time.time(),
            "requested_by_user_id": user.id,
            "requested_by_name": user.name,
            "note": str(payload.get("note") or "")[:400],
        }
        quest["updated_at"] = time.time()
        quests[index] = quest
        session.session_quests = quests
        session.session_quests = resolve_session_quest_progression(session)
        await save_campaign_async(session)
        await _broadcast_session_quests(session)
        await manager.send_to(session.id, user.id, {
            "type": "session_quest_turn_in_result",
            "payload": {
                "ok": True,
                "quest_id": quest_id,
                "quest": quest,
                "status": "pending_dm_approval",
                "message": "Update sent to DM for approval.",
            },
        })
        return

    if normalized_status not in {"ready_to_turn_in", "completed", "rewards_pending", "rewards_granted"}:
        await manager.send_to(session.id, user.id, {
            "type": "session_quest_turn_in_result",
            "payload": {"ok": False, "quest_id": quest_id, "error": "Quest is not ready for turn-in."},
        })
        return

    reward_bundle = _normalize_reward_bundle(dict(quest.get("reward_bundle") or {}))
    has_rewards = any((
        reward_bundle.get("gold"),
        reward_bundle.get("xp"),
        list(reward_bundle.get("items") or []),
        reward_bundle.get("reputation"),
        list(reward_bundle.get("handout_unlock_ids") or []),
        dict(reward_bundle.get("flags") or {}),
    ))
    apply_rewards = bool(payload.get("apply_rewards")) or (user.role == "dm" and payload.get("apply_rewards") is not False)
    quest_reward_state = dict(quest.get("reward_state") or {})
    quest_reward_state.update({
        "turned_in_at": time.time(),
        "turned_in_by_user_id": user.id,
        "turned_in_by_name": user.name,
        "status": "pending",
    })

    summary: list[str] = []
    if apply_rewards and has_rewards:
        claim_user_id = str(payload.get("target_user_id") or "").strip()[:64]
        summary = await _apply_quest_rewards(session, quest, reward_bundle, user, claim_user_id)
        quest["status"] = "rewards_granted"
        quest_reward_state["status"] = "granted"
        quest_reward_state["granted_at"] = time.time()
        quest_reward_state["granted_by_user_id"] = user.id
        quest_reward_state["granted_by_name"] = user.name
        quest_reward_state["grant_summary"] = summary[:20]
        completed_event = emit_world_event(session, "quest_completed", {
            "source": "session_quest_turn_in",
            "actor_user_id": user.id,
            "quest_id": quest_id,
            "summary": f"Quest completed with rewards: {str(quest.get('title') or quest_id)[:120]}",
        })
        reaction_summary = consume_world_event(session, completed_event, {
            "unlock_handout_ids": list(reward_bundle.get("handout_unlock_ids") or []),
            "summary_messages": summary[:4],
            "refresh_quest_ids": [quest_id],
        })
        if reaction_summary.get("applied"):
            summary.append("Living world reactions applied.")
    elif has_rewards:
        quest["status"] = "rewards_pending"
    else:
        quest["status"] = "completed"
        quest_reward_state["status"] = "none"
        completed_event = emit_world_event(session, "quest_completed", {
            "source": "session_quest_turn_in",
            "actor_user_id": user.id,
            "quest_id": quest_id,
            "summary": f"Quest completed: {str(quest.get('title') or quest_id)[:120]}",
        })
        consume_world_event(session, completed_event, {"refresh_quest_ids": [quest_id]})

    quest["reward_bundle"] = reward_bundle
    quest["reward_state"] = quest_reward_state
    quest["turn_in_request"] = {}
    quest["updated_at"] = time.time()
    quests[index] = quest
    session.session_quests = quests
    session.session_quests = resolve_session_quest_progression(session)
    await save_campaign_async(session)
    await _broadcast_session_quests(session)
    progression = build_premium_progression_snapshot(session, role=user.role, user_id=user.id)
    rank = (progression.get("guild_rank") or {})
    rank_label = str(rank.get("rank_label") or "Guild Rank")
    rank_points = int(rank.get("points") or 0)
    unlocked_follow_ups = [
        str(row.get("title") or row.get("id") or "Quest")
        for row in (getattr(session, "session_quests", []) or [])
        if str((row or {}).get("availability_state") or "") == "unlocked"
        and str((row or {}).get("status") or "").strip().lower() == "available"
        and quest_id in list((row or {}).get("prerequisite_quest_ids") or [])
    ][:4]
    await _broadcast_quest_board_notice(
        session,
        actor=user,
        quest=quest,
        scope="quest_completion",
        message=f"{user.name} completed {str(quest.get('title') or 'a quest')[:120]}.",
        details=[
            *(summary[:4] if summary else ["Quest completion recorded."]),
            f"Guild standing: {rank_label} ({rank_points} points)",
            *([f"Unlocked follow-up contracts: {', '.join(unlocked_follow_ups)}"] if unlocked_follow_ups else []),
        ],
    )
    await manager.send_to(session.id, user.id, {
        "type": "session_quest_turn_in_result",
        "payload": {
            "ok": True,
            "quest_id": quest_id,
            "quest": quest,
            "status": quest.get("status"),
            "reward_summary": summary,
            "premium_progression": build_premium_progression_snapshot(session, role=user.role, user_id=user.id),
        },
    })


async def handle_session_quest_progress_override(payload: dict, session: Session, user: User):
    """DM-safe manual override/correction path for objective and quest lifecycle state."""
    if user.role != "dm" and not assistant_dm_has_scope(session, user, "quests.manage"):
        return
    quest_id = str(payload.get("quest_id") or payload.get("id") or "").strip()[:64]
    if not quest_id:
        return
    quests = list(getattr(session, "session_quests", []) or [])
    index = next((idx for idx, row in enumerate(quests) if str((row or {}).get("id") or "") == quest_id), None)
    if index is None:
        return
    quest = normalize_quest_payload_shape(dict(quests[index] or {}))
    if not apply_dm_override(quest, dict(payload or {})):
        await manager.send_to(session.id, user.id, {
            "type": "session_quest_override_result",
            "payload": {"ok": False, "quest_id": quest_id, "error": "No objective or status changed."},
        })
        return
    quest["turn_in_request"] = {}
    reward_state = dict(quest.get("reward_state") or {})
    if str(reward_state.get("status") or "").strip().lower() == "awaiting_dm_approval":
        reward_state["status"] = "none"
        quest["reward_state"] = reward_state
    quests[index] = quest
    session.session_quests = quests
    session.session_quests = resolve_session_quest_progression(session)
    await save_campaign_async(session)
    await _broadcast_session_quests(session)
    await manager.send_to(session.id, user.id, {
        "type": "session_quest_override_result",
        "payload": {"ok": True, "quest_id": quest_id, "quest": quest},
    })


# Whitelist of settings that DMs can broadcast to players
ALLOWED_DM_SETTINGS = {'show_vision_fallback_banner', 'viewer_fx_intensity', 'viewer_fx_disable_disruptive'}


async def handle_setting_update(payload: dict, session: Session, user: User):
    """Broadcast a DM setting change to all connected users (e.g., show_vision_fallback_banner)."""
    if user.role != "dm":
        return
    setting = str(payload.get("setting", "")).strip()
    value = payload.get("value")
    if not setting or setting not in ALLOWED_DM_SETTINGS:
        return
    # Broadcast to all users
    await manager.broadcast(session.id, {
        "type": "setting_sync",
        "payload": {"setting": setting, "value": value}
    })


# ═══════════════════════════════════════════════════════════════════
# HANDOUT SYSTEM
# ═══════════════════════════════════════════════════════════════════

async def _broadcast_handout_state(session: Session):
    """Broadcast handouts filtered by recipient to each connected user."""
    all_handouts = list(getattr(session, "handouts", []) or [])
    for uid, u in session.users.items():
        if u.role == "dm":
            visible = all_handouts
        else:
            # Filter by recipients first, then strip dm_secret_text
            targeted = [
                h for h in all_handouts
                if _handout_targets_user(h, session, uid)
            ]
            visible = [{k: v for k, v in h.items() if k != "dm_secret_text"} for h in targeted]
        await manager.send_to(session.id, uid, {
            "type": "handout_sync",
            "payload": {"handouts": visible}
        })


async def handle_send_handout(payload: dict, session: Session, user: User):
    """DM creates/updates and sends a handout to players."""
    if user.role != "dm" and not assistant_dm_has_scope(session, user, "handouts.manage"):
        return
    handouts = list(getattr(session, "handouts", []) or [])
    handout_id = str(payload.get("id") or secrets.token_hex(6))
    now = time.time()
    title = str(payload.get("title", "") or "").strip()[:120] or "Untitled Handout"
    # Support both new public_text/dm_secret_text fields and legacy content field
    public_text = str(payload.get("public_text", "") or payload.get("content", "") or "")[:12000]
    dm_secret_text = str(payload.get("dm_secret_text", "") or "")[:4000]
    # recipients: "all" or list of user IDs / subgroup:<id>
    raw_recipients = payload.get("recipients", "all")
    if isinstance(raw_recipients, list):
        cleaned = []
        for raw_target in raw_recipients[:50]:
            target = str(raw_target or "").strip()[:80]
            if not target or target in cleaned:
                continue
            if target.startswith("subgroup:"):
                subgroup = str(target.split(":", 1)[1] or "").strip().lower()[:48]
                if not subgroup:
                    continue
                cleaned.append(f"subgroup:{subgroup}")
            else:
                cleaned.append(target[:64])
        recipients = cleaned
    else:
        recipients = "all"

    existing = next((h for h in handouts if h.get("id") == handout_id), None)
    if existing:
        existing.update({
            "id": handout_id,
            "title": title,
            "public_text": public_text,
            "dm_secret_text": dm_secret_text,
            "content": public_text,  # backward compat
            "recipients": recipients,
            "updated_at": now,
        })
    else:
        handouts.append({
            "id": handout_id,
            "title": title,
            "public_text": public_text,
            "dm_secret_text": dm_secret_text,
            "content": public_text,  # backward compat
            "recipients": recipients,
            "created_at": now,
            "updated_at": now,
        })
    session.handouts = handouts
    handout_event = emit_world_event(session, "handout_unlocked", {
        "source": "send_handout",
        "actor_user_id": user.id,
        "handout_id": handout_id,
        "summary": f"Handout published: {title}",
    })
    consume_world_event(session, handout_event, {"unlock_handout_ids": [handout_id]})

    # Send parchment notification to targeted players (includes public_text for overlay)
    for uid, u in session.users.items():
        if u.role == "dm":
            continue
        is_target = _handout_targets_user({"recipients": recipients}, session, uid)
        if is_target:
            await manager.send_to(session.id, uid, {
                "type": "handout_received",
                "payload": {"handout_id": handout_id, "title": title, "public_text": public_text}
            })

    await _broadcast_handout_state(session)
    await save_campaign_async(session)


async def handle_handout_delete(payload: dict, session: Session, user: User):
    """DM deletes a handout."""
    if user.role != "dm":
        return
    handout_id = str(payload.get("id") or "")
    if not handout_id:
        return
    handouts = [h for h in (getattr(session, "handouts", []) or []) if h.get("id") != handout_id]
    session.handouts = handouts
    await _broadcast_handout_state(session)
    await save_campaign_async(session)


def _normalize_discovery_card_payload(raw: dict, user: User) -> dict | None:
    if not isinstance(raw, dict):
        return None
    discovery_id = str(raw.get("id") or secrets.token_hex(6)).strip()[:48] or secrets.token_hex(6)
    title = str(raw.get("title") or "").strip()[:120]
    body = str(raw.get("body") or raw.get("text") or raw.get("message") or "").strip()[:1200]
    if not title and not body:
        return None
    kind = str(raw.get("kind") or "clue").strip().lower()[:40] or "clue"
    visibility = str(raw.get("visibility") or "private_player").strip().lower()[:32] or "private_player"
    if visibility not in {"private_player", "party_public", "dm_reveal_later", "subgroup_public"}:
        visibility = "private_player"
    target_user_id = str(raw.get("target_user_id") or "").strip()[:64]
    subgroup_ids = _normalize_optional_ref_list(raw.get("subgroup_ids"), limit=8)
    subgroup_ids = [str(sid or "").strip().lower()[:48] for sid in subgroup_ids if str(sid or "").strip()]
    share_mode = str(raw.get("share_mode") or "").strip().lower()[:24]
    allow_player_share = bool(raw.get("allow_player_share")) or share_mode in {"player_optional", "allowed"}
    if visibility != "private_player":
        target_user_id = ""
        allow_player_share = False
    if visibility != "subgroup_public":
        subgroup_ids = []
    audience = "party" if visibility in {"party_public", "subgroup_public"} else "player"
    return {
        "id": discovery_id,
        "title": title or "New Discovery",
        "body": body,
        "kind": kind,
        "icon": str(raw.get("icon") or "").strip()[:8],
        "tone": str(raw.get("tone") or "mystic").strip().lower()[:24] or "mystic",
        "source": str(raw.get("source") or raw.get("trigger") or "dm").strip()[:80] or "dm",
        "visibility": visibility,
        "target_user_id": target_user_id,
        "subgroup_ids": subgroup_ids,
        "allow_player_share": allow_player_share,
        "can_acknowledge": bool(raw.get("can_acknowledge", True)),
        "can_save": bool(raw.get("can_save", True)),
        "can_share": bool(allow_player_share),
        "created_at": time.time(),
        "created_by_user_id": user.id,
        "created_by_name": user.name,
        "revealed_at": time.time() if visibility == "party_public" else None,
        "acknowledged_by": [],
        "saved_by": [],
        "shared_at": None,
        "meta": {
            "scope": "discovery_card",
            "audience": audience,
            "ui_channel": "discovery_card",
            "kind": kind,
            "visibility": visibility,
        },
    }


def _discovery_ws_message(discovery: dict) -> dict:
    payload = {"discovery": discovery}
    payload.update(discovery.get("meta") or {})
    return {
        "type": "discovery_card",
        "payload": payload,
        "meta": dict(discovery.get("meta") or {}),
    }


def _normalize_private_story_hook_payload(raw: dict, user: User, existing: dict | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    prior = dict(existing or {})
    hook_id = str(raw.get("id") or prior.get("id") or secrets.token_hex(6)).strip()[:48] or secrets.token_hex(6)
    target_user_id = str(raw.get("target_user_id") or prior.get("target_user_id") or "").strip()[:64]
    kind = str(raw.get("kind") or prior.get("kind") or "prompt").strip().lower()[:24] or "prompt"
    if kind not in {"prompt", "objective"}:
        kind = "prompt"
    title = str(raw.get("title") if raw.get("title") is not None else prior.get("title") or "").strip()[:140]
    body = str(raw.get("body") if raw.get("body") is not None else prior.get("body") or "").strip()[:1600]
    if not target_user_id or (not title and not body):
        return None
    status = str(raw.get("status") or prior.get("status") or "active").strip().lower()[:24] or "active"
    if status not in {"active", "resolved"}:
        status = "active"
    now = time.time()
    persistent = bool(raw.get("persistent", prior.get("persistent", kind == "objective")))
    one_off = bool(raw.get("one_off", prior.get("one_off", kind == "prompt" and not persistent)))
    resolved_at = prior.get("resolved_at")
    if status == "resolved":
        resolved_at = float(raw.get("resolved_at") or resolved_at or now)
    else:
        resolved_at = None
    tone = str(raw.get("tone") or prior.get("tone") or "personal").strip().lower()[:24] or "personal"
    source = str(raw.get("source") or prior.get("source") or "dm_manual").strip()[:80] or "dm_manual"
    created_at = float(prior.get("created_at") or now)
    created_by_user_id = str(prior.get("created_by_user_id") or user.id)[:64]
    created_by_name = str(prior.get("created_by_name") or user.name)[:120]
    return {
        "id": hook_id,
        "target_user_id": target_user_id,
        "title": title or ("Private Objective" if kind == "objective" else "Private Prompt"),
        "body": body,
        "kind": kind,
        "status": status,
        "persistent": persistent,
        "one_off": one_off,
        "tone": tone,
        "source": source,
        "created_at": created_at,
        "updated_at": now,
        "resolved_at": resolved_at,
        "created_by_user_id": created_by_user_id,
        "created_by_name": created_by_name,
        "meta": {
            "scope": "private_story_hook",
            "audience": "player",
            "ui_channel": "private_story_hook",
            "kind": kind,
            "status": status,
        },
    }


def _story_hook_ws_message(hook: dict, event: str = "sync") -> dict:
    return {
        "type": "private_story_hook",
        "payload": {
            "hook": hook,
            "event": event,
        },
        "meta": dict(hook.get("meta") or {}),
    }


async def _broadcast_private_story_hooks_state(session: Session):
    for uid, current_user in session.users.items():
        visible = session._visible_private_story_hooks_for_role(current_user.role, uid)
        await manager.send_to(session.id, uid, {
            "type": "private_story_hooks_sync",
            "payload": {"hooks": visible},
        })


async def handle_discovery_trigger(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    discovery = _normalize_discovery_card_payload(payload or {}, user)
    if not discovery:
        return await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Discovery cards need a title or body."},
        })

    visibility = discovery["visibility"]
    cards = [
        card for card in (getattr(session, "discovery_cards", []) or [])
        if str(card.get("id") or "") != discovery["id"]
    ]

    if visibility == "private_player":
        target_user_id = str(discovery.get("target_user_id") or "").strip()
        target = session.users.get(target_user_id)
        if not target or target.role != "player":
            return await manager.send_to(session.id, user.id, {
                "type": "error",
                "payload": {"message": "Choose a valid player for a private discovery."},
            })
        cards.append(discovery)
        session.discovery_cards = cards
        discovery_event = emit_world_event(session, "discovery_unlocked", {
            "source": "discovery_trigger",
            "actor_user_id": user.id,
            "discovery_id": str(discovery.get("id") or ""),
            "summary": f"Discovery unlocked: {str(discovery.get('title') or discovery.get('id') or 'Discovery')[:120]}",
        })
        consume_world_event(session, discovery_event, {
            "append_discovery_ids": [discovery.get("id")],
            "summary_messages": [discovery_event.get("summary") or "Discovery unlocked."],
        })
        await manager.send_to(session.id, target.id, _discovery_ws_message(discovery))
        await manager.send_to(session.id, user.id, {
            "type": "discovery_card_pending",
            "payload": {"discovery": discovery, "status": "delivered_private"},
        })
        await save_campaign_async(session)
        return

    if visibility == "dm_reveal_later":
        cards.append(discovery)
        session.discovery_cards = cards
        discovery_event = emit_world_event(session, "discovery_unlocked", {
            "source": "discovery_trigger",
            "actor_user_id": user.id,
            "discovery_id": str(discovery.get("id") or ""),
            "summary": f"Discovery queued for reveal: {str(discovery.get('title') or discovery.get('id') or 'Discovery')[:120]}",
        })
        consume_world_event(session, discovery_event, {"append_discovery_ids": [discovery.get("id")]})
        await manager.send_to(session.id, user.id, {
            "type": "discovery_card_pending",
            "payload": {"discovery": discovery, "status": "held_for_reveal"},
        })
        await save_campaign_async(session)
        return

    if visibility == "subgroup_public":
        target_subgroups = {
            str(sid or "").strip().lower()[:48]
            for sid in (discovery.get("subgroup_ids") or [])
            if str(sid or "").strip()
        }
        if not target_subgroups:
            return await manager.send_to(session.id, user.id, {
                "type": "error",
                "payload": {"message": "Choose at least one subgroup for subgroup discovery."},
            })
        discovery["revealed_at"] = time.time()
        cards.append(discovery)
        session.discovery_cards = cards
        discovery_event = emit_world_event(session, "discovery_unlocked", {
            "source": "discovery_trigger",
            "actor_user_id": user.id,
            "discovery_id": str(discovery.get("id") or ""),
            "summary": f"Subgroup discovery unlocked: {str(discovery.get('title') or discovery.get('id') or 'Discovery')[:120]}",
        })
        consume_world_event(session, discovery_event, {"append_discovery_ids": [discovery.get("id")]})
        message = _discovery_ws_message(discovery)
        for uid, target in session.users.items():
            target_role = str(getattr(target, "role", "") or "").strip().lower()
            if target_role == "dm":
                await manager.send_to(session.id, uid, message)
                continue
            if target_role != "player":
                continue
            if session.get_user_subgroup_id(uid) in target_subgroups:
                await manager.send_to(session.id, uid, message)
        await save_campaign_async(session)
        return

    discovery["revealed_at"] = time.time()
    cards.append(discovery)
    session.discovery_cards = cards
    discovery_event = emit_world_event(session, "discovery_unlocked", {
        "source": "discovery_trigger",
        "actor_user_id": user.id,
        "discovery_id": str(discovery.get("id") or ""),
        "summary": f"Discovery unlocked: {str(discovery.get('title') or discovery.get('id') or 'Discovery')[:120]}",
    })
    consume_world_event(session, discovery_event, {"append_discovery_ids": [discovery.get("id")]})
    message = _discovery_ws_message(discovery)
    for uid, target in session.users.items():
        if target.role in {"dm", "player"}:
            await manager.send_to(session.id, uid, message)
    await save_campaign_async(session)


async def handle_discovery_reveal(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    discovery_id = str(payload.get("id") or "").strip()[:48]
    if not discovery_id:
        return
    cards = list(getattr(session, "discovery_cards", []) or [])
    discovery = next((card for card in cards if str(card.get("id") or "") == discovery_id), None)
    if not discovery:
        return await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Discovery card not found."},
        })
    if str(discovery.get("visibility") or "") != "dm_reveal_later":
        return await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "That discovery is not waiting for reveal."},
        })
    discovery["visibility"] = "party_public"
    discovery["revealed_at"] = time.time()
    discovery["target_user_id"] = ""
    discovery["allow_player_share"] = False
    discovery["can_share"] = False
    discovery["meta"] = {
        "scope": "discovery_card",
        "audience": "party",
        "ui_channel": "discovery_card",
        "kind": str(discovery.get("kind") or "clue"),
        "visibility": "party_public",
    }
    message = _discovery_ws_message(discovery)
    for uid, target in session.users.items():
        if target.role in {"dm", "player"}:
            await manager.send_to(session.id, uid, message)
    await save_campaign_async(session)


async def handle_discovery_acknowledge(payload: dict, session: Session, user: User):
    discovery_id = str(payload.get("id") or "").strip()[:48]
    if not discovery_id:
        return
    cards = list(getattr(session, "discovery_cards", []) or [])
    discovery = next((card for card in cards if str(card.get("id") or "") == discovery_id), None)
    if not discovery:
        return
    visible_to_user = str(discovery.get("visibility") or "") == "party_public" or (
        str(discovery.get("visibility") or "") == "private_player"
        and str(discovery.get("target_user_id") or "") == str(user.id)
    )
    if user.role != "dm" and not visible_to_user:
        return
    acknowledged_by = [str(uid) for uid in (discovery.get("acknowledged_by") or []) if str(uid).strip()]
    if user.id not in acknowledged_by:
        acknowledged_by.append(user.id)
    discovery["acknowledged_by"] = acknowledged_by
    quests = list(getattr(session, "session_quests", []) or [])
    event = {"event_type": "read_handout_clue", "target_id": discovery_id}
    for idx, quest in enumerate(quests):
        entry = normalize_quest_payload_shape(dict(quest or {}))
        if apply_objective_event(entry, event):
            quests[idx] = entry
    session.session_quests = quests
    await save_campaign_async(session)


async def handle_discovery_save(payload: dict, session: Session, user: User):
    discovery_id = str(payload.get("id") or "").strip()[:48]
    if not discovery_id or user.role != "player":
        return
    cards = list(getattr(session, "discovery_cards", []) or [])
    discovery = next((card for card in cards if str(card.get("id") or "") == discovery_id), None)
    if not discovery or not bool(discovery.get("can_save", True)):
        return
    visible_to_user = str(discovery.get("visibility") or "") == "party_public" or (
        str(discovery.get("visibility") or "") == "private_player"
        and str(discovery.get("target_user_id") or "") == str(user.id)
    )
    if not visible_to_user:
        return
    saved_by = [str(uid) for uid in (discovery.get("saved_by") or []) if str(uid).strip()]
    if user.id not in saved_by:
        saved_by.append(user.id)
    discovery["saved_by"] = saved_by
    await save_campaign_async(session)
    await manager.send_to(session.id, user.id, {
        "type": "discovery_saved",
        "payload": {"discovery": discovery},
    })


async def handle_discovery_unsave(payload: dict, session: Session, user: User):
    discovery_id = str(payload.get("id") or "").strip()[:48]
    if not discovery_id or user.role != "player":
        return
    cards = list(getattr(session, "discovery_cards", []) or [])
    discovery = next((card for card in cards if str(card.get("id") or "") == discovery_id), None)
    if not discovery:
        return
    saved_by = [str(uid) for uid in (discovery.get("saved_by") or []) if str(uid).strip() and str(uid) != str(user.id)]
    discovery["saved_by"] = saved_by
    await save_campaign_async(session)
    await manager.send_to(session.id, user.id, {
        "type": "discovery_unsaved",
        "payload": {"id": discovery_id},
    })


async def handle_private_story_hook_upsert(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    hooks = list(getattr(session, "private_story_hooks", []) or [])
    hook_id = str(payload.get("id") or "").strip()[:48]
    existing = next((entry for entry in hooks if str(entry.get("id") or "") == hook_id), None) if hook_id else None
    hook = _normalize_private_story_hook_payload(payload or {}, user, existing)
    if not hook:
        return await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Private prompts need a player and either a title or body."},
        })
    target = session.users.get(hook["target_user_id"])
    if not target or target.role != "player":
        return await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Choose a valid player for this private prompt or objective."},
        })
    hooks = [entry for entry in hooks if str(entry.get("id") or "") != hook["id"]]
    hooks.append(hook)
    hooks.sort(key=lambda entry: float(entry.get("updated_at") or entry.get("created_at") or 0.0), reverse=True)
    session.private_story_hooks = hooks
    await _broadcast_private_story_hooks_state(session)
    await manager.send_to(session.id, target.id, _story_hook_ws_message(hook, "upsert"))
    await manager.send_to(session.id, user.id, {
        "type": "private_story_hook_admin_status",
        "payload": {
            "hook": hook,
            "status": "updated" if existing else "created",
        },
    })
    await save_campaign_async(session)


async def handle_private_story_hook_delete(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    hook_id = str(payload.get("id") or "").strip()[:48]
    if not hook_id:
        return
    hooks = list(getattr(session, "private_story_hooks", []) or [])
    existing = next((entry for entry in hooks if str(entry.get("id") or "") == hook_id), None)
    if not existing:
        return
    session.private_story_hooks = [entry for entry in hooks if str(entry.get("id") or "") != hook_id]
    await _broadcast_private_story_hooks_state(session)
    await manager.send_to(session.id, user.id, {
        "type": "private_story_hook_admin_status",
        "payload": {
            "hook": existing,
            "status": "deleted",
        },
    })
    target_user_id = str(existing.get("target_user_id") or "").strip()
    if target_user_id:
        await manager.send_to(session.id, target_user_id, {
            "type": "private_story_hook_removed",
            "payload": {"id": hook_id},
        })
    await save_campaign_async(session)


async def handle_private_story_hook_resolve(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    hook_id = str(payload.get("id") or "").strip()[:48]
    if not hook_id:
        return
    hooks = list(getattr(session, "private_story_hooks", []) or [])
    existing = next((entry for entry in hooks if str(entry.get("id") or "") == hook_id), None)
    if not existing:
        return
    next_payload = dict(existing)
    next_payload["status"] = "resolved" if bool(payload.get("resolved", True)) else "active"
    hook = _normalize_private_story_hook_payload(next_payload, user, existing)
    if not hook:
        return
    session.private_story_hooks = [
        hook if str(entry.get("id") or "") == hook_id else entry
        for entry in hooks
    ]
    await _broadcast_private_story_hooks_state(session)
    target_user_id = str(hook.get("target_user_id") or "").strip()
    if target_user_id:
        await manager.send_to(session.id, target_user_id, _story_hook_ws_message(hook, "resolve"))
    await manager.send_to(session.id, user.id, {
        "type": "private_story_hook_admin_status",
        "payload": {
            "hook": hook,
            "status": "resolved" if hook["status"] == "resolved" else "reopened",
        },
    })
    await save_campaign_async(session)


def _normalize_encounter_template_entry(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    creature_id = str(raw.get("creature_id") or raw.get("canonical_creature_id") or "").strip()[:80]
    if not creature_id:
        return None
    qty = _safe_int(raw.get("qty"), 1, minimum=1, maximum=20)
    return {
        "creature_id": creature_id,
        "canonical_creature_id": creature_id,
        "name": str(raw.get("name") or "Creature").strip()[:120] or "Creature",
        "qty": qty,
        "source": str(raw.get("source") or "custom").strip()[:32].lower() or "custom",
        "source_type": str(raw.get("source_type") or raw.get("source") or "custom").strip()[:32].lower() or "custom",
        "entry_type": str(raw.get("entry_type") or raw.get("creature_type") or "monster").strip()[:24].lower() or "monster",
        "creature_type": str(raw.get("creature_type") or raw.get("entry_type") or "monster").strip()[:24].lower() or "monster",
        "monster_type": str(raw.get("monster_type") or "").strip()[:40].lower(),
        "cr": str(raw.get("cr") or "").strip()[:16],
    }


def _normalize_encounter_template(raw: dict, *, existing: dict | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:120]
    if not name:
        return None
    entries = []
    for row in list(raw.get("entries") or [])[:40]:
        entry = _normalize_encounter_template_entry(row)
        if entry:
            entries.append(entry)
    if not entries:
        return None
    now = time.time()
    template_id = str(raw.get("id") or (existing or {}).get("id") or secrets.token_hex(6)).strip()[:48] or secrets.token_hex(6)
    created_at = float((existing or {}).get("created_at") or now)
    return {
        "id": template_id,
        "name": name,
        "notes": str(raw.get("notes") or "").strip()[:400],
        "map_context": str(raw.get("map_context") or "world").strip()[:80] or "world",
        "entries": entries,
        "created_at": created_at,
        "updated_at": now,
    }


async def handle_encounter_template_upsert(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    templates = list(getattr(session, "encounter_templates", []) or [])
    existing = next((tpl for tpl in templates if str(tpl.get("id") or "") == str(payload.get("id") or "")), None)
    template = _normalize_encounter_template(payload or {}, existing=existing)
    if not template:
        return
    if existing:
        existing.clear()
        existing.update(template)
    else:
        templates.append(template)
    templates.sort(key=lambda item: (str(item.get("name") or "").lower(), float(item.get("created_at") or 0.0)))
    session.encounter_templates = templates
    await _broadcast_encounter_template_state(session)
    await save_campaign_async(session)


async def handle_encounter_template_delete(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    template_id = str(payload.get("id") or "").strip()[:48]
    if not template_id:
        return
    session.encounter_templates = [
        tpl for tpl in (getattr(session, "encounter_templates", []) or [])
        if str(tpl.get("id") or "") != template_id
    ]
    await _broadcast_encounter_template_state(session)
    await save_campaign_async(session)


async def handle_encounter_spawn_group(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    entries = []
    for row in list(payload.get("entries") or [])[:40]:
        entry = _normalize_encounter_template_entry(row)
        if entry:
            entries.append(entry)
    if not entries:
        return

    from server.creatures.service import spawn_creature_from_library_entry
    map_ctx = str(payload.get("map_context") or "world").strip()[:80] or "world"
    base_x = _safe_int(payload.get("x"), 120, minimum=-100000, maximum=100000)
    base_y = _safe_int(payload.get("y"), 120, minimum=-100000, maximum=100000)
    spacing = _safe_int(payload.get("spacing"), 60, minimum=40, maximum=160)
    grid_size_px = _safe_int(payload.get("grid_size_px") or payload.get("gridSizePx"), 0, minimum=0, maximum=256) or None
    max_cols = max(1, min(6, _safe_int(payload.get("columns"), 3, minimum=1, maximum=6)))
    template_name = str(payload.get("template_name") or "Encounter").strip()[:120] or "Encounter"

    spawned = []
    index = 0
    for entry in entries:
        for _ in range(max(1, int(entry.get("qty", 1) or 1))):
            row = index // max_cols
            col = index % max_cols
            token = await spawn_creature_from_library_entry(
                session=session,
                creature_id=str(entry.get("canonical_creature_id") or entry.get("creature_id") or "").strip(),
                dm_user_id=user.id,
                request_source=str(entry.get("source_type") or entry.get("source") or "").strip().lower(),
                request_entity_type=str(entry.get("entry_type") or entry.get("creature_type") or "").strip().lower(),
                x=float(base_x + (col * spacing)),
                y=float(base_y + (row * spacing)),
                map_ctx=map_ctx,
                session_user=session.users.get(user.id),
                grid_size_px=grid_size_px,
            )
            if token:
                spawned.append(token.id)
            index += 1

    if spawned:
        session.add_log(f"{user.name} spawned encounter '{template_name}' ({len(spawned)} tokens).", "system", user.name)
        await manager.broadcast(session.id, {
            "type": "encounter_spawn_result",
            "payload": {"template_name": template_name, "token_ids": spawned, "map_context": map_ctx},
        })
        await save_campaign_async(session)


# ═══════════════════════════════════════════════════════════════════
# SPELL LIBRARY SYSTEM
# ═══════════════════════════════════════════════════════════════════

async def handle_create_custom_spell(payload: dict, session: Session, user: User):
    """DM creates/updates a custom spell, broadcasts library update."""
    from server.rules_db import upsert_custom_spell, get_spell_library
    if user.role != "dm":
        return
    spell_data = payload.get("spell") or {}
    if not spell_data or not str(spell_data.get("name") or "").strip():
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Spell name is required."}
        })
        return
    saved = upsert_custom_spell(session.id, user.id, spell_data)
    library = get_spell_library(session.id)
    await manager.broadcast(session.id, {
        "type": "spell_library_updated",
        "payload": {"library": library, "changed_spell": saved}
    })


async def handle_grant_spell_to_player(payload: dict, session: Session, user: User):
    """DM grants a spell to a player. Sends spell_granted directly to recipient."""
    from server.rules_db import grant_spell, get_spell_by_id, get_custom_spell, get_all_granted_spells
    if user.role != "dm":
        return
    spell_id = str(payload.get("spell_id") or "").strip()
    spell_source = str(payload.get("spell_source") or "srd").strip()
    recipient_user_id = str(payload.get("recipient_user_id") or "").strip()

    if not spell_id or not recipient_user_id:
        return
    recipient = session.users.get(recipient_user_id)
    if not recipient or recipient.role not in ("player",):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Invalid recipient."}
        })
        return

    # Fetch spell data
    if spell_source == "custom":
        spell_data = get_custom_spell(session.id, spell_id)
    else:
        spell_data = get_spell_by_id(spell_id)

    if not spell_data:
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Spell not found."}
        })
        return

    grant_record = grant_spell(
        session_id=session.id,
        recipient_user_id=recipient_user_id,
        spell_id=spell_id,
        spell_source=spell_source,
        granter_user_id=user.id,
    )

    spell_data["spell_source"] = spell_source
    spell_data["grant_id"] = grant_record["id"]
    spell_data["granted_by_user_id"] = user.id
    spell_data["granted_by_name"] = user.name

    await manager.send_to(session.id, recipient_user_id, {
        "type": "spell_granted",
        "payload": {
            "grant": grant_record,
            "spell": spell_data,
            "granted_by_name": user.name,
        }
    })

    # Also update DM's view of all grants
    all_grants = get_all_granted_spells(session.id)
    await manager.send_to(session.id, user.id, {
        "type": "spell_grants_sync",
        "payload": {"grants": all_grants}
    })


async def handle_get_spell_library(payload: dict, session: Session, user: User):
    """Return a role-safe spell payload."""
    from server.rules_db import (
        get_all_granted_spells,
        get_granted_spell_library_for_user,
        get_granted_spells_for_user,
        get_spell_library,
    )
    if user.role == "dm":
        library = get_spell_library(session.id)
        grants = get_all_granted_spells(session.id)
        player_spells = []
    else:
        library = []
        grants = get_granted_spells_for_user(session.id, user.id)
        player_spells = get_granted_spell_library_for_user(session.id, user.id)
    await manager.send_to(session.id, user.id, {
        "type": "spell_library_sync",
        "payload": {"library": library, "grants": grants, "player_spells": player_spells}
    })


async def handle_revoke_spell(payload: dict, session: Session, user: User):
    """DM revokes a previously granted spell."""
    from server.rules_db import revoke_granted_spell, get_all_granted_spells
    if user.role != "dm":
        return
    grant_id = str(payload.get("grant_id") or "").strip()
    spell_name = str(payload.get("spell_name") or "spell").strip()
    if not grant_id:
        return
    recipient_id = revoke_granted_spell(grant_id, session.id)
    if recipient_id:
        await manager.send_to(session.id, recipient_id, {
            "type": "spell_revoked",
            "payload": {"grant_id": grant_id, "spell_name": spell_name}
        })
        all_grants = get_all_granted_spells(session.id)
        await manager.send_to(session.id, user.id, {
            "type": "spell_grants_sync",
            "payload": {"grants": all_grants}
        })


_VIEWER_EMOTES = {"⚔️", "💀", "🎲", "❤️", "🔥", "😱", "👑", "🍺"}
_VIEWER_EMOTE_COOLDOWN = 3.0  # seconds


async def handle_viewer_emote(payload: dict, session: Session, user: User):
    """Viewer sends a reaction emote visible to all clients."""
    if str(getattr(user, "role", "") or "").strip().lower() != "viewer":
        return

    emote = str(payload.get("emote", "")).strip()
    if emote not in _VIEWER_EMOTES:
        return

    now = time.time()
    if now - user.last_emote_at < _VIEWER_EMOTE_COOLDOWN:
        return
    user.last_emote_at = now

    await manager.broadcast(session.id, {
        "type": "viewer_emote",
        "payload": {
            "viewer_name": user.name,
            "emote": emote,
            "user_id": user.id,
        }
    })


# ═══════════════════════════════════════════════════════════════════
# PARTY VOTE SYSTEM
# ═══════════════════════════════════════════════════════════════════

_MAX_POLL_OPTIONS = 5
_MAX_POLL_DURATION_SEC = 600
_POLL_RESULTS_MODES = {"live", "final"}


def _normalize_poll_results_mode(raw_value) -> str:
    mode = str(raw_value or "live").strip().lower()
    return mode if mode in _POLL_RESULTS_MODES else "live"


def _normalize_poll_duration(raw_value) -> int | None:
    if raw_value in (None, "", False):
        return 60
    try:
        duration = int(raw_value)
    except (TypeError, ValueError):
        return 60
    if duration <= 0:
        return None
    return min(duration, _MAX_POLL_DURATION_SEC)


def _poll_vote_counts(poll: dict) -> list[int]:
    options = poll.get("options") or []
    counts = [0] * len(options)
    for opt_idx in (poll.get("votes") or {}).values():
        if isinstance(opt_idx, int) and 0 <= opt_idx < len(counts):
            counts[opt_idx] += 1
    return counts


def _poll_public_payload(session: Session) -> dict:
    """Build the public (non-DM) vote payload with aggregate counts only."""
    poll = session.active_poll
    if not poll:
        return None
    counts = _poll_vote_counts(poll)
    return {
        "id": poll.get("id"),
        "kind": poll.get("kind") or "",
        "quest_id": poll.get("quest_id") or "",
        "title": poll.get("title") or "Party Vote",
        "question": poll.get("question"),
        "options": list(poll.get("options") or []),
        "vote_counts": counts,
        "total_votes": len(poll.get("votes") or {}),
        "created_at": poll.get("created_at"),
        "closes_at": poll.get("closes_at"),
        "closed": bool(poll.get("closed")),
        "results_mode": _normalize_poll_results_mode(poll.get("results_mode")),
        "closed_by": poll.get("closed_by") or "dm",
        "closed_reason": poll.get("closed_reason") or ("dm_closed" if poll.get("closed") else "active"),
        "authority_note": poll.get("authority_note") or "The DM keeps final say.",
    }


def _poll_full_payload(session: Session) -> dict | None:
    poll = session.active_poll
    if not poll:
        return None
    full = dict(poll)
    full.setdefault("title", "Party Vote")
    full["results_mode"] = _normalize_poll_results_mode(full.get("results_mode"))
    full["authority_note"] = full.get("authority_note") or "The DM keeps final say."
    full["closed_by"] = full.get("closed_by") or "dm"
    full["closed_reason"] = full.get("closed_reason") or ("dm_closed" if full.get("closed") else "active")
    full["vote_counts"] = _poll_vote_counts(full)
    full["total_votes"] = len(full.get("votes") or {})
    return full


async def _broadcast_poll_state(session: Session, message_type: str, *, include_dm_full: bool = True):
    public = _poll_public_payload(session)
    full = _poll_full_payload(session) if include_dm_full else public
    for uid, participant in session.users.items():
        payload = full if getattr(participant, "role", "") == "dm" else public
        if payload is None:
            continue
        personal = dict(payload)
        if getattr(participant, "role", "") != "dm":
            votes = (session.active_poll or {}).get("votes") or {}
            personal["user_vote"] = votes.get(uid)
        await manager.send_to(session.id, uid, {"type": message_type, "payload": personal})


async def _close_poll(session: Session, *, closed_by: str, closed_reason: str):
    poll = session.active_poll
    if not poll or poll.get("closed"):
        return
    poll["closed"] = True
    poll["closed_by"] = closed_by
    poll["closed_reason"] = closed_reason
    await save_campaign_async(session)
    await _broadcast_poll_state(session, "poll_closed")


async def _auto_close_poll_after(session: Session, poll_id: str, duration_sec: int):
    await asyncio.sleep(max(1, int(duration_sec)))
    poll = session.active_poll
    if not poll or poll.get("id") != poll_id or poll.get("closed"):
        return
    await _close_poll(session, closed_by="timer", closed_reason="timer_elapsed")


async def handle_poll_create(payload: dict, session: Session, user: User):
    """DM creates a lightweight party vote prompt."""
    if user.role != "dm":
        return
    question = str(payload.get("question") or "").strip()[:300]
    if not question:
        return
    raw_options = payload.get("options") or []
    if not isinstance(raw_options, list):
        return
    options = [str(o).strip()[:200] for o in raw_options if str(o).strip()][:_MAX_POLL_OPTIONS]
    if len(options) < 2:
        return
    duration_sec = _normalize_poll_duration(payload.get("duration_sec"))
    now = time.time()
    poll_id = secrets.token_hex(6)
    session.active_poll = {
        "id": poll_id,
        "title": str(payload.get("title") or "").strip()[:120] or "Party Vote",
        "question": question,
        "options": options,
        "votes": {},
        "created_at": now,
        "closes_at": (now + duration_sec) if duration_sec else None,
        "closed": False,
        "results_mode": _normalize_poll_results_mode(payload.get("results_mode")),
        "authority_note": "The DM keeps final say.",
        "closed_by": "dm",
        "closed_reason": "active",
    }
    await save_campaign_async(session)
    await _broadcast_poll_state(session, "poll_created")
    if duration_sec:
        asyncio.create_task(_auto_close_poll_after(session, poll_id, duration_sec))


async def handle_poll_vote(payload: dict, session: Session, user: User):
    """Viewer or player votes on the active party vote."""
    if user.role == "dm":
        return
    poll = session.active_poll
    if not poll or poll.get("closed"):
        return
    poll_id = str(payload.get("poll_id") or "").strip()
    if poll_id != poll.get("id"):
        return
    option_index = payload.get("option_index")
    if not isinstance(option_index, int):
        return
    options = poll.get("options") or []
    if option_index < 0 or option_index >= len(options):
        return
    poll.setdefault("votes", {})[user.id] = option_index
    await save_campaign_async(session)
    await _broadcast_poll_state(session, "poll_updated")


async def handle_poll_close(payload: dict, session: Session, user: User):
    """DM closes the active party vote."""
    if user.role != "dm":
        return
    await _close_poll(session, closed_by="dm", closed_reason="dm_closed")


# ── Party Memory Log ────────────────────────────────────────────────────────

_MEMORY_MAX_ENTRIES = 200
_MEMORY_TEXT_LIMIT = 160


async def _broadcast_party_memory_state(session: Session):
    """Send full party memory log to all connected users."""
    entries = list(getattr(session, "party_memory_log", []) or [])
    await manager.broadcast(session.id, {
        "type": "party_memory_sync",
        "payload": {"entries": entries},
    })


def add_auto_party_memory(session: Session, text: str) -> dict:
    """Create a system-generated memory entry and append it to the session log.

    Returns the new entry dict. Callers are responsible for broadcasting and
    saving; this function only mutates in-memory state.
    """
    entry = {
        "id": secrets.token_hex(6),
        "text": str(text).strip()[:_MEMORY_TEXT_LIMIT],
        "source": "auto",
        "created_at": time.time(),
    }
    log = getattr(session, "party_memory_log", None)
    if log is None:
        session.party_memory_log = []
        log = session.party_memory_log
    log.append(entry)
    if len(log) > _MEMORY_MAX_ENTRIES:
        session.party_memory_log = log[-_MEMORY_MAX_ENTRIES:]
    return entry


async def handle_party_memory_add(payload: dict, session: Session, user: User):
    """DM manually adds a memorable moment to the party memory log."""
    if user.role != "dm":
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Only the DM can add party memories."},
        })
        return

    text = str(payload.get("text") or "").strip()[:_MEMORY_TEXT_LIMIT]
    if not text:
        return

    entry = {
        "id": secrets.token_hex(6),
        "text": text,
        "source": "dm",
        "created_at": time.time(),
    }
    log = getattr(session, "party_memory_log", None)
    if log is None:
        session.party_memory_log = []
        log = session.party_memory_log
    log.append(entry)
    if len(log) > _MEMORY_MAX_ENTRIES:
        session.party_memory_log = log[-_MEMORY_MAX_ENTRIES:]

    await save_campaign_async(session)
    await _broadcast_party_memory_state(session)


async def handle_party_memory_delete(payload: dict, session: Session, user: User):
    """DM deletes a party memory entry by id."""
    if user.role != "dm":
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Only the DM can delete party memories."},
        })
        return

    entry_id = str(payload.get("id") or "").strip()
    if not entry_id:
        return

    log = getattr(session, "party_memory_log", None) or []
    session.party_memory_log = [e for e in log if e.get("id") != entry_id]

    await save_campaign_async(session)
    await _broadcast_party_memory_state(session)



# ── DM Notes ────────────────────────────────────────────────────────────────

async def handle_dm_notes_save(payload: dict, session: Session, user: User):
    """DM saves private notes; never broadcast to other users."""
    if user.role != "dm":
        return
    notes = str(payload.get("notes") or "")[:8000]
    session.dm_notes = notes
    await save_campaign_async(session)
    # Acknowledge only to the DM
    await manager.send_to(session.id, user.id, {
        "type": "dm_notes_saved",
        "payload": {"ok": True},
    })
