from __future__ import annotations

import copy
from typing import Any

from server.editor_schema import canonical_weather_type, normalize_map_settings
from server.map_document import build_map_documents_from_session, normalize_map_documents
from server.faction_reputation import normalize_faction_reputation_state


PERSISTED_LIST_FIELDS = {
    "journal_entries",
    "codex_entries",
    "codex_links",
    "library_entries",
    "item_library_entries",
    "party_loot_log",
    "party_memory_log",
    "handouts",
    "discovery_cards",
    "private_story_hooks",
    "encounter_templates",
    "quest_templates",
    "session_quests",
    "quest_board_bindings",
}

PERSISTED_DICT_FIELDS = {
    "editor_layers",
    "editor_walls",
    "editor_props",
    "map_settings",
    "editor_paths",
    "editor_labels",
    "editor_markers",
    "editor_lights",
    "map_documents",
}



def _clone(value: Any) -> Any:
    return copy.deepcopy(value)



def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}



def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []



def _safe_text(value: Any, default: str = "", limit: int = 120) -> str:
    text = str(value or default).strip()[:limit]
    return text or default



def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        num = float(value)
    except Exception:
        num = default
    return max(minimum, min(maximum, num))



def normalize_combat_state(raw: Any) -> dict:
    src = _as_dict(raw)
    combatants = _as_list(src.get("combatants"))
    suspended = _as_list(src.get("suspended_combatants"))
    fog_suspended = _as_list(src.get("fog_suspended_combatants"))
    hidden_suspended = _as_list(src.get("hidden_suspended_combatants"))
    return {
        "active": bool(src.get("active", False)),
        "turn": int(src.get("turn", 0) or 0),
        "combatants": combatants,
        "suspended_combatants": suspended,
        "fog_suspended_combatants": fog_suspended,
        "hidden_suspended_combatants": hidden_suspended,
        "round": int(src.get("round", 1) or 1) if src.get("round") is not None else 1,
        "movement": _as_dict(src.get("movement")),
        "selected_target_id": src.get("selected_target_id"),
        "pending_attack": _as_dict(src.get("pending_attack")) if isinstance(src.get("pending_attack"), dict) else None,
    }



def normalize_fog_maps(raw: Any) -> dict:
    src = _as_dict(raw)
    normalized = {}
    for ctx, entry in src.items():
        payload = _as_dict(entry)
        cells = payload.get("cells", "")
        if isinstance(cells, (list, bytes, bytearray)):
            cells = "".join(str(int(bool(cell))) for cell in cells)
        elif not isinstance(cells, str):
            cells = ""
        cols = int(payload.get("cols", 64) or 64)
        rows = int(payload.get("rows", 64) or 64)
        # Allow up to cols*rows characters so large maps (e.g. 128×128 = 16384
        # cells) are not silently truncated. Cap at a generous upper bound to
        # prevent unbounded storage from malformed input.
        max_cells = max(4096, cols * rows)
        try:
            revision = int(payload.get("revision", 0) or 0)
        except Exception:
            revision = 0
        try:
            updated_at = float(payload.get("updated_at", 0.0) or 0.0)
        except Exception:
            updated_at = 0.0
        ctx_key = str(ctx or "world")[:80] or "world"
        normalized[ctx_key] = {
            "enabled": bool(payload.get("enabled", False)),
            "cols": cols,
            "rows": rows,
            "cells": cells[:max_cells],
            "revision": revision,
            "updated_at": updated_at,
            "map_context": ctx_key,
        }
    return normalized



def normalize_sound_state(raw: Any) -> dict:
    src = _as_dict(raw)
    track = _safe_text(src.get("track"), "silence", 32).lower()
    if not track:
        track = "silence"
    state = {
        "track": track,
        "volume": _clamp_float(src.get("volume", 0.7), 0.7, 0.0, 1.0),
        "fade_ms": max(0, min(5000, int(src.get("fade_ms", 800) or 800))),
    }
    pre_combat = src.get("pre_combat_track")
    if pre_combat not in (None, ""):
        state["pre_combat_track"] = _safe_text(pre_combat, "silence", 32).lower()
    return state



