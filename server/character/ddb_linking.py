"""Pure helpers for persistent D&D Beyond character linking.

D&D Beyond access is intentionally treated as an optional, unofficial external
source. These helpers never require the remote service to load a saved
character; the canonical Tavern profile remains the playable source of truth.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

DDB_PROVIDER = "dndbeyond"
DDB_SYNC_OK = "ok"
DDB_SYNC_ERROR = "error"
DDB_DEFAULT_AUTO_REFRESH_MINUTES = 360

_SOURCE_IDENTITY_KEYS = {"name", "displayName", "portraitUrl", "alignment"}
_SOURCE_ROOT_KEYS = {"species", "background", "abilities", "classes", "feats"}
_IMPORT_ROW_FIELDS = ("importedActions", "importedFeatures", "importedSpells")
_RUNTIME_ROW_KEYS = {
    "qty", "quantity", "charges", "numberUsed", "usesRemaining",
    "currentUses", "consumed", "equipped", "attuned",
}
_PROFILE_PRESERVE_KEYS = (
    "curhp", "hp", "tempHp", "initiative", "ac", "speed", "passive",
    "faction", "notes", "characterNotes", "classId", "color", "accentColor",
    "diceTheme", "portraitFrame", "tagline",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ddb_auto_refresh_minutes() -> int:
    try:
        return max(30, int(str(os.environ.get("DDB_AUTO_REFRESH_MINUTES", DDB_DEFAULT_AUTO_REFRESH_MINUTES)).strip()))
    except Exception:
        return DDB_DEFAULT_AUTO_REFRESH_MINUTES


def canonical_ddb_source_url(character_id: str) -> str:
    character_id = str(character_id or "").strip()
    return f"https://www.dndbeyond.com/characters/{character_id}" if character_id else ""


def get_external_source(document_or_profile: Any) -> dict[str, Any]:
    if not isinstance(document_or_profile, dict):
        return {}
    native = document_or_profile.get("nativeCharacter")
    if isinstance(native, dict):
        document_or_profile = native
    import_meta = document_or_profile.get("importMeta")
    if not isinstance(import_meta, dict):
        return {}
    external = import_meta.get("externalSource")
    return copy.deepcopy(external) if isinstance(external, dict) else {}


def _stable_row_key(row: Any) -> str:
    if not isinstance(row, dict):
        return str(row or "").strip().lower()
    for key in ("id", "itemId", "spellId", "definitionId", "name", "displayName", "label"):
        value = str(row.get(key) or "").strip().lower()
        if value:
            return value
    try:
        return hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
    except Exception:
        return ""


def _scrub_snapshot(document: Any) -> dict[str, Any]:
    """Return a deterministic, normalized-source snapshot safe for persistence."""
    doc = copy.deepcopy(document) if isinstance(document, dict) else {}
    doc.pop("audit", None)
    import_meta = doc.get("importMeta")
    if isinstance(import_meta, dict):
        for key in ("rawSnapshot", "importedAt", "importReview", "externalSource"):
            import_meta.pop(key, None)
        doc["importMeta"] = import_meta
    for key in ("currentHP", "currentHp", "tempHP", "tempHp"):
        doc.pop(key, None)
    spell_state = doc.get("spellState")
    if isinstance(spell_state, dict):
        spell_state.pop("slots", None)
    return doc


def build_source_snapshot(document: Any) -> dict[str, Any]:
    return _scrub_snapshot(document)


def source_fingerprint(document: Any) -> str:
    snapshot = _scrub_snapshot(document)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _copy_runtime_differences(fresh_row: dict[str, Any], local_row: dict[str, Any] | None,
                              old_source_row: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(fresh_row)
    if not isinstance(local_row, dict):
        return out
    old = old_source_row if isinstance(old_source_row, dict) else {}
    for key in _RUNTIME_ROW_KEYS:
        if key not in local_row:
            continue
        if key not in old or local_row.get(key) != old.get(key):
            out[key] = copy.deepcopy(local_row.get(key))
    return out


def _merge_source_rows(local_rows: Any, old_source_rows: Any, fresh_rows: Any) -> list[Any]:
    """Three-way merge a source-owned list while retaining Tavern-local additions."""
    local_list = list(local_rows) if isinstance(local_rows, list) else []
    old_list = list(old_source_rows) if isinstance(old_source_rows, list) else []
    fresh_list = list(fresh_rows) if isinstance(fresh_rows, list) else []
    local_map = {_stable_row_key(row): row for row in local_list if _stable_row_key(row)}
    old_map = {_stable_row_key(row): row for row in old_list if _stable_row_key(row)}
    fresh_keys = {_stable_row_key(row) for row in fresh_list if _stable_row_key(row)}
    merged: list[Any] = []
    for row in fresh_list:
        key = _stable_row_key(row)
        if isinstance(row, dict):
            merged.append(_copy_runtime_differences(row, local_map.get(key) if key else None,
                                                    old_map.get(key) if key else None))
        else:
            merged.append(copy.deepcopy(row))
    for row in local_list:
        key = _stable_row_key(row)
        if not key:
            merged.append(copy.deepcopy(row))
            continue
        if key not in old_map and key not in fresh_keys:
            merged.append(copy.deepcopy(row))
            continue
        if key in old_map and key not in fresh_keys and isinstance(row, dict):
            old = old_map.get(key)
            if isinstance(old, dict) and any(
                key_name in row and row.get(key_name) != old.get(key_name) for key_name in _RUNTIME_ROW_KEYS
            ):
                retained = copy.deepcopy(row)
                retained.setdefault("tavernRetainedAfterSourceRemoval", True)
                merged.append(retained)
    return merged


def merge_refresh_document(existing_document: dict[str, Any], fresh_source_document: dict[str, Any],
                           previous_source_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply explicit DDB source fields without overwriting Tavern live state."""
    existing = copy.deepcopy(existing_document) if isinstance(existing_document, dict) else {}
    fresh = copy.deepcopy(fresh_source_document) if isinstance(fresh_source_document, dict) else {}
    old_source = copy.deepcopy(previous_source_snapshot) if isinstance(previous_source_snapshot, dict) else {}
    merged = copy.deepcopy(existing)
    merged["sourceMode"] = "dndbeyond"
    for key in _SOURCE_ROOT_KEYS:
        if key in fresh:
            merged[key] = copy.deepcopy(fresh.get(key))

    merged_identity = merged.get("identity") if isinstance(merged.get("identity"), dict) else {}
    fresh_identity = fresh.get("identity") if isinstance(fresh.get("identity"), dict) else {}
    merged_identity = copy.deepcopy(merged_identity)
    for key in _SOURCE_IDENTITY_KEYS:
        if key in fresh_identity:
            merged_identity[key] = copy.deepcopy(fresh_identity.get(key))
    merged["identity"] = merged_identity

    local_equipment = existing.get("equipment") if isinstance(existing.get("equipment"), dict) else {}
    old_equipment = old_source.get("equipment") if isinstance(old_source.get("equipment"), dict) else {}
    fresh_equipment = fresh.get("equipment") if isinstance(fresh.get("equipment"), dict) else {}
    merged_equipment = copy.deepcopy(local_equipment)
    merged_equipment["inventory"] = _merge_source_rows(
        local_equipment.get("inventory"), old_equipment.get("inventory"), fresh_equipment.get("inventory")
    )
    for key, value in fresh_equipment.items():
        if key == "inventory":
            continue
        if key not in merged_equipment:
            merged_equipment[key] = copy.deepcopy(value)
    merged["equipment"] = merged_equipment

    local_spells = existing.get("spellState") if isinstance(existing.get("spellState"), dict) else {}
    fresh_spells = fresh.get("spellState") if isinstance(fresh.get("spellState"), dict) else {}
    merged_spells = copy.deepcopy(local_spells)
    for key in ("known", "prepared", "rituals", "spellbookEntries", "classSources"):
        if key in fresh_spells:
            merged_spells[key] = copy.deepcopy(fresh_spells.get(key))
    if "slots" not in merged_spells and "slots" in fresh_spells:
        merged_spells["slots"] = copy.deepcopy(fresh_spells.get("slots"))
    if "focus" not in merged_spells and "focus" in fresh_spells:
        merged_spells["focus"] = copy.deepcopy(fresh_spells.get("focus"))
    merged["spellState"] = merged_spells

    for key in ("maxHP", "maxHp", "ac", "proficiencyBonus", "defenses", "passives"):
        if key in fresh:
            merged[key] = copy.deepcopy(fresh.get(key))
    for key in ("currentHP", "currentHp", "tempHP", "tempHp"):
        if key in existing:
            merged[key] = copy.deepcopy(existing.get(key))

    local_meta = existing.get("importMeta") if isinstance(existing.get("importMeta"), dict) else {}
    old_meta = old_source.get("importMeta") if isinstance(old_source.get("importMeta"), dict) else {}
    fresh_meta = fresh.get("importMeta") if isinstance(fresh.get("importMeta"), dict) else {}
    merged_meta = copy.deepcopy(local_meta)
    for key, value in fresh_meta.items():
        if key in {"rawSnapshot", "externalSource"}:
            continue
        if key in _IMPORT_ROW_FIELDS:
            merged_meta[key] = _merge_source_rows(local_meta.get(key), old_meta.get(key), value)
        else:
            merged_meta[key] = copy.deepcopy(value)
    if "rawSnapshot" in fresh_meta:
        merged_meta["rawSnapshot"] = copy.deepcopy(fresh_meta.get("rawSnapshot"))
    merged["importMeta"] = merged_meta
    return merged


