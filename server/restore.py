"""
server/restore.py — Rebuild in-memory Session objects from persisted DB data.

This module is used when the server restarts or a WS client connects to a session
that is no longer in the in-memory store.
"""
import os
import secrets
from server.session import _sessions, Session, Token, User, POI, ChatParticipant, normalize_interactable
from server.map_document import build_map_documents_from_session, hydrate_session_from_map_documents
from server.persistence_schema import normalize_persisted_campaign_data
from server.quest_progression import resolve_session_quest_progression
from server.character.summon_runtime import reconcile_session_active_summons
from server.character.profile_sanitize import clean_char_profiles_map


def _valid_map_contexts(session: Session) -> set[str]:
    contexts = {"world"}
    contexts.update(str(k or "").strip() for k in (getattr(session, "pois", {}) or {}).keys())
    contexts.update(str(k or "").strip() for k in (getattr(session, "map_documents", {}) or {}).keys())
    contexts.discard("")
    return contexts


def _normalize_token_map_context(session: Session, raw_ctx) -> str:
    ctx = str(raw_ctx or "world").strip()[:80] or "world"
    return ctx if ctx in _valid_map_contexts(session) else "world"


def _normalize_restored_dm_map_state(session: Session, data: dict) -> None:
    raw_ctx = str(data.get("dm_map_context") or "world").strip()[:80] or "world"
    if raw_ctx != "world":
        pois = dict(getattr(session, "pois", {}) or {})
        docs = dict(getattr(session, "map_documents", {}) or {})
        if raw_ctx not in pois and raw_ctx not in docs:
            raw_ctx = "world"

    if raw_ctx == "world":
        session.dm_map_context = "world"
        session.dm_current_map_url = None
    else:
        poi = (getattr(session, "pois", {}) or {}).get(raw_ctx)
        poi_url = str(getattr(poi, "local_map_url", "") or "").strip()
        doc = (getattr(session, "map_documents", {}) or {}).get(raw_ctx)
        doc_assets = doc.get("assets") if isinstance(doc, dict) else {}
        doc_url = str((doc_assets or {}).get("background_url") or "").strip()
        persisted_url = str(data.get("dm_current_map_url") or "").strip()
        session.dm_map_context = raw_ctx
        session.dm_current_map_url = poi_url or doc_url or persisted_url or None

    # Keep nav counters monotonic after restore so stale queued messages can't win.
    try:
        map_nav_version = int(data.get("map_nav_version", 0) or 0)
    except Exception:
        map_nav_version = 0
    map_nav_version = max(0, map_nav_version)
    if session.dm_map_context != "world":
        map_nav_version = max(1, map_nav_version)
    session.map_nav_version = map_nav_version

    try:
        restored_intent = int(data.get("dm_nav_intent", 0) or 0)
    except Exception:
        restored_intent = 0
    session.dm_nav_intent = max(int(session.map_nav_version or 0), max(0, restored_intent))


def _migrate_journal_to_codex(session: Session) -> None:
    """One-time idempotent migration: seed codex_entries from journal_entries if codex is empty."""
    import time as _time
    if session.codex_entries:
        return
    journal = list(getattr(session, "journal_entries", []) or [])
    if not journal:
        return
    migrated = []
    for entry in journal:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "").strip()[:64]
        if not entry_id:
            continue
        shared = bool(entry.get("shared", False))
        migrated.append({
            "id": entry_id,
            "type": "lore",
            "visibility": "party" if shared else "dm",
            "author_id": "__dm__",
            "title": str(entry.get("title") or "").strip()[:240] or "Untitled",
            "content_md": str(entry.get("content_md") or entry.get("content") or "")[:16000],
            "created_at": float(entry.get("created_at") or _time.time()),
            "updated_at": float(entry.get("updated_at") or _time.time()),
            "tags": [],
            "poi_id": str(entry.get("poi_id") or "").strip()[:64] or None,
        })
    session.codex_entries = migrated


