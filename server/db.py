"""
server/db.py — SQLite persistence
"""
import logging
import sqlite3
import json
import time
import asyncio
from typing import Optional, Any
from concurrent.futures import ThreadPoolExecutor

from server.paths import DB_PATH, ensure_data_dirs
from server.persistence_schema import extract_persistable_campaign_state, normalize_persisted_campaign_data
from server.item_schema import normalize_shop_item_row

ensure_data_dirs()
_db_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
logger = logging.getLogger(__name__)

_LARGE_FIELD_WARN_THRESHOLD = 512_000
_large_field_warned = set()

_CAMPAIGN_JSON_FIELD_DEFAULTS = {
    "fog_maps": {},
    "combat": {},
    "chat_participants": {},
    "journal_entries": [],
    "codex_entries": [],
    "codex_links": [],
    "library_entries": [],
    "item_library_entries": [],
    "char_profiles": {},
    "active_char_profiles": {},
    "player_inventories": {},
    "player_gold": {},
    "party_loot_log": [],
    "editor_layers": {},
    "editor_walls": {},
    "editor_props": {},
    "map_settings": {},
    "editor_paths": {},
    "editor_labels": {},
    "editor_markers": {},
    "editor_lights": {},
    "map_documents": {},
    "viewer_profiles": {},
    "viewer_pending_actions": {},
    "viewer_power_catalog": {},
    "hazard_zones": {},
    "corpse_states": {},
    "corpse_dm_config": {},
    "handouts": [],
    "discovery_cards": [],
    "private_story_hooks": [],
    "encounter_templates": [],
    "quest_templates": [],
    "session_quests": [],
    "quest_board_bindings": [],
    "sound_state": {},
    "weather_state": {},
    "world_state": {},
    "active_poll": {},
    "party_memory_log": [],
}

def _json_dumps_compact(value) -> str:
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False)


def _log_campaign_field_issue(action: str, campaign_id: str, field: str, reason: str, detail: Any = None) -> None:
    detail_text = f" detail={detail}" if detail not in (None, "") else ""
    msg = f"[DB] {action} campaign={campaign_id} field={field} reason={reason}{detail_text}"
    logger.warning(msg)
    # Print to stdout so structured field-level warnings are visible in
    # container log aggregators and capturable by tests via redirect_stdout.
    print(msg)


def _char_profiles_size_breakdown(value: str, limit: int = 8) -> str:
    """Return a compact "owner/profile=bytes" breakdown of the largest entries in
    a serialized char_profiles field, so an oversized warning names exactly what
    is huge instead of only reporting a total byte count."""
    try:
        data = json.loads(value)
    except Exception:
        return ""
    sizes: list[tuple[str, int]] = []
    if isinstance(data, dict):
        for owner_key, profiles in data.items():
            entries = profiles if isinstance(profiles, list) else [profiles]
            for profile in entries:
                if not isinstance(profile, dict):
                    continue
                label = f"{owner_key}/{profile.get('id') or profile.get('name') or '?'}"
                for key, sub in profile.items():
                    try:
                        sizes.append((f"{label}:{key}", len(_json_dumps_compact(sub))))
                    except Exception:
                        continue
    sizes.sort(key=lambda kv: kv[1], reverse=True)
    return " ".join(f"{name}={size}" for name, size in sizes[:limit])


def _warn_large_persisted_field(campaign_id: str, field: str, value: str) -> None:
    if len(value) <= _LARGE_FIELD_WARN_THRESHOLD:
        return
    warn_key = f"{campaign_id}:{field}"
    if warn_key in _large_field_warned:
        return
    detail = len(value)
    if field == "char_profiles":
        breakdown = _char_profiles_size_breakdown(value)
        if breakdown:
            detail = f"{len(value)} top_keys[{breakdown}]"
    _log_campaign_field_issue("warning", campaign_id, field, "large_field", detail)
    _large_field_warned.add(warn_key)


def _deserialize_campaign_field(campaign_id: str, field: str, raw: Any) -> Any:
    fallback = _CAMPAIGN_JSON_FIELD_DEFAULTS[field]
    if raw in (None, ""):
        return json.loads(_json_dumps_compact(fallback))
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        _log_campaign_field_issue("load", campaign_id, field, "invalid_json", exc)
        return json.loads(_json_dumps_compact(fallback))
    if not isinstance(value, type(fallback)):
        actual_type = type(value).__name__
        expected_type = type(fallback).__name__
        _log_campaign_field_issue("load", campaign_id, field, "invalid_shape", f"expected={expected_type} got={actual_type}")
        return json.loads(_json_dumps_compact(fallback))
    if field == "char_profiles":
        # Stored rows may be dedup storage-shaped; runtime code only ever
        # sees fully hydrated profiles.
        from server.character.profile_dedup import hydrate_char_profiles_from_storage
        value = hydrate_char_profiles_from_storage(value)
    return value


def _serialize_campaign_field(campaign_id: str, field: str, value: Any) -> str:
    try:
        if field == "fog_maps":
            serialized = _safe_fog_json(value)
        elif field == "char_profiles":
            # Persist with shared subtrees stored once (storage layout only —
            # the live session object is not touched; loads re-hydrate).
            from server.character.profile_dedup import dedupe_char_profiles_for_storage
            serialized = _json_dumps_compact(dedupe_char_profiles_for_storage(value))
        else:
            serialized = _json_dumps_compact(value)
    except Exception as exc:
        _log_campaign_field_issue("save", campaign_id, field, "serialize_failed", exc)
        raise
    _warn_large_persisted_field(campaign_id, field, serialized)
    return serialized



def _sanitize_token_row(row: dict) -> dict:
    """Clean up a token row from DB — ensure token fields have safe defaults."""
    try:
        cond = json.loads(row.get("conditions") or "[]")
        if not isinstance(cond, list):
            cond = []
        row["conditions"] = [str(c)[:50] for c in cond if isinstance(c, str)][:20]
    except Exception:
        row["conditions"] = []
    row["temp_hp"] = int(row.get("temp_hp") or 0)
    try:
        raw_timers = json.loads(row.get("condition_timers") or "{}")
        if not isinstance(raw_timers, dict):
            raw_timers = {}
        now = time.time()
        safe_timers = {}
        for k, v in raw_timers.items():
            key = str(k)[:50].strip().lower()
            if not key:
                continue
            try:
                expiry = float(v or 0)
            except Exception:
                continue
            if expiry > now:
                safe_timers[key] = expiry
        row["condition_timers"] = safe_timers
    except Exception:
        row["condition_timers"] = {}
    row["token_type"] = str(row.get("token_type") or "player")
    row["notes"] = str(row.get("notes") or "")[:2000]
    row["faction"] = str(row.get("faction") or "")[:100]
    try:
        raw_save_bonuses = json.loads(row.get("save_bonuses") or "{}")
        row["save_bonuses"] = raw_save_bonuses if isinstance(raw_save_bonuses, dict) else {}
    except Exception:
        row["save_bonuses"] = {}
    row["vision_enabled"] = int(row.get("vision_enabled") or 0)
    row["vision_radius"] = int(row.get("vision_radius") or 0)
    row["bright_radius"] = int(row.get("bright_radius") or 0)
    row["dim_radius"] = int(row.get("dim_radius") or 0)
    row["has_darkvision"] = int(row.get("has_darkvision") or 0)
    row["darkvision_radius"] = int(row.get("darkvision_radius") or 0)
    raw_image_url = row.get("image_url") or ""
    row["image_url"] = str(raw_image_url)[:300] if raw_image_url else None
    row["creature_id"] = str(row.get("creature_id") or "")[:120]
    row["creature_type"] = str(row.get("creature_type") or "")[:40]
    row["monster_type"] = str(row.get("monster_type") or "")[:60]
    row["cr"] = str(row.get("cr") or "")[:16]
    row["profile_id"] = str(row.get("profile_id") or row.get("profileId") or "")[:120]
    row["library_id"] = str(row.get("library_id") or row.get("libraryId") or "")[:120]
    row["character_id"] = str(row.get("character_id") or row.get("characterId") or "")[:120]
    return row


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Ensure the string/blob limit is 1GB (some SQLite builds default lower)
    try:
        conn.setlimit(0, 1_000_000_000)  # SQLITE_LIMIT_LENGTH = 0
    except AttributeError:
        pass  # setlimit added in Python 3.11, older builds don't have it
    return conn


