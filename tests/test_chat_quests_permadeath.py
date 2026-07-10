"""
tests/test_chat_quests_permadeath.py — Chat quests, permadeath, and the
graveyard (server side).

Covers:
  - quest_start stamps SERVER-clock timestamps, enforces one-active-quest,
    the post-quest cooldown, and the dead gate
  - quest_resolve is refereed by the server clock (active vs complete) and
    stamps the cooldown on completion
  - arena_sync preserves the server-owned quest fields (a bridge sync can
    never clear or forge quest timers)
  - arena_load reports server-computed quest/cooldown countdowns
  - !use / !target is blocked while questing or dead
  - arena_death archives the character to the graveyard (capped), writes a
    tombstone with the optional legacy bonus, and resets arena_stats
  - graveyard query, active-quest enumeration for bridge restarts
  - sanitizer clamps for the new sheet fields; graveyard persistence roundtrip
  - chat_bridge role permissions for the new message types
"""
import asyncio
import os
import sys
import time

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

    async def _noop(session):
        return True

    monkeypatch.setattr(cb, "save_campaign_async", _noop)


def _make_session():
    from server.session import Session, User
    session = Session(id="quest-1")
    bridge = User(id="bridge1", name="Bridge", role="chat_bridge")
    session.users[bridge.id] = bridge
    return session, bridge


def _join(session, username="dave", **kwargs):
    from server.session import ChatParticipant
    participant = ChatParticipant(
        twitch_username=username,
        display_name=username.capitalize(),
        joined_at=time.time(),
        **kwargs,
    )
    session.chat_participants[username] = participant
    return participant


def _live_sheet(**overrides):
    sheet = {
        "class": "Barbarian", "level": 4, "xp": 120, "gold": 200,
        "wins": 6, "losses": 2, "best_streak": 4,
        "items": [{"id": "steel_sword", "name": "Steel Sword", "slot": "weapon",
                   "rarity": "uncommon", "bonuses": {"atk": 2}}],
        "equipped": {"weapon": "steel_sword", "armor": None, "trinket": None},
    }
    sheet.update(overrides)
    return sheet


def _last(sent, msg_type):
    results = [m for kind, m in sent if m.get("type") == msg_type]
    assert results, f"no {msg_type} was sent"
    return results[-1]["payload"]


def _run(handler, payload, session, user):
    asyncio.run(handler(payload, session, user))


# ---------------------------------------------------------------------------
# Quest start
# ---------------------------------------------------------------------------

def _start_quest(session, bridge, sent, username="dave", **quest_overrides):
    from server.handlers.chat_bridge import handle_chat_participant_quest_start
    quest = {"id": "sunken_crypt", "name": "The Sunken Crypt", "risk": "hard",
             "duration_s": 600, "cooldown_s": 300}
    quest.update(quest_overrides)
    _run(handle_chat_participant_quest_start,
         {"twitch_username": username, "display_name": username, "quest": quest},
         session, bridge)
    return _last(sent, "chat_participant_quest_start_result")


def test_quest_start_stamps_server_clock(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session)
    p.arena_character = _live_sheet()

    before = time.time()
    result = _start_quest(session, bridge, sent)
    after = time.time()

    assert result["success"] is True
    quest = p.arena_character["quest"]
    assert before <= quest["started_at"] <= after
    assert quest["ends_at"] == quest["started_at"] + 600
    assert quest["id"] == "sunken_crypt"
    assert result["remaining_s"] == 600


