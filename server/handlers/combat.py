"""
server/handlers/combat.py — Combat state management helpers and handlers.
"""
import logging
import time as _time
import uuid
from server.session import normalize_profile_owner_key, assistant_dm_has_scope, owner_id_matches_user
from server.quest_progress import apply_objective_event, normalize_quest_payload_shape
from server.encumbrance import ENC_HEAVY

from server.handlers.common import (
    Session, User, manager,
    save_campaign_async,
    PX_PER_GRID, FT_PER_GRID,
    _safe_int,
    _broadcast_combat,
    _combat_state_payload_for_user,
    bump_visibility_revision,
    is_player_owned_token,
    is_npc_or_monster_token,
    _token_map_context,
    is_token_touching_unrevealed_fog,
    _send_action_ack,
    build_live_state_debug_summary,
    _broadcast_token_event,
    _persist_token_hp_to_owned_profiles,
)
from server.movement import resolve_movement, normalize_movement_mode
from server.handlers.durability import mark_session_dirty

logger = logging.getLogger(__name__)


def _bump_combat_revision(session: Session, reason: str) -> None:
    """Monotonically increment combat.revision so clients can detect stale combat_state payloads."""
    combat = session.combat or {}
    combat["revision"] = _safe_int(combat.get("revision"), 0, minimum=0, maximum=2**31) + 1
    combat["updated_at"] = _time.time()
    combat["last_update_reason"] = str(reason or "")[:80]
    session.combat = combat


def _can_manage_combat(session: Session, user: User, *, full: bool = False) -> bool:
    if user.role == "dm":
        return True
    return assistant_dm_has_scope(session, user, "combat.manage_limited") and not full



def _owner_matches_user(owner_id, user: User) -> bool:
    """Ownership check for combatants/tokens — the single canonical resolver.

    Matches when ``owner_id`` is the player's user_id, display-name bucket, OR
    resolved player_key (``auth_<id>``). Previously this only checked user_id /
    display name, so a combatant whose owner_id was the player's player_key was
    rejected ("You may only roll initiative for your own character.") even
    though the player legitimately owns it.
    """
    return owner_id_matches_user(owner_id, None, user)


def _has_encumbrance_attack_disadvantage(session: Session, attacker_owner_id: str) -> bool:
    """True when the attacking player is heavily encumbered."""
    owner_id = str(attacker_owner_id or "").strip()
    if not owner_id:
        return False
    cache = getattr(session, "_encumbrance_cache", {}) or {}
    entry = cache.get(owner_id)
    if not isinstance(entry, dict):
        return False
    return str(entry.get("state") or "").strip().lower() == ENC_HEAVY



def _combat_fog_mode(session: Session) -> str:
    settings = getattr(session, "settings", None) or {}
    combat_settings = settings.get("combat") if isinstance(settings.get("combat"), dict) else {}
    mode = combat_settings.get("auto_sync_npcs_with_fog", settings.get("combat_auto_sync_npcs_with_fog", "auto"))
    mode = str(mode or "auto").strip().lower().replace("_", "-")
    if mode in {"off", "false", "disabled", "none"}:
        return "off"
    if mode in {"suggest", "suggest-only", "suggest_only"}:
        return "suggest"
    return "auto"


def _current_combat_map_context(session: Session, reason: str | None = None) -> str:
    return str(getattr(session, "dm_map_context", "world") or "world")[:80] or "world"


def is_token_visible_to_party(session: Session, token, map_context: str | None = None, *, ignore_hidden: bool = False, ignore_staged: bool = False) -> bool:
    if token is None:
        return False
    if bool(getattr(token, "hidden", False)) and not ignore_hidden:
        return False
    if bool(getattr(token, "staged", False)) and not ignore_staged:
        return False
    if is_token_touching_unrevealed_fog(session, token, map_context):
        return False
    from server.visibility import token_blocked_by_los
    return not token_blocked_by_los(session, token, map_context)


def _ensure_suspended_lists(combat: dict) -> list[dict]:
    shared = combat.get("suspended_combatants")
    if not isinstance(shared, list):
        shared = []
    for key in ("fog_suspended_combatants", "hidden_suspended_combatants"):
        for item in combat.get(key) or []:
            if isinstance(item, dict) and not any(str(x.get("token_id") or "") == str(item.get("token_id") or "") for x in shared):
                shared.append(item)
        combat[key] = [x for x in shared if key.startswith("fog") and "fog" in (x.get("suspended_reasons") or []) or key.startswith("hidden") and "hidden" in (x.get("suspended_reasons") or [])]
    combat["suspended_combatants"] = shared
    return shared


def _suspend_reasons(session: Session, token, map_context: str | None = None) -> list[str]:
    reasons=[]
    if bool(getattr(token, "hidden", False)): reasons.append("hidden")
    if bool(getattr(token, "staged", False)): reasons.append("staged")
    if is_token_touching_unrevealed_fog(session, token, map_context):
        reasons.append("fog")
    else:
        from server.visibility import token_blocked_by_los
        if token_blocked_by_los(session, token, map_context):
            reasons.append("los")
    return reasons


def _merge_suspended(existing: dict | None, combatant: dict, reasons: list[str]) -> dict:
    merged = dict(existing or {})
    merged.update({k:v for k,v in dict(combatant or {}).items() if v is not None or k not in merged})
    merged["suspended_reasons"] = sorted(set((merged.get("suspended_reasons") or []) + list(reasons)))
    merged["suspended_at"] = _time.time()
    return merged


def _adjust_turn_after_removal(combat: dict, remove_idx: int, removed_id: str, old_turn: int) -> None:
    coms = combat.get("combatants") or []
    if not coms:
        combat["turn"] = 0; combat["movement"] = {}; return
    if remove_idx == old_turn:
        combat["turn"] = min(remove_idx, len(coms)-1); combat["movement"] = {}
    else:
        combat["turn"] = max(0, old_turn-1) if remove_idx < old_turn else min(old_turn, len(coms)-1)


def sync_combat_visibility(session: Session, map_context: str | None = None, reason: str = "sync") -> dict:
    combat = getattr(session, "combat", None) or {}
    if not combat.get("active") or _combat_fog_mode(session) == "off":
        return {"changed": False, "added": [], "removed": []}
    # A read-only state snapshot (DM reconnect, new joiner, authoritative
    # snapshot rebuild) must never mutate the shared combat roster. It runs with
    # the requester's transient ``dm_map_context`` and writing the suspend/
    # restore/auto-add result back would drop or re-add real combatants for
    # every client based on that one reader's fog/context. Per-user visibility
    # is already applied separately by ``_combat_state_payload_for_user`` at
    # send time, so the snapshot can safely carry the authoritative roster.
    if str(reason or "") == "state_snapshot":
        return {"changed": False, "added": [], "removed": []}
    map_ctx = str(map_context or _current_combat_map_context(session, reason) or "world")[:80] or "world"
    coms = combat.get("combatants") if isinstance(combat.get("combatants"), list) else []
    suspended = _ensure_suspended_lists(combat)
    suspended_by_token = {str((c or {}).get("token_id") or ""): c for c in suspended if isinstance(c, dict)}
    active_ids = {str((c or {}).get("token_id") or "") for c in coms}
    changed=False; added=[]; removed=[]
    old_turn = _safe_int(combat.get("turn"), 0, minimum=0, maximum=max(0, len(coms)-1))
    for idx in range(len(coms)-1, -1, -1):
        c=coms[idx] or {}; tid=str(c.get("token_id") or "")
        token=session.tokens.get(tid) if tid else None
        # A combatant bound to a token that no longer exists (deleted mid-combat)
        # is a ghost entry — drop it from the roster entirely, on any map. Not a
        # "suspension" (which can be restored), so it is not added to ``removed``.
        if tid and token is None:
            coms.pop(idx); active_ids.discard(tid); suspended_by_token.pop(tid, None)
            changed=True
            _adjust_turn_after_removal(combat, idx, str(c.get("id") or ""), old_turn)
            continue
        if not token or _token_map_context(token) != map_ctx or not is_npc_or_monster_token(token):
            continue
        # Always judge fog/LOS against the token's OWN map context, never the
        # requester's transient panel context, so a combatant is only suspended
        # when it genuinely sits in unrevealed fog on its own map.
        reasons=_suspend_reasons(session, token, _token_map_context(token))
        reasons=[r for r in reasons if r in {"fog","hidden","staged","los"}]
        if reasons:
            removed_c=coms.pop(idx); active_ids.discard(tid)
            suspended_by_token[tid]=_merge_suspended(suspended_by_token.get(tid), removed_c, reasons)
            removed.append(suspended_by_token[tid]); changed=True
            _adjust_turn_after_removal(combat, idx, str(removed_c.get("id") or ""), old_turn)
    # restore suspended whose blockers cleared
    for tid, saved in list(suspended_by_token.items()):
        token=session.tokens.get(tid) if tid else None
        if not token or _token_map_context(token) != map_ctx or not is_npc_or_monster_token(token):
            continue
        reasons=_suspend_reasons(session, token, _token_map_context(token))
        if not reasons and tid not in active_ids:
            restored=dict(saved); restored.pop("suspended_reasons", None); restored.pop("suspended_at", None)
            coms.append(restored); active_ids.add(tid); added.append(restored); changed=True
            suspended_by_token.pop(tid, None)
        else:
            saved["suspended_reasons"] = sorted(set(reasons))
    # auto-add visible NPCs not seen before
    for token in list((getattr(session, "tokens", {}) or {}).values()):
        tid=str(getattr(token,"id","") or "")
        if not tid or tid in active_ids or tid in suspended_by_token or _token_map_context(token) != map_ctx:
            continue
        if is_npc_or_monster_token(token) and not bool(getattr(token,"hidden",False)) and not bool(getattr(token,"staged",False)) and is_token_visible_to_party(session, token, map_ctx):
            c=_combatant_from_token(session, token)
            coms.append(c); active_ids.add(tid); added.append(c); changed=True
    combat["combatants"] = coms
    combat["suspended_combatants"] = list(suspended_by_token.values())
    combat["fog_suspended_combatants"] = [c for c in combat["suspended_combatants"] if "fog" in (c.get("suspended_reasons") or [])]
    combat["hidden_suspended_combatants"] = [c for c in combat["suspended_combatants"] if "hidden" in (c.get("suspended_reasons") or [])]
    if changed:
        _sort_combatants_preserving_turn(combat)
        combat["turn"] = _safe_int(combat.get("turn"), 0, minimum=0, maximum=max(0, len(coms)-1))
        session.combat=combat
        _ensure_combat_movement_state(session, reset=True)
        for c in removed:
            name=c.get("name") or "A creature"; rs=", ".join(c.get("suspended_reasons") or ["fog"])
            session.add_log(f"{name} was suspended from visible initiative ({rs}).", "combat", "System")
        for c in added:
            session.add_log(f"{c.get('name') or 'A creature'} rejoined visible initiative.", "combat", "System")
    return {"changed": changed, "added": added, "removed": removed}


