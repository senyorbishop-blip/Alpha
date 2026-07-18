"""Rewards panel ⇄ VIEWER_POWER_DEFS sync.

The Stream panel's rewards checklist, the DM grant dropdowns, and the viewer
panel all render from server power defs — never a hardcoded frontend list.
These tests pin down:
  * state_sync carries the FULL merged power catalog (shipped + custom),
  * pre-new-power saved reward configs load cleanly (missing powers are
    simply unticked, and still offered in available_powers),
  * ticking new powers round-trips to the bridge payload with the counts the
    bridge logs in "[rewards] Applied server reward config",
  * default tier placement of the five new powers,
  * a REWARDED (loot-granted, not DM-granted) Lightning Bolt copy resolves
    the full direction follow-up flow.
"""
import asyncio
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server.session import Session, User, Token
from server.handlers.chat_bridge import (
    _default_chat_rewards_config,
    _chat_rewards_config,
    _resolved_chat_rewards_payload,
    handle_dm_chat_rewards_update,
    handle_chat_bridge_rewards_get,
    handle_chat_bridge_loot_grant,
    handle_chat_participant_target,
)
from server.handlers.viewer_powers import VIEWER_BASE_POWER_DEFS

NEW_POWERS = {"magic_missile", "shield", "ray_of_frost", "sleep", "lightning_bolt"}


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
    session = Session(id="rw-sync-1")
    dm = User(id="dm1", name="DM", role="dm")
    bridge = User(id="chat_bridge", name="chat_bridge", role="chat_bridge")
    session.users[dm.id] = dm
    session.users[bridge.id] = bridge
    session.dm_id = dm.id
    session.dm_map_context = "world"
    return session, dm, bridge


def _table_ids(table):
    return {e["power_id"] for e in table}


# ---------------------------------------------------------------------------
# state_sync carries the full merged catalog (single source of truth)
# ---------------------------------------------------------------------------

def test_state_sync_carries_full_power_catalog():
    session, _, _ = _session()
    catalog = session.to_state_dict()["viewer_power_catalog"]
    assert set(VIEWER_BASE_POWER_DEFS.keys()) <= set(catalog.keys())
    for pid in NEW_POWERS:
        assert catalog[pid]["name"]
        assert catalog[pid]["description"]


def test_state_sync_catalog_merges_custom_powers():
    session, _, _ = _session()
    session.viewer_power_catalog = {
        "chaos_bolt": {"name": "Chaos Bolt", "kind": "single_damage",
                       "dice": [2, 8], "description": "Custom chaos."},
    }
    catalog = session.to_state_dict()["viewer_power_catalog"]
    assert "chaos_bolt" in catalog
    assert catalog["chaos_bolt"]["custom"] is True
    # Shipped powers ride alongside, and are NOT flagged custom
    assert "lightning_bolt" in catalog
    assert not catalog["lightning_bolt"].get("custom")


def test_role_scoped_state_sync_carries_catalog_for_all_roles():
    session, dm, _ = _session()
    viewer = User(id="v1", name="Viewer", role="viewer")
    player = User(id="p1", name="Player", role="player")
    session.users[viewer.id] = viewer
    session.users[player.id] = player
    for role, uid in (("dm", dm.id), ("viewer", viewer.id), ("player", player.id)):
        catalog = session.to_state_dict_for_role(role, uid)["viewer_power_catalog"]
        assert set(VIEWER_BASE_POWER_DEFS.keys()) <= set(catalog.keys()), role


def test_persisted_catalog_still_stores_only_custom_powers():
    from server.persistence_schema import extract_persistable_campaign_state
    session, _, _ = _session()
    session.viewer_power_catalog = {
        "chaos_bolt": {"name": "Chaos Bolt", "kind": "single_damage",
                       "dice": [2, 8], "description": "Custom chaos."},
    }
    data = extract_persistable_campaign_state(session)
    assert set(data["viewer_power_catalog"].keys()) == {"chaos_bolt"}


# ---------------------------------------------------------------------------
# Old saved configs (pre-new-powers) load cleanly
# ---------------------------------------------------------------------------

OLD_CONFIG = {
    "version": 1,
    "sub": [{"power_id": "healing_spark", "weight": 30},
            {"power_id": "give_potion", "weight": 15}],
    "gift_tiers": [
        {"min_count": 1, "table": [{"power_id": "healing_spark", "weight": 25}]},
        {"min_count": 10, "table": [{"power_id": "fireball", "weight": 10}]},
    ],
    "bits_tiers": [
        {"threshold": 100, "table": [{"power_id": "pebble_toss", "weight": 40}]},
    ],
}


def test_old_saved_config_loads_with_new_powers_unticked():
    session, _, _ = _session()
    session.chat_rewards_config = dict(OLD_CONFIG)
    payload = _resolved_chat_rewards_payload(session)

    # The saved tables come back exactly as saved — new powers simply absent
    config = payload["config"]
    assert _table_ids(config["sub"]) == {"healing_spark", "give_potion"}
    for tier in config["gift_tiers"] + config["bits_tiers"]:
        assert not (_table_ids(tier["table"]) & NEW_POWERS)

    # …but the checklist still offers every power, with name + description
    # (the panel's hover tooltip)
    available = {p["power_id"]: p for p in payload["available_powers"]}
    for pid in NEW_POWERS:
        assert pid in available
        assert available[pid]["name"]
        assert available[pid]["description"]


