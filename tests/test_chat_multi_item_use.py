"""
tests/test_chat_multi_item_use.py — Multi-item ability selection for chat
participants (!use <item> <target>) and the DM friendly-fire toggle.

Covers the server side of the feature:
  - !target with exactly one usable item keeps the old behavior (uses it)
  - !target with several distinct items refuses to pick silently and returns
    error_code=multiple_items with the held item names
  - optional item_name on chat_participant_target is matched server-side
    against the chatter's own inventory (exact > prefix > fuzzy);
    ambiguous / unowned queries return the relevant item lists
  - hostile powers aimed at party (player-owned / character-bound) tokens are
    blocked while session.chat_friendly_fire is off (the default); support
    powers (heals) always go through
  - the dm_chat_friendly_fire handler flips the flag and broadcasts it
  - the flag round-trips through campaign persistence via __config__
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
    from server.session import Session, User, ChatParticipant
    session = Session(id="chat-use-1")
    dm = User(id="dm1", name="DM", role="dm")
    bridge = User(id="bridge1", name="Bridge", role="chat_bridge")
    player = User(id="player1", name="Alice", role="player")
    for u in (dm, bridge, player):
        session.users[u.id] = u
    session.dm_id = dm.id
    session.dm_map_context = "world"
    return session, dm, bridge, player


def _add_token(session, tid, name, owner_id=None, **kwargs):
    from server.session import Token
    token = Token(id=tid, name=name, x=100, y=100, width=40, height=40,
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


def _target(session, bridge, monkeypatch, sent, **payload):
    from server.handlers.chat_bridge import handle_chat_participant_target
    payload.setdefault("twitch_username", "dave")
    asyncio.run(handle_chat_participant_target(payload, session, bridge))
    results = [m for kind, m in sent if m.get("type") == "chat_participant_target_result"]
    assert results, "no chat_participant_target_result was sent"
    return results[-1]["payload"]


# ---------------------------------------------------------------------------
# !target — one item vs many
# ---------------------------------------------------------------------------

def test_target_single_item_uses_it(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin")
    p = _join(session, items=[{"name": "Pebble", "qty": 1}])

    result = _target(session, bridge, monkeypatch, sent, target_name="Goblin")
    assert result["success"] is True
    assert result["item_name"] == "Pebble"
    assert result["target_name"] == "Goblin"
    assert p.inventory == []  # consumed


def test_target_multiple_items_refuses_to_pick(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin")
    p = _join(session, items=[
        {"name": "Fireball", "qty": 1},
        {"name": "Healing Spark", "qty": 1},
    ])

    result = _target(session, bridge, monkeypatch, sent, target_name="Goblin")
    assert result["success"] is False
    assert result["error_code"] == "multiple_items"
    assert result["items"] == ["Fireball", "Healing Spark"]
    assert len(p.inventory) == 2  # nothing consumed


def test_target_two_stacks_same_name_is_not_ambiguous(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin")
    _join(session, items=[
        {"name": "Pebble", "qty": 1},
        {"name": "Pebble", "qty": 2},
    ])

    result = _target(session, bridge, monkeypatch, sent, target_name="Goblin")
    assert result["success"] is True
    assert result["item_name"] == "Pebble"


# ---------------------------------------------------------------------------
# !use — server-side item matching
# ---------------------------------------------------------------------------

def test_use_exact_and_prefix_match(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin")
    p = _join(session, items=[
        {"name": "Fireball", "qty": 1},
        {"name": "Healing Spark", "qty": 1},
    ])

    result = _target(session, bridge, monkeypatch, sent,
                     target_name="Goblin", item_name="fire")
    assert result["success"] is True
    assert result["item_name"] == "Fireball"
    assert [i["name"] for i in p.inventory] == ["Healing Spark"]


def test_use_word_prefix_fuzzy_match(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin")
    _join(session, items=[
        {"name": "Wand of Zaps", "qty": 1},
        {"name": "Fireball", "qty": 1},
    ])

    # "zaps" is not a prefix of the full name but is a word prefix
    result = _target(session, bridge, monkeypatch, sent,
                     target_name="Goblin", item_name="zaps")
    assert result["success"] is True
    assert result["item_name"] == "Wand of Zaps"


def test_use_ambiguous_lists_matching_items(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin")
    p = _join(session, items=[
        {"name": "Wand of Zaps", "qty": 1},
        {"name": "Wand of Fire", "qty": 1},
    ])

    result = _target(session, bridge, monkeypatch, sent,
                     target_name="Goblin", item_name="wand")
    assert result["success"] is False
    assert result["error_code"] == "item_ambiguous"
    assert result["matching_items"] == ["Wand of Zaps", "Wand of Fire"]
    assert len(p.inventory) == 2


def test_use_unowned_lists_owned_items(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin")
    p = _join(session, items=[
        {"name": "Pebble", "qty": 1},
        {"name": "Healing Spark", "qty": 1},
    ])

    result = _target(session, bridge, monkeypatch, sent,
                     target_name="Goblin", item_name="excalibur")
    assert result["success"] is False
    assert result["error_code"] == "item_not_owned"
    assert result["owned_items"] == ["Pebble", "Healing Spark"]
    assert len(p.inventory) == 2


def test_use_exact_name_beats_prefix_ambiguity(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    _add_token(session, "t1", "Goblin")
    _join(session, items=[
        {"name": "Pebble", "qty": 1},
        {"name": "Pebble Pouch", "qty": 1},
    ])

    result = _target(session, bridge, monkeypatch, sent,
                     target_name="Goblin", item_name="pebble")
    assert result["success"] is True
    assert result["item_name"] == "Pebble"


# ---------------------------------------------------------------------------
# Friendly fire
# ---------------------------------------------------------------------------

def test_hostile_item_blocked_on_party_token_by_default(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    party = _add_token(session, "t1", "Elowen", owner_id="player1")
    p = _join(session, items=[{"name": "Pebble", "qty": 1}])

    result = _target(session, bridge, monkeypatch, sent, target_name="Elowen")
    assert result["success"] is False
    assert result["error_code"] == "friendly_fire_blocked"
    assert party.hp == 60
    assert len(p.inventory) == 1  # not consumed


def test_hostile_item_allowed_when_friendly_fire_on(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    party = _add_token(session, "t1", "Elowen", owner_id="player1")
    _join(session, items=[{"name": "Pebble", "qty": 1}])
    session.chat_friendly_fire = True

    result = _target(session, bridge, monkeypatch, sent, target_name="Elowen")
    assert result["success"] is True
    assert party.hp < 60


def test_heal_always_allowed_on_party_token(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    party = _add_token(session, "t1", "Elowen", owner_id="player1")
    party.hp = 30
    _join(session, items=[{"name": "Healing Spark", "qty": 1}])

    result = _target(session, bridge, monkeypatch, sent, target_name="Elowen")
    assert result["success"] is True
    assert party.hp > 30
    assert result["healed"] > 0


def test_heal_targets_chat_character_token_by_name(monkeypatch):
    """A token named after another chatter's character resolves like any
    other visible token, so support powers reach chat characters too."""
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    grognak = _add_token(session, "t1", "Grognak", character_id="chr-42")
    grognak.hp = 10
    _join(session, items=[{"name": "Healing Spark", "qty": 1}])

    result = _target(session, bridge, monkeypatch, sent, target_name="grognak")
    assert result["success"] is True
    assert grognak.hp > 10


def test_hostile_blocked_on_character_bound_token(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    grognak = _add_token(session, "t1", "Grognak", character_id="chr-42")
    _join(session, items=[{"name": "Pebble", "qty": 1}])

    result = _target(session, bridge, monkeypatch, sent, target_name="grognak")
    assert result["success"] is False
    assert result["error_code"] == "friendly_fire_blocked"


def test_hostile_allowed_on_unowned_monster_token(monkeypatch):
    """Generic DM-placed tokens (no owner, no character binding) stay
    attackable even though their token_type defaults to 'player'."""
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()
    goblin = _add_token(session, "t1", "Goblin")
    _join(session, items=[{"name": "Pebble", "qty": 1}])

    result = _target(session, bridge, monkeypatch, sent, target_name="Goblin")
    assert result["success"] is True
    assert goblin.hp < 60


# ---------------------------------------------------------------------------
# DM toggle handler + permissions
# ---------------------------------------------------------------------------

def test_dm_friendly_fire_handler_flips_flag_and_broadcasts(monkeypatch):
    from server.handlers.chat_bridge import handle_dm_chat_friendly_fire
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()

    asyncio.run(handle_dm_chat_friendly_fire({"enabled": True}, session, dm))
    assert session.chat_friendly_fire is True
    statuses = [m for kind, m in sent if m.get("type") == "chat_bridge_status"]
    assert statuses and statuses[-1]["payload"]["friendly_fire"] is True

    asyncio.run(handle_dm_chat_friendly_fire({"enabled": False}, session, dm))
    assert session.chat_friendly_fire is False


def test_kill_switch_piggyback_sets_friendly_fire(monkeypatch):
    from server.handlers.chat_bridge import handle_dm_chat_bridge_kill_switch
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, dm, bridge, player = _make_session()

    asyncio.run(handle_dm_chat_bridge_kill_switch(
        {"paused": False, "friendly_fire": True}, session, dm))
    assert session.chat_friendly_fire is True
    statuses = [m for kind, m in sent if m.get("type") == "chat_bridge_status"]
    assert statuses[-1]["payload"]["friendly_fire"] is True


def test_friendly_fire_toggle_is_dm_only():
    from server.handlers.ws_permissions import is_ws_message_allowed_for_role
    assert is_ws_message_allowed_for_role("dm_chat_friendly_fire", "dm").allowed
    assert not is_ws_message_allowed_for_role("dm_chat_friendly_fire", "chat_bridge").allowed
    assert not is_ws_message_allowed_for_role("dm_chat_friendly_fire", "player").allowed
    assert not is_ws_message_allowed_for_role("dm_chat_friendly_fire", "viewer").allowed


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_friendly_fire_round_trips_through_persistence():
    from server.persistence_schema import _serialize_chat_participants
    from server.restore import _restore_chat_participants
    from server.session import Session

    serialized = _serialize_chat_participants({}, "everything", friendly_fire=True)
    assert serialized["__config__"]["friendly_fire"] is True

    session = Session(id="ff-restore", created_at=0)
    _restore_chat_participants(session, {"chat_participants": serialized})
    assert session.chat_friendly_fire is True

    serialized_off = _serialize_chat_participants({}, "everything")
    session2 = Session(id="ff-restore-2", created_at=0)
    _restore_chat_participants(session2, {"chat_participants": serialized_off})
    assert session2.chat_friendly_fire is False