def sync_fogged_combatants(session: Session, reason: str = "sync", map_context: str | None = None) -> dict:
    return sync_combat_visibility(session, map_context=map_context, reason=reason)


def sync_active_player_tokens(session: Session, map_context: str | None = None, reason: str = "sync") -> dict:
    """Auto-add visible player-owned tokens to the active initiative order.

    NPC/monster tokens that appear mid-combat are folded into initiative by
    ``sync_combat_visibility`` (the fog-reveal path). Player tokens are not:
    they are never fog-suspended, and that function is gated on the NPC fog
    setting and can early-return entirely. So a player who joins — or whose
    token is placed on the encounter map — after combat has begun never shows
    up in the order, even though every other token does. This runs alongside
    the fog sync, independent of the NPC fog setting, so a late-arriving
    player token gets the same automatic entry as everything else on the map.

    The combatant is added with ``initiative=None`` so the DM/player rolls for
    it exactly like the encounter's other fresh entries.
    """
    combat = getattr(session, "combat", None) or {}
    if not combat.get("active"):
        return {"changed": False, "added": []}
    # A read-only snapshot (DM reconnect, new-joiner state rebuild) must never
    # mutate the shared roster — mirrors the guard in sync_combat_visibility.
    if str(reason or "") == "state_snapshot":
        return {"changed": False, "added": []}
    map_ctx = str(map_context or _current_combat_map_context(session, reason) or "world")[:80] or "world"
    coms = combat.get("combatants") if isinstance(combat.get("combatants"), list) else []
    suspended = combat.get("suspended_combatants") if isinstance(combat.get("suspended_combatants"), list) else []
    known_ids = {str((c or {}).get("token_id") or "") for c in coms}
    known_ids |= {str((c or {}).get("token_id") or "") for c in suspended if isinstance(c, dict)}
    added: list[dict] = []
    for token in list((getattr(session, "tokens", {}) or {}).values()):
        tid = str(getattr(token, "id", "") or "")
        if not tid or tid in known_ids:
            continue
        if _token_map_context(token) != map_ctx or not is_player_owned_token(token):
            continue
        # Hidden/staged player tokens are deliberately off the board; everything
        # else present on the encounter map belongs in the order.
        if bool(getattr(token, "hidden", False)) or bool(getattr(token, "staged", False)):
            continue
        combatant = _combatant_from_token(session, token)
        coms.append(combatant); known_ids.add(tid); added.append(combatant)
    if not added:
        return {"changed": False, "added": []}
    combat["combatants"] = coms
    _sort_combatants_preserving_turn(combat)
    combat["turn"] = _safe_int(combat.get("turn"), 0, minimum=0, maximum=max(0, len(coms) - 1))
    session.combat = combat
    _sync_action_economy_roster(session)
    _ensure_combat_movement_state(session, reset=False)
    for c in added:
        session.add_log(f"{c.get('name') or 'A character'} joined the initiative order.", "combat", "System")
    return {"changed": True, "added": added}


async def run_combat_fog_sync(session: Session, reason: str = "sync", map_context: str | None = None) -> dict:
    result = sync_fogged_combatants(session, reason=reason, map_context=map_context)
    # Late-arriving player tokens enter initiative the same way other tokens do.
    player_result = sync_active_player_tokens(session, reason=reason, map_context=map_context)
    if result.get("changed") or player_result.get("changed"):
        _bump_combat_revision(session, reason or "fog_sync")
        await save_campaign_async(session)
        await _broadcast_combat(session)
    return result


COMBAT_FOG_SYNC_REQUEST_COOLDOWNS = {
    "assistant_dm": 1.0,
    "player": 2.5,
}


def _combat_fog_sync_cooldown_for_user(user: User) -> float | None:
    role = str(getattr(user, "role", "") or "").strip().lower()
    if role == "dm":
        return 0.0
    if role in COMBAT_FOG_SYNC_REQUEST_COOLDOWNS:
        return COMBAT_FOG_SYNC_REQUEST_COOLDOWNS[role]
    return None


def _is_combat_fog_sync_request_rate_limited(session: Session, user: User, now: float | None = None) -> bool:
    cooldown = _combat_fog_sync_cooldown_for_user(user)
    if cooldown is None:
        return True
    if cooldown <= 0:
        return False
    uid = str(getattr(user, "id", "") or "").strip()
    if not uid:
        return True
    now = float(_time.time() if now is None else now)
    last_by_user = getattr(session, "_combat_fog_sync_request_last_at", None)
    if not isinstance(last_by_user, dict):
        last_by_user = {}
        setattr(session, "_combat_fog_sync_request_last_at", last_by_user)
    last_at = float(last_by_user.get(uid) or 0.0)
    if last_at and now - last_at < cooldown:
        return True
    last_by_user[uid] = now
    return False


async def handle_combat_fog_sync_request(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "assistant_dm", "player"}:
        return
    if user.role == "assistant_dm" and not assistant_dm_has_scope(session, user, "combat.manage_limited"):
        return
    if _is_combat_fog_sync_request_rate_limited(session, user):
        return
    payload = payload if isinstance(payload, dict) else {}
    map_context = payload.get("map_context") or payload.get("map_ctx") or getattr(session, "dm_map_context", None) or "world"
    reason = str(payload.get("reason") or "request")[:80]
    await run_combat_fog_sync(session, reason=reason, map_context=map_context)

def _combatant_token_ids(session: Session) -> set[str]:
    combat = getattr(session, "combat", None) or {}
    combatants = combat.get("combatants") or []
    ids = set()
    for combatant in combatants:
        token_id = str((combatant or {}).get("token_id") or "").strip()
        if token_id:
            ids.add(token_id)
    return ids


def _get_current_combatant(session: Session) -> dict | None:
    combat = getattr(session, "combat", None) or {}
    combatants = combat.get("combatants") or []
    if not combat.get("active") or not combatants:
        return None
    try:
        turn = int(combat.get("turn", 0) or 0)
    except Exception:
        turn = 0
    if turn < 0 or turn >= len(combatants):
        return None
    current = combatants[turn] or {}
    return current if isinstance(current, dict) else None


ACTION_ECONOMY_TYPES = {"action", "bonus_action", "reaction", "ready_action", "free", "special"}

def _combat_action_economy(session: Session) -> dict:
    combat = getattr(session, "combat", None) or {}
    if not isinstance(combat, dict):
        combat = {"active": False, "turn": 0, "combatants": []}
        session.combat = combat
    econ = combat.get("combat_action_economy")
    if not isinstance(econ, dict):
        econ = {}
    combat["combat_action_economy"] = econ
    session.combat = combat
    return econ

def _default_action_economy_entry(token_id: str, combatant_id: str = "") -> dict:
    return {
        "token_id": str(token_id or ""),
        "combatant_id": str(combatant_id or ""),
        "action_used": False,
        "bonus_action_used": False,
        "reaction_used": False,
        "ready_action": None,
        "movement_spent": 0.0,
        "free_notes": [],
        "special_notes": [],
        "updated_at": 0.0,
    }

def _get_action_economy_entry(session: Session, token_id: str, combatant: dict | None = None) -> dict:
    token_id = str(token_id or "").strip()
    econ = _combat_action_economy(session)
    entry = econ.get(token_id) if token_id else None
    if not isinstance(entry, dict):
        entry = _default_action_economy_entry(token_id, str((combatant or {}).get("id") or ""))
    entry.setdefault("token_id", token_id)
    entry.setdefault("combatant_id", str((combatant or {}).get("id") or ""))
    entry.setdefault("action_used", False)
    entry.setdefault("bonus_action_used", False)
    entry.setdefault("reaction_used", False)
    entry.setdefault("ready_action", None)
    entry.setdefault("movement_spent", 0.0)
    entry.setdefault("free_notes", [])
    entry.setdefault("special_notes", [])
    if token_id:
        econ[token_id] = entry
    return entry

def _sync_action_economy_roster(session: Session) -> dict:
    combat = getattr(session, "combat", None) or {}
    econ = _combat_action_economy(session)
    active_ids = set()
    for c in combat.get("combatants") or []:
        if not isinstance(c, dict):
            continue
        tid = str(c.get("token_id") or "").strip()
        if not tid:
            continue
        active_ids.add(tid)
        _get_action_economy_entry(session, tid, c)
    for tid in list(econ.keys()):
        if tid not in active_ids:
            econ.pop(tid, None)
    return econ

def _reset_action_economy_for_turn_start(session: Session) -> None:
    current = _get_current_combatant(session)
    token_id = str((current or {}).get("token_id") or "").strip()
    if not token_id:
        return
    entry = _get_action_economy_entry(session, token_id, current)
    entry["action_used"] = False
    entry["bonus_action_used"] = False
    entry["reaction_used"] = False
    entry["ready_action"] = None
    entry["updated_at"] = _time.time()