def normalize_weather_state(raw: Any) -> dict:
    src = _as_dict(raw)
    # Accept either the legacy {type, wind} editor shape or the runtime
    # {weather_type, wind_speed, wind_angle} shape; unknown types → "none".
    canon = canonical_weather_type(src.get("weather_type", src.get("type", "none")))
    return {
        "weather_type": canon,
        "intensity": _clamp_float(src.get("intensity", 0.5), 0.5, 0.0, 1.0),
        "wind_angle": _clamp_float(src.get("wind_angle", src.get("windAngle", 0.0)), 0.0, 0.0, 360.0),
        "wind_speed": _clamp_float(src.get("wind_speed", src.get("wind", 0.3)), 0.3, 0.0, 1.0),
        "darkness": _clamp_float(src.get("darkness", 0.0), 0.0, 0.0, 1.0),
        "lightning_frequency": _clamp_float(
            src.get("lightning_frequency", src.get("lightningFrequency", 0.5)), 0.5, 0.0, 1.0
        ),
        "audio_linked": bool(src.get("audio_linked", src.get("audioLinked", True))),
        "map_context": _safe_text(src.get("map_context"), "world", 80),
    }



def normalize_active_poll(raw: Any) -> dict | None:
    src = _as_dict(raw)
    question = _safe_text(src.get("question"), "", 300)
    options = [str(opt).strip()[:200] for opt in _as_list(src.get("options")) if str(opt).strip()][:4]
    if not src or not question or len(options) < 2:
        return None
    votes = {}
    for user_id, opt_idx in _as_dict(src.get("votes")).items():
        try:
            opt_index = int(opt_idx)
        except Exception:
            continue
        if 0 <= opt_index < len(options):
            votes[str(user_id)[:64]] = opt_index
    created_at = float(src.get("created_at") or 0.0)
    raw_closes_at = src.get("closes_at")
    closes_at = float(raw_closes_at or 0.0) if raw_closes_at not in (None, "", False) else None
    if closes_at is not None and closes_at < created_at:
        closes_at = created_at
    results_mode = str(src.get("results_mode") or "live").strip().lower()
    if results_mode not in {"live", "final"}:
        results_mode = "live"
    closed = bool(src.get("closed", False))
    return {
        "id": _safe_text(src.get("id"), "", 32),
        "title": _safe_text(src.get("title"), "Party Vote", 120),
        "question": question,
        "options": options,
        "votes": votes,
        "created_at": created_at,
        "closes_at": closes_at,
        "closed": closed,
        "results_mode": results_mode,
        "authority_note": _safe_text(src.get("authority_note"), "The DM keeps final say.", 160),
        "closed_by": _safe_text(src.get("closed_by"), "dm", 24),
        "closed_reason": _safe_text(src.get("closed_reason"), ("dm_closed" if closed else "active"), 40),
    }



def normalize_editor_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = raw or {}
    normalized = {field: (_as_dict(src.get(field)) if field != "map_settings" else {}) for field in PERSISTED_DICT_FIELDS if field != "map_documents"}
    normalized["map_settings"] = {
        str(ctx or "world")[:80] or "world": normalize_map_settings(settings)
        for ctx, settings in _as_dict(src.get("map_settings")).items()
    }
    normalized["map_documents"] = normalize_map_documents(src.get("map_documents"))
    return normalized


def normalize_encounter_templates(raw: Any) -> list[dict[str, Any]]:
    templates = []
    for item in _as_list(raw)[:100]:
        src = _as_dict(item)
        name = _safe_text(src.get("name"), "Encounter Template", 120)
        entries = []
        for entry in _as_list(src.get("entries"))[:40]:
            row = _as_dict(entry)
            creature_id = _safe_text(row.get("creature_id") or row.get("canonical_creature_id"), "", 80)
            if not creature_id:
                continue
            qty = max(1, min(20, int(row.get("qty", 1) or 1)))
            entries.append({
                "creature_id": creature_id,
                "canonical_creature_id": creature_id,
                "name": _safe_text(row.get("name"), "Creature", 120),
                "qty": qty,
                "source": _safe_text(row.get("source"), "custom", 32).lower(),
                "source_type": _safe_text(row.get("source_type") or row.get("source"), "custom", 32).lower(),
                "entry_type": _safe_text(row.get("entry_type") or row.get("creature_type"), "monster", 24).lower(),
                "creature_type": _safe_text(row.get("creature_type") or row.get("entry_type"), "monster", 24).lower(),
                "monster_type": _safe_text(row.get("monster_type"), "", 40).lower(),
                "cr": _safe_text(row.get("cr"), "", 16),
            })
        templates.append({
            "id": _safe_text(src.get("id"), "", 48),
            "name": name,
            "notes": _safe_text(src.get("notes"), "", 400),
            "map_context": _safe_text(src.get("map_context"), "world", 80),
            "created_at": float(src.get("created_at") or 0.0),
            "updated_at": float(src.get("updated_at") or 0.0),
            "entries": entries,
        })
    return templates


