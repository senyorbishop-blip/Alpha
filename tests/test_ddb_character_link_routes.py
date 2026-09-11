from types import SimpleNamespace
import copy

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import main
import server.character.ddb_link_routes as ddb_routes
from server.character.ddb_linking import attach_external_source_metadata, build_external_source_metadata, get_external_source


def _csrf_headers(client: TestClient) -> dict:
    client.get("/join")
    token = client.cookies.get("csrf_token") or ""
    return {"X-CSRF-Token": token} if token else {}


def _source_doc(level=5):
    return {
        "schemaVersion": 1,
        "sourceMode": "dndbeyond",
        "identity": {
            "characterId": "87369199", "name": "Aria", "displayName": "Aria",
            "portraitUrl": "https://images.example/aria.png",
            "tokenImageUrl": "/static/user_uploads/tokens/manual.png", "notes": "Keep me",
        },
        "species": {"id": "human", "name": "Human", "speed": 30, "traits": []},
        "background": {"id": "sage", "name": "Sage"},
        "abilities": {
            "generationMode": "imported",
            "scores": {"str": 10, "dex": 14, "con": 14, "int": 18, "wis": 12, "cha": 8},
        },
        "classes": [{"classId": "wizard", "name": "Wizard", "level": level}],
        "equipment": {
            "currency": {"gp": 10},
            "inventory": [{"id": "staff", "name": "Quarterstaff", "quantity": 1}],
            "equipped": ["staff"], "containers": [],
        },
        "spellState": {
            "known": ["magic-missile"], "prepared": ["magic-missile"],
            "slots": {"1": {"max": 4, "used": 2}}, "rituals": [],
            "spellbookEntries": [{"id": "magic-missile", "name": "Magic Missile"}],
            "classSources": [], "focus": {},
        },
        "importMeta": {
            "origin": "dndbeyond", "source": "dndbeyond", "externalId": "87369199",
            "importedActions": [], "importedFeatures": [],
            "importedSpells": [{"id": "magic-missile", "name": "Magic Missile"}],
        },
        "currentHP": 17,
        "tempHP": 3,
        "presentation": {"tokenColor": "#123456"},
    }


def _linked_profile(profile_id="profile-a", level=5):
    source = _source_doc(level=level)
    external = build_external_source_metadata(
        character_id="87369199", source_document=source, now="2026-09-11T01:00:00Z"
    )
    native = attach_external_source_metadata(source, external)
    return {
        "id": profile_id, "name": "Aria", "classSummary": "Wizard", "level": level,
        "sourceMode": "dndbeyond", "nativeCharacter": native,
        "nativeRuntime": {"hp": {"max": 40, "current": 17, "temp": 3}, "ac": 14},
        "importMeta": native["importMeta"], "characterNotes": "Keep profile note",
    }


def _session():
    dm = SimpleNamespace(id="dm-1", name="Dungeon Master", role="dm")
    player_a = SimpleNamespace(id="player-a", name="Player A", role="player")
    player_b = SimpleNamespace(id="player-b", name="Player B", role="player")
    token = SimpleNamespace(
        id="token-a", owner_id="player-a", profile_id="profile-a", library_id="profile-a",
        character_id="", x=123, y=456, hp=17, max_hp=40,
        image_url="/manual-token.png", color="#abcdef", size=1.5,
    )
    return SimpleNamespace(
        id="S1", dm_id="dm-1",
        users={"dm-1": dm, "player-a": player_a, "player-b": player_b},
        char_profiles={
            "player a": [_linked_profile("profile-a")],
            "player b": [_linked_profile("profile-b")],
        },
        active_char_profiles={}, tokens={"token-a": token},
    )


def _install_common(monkeypatch, session, auth_holder):
    async def fake_save_campaign(_session):
        return None
    monkeypatch.setattr(ddb_routes, "get_request_user", lambda request: auth_holder["user"])
    monkeypatch.setattr(ddb_routes, "get_or_restore_session", lambda session_id: session)
    monkeypatch.setattr(ddb_routes, "save_campaign_async", fake_save_campaign)


def _fake_save_factory():
    async def fake_save_document(*, session, owner_key, document, profile_id="", existing_profile=None, source="dndbeyond"):
        if existing_profile is None:
            saved = {
                "id": profile_id or str(((document.get("importMeta") or {}).get("externalId") or "87369199")),
                "name": str(((document.get("identity") or {}).get("name") or "Imported")),
                "classSummary": "Wizard",
                "level": sum(int(row.get("level") or 0) for row in (document.get("classes") or []) if isinstance(row, dict)),
                "sourceMode": "dndbeyond", "nativeCharacter": copy.deepcopy(document),
                "nativeRuntime": {"hp": {"max": 40, "current": 40, "temp": 0}, "ac": 14},
                "importMeta": copy.deepcopy(document.get("importMeta") or {}),
            }
            session.char_profiles.setdefault(owner_key, []).append(saved)
            return saved
        runtime = copy.deepcopy(existing_profile.get("nativeRuntime") or {})
        notes = existing_profile.get("characterNotes")
        existing_profile["nativeCharacter"] = copy.deepcopy(document)
        existing_profile["importMeta"] = copy.deepcopy(document.get("importMeta") or {})
        existing_profile["name"] = str(((document.get("identity") or {}).get("name") or existing_profile.get("name") or "Imported"))
        existing_profile["level"] = sum(int(row.get("level") or 0) for row in (document.get("classes") or []) if isinstance(row, dict))
        existing_profile["nativeRuntime"] = runtime
        existing_profile["characterNotes"] = notes
        return existing_profile
    return fake_save_document