def _action_economy_usage_field(action_type: str) -> str | None:
    if action_type in {"action", "item_action", "spell", "weapon_attack"}:
        return "action_used"
    if action_type == "bonus_action":
        return "bonus_action_used"
    if action_type == "reaction":
        return "reaction_used"
    if action_type == "ready_action":
        return "action_used"
    return None

def _action_economy_token_for_payload(session: Session, payload: dict) -> tuple[str, dict | None]:
    token_id = str(payload.get("token_id") or "").strip()
    current = _get_current_combatant(session)
    if not token_id:
        token_id = str((current or {}).get("token_id") or "").strip()
    combatant = current if current and str(current.get("token_id") or "") == token_id else None
    if combatant is None:
        for c in (getattr(session, "combat", {}) or {}).get("combatants") or []:
            if isinstance(c, dict) and str(c.get("token_id") or "") == token_id:
                combatant = c; break
    return token_id, combatant

async def _deny_action_economy(session: Session, user: User, reason: str, *, client_action_id=None):
    await _send_action_ack(session, user, action="combat_action_economy_use", client_action_id=client_action_id, status="denied", reason=reason)
    await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": reason}})

def _user_can_spend_for_token(session: Session, user: User, token_id: str, combatant: dict | None, *, override: bool = False) -> bool:
    if user.role == "dm":
        return True
    current = _get_current_combatant(session)
    action_type_ok = override is False
    if not action_type_ok or str((current or {}).get("token_id") or "") != str(token_id or ""):
        return False
    token = session.tokens.get(token_id) if token_id else None
    owner_id = str(getattr(token, "owner_id", "") or (combatant or {}).get("owner_id") or "").strip()
    return _owner_matches_user(owner_id, user)

def _mark_action_economy(session: Session, token_id: str, action_type: str, *, note: str = "", override: bool = False, combatant: dict | None = None, bump: bool = True) -> tuple[bool, str, dict]:
    action_type = str(action_type or "action").strip().lower()
    entry = _get_action_economy_entry(session, token_id, combatant)
    field = _action_economy_usage_field(action_type)
    if field and entry.get(field) and not override:
        return False, f"{action_type.replace('_', ' ').title()} already spent.", entry
    if field:
        entry[field] = True
    if action_type == "ready_action":
        entry["ready_action"] = {"note": str(note or "Ready action")[:240], "set_at": _time.time()}
    elif action_type == "free" and note:
        entry.setdefault("free_notes", []).append(str(note)[:240])
    elif action_type == "special" and note:
        entry.setdefault("special_notes", []).append(str(note)[:240])
    entry["updated_at"] = _time.time()
    if bump:
        _bump_combat_revision(session, "action_economy")
    return True, "", entry


def _char_profile_speed(session: Session, owner_id: str) -> int:
    """Return the speed stored in the player's most-recent char profile, or 0."""
    try:
        user = (getattr(session, "users", {}) or {}).get(owner_id)
        if not user:
            return 0
        profiles_all = dict(getattr(session, "char_profiles", {}) or {})
        name_key = normalize_profile_owner_key(getattr(user, "name", ""))
        mine = list(profiles_all.get(name_key) or profiles_all.get(owner_id) or [])
        if not mine:
            return 0
        best = max(mine, key=lambda p: float(p.get("updated_at") or 0.0), default=None)
        if not best:
            return 0
        speed = int(best.get("speed") or 0)
        return max(0, speed)
    except Exception:
        return 0


def _token_speed_ft(session: Session, token, combatant: dict | None = None) -> int:
    def _safe_speed(value) -> int:
        try:
            return max(0, int(value))
        except Exception:
            return 0

    combatant_speed = None
    if isinstance(combatant, dict):
        combatant_speed = combatant.get("speed")
    token_speed = getattr(token, "speed", None) if token is not None else None

    combatant_speed_val = _safe_speed(combatant_speed)
    token_speed_val = _safe_speed(token_speed)

    # Combatants created before a character import/re-login can have stale 0 speed.
    # Prefer the live token speed if combatant speed is missing/zero and token speed is valid.
    base = combatant_speed_val
    if base <= 0 and token_speed_val > 0:
        base = token_speed_val
    # Apply encumbrance speed penalty for player-owned tokens
    owner_id = None
    if token is not None:
        owner_id = str(getattr(token, "owner_id", "") or "").strip()
    if not owner_id and isinstance(combatant, dict):
        owner_id = str((combatant or {}).get("owner_id") or "").strip()
    # Last fallback: read speed from the player's saved character profile
    if base <= 0 and owner_id:
        base = _char_profile_speed(session, owner_id)
    if owner_id:
        cache = getattr(session, "_encumbrance_cache", {}) or {}
        entry = cache.get(owner_id)
        if isinstance(entry, dict):
            penalty = int(entry.get("speed_penalty") or 0)
            if penalty <= -9999:
                return 0
            base = max(0, base + penalty)
    # Viewer-spell conditions: 'slowed' (Ray of Frost) halves movement,
    # 'asleep' (Sleep) prevents it entirely until the timer expires.
    if token is not None:
        from server.handlers.conditions import _prune_token_condition_timers
        _prune_token_condition_timers(token)
        conditions = getattr(token, "conditions", None) or []
        if "asleep" in conditions:
            return 0
        if "slowed" in conditions:
            base = base // 2
    return base


def _movement_mode_for_session(session: Session, requested: str | None = None) -> str:
    if requested:
        return normalize_movement_mode(requested)
    settings = getattr(session, "settings", None) or {}
    combat = getattr(session, "combat", None) or {}
    mode = combat.get("movement_mode") or settings.get("movement_mode") or settings.get("grid_movement_mode")
    return normalize_movement_mode(mode)


def _session_grid_size_px(session: Session) -> float:
    try:
        settings = getattr(session, "settings", None) or {}
        v = float((settings.get("grid") or {}).get("size_px") or PX_PER_GRID)
        return v if v >= 10.0 else PX_PER_GRID
    except Exception:
        return PX_PER_GRID


def _resolve_combat_movement(session: Session, token, move_state: dict, to_x: float, to_y: float, *, path=None, movement_mode: str | None = None, grid_size_px: float | None = None) -> dict:
    px = float(grid_size_px) if grid_size_px and float(grid_size_px) >= 10.0 else _session_grid_size_px(session)
    return resolve_movement(
        from_x=float(move_state.get("last_x", getattr(token, "x", 0.0)) or 0.0),
        from_y=float(move_state.get("last_y", getattr(token, "y", 0.0)) or 0.0),
        to_x=float(to_x),
        to_y=float(to_y),
        token_width=float(getattr(token, "width", px) or px),
        token_height=float(getattr(token, "height", px) or px),
        path=path,
        movement_mode=_movement_mode_for_session(session, movement_mode),
        speed_feet=float(move_state.get("speed_ft", 0.0) or 0.0),
        bonus_feet=float(move_state.get("bonus_ft", 0.0) or 0.0),
        spent_feet=float(move_state.get("spent_ft", 0.0) or 0.0),
        difficult_terrain=bool(move_state.get("difficult_terrain")),
        cost_multiplier=float(move_state.get("cost_multiplier", 1.0) or 1.0),
        grid_size_px=px,
    )


def _ensure_combat_movement_state(session: Session, *, reset: bool = False) -> dict:
    combat = getattr(session, "combat", None) or {}
    if not isinstance(combat, dict):
        combat = {"active": False, "turn": 0, "combatants": []}
        session.combat = combat
    if not combat.get("active"):
        combat["movement"] = {}
        return combat["movement"]
    current = _get_current_combatant(session)
    token_id = str((current or {}).get("token_id") or "").strip()
    if not token_id:
        combat["movement"] = {}
        return combat["movement"]
    token = session.tokens.get(token_id)
    existing = combat.get("movement")
    if not isinstance(existing, dict):
        existing = {}
    if reset or existing.get("token_id") != token_id:
        existing = {
            "token_id": token_id,
            "turn": int(combat.get("turn", 0) or 0),
            "spent_ft": 0.0,
            "speed_ft": _token_speed_ft(session, token, current),
            "remaining_ft": _token_speed_ft(session, token, current),
            "bonus_ft": 0.0,
            "dash_used": False,
            "difficult_terrain": False,
            "disengaged": False,
            "cost_multiplier": 1.0,
            "movement_mode": _movement_mode_for_session(session),
            "last_x": float(getattr(token, "x", 0.0) or 0.0) if token is not None else None,
            "last_y": float(getattr(token, "y", 0.0) or 0.0) if token is not None else None,
        }
    else:
        speed_ft = _token_speed_ft(session, token, current)
        spent_ft = float(existing.get("spent_ft", 0.0) or 0.0)
        bonus_ft = float(existing.get("bonus_ft", 0.0) or 0.0)
        existing["turn"] = int(combat.get("turn", 0) or 0)
        existing["speed_ft"] = speed_ft
        existing["bonus_ft"] = max(0.0, round(bonus_ft, 2))
        existing.setdefault("dash_used", bool(existing.get("bonus_ft")))
        existing.setdefault("difficult_terrain", False)
        existing.setdefault("disengaged", False)
        existing.setdefault("cost_multiplier", 1.0)
        existing["movement_mode"] = _movement_mode_for_session(session, existing.get("movement_mode"))
        total_budget_ft = _movement_total_budget_ft(existing)
        existing["remaining_ft"] = max(0.0, round(total_budget_ft - spent_ft, 2))
        if existing.get("last_x") is None and token is not None:
            existing["last_x"] = float(getattr(token, "x", 0.0) or 0.0)
        if existing.get("last_y") is None and token is not None:
            existing["last_y"] = float(getattr(token, "y", 0.0) or 0.0)
    combat["movement"] = existing
    try:
        _get_action_economy_entry(session, token_id, current)["movement_spent"] = round(float(existing.get("spent_ft", 0.0) or 0.0), 2)
    except Exception:
        pass
    session.combat = combat
    return existing


