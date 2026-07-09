import asyncio
import secrets

from fastapi.responses import JSONResponse

from server.connections import manager
from server.db import save_campaign_async
from server.http.auth import auth_display_name, auth_player_key, get_request_user
from server.http.session_access import get_or_restore_session, resolve_session_authority
from server.session import create_session, get_session, join_session, normalize_profile_owner_key, set_player_gold_for_user, build_token_runtime_payload, normalize_fog_maps, normalize_map_context




def _profile_owner_keys_for_auth(auth_user) -> list[str]:
    keys: list[str] = []
    display = normalize_profile_owner_key(auth_display_name(auth_user, fallback=""))
    if display:
        keys.append(display)
    username = normalize_profile_owner_key((auth_user or {}).get("username") or "")
    if username and username not in keys:
        keys.append(username)
    uid = str((auth_user or {}).get("id") or "").strip()
    if uid and uid not in keys:
        keys.append(uid)
    return keys


def _find_profile_for_auth_user(session, auth_user, profile_id: str) -> dict | None:
    target = str(profile_id or "").strip()
    if not target:
        return None
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    for owner_key in _profile_owner_keys_for_auth(auth_user):
        rows = list(profiles.get(owner_key) or [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or "").strip() == target:
                return row
    return None


def _equipment_entries_from_profile(profile: dict) -> list[dict]:
    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    equipment = native.get("equipment") if isinstance(native.get("equipment"), dict) else {}
    choices = equipment.get("choices") if isinstance(equipment.get("choices"), list) else []
    entries: list[dict] = []
    for raw in choices:
        if isinstance(raw, str):
            name = raw.strip()[:80]
            notes = ''
        elif isinstance(raw, dict):
            name = str(raw.get("name") or raw.get("id") or "").strip()[:80]
            notes = str(raw.get("notes") or raw.get("description") or "").strip()[:160]
        else:
            continue
        if not name:
            continue
        lower = name.lower()
        item = {"name": name, "qty": 1, "source": "Character creation"}
        if notes:
            item["notes"] = notes
        if any(k in lower for k in ["mail", "armor", "armour", "robe", "robes", "leather", "shield"]):
            item["equipment_kind"] = "shield" if "shield" in lower else "armor"
        elif any(k in lower for k in ["sword", "axe", "staff", "bow", "dagger", "mace", "hammer", "crossbow", "pistol", "cutlass", "club", "spear", "javelin", "wand"]):
            item["equipment_kind"] = "weapon"
        else:
            item["equipment_kind"] = "gear"
        entries.append(item)
    return entries


def _sync_player_state_from_profile(session, user, profile: dict) -> None:
    if not isinstance(profile, dict):
        return
    profile_id = str(profile.get("id") or "").strip()
    if profile_id:
        active = dict(getattr(session, "active_char_profiles", {}) or {})
        active[user.id] = profile_id
        session.active_char_profiles = active

    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    equipment = native.get("equipment") if isinstance(native.get("equipment"), dict) else {}
    currency = equipment.get("currency") if isinstance(equipment.get("currency"), dict) else {}
    gp = int(currency.get("gp") or 0)
    if gp or str(user.id):
        set_player_gold_for_user(session, user.id, gp)

    from server.handlers.inventory import _get_player_inventory_store, _normalize_player_inventory_entry  # local import avoids circulars

    entries = _equipment_entries_from_profile(profile)
    inventories, owner_key, mine = _get_player_inventory_store(session, user)
    if entries:
        normalized = [_normalize_player_inventory_entry(e) for e in entries]
        inventories[owner_key] = [e for e in normalized if e]
        session.player_inventories = inventories

    tokens = [t for t in session.tokens.values() if str(getattr(t, 'owner_id', '') or '') == str(user.id)]
    if not tokens:
        return
    keep = tokens[0]
    for extra in tokens[1:]:
        session.tokens.pop(extra.id, None)
    keep.profile_id = str(profile.get("id") or getattr(keep, "profile_id", "") or "")[:120]
    keep.library_id = str(profile.get("libraryId") or profile.get("library_id") or keep.profile_id or getattr(keep, "library_id", "") or "")[:120]
    native_identity = ((profile.get("nativeCharacter") or {}).get("identity") if isinstance(profile.get("nativeCharacter"), dict) else {}) or {}
    keep.character_id = str(profile.get("characterId") or profile.get("character_id") or native_identity.get("characterId") or native_identity.get("id") or getattr(keep, "character_id", "") or "")[:120]
    keep.name = str(profile.get("name") or keep.name or user.name)[:80]
    sheet = profile.get("charSheet") if isinstance(profile.get("charSheet"), dict) else {}
    book = profile.get("charBook") if isinstance(profile.get("charBook"), dict) else {}
    runtime = profile.get("nativeRuntime") if isinstance(profile.get("nativeRuntime"), dict) else {}
    runtime_hp = runtime.get("hp") if isinstance(runtime.get("hp"), dict) else {}
    sheet_hp = sheet.get("hp") if isinstance(sheet.get("hp"), dict) else {}
    max_hp = runtime_hp.get("max")
    cur_hp = runtime_hp.get("current")
    temp_hp = runtime_hp.get("temp")
    if max_hp is None:
        max_hp = book.get("maxHp")
    if cur_hp is None:
        cur_hp = book.get("currentHp")
    if temp_hp is None:
        temp_hp = book.get("tempHp")
    if max_hp is None:
        max_hp = sheet_hp.get("max")
    if cur_hp is None:
        cur_hp = sheet_hp.get("current")
    if temp_hp is None:
        temp_hp = sheet_hp.get("temp")
    # The sheet/profile is authoritative for max HP and derived stats; the
    # live token is authoritative for current/temp HP — claiming or rejoining
    # must never undo damage dealt during the session. Current HP is seeded
    # from the profile only when the token has no live HP yet.
    try:
        if max_hp is not None:
            keep.max_hp = max(1, int(max_hp))
    except Exception:
        pass
    if getattr(keep, "hp", None) is None:
        try:
            if cur_hp is not None:
                keep.hp = max(0, int(cur_hp))
        except Exception:
            pass
        try:
            if temp_hp is not None:
                keep.temp_hp = max(0, int(temp_hp))
        except Exception:
            pass
    # A sheet edit may have lowered max HP below the live current HP — clamp.
    try:
        if keep.hp is not None and keep.max_hp is not None and keep.hp > keep.max_hp:
            keep.hp = keep.max_hp
    except Exception:
        pass
    token_display = (sheet.get("tokenDisplay") if isinstance(sheet.get("tokenDisplay"), dict) else {}) or (book.get("tokenDisplay") if isinstance(book.get("tokenDisplay"), dict) else {})
    accent = str(token_display.get("accentColor") or keep.color or "#6f5936")[:32]
    if accent:
        keep.color = accent
    token_image = str(sheet.get("tokenImageUrl") or book.get("tokenImageUrl") or "").strip()
    if token_image:
        keep.image_url = token_image[:300]
    level = sheet.get("totalLevel") or profile.get("level") or 1
    try:
        keep.level = int(level)
    except Exception:
        pass
    ac = sheet.get("ac")
    try:
        if ac is not None:
            keep.ac = int(ac)
    except Exception:
        pass
    speed = sheet.get("speed") if isinstance(sheet.get("speed"), dict) else None
    try:
        if isinstance(speed, dict):
            keep.speed = int(speed.get("walk") or keep.speed or 0)
        elif book.get("speed") is not None:
            keep.speed = int(book.get("speed") or keep.speed or 0)
    except Exception:
        pass

async def create_session_response(request, body: dict):
    auth_user = get_request_user(request)
    dm_name = str(body.get("dm_name", "Dungeon Master")).strip()[:40] or "Dungeon Master"
    session, dm = create_session(dm_name)
    session.name = str(body.get("campaign_name", "My Campaign")).strip()[:60] or "My Campaign"
    if auth_user:
        dm.player_key = auth_player_key(auth_user["id"])
    await save_campaign_async(session)
    return JSONResponse({
        "session_id": session.id,
        "user_id": dm.id,
        "role": "dm",
        "player_invite": session.player_invite,
        "viewer_invite": session.viewer_invite,
        "name": dm.name,
        "campaign_name": session.name,
    })


async def join_session_response(request, body: dict):
    auth_user = get_request_user(request)
    if not auth_user:
        return JSONResponse(
            {"error": "Authentication required. Please log in before joining a session."},
            status_code=401,
        )
    session_id = str(body.get("session_id", "")).strip().upper()
    invite_code = str(body.get("invite_code", "")).strip()
    claim_token = body.get("claim_token")
    user_name = auth_display_name(auth_user)
    player_key = auth_player_key(auth_user["id"])
    get_or_restore_session(session_id)
    session, user, error = join_session(session_id, invite_code, user_name, player_key)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    profile_id = str(body.get("profile_id") or body.get("profileId") or "").strip()
    if profile_id and getattr(user, "role", "") == "player":
        profile = _find_profile_for_auth_user(session, auth_user, profile_id)
        if profile:
            _sync_player_state_from_profile(session, user, profile)
    if claim_token:
        tok = session.tokens.get(claim_token)
        if not tok:
            return JSONResponse({"error": "Character not found."}, status_code=400)
        if user.role != "player":
            return JSONResponse({"error": "Only players can select a character."}, status_code=403)
        if tok.owner_id != user.id:
            return JSONResponse({"error": "You can only select characters linked to your account."}, status_code=403)
    owned_tokens = [t.id for t in session.tokens.values() if t.owner_id == user.id]
    await save_campaign_async(session)
    return JSONResponse({
        "session_id": session.id,
        "user_id": user.id,
        "role": user.role,
        "name": user.name,
        "owned_tokens": owned_tokens,
        "returning": len(owned_tokens) > 0,
    })


def session_invites_response(session_id: str, user_id: str = ""):
    session = get_session(session_id)
    if not session:
        return JSONResponse({"error": "Not found"}, status_code=404)
    user = session.users.get(user_id)
    if not user or user.role != "dm":
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return JSONResponse({
        "player_invite": session.player_invite,
        "viewer_invite": session.viewer_invite,
        "session_id": session_id,
    })


def session_fog_debug_response(session_id: str, user_id: str = ""):
    """DM-only diagnostic summary of persisted fog maps.

    Mirrors the client-side ``window.__debugFog()`` output so a DM can confirm
    that revealed/hidden state survived a restart and that map-context keys line
    up between the saved fog maps and the active map.
    """
    session = get_or_restore_session(session_id)
    if not session:
        return JSONResponse({"error": "Not found"}, status_code=404)
    uid = str(user_id or "").strip()
    user = session.users.get(uid) if uid else None
    if not user or str(getattr(user, "role", "") or "").strip().lower() != "dm":
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    fog_maps = normalize_fog_maps(getattr(session, "fog_maps", None) or {})
    maps_summary = {}
    for ctx, entry in fog_maps.items():
        cells = str(entry.get("cells") or "")
        maps_summary[ctx] = {
            "map_context": entry.get("map_context", ctx),
            "enabled": bool(entry.get("enabled", False)),
            "cols": int(entry.get("cols") or 0),
            "rows": int(entry.get("rows") or 0),
            "cells_length": len(cells),
            "revealed_count": cells.count("1"),
            "revision": int(entry.get("revision") or 0),
            "updated_at": float(entry.get("updated_at") or 0.0),
        }
    return JSONResponse({
        "session_id": session_id,
        "active_map_context": normalize_map_context(getattr(session, "dm_map_context", "world")),
        "keys": list(maps_summary.keys()),
        "fog_maps": maps_summary,
    })


def session_info_response(session_id: str):
    session = get_session(session_id)
    if not session:
        return JSONResponse({"error": "Not found"}, status_code=404)
    users = [
        {"id": u.id, "name": u.name, "role": u.role, "connected": u.connected}
        for u in session.users.values()
    ]
    return JSONResponse({"session_id": session_id, "users": users})


def lobby_response(request, session_id: str, role: str = "", player_key: str = "", user_name: str = ""):
    session = get_or_restore_session(session_id)
    if not session:
        return JSONResponse({"error": "Not found"}, status_code=404)
    auth_user = get_request_user(request)
    if auth_user:
        player_key = auth_player_key(auth_user["id"])
        user_name = auth_display_name(auth_user, fallback=user_name)
    else:
        player_key = (player_key or "").strip()[:64]
        user_name = (user_name or "").strip()[:40]
    role = (role or "").strip().lower()
    tokens = []
    if role == "player":
        matched_user = None
        if player_key:
            matched_user = next(
                (u for u in session.users.values() if u.role == "player" and getattr(u, "player_key", "") == player_key),
                None,
            )
        if not matched_user and user_name:
            matched_user = next(
                (u for u in session.users.values() if u.role == "player" and u.name.lower() == user_name.lower()),
                None,
            )
        if matched_user:
            for t in session.tokens.values():
                if t.owner_id != matched_user.id:
                    continue
                row = build_token_runtime_payload(session, t)
                row["owner_name"] = matched_user.name
                row["class_summary"] = row.get("classSummary", row.get("class_summary", ""))
                tokens.append(row)
    elif role == "dm":
        authority = resolve_session_authority(request, session, fallback_user_id="")
        if authority.get("is_session_dm"):
            for t in session.tokens.values():
                owner_name = session.users[t.owner_id].name if t.owner_id and t.owner_id in session.users else ""
                row = build_token_runtime_payload(session, t)
                row["owner_name"] = owner_name
                row["class_summary"] = row.get("classSummary", row.get("class_summary", ""))
                tokens.append(row)
    return JSONResponse({
        "session_id": session_id,
        "campaign_name": getattr(session, "name", "Campaign"),
        "tokens": tokens,
    })


async def upload_token_image_response(session_id: str, token_id: str, user_id: str, file, maps_dir):
    session = get_or_restore_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    user = session.users.get(user_id)
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)
    token = session.tokens.get(token_id)
    if not token:
        return JSONResponse({"error": "Token not found"}, status_code=404)
    if user.role != "dm" and getattr(token, "owner_id", None) != user_id:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not (file.content_type or "").startswith("image/"):
        return JSONResponse({"error": "File must be an image"}, status_code=400)
    maps_dir.mkdir(parents=True, exist_ok=True)
    for old_f in maps_dir.glob(f"{session_id}_token_{token_id}.*"):
        try:
            old_f.unlink()
        except Exception:
            pass
    raw = await file.read()
    filename_holder = {"name": f"{session_id}_token_{token_id}.png"}

    def _save_token_image():
        dest = maps_dir / filename_holder["name"]
        try:
            from PIL import Image
            import io as _io

            img = Image.open(_io.BytesIO(raw))
            if img.mode not in ("RGBA", "LA"):
                img = img.convert("RGBA")
            if img.width > 1024 or img.height > 1024:
                img.thumbnail((1024, 1024), Image.LANCZOS)
            filename_holder["name"] = f"{session_id}_token_{token_id}.webp"
            dest = maps_dir / filename_holder["name"]
            img.save(dest, "WEBP", quality=92, method=6)
        except Exception:
            with open(dest, 'wb') as fh:
                fh.write(raw)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _save_token_image)
    url = f"/static/maps/{filename_holder['name']}?v={secrets.token_hex(4)}"
    token.image_url = url
    await save_campaign_async(session)
    for uid, u in session.users.items():
        if getattr(u, "role", None) == "dm" or not getattr(token, "hidden", False):
            await manager.send_to(session_id, uid, {
                "type": "token_image_updated",
                "payload": {"token_id": token.id, "image_url": url}
            })
    return JSONResponse({"ok": True, "url": url})