def normalize_discovery_cards(raw: Any) -> list[dict[str, Any]]:
    cards = []
    for item in _as_list(raw)[:300]:
        src = _as_dict(item)
        visibility = _safe_text(src.get("visibility"), "private_player", 32).lower()
        if visibility not in {"private_player", "party_public", "dm_reveal_later"}:
            visibility = "private_player"
        target_user_id = _safe_text(src.get("target_user_id"), "", 64) if visibility == "private_player" else ""
        acknowledged_by = [_safe_text(uid, "", 64) for uid in _as_list(src.get("acknowledged_by")) if _safe_text(uid, "", 64)]
        saved_by = [_safe_text(uid, "", 64) for uid in _as_list(src.get("saved_by")) if _safe_text(uid, "", 64)]
        meta = _as_dict(src.get("meta"))
        cards.append({
            "id": _safe_text(src.get("id"), "", 48),
            "title": _safe_text(src.get("title"), "New Discovery", 120),
            "body": _safe_text(src.get("body"), "", 1200),
            "kind": _safe_text(src.get("kind"), "clue", 40).lower(),
            "icon": _safe_text(src.get("icon"), "", 8),
            "tone": _safe_text(src.get("tone"), "mystic", 24).lower(),
            "source": _safe_text(src.get("source"), "dm", 80),
            "visibility": visibility,
            "target_user_id": target_user_id,
            "allow_player_share": bool(src.get("allow_player_share", False)),
            "can_acknowledge": bool(src.get("can_acknowledge", True)),
            "can_save": bool(src.get("can_save", True)),
            "can_share": bool(src.get("can_share", False)),
            "created_at": float(src.get("created_at") or 0.0),
            "created_by_user_id": _safe_text(src.get("created_by_user_id"), "", 64),
            "created_by_name": _safe_text(src.get("created_by_name"), "", 120),
            "revealed_at": float(src.get("revealed_at") or 0.0) if src.get("revealed_at") not in (None, "") else None,
            "acknowledged_by": acknowledged_by,
            "saved_by": saved_by,
            "shared_at": float(src.get("shared_at") or 0.0) if src.get("shared_at") not in (None, "") else None,
            "meta": {
                "scope": _safe_text(meta.get("scope"), "discovery_card", 32),
                "audience": _safe_text(meta.get("audience"), "player", 24),
                "ui_channel": _safe_text(meta.get("ui_channel"), "discovery_card", 32),
                "kind": _safe_text(meta.get("kind"), _safe_text(src.get("kind"), "clue", 40), 40).lower(),
                "visibility": _safe_text(meta.get("visibility"), visibility, 32).lower(),
            },
        })
    return cards


def normalize_private_story_hooks(raw: Any) -> list[dict[str, Any]]:
    hooks = []
    for item in _as_list(raw)[:400]:
        src = _as_dict(item)
        kind = _safe_text(src.get("kind"), "prompt", 24).lower()
        if kind not in {"prompt", "objective"}:
            kind = "prompt"
        status = _safe_text(src.get("status"), "active", 24).lower()
        if status not in {"active", "resolved"}:
            status = "active"
        target_user_id = _safe_text(src.get("target_user_id"), "", 64)
        if not target_user_id:
            continue
        hooks.append({
            "id": _safe_text(src.get("id"), "", 48),
            "target_user_id": target_user_id,
            "title": _safe_text(src.get("title"), "", 140),
            "body": _safe_text(src.get("body"), "", 1600),
            "kind": kind,
            "status": status,
            "persistent": bool(src.get("persistent", kind == "objective")),
            "one_off": bool(src.get("one_off", kind == "prompt")),
            "tone": _safe_text(src.get("tone"), "personal", 24).lower(),
            "source": _safe_text(src.get("source"), "dm_manual", 80),
            "created_at": float(src.get("created_at") or 0.0),
            "updated_at": float(src.get("updated_at") or 0.0),
            "resolved_at": float(src.get("resolved_at") or 0.0) if src.get("resolved_at") not in (None, "") else None,
            "created_by_user_id": _safe_text(src.get("created_by_user_id"), "", 64),
            "created_by_name": _safe_text(src.get("created_by_name"), "", 120),
            "meta": {
                "scope": _safe_text(_as_dict(src.get("meta")).get("scope"), "private_story_hook", 32),
                "audience": _safe_text(_as_dict(src.get("meta")).get("audience"), "player", 24),
                "ui_channel": _safe_text(_as_dict(src.get("meta")).get("ui_channel"), "private_story_hook", 32),
                "kind": kind,
                "status": status,
            },
        })
    return hooks


