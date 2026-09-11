import copy
import json

import pytest

from server.character.ddb_linking import (
    attach_external_source_metadata,
    build_change_summary,
    build_external_source_metadata,
    build_source_snapshot,
    get_external_source,
    merge_refresh_document,
    remove_external_source_metadata,
    source_fingerprint,
)
from server.character.ddb_link_routes import validate_remote_ddb_payload
from server.integrations.service import extract_ddb_character_id


def _source_doc(level=5, max_hp=40):
    return {
        "schemaVersion": 1,
        "sourceMode": "dndbeyond",
        "identity": {
            "characterId": "87369199",
            "name": "Aria",
            "displayName": "Aria",
            "portraitUrl": "https://images.example/aria.png",
        },
        "species": {
            "id": "human", "name": "Human", "speed": 30,
            "traits": [{"id": "ddb-species-versatile", "name": "Versatile"}],
        },
        "background": {"id": "sage", "name": "Sage"},
        "abilities": {
            "generationMode": "imported",
            "scores": {"str": 10, "dex": 14, "con": 14, "int": 18, "wis": 12, "cha": 8},
        },
        "classes": [{"classId": "wizard", "name": "Wizard", "level": level}],
        "maxHP": max_hp,
        "ac": 14,
        "equipment": {
            "currency": {"gp": 10},
            "inventory": [{"id": "staff", "name": "Quarterstaff", "quantity": 1, "damage": "1d6"}],
            "equipped": ["staff"],
            "containers": [],
        },
        "spellState": {
            "known": ["magic-missile"], "prepared": ["magic-missile"],
            "slots": {"1": {"max": 4, "used": 0}}, "rituals": [],
            "spellbookEntries": [{"id": "magic-missile", "name": "Magic Missile"}],
            "classSources": [], "focus": {},
        },
        "importMeta": {
            "origin": "dndbeyond", "source": "dndbeyond", "externalId": "87369199",
            "importedAt": 1, "rawSnapshot": {"data": {"id": 87369199}},
            "importedActions": [{"id": "arcane-recovery", "name": "Arcane Recovery", "numberUsed": 0}],
            "importedFeatures": [{"id": "spellcasting", "name": "Spellcasting"}],
            "importedSpells": [{"id": "magic-missile", "name": "Magic Missile"}],
        },
    }


def test_ddb_id_parsing_covers_supported_inputs_and_rejects_garbage():
    assert extract_ddb_character_id("87369199") == "87369199"
    assert extract_ddb_character_id("https://www.dndbeyond.com/characters/87369199") == "87369199"
    assert extract_ddb_character_id("https://www.dndbeyond.com/profile/player/characters/87369199") == "87369199"
    assert extract_ddb_character_id("https://www.dndbeyond.com/sheet-pdfs/player42_87369199.pdf") == "87369199"
    assert extract_ddb_character_id("garbage") == ""
    assert extract_ddb_character_id("") == ""


def test_remote_payload_validation_rejects_schema_drift_and_mismatched_ids():
    good = {"data": {"id": 87369199, "name": "Aria", "classes": [{"definition": {"name": "Wizard"}, "level": 5}]}}
    assert validate_remote_ddb_payload(good, expected_id="87369199")["name"] == "Aria"
    with pytest.raises(ValueError):
        validate_remote_ddb_payload({"data": {"id": 87369199, "name": "Aria", "classes": []}}, expected_id="87369199")
    with pytest.raises(ValueError):
        validate_remote_ddb_payload({"data": {"id": 123, "name": "Aria", "classes": [{"definition": {"name": "Wizard"}}]}}, expected_id="87369199")
    with pytest.raises(ValueError):
        validate_remote_ddb_payload(["unexpected"])


def test_source_fingerprint_ignores_volatile_import_and_live_state():
    first = _source_doc()
    second = copy.deepcopy(first)
    second["importMeta"]["importedAt"] = 999
    second["importMeta"]["rawSnapshot"] = {"different": "diagnostic payload"}
    second["currentHP"] = 1
    second["tempHP"] = 9
    second["spellState"]["slots"]["1"]["used"] = 3
    metadata = build_external_source_metadata(character_id="87369199", source_document=first)
    second = attach_external_source_metadata(second, metadata)
    assert source_fingerprint(first) == source_fingerprint(second)


