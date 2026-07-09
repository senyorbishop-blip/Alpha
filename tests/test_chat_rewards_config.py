"""Tests for the DM-configurable Twitch reward tables (sub/gift/bits →
viewer-power loot tables) and the Chat Party kick action."""
import asyncio
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server.session import Session, User, ChatParticipant
from server.handlers.chat_bridge import (
    _default_chat_rewards_config,
    _normalize_chat_rewards_config,
    _chat_rewards_config,
    _resolved_chat_rewards_payload,
    handle_dm_chat_rewards_update,
    handle_chat_bridge_rewards_get,
    handle_dm_chat_participant_reset,
)
from server.handlers.viewer_powers import VIEWER_BASE_POWER_DEFS


def _patch_manager(monkeypatch):
    import server.handlers.common as common_mod
    sent = []

    async def _broadcast(session_id, message, exclude_user=None):
        sent.append(("broadcast", message))

    async def _send_to(session_id, user_id, message):
        sent.append(("send_to", message))

    monkeypatch.setattr(common_mod.manager, "broadcast", _broadcast)
    monkeypatch.setattr(common_mod.manager, "send_to", _send_to)

    async def _noop_save(session):
        return None

    import server.handlers.chat_bridge as cb_mod
    monkeypatch.setattr(cb_mod, "save_campaign_async", _noop_save)
    return sent


def _session():
    session = Session(id="rw-1")
    dm = User(id="dm1", name="DM", role="dm")
    bridge = User(id="chat_bridge", name="chat_bridge", role="chat_bridge")
    session.users[dm.id] = dm
    session.users[bridge.id] = bridge
    session.dm_id = dm.id
    return session, dm, bridge


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_default_config_ships_prefilled_tables():
    config = _default_chat_rewards_config()
    assert config["sub"], "sub table must ship pre-filled"
    assert [t["min_count"] for t in config["gift_tiers"]] == [1, 5, 10]
    assert [t["threshold"] for t in config["bits_tiers"]] == [100, 500, 1000]
    for tier in config["gift_tiers"] + config["bits_tiers"]:
        assert tier["table"], "every default tier must have powers"


def test_default_config_uses_only_known_powers():
    config = _default_chat_rewards_config()
    tables = [config["sub"]]
    tables += [t["table"] for t in config["gift_tiers"]]
    tables += [t["table"] for t in config["bits_tiers"]]
    for table in tables:
        for entry in table:
            assert entry["power_id"] in VIEWER_BASE_POWER_DEFS


def test_default_top_tiers_cover_all_powers():
    config = _default_chat_rewards_config()
    all_ids = set(VIEWER_BASE_POWER_DEFS.keys())
    top_gift = {e["power_id"] for e in config["gift_tiers"][-1]["table"]}
    top_bits = {e["power_id"] for e in config["bits_tiers"][-1]["table"]}
    assert top_gift == all_ids
    assert top_bits == all_ids


def test_default_sub_table_is_support_flavored():
    config = _default_chat_rewards_config()
    sub_ids = {e["power_id"] for e in config["sub"]}
    assert "healing_spark" in sub_ids
    assert "give_potion" in sub_ids
    # No DM-approval-heavy nukes in the single-sub table
    assert "fireball" not in sub_ids
    assert "chain_lightning" not in sub_ids


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalize_drops_unknown_powers_and_clamps_weights():
    session, _, _ = _session()
    raw = {
        "sub": [
            {"power_id": "healing_spark", "weight": 99999},
            {"power_id": "not_a_power", "weight": 5},
            {"power_id": "fireball", "weight": -3},
            "garbage",
        ],
        "gift_tiers": [{"min_count": 0, "table": [{"power_id": "arcane_zap", "weight": 2}]}],
        "bits_tiers": "nope",
    }
    out = _normalize_chat_rewards_config(session, raw)
    sub_ids = [e["power_id"] for e in out["sub"]]
    assert sub_ids == ["healing_spark", "fireball"]
    assert out["sub"][0]["weight"] == 1000     # clamped high
    assert out["sub"][1]["weight"] == 1        # clamped low
    assert out["gift_tiers"][0]["min_count"] == 1  # clamped to >= 1
    assert out["bits_tiers"] == []


def test_normalize_rejects_non_dict():
    session, _, _ = _session()
    assert _normalize_chat_rewards_config(session, None) is None
    assert _normalize_chat_rewards_config(session, "x") is None


def test_config_falls_back_to_defaults_when_unset():
    session, _, _ = _session()
    session.chat_rewards_config = {}
    config = _chat_rewards_config(session)
    assert config["sub"]
    assert len(config["gift_tiers"]) == 3