async def save_campaign_async(session) -> bool:
    """Non-blocking save — runs save_campaign in a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_db_executor, save_campaign, session)


def migrate_char_profiles_dedup() -> dict:
    """One-time (idempotent) rewrite of persisted char_profiles with shared
    subtrees stored once.

    Runs at startup AFTER create_startup_backup(), so the pre-migration state
    is always recoverable from DATA_DIR/backups. Each campaign row is only
    rewritten when the dedup round-trip is verified content-identical AND the
    serialized field actually shrinks; otherwise the row is left untouched and
    the reason is logged. Before/after byte sizes are logged per campaign.
    """
    from server.character.profile_dedup import (
        char_profiles_roundtrip_ok,
        dedupe_char_profiles_for_storage,
        hydrate_char_profiles_from_storage,
    )

    stats = {"campaigns": 0, "rewritten": 0, "skipped": 0, "bytes_before": 0, "bytes_after": 0}
    try:
        with get_conn() as conn:
            rows = conn.execute("SELECT id, char_profiles FROM campaigns").fetchall()
    except Exception as exc:
        logger.error("[DB] char_profiles dedup migration scan failed: %s", exc)
        return stats

    for row in rows:
        campaign_id = row["id"]
        raw = row["char_profiles"]
        if not raw or raw in ("{}", "null"):
            continue
        stats["campaigns"] += 1
        try:
            stored = json.loads(raw)
            if not isinstance(stored, dict):
                continue
            hydrated = hydrate_char_profiles_from_storage(stored)
            if not char_profiles_roundtrip_ok(hydrated):
                logger.error(
                    "[DB] char_profiles dedup migration skipped campaign=%s reason=roundtrip_mismatch",
                    campaign_id,
                )
                stats["skipped"] += 1
                continue
            deduped = dedupe_char_profiles_for_storage(hydrated)
            new_raw = _json_dumps_compact(deduped)
            before, after = len(raw), len(new_raw)
            if after >= before:
                stats["skipped"] += 1
                continue
            with get_conn() as conn:
                conn.execute(
                    "UPDATE campaigns SET char_profiles=? WHERE id=?",
                    (new_raw, campaign_id),
                )
                conn.commit()
            stats["rewritten"] += 1
            stats["bytes_before"] += before
            stats["bytes_after"] += after
            logger.info(
                "[DB] char_profiles dedup migration campaign=%s bytes_before=%s bytes_after=%s saved=%s (%.1f%%)",
                campaign_id, before, after, before - after, (before - after) * 100.0 / before,
            )
        except Exception as exc:
            stats["skipped"] += 1
            logger.error("[DB] char_profiles dedup migration failed campaign=%s error=%s", campaign_id, exc)
    if stats["rewritten"]:
        logger.info(
            "[DB] char_profiles dedup migration complete campaigns=%s rewritten=%s skipped=%s total_bytes %s -> %s",
            stats["campaigns"], stats["rewritten"], stats["skipped"], stats["bytes_before"], stats["bytes_after"],
        )
    return stats


def init_db():
    with get_conn() as conn:
        for migration in [
            "ALTER TABLE campaigns ADD COLUMN map_image_url TEXT",
            "ALTER TABLE pois ADD COLUMN local_map_url TEXT",
            "ALTER TABLE pois ADD COLUMN map_context TEXT NOT NULL DEFAULT 'world'",
            "ALTER TABLE pois ADD COLUMN interactable TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE pois ADD COLUMN revealed_to_players INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE tokens ADD COLUMN hp INTEGER",
            "ALTER TABLE tokens ADD COLUMN max_hp INTEGER",
            "ALTER TABLE tokens ADD COLUMN hidden_hp INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN map_context TEXT NOT NULL DEFAULT 'world'",
            "ALTER TABLE tokens ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN initiative_mod INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN conditions TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE tokens ADD COLUMN condition_timers TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE tokens ADD COLUMN staged INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN temp_hp INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN ac INTEGER",
            "ALTER TABLE tokens ADD COLUMN speed INTEGER",
            "ALTER TABLE tokens ADD COLUMN token_type TEXT NOT NULL DEFAULT 'player'",
            "ALTER TABLE tokens ADD COLUMN notes TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tokens ADD COLUMN level INTEGER",
            "ALTER TABLE tokens ADD COLUMN faction TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tokens ADD COLUMN passive_perception INTEGER",
            "ALTER TABLE tokens ADD COLUMN save_bonuses TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE tokens ADD COLUMN vision_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN vision_radius INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN bright_radius INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN dim_radius INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN has_darkvision INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN darkvision_radius INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tokens ADD COLUMN image_url TEXT",
            "ALTER TABLE tokens ADD COLUMN creature_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tokens ADD COLUMN creature_type TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tokens ADD COLUMN monster_type TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tokens ADD COLUMN cr TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tokens ADD COLUMN profile_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tokens ADD COLUMN library_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tokens ADD COLUMN character_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN dm_map_context TEXT NOT NULL DEFAULT 'world'",
            "ALTER TABLE campaigns ADD COLUMN dm_current_map_url TEXT",
            "ALTER TABLE campaigns ADD COLUMN fog_maps TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN combat TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN journal_entries TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN library_entries TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN item_library_entries TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN char_profiles TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN active_char_profiles TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN player_inventories TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN player_gold TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN party_loot_log TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN editor_layers TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN editor_walls TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN editor_props TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN map_settings TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN editor_paths TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN editor_labels TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN editor_markers TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN editor_lights TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN map_documents TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN viewer_profiles TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN viewer_pending_actions TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN viewer_power_catalog TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN hazard_zones TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN corpse_states TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN corpse_dm_config TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN handouts TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN discovery_cards TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN private_story_hooks TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN encounter_templates TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN quest_templates TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN session_quests TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN quest_board_bindings TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN sound_state TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN weather_state TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN world_state TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN active_poll TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN show_viewer_presence INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE user_creature_library ADD COLUMN token_size INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE user_creature_library ADD COLUMN owner_user_id TEXT",
            "ALTER TABLE user_creature_library ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'monster'",
            "ALTER TABLE user_creature_library ADD COLUMN slug TEXT",
            "ALTER TABLE user_creature_library ADD COLUMN source_type TEXT NOT NULL DEFAULT 'custom'",
            "ALTER TABLE user_creature_library ADD COLUMN source_label TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE user_creature_library ADD COLUMN subtype TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE user_creature_library ADD COLUMN size TEXT NOT NULL DEFAULT 'Medium'",
            "ALTER TABLE user_creature_library ADD COLUMN alignment TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE user_creature_library ADD COLUMN xp INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE user_creature_library ADD COLUMN proficiency_bonus INTEGER NOT NULL DEFAULT 2",
            "ALTER TABLE user_creature_library ADD COLUMN hit_points INTEGER",
            "ALTER TABLE user_creature_library ADD COLUMN hit_dice TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE user_creature_library ADD COLUMN speed_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE user_creature_library ADD COLUMN ability_scores_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE user_creature_library ADD COLUMN saving_throws_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE user_creature_library ADD COLUMN skills_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE user_creature_library ADD COLUMN senses_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE user_creature_library ADD COLUMN languages_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN damage_resistances_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN damage_immunities_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN condition_immunities_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN vulnerabilities_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN traits_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN actions_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN bonus_actions_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN reactions_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN legendary_actions_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN spellcasting_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE user_creature_library ADD COLUMN equipment_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN environment_tags_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN role_tags_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_creature_library ADD COLUMN token_url TEXT",
            "ALTER TABLE user_creature_library ADD COLUMN asset_path TEXT",
            "ALTER TABLE user_creature_library ADD COLUMN personality TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE user_creature_library ADD COLUMN notes TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE user_creature_library ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE user_creature_library ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE user_creature_library ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE user_creature_library ADD COLUMN last_used_at REAL",
            "ALTER TABLE user_creature_library ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE user_creature_library ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE user_creature_library ADD COLUMN seed_key TEXT",
            "ALTER TABLE campaigns ADD COLUMN dm_id TEXT",
            "ALTER TABLE campaigns ADD COLUMN dm_player_key TEXT",
            "ALTER TABLE shops ADD COLUMN taught_professions_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE shops ADD COLUMN crafting_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE shops ADD COLUMN selling_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE shops ADD COLUMN shop_sales_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE shops ADD COLUMN player_sell_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE shops ADD COLUMN buy_categories_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE shops ADD COLUMN vendor_cash_units INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE shops ADD COLUMN buy_rate_pct INTEGER NOT NULL DEFAULT 50",
            "ALTER TABLE craft_jobs ADD COLUMN inputs_locked_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE craft_jobs ADD COLUMN logs_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE shops ADD COLUMN buy_rate_pct INTEGER NOT NULL DEFAULT 50",
            "ALTER TABLE shops ADD COLUMN vendor_cash_units INTEGER",
            "ALTER TABLE shops ADD COLUMN accepted_item_types_json TEXT NOT NULL DEFAULT '[\"weapon\",\"armour\",\"consumable\",\"tool\",\"material\",\"trinket\",\"magic\",\"misc\"]'",
            "ALTER TABLE shops ADD COLUMN selling_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE shops ADD COLUMN shop_sales_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE shops ADD COLUMN player_sell_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE shops ADD COLUMN buyback_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE shops ADD COLUMN personality TEXT NOT NULL DEFAULT 'friendly'",
            "ALTER TABLE shops ADD COLUMN dialogue_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE shops ADD COLUMN voice TEXT NOT NULL DEFAULT 'grand_narrator'",
            "ALTER TABLE shops ADD COLUMN tts_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE shops ADD COLUMN greeting_override TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE shop_transactions ADD COLUMN direction TEXT NOT NULL DEFAULT 'buy'",
            "ALTER TABLE campaigns ADD COLUMN codex_entries TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN codex_links TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN chat_participants TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE campaigns ADD COLUMN twitch_channel TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN twitch_channel_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN twitch_access_token_enc TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN twitch_refresh_token_enc TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN twitch_token_expires_at REAL NOT NULL DEFAULT 0",
            "ALTER TABLE campaigns ADD COLUMN twitch_chat_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE campaigns ADD COLUMN overlay_game_token TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN overlay_arena_token TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                conn.execute(migration)
                conn.commit()
            except Exception:
                pass
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            dm_name     TEXT NOT NULL,
            player_invite TEXT NOT NULL,
            viewer_invite TEXT NOT NULL,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL,
            map_image_url TEXT,
            dm_map_context TEXT NOT NULL DEFAULT 'world',
            dm_current_map_url TEXT,
            fog_maps TEXT NOT NULL DEFAULT '{}',
            combat TEXT NOT NULL DEFAULT '{}',
            journal_entries TEXT NOT NULL DEFAULT '[]',
            codex_entries TEXT NOT NULL DEFAULT '[]',
            codex_links TEXT NOT NULL DEFAULT '[]',
            library_entries TEXT NOT NULL DEFAULT '[]',
            item_library_entries TEXT NOT NULL DEFAULT '[]',
            char_profiles TEXT NOT NULL DEFAULT '{}',
            active_char_profiles TEXT NOT NULL DEFAULT '{}',
            player_inventories TEXT NOT NULL DEFAULT '{}',
            player_gold TEXT NOT NULL DEFAULT '{}',
            party_loot_log TEXT NOT NULL DEFAULT '[]',
            editor_layers TEXT NOT NULL DEFAULT '{}',
            editor_walls TEXT NOT NULL DEFAULT '{}',
            editor_props TEXT NOT NULL DEFAULT '{}',
            map_settings TEXT NOT NULL DEFAULT '{}',
            editor_paths TEXT NOT NULL DEFAULT '{}',
            editor_labels TEXT NOT NULL DEFAULT '{}',
            editor_markers TEXT NOT NULL DEFAULT '{}',
            editor_lights TEXT NOT NULL DEFAULT '{}',
            map_documents TEXT NOT NULL DEFAULT '{}',
            viewer_profiles TEXT NOT NULL DEFAULT '{}',
            viewer_pending_actions TEXT NOT NULL DEFAULT '{}',
            viewer_power_catalog TEXT NOT NULL DEFAULT '{}',
            hazard_zones TEXT NOT NULL DEFAULT '{}',
            corpse_states TEXT NOT NULL DEFAULT '{}',
            corpse_dm_config TEXT NOT NULL DEFAULT '{}',
            handouts TEXT NOT NULL DEFAULT '[]',
            discovery_cards TEXT NOT NULL DEFAULT '[]',
            private_story_hooks TEXT NOT NULL DEFAULT '[]',
            encounter_templates TEXT NOT NULL DEFAULT '[]',
            quest_templates TEXT NOT NULL DEFAULT '[]',
            session_quests TEXT NOT NULL DEFAULT '[]',
            quest_board_bindings TEXT NOT NULL DEFAULT '[]',
            sound_state TEXT NOT NULL DEFAULT '{}',
            weather_state TEXT NOT NULL DEFAULT '{}',
            world_state TEXT NOT NULL DEFAULT '{}',
            active_poll TEXT NOT NULL DEFAULT '{}',
            show_viewer_presence INTEGER NOT NULL DEFAULT 0,
            chat_participants TEXT NOT NULL DEFAULT '{}',
            twitch_channel TEXT NOT NULL DEFAULT '',
            twitch_channel_id TEXT NOT NULL DEFAULT '',
            twitch_access_token_enc TEXT NOT NULL DEFAULT '',
            twitch_refresh_token_enc TEXT NOT NULL DEFAULT '',
            twitch_token_expires_at REAL NOT NULL DEFAULT 0,
            twitch_chat_enabled INTEGER NOT NULL DEFAULT 1,
            overlay_game_token TEXT NOT NULL DEFAULT '',
            overlay_arena_token TEXT NOT NULL DEFAULT '',
            dm_id TEXT,
            dm_player_key TEXT
        );

        CREATE TABLE IF NOT EXISTS tokens (
            id          TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            name        TEXT NOT NULL,
            x           REAL NOT NULL,
            y           REAL NOT NULL,
            width       REAL NOT NULL,
            height      REAL NOT NULL,
            color       TEXT NOT NULL,
            shape       TEXT NOT NULL,
            owner_id    TEXT,
            hp          INTEGER,
            max_hp      INTEGER,
            temp_hp     INTEGER NOT NULL DEFAULT 0,
            hidden_hp   INTEGER NOT NULL DEFAULT 0,
            hidden      INTEGER NOT NULL DEFAULT 0,
            initiative_mod INTEGER NOT NULL DEFAULT 0,
            ac          INTEGER,
            speed       INTEGER,
            token_type  TEXT NOT NULL DEFAULT 'player',
            notes       TEXT NOT NULL DEFAULT '',
            conditions  TEXT NOT NULL DEFAULT '[]',
            condition_timers TEXT NOT NULL DEFAULT '{}',
            level       INTEGER,
            faction     TEXT NOT NULL DEFAULT '',
            passive_perception INTEGER,
            save_bonuses TEXT NOT NULL DEFAULT '{}',
            vision_enabled INTEGER NOT NULL DEFAULT 0,
            vision_radius INTEGER NOT NULL DEFAULT 0,
            bright_radius INTEGER NOT NULL DEFAULT 0,
            dim_radius INTEGER NOT NULL DEFAULT 0,
            has_darkvision INTEGER NOT NULL DEFAULT 0,
            darkvision_radius INTEGER NOT NULL DEFAULT 0,
            map_context TEXT NOT NULL DEFAULT 'world',
            staged      INTEGER NOT NULL DEFAULT 0,
            image_url   TEXT,
            creature_id TEXT NOT NULL DEFAULT '',
            creature_type TEXT NOT NULL DEFAULT '',
            monster_type TEXT NOT NULL DEFAULT '',
            cr TEXT NOT NULL DEFAULT '',
            profile_id TEXT NOT NULL DEFAULT '',
            library_id TEXT NOT NULL DEFAULT '',
            character_id TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS pois (
            id            TEXT PRIMARY KEY,
            campaign_id   TEXT NOT NULL,
            x             REAL NOT NULL,
            y             REAL NOT NULL,
            name          TEXT NOT NULL,
            description   TEXT,
            dm_notes      TEXT,
            poi_type      TEXT NOT NULL DEFAULT 'city',
            local_map_url TEXT,
            map_context   TEXT NOT NULL DEFAULT 'world',
            interactable  TEXT NOT NULL DEFAULT '{}',
            revealed_to_players INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS players (
            id          TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            name        TEXT NOT NULL,
            role        TEXT NOT NULL,
            player_key  TEXT,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS logs (
            id          TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            timestamp   REAL NOT NULL,
            type        TEXT NOT NULL,
            user        TEXT NOT NULL,
            message     TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );
        """)

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS shops (
            id              TEXT PRIMARY KEY,
            campaign_id     TEXT NOT NULL,
            prop_id         TEXT,
            poi_id          TEXT,
            name            TEXT NOT NULL DEFAULT 'Shop',
            shopkeeper_name TEXT NOT NULL DEFAULT 'Shopkeeper',
            shop_type       TEXT NOT NULL DEFAULT 'general',
            taught_professions_json TEXT NOT NULL DEFAULT '[]',
            crafting_enabled INTEGER NOT NULL DEFAULT 1,
            selling_enabled INTEGER NOT NULL DEFAULT 1,
            shop_sales_enabled INTEGER NOT NULL DEFAULT 1,
            player_sell_enabled INTEGER NOT NULL DEFAULT 1,
            buy_categories_json TEXT NOT NULL DEFAULT '[]',
            vendor_cash_units INTEGER NOT NULL DEFAULT 0,
            buy_rate_pct INTEGER NOT NULL DEFAULT 50,
            accepted_item_types_json TEXT NOT NULL DEFAULT '["weapon","armour","consumable","tool","material","trinket","magic","misc"]',
            buyback_enabled INTEGER NOT NULL DEFAULT 0,
            personality     TEXT NOT NULL DEFAULT 'friendly',
            dialogue_enabled INTEGER NOT NULL DEFAULT 1,
            voice           TEXT NOT NULL DEFAULT 'grand_narrator',
            tts_enabled     INTEGER NOT NULL DEFAULT 0,
            greeting_override TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            is_open         INTEGER NOT NULL DEFAULT 1,
            created_at      REAL NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );
        CREATE TABLE IF NOT EXISTS shop_inventory (
            id          TEXT PRIMARY KEY,
            shop_id     TEXT NOT NULL,
            item_name   TEXT NOT NULL,
            item_type   TEXT NOT NULL DEFAULT 'misc',
            description TEXT NOT NULL DEFAULT '',
            price_gp    INTEGER NOT NULL DEFAULT 0,
            price_sp    INTEGER NOT NULL DEFAULT 0,
            price_cp    INTEGER NOT NULL DEFAULT 0,
            quantity    INTEGER,
            item_data   TEXT NOT NULL DEFAULT '{}',
            created_at  REAL NOT NULL,
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        );
        CREATE TABLE IF NOT EXISTS shop_transactions (
            id              TEXT PRIMARY KEY,
            shop_id         TEXT NOT NULL,
            buyer_user_id   TEXT NOT NULL,
            item_id         TEXT NOT NULL,
            quantity        INTEGER NOT NULL DEFAULT 1,
            price_paid_gp   INTEGER NOT NULL DEFAULT 0,
            sold_at         REAL NOT NULL,
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        );
        CREATE TABLE IF NOT EXISTS professions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            taught_by_shop_types_json TEXT NOT NULL DEFAULT '[]',
            tool_hints_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS player_professions (
            campaign_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            profession_ids_json TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL,
            PRIMARY KEY (campaign_id, user_id),
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );
        CREATE TABLE IF NOT EXISTS crafting_recipes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            result_item_json TEXT NOT NULL DEFAULT '{}',
            requires_professions_json TEXT NOT NULL DEFAULT '[]',
            requires_materials_json TEXT NOT NULL DEFAULT '[]',
            fee_units INTEGER NOT NULL DEFAULT 0,
            duration_seconds INTEGER NOT NULL DEFAULT 0,
            station_shop_types_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            rarity TEXT NOT NULL DEFAULT 'common',
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS craft_jobs (
            job_id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            recipe_id TEXT NOT NULL,
            shop_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            ready_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'crafting',
            inputs_locked_json TEXT NOT NULL DEFAULT '[]',
            result_json TEXT NOT NULL DEFAULT '{}',
            logs_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
            FOREIGN KEY (recipe_id) REFERENCES crafting_recipes(id),
            FOREIGN KEY (shop_id) REFERENCES shops(id)
        );
        """)
        _seed_professions(conn)
        _seed_crafting_recipes(conn)

        # Creature library table (global, cross-campaign)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_creature_library (
            id              TEXT PRIMARY KEY,
            owner_user_id   TEXT,
            entry_type      TEXT NOT NULL DEFAULT 'monster',
            name            TEXT NOT NULL,
            slug            TEXT,
            creature_type   TEXT NOT NULL DEFAULT 'monster',
            subtype         TEXT NOT NULL DEFAULT '',
            size            TEXT NOT NULL DEFAULT 'Medium',
            alignment       TEXT NOT NULL DEFAULT '',
            cr              TEXT NOT NULL DEFAULT '0',
            xp              INTEGER NOT NULL DEFAULT 0,
            proficiency_bonus INTEGER NOT NULL DEFAULT 2,
            hp              INTEGER NOT NULL DEFAULT 1,
            hit_points      INTEGER,
            hit_dice        TEXT NOT NULL DEFAULT '',
            ac              INTEGER NOT NULL DEFAULT 10,
            speed           TEXT NOT NULL DEFAULT '30 ft.',
            speed_json      TEXT NOT NULL DEFAULT '{}',
            str_score       INTEGER NOT NULL DEFAULT 10,
            dex_score       INTEGER NOT NULL DEFAULT 10,
            con_score       INTEGER NOT NULL DEFAULT 10,
            int_score       INTEGER NOT NULL DEFAULT 10,
            wis_score       INTEGER NOT NULL DEFAULT 10,
            cha_score       INTEGER NOT NULL DEFAULT 10,
            ability_scores_json TEXT NOT NULL DEFAULT '{}',
            saving_throws_json TEXT NOT NULL DEFAULT '{}',
            skills_json     TEXT NOT NULL DEFAULT '{}',
            senses_json     TEXT NOT NULL DEFAULT '{}',
            languages_json  TEXT NOT NULL DEFAULT '[]',
            damage_resistances_json TEXT NOT NULL DEFAULT '[]',
            damage_immunities_json TEXT NOT NULL DEFAULT '[]',
            condition_immunities_json TEXT NOT NULL DEFAULT '[]',
            vulnerabilities_json TEXT NOT NULL DEFAULT '[]',
            attacks         TEXT NOT NULL DEFAULT '[]',
            abilities       TEXT NOT NULL DEFAULT '[]',
            traits_json     TEXT NOT NULL DEFAULT '[]',
            actions_json    TEXT NOT NULL DEFAULT '[]',
            bonus_actions_json TEXT NOT NULL DEFAULT '[]',
            reactions_json  TEXT NOT NULL DEFAULT '[]',
            legendary_actions_json TEXT NOT NULL DEFAULT '[]',
            spellcasting_json TEXT NOT NULL DEFAULT '{}',
            equipment_json  TEXT NOT NULL DEFAULT '[]',
            portrait_url    TEXT,
            token_url       TEXT,
            asset_path      TEXT,
            backstory       TEXT NOT NULL DEFAULT '',
            personality     TEXT NOT NULL DEFAULT '',
            voice_style     TEXT NOT NULL DEFAULT '',
            notes           TEXT NOT NULL DEFAULT '',
            tags            TEXT NOT NULL DEFAULT '[]',
            tags_json       TEXT NOT NULL DEFAULT '[]',
            environment_tags_json TEXT NOT NULL DEFAULT '[]',
            role_tags_json  TEXT NOT NULL DEFAULT '[]',
            source          TEXT NOT NULL DEFAULT 'custom',
            source_type     TEXT NOT NULL DEFAULT 'custom',
            source_label    TEXT NOT NULL DEFAULT '',
            srd_id          TEXT,
            monster_type    TEXT NOT NULL DEFAULT '',
            token_size      INTEGER NOT NULL DEFAULT 1,
            is_favorite     INTEGER NOT NULL DEFAULT 0,
            is_pinned       INTEGER NOT NULL DEFAULT 0,
            is_public       INTEGER NOT NULL DEFAULT 0,
            last_used_at    REAL,
            use_count       INTEGER NOT NULL DEFAULT 0,
            archived        INTEGER NOT NULL DEFAULT 0,
            deleted         INTEGER NOT NULL DEFAULT 0,
            seed_key        TEXT,
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_creature_seed ON user_creature_library(owner_user_id, seed_key);
        """)

        # Rules content tables + seeds
        from server.rules_db import init_rules_db, init_loot_db
        init_rules_db()
        init_loot_db()


def _safe_fog_json(fog_maps) -> str:
    """Serialize fog_maps safely — ensure cells are compact strings not large lists."""
    if not fog_maps:
        return '{}'
    safe = {}
    for ctx, entry in fog_maps.items():
        cells = entry.get('cells', '')
        # If cells is a list/array (from client Uint8Array), convert to string
        if isinstance(cells, (list, bytes, bytearray)):
            cells = ''.join(str(int(bool(c))) for c in cells)
        elif not isinstance(cells, str):
            cells = ''
        cols = int(entry.get('cols', 64) or 64)
        rows = int(entry.get('rows', 64) or 64)
        # Persist the full grid so revealed/hidden state survives a restart even
        # on maps larger than 64×64 (4096 cells). Cap at a generous upper bound
        # relative to the grid so malformed input can't store unbounded data.
        max_cells = max(4096, cols * rows)
        try:
            revision = int(entry.get('revision', 0) or 0)
        except Exception:
            revision = 0
        try:
            updated_at = float(entry.get('updated_at', 0.0) or 0.0)
        except Exception:
            updated_at = 0.0
        ctx_key = str(ctx or 'world')[:80] or 'world'
        safe[ctx_key] = {
            'enabled': bool(entry.get('enabled', False)),
            'cols': cols,
            'rows': rows,
            'cells': cells[:max_cells],
            'revision': revision,
            'updated_at': updated_at,
            'map_context': ctx_key,
        }
    return json.dumps(safe)


def _save_campaign_row(conn, session, serialized_fields: dict, persisted_state: dict, now: float) -> None:
    """Upsert the main campaigns row."""
    try:
        conn.execute("""
            INSERT INTO campaigns (id, name, dm_name, player_invite, viewer_invite, created_at, updated_at, map_image_url, dm_map_context, dm_current_map_url, fog_maps, combat, chat_participants, journal_entries, codex_entries, codex_links, library_entries, item_library_entries, char_profiles, active_char_profiles, player_inventories, player_gold, party_loot_log, editor_layers, editor_walls, editor_props, map_settings, editor_paths, editor_labels, editor_markers, editor_lights, map_documents, viewer_profiles, viewer_pending_actions, viewer_power_catalog, hazard_zones, corpse_states, corpse_dm_config, handouts, discovery_cards, private_story_hooks, encounter_templates, quest_templates, session_quests, quest_board_bindings, sound_state, weather_state, world_state, active_poll, show_viewer_presence, dm_id, dm_player_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                updated_at=excluded.updated_at,
                map_image_url=excluded.map_image_url,
                dm_map_context=excluded.dm_map_context,
                dm_current_map_url=excluded.dm_current_map_url,
                fog_maps=excluded.fog_maps,
                combat=excluded.combat,
                chat_participants=excluded.chat_participants,
                journal_entries=excluded.journal_entries,
                codex_entries=excluded.codex_entries,
                codex_links=excluded.codex_links,
                library_entries=excluded.library_entries,
                item_library_entries=excluded.item_library_entries,
                char_profiles=excluded.char_profiles,
                active_char_profiles=excluded.active_char_profiles,
                player_inventories=excluded.player_inventories,
                player_gold=excluded.player_gold,
                party_loot_log=excluded.party_loot_log,
                editor_layers=excluded.editor_layers,
                editor_walls=excluded.editor_walls,
                editor_props=excluded.editor_props,
                map_settings=excluded.map_settings,
                editor_paths=excluded.editor_paths,
                editor_labels=excluded.editor_labels,
                editor_markers=excluded.editor_markers,
                editor_lights=excluded.editor_lights,
                viewer_profiles=excluded.viewer_profiles,
                viewer_pending_actions=excluded.viewer_pending_actions,
                viewer_power_catalog=excluded.viewer_power_catalog,
                map_documents=excluded.map_documents,
                hazard_zones=excluded.hazard_zones,
                corpse_states=excluded.corpse_states,
                corpse_dm_config=excluded.corpse_dm_config,
                handouts=excluded.handouts,
                discovery_cards=excluded.discovery_cards,
                private_story_hooks=excluded.private_story_hooks,
                encounter_templates=excluded.encounter_templates,
                quest_templates=excluded.quest_templates,
                session_quests=excluded.session_quests,
                quest_board_bindings=excluded.quest_board_bindings,
                sound_state=excluded.sound_state,
                weather_state=excluded.weather_state,
                world_state=excluded.world_state,
                active_poll=excluded.active_poll,
                show_viewer_presence=excluded.show_viewer_presence,
                dm_id=excluded.dm_id,
                dm_player_key=excluded.dm_player_key
        """, (
            session.id,
            getattr(session, 'name', 'My Campaign'),
            _get_dm_name(session),
            session.player_invite,
            session.viewer_invite,
            getattr(session, 'created_at', now),
            now,
            getattr(session, 'map_image_url', None),
            getattr(session, 'dm_map_context', 'world'),
            getattr(session, 'dm_current_map_url', None),
            serialized_fields["fog_maps"],
            serialized_fields["combat"],
            serialized_fields["chat_participants"],
            serialized_fields["journal_entries"],
            serialized_fields["codex_entries"],
            serialized_fields["codex_links"],
            serialized_fields["library_entries"],
            serialized_fields["item_library_entries"],
            serialized_fields["char_profiles"],
            serialized_fields["active_char_profiles"],
            serialized_fields["player_inventories"],
            serialized_fields["player_gold"],
            serialized_fields["party_loot_log"],
            serialized_fields["editor_layers"],
            serialized_fields["editor_walls"],
            serialized_fields["editor_props"],
            serialized_fields["map_settings"],
            serialized_fields["editor_paths"],
            serialized_fields["editor_labels"],
            serialized_fields["editor_markers"],
            serialized_fields["editor_lights"],
            serialized_fields["map_documents"],
            serialized_fields["viewer_profiles"],
            serialized_fields["viewer_pending_actions"],
            serialized_fields["viewer_power_catalog"],
            serialized_fields["hazard_zones"],
            serialized_fields["corpse_states"],
            serialized_fields["corpse_dm_config"],
            serialized_fields["handouts"],
            serialized_fields["discovery_cards"],
            serialized_fields["private_story_hooks"],
            serialized_fields["encounter_templates"],
            serialized_fields["quest_templates"],
            serialized_fields["session_quests"],
            serialized_fields["quest_board_bindings"],
            serialized_fields["sound_state"],
            serialized_fields["weather_state"],
            serialized_fields["world_state"],
            _json_dumps_compact(persisted_state["active_poll"] or {}),
            int(bool(persisted_state["show_viewer_presence"])),
            getattr(session, 'dm_id', None),
            _get_dm_player_key(session),
        ))
    except Exception as _ce:
        logger.error("[DB] campaigns INSERT failed: %s", _ce)
        raise


