"""Stage C: reconnect delta sync — unchanged heavy domains are omitted."""
import anyio

from server.session import Session, User
from server.state_delta import (
    DELTA_DOMAIN_KEYS,
    apply_reconnect_delta,
    compute_domain_revisions,
)


def _session():
    session = Session(id="delta-test")
    session.users["dm1"] = User(id="dm1", name="DM", role="dm", connected=True)
    session.users["p1"] = User(id="p1", name="Hero", role="player", connected=True)
    session.char_profiles = {"hero": [{"id": "prof-1", "name": "Hero", "hp": 20, "curhp": 20,
                                       "charSheet": {"x": "y" * 3000}}]}
    session.editor_props = {"world": [{"id": f"p{i}", "kind": "door"} for i in range(30)]}
    session.party_loot_log = [{"action": "loot", "item_name": f"item-{i}"} for i in range(30)]
    session.token_state_revision = 7
    session.visibility_revision = 5
    session.inventory_revision = 3
    return session


def test_full_sync_attaches_revisions_and_omits_nothing():
    session = _session()
    state = session.to_state_dict_for_role("dm", "dm1")
    apply_reconnect_delta(state, None)
    assert state["delta"] is False
    assert state["omitted_domains"] == []
    assert set(state["domain_revisions"].keys()) == set(DELTA_DOMAIN_KEYS.keys())
    for keys in DELTA_DOMAIN_KEYS.values():
        for key in keys:
            if key in ("player_inventory",):  # player-only key absent for dm
                continue
    assert "tokens" in state and "char_profiles" in state and "editor_props" in state


def test_matching_crcs_omit_all_delta_domains():
    session = _session()
    baseline = session.to_state_dict_for_role("dm", "dm1")
    known = compute_domain_revisions(baseline)

    state = session.to_state_dict_for_role("dm", "dm1")
    apply_reconnect_delta(state, known)
    assert state["delta"] is True
    assert set(state["omitted_domains"]) == set(DELTA_DOMAIN_KEYS.keys())
    for keys in DELTA_DOMAIN_KEYS.values():
        for key in keys:
            assert key not in state
    # Small always-send fields survive.
    assert "users" in state and "log" in state and "combat" in state
    assert state["token_state_revision"] == 7


def test_changed_domain_is_resent_others_still_omitted():
    session = _session()
    baseline = session.to_state_dict_for_role("dm", "dm1")
    known = compute_domain_revisions(baseline)

    # Mutate profiles + editor; tokens/fog/inventory untouched.
    session.char_profiles["hero"][0]["curhp"] = 5
    session.editor_props["world"].append({"id": "new", "kind": "chest"})

    state = session.to_state_dict_for_role("dm", "dm1")
    apply_reconnect_delta(state, known)
    omitted = set(state["omitted_domains"])
    assert "char_profiles" not in omitted and "editor" not in omitted
    assert {"tokens", "fog", "inventory"} <= omitted
    assert state["char_profiles"]["hero"][0]["curhp"] == 5
    assert "editor_props" in state


def test_live_counter_match_skips_domain_even_with_stale_crc():
    """Mid-combat reconnect: the client tracked token_state_revision from live
    pushes, so tokens are skipped although its content CRC is long stale."""
    session = _session()
    state = session.to_state_dict_for_role("dm", "dm1")
    known = {
        "tokens": 12345,  # stale CRC from an old sync
        "token_state_revision": 7,  # but the live counter is current
    }
    apply_reconnect_delta(state, known)
    assert "tokens" in state["omitted_domains"]
    assert "tokens" not in state
    # fog/inventory had no matching counter or crc → still sent.
    assert "fog" not in state["omitted_domains"]
    assert "fog_maps" in state


def test_counter_mismatch_resends_domain():
    session = _session()
    state = session.to_state_dict_for_role("dm", "dm1")
    known = {"token_state_revision": 6, "tokens": 1}  # both stale
    apply_reconnect_delta(state, known)
    assert "tokens" not in state["omitted_domains"]
    assert "tokens" in state


def test_inventory_domain_omission_is_atomic_with_loot_log():
    session = _session()
    baseline = session.to_state_dict_for_role("player", "p1")
    known = compute_domain_revisions(baseline)
    state = session.to_state_dict_for_role("player", "p1")
    apply_reconnect_delta(state, known)
    assert "inventory" in state["omitted_domains"]
    for key in ("player_inventory", "party_stash", "player_gold", "party_loot_log"):
        assert key not in state


class _CaptureManager:
    def __init__(self):
        self.sent = []

    async def send_to(self, session_id, user_id, message):
        self.sent.append((user_id, message))
        return True


def test_handle_request_state_reconnect_delta_and_fresh_full(monkeypatch):
    from server.handlers.content import handle_request_state

    session = _session()
    dm = session.users["dm1"]
    captured = _CaptureManager()
    monkeypatch.setattr("server.handlers.content.manager", captured)

    # Fresh request (no revisions): full payload.
    anyio.run(handle_request_state, {"reason": "reconnect"}, session, dm)
    full = next(m for _, m in captured.sent if m["type"] == "state_sync")["payload"]
    assert full["omitted_domains"] == []
    assert "tokens" in full and "char_profiles" in full

    # Reconnect with current revisions: heavy domains omitted.
    captured.sent.clear()
    anyio.run(handle_request_state, {"reason": "reconnect", "known_revisions": full["domain_revisions"]}, session, dm)
    delta = next(m for _, m in captured.sent if m["type"] == "state_sync")["payload"]
    assert set(delta["omitted_domains"]) == set(DELTA_DOMAIN_KEYS.keys())
    assert "tokens" not in delta and "char_profiles" not in delta
    # Snapshot v2 still follows the state_sync.
    assert any(m.get("type") == "authoritative_snapshot" for _, m in captured.sent)