def _normalize_quest_link_ids(raw: Any, max_items: int = 200, max_len: int = 80) -> list[str]:
    values = []
    for entry in _as_list(raw)[:max_items]:
        text = _safe_text(entry, "", max_len)
        if text:
            values.append(text)
    return values


def _normalize_quest_objectives(raw: Any) -> list[dict[str, Any]]:
    objectives = []
    for item in _as_list(raw)[:200]:
        src = _as_dict(item)
        objectives.append({
            "id": _safe_text(src.get("id"), "", 64),
            "title": _safe_text(src.get("title"), "", 200),
            "description": _safe_text(src.get("description"), "", 600),
            "status": _safe_text(src.get("status"), "pending", 24).lower(),
            "required": bool(src.get("required", True)),
            "order": max(0, int(src.get("order", 0) or 0)),
            "progress": _as_dict(src.get("progress")),
            "meta": _as_dict(src.get("meta")),
        })
    return objectives


def _normalize_quest_stages(raw: Any) -> list[dict[str, Any]]:
    stages = []
    for item in _as_list(raw)[:100]:
        src = _as_dict(item)
        stages.append({
            "id": _safe_text(src.get("id"), "", 64),
            "name": _safe_text(src.get("name"), "", 140),
            "description": _safe_text(src.get("description"), "", 600),
            "order": max(0, int(src.get("order", 0) or 0)),
            "status": _safe_text(src.get("status"), "locked", 24).lower(),
            "objective_ids": _normalize_quest_link_ids(src.get("objective_ids"), max_items=200, max_len=64),
            "meta": _as_dict(src.get("meta")),
        })
    return stages


def _normalize_reward_bundle(raw: Any) -> dict[str, Any]:
    src = _as_dict(raw)
    raw_rep = src.get("reputation")
    if isinstance(raw_rep, list):
        reputation: list[dict[str, Any]] = []
        for entry in raw_rep[:30]:
            row = _as_dict(entry)
            amount = int(row.get("delta") or 0)
            if not amount:
                continue
            faction_name = _safe_text(row.get("name") or row.get("faction"), "", 80)
            faction_id = _safe_text(row.get("id"), "", 64)
            if not faction_name and not faction_id:
                continue
            visibility = _safe_text(row.get("visibility"), "party", 16).lower()
            if visibility not in {"party", "dm_only"}:
                visibility = "party"
            reputation.append({
                "id": faction_id,
                "name": faction_name,
                "tag": _safe_text(row.get("tag"), "", 48),
                "delta": amount,
                "visibility": visibility,
            })
    else:
        reputation = _as_dict(raw_rep)
    return {
        "xp": max(0, int(src.get("xp", 0) or 0)),
        "gold": max(0, int(src.get("gold", 0) or 0)),
        "items": _as_list(src.get("items"))[:200],
        "reputation": reputation,
        "unlock_ids": _normalize_quest_link_ids(src.get("unlock_ids"), max_items=100, max_len=64),
        "meta": _as_dict(src.get("meta")),
    }