def _save_players(conn, session) -> None:
    """Replace all players for this campaign."""
    conn.execute("DELETE FROM players WHERE campaign_id=?", (session.id,))
    for u in session.users.values():
        if u.role in ("player", "viewer", "assistant_dm"):
            conn.execute(
                "INSERT OR REPLACE INTO players (id, campaign_id, name, role, player_key) VALUES (?, ?, ?, ?, ?)",
                (u.id, session.id, u.name, u.role, getattr(u, 'player_key', None))
            )


def _save_pois(conn, session) -> None:
    """Replace all POIs for this campaign."""
    conn.execute("DELETE FROM pois WHERE campaign_id=?", (session.id,))
    for p in session.pois.values():
        interactable = _json_dumps_compact(getattr(p, "interactable", None) or {})
        conn.execute(
            """
            INSERT OR REPLACE INTO pois (
                id, campaign_id, x, y, name, description, dm_notes, poi_type,
                local_map_url, map_context, interactable, revealed_to_players
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                p.id, session.id, p.x, p.y, p.name, p.description, p.dm_notes,
                p.poi_type, p.local_map_url, p.map_context, interactable,
                1 if getattr(p, "revealed_to_players", True) else 0,
            ),
        )


def _sanitize_token_for_save(t) -> tuple:
    """Return sanitized (name, conditions_json, timers_json, save_bonuses_json, image_url) for a token INSERT."""
    t_name = str(t.name or '')[:200]
    raw_cond = getattr(t, "conditions", [])
    if not isinstance(raw_cond, list):
        raw_cond = []
    safe_cond = [str(c)[:50] for c in raw_cond if isinstance(c, str)][:20]
    t_cond = json.dumps(safe_cond)
    raw_timers = getattr(t, 'condition_timers', None) or {}
    safe_timers = {}
    now = time.time()
    if isinstance(raw_timers, dict):
        for k, v in raw_timers.items():
            key = str(k)[:50].strip().lower()
            if not key or key not in safe_cond:
                continue
            try:
                expiry = float(v or 0)
            except Exception:
                continue
            if expiry > now:
                safe_timers[key] = expiry
    t_condition_timers = _json_dumps_compact(safe_timers)
    t_save_bonuses = _json_dumps_compact(getattr(t, 'save_bonuses', None) or {})
    t_image_url = str(getattr(t, 'image_url', '') or '')[:300] or None
    return t_name, t_cond, t_condition_timers, t_save_bonuses, t_image_url


def _save_tokens(conn, session) -> None:
    """Replace all tokens for this campaign."""
    conn.execute("DELETE FROM tokens WHERE campaign_id=?", (session.id,))
    for t in session.tokens.values():
        t_name, t_cond, t_condition_timers, t_save_bonuses, t_image_url = _sanitize_token_for_save(t)
        conn.execute("""
            INSERT OR REPLACE INTO tokens
            (id, campaign_id, name, x, y, width, height, color, shape, owner_id,
             hp, max_hp, temp_hp, hidden_hp, hidden, initiative_mod, ac, speed, token_type, notes,
             conditions, condition_timers, level, faction, passive_perception, save_bonuses,
             vision_enabled, vision_radius, bright_radius, dim_radius, has_darkvision, darkvision_radius, map_context, staged, image_url, creature_id, creature_type, monster_type, cr, profile_id, library_id, character_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (t.id, session.id, t_name, t.x, t.y, t.width, t.height, t.color, t.shape, t.owner_id,
              t.hp, t.max_hp, int(getattr(t, 'temp_hp', 0) or 0), int(t.hidden_hp), int(t.hidden), int(getattr(t, 'initiative_mod', 0) or 0),
              getattr(t, 'ac', None), getattr(t, 'speed', None), str(getattr(t, 'token_type', 'player') or 'player'),
              str(getattr(t, 'notes', '') or '')[:2000], t_cond, t_condition_timers, getattr(t, 'level', None), str(getattr(t, 'faction', '') or '')[:100],
              getattr(t, 'passive_perception', None), t_save_bonuses, int(bool(getattr(t, 'vision_enabled', False))), int(getattr(t, 'vision_radius', 0) or 0), int(getattr(t, 'bright_radius', 0) or 0), int(getattr(t, 'dim_radius', 0) or 0), int(bool(getattr(t, 'has_darkvision', False))), int(getattr(t, 'darkvision_radius', 0) or 0), t.map_context, int(getattr(t, 'staged', False)), t_image_url, str(getattr(t, 'creature_id', '') or '')[:120], str(getattr(t, 'creature_type', '') or '')[:40], str(getattr(t, 'monster_type', '') or '')[:60], str(getattr(t, 'cr', '') or '')[:16], str(getattr(t, 'profile_id', '') or '')[:120], str(getattr(t, 'library_id', '') or '')[:120], str(getattr(t, 'character_id', '') or '')[:120]))


def _save_logs(conn, session) -> None:
    """Replace the last 200 log entries for this campaign."""
    conn.execute("DELETE FROM logs WHERE campaign_id=?", (session.id,))
    for entry in session.log[-200:]:
        msg = entry['message']
        if len(str(msg)) > 50000:
            logger.warning("[DB] HUGE log message (%d bytes), truncating", len(str(msg)))
            msg = str(msg)[:1000]
        conn.execute("""
            INSERT OR REPLACE INTO logs (id, campaign_id, timestamp, type, user, message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (entry['id'], session.id, entry['timestamp'], entry['type'], entry['user'], msg))


def _diagnose_save_error(session, exc: Exception) -> None:
    """Log field-size diagnostics when a save fails."""
    try:
        fog_size = len(_safe_fog_json(session.fog_maps))
        combat_size = len(json.dumps(getattr(session, "combat", None) or {}))
        log_size = sum(len(str(entry)) for entry in session.log[-200:])
        poi_size = sum(len(str(p.description or '')) + len(str(p.dm_notes or '')) for p in session.pois.values())
        map_url_size = len(str(getattr(session, 'map_image_url', '') or ''))
        logger.error("[DB] sizes — fog:%d combat:%d logs:%d pois:%d map_url:%d", fog_size, combat_size, log_size, poi_size, map_url_size)
    except Exception as diag_e:
        logger.error("[DB] diag error: %s", diag_e)
    logger.error("[DB] save_campaign error: %s", exc)


def save_campaign(session) -> bool:
    """Persist a live session to the database."""
    try:
        campaign_id = getattr(session, 'id', 'unknown')
        persisted_state = extract_persistable_campaign_state(session)
        map_url    = str(getattr(session, 'map_image_url', '') or '')
        dm_map_url = str(getattr(session, 'dm_current_map_url', '') or '')
        serialized_fields = {
            field: _serialize_campaign_field(campaign_id, field, persisted_state[field])
            for field in _CAMPAIGN_JSON_FIELD_DEFAULTS
        }
        _warn_large_persisted_field(campaign_id, "map_image_url", map_url)
        _warn_large_persisted_field(campaign_id, "dm_current_map_url", dm_map_url)
        with get_conn() as conn:
            now = time.time()
            _save_campaign_row(conn, session, serialized_fields, persisted_state, now)
            _save_players(conn, session)
            _save_pois(conn, session)
            _save_tokens(conn, session)
            _save_logs(conn, session)
        return True
    except Exception as e:
        _diagnose_save_error(session, e)
        return False


def load_campaign(campaign_id: str) -> Optional[dict]:
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not row:
                return None
            row_keys = set(row.keys()) if hasattr(row, "keys") else set()
            def _row_value(key: str, default=None):
                if key in row_keys:
                    return row[key]
                return default
            tokens  = conn.execute("SELECT * FROM tokens WHERE campaign_id=?", (campaign_id,)).fetchall()
            logs    = conn.execute("SELECT * FROM logs WHERE campaign_id=? ORDER BY timestamp", (campaign_id,)).fetchall()
            players = conn.execute("SELECT * FROM players WHERE campaign_id=?", (campaign_id,)).fetchall()
            pois    = conn.execute("SELECT * FROM pois WHERE campaign_id=?", (campaign_id,)).fetchall()
            poi_rows = []
            for poi in pois:
                row_poi = dict(poi)
                try:
                    raw_interactable = json.loads(row_poi.get("interactable") or "{}")
                    row_poi["interactable"] = raw_interactable if isinstance(raw_interactable, dict) else {}
                except Exception:
                    row_poi["interactable"] = {}
                poi_rows.append(row_poi)
            payload = {
                "id": row["id"], "name": row["name"], "dm_name": row["dm_name"],
                "player_invite": row["player_invite"], "viewer_invite": row["viewer_invite"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "map_image_url": _row_value("map_image_url"),
                "dm_map_context": _row_value("dm_map_context", "world") or "world",
                "dm_current_map_url": _row_value("dm_current_map_url"),
                "dm_id": _row_value("dm_id") or None,
                "dm_player_key": _row_value("dm_player_key") or None,
                "show_viewer_presence": bool(_row_value("show_viewer_presence", 0) or 0),
                "tokens":  [_sanitize_token_row(dict(t)) for t in tokens],
                "logs":    [dict(l) for l in logs],
                "players": [dict(p) for p in players],
                "pois":    poi_rows,
            }
            for field in _CAMPAIGN_JSON_FIELD_DEFAULTS:
                payload[field] = _deserialize_campaign_field(campaign_id, field, _row_value(field))
            return normalize_persisted_campaign_data(payload)
    except Exception as e:
        logger.error("[DB] load_campaign error: %s", e)
        return None


def list_campaigns() -> list:
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, dm_name, created_at, updated_at FROM campaigns ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[DB] list_campaigns error: %s", e)
        return []


def delete_campaign(campaign_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM players WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM pois    WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM logs   WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM tokens WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))


def _get_dm_name(session) -> str:
    if session.dm_id and session.dm_id in session.users:
        return session.users[session.dm_id].name
    return "Dungeon Master"


def _get_dm_player_key(session) -> Optional[str]:
    """Return the DM session user's stored player_key (auth linkage) for persistence.

    The DM is not stored in the players table, so its player_key must be saved on
    the campaigns row. Without this, a restored session loses the DM↔auth linkage
    and a returning DM is resolved as a stranger/viewer (WS handshake denied).
    """
    dm_id = getattr(session, 'dm_id', None)
    if dm_id and dm_id in session.users:
        key = str(getattr(session.users[dm_id], 'player_key', '') or '').strip()
        return key or None
    return None


_SHOP_TYPE_DEFAULT_PROFESSIONS = {
    "blacksmith": ["blacksmithing"],
    "alchemist": ["alchemy"],
    "general": ["woodworking", "shipwright"],
    "magic": ["tailoring", "jeweling"],
    "black_market": ["leatherworking", "tinkering"],
}


def _seed_professions(conn) -> None:
    now = time.time()
    starter = [
        {
            "id": "blacksmithing",
            "name": "Blacksmithing",
            "description": "Forge metal gear and reinforcement components.",
            "taught_by_shop_types_json": json.dumps(["blacksmith"]),
            "tool_hints_json": json.dumps(["Smith's tools", "Forge", "Tongs"]),
        },
        {
            "id": "leatherworking",
            "name": "Leatherworking",
            "description": "Craft hides, straps, and leather armor upgrades.",
            "taught_by_shop_types_json": json.dumps(["general", "black_market"]),
            "tool_hints_json": json.dumps(["Leatherworker's tools", "Awl", "Needle set"]),
        },
        {
            "id": "alchemy",
            "name": "Potion Crafting / Alchemy",
            "description": "Prepare tinctures and simple potion batches.",
            "taught_by_shop_types_json": json.dumps(["alchemist", "magic"]),
            "tool_hints_json": json.dumps(["Alchemist's supplies", "Glassware", "Mortar & pestle"]),
        },
        {
            "id": "woodworking",
            "name": "Woodworking",
            "description": "Shape timber parts, bows, and field utility items.",
            "taught_by_shop_types_json": json.dumps(["general"]),
            "tool_hints_json": json.dumps(["Woodcarver's tools", "Saw", "Chisel set"]),
        },
        {
            "id": "tailoring",
            "name": "Tailoring",
            "description": "Sew cloth gear, travel wear, and padded linings.",
            "taught_by_shop_types_json": json.dumps(["magic", "general"]),
            "tool_hints_json": json.dumps(["Weaver's tools", "Needle kit", "Loom basics"]),
        },
        {
            "id": "tinkering",
            "name": "Tinkering",
            "description": "Build clever gadgets, launcher rigs, and mechanical utility tools.",
            "taught_by_shop_types_json": json.dumps(["blacksmith", "black_market"]),
            "tool_hints_json": json.dumps(["Tinker's tools", "Precision screwdriver", "Spring clamp set"]),
        },
        {
            "id": "shipwright",
            "name": "Navigation & Shipwrighting",
            "description": "Craft rigging tools, navigation kits, and sea-hardened utility gear.",
            "taught_by_shop_types_json": json.dumps(["general", "black_market"]),
            "tool_hints_json": json.dumps(["Carpenter's tools", "Navigator's tools", "Pitch and cord"]),
        },
        {
            "id": "jeweling",
            "name": "Jeweling",
            "description": "Set gems, craft signets and charms, and prepare focus-grade inlays.",
            "taught_by_shop_types_json": json.dumps(["magic", "general"]),
            "tool_hints_json": json.dumps(["Jeweler's tools", "Fine files", "Gem loupe"]),
        },
        {
            "id": "herbalism",
            "name": "Herbalism",
            "description": "Harvest herbs and fungi, then refine practical salves and draughts.",
            "taught_by_shop_types_json": json.dumps(["alchemist", "general"]),
            "tool_hints_json": json.dumps(["Herbalism kit", "Drying rack", "Field knife"]),
        },
    ]
    for row in starter:
        conn.execute("""
            INSERT OR IGNORE INTO professions (
                id, name, description, taught_by_shop_types_json, tool_hints_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            row["id"], row["name"], row["description"],
            row["taught_by_shop_types_json"], row["tool_hints_json"], now,
        ))


def _safe_json_array(raw) -> list:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return list(value) if isinstance(value, list) else []
    except Exception:
        return []


def _safe_json_object(raw) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return dict(value) if isinstance(value, dict) else {}
    except Exception:
        return {}


def _normalize_profession_id(raw: str) -> str:
    return str(raw or "").strip().lower()[:40]


def get_default_profession_ids_for_shop_type(shop_type: str) -> list[str]:
    st = str(shop_type or "").strip().lower()
    return list(_SHOP_TYPE_DEFAULT_PROFESSIONS.get(st, []))


def list_professions() -> list[dict]:
    try:
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM professions ORDER BY name COLLATE NOCASE").fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["taught_by_shop_types_json"] = _safe_json_array(item.get("taught_by_shop_types_json"))
                item["tool_hints_json"] = _safe_json_array(item.get("tool_hints_json"))
                out.append(item)
            return out
    except Exception as e:
        logger.error("[DB] list_professions error: %s", e)
        return []


def get_profession_by_id(profession_id: str) -> Optional[dict]:
    pid = _normalize_profession_id(profession_id)
    if not pid:
        return None
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM professions WHERE id=?", (pid,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["taught_by_shop_types_json"] = _safe_json_array(item.get("taught_by_shop_types_json"))
            item["tool_hints_json"] = _safe_json_array(item.get("tool_hints_json"))
            return item
    except Exception as e:
        logger.error("[DB] get_profession_by_id error: %s", e)
        return None


def get_player_professions(campaign_id: str, user_id: str) -> list[str]:
    cid = str(campaign_id or "").strip()[:64]
    uid = str(user_id or "").strip()[:64]
    if not cid or not uid:
        return []
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT profession_ids_json FROM player_professions WHERE campaign_id=? AND user_id=?",
                (cid, uid)
            ).fetchone()
            if not row:
                return []
            ids = [_normalize_profession_id(x) for x in _safe_json_array(row["profession_ids_json"])]
            return [x for x in ids if x][:2]
    except Exception as e:
        logger.error("[DB] get_player_professions error: %s", e)
        return []


