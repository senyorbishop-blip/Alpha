"""
server/session.py — In-memory session + state management for Phase 1
"""
import hashlib
import secrets
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from copy import deepcopy
from server.encumbrance import auto_tag_extradimensional
from server.quest_premium_progression import build_premium_progression_snapshot
from server.character.profile_assets import sanitize_profiles_for_websocket


def display_user_handle(session_id: str, user_id: str) -> str:
    """Build a stable, display-safe user handle for join broadcasts.

    The handle is deterministic from ``(session_id, user_id)`` so any surface
    that only learns a participant's handle (e.g. the DM client receiving a
    ``user_joined`` broadcast that omits the raw id for privacy) can be mapped
    back to the real user by recomputing this value. Keep this in lockstep with
    every consumer — it is the single source of truth for the handle algorithm.
    """
    raw = f"{session_id}:{user_id}".encode("utf-8", errors="ignore")
    return f"user_{hashlib.sha256(raw).hexdigest()[:12]}"






MAP_CONTEXT_ALIAS_KEYS = ("map_ctx", "map_context", "dm_map_context", "current_map", "currentMap", "context", "map")

def normalize_map_context(value=None, *, fallback: str = "world") -> str:
    """Canonical map-context key for persisted/shared runtime state.

    Boundaries may accept aliases, but storage must use this normalizer:
    the world map is always ``world`` and local/POI maps retain their runtime ID.
    """
    raw = str(value if value is not None else fallback or "world").strip()[:80] or "world"
    if raw in {"", "default", "main", "world_map"}:
        return "world"
    if raw == "__local__":
        return normalize_map_context(fallback)
    return "world" if raw == "world" else raw

def normalize_map_context_from_payload(payload: dict | None, *, fallback: str = "world") -> str:
    data = payload if isinstance(payload, dict) else {}
    for key in MAP_CONTEXT_ALIAS_KEYS:
        if key in data and str(data.get(key) or "").strip():
            return normalize_map_context(data.get(key), fallback=fallback)
    return normalize_map_context(fallback)

def normalize_fog_maps(fog_maps: dict | None) -> dict:
    normalized = {}
    for key, raw in dict(fog_maps or {}).items():
        ctx = normalize_map_context(key)
        entry = dict(raw or {}) if isinstance(raw, dict) else {}
        try:
            cols = max(1, min(int(entry.get("cols") or 64), 512))
        except Exception:
            cols = 64
        try:
            rows = max(1, min(int(entry.get("rows") or 64), 512))
        except Exception:
            rows = 64
        total = cols * rows
        cells = entry.get("cells")
        if not isinstance(cells, str):
            cells = "".join("1" if int(v or 0) else "0" for v in (cells or []))
        cells = (cells[:total]).ljust(total, "0")
        try:
            updated_at = float(entry.get("updated_at") or 0.0)
        except Exception:
            updated_at = 0.0
        normalized[ctx] = {
            "enabled": bool(entry.get("enabled", False)),
            "cols": cols,
            "rows": rows,
            "cells": cells,
            "revision": int(entry.get("revision") or 0),
            "updated_at": updated_at,
            "map_context": ctx,
        }
    return normalized


ASSISTANT_DM_SCOPE_KEYS = frozenset({
    "tokens.control_npc",
    "maps.fog",
    "narration.broadcast",
    "handouts.manage",
    "quests.manage",
    "combat.manage_limited",
})


def _assistant_dm_store(session: "Session") -> dict:
    world_state = dict(getattr(session, "world_state", {}) or {})
    block = world_state.get("assistant_dm") if isinstance(world_state.get("assistant_dm"), dict) else {}
    users = block.get("users") if isinstance(block.get("users"), dict) else {}
    normalized_users = {}
    for uid, raw in users.items():
        if not isinstance(raw, dict):
            continue
        scopes = sorted({str(scope or "").strip() for scope in (raw.get("scopes") or []) if str(scope or "").strip() in ASSISTANT_DM_SCOPE_KEYS})
        token_ids = sorted({str(v or "").strip()[:64] for v in (raw.get("token_ids") or []) if str(v or "").strip()})
        map_contexts = sorted({normalize_map_context(v) for v in (raw.get("map_contexts") or []) if str(v or "").strip()})
        normalized_users[str(uid)] = {
            "enabled": bool(raw.get("enabled", True)),
            "scopes": scopes,
            "token_ids": token_ids,
            "map_contexts": map_contexts,
            "updated_at": float(raw.get("updated_at") or time.time()),
            "updated_by": str(raw.get("updated_by") or "")[:64],
        }
    block = {"users": normalized_users}
    world_state["assistant_dm"] = block
    session.world_state = world_state
    return block


def assistant_dm_permissions_for_user(session: "Session", user_id: str) -> dict:
    users = _assistant_dm_store(session).get("users", {})
    return dict(users.get(str(user_id or ""), {}))


def assistant_dm_has_scope(session: "Session", user: "User", scope: str, *, map_ctx: str | None = None, token_id: str | None = None) -> bool:
    if not user:
        return False
    role = str(getattr(user, "role", "") or "").strip().lower()
    if role == "dm":
        return True
    if role != "assistant_dm":
        return False
    perms = assistant_dm_permissions_for_user(session, str(getattr(user, "id", "") or ""))
    if not perms or not perms.get("enabled", True):
        return False
    scopes = set(perms.get("scopes") or [])
    if scope not in scopes:
        return False
    if map_ctx is not None:
        allowed = set(perms.get("map_contexts") or [])
        if allowed and str(map_ctx or "world") not in allowed:
            return False
    if token_id is not None:
        allowed_tokens = set(perms.get("token_ids") or [])
        if allowed_tokens and str(token_id or "") not in allowed_tokens:
            return False
    return True


def set_assistant_dm_permissions(session: "Session", *, actor: "User", target_user_id: str, enabled: bool, scopes: list[str], token_ids: list[str], map_contexts: list[str]) -> dict:
    block = _assistant_dm_store(session)
    users = dict(block.get("users") or {})
    target_id = str(target_user_id or "").strip()[:64]
    if not target_id:
        return {}
    clean_scopes = sorted({str(scope or "").strip() for scope in (scopes or []) if str(scope or "").strip() in ASSISTANT_DM_SCOPE_KEYS})
    clean_token_ids = sorted({str(v or "").strip()[:64] for v in (token_ids or []) if str(v or "").strip()})
    clean_map_ctx = sorted({str(v or "").strip()[:80] or "world" for v in (map_contexts or []) if str(v or "").strip()})
    users[target_id] = {
        "enabled": bool(enabled),
        "scopes": clean_scopes,
        "token_ids": clean_token_ids,
        "map_contexts": clean_map_ctx,
        "updated_at": time.time(),
        "updated_by": str(getattr(actor, "id", "") or "")[:64],
    }
    block["users"] = users
    world_state = dict(getattr(session, "world_state", {}) or {})
    world_state["assistant_dm"] = block
    session.world_state = world_state
    return dict(users[target_id])

INTERACTABLE_ACTIONS = {
    "inspect",
    "interact",
    "mark_for_party",
    "ask_party",
    "attempt_skill_action",
    "open",
    "loot",
    "disable",
    "reveal",
    "exhaust",
}
INTERACTABLE_STATE_IDS = {"closed", "opened", "looted", "disabled", "revealed", "exhausted"}

SCENE_TRIGGER_VISIBILITY_MODES = {
    "public",
    "party",
    "players_only",
    "dm_only",
    "owner_only",
}

SCENE_TRIGGER_ACTIONS = {
    "ambient_profile",
    "weather_preset",
    "narration_hook",
    "unlock_discovery",
    "set_world_state_flag",
    "living_world_event",
}


def normalize_interactable(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    enabled = bool(raw.get("enabled"))
    interaction_id = str(raw.get("id") or "").strip()[:48]
    kind = str(raw.get("kind") or raw.get("type") or "").strip().lower()[:40]
    prompt = str(raw.get("prompt") or raw.get("prompt_text") or "").strip()[:240]
    discovery_hook = str(raw.get("discovery_hook") or "").strip()[:80]
    permissions_src = raw.get("permissions") if isinstance(raw.get("permissions"), dict) else {}
    visibility_src = raw.get("visibility") if isinstance(raw.get("visibility"), dict) else {}

    actions = []
    seen = set()
    for entry in raw.get("actions") or []:
        if isinstance(entry, dict):
            action_id = str(entry.get("id") or "").strip().lower()[:40]
            if action_id not in INTERACTABLE_ACTIONS or action_id in seen:
                continue
            action = {"id": action_id, "label": str(entry.get("label") or action_id.replace("_", " ").title()).strip()[:80]}
            if entry.get("skill"):
                action["skill"] = str(entry.get("skill") or "").strip().lower()[:40]
            seen.add(action_id)
            actions.append(action)
            continue
        action_id = str(entry or "").strip().lower()[:40]
        if action_id not in INTERACTABLE_ACTIONS or action_id in seen:
            continue
        seen.add(action_id)
        actions.append({"id": action_id, "label": action_id.replace("_", " ").title()})

    states_src = raw.get("states") if isinstance(raw.get("states"), dict) else {}
    states = {}
    for state_id, state_raw in states_src.items():
        clean_state = str(state_id or "").strip().lower()[:32]
        if clean_state not in INTERACTABLE_STATE_IDS or not isinstance(state_raw, dict):
            continue
        state_actions = []
        state_seen = set()
        for entry in state_raw.get("available_actions") or []:
            if isinstance(entry, dict):
                action_id = str(entry.get("id") or "").strip().lower()[:40]
                if action_id not in INTERACTABLE_ACTIONS or action_id in state_seen:
                    continue
                row = {"id": action_id, "label": str(entry.get("label") or action_id.replace("_", " ").title()).strip()[:80]}
                if entry.get("skill"):
                    row["skill"] = str(entry.get("skill") or "").strip().lower()[:40]
                state_seen.add(action_id)
                state_actions.append(row)
            else:
                action_id = str(entry or "").strip().lower()[:40]
                if action_id not in INTERACTABLE_ACTIONS or action_id in state_seen:
                    continue
                state_seen.add(action_id)
                state_actions.append({"id": action_id, "label": action_id.replace("_", " ").title()})
        next_state_by_action = {}
        for action_id, next_state in dict(state_raw.get("next_state_by_action") or {}).items():
            clean_action = str(action_id or "").strip().lower()[:40]
            mapped_state = str(next_state or "").strip().lower()[:32]
            if clean_action in INTERACTABLE_ACTIONS and mapped_state in INTERACTABLE_STATE_IDS:
                next_state_by_action[clean_action] = mapped_state
        states[clean_state] = {
            "label_override": str(state_raw.get("label_override") or "").strip()[:120],
            "asset_key_override": str(state_raw.get("asset_key_override") or state_raw.get("art_override") or "").strip()[:120],
            "available_actions": state_actions,
            "one_time_flags": [str(v).strip()[:80] for v in list(state_raw.get("one_time_flags") or [])[:30] if str(v).strip()],
            "world_state_flags": {str(k).strip()[:80]: v for k, v in dict(state_raw.get("world_state_flags") or {}).items() if str(k).strip()},
            "discovery_hook": str(state_raw.get("discovery_hook") or "").strip()[:80],
            "discovery_visibility": str(state_raw.get("discovery_visibility") or "").strip().lower()[:32],
            "handout_unlock_ids": [str(v).strip()[:80] for v in list(state_raw.get("handout_unlock_ids") or [])[:30] if str(v).strip()],
            "next_state": str(state_raw.get("next_state") or "").strip().lower()[:32],
            "next_state_by_action": next_state_by_action,
        }
    current_state = str(raw.get("current_state") or raw.get("state") or "").strip().lower()[:32]
    if current_state not in INTERACTABLE_STATE_IDS:
        current_state = ""
    if not current_state and states:
        current_state = "closed" if "closed" in states else next(iter(states.keys()), "")

    if not enabled and not actions and not kind and not interaction_id and not prompt and not discovery_hook and not states:
        return None

    permissions = {
        "dm_only": bool(permissions_src.get("dm_only", False)),
        "requires_token": bool(permissions_src.get("requires_token", False)),
        "allow_players": bool(permissions_src.get("allow_players", True)),
        "allow_viewers": bool(permissions_src.get("allow_viewers", False)),
    }
    visibility = {
        "mode": str(visibility_src.get("mode") or "public").strip().lower()[:24] or "public",
        "discovery_visibility": str(visibility_src.get("discovery_visibility") or "").strip().lower()[:32],
    }
    raw_cooldown = raw.get("cooldown_ms", 0)
    raw_debounce = raw.get("debounce_ms", 250)
    out = {
        "enabled": enabled or bool(actions),
        "id": interaction_id,
        "kind": kind,
        "prompt": prompt,
        "actions": actions,
        "permissions": permissions,
        "visibility": visibility,
        "discovery_hook": discovery_hook,
        "cooldown_ms": max(0, min(120000, int(0 if raw_cooldown is None else raw_cooldown))),
        "debounce_ms": max(0, min(10000, int(250 if raw_debounce is None else raw_debounce))),
    }
    if states:
        out["states"] = states
        if current_state:
            out["current_state"] = current_state
        used_flags = sorted({str(v).strip()[:80] for v in list(raw.get("used_one_time_flags") or [])[:80] if str(v).strip()})
        if used_flags:
            out["used_one_time_flags"] = used_flags
    return out


def normalize_scene_trigger_zone(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    zone_id = str(raw.get("id") or "").strip()[:64]
    bounds_src = raw.get("bounds") if isinstance(raw.get("bounds"), dict) else {}
    shape = str(bounds_src.get("shape") or "rect").strip().lower()[:16]
    if shape not in {"rect", "circle"}:
        shape = "rect"
    bounds = {
        "shape": shape,
        "x": float(bounds_src.get("x", 0.0) or 0.0),
        "y": float(bounds_src.get("y", 0.0) or 0.0),
        "width": max(1.0, float(bounds_src.get("width", 1.0) or 1.0)),
        "height": max(1.0, float(bounds_src.get("height", 1.0) or 1.0)),
        "radius": max(1.0, float(bounds_src.get("radius", 1.0) or 1.0)),
    }
    visibility_src = raw.get("visibility") if isinstance(raw.get("visibility"), dict) else {}
    visibility_mode = str(visibility_src.get("mode") or "party").strip().lower()[:24] or "party"
    if visibility_mode not in SCENE_TRIGGER_VISIBILITY_MODES:
        visibility_mode = "party"
    actions = {"on_enter": [], "on_exit": []}
    for phase in ("on_enter", "on_exit"):
        for item in (raw.get(phase) or []):
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("type") or "").strip().lower()[:40]
            if action_type not in SCENE_TRIGGER_ACTIONS:
                continue
            actions[phase].append({
                "type": action_type,
                "payload": dict(item.get("payload") or {}),
            })
    if not zone_id or (not actions["on_enter"] and not actions["on_exit"]):
        return None
    raw_cooldown = raw.get("cooldown_ms", 0)
    raw_debounce = raw.get("debounce_ms", 250)
    return {
        "id": zone_id,
        "map_context": str(raw.get("map_context") or "world").strip()[:80] or "world",
        "enabled": bool(raw.get("enabled", True)),
        "trigger_once": bool(raw.get("trigger_once", False)),
        "repeatable": bool(raw.get("repeatable", True)),
        "allow_dm_trigger": bool(raw.get("allow_dm_trigger", False)),
        "cooldown_ms": max(0, min(120000, int(0 if raw_cooldown is None else raw_cooldown))),
        "debounce_ms": max(0, min(10000, int(250 if raw_debounce is None else raw_debounce))),
        "bounds": bounds,
        "visibility": {
            "mode": visibility_mode,
            "allow_viewers": bool(visibility_src.get("allow_viewers", False)),
            "dm_only_narration": bool(visibility_src.get("dm_only_narration", False)),
        },
        "on_enter": actions["on_enter"],
        "on_exit": actions["on_exit"],
    }


def generate_code(length: int = 8) -> str:
    """Generate a cryptographically random invite code."""
    return secrets.token_urlsafe(length)[:length].upper()


@dataclass
class POI:
    id: str
    x: float
    y: float
    name: str
    description: str = ""
    dm_notes: str = ""
    poi_type: str = "city"
    local_map_url: Optional[str] = None
    map_context: str = "world"   # "world" or poi_id of local map this belongs to
    revealed_to_players: bool = True
    interactable: Optional[dict] = None

    def to_dict(self, include_dm_notes: bool = False) -> dict:
        d = {
            "id": self.id, "x": self.x, "y": self.y,
            "name": self.name, "description": self.description,
            "poi_type": self.poi_type,
            "local_map_url": self.local_map_url,
            "map_context": self.map_context,
            "revealed_to_players": self.revealed_to_players,
        }
        interactable = normalize_interactable(self.interactable)
        if interactable:
            d["interactable"] = interactable
        if include_dm_notes:
            d["dm_notes"] = self.dm_notes
        return d


@dataclass
class Token:
    id: str
    name: str
    x: float
    y: float
    width: float
    height: float
    color: str
    shape: str  # "circle" | "rect"
    owner_id: Optional[str]  # None = DM-only token
    temp_permissions: Dict[str, float] = field(default_factory=dict)
    hp: Optional[int] = None
    max_hp: Optional[int] = None
    temp_hp: int = 0
    hidden_hp: bool = False
    hidden: bool = False      # hidden from players/viewers
    initiative_mod: int = 0
    ac: Optional[int] = None
    ac_from_equipment: bool = False   # True when AC was last set by equipment calculation
    speed: Optional[int] = None
    token_type: str = "player"  # player | npc | monster
    notes: str = ""
    conditions: list = field(default_factory=list)
    condition_timers: dict = field(default_factory=dict)
    level: Optional[int] = None
    faction: str = ""
    passive_perception: Optional[int] = None
    map_context: str = "world"  # which map this token was placed on
    staged: bool = False          # waiting to be placed from the staging tray
    image_url: Optional[str] = None
    save_bonuses: dict = field(default_factory=dict)
    vision_enabled: bool = False
    vision_radius: int = 0
    bright_radius: int = 0
    dim_radius: int = 0
    has_darkvision: bool = False
    darkvision_radius: int = 0
    creature_id: str = ""
    creature_type: str = ""
    monster_type: str = ""
    cr: str = ""
    profile_id: str = ""
    library_id: str = ""
    character_id: str = ""
    revision: int = 0  # per-token monotonic stamp, set to session.token_state_revision on every authoritative mutation

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "color": self.color,
            "shape": self.shape,
            "owner_id": self.owner_id,
            "temp_permissions": {k: v for k, v in self.temp_permissions.items() if v > time.time()},
        }
        if self.hp is not None:    d["hp"]    = self.hp
        if self.max_hp is not None: d["maxHp"] = self.max_hp
        d["tempHp"] = int(self.temp_hp or 0)
        d["hidden_hp"] = self.hidden_hp
        d["hidden"]    = self.hidden
        d["initiativeMod"] = int(self.initiative_mod or 0)
        if self.ac is not None: d["ac"] = int(self.ac)
        d["ac_from_equipment"] = bool(self.ac_from_equipment)
        if self.speed is not None: d["speed"] = int(self.speed)
        d["tokenType"] = str(self.token_type or "player")
        d["notes"] = str(self.notes or "")
        d["conditions"] = list(self.conditions or [])
        d["condition_timers"] = dict(self.condition_timers or {})
        if self.level is not None: d["level"] = int(self.level)
        d["faction"] = str(self.faction or "")
        if self.passive_perception is not None: d["passivePerception"] = int(self.passive_perception)
        d["map_context"] = self.map_context
        d["staged"] = self.staged
        if self.image_url:
            d["image_url"] = str(self.image_url)
        d["saveBonuses"] = dict(self.save_bonuses or {})
        d["visionEnabled"] = bool(self.vision_enabled)
        d["visionRadius"] = int(self.vision_radius or 0)
        d["brightRadius"] = int(self.bright_radius or 0)
        d["dimRadius"] = int(self.dim_radius or 0)
        d["hasDarkvision"] = bool(self.has_darkvision)
        d["darkvisionRadius"] = int(self.darkvision_radius or 0)
        if self.creature_id:
            d["creature_id"] = str(self.creature_id)
        if self.creature_type:
            d["creature_type"] = str(self.creature_type)
        if self.monster_type:
            d["monster_type"] = str(self.monster_type)
        if self.cr:
            d["cr"] = str(self.cr)
        if self.profile_id:
            d["profile_id"] = str(self.profile_id)
        if self.library_id:
            d["libraryId"] = str(self.library_id)
            d["library_id"] = str(self.library_id)
        if self.character_id:
            d["characterId"] = str(self.character_id)
            d["character_id"] = str(self.character_id)
        d["revision"] = int(self.revision or 0)
        return d

    def can_move(self, user_id: str, role: str) -> bool:
        if role == "dm":
            return True
        if role == "player":
            owner = str(self.owner_id or "").strip()
            user_id_norm = str(user_id or "").strip()
            if owner and owner == user_id_norm:
                return True
            # Backward compatibility: older campaigns and some reconnect paths
            # can persist token owner_id as a normalized profile key.
            if owner and normalize_profile_owner_key(owner) == normalize_profile_owner_key(user_id_norm):
                return True
        # Check temporary permission
        expiry = self.temp_permissions.get(user_id, 0)
        return time.time() < expiry


