"""
server/handlers/tokens.py — Token creation, movement, and state handlers.
"""
from server.editor_schema import canonical_weather_type
from server.session import create_token, assistant_dm_has_scope, build_token_runtime_payload
from server.map_logic import find_movement_blocker
from server.handlers.common import (
    Session, User, manager,
    save_campaign_async,
    _sanitize_token_vision_payload,
    _broadcast_token_event,
    _broadcast_token_state_sync,
    _broadcast_token_visibility,
    _sync_combatant_token_state,
    _persist_token_hp_to_owned_profiles,
    _profile_owner_keys_for_token_owner,
    _broadcast_combat,
    _sanitize_save_bonuses,
    bump_visibility_revision,
    bump_token_state_revision,
    _stamp_token_revision,
    _send_action_ack,
)
from server.session import normalize_profile_owner_key, normalize_map_context
from server.handlers.conditions import (
    _prune_token_condition_timers,
    _set_token_condition,
    _clear_token_condition,
    _broadcast_token_condition_state,
)
from server.handlers.combat import (
    _enforce_player_combat_movement,
    _broadcast_combat_move_state,
    _send_token_move_denied,
    _can_act_current_turn,
    run_combat_fog_sync,
    remove_token_from_combat,
    _bump_combat_revision,
)
from server.handlers.hazards import _process_hazard_triggers_for_token
from server.handlers.token_move_performance import decide_token_move_heavy_work, mark_token_move_heavy_work_ran
from server.handlers.move_coalescer import schedule_token_move_flush
from server.handlers.durability import mark_session_dirty
from server.handlers.content import handle_discovery_trigger
from server.handlers.narration import broadcast_narration_hook
from server.ambient_audio import normalize_ambient_profile
from server.living_world_events import emit_world_event, consume_world_event
from server.character.summon_runtime import synchronize_active_summon_state
import time


_TOKEN_EMOTE_DEFS = {
    "question": {"icon": "❓", "label": "Question"},
    "danger": {"icon": "⚠️", "label": "Danger"},
    "laugh": {"icon": "😂", "label": "Laugh"},
    "help": {"icon": "🆘", "label": "Help"},
    "ready": {"icon": "✅", "label": "Ready"},
    "angry": {"icon": "😠", "label": "Angry"},
    "stealth": {"icon": "🕵️", "label": "Stealth"},
    "prayer": {"icon": "🙏", "label": "Prayer"},
    "arcane_focus": {"icon": "✨", "label": "Arcane Focus"},
}
_TOKEN_EMOTE_COOLDOWN_SEC = 2.0
_TOKEN_EMOTE_TTL_SEC = 2.8