def _movement_total_budget_ft(move_state: dict | None) -> float:
    if not isinstance(move_state, dict):
        return 0.0
    speed_ft = float(move_state.get("speed_ft", 0.0) or 0.0)
    bonus_ft = float(move_state.get("bonus_ft", 0.0) or 0.0)
    return max(0.0, round(speed_ft + bonus_ft, 2))


def _movement_cost_multiplier(move_state: dict | None) -> float:
    if not isinstance(move_state, dict):
        return 1.0
    if move_state.get("difficult_terrain"):
        return 2.0
    try:
        mult = float(move_state.get("cost_multiplier", 1.0) or 1.0)
    except Exception:
        mult = 1.0
    return max(1.0, mult)


def _can_manage_current_turn_movement(session: Session, user: User) -> tuple[bool, str | None, dict | None, object | None]:
    combat = getattr(session, "combat", None) or {}
    if not combat.get("active"):
        return False, "Combat is not active.", None, None
    current = _get_current_combatant(session)
    token_id = str((current or {}).get("token_id") or "").strip()
    if not token_id:
        return False, "No active combatant.", current, None
    token = session.tokens.get(token_id)
    if user.role == "dm":
        return True, None, current, token
    owner_id = str(getattr(token, "owner_id", "") or (current or {}).get("owner_id") or "").strip()
    if not _owner_matches_user(owner_id, user):
        return False, "Only the active player can use that combat movement action.", current, token
    return True, None, current, token


async def _broadcast_combat_move_state(session: Session):
    move_state = _ensure_combat_movement_state(session)
    # combat_move_state carries the active combatant's token_id/x/y; if that
    # combatant is a hidden/fog-hidden/wall-hidden NPC (DM moving it on its own
    # turn) it must not be broadcast to users who can't see that token.
    from server.handlers.common import _is_token_visible_to_user
    token_id = str((move_state or {}).get("token_id") or "").strip()
    token = session.tokens.get(token_id) if token_id else None
    for uid, u in (session.users or {}).items():
        if token is not None and not _is_token_visible_to_user(session, token, u):
            continue
        await manager.send_to(session.id, uid, {
            "type": "combat_move_state",
            "payload": move_state,
        })


async def _send_token_move_denied(session: Session, user: User, token, message: str, *, client_action_id=None):
    move_state = _ensure_combat_movement_state(session)
    safe_message = str(message or "Movement blocked.")
    await manager.send_to(session.id, user.id, {
        "type": "token_move_denied",
        "payload": {
            "token_id": getattr(token, "id", None),
            "x": getattr(token, "x", None),
            "y": getattr(token, "y", None),
            "message": safe_message,
            "movement": move_state,
        }
    })
    await _send_action_ack(session, user, action="token_move", client_action_id=client_action_id,
                            status="failed", reason=safe_message)


async def _enforce_player_combat_movement(session: Session, user: User, token, new_x: float, new_y: float, *, client_action_id=None) -> bool:
    if user.role == "dm":
        return True
    combat = getattr(session, "combat", None) or {}
    if not combat.get("active"):
        return True
    token_ids = _combatant_token_ids(session)
    token_id = str(getattr(token, "id", "") or "")
    if token_id not in token_ids:
        return True
    current = _get_current_combatant(session)
    current_token_id = str((current or {}).get("token_id") or "").strip()
    if not current_token_id or current_token_id != token_id:
        await _send_token_move_denied(session, user, token, "It is not your turn to move that token.", client_action_id=client_action_id)
        return False
    move_state = _ensure_combat_movement_state(session)
    total_budget_ft = _movement_total_budget_ft(move_state)
    if total_budget_ft <= 0:
        return True
    resolved = _resolve_combat_movement(session, token, move_state, new_x, new_y)
    move_cost_ft = float(resolved.get("finalCostFeet", 0.0) or 0.0)
    if move_cost_ft <= 0:
        return True
    spent_ft = float(move_state.get("spent_ft", 0.0) or 0.0)
    remaining_ft = max(0.0, round(total_budget_ft - spent_ft, 2))
    if not resolved.get("valid", False):
        remaining_text = int(round(remaining_ft)) if abs(remaining_ft - round(remaining_ft)) < 0.05 else round(remaining_ft, 1)
        await _send_token_move_denied(session, user, token, f"Movement limit reached — {remaining_text} ft remaining this turn.", client_action_id=client_action_id)
        return False
    move_state["spent_ft"] = round(spent_ft + move_cost_ft, 2)
    move_state["remaining_ft"] = max(0.0, round(total_budget_ft - move_state["spent_ft"], 2))
    move_state["last_x"] = float(new_x)
    move_state["last_y"] = float(new_y)
    move_state["last_resolver"] = resolved
    session.combat["movement"] = move_state
    return True


async def handle_combat_move_preview(payload: dict, session: Session, user: User):
    await _handle_combat_move_plan(payload, session, user, commit=False)


async def handle_combat_move_commit(payload: dict, session: Session, user: User):
    await _handle_combat_move_plan(payload, session, user, commit=True)


async def _handle_combat_move_plan(payload: dict, session: Session, user: User, *, commit: bool):
    token_id = str(payload.get("token_id") or "").strip()
    token = session.tokens.get(token_id)
    if not token:
        return
    current = _get_current_combatant(session)
    if not (getattr(session, "combat", {}) or {}).get("active"):
        await _send_token_move_denied(session, user, token, "Combat is not active.")
        return
    if str((current or {}).get("token_id") or "") != token_id and user.role != "dm":
        await _send_token_move_denied(session, user, token, "Not your turn")
        return
    if user.role != "dm" and not _owner_matches_user(getattr(token, "owner_id", ""), user):
        await _send_token_move_denied(session, user, token, "You don't own the active token.")
        return
    move_state = _ensure_combat_movement_state(session)
    to_x = float(payload.get("to_x", payload.get("x", getattr(token, "x", 0.0))) or 0.0)
    to_y = float(payload.get("to_y", payload.get("y", getattr(token, "y", 0.0))) or 0.0)
    client_grid_px = payload.get("grid_size_px")
    resolved = _resolve_combat_movement(session, token, move_state, to_x, to_y, path=payload.get("path"), movement_mode=payload.get("movement_mode"), grid_size_px=float(client_grid_px) if client_grid_px is not None else None)
    expected = payload.get("expected_cost_ft")
    if expected is not None and abs(float(expected or 0) - float(resolved.get("finalCostFeet") or 0)) > 0.01:
        resolved["valid"] = False
        resolved["reason"] = "Movement cost changed on the server; preview again."
    if not resolved.get("valid"):
        await manager.send_to(session.id, user.id, {"type": "combat_move_preview_result" if not commit else "token_move_denied", "payload": {"token_id": token_id, "movement": move_state, "resolver": resolved, "message": resolved.get("reason")}})
        return
    if not commit:
        await manager.send_to(session.id, user.id, {"type": "combat_move_preview_result", "payload": {"token_id": token_id, "movement": move_state, "resolver": resolved}})
        return
    old_x, old_y = float(getattr(token, "x", 0.0) or 0.0), float(getattr(token, "y", 0.0) or 0.0)
    token.x, token.y = to_x, to_y
    move_state["spent_ft"] = round(float(move_state.get("spent_ft", 0.0) or 0.0) + float(resolved.get("finalCostFeet", 0.0) or 0.0), 2)
    move_state["remaining_ft"] = max(0.0, round(_movement_total_budget_ft(move_state) - move_state["spent_ft"], 2))
    move_state["last_x"], move_state["last_y"] = to_x, to_y
    move_state["last_resolver"] = resolved
    session.combat["movement"] = move_state
    _get_action_economy_entry(session, token_id, current)["movement_spent"] = round(float(move_state.get("spent_ft", 0.0) or 0.0), 2)
    _bump_combat_revision(session, "move_commit")
    # The combatant whose turn this is can be a hidden/fog-hidden/wall-hidden
    # NPC the DM is moving on its turn — never broadcast position unconditionally.
    await _broadcast_token_event(manager, session, "token_moved", {
        "token_id": token_id, "x": token.x, "y": token.y, "moved_by": user.name,
    }, token)
    await _broadcast_combat_move_state(session)
    session.add_log(f"{user.name} moved {getattr(token, 'name', 'token')} {resolved.get('finalCostFeet', 0):g} ft ({resolved.get('movementMode')}).", "combat")
    await save_campaign_async(session)


async def _advance_combat_turn(session: Session):
    """Advance to next turn, increment round when wrapping."""
    coms = session.combat.get("combatants", [])
    if not coms:
        return False
    prev_turn = session.combat.get("turn", 0)
    next_turn = (prev_turn + 1) % len(coms)
    session.combat["active"] = True
    session.combat["turn"] = next_turn
    session.combat["turn_locked"] = True
    session.combat["turn_started"] = True
    if next_turn == 0 and prev_turn != 0:
        session.combat["round"] = session.combat.get("round", 1) + 1
    elif "round" not in session.combat:
        session.combat["round"] = 1
    _ensure_combat_movement_state(session, reset=True)
    _sync_action_economy_roster(session)
    _reset_action_economy_for_turn_start(session)
    return True


def _combatant_from_token(session: Session, token, *, initiative=None, roll=None, modifier=None) -> dict:
    init_mod = _safe_int(modifier, _safe_int(getattr(token, "initiative_mod", 0), 0, minimum=-99, maximum=99), minimum=-99, maximum=99)
    init_value = None if initiative is None else _safe_int(initiative, 0, minimum=-99, maximum=99)
    return {
        "id": f"cmb-{getattr(token, 'id', '')}-{int(_time.time() * 1000)}",
        "token_id": getattr(token, "id", ""),
        "name": getattr(token, "name", "Token") or "Token",
        "color": getattr(token, "color", "#888888") or "#888888",
        "initiative": init_value,
        "roll": roll,
        "modifier": init_mod,
        "hp": getattr(token, "hp", None),
        "max_hp": getattr(token, "max_hp", None),
        "speed": getattr(token, "speed", None),
        "is_player": bool(getattr(token, "owner_id", None)),
        "owner_id": getattr(token, "owner_id", None) or None,
        "map_context": str(getattr(token, "map_context", "world") or "world"),
    }