def test_quest_start_clamps_duration_and_cooldown(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    _join(session).arena_character = _live_sheet()

    result = _start_quest(session, bridge, sent, duration_s=1, cooldown_s=999_999_999)
    quest = session.chat_participants["dave"].arena_character["quest"]
    assert quest["duration_s"] == 30           # min clamp
    assert quest["cooldown_s"] == 24 * 3600    # max clamp
    assert result["success"] is True


def test_quest_start_rejects_second_quest_and_reports_remaining(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    _join(session).arena_character = _live_sheet()

    assert _start_quest(session, bridge, sent)["success"] is True
    result = _start_quest(session, bridge, sent)
    assert result["success"] is False
    assert result["error_code"] == "already_questing"
    assert 0 < result["remaining_s"] <= 600


def test_quest_start_rejects_finished_but_unresolved_quest(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session)
    p.arena_character = _live_sheet()
    _start_quest(session, bridge, sent)
    # Simulate the quest having ended without the bridge resolving it yet
    p.arena_character["quest"]["ends_at"] = time.time() - 5

    result = _start_quest(session, bridge, sent)
    assert result["error_code"] == "unresolved_quest"


def test_quest_start_respects_cooldown_and_dead_gate(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session)
    p.arena_character = _live_sheet(quest_cooldown_until=time.time() + 120)

    result = _start_quest(session, bridge, sent)
    assert result["error_code"] == "cooldown"
    assert 0 < result["remaining_s"] <= 120

    p.arena_character = {"dead": True, "death_cause": "testing"}
    result = _start_quest(session, bridge, sent)
    assert result["error_code"] == "dead"
    assert "reborn" in result["message"]


def test_quest_start_auto_creates_participant(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()

    result = _start_quest(session, bridge, sent, username="stranger")
    assert result["success"] is True
    assert "stranger" in session.chat_participants
    assert not session.chat_participants["stranger"].is_active


# ---------------------------------------------------------------------------
# Quest resolve (server clock is the referee)
# ---------------------------------------------------------------------------

def test_quest_resolve_active_then_complete_with_cooldown(monkeypatch):
    from server.handlers.chat_bridge import handle_chat_participant_quest_resolve
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session)
    p.arena_character = _live_sheet()
    _start_quest(session, bridge, sent)

    _run(handle_chat_participant_quest_resolve, {"twitch_username": "dave"}, session, bridge)
    result = _last(sent, "chat_participant_quest_resolve_result")
    assert result["status"] == "active"
    assert 0 < result["remaining_s"] <= 600
    assert "quest" in p.arena_character  # untouched

    # Server clock passes ends_at → complete, quest cleared, cooldown stamped
    p.arena_character["quest"]["ends_at"] = time.time() - 1
    _run(handle_chat_participant_quest_resolve, {"twitch_username": "dave"}, session, bridge)
    result = _last(sent, "chat_participant_quest_resolve_result")
    assert result["status"] == "complete"
    assert result["quest"]["id"] == "sunken_crypt"
    assert result["cooldown_s"] == 300
    assert "quest" not in p.arena_character
    assert p.arena_character["quest_cooldown_until"] > time.time()


def test_quest_resolve_idle_when_no_quest(monkeypatch):
    from server.handlers.chat_bridge import handle_chat_participant_quest_resolve
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    _join(session).arena_character = _live_sheet()

    _run(handle_chat_participant_quest_resolve, {"twitch_username": "dave"}, session, bridge)
    assert _last(sent, "chat_participant_quest_resolve_result")["status"] == "idle"


# ---------------------------------------------------------------------------
# Server-owned quest fields survive bridge syncs
# ---------------------------------------------------------------------------

def test_arena_sync_preserves_server_owned_quest_fields(monkeypatch):
    from server.handlers.chat_bridge import handle_chat_participant_arena_sync
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session)
    p.arena_character = _live_sheet()
    _start_quest(session, bridge, sent)
    quest_before = dict(p.arena_character["quest"])

    # Bridge syncs a sheet with NO quest and a forged cooldown — server keeps its own
    forged = _live_sheet(gold=999)
    forged["quest_cooldown_until"] = 1.0
    _run(handle_chat_participant_arena_sync,
         {"twitch_username": "dave", "arena_character": forged}, session, bridge)

    assert p.arena_character["gold"] == 999          # normal fields synced
    assert p.arena_character["quest"] == quest_before  # server quest untouched
    assert "quest_cooldown_until" not in p.arena_character  # forged field dropped


def test_arena_load_reports_server_computed_countdowns(monkeypatch):
    from server.handlers.chat_bridge import handle_chat_participant_arena_load
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session)
    p.arena_character = _live_sheet()
    _start_quest(session, bridge, sent, duration_s=600)

    _run(handle_chat_participant_arena_load, {"twitch_username": "dave"}, session, bridge)
    result = _last(sent, "chat_participant_arena_load_result")
    assert result["found"] is True
    assert 0 < result["quest_remaining_s"] <= 600
    assert result["quest_cooldown_remaining_s"] == 0