def _safe_int(value, fallback: int = 0, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = fallback
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


# _profile_owner_keys_for_token_owner / _persist_token_hp_to_owned_profiles now
# live in server/handlers/common.py so every HP-mutating handler (viewer powers,
# hazards, rests, combat) shares the same profile mirror; imported above.


def _point_in_scene_trigger_bounds(x: float, y: float, bounds: dict) -> bool:
    shape = str((bounds or {}).get("shape") or "rect").strip().lower()
    bx = float((bounds or {}).get("x", 0.0) or 0.0)
    by = float((bounds or {}).get("y", 0.0) or 0.0)
    if shape == "circle":
        radius = max(1.0, float((bounds or {}).get("radius", 1.0) or 1.0))
        dx = x - bx
        dy = y - by
        return (dx * dx + dy * dy) <= (radius * radius)
    bw = max(1.0, float((bounds or {}).get("width", 1.0) or 1.0))
    bh = max(1.0, float((bounds or {}).get("height", 1.0) or 1.0))
    return bx <= x <= (bx + bw) and by <= y <= (by + bh)


def _zone_audience_user_ids(session: Session, zone: dict, actor: User) -> set[str]:
    users = dict(getattr(session, "users", {}) or {})
    mode = str(((zone.get("visibility") or {}).get("mode")) or "party").strip().lower()
    allow_viewers = bool(((zone.get("visibility") or {}).get("allow_viewers")))
    actor_id = str(getattr(actor, "id", "") or "")
    out: set[str] = set()
    for uid, user in users.items():
        role = str(getattr(user, "role", "") or "").strip().lower()
        if mode == "dm_only" and role != "dm":
            continue
        if mode == "players_only" and role != "player":
            continue
        if mode == "owner_only" and uid not in {actor_id, str(getattr(session, "dm_id", "") or "")}:
            continue
        if mode == "party" and role not in {"dm", "player"}:
            continue
        if role == "viewer" and not allow_viewers:
            continue
        out.add(str(uid))
    return out


async def _apply_scene_trigger_action(session: Session, zone: dict, action: dict, actor: User, token, *, phase: str):
    action_type = str((action or {}).get("type") or "").strip().lower()
    payload = dict((action or {}).get("payload") or {})
    audience = _zone_audience_user_ids(session, zone, actor)
    map_ctx = str((zone.get("map_context") or getattr(token, "map_context", "world") or "world"))

    if action_type == "ambient_profile":
        track = normalize_ambient_profile(payload.get("track") or payload.get("profile") or "silence")
        volume = max(0.0, min(1.0, float(payload.get("volume", 0.7) or 0.7)))
        fade_ms = max(0, min(5000, int(payload.get("fade_ms", 1200) or 1200)))
        session.sound_state = {"track": track, "volume": volume, "fade_ms": fade_ms}
        await manager.broadcast(session.id, {"type": "sound_set_ambient", "payload": {"track": track, "volume": volume, "fade_ms": fade_ms}})
        return

    if action_type == "weather_preset":
        weather_cfg = {
            "weather_type": canonical_weather_type(payload.get("weather_type") or payload.get("preset") or "none"),
            "intensity": max(0.0, min(1.0, float(payload.get("intensity", 0.5) or 0.5))),
            "wind_angle": max(0.0, min(360.0, float(payload.get("wind_angle", 0.0) or 0.0))),
            "wind_speed": max(0.0, min(1.0, float(payload.get("wind_speed", 0.3) or 0.3))),
            "darkness": max(0.0, min(1.0, float(payload.get("darkness", 0.0) or 0.0))),
            "lightning_frequency": max(0.0, min(1.0, float(payload.get("lightning_frequency", 0.5) or 0.5))),
            "audio_linked": bool(payload.get("audio_linked", True)),
            "map_context": map_ctx,
        }
        session.weather_state = dict(weather_cfg)
        settings = dict((getattr(session, "map_settings", {}) or {}))
        row = dict(settings.get(map_ctx) or {})
        row["weather"] = {k: v for k, v in weather_cfg.items() if k != "map_context"}
        settings[map_ctx] = row
        session.map_settings = settings
        await manager.broadcast(session.id, {"type": "weather_sync", "payload": weather_cfg})
        return

    if action_type == "narration_hook":
        hook_payload = {
            "zone_id": str(zone.get("id") or ""),
            "phase": phase,
            "map_context": map_ctx,
            "title": str(payload.get("title") or "Narration Hook")[:120],
            "prompt": str(payload.get("prompt") or payload.get("text") or "")[:500],
            "token_id": str(getattr(token, "id", "") or ""),
            "actor_user_id": str(getattr(actor, "id", "") or ""),
        }
        if bool(((zone.get("visibility") or {}).get("dm_only_narration", False))):
            dm_id = str(getattr(session, "dm_id", "") or "")
            await broadcast_narration_hook(session, hook_payload, {dm_id} if dm_id else None)
        else:
            await broadcast_narration_hook(session, hook_payload, audience)
        return

    if action_type == "unlock_discovery":
        discovery_payload = {
            "id": str(payload.get("id") or f"zone-{zone.get('id', 'unknown')}-discovery")[:48],
            "title": str(payload.get("title") or "New Discovery")[:120],
            "body": str(payload.get("body") or payload.get("text") or "")[:1200],
            "kind": str(payload.get("kind") or "clue")[:40],
            "tone": str(payload.get("tone") or "mystic")[:24],
            "visibility": str(payload.get("visibility") or "party_public")[:32],
            "target_user_id": str(payload.get("target_user_id") or getattr(actor, "id", ""))[:64],
            "source": str(payload.get("source") or f"scene_trigger:{zone.get('id')}")[:80],
        }
        dm = (getattr(session, "users", {}) or {}).get(getattr(session, "dm_id", ""))
        if dm:
            await handle_discovery_trigger(discovery_payload, session, dm)
        return

    if action_type == "set_world_state_flag":
        key = str(payload.get("key") or payload.get("flag") or "").strip()[:80]
        if key:
            world_state = dict(getattr(session, "world_state", {}) or {})
            flags = dict(world_state.get("world_state_flags") or {})
            flags[key] = payload.get("value", True)
            world_state["world_state_flags"] = flags
            session.world_state = world_state
        return

    if action_type == "living_world_event":
        event_type = str(payload.get("event_type") or "world_state_flag_set").strip().lower()[:64] or "world_state_flag_set"
        summary = str(payload.get("summary") or f"Scene trigger {zone.get('id')} fired.")[:240]
        event = emit_world_event(session, event_type, {
            "source": f"scene_trigger:{zone.get('id')}",
            "actor_user_id": str(getattr(actor, "id", "") or ""),
            "summary": summary,
            "meta": {"zone_id": str(zone.get("id") or ""), "phase": phase, "map_context": map_ctx},
        })
        consume_world_event(session, event, dict(payload.get("reaction_bundle") or {}))
        notice = {"type": "world_event_notice", "payload": event}
        if audience:
            for user_id in audience:
                await manager.send_to(session.id, user_id, notice)
        else:
            await manager.broadcast(session.id, notice)


async def _process_scene_triggers_for_token(session: Session, token, user: User, *, old_x: float, old_y: float) -> bool:
    if str(getattr(token, "owner_id", "") or "").strip() == "":
        return False
    if user.role == "dm" and not bool(getattr(token, "owner_id", None)):
        return False
    zones = dict((session.scene_trigger_zones() if hasattr(session, "scene_trigger_zones") else {}) or {})
    if not zones:
        return False
    token_ctx = str(getattr(token, "map_context", "world") or "world")
    world_state = dict(getattr(session, "world_state", {}) or {})
    runtime = dict(world_state.get("scene_trigger_runtime") or {})
    consumed_ids = set(str(v) for v in (runtime.get("consumed_zone_ids") or []) if str(v))
    last_trigger_at = dict(runtime.get("last_trigger_at") or {})
    now = time.time()

    triggered_any = False
    for zone in zones.values():
        if not zone or not bool(zone.get("enabled", True)):
            continue
        if str(zone.get("map_context") or "world") != token_ctx:
            continue
        if user.role == "dm" and not bool(zone.get("allow_dm_trigger", False)):
            continue
        zone_id = str(zone.get("id") or "")
        if not zone_id:
            continue
        was_inside = _point_in_scene_trigger_bounds(old_x, old_y, zone.get("bounds") or {})
        is_inside = _point_in_scene_trigger_bounds(float(getattr(token, "x", 0.0) or 0.0), float(getattr(token, "y", 0.0) or 0.0), zone.get("bounds") or {})
        if was_inside == is_inside:
            continue
        phase = "on_enter" if is_inside else "on_exit"
        if phase == "on_enter" and bool(zone.get("trigger_once")) and zone_id in consumed_ids:
            continue
        key = f"{zone_id}:{phase}:{getattr(token, 'id', '')}"
        cooldown_ms = max(0, int(zone.get("cooldown_ms", 0) or 0))
        debounce_ms = max(0, int(zone.get("debounce_ms", 0) or 0))
        wait_sec = max(cooldown_ms, debounce_ms) / 1000.0
        if wait_sec > 0 and (now - float(last_trigger_at.get(key, 0.0) or 0.0)) < wait_sec:
            continue
        for action in (zone.get(phase) or []):
            await _apply_scene_trigger_action(session, zone, action, user, token, phase=phase)
        triggered_any = True
        last_trigger_at[key] = now
        if phase == "on_enter" and bool(zone.get("trigger_once")):
            consumed_ids.add(zone_id)

    runtime["consumed_zone_ids"] = sorted(consumed_ids)
    runtime["last_trigger_at"] = last_trigger_at
    merged_world_state = dict(getattr(session, "world_state", {}) or {})
    merged_world_state["scene_trigger_runtime"] = runtime
    session.world_state = merged_world_state
    return triggered_any


def _token_cr_as_float(token) -> float:
    raw = getattr(token, "cr", None)
    if raw is None:
        return 0.0
    text = str(raw).strip().lower()
    if not text:
        return 0.0
    if "/" in text:
        parts = text.split("/", 1)
        try:
            num = float(parts[0])
            den = float(parts[1] or 1.0)
            if den > 0:
                return max(0.0, num / den)
        except Exception:
            return 0.0
    try:
        return max(0.0, float(text))
    except Exception:
        return 0.0


def _ensure_corpse_state_for_token(session: Session, token):
    if not token:
        return
    token_kind = str(getattr(token, "token_type", "player") or "player").strip().lower()
    if token_kind not in {"monster", "npc"}:
        return
    if int(getattr(token, "hp", 1) or 0) > 0:
        return
    corpse_states = dict(getattr(session, "corpse_states", {}) or {})
    corpse_id = str(getattr(token, "id", "") or "").strip()
    if not corpse_id:
        return
    if corpse_id in corpse_states:
        return
    now = time.time()
    corpse_states[corpse_id] = {
        "corpse_id": corpse_id,
        "token_id": corpse_id,
        "token_name": str(getattr(token, "name", "Corpse") or "Corpse")[:120],
        "map_context": str(getattr(token, "map_context", "world") or "world"),
        "defeated_at": now,
        "depleted": False,
        "search_attempts": {},
        "harvest_attempts": {},
        "creature_ref": {
            "creature_id": str(getattr(token, "creature_id", "") or ""),
            "creature_type": str(getattr(token, "creature_type", "") or ""),
            "monster_type": str(getattr(token, "monster_type", "") or ""),
            "cr": str(getattr(token, "cr", "") or ""),
            "cr_num": _token_cr_as_float(token),
            "token_type": token_kind,
        },
    }
    session.corpse_states = corpse_states


def _owner_matches_user(owner_id, user: User) -> bool:
    owner = str(owner_id or "").strip()
    if not owner:
        return False
    if owner == str(user.id):
        return True
    owner_key = normalize_profile_owner_key(owner)
    if not owner_key:
        return False
    return owner_key == normalize_profile_owner_key(getattr(user, "name", ""))




def _assistant_can_control_token(session: Session, user: User, token) -> bool:
    if not token:
        return False
    token_kind = str(getattr(token, "token_type", "player") or "player").strip().lower()
    if token_kind not in {"npc", "monster"}:
        return False
    return assistant_dm_has_scope(session, user, "tokens.control_npc", token_id=str(getattr(token, "id", "") or ""))

def _player_active_owned_tokens(session: Session, owner_id: str, *, exclude_token_id: str | None = None, include_companions: bool = False) -> list:
    owner = str(owner_id or "").strip()
    exclude = str(exclude_token_id or "").strip()
    if not owner:
        return []
    active = []
    for tok in (getattr(session, "tokens", {}) or {}).values():
        tok_owner = str(getattr(tok, "owner_id", "") or "").strip()
        if tok_owner != owner:
            continue
        if bool(getattr(tok, "staged", False)):
            continue
        token_kind = str(getattr(tok, "token_type", "player") or "player").strip().lower()
        if not include_companions and token_kind == "companion":
            continue
        tok_id = str(getattr(tok, "id", "") or "")
        if exclude and tok_id == exclude:
            continue
        active.append(tok)
    return active


async def _deny_player_single_token_limit(session: Session, user: User, *, token_name: str = "token"):
    await manager.send_to(session.id, user.id, {
        "type": "error",
        "payload": {"message": f"You can only have one active token on the field. Remove or stage your current token before creating or claiming another {token_name}."}
    })


async def _move_player_owned_token_from_other_map(payload: dict, session: Session, user: User, active_tokens: list) -> bool:
    """Let a player bring their one existing token onto the DM's current map."""
    if user.role != "player" or len(active_tokens) != 1:
        return False
    token = active_tokens[0]
    requested_ctx = normalize_map_context(payload.get("map_context", getattr(token, "map_context", "world")))
    current_ctx = normalize_map_context(getattr(token, "map_context", "world"))
    if requested_ctx == current_ctx:
        return False

    token.x = payload.get("x", token.x)
    token.y = payload.get("y", token.y)
    token.map_context = requested_ctx
    token.staged = False
    if payload.get("name"):
        token.name = str(payload.get("name"))[:80]
    if payload.get("hp") is not None:
        token.hp = _safe_int(payload.get("hp"), getattr(token, "hp", 1), minimum=0)
    if payload.get("maxHp") is not None:
        token.max_hp = _safe_int(payload.get("maxHp"), getattr(token, "max_hp", 1), minimum=1)
    if payload.get("tempHp") is not None:
        token.temp_hp = _safe_int(payload.get("tempHp"), getattr(token, "temp_hp", 0), minimum=0)
    if payload.get("initiativeMod") is not None:
        token.initiative_mod = _safe_int(payload.get("initiativeMod"), getattr(token, "initiative_mod", 0))
    if payload.get("ac") is not None:
        token.ac = _safe_int(payload.get("ac"), getattr(token, "ac", 10), minimum=1)
    if payload.get("speed") is not None:
        token.speed = _safe_int(payload.get("speed"), getattr(token, "speed", 30), minimum=0)
    if payload.get("profile_id") is not None:
        token.profile_id = str(payload.get("profile_id") or "")[:120]
    if payload.get("libraryId", payload.get("library_id")) is not None:
        token.library_id = str(payload.get("libraryId", payload.get("library_id")) or "")[:120]
    if payload.get("image_url"):
        token.image_url = str(payload.get("image_url") or "")[:300]
    token_rev = _stamp_token_revision(session, token)
    placed_payload = {
        "token": build_token_runtime_payload(session, token),
        "token_id": token.id,
        "x": token.x,
        "y": token.y,
        "map_context": requested_ctx,
        "client_nonce": payload.get("client_nonce"),
        "token_state_revision": token_rev,
        "visibility_revision": int(getattr(session, "visibility_revision", 0) or 0),
    }
    await _broadcast_token_event(manager, session, "token_placed", placed_payload, token)
    await _broadcast_token_state_sync(session)
    await run_combat_fog_sync(session, reason="token_recalled", map_context=requested_ctx)
    await save_campaign_async(session)
    return True


async def handle_token_move(payload: dict, session: Session, user: User):
    token_id = payload.get("token_id")
    client_action_id = payload.get("client_action_id")
    token = session.tokens.get(token_id)
    # New clients stamp each committed drag move with a per-user/token monotonic
    # sequence.  Keep this server-side so an older in-flight packet can never
    # rewind a newer authoritative position if websocket delivery/handler work
    # is reordered.  Payloads without a sequence remain backward compatible.
    move_seq_raw = payload.get("client_move_seq", payload.get("move_sequence"))
    try:
        move_seq = int(move_seq_raw) if move_seq_raw is not None and str(move_seq_raw).strip() != "" else None
    except Exception:
        move_seq = None
    if move_seq is not None:
        runtime = getattr(session, "_token_move_sequence_runtime", None)
        if not isinstance(runtime, dict):
            runtime = {}
            setattr(session, "_token_move_sequence_runtime", runtime)
        seq_key = f"{getattr(user, 'id', '')}:{token_id}"
        last_seq = int(runtime.get(seq_key) or 0)
        if move_seq < last_seq:
            await _send_action_ack(session, user, action="token_move", client_action_id=client_action_id,
                                    status="stale", token_id=token_id, last_client_move_seq=last_seq)
            return
        runtime[seq_key] = move_seq
    if not token:
        await _send_action_ack(session, user, action="token_move", client_action_id=client_action_id,
                                status="failed", reason="Token not found.")
        return

    assistant_token_control = _assistant_can_control_token(session, user, token)
    if not assistant_token_control and not token.can_move(user.id, user.role) and not (user.role == "player" and _owner_matches_user(getattr(token, "owner_id", ""), user)):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "You don't have permission to move this token."}
        })
        await _send_action_ack(session, user, action="token_move", client_action_id=client_action_id,
                                status="failed", reason="You don't have permission to move this token.")
        return

    try:
        new_x = float(payload.get("x", token.x))
    except Exception:
        new_x = float(getattr(token, "x", 0.0) or 0.0)
    try:
        new_y = float(payload.get("y", token.y))
    except Exception:
        new_y = float(getattr(token, "y", 0.0) or 0.0)

    movement_updated = False
    old_x = float(getattr(token, "x", 0.0) or 0.0)
    old_y = float(getattr(token, "y", 0.0) or 0.0)
    if user.role != "dm" and not assistant_token_control:
        blocker = None
        try:
            blocker = find_movement_blocker(session, str(getattr(token, "map_context", "world") or "world"), float(getattr(token, "x", 0.0) or 0.0), float(getattr(token, "y", 0.0) or 0.0), new_x, new_y)
        except Exception:
            blocker = None
        if blocker:
            await _send_token_move_denied(session, user, token, f"Movement blocked by {str(getattr(blocker, 'source', 'terrain')).replace('_', ' ')}.",
                                           client_action_id=client_action_id)
            return
        allowed = await _enforce_player_combat_movement(session, user, token, new_x, new_y, client_action_id=client_action_id)
        if not allowed:
            return
        movement_updated = bool((getattr(session, "combat", None) or {}).get("movement"))

    heavy_decision = decide_token_move_heavy_work(
        session, token, old_x=old_x, old_y=old_y, new_x=new_x, new_y=new_y, payload=payload
    )

    token.x = new_x
    token.y = new_y

    # The token_moved fan-out is the highest-frequency message in the system: a
    # single drag emits dozens of frames per second. Coalesce repeated moves of
    # the same token into one broadcast per ~50ms window, carrying only the
    # final position, with BOTH revision bumps (visibility + token_state)
    # happening once at flush time. The synchronous state mutation, validation,
    # and the mover's own ack below all stay immediate. Clients still drop
    # stale/out-of-order token_moved events because the counters remain
    # monotonic (one bump per flush). With MOVE_COALESCE_WINDOW_MS=0 this sends
    # immediately, reproducing the legacy one-broadcast-per-frame behavior.
    immediate_token_state_revision = await schedule_token_move_flush(
        session, token,
        lambda: {
            "token_id": token_id,
            "x": token.x,
            "y": token.y,
            "moved_by": user.name,
            # Read at flush time, after the visibility bump, so the coalesced
            # broadcast carries the final monotonic revision (the same value
            # _broadcast_token_event re-stamps). Clients drop stale moves off it.
            "visibility_revision": int(getattr(session, "visibility_revision", 0) or 0),
        },
        exclude_user=user.id,
    )
    # Ack with the synchronously-known position. Report the revisions only as
    # actually broadcast: the immediate (disabled-window) value when we sent
    # now, otherwise the session's current already-broadcast revisions — never a
    # future revision that the coalesced flush has not emitted yet.
    if immediate_token_state_revision is not None:
        ack_token_state_revision = immediate_token_state_revision
    else:
        ack_token_state_revision = int(getattr(session, "token_state_revision", 0) or 0)
    move_revision = int(getattr(session, "visibility_revision", 0) or 0)
    await _send_action_ack(session, user, action="token_move", client_action_id=client_action_id,
                            status="confirmed", token_id=token_id, visibility_revision=move_revision,
                            token_state_revision=ack_token_state_revision, client_move_seq=move_seq)
    if not heavy_decision.run_heavy_work:
        return
    mark_token_move_heavy_work_ran(session, token, map_context=getattr(token, "map_context", "world"))
    # Movement can flip fog/footprint visibility immediately (entering or
    # leaving unrevealed fog); resync per-user so a player who just lost or
    # gained sight of this token gets the add/remove on this committed move,
    # not only after a later unrelated change.
    await _broadcast_token_visibility(session, token, "token_hidden_changed")
    await _process_hazard_triggers_for_token(session, token, trigger='enter', old_x=old_x, old_y=old_y)
    scene_triggered = await _process_scene_triggers_for_token(session, token, user, old_x=old_x, old_y=old_y)
    if scene_triggered:
        await save_campaign_async(session)
    # A moving player vision source can newly reveal fog cells via LOS; this is
    # purely additive on top of manual fog paint (see apply_los_fog_reveal).
    from server.handlers.map_editor import _apply_los_fog_reveal_and_broadcast
    await _apply_los_fog_reveal_and_broadcast(session, str(getattr(token, "map_context", "world") or "world"), reason="token_moved")
    await run_combat_fog_sync(session, reason="token_moved", map_context=getattr(token, "map_context", "world"))
    if movement_updated:
        await _broadcast_combat_move_state(session)