def normalize_quest_entries(raw: Any) -> list[dict[str, Any]]:
    quests = []
    for item in _as_list(raw)[:500]:
        src = _as_dict(item)
        visibility = _as_dict(src.get("visibility"))
        source_type = _safe_text(src.get("source_type"), "custom", 16).lower()
        if source_type not in {"premade", "custom"}:
            source_type = "custom"
        quests.append({
            "id": _safe_text(src.get("id"), "", 64),
            "template_id": _safe_text(src.get("template_id"), "", 64),
            "title": _safe_text(src.get("title"), "", 180),
            "summary": _safe_text(src.get("summary"), "", 400),
            "description": _safe_text(src.get("description"), "", 4000),
            "category": _safe_text(src.get("category"), "general", 40).lower(),
            "difficulty": _safe_text(src.get("difficulty"), "", 40).lower(),
            "tier": _safe_text(src.get("tier"), "", 40).lower(),
            "status": _safe_text(src.get("status"), "draft", 24).lower(),
            "source_type": source_type,
            "faction_tags": _normalize_quest_link_ids(src.get("faction_tags"), max_items=50, max_len=48),
            "required_faction_tags": _normalize_quest_link_ids(src.get("required_faction_tags"), max_items=50, max_len=48),
            "required_guild_rank_id": _safe_text(src.get("required_guild_rank_id"), "", 40).lower(),
            "required_guild_rank_points": max(0, int(src.get("required_guild_rank_points", 0) or 0)),
            "guild_tags": _normalize_quest_link_ids(src.get("guild_tags"), max_items=50, max_len=48),
            "prerequisite_quest_ids": _normalize_quest_link_ids(src.get("prerequisite_quest_ids"), max_items=100, max_len=64),
            "unlocks_quest_ids": _normalize_quest_link_ids(src.get("unlocks_quest_ids"), max_items=100, max_len=64),
            "hidden_until_unlocked": bool(src.get("hidden_until_unlocked", False)),
            "lock_visibility": _safe_text(src.get("lock_visibility"), "listed", 16).lower(),
            "visibility": {
                "mode": _safe_text(visibility.get("mode"), "dm_only", 24).lower(),
                "roles": _normalize_quest_link_ids(visibility.get("roles"), max_items=6, max_len=24),
                "player_ids": _normalize_quest_link_ids(visibility.get("player_ids"), max_items=50, max_len=64),
                "hidden_objective_ids": _normalize_quest_link_ids(visibility.get("hidden_objective_ids"), max_items=200, max_len=64),
            },
            "prerequisites": _as_dict(src.get("prerequisites")),
            "linked_poi_ids": _normalize_quest_link_ids(src.get("linked_poi_ids")),
            "linked_map_ids": _normalize_quest_link_ids(src.get("linked_map_ids")),
            "linked_npc_ids": _normalize_quest_link_ids(src.get("linked_npc_ids")),
            "linked_handout_ids": _normalize_quest_link_ids(src.get("linked_handout_ids")),
            "linked_encounter_ids": _normalize_quest_link_ids(src.get("linked_encounter_ids")),
            "linked_monster_ids": _normalize_quest_link_ids(src.get("linked_monster_ids")),
            "objective_list": _normalize_quest_objectives(src.get("objective_list")),
            "reward_bundle": _normalize_reward_bundle(src.get("reward_bundle")),
            "stage_list": _normalize_quest_stages(src.get("stage_list")),
            "current_stage_id": _safe_text(src.get("current_stage_id"), "", 64),
            "progress": _as_dict(src.get("progress")),
            "chain": _as_dict(src.get("chain")),
            "board_binding_id": _safe_text(src.get("board_binding_id"), "", 64),
            "campaign_id": _safe_text(src.get("campaign_id"), "", 80),
            "session_id": _safe_text(src.get("session_id"), "", 80),
            "published_at": float(src.get("published_at") or 0.0) if src.get("published_at") not in (None, "") else None,
            "created_by_user_id": _safe_text(src.get("created_by_user_id"), "", 64),
            "created_at": float(src.get("created_at") or 0.0),
            "updated_at": float(src.get("updated_at") or 0.0),
            "meta": _as_dict(src.get("meta")),
        })
    return quests


def normalize_quest_board_bindings(raw: Any) -> list[dict[str, Any]]:
    bindings = []
    for item in _as_list(raw)[:200]:
        src = _as_dict(item)
        bindings.append({
            "id": _safe_text(src.get("id"), "", 64),
            "board_type": _safe_text(src.get("board_type"), "guild_board", 40).lower(),
            "board_id": _safe_text(src.get("board_id"), "", 80),
            "prop_id": _safe_text(src.get("prop_id"), "", 80),
            "poi_id": _safe_text(src.get("poi_id"), "", 80),
            "map_context": _safe_text(src.get("map_context"), "world", 80),
            "quest_ids": _normalize_quest_link_ids(src.get("quest_ids"), max_items=500, max_len=64),
            "visibility": _as_dict(src.get("visibility")),
            "status": _safe_text(src.get("status"), "active", 24).lower(),
            "created_at": float(src.get("created_at") or 0.0),
            "updated_at": float(src.get("updated_at") or 0.0),
            "meta": _as_dict(src.get("meta")),
        })
    return bindings