def _visible_combatants_missing_initiative(combat: dict) -> list[dict]:
    coms = combat.get("combatants") if isinstance(combat.get("combatants"), list) else []
    return [c for c in coms if isinstance(c, dict) and c.get("initiative") is None]


def _combat_initiative_setup_phase(combat: dict) -> bool:
    """True while an active encounter is collecting initial initiative rolls.

    During setup the first row is only a placeholder, not an intentional active
    turn.  Once a DM/player advances or ends a turn we set ``turn_locked`` and
    preserve that active combatant for later mid-round initiative edits.
    """
    if not combat.get("active"):
        return False
    if combat.get("turn_locked") or combat.get("turn_started"):
        return False
    return bool(_visible_combatants_missing_initiative(combat))


def _sort_combatants_preserving_turn(combat: dict, *, preserve_turn: bool = True) -> None:
    coms = combat.get("combatants") or []
    old_turn = _safe_int(combat.get("turn"), 0, minimum=0, maximum=max(0, len(coms) - 1))
    current_id = None
    if preserve_turn and 0 <= old_turn < len(coms):
        current_id = str((coms[old_turn] or {}).get("id") or "")
    coms.sort(key=lambda x: (x.get("initiative") if x.get("initiative") is not None else -99), reverse=True)
    if current_id:
        for idx, combatant in enumerate(coms):
            if str((combatant or {}).get("id") or "") == current_id:
                combat["turn"] = idx
                break
        else:
            combat["turn"] = min(old_turn, max(0, len(coms) - 1))
    else:
        combat["turn"] = 0 if coms else 0


async def handle_combat_add_token(payload: dict, session: Session, user: User):
    """DM adds one visible current-map token to the active initiative order."""
    if user.role != "dm":
        return
    combat = getattr(session, "combat", None) or {}
    if not combat.get("active"):
        await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": "Combat is not active."}})
        return
    token_id = str(payload.get("token_id") or "").strip()
    token = session.tokens.get(token_id) if token_id else None
    if token is None:
        await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": "Token not found."}})
        return
    if bool(getattr(token, "hidden", False)) or bool(getattr(token, "staged", False)):
        await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": "Hidden or staged tokens cannot be added to initiative."}})
        return
    token_map = str(getattr(token, "map_context", "world") or "world")[:80] or "world"
    current_map = str(payload.get("map_context") or getattr(session, "dm_map_context", "world") or "world")[:80] or "world"
    if token_map != current_map:
        await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": "Token is not on the current map."}})
        return
    coms = combat.get("combatants") if isinstance(combat.get("combatants"), list) else []
    if any(str((c or {}).get("token_id") or "") == token_id for c in coms):
        await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": "Token is already in initiative."}})
        return
    combat["combatants"] = coms
    combat["active"] = True
    combat.setdefault("turn", 0)
    combat.setdefault("round", 1)
    combatant = _combatant_from_token(session, token, initiative=payload.get("initiative"), roll=payload.get("roll"), modifier=payload.get("modifier"))
    coms.append(combatant)
    if combatant.get("initiative") is not None:
        _sort_combatants_preserving_turn(combat)
    session.combat = combat
    _sync_action_economy_roster(session)
    _ensure_combat_movement_state(session, reset=False)
    sync_fogged_combatants(session, reason="manual_add", map_context=current_map)
    session.add_log(f"{getattr(token, 'name', 'Token')} joined the initiative order.", "combat", user.name)
    _bump_combat_revision(session, "add_token")
    await save_campaign_async(session)
    await _broadcast_combat(session)


async def handle_combat_remove_combatant(payload: dict, session: Session, user: User):
    """DM removes one combatant without ending combat."""
    if user.role != "dm":
        return
    combat = getattr(session, "combat", None) or {}
    coms = combat.get("combatants") if isinstance(combat.get("combatants"), list) else []
    combatant_id = str(payload.get("combatant_id") or "").strip()
    token_id = str(payload.get("token_id") or "").strip()
    old_turn = _safe_int(combat.get("turn"), 0, minimum=0, maximum=max(0, len(coms) - 1))
    current_id = str((coms[old_turn] or {}).get("id") or "") if 0 <= old_turn < len(coms) else ""
    remove_idx = -1
    for idx, combatant in enumerate(coms):
        if combatant_id and str((combatant or {}).get("id") or "") == combatant_id:
            remove_idx = idx; break
        if token_id and str((combatant or {}).get("token_id") or "") == token_id:
            remove_idx = idx; break
    if remove_idx < 0:
        return
    removed = coms.pop(remove_idx)
    combat["combatants"] = coms
    if not coms:
        combat["turn"] = 0
        combat["movement"] = {}
    elif str((removed or {}).get("id") or "") == current_id:
        combat["turn"] = min(remove_idx, len(coms) - 1)
        _ensure_combat_movement_state(session, reset=True)
    else:
        combat["turn"] = max(0, old_turn - 1) if remove_idx < old_turn else min(old_turn, len(coms) - 1)
        _ensure_combat_movement_state(session, reset=False)
    session.combat = combat
    session.add_log(f"{(removed or {}).get('name') or 'Combatant'} left the initiative order.", "combat", user.name)
    _bump_combat_revision(session, "remove_combatant")
    await save_campaign_async(session)
    await _broadcast_combat(session)


def remove_token_from_combat(session: Session, token_id: str) -> bool:
    """Drop every combatant (and suspended entry) bound to ``token_id``.

    Called when a token is deleted mid-combat so the initiative roster never
    keeps a ghost entry for a token that no longer exists. Mutates
    ``session.combat`` in place and returns True when the roster changed. The
    caller is responsible for bumping the combat revision, saving, and
    broadcasting the updated state.
    """
    token_id = str(token_id or "").strip()
    if not token_id:
        return False
    combat = getattr(session, "combat", None) or {}
    coms = combat.get("combatants") if isinstance(combat.get("combatants"), list) else []
    old_turn = _safe_int(combat.get("turn"), 0, minimum=0, maximum=max(0, len(coms) - 1))
    current_id = str((coms[old_turn] or {}).get("id") or "") if 0 <= old_turn < len(coms) else ""
    changed = False
    removed_names: list[str] = []
    kept = []
    for c in coms:
        if str((c or {}).get("token_id") or "") == token_id:
            changed = True
            removed_names.append(str((c or {}).get("name") or "Combatant"))
            continue
        kept.append(c)
    for key in ("suspended_combatants", "fog_suspended_combatants", "hidden_suspended_combatants"):
        lst = combat.get(key)
        if isinstance(lst, list):
            pruned = [x for x in lst if str((x or {}).get("token_id") or "") != token_id]
            if len(pruned) != len(lst):
                changed = True
            combat[key] = pruned
    if not changed:
        return False
    combat["combatants"] = kept
    if not kept:
        combat["turn"] = 0
        combat["movement"] = {}
    elif current_id and any(str((c or {}).get("id") or "") == current_id for c in kept):
        # The active combatant survived — keep the pointer on it.
        for i, c in enumerate(kept):
            if str((c or {}).get("id") or "") == current_id:
                combat["turn"] = i
                break
    else:
        # The active combatant (or one before it) was removed — clamp the turn.
        combat["turn"] = min(old_turn, len(kept) - 1)
    session.combat = combat
    _sync_action_economy_roster(session)
    _ensure_combat_movement_state(session, reset=True)
    for name in removed_names:
        session.add_log(f"{name} left the initiative order (token removed).", "combat", "System")
    return True


async def handle_combat_update(payload: dict, session: Session, user: User):
    """DM sets full combatant list (add/remove/reorder/edit initiative)."""
    if user.role != "dm":
        return
    # Lazy import to avoid circular dependency: combat -> hazards -> viewer_powers -> combat
    from server.handlers.hazards import _process_current_start_turn_hazards
    was_active = bool((session.combat or {}).get("active"))
    next_active = bool(payload.get("active", session.combat.get("active", False)))
    session.combat["combatants"] = payload.get("combatants", [])
    session.combat["active"] = next_active
    session.combat["turn"] = payload.get("turn", session.combat.get("turn", 0))
    session.combat["round"] = payload.get("round", session.combat.get("round", 1))
    if next_active and (not was_active or not session.combat.get("encounter_id")):
        session.combat["encounter_id"] = str(payload.get("encounter_id") or uuid.uuid4())
    if session.combat.get("active"):
        _sync_action_economy_roster(session)
        _reset_action_economy_for_turn_start(session)
        _ensure_combat_movement_state(session, reset=True)
        sync_fogged_combatants(session, reason="combat_update", map_context=payload.get("map_context"))
    else:
        session.combat["movement"] = {}
    await _process_current_start_turn_hazards(session)
    _bump_combat_revision(session, "combat_update")
    await save_campaign_async(session)
    await _broadcast_combat(session)
    # Auto-trigger ambient sound on combat start
    if not was_active and session.combat.get("active"):
        sound_state = getattr(session, "sound_state", None) or {}
        sound_state["pre_combat_track"] = sound_state.get("track", "silence")
        sound_state["track"] = "battle"
        sound_state["volume"] = sound_state.get("volume", 0.7)
        sound_state["fade_ms"] = 1500
        session.sound_state = sound_state
        await manager.broadcast(session.id, {
            "type": "sound_set_ambient",
            "payload": {"track": "battle", "volume": sound_state["volume"], "fade_ms": 1500},
        })