def test_resolved_payload_includes_power_names_and_catalog():
    session, _, _ = _session()
    payload = _resolved_chat_rewards_payload(session)
    assert payload["config"]["sub"][0]["name"]
    ids = {p["power_id"] for p in payload["available_powers"]}
    assert set(VIEWER_BASE_POWER_DEFS.keys()) <= ids
    for p in payload["available_powers"]:
        assert p["name"]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def test_dm_update_persists_and_broadcasts_to_bridge(monkeypatch):
    sent = _patch_manager(monkeypatch)
    session, dm, bridge = _session()

    new_config = {
        "sub": [{"power_id": "pebble_toss", "weight": 1}],
        "gift_tiers": [{"min_count": 1, "table": [{"power_id": "fireball", "weight": 1}]}],
        "bits_tiers": [{"threshold": 200, "table": [{"power_id": "arcane_zap", "weight": 1}]}],
    }
    asyncio.run(handle_dm_chat_rewards_update({"config": new_config}, session, dm))

    # Persisted on the session (campaign persistence picks this up)
    assert [e["power_id"] for e in session.chat_rewards_config["sub"]] == ["pebble_toss"]

    # Broadcast so the live bridge refreshes without a restart
    broadcast_types = [m["type"] for kind, m in sent if kind == "broadcast"]
    assert "chat_rewards_updated" in broadcast_types

    # Bridge fetch returns the updated, name-resolved config
    sent.clear()
    asyncio.run(handle_chat_bridge_rewards_get({"_req_id": "r1"}, session, bridge))
    kind, msg = sent[0]
    assert msg["type"] == "chat_bridge_rewards_result"
    assert msg["payload"]["_req_id"] == "r1"
    sub = msg["payload"]["config"]["sub"]
    assert sub[0]["power_id"] == "pebble_toss"
    assert sub[0]["name"] == "Pebble Toss"


def test_dm_update_reset_restores_defaults(monkeypatch):
    sent = _patch_manager(monkeypatch)
    session, dm, _ = _session()
    session.chat_rewards_config = {
        "version": 1,
        "sub": [{"power_id": "pebble_toss", "weight": 1}],
        "gift_tiers": [],
        "bits_tiers": [],
    }
    asyncio.run(handle_dm_chat_rewards_update({"reset": True}, session, dm))
    assert session.chat_rewards_config == {}
    config = _chat_rewards_config(session)
    assert len(config["gift_tiers"]) == 3


def test_dm_update_invalid_config_errors(monkeypatch):
    sent = _patch_manager(monkeypatch)
    session, dm, _ = _session()
    asyncio.run(handle_dm_chat_rewards_update({"config": "junk"}, session, dm))
    assert any(m["type"] == "error" for _, m in sent)
    assert session.chat_rewards_config == {}


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def test_rewards_config_persists_and_restores():
    from server.persistence_schema import extract_persistable_campaign_state
    from server.restore import restore_session_from_db

    session, _, _ = _session()
    session.chat_rewards_config = {
        "version": 1,
        "sub": [{"power_id": "healing_spark", "weight": 7}],
        "gift_tiers": [],
        "bits_tiers": [],
    }
    data = extract_persistable_campaign_state(session)
    assert data["chat_rewards_config"]["sub"][0]["weight"] == 7

    data.update({
        "id": "rw-2", "player_invite": "P", "viewer_invite": "V",
        "created_at": 0, "dm_name": "DM", "dm_id": "dm1", "name": "Test",
        "tokens": [],
    })
    restored, _dm_id = restore_session_from_db(data)
    assert restored.chat_rewards_config["sub"][0]["power_id"] == "healing_spark"


# ---------------------------------------------------------------------------
# Chat Party kick
# ---------------------------------------------------------------------------

def test_kick_scope_marks_inactive_and_broadcasts_left(monkeypatch):
    sent = _patch_manager(monkeypatch)
    session, dm, _ = _session()
    session.chat_participants["viewer1"] = ChatParticipant(
        twitch_username="viewer1", display_name="Viewer1", joined_at=0.0,
        inventory=[{"name": "Fireball", "qty": 1}],
    )

    asyncio.run(handle_dm_chat_participant_reset(
        {"twitch_username": "viewer1", "scope": "kick"}, session, dm))

    participant = session.chat_participants["viewer1"]
    assert participant.is_active is False
    # Kick keeps the record (inventory intact) so the chatter can rejoin
    assert participant.inventory == [{"name": "Fireball", "qty": 1}]
    broadcast_types = [m["type"] for kind, m in sent if kind == "broadcast"]
    assert "chat_participant_left" in broadcast_types