def set_player_professions(campaign_id: str, user_id: str, profession_ids: list[str]) -> list[str]:
    cid = str(campaign_id or "").strip()[:64]
    uid = str(user_id or "").strip()[:64]
    if not cid or not uid:
        return []
    cleaned: list[str] = []
    seen = set()
    for entry in (profession_ids or []):
        pid = _normalize_profession_id(entry)
        if not pid or pid in seen:
            continue
        seen.add(pid)
        cleaned.append(pid)
        if len(cleaned) >= 2:
            break
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO player_professions (campaign_id, user_id, profession_ids_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(campaign_id, user_id)
                DO UPDATE SET profession_ids_json=excluded.profession_ids_json, updated_at=excluded.updated_at
            """, (cid, uid, json.dumps(cleaned), time.time()))
        return cleaned
    except Exception as e:
        logger.error("[DB] set_player_professions error: %s", e)
        return []


def resolve_shop_taught_profession_ids(shop: dict) -> list[str]:
    taught = [_normalize_profession_id(x) for x in _safe_json_array(shop.get("taught_professions_json"))]
    taught = [x for x in taught if x]
    if taught:
        return taught
    return get_default_profession_ids_for_shop_type(str(shop.get("shop_type") or ""))


def _seed_crafting_recipes(conn) -> None:
    # Stage 8: Starter Content Pack — recipes use canonical mat_ material names.
    # INSERT OR REPLACE ensures updated seed data takes effect on existing installs
    # while still being idempotent (duplicate calls produce no additional rows).
    now = time.time()
    recipes = [
        {
            "id": "rec_minor_healing_draught",
            "name": "Minor Healing Draught",
            "result_item_json": json.dumps({
                "id": "crafted_minor_healing_draught",
                "name": "Minor Healing Draught",
                "notes": (
                    "A restorative tonic sealed in glass and brewed from crushed sunleaf. "
                    "Eases minor wounds and fatigue during travel or after skirmishes."
                ),
                "category": "Consumable",
                "item_type": "potion",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["alchemy"]),
            "requires_materials_json": json.dumps([
                {"name": "Glass Vial", "qty": 1},
                {"name": "Sunleaf Bunch", "qty": 1},
            ]),
            "fee_units": 90,
            "duration_seconds": 120,
            "station_shop_types_json": json.dumps(["alchemist", "magic"]),
            "tags_json": json.dumps(["potion", "healing", "starter"]),
            "rarity": "common",
        },
        {
            "id": "rec_leather_patch_kit",
            "name": "Leather Patch Kit",
            "result_item_json": json.dumps({
                "id": "crafted_leather_patch_kit",
                "name": "Leather Patch Kit",
                "notes": (
                    "Sturdy cured-hide patches backed with iron rivets, wax cord, and a compact awl. "
                    "An essential kit for field repairs to armor, bags, and tack."
                ),
                "category": "Tool",
                "item_type": "tool",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["leatherworking"]),
            "requires_materials_json": json.dumps([
                {"name": "Cured Hide", "qty": 2},
                {"name": "Iron Ingot", "qty": 1},
            ]),
            "fee_units": 140,
            "duration_seconds": 150,
            "station_shop_types_json": json.dumps(["general", "black_market", "blacksmith"]),
            "tags_json": json.dumps(["repair", "tool", "starter"]),
            "rarity": "common",
        },
        {
            "id": "rec_batwing_cloak",
            "name": "Batwing Cloak",
            "result_item_json": json.dumps({
                "id": "crafted_batwing_cloak",
                "name": "Batwing Cloak",
                "notes": (
                    "A midnight cloak cut in layered bat-wing arcs and treated with shadow resin "
                    "for natural stealth. Worn by cave scouts, night couriers, and shadow-walkers "
                    "who move unseen at the edges of torchlight."
                ),
                "category": "Gear",
                "item_type": "cloak",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["tailoring", "leatherworking"]),
            "requires_materials_json": json.dumps([
                {"name": "Bat Wing Membrane", "qty": 2},
                {"name": "Cured Hide", "qty": 1},
                {"name": "Shadow Resin", "qty": 1},
            ]),
            "fee_units": 520,
            "duration_seconds": 480,
            "station_shop_types_json": json.dumps(["magic", "black_market", "general"]),
            "tags_json": json.dumps(["cloak", "bat-themed", "stealth"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_treated_bandage_roll",
            "name": "Treated Bandage Roll",
            "result_item_json": json.dumps({
                "id": "crafted_treated_bandage_roll",
                "name": "Treated Bandage Roll",
                "notes": "A clean medicinal wrap packed with stitchleaf and salt resin for fast field treatment.",
                "category": "Consumable",
                "item_type": "consumable",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["herbalism", "alchemy"]),
            "requires_materials_json": json.dumps([{"name": "Sunleaf Bunch", "qty": 1}, {"name": "Glass Vial", "qty": 1}]),
            "fee_units": 70,
            "duration_seconds": 90,
            "station_shop_types_json": json.dumps(["alchemist", "general"]),
            "tags_json": json.dumps(["field-medicine", "bandage", "healing-family"]),
            "rarity": "common",
        },
        {
            "id": "rec_stormguard_tonic",
            "name": "Stormguard Tonic",
            "result_item_json": json.dumps({
                "id": "crafted_stormguard_tonic",
                "name": "Stormguard Tonic",
                "notes": "A conductive tonic brewed for sailors to blunt lightning arcs and bitter sea-cold.",
                "category": "Potion",
                "item_type": "potion",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["alchemy"]),
            "requires_materials_json": json.dumps([{"name": "Glass Vial", "qty": 1}, {"name": "Crystal Shard Cluster", "qty": 1}, {"name": "Frostcap Mushroom", "qty": 1}]),
            "fee_units": 240,
            "duration_seconds": 240,
            "station_shop_types_json": json.dumps(["alchemist", "magic"]),
            "tags_json": json.dumps(["resistance-family", "potion", "pirate"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_flash_prism_capsule",
            "name": "Flash Prism Capsule",
            "result_item_json": json.dumps({
                "id": "crafted_flash_prism_capsule",
                "name": "Flash Prism Capsule",
                "notes": "A brittle lens-capsule that pops in a bright flash for disruption and escape.",
                "category": "Consumable",
                "item_type": "consumable",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["tinkering", "alchemy"]),
            "requires_materials_json": json.dumps([{"name": "Crystal Shard Cluster", "qty": 1}, {"name": "Amberglass Vial", "qty": 1}]),
            "fee_units": 110,
            "duration_seconds": 120,
            "station_shop_types_json": json.dumps(["blacksmith", "black_market", "alchemist"]),
            "tags_json": json.dumps(["tinker-gadget-family", "utility", "consumable"]),
            "rarity": "common",
        },
        {
            "id": "rec_smoke_injector_charges",
            "name": "Smoke Injector Charges",
            "result_item_json": json.dumps({
                "id": "crafted_smoke_injector_charges",
                "name": "Smoke Injector Charges (3)",
                "notes": "A batch of compact smoke charges sized for belt launchers and injector tubes.",
                "category": "Consumable",
                "item_type": "consumable",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["tinkering", "alchemy"]),
            "requires_materials_json": json.dumps([{"name": "Glass Vial", "qty": 1}, {"name": "Bone Shards", "qty": 1}]),
            "fee_units": 120,
            "duration_seconds": 140,
            "station_shop_types_json": json.dumps(["black_market", "alchemist"]),
            "tags_json": json.dumps(["tinker-gadget-family", "smoke", "consumable"]),
            "rarity": "common",
        },
        {
            "id": "rec_grapnel_spool_launcher",
            "name": "Grapnel Spool Launcher",
            "result_item_json": json.dumps({
                "id": "crafted_grapnel_spool_launcher",
                "name": "Grapnel Spool Launcher",
                "notes": "A spring-loaded traversal device with lock-safe spool for climbing and boarding.",
                "category": "Gear",
                "item_type": "gear",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["tinkering"]),
            "requires_materials_json": json.dumps([{"name": "Bloomsteel Ingot", "qty": 1}, {"name": "Hardwood Plank Bundle", "qty": 1}, {"name": "Spider Silk Spool", "qty": 1}]),
            "fee_units": 360,
            "duration_seconds": 360,
            "station_shop_types_json": json.dumps(["blacksmith", "black_market"]),
            "tags_json": json.dumps(["tinker-gadget-family", "traversal", "tool"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_signal_beetle",
            "name": "Signal Beetle",
            "result_item_json": json.dumps({
                "id": "crafted_signal_beetle",
                "name": "Signal Beetle",
                "notes": "A tiny clockwork messenger that blinks coded pulses to paired receivers.",
                "category": "Gear",
                "item_type": "gear",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["tinkering", "jeweling"]),
            "requires_materials_json": json.dumps([{"name": "Crystal Shard Cluster", "qty": 1}, {"name": "Aether Dust", "qty": 1}]),
            "fee_units": 420,
            "duration_seconds": 420,
            "station_shop_types_json": json.dumps(["magic", "blacksmith"]),
            "tags_json": json.dumps(["tinker-gadget-family", "communication", "utility"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_weatherproof_deck_boots",
            "name": "Weatherproof Deck Boots",
            "result_item_json": json.dumps({
                "id": "crafted_weatherproof_deck_boots",
                "name": "Weatherproof Deck Boots",
                "notes": "Tar-sealed boots with reinforced soles for wet decks and storm marches.",
                "category": "Gear",
                "item_type": "boots",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["leatherworking"]),
            "requires_materials_json": json.dumps([{"name": "Cured Hide", "qty": 2}, {"name": "Scaled Scraps", "qty": 1}]),
            "fee_units": 180,
            "duration_seconds": 180,
            "station_shop_types_json": json.dumps(["general", "black_market"]),
            "tags_json": json.dumps(["pirate-utility-family", "boots", "travel"]),
            "rarity": "common",
        },
        {
            "id": "rec_smugglers_satchel",
            "name": "Smuggler's Satchel",
            "result_item_json": json.dumps({
                "id": "crafted_smugglers_satchel",
                "name": "Smuggler's Satchel",
                "notes": "A moisture-lined satchel with hidden seams and reinforced inner pockets.",
                "category": "Gear",
                "item_type": "gear",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["leatherworking", "shipwright"]),
            "requires_materials_json": json.dumps([{"name": "Cured Hide", "qty": 2}, {"name": "Spider Silk Spool", "qty": 1}]),
            "fee_units": 250,
            "duration_seconds": 300,
            "station_shop_types_json": json.dumps(["black_market", "general"]),
            "tags_json": json.dumps(["pirate-utility-family", "container", "utility"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_boarding_grapple_bundle",
            "name": "Boarding Grapple Bundle",
            "result_item_json": json.dumps({
                "id": "crafted_boarding_grapple_bundle",
                "name": "Boarding Grapple Bundle",
                "notes": "A quick-rig boarding set with weighted hook, tarred line, and release knot.",
                "category": "Gear",
                "item_type": "gear",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["shipwright", "woodworking"]),
            "requires_materials_json": json.dumps([{"name": "Hardwood Plank Bundle", "qty": 1}, {"name": "Iron Ingot", "qty": 1}]),
            "fee_units": 190,
            "duration_seconds": 180,
            "station_shop_types_json": json.dumps(["general", "blacksmith"]),
            "tags_json": json.dumps(["pirate-utility-family", "traversal", "tool"]),
            "rarity": "common",
        },
        {
            "id": "rec_stormshore_cloak",
            "name": "Stormshore Cloak",
            "result_item_json": json.dumps({
                "id": "crafted_stormshore_cloak",
                "name": "Stormshore Cloak",
                "notes": "A stormproof cloak stitched with conductive thread and waxed sea-cloth.",
                "category": "Gear",
                "item_type": "cloak",
                "rarity": "rare",
            }),
            "requires_professions_json": json.dumps(["tailoring", "alchemy"]),
            "requires_materials_json": json.dumps([{"name": "Spider Silk Spool", "qty": 1}, {"name": "Scaled Scraps", "qty": 1}, {"name": "Crystal Shard Cluster", "qty": 1}]),
            "fee_units": 650,
            "duration_seconds": 540,
            "station_shop_types_json": json.dumps(["magic", "general"]),
            "tags_json": json.dumps(["tailor-family", "pirate-utility-family", "rare"]),
            "rarity": "rare",
        },
        # ── EXPANSION PACK: deeper profession content ─────────────────────────
        # Blacksmithing
        {
            "id": "rec_iron_buckler",
            "name": "Iron Buckler",
            "result_item_json": json.dumps({
                "id": "crafted_iron_buckler",
                "name": "Iron Buckler",
                "notes": (
                    "A compact round shield forged from double-folded iron with a hide-wrapped center grip. "
                    "Lighter than a standard shield but fully functional at +2 AC. "
                    "Favored by scouts, fencers, and sailors who need a free off-hand for rigging work."
                ),
                "category": "Armor",
                "item_type": "shield",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["blacksmithing"]),
            "requires_materials_json": json.dumps([
                {"name": "Iron Ingot", "qty": 2},
                {"name": "Cured Hide", "qty": 1},
            ]),
            "fee_units": 200,
            "duration_seconds": 200,
            "station_shop_types_json": json.dumps(["blacksmith"]),
            "tags_json": json.dumps(["blacksmith-family", "shield", "combat"]),
            "rarity": "common",
        },
        {
            "id": "rec_throwing_axe_bundle",
            "name": "Throwing Axe Bundle",
            "result_item_json": json.dumps({
                "id": "crafted_throwing_axe_bundle",
                "name": "Throwing Axe Bundle (3)",
                "notes": (
                    "Three balanced throwing axes with hickory handles, weighted for distance throws "
                    "and close-range fighting alike. A reliable loadout for scouts, soldiers, and deck fighters."
                ),
                "category": "Weapon",
                "item_type": "weapon",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["blacksmithing"]),
            "requires_materials_json": json.dumps([
                {"name": "Iron Ingot", "qty": 2},
                {"name": "Hardwood Plank Bundle", "qty": 1},
            ]),
            "fee_units": 250,
            "duration_seconds": 220,
            "station_shop_types_json": json.dumps(["blacksmith"]),
            "tags_json": json.dumps(["blacksmith-family", "weapon", "combat"]),
            "rarity": "common",
        },
        {
            "id": "rec_boarding_shield",
            "name": "Boarding Shield",
            "result_item_json": json.dumps({
                "id": "crafted_boarding_shield",
                "name": "Boarding Shield",
                "notes": (
                    "A compact circular shield reinforced with iron studs along the rim. "
                    "Provides the standard +2 AC bonus of a shield and can also be used to "
                    "shove opponents in narrow corridors and gangways."
                ),
                "category": "Armor",
                "item_type": "shield",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["blacksmithing"]),
            "requires_materials_json": json.dumps([
                {"name": "Iron Ingot", "qty": 2},
                {"name": "Cured Hide", "qty": 1},
            ]),
            "fee_units": 220,
            "duration_seconds": 200,
            "station_shop_types_json": json.dumps(["blacksmith"]),
            "tags_json": json.dumps(["blacksmith-family", "pirate-utility-family", "shield"]),
            "rarity": "uncommon",
        },
        # Leatherworking
        {
            "id": "rec_scaled_vest",
            "name": "Scaled Leather Vest",
            "result_item_json": json.dumps({
                "id": "crafted_scaled_vest",
                "name": "Scaled Leather Vest",
                "notes": (
                    "A tanned hide vest reinforced with overlapping scale panels along the torso. "
                    "Treated leather with scale overlay gives it the protection profile of studded leather "
                    "while still allowing free movement. Favored by rangers, rogues, and corsairs."
                ),
                "category": "Armor",
                "item_type": "armor",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["leatherworking"]),
            "requires_materials_json": json.dumps([
                {"name": "Cured Hide", "qty": 2},
                {"name": "Scaled Scraps", "qty": 1},
            ]),
            "fee_units": 240,
            "duration_seconds": 240,
            "station_shop_types_json": json.dumps(["general", "black_market"]),
            "tags_json": json.dumps(["leatherwork-family", "armor", "pirate"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_corsair_vest",
            "name": "Corsair's Boiled-Leather Vest",
            "result_item_json": json.dumps({
                "id": "crafted_corsair_vest",
                "name": "Corsair's Boiled-Leather Vest",
                "notes": (
                    "Boiled and lacquer-sealed leather vest worn over a shirt for the AC profile "
                    "of light armor without the bulk of studded leather. A corsair staple favoring "
                    "mobility on rolling decks and tight rigging."
                ),
                "category": "Armor",
                "item_type": "armor",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["leatherworking"]),
            "requires_materials_json": json.dumps([
                {"name": "Cured Hide", "qty": 3},
                {"name": "Scaled Scraps", "qty": 1},
            ]),
            "fee_units": 310,
            "duration_seconds": 300,
            "station_shop_types_json": json.dumps(["black_market", "general"]),
            "tags_json": json.dumps(["leatherwork-family", "pirate-utility-family", "armor"]),
            "rarity": "uncommon",
        },
        # Alchemy / Herbalism
        {
            "id": "rec_fever_break_draught",
            "name": "Fever-Break Draught",
            "result_item_json": json.dumps({
                "id": "crafted_fever_break_draught",
                "name": "Fever-Break Draught",
                "notes": (
                    "A sharp herbal brew using sunleaf and deepmoss that clears fever and restores "
                    "focus. Removes the Poisoned condition when it is caused by a disease effect "
                    "(subject to DM ruling). A practical and cheap medic staple."
                ),
                "category": "Consumable",
                "item_type": "consumable",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["herbalism", "alchemy"]),
            "requires_materials_json": json.dumps([
                {"name": "Sunleaf Bunch", "qty": 2},
                {"name": "Deepmoss Herb", "qty": 1},
                {"name": "Glass Vial", "qty": 1},
            ]),
            "fee_units": 80,
            "duration_seconds": 100,
            "station_shop_types_json": json.dumps(["alchemist", "general"]),
            "tags_json": json.dumps(["healing-family", "field-medicine", "consumable"]),
            "rarity": "common",
        },
        {
            "id": "rec_antitoxin_brew",
            "name": "Artisan Antitoxin",
            "result_item_json": json.dumps({
                "id": "crafted_antitoxin_brew",
                "name": "Artisan Antitoxin",
                "notes": (
                    "A refined antitoxin brewed from a rendered venom sac neutralized by sunleaf reduction. "
                    "Grants advantage on Constitution saving throws against all poison for 1 hour — stronger "
                    "than the standard antitoxin vial found at general stores."
                ),
                "category": "Consumable",
                "item_type": "consumable",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["herbalism", "alchemy"]),
            "requires_materials_json": json.dumps([
                {"name": "Venom Sac", "qty": 1},
                {"name": "Sunleaf Bunch", "qty": 1},
                {"name": "Glass Vial", "qty": 1},
            ]),
            "fee_units": 200,
            "duration_seconds": 200,
            "station_shop_types_json": json.dumps(["alchemist"]),
            "tags_json": json.dumps(["field-medicine", "antitoxin", "alchemy-family"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_numbing_poultice",
            "name": "Numbing Poultice",
            "result_item_json": json.dumps({
                "id": "crafted_numbing_poultice",
                "name": "Numbing Poultice",
                "notes": (
                    "A damp clay-and-herb wrap made from frostcap mushroom and crushed sunleaf. "
                    "Applied to a fresh wound it dulls sharp pain and slows bleeding from shallow cuts. "
                    "Common field medicine among mercenary surgeons and herbalists."
                ),
                "category": "Consumable",
                "item_type": "consumable",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["herbalism"]),
            "requires_materials_json": json.dumps([
                {"name": "Frostcap Mushroom", "qty": 1},
                {"name": "Sunleaf Bunch", "qty": 1},
            ]),
            "fee_units": 60,
            "duration_seconds": 80,
            "station_shop_types_json": json.dumps(["alchemist", "general"]),
            "tags_json": json.dumps(["healing-family", "field-medicine", "consumable"]),
            "rarity": "common",
        },
        {
            "id": "rec_travel_tonic",
            "name": "Trail Endurance Tonic",
            "result_item_json": json.dumps({
                "id": "crafted_travel_tonic",
                "name": "Trail Endurance Tonic",
                "notes": (
                    "A bracing tea-tonic brewed from double-strength sunleaf with frostcap edge. "
                    "Popular with couriers, long-march soldiers, and scouts who need to push past "
                    "fatigue. Reduces exhaustion penalties for 2 hours."
                ),
                "category": "Potion",
                "item_type": "potion",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["herbalism"]),
            "requires_materials_json": json.dumps([
                {"name": "Sunleaf Bunch", "qty": 2},
                {"name": "Frostcap Mushroom", "qty": 1},
                {"name": "Glass Vial", "qty": 1},
            ]),
            "fee_units": 90,
            "duration_seconds": 100,
            "station_shop_types_json": json.dumps(["alchemist", "general"]),
            "tags_json": json.dumps(["healing-family", "potion", "travel"]),
            "rarity": "common",
        },
        # Woodworking
        {
            "id": "rec_recurve_bow",
            "name": "Recurve Hunting Bow",
            "result_item_json": json.dumps({
                "id": "crafted_recurve_bow",
                "name": "Recurve Hunting Bow",
                "notes": (
                    "A recurved bow carved from darkwood limbs and strung with spider silk. "
                    "The recurve draw gives it better range and punch than a standard shortbow "
                    "while remaining compact enough for mounted use or forest stalking. "
                    "Use the longbow damage profile."
                ),
                "category": "Weapon",
                "item_type": "weapon",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["woodworking"]),
            "requires_materials_json": json.dumps([
                {"name": "Darkwood Plank", "qty": 2},
                {"name": "Spider Silk Spool", "qty": 1},
            ]),
            "fee_units": 280,
            "duration_seconds": 300,
            "station_shop_types_json": json.dumps(["general"]),
            "tags_json": json.dumps(["woodwork-family", "weapon", "bow"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_iron_spike_bundle",
            "name": "Iron-Tipped Stake Bundle",
            "result_item_json": json.dumps({
                "id": "crafted_iron_spike_bundle",
                "name": "Iron-Tipped Stake Bundle (5)",
                "notes": (
                    "Five hardwood stakes with hammered iron tips. Used as perimeter spikes, "
                    "improvised caltrops, vampire deterrents, and tent pegs in a pinch. "
                    "A cheap, versatile field supply."
                ),
                "category": "Gear",
                "item_type": "gear",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["woodworking", "blacksmithing"]),
            "requires_materials_json": json.dumps([
                {"name": "Hardwood Plank Bundle", "qty": 1},
                {"name": "Iron Ingot", "qty": 1},
            ]),
            "fee_units": 120,
            "duration_seconds": 120,
            "station_shop_types_json": json.dumps(["general", "blacksmith"]),
            "tags_json": json.dumps(["woodwork-family", "field-gear", "trap"]),
            "rarity": "common",
        },
        # Tailoring
        {
            "id": "rec_padded_surcoat",
            "name": "Padded Surcoat",
            "result_item_json": json.dumps({
                "id": "crafted_padded_surcoat",
                "name": "Padded Surcoat",
                "notes": (
                    "A quilted coat stitched from spider-silk over a hide base. "
                    "Worn over clothing as light armor. The silk layer cushions blows while the "
                    "hide backing maintains shape on campaign. AC profile matches padded armor."
                ),
                "category": "Armor",
                "item_type": "armor",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["tailoring", "leatherworking"]),
            "requires_materials_json": json.dumps([
                {"name": "Cured Hide", "qty": 1},
                {"name": "Spider Silk Spool", "qty": 1},
            ]),
            "fee_units": 200,
            "duration_seconds": 240,
            "station_shop_types_json": json.dumps(["magic", "general"]),
            "tags_json": json.dumps(["tailor-family", "armor", "clothing"]),
            "rarity": "common",
        },
        {
            "id": "rec_winter_wrap",
            "name": "Winter Travel Wrap",
            "result_item_json": json.dumps({
                "id": "crafted_winter_wrap",
                "name": "Winter Travel Wrap",
                "notes": (
                    "A layered travel cloak stitched from spider-silk outer cloth with scale scrap "
                    "lining that traps heat without bulk. Favored by mountain scouts, cold-coast "
                    "traders, and rangers working boreal terrain."
                ),
                "category": "Gear",
                "item_type": "cloak",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["tailoring"]),
            "requires_materials_json": json.dumps([
                {"name": "Spider Silk Spool", "qty": 2},
                {"name": "Scaled Scraps", "qty": 1},
            ]),
            "fee_units": 230,
            "duration_seconds": 260,
            "station_shop_types_json": json.dumps(["magic", "general"]),
            "tags_json": json.dumps(["tailor-family", "cloak", "cold-resistance"]),
            "rarity": "uncommon",
        },
        # Tinkering
        {
            "id": "rec_arc_lantern",
            "name": "Arc Lantern",
            "result_item_json": json.dumps({
                "id": "crafted_arc_lantern",
                "name": "Arc Lantern",
                "notes": (
                    "A brass-cased lantern powered by an arc battery rather than oil. "
                    "Bright 30-foot radius; dim 30 more. Lasts 12 hours per battery charge. "
                    "Does not spill, does not flame out in wind or rain. "
                    "A tinker's answer to the problem of light on a rolling ship or a wet cave."
                ),
                "category": "Gear",
                "item_type": "gear",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["tinkering"]),
            "requires_materials_json": json.dumps([
                {"name": "Arc Battery Cell", "qty": 1},
                {"name": "Crystal Shard Cluster", "qty": 1},
                {"name": "Salvaged Brass Bundle", "qty": 1},
            ]),
            "fee_units": 320,
            "duration_seconds": 300,
            "station_shop_types_json": json.dumps(["blacksmith", "black_market"]),
            "tags_json": json.dumps(["tinker-gadget-family", "light", "utility"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_clockwork_sentry",
            "name": "Clockwork Sentry",
            "result_item_json": json.dumps({
                "id": "crafted_clockwork_sentry",
                "name": "Clockwork Sentry",
                "notes": (
                    "A wound-spring perimeter sentry with integrated alert mechanism. "
                    "Deploy as an action; covers a 20-foot radius and emits a loud tone when "
                    "disturbed by movement. Stays active for 8 hours before winding down. "
                    "Cannot be silenced without disabling the device (DC 14 Thieves' Tools)."
                ),
                "category": "Gear",
                "item_type": "gear",
                "rarity": "rare",
            }),
            "requires_professions_json": json.dumps(["tinkering"]),
            "requires_materials_json": json.dumps([
                {"name": "Clockwork Spring Set", "qty": 2},
                {"name": "Precision Gear Pack", "qty": 1},
                {"name": "Arc Battery Cell", "qty": 1},
            ]),
            "fee_units": 500,
            "duration_seconds": 480,
            "station_shop_types_json": json.dumps(["blacksmith", "black_market"]),
            "tags_json": json.dumps(["tinker-gadget-family", "sentry", "utility"]),
            "rarity": "rare",
        },
        # Shipwright / Pirate
        {
            "id": "rec_tar_caulk_kit",
            "name": "Tar Caulk Repair Kit",
            "result_item_json": json.dumps({
                "id": "crafted_tar_caulk_kit",
                "name": "Tar Caulk Repair Kit",
                "notes": (
                    "A field repair kit for hulls, barrels, and field shelters. "
                    "Contains pre-mixed tar caulk and pressing tools for sealing planks, "
                    "cracks, and waterway joints. Three standard uses per kit."
                ),
                "category": "Tool",
                "item_type": "tool",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["shipwright", "woodworking"]),
            "requires_materials_json": json.dumps([
                {"name": "Tarred Rope Bundle", "qty": 1},
                {"name": "Sea-Oak Plank", "qty": 1},
            ]),
            "fee_units": 130,
            "duration_seconds": 120,
            "station_shop_types_json": json.dumps(["general"]),
            "tags_json": json.dumps(["pirate-utility-family", "repair", "tool"]),
            "rarity": "common",
        },
        {
            "id": "rec_signal_beacon",
            "name": "Signal Beacon",
            "result_item_json": json.dumps({
                "id": "crafted_signal_beacon",
                "name": "Signal Beacon",
                "notes": (
                    "A brass-mounted signal fire on a folding hardwood post. "
                    "Burns signal-bright for 30 minutes and can be seen for up to 2 miles at night. "
                    "Standard pirate and naval rendezvous equipment."
                ),
                "category": "Gear",
                "item_type": "gear",
                "rarity": "common",
            }),
            "requires_professions_json": json.dumps(["shipwright"]),
            "requires_materials_json": json.dumps([
                {"name": "Salvaged Brass Bundle", "qty": 1},
                {"name": "Hardwood Plank Bundle", "qty": 1},
            ]),
            "fee_units": 150,
            "duration_seconds": 150,
            "station_shop_types_json": json.dumps(["general", "blacksmith"]),
            "tags_json": json.dumps(["pirate-utility-family", "signal", "navigation"]),
            "rarity": "common",
        },
        # Jeweling
        {
            "id": "rec_seafarers_amulet",
            "name": "Seafarer's Coral Amulet",
            "result_item_json": json.dumps({
                "id": "crafted_seafarers_amulet",
                "name": "Seafarer's Coral Amulet",
                "notes": (
                    "A polished coral shard mounted in a crystal-inlaid silver setting. "
                    "Worn as a protective charm against sea peril, storms, and salt exposure. "
                    "Prized by navigators, corsairs, and deep-sea divers."
                ),
                "category": "Wondrous Item",
                "item_type": "wondrous",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["jeweling"]),
            "requires_materials_json": json.dumps([
                {"name": "Coral Inlay Shards", "qty": 1},
                {"name": "Crystal Shard Cluster", "qty": 1},
            ]),
            "fee_units": 380,
            "duration_seconds": 360,
            "station_shop_types_json": json.dumps(["magic"]),
            "tags_json": json.dumps(["jewel-family", "pirate-utility-family", "amulet"]),
            "rarity": "uncommon",
        },
        {
            "id": "rec_resonance_focus",
            "name": "Resonance Focus",
            "result_item_json": json.dumps({
                "id": "crafted_resonance_focus",
                "name": "Resonance Focus",
                "notes": (
                    "A faceted crystal shard set in a copper-wire housing. "
                    "Used by casters as a spellcasting focus; also amplifies detection-school spells — "
                    "when used with Detect Magic, the range increases by 15 feet."
                ),
                "category": "Wondrous Item",
                "item_type": "wondrous",
                "rarity": "uncommon",
            }),
            "requires_professions_json": json.dumps(["jeweling", "tinkering"]),
            "requires_materials_json": json.dumps([
                {"name": "Crystal Shard Cluster", "qty": 2},
                {"name": "Copper Wire Spool", "qty": 1},
            ]),
            "fee_units": 420,
            "duration_seconds": 400,
            "station_shop_types_json": json.dumps(["magic"]),
            "tags_json": json.dumps(["jewel-family", "tinker-gadget-family", "focus"]),
            "rarity": "uncommon",
        },
    ]
    recipes += [
        {
            "id": "rec_emberward_pendant",
            "name": "Pendant of Emberward",
            "result_item_json": json.dumps({"id": "ring-of-warded-steps", "name": "Ring of Warded Steps", "category": "ring", "rarity": "rare", "item_type": "amulet"}),
            "requires_professions_json": json.dumps(["enchanting", "jeweling"]),
            "requires_materials_json": json.dumps([{"name": "Emberbloom Petal", "qty": 3}, {"name": "Crystal Shard Cluster", "qty": 1}, {"name": "Aether Dust", "qty": 1}]),
            "fee_units": 420, "duration_seconds": 600,
            "station_shop_types_json": json.dumps(["magic", "jeweler"]),
            "tags_json": json.dumps(["amulet", "fire", "enchanting"]), "rarity": "uncommon",
        },
        {
            "id": "rec_frost_lance_wand",
            "name": "Wand of Frost Lances",
            "result_item_json": json.dumps({"id": "wand-of-frost-lances", "name": "Wand of Frost Lances", "category": "wand", "rarity": "uncommon", "item_type": "wand"}),
            "requires_professions_json": json.dumps(["enchanting", "woodworking"]),
            "requires_materials_json": json.dumps([{"name": "Living Ironwood", "qty": 1}, {"name": "Frost Lotus", "qty": 2}, {"name": "Aether Dust", "qty": 2}]),
            "fee_units": 480, "duration_seconds": 720,
            "station_shop_types_json": json.dumps(["magic"]),
            "tags_json": json.dumps(["wand", "cold", "enchanting"]), "rarity": "uncommon",
        },
        {
            "id": "rec_goggles_deepdark",
            "name": "Goggles of the Deepdark",
            "result_item_json": json.dumps({"id": "goggles-of-the-deepdark", "name": "Goggles of the Deepdark", "category": "wondrous", "rarity": "common", "item_type": "wondrous"}),
            "requires_professions_json": json.dumps(["tinkering", "jeweling"]),
            "requires_materials_json": json.dumps([{"name": "Amberglass Vial", "qty": 2}, {"name": "Copper Wire Spool", "qty": 1}, {"name": "Shadow Resin", "qty": 1}]),
            "fee_units": 220, "duration_seconds": 480,
            "station_shop_types_json": json.dumps(["general", "magic"]),
            "tags_json": json.dumps(["wondrous", "vision", "starter"]), "rarity": "common",
        },
        {
            "id": "rec_mending_tide_amulet",
            "name": "Amulet of the Mending Tide",
            "result_item_json": json.dumps({"id": "amulet-of-the-mending-tide", "name": "Amulet of the Mending Tide", "category": "wondrous", "rarity": "uncommon", "item_type": "amulet"}),
            "requires_professions_json": json.dumps(["enchanting", "herbalism"]),
            "requires_materials_json": json.dumps([{"name": "Deepmoss Herb", "qty": 3}, {"name": "Coral Inlay Shards", "qty": 2}, {"name": "Wisp Essence", "qty": 1}]),
            "fee_units": 400, "duration_seconds": 660,
            "station_shop_types_json": json.dumps(["magic", "jeweler"]),
            "tags_json": json.dumps(["amulet", "healing", "enchanting"]), "rarity": "uncommon",
        },
        {
            "id": "rec_storm_cloak",
            "name": "Cloak of the Gale",
            "result_item_json": json.dumps({"id": "cloak-of-the-gale", "name": "Cloak of the Gale", "category": "wondrous", "rarity": "rare", "item_type": "wondrous"}),
            "requires_professions_json": json.dumps(["leatherworking", "enchanting"]),
            "requires_materials_json": json.dumps([{"name": "Thunderscale Hide", "qty": 2}, {"name": "Spider Silk Spool", "qty": 2}, {"name": "Aether Dust", "qty": 2}]),
            "fee_units": 900, "duration_seconds": 1200,
            "station_shop_types_json": json.dumps(["magic", "leatherworker"]),
            "tags_json": json.dumps(["cloak", "lightning", "enchanting"]), "rarity": "rare",
        },
        {
            "id": "rec_thornwarden_rod",
            "name": "Rod of the Thornwarden",
            "result_item_json": json.dumps({"id": "rod-of-the-thornwarden", "name": "Rod of the Thornwarden", "category": "rod", "rarity": "rare", "item_type": "rod"}),
            "requires_professions_json": json.dumps(["woodworking", "enchanting"]),
            "requires_materials_json": json.dumps([{"name": "Living Ironwood", "qty": 2}, {"name": "Deepmoss Herb", "qty": 2}, {"name": "Crystal Shard Cluster", "qty": 1}]),
            "fee_units": 880, "duration_seconds": 1200,
            "station_shop_types_json": json.dumps(["magic"]),
            "tags_json": json.dumps(["rod", "nature", "enchanting"]), "rarity": "rare",
        },
        {
            "id": "rec_sparks_circlet",
            "name": "Circlet of Whispered Sparks",
            "result_item_json": json.dumps({"id": "circlet-of-whispered-sparks", "name": "Circlet of Whispered Sparks", "category": "wondrous", "rarity": "uncommon", "item_type": "wondrous"}),
            "requires_professions_json": json.dumps(["tinkering", "enchanting"]),
            "requires_materials_json": json.dumps([{"name": "Arc Battery Cell", "qty": 1}, {"name": "Copper Wire Spool", "qty": 2}, {"name": "Quicksilver Dram", "qty": 1}]),
            "fee_units": 460, "duration_seconds": 720,
            "station_shop_types_json": json.dumps(["magic", "general"]),
            "tags_json": json.dumps(["circlet", "lightning", "enchanting"]), "rarity": "uncommon",
        },
        {
            "id": "rec_binding_vines_wand",
            "name": "Wand of Binding Vines",
            "result_item_json": json.dumps({"id": "wand-of-binding-vines", "name": "Wand of Binding Vines", "category": "wand", "rarity": "uncommon", "item_type": "wand"}),
            "requires_professions_json": json.dumps(["enchanting", "herbalism"]),
            "requires_materials_json": json.dumps([{"name": "Living Ironwood", "qty": 1}, {"name": "Spider Silk Spool", "qty": 2}, {"name": "Sunleaf Bunch", "qty": 1}]),
            "fee_units": 460, "duration_seconds": 700,
            "station_shop_types_json": json.dumps(["magic"]),
            "tags_json": json.dumps(["wand", "nature", "enchanting"]), "rarity": "uncommon",
        },
        {
            "id": "rec_antivenom_draught",
            "name": "Antivenom Draught",
            "result_item_json": json.dumps({"id": "crafted_antivenom_draught", "name": "Antivenom Draught", "category": "Consumable", "item_type": "potion", "rarity": "common"}),
            "requires_professions_json": json.dumps(["alchemy"]),
            "requires_materials_json": json.dumps([{"name": "Glass Vial", "qty": 1}, {"name": "Venom Sac", "qty": 1}, {"name": "Deepmoss Herb", "qty": 1}]),
            "fee_units": 120, "duration_seconds": 180,
            "station_shop_types_json": json.dumps(["alchemist", "magic"]),
            "tags_json": json.dumps(["potion", "cure", "starter"]), "rarity": "common",
        },
        {
            "id": "rec_frostfire_oil",
            "name": "Frostfire Coating Oil",
            "result_item_json": json.dumps({"id": "crafted_frostfire_oil", "name": "Frostfire Coating Oil", "category": "Consumable", "item_type": "oil", "rarity": "uncommon"}),
            "requires_professions_json": json.dumps(["alchemy"]),
            "requires_materials_json": json.dumps([{"name": "Amberglass Vial", "qty": 1}, {"name": "Emberbloom Petal", "qty": 1}, {"name": "Frost Lotus", "qty": 1}]),
            "fee_units": 260, "duration_seconds": 300,
            "station_shop_types_json": json.dumps(["alchemist", "magic"]),
            "tags_json": json.dumps(["oil", "weapon", "elemental"]), "rarity": "uncommon",
        },
        {
            "id": "rec_scaled_brigandine",
            "name": "Scaled Brigandine",
            "result_item_json": json.dumps({"id": "crafted_scaled_brigandine", "name": "Scaled Brigandine", "category": "Armor", "item_type": "armor", "rarity": "uncommon"}),
            "requires_professions_json": json.dumps(["leatherworking", "blacksmithing"]),
            "requires_materials_json": json.dumps([{"name": "Scaled Scraps", "qty": 4}, {"name": "Cured Hide", "qty": 2}, {"name": "Steel Ingot", "qty": 1}]),
            "fee_units": 520, "duration_seconds": 900,
            "station_shop_types_json": json.dumps(["blacksmith", "leatherworker"]),
            "tags_json": json.dumps(["armor", "medium"]), "rarity": "uncommon",
        },
        {
            "id": "rec_bloomsteel_blade",
            "name": "Bloomsteel Blade",
            "result_item_json": json.dumps({"id": "crafted_bloomsteel_blade", "name": "Bloomsteel Blade", "category": "Weapon", "item_type": "weapon", "rarity": "rare"}),
            "requires_professions_json": json.dumps(["blacksmithing", "enchanting"]),
            "requires_materials_json": json.dumps([{"name": "Bloomsteel Ingot", "qty": 2}, {"name": "Darkwood Plank", "qty": 1}, {"name": "Aether Dust", "qty": 1}]),
            "fee_units": 950, "duration_seconds": 1500,
            "station_shop_types_json": json.dumps(["blacksmith", "magic"]),
            "tags_json": json.dumps(["weapon", "sword", "enchanting"]), "rarity": "rare",
        },
        {
            "id": "rec_tideheart_vest",
            "name": "Vest of the Tideheart",
            "result_item_json": json.dumps({"id": "vest-of-the-tideheart", "name": "Vest of the Tideheart", "category": "wondrous", "rarity": "rare", "item_type": "wondrous"}),
            "requires_professions_json": json.dumps(["leatherworking", "enchanting"]),
            "requires_materials_json": json.dumps([{"name": "Scaled Scraps", "qty": 3}, {"name": "Coral Inlay Shards", "qty": 2}, {"name": "Wisp Essence", "qty": 1}]),
            "fee_units": 880, "duration_seconds": 1200,
            "station_shop_types_json": json.dumps(["magic", "leatherworker"]),
            "tags_json": json.dumps(["armor", "cold", "enchanting"]), "rarity": "rare",
        },
        {
            "id": "rec_fleetfoot_boots",
            "name": "Boots of the Fleetfoot",
            "result_item_json": json.dumps({"id": "boots-of-the-fleetfoot", "name": "Boots of the Fleetfoot", "category": "wondrous", "rarity": "uncommon", "item_type": "wondrous"}),
            "requires_professions_json": json.dumps(["leatherworking", "enchanting"]),
            "requires_materials_json": json.dumps([{"name": "Cured Hide", "qty": 2}, {"name": "Bat Wing Membrane", "qty": 2}, {"name": "Aether Dust", "qty": 1}]),
            "fee_units": 420, "duration_seconds": 720,
            "station_shop_types_json": json.dumps(["magic", "leatherworker"]),
            "tags_json": json.dumps(["boots", "speed", "enchanting"]), "rarity": "uncommon",
        },
    ]
    for row in recipes:
        conn.execute("""
            INSERT OR REPLACE INTO crafting_recipes (
                id, name, result_item_json, requires_professions_json, requires_materials_json,
                fee_units, duration_seconds, station_shop_types_json, tags_json, rarity, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["id"], row["name"], row["result_item_json"], row["requires_professions_json"],
            row["requires_materials_json"], int(row["fee_units"]), int(row["duration_seconds"]),
            row["station_shop_types_json"], row["tags_json"], row["rarity"], now,
        ))