def test_link_metadata_is_persistent_serializable_and_removable_without_character_loss():
    doc = _source_doc()
    metadata = build_external_source_metadata(
        character_id="87369199", source_document=doc, auto_refresh=True, now="2026-09-11T01:00:00Z"
    )
    linked = attach_external_source_metadata(doc, metadata)
    restored = json.loads(json.dumps(linked))
    external = get_external_source(restored)
    assert external["provider"] == "dndbeyond"
    assert external["characterId"] == "87369199"
    assert external["linked"] is True
    assert external["lastSuccessfulSyncAt"] == "2026-09-11T01:00:00Z"
    assert external["sourceRevisionHash"] == source_fingerprint(doc)
    assert isinstance(external["sourceSnapshot"], dict)
    unlinked = remove_external_source_metadata(restored)
    assert get_external_source(unlinked) == {}
    assert unlinked["identity"]["name"] == "Aria"
    assert unlinked["equipment"]["inventory"][0]["name"] == "Quarterstaff"


def test_refresh_merge_updates_source_fields_and_preserves_tavern_live_state():
    old_source = _source_doc(level=5, max_hp=40)
    local = copy.deepcopy(old_source)
    local["currentHP"] = 17
    local["tempHP"] = 4
    local["identity"]["tokenImageUrl"] = "/static/user_uploads/tokens/manual.png"
    local["identity"]["notes"] = "Tavern-only player note"
    local["presentation"] = {"tokenColor": "#123456"}
    local["spellState"]["slots"]["1"]["used"] = 3
    local["equipment"]["inventory"][0]["quantity"] = 0
    local["equipment"]["inventory"].append({"id": "dm-medal", "name": "DM Medal", "quantity": 1, "source": "campaign"})
    local["importMeta"]["importedActions"][0]["numberUsed"] = 1
    local["importMeta"]["importedActions"].append({"id": "tavern-custom-action", "name": "Campaign Rally", "numberUsed": 0})

    fresh = _source_doc(level=6, max_hp=48)
    fresh["ac"] = 15
    fresh["equipment"]["inventory"][0]["damage"] = "1d8"
    fresh["equipment"]["inventory"].append({"id": "wand", "name": "Wand of Magic Missiles", "quantity": 1})
    fresh["spellState"]["known"].append("fireball")
    fresh["spellState"]["prepared"].append("fireball")
    fresh["spellState"]["spellbookEntries"].append({"id": "fireball", "name": "Fireball"})
    fresh["importMeta"]["importedActions"].append({"id": "new-action", "name": "New Action", "numberUsed": 0})

    merged = merge_refresh_document(local, fresh, build_source_snapshot(old_source))
    assert merged["classes"][0]["level"] == 6
    assert merged["maxHP"] == 48
    assert merged["ac"] == 15
    assert merged["currentHP"] == 17
    assert merged["tempHP"] == 4
    assert merged["identity"]["tokenImageUrl"] == "/static/user_uploads/tokens/manual.png"
    assert merged["identity"]["notes"] == "Tavern-only player note"
    assert merged["presentation"] == {"tokenColor": "#123456"}
    assert merged["spellState"]["slots"]["1"]["used"] == 3

    inventory = {row["id"]: row for row in merged["equipment"]["inventory"]}
    assert inventory["staff"]["damage"] == "1d8"
    assert inventory["staff"]["quantity"] == 0
    assert inventory["wand"]["quantity"] == 1
    assert inventory["dm-medal"]["source"] == "campaign"
    actions = {row["id"]: row for row in merged["importMeta"]["importedActions"]}
    assert actions["arcane-recovery"]["numberUsed"] == 1
    assert "new-action" in actions
    assert "tavern-custom-action" in actions
    assert "fireball" in merged["spellState"]["known"]
    assert "fireball" in merged["spellState"]["prepared"]


def test_refresh_change_summary_focuses_on_human_relevant_changes():
    before = _source_doc(level=5, max_hp=40)
    after = _source_doc(level=6, max_hp=48)
    after["equipment"]["inventory"].append({"id": "wand", "name": "Wand", "quantity": 1})
    after["spellState"]["known"].append("fireball")
    after["spellState"]["spellbookEntries"].append({"id": "fireball", "name": "Fireball"})
    changes = build_change_summary(before, after)
    by_field = {row["field"]: row for row in changes}
    assert by_field["level"] == {"field": "level", "before": 5, "after": 6}
    assert by_field["maxHp"]["after"] == 48
    assert by_field["equipment"]["added"] == ["Wand"]
    assert by_field["spells"]["added"] == ["Fireball"]