async def handle_combat_next(payload: dict, session: Session, user: User):
    """Advance to next turn, increment round when wrapping."""
    if not _can_manage_combat(session, user):
        return
    # Lazy imports to avoid circular dependency: combat -> hazards -> viewer_powers -> combat
    from server.handlers.hazards import (
        _process_current_end_turn_hazards,
        _process_current_start_turn_hazards,
        _process_end_round_hazards,
    )
    prev_round = _safe_int((getattr(session, 'combat', {}) or {}).get('round'), 1, minimum=1, maximum=999999)
    await _process_current_end_turn_hazards(session, round_number=prev_round)
    advanced = await _advance_combat_turn(session)
    if not advanced:
        return
    new_round = _safe_int((getattr(session, 'combat', {}) or {}).get('round'), prev_round, minimum=1, maximum=999999)
    if new_round > prev_round:
        await _process_end_round_hazards(session, round_number=prev_round)
    await _process_current_start_turn_hazards(session)
    _bump_combat_revision(session, "next_turn")
    mark_session_dirty(session)
    await save_campaign_async(session)
    await _broadcast_combat(session)


async def handle_combat_prev(payload: dict, session: Session, user: User):
    """Go back one turn, decrement round when wrapping."""
    if not _can_manage_combat(session, user):
        return
    coms = session.combat.get("combatants", [])
    if not coms:
        return
    prev_turn = session.combat.get("turn", 0)
    prev_round = session.combat.get("round", 1)
    new_turn = (prev_turn - 1) % len(coms)
    session.combat["active"] = True
    session.combat["turn"] = new_turn
    session.combat["turn_locked"] = True
    session.combat["turn_started"] = True
    if new_turn == len(coms) - 1 and prev_turn == 0:
        session.combat["round"] = max(1, prev_round - 1)
    _ensure_combat_movement_state(session, reset=True)
    _sync_action_economy_roster(session)
    _reset_action_economy_for_turn_start(session)
    _bump_combat_revision(session, "prev_turn")
    await save_campaign_async(session)
    await _broadcast_combat(session)


async def handle_combat_clear(payload: dict, session: Session, user: User):
    """End combat."""
    if user.role != "dm":
        return
    # Auto-restore ambient track that was playing before combat started
    sound_state = getattr(session, "sound_state", None) or {}
    pre_combat = sound_state.get("pre_combat_track", "silence")
    previous_revision = _safe_int((getattr(session, "combat", {}) or {}).get("revision"), 0, minimum=0, maximum=2**31)
    session.combat = {"active": False, "turn": 0, "combatants": [], "movement": {}, "combat_action_economy": {}, "encounter_id": str(uuid.uuid4()), "reason": "clear_combat", "clear_reason": "clear_combat", "revision": previous_revision}
    _bump_combat_revision(session, "clear_combat")
    event = {
        "event_type": "clear_encounter",
        "target_id": str(payload.get("encounter_id") or payload.get("encounter_template_id") or "").strip(),
    }
    quests = list(getattr(session, "session_quests", []) or [])
    for idx, quest in enumerate(quests):
        entry = normalize_quest_payload_shape(dict(quest or {}))
        if apply_objective_event(entry, event):
            quests[idx] = entry
    session.session_quests = quests
    await save_campaign_async(session)
    await _broadcast_combat(session)
    if sound_state.get("track") == "battle":
        sound_state["track"] = pre_combat
        sound_state.pop("pre_combat_track", None)
        sound_state["fade_ms"] = 2000
        session.sound_state = sound_state
        await manager.broadcast(session.id, {
            "type": "sound_set_ambient",
            "payload": {"track": pre_combat, "volume": sound_state.get("volume", 0.7), "fade_ms": 2000},
        })


async def handle_combat_dash(payload: dict, session: Session, user: User):
    allowed, message, current, token = _can_manage_current_turn_movement(session, user)
    if not allowed:
        if message:
            await manager.send_to(session.id, user.id, {"type": "token_move_denied", "payload": {"message": message, "movement": _ensure_combat_movement_state(session)}})
        return
    move_state = _ensure_combat_movement_state(session)
    if move_state.get("dash_used"):
        await manager.send_to(session.id, user.id, {"type": "token_move_denied", "payload": {"message": "Dash is already active for this turn.", "movement": move_state}})
        return
    speed_ft = float(move_state.get("speed_ft", 0.0) or 0.0)
    if speed_ft <= 0:
        await manager.send_to(session.id, user.id, {"type": "token_move_denied", "payload": {"message": "This token has no speed set.", "movement": move_state}})
        return
    move_state["bonus_ft"] = round(float(move_state.get("bonus_ft", 0.0) or 0.0) + speed_ft, 2)
    move_state["dash_used"] = True
    move_state["remaining_ft"] = max(0.0, round(_movement_total_budget_ft(move_state) - float(move_state.get("spent_ft", 0.0) or 0.0), 2))
    session.combat["movement"] = move_state
    _bump_combat_revision(session, "dash")
    await save_campaign_async(session)
    await _broadcast_combat(session)


async def handle_combat_toggle_difficult_terrain(payload: dict, session: Session, user: User):
    allowed, message, current, token = _can_manage_current_turn_movement(session, user)
    if not allowed:
        if message:
            await manager.send_to(session.id, user.id, {"type": "token_move_denied", "payload": {"message": message, "movement": _ensure_combat_movement_state(session)}})
        return
    move_state = _ensure_combat_movement_state(session)
    enabled = payload.get("enabled")
    if enabled is None:
        enabled = not bool(move_state.get("difficult_terrain"))
    move_state["difficult_terrain"] = bool(enabled)
    move_state["cost_multiplier"] = 2.0 if move_state["difficult_terrain"] else 1.0
    move_state["remaining_ft"] = max(0.0, round(_movement_total_budget_ft(move_state) - float(move_state.get("spent_ft", 0.0) or 0.0), 2))
    session.combat["movement"] = move_state
    _bump_combat_revision(session, "difficult_terrain")
    await save_campaign_async(session)
    await _broadcast_combat(session)


async def handle_combat_reset_movement(payload: dict, session: Session, user: User):
    if not _can_manage_combat(session, user):
        return
    move_state = _ensure_combat_movement_state(session)
    token_id = str(move_state.get("token_id") or "")
    token = session.tokens.get(token_id) if token_id else None
    move_state["spent_ft"] = 0.0
    move_state["remaining_ft"] = _movement_total_budget_ft(move_state)
    move_state["last_x"] = float(getattr(token, "x", 0.0) or 0.0) if token is not None else None
    move_state["last_y"] = float(getattr(token, "y", 0.0) or 0.0) if token is not None else None
    session.combat["movement"] = move_state
    _bump_combat_revision(session, "reset_movement")
    await save_campaign_async(session)
    await _broadcast_combat(session)


async def handle_combat_toggle_disengage(payload: dict, session: Session, user: User):
    allowed, message, current, token = _can_manage_current_turn_movement(session, user)
    if not allowed:
        if message:
            await manager.send_to(session.id, user.id, {"type": "token_move_denied", "payload": {"message": message, "movement": _ensure_combat_movement_state(session)}})
        return
    move_state = _ensure_combat_movement_state(session)
    enabled = payload.get("enabled")
    if enabled is None:
        enabled = not bool(move_state.get("disengaged"))
    move_state["disengaged"] = bool(enabled)
    session.combat["movement"] = move_state
    _bump_combat_revision(session, "disengage")
    await save_campaign_async(session)
    await _broadcast_combat(session)


async def handle_combat_end_turn(payload: dict, session: Session, user: User):
    combat = getattr(session, "combat", None) or {}
    if not combat.get("active"):
        return
    if user.role != "dm":
        current = _get_current_combatant(session)
        token_id = str((current or {}).get("token_id") or "").strip()
        token = session.tokens.get(token_id) if token_id else None
        owner_id = str(getattr(token, "owner_id", "") or (current or {}).get("owner_id") or "").strip()
        if not _owner_matches_user(owner_id, user):
            await manager.send_to(session.id, user.id, {"type": "token_move_denied", "payload": {"message": "Only the active player can end this turn.", "movement": _ensure_combat_movement_state(session)}})
            return
    advanced = await _advance_combat_turn(session)
    if not advanced:
        return
    _bump_combat_revision(session, "end_turn")
    await save_campaign_async(session)
    await _broadcast_combat(session)


async def handle_combat_death_save(payload: dict, session: Session, user: User):
    import random

    combat = getattr(session, "combat", None) or {}
    if not combat.get("active"):
        return
    current = _get_current_combatant(session)
    token_id = str((current or {}).get("token_id") or "").strip()
    token = session.tokens.get(token_id) if token_id else None
    if not current or not token:
        return
    owner_id = str(getattr(token, "owner_id", "") or (current or {}).get("owner_id") or "").strip()
    if user.role != "dm" and not _owner_matches_user(owner_id, user):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Only the active player can roll that death save."}
        })
        return
    hp_value = getattr(token, "hp", None)
    if hp_value is None or int(hp_value or 0) > 0:
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "That creature is not at 0 HP."}
        })
        return

    existing = current.get("death_saves") if isinstance(current.get("death_saves"), dict) else None
    death_saves = dict(existing or {"successes": 0, "fails": 0, "stable": False, "dead": False})
    if death_saves.get("dead"):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": f"{token.name} is already dead."}
        })
        return

    roll = random.randint(1, 20)
    if roll == 1:
        death_saves["fails"] = min(3, int(death_saves.get("fails", 0) or 0) + 2)
    elif roll >= 10:
        death_saves["successes"] = min(3, int(death_saves.get("successes", 0) or 0) + 1)
    else:
        death_saves["fails"] = min(3, int(death_saves.get("fails", 0) or 0) + 1)

    recovered = False
    dead = False
    if roll >= 10:
        recovered = True
        token.hp = 1
        current["hp"] = 1
        current.pop("death_saves", None)
        _persist_token_hp_to_owned_profiles(session, token)
    elif int(death_saves.get("fails", 0) or 0) >= 3:
        dead = True
        death_saves["dead"] = True
        current["death_saves"] = death_saves
    else:
        current["death_saves"] = death_saves

    detail = f"{token.name} death save {roll}: {int(death_saves.get('successes', 0) or 0)} success / {int(death_saves.get('fails', 0) or 0)} fail"
    if recovered:
        detail += " • recovers to 1 HP"
    elif dead:
        detail += " • dead"
    log_entry = session.add_log(detail, "dice", user.name)
    await manager.broadcast(session.id, {
        "type": "dice_result",
        "payload": {
            "user_id": user.id,
            "user_name": user.name,
            "dice_type": 20,
            "quantity": 1,
            "rolls": [roll],
            "total": roll,
            "modifier": 0,
            "roll_label": f"{token.name} death save",
            "log": log_entry,
        }
    })
    if recovered:
        from server.handlers.common import _broadcast_token_event
        await _broadcast_token_event(manager, session, "token_hp_updated", {
            "token_id": token.id,
            "hp": token.hp,
            "maxHp": token.max_hp,
            "hidden_hp": token.hidden_hp,
            "log": None,
        }, token)
        # Auto-generate a party memory for a dramatic survival moment
        from server.handlers.content import add_auto_party_memory, _broadcast_party_memory_state
        add_auto_party_memory(session, f"{token.name} survived on 1 HP.")
        await _broadcast_party_memory_state(session)
    _bump_combat_revision(session, "death_save")
    await save_campaign_async(session)
    await _broadcast_combat(session)