def list_crafting_recipes() -> list[dict]:
    try:
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM crafting_recipes ORDER BY name COLLATE NOCASE").fetchall()
            out: list[dict] = []
            for row in rows:
                item = dict(row)
                item["result_item_json"] = _safe_json_object(item.get("result_item_json"))
                item["requires_professions_json"] = _safe_json_array(item.get("requires_professions_json"))
                item["requires_materials_json"] = _safe_json_array(item.get("requires_materials_json"))
                item["station_shop_types_json"] = _safe_json_array(item.get("station_shop_types_json"))
                item["tags_json"] = _safe_json_array(item.get("tags_json"))
                out.append(item)
            return out
    except Exception as e:
        logger.error("[DB] list_crafting_recipes error: %s", e)
        return []


def get_crafting_recipe(recipe_id: str) -> Optional[dict]:
    rid = str(recipe_id or "").strip()[:80]
    if not rid:
        return None
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM crafting_recipes WHERE id=?", (rid,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["result_item_json"] = _safe_json_object(item.get("result_item_json"))
            item["requires_professions_json"] = _safe_json_array(item.get("requires_professions_json"))
            item["requires_materials_json"] = _safe_json_array(item.get("requires_materials_json"))
            item["station_shop_types_json"] = _safe_json_array(item.get("station_shop_types_json"))
            item["tags_json"] = _safe_json_array(item.get("tags_json"))
            return item
    except Exception as e:
        logger.error("[DB] get_crafting_recipe error: %s", e)
        return None