async def handle_token_create(payload: dict, session: Session, user: User):
    session.enforce_single_active_player_token_rule()
    owner_id = payload.get("owner_id")
    if owner_id:
        owner_user = session.users.get(owner_id)
        if not owner_user or owner_user.role != "player":
            owner_id = None
    requested_token_type = str(payload.get("tokenType", payload.get("token_type", "player")) or "player").strip().lower()
    if user.role == "player":
        if owner_id != user.id:
            await manager.send_to(session.id, user.id, {
                "type": "error",
                "payload": {"message": "Players can only create tokens they own."}
            })
            return
        if requested_token_type not in {"player", "companion"}:
            await manager.send_to(session.id, user.id, {
                "type": "error",
                "payload": {"message": "Players can only create their character or a companion token."}
            })
            return
        active_owned_tokens = _player_active_owned_tokens(session, user.id)
        if requested_token_type == "player" and active_owned_tokens:
            if await _move_player_owned_token_from_other_map(payload, session, user, active_owned_tokens):
                return
            await _deny_player_single_token_limit(session, user, token_name="token")
            return
        if requested_token_type == "companion":
            active_companions = [
                tok for tok in _player_active_owned_tokens(session, user.id, include_companions=True)
                if str(getattr(tok, "token_type", "") or "").strip().lower() == "companion"
            ]
            if len(active_companions) >= 8:
                await manager.send_to(session.id, user.id, {
                    "type": "error",
                    "payload": {"message": "You can have up to eight active companion tokens."}
                })
                return
    elif user.role == "viewer":
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Viewers cannot create tokens."}
        })
        return
    if owner_id and requested_token_type != "companion" and not bool(payload.get("staged", False)):
        if _player_active_owned_tokens(session, owner_id):
            await manager.send_to(session.id, user.id, {
                "type": "error",
                "payload": {"message": "That player already has an active token on the field."}
            })
            return

    token_type = requested_token_type
    vision_cfg = _sanitize_token_vision_payload(payload, owner_id=owner_id, token_type=token_type, existing=None)

    token = create_token(
        session=session,
        dm_id=user.id,
        name=payload.get("name", "Token"),
        x=payload.get("x", 100),
        y=payload.get("y", 100),
        color=payload.get("color", "#e74c3c"),
        shape=payload.get("shape", "circle"),
        width=payload.get("width", 40),
        height=payload.get("height", 40),
        owner_id=owner_id,
        hp=payload.get("hp"),
        max_hp=payload.get("maxHp"),
        temp_hp=int(payload.get("tempHp", 0) or 0),
        map_context=payload.get("map_context", "world"),
        hidden_hp=bool(payload.get("hidden_hp", False)),
        hidden=bool(payload.get("hidden", False)),
        initiative_mod=int(payload.get("initiativeMod", 0) or 0),
        ac=(int(payload.get("ac")) if payload.get("ac") is not None else None),
        speed=(int(payload.get("speed")) if payload.get("speed") is not None else None),
        token_type=token_type,
        notes=str(payload.get("notes", "") or ""),
        level=(int(payload.get("level")) if payload.get("level") is not None else None),
        faction=str(payload.get("faction", "") or ""),
        passive_perception=(int(payload.get("passivePerception", payload.get("passive_perception"))) if payload.get("passivePerception", payload.get("passive_perception")) is not None else None),
        staged=bool(payload.get("staged", False)),
        image_url=(str(payload.get("image_url", payload.get("imageUrl", "")) or "")[:300] or None),
        save_bonuses=_sanitize_save_bonuses(payload.get("saveBonuses") or payload.get("save_bonuses") or {}),
        creature_id=str(payload.get("creature_id", payload.get("creatureId", "")) or "")[:120],
        creature_type=str(payload.get("creature_type", payload.get("creatureType", "")) or "")[:40],
        monster_type=str(payload.get("monster_type", payload.get("monsterType", "")) or "")[:60],
        cr=str(payload.get("cr", ""))[:16],
        profile_id=str(payload.get("profile_id", payload.get("profileId", "")) or "")[:120],
        library_id=str(payload.get("libraryId", payload.get("library_id", "")) or "")[:120],
        character_id=str(payload.get("characterId", payload.get("character_id", "")) or "")[:120],
        **vision_cfg,
    )

    log_entry = session.add_log(f"{user.name} created token '{token.name}'.", "system")

    await _broadcast_token_event(
        manager,
        session,
        "token_created",
        {"token": build_token_runtime_payload(session, token), "log": log_entry, "client_nonce": payload.get("client_nonce")},
        token,
    )
    await _broadcast_token_state_sync(session)
    await run_combat_fog_sync(session, reason="token_placed", map_context=getattr(token, "map_context", "world"))
    await save_campaign_async(session)


