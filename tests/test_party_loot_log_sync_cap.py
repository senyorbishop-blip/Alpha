"""Stage D: loot log sync cap + on-demand fetch of older entries."""
import anyio

from server.handlers.inventory import handle_party_loot_log_fetch
from server.session import PARTY_LOOT_LOG_SYNC_LIMIT, Session, User


def _session_with_loot(count=120):
    session = Session(id="loot-cap")
    session.users["dm1"] = User(id="dm1", name="DM", role="dm", connected=True)
    session.users["p1"] = User(id="p1", name="Hero", role="player", connected=True)
    session.users["v1"] = User(id="v1", name="Watcher", role="viewer", connected=True)
    session.party_loot_log = [
        {"id": f"loot-{i}", "timestamp": 1000.0 + i, "action": "loot", "item_name": f"item-{i}"}
        for i in range(count)
    ]
    return session


def test_state_sync_ships_only_newest_50_entries():
    session = _session_with_loot()
    for role, uid in (("dm", "dm1"), ("player", "p1")):
        state = session.to_state_dict_for_role(role, uid)
        entries = state["party_loot_log"]
        assert len(entries) == PARTY_LOOT_LOG_SYNC_LIMIT == 50
        assert entries[-1]["id"] == "loot-119"
        assert entries[0]["id"] == "loot-70"
    assert session.to_state_dict_for_role("viewer", "v1")["party_loot_log"] == []


class _CaptureManager:
    def __init__(self):
        self.sent = []

    async def send_to(self, session_id, user_id, message):
        self.sent.append((user_id, message))
        return True


def test_fetch_returns_older_entries_for_dm_and_player(monkeypatch):
    session = _session_with_loot()
    captured = _CaptureManager()
    monkeypatch.setattr("server.handlers.inventory.manager", captured)

    anyio.run(handle_party_loot_log_fetch, {"have": 50, "limit": 50}, session, session.users["p1"])
    payload = captured.sent[-1][1]["payload"]
    assert captured.sent[-1][1]["type"] == "party_loot_log_response"
    assert payload["total"] == 120
    assert len(payload["entries"]) == 50
    assert payload["entries"][-1]["id"] == "loot-69"  # directly older than the synced tail
    assert payload["entries"][0]["id"] == "loot-20"

    anyio.run(handle_party_loot_log_fetch, {"have": 100, "limit": 50}, session, session.users["dm1"])
    payload = captured.sent[-1][1]["payload"]
    assert [e["id"] for e in payload["entries"]] == [f"loot-{i}" for i in range(20)]


def test_fetch_denied_for_viewer(monkeypatch):
    session = _session_with_loot()
    captured = _CaptureManager()
    monkeypatch.setattr("server.handlers.inventory.manager", captured)
    anyio.run(handle_party_loot_log_fetch, {"have": 50}, session, session.users["v1"])
    assert captured.sent == []
