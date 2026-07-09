"""Regression matrix for the state-sync payload optimization.

Covers the hard-requirement checks: profile-edit propagation (stub push with
new revision + fetch returns the updated body), the player seeing every item/
spell of their own sheet through the fetch path, DM library contents identical
to the session data, and the chat_bridge role keeping its minimal filtered sync.
"""
import anyio

from server.character.profile_stub import char_profile_revision
from server.handlers.content import (
    handle_char_profile_fetch,
    handle_char_profile_upsert,
)
from server.session import Session, User


class _CaptureManager:
    def __init__(self):
        self.sent = []

    def get_session_connections(self, session_id):
        return {}

    async def send_to(self, session_id, user_id, message):
        self.sent.append((user_id, message))
        return True

    async def broadcast(self, session_id, message, exclude_user=None):
        self.sent.append(("*", message))
        return True


def _session():
    session = Session(id="matrix")
    session.users["dm1"] = User(id="dm1", name="The DM", role="dm", connected=True)
    session.users["p1"] = User(id="p1", name="Hero", role="player", connected=True)
    session.users["chat_bridge"] = User(id="chat_bridge", name="Chat Bridge", role="chat_bridge", connected=True)
    session.char_profiles = {"hero": [{
        "id": "prof-1", "name": "Hero", "curhp": 10, "hp": 20, "updated_at": 100.0,
        "charSheet": {"spells": [f"spell-{i}" for i in range(40)]},
        "nativeCharacter": {"sourceMode": "native", "identity": {"name": "Hero"},
                            "equipment": {"inventory": [{"name": f"item-{i}", "qty": 1} for i in range(25)]}},
        "nativeRuntime": {"spellAccess": [{"id": f"spell-{i}"} for i in range(40)]},
    }]}
    session.item_library_entries = [{"id": f"lib-{i}", "name": f"Item {i}", "category": "Gear",
                                     "rarity": "Common", "default_qty": 1, "notes": "", "updated_at": 1.0}
                                    for i in range(15)]
    session.library_entries = [{"id": f"map-{i}", "name": f"Map {i}"} for i in range(6)]
    session.encounter_templates = [{"id": f"enc-{i}", "name": f"Encounter {i}"} for i in range(4)]
    return session


def test_profile_edit_propagates_stub_with_new_revision_then_fetch_returns_updated_body(monkeypatch):
    session = _session()
    player = session.users["p1"]
    captured = _CaptureManager()
    monkeypatch.setattr("server.handlers.content.manager", captured)
    monkeypatch.setattr("server.handlers.inventory.manager", captured)
    monkeypatch.setattr("server.handlers.common.manager", captured)

    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr("server.handlers.content.save_campaign_async", _noop)
    monkeypatch.setattr("server.handlers.common.save_campaign_async", _noop, raising=False)

    old_revision = char_profile_revision(session.char_profiles["hero"][0])

    anyio.run(handle_char_profile_upsert, {
        "id": "prof-1", "name": "Hero Renamed", "curhp": 4,
        "charSheet": {"spells": ["fireball"]},
    }, session, player)

    syncs = [m for uid, m in captured.sent if uid == "p1" and m["type"] == "char_profiles_sync"]
    assert syncs, "owner must receive a char_profiles_sync push"
    pushed = syncs[-1]["payload"]["profiles"]
    assert all(p["stub"] is True for p in pushed)
    stub = next(p for p in pushed if p["id"] == "prof-1")
    assert stub["name"] == "Hero Renamed"
    assert stub["revision"] != old_revision  # revision bumped by content change
    assert "charSheet" not in stub  # never the full body

    # Re-fetch returns the updated body at the pushed revision.
    captured.sent.clear()
    anyio.run(handle_char_profile_fetch, {"id": "prof-1"}, session, player)
    response = captured.sent[-1][1]["payload"]
    assert response["ok"] is True
    assert response["revision"] == stub["revision"]
    assert response["profile"]["name"] == "Hero Renamed"
    assert response["profile"]["charSheet"]["spells"] == ["fireball"]


def test_player_fetch_returns_every_item_and_spell_of_own_sheet(monkeypatch):
    session = _session()
    captured = _CaptureManager()
    monkeypatch.setattr("server.handlers.content.manager", captured)
    anyio.run(handle_char_profile_fetch, {"id": "prof-1"}, session, session.users["p1"])
    profile = captured.sent[-1][1]["payload"]["profile"]
    stored = session.char_profiles["hero"][0]
    assert profile["charSheet"]["spells"] == stored["charSheet"]["spells"]
    assert profile["nativeRuntime"]["spellAccess"] == stored["nativeRuntime"]["spellAccess"]
    assert profile["nativeCharacter"]["equipment"]["inventory"] == stored["nativeCharacter"]["equipment"]["inventory"]


def test_dm_state_sync_library_contents_identical_to_session_data():
    session = _session()
    state = session.to_state_dict_for_role("dm", "dm1")
    assert state["item_library_entries"] == session.item_library_entries
    assert state["library_entries"] == session.library_entries
    assert state["encounter_templates"] == session.encounter_templates


def test_chat_bridge_sync_stays_minimal():
    session = _session()
    state = session.to_state_dict_for_role("chat_bridge", "chat_bridge")
    assert state["char_profiles"] == []
    assert state["party_loot_log"] == []
    assert state["player_inventory"] == []
    assert state["encounter_templates"] == []