# ---------------------------------------------------------------------------
# Busy/dead blocking of campaign item use (!use / !target)
# ---------------------------------------------------------------------------

def test_target_blocked_while_questing_and_dead(monkeypatch):
    from server.handlers.chat_bridge import handle_chat_participant_target
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session, inventory=[{"name": "Pebble", "qty": 1}])
    p.character_name = "Grognak"
    p.arena_character = _live_sheet()
    _start_quest(session, bridge, sent)

    _run(handle_chat_participant_target,
         {"twitch_username": "dave", "target_name": "Goblin"}, session, bridge)
    result = _last(sent, "chat_participant_target_result")
    assert result["success"] is False
    assert result["error_code"] == "questing"
    assert "Grognak is off questing" in result["message"]
    assert p.inventory  # nothing consumed

    # Finished quests no longer block
    p.arena_character["quest"]["ends_at"] = time.time() - 1
    _run(handle_chat_participant_target,
         {"twitch_username": "dave", "target_name": "Goblin"}, session, bridge)
    result = _last(sent, "chat_participant_target_result")
    assert result["error_code"] != "questing"

    p.arena_character = {"dead": True}
    _run(handle_chat_participant_target,
         {"twitch_username": "dave", "target_name": "Goblin"}, session, bridge)
    result = _last(sent, "chat_participant_target_result")
    assert result["error_code"] == "dead"
    assert "!stats" in result["message"]


# ---------------------------------------------------------------------------
# Permadeath: death → graveyard → tombstone
# ---------------------------------------------------------------------------

def _kill(session, bridge, sent, username="dave", **payload):
    from server.handlers.chat_bridge import handle_chat_participant_arena_death
    _run(handle_chat_participant_arena_death,
         {"twitch_username": username, "cause": "fell on the quest 'The Sunken Crypt'", **payload},
         session, bridge)
    return _last(sent, "chat_participant_arena_death_result")


def test_death_archives_to_graveyard_and_writes_tombstone(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session)
    p.character_name = "Grognak"
    p.arena_character = _live_sheet()
    p.arena_stats = {"wins": 6, "losses": 2, "arena_gold": 200}

    result = _kill(session, bridge, sent, legacy_gold=20,
                   heirloom={"name": "Steel Sword", "slot": "weapon", "bonuses": {"atk": 2}})
    assert result["success"] is True
    assert result["graveyard_count"] == 1

    record = p.graveyard[0]
    assert record["class"] == "Barbarian"
    assert record["level"] == 4
    assert record["wins"] == 6
    assert record["character_name"] == "Grognak"
    assert "Sunken Crypt" in record["cause"]

    tomb = p.arena_character
    assert tomb["dead"] is True
    assert tomb["legacy_gold"] == 20
    assert tomb["heirloom"]["name"] == "Steel Sword"
    assert "class" not in tomb  # the old life is gone
    assert p.arena_stats == {"wins": 0, "losses": 0, "arena_gold": 0}


def test_death_requires_live_character(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session)

    assert _kill(session, bridge, sent)["error_code"] == "no_character"

    p.arena_character = {"dead": True}
    assert _kill(session, bridge, sent)["error_code"] == "already_dead"


def test_graveyard_is_capped(monkeypatch):
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p = _join(session)
    p.graveyard = [{"class": "Rogue", "level": 1, "died_at": i, "cause": "old"}
                   for i in range(25)]
    p.arena_character = _live_sheet()

    result = _kill(session, bridge, sent)
    assert result["graveyard_count"] == 25
    assert p.graveyard[-1]["class"] == "Barbarian"  # newest kept, oldest dropped
    assert p.graveyard[0]["died_at"] == 1


def test_graveyard_query_newest_first(monkeypatch):
    from server.handlers.chat_bridge import handle_chat_participant_graveyard
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p1 = _join(session, username="dave")
    p1.graveyard = [{"class": "Rogue", "level": 3, "wins": 1, "losses": 0,
                     "cause": "goblin ambush", "died_at": 100.0, "character_name": "Sneaky"}]
    p2 = _join(session, username="alice")
    p2.graveyard = [{"class": "Wizard", "level": 7, "wins": 9, "losses": 1,
                     "cause": "the crypt", "died_at": 200.0, "character_name": ""}]

    _run(handle_chat_participant_graveyard, {"limit": 5}, session, bridge)
    entries = _last(sent, "chat_participant_graveyard_result")["entries"]
    assert [e["class"] for e in entries] == ["Wizard", "Rogue"]
    assert entries[1]["character_name"] == "Sneaky"