def create_craft_job(campaign_id: str, user_id: str, recipe_id: str, shop_id: str, started_at: float,
                     ready_at: float, status: str, inputs_locked: list[dict], result_json: dict, logs: list[dict]) -> Optional[dict]:
    import secrets as _secrets
    try:
        job_id = _secrets.token_hex(12)
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO craft_jobs (
                    job_id, campaign_id, user_id, recipe_id, shop_id, started_at, ready_at, status,
                    inputs_locked_json, result_json, logs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id, str(campaign_id or "")[:64], str(user_id or "")[:64], str(recipe_id or "")[:80], str(shop_id or "")[:40],
                float(started_at or time.time()), float(ready_at or time.time()), str(status or "crafting")[:24],
                json.dumps(list(inputs_locked or [])), json.dumps(dict(result_json or {})), json.dumps(list(logs or [])),
            ))
        return get_craft_job(job_id)
    except Exception as e:
        logger.error("[DB] create_craft_job error: %s", e)
        return None


def get_craft_job(job_id: str) -> Optional[dict]:
    jid = str(job_id or "").strip()[:40]
    if not jid:
        return None
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM craft_jobs WHERE job_id=?", (jid,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["inputs_locked_json"] = _safe_json_array(item.get("inputs_locked_json"))
            item["result_json"] = _safe_json_object(item.get("result_json"))
            item["logs_json"] = _safe_json_array(item.get("logs_json"))
            return item
    except Exception as e:
        logger.error("[DB] get_craft_job error: %s", e)
        return None


def list_craft_jobs(campaign_id: str, user_id: str, *, shop_id: str | None = None) -> list[dict]:
    cid = str(campaign_id or "").strip()[:64]
    uid = str(user_id or "").strip()[:64]
    sid = str(shop_id or "").strip()[:40]
    if not cid or not uid:
        return []
    try:
        with get_conn() as conn:
            if sid:
                rows = conn.execute("""
                    SELECT * FROM craft_jobs
                    WHERE campaign_id=? AND user_id=? AND shop_id=?
                    ORDER BY started_at DESC
                """, (cid, uid, sid)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM craft_jobs
                    WHERE campaign_id=? AND user_id=?
                    ORDER BY started_at DESC
                """, (cid, uid)).fetchall()
            out: list[dict] = []
            for row in rows:
                item = dict(row)
                item["inputs_locked_json"] = _safe_json_array(item.get("inputs_locked_json"))
                item["result_json"] = _safe_json_object(item.get("result_json"))
                item["logs_json"] = _safe_json_array(item.get("logs_json"))
                out.append(item)
            return out
    except Exception as e:
        logger.error("[DB] list_craft_jobs error: %s", e)
        return []


def update_craft_job_status(job_id: str, status: str, logs: list[dict] | None = None) -> Optional[dict]:
    jid = str(job_id or "").strip()[:40]
    st = str(status or "").strip()[:24]
    if not jid or not st:
        return None
    try:
        with get_conn() as conn:
            if logs is None:
                conn.execute("UPDATE craft_jobs SET status=? WHERE job_id=?", (st, jid))
            else:
                conn.execute("UPDATE craft_jobs SET status=?, logs_json=? WHERE job_id=?", (st, json.dumps(list(logs or [])), jid))
        return get_craft_job(jid)
    except Exception as e:
        logger.error("[DB] update_craft_job_status error: %s", e)
        return None


# ─── Shop DB helpers ───────────────────────────────────────────────────────────

def _parse_shop_sell_fields(shop: dict) -> dict:
    """Parse sell-related JSON/integer fields on a shop dict, filling defaults."""
    shop["taught_professions_json"] = _safe_json_array(shop.get("taught_professions_json"))
    shop["accepted_item_types_json"] = _safe_json_array(
        shop.get("accepted_item_types_json") or
        '["weapon","armour","consumable","tool","material","trinket","magic","misc"]'
    )
    if not shop["accepted_item_types_json"]:
        shop["accepted_item_types_json"] = ["weapon", "armour", "consumable", "tool", "material", "trinket", "magic", "misc"]
    shop["buy_rate_pct"] = max(5, min(95, int(shop.get("buy_rate_pct") or 50)))
    raw_cash = shop.get("vendor_cash_units")
    shop["vendor_cash_units"] = int(raw_cash) if raw_cash is not None else None
    legacy_selling_enabled = bool(int(shop.get("selling_enabled") or 1))
    raw_shop_sales = shop.get("shop_sales_enabled")
    raw_player_sell = shop.get("player_sell_enabled")
    shop["shop_sales_enabled"] = legacy_selling_enabled if raw_shop_sales is None else bool(int(raw_shop_sales or 0))
    shop["player_sell_enabled"] = legacy_selling_enabled if raw_player_sell is None else bool(int(raw_player_sell or 0))
    # Backward-compat for old callers.
    shop["selling_enabled"] = shop["player_sell_enabled"]
    shop["buyback_enabled"] = bool(int(shop.get("buyback_enabled") or 0))
    personality = str(shop.get("personality") or "friendly").strip().lower()
    if personality not in {"friendly", "gruff", "greedy", "shifty", "scholarly"}:
        personality = "friendly"
    shop["personality"] = personality
    shop["dialogue_enabled"] = bool(int(shop.get("dialogue_enabled") if shop.get("dialogue_enabled") is not None else 1))
    shop["voice"] = str(shop.get("voice") or "grand_narrator").strip()[:80] or "grand_narrator"
    shop["tts_enabled"] = bool(int(shop.get("tts_enabled") or 0))
    shop["greeting_override"] = str(shop.get("greeting_override") or "")[:220]
    return shop


def get_shop_by_prop_id(campaign_id: str, prop_id: str) -> Optional[dict]:
    """Return the shop row (with inventory) for a given prop_id, or None."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shops WHERE campaign_id=? AND prop_id=?",
                (campaign_id, prop_id)
            ).fetchone()
            if not row:
                return None
            shop = dict(row)
            shop = _parse_shop_sell_fields(shop)
            shop["buy_categories_json"] = _safe_json_array(shop.get("buy_categories_json"))
            shop["crafting_enabled"] = bool(int(shop.get("crafting_enabled") or 0))
            items = conn.execute(
                "SELECT * FROM shop_inventory WHERE shop_id=? ORDER BY created_at",
                (shop["id"],)
            ).fetchall()
            shop["inventory"] = [dict(i) for i in items]
            for item in shop["inventory"]:
                try:
                    item["item_data"] = json.loads(item.get("item_data") or "{}")
                except Exception:
                    item["item_data"] = {}
                item["normalized_item"] = normalize_shop_item_row(item)
            return shop
    except Exception as e:
        logger.error("[DB] get_shop_by_prop_id error: %s", e)
        return None


def get_shop_by_id(shop_id: str) -> Optional[dict]:
    """Return a shop row with its inventory by shop_id."""
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
            if not row:
                return None
            shop = dict(row)
            shop = _parse_shop_sell_fields(shop)
            shop["buy_categories_json"] = _safe_json_array(shop.get("buy_categories_json"))
            shop["crafting_enabled"] = bool(int(shop.get("crafting_enabled") or 0))
            items = conn.execute(
                "SELECT * FROM shop_inventory WHERE shop_id=? ORDER BY created_at",
                (shop_id,)
            ).fetchall()
            shop["inventory"] = [dict(i) for i in items]
            for item in shop["inventory"]:
                try:
                    item["item_data"] = json.loads(item.get("item_data") or "{}")
                except Exception:
                    item["item_data"] = {}
                item["normalized_item"] = normalize_shop_item_row(item)
            return shop
    except Exception as e:
        logger.error("[DB] get_shop_by_id error: %s", e)
        return None


def upsert_shop(campaign_id: str, prop_id: str, name: str, shopkeeper_name: str,
                shop_type: str, description: str, inventory: list, taught_profession_ids: list[str] | None = None,
                crafting_enabled: bool = True, shop_sales_enabled: bool = True, player_sell_enabled: bool = True,
                buy_categories: list[str] | None = None, vendor_cash_units: int | None = 0, buy_rate_pct: int = 50,
                accepted_item_types: list[str] | None = None, buyback_enabled: bool = False,
                personality: str = "friendly", dialogue_enabled: bool = True, voice: str = "grand_narrator",
                tts_enabled: bool = False, greeting_override: str = "") -> Optional[dict]:
    """Create or replace a shop and its full inventory. Returns the shop dict."""
    import secrets as _secrets
    try:
        with get_conn() as conn:
            now = time.time()
            taught = []
            seen = set()
            for pid in (taught_profession_ids or []):
                norm = _normalize_profession_id(pid)
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                taught.append(norm)
            if not taught:
                taught = get_default_profession_ids_for_shop_type(shop_type)
            buy_categories_clean = []
            seen_cat = set()
            for cat in (buy_categories or []):
                c = str(cat or "").strip().lower()[:32]
                if not c or c in seen_cat:
                    continue
                seen_cat.add(c)
                buy_categories_clean.append(c)
            vendor_cash_units = max(0, int(vendor_cash_units)) if vendor_cash_units is not None else None
            buy_rate_pct = max(0, min(100, int(buy_rate_pct or 50)))
            valid_types = {"weapon", "armour", "consumable", "tool", "material", "trinket", "magic", "misc"}
            accepted_clean: list[str] = []
            seen_types: set[str] = set()
            for raw in (accepted_item_types or list(valid_types)):
                item_type = str(raw or "").strip().lower()[:40]
                if not item_type or item_type not in valid_types or item_type in seen_types:
                    continue
                seen_types.add(item_type)
                accepted_clean.append(item_type)
            if not accepted_clean:
                accepted_clean = list(valid_types)
            personality = str(personality or "friendly").strip().lower()[:40]
            if personality not in {"friendly", "gruff", "greedy", "shifty", "scholarly"}:
                personality = "friendly"
            voice = str(voice or "grand_narrator").strip()[:80] or "grand_narrator"
            greeting_override = str(greeting_override or "").strip()[:220]
            row = conn.execute(
                "SELECT id FROM shops WHERE campaign_id=? AND prop_id=?",
                (campaign_id, prop_id)
            ).fetchone()
            if row:
                shop_id = row["id"]
                conn.execute("""
                    UPDATE shops SET name=?, shopkeeper_name=?, shop_type=?, taught_professions_json=?, crafting_enabled=?,
                    selling_enabled=?, shop_sales_enabled=?, player_sell_enabled=?, buy_categories_json=?,
                    vendor_cash_units=?, buy_rate_pct=?, accepted_item_types_json=?, buyback_enabled=?, personality=?, dialogue_enabled=?, voice=?, tts_enabled=?, greeting_override=?, description=?, is_open=1
                    WHERE id=?
                """, (
                    name, shopkeeper_name, shop_type, json.dumps(taught), 1 if crafting_enabled else 0,
                    1 if player_sell_enabled else 0, 1 if shop_sales_enabled else 0, 1 if player_sell_enabled else 0, json.dumps(buy_categories_clean),
                    vendor_cash_units, buy_rate_pct, json.dumps(accepted_clean), 1 if buyback_enabled else 0,
                    personality, 1 if dialogue_enabled else 0, voice, 1 if tts_enabled else 0, greeting_override, description, shop_id
                ))
            else:
                shop_id = _secrets.token_hex(8)
                conn.execute("""
                    INSERT INTO shops (
                        id, campaign_id, prop_id, name, shopkeeper_name, shop_type, taught_professions_json,
                        crafting_enabled, selling_enabled, shop_sales_enabled, player_sell_enabled,
                        buy_categories_json, vendor_cash_units, buy_rate_pct, accepted_item_types_json,
                        buyback_enabled, personality, dialogue_enabled, voice, tts_enabled, greeting_override, description, is_open, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (
                    shop_id, campaign_id, prop_id, name, shopkeeper_name, shop_type, json.dumps(taught),
                    1 if crafting_enabled else 0, 1 if player_sell_enabled else 0, 1 if shop_sales_enabled else 0, 1 if player_sell_enabled else 0,
                    json.dumps(buy_categories_clean), vendor_cash_units, buy_rate_pct, json.dumps(accepted_clean),
                    1 if buyback_enabled else 0, personality, 1 if dialogue_enabled else 0, voice, 1 if tts_enabled else 0, greeting_override, description, now
                ))
            # Replace inventory
            conn.execute("DELETE FROM shop_inventory WHERE shop_id=?", (shop_id,))
            for item in (inventory or []):
                item_id = _secrets.token_hex(8)
                conn.execute("""
                    INSERT INTO shop_inventory
                    (id, shop_id, item_name, item_type, description, price_gp, price_sp, price_cp, quantity, item_data, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    item_id, shop_id,
                    str(item.get("item_name") or "Item")[:80],
                    str(item.get("item_type") or "misc")[:40],
                    str(item.get("description") or "")[:500],
                    int(item.get("price_gp") or 0),
                    int(item.get("price_sp") or 0),
                    int(item.get("price_cp") or 0),
                    item.get("quantity"),  # nullable
                    json.dumps(item.get("item_data") or {}),
                    now,
                ))
        return get_shop_by_id(shop_id)
    except Exception as e:
        logger.error("[DB] upsert_shop error: %s", e)
        return None