async def _send_initiative_sync_event(session: Session, user: User, message: dict, *, target_label: str) -> tuple[list[str], list[str]]:
    """Send an initiative follow-up popup/animation event to the roller only.

    The authoritative initiative *tracker* is synced to everyone separately by
    ``_broadcast_combat`` (combat_state). These follow-up events — the
    dice_result popup and the combat_initiative_rolled animation — are
    popup/animation only and must stay PRIVATE to the person who rolled.
    Broadcasting them previously made the big dice popup appear for every
    connected client whenever anyone rolled initiative.

    combat_state is still delivered first; this event is sent after it, but only
    to the roller, so the order the roller observes is: combat_state →
    dice_result → combat_initiative_rolled.
    """
    roller_id = str(getattr(user, "id", "") or "")
    sent_to: list[str] = []
    failed: list[str] = []
    if not roller_id:
        return sent_to, failed
    ok = await manager.send_to(session.id, roller_id, message)
    if ok:
        sent_to.append(roller_id)
    else:
        failed.append(roller_id)
    logger.info(
        "[combat initiative sync] send_after_roll type=%s rolled_by=%s target=%s revision=%s sent_to=%s failed=%s",
        message.get("type"),
        getattr(user, "id", None),
        target_label,
        (getattr(session, "combat", {}) or {}).get("revision"),
        roller_id if ok else None,
        roller_id if not ok else None,
    )
    return sent_to, failed


async def handle_combat_roll_initiative(payload: dict, session: Session, user: User):
    """Player or DM rolls initiative for a combatant. Rolls d20 + modifier, updates list.

    DMs can roll for any combatant. Players may only roll for their own combatant.
    Assistant DMs with combat.manage_limited scope can roll for any combatant.
    """
    combatant_id = str(payload.get("combatant_id") or "").strip()
    token_id = str(payload.get("token_id") or "").strip()
    roll_id = str(payload.get("roll_id") or "").strip()
    roll = _safe_int(payload.get("roll"), 0, minimum=1, maximum=20)

    coms = session.combat.get("combatants", [])
    target_combatant = None
    for c in coms:
        if combatant_id and str(c.get("id") or "") == combatant_id:
            target_combatant = c
            break
        if token_id and str(c.get("token_id") or "") == token_id:
            target_combatant = c
            break
    if target_combatant is None:
        return

    modifier = _safe_int(payload.get("modifier"), _safe_int(target_combatant.get("modifier"), 0, minimum=-99, maximum=99), minimum=-99, maximum=99)
    total = roll + modifier

    # Role check: DM and assistant DMs with combat scope can roll for anyone.
    # Players may only roll initiative for their own combatant.
    if not _can_manage_combat(session, user):
        combatant_owner_id = str((target_combatant or {}).get("owner_id") or "").strip()
        combatant_token_id = str((target_combatant or {}).get("token_id") or "").strip()
        token = session.tokens.get(combatant_token_id) if combatant_token_id else None
        token_owner_id = str(getattr(token, "owner_id", "") or "").strip()
        owner_id = combatant_owner_id or token_owner_id
        if not _owner_matches_user(owner_id, user):
            await manager.send_to(session.id, user.id, {
                "type": "error",
                "payload": {"message": "You may only roll initiative for your own character."}
            })
            return

    target_combatant["initiative"] = total
    target_combatant["roll"] = roll
    target_combatant["modifier"] = modifier

    # If this encounter is still collecting initial initiative, the old turn
    # index is not authoritative. Sort immediately and put the active pointer on
    # the highest rolled combatant (index 0 after sorting). Only preserve the
    # active combatant once the DM has explicitly advanced/locked the turn.
    setup_phase = _combat_initiative_setup_phase(session.combat)
    preserve_turn = bool(session.combat.get("turn_locked") or session.combat.get("turn_started")) and not setup_phase
    _sort_combatants_preserving_turn(session.combat, preserve_turn=preserve_turn)
    session.combat["combatants"] = coms
    if setup_phase or not preserve_turn:
        session.combat["turn"] = 0 if coms else 0
    _ensure_combat_movement_state(session, reset=True)
    _bump_combat_revision(session, "roll_initiative")
    await save_campaign_async(session)

    broadcast_result = await _broadcast_combat(session)
    if isinstance(broadcast_result, dict) and user.id in set(broadcast_result.get("failed") or []):
        ack_payload = _combat_state_payload_for_user(session, user, broadcast_result.get("visibility_revision"))
        ack_ok = await manager.send_to(session.id, user.id, {"type": "combat_state", "payload": ack_payload})
        logger.warning(
            "[combat initiative sync] direct roller ack after failed broadcast user_id=%s success=%s revision=%s",
            user.id,
            bool(ack_ok),
            session.combat.get("revision"),
        )

    # Drive the dice popup through the dice_result path, but PRIVATELY: only the
    # roller sees their own big initiative popup (and the matching
    # combat_initiative_rolled animation). The initiative *tracker* still updates
    # for everyone via the combat_state broadcast above, so the order populates
    # for all clients without showing them someone else's dice popup. The
    # combatant_id/roll_id let the roller dedupe their own already-shown local
    # animation instead of double-popping.
    initiative_name = str(target_combatant.get("name") or "Combatant")
    target_label = str(target_combatant.get("token_id") or target_combatant.get("id") or combatant_id or token_id or "")
    dice_message = {
        "type": "dice_result",
        "payload": {
            "user_id": user.id,
            "user_name": user.name,
            "dice_type": 20,
            "quantity": 1,
            "rolls": [roll],
            "total": total,
            "modifier": modifier,
            "roll_label": f"{initiative_name} initiative",
            "combatant_id": str(target_combatant.get("id") or ""),
            "token_id": str(target_combatant.get("token_id") or ""),
            "encounter_id": session.combat.get("encounter_id"),
            "revision": session.combat.get("revision"),
            "roll_id": roll_id,
        },
    }
    # Intended order: combat_state first (above), then dice_result, then
    # combat_initiative_rolled. The state payload is authoritative; these two
    # follow-up events animate/diagnose the already-applied roll.
    dice_sent_to, dice_failed = await _send_initiative_sync_event(session, user, dice_message, target_label=target_label)

    initiative_message = {
        "type": "combat_initiative_rolled",
        "payload": {
            "combatant_id": str(target_combatant.get("id") or ""),
            "token_id": str(target_combatant.get("token_id") or ""),
            "initiative": total,
            "roll": roll,
            "modifier": modifier,
            "revision": session.combat.get("revision"),
            "encounter_id": session.combat.get("encounter_id"),
        },
    }
    rolled_sent_to, rolled_failed = await _send_initiative_sync_event(session, user, initiative_message, target_label=target_label)
    logger.info(
        "[combat initiative sync] rolled_by=%s target=%s revision=%s sent_to=%s failed=%s",
        user.id,
        combatant_id or token_id,
        session.combat.get("revision"),
        {"dice_result": dice_sent_to, "combat_initiative_rolled": rolled_sent_to},
        {"dice_result": dice_failed, "combat_initiative_rolled": rolled_failed},
    )
    log_entry = session.add_log(
        f"🎲 {user.name} initiative: {roll} + {modifier:+d} = {total}",
        "dice", user.name
    )
    await manager.broadcast(session.id, {"type": "log_entry", "payload": {"log": log_entry}})


async def handle_combat_state_request(payload: dict, session: Session, user: User):
    """Send the requesting client the current authoritative combat_state."""
    from server.handlers.common import bump_visibility_revision

    revision = bump_visibility_revision(session)
    out = _combat_state_payload_for_user(session, user, revision)
    filter_summary = out.pop("_filter_summary", {})
    ok = await manager.send_to(session.id, user.id, {"type": "combat_state", "payload": out})
    logger.info(
        "[combat initiative sync] state_request user_id=%s role=%s success=%s revision=%s active=%s sent=%s filtered=%s visibility_revision=%s source=%s",
        user.id,
        getattr(user, "role", "unknown"),
        bool(ok),
        out.get("revision"),
        bool(out.get("active")),
        filter_summary.get("sent_count", len(out.get("combatants") or [])),
        filter_summary.get("filtered_count", 0),
        out.get("visibility_revision"),
        "combat_state_request",
    )
    logger.info("[live_state] combat_state_request %s", build_live_state_debug_summary(session, user.id, user.role, {"combat": out, "visibility_revision": out.get("visibility_revision")}))