async def handle_token_delete(payload: dict, session: Session, user: User):
    token_id = payload.get("token_id")
    token = session.tokens.get(token_id)
    if not token:
        return
    owner_user_delete = bool(user.role == "player" and _owner_matches_user(getattr(token, "owner_id", ""), user))
    if user.role != "dm" and not owner_user_delete:
        return
    deleted_map_context = normalize_map_context(getattr(token, "map_context", "world"))
    token_rev = _stamp_token_revision(session, token)
    session.tokens.pop(token_id, None)
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    summon_sync_changed = False
    for owner_key, rows in list(profiles.items()):
        if not isinstance(rows, list):
            continue
        bucket = list(rows)
        for idx, row in enumerate(bucket):
            if not isinstance(row, dict):
                continue
            native = row.get("nativeCharacter") if isinstance(row.get("nativeCharacter"), dict) else {}
            if not native:
                continue
            if synchronize_active_summon_state(native, token_id=str(token_id), remove=True):
                row["nativeCharacter"] = native
                bucket[idx] = row
                summon_sync_changed = True
        profiles[owner_key] = bucket
    if summon_sync_changed:
        session.char_profiles = profiles
    corpse_states = dict(getattr(session, "corpse_states", {}) or {})
    corpse_states.pop(str(token_id), None)
    session.corpse_states = corpse_states
    # A deleted token must also leave any active initiative order — otherwise a
    # ghost combatant lingers in the combat list for the whole table.
    combat_changed = remove_token_from_combat(session, str(token_id))
    if token:
        log_entry = session.add_log(f"{user.name} removed token '{token.name}'.", "system")
        deleted_payload = {
            "token_id": token_id,
            "map_context": deleted_map_context,
            "token_state_revision": token_rev,
            "visibility_revision": int(getattr(session, "visibility_revision", 0) or 0),
            "log": log_entry,
        }
        if token.owner_id:
            await manager.broadcast(session.id, {
                "type": "token_deleted",
                "payload": deleted_payload
            })
        else:
            await manager.broadcast_to_role(session.id, {
                "type": "token_deleted",
                "payload": deleted_payload
            }, {"dm"}, session)
        await _broadcast_token_state_sync(session)
        if combat_changed:
            _bump_combat_revision(session, "token_deleted")
            await _broadcast_combat(session)
        await save_campaign_async(session)