def test_existing_ddb_commit_path_is_upgraded_to_link_route(monkeypatch):
    async def fake_link(request, payload):
        return JSONResponse({"ok": True, "linked_route": True})
    monkeypatch.setattr(ddb_routes, "_link_from_payload", fake_link)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/character/import/ddb-id/commit",
            json={"session_id": "s1", "preview_document": {}},
            headers=_csrf_headers(client),
        )
    assert res.status_code == 200
    assert res.json()["linked_route"] is True


def test_link_rejects_unauthenticated_user(monkeypatch):
    session = _session()
    auth_holder = {"user": None}
    _install_common(monkeypatch, session, auth_holder)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/character/link/dndbeyond",
            json={"session_id": "s1", "character": "87369199"},
            headers=_csrf_headers(client),
        )
    assert res.status_code == 401


def test_authenticated_owner_can_link_new_profile_and_metadata_persists(monkeypatch):
    session = _session()
    auth_holder = {"user": {"id": "player-a", "username": "Player A"}}
    _install_common(monkeypatch, session, auth_holder)
    fresh = _source_doc(level=5)
    async def fake_fetch(character_input, *, import_resolution=None):
        return "87369199", copy.deepcopy(fresh), {"warnings": [], "requires_resolution": False, "required_choices": []}
    monkeypatch.setattr(ddb_routes, "_fetch_normalized_source", fake_fetch)
    monkeypatch.setattr(ddb_routes, "_save_document", _fake_save_factory())
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/character/link/dndbeyond",
            json={"session_id": "s1", "character": "https://www.dndbeyond.com/characters/87369199"},
            headers=_csrf_headers(client),
        )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["externalSource"]["provider"] == "dndbeyond"
    assert body["externalSource"]["characterId"] == "87369199"
    assert get_external_source(session.char_profiles["player a"][-1])["sourceRevisionHash"]


def test_player_cannot_refresh_or_relink_another_players_profile(monkeypatch):
    session = _session()
    auth_holder = {"user": {"id": "player-a", "username": "Player A"}}
    _install_common(monkeypatch, session, auth_holder)
    calls = {"fetch": 0}
    async def should_not_fetch(*args, **kwargs):
        calls["fetch"] += 1
        return "87369199", _source_doc(), {"warnings": [], "requires_resolution": False}
    monkeypatch.setattr(ddb_routes, "_fetch_normalized_source", should_not_fetch)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        headers = _csrf_headers(client)
        refresh_res = client.post(
            "/api/character/profile-b/refresh/dndbeyond", json={"session_id": "s1"}, headers=headers
        )
        relink_res = client.post(
            "/api/character/link/dndbeyond",
            json={"session_id": "s1", "profile_id": "profile-b", "character": "87369199"},
            headers=headers,
        )
    assert refresh_res.status_code == 403
    assert relink_res.status_code == 403
    assert calls["fetch"] == 0


def test_refresh_unchanged_only_updates_sync_metadata(monkeypatch):
    session = _session()
    auth_holder = {"user": {"id": "player-a", "username": "Player A"}}
    _install_common(monkeypatch, session, auth_holder)
    profile = session.char_profiles["player a"][0]
    source_snapshot = copy.deepcopy(get_external_source(profile)["sourceSnapshot"])
    playable_before = copy.deepcopy(profile["nativeCharacter"])
    playable_before["importMeta"].pop("externalSource", None)
    async def fake_fetch(character_input, *, import_resolution=None):
        return "87369199", _source_doc(level=5), {"warnings": [], "requires_resolution": False, "required_choices": []}
    monkeypatch.setattr(ddb_routes, "_fetch_normalized_source", fake_fetch)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/character/profile-a/refresh/dndbeyond", json={"session_id": "s1"}, headers=_csrf_headers(client)
        )
    assert res.status_code == 200
    assert res.json()["changed"] is False
    after = copy.deepcopy(profile["nativeCharacter"])
    after["importMeta"].pop("externalSource", None)
    assert after == playable_before
    external = get_external_source(profile)
    assert external["syncStatus"] == "ok"
    assert external["sourceSnapshot"] == source_snapshot