# ── Combat attack / spell flow ─────────────────────────────────────────────


def _can_act_current_turn(session: Session, user: User) -> tuple[bool, str | None]:
    """Return (allowed, error_message). True if user is DM or owns the active combatant."""
    combat = getattr(session, "combat", None) or {}
    if not combat.get("active"):
        return False, "Combat is not active."
    current = _get_current_combatant(session)
    if not current:
        return False, "No active combatant."
    if user.role == "dm":
        return True, None
    token_id = str((current or {}).get("token_id") or "").strip()
    token = session.tokens.get(token_id) if token_id else None
    owner_id = str(getattr(token, "owner_id", "") or (current or {}).get("owner_id") or "").strip()
    if not _owner_matches_user(owner_id, user):
        return False, "Only the active combatant can do that."
    return True, None



async def handle_combat_action_economy_use(payload: dict, session: Session, user: User):
    """Authoritatively spend/reset one combatant action-economy resource."""
    combat = getattr(session, "combat", None) or {}
    if not combat.get("active"):
        await _deny_action_economy(session, user, "Combat is not active.", client_action_id=payload.get("client_action_id"))
        return
    action_type = str(payload.get("action_type") or payload.get("kind") or "action").strip().lower()
    if action_type not in ACTION_ECONOMY_TYPES and action_type not in {"weapon_attack", "spell", "item_action"}:
        action_type = "action"
    token_id, combatant = _action_economy_token_for_payload(session, payload)
    if not token_id:
        await _deny_action_economy(session, user, "No combatant selected.", client_action_id=payload.get("client_action_id"))
        return
    override = bool(payload.get("override") or payload.get("dm_override"))
    reset = bool(payload.get("reset"))
    if reset and user.role != "dm":
        await _deny_action_economy(session, user, "Only the DM can reset action economy.", client_action_id=payload.get("client_action_id"))
        return
    if not reset and not _user_can_spend_for_token(session, user, token_id, combatant, override=override):
        await _deny_action_economy(session, user, "Only the active combatant can spend that action.", client_action_id=payload.get("client_action_id"))
        return
    if override and user.role != "dm":
        await _deny_action_economy(session, user, "Only the DM can override action economy.", client_action_id=payload.get("client_action_id"))
        return
    if reset:
        entry = _get_action_economy_entry(session, token_id, combatant)
        entry.update(_default_action_economy_entry(token_id, str((combatant or {}).get("id") or "")))
        entry["updated_at"] = _time.time()
        _bump_combat_revision(session, "action_economy_reset")
    else:
        ok, reason, entry = _mark_action_economy(session, token_id, action_type, note=str(payload.get("note") or "")[:240], override=override, combatant=combatant)
        if not ok:
            await _deny_action_economy(session, user, reason, client_action_id=payload.get("client_action_id"))
            return
    await save_campaign_async(session)
    await _broadcast_combat(session)
    await _send_action_ack(session, user, action="combat_action_economy_use", client_action_id=payload.get("client_action_id"), status="confirmed", token_id=token_id, action_type=action_type, combat_action_economy=(session.combat or {}).get("combat_action_economy") or {})

async def handle_combat_select_target(payload: dict, session: Session, user: User):
    """Store the chosen target token on the combat state and broadcast."""
    allowed, message = _can_act_current_turn(session, user)
    if not allowed:
        if message:
            await manager.send_to(session.id, user.id, {
                "type": "error", "payload": {"message": message}
            })
        return
    target_id = str(payload.get("target_id") or "").strip()
    if target_id and target_id not in session.tokens:
        await manager.send_to(session.id, user.id, {
            "type": "error", "payload": {"message": "Target token not found."}
        })
        return
    session.combat["selected_target_id"] = target_id
    _bump_combat_revision(session, "select_target")
    await _broadcast_combat(session)


async def handle_combat_attack_request(payload: dict, session: Session, user: User):
    """Player (or DM) submits an attack/spell request for DM resolution.

    Stores a ``pending_attack`` on the combat dict and broadcasts so the DM
    sees the Succeed / Fail prompt. Prevents duplicate submissions.
    """
    client_action_id = payload.get("client_action_id")

    async def _denied(reason: str, *, status: str = "denied"):
        await _send_action_ack(session, user, action="combat_attack_request",
                                client_action_id=client_action_id, status=status, reason=reason)

    allowed, message = _can_act_current_turn(session, user)
    if not allowed:
        if message:
            await manager.send_to(session.id, user.id, {
                "type": "error", "payload": {"message": message}
            })
        await _denied("Not your turn")
        return

    # Prevent duplicate submission while one is already pending.
    if session.combat.get("pending_attack"):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "An attack is already waiting for DM resolution."}
        })
        await _denied("Action denied")
        return

    target_id = str(payload.get("target_id") or "").strip()
    target_token = session.tokens.get(target_id) if target_id else None
    if not target_token:
        await manager.send_to(session.id, user.id, {
            "type": "error", "payload": {"message": "No valid target selected."}
        })
        await _denied("Invalid target")
        return

    attack_kind = str(payload.get("attack_kind") or "weapon").strip().lower()
    if attack_kind not in ("weapon", "spell"):
        attack_kind = "weapon"

    current = _get_current_combatant(session) or {}
    attacker_token_id = str(current.get("token_id") or "").strip()
    attacker_token = session.tokens.get(attacker_token_id) if attacker_token_id else None
    attacker_name = str(getattr(attacker_token, "name", None) or current.get("name") or user.name)
    attacker_owner_id = str(getattr(attacker_token, "owner_id", None) or current.get("owner_id") or user.id or "").strip()
    target_name = str(getattr(target_token, "name", None) or "Target")
    has_disadvantage = _has_encumbrance_attack_disadvantage(session, attacker_owner_id)
    disadvantage_reason = "heavily_encumbered" if has_disadvantage else ""

    spell_name = str(payload.get("spell_name") or "").strip()
    spell_id = str(payload.get("spell_id") or "").strip()

    econ_kind = "spell" if attack_kind == "spell" else "weapon_attack"
    ok, reason, _entry = _mark_action_economy(session, attacker_token_id, econ_kind, note=spell_name or attack_kind, override=(user.role == "dm" and bool(payload.get("override"))), combatant=current, bump=False)
    if not ok:
        await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": reason}})
        await _denied(reason)
        return

    session.combat["pending_attack"] = {
        "attacker_user_id": str(user.id),
        "attacker_token_id": attacker_token_id,
        "attacker_name": attacker_name,
        "target_token_id": target_id,
        "target_name": target_name,
        "attack_kind": attack_kind,
        "spell_name": spell_name,
        "spell_id": spell_id,
        "disadvantage": has_disadvantage,
        "disadvantage_reason": disadvantage_reason,
        "submitted_at": _time.time(),
    }

    action_label = spell_name if (attack_kind == "spell" and spell_name) else attack_kind
    if has_disadvantage:
        action_label = f"{action_label} · DISADVANTAGE"
    log_entry = session.add_log(
        f"{'✨' if attack_kind == 'spell' else '⚔'} {attacker_name} → {target_name} ({action_label}) — awaiting DM",
        "system", user.name
    )
    await save_campaign_async(session)
    await _broadcast_combat(session)
    await manager.broadcast(session.id, {"type": "log_entry", "payload": {"log": log_entry}})
    await _send_action_ack(
        session, user, action="combat_attack_request", client_action_id=client_action_id,
        status="confirmed", target_id=target_id,
        combat_revision=_safe_int((session.combat or {}).get("revision"), 0, minimum=0, maximum=2**31),
    )


async def handle_combat_attack_override(payload: dict, session: Session, user: User):
    """DM resolves a pending attack as hit or miss and broadcasts the result animation."""
    if user.role != "dm":
        await manager.send_to(session.id, user.id, {
            "type": "error", "payload": {"message": "Only the DM can resolve attacks."}
        })
        return

    # Atomically pop the pending attack to prevent double-trigger.
    pending = session.combat.pop("pending_attack", None)
    if not pending:
        await manager.send_to(session.id, user.id, {
            "type": "error", "payload": {"message": "No pending attack to resolve."}
        })
        return

    result = str(payload.get("result") or "miss").strip().lower()
    if result not in ("hit", "miss"):
        result = "miss"

    target_token_id = str(pending.get("target_token_id") or "").strip()
    attack_kind = str(pending.get("attack_kind") or "weapon")
    attacker_name = str(pending.get("attacker_name") or "Attacker")
    target_name = str(pending.get("target_name") or "Target")

    attacker_user_id = str(pending.get("attacker_user_id") or "")
    attacker_token_id_out = str(pending.get("attacker_token_id") or "")
    spell_name_out = str(pending.get("spell_name") or "")
    spell_id_out = str(pending.get("spell_id") or "")
    has_disadvantage = bool(pending.get("disadvantage"))
    disadvantage_reason = str(pending.get("disadvantage_reason") or "")

    # Broadcast the visual result to every connected client.
    await manager.broadcast(session.id, {
        "type": "combat_attack_result",
        "payload": {
            "result": result,
            "target_token_id": target_token_id,
            "attack_kind": attack_kind,
            "attacker_name": attacker_name,
            "target_name": target_name,
            "attacker_user_id": attacker_user_id,
            "attacker_token_id": attacker_token_id_out,
            "spell_name": spell_name_out,
            "spell_id": spell_id_out,
            "disadvantage": has_disadvantage,
            "disadvantage_reason": disadvantage_reason,
        },
    })

    outcome_text = "hit" if result == "hit" else "missed"
    log_entry = session.add_log(
        f"{'⚔' if attack_kind == 'weapon' else '✨'} {attacker_name} {outcome_text} {target_name} ({attack_kind})",
        "system", user.name
    )
    await manager.broadcast(session.id, {"type": "log_entry", "payload": {"log": log_entry}})

    # Broadcast updated combat state (pending_attack now cleared).
    await _broadcast_combat(session)