def normalize_world_state(raw: Any) -> dict[str, Any]:
    src = _as_dict(raw)
    history = []
    for item in _as_list(src.get("world_change_history"))[:1000]:
        entry = _as_dict(item)
        history.append({
            "id": _safe_text(entry.get("id"), "", 64),
            "ts": float(entry.get("ts") or 0.0),
            "kind": _safe_text(entry.get("kind"), "", 48).lower(),
            "scope": _safe_text(entry.get("scope"), "", 80),
            "ref_id": _safe_text(entry.get("ref_id"), "", 80),
            "summary": _safe_text(entry.get("summary"), "", 400),
            "meta": _as_dict(entry.get("meta")),
        })
    recent_events = []
    for item in _as_list(src.get("recent_events"))[:50]:
        entry = _as_dict(item)
        recent_events.append({
            "id": _safe_text(entry.get("id"), "", 64),
            "event_type": _safe_text(entry.get("event_type"), "world_state_flag_set", 64).lower(),
            "ts": float(entry.get("ts") or 0.0),
            "source": _safe_text(entry.get("source"), "", 120),
            "actor_user_id": _safe_text(entry.get("actor_user_id"), "", 64),
            "summary": _safe_text(entry.get("summary"), "", 280),
            "quest_id": _safe_text(entry.get("quest_id"), "", 64),
            "handout_id": _safe_text(entry.get("handout_id"), "", 80),
            "discovery_id": _safe_text(entry.get("discovery_id"), "", 80),
            "faction_id": _safe_text(entry.get("faction_id"), "", 64),
            "guild_rank_id": _safe_text(entry.get("guild_rank_id"), "", 64),
            "meta": _as_dict(entry.get("meta")),
        })
    # Imported lazily to avoid a circular import at module load: server.session
    # pulls in server.character -> ... -> server.db -> server.persistence_schema,
    # so importing from server.session at the top level would deadlock the chain.
    from server.session import normalize_scene_trigger_zone

    trigger_zones: dict[str, dict[str, Any]] = {}
    for _, item in _as_dict(src.get("scene_trigger_zones")).items():
        zone = normalize_scene_trigger_zone(item)
        if zone:
            trigger_zones[str(zone.get("id") or "")] = zone
    trigger_runtime_src = _as_dict(src.get("scene_trigger_runtime"))
    consumed_zone_ids = _normalize_quest_link_ids(trigger_runtime_src.get("consumed_zone_ids"), max_items=1000, max_len=64)
    last_trigger_at = {}
    for key, value in _as_dict(trigger_runtime_src.get("last_trigger_at")).items():
        zone_key = _safe_text(key, "", 96)
        if not zone_key:
            continue
        try:
            last_trigger_at[zone_key] = float(value)
        except Exception:
            continue
    return {
        "world_state_flags": _as_dict(src.get("world_state_flags")),
        "discovered_pois": _as_dict(src.get("discovered_pois")),
        "cleared_or_completed_locations": _as_dict(src.get("cleared_or_completed_locations")),
        "unlocked_services": _as_dict(src.get("unlocked_services")),
        "region_state": _as_dict(src.get("region_state")),
        "town_state": _as_dict(src.get("town_state")),
        "faction_world_flags": _as_dict(src.get("faction_world_flags")),
        "faction_reputation": normalize_faction_reputation_state(src.get("faction_reputation")),
        "world_event_flags": _as_dict(src.get("world_event_flags")),
        "world_change_history": history,
        "recent_events": recent_events,
        "unlocked_handout_ids": _normalize_quest_link_ids(src.get("unlocked_handout_ids"), max_items=200, max_len=80),
        "discovery_ids": _normalize_quest_link_ids(src.get("discovery_ids"), max_items=200, max_len=80),
        "event_messages": _normalize_quest_link_ids(src.get("event_messages"), max_items=50, max_len=240),
        "quest_refresh_ids": _normalize_quest_link_ids(src.get("quest_refresh_ids"), max_items=200, max_len=64),
        "scene_trigger_zones": trigger_zones,
        "scene_trigger_runtime": {
            "consumed_zone_ids": consumed_zone_ids,
            "last_trigger_at": last_trigger_at,
        },
    }


def _normalize_codex_entry(entry: Any) -> dict | None:
    if not isinstance(entry, dict):
        return None
    entry_id = str(entry.get("id") or "").strip()[:64]
    if not entry_id:
        return None
    entry_type = str(entry.get("type") or "note").strip().lower()[:32]
    if entry_type not in {"note", "log", "lore", "quest"}:
        entry_type = "note"
    visibility = str(entry.get("visibility") or "party").strip().lower()[:16]
    if visibility not in {"private", "party", "dm"}:
        visibility = "party"
    return {
        "id": entry_id,
        "type": entry_type,
        "visibility": visibility,
        "author_id": str(entry.get("author_id") or "").strip()[:64],
        "title": str(entry.get("title") or "").strip()[:240],
        "content_md": str(entry.get("content_md") or "")[:16000],
        "created_at": float(entry.get("created_at") or 0.0),
        "updated_at": float(entry.get("updated_at") or 0.0),
        "tags": [str(t)[:64] for t in (entry.get("tags") or []) if isinstance(t, str)][:32],
        "poi_id": str(entry.get("poi_id") or "").strip()[:64] or None,
    }