def decrement_shop_item(item_id: str, quantity: int) -> bool:
    """Decrement shop item quantity by qty; remove if reaches 0. Returns True on success."""
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT quantity FROM shop_inventory WHERE id=?", (item_id,)).fetchone()
            if not row:
                return False
            current_qty = row["quantity"]
            if current_qty is None:
                return True  # infinite stock
            new_qty = current_qty - quantity
            if new_qty <= 0:
                conn.execute("DELETE FROM shop_inventory WHERE id=?", (item_id,))
            else:
                conn.execute("UPDATE shop_inventory SET quantity=? WHERE id=?", (new_qty, item_id))
        return True
    except Exception as e:
        logger.error("[DB] decrement_shop_item error: %s", e)
        return False


def restock_shop_item(item_id: str, quantity: int) -> bool:
    """Set a shop item quantity back to given value."""
    try:
        with get_conn() as conn:
            conn.execute("UPDATE shop_inventory SET quantity=? WHERE id=?", (quantity, item_id))
        return True
    except Exception as e:
        logger.error("[DB] restock_shop_item error: %s", e)
        return False


def record_shop_transaction(shop_id: str, buyer_user_id: str, item_id: str,
                             quantity: int, price_paid_gp: int) -> bool:
    import secrets as _secrets
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO shop_transactions (id, shop_id, buyer_user_id, item_id, quantity, price_paid_gp, sold_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (_secrets.token_hex(8), shop_id, buyer_user_id, item_id, quantity, price_paid_gp, time.time()))
        return True
    except Exception as e:
        logger.error("[DB] record_shop_transaction error: %s", e)
        return False


def record_shop_sell_transaction(shop_id: str, seller_user_id: str, item_name: str,
                                  quantity: int, price_paid_gp: int) -> bool:
    """Record a player-to-shop sell transaction."""
    import secrets as _secrets
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO shop_transactions (id, shop_id, buyer_user_id, item_id, quantity, price_paid_gp, sold_at, direction)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'sell')
            """, (_secrets.token_hex(8), shop_id, seller_user_id,
                  str(item_name or "")[:80], quantity, price_paid_gp, time.time()))
        return True
    except Exception as e:
        logger.error("[DB] record_shop_sell_transaction error: %s", e)
        return False


def update_vendor_cash(shop_id: str, new_cash_units: int) -> bool:
    """Update the vendor's cash on hand (reduces after a purchase from player)."""
    try:
        with get_conn() as conn:
            conn.execute("UPDATE shops SET vendor_cash_units=? WHERE id=?",
                         (max(0, int(new_cash_units)), shop_id))
        return True
    except Exception as e:
        logger.error("[DB] update_vendor_cash error: %s", e)
        return False


def get_shop_transactions_for_campaign(campaign_id: str) -> list:
    """Return all transactions for a campaign with shop and item names joined."""
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT t.*, s.name as shop_name, i.item_name
                FROM shop_transactions t
                JOIN shops s ON t.shop_id = s.id
                JOIN shop_inventory i ON t.item_id = i.id
                WHERE s.campaign_id = ?
                ORDER BY t.sold_at DESC
                LIMIT 200
            """, (campaign_id,)).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[DB] get_shop_transactions_for_campaign error: %s", e)
        return []


def get_all_shops_for_campaign(campaign_id: str) -> list:
    """Return all shops with their inventory for a campaign."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM shops WHERE campaign_id=? ORDER BY created_at",
                (campaign_id,)
            ).fetchall()
            shops = []
            for row in rows:
                shop = dict(row)
                shop["taught_professions_json"] = _safe_json_array(shop.get("taught_professions_json"))
                items = conn.execute(
                    "SELECT * FROM shop_inventory WHERE shop_id=? ORDER BY created_at",
                    (shop["id"],)
                ).fetchall()
                shop["inventory"] = [dict(i) for i in items]
                for item in shop["inventory"]:
                    try:
                        item["item_data"] = json.loads(item.get("item_data") or "{}")
                    except Exception:
                        item["item_data"] = {}
                    item["normalized_item"] = normalize_shop_item_row(item)
                shops.append(shop)
            return shops
    except Exception as e:
        logger.error("[DB] get_all_shops_for_campaign error: %s", e)
        return []


# ─── Creature Library DB helpers ──────────────────────────────────────────────

CREATURE_JSON_ARRAY_FIELDS = (
    "attacks", "abilities", "traits_json", "actions_json", "bonus_actions_json",
    "reactions_json", "legendary_actions_json", "equipment_json", "languages_json",
    "damage_resistances_json", "damage_immunities_json", "condition_immunities_json",
    "vulnerabilities_json", "tags", "tags_json", "environment_tags_json", "role_tags_json",
)
CREATURE_JSON_OBJECT_FIELDS = (
    "speed_json", "ability_scores_json", "saving_throws_json", "skills_json",
    "senses_json", "spellcasting_json",
)


def _safe_json_loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, type(fallback)) else fallback
    except Exception:
        return fallback


def _slugify(text: str) -> str:
    value = ''.join(ch.lower() if ch.isalnum() else '-' for ch in str(text or '').strip())
    while '--' in value:
        value = value.replace('--', '-')
    return value.strip('-')[:120] or 'creature'


def _cr_to_float(cr_str: str) -> float:
    if not cr_str:
        return 0.0
    cr_str = str(cr_str).strip()
    if '/' in cr_str:
        parts = cr_str.split('/')
        try:
            return float(parts[0]) / float(parts[1])
        except Exception:
            return 0.0
    try:
        return float(cr_str)
    except Exception:
        return 0.0


def _normalize_speed(value: Any) -> tuple[str, dict[str, str]]:
    if isinstance(value, dict):
        obj = {str(k): str(v) for k, v in value.items() if v not in (None, '')}
        label = ', '.join(f"{k} {v}" for k, v in obj.items()) or '30 ft.'
        return label[:200], obj
    text = str(value or '30 ft.').strip()[:200] or '30 ft.'
    obj = {'walk': text}
    for part in [p.strip() for p in text.split(',') if p.strip()]:
        bits = part.split(' ', 1)
        if len(bits) == 2 and bits[0].isalpha():
            obj[bits[0].lower()] = bits[1]
    return text, obj


def _normalize_creature_payload(data: dict, existing: Optional[dict] = None) -> dict:
    existing = existing or {}
    entry_type = str(data.get('entry_type') or data.get('creature_type') or existing.get('entry_type') or existing.get('creature_type') or 'monster').lower()
    creature_type = entry_type if entry_type in ('monster', 'npc') else 'monster'
    source_type = str(data.get('source') or data.get('source_type') or existing.get('source') or existing.get('source_type') or 'custom').lower()
    source_label = str(data.get('source_label') or existing.get('source_label') or (source_type.upper() if source_type else '')).strip()[:120]
    speed_label, speed_obj = _normalize_speed(data.get('speed_json') or data.get('speed') or existing.get('speed_json') or existing.get('speed') or '30 ft.')
    abilities = data.get('abilities', existing.get('abilities') or [])
    attacks = data.get('attacks', existing.get('attacks') or [])
    traits = data.get('traits_json')
    if traits is None:
        traits = data.get('traits')
    if traits is None:
        traits = existing.get('traits_json') or abilities or []
    actions = data.get('actions_json')
    if actions is None:
        actions = data.get('actions')
    if actions is None:
        actions = existing.get('actions_json') or attacks or []
    tags = [str(v).strip() for v in (data.get('tags_json') or data.get('tags') or existing.get('tags_json') or existing.get('tags') or []) if str(v).strip()]
    role_tags = [str(v).strip() for v in (data.get('role_tags_json') or data.get('role_tags') or existing.get('role_tags_json') or []) if str(v).strip()]
    env_tags = [str(v).strip() for v in (data.get('environment_tags_json') or data.get('environment_tags') or existing.get('environment_tags_json') or []) if str(v).strip()]
    hp_value = int(data.get('hp') or data.get('hit_points') or existing.get('hp') or existing.get('hit_points') or 1)
    ability_scores = {
        'str': int(data.get('str_score', existing.get('str_score', 10)) or 10),
        'dex': int(data.get('dex_score', existing.get('dex_score', 10)) or 10),
        'con': int(data.get('con_score', existing.get('con_score', 10)) or 10),
        'int': int(data.get('int_score', existing.get('int_score', 10)) or 10),
        'wis': int(data.get('wis_score', existing.get('wis_score', 10)) or 10),
        'cha': int(data.get('cha_score', existing.get('cha_score', 10)) or 10),
    }
    if isinstance(data.get('ability_scores_json'), dict):
        ability_scores.update({k: int(v or 10) for k, v in data.get('ability_scores_json', {}).items() if k in ability_scores})
    normalized = {
        'name': str(data.get('name', existing.get('name', 'Unnamed'))).strip()[:200] or 'Unnamed',
        'slug': _slugify(data.get('slug') or data.get('name') or existing.get('slug') or existing.get('name') or 'creature'),
        'entry_type': creature_type,
        'creature_type': creature_type,
        'monster_type': str(data.get('monster_type', existing.get('monster_type', '')) or '').strip()[:60],
        'subtype': str(data.get('subtype', existing.get('subtype', '')) or '').strip()[:80],
        'size': str(data.get('size', existing.get('size', 'Medium')) or 'Medium').strip()[:30],
        'alignment': str(data.get('alignment', existing.get('alignment', '')) or '').strip()[:60],
        'cr': str(data.get('cr', existing.get('cr', '0')) or '0')[:12],
        'xp': int(data.get('xp', existing.get('xp', 0)) or 0),
        'proficiency_bonus': int(data.get('proficiency_bonus', existing.get('proficiency_bonus', 2)) or 2),
        'hp': hp_value,
        'hit_points': hp_value,
        'hit_dice': str(data.get('hit_dice', existing.get('hit_dice', '')) or '')[:80],
        'ac': int(data.get('ac', existing.get('ac', 10)) or 10),
        'speed': speed_label,
        'speed_json': speed_obj,
        'ability_scores_json': ability_scores,
        'saving_throws_json': data.get('saving_throws_json') or existing.get('saving_throws_json') or {},
        'skills_json': data.get('skills_json') or existing.get('skills_json') or {},
        'senses_json': data.get('senses_json') or existing.get('senses_json') or {},
        'languages_json': data.get('languages_json') or existing.get('languages_json') or [],
        'damage_resistances_json': data.get('damage_resistances_json') or existing.get('damage_resistances_json') or [],
        'damage_immunities_json': data.get('damage_immunities_json') or existing.get('damage_immunities_json') or [],
        'condition_immunities_json': data.get('condition_immunities_json') or existing.get('condition_immunities_json') or [],
        'vulnerabilities_json': data.get('vulnerabilities_json') or existing.get('vulnerabilities_json') or [],
        'attacks': attacks,
        'abilities': abilities,
        'traits_json': traits if isinstance(traits, list) else [],
        'actions_json': actions if isinstance(actions, list) else [],
        'bonus_actions_json': data.get('bonus_actions_json') or existing.get('bonus_actions_json') or [],
        'reactions_json': data.get('reactions_json') or existing.get('reactions_json') or [],
        'legendary_actions_json': data.get('legendary_actions_json') or existing.get('legendary_actions_json') or [],
        'spellcasting_json': data.get('spellcasting_json') or existing.get('spellcasting_json') or {},
        'equipment_json': data.get('equipment_json') or existing.get('equipment_json') or [],
        'portrait_url': str(data.get('portrait_url', existing.get('portrait_url', '')) or '')[:300] or None,
        'token_url': str(data.get('token_url', existing.get('token_url', '')) or '')[:300] or None,
        'asset_path': str(data.get('asset_path', existing.get('asset_path', '')) or '')[:300] or None,
        'backstory': str(data.get('backstory', existing.get('backstory', '')) or '')[:5000],
        'personality': str(data.get('personality', existing.get('personality', '')) or '')[:2500],
        'voice_style': str(data.get('voice_style', existing.get('voice_style', '')) or '')[:100],
        'notes': str(data.get('notes', existing.get('notes', '')) or '')[:5000],
        'tags': tags,
        'tags_json': tags,
        'environment_tags_json': env_tags,
        'role_tags_json': role_tags,
        'source': source_type,
        'source_type': source_type,
        'source_label': source_label,
        'srd_id': data.get('srd_id') or existing.get('srd_id'),
        'token_size': max(1, min(4, int(data.get('token_size', existing.get('token_size', 1)) or 1))),
        'is_favorite': 1 if data.get('is_favorite', existing.get('is_favorite', 0)) else 0,
        'is_pinned': 1 if data.get('is_pinned', existing.get('is_pinned', 0)) else 0,
        'is_public': 1 if data.get('is_public', existing.get('is_public', 0)) else 0,
        'last_used_at': data.get('last_used_at', existing.get('last_used_at')),
        'use_count': int(data.get('use_count', existing.get('use_count', 0)) or 0),
        'archived': 1 if data.get('archived', existing.get('archived', 0)) else 0,
        'seed_key': str(data.get('seed_key', existing.get('seed_key', '')) or '')[:160] or None,
    }
    normalized.update({
        'str_score': ability_scores['str'], 'dex_score': ability_scores['dex'], 'con_score': ability_scores['con'],
        'int_score': ability_scores['int'], 'wis_score': ability_scores['wis'], 'cha_score': ability_scores['cha'],
    })
    return normalized


def _parse_creature_row(row: dict) -> dict:
    for field in CREATURE_JSON_ARRAY_FIELDS:
        row[field] = _safe_json_loads(row.get(field) or '[]', [])
    for field in CREATURE_JSON_OBJECT_FIELDS:
        row[field] = _safe_json_loads(row.get(field) or '{}', {})
    row['entry_type'] = row.get('entry_type') or row.get('creature_type') or 'monster'
    row['source_type'] = row.get('source_type') or row.get('source') or 'custom'
    row['source_label'] = row.get('source_label') or (str(row.get('source_type') or '').upper())
    row['hp'] = int(row.get('hit_points') or row.get('hp') or 1)
    row['hit_points'] = row['hp']
    row['ability_scores_json'] = row.get('ability_scores_json') or {
        'str': int(row.get('str_score') or 10), 'dex': int(row.get('dex_score') or 10), 'con': int(row.get('con_score') or 10),
        'int': int(row.get('int_score') or 10), 'wis': int(row.get('wis_score') or 10), 'cha': int(row.get('cha_score') or 10),
    }
    row['actions'] = row.get('actions_json') or row.get('attacks') or []
    row['traits'] = row.get('traits_json') or row.get('abilities') or []
    row['languages'] = row.get('languages_json') or []
    row['environment_tags'] = row.get('environment_tags_json') or []
    row['role_tags'] = row.get('role_tags_json') or []
    return row


def _seed_entry_payload(seed: dict, owner_user_id: str, source_type: str) -> dict:
    return _normalize_creature_payload({
        **seed,
        'entry_type': seed.get('creature_type', 'monster'),
        'source': source_type,
        'source_type': source_type,
        'source_label': 'SRD 5.1' if source_type == 'srd' else seed.get('source_label') or 'Built-in',
        'seed_key': f"{source_type}:{seed.get('srd_id') or _slugify(seed.get('name'))}",
    })