# ---------------------------------------------------------------------------
# Active-quest enumeration (bridge restart resume)
# ---------------------------------------------------------------------------

def test_active_quests_lists_in_flight_and_overdue(monkeypatch):
    from server.handlers.chat_bridge import handle_chat_bridge_active_quests
    sent = _patch_manager(monkeypatch)
    _patch_save(monkeypatch)
    session, bridge = _make_session()
    p1 = _join(session, username="dave")
    p1.arena_character = _live_sheet()
    _start_quest(session, bridge, sent, username="dave")
    p2 = _join(session, username="alice")
    p2.arena_character = _live_sheet()
    _start_quest(session, bridge, sent, username="alice")
    p2.arena_character["quest"]["ends_at"] = time.time() - 10  # overdue
    _join(session, username="idle").arena_character = _live_sheet()

    _run(handle_chat_bridge_active_quests, {}, session, bridge)
    entries = _last(sent, "chat_bridge_active_quests_result")["entries"]
    by_user = {e["twitch_username"]: e for e in entries}
    assert set(by_user) == {"dave", "alice"}
    assert by_user["dave"]["remaining_s"] > 0
    assert by_user["alice"]["remaining_s"] == 0


# ---------------------------------------------------------------------------
# Sanitizer + persistence
# ---------------------------------------------------------------------------

def test_sanitizer_clamps_new_fields():
    from server.handlers.chat_bridge import _sanitize_arena_character
    clean = _sanitize_arena_character({
        "class": "Wizard", "injuries": -5, "potions": 500,
    })
    assert clean["injuries"] == 0
    assert clean["potions"] == 99
    assert "dead" not in clean

    tomb = _sanitize_arena_character({
        "dead": True, "death_cause": "<b>x</b>" + "y" * 300,
        "legacy_gold": -3, "heirloom": {"name": "Axe", "slot": "weapon"},
    })
    assert tomb["dead"] is True
    assert "<" not in tomb["death_cause"] and len(tomb["death_cause"]) <= 120
    assert tomb["legacy_gold"] == 0
    assert tomb["heirloom"]["name"] == "Axe"


def test_graveyard_persistence_roundtrip():
    from server.persistence_schema import _serialize_chat_participants
    from server.restore import _restore_chat_participants
    from server.session import Session, ChatParticipant

    p = ChatParticipant(
        twitch_username="dave", display_name="Dave", joined_at=1.0,
        graveyard=[{"class": "Monk", "level": 9, "cause": "the crypt", "died_at": 5.0}],
    )
    serialized = _serialize_chat_participants({"dave": p}, "everything")
    assert serialized["dave"]["graveyard"][0]["class"] == "Monk"

    session = Session(id="rt", player_invite="P", viewer_invite="V", created_at=0)
    _restore_chat_participants(session, {"chat_participants": serialized})
    assert session.chat_participants["dave"].graveyard[0]["level"] == 9

    # "stats" mode keeps the legacy record too (it is stats, not inventory)
    serialized_stats = _serialize_chat_participants({"dave": p}, "stats")
    assert serialized_stats["dave"]["graveyard"][0]["class"] == "Monk"


def test_chat_bridge_role_allows_quest_and_death_types():
    from server.handlers.ws_permissions import (
        is_ws_message_allowed_for_role,
        CHAT_BRIDGE_ALLOWED_MESSAGE_TYPES,
    )
    for msg_type in (
        "chat_participant_quest_start",
        "chat_participant_quest_resolve",
        "chat_bridge_active_quests",
        "chat_participant_arena_death",
        "chat_participant_graveyard",
    ):
        assert msg_type in CHAT_BRIDGE_ALLOWED_MESSAGE_TYPES
        assert is_ws_message_allowed_for_role(msg_type, "chat_bridge").allowed
        assert not is_ws_message_allowed_for_role(msg_type, "viewer").allowed