def test_ticking_new_powers_round_trips_to_bridge_with_counts(monkeypatch):
    sent = _patch_manager(monkeypatch)
    session, dm, bridge = _session()
    session.chat_rewards_config = dict(OLD_CONFIG)

    # DM ticks Lightning Bolt in gift ×10 and Sleep in bits 100+ and saves
    updated = {
        "version": 1,
        "sub": OLD_CONFIG["sub"],
        "gift_tiers": [
            {"min_count": 1, "table": [{"power_id": "healing_spark", "weight": 25}]},
            {"min_count": 10, "table": [{"power_id": "fireball", "weight": 10},
                                        {"power_id": "lightning_bolt", "weight": 1}]},
        ],
        "bits_tiers": [
            {"threshold": 100, "table": [{"power_id": "pebble_toss", "weight": 40},
                                         {"power_id": "sleep", "weight": 1}]},
        ],
    }
    asyncio.run(handle_dm_chat_rewards_update({"config": updated}, session, dm))

    # The bridge refresh broadcast and a fresh fetch both carry the new powers
    sent.clear()
    asyncio.run(handle_chat_bridge_rewards_get({"_req_id": "r9"}, session, bridge))
    payload = sent[0][1]["payload"]
    config = payload["config"]
    # These lengths are exactly what the bridge logs in
    # "[rewards] Applied server reward config (sub: N powers, gift tiers: N, …)"
    assert len(config["sub"]) == 2
    assert len(config["gift_tiers"]) == 2
    assert len(config["bits_tiers"]) == 1
    x10 = config["gift_tiers"][-1]["table"]
    assert {"fireball", "lightning_bolt"} == _table_ids(x10)
    lb = next(e for e in x10 if e["power_id"] == "lightning_bolt")
    assert lb["name"] == "Lightning Bolt"
    assert _table_ids(config["bits_tiers"][0]["table"]) == {"pebble_toss", "sleep"}


# ---------------------------------------------------------------------------
# Default tier placement of the new powers
# ---------------------------------------------------------------------------

def test_default_placement_of_new_powers():
    config = _default_chat_rewards_config()
    gift = {t["min_count"]: _table_ids(t["table"]) for t in config["gift_tiers"]}
    bits = {t["threshold"]: _table_ids(t["table"]) for t in config["bits_tiers"]}
    sub = _table_ids(config["sub"])

    # Shield: support-ish tiers
    assert "shield" in sub
    assert "shield" in gift[1]

    # Magic Missile / Ray of Frost: mid tiers
    assert {"magic_missile", "ray_of_frost"} <= gift[5]
    assert {"magic_missile", "ray_of_frost"} <= bits[500]

    # Lightning Bolt and Sleep: strongest control/line powers — ONLY in
    # gift ×10 and bits 1000+ (which cover every power)
    for pid in ("lightning_bolt", "sleep"):
        assert pid in gift[10]
        assert pid in bits[1000]
        assert pid not in sub
        assert pid not in gift[1]
        assert pid not in gift[5]
        assert pid not in bits[100]
        assert pid not in bits[500]


# ---------------------------------------------------------------------------
# A REWARDED Lightning Bolt copy resolves the full direction flow
# ---------------------------------------------------------------------------

def _add_token(session, tid, name, x=100, y=100):
    token = Token(id=tid, name=name, x=x, y=y, width=40, height=40,
                  color="#f00", shape="circle", owner_id=None, hp=60, max_hp=60)
    session.tokens[token.id] = token
    return token


def test_rewarded_lightning_bolt_rolls_and_resolves_direction_flow(monkeypatch):
    sent = _patch_manager(monkeypatch)
    session, dm, bridge = _session()
    goblin = _add_token(session, "t1", "Goblin", x=100, y=100)
    skeleton = _add_token(session, "t2", "Skeleton", x=280, y=100)  # on the rightward line

    # DM ticks Lightning Bolt in the gift ×10 tier and saves
    asyncio.run(handle_dm_chat_rewards_update({"config": {
        "version": 1,
        "sub": [],
        "gift_tiers": [{"min_count": 10,
                        "table": [{"power_id": "lightning_bolt", "weight": 1}]}],
        "bits_tiers": [],
    }}, session, dm))

    # Bridge rolls the ×10 gift table (single entry → Lightning Bolt) and
    # grants it like grantPowerReward() does — name + power_id item entry.
    payload = _resolved_chat_rewards_payload(session)
    entry = payload["config"]["gift_tiers"][0]["table"][0]
    assert entry["power_id"] == "lightning_bolt"
    asyncio.run(handle_chat_bridge_loot_grant({
        "twitch_username": "gifter",
        "display_name": "Gifter",
        "item_entry": {"name": entry["name"], "qty": 1, "power_id": entry["power_id"]},
        "trigger": "gift",
    }, session, bridge))
    participant = session.chat_participants["gifter"]
    inv_entry = next(i for i in participant.inventory
                     if str(i.get("name", "")).lower() == "lightning bolt")
    assert inv_entry.get("power_id") == "lightning_bolt"

    def target(**payload):
        asyncio.run(handle_chat_participant_target(payload, session, bridge))
        results = [m for kind, m in sent
                   if m.get("type") == "chat_participant_target_result"]
        return results[-1]["payload"]

    # First use: no direction yet → the multi-step follow-up is requested and
    # nothing is consumed
    result = target(twitch_username="gifter", target_name="Goblin")
    assert result["success"] is False
    assert result["error_code"] == "needs_follow_up"
    assert result["follow_up"]["id"] == "direction"
    assert any(i.get("power_id") == "lightning_bolt" for i in participant.inventory)

    # Answer the follow-up: the bolt tears rightward through both tokens and
    # the rewarded copy is consumed
    result = target(twitch_username="gifter", target_name="Goblin",
                    follow_up={"direction": "right"})
    assert result["success"] is True, result
    assert "tears rightward" in result["message"]
    assert goblin.hp < 60
    assert skeleton.hp < 60
    assert not any(i.get("power_id") == "lightning_bolt" for i in participant.inventory)