def normalize_codex_entries(raw: Any) -> list:
    src = raw if isinstance(raw, list) else []
    out = []
    for item in src:
        normalized = _normalize_codex_entry(item)
        if normalized is not None:
            out.append(normalized)
    return out


def _normalize_codex_link(link: Any) -> dict | None:
    if not isinstance(link, dict):
        return None
    link_id = str(link.get("id") or "").strip()[:64]
    if not link_id:
        return None
    from_type = str(link.get("from_type") or "").strip()[:32]
    from_id = str(link.get("from_id") or "").strip()[:64]
    to_type = str(link.get("to_type") or "").strip()[:32]
    to_id = str(link.get("to_id") or "").strip()[:64]
    if not from_type or not from_id or not to_type or not to_id:
        return None
    return {
        "id": link_id,
        "from_type": from_type,
        "from_id": from_id,
        "to_type": to_type,
        "to_id": to_id,
        "label": str(link.get("label") or "").strip()[:120],
        "author_id": str(link.get("author_id") or "").strip()[:64],
        "created_at": float(link.get("created_at") or 0.0),
    }


def normalize_codex_links(raw: Any) -> list:
    src = raw if isinstance(raw, list) else []
    out = []
    for item in src:
        normalized = _normalize_codex_link(item)
        if normalized is not None:
            out.append(normalized)
    return out


def normalize_persisted_campaign_data(data: dict[str, Any] | None) -> dict[str, Any]:
    src = data or {}
    normalized = dict(src)
    normalized["fog_maps"] = normalize_fog_maps(src.get("fog_maps"))
    normalized["combat"] = normalize_combat_state(src.get("combat"))
    normalized["sound_state"] = normalize_sound_state(src.get("sound_state"))
    normalized["weather_state"] = normalize_weather_state(src.get("weather_state"))
    normalized["active_poll"] = normalize_active_poll(src.get("active_poll"))
    normalized["show_viewer_presence"] = bool(src.get("show_viewer_presence", False))
    normalized["world_state"] = normalize_world_state(src.get("world_state"))
    for field in PERSISTED_LIST_FIELDS:
        normalized[field] = _as_list(src.get(field))
    normalized["codex_entries"] = normalize_codex_entries(src.get("codex_entries"))
    normalized["codex_links"] = normalize_codex_links(src.get("codex_links"))
    normalized["discovery_cards"] = normalize_discovery_cards(src.get("discovery_cards"))
    normalized["private_story_hooks"] = normalize_private_story_hooks(src.get("private_story_hooks"))
    normalized["encounter_templates"] = normalize_encounter_templates(src.get("encounter_templates"))
    normalized["quest_templates"] = normalize_quest_entries(src.get("quest_templates"))
    normalized["session_quests"] = normalize_quest_entries(src.get("session_quests"))
    normalized["quest_board_bindings"] = normalize_quest_board_bindings(src.get("quest_board_bindings"))
    normalized.update(normalize_editor_state(src))
    for field in (
        "char_profiles",
        "active_char_profiles",
        "player_inventories",
        "player_gold",
        "viewer_profiles",
        "viewer_pending_actions",
        "viewer_power_catalog",
        "hazard_zones",
        "corpse_states",
        "corpse_dm_config",
        "chat_participants",
    ):
        normalized[field] = _as_dict(src.get(field))
    # Encumbrance settings (DM-configurable rules)
    normalized["encumbrance_settings"] = _normalize_encumbrance_settings(src.get("encumbrance_settings"))
    return normalized


def _normalize_encumbrance_settings(raw: Any) -> dict:
    src = _as_dict(raw)
    return {
        "use_encumbrance":   bool(src.get("use_encumbrance",   True)),
        "variant":           _safe_text(src.get("variant"),    "variant", 16).lower() if _safe_text(src.get("variant"), "variant", 16).lower() in {"basic", "variant"} else "variant",
        "size_restrictions": bool(src.get("size_restrictions", True)),
        "allow_dm_override": bool(src.get("allow_dm_override", True)),
        "bag_destruction_events": bool(src.get("bag_destruction_events", True)),
        "extradim_conflict_block": bool(src.get("extradim_conflict_block", True)),
    }