async def delete_session_token_response(request, session_id: str, token_id: str):
    """Allow an authenticated player to delete one of their own session tokens via HTTP."""
    auth_user = get_request_user(request)
    if not auth_user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    session = get_or_restore_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    token = session.tokens.get(token_id)
    if not token:
        return JSONResponse({"error": "Token not found"}, status_code=404)

    player_key = auth_player_key(auth_user["id"])
    is_dm = resolve_session_authority(request, session, fallback_user_id="").get("is_session_dm")
    token_owner_id = str(getattr(token, "owner_id", "") or "").strip()
    owner_matches = any(
        str(getattr(u, "id", "") or "").strip() == token_owner_id
        and str(getattr(u, "player_key", "") or "").strip() == player_key
        for u in session.users.values()
    )

    if not is_dm and not owner_matches:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    session.tokens.pop(token_id, None)
    corpse_states = dict(getattr(session, "corpse_states", {}) or {})
    corpse_states.pop(str(token_id), None)
    session.corpse_states = corpse_states
    # A deleted token must also leave any active initiative order so it doesn't
    # linger as a ghost combatant. Local import avoids a circular dependency.
    from server.handlers.combat import remove_token_from_combat, _bump_combat_revision
    from server.handlers.common import _broadcast_combat
    if remove_token_from_combat(session, str(token_id)):
        _bump_combat_revision(session, "token_deleted")
        await _broadcast_combat(session)
    await save_campaign_async(session)
    return JSONResponse({"ok": True, "token_id": token_id})


