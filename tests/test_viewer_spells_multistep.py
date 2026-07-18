"""
tests/test_viewer_spells_multistep.py — The expanded viewer spell set and
multi-step chat interactions (server side).

Covers:
  - Lightning Bolt's follow-up flow on chat_participant_target:
    needs_follow_up spec when no direction was given, alias-normalized
    answers, invalid answers, and line resolution hitting every token in
    the chosen direction
  - friendly-fire off skips PCs standing in the line (and still blocks
    directly targeting a PC with a hostile power) while Shield / Bless /
    heals on PCs always go through
  - Sleep's server-side saving throw: failure applies 'asleep' (with the
    roll announced), success resists (also announced)
  - Shield absorbing the next incoming hit exactly once
  - Ray of Frost dealing damage and applying 'slowed'
  - Magic Missile dealing 3d4+3
  - alias matching for renamed powers (Healing Word / Bless / Grease /
    Fog Cloud) in !use item matching
  - 'slowed' halving and 'asleep' zeroing combat movement speed
  - the chat_bridge_power_info handler + its role permission
"""
import asyncio
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _patch_manager(monkeypatch):
    import server.handlers.common as common_mod
    sent = []

    async def _broadcast(session_id, message, exclude_user=None):
        sent.append(("broadcast", message))

    async def _send_to(session_id, user_id, message):
        sent.append(("send_to", message))

    monkeypatch.setattr(common_mod.manager, "broadcast", _broadcast)
    monkeypatch.setattr(common_mod.manager, "send_to", _send_to)
    return sent


def _patch_save(monkeypatch):
    import server.handlers.chat_bridge as cb
    import server.handlers.viewer_powers as vp

    async def _noop(session):
        return True

    monkeypatch.setattr(cb, "save_campaign_async", _noop)
    monkeypatch.setattr(vp, "save_campaign_async", _noop)


def _make_session():
    from server.session import Session, User
    session = Session(id="spells-1")
    dm = User(id="dm1", name="DM", role="dm")
    bridge = User(id="bridge1", name="Bridge", role="chat_bridge")
    player = User(id="player1", name="Alice", role="player")
    for u in (dm, bridge, player):
        session.users[u.id] = u
    session.dm_id = dm.id
    session.dm_map_context = "world"
    return session, dm, bridge, player


def _add_token(session, tid, name, x=100, y=100, owner_id=None, **kwargs):
    from server.session import Token
    token = Token(id=tid, name=name, x=x, y=y, width=40, height=40,
                  color="#f00", shape="circle", owner_id=owner_id,
                  hp=60, max_hp=60, **kwargs)
    session.tokens[token.id] = token
    return token


def _join(session, username="dave", items=None):
    from server.session import ChatParticipant
    import time
    participant = ChatParticipant(
        twitch_username=username,
        display_name=username.capitalize(),
        joined_at=time.time(),
        inventory=list(items or []),
    )
    session.chat_participants[username] = participant
    return participant


def _target(session, bridge, sent, **payload):
    from server.handlers.chat_bridge import handle_chat_participant_target
    payload.setdefault("twitch_username", "dave")
    asyncio.run(handle_chat_participant_target(payload, session, bridge))
    results = [m for kind, m in sent if m.get("type") == "chat_participant_target_result"]
    assert results, "no chat_participant_target_result was sent"
    return results[-1]["payload"]


# ---------------------------------------------------------------------------
# Lightning Bolt — follow-up flow
# ---------------------------------------------------------------------------