async def handle_token_hp_update(payload: dict, session: Session, user: User):
    token_id = payload.get("token_id")
    token = session.tokens.get(token_id)
    if not token:
        return
    if user.role != "dm" and not _owner_matches_user(getattr(token, "owner_id", ""), user) and not _assistant_can_control_token(session, user, token):
        return
    previous_hp = token.hp
    new_hp = max(0, int(payload.get("hp", token.hp or 0)))
    if "max_hp" in payload and payload["max_hp"]:
        token.max_hp = max(1, int(payload["max_hp"]))
    token.hp = min(new_hp, token.max_hp) if token.max_hp else new_hp
    _persist_token_hp_to_owned_profiles(session, token)
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    summon_sync_changed = False
    for owner_key, rows in list(profiles.items()):
        if not isinstance(rows, list):
            continue
        bucket = list(rows)
        for idx, row in enumerate(bucket):
            if not isinstance(row, dict):
                continue
            native = row.get("nativeCharacter") if isinstance(row.get("nativeCharacter"), dict) else {}
            if not native:
                continue
            if synchronize_active_summon_state(native, token_id=str(token_id), hp_current=int(token.hp or 0), hp_max=int(token.max_hp or 1)):
                row["nativeCharacter"] = native
                bucket[idx] = row
                summon_sync_changed = True
        profiles[owner_key] = bucket
    if summon_sync_changed:
        session.char_profiles = profiles
    if int(token.hp or 0) <= 0:
        _ensure_corpse_state_for_token(session, token)
    combat_changed = _sync_combatant_token_state(session, token, previous_hp=previous_hp)
    log_entry = None
    token_kind = str(getattr(token, "token_type", "player") or "player").strip().lower()
    if token_kind == "player" or getattr(token, "owner_id", None):
        log_msg = f"{user.name} set {token.name} HP to {token.hp}/{token.max_hp}"
        log_entry = session.add_log(log_msg, "system", user.name)
    corpse_state_payload = None
    if int(token.hp or 0) <= 0 and token_kind in {"monster", "npc"}:
        corpse_state_payload = (getattr(session, "corpse_states", None) or {}).get(str(token_id))
    await _broadcast_token_event(manager, session, "token_hp_updated",
        {"token_id": token_id, "hp": token.hp, "maxHp": token.max_hp,
         "hidden_hp": token.hidden_hp, "log": log_entry,
         "corpse_state": corpse_state_payload}, token)
    if combat_changed:
        await _broadcast_combat(session)
    mark_session_dirty(session)
    await save_campaign_async(session)