def _restore_chat_participants(session: Session, data: dict) -> None:
    """Restore chat_participants from persisted data.

    If CHAT_BRIDGE_PERSIST_PARTICIPANTS=false (the default), participants are
    restored with is_active=False so they retain their inventory but must
    !join again.  Set to 'true' to restore them as fully active.

    A reserved "__config__" entry carries the per-campaign persistence mode
    (everything / stats / nothing) chosen by the DM.
    """
    persist = os.environ.get("CHAT_BRIDGE_PERSIST_PARTICIPANTS", "false").strip().lower()
    keep_active = persist in {"1", "true", "yes"}

    raw_map = data.get("chat_participants") or {}

    config = raw_map.get("__config__")
    if isinstance(config, dict):
        mode = str(config.get("persistence_mode") or "everything").strip().lower()
        if mode in ("everything", "stats", "nothing"):
            session.chat_persistence_mode = mode

    for key, p in raw_map.items():
        if key == "__config__" or not isinstance(p, dict):
            continue
        session.chat_participants[key] = ChatParticipant(
            twitch_username=str(p.get("twitch_username") or key),
            display_name=str(p.get("display_name") or key),
            joined_at=float(p.get("joined_at") or 0),
            inventory=list(p.get("inventory") or []),
            is_active=keep_active and bool(p.get("is_active", True)),
            twitch_user_id=str(p.get("twitch_user_id", "") or ""),
            character_name=str(p.get("character_name", "") or ""),
            last_joined_session_id=str(p.get("last_joined_session_id", "") or ""),
            lifetime_stats=dict(p.get("lifetime_stats", None) or {
                "sessions_joined": 0, "items_used": 0, "damage_dealt": 0, "kills": 0,
            }),
            arena_stats=dict(p.get("arena_stats", None) or {
                "wins": 0, "losses": 0, "arena_gold": 0,
            }),
            arena_character=dict(p.get("arena_character", None) or {}),
        )


