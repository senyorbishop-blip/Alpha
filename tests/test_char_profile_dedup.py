"""Stage B: persisted char_profiles dedup — storage layout changes, content never."""
import json
import sqlite3

import pytest

from server.character.profile_dedup import (
    DEDUP_META_KEY,
    char_profiles_roundtrip_ok,
    dedupe_char_profiles_for_storage,
    dedupe_profile_for_storage,
    hydrate_char_profiles_from_storage,
    hydrate_profile_from_storage,
)


def _duplicated_profile(profile_id="pdf-seraphine-crowe"):
    """Mimics the real-world shape: spellAccess/passives/classFeatures stored
    byte-identically in BOTH charSheet and nativeRuntime."""
    spell_access = [{"id": f"spell-{i}", "name": f"Spell {i}", "desc": "d" * 500} for i in range(120)]
    passives = [{"id": f"pv-{i}", "text": "p" * 400} for i in range(60)]
    class_features = [{"id": f"cf-{i}", "text": "c" * 400} for i in range(60)]
    return {
        "id": profile_id,
        "name": "Seraphine Crowe",
        "curhp": 18,
        "hp": 27,
        "charSheet": {
            "spellAccess": json.loads(json.dumps(spell_access)),
            "nativePassives": json.loads(json.dumps(passives)),
            "nativeClassFeatures": json.loads(json.dumps(class_features)),
            "classes": [{"classId": "warlock", "level": 4}],
        },
        "nativeRuntime": {
            "spellAccess": json.loads(json.dumps(spell_access)),
            "passives": json.loads(json.dumps(passives)),
            "classFeatures": json.loads(json.dumps(class_features)),
            "hp": {"max": 27, "current": 18, "temp": 0},
        },
        "nativeCharacter": {"sourceMode": "pdf", "identity": {"name": "Seraphine Crowe"}},
    }


def test_dedup_halves_duplicated_subtrees_and_roundtrips_identically():
    profiles = {"sezzypantz": [_duplicated_profile()]}
    before_bytes = len(json.dumps(profiles, separators=(",", ":")))

    deduped = dedupe_char_profiles_for_storage(profiles)
    after_bytes = len(json.dumps(deduped, separators=(",", ":")))
    assert after_bytes < before_bytes * 0.62  # duplicated bulk stored once

    rehydrated = hydrate_char_profiles_from_storage(json.loads(json.dumps(deduped)))
    assert json.dumps(rehydrated, sort_keys=True) == json.dumps(profiles, sort_keys=True)
    assert char_profiles_roundtrip_ok(profiles)


def test_dedup_leaves_input_untouched_and_marks_storage_shape():
    profile = _duplicated_profile()
    snapshot = json.dumps(profile, sort_keys=True)
    deduped = dedupe_profile_for_storage(profile)

    assert json.dumps(profile, sort_keys=True) == snapshot  # no mutation
    assert DEDUP_META_KEY in deduped
    assert deduped["charSheet"]["spellAccess"] == deduped["nativeRuntime"]["spellAccess"]
    assert "__dedup_ref__" in deduped["charSheet"]["spellAccess"]
    # Hydrated copies are independent objects again.
    hydrated = hydrate_profile_from_storage(json.loads(json.dumps(deduped)))
    assert hydrated["charSheet"]["spellAccess"] is not hydrated["nativeRuntime"]["spellAccess"]
    assert hydrated["charSheet"]["spellAccess"] == hydrated["nativeRuntime"]["spellAccess"]


def test_profiles_without_duplication_pass_through_unchanged():
    profile = {"id": "small", "name": "Tiny", "charSheet": {"hp": {"max": 10}}}
    assert dedupe_profile_for_storage(profile) is profile
    assert hydrate_profile_from_storage(profile) is profile


def test_db_serializer_dedupes_and_deserializer_hydrates():
    from server.db import _deserialize_campaign_field, _serialize_campaign_field

    profiles = {"sezzypantz": [_duplicated_profile()]}
    serialized = _serialize_campaign_field("audit-camp", "char_profiles", profiles)
    assert "__dedup_ref__" in serialized

    loaded = _deserialize_campaign_field("audit-camp", "char_profiles", serialized)
    assert json.dumps(loaded, sort_keys=True) == json.dumps(profiles, sort_keys=True)
    assert DEDUP_META_KEY not in loaded["sezzypantz"][0]


def test_startup_migration_rewrites_rows_and_preserves_content(tmp_path, monkeypatch):
    import server.db as db_mod

    db_path = tmp_path / "campaigns.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)

    profiles = {"sezzypantz": [_duplicated_profile()], "test": [_duplicated_profile("pdf-elphaba-swiftarrow")]}
    raw = json.dumps(profiles, separators=(",", ":"))
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE campaigns (id TEXT PRIMARY KEY, char_profiles TEXT)")
    conn.execute("INSERT INTO campaigns (id, char_profiles) VALUES (?, ?)", ("CAMP1", raw))
    conn.execute("INSERT INTO campaigns (id, char_profiles) VALUES (?, ?)", ("EMPTY", "{}"))
    conn.commit()
    conn.close()

    stats = db_mod.migrate_char_profiles_dedup()
    assert stats["rewritten"] == 1
    assert stats["bytes_after"] < stats["bytes_before"] * 0.62

    conn = sqlite3.connect(db_path)
    stored = conn.execute("SELECT char_profiles FROM campaigns WHERE id='CAMP1'").fetchone()[0]
    conn.close()
    assert "__dedup_ref__" in stored
    hydrated = hydrate_char_profiles_from_storage(json.loads(stored))
    assert json.dumps(hydrated, sort_keys=True) == json.dumps(profiles, sort_keys=True)

    # Second run is a no-op (idempotent).
    stats2 = db_mod.migrate_char_profiles_dedup()
    assert stats2["rewritten"] == 0


def test_full_persistence_roundtrip_keeps_profiles_identical(tmp_path, monkeypatch):
    """save_campaign → load_campaign round trip on a real Session object."""
    import server.db as db_mod
    from server.session import Session, User

    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "campaigns.db")
    db_mod.init_db()

    session = Session(id="RT1")
    session.name = "Roundtrip"
    session.users["p1"] = User(id="p1", name="Sezzypantz", role="player", connected=True)
    session.char_profiles = {"sezzypantz": [_duplicated_profile()]}

    assert db_mod.save_campaign(session) is True
    loaded = db_mod.load_campaign("RT1")
    assert loaded is not None
    assert json.dumps(loaded["char_profiles"], sort_keys=True) == json.dumps(session.char_profiles, sort_keys=True)

    # The stored column really is deduped.
    conn = sqlite3.connect(tmp_path / "campaigns.db")
    stored = conn.execute("SELECT char_profiles FROM campaigns WHERE id='RT1'").fetchone()[0]
    conn.close()
    assert "__dedup_ref__" in stored
    assert len(stored) < len(json.dumps(session.char_profiles, separators=(",", ":"))) * 0.62