async def handle_token_edit(payload: dict, session: Session, user: User):
    session.enforce_single_active_player_token_rule()
    token_id = payload.get("token_id")
    token = session.tokens.get(token_id)
    if not token:
        return

    is_dm = user.role == "dm"
    assistant_can_control = _assistant_can_control_token(session, user, token)
    is_owner = _owner_matches_user(getattr(token, "owner_id", ""), user)
    is_owned_companion = bool(is_owner and str(getattr(token, "token_type", "") or "").strip().lower() == "companion")
    if not is_dm and not is_owner and not assistant_can_control:
        return

    if assistant_can_control and not is_dm:
        if payload.get("hp") is not None:
            token.hp = max(0, int(payload.get("hp") or 0))
        if payload.get("maxHp") is not None:
            token.max_hp = max(1, int(payload.get("maxHp") or 1))
        if payload.get("x") is not None:
            token.x = float(payload.get("x"))
        if payload.get("y") is not None:
            token.y = float(payload.get("y"))

    if is_dm:
        if payload.get("name"):   token.name   = payload["name"]
        if payload.get("color"):  token.color  = payload["color"]
        if "owner_id" in payload:
            new_owner_id = payload.get("owner_id")
            if new_owner_id:
                owner_user = session.users.get(new_owner_id)
                if owner_user and owner_user.role == "player":
                    if not bool(getattr(token, "staged", False)) and _player_active_owned_tokens(session, new_owner_id, exclude_token_id=token.id):
                        await manager.send_to(session.id, user.id, {
                            "type": "error",
                            "payload": {"message": "That player already has an active token on the field."}
                        })
                        return
                    token.owner_id = new_owner_id
                else:
                    token.owner_id = None
            else:
                token.owner_id = None
        if payload.get("width"):  token.width  = float(payload["width"])
        if payload.get("height"): token.height = float(payload["height"])
        if payload.get("maxHp"):  token.max_hp = int(payload["maxHp"])
        if payload.get("hp") is not None:
            token.hp = max(0, int(payload["hp"]))
        token.hidden_hp = bool(payload.get("hidden_hp", token.hidden_hp))
        if "hidden" in payload:
            token.hidden = bool(payload["hidden"])
        if "tokenType" in payload or "token_type" in payload:
            token.token_type = str(payload.get("tokenType", payload.get("token_type", token.token_type)) or "player")

    if is_owned_companion and not is_dm:
        if payload.get("name"):
            token.name = str(payload.get("name"))[:80]
        if payload.get("color"):
            token.color = str(payload.get("color"))[:32]
        if payload.get("maxHp") is not None:
            token.max_hp = max(1, _safe_int(payload.get("maxHp"), getattr(token, "max_hp", 1), minimum=1))
        if payload.get("hp") is not None:
            token.hp = min(token.max_hp, _safe_int(payload.get("hp"), getattr(token, "hp", 0), minimum=0))
        if payload.get("width") is not None:
            token.width = max(10.0, min(400.0, float(payload.get("width"))))
        if payload.get("height") is not None:
            token.height = max(10.0, min(400.0, float(payload.get("height"))))

    if "tempHp" in payload:
        token.temp_hp = max(0, int(payload.get("tempHp", 0) or 0))
    if "initiativeMod" in payload:
        token.initiative_mod = int(payload.get("initiativeMod", 0) or 0)
    if "ac" in payload:
        token.ac = int(payload["ac"]) if payload.get("ac") is not None else None
        token.ac_from_equipment = False  # manually set — equipment should not overwrite
    if "speed" in payload:
        token.speed = int(payload["speed"]) if payload.get("speed") is not None else None
    if "notes" in payload:
        token.notes = str(payload.get("notes", "") or "")[:2000]
    if "level" in payload:
        token.level = int(payload["level"]) if payload.get("level") is not None else None
    if "faction" in payload:
        token.faction = str(payload.get("faction", "") or "")[:100]
    if "passivePerception" in payload or "passive_perception" in payload:
        _pp = payload.get("passivePerception", payload.get("passive_perception"))
        token.passive_perception = int(_pp) if _pp is not None else None
    if "image_url" in payload or "imageUrl" in payload:
        token.image_url = (str(payload.get("image_url", payload.get("imageUrl", "")) or "")[:300] or None)
    if "saveBonuses" in payload or "save_bonuses" in payload:
        token.save_bonuses = _sanitize_save_bonuses(payload.get("saveBonuses") or payload.get("save_bonuses") or {})
    if is_dm and ("creature_id" in payload or "creatureId" in payload):
        token.creature_id = str(payload.get("creature_id", payload.get("creatureId", "")) or "")[:120]
    if is_dm and ("creature_type" in payload or "creatureType" in payload):
        token.creature_type = str(payload.get("creature_type", payload.get("creatureType", "")) or "")[:40]
    if is_dm and ("monster_type" in payload or "monsterType" in payload):
        token.monster_type = str(payload.get("monster_type", payload.get("monsterType", "")) or "")[:60]
    if is_dm and "cr" in payload:
        token.cr = str(payload.get("cr", "") or "")[:16]
    if any(k in payload for k in ("profile_id", "profileId")):
        token.profile_id = str(payload.get("profile_id", payload.get("profileId", "")) or "")[:120]
    if any(k in payload for k in ("libraryId", "library_id")):
        token.library_id = str(payload.get("libraryId", payload.get("library_id", "")) or "")[:120]
    if any(k in payload for k in ("characterId", "character_id")):
        token.character_id = str(payload.get("characterId", payload.get("character_id", "")) or "")[:120]

    if is_dm and any(k in payload for k in ("visionEnabled", "vision_enabled", "visionRadius", "vision_radius", "brightRadius", "bright_radius", "dimRadius", "dim_radius", "hasDarkvision", "has_darkvision", "darkvisionRadius", "darkvision_radius", "owner_id", "tokenType", "token_type")):
        vision_cfg = _sanitize_token_vision_payload(payload, owner_id=token.owner_id, token_type=token.token_type, existing=token)
        token.vision_enabled = vision_cfg['vision_enabled']
        token.vision_radius = vision_cfg['vision_radius']
        token.bright_radius = vision_cfg['bright_radius']
        token.dim_radius = vision_cfg['dim_radius']
        token.has_darkvision = vision_cfg['has_darkvision']
        token.darkvision_radius = vision_cfg['darkvision_radius']

    if any(k in payload for k in ("hp", "maxHp", "max_hp", "tempHp")):
        _persist_token_hp_to_owned_profiles(session, token)

    combat_changed = _sync_combatant_token_state(session, token, previous_hp=None)
    await _broadcast_token_visibility(session, token, "token_hidden_changed")
    await _broadcast_token_state_sync(session)
    if combat_changed:
        await _broadcast_combat(session)
    if int(getattr(token, "hp", 1) or 0) <= 0:
        _ensure_corpse_state_for_token(session, token)
    await save_campaign_async(session)