@dataclass
class User:
    id: str
    name: str
    role: str  # "dm" | "assistant_dm" | "player" | "viewer"
    connected: bool = True
    connected_at: float = field(default_factory=time.time)
    last_emote_at: float = 0.0
    last_token_emote_at: float = 0.0
    subgroup_id: str = "main"

    def __post_init__(self):
        normalized = str(self.role or "viewer").strip().lower() or "viewer"
        if normalized not in {"dm", "assistant_dm", "player", "viewer", "chat_bridge"}:
            normalized = "viewer"
        self.role = normalized


@dataclass
class ChatParticipant:
    """A Twitch viewer registered as an in-session chat participant."""
    twitch_username: str   # lowercase — primary key
    display_name: str      # original capitalisation from Twitch
    joined_at: float
    inventory: list = field(default_factory=list)  # list of item dicts
    is_active: bool = True
    twitch_user_id: str = ""          # Twitch numeric user ID (stable across name changes)
    character_name: str = ""          # custom in-game name set via !name
    last_joined_session_id: str = ""  # tracks new-session joins for sessions_joined counting
    lifetime_stats: dict = field(default_factory=lambda: {
        "sessions_joined": 0,
        "items_used": 0,
        "damage_dealt": 0,
        "kills": 0,
    })
    arena_stats: dict = field(default_factory=lambda: {
        "wins": 0,
        "losses": 0,
        "arena_gold": 0,
    })
    # Full arena progression sheet (class, level, xp, gold, items…) —
    # opaque to the campaign; written only by the arena sync handler.
    arena_character: dict = field(default_factory=dict)