def _upsert_seed_creatures(owner_user_id: str, seeds: list[dict], source_type: str) -> None:
    import secrets as _secrets
    now = time.time()
    with get_conn() as conn:
        for seed in seeds:
            payload = _seed_entry_payload(seed, owner_user_id, source_type)
            existing = conn.execute(
                "SELECT * FROM user_creature_library WHERE owner_user_id=? AND seed_key=?",
                (owner_user_id, payload['seed_key'])
            ).fetchone()
            if existing and str(existing['source']) in ('custom', 'variant', 'imported'):
                continue
            row_id = dict(existing)['id'] if existing else _secrets.token_hex(8)
            conn.execute("""
                INSERT INTO user_creature_library (
                    id, owner_user_id, entry_type, name, slug, creature_type, subtype, size, alignment, cr, xp, proficiency_bonus,
                    hp, hit_points, hit_dice, ac, speed, speed_json, str_score, dex_score, con_score, int_score, wis_score, cha_score,
                    ability_scores_json, saving_throws_json, skills_json, senses_json, languages_json, damage_resistances_json,
                    damage_immunities_json, condition_immunities_json, vulnerabilities_json, attacks, abilities, traits_json, actions_json,
                    bonus_actions_json, reactions_json, legendary_actions_json, spellcasting_json, equipment_json, portrait_url, token_url,
                    asset_path, backstory, personality, voice_style, notes, tags, tags_json, environment_tags_json, role_tags_json, source,
                    source_type, source_label, srd_id, monster_type, token_size, is_favorite, is_pinned, is_public, last_used_at, use_count,
                    archived, deleted, seed_key, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, slug=excluded.slug, entry_type=excluded.entry_type, creature_type=excluded.creature_type, subtype=excluded.subtype,
                    size=excluded.size, alignment=excluded.alignment, cr=excluded.cr, xp=excluded.xp, proficiency_bonus=excluded.proficiency_bonus,
                    hp=excluded.hp, hit_points=excluded.hit_points, hit_dice=excluded.hit_dice, ac=excluded.ac, speed=excluded.speed, speed_json=excluded.speed_json,
                    str_score=excluded.str_score, dex_score=excluded.dex_score, con_score=excluded.con_score, int_score=excluded.int_score, wis_score=excluded.wis_score, cha_score=excluded.cha_score,
                    ability_scores_json=excluded.ability_scores_json, saving_throws_json=excluded.saving_throws_json, skills_json=excluded.skills_json, senses_json=excluded.senses_json,
                    languages_json=excluded.languages_json, damage_resistances_json=excluded.damage_resistances_json, damage_immunities_json=excluded.damage_immunities_json,
                    condition_immunities_json=excluded.condition_immunities_json, vulnerabilities_json=excluded.vulnerabilities_json, attacks=excluded.attacks, abilities=excluded.abilities,
                    traits_json=excluded.traits_json, actions_json=excluded.actions_json, bonus_actions_json=excluded.bonus_actions_json, reactions_json=excluded.reactions_json,
                    legendary_actions_json=excluded.legendary_actions_json, spellcasting_json=excluded.spellcasting_json, equipment_json=excluded.equipment_json, portrait_url=excluded.portrait_url,
                    token_url=excluded.token_url, asset_path=excluded.asset_path, backstory=excluded.backstory, personality=excluded.personality, voice_style=excluded.voice_style, notes=excluded.notes,
                    tags=excluded.tags, tags_json=excluded.tags_json, environment_tags_json=excluded.environment_tags_json, role_tags_json=excluded.role_tags_json, source=excluded.source,
                    source_type=excluded.source_type, source_label=excluded.source_label, srd_id=excluded.srd_id, monster_type=excluded.monster_type, token_size=excluded.token_size,
                    archived=0, deleted=0, updated_at=excluded.updated_at
            """, (
                row_id, owner_user_id, payload['entry_type'], payload['name'], payload['slug'], payload['creature_type'], payload['subtype'], payload['size'], payload['alignment'],
                payload['cr'], payload['xp'], payload['proficiency_bonus'], payload['hp'], payload['hit_points'], payload['hit_dice'], payload['ac'], payload['speed'],
                _json_dumps_compact(payload['speed_json']), payload['str_score'], payload['dex_score'], payload['con_score'], payload['int_score'], payload['wis_score'], payload['cha_score'],
                _json_dumps_compact(payload['ability_scores_json']), _json_dumps_compact(payload['saving_throws_json']), _json_dumps_compact(payload['skills_json']),
                _json_dumps_compact(payload['senses_json']), _json_dumps_compact(payload['languages_json']), _json_dumps_compact(payload['damage_resistances_json']),
                _json_dumps_compact(payload['damage_immunities_json']), _json_dumps_compact(payload['condition_immunities_json']), _json_dumps_compact(payload['vulnerabilities_json']),
                _json_dumps_compact(payload['attacks']), _json_dumps_compact(payload['abilities']), _json_dumps_compact(payload['traits_json']), _json_dumps_compact(payload['actions_json']),
                _json_dumps_compact(payload['bonus_actions_json']), _json_dumps_compact(payload['reactions_json']), _json_dumps_compact(payload['legendary_actions_json']),
                _json_dumps_compact(payload['spellcasting_json']), _json_dumps_compact(payload['equipment_json']), payload['portrait_url'], payload['token_url'], payload['asset_path'],
                payload['backstory'], payload['personality'], payload['voice_style'], payload['notes'], _json_dumps_compact(payload['tags']), _json_dumps_compact(payload['tags_json']),
                _json_dumps_compact(payload['environment_tags_json']), _json_dumps_compact(payload['role_tags_json']), payload['source'], payload['source_type'], payload['source_label'],
                payload['srd_id'], payload['monster_type'], payload['token_size'], 0, 0, 0, None, 0, 0, 0, payload['seed_key'], now, now
            ))


def seed_srd_for_user(owner_user_id: str) -> None:
    from server.srd_bestiary import get_srd_monsters
    try:
        _upsert_seed_creatures(owner_user_id, get_srd_monsters(), 'srd')
    except Exception as e:
        logger.error("[DB] seed_srd_for_user error: %s", e)


def seed_srd_npcs_for_user(owner_user_id: str) -> None:
    from server.srd_npcs import SRD_NPCS
    try:
        _upsert_seed_creatures(owner_user_id, SRD_NPCS, 'builtin')
    except Exception as e:
        logger.error("[DB] seed_srd_npcs_for_user error: %s", e)


def get_creatures(
    owner_user_id: str,
    creature_type: Optional[str] = None,
    cr_min: Optional[str] = None,
    cr_max: Optional[str] = None,
    search: Optional[str] = None,
    source: Optional[str] = None,
    monster_type: Optional[str] = None,
    environment: Optional[str] = None,
    role_tag: Optional[str] = None,
    favorites_only: bool = False,
    recent_only: bool = False,
    custom_mode: Optional[str] = None,
    sort: Optional[str] = None,
    archived: bool = False,
) -> list:
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM user_creature_library WHERE owner_user_id=? AND deleted=0 AND archived=?",
                (owner_user_id, 1 if archived else 0)
            ).fetchall()
        results = []
        cr_min_f = _cr_to_float(cr_min) if cr_min is not None else None
        cr_max_f = _cr_to_float(cr_max) if cr_max is not None else None
        search_lower = search.lower().strip() if search else ''
        active_source = (source or '').lower()
        for r in rows:
            row = _parse_creature_row(dict(r))
            if creature_type and row.get('entry_type') != creature_type and row.get('creature_type') != creature_type:
                continue
            if active_source:
                if active_source == 'custom_only' and row.get('source_type') not in ('custom', 'variant', 'imported'):
                    continue
                elif active_source == 'builtin_only' and row.get('source_type') in ('custom', 'variant', 'imported'):
                    continue
                elif row.get('source_type') != active_source and row.get('source') != active_source:
                    continue
            if custom_mode == 'custom_only' and row.get('source_type') not in ('custom', 'variant', 'imported'):
                continue
            if custom_mode == 'builtin_only' and row.get('source_type') in ('custom', 'variant', 'imported'):
                continue
            if monster_type and row.get('monster_type', '').lower() != monster_type.lower():
                continue
            if environment and environment.lower() not in ' '.join(row.get('environment_tags_json') or []).lower():
                continue
            if role_tag and role_tag.lower() not in ' '.join(row.get('role_tags_json') or []).lower():
                continue
            if favorites_only and not row.get('is_favorite'):
                continue
            if recent_only and not row.get('last_used_at'):
                continue
            cr_f = _cr_to_float(row.get('cr', '0'))
            if cr_min_f is not None and cr_f < cr_min_f:
                continue
            if cr_max_f is not None and cr_f > cr_max_f:
                continue
            if search_lower:
                haystack = ' '.join([
                    str(row.get('name', '')), str(row.get('monster_type', '')), str(row.get('subtype', '')),
                    ' '.join(row.get('tags_json') or []), ' '.join(row.get('environment_tags_json') or []),
                    ' '.join(row.get('role_tags_json') or []), row.get('source_label', '')
                ]).lower()
                if search_lower not in haystack:
                    continue
            score = 0
            name_lower = str(row.get('name', '')).lower()
            if search_lower:
                if name_lower == search_lower:
                    score += 80
                elif name_lower.startswith(search_lower):
                    score += 40
                elif search_lower in name_lower:
                    score += 20
            score += 15 if row.get('is_pinned') else 0
            score += 10 if row.get('is_favorite') else 0
            score += min(int(row.get('use_count') or 0), 20)
            if row.get('last_used_at'):
                score += 8
            if cr_min_f is not None or cr_max_f is not None:
                target = cr_min_f if cr_min_f is not None else cr_max_f
                score += max(0, 8 - abs(cr_f - float(target or 0)))
            row['_score'] = score
            row['_cr_float'] = cr_f
            results.append(row)
        if sort == 'alpha':
            results.sort(key=lambda row: ((0 if row.get('is_pinned') else 1), str(row.get('name', '')).lower()))
        elif sort == 'cr':
            results.sort(key=lambda row: ((0 if row.get('is_pinned') else 1), row.get('_cr_float', 0), str(row.get('name', '')).lower()))
        elif sort == 'recent':
            results.sort(key=lambda row: (-(row.get('last_used_at') or 0), -(row.get('is_pinned') or 0), str(row.get('name', '')).lower()))
        elif sort == 'most_used':
            results.sort(key=lambda row: (-(row.get('use_count') or 0), -(row.get('is_pinned') or 0), str(row.get('name', '')).lower()))
        else:
            results.sort(key=lambda row: (-row.get('_score', 0), (0 if row.get('is_pinned') else 1), str(row.get('name', '')).lower()))
        for row in results:
            row.pop('_score', None)
            row.pop('_cr_float', None)
        return results
    except Exception as e:
        logger.error("[DB] get_creatures error: %s", e)
        return []


def get_creature(creature_id: str, owner_user_id: str) -> Optional[dict]:
    """Return a single creature by ID (verifying ownership)."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM user_creature_library WHERE id=? AND owner_user_id=? AND deleted=0",
                (creature_id, owner_user_id)
            ).fetchone()
            if row:
                return _parse_creature_row(dict(row))
            return None
    except Exception as e:
        logger.error("[DB] get_creature error: %s", e)
        return None




def get_creature_any(creature_id: str) -> Optional[dict]:
    """Return a creature by ID regardless of owner."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM user_creature_library WHERE id=? AND deleted=0",
                (creature_id,)
            ).fetchone()
            return _parse_creature_row(dict(row)) if row else None
    except Exception as e:
        logger.error("[DB] get_creature_any error: %s", e)
        return None


def is_creature_owned_by_user(creature: Optional[dict], owner_user_id: str) -> bool:
    if not creature:
        return False
    return str(creature.get('owner_user_id') or '').strip() == str(owner_user_id or '').strip()


def create_creature_variant(owner_user_id: str, original: dict, edits: Optional[dict] = None, *, variant_name: Optional[str] = None) -> Optional[dict]:
    """Clone a creature into the requesting user's owned library and apply edits."""
    if not original:
        return None
    payload = dict(original)
    for key in ('id', 'created_at', 'updated_at', 'owner_user_id', 'deleted'):
        payload.pop(key, None)
    payload['name'] = str(variant_name or (edits or {}).get('name') or payload.get('name') or 'Creature').strip()[:200]
    payload['source'] = 'variant'
    payload['source_type'] = 'variant'
    payload['source_label'] = 'Custom Variant'
    payload['seed_key'] = None
    payload['is_favorite'] = 0
    payload['is_pinned'] = 0
    payload['archived'] = 0
    if edits:
        payload.update(edits)
    return create_creature(owner_user_id, payload)


def save_creature_edits(creature_id: str, owner_user_id: str, data: dict) -> tuple[Optional[dict], str]:
    """Save creature edits, cloning read-only/non-owned entries into a new owned variant when needed."""
    original = get_creature_any(creature_id)
    if not original:
        return None, 'missing'
    source_type = str(original.get('source_type') or original.get('source') or '').lower()
    is_owned = is_creature_owned_by_user(original, owner_user_id)
    is_read_only = source_type in {'srd', 'builtin', 'system', 'shared'}
    if is_owned and not is_read_only:
        return update_creature(creature_id, owner_user_id, data), 'updated'
    variant_name = str((data or {}).get('name') or '').strip() or f"{original.get('name') or 'Creature'} (Variant)"
    return create_creature_variant(owner_user_id, original, data, variant_name=variant_name), 'created_variant'

def create_creature(owner_user_id: str, data: dict) -> Optional[dict]:
    """Insert a new creature and return it."""
    import secrets as _secrets
    try:
        now = time.time()
        cid = _secrets.token_hex(8)
        payload = _normalize_creature_payload(data)
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO user_creature_library (
                    id, owner_user_id, entry_type, name, slug, creature_type, subtype, size, alignment, cr, xp, proficiency_bonus,
                    hp, hit_points, hit_dice, ac, speed, speed_json, str_score, dex_score, con_score, int_score, wis_score, cha_score,
                    ability_scores_json, saving_throws_json, skills_json, senses_json, languages_json, damage_resistances_json,
                    damage_immunities_json, condition_immunities_json, vulnerabilities_json, attacks, abilities, traits_json, actions_json,
                    bonus_actions_json, reactions_json, legendary_actions_json, spellcasting_json, equipment_json, portrait_url, token_url,
                    asset_path, backstory, personality, voice_style, notes, tags, tags_json, environment_tags_json, role_tags_json, source,
                    source_type, source_label, srd_id, monster_type, token_size, is_favorite, is_pinned, is_public, last_used_at, use_count,
                    archived, deleted, seed_key, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                cid, owner_user_id, payload['entry_type'], payload['name'], payload['slug'], payload['creature_type'], payload['subtype'], payload['size'], payload['alignment'],
                payload['cr'], payload['xp'], payload['proficiency_bonus'], payload['hp'], payload['hit_points'], payload['hit_dice'], payload['ac'], payload['speed'],
                _json_dumps_compact(payload['speed_json']), payload['str_score'], payload['dex_score'], payload['con_score'], payload['int_score'], payload['wis_score'], payload['cha_score'],
                _json_dumps_compact(payload['ability_scores_json']), _json_dumps_compact(payload['saving_throws_json']), _json_dumps_compact(payload['skills_json']), _json_dumps_compact(payload['senses_json']),
                _json_dumps_compact(payload['languages_json']), _json_dumps_compact(payload['damage_resistances_json']), _json_dumps_compact(payload['damage_immunities_json']),
                _json_dumps_compact(payload['condition_immunities_json']), _json_dumps_compact(payload['vulnerabilities_json']), _json_dumps_compact(payload['attacks']), _json_dumps_compact(payload['abilities']),
                _json_dumps_compact(payload['traits_json']), _json_dumps_compact(payload['actions_json']), _json_dumps_compact(payload['bonus_actions_json']), _json_dumps_compact(payload['reactions_json']),
                _json_dumps_compact(payload['legendary_actions_json']), _json_dumps_compact(payload['spellcasting_json']), _json_dumps_compact(payload['equipment_json']), payload['portrait_url'], payload['token_url'],
                payload['asset_path'], payload['backstory'], payload['personality'], payload['voice_style'], payload['notes'], _json_dumps_compact(payload['tags']), _json_dumps_compact(payload['tags_json']),
                _json_dumps_compact(payload['environment_tags_json']), _json_dumps_compact(payload['role_tags_json']), payload['source'], payload['source_type'], payload['source_label'], payload['srd_id'],
                payload['monster_type'], payload['token_size'], payload['is_favorite'], payload['is_pinned'], payload['is_public'], payload['last_used_at'], payload['use_count'], payload['archived'], 0,
                payload['seed_key'], now, now,
            ))
        return get_creature(cid, owner_user_id)
    except Exception as e:
        logger.error("[DB] create_creature error: %s", e)
        return None


def update_creature(creature_id: str, owner_user_id: str, data: dict) -> Optional[dict]:
    """Update a creature's fields (owner must match)."""
    try:
        now = time.time()
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM user_creature_library WHERE id=? AND owner_user_id=? AND deleted=0",
                (creature_id, owner_user_id)
            ).fetchone()
            if not existing:
                return None
            existing = _parse_creature_row(dict(existing))
            payload = _normalize_creature_payload(data, existing)
            conn.execute("""
                UPDATE user_creature_library SET
                    entry_type=?, name=?, slug=?, creature_type=?, subtype=?, size=?, alignment=?, cr=?, xp=?, proficiency_bonus=?,
                    hp=?, hit_points=?, hit_dice=?, ac=?, speed=?, speed_json=?, str_score=?, dex_score=?, con_score=?, int_score=?, wis_score=?, cha_score=?,
                    ability_scores_json=?, saving_throws_json=?, skills_json=?, senses_json=?, languages_json=?, damage_resistances_json=?, damage_immunities_json=?,
                    condition_immunities_json=?, vulnerabilities_json=?, attacks=?, abilities=?, traits_json=?, actions_json=?, bonus_actions_json=?, reactions_json=?, legendary_actions_json=?,
                    spellcasting_json=?, equipment_json=?, portrait_url=?, token_url=?, asset_path=?, backstory=?, personality=?, voice_style=?, notes=?, tags=?, tags_json=?,
                    environment_tags_json=?, role_tags_json=?, source=?, source_type=?, source_label=?, srd_id=?, monster_type=?, token_size=?, is_favorite=?, is_pinned=?, is_public=?,
                    last_used_at=?, use_count=?, archived=?, seed_key=?, updated_at=?
                WHERE id=? AND owner_user_id=?
            """, (
                payload['entry_type'], payload['name'], payload['slug'], payload['creature_type'], payload['subtype'], payload['size'], payload['alignment'], payload['cr'], payload['xp'], payload['proficiency_bonus'],
                payload['hp'], payload['hit_points'], payload['hit_dice'], payload['ac'], payload['speed'], _json_dumps_compact(payload['speed_json']), payload['str_score'], payload['dex_score'], payload['con_score'], payload['int_score'], payload['wis_score'], payload['cha_score'],
                _json_dumps_compact(payload['ability_scores_json']), _json_dumps_compact(payload['saving_throws_json']), _json_dumps_compact(payload['skills_json']), _json_dumps_compact(payload['senses_json']), _json_dumps_compact(payload['languages_json']),
                _json_dumps_compact(payload['damage_resistances_json']), _json_dumps_compact(payload['damage_immunities_json']), _json_dumps_compact(payload['condition_immunities_json']), _json_dumps_compact(payload['vulnerabilities_json']),
                _json_dumps_compact(payload['attacks']), _json_dumps_compact(payload['abilities']), _json_dumps_compact(payload['traits_json']), _json_dumps_compact(payload['actions_json']), _json_dumps_compact(payload['bonus_actions_json']), _json_dumps_compact(payload['reactions_json']), _json_dumps_compact(payload['legendary_actions_json']),
                _json_dumps_compact(payload['spellcasting_json']), _json_dumps_compact(payload['equipment_json']), payload['portrait_url'], payload['token_url'], payload['asset_path'], payload['backstory'], payload['personality'], payload['voice_style'], payload['notes'], _json_dumps_compact(payload['tags']), _json_dumps_compact(payload['tags_json']),
                _json_dumps_compact(payload['environment_tags_json']), _json_dumps_compact(payload['role_tags_json']), payload['source'], payload['source_type'], payload['source_label'], payload['srd_id'], payload['monster_type'], payload['token_size'], payload['is_favorite'], payload['is_pinned'], payload['is_public'],
                payload['last_used_at'], payload['use_count'], payload['archived'], payload['seed_key'], now, creature_id, owner_user_id,
            ))
        return get_creature(creature_id, owner_user_id)
    except Exception as e:
        logger.error("[DB] update_creature error: %s", e)
        return None


def delete_creature(creature_id: str, owner_user_id: str) -> bool:
    """Archive a creature from the active library."""
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE user_creature_library SET archived=1, deleted=1, updated_at=? WHERE id=? AND owner_user_id=?",
                (time.time(), creature_id, owner_user_id)
            )
        return True
    except Exception as e:
        logger.error("[DB] delete_creature error: %s", e)
        return False