def restore_session_from_db(data: dict):
    """Rebuild a full Session object from persisted DB data.

    Returns (session, dm_id) tuple.
    """
    data = normalize_persisted_campaign_data(data)

    session = Session(
        id=data["id"],
        player_invite=data["player_invite"],
        viewer_invite=data["viewer_invite"],
        created_at=data["created_at"],
    )
    session.name = data.get("name", "Campaign")
    session.map_image_url = data.get("map_image_url")
    session.fog_maps = data.get("fog_maps", {}) or {}
    raw_combat = data.get("combat", {}) or {}
    session.combat = (
        raw_combat
        if isinstance(raw_combat, dict) and "combatants" in raw_combat
        else {"active": False, "turn": 0, "combatants": []}
    )
    session.journal_entries = data.get("journal_entries", []) or []
    session.codex_entries = data.get("codex_entries", []) or []
    session.codex_links = data.get("codex_links", []) or []
    _migrate_journal_to_codex(session)
    session.library_entries = data.get("library_entries", []) or []
    session.item_library_entries = data.get("item_library_entries", []) or []
    session.char_profiles = data.get("char_profiles", {}) or {}
    # Migration: strip runtime caches from previously-persisted profiles so an
    # already-oversized char_profiles field is shrunk safely on first load,
    # without deleting any real character data.
    clean_char_profiles_map(session.char_profiles)
    session.active_char_profiles = data.get("active_char_profiles", {}) or {}
    session.player_inventories = data.get("player_inventories", {}) or {}
    session.player_gold = data.get("player_gold", {}) or {}
    session.party_loot_log = data.get("party_loot_log", []) or []
    session.viewer_profiles = data.get("viewer_profiles", {}) or {}
    session.viewer_pending_actions = data.get("viewer_pending_actions", {}) or {}
    session.viewer_power_catalog = data.get("viewer_power_catalog", {}) or {}
    session.hazard_zones = data.get("hazard_zones", {}) or {}
    session.corpse_states = data.get("corpse_states", {}) or {}
    session.corpse_dm_config = data.get("corpse_dm_config", {}) or {}
    session.handouts = data.get("handouts", []) or []
    session.discovery_cards = data.get("discovery_cards", []) or []
    session.private_story_hooks = data.get("private_story_hooks", []) or []
    session.encounter_templates = data.get("encounter_templates", []) or []
    session.quest_templates = data.get("quest_templates", []) or []
    session.session_quests = data.get("session_quests", []) or []
    session.session_quests = resolve_session_quest_progression(session)
    session.quest_board_bindings = data.get("quest_board_bindings", []) or []
    session.sound_state = data.get("sound_state", {}) or {}
    session.weather_state = data.get("weather_state", {}) or {}
    session.world_state = data.get("world_state", {}) or {}
    session.active_poll = data.get("active_poll")
    session.show_viewer_presence = bool(data.get("show_viewer_presence", False))

    _restore_chat_participants(session, data)

    map_documents = data.get("map_documents", {}) or {}
    if map_documents:
        hydrate_session_from_map_documents(session, map_documents)
    else:
        session.editor_layers = data.get("editor_layers", {}) or {}
        session.editor_walls = data.get("editor_walls", {}) or {}
        session.editor_props = data.get("editor_props", {}) or {}
        session.map_settings = data.get("map_settings", {}) or {}
        session.editor_paths = data.get("editor_paths", {}) or {}
        session.editor_labels = data.get("editor_labels", {}) or {}
        session.editor_markers = data.get("editor_markers", {}) or {}
        session.editor_lights = data.get("editor_lights", {}) or {}
        session.map_documents = build_map_documents_from_session(session)

    # Restore DM slot — use persisted dm_id so returning DMs reconnect correctly
    dm_id = data.get("dm_id") or secrets.token_hex(8)
    dm = User(id=dm_id, name=data["dm_name"], role="dm", connected=False)
    # Preserve the DM↔auth linkage so a returning DM is not resolved as a stranger
    # after a restart/restore (which would deny the websocket handshake).
    dm_player_key = data.get("dm_player_key")
    if dm_player_key:
        dm.player_key = dm_player_key
    session.users[dm_id] = dm
    session.dm_id = dm_id

    # Restore saved players/viewers with their original IDs
    for p in data.get("players", []):
        u = User(id=p["id"], name=p["name"], role=p["role"], connected=False)
        if p.get("player_key"):
            u.player_key = p["player_key"]
        session.users[p["id"]] = u

    # Restore POIs
    for p in data.get("pois", []):
        poi = POI(
            id=p["id"],
            x=p["x"],
            y=p["y"],
            name=p["name"],
            description=p.get("description", ""),
            dm_notes=p.get("dm_notes", ""),
            poi_type=p.get("poi_type", "city"),
            local_map_url=p.get("local_map_url"),
            map_context=p.get("map_context", "world"),
            revealed_to_players=bool(p.get("revealed_to_players", True)),
            interactable=normalize_interactable(p.get("interactable")),
        )
        session.pois[poi.id] = poi

    _normalize_restored_dm_map_state(session, data)

    # Restore tokens (owner_id links back to restored player IDs)
    for t in data["tokens"]:
        raw_cond = t.get("conditions") or []
        if not isinstance(raw_cond, list):
            raw_cond = []
        safe_cond = [str(c)[:50] for c in raw_cond if isinstance(c, str)][:20]
        tok = Token(
            id=t["id"],
            name=t["name"],
            x=t["x"],
            y=t["y"],
            width=t["width"],
            height=t["height"],
            color=t["color"],
            shape=t["shape"],
            owner_id=t.get("owner_id"),
            hp=t.get("hp"),
            max_hp=t.get("max_hp") or t.get("maxHp"),
            temp_hp=int(t.get("temp_hp", t.get("tempHp", 0)) or 0),
            hidden_hp=bool(t.get("hidden_hp", 0)),
            hidden=bool(t.get("hidden", 0)),
            initiative_mod=int(t.get("initiative_mod", t.get("initiativeMod", 0)) or 0),
            ac=(int(t.get("ac")) if t.get("ac") is not None else None),
            ac_from_equipment=bool(t.get("ac_from_equipment", False)),
            speed=(int(t.get("speed")) if t.get("speed") is not None else None),
            token_type=t.get("token_type", t.get("tokenType", "player")),
            notes=str(t.get("notes", "") or ""),
            conditions=safe_cond,
            condition_timers=(
                t.get("condition_timers")
                if isinstance(t.get("condition_timers"), dict)
                else {}
            ),
            level=(int(t.get("level")) if t.get("level") is not None else None),
            faction=str(t.get("faction", "") or ""),
            passive_perception=(
                int(t.get("passive_perception", t.get("passivePerception")))
                if t.get("passive_perception", t.get("passivePerception")) is not None
                else None
            ),
            save_bonuses=(
                t.get("save_bonuses")
                if isinstance(t.get("save_bonuses"), dict)
                else (
                    t.get("saveBonuses")
                    if isinstance(t.get("saveBonuses"), dict)
                    else {}
                )
            ),
            map_context=_normalize_token_map_context(session, t.get("map_context", "world")),
            staged=bool(t.get("staged", False)),
            image_url=(str(t.get("image_url") or t.get("imageUrl") or "")[:300] or None),
            creature_id=str(t.get("creature_id") or "")[:120],
            creature_type=str(t.get("creature_type") or "")[:40],
            monster_type=str(t.get("monster_type") or "")[:60],
            cr=str(t.get("cr") or "")[:16],
            profile_id=str(t.get("profile_id") or t.get("profileId") or "")[:120],
            library_id=str(t.get("library_id") or t.get("libraryId") or "")[:120],
            character_id=str(t.get("character_id") or t.get("characterId") or "")[:120],
        )
        session.tokens[tok.id] = tok

    # Restore logs
    session.log = data.get("logs", [])
    session.enforce_single_active_player_token_rule()
    reconcile_session_active_summons(session)

    _sessions[session.id] = session
    return session, dm_id