async def handle_token_placed(payload: dict, session: Session, user: User):
    """Token moved from staging panel onto the map — update position + map_context, broadcast to all."""
    session.enforce_single_active_player_token_rule()
    token_id = payload.get("token_id")
    token = session.tokens.get(token_id)
    if not token:
        return
    if user.role == "player" and not _owner_matches_user(getattr(token, "owner_id", ""), user):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "You can only place your own token from staging."}
        })
        return
    owner_id = str(getattr(token, "owner_id", "") or "").strip()
    if owner_id and _player_active_owned_tokens(session, owner_id, exclude_token_id=token.id):
        msg = "That player already has an active token on the field."
        if user.role == "player" and _owner_matches_user(owner_id, user):
            await _deny_player_single_token_limit(session, user, token_name="token")
        else:
            await manager.send_to(session.id, user.id, {
                "type": "error",
                "payload": {"message": msg}
            })
        return
    token.x = payload.get("x", token.x)
    token.y = payload.get("y", token.y)
    token.map_context = payload.get("map_context", token.map_context)
    token.staged = False
    token_rev = _stamp_token_revision(session, token)

    placed_payload = {
        **payload,
        "token": build_token_runtime_payload(session, token),
        "token_id": token.id,
        "map_context": normalize_map_context(getattr(token, "map_context", "world")),
        "token_state_revision": token_rev,
        "visibility_revision": int(getattr(session, "visibility_revision", 0) or 0),
    }
    await manager.broadcast(session.id, {"type": "token_placed", "payload": placed_payload})
    await _broadcast_token_state_sync(session)
    await save_campaign_async(session)


