"""Stage A: state sync ships char-profile stubs; bodies come via char_profile_fetch.

Covers the access matrix hard requirement: fetch visibility must exactly
mirror state-sync visibility (DM = every profile, player = own bucket only,
viewer/chat_bridge = none), and every denial/miss must produce an explicit
error response so the client can surface it.
"""
import anyio

from server.character.profile_stub import (
    build_char_profile_stub,
    build_char_profile_stub_list,
    build_char_profile_stub_map,
    char_profile_revision,
    char_profiles_fingerprint,
)
from server.handlers.content import handle_char_profile_fetch, _send_char_profiles
from server.payload_diagnostics import payload_byte_size
from server.session import Session, User


def _fat_profile(profile_id="prof-1", name="Elphaba"):
    spell_access = [{"id": f"spell-{i}", "name": f"Spell {i}", "desc": "x" * 400} for i in range(200)]
    passives = [{"id": f"passive-{i}", "text": "y" * 300} for i in range(100)]
    return {
        "id": profile_id,
        "name": name,
        "updated_at": 1000.0,
        "created_at": 900.0,
        "curhp": 22,
        "hp": 31,
        "tempHp": 3,
        "ac": 15,
        "level": 5,
        "classId": "wizard",
        "color": "#00e5cc",
        "sourceMode": "pdf",
        "charBook": {"className": "Wizard", "avatarUrl": "/static/user_uploads/elphaba.png", "backstory": "z" * 2000},
        "charSheet": {"spellAccess": spell_access, "classes": [{"classId": "wizard", "level": 5}], "totalLevel": 5},
        "nativeCharacter": {"sourceMode": "pdf", "identity": {"name": name}},
        "nativeRuntime": {"spellAccess": spell_access, "passives": passives},
    }


def _session_with_profiles():
    session = Session(id="stub-sync-test")
    session.users["dm1"] = User(id="dm1", name="The DM", role="dm", connected=True)
    session.users["p1"] = User(id="p1", name="Hero", role="player", connected=True)
    session.users["p2"] = User(id="p2", name="Rival", role="player", connected=True)
    session.users["v1"] = User(id="v1", name="Watcher", role="viewer", connected=True)
    session.users["chat_bridge"] = User(id="chat_bridge", name="Chat Bridge", role="chat_bridge", connected=True)
    session.char_profiles = {
        "hero": [_fat_profile("prof-hero", "Elphaba")],
        "rival": [_fat_profile("prof-rival", "Fiyero")],
    }
    return session


def test_stub_contains_scalars_and_revision_but_no_body():
    profile = _fat_profile()
    stub = build_char_profile_stub(profile)

    assert stub["stub"] is True
    assert stub["id"] == "prof-1"
    assert stub["name"] == "Elphaba"
    assert stub["curhp"] == 22 and stub["hp"] == 31 and stub["tempHp"] == 3
    assert stub["ac"] == 15 and stub["level"] == 5
    assert stub["updated_at"] == 1000.0
    assert stub["revision"] == char_profile_revision(profile)
    # Sheet-mode resolution shim survives without shipping the document.
    assert stub["nativeCharacter"] == {"stub": True, "sourceMode": "pdf"}
    assert stub["charBook"] == {"className": "Wizard"}
    # No body payloads.
    assert "charSheet" not in stub
    assert "nativeRuntime" not in stub
    assert payload_byte_size({"stub": stub}) < 2048


def test_stub_revision_changes_on_any_profile_mutation():
    profile = _fat_profile()
    before = char_profile_revision(profile)
    profile["nativeRuntime"]["passives"][0]["text"] = "changed"
    after = char_profile_revision(profile)
    assert before != after

    session = _session_with_profiles()
    fp_before = char_profiles_fingerprint(session)
    session.char_profiles["hero"][0]["curhp"] = 1
    assert char_profiles_fingerprint(session) != fp_before