def test_lightning_bolt_without_direction_asks_follow_up(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin")
    p = _join(session, items=[{"name": "Lightning Bolt", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Goblin")
    assert result["success"] is False
    assert result["error_code"] == "needs_follow_up"
    assert result["power_id"] == "lightning_bolt"
    fu = result["follow_up"]
    assert fu["id"] == "direction"
    assert "direction" in fu["prompt"]
    values = [o["value"] for o in fu["options"]]
    assert values == ["left", "right", "up", "down"]
    # aliases (l/r/u/d, arrows) ride along for the bridge's matcher
    assert "r" in fu["options"][1]["aliases"]
    assert len(p.inventory) == 1  # nothing consumed yet


def test_lightning_bolt_line_hits_everything_in_direction(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    # Goblin center (120,120); line extends 6 squares (300px) rightward.
    goblin = _add_token(session, "t1", "Goblin", x=100, y=100)
    skeleton = _add_token(session, "t2", "Skeleton", x=280, y=100)   # center (300,120) — on the line
    bat = _add_token(session, "t3", "Bat", x=100, y=400)             # far below — off the line
    p = _join(session, items=[{"name": "Lightning Bolt", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Goblin",
                     follow_up={"direction": "right"})
    assert result["success"] is True, result
    assert goblin.hp < 60
    assert skeleton.hp < 60
    assert bat.hp == 60
    assert p.inventory == []  # consumed after resolving
    assert "tears rightward" in result["message"]
    assert "Skeleton" in result["message"]
    # The overlay got a real line to draw
    lines = [m for kind, m in sent if m.get("type") == "viewer_fx"
             and m.get("payload", {}).get("effect") == "line_effect"]
    assert lines, "no line_effect fx broadcast"
    fx = lines[-1]["payload"]
    assert fx["x2"] == fx["x1"] + 300  # 6 squares * 50px, rightward
    assert fx["y2"] == fx["y1"]


def test_lightning_bolt_direction_alias_is_normalized(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin")
    _join(session, items=[{"name": "Lightning Bolt", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Goblin",
                     follow_up={"direction": "U"})
    assert result["success"] is True
    assert "upward" in result["message"]
    assert goblin.hp < 60


def test_lightning_bolt_invalid_direction_rejected(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin")
    p = _join(session, items=[{"name": "Lightning Bolt", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Goblin",
                     follow_up={"direction": "sideways"})
    assert result["success"] is False
    assert result["error_code"] == "invalid_follow_up"
    assert goblin.hp == 60
    assert len(p.inventory) == 1


def test_lightning_bolt_line_skips_pcs_when_friendly_fire_off(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin", x=100, y=100)
    elowen = _add_token(session, "t2", "Elowen", x=280, y=100, owner_id="player1")
    _join(session, items=[{"name": "Lightning Bolt", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Goblin",
                     follow_up={"direction": "right"})
    assert result["success"] is True
    assert goblin.hp < 60
    assert elowen.hp == 60  # PC in the line is spared
    assert "Elowen" not in result["message"]


def test_lightning_bolt_line_hits_pcs_when_friendly_fire_on(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin", x=100, y=100)
    elowen = _add_token(session, "t2", "Elowen", x=280, y=100, owner_id="player1")
    _join(session, items=[{"name": "Lightning Bolt", "qty": 1}])
    session.chat_friendly_fire = True

    result = _target(session, bridge, sent, target_name="Goblin",
                     follow_up={"direction": "right"})
    assert result["success"] is True
    assert elowen.hp < 60


def test_lightning_bolt_direct_pc_target_blocked_when_friendly_fire_off(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    elowen = _add_token(session, "t1", "Elowen", owner_id="player1")
    p = _join(session, items=[{"name": "Lightning Bolt", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Elowen")
    assert result["success"] is False
    assert result["error_code"] == "friendly_fire_blocked"
    assert elowen.hp == 60
    assert len(p.inventory) == 1


# ---------------------------------------------------------------------------
# Sleep — server-side save, both outcomes announced with the roll
# ---------------------------------------------------------------------------

def test_sleep_failed_save_applies_asleep_and_announces_roll(monkeypatch):
    import server.handlers.viewer_powers as vp
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    monkeypatch.setattr(vp, "_resolve_save", lambda dc, bonus: (7, False))
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin")
    _join(session, items=[{"name": "Sleep", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Goblin")
    assert result["success"] is True
    assert "asleep" in (goblin.conditions or [])
    assert goblin.condition_timers.get("asleep", 0) > 0
    assert "WIS save 7 vs DC 12" in result["message"]
    assert "asleep" in result["message"]


def test_sleep_successful_save_resists_and_announces_roll(monkeypatch):
    import server.handlers.viewer_powers as vp
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    monkeypatch.setattr(vp, "_resolve_save", lambda dc, bonus: (18, True))
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin")
    _join(session, items=[{"name": "Sleep", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Goblin")
    assert result["success"] is True
    assert "asleep" not in (goblin.conditions or [])
    assert "resists" in result["message"]
    assert "WIS save 18 vs DC 12" in result["message"]


# ---------------------------------------------------------------------------
# Shield — absorbs exactly the next incoming hit
# ---------------------------------------------------------------------------

def test_shield_targets_pc_with_friendly_fire_off(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    elowen = _add_token(session, "t1", "Elowen", owner_id="player1")
    _join(session, items=[{"name": "Shield", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Elowen")
    assert result["success"] is True, result
    assert "shielded" in (elowen.conditions or [])
    assert "absorbed" in result["message"]


def test_shield_absorbs_one_hit_then_expires(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin")
    p = _join(session, items=[
        {"name": "Shield", "qty": 1},
        {"name": "Pebble", "qty": 2},
    ])

    result = _target(session, bridge, sent, target_name="Goblin", item_name="shield")
    assert result["success"] is True
    assert "shielded" in (goblin.conditions or [])

    # First hit: fully absorbed, shield consumed.
    result = _target(session, bridge, sent, target_name="Goblin", item_name="pebble")
    assert result["success"] is True
    assert result["damage"] == 0
    assert goblin.hp == 60
    assert "shielded" not in (goblin.conditions or [])
    assert "shield" in result["message"].lower()

    # Second hit: lands normally.
    result = _target(session, bridge, sent, target_name="Goblin", item_name="pebble")
    assert result["success"] is True
    assert result["damage"] > 0
    assert goblin.hp < 60


def test_expired_shield_does_not_absorb():
    import time
    from server.handlers.common import _apply_damage
    from server.session import Token
    token = Token(id="t", name="Tok", x=0, y=0, width=40, height=40,
                  color="#f00", shape="circle", owner_id=None, hp=60, max_hp=60)
    token.conditions = ["shielded"]
    token.condition_timers = {"shielded": time.time() - 5}
    _apply_damage(token, 10)
    assert token.hp == 50
    assert "shielded" not in token.conditions


# ---------------------------------------------------------------------------
# Ray of Frost / Magic Missile
# ---------------------------------------------------------------------------

def test_ray_of_frost_damages_and_slows(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin")
    _join(session, items=[{"name": "Ray of Frost", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Goblin")
    assert result["success"] is True
    assert result["power_id"] == "ray_of_frost"
    assert 1 <= result["damage"] <= 8
    assert "slowed" in (goblin.conditions or [])
    assert goblin.condition_timers.get("slowed", 0) > 0
    # Condition flash fx went out for the overlay
    flashes = [m for kind, m in sent if m.get("type") == "viewer_fx"
               and m.get("payload", {}).get("effect") == "condition_flash"]
    assert flashes and flashes[-1]["payload"]["condition"] == "slowed"


def test_magic_missile_never_misses_and_rolls_3d4_plus_3(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin")
    _join(session, items=[{"name": "Magic Missile", "qty": 1}])

    result = _target(session, bridge, sent, target_name="Goblin")
    assert result["success"] is True
    assert result["power_id"] == "magic_missile"
    assert 6 <= result["damage"] <= 15
    assert goblin.hp == 60 - result["damage"]


# ---------------------------------------------------------------------------
# Alias matching for renamed powers
# ---------------------------------------------------------------------------

def test_use_matches_dnd_alias_names(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin")
    goblin.hp = 30
    _join(session, items=[
        {"name": "Healing Spark", "qty": 1},
        {"name": "Battle Blessing", "qty": 1},
        {"name": "Goo Burst", "qty": 1},
        {"name": "Smoke Burst", "qty": 1},
    ])

    result = _target(session, bridge, sent, target_name="Goblin", item_name="healing word")
    assert result["success"] is True
    assert result["item_name"] == "Healing Spark"

    result = _target(session, bridge, sent, target_name="Goblin", item_name="bless")
    assert result["success"] is True
    assert result["item_name"] == "Battle Blessing"


def test_use_alias_matching_helper_covers_all_renames():
    from server.handlers.chat_bridge import _match_inventory_item, _power_alias_lookup
    session, dm, bridge, player = _make_session()
    lookup = _power_alias_lookup(session)
    inventory = [
        {"name": "Healing Spark", "qty": 1},
        {"name": "Battle Blessing", "qty": 1},
        {"name": "Goo Burst", "qty": 1},
        {"name": "Smoke Burst", "qty": 1},
    ]
    for query, expected in [
        ("healing word", "Healing Spark"),
        ("bless", "Battle Blessing"),
        ("grease", "Goo Burst"),
        ("fog cloud", "Smoke Burst"),
        ("healing spark", "Healing Spark"),   # primary names still work
    ]:
        item, err = _match_inventory_item(inventory, query, lookup)
        assert err is None, f"{query}: {err}"
        assert item["name"] == expected


def test_alias_named_items_match_primary_query():
    """A loot item granted under the ALIAS name answers to the primary name."""
    from server.handlers.chat_bridge import _match_inventory_item, _power_alias_lookup
    session, dm, bridge, player = _make_session()
    lookup = _power_alias_lookup(session)
    inventory = [{"name": "Bless", "qty": 1}]
    item, err = _match_inventory_item(inventory, "battle blessing", lookup)
    assert err is None
    assert item["name"] == "Bless"


# ---------------------------------------------------------------------------
# Condition integration with combat movement
# ---------------------------------------------------------------------------

def test_slowed_halves_speed_and_asleep_zeroes_it():
    import time
    from server.handlers.combat import _token_speed_ft
    session, dm, bridge, player = _make_session()
    token = _add_token(session, "t1", "Runner")
    token.speed = 30

    assert _token_speed_ft(session, token) == 30

    token.conditions = ["slowed"]
    token.condition_timers = {"slowed": time.time() + 30}
    assert _token_speed_ft(session, token) == 15

    token.conditions = ["asleep"]
    token.condition_timers = {"asleep": time.time() + 30}
    assert _token_speed_ft(session, token) == 0

    # Expired timers are pruned and speed recovers
    token.conditions = ["slowed"]
    token.condition_timers = {"slowed": time.time() - 1}
    assert _token_speed_ft(session, token) == 30
    assert token.conditions == []


# ---------------------------------------------------------------------------
# !powers — power description lookup
# ---------------------------------------------------------------------------

def test_power_info_matches_primary_and_alias_names(monkeypatch):
    from server.handlers.chat_bridge import handle_chat_bridge_power_info
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()

    asyncio.run(handle_chat_bridge_power_info({"name": "bless"}, session, bridge))
    result = [m for kind, m in sent if m.get("type") == "chat_bridge_power_info_result"][-1]["payload"]
    assert result["found"] is True
    assert result["power_id"] == "battle_blessing"
    assert result["name"] == "Battle Blessing"
    assert "Bless" in result["aliases"]
    assert result["description"]

    asyncio.run(handle_chat_bridge_power_info({"name": "lightning bolt"}, session, bridge))
    result = [m for kind, m in sent if m.get("type") == "chat_bridge_power_info_result"][-1]["payload"]
    assert result["found"] is True
    assert result["power_id"] == "lightning_bolt"

    asyncio.run(handle_chat_bridge_power_info({"name": "wishcraft"}, session, bridge))
    result = [m for kind, m in sent if m.get("type") == "chat_bridge_power_info_result"][-1]["payload"]
    assert result["found"] is False


def test_power_info_is_chat_bridge_readable():
    from server.handlers.ws_permissions import is_ws_message_allowed_for_role
    assert is_ws_message_allowed_for_role("chat_bridge_power_info", "chat_bridge").allowed
    assert not is_ws_message_allowed_for_role("chat_bridge_power_info", "viewer").allowed
    assert not is_ws_message_allowed_for_role("chat_bridge_power_info", "player").allowed


# ---------------------------------------------------------------------------
# Reward tables + DM grant surface include the new powers
# ---------------------------------------------------------------------------

def test_new_powers_present_in_defs_and_reward_payload():
    from server.handlers.chat_bridge import _resolved_chat_rewards_payload, _default_chat_rewards_config
    from server.handlers.viewer_powers import VIEWER_BASE_POWER_DEFS
    session, dm, bridge, player = _make_session()

    for pid in ("magic_missile", "shield", "ray_of_frost", "sleep", "lightning_bolt"):
        assert pid in VIEWER_BASE_POWER_DEFS
        assert VIEWER_BASE_POWER_DEFS[pid]["description"]

    payload = _resolved_chat_rewards_payload(session)
    available = {p["power_id"] for p in payload["available_powers"]}
    assert {"magic_missile", "shield", "ray_of_frost", "sleep", "lightning_bolt"} <= available

    config = _default_chat_rewards_config()
    def all_power_ids(cfg):
        out = set()
        for entry in cfg["sub"]:
            out.add(entry["power_id"])
        for tier in cfg["gift_tiers"] + cfg["bits_tiers"]:
            for entry in tier["table"]:
                out.add(entry["power_id"])
        return out
    assert {"magic_missile", "shield", "ray_of_frost", "sleep", "lightning_bolt"} <= all_power_ids(config)