def test_refresh_changed_merges_source_without_resetting_tavern_state_or_token(monkeypatch):
    session = _session()
    auth_holder = {"user": {"id": "player-a", "username": "Player A"}}
    _install_common(monkeypatch, session, auth_holder)
    monkeypatch.setattr(ddb_routes, "_save_document", _fake_save_factory())
    profile = session.char_profiles["player a"][0]
    profile["nativeCharacter"]["equipment"]["inventory"].append({"id": "dm-item", "name": "DM Item", "quantity": 1})
    token_before = copy.deepcopy(vars(session.tokens["token-a"]))
    async def fake_fetch(character_input, *, import_resolution=None):
        fresh = _source_doc(level=6)
        fresh["spellState"]["known"].append("fireball")
        fresh["spellState"]["prepared"].append("fireball")
        fresh["spellState"]["spellbookEntries"].append({"id": "fireball", "name": "Fireball"})
        return "87369199", fresh, {"warnings": [], "requires_resolution": False, "required_choices": []}
    monkeypatch.setattr(ddb_routes, "_fetch_normalized_source", fake_fetch)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/character/profile-a/refresh/dndbeyond", json={"session_id": "s1"}, headers=_csrf_headers(client)
        )
    assert res.status_code == 200
    assert res.json()["changed"] is True
    native = profile["nativeCharacter"]
    assert native["classes"][0]["level"] == 6
    assert native["currentHP"] == 17
    assert native["tempHP"] == 3
    assert native["spellState"]["slots"]["1"]["used"] == 2
    assert native["identity"]["tokenImageUrl"] == "/static/user_uploads/tokens/manual.png"
    assert native["identity"]["notes"] == "Keep me"
    assert profile["characterNotes"] == "Keep profile note"
    assert any(row.get("id") == "dm-item" for row in native["equipment"]["inventory"])
    assert vars(session.tokens["token-a"]) == token_before


def test_refresh_failure_keeps_playable_character_and_marks_sync_error(monkeypatch):
    session = _session()
    auth_holder = {"user": {"id": "player-a", "username": "Player A"}}
    _install_common(monkeypatch, session, auth_holder)
    profile = session.char_profiles["player a"][0]
    before = copy.deepcopy(profile["nativeCharacter"])
    before["importMeta"].pop("externalSource", None)
    async def failing_fetch(*args, **kwargs):
        raise HTTPException(status_code=502, detail="D&D Beyond returned 500")
    monkeypatch.setattr(ddb_routes, "_fetch_normalized_source", failing_fetch)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/character/profile-a/refresh/dndbeyond", json={"session_id": "s1"}, headers=_csrf_headers(client)
        )
    assert res.status_code == 502
    assert "still available" in res.json()["error"]
    after = copy.deepcopy(profile["nativeCharacter"])
    external = after["importMeta"].pop("externalSource")
    assert after == before
    assert external["syncStatus"] == "error"
    assert external["syncError"]


def test_refresh_private_character_failure_is_friendly_and_non_destructive(monkeypatch):
    session = _session()
    auth_holder = {"user": {"id": "player-a", "username": "Player A"}}
    _install_common(monkeypatch, session, auth_holder)
    async def private_fetch(*args, **kwargs):
        raise HTTPException(status_code=502, detail="D&D Beyond returned 403")
    monkeypatch.setattr(ddb_routes, "_fetch_normalized_source", private_fetch)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/character/profile-a/refresh/dndbeyond", json={"session_id": "s1"}, headers=_csrf_headers(client)
        )
    assert res.status_code == 502
    assert res.json()["error"] == "D&D Beyond could not return this character. Make the character public and try again."
    assert session.char_profiles["player a"][0]["nativeCharacter"]["identity"]["name"] == "Aria"


def test_dm_can_refresh_a_players_linked_profile(monkeypatch):
    session = _session()
    auth_holder = {"user": {"id": "dm-1", "username": "Dungeon Master", "role": "dm"}}
    _install_common(monkeypatch, session, auth_holder)
    async def fake_fetch(character_input, *, import_resolution=None):
        return "87369199", _source_doc(level=5), {"warnings": [], "requires_resolution": False, "required_choices": []}
    monkeypatch.setattr(ddb_routes, "_fetch_normalized_source", fake_fetch)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/character/profile-b/refresh/dndbeyond", json={"session_id": "s1"}, headers=_csrf_headers(client)
        )
    assert res.status_code == 200


def test_unlink_removes_relationship_but_keeps_profile_token_and_campaign_state(monkeypatch):
    session = _session()
    auth_holder = {"user": {"id": "player-a", "username": "Player A"}}
    _install_common(monkeypatch, session, auth_holder)
    profile = session.char_profiles["player a"][0]
    native_before = copy.deepcopy(profile["nativeCharacter"])
    native_before["importMeta"].pop("externalSource", None)
    token_before = copy.deepcopy(vars(session.tokens["token-a"]))
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/api/character/profile-a/unlink/dndbeyond", json={"session_id": "s1"}, headers=_csrf_headers(client)
        )
    assert res.status_code == 200
    assert res.json()["linked"] is False
    assert get_external_source(profile) == {}
    assert profile["nativeCharacter"] == native_before
    assert vars(session.tokens["token-a"]) == token_before