def _serialize_chat_participants(participants: dict) -> dict:
    """Convert ChatParticipant dataclasses to plain dicts for JSON serialization."""
    out = {}
    for key, p in participants.items():
        if hasattr(p, "__dataclass_fields__"):
            out[key] = {
                "twitch_username": p.twitch_username,
                "display_name": p.display_name,
                "joined_at": p.joined_at,
                "inventory": list(p.inventory),
                "is_active": p.is_active,
            }
        elif isinstance(p, dict):
            out[key] = p
    return out


def extract_persistable_campaign_state(session: Any) -> dict[str, Any]:
    raw = {
        "fog_maps": getattr(session, "fog_maps", None) or {},
        "combat": getattr(session, "combat", None) or {},
        "journal_entries": _clone(getattr(session, "journal_entries", None) or []),
        "codex_entries": _clone(getattr(session, "codex_entries", None) or []),
        "codex_links": _clone(getattr(session, "codex_links", None) or []),
        "library_entries": _clone(getattr(session, "library_entries", None) or []),
        "item_library_entries": _clone(getattr(session, "item_library_entries", None) or []),
        "char_profiles": _clone(getattr(session, "char_profiles", None) or {}),
        "active_char_profiles": _clone(getattr(session, "active_char_profiles", None) or {}),
        "player_inventories": _clone(getattr(session, "player_inventories", None) or {}),
        "player_gold": _clone(getattr(session, "player_gold", None) or {}),
        "party_loot_log": _clone(getattr(session, "party_loot_log", None) or []),
        "editor_layers": _clone(getattr(session, "editor_layers", None) or {}),
        "editor_walls": _clone(getattr(session, "editor_walls", None) or {}),
        "editor_props": _clone(getattr(session, "editor_props", None) or {}),
        "map_settings": _clone(getattr(session, "map_settings", None) or {}),
        "editor_paths": _clone(getattr(session, "editor_paths", None) or {}),
        "editor_labels": _clone(getattr(session, "editor_labels", None) or {}),
        "editor_markers": _clone(getattr(session, "editor_markers", None) or {}),
        "editor_lights": _clone(getattr(session, "editor_lights", None) or {}),
        "map_documents": build_map_documents_from_session(session),
        "viewer_profiles": _clone(getattr(session, "viewer_profiles", None) or {}),
        "viewer_pending_actions": _clone(getattr(session, "viewer_pending_actions", None) or {}),
        "viewer_power_catalog": _clone(getattr(session, "viewer_power_catalog", None) or {}),
        "hazard_zones": _clone(getattr(session, "hazard_zones", None) or {}),
        "corpse_states": _clone(getattr(session, "corpse_states", None) or {}),
        "corpse_dm_config": _clone(getattr(session, "corpse_dm_config", None) or {}),
        "encumbrance_settings": _clone(getattr(session, "encumbrance_settings", None) or {}),
        "chat_participants": _serialize_chat_participants(getattr(session, "chat_participants", None) or {}),
        "handouts": _clone(getattr(session, "handouts", None) or []),
        "discovery_cards": _clone(getattr(session, "discovery_cards", None) or []),
        "private_story_hooks": _clone(getattr(session, "private_story_hooks", None) or []),
        "encounter_templates": _clone(getattr(session, "encounter_templates", None) or []),
        "quest_templates": _clone(getattr(session, "quest_templates", None) or []),
        "session_quests": _clone(getattr(session, "session_quests", None) or []),
        "quest_board_bindings": _clone(getattr(session, "quest_board_bindings", None) or []),
        "sound_state": _clone(getattr(session, "sound_state", None) or {}),
        "weather_state": _clone(getattr(session, "weather_state", None) or {}),
        "active_poll": _clone(getattr(session, "active_poll", None)),
        "show_viewer_presence": bool(getattr(session, "show_viewer_presence", False)),
        "world_state": _clone(getattr(session, "world_state", None) or {}),
    }
    normalized = normalize_persisted_campaign_data(raw)
    session.map_documents = _clone(normalized["map_documents"])
    session.map_settings = _clone(normalized["map_settings"])
    session.sound_state = _clone(normalized["sound_state"])
    session.weather_state = _clone(normalized["weather_state"])
    session.active_poll = _clone(normalized["active_poll"])
    session.show_viewer_presence = bool(normalized["show_viewer_presence"])
    session.quest_templates = _clone(normalized["quest_templates"])
    session.session_quests = _clone(normalized["session_quests"])
    session.quest_board_bindings = _clone(normalized["quest_board_bindings"])
    session.world_state = _clone(normalized["world_state"])
    return normalized