def build_external_source_metadata(*, character_id: str, source_document: dict[str, Any],
                                   previous: dict[str, Any] | None = None,
                                   auto_refresh: bool | None = None,
                                   now: str | None = None) -> dict[str, Any]:
    now = now or utc_now_iso()
    prev = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    linked_at = str(prev.get("linkedAt") or now)
    if auto_refresh is None:
        auto_refresh = bool(prev.get("autoRefresh", True))
    return {
        "provider": DDB_PROVIDER,
        "characterId": str(character_id or "").strip(),
        "sourceUrl": canonical_ddb_source_url(character_id),
        "linked": True,
        "linkedAt": linked_at,
        "lastSyncAttemptAt": now,
        "lastSyncedAt": now,
        "lastSuccessfulSyncAt": now,
        "syncStatus": DDB_SYNC_OK,
        "syncError": None,
        "sourceRevisionHash": source_fingerprint(source_document),
        "sourceSnapshot": build_source_snapshot(source_document),
        "autoRefresh": bool(auto_refresh),
        "autoRefreshMinutes": ddb_auto_refresh_minutes(),
    }


def attach_external_source_metadata(document: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(document) if isinstance(document, dict) else {}
    import_meta = out.get("importMeta") if isinstance(out.get("importMeta"), dict) else {}
    import_meta = copy.deepcopy(import_meta)
    import_meta["externalSource"] = copy.deepcopy(metadata)
    out["importMeta"] = import_meta
    return out


def remove_external_source_metadata(document: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(document) if isinstance(document, dict) else {}
    import_meta = out.get("importMeta") if isinstance(out.get("importMeta"), dict) else {}
    import_meta = copy.deepcopy(import_meta)
    import_meta.pop("externalSource", None)
    out["importMeta"] = import_meta
    return out


def mark_external_source_failure(profile: dict[str, Any], message: str, *, now: str | None = None) -> None:
    """Mutate link metadata only; playable character fields remain untouched."""
    if not isinstance(profile, dict):
        return
    native = profile.get("nativeCharacter")
    if not isinstance(native, dict):
        return
    import_meta = native.get("importMeta")
    if not isinstance(import_meta, dict):
        import_meta = {}
        native["importMeta"] = import_meta
    external = import_meta.get("externalSource")
    if not isinstance(external, dict):
        return
    external["lastSyncAttemptAt"] = now or utc_now_iso()
    external["syncStatus"] = DDB_SYNC_ERROR
    external["syncError"] = str(message or "D&D Beyond refresh failed.")[:500]
    profile["importMeta"] = import_meta


def merge_profile_state_for_upsert(existing_profile: dict[str, Any] | None,
                                   upsert_payload: dict[str, Any]) -> dict[str, Any]:
    """Carry profile-level Tavern personalization/live fields through an upsert."""
    out = copy.deepcopy(upsert_payload) if isinstance(upsert_payload, dict) else {}
    existing = existing_profile if isinstance(existing_profile, dict) else {}
    for key in _PROFILE_PRESERVE_KEYS:
        if key in existing:
            out[key] = copy.deepcopy(existing.get(key))
    return out


def _class_summary(document: dict[str, Any]) -> tuple[int, list[str]]:
    classes = document.get("classes") if isinstance(document.get("classes"), list) else []
    total = 0
    labels: list[str] = []
    for row in classes:
        if not isinstance(row, dict):
            continue
        try:
            level = max(0, int(row.get("level") or 0))
        except Exception:
            level = 0
        total += level
        name = str(row.get("name") or row.get("classId") or "").strip()
        subclass = str(row.get("subclass") or row.get("subclassName") or "").strip()
        if name:
            labels.append(f"{name} ({subclass})" if subclass else name)
    return total, labels


def _named_set(rows: Any) -> set[str]:
    out: set[str] = set()
    if not isinstance(rows, list):
        return out
    for row in rows:
        if isinstance(row, dict):
            value = str(row.get("name") or row.get("displayName") or row.get("id") or row.get("spellId") or "").strip()
        else:
            value = str(row or "").strip()
        if value:
            out.add(value)
    return out


def _spell_names(document: dict[str, Any]) -> set[str]:
    spell_state = document.get("spellState") if isinstance(document.get("spellState"), dict) else {}
    names = _named_set(spell_state.get("spellbookEntries"))
    if names:
        return names
    return _named_set(spell_state.get("known")) | _named_set(spell_state.get("prepared"))


def _equipment_names(document: dict[str, Any]) -> set[str]:
    equipment = document.get("equipment") if isinstance(document.get("equipment"), dict) else {}
    return _named_set(equipment.get("inventory"))


def _feature_names(document: dict[str, Any], key: str) -> set[str]:
    meta = document.get("importMeta") if isinstance(document.get("importMeta"), dict) else {}
    return _named_set(meta.get(key))


def _resolved_scalar(document: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if document.get(key) not in (None, ""):
            return document.get(key)
    return None


def build_change_summary(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    changes: list[dict[str, Any]] = []
    before_identity = before.get("identity") if isinstance(before.get("identity"), dict) else {}
    after_identity = after.get("identity") if isinstance(after.get("identity"), dict) else {}
    before_name = str(before_identity.get("name") or before_identity.get("displayName") or "")
    after_name = str(after_identity.get("name") or after_identity.get("displayName") or "")
    if before_name != after_name:
        changes.append({"field": "name", "before": before_name, "after": after_name})

    before_level, before_classes = _class_summary(before)
    after_level, after_classes = _class_summary(after)
    if before_level != after_level:
        changes.append({"field": "level", "before": before_level, "after": after_level})
    if before_classes != after_classes:
        changes.append({"field": "classes", "before": before_classes, "after": after_classes})

    before_scores = ((before.get("abilities") or {}).get("scores") if isinstance(before.get("abilities"), dict) else {}) or {}
    after_scores = ((after.get("abilities") or {}).get("scores") if isinstance(after.get("abilities"), dict) else {}) or {}
    if before_scores != after_scores:
        changes.append({"field": "stats", "before": before_scores, "after": after_scores})

    before_hp = _resolved_scalar(before, "maxHP", "maxHp")
    after_hp = _resolved_scalar(after, "maxHP", "maxHp")
    if before_hp != after_hp and (before_hp is not None or after_hp is not None):
        changes.append({"field": "maxHp", "before": before_hp, "after": after_hp})
    before_ac = _resolved_scalar(before, "ac")
    after_ac = _resolved_scalar(after, "ac")
    if before_ac != after_ac and (before_ac is not None or after_ac is not None):
        changes.append({"field": "ac", "before": before_ac, "after": after_ac})

    for field, getter in (
        ("equipment", _equipment_names),
        ("spells", _spell_names),
        ("features", lambda doc: _feature_names(doc, "importedFeatures")),
        ("actions", lambda doc: _feature_names(doc, "importedActions")),
    ):
        old = getter(before)
        new = getter(after)
        added = sorted(new - old)
        removed = sorted(old - new)
        if added or removed:
            changes.append({"field": field, "added": added[:40], "removed": removed[:40]})
    return changes