async def handle_token_send_to_staging(payload: dict, session: Session, user: User):
    """Move a token into the waiting-to-place tray without changing its owning map."""
    token_id = payload.get("token_id")
    token = session.tokens.get(token_id)
    if not token:
        return
    if user.role != "dm" and token.owner_id != user.id and not _assistant_can_control_token(session, user, token):
        return

    token.staged = True
    # A staged token (e.g. a hidden ambush NPC the DM is pulling off the map,
    # or another player's token) must only reach clients who can still see
    # it (DM, or the owning player) — never an unconditional broadcast.
    await _broadcast_token_visibility(session, token, "token_sent_to_staging")
    await _broadcast_token_state_sync(session)
    await run_combat_fog_sync(session, reason="token_staged", map_context=getattr(token, "map_context", "world"))
    await save_campaign_async(session)


async def handle_toggle_hidden(payload: dict, session: Session, user: User):
    """DM toggles a token's hidden-from-players state."""
    if user.role != 'dm':
        return
    token_id = payload.get('token_id')
    token = session.tokens.get(token_id)
    if not token:
        return
    token.hidden = bool(payload.get('hidden', not token.hidden))
    await _broadcast_token_visibility(session, token, "token_hidden_changed")
    await run_combat_fog_sync(session, reason="token_hidden_changed", map_context=getattr(token, "map_context", "world"))
    await save_campaign_async(session)


async def handle_token_condition(payload: dict, session: Session, user: User):
    token_id = payload.get("token_id")
    condition = payload.get("condition")
    token = session.tokens.get(token_id)
    if not token or not condition:
        return
    if user.role != "dm" and token.owner_id != user.id:
        return
    _prune_token_condition_timers(token)
    if condition in (getattr(token, 'conditions', None) or []):
        _clear_token_condition(token, condition)
    else:
        _set_token_condition(token, condition, 0)
    await save_campaign_async(session)
    await _broadcast_token_condition_state(session, token)


async def handle_char_hp_update(payload: dict, session: Session, user: User):
    """Broadcast HP change to all connected users (DM + other players can see)."""
    await manager.broadcast(session.id, {
        "type": "char_hp_update",
        "payload": payload,
    })


_VALID_MARK_KINDS = {"hunters_mark", "hex"}


async def handle_mark_target(payload: dict, session: Session, user: User):
    """Player places a Hunter's Mark or Hex marker on a target token.

    Outside combat: applied immediately.
    During combat: only allowed on the player's own turn.
    """
    if user.role not in ("player", "dm"):
        return

    mark_kind = str(payload.get("mark_kind") or "").strip().lower()
    if mark_kind not in _VALID_MARK_KINDS:
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Invalid mark kind."}
        })
        return

    target_token_id = str(payload.get("target_token_id") or "").strip()
    token = (session.tokens or {}).get(target_token_id)
    if not token or getattr(token, "hidden", False):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Choose a visible target token."}
        })
        return

    from server.handlers.common import _token_map_context
    map_context = _token_map_context(token)
    if map_context != str(getattr(session, "dm_map_context", "world") or "world"):
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "That target is not on the active map."}
        })
        return

    combat = getattr(session, "combat", None) or {}
    if combat.get("active"):
        allowed, err_msg = _can_act_current_turn(session, user)
        if not allowed and user.role != "dm":
            await manager.send_to(session.id, user.id, {
                "type": "error",
                "payload": {"message": err_msg or "You must wait for your turn to place a mark."}
            })
            return

    _prune_token_condition_timers(token)
    _set_token_condition(token, mark_kind, 0)
    await save_campaign_async(session)
    await _broadcast_token_condition_state(session, token)

    mark_label = "Hunter's Mark" if mark_kind == "hunters_mark" else "Hex"
    actor_name = user.name or "Player"
    session.add_log(f"{actor_name} placed {mark_label} on {token.name}.", "system", actor_name)
    await manager.broadcast(session.id, {
        "type": "mark_target_result",
        "payload": {
            "ok": True,
            "message": f"{mark_label} placed on {token.name}.",
            "mark_kind": mark_kind,
            "token_id": token.id,
            "token_name": token.name,
        }
    })


async def handle_token_emote(payload: dict, session: Session, user: User):
    """Broadcast a short-lived token-side emote for a player-owned token."""
    if user.role not in ("player", "dm"):
        return

    token_id = str(payload.get("token_id") or "").strip()
    emote_id = str(payload.get("emote_id") or "").strip().lower()
    token = session.tokens.get(token_id)
    emote_def = _TOKEN_EMOTE_DEFS.get(emote_id)
    if not token or not emote_def:
        return

    if user.role != "dm":
        if str(getattr(token, "owner_id", "") or "").strip() != str(user.id):
            await manager.send_to(session.id, user.id, {
                "type": "token_emote_denied",
                "payload": {"token_id": token_id, "message": "You can only emote on your own token."}
            })
            return

    now = time.time()
    last_used = float(getattr(user, "last_token_emote_at", 0.0) or 0.0)
    remaining = _TOKEN_EMOTE_COOLDOWN_SEC - (now - last_used)
    if remaining > 0:
        await manager.send_to(session.id, user.id, {
            "type": "token_emote_denied",
            "payload": {
                "token_id": token_id,
                "message": "Quick reaction is cooling down.",
                "cooldown_remaining_ms": int(max(1, round(remaining * 1000))),
            }
        })
        return
    user.last_token_emote_at = now

    await manager.broadcast(session.id, {
        "type": "token_emote",
        "payload": {
            "token_id": token_id,
            "emote_id": emote_id,
            "icon": emote_def["icon"],
            "label": emote_def["label"],
            "actor_user_id": user.id,
            "actor_name": user.name,
            "expires_at": now + _TOKEN_EMOTE_TTL_SEC,
            "map_context": str(getattr(token, "map_context", "world") or "world"),
        }
    })