@dataclass
class Session:
    id: str
    dm_id: Optional[str] = None
    player_invite: str = field(default_factory=lambda: generate_code(8))
    viewer_invite: str = field(default_factory=lambda: generate_code(8))
    users: Dict[str, User] = field(default_factory=dict)
    tokens: Dict[str, Token] = field(default_factory=dict)
    log: List[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    map_image_url: Optional[str] = None   # relative URL to uploaded map
    pois: Dict[str, 'POI'] = field(default_factory=dict)
    dm_map_context: str = 'world'  # tracks which map the DM is currently on
    dm_current_map_url: Optional[str] = None  # local map URL the DM is currently viewing
    map_nav_version: int = 0  # monotonic nav counter so stale POI/world sync cannot win
    dm_nav_intent: int = 0  # latest DM client-side nav intent; stale queued enter/exit messages are ignored
    fog_maps: dict = None  # keyed by map_ctx
    combat: dict = None  # {active: bool, turn: int, combatants: [{id,name,initiative,token_id,hp,max_hp}]} ('world' or poi_id) → {enabled, cols, rows, cells}
    visibility_revision: int = 0  # monotonic counter bumped on any fog/hidden/token-position/wall/door change; clients drop stale payloads behind it
    token_state_revision: int = 0  # monotonic counter bumped on every authoritative token mutation (create/move/delete/hide/hp/edit/staged/ownership); independent of visibility filtering
    wall_revision: int = 0  # monotonic counter bumped on every wall create/update/delete (PR 7)
    door_revision: int = 0  # monotonic counter bumped on every door create/update/toggle/lock/delete (PR 7)
    inventory_revision: int = 0  # monotonic counter bumped on any server-authoritative inventory/item/charge mutation; clients drop stale player_inventory_sync payloads behind it
    character_runtime_revision: int = 0  # monotonic active-character/profile runtime hydration revision
    spell_manifest_revision: int = 0  # monotonic spell/prep/known/item-spell manifest revision
    quick_actions_revision: int = 0  # monotonic server-assisted quick-actions hydration revision
    journal_entries: list = field(default_factory=list)
    codex_entries: list = field(default_factory=list)
    codex_links: list = field(default_factory=list)
    # library_entries: preserved for campaign backward-compatibility only.
    # The legacy in-session creature library has been removed; all creatures
    # now live in the DB-backed user_creature_library (see /api/library/creatures).
    # Old saved campaigns still carry this field; it is loaded but never written.
    library_entries: list = field(default_factory=list)
    item_library_entries: list = field(default_factory=list)
    char_profiles: dict = field(default_factory=dict)
    active_char_profiles: dict = field(default_factory=dict)
    player_inventories: dict = field(default_factory=dict)
    player_gold: dict = field(default_factory=dict)
    party_loot_log: list = field(default_factory=list)
    editor_layers: dict = field(default_factory=dict)
    editor_walls: dict = field(default_factory=dict)
    editor_props: dict = field(default_factory=dict)
    map_settings: dict = field(default_factory=dict)
    editor_paths: dict = field(default_factory=dict)
    editor_labels: dict = field(default_factory=dict)
    editor_markers: dict = field(default_factory=dict)
    editor_lights: dict = field(default_factory=dict)
    map_documents: dict = field(default_factory=dict)
    viewer_profiles: dict = field(default_factory=dict)
    viewer_pending_actions: dict = field(default_factory=dict)
    viewer_power_catalog: dict = field(default_factory=dict)
    hazard_zones: dict = field(default_factory=dict)
    handouts: list = field(default_factory=list)
    discovery_cards: list = field(default_factory=list)
    private_story_hooks: list = field(default_factory=list)
    encounter_templates: list = field(default_factory=list)
    quest_templates: list = field(default_factory=list)
    session_quests: list = field(default_factory=list)
    quest_board_bindings: list = field(default_factory=list)
    sound_state: dict = field(default_factory=dict)
    weather_state: dict = field(default_factory=dict)
    world_state: dict = field(default_factory=dict)
    active_poll: dict = None
    show_viewer_presence: bool = False
    party_memory_log: list = field(default_factory=list)
    camp_rest: dict = None  # {active, label, available_activities, player_activities}
    conversation_mode: dict = None  # {active, npc_id, npc_name, participants, speak_queue, reaction_cue, started_at}
    dm_notes: str = field(default="")
    # Encumbrance: settings persisted, speed-penalty cache is transient (not saved)
    encumbrance_settings: dict = field(default_factory=dict)
    # DM-set per-player STR/size overrides (persisted); keyed by user_id → {strength, size}
    enc_str_overrides: dict = field(default_factory=dict)
    corpse_states: dict = field(default_factory=dict)
    corpse_dm_config: dict = field(default_factory=dict)
    # Chat bridge: runtime-only (not persisted to DB by default).
    # chat_participants keyed by lowercase Twitch username.
    chat_participants: dict = field(default_factory=dict)
    chat_bridge_paused: bool = False
    arena_enabled: bool = True
    # Arena quiet mode: duels queue until the DM signals downtime.
    arena_quiet: bool = False
    # What chat-participant data survives between sessions:
    # "everything" (default) | "stats" (lifetime/arena stats only) | "nothing"
    chat_persistence_mode: str = "everything"
    # _encumbrance_cache is set dynamically; keyed by user_id → {state, speed_penalty}
    # Not declared here so it doesn't show up in persistence serialisation.

    def __post_init__(self):
        if self.fog_maps is None:
            self.fog_maps = {}
        if self.combat is None:
            self.combat = {"active": False, "turn": 0, "combatants": []}
        if self.camp_rest is None:
            self.camp_rest = {"active": False, "label": "", "available_activities": [], "player_activities": {}}
        if not isinstance(self.corpse_dm_config, dict) or not self.corpse_dm_config:
            self.corpse_dm_config = {
                "search_attempts_per_corpse": 2,
                "harvest_attempts_per_corpse": 1,
                "allow_humanoid_salvage": False,
                "fail_by_5_consequence": False,
                "reward_destination": "player_inventory",
                "crafting_enabled": True,
                "selling_enabled": True,
            }

    def add_log(self, message: str, msg_type: str = "system", user_name: str = "System") -> dict:
        entry = {
            "id": secrets.token_hex(4),
            "timestamp": time.time(),
            "type": msg_type,
            "user": user_name,
            "message": message,
        }
        self.log.append(entry)
        # Keep log bounded
        if len(self.log) > 500:
            self.log = self.log[-500:]
        return entry

    def _split_party_store(self) -> dict:
        world_state = dict(getattr(self, "world_state", {}) or {})
        raw = world_state.get("split_party") if isinstance(world_state.get("split_party"), dict) else {}
        assignments_src = raw.get("assignments") if isinstance(raw.get("assignments"), dict) else {}
        subgroup_ctx_src = raw.get("subgroup_contexts") if isinstance(raw.get("subgroup_contexts"), dict) else {}

        assignments: dict[str, str] = {}
        for uid, subgroup_id in assignments_src.items():
            user_id = str(uid or "").strip()[:64]
            subgroup = str(subgroup_id or "").strip().lower()[:48] or "main"
            if not user_id:
                continue
            assignments[user_id] = subgroup

        subgroup_contexts: dict[str, dict] = {}
        for subgroup_id, entry in subgroup_ctx_src.items():
            subgroup = str(subgroup_id or "").strip().lower()[:48] or "main"
            row = dict(entry or {}) if isinstance(entry, dict) else {}
            map_context = str(row.get("map_context") or "world").strip()[:80] or "world"
            subgroup_contexts[subgroup] = {
                "map_context": map_context,
                "updated_at": float(row.get("updated_at") or 0.0),
                "updated_by": str(row.get("updated_by") or "").strip()[:64],
            }
        if "main" not in subgroup_contexts:
            subgroup_contexts["main"] = {"map_context": "world", "updated_at": 0.0, "updated_by": ""}

        out = {"assignments": assignments, "subgroup_contexts": subgroup_contexts}
        world_state["split_party"] = out
        self.world_state = world_state
        return out

    def get_user_subgroup_id(self, user_id: str | None) -> str:
        uid = str(user_id or "").strip()[:64]
        if not uid:
            return "main"
        subgroup = str((self._split_party_store().get("assignments") or {}).get(uid) or "main").strip().lower()[:48] or "main"
        user = (self.users or {}).get(uid)
        if user is not None:
            user.subgroup_id = subgroup
        return subgroup

    def set_user_subgroup_id(self, user_id: str, subgroup_id: str, *, actor_id: str = "") -> str:
        uid = str(user_id or "").strip()[:64]
        if not uid:
            return "main"
        subgroup = str(subgroup_id or "main").strip().lower()[:48] or "main"
        store = self._split_party_store()
        assignments = dict(store.get("assignments") or {})
        assignments[uid] = subgroup
        store["assignments"] = assignments
        subgroup_contexts = dict(store.get("subgroup_contexts") or {})
        if subgroup not in subgroup_contexts:
            subgroup_contexts[subgroup] = {"map_context": "world", "updated_at": time.time(), "updated_by": str(actor_id or "")[:64]}
        store["subgroup_contexts"] = subgroup_contexts
        world_state = dict(getattr(self, "world_state", {}) or {})
        world_state["split_party"] = store
        self.world_state = world_state
        user = (self.users or {}).get(uid)
        if user is not None:
            user.subgroup_id = subgroup
        return subgroup

    def set_subgroup_map_context(self, subgroup_id: str, map_context: str, *, actor_id: str = "") -> dict:
        subgroup = str(subgroup_id or "main").strip().lower()[:48] or "main"
        target_map_ctx = normalize_map_context(map_context)
        store = self._split_party_store()
        subgroup_contexts = dict(store.get("subgroup_contexts") or {})
        subgroup_contexts[subgroup] = {
            "map_context": target_map_ctx,
            "updated_at": time.time(),
            "updated_by": str(actor_id or "")[:64],
        }
        store["subgroup_contexts"] = subgroup_contexts
        world_state = dict(getattr(self, "world_state", {}) or {})
        world_state["split_party"] = store
        self.world_state = world_state
        return dict(subgroup_contexts[subgroup])

    def get_subgroup_map_context(self, subgroup_id: str | None) -> str:
        subgroup = str(subgroup_id or "main").strip().lower()[:48] or "main"
        subgroup_contexts = dict(self._split_party_store().get("subgroup_contexts") or {})
        entry = subgroup_contexts.get(subgroup) if isinstance(subgroup_contexts.get(subgroup), dict) else {}
        return normalize_map_context((entry or {}).get("map_context"))

    def split_party_state(self) -> dict:
        store = self._split_party_store()
        return {
            "enabled": bool((store.get("assignments") or {})),
            "assignments": dict(store.get("assignments") or {}),
            "subgroup_contexts": dict(store.get("subgroup_contexts") or {}),
        }

    def visible_map_contexts_for_user(self, user_id: str | None) -> set[str]:
        subgroup_id = self.get_user_subgroup_id(user_id)
        subgroup_ctx = self.get_subgroup_map_context(subgroup_id)
        contexts = {"world"}
        if subgroup_ctx:
            contexts.add(subgroup_ctx)
        participant = (self.users or {}).get(str(user_id or ""))
        role = str(getattr(participant, "role", "") or "").strip().lower() if participant else ""
        dm_ctx = normalize_map_context(getattr(self, "dm_map_context", "world"))
        split_enabled = bool((self._split_party_store().get("assignments") or {}))
        if role == "viewer":
            contexts.add(dm_ctx)
        elif role == "player" and not split_enabled:
            # Single-party default: players should follow the DM's active scene
            # context for fog/map sync. Split-party assignments remain the
            # authority once enabled.
            contexts.add(dm_ctx)
        elif role == "player" and split_enabled and subgroup_id == "main":
            # Guardrail: many sessions keep split-party assignment metadata for
            # side groups while "main" still follows the DM camera. Ensure fog
            # broadcasts remain visible to main-party players on DM scene maps.
            contexts.add(dm_ctx)
        return {str(ctx).strip()[:80] or "world" for ctx in contexts}

    def get_connected_users(self) -> List[User]:
        return [u for u in self.users.values() if u.connected]

    def scene_trigger_zones(self) -> dict[str, dict]:
        world_state = dict(getattr(self, "world_state", {}) or {})
        raw = world_state.get("scene_trigger_zones") if isinstance(world_state.get("scene_trigger_zones"), dict) else {}
        normalized: dict[str, dict] = {}
        for _, entry in raw.items():
            zone = normalize_scene_trigger_zone(entry)
            if zone:
                normalized[str(zone["id"])] = zone
        world_state["scene_trigger_zones"] = normalized
        self.world_state = world_state
        return normalized

    def to_state_dict(self) -> dict:
        """Full state snapshot for new joiners."""
        # A full state snapshot is read-only: it must never recompute
        # fog/LOS-driven combat suspension. A snapshot build (e.g. a DM
        # reconnecting) can run with a transient/stale dm_map_context, and
        # re-deriving suspension from that one reader's context would make the
        # authoritative roster appear to drop members for every client. Live
        # combat/fog/token handlers are the sole writers of combat visibility,
        # so the snapshot simply mirrors the authoritative roster.
        # (sync_combat_visibility also hard-skips reason="state_snapshot".)
        combat_for_snapshot = self.combat
        return {
            "session_id": self.id,
            "tokens": {tid: build_token_runtime_payload(self, t) for tid, t in self.tokens.items()},
            "users": {
                uid: {"id": u.id, "name": u.name, "role": u.role, "connected": u.connected, "subgroup_id": self.get_user_subgroup_id(uid)}
                for uid, u in self.users.items()
            },
            "log": self.log[-100:],
            "map_image_url": self.map_image_url,
            "world_map_layers": list((((self.map_documents or {}).get('world') or {}).get('assets', {}) or {}).get('background_layers') or []),
            "pois": {pid: p.to_dict(include_dm_notes=False) for pid, p in self.pois.items()},
            "dm_map_context": self.dm_map_context,
            "dm_current_map_url": self.dm_current_map_url,
            "map_nav_version": int(self.map_nav_version or 0),
            "map_mode": "world" if normalize_map_context(self.dm_map_context) == "world" else "local",
            "visibility_revision": int(self.visibility_revision or 0),
            "token_state_revision": int(self.token_state_revision or 0),
            "inventory_revision": int(self.inventory_revision or 0),
            "character_runtime_revision": int(self.character_runtime_revision or 0),
            "spell_manifest_revision": int(self.spell_manifest_revision or 0),
            "quick_actions_revision": int(self.quick_actions_revision or 0),
            "dm_nav_intent": int(self.dm_nav_intent or 0),
            "fog_maps": normalize_fog_maps(self.fog_maps),
            "fog_source": "state_sync",
            "combat": combat_for_snapshot,
            "journal_entries": list(self.journal_entries or []),
            "party_memory_log": list(self.party_memory_log or []),
            "library_entries": list(self.library_entries or []),
            "item_library_entries": list(self.item_library_entries or []),
            "char_profiles": sanitize_profiles_for_websocket(dict(self.char_profiles or {})),
            "active_char_profiles": dict(self.active_char_profiles or {}),
            "player_gold": dict(self.player_gold or {}),
            "editor_layers": dict(self.editor_layers or {}),
            "editor_walls": dict(self.editor_walls or {}),
            "editor_props": dict(self.editor_props or {}),
            "map_settings": dict(self.map_settings or {}),
            "editor_paths": dict(self.editor_paths or {}),
            "editor_labels": dict(self.editor_labels or {}),
            "editor_markers": dict(self.editor_markers or {}),
            "editor_lights": dict(self.editor_lights or {}),
            "viewer_profiles": dict(self.viewer_profiles or {}),
            "viewer_pending_actions": dict(self.viewer_pending_actions or {}),
            "viewer_power_catalog": dict(self.viewer_power_catalog or {}),
            "hazard_zones": dict(self.hazard_zones or {}),
            "scene_trigger_zones": dict(self.scene_trigger_zones() or {}),
            "handouts": list(self.handouts or []),
            "private_story_hooks": list(self.private_story_hooks or []),
            "encounter_templates": list(self.encounter_templates or []),
            "sound_state": dict(self.sound_state or {}),
            "weather_state": dict(self.weather_state or {}),
            "active_poll": self.active_poll,
            "show_viewer_presence": bool(self.show_viewer_presence),
            "premium_progression": build_premium_progression_snapshot(self, role="dm"),
            "camp_rest": dict(self.camp_rest or {}),
            "conversation_mode": dict(self.conversation_mode or {"active": False}),
            "corpse_states": dict(self.corpse_states or {}),
            "corpse_dm_config": dict(self.corpse_dm_config or {}),
            "assistant_dm_permissions": dict(((_assistant_dm_store(self) or {}).get("users") or {})),
            "split_party": self.split_party_state(),
        }

    def _normalize_orphan_token_owners(self) -> None:
        """Re-link legacy owner keys to active players without stripping unresolved ownership."""
        valid_player_ids = {uid for uid, u in self.users.items() if u.role == "player"}
        player_name_keys = {
            normalize_profile_owner_key(getattr(u, "name", "")): uid
            for uid, u in self.users.items()
            if getattr(u, "role", "") == "player"
        }
        player_keys = {
            str(getattr(u, "player_key", "") or "").strip(): uid
            for uid, u in self.users.items()
            if getattr(u, "role", "") == "player" and str(getattr(u, "player_key", "") or "").strip()
        }
        for t in self.tokens.values():
            owner_id = str(getattr(t, "owner_id", "") or "").strip()
            if not owner_id or owner_id in valid_player_ids:
                continue
            mapped_user_id = player_keys.get(owner_id) or player_name_keys.get(normalize_profile_owner_key(owner_id))
            if mapped_user_id:
                t.owner_id = mapped_user_id

    def enforce_single_active_player_token_rule(self) -> dict:
        """Ensure each player has at most one active (non-staged) owned token.

        Legacy campaigns can contain duplicate active player-owned tokens.
        We keep one deterministic active token per player and stage any extras.
        Combat entries pointing at newly-staged tokens are removed to keep
        combat state coherent.
        """
        self._normalize_orphan_token_owners()
        valid_player_ids = {uid for uid, u in (self.users or {}).items() if getattr(u, "role", "") == "player"}
        if not valid_player_ids:
            return {"staged_token_ids": [], "removed_combatant_token_ids": []}

        combat = self.combat if isinstance(self.combat, dict) else {"active": False, "turn": 0, "combatants": []}
        combatants = list(combat.get("combatants") or [])
        combat_order = {
            str((entry or {}).get("token_id") or "").strip(): idx
            for idx, entry in enumerate(combatants)
            if str((entry or {}).get("token_id") or "").strip()
        }

        owned_active: dict[str, list[Token]] = {}
        for token in (self.tokens or {}).values():
            owner_id = str(getattr(token, "owner_id", "") or "").strip()
            if not owner_id or owner_id not in valid_player_ids:
                continue
            if bool(getattr(token, "staged", False)):
                continue
            token_type = str(getattr(token, "token_type", "player") or "player").strip().lower()
            if token_type == "companion":
                # Companions are intentionally allowed alongside a player's primary token.
                continue
            owned_active.setdefault(owner_id, []).append(token)

        staged_token_ids: list[str] = []
        for owner_id, owner_tokens in owned_active.items():
            if len(owner_tokens) <= 1:
                continue
            ordered = sorted(
                owner_tokens,
                key=lambda tok: (
                    0 if str(getattr(tok, "id", "") or "") in combat_order else 1,
                    combat_order.get(str(getattr(tok, "id", "") or ""), 999999),
                    str(getattr(tok, "id", "") or ""),
                ),
            )
            keep_id = str(getattr(ordered[0], "id", "") or "")
            for tok in ordered[1:]:
                tok.staged = True
                tok_id = str(getattr(tok, "id", "") or "")
                if tok_id and tok_id != keep_id:
                    staged_token_ids.append(tok_id)

        removed_combatant_token_ids: list[str] = []
        if staged_token_ids and combatants:
            staged_set = set(staged_token_ids)
            filtered = []
            for entry in combatants:
                token_id = str((entry or {}).get("token_id") or "").strip()
                if token_id and token_id in staged_set:
                    removed_combatant_token_ids.append(token_id)
                    continue
                filtered.append(entry)
            if len(filtered) != len(combatants):
                turn = int(combat.get("turn", 0) or 0)
                combat["combatants"] = filtered
                if not filtered:
                    combat["active"] = False
                    combat["turn"] = 0
                    combat["movement"] = {}
                else:
                    if turn >= len(filtered):
                        combat["turn"] = max(0, len(filtered) - 1)
                    elif turn < 0:
                        combat["turn"] = 0
                self.combat = combat

        return {
            "staged_token_ids": staged_token_ids,
            "removed_combatant_token_ids": removed_combatant_token_ids,
        }

    def _visible_discovery_cards_for_role(self, role: str, user_id: str | None = None) -> list[dict]:
        cards = list(getattr(self, "discovery_cards", []) or [])
        if role == "dm":
            return [dict(card or {}) for card in cards]
        if role != "player" or not user_id:
            return []
        visible = []
        for card in cards:
            entry = dict(card or {})
            visibility = str(entry.get("visibility") or "").strip().lower()
            acknowledged_by = [str(uid) for uid in (entry.get("acknowledged_by") or []) if str(uid).strip()]
            if user_id in acknowledged_by:
                continue
            subgroup_ids = {
                str(v or "").strip().lower()[:48]
                for v in (entry.get("subgroup_ids") or [])
                if str(v or "").strip()
            }
            if subgroup_ids and self.get_user_subgroup_id(user_id) not in subgroup_ids:
                continue
            if visibility == "party_public":
                visible.append(entry)
                continue
            if visibility == "subgroup_public":
                visible.append(entry)
                continue
            if visibility == "private_player" and str(entry.get("target_user_id") or "").strip() == user_id:
                visible.append(entry)
        return visible


    def _saved_discovery_cards_for_user(self, user_id: str | None = None) -> list[dict]:
        if not user_id:
            return []
        saved = []
        for card in list(getattr(self, "discovery_cards", []) or []):
            entry = dict(card or {})
            saved_by = [str(uid) for uid in (entry.get("saved_by") or []) if str(uid).strip()]
            if user_id in saved_by:
                saved.append(entry)
        saved.sort(key=lambda entry: float(entry.get("created_at") or 0.0), reverse=True)
        return saved

    def _visible_private_story_hooks_for_role(self, role: str, user_id: str | None = None) -> list[dict]:
        hooks = list(getattr(self, "private_story_hooks", []) or [])
        if role == "dm":
            visible = [dict(hook or {}) for hook in hooks]
            visible.sort(key=lambda entry: float(entry.get("updated_at") or entry.get("created_at") or 0.0), reverse=True)
            return visible
        if role != "player" or not user_id:
            return []
        visible = []
        for hook in hooks:
            entry = dict(hook or {})
            target_user_id = str(entry.get("target_user_id") or "").strip()
            if not target_user_id or target_user_id != str(user_id):
                continue
            visible.append(entry)
        visible.sort(key=lambda entry: float(entry.get("updated_at") or entry.get("created_at") or 0.0), reverse=True)
        return visible

    def _visible_session_quests_for_role(self, role: str, user_id: str | None = None) -> list[dict]:
        quests = list(getattr(self, "session_quests", []) or [])
        if role == "dm":
            return [deepcopy(dict(entry or {})) for entry in quests]

        role_norm = str(role or "viewer").strip().lower() or "viewer"
        user_norm = str(user_id or "").strip()
        visible: list[dict] = []
        for quest in quests:
            entry = deepcopy(dict(quest or {}))
            visibility = dict(entry.get("visibility") or {})
            mode = str(visibility.get("mode") or "dm_only").strip().lower() or "dm_only"
            roles = {
                str(r or "").strip().lower()
                for r in (visibility.get("roles") or [])
                if str(r or "").strip()
            }
            player_ids = {
                str(pid or "").strip()
                for pid in (visibility.get("player_ids") or [])
                if str(pid or "").strip()
            }
            subgroup_ids = {
                str(sid or "").strip().lower()[:48]
                for sid in (visibility.get("subgroup_ids") or [])
                if str(sid or "").strip()
            }

            allowed = False
            if mode in {"dm_only", "hidden", "hidden_locked"}:
                allowed = False
            elif mode in {"locked"}:
                allowed = role_norm in {"player", "viewer"}
            elif mode in {"private_player", "player_private", "personal"}:
                allowed = bool(user_norm and user_norm in player_ids)
            elif mode in {"party_public", "party", "public", "shared", "player_public"}:
                allowed = role_norm in {"player", "viewer"}
            elif mode in {"viewer_public", "viewers"}:
                allowed = role_norm == "viewer"
            elif mode in {"players", "player_only"}:
                allowed = role_norm == "player"

            if roles and role_norm not in roles:
                allowed = False
            if role_norm == "player" and player_ids and user_norm not in player_ids:
                allowed = False
            if role_norm == "player" and subgroup_ids and self.get_user_subgroup_id(user_norm) not in subgroup_ids:
                allowed = False
            if not allowed:
                continue

            if mode == "locked":
                entry["description"] = ""
                entry["objective_list"] = []
                entry["progress"] = {
                    "objective_status": {},
                    "objective_counts": {},
                    "completed_objectives": 0,
                    "total_objectives": 0,
                    "summary": "Locked quest",
                }
                entry["reward_bundle"] = {}
                entry["linked_handout_ids"] = []
                entry["linked_poi_ids"] = []
                entry["linked_map_ids"] = []
                entry["linked_npc_ids"] = []
                entry["linked_encounter_template_ids"] = []

            hidden_objective_ids = {
                str(oid or "").strip()
                for oid in (visibility.get("hidden_objective_ids") or [])
                if str(oid or "").strip()
            }
            if hidden_objective_ids:
                entry["objective_list"] = [
                    objective
                    for objective in (entry.get("objective_list") or [])
                    if str((objective or {}).get("id") or "").strip() not in hidden_objective_ids
                ]
                progress = dict(entry.get("progress") or {})
                objective_status = dict(progress.get("objective_status") or {})
                if objective_status:
                    progress["objective_status"] = {
                        oid: status
                        for oid, status in objective_status.items()
                        if str(oid or "").strip() not in hidden_objective_ids
                    }
                entry["progress"] = progress

            visible.append(entry)
        return visible

    def to_state_dict_for_role(self, role: str, user_id: str = None) -> dict:
        """State snapshot filtered by role."""
        self._normalize_orphan_token_owners()
        self.enforce_single_active_player_token_rule()
        role = str(role or "viewer").strip().lower() or "viewer"
        d = self.to_state_dict()
        visible_contexts = self.visible_map_contexts_for_user(user_id) if role != "dm" else set()
        visible_hazards = {
            zid: dict(zone or {})
            for zid, zone in (self.hazard_zones or {}).items()
            if not bool((zone or {}).get("hidden_from_players"))
        }
        if role == "dm":
            d["pois"] = {pid: p.to_dict(include_dm_notes=True) for pid, p in self.pois.items()}
            d["journal_entries"] = list(self.journal_entries or [])
            d["codex_entries"] = list(self.codex_entries or [])
            d["codex_links"] = list(self.codex_links or [])
            d["library_entries"] = list(self.library_entries or [])
            d["item_library_entries"] = list(self.item_library_entries or [])
            # char_profiles already sanitized by to_state_dict(); reuse to avoid double sanitization.
            d["player_inventories"] = build_player_inventory_payload_for_dm(self)
            d["party_stash"] = get_party_stash_inventory(self)
            d["party_loot_log"] = list(self.party_loot_log or [])[-120:]
            d["player_gold"] = dict(self.player_gold or {})
            d["editor_layers"] = dict(self.editor_layers or {})
            d["editor_walls"] = dict(self.editor_walls or {})
            d["editor_props"] = filter_editor_props_for_role(self.editor_props, "dm")
            d["map_settings"] = dict(self.map_settings or {})
            d["editor_paths"] = dict(self.editor_paths or {})
            d["editor_labels"] = dict(self.editor_labels or {})
            d["editor_markers"] = dict(self.editor_markers or {})
            d["editor_lights"] = dict(self.editor_lights or {})
            d["viewer_profiles"] = dict(self.viewer_profiles or {})
            d["viewer_pending_actions"] = dict(self.viewer_pending_actions or {})
            d["viewer_power_catalog"] = dict(self.viewer_power_catalog or {})
            d["hazard_zones"] = dict(self.hazard_zones or {})
            d["scene_trigger_zones"] = dict(self.scene_trigger_zones() or {})
            d["handouts"] = list(self.handouts or [])
            d["discovery_cards"] = self._visible_discovery_cards_for_role("dm", user_id)
            d["private_story_hooks"] = self._visible_private_story_hooks_for_role("dm", user_id)
            d["encounter_templates"] = list(self.encounter_templates or [])
            d["session_quests"] = self._visible_session_quests_for_role("dm", user_id)
            d["dm_notes"] = str(self.dm_notes or "")
            d["assistant_dm_permissions"] = dict(((_assistant_dm_store(self) or {}).get("users") or {}))
        else:
            d.pop("active_char_profiles", None)
            # Players and viewers can see any token that is not hidden by the DM.
            try:
                from server.handlers.common import _can_user_see_token
                user_obj = self.users.get(user_id)
            except Exception:
                _can_user_see_token = None
                user_obj = None
            d["tokens"] = {
                tid: build_token_runtime_payload(self, t)
                for tid, t in self.tokens.items()
                if (not t.hidden)
                and (normalize_map_context(getattr(t, "map_context", "world")) in visible_contexts)
                and (not _can_user_see_token or not user_obj or _can_user_see_token(self, t, user_obj))
            }
            if isinstance(d.get("combat"), dict):
                try:
                    from server.handlers.common import _combat_state_payload_for_user
                    d["combat"] = _combat_state_payload_for_user(self, user_obj, int(getattr(self, "visibility_revision", 0) or 0))
                    d["combat"].pop("_filter_summary", None)
                except Exception:
                    combat_public = dict(d["combat"] or {})
                    combat_public.pop("suspended_combatants", None)
                    combat_public.pop("fog_suspended_combatants", None)
                    combat_public.pop("hidden_suspended_combatants", None)
                    d["combat"] = combat_public
            d["journal_entries"] = [entry for entry in (self.journal_entries or []) if entry.get("shared")]
            d["codex_entries"] = [
                e for e in (self.codex_entries or [])
                if isinstance(e, dict) and (
                    e.get("visibility") == "party"
                    or (e.get("visibility") == "private" and e.get("author_id") == user_id)
                )
            ]
            d["codex_links"] = list(self.codex_links or [])
            d["library_entries"] = []
            d["item_library_entries"] = list(self.item_library_entries or [])
            if role == "player" and user_id:
                user = self.users.get(user_id)
                owner_key = normalize_profile_owner_key(user.name if user else "")
                profiles = dict(self.char_profiles or {})
                mine = list(profiles.get(owner_key, []) or [])
                if not mine and user_id in profiles:
                    mine = list(profiles.get(user_id, []) or [])
                d["char_profiles"] = sanitize_profiles_for_websocket(mine)
            else:
                d["char_profiles"] = []
            if role == "player" and user_id:
                d["player_inventory"] = get_player_inventory_for_user(self, user_id)
                d["party_stash"] = get_party_stash_inventory(self)
                d["player_gold"] = get_player_gold_for_user(self, user_id)
                d["active_char_profile_id"] = str((self.active_char_profiles or {}).get(user_id) or "")
                d["party_loot_log"] = list(self.party_loot_log or [])[-120:]
                d["saved_discoveries"] = self._saved_discovery_cards_for_user(user_id)
                try:
                    d["quick_actions"] = build_quick_actions_sync_payload(self, user_id)
                except Exception:
                    d["quick_actions"] = {}
            else:
                d["player_inventory"] = []
                d["party_stash"] = []
                d["player_gold"] = 0
                d["active_char_profile_id"] = ""
                d["party_loot_log"] = []
                d["saved_discoveries"] = []
            d["editor_layers"] = {ctx: val for ctx, val in dict(self.editor_layers or {}).items() if str(ctx) in visible_contexts}
            d["editor_walls"] = {ctx: val for ctx, val in dict(self.editor_walls or {}).items() if str(ctx) in visible_contexts}
            d["editor_props"] = {
                ctx: val for ctx, val in filter_editor_props_for_role(self.editor_props, role).items()
                if str(ctx) in visible_contexts
            }
            d["map_settings"] = {ctx: val for ctx, val in dict(self.map_settings or {}).items() if str(ctx) in visible_contexts}
            d["editor_paths"] = {ctx: val for ctx, val in dict(self.editor_paths or {}).items() if str(ctx) in visible_contexts}
            d["editor_labels"] = {ctx: val for ctx, val in dict(self.editor_labels or {}).items() if str(ctx) in visible_contexts}
            d["editor_markers"] = {ctx: val for ctx, val in dict(self.editor_markers or {}).items() if str(ctx) in visible_contexts}
            d["editor_lights"] = {ctx: val for ctx, val in dict(self.editor_lights or {}).items() if str(ctx) in visible_contexts}
            d["hazard_zones"] = {
                zid: zone for zid, zone in visible_hazards.items()
                if str((zone or {}).get("map_context") or "world") in visible_contexts
            }
            d["scene_trigger_zones"] = {}
            d["fog_maps"] = {ctx: val for ctx, val in normalize_fog_maps(self.fog_maps).items() if normalize_map_context(ctx) in visible_contexts}
            d["fog_source"] = "state_sync"
            d["split_party"] = self.split_party_state()
            d["user_subgroup_id"] = self.get_user_subgroup_id(user_id)
            d["subgroup_map_context"] = self.get_subgroup_map_context(d["user_subgroup_id"])
            # Players see handouts targeted to them or to "all"
            d["handouts"] = [
                h for h in (self.handouts or [])
                if h.get("recipients") == "all" or (isinstance(h.get("recipients"), list) and user_id in h["recipients"])
            ]
            d["discovery_cards"] = self._visible_discovery_cards_for_role(role, user_id)
            d["private_story_hooks"] = self._visible_private_story_hooks_for_role(role, user_id)
            d["encounter_templates"] = []
            d["session_quests"] = self._visible_session_quests_for_role(role, user_id)
            if role == "viewer" and user_id:
                user = self.users.get(user_id)
                # Use the same key fallback as _viewer_key_for_user in viewer_powers.py
                raw_key = str(getattr(user, "player_key", "") or "").strip()[:64] if user else ""
                viewer_key = raw_key or (f"user:{user.id}" if user else "")
                profiles = dict(self.viewer_profiles or {})
                viewer_profile = profiles.get(viewer_key) or {}
                if not viewer_profile and user:
                    legacy_keys = []
                    for candidate in (f"user:{user.id}", str(getattr(user, "id", "") or "").strip()[:64]):
                        key = str(candidate or "").strip()[:64]
                        if key and key not in legacy_keys:
                            legacy_keys.append(key)
                    for legacy_key in legacy_keys:
                        if legacy_key in profiles:
                            viewer_profile = profiles.get(legacy_key) or {}
                            break
                d["viewer_profiles"] = {viewer_key: viewer_profile} if viewer_key else {}
                d["viewer_power_catalog"] = dict(self.viewer_power_catalog or {})
                all_pending = dict(self.viewer_pending_actions or {})
                if viewer_key:
                    d["viewer_pending_actions"] = {pid: entry for pid, entry in all_pending.items() if str((entry or {}).get("viewer_key") or "") == viewer_key}
                    if not d["viewer_pending_actions"] and user:
                        legacy_pending = {}
                        legacy_keys = []
                        for candidate in (f"user:{user.id}", str(getattr(user, "id", "") or "").strip()[:64]):
                            key = str(candidate or "").strip()[:64]
                            if key and key not in legacy_keys:
                                legacy_keys.append(key)
                        if legacy_keys:
                            for pid, entry in all_pending.items():
                                entry_key = str((entry or {}).get("viewer_key") or "")
                                if entry_key in legacy_keys:
                                    legacy_pending[pid] = entry
                        d["viewer_pending_actions"] = legacy_pending
                else:
                    d["viewer_pending_actions"] = {}
            else:
                d["viewer_profiles"] = {}
                d["viewer_pending_actions"] = {}
                d["viewer_power_catalog"] = dict(self.viewer_power_catalog or {})
                if role != "player":
                    d["saved_discoveries"] = []
            d["assistant_dm_permissions"] = assistant_dm_permissions_for_user(self, user_id) if role == "assistant_dm" and user_id else {}
        d["premium_progression"] = build_premium_progression_snapshot(self, role=role, user_id=user_id)
        # active_poll: DM sees full data; others see vote counts (not per-user breakdown)
        poll = self.active_poll
        # Filter viewer-channel messages from state log for players who shouldn't see them
        if role == "player":
            d["log"] = [e for e in d.get("log", []) if e.get("channel") != "viewers"]
        if poll:
            votes = poll.get("votes") or {}
            options = poll.get("options") or []
            counts = [0] * len(options)
            for opt_idx in votes.values():
                if isinstance(opt_idx, int) and 0 <= opt_idx < len(counts):
                    counts[opt_idx] += 1
            if role == "dm":
                full_poll = dict(poll)
                full_poll.setdefault("title", "Party Vote")
                full_poll["vote_counts"] = counts
                full_poll["total_votes"] = len(votes)
                full_poll["results_mode"] = str(full_poll.get("results_mode") or "live")
                full_poll["authority_note"] = str(full_poll.get("authority_note") or "The DM keeps final say.")
                full_poll["closed_by"] = str(full_poll.get("closed_by") or "dm")
                full_poll["closed_reason"] = str(full_poll.get("closed_reason") or ("dm_closed" if full_poll.get("closed") else "active"))
                d["active_poll"] = full_poll
            else:
                d["active_poll"] = {
                    "id": poll.get("id"),
                    "title": poll.get("title") or "Party Vote",
                    "question": poll.get("question"),
                    "options": list(options),
                    "vote_counts": counts,
                    "total_votes": len(votes),
                    "created_at": poll.get("created_at"),
                    "closes_at": poll.get("closes_at"),
                    "closed": bool(poll.get("closed")),
                    "results_mode": str(poll.get("results_mode") or "live"),
                    "authority_note": str(poll.get("authority_note") or "The DM keeps final say."),
                    "closed_by": str(poll.get("closed_by") or "dm"),
                    "closed_reason": str(poll.get("closed_reason") or ("dm_closed" if poll.get("closed") else "active")),
                    "user_vote": votes.get(user_id) if user_id else None,
                }
        else:
            d["active_poll"] = None
        return d

    def to_authoritative_snapshot_for_role(self, role: str, user_id: str = None, source: str = "ws_connect") -> dict:
        """Build the reconnect snapshot v2 envelope for a single participant.

        The payload is intentionally composed from ``to_state_dict_for_role`` so
        PR 2 inherits the existing state_sync visibility/security rules while
        exposing a smaller, documented reconnect contract.
        """
        resolved_role = str(role or "viewer").strip().lower() or "viewer"
        if resolved_role not in {"dm", "assistant_dm", "player", "viewer"}:
            resolved_role = "viewer"
        state = self.to_state_dict_for_role(resolved_role, user_id)
        tokens_payload = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
        all_tokens = getattr(self, "tokens", {}) or {}
        hidden_filtered = 0
        fog_hidden_filtered = 0
        if resolved_role != "dm":
            visible_ids = {str(tid) for tid in tokens_payload.keys()}
            try:
                from server.handlers.common import is_npc_or_monster_token, is_token_touching_unrevealed_fog
            except Exception:
                is_npc_or_monster_token = None
                is_token_touching_unrevealed_fog = None
            for tid, token in all_tokens.items():
                if str(tid) in visible_ids:
                    continue
                if bool(getattr(token, "hidden", False)):
                    hidden_filtered += 1
                elif is_npc_or_monster_token and is_token_touching_unrevealed_fog and is_npc_or_monster_token(token) and is_token_touching_unrevealed_fog(self, token):
                    fog_hidden_filtered += 1

        map_ctx = normalize_map_context(state.get("dm_map_context") or getattr(self, "dm_map_context", "world"))
        fog_maps = state.get("fog_maps") if isinstance(state.get("fog_maps"), dict) else {}
        fog_entry = fog_maps.get(map_ctx) or fog_maps.get("world") or {}
        try:
            from server.handlers.map_editor import filter_door_summary_for_role
            door_summary = filter_door_summary_for_role(self, map_ctx, resolved_role)
        except Exception:
            door_summary = []
        try:
            from server.visibility import compute_visible_cell_indices
            fog_cols = int((fog_entry or {}).get("cols") or 0)
            fog_rows = int((fog_entry or {}).get("rows") or 0)
            visible_cell_count = len(compute_visible_cell_indices(self, map_ctx, fog_cols, fog_rows)) if fog_cols and fog_rows else 0
        except Exception:
            visible_cell_count = 0
        combat_state = state.get("combat") if isinstance(state.get("combat"), dict) else {}
        try:
            from server.handlers.common import _combat_state_payload_for_user
            user_obj = (getattr(self, "users", {}) or {}).get(user_id)
            combat_state = _combat_state_payload_for_user(self, user_obj, int(state.get("visibility_revision") or getattr(self, "visibility_revision", 0) or 0))
            combat_state.pop("_filter_summary", None)
        except Exception:
            pass
        active_profile_id = str(state.get("active_char_profile_id") or (self.active_char_profiles or {}).get(user_id or "") or "")
        inventory_items = state.get("player_inventory") if isinstance(state.get("player_inventory"), list) else []
        if resolved_role == "dm":
            inv_count = sum(len((row or {}).get("items") or []) if isinstance(row, dict) else 0 for row in state.get("player_inventories") or [])
        else:
            inv_count = len(inventory_items)

        authority_role = "dm" if resolved_role == "dm" else ("player" if resolved_role == "player" else "viewer")

        # PR 5: active-profile/inventory/spell reconnect hardening. character_block/
        # spells_block both key off the SAME active-profile lookup so a player who
        # loses their active profile sees both report missing_profile/missing_runtime
        # instead of Quick Actions silently going blank with no diagnosis.
        active_profile = None
        if resolved_role in {"player", "dm", "assistant_dm"} and active_profile_id and user_id:
            active_profile = _find_active_profile_for_user(self, user_id)
        profile_summary = _profile_runtime_summary(active_profile) if active_profile else {}
        if resolved_role not in {"player", "dm", "assistant_dm"}:
            character_hydration = "unknown"
        elif not active_profile_id:
            character_hydration = "missing_profile"
        elif not active_profile:
            character_hydration = "missing_runtime"
        else:
            character_hydration = "ok"

        native_for_spells = (active_profile or {}).get("nativeCharacter") if isinstance((active_profile or {}).get("nativeCharacter"), dict) else {}
        spell_state_for_snapshot = native_for_spells.get("spellState") if isinstance(native_for_spells.get("spellState"), dict) else {}
        spells_block = {
            "manifest_revision": int(getattr(self, "spell_manifest_revision", 0) or 0),
            "hydration_status": character_hydration,
            "summary": {
                "known_count": len(spell_state_for_snapshot.get("known") or []) if isinstance(spell_state_for_snapshot, dict) else 0,
                "prepared_count": len(spell_state_for_snapshot.get("prepared") or []) if isinstance(spell_state_for_snapshot, dict) else 0,
            },
        }

        character_block = {
            "active_profile_id": active_profile_id,
            "active_profile_owner": str(user_id or "") if active_profile_id else "",
            "runtime_revision": int(getattr(self, "character_runtime_revision", 0) or 0),
            "hydration_status": character_hydration,
            "summary": {
                "profile_count": len(state.get("char_profiles") or []) if isinstance(state.get("char_profiles"), list) else 0,
                "name": profile_summary.get("name", ""),
                "class_summary": profile_summary.get("classSummary", ""),
                "species_name": profile_summary.get("species_name", ""),
                "level": profile_summary.get("level"),
                "hp": profile_summary.get("hp"),
                "max_hp": profile_summary.get("maxHp"),
                "temp_hp": profile_summary.get("tempHp"),
                "ac": profile_summary.get("ac"),
                "speed": profile_summary.get("speed"),
            } if profile_summary else {"profile_count": len(state.get("char_profiles") or []) if isinstance(state.get("char_profiles"), list) else 0},
        }

        inventory_summary = {"item_count": inv_count}
        inventory_hydration = "ok" if (resolved_role == "dm" or inventory_items or active_profile_id) else "missing_profile"
        if resolved_role == "player" and user_id and isinstance(inventory_items, list):
            try:
                from server.handlers.inventory import _derive_item_actions_and_passives, _build_item_spell_cards
                equipped = [it for it in inventory_items if isinstance(it, dict) and bool(it.get("equipped"))]
                item_actions, _item_passives = _derive_item_actions_and_passives(inventory_items)
                item_spell_cards = _build_item_spell_cards(inventory_items)
                inventory_summary = {
                    "item_count": inv_count,
                    "equipped_count": len(equipped),
                    "equipped_items": [
                        {
                            "id": str(it.get("id") or ""),
                            "name": str(it.get("name") or ""),
                            "rarity": str(it.get("rarity") or ""),
                            "charges_current": it.get("charges_current"),
                            "charges_max": it.get("charges_max"),
                        }
                        for it in equipped[:20]
                    ],
                    "item_action_count": len(item_actions),
                    "item_spell_card_count": len(item_spell_cards),
                }
                inventory_hydration = "ok" if active_profile_id else "missing_profile"
            except Exception:
                inventory_hydration = "unknown"
        quick_actions_block = {}
        if resolved_role == "player" and user_id:
            try:
                quick_actions_block = build_quick_actions_sync_payload(self, user_id)
            except Exception:
                quick_actions_block = {"revision": int(getattr(self, "quick_actions_revision", 0) or 0), "hydration_status": "unknown", "diagnostics": [{"code": "spell_data_missing", "message": "Quick Actions payload could not be built."}]}

        inventory_block = {
            "revision": int(getattr(self, "inventory_revision", 0) or 0),
            "hydration_status": inventory_hydration,
            "summary": inventory_summary,
        }

        payload = {
            "snapshot_revision": 0,
            "session": {
                "id": str(self.id or ""),
                "resolved_role": resolved_role,
                "user_id": str(user_id or ""),
                "authority": {
                    "role": authority_role,
                    "is_dm": resolved_role == "dm",
                    "is_player": resolved_role == "player",
                    "is_viewer": resolved_role == "viewer",
                    "matched_via": "server_session_user",
                    "can_control_tokens": resolved_role in {"dm", "player", "assistant_dm"},
                    "can_see_hidden": resolved_role == "dm",
                },
            },
            "map": {
                "context": map_ctx,
                "mode": "world" if map_ctx == "world" else ("local" if map_ctx else "unknown"),
                "current_map_id": "" if map_ctx == "world" else map_ctx,
                "current_map_url": state.get("dm_current_map_url") or state.get("map_image_url") or "",
                "map_nav_version": int(state.get("map_nav_version") or 0),
                "map_context_revision": int(state.get("map_nav_version") or 0),
                "dm_nav_intent": int(state.get("dm_nav_intent") or 0),
                "token_state_revision": int(state.get("token_state_revision") or getattr(self, "token_state_revision", 0) or 0),
                "visibility_revision": int(state.get("visibility_revision") or 0),
                "map_document_revision": 0,
                "wall_revision": int(getattr(self, "wall_revision", 0) or 0),
                "door_revision": int(getattr(self, "door_revision", 0) or 0),
                "doors": door_summary,
            },
            "tokens": {
                "revision": int(state.get("visibility_revision") or 0),
                "token_state_revision": int(state.get("token_state_revision") or getattr(self, "token_state_revision", 0) or 0),
                "visibility_revision": int(state.get("visibility_revision") or 0),
                "items": tokens_payload,
                "count": len(tokens_payload),
                "filter_summary": {
                    "hidden_filtered": hidden_filtered,
                    "fog_hidden_filtered": fog_hidden_filtered,
                },
            },
            "combat": {
                "active": bool(combat_state.get("active")) if isinstance(combat_state, dict) else False,
                "revision": int((combat_state or {}).get("revision") or 0) if isinstance(combat_state, dict) else 0,
                "updated_at": float((combat_state or {}).get("updated_at") or 0.0) if isinstance(combat_state, dict) else 0.0,
                "visibility_revision": int((combat_state or {}).get("visibility_revision") or state.get("visibility_revision") or 0) if isinstance(combat_state, dict) else int(state.get("visibility_revision") or 0),
                "state": combat_state,
            },
            "character": character_block,
            "spells": spells_block,
            "inventory": inventory_block,
            "quick_actions": quick_actions_block,
            "fog": {
                "map_context": str((fog_entry or {}).get("map_context") or map_ctx),
                "revision": int((fog_entry or {}).get("revision") or 0) if isinstance(fog_entry, dict) else 0,
                "visibility_revision": int(state.get("visibility_revision") or 0),
                "enabled": bool((fog_entry or {}).get("enabled")) if isinstance(fog_entry, dict) else False,
                # PR 6: snapshot fog source mirrors the overall reconnect/manual-
                # refresh distinction so clients can attribute a stale-fog debug
                # log to "reconnect" specifically rather than a generic refresh.
                "source": "reconnect" if str(source or "") == "ws_connect" else "authoritative_snapshot",
                "summary": {
                    "cols": int((fog_entry or {}).get("cols") or 0) if isinstance(fog_entry, dict) else 0,
                    "rows": int((fog_entry or {}).get("rows") or 0) if isinstance(fog_entry, dict) else 0,
                },
                # Explored/revealed fog is manual DM paint plus any cells the
                # PR 7 wall/door LOS engine has auto-revealed for a player
                # token's vision (see server/visibility.py). currently_visible
                # is a same-tick LOS recompute summary (never a raw cell list,
                # to avoid bloating the snapshot on large maps); unseen stays
                # null — total-minus-visible is derivable client-side and an
                # explicit "definitely hidden" set isn't part of this model.
                "explored": {
                    "revealed_cells": str((fog_entry or {}).get("cells") or "").count("1") if isinstance(fog_entry, dict) else 0,
                    "total_cells": (int((fog_entry or {}).get("cols") or 0) * int((fog_entry or {}).get("rows") or 0)) if isinstance(fog_entry, dict) else 0,
                },
                "currently_visible": {"cell_count": visible_cell_count},
                "unseen": None,
                "visibility_source": "manual_fog_plus_wall_door_los",
                "wall_revision": int(getattr(self, "wall_revision", 0) or 0),
                "door_revision": int(getattr(self, "door_revision", 0) or 0),
            },
            "debug": {
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": str(source or "manual"),
                "legacy_state_sync_also_sent": True,
            },
        }
        return {"type": "authoritative_snapshot", "payload": payload}


def normalize_profile_owner_key(name: str) -> str:
    return " ".join(str(name or "").strip().lower().split())


def resolve_owner_identity(session, user) -> set[str]:
    """Canonical set of identity keys a user legitimately owns.

    This is the single source of truth for answering "does this user own this
    thing?" across the server. A combatant, token, or character profile belongs
    to ``user`` when its stored ``owner_id`` / owner-bucket key matches ANY key
    in this set:

      - the user's session-user id (``user.id``)
      - the normalized display-name bucket (``normalize_profile_owner_key(name)``)
      - the user's resolved ``player_key`` (``auth_<id>`` for logged-in accounts),
        i.e. the same key the authority layer
        (``server.http.session_access.resolve_session_authority``) resolves.

    Display-name comparisons must use ``normalize_profile_owner_key`` on BOTH
    sides so buckets line up regardless of casing/whitespace; raw ids and
    player_keys match verbatim. The set only ever contains keys belonging to
    this one user, so routing checks through it never widens access to another
    user's combatant/profile.
    """
    keys: set[str] = set()
    if user is None:
        return keys
    user_id = str(getattr(user, "id", "") or "").strip()
    if user_id:
        keys.add(user_id)
    name_key = normalize_profile_owner_key(getattr(user, "name", ""))
    if name_key:
        keys.add(name_key)
    player_key = str(getattr(user, "player_key", "") or "").strip()
    if player_key:
        keys.add(player_key)
    return keys


def owner_id_matches_user(owner_id, session, user) -> bool:
    """True when ``owner_id`` resolves to an identity key owned by ``user``.

    Routes all ownership comparisons through :func:`resolve_owner_identity` so
    a raw user id, the player_key, or a (normalized) display-name bucket all
    match. Returns False for an empty owner or an unmatched identity, so a
    player stays blocked from another player's combatant/profile.
    """
    owner = str(owner_id or "").strip()
    if not owner:
        return False
    identity = resolve_owner_identity(session, user)
    if not identity:
        return False
    if owner in identity:
        return True
    owner_key = normalize_profile_owner_key(owner)
    return bool(owner_key) and owner_key in identity


ACTIVE_PROFILE_ID_KEY_LIMIT = 80


def _user_bucket_key(user) -> str:
    key = normalize_profile_owner_key(getattr(user, "name", ""))
    return key or str(getattr(user, "id", "") or "")


def _find_active_profile_for_user(session, user_id: str) -> dict | None:
    """Resolve the saved profile record this user's active_char_profiles entry points at.

    Used by the reconnect snapshot to report a runtime summary (class/level/HP/AC/etc.)
    without re-deriving the full character sheet, and to distinguish "no active
    profile selected" from "active profile selected but its row is missing".
    """
    user = (getattr(session, "users", {}) or {}).get(user_id)
    if not user:
        return None
    active_id = str((getattr(session, "active_char_profiles", {}) or {}).get(user_id) or "").strip()
    if not active_id:
        return None
    owner_key = _user_bucket_key(user)
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    mine = profiles.get(owner_key)
    if not isinstance(mine, list):
        mine = profiles.get(user_id)
    if not isinstance(mine, list):
        return None
    for entry in mine:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip() == active_id:
            return entry
    return None


def _inventory_owner_key(session, user) -> str:
    base_key = _user_bucket_key(user)
    active_map = dict(getattr(session, "active_char_profiles", {}) or {})
    profile_id = str(active_map.get(getattr(user, "id", "")) or "").strip()[:ACTIVE_PROFILE_ID_KEY_LIMIT]
    if not profile_id:
        return base_key
    return f"{base_key}::profile::{profile_id}"


def _legacy_inventory_keys(user, user_id: str) -> tuple[str, ...]:
    keys: list[str] = []
    owner_key = _user_bucket_key(user)
    if owner_key:
        keys.append(owner_key)
    user_key = str(user_id or "").strip()
    if user_key and user_key not in keys:
        keys.append(user_key)
    return tuple(keys)


PARTY_STASH_KEY = "__party_stash__"


def _normalize_inventory_entry(entry: Any) -> dict | None:
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()[:80]
    if not name:
        return None
    qty = int(entry.get("qty") or 1)
    qty = max(1, min(9999, qty))
    notes = str(entry.get("notes") or "").strip()[:160]
    price = str(entry.get("price") or "").strip()[:32]
    out = {"name": name, "qty": qty, "notes": notes}
    if price:
        out["price"] = price
    source = str(entry.get("source") or "").strip()[:60]
    if source:
        out["source"] = source
    entry_id = str(entry.get("id") or "").strip()[:64]
    if entry_id:
        out["id"] = entry_id
    for key, limit in (
        ("category", 40),
        ("icon", 300),
        ("rarity", 32),
        ("magic_item_id", 64),
        ("item_type", 32),
        ("effect", 400),
        ("unidentified_description", 400),
    ):
        value = str(entry.get(key) or "").strip()[:limit]
        if value:
            out[key] = value
    if "is_magic" in entry:
        out["is_magic"] = bool(entry.get("is_magic"))
    if "is_identified" in entry:
        out["is_identified"] = bool(entry.get("is_identified"))
    if "attunement_required" in entry:
        out["attunement_required"] = bool(entry.get("attunement_required"))
    # ── Encumbrance / extradimensional container fields ──
    if "weight_lbs" in entry:
        try:
            out["weight_lbs"] = max(0.0, float(entry["weight_lbs"]))
        except Exception:
            pass
    if entry.get("extradimensional") or entry.get("is_container") or entry.get("capacity_lbs") is not None or isinstance(entry.get("bag_contents"), list):
        if entry.get("extradimensional"):
            out["extradimensional"] = True
        if entry.get("is_container"):
            out["is_container"] = True
        for k in ("own_weight_lbs", "capacity_lbs", "volume_ft3"):
            if entry.get(k) is not None:
                try:
                    out[k] = float(entry[k])
                except Exception:
                    pass
        if "is_devouring" in entry:
            out["is_devouring"] = bool(entry["is_devouring"])
        if isinstance(entry.get("bag_contents"), list):
            cleaned = []
            for sub in entry["bag_contents"]:
                norm = _normalize_inventory_entry(sub)
                if norm:
                    cleaned.append(norm)
            out["bag_contents"] = cleaned

    equipment_kind = str(entry.get("equipment_kind") or "").strip().lower()
    if equipment_kind in {"armor", "shield", "weapon", "gear"}:
        out["equipment_kind"] = equipment_kind
    armor_type = str(entry.get("armor_type") or "").strip().lower()
    if armor_type in {"light", "medium", "heavy"}:
        out["armor_type"] = armor_type
    handedness = str(entry.get("handedness") or "").strip().lower()
    if handedness in {"one_handed", "two_handed", "shield"}:
        out["handedness"] = handedness
    equip_slot = str(entry.get("equip_slot") or "").strip().lower()
    if equip_slot in {"armor", "shield", "main_hand", "off_hand"}:
        out["equip_slot"] = equip_slot
    if "equipped" in entry:
        out["equipped"] = bool(entry.get("equipped"))
    for int_key, minimum, maximum in (("base_ac", 0, 99), ("dex_cap", -5, 20), ("ac_bonus", -20, 20), ("strength_requirement", 0, 30)):
        if entry.get(int_key) is None or str(entry.get(int_key)).strip() == "":
            continue
        try:
            out[int_key] = max(minimum, min(maximum, int(entry.get(int_key))))
        except Exception:
            pass
    for text_key, limit in (("damage_dice", 20), ("damage_type", 24), ("versatile_damage", 20)):
        value = str(entry.get(text_key) or "").strip()[:limit]
        if value:
            out[text_key] = value
    if "stealth_disadvantage" in entry:
        out["stealth_disadvantage"] = bool(entry.get("stealth_disadvantage"))
    if isinstance(entry.get("weapon_properties"), list):
        cleaned_props = []
        for prop in entry.get("weapon_properties") or []:
            txt = str(prop or "").strip()[:32]
            if txt and txt not in cleaned_props:
                cleaned_props.append(txt)
        if cleaned_props:
            out["weapon_properties"] = cleaned_props
    if "attuned" in entry:
        out["attuned"] = bool(entry.get("attuned"))
    if "requires_attunement" in entry:
        out["requires_attunement"] = bool(entry.get("requires_attunement"))
    for int_key, minimum, maximum in (
        ("charges_current", 0, 9999),
        ("charges_max", 0, 9999),
        ("attack_bonus", -20, 30),
        ("damage_bonus", -20, 30),
        ("item_spell_save_dc", 0, 40),
        ("item_spell_attack_bonus", -20, 30),
        ("item_schema_version", 1, 99),
    ):
        if entry.get(int_key) is not None and str(entry.get(int_key)).strip() != "":
            try:
                out[int_key] = max(minimum, min(maximum, int(entry.get(int_key))))
            except Exception:
                pass
    for text_key, limit in (
        ("recharge_type", 32),
        ("recharge_formula", 64),
        ("healing_formula", 32),
    ):
        value = str(entry.get(text_key) or "").strip()[:limit]
        if value:
            out[text_key] = value
    if isinstance(entry.get("granted_spells"), list):
        cleaned_gs = []
        for gs in entry["granted_spells"][:12]:
            if isinstance(gs, dict):
                cleaned_gs.append({k: v for k, v in gs.items() if k in (
                    "id", "name", "charge_cost", "cast_level",
                    "uses_item_dc", "uses_item_attack_bonus",
                    "consume_spell_slot", "description",
                )})
            elif isinstance(gs, str) and gs.strip():
                cleaned_gs.append(gs.strip()[:120])
        if cleaned_gs:
            out["granted_spells"] = cleaned_gs
    return out


def get_player_inventory_for_user(session, user_id: str) -> list:
    inventories = dict(getattr(session, "player_inventories", {}) or {})
    user = (getattr(session, "users", {}) or {}).get(user_id)
    if not user:
        return []
    owner_key = _inventory_owner_key(session, user)
    mine = list(inventories.get(owner_key, []) or [])
    if not mine:
        for legacy_key in _legacy_inventory_keys(user, user_id):
            if not legacy_key or legacy_key == owner_key:
                continue
            legacy_items = list(inventories.get(legacy_key, []) or [])
            if not legacy_items:
                continue
            mine = legacy_items
            inventories[owner_key] = mine
            inventories.pop(legacy_key, None)
            session.player_inventories = inventories
            break
    return [auto_tag_extradimensional(entry) for entry in (_normalize_inventory_entry(x) for x in mine) if entry]



def bump_character_runtime_revision(session) -> int:
    next_rev = int(getattr(session, "character_runtime_revision", 0) or 0) + 1
    session.character_runtime_revision = next_rev
    return next_rev


def bump_spell_manifest_revision(session) -> int:
    next_rev = int(getattr(session, "spell_manifest_revision", 0) or 0) + 1
    session.spell_manifest_revision = next_rev
    return next_rev


def bump_quick_actions_revision(session) -> int:
    next_rev = int(getattr(session, "quick_actions_revision", 0) or 0) + 1
    session.quick_actions_revision = next_rev
    return next_rev


def bump_character_hydration_revisions(session, *, spells: bool = True, quick_actions: bool = True) -> dict:
    revs = {"character_runtime_revision": bump_character_runtime_revision(session)}
    if spells:
        revs["spell_manifest_revision"] = bump_spell_manifest_revision(session)
    if quick_actions:
        revs["quick_actions_revision"] = bump_quick_actions_revision(session)
    return revs


def _qa_slug(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


def _qa_safe_int(value, default: int = 0, minimum: int = 0, maximum: int = 99) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(minimum, min(maximum, parsed))


def _qa_spellcasting_stats(native: dict) -> dict:
    stats = native.get("spellcasting") if isinstance(native.get("spellcasting"), dict) else {}
    dc = _qa_safe_int(native.get("spellSaveDc", native.get("spell_save_dc", stats.get("saveDc", stats.get("spellSaveDc", 0)))), 0, 0, 40)
    atk_raw = native.get("spellAttack", native.get("spell_attack", stats.get("attackBonus", stats.get("spellAttack", 0))))
    atk = _qa_safe_int(str(atk_raw).replace("+", ""), 0, -20, 40)
    return {"spell_save_dc": dc, "spell_attack_bonus": atk}


def _qa_spell_action(raw, idx: int, native: dict, diagnostics: list[dict]) -> dict | None:
    from server.item_compendium import get_spell_metadata
    if isinstance(raw, dict):
        sid = str(raw.get("spellId") or raw.get("spell_id") or raw.get("id") or raw.get("name") or f"spell_{idx}").strip()
        fallback_name = str(raw.get("name") or sid).strip()
        explicit_level = raw.get("level", raw.get("spell_level", raw.get("baseLevel")))
    else:
        sid = str(raw or f"spell_{idx}").strip()
        fallback_name = sid.replace("-", " ").title()
        explicit_level = None
    if not sid:
        return None
    meta = get_spell_metadata(_qa_slug(sid)) or get_spell_metadata(sid) or {}
    if not meta and explicit_level is None:
        diagnostics.append({"code": "spell_data_missing", "message": f"Spell data is missing for {fallback_name}."})
    level_i = _qa_safe_int(explicit_level if explicit_level is not None else meta.get("level"), 0, 0, 9)
    spell_id = str(meta.get("id") or _qa_slug(sid))
    name = str(meta.get("displayName") or fallback_name or spell_id)
    stats = _qa_spellcasting_stats(native)
    slots = ((native.get("spellState") or {}).get("slots") if isinstance(native.get("spellState"), dict) else {}) or native.get("spellSlots") or {}
    available_levels = []
    if level_i > 0:
        for lvl in range(level_i, 10):
            row = slots.get(str(lvl), slots.get(lvl, None)) if isinstance(slots, dict) else None
            if isinstance(row, dict):
                max_v = _qa_safe_int(row.get("max", row.get("total", 0)), 0, 0, 99)
                used_v = _qa_safe_int(row.get("used", 0), 0, 0, 99)
                if max_v - used_v > 0:
                    available_levels.append(lvl)
            elif isinstance(row, int) and row > 0:
                available_levels.append(lvl)
    attack_type = str(meta.get("attackType") or "")
    save = str(meta.get("savingThrow") or "")
    action_kind = "cantrip_spell" if level_i == 0 else "levelled_spell"
    if level_i > 0 and available_levels and max(available_levels) > level_i:
        action_kind = "upcast_spell"
    if save:
        action_kind = "save_dc_spell" if level_i == 0 else action_kind
    return {
        "id": f"spell:{spell_id}", "kind": action_kind, "spell_id": spell_id, "name": name,
        "level": level_i, "base_level": level_i, "requires_slot": level_i > 0,
        "requires_cast_level_prompt": level_i > 0, "available_cast_levels": available_levels,
        "default_cast_level": level_i, "can_upcast": bool(available_levels and max(available_levels) > level_i),
        "attack_type": attack_type, "spell_attack_bonus": stats["spell_attack_bonus"],
        "saving_throw": save, "save_dc": stats["spell_save_dc"] if save else 0,
        "damage_formula": str(meta.get("damageFormula") or ""), "damage_type": str(meta.get("damageType") or ""),
        "disabled": False, "disabled_reason": "",
    }


def build_quick_actions_sync_payload(session, user_id: str) -> dict:
    """Server-authoritative Quick Actions hydration contract for the active player."""
    user = (getattr(session, "users", {}) or {}).get(user_id)
    active_profile_id = str((getattr(session, "active_char_profiles", {}) or {}).get(user_id) or "")
    active_profile = _find_active_profile_for_user(session, user_id) if active_profile_id else None
    inventory = get_player_inventory_for_user(session, user_id) if user else []
    diagnostics = []
    if not active_profile_id:
        diagnostics.append({"code": "no_active_character", "message": "No active character is selected."})
    elif not active_profile:
        diagnostics.append({"code": "missing_active_profile", "message": f"Active character profile {active_profile_id} could not be restored."})
    if user and inventory == []:
        diagnostics.append({"code": "inventory_not_loaded", "message": "Inventory is empty or not loaded yet."})

    weapon_actions = []
    equipped_weapons = []
    for idx, item in enumerate(inventory):
        if not isinstance(item, dict) or not bool(item.get("equipped")):
            continue
        kind = str(item.get("equipment_kind") or item.get("item_type") or item.get("category") or "").lower()
        name = str(item.get("name") or "").strip()
        if kind == "weapon" or any(h in name.lower() for h in ("dagger", "staff", "sword", "bow", "axe", "mace", "spear", "quarterstaff")):
            equipped_weapons.append(item)
            item_id = str(item.get("id") or item.get("magic_item_id") or f"weapon_{idx}")
            dmg = str(item.get("damage_dice") or item.get("damage_formula") or item.get("damage") or "").strip()
            dtype = str(item.get("damage_type") or "").strip()
            bonus = _qa_safe_int(item.get("attack_bonus", item.get("weapon_attack_bonus", 0)), 0, -20, 40)
            weapon_actions.append({"id": f"weapon:{item_id}:attack", "kind": "weapon_attack", "item_id": item_id, "name": name or "Weapon", "disabled": False, "disabled_reason": "", "attack_bonus": bonus, "damage_formula": dmg, "damage_type": dtype})
            weapon_actions.append({"id": f"weapon:{item_id}:damage", "kind": "weapon_damage", "item_id": item_id, "name": (name or "Weapon") + " Damage", "disabled": not bool(dmg), "disabled_reason": "weapon damage not found" if not dmg else "", "damage_formula": dmg, "damage_type": dtype})
    if active_profile_id and not equipped_weapons:
        diagnostics.append({"code": "no_equipped_weapon", "message": "No equipped weapon is available for Quick Actions."})

    item_actions, _passives, item_spell_cards = [], [], []
    try:
        from server.handlers.inventory import _derive_item_actions_and_passives, _build_item_spell_cards
        item_actions, _passives = _derive_item_actions_and_passives(inventory)
        # Quick actions surface unequipped/unattuned item spells as disabled cards
        # so the UI can explain why they cannot be cast.
        item_spell_cards = _build_item_spell_cards(inventory, include_unavailable=True)
    except Exception:
        diagnostics.append({"code": "spell_data_missing", "message": "Item action/spell data could not be built."})

    for item in inventory:
        if not isinstance(item, dict):
            continue
        has_granted = bool(item.get("granted_spells") or item.get("grantedSpells") or item.get("itemSpells") or item.get("item_spells") or item.get("spellsGranted") or item.get("spellGrants"))
        if not has_granted:
            continue
        if not bool(item.get("equipped")):
            diagnostics.append({"code": "item_not_equipped", "message": f"{item.get('name') or 'Item'} is not equipped."})
        if bool(item.get("attunement_required")) and not bool(item.get("attuned")):
            diagnostics.append({"code": "item_not_attuned", "message": f"{item.get('name') or 'Item'} is not attuned."})
        try:
            if int(item.get("charges_max") or 0) > 0 and int(item.get("charges_current") or 0) <= 0:
                diagnostics.append({"code": "no_charges", "message": f"{item.get('name') or 'Item'} has no charges."})
        except Exception:
            pass

    native = active_profile.get("nativeCharacter") if isinstance(active_profile, dict) and isinstance(active_profile.get("nativeCharacter"), dict) else {}
    spell_state = native.get("spellState") if isinstance(native.get("spellState"), dict) else {}
    known = spell_state.get("known") if isinstance(spell_state.get("known"), list) else []
    prepared = spell_state.get("prepared") if isinstance(spell_state.get("prepared"), list) else []
    spell_actions = []
    seen_spells = set()
    for idx, raw in enumerate((prepared or known)[:80]):
        action = _qa_spell_action(raw, idx, native, diagnostics)
        if action and action["spell_id"] not in seen_spells:
            seen_spells.add(action["spell_id"])
            spell_actions.append(action)
    for card in item_spell_cards:
        if isinstance(card, dict):
            card.setdefault("kind", "item_granted_spell")
            card.setdefault("requires_cast_level_prompt", False)

    hydration = "ok" if active_profile_id and active_profile else ("missing_runtime" if active_profile_id else "missing_profile")
    return {
        "active_profile_id": active_profile_id,
        "character_runtime_revision": int(getattr(session, "character_runtime_revision", 0) or 0),
        "spell_manifest_revision": int(getattr(session, "spell_manifest_revision", 0) or 0),
        "inventory_revision": int(getattr(session, "inventory_revision", 0) or 0),
        "quick_actions_revision": int(getattr(session, "quick_actions_revision", 0) or 0),
        "revision": int(getattr(session, "quick_actions_revision", 0) or 0),
        "hydration_status": hydration,
        "weapon_actions": weapon_actions,
        "spell_actions": spell_actions,
        "item_actions": item_actions,
        "item_spell_cards": item_spell_cards,
        "charges": [{"item_id": str(i.get("id") or ""), "name": str(i.get("name") or ""), "charges_current": i.get("charges_current"), "charges_max": i.get("charges_max")} for i in inventory if isinstance(i, dict) and (i.get("charges_current") is not None or i.get("charges_max") is not None)],
        "disabled_reasons": diagnostics,
        "diagnostics": diagnostics,
    }

def get_party_stash_inventory(session) -> list:
    inventories = dict(getattr(session, "player_inventories", {}) or {})
    stash = list(inventories.get(PARTY_STASH_KEY, []) or [])
    return [entry for entry in (_normalize_inventory_entry(x) for x in stash) if entry]


def get_player_gold_for_user(session, user_id: str) -> int:
    balances = dict(getattr(session, "player_gold", {}) or {})
    user = (getattr(session, "users", {}) or {}).get(user_id)
    if not user:
        return 0
    owner_key = _inventory_owner_key(session, user)
    value = balances.get(owner_key, None)
    migrated_from = None
    if value is None:
        for legacy_key in _legacy_inventory_keys(user, user_id):
            if not legacy_key or legacy_key == owner_key:
                continue
            if legacy_key in balances:
                value = balances.get(legacy_key, 0)
                migrated_from = legacy_key
                break
    if value is None:
        value = 0
    try:
        amount = int(round(float(value or 0)))
    except Exception:
        amount = 0
    amount = max(0, min(999999999, amount))
    if migrated_from:
        balances.pop(migrated_from, None)
        balances[owner_key] = amount
        session.player_gold = balances
    return amount


def set_player_gold_for_user(session, user_id: str, amount: int) -> int:
    balances = dict(getattr(session, "player_gold", {}) or {})
    user = (getattr(session, "users", {}) or {}).get(user_id)
    if not user:
        return 0
    owner_key = _inventory_owner_key(session, user)
    try:
        clean = int(round(float(amount or 0)))
    except Exception:
        clean = 0
    clean = max(0, min(999999999, clean))
    if clean > 0:
        balances[owner_key] = clean
    else:
        balances.pop(owner_key, None)
    for legacy_key in _legacy_inventory_keys(user, user_id):
        if legacy_key and legacy_key != owner_key:
            balances.pop(legacy_key, None)
    session.player_gold = balances
    return clean


def build_player_inventory_payload_for_dm(session) -> list:
    inventories = dict(getattr(session, "player_inventories", {}) or {})
    payload = []
    seen = set()
    users = dict(getattr(session, "users", {}) or {})
    for uid, user in users.items():
        if getattr(user, "role", "viewer") == "viewer":
            continue
        owner_key = _inventory_owner_key(session, user)
        seen.add(owner_key)
        items = get_player_inventory_for_user(session, uid)
        payload.append({
            "owner_key": owner_key,
            "user_id": uid,
            "name": getattr(user, "name", "Unknown") or "Unknown",
            "role": getattr(user, "role", "player"),
            "active_char_profile_id": str((getattr(session, "active_char_profiles", {}) or {}).get(uid) or ""),
            "gold": get_player_gold_for_user(session, uid),
            "items": items,
        })
    for owner_key, raw_items in inventories.items():
        if owner_key == PARTY_STASH_KEY:
            continue
        if owner_key in seen:
            continue
        items = [entry for entry in (_normalize_inventory_entry(x) for x in list(raw_items or [])) if entry]
        if not items:
            continue
        payload.append({
            "owner_key": str(owner_key),
            "user_id": "",
            "name": str(owner_key).title() or "Unknown",
            "role": "player",
            "gold": 0,
            "items": items,
        })
    payload.sort(key=lambda entry: (0 if entry.get("role") == "dm" else 1, str(entry.get("name") or "").lower()))
    return payload


def build_editor_prop_interaction_model(item: dict, role: str) -> dict:
    raw = dict(item or {})
    custom_model = normalize_interactable(raw.get("interactable"))
    if custom_model:
        return custom_model
    kind = str(raw.get("kind") or "").strip().lower()
    normalized_role = str(role or "viewer").strip().lower() or "viewer"
    is_dm = normalized_role == "dm"
    is_shop = kind in {"merchant", "store", "tavern", "blacksmith", "market_stall", "inn", "shop", "shop_front"}
    is_chest = kind == "chest"
    is_door = kind == "door"
    hidden = bool(raw.get("hidden")) if kind in {"chest", "merchant", "store", "tavern", "blacksmith", "market_stall", "inn", "shop", "shop_front"} else False
    actions = []

    if is_chest:
        actions.append({
            "id": "open_contents",
            "label": "Open Chest" if is_dm else "Browse Chest",
            "intent": "contents",
            "requires_dm": False,
            "visible": not hidden or is_dm,
        })
        if is_dm:
            actions.append({
                "id": "toggle_visibility",
                "label": "Reveal Chest" if hidden else "Hide Chest",
                "intent": "visibility",
                "requires_dm": True,
                "visible": True,
            })
    elif is_shop:
        actions.append({
            "id": "open_contents",
            "label": "Manage Stock" if is_dm else "Browse Stock",
            "intent": "contents",
            "requires_dm": False,
            "visible": not hidden or is_dm,
        })
        if is_dm:
            actions.append({
                "id": "toggle_visibility",
                "label": "Reveal Shop" if hidden else "Hide Shop",
                "intent": "visibility",
                "requires_dm": True,
                "visible": True,
            })
    elif is_door and is_dm:
        is_open = str(raw.get("state") or "closed").strip().lower() == "open"
        is_locked = bool(raw.get("locked"))
        actions.extend([
            {
                "id": "toggle_door",
                "label": "Close Door" if is_open else "Open Door",
                "intent": "door_state",
                "requires_dm": True,
                "visible": True,
            },
            {
                "id": "toggle_lock",
                "label": "Unlock Door" if is_locked else "Lock Door",
                "intent": "door_lock",
                "requires_dm": True,
                "visible": True,
            },
        ])

    return {
        "kind": kind or "prop",
        "role": normalized_role,
        "hidden_from_players": hidden,
        "supports_contents": bool(is_chest or is_shop),
        "supports_shop": bool(is_shop),
        "supports_door": bool(is_door),
        "actions": [action for action in actions if action.get("visible")],
    }


def filter_editor_props_for_role(editor_props: dict, role: str) -> dict:
    data = dict(editor_props or {})
    filtered: dict = {}
    for ctx, items in data.items():
        safe_items = []
        for item in list(items or []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            _can_be_hidden = {"chest", "merchant", "store", "tavern", "blacksmith", "market_stall", "inn", "shop"}
            hidden = bool(item.get("hidden")) if kind in _can_be_hidden else False
            if role != "dm" and hidden:
                continue
            # PR 7: a secret/hidden door's existence and position must not
            # leak to non-DM roles until the DM marks it "revealed". The LOS
            # engine (server/visibility.py) still uses the real geometry from
            # session.editor_props directly, so a secret closed door keeps
            # blocking sight even though players are never told it's there.
            if role != "dm" and kind == "door" and bool(item.get("secret")) and not bool(item.get("revealed")):
                continue
            item_copy = dict(item)
            # Normalize hidden=False for prop kinds that shouldn't be hidden (fix legacy data)
            if kind not in _can_be_hidden:
                item_copy["hidden"] = False
            if role != "dm" and kind == "door":
                item_copy.pop("secret", None)
                item_copy.pop("revealed", None)
            item_copy["interactable"] = build_editor_prop_interaction_model(item_copy, role)
            safe_items.append(item_copy)
        filtered[str(ctx)] = safe_items
    return filtered


def _clean_character_ref(value: Any, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]

def _profile_ref_candidates(profile: dict) -> set[str]:
    if not isinstance(profile, dict):
        return set()
    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    identity = native.get("identity") if isinstance(native.get("identity"), dict) else {}
    import_meta = profile.get("importMeta") if isinstance(profile.get("importMeta"), dict) else {}
    native_meta = profile.get("nativeMeta") if isinstance(profile.get("nativeMeta"), dict) else {}
    raw = [
        profile.get("id"), profile.get("profile_id"), profile.get("profileId"),
        profile.get("libraryId"), profile.get("library_id"),
        profile.get("characterId"), profile.get("character_id"),
        identity.get("id"), identity.get("characterId"), identity.get("libraryId"),
        import_meta.get("character_id"), import_meta.get("characterId"), import_meta.get("ddbId"),
        native_meta.get("character_id"), native_meta.get("characterId"), native_meta.get("libraryId"),
    ]
    out: set[str] = set()
    for value in raw:
        text = _clean_character_ref(value)
        if not text:
            continue
        out.add(text)
        if text.startswith("library:"):
            out.add(text.split(":", 1)[1])
        else:
            out.add(f"library:{text}")
    return out

def resolve_token_character_profile(session: "Session", token) -> dict | None:
    """Resolve the saved character profile linked to *token*.

    Tokens intentionally store only lightweight profile references plus board state;
    the profile row remains the character-sheet source of truth.
    """
    if session is None or token is None:
        return None
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    refs = {
        _clean_character_ref(getattr(token, "profile_id", "")),
        _clean_character_ref(getattr(token, "library_id", "")),
        _clean_character_ref(getattr(token, "character_id", "")),
    }
    refs.discard("")
    owner_id = _clean_character_ref(getattr(token, "owner_id", ""), 64)
    active_id = _clean_character_ref((getattr(session, "active_char_profiles", {}) or {}).get(owner_id))
    if active_id:
        refs.add(active_id)
    owner_keys = []
    if owner_id:
        owner_keys.append(owner_id)
        user = (getattr(session, "users", {}) or {}).get(owner_id)
        name_key = normalize_profile_owner_key(getattr(user, "name", "")) if user else ""
        if name_key:
            owner_keys.append(name_key)
    search_buckets = []
    for key in owner_keys:
        if key in profiles and key not in search_buckets:
            search_buckets.append(key)
    for key in profiles.keys():
        if key not in search_buckets:
            search_buckets.append(key)
    fallback = None
    for key in search_buckets:
        rows = profiles.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidates = _profile_ref_candidates(row)
            if refs and candidates.intersection(refs):
                return row
            if active_id and active_id in candidates:
                return row
            if fallback is None and owner_id and key in owner_keys:
                fallback = row
    return fallback

def _first_int(*values, minimum: int | None = None) -> int | None:
    for value in values:
        if value is None or str(value).strip() == "":
            continue
        try:
            parsed = int(value)
        except Exception:
            continue
        if minimum is not None and parsed < minimum:
            continue
        return parsed
    return None

def _profile_runtime_summary(profile: dict) -> dict:
    if not isinstance(profile, dict):
        return {}
    native = profile.get("nativeCharacter") if isinstance(profile.get("nativeCharacter"), dict) else {}
    runtime = profile.get("nativeRuntime") if isinstance(profile.get("nativeRuntime"), dict) else {}
    identity = native.get("identity") if isinstance(native.get("identity"), dict) else {}
    presentation = native.get("presentation") if isinstance(native.get("presentation"), dict) else {}
    species = native.get("species") if isinstance(native.get("species"), dict) else {}
    classes = native.get("classes") if isinstance(native.get("classes"), list) else []
    first_class = classes[0] if classes and isinstance(classes[0], dict) else {}
    class_data = native.get("class") if isinstance(native.get("class"), dict) else {}
    sheet = profile.get("charSheet") if isinstance(profile.get("charSheet"), dict) else {}
    book = profile.get("charBook") if isinstance(profile.get("charBook"), dict) else {}
    runtime_hp = runtime.get("hp") if isinstance(runtime.get("hp"), dict) else {}
    runtime_combat = runtime.get("combat") if isinstance(runtime.get("combat"), dict) else {}
    runtime_speed = runtime.get("speed") if isinstance(runtime.get("speed"), dict) else {}
    sheet_hp = sheet.get("hp") if isinstance(sheet.get("hp"), dict) else {}
    max_hp = _first_int(runtime_hp.get("max"), runtime_combat.get("maxHP"), runtime_combat.get("maxHp"), sheet_hp.get("max"), book.get("maxHp"), profile.get("hp"), minimum=1)
    cur_hp = _first_int(runtime_hp.get("current"), runtime_combat.get("currentHP"), runtime_combat.get("currentHp"), sheet_hp.get("current"), book.get("currentHp"), profile.get("curhp"), minimum=0)
    temp_hp = _first_int(runtime_hp.get("temp"), runtime_combat.get("tempHP"), runtime_combat.get("tempHp"), sheet_hp.get("temp"), book.get("tempHp"), profile.get("tempHp"), minimum=0)
    class_name = str(first_class.get("name") or class_data.get("name") or book.get("className") or profile.get("classSummary") or "").strip()
    subclass = str(first_class.get("subclassName") or first_class.get("subclass") or book.get("subclass") or "").strip()
    class_summary = str(profile.get("classSummary") or (f"{class_name} ({subclass})" if class_name and subclass else class_name)).strip()
    portrait = str(identity.get("portraitUrl") or sheet.get("avatarUrl") or book.get("avatarUrl") or profile.get("avatarUrl") or "").strip()
    token_image = str(identity.get("tokenImageUrl") or sheet.get("tokenImageUrl") or book.get("tokenImageUrl") or profile.get("tokenImageUrl") or portrait).strip()
    return {
        "profile_id": _clean_character_ref(profile.get("id")),
        "library_id": _clean_character_ref(profile.get("libraryId") or profile.get("library_id") or profile.get("id")),
        "character_id": _clean_character_ref(profile.get("characterId") or profile.get("character_id") or identity.get("characterId") or identity.get("id")),
        "name": str(profile.get("name") or identity.get("name") or book.get("name") or sheet.get("name") or "").strip(),
        "image_url": token_image,
        "portraitUrl": portrait,
        "tokenImageUrl": token_image,
        "classSummary": class_summary,
        "class_id": str(first_class.get("classId") or class_data.get("id") or profile.get("classId") or "").strip().lower(),
        "species_id": str(species.get("id") or "").strip().lower(),
        "species_name": str(species.get("name") or sheet.get("species") or book.get("race") or book.get("species") or "").strip(),
        "level": _first_int(runtime.get("levelTotal"), sheet.get("totalLevel"), sheet.get("level"), book.get("level"), profile.get("level"), minimum=0),
        "maxHp": max_hp,
        "hp": cur_hp,
        "tempHp": temp_hp,
        "ac": _first_int(runtime_combat.get("ac"), runtime.get("ac"), sheet.get("ac"), book.get("ac"), profile.get("ac"), minimum=0),
        "speed": _first_int(runtime_combat.get("speed"), runtime_speed.get("walk"), sheet.get("speed"), book.get("speed"), profile.get("speed"), minimum=0),
        "actions": list(runtime.get("actions") or native.get("actions") or []),
        "spells": list(runtime.get("spells") or native.get("spells") or []),
    }

def build_token_runtime_payload(session: "Session", token) -> dict:
    payload = token.to_dict()
    profile = resolve_token_character_profile(session, token)
    if not profile:
        return payload
    summary = _profile_runtime_summary(profile)
    payload["profile_id"] = _clean_character_ref(getattr(token, "profile_id", "") or summary.get("profile_id"))
    payload["libraryId"] = _clean_character_ref(getattr(token, "library_id", "") or summary.get("library_id"))
    payload["characterId"] = _clean_character_ref(getattr(token, "character_id", "") or summary.get("character_id"))
    payload["characterProfileLinked"] = True
    for key in ("name", "image_url", "portraitUrl", "tokenImageUrl", "classSummary", "class_id", "species_id", "species_name", "level", "ac", "speed"):
        value = summary.get(key)
        if value is not None and value != "":
            payload[key] = value
    if summary.get("maxHp") is not None:
        payload["maxHp"] = summary["maxHp"]
    current_hp = getattr(token, "hp", None)
    if current_hp is None:
        current_hp = summary.get("hp")
    if current_hp is not None:
        payload["hp"] = max(0, int(current_hp))
        if payload.get("maxHp") is not None:
            payload["hp"] = min(int(payload["maxHp"]), int(payload["hp"]))
    payload["tempHp"] = max(0, int(getattr(token, "temp_hp", summary.get("tempHp") or 0) or 0))
    return payload


# Global session store (Phase 1: in-memory only)
_sessions: Dict[str, Session] = {}


def create_session(dm_name: str) -> tuple[Session, User]:
    session_id = generate_code(12)
    session = Session(id=session_id)
    dm_id = secrets.token_hex(8)
    dm = User(id=dm_id, name=dm_name, role="dm")
    session.users[dm_id] = dm
    session.dm_id = dm_id
    _sessions[session_id] = session
    session.add_log(f"{dm_name} created the session as DM.", "system")
    return session, dm


def get_session(session_id: str) -> Optional[Session]:
    return _sessions.get(session_id)


def join_session(session_id: str, invite_code: str, user_name: str, player_key: str = "") -> tuple[Optional[Session], Optional[User], str]:
    """Returns (session, user, error_message).
    player_key: stable browser fingerprint — lets returning players reclaim their identity.
    """
    session = _sessions.get(session_id)
    if not session:
        return None, None, "Session not found."

    invite_code = invite_code.strip().upper()
    if invite_code == session.player_invite:
        role = "player"
    elif invite_code == session.viewer_invite:
        role = "viewer"
    else:
        return None, None, "Invalid invite code."

    # Check if this player is returning (same player_key or same name+role)
    returning_user = None
    if player_key:
        # Match by stable key first
        returning_user = next(
            (u for u in session.users.values()
             if u.role == role and getattr(u, 'player_key', '') == player_key),
            None
        )
    if not returning_user:
        # Fallback: match by name + role (reconnect by name)
        returning_user = next(
            (u for u in session.users.values()
             if u.role == role and u.name.lower() == user_name.lower() and not u.connected),
            None
        )

    if returning_user:
        if player_key:
            # Backfill/refresh stable auth-linked player_key for returning participants.
            # This prevents authority sync from downgrading authenticated players/viewers
            # to viewer mode when legacy session rows were created before auth key binding.
            returning_user.player_key = player_key
        returning_user.connected = False  # will be set True on WS connect
        session.add_log(f"{returning_user.name} returned to the session.", "system")
        return session, returning_user, ""

    # New player
    user_id = secrets.token_hex(8)
    user = User(id=user_id, name=user_name, role=role)
    if player_key:
        user.player_key = player_key
    session.users[user_id] = user
    return session, user, ""


def create_token(session: Session, dm_id: str, name: str, x: float, y: float,
                 color: str = "#e74c3c", shape: str = "circle",
                 width: float = 40, height: float = 40,
                 owner_id: Optional[str] = None,
             hp: Optional[int] = None,
             max_hp: Optional[int] = None,
             temp_hp: int = 0,
             hidden_hp: bool = False,
             hidden: bool = False,
             initiative_mod: int = 0,
             ac: Optional[int] = None,
             speed: Optional[int] = None,
             token_type: str = "player",
             notes: str = "",
             conditions: list = None,
             condition_timers: dict | None = None,
             level: Optional[int] = None,
             faction: str = "",
             passive_perception: Optional[int] = None,
             map_context: str = "world",
             staged: bool = False,
             image_url: Optional[str] = None,
             save_bonuses: dict | None = None,
             vision_enabled: bool = False,
             vision_radius: int = 0,
             bright_radius: int = 0,
             dim_radius: int = 0,
             has_darkvision: bool = False,
             darkvision_radius: int = 0,
             creature_id: str = "",
             creature_type: str = "",
             monster_type: str = "",
             cr: str = "",
             profile_id: str = "",
             library_id: str = "",
             character_id: str = "") -> Token:
    token_id = secrets.token_hex(6)
    token = Token(
        id=token_id, name=name, x=x, y=y,
        width=width, height=height, color=color,
        shape=shape, owner_id=owner_id, hp=hp, max_hp=max_hp, temp_hp=int(temp_hp or 0), hidden_hp=hidden_hp, hidden=hidden, initiative_mod=int(initiative_mod or 0), ac=(int(ac) if ac is not None else None), speed=(int(speed) if speed is not None else None), token_type=str(token_type or "player"), notes=str(notes or "")[:2000], conditions=list(conditions or []), condition_timers=dict(condition_timers or {}), level=(int(level) if level is not None else None), faction=str(faction or "")[:100], passive_perception=(int(passive_perception) if passive_perception is not None else None), map_context=map_context, staged=staged, image_url=(str(image_url or "")[:300] or None), save_bonuses=dict(save_bonuses or {}), vision_enabled=bool(vision_enabled), vision_radius=max(0, int(vision_radius or 0)), bright_radius=max(0, int(bright_radius or 0)), dim_radius=max(0, int(dim_radius or 0)), has_darkvision=bool(has_darkvision), darkvision_radius=max(0, int(darkvision_radius or 0)), creature_id=str(creature_id or "")[:120], creature_type=str(creature_type or "")[:40], monster_type=str(monster_type or "")[:60], cr=str(cr or "")[:16], profile_id=str(profile_id or "")[:120], library_id=str(library_id or "")[:120], character_id=str(character_id or "")[:120],
    )
    session.tokens[token_id] = token
    return token


def grant_temp_permission(session: Session, token_id: str, user_id: str, duration: int = 30) -> bool:
    token = session.tokens.get(token_id)
    if not token:
        return False
    token.temp_permissions[user_id] = time.time() + duration
    return True