def test_state_sync_ships_stubs_per_role():
    session = _session_with_profiles()

    dm_state = session.to_state_dict_for_role("dm", "dm1")
    assert set(dm_state["char_profiles"].keys()) == {"hero", "rival"}
    for rows in dm_state["char_profiles"].values():
        assert all(row.get("stub") is True for row in rows)
        assert all("charSheet" not in row and "nativeRuntime" not in row for row in rows)

    player_state = session.to_state_dict_for_role("player", "p1")
    assert [row["id"] for row in player_state["char_profiles"]] == ["prof-hero"]
    assert player_state["char_profiles"][0]["stub"] is True

    viewer_state = session.to_state_dict_for_role("viewer", "v1")
    assert viewer_state["char_profiles"] == []
    bridge_state = session.to_state_dict_for_role("chat_bridge", "chat_bridge")
    assert bridge_state["char_profiles"] == []


def test_state_sync_char_profiles_domain_is_small():
    session = _session_with_profiles()
    full_bytes = payload_byte_size({"payload": dict(session.char_profiles)})
    dm_state = session.to_state_dict_for_role("dm", "dm1")
    stub_bytes = payload_byte_size({"payload": dm_state["char_profiles"]})
    assert full_bytes > 200_000  # the fixture really is fat
    assert stub_bytes < 8_192


class _CaptureManager:
    def __init__(self):
        self.sent = []

    def get_session_connections(self, session_id):
        return {}

    async def send_to(self, session_id, user_id, message):
        self.sent.append((user_id, message))
        return True


def _run_fetch(session, user, payload, monkeypatch):
    captured = _CaptureManager()
    monkeypatch.setattr("server.handlers.content.manager", captured)
    anyio.run(handle_char_profile_fetch, payload, session, user)
    assert len(captured.sent) == 1
    user_id, message = captured.sent[0]
    assert user_id == user.id
    assert message["type"] == "char_profile_response"
    return message["payload"]


def test_char_profile_fetch_dm_can_fetch_every_profile(monkeypatch):
    session = _session_with_profiles()
    dm = session.users["dm1"]
    for profile_id, owner in (("prof-hero", "hero"), ("prof-rival", "rival")):
        payload = _run_fetch(session, dm, {"id": profile_id}, monkeypatch)
        assert payload["ok"] is True
        assert payload["owner_key"] == owner
        assert payload["profile"]["id"] == profile_id
        # Full body present (sanitized), not a stub.
        assert payload["profile"].get("stub") is not True
        assert "charSheet" in payload["profile"]
        assert payload["revision"] == char_profile_revision(
            session.char_profiles[owner][0]
        )


def test_char_profile_fetch_player_own_only(monkeypatch):
    session = _session_with_profiles()
    hero = session.users["p1"]

    own = _run_fetch(session, hero, {"id": "prof-hero"}, monkeypatch)
    assert own["ok"] is True
    assert own["profile"]["id"] == "prof-hero"
    assert "nativeRuntime" in own["profile"]

    others = _run_fetch(session, hero, {"id": "prof-rival"}, monkeypatch)
    assert others["ok"] is False
    assert others["error"]


def test_char_profile_fetch_denied_roles_get_visible_error(monkeypatch):
    session = _session_with_profiles()
    for uid in ("v1", "chat_bridge"):
        payload = _run_fetch(session, session.users[uid], {"id": "prof-hero"}, monkeypatch)
        assert payload["ok"] is False
        assert payload["error"]

    missing = _run_fetch(session, session.users["dm1"], {"id": ""}, monkeypatch)
    assert missing["ok"] is False
    unknown = _run_fetch(session, session.users["dm1"], {"id": "prof-nope"}, monkeypatch)
    assert unknown["ok"] is False


def test_char_profiles_sync_pushes_stubs_not_bodies(monkeypatch):
    session = _session_with_profiles()
    captured = _CaptureManager()
    monkeypatch.setattr("server.handlers.content.manager", captured)

    anyio.run(_send_char_profiles, session, "p1")

    sync = next(m for uid, m in captured.sent if m["type"] == "char_profiles_sync")
    profiles = sync["payload"]["profiles"]
    assert profiles and all(p["stub"] is True for p in profiles)
    assert all("charSheet" not in p and "nativeRuntime" not in p for p in profiles)
    assert payload_byte_size(sync) < 8_192


def test_stub_map_and_list_builders_tolerate_junk():
    assert build_char_profile_stub_map(None) == {}
    assert build_char_profile_stub_map({"o": "not-a-list"}) == {"o": []}
    assert build_char_profile_stub_list([None, "x", {"id": "a", "name": "A"}])[0]["id"] == "a"