def _backfill_dm_player_key_if_needed(request, session, fallback_user_id: str) -> bool:
    """Set the DM's player_key when missing — one-time migration for sessions created before auth.

    Returns True if a backfill was performed (caller should persist the session).
    Only backfills when the fallback_user_id matches the DM slot, the DM has no key yet,
    and the authenticated user's key is not already linked to another participant.
    """
    if not fallback_user_id:
        return False
    dm_id = str(getattr(session, 'dm_id', '') or '').strip()
    if not dm_id or fallback_user_id != dm_id:
        return False
    dm_user = session.users.get(dm_id)
    if not dm_user:
        return False
    if str(getattr(dm_user, 'player_key', '') or '').strip():
        return False
    auth_user = get_request_user(request)
    if not auth_user:
        return False
    auth_role = str((auth_user or {}).get("role") or "").strip().lower()
    if auth_role not in {"dm", "assistant_dm"}:
        return False
    auth_pk = auth_player_key(str(auth_user.get('id') or '').strip())
    if not auth_pk:
        return False
    # Legacy recovery note:
    # Some sessions ended up with the DM account also joined as a player, which
    # means the auth player_key may already exist on a non-DM participant.
    # In that case we still backfill the DM slot so authority resolution can
    # correctly treat the authenticated DM as session DM.
    dm_user.player_key = auth_pk
    import logging as _logging
    _logging.getLogger(__name__).info(
        '[Authority] backfilled DM player_key for session %s user %s', session.id, dm_id
    )
    return True


async def session_authority_response(request, session_id: str, fallback_user_id: str = ""):
    session = get_or_restore_session(session_id)
    if not session:
        return JSONResponse({"error": "Not found"}, status_code=404)
    backfilled = _backfill_dm_player_key_if_needed(request, session, fallback_user_id)
    dm_id = str(getattr(session, "dm_id", "") or "").strip()
    if not backfilled and dm_id and fallback_user_id != dm_id:
        backfilled = _backfill_dm_player_key_if_needed(request, session, dm_id)
    if backfilled:
        await save_campaign_async(session)
    authority = resolve_session_authority(request, session, fallback_user_id=fallback_user_id)
    resolved_role = "dm" if authority.get("is_session_dm") else (authority.get("participant_role") or "viewer")
    return JSONResponse({
        "session_id": session_id,
        "resolved_user_id": authority.get("resolved_user_id"),
        "resolved_session_user_id": authority.get("resolved_session_user_id"),
        "session_dm_id": authority.get("session_dm_id"),
        "participant_role": authority.get("participant_role"),
        "is_session_dm": bool(authority.get("is_session_dm")),
        "matched_via": authority.get("matched_via"),
        "resolved_role": resolved_role,
    })
