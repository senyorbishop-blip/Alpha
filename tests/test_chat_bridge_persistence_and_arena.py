"""Tests for chat-participant persistence modes, arena character sanitization,
bridge request correlation, and chat_bridge role isolation."""
import time

from server.session import Session, User, ChatParticipant
from server.persistence_schema import _serialize_chat_participants
from server.handlers.chat_bridge import (
    _sanitize_arena_character,
    _echo_req,
    _find_participant_key_by_user_id,
)
from server.handlers.ws_permissions import (
    is_ws_message_allowed_for_role,
    CHAT_BRIDGE_ALLOWED_MESSAGE_TYPES,
)


def _participant(**kwargs):
    defaults = dict(
        twitch_username="dave",
        display_name="Dave",
        joined_at=time.time(),
        inventory=[{"name": "Fireball Scroll", "qty": 1}],
        twitch_user_id="12345",
        character_name="Grognak",
        lifetime_stats={"sessions_joined": 3, "items_used": 2, "damage_dealt": 40, "kills": 1},
        arena_stats={"wins": 5, "losses": 2, "arena_gold": 120},
        arena_character={"class": "Barbarian", "level": 4, "gold": 120},
    )
    defaults.update(kwargs)
    return ChatParticipant(**defaults)


# ---------------------------------------------------------------------------
# Persistence modes
# ---------------------------------------------------------------------------

def test_serialize_everything_keeps_full_record_and_mode():
    out = _serialize_chat_participants({"dave": _participant()}, "everything")
    assert out["__config__"] == {"persistence_mode": "everything"}
    rec = out["dave"]
    assert rec["inventory"] == [{"name": "Fireball Scroll", "qty": 1}]
    assert rec["lifetime_stats"]["damage_dealt"] == 40
    assert rec["arena_character"]["class"] == "Barbarian"
    assert rec["twitch_user_id"] == "12345"


def test_serialize_stats_mode_drops_inventory_keeps_stats():
    out = _serialize_chat_participants({"dave": _participant()}, "stats")
    rec = out["dave"]
    assert rec["inventory"] == []
    assert rec["lifetime_stats"]["sessions_joined"] == 3
    assert rec["arena_stats"]["wins"] == 5
    assert rec["character_name"] == "Grognak"


def test_serialize_nothing_mode_persists_only_config():
    out = _serialize_chat_participants({"dave": _participant()}, "nothing")
    assert list(out.keys()) == ["__config__"]
    assert out["__config__"]["persistence_mode"] == "nothing"


def test_serialize_invalid_mode_falls_back_to_everything():
    out = _serialize_chat_participants({"dave": _participant()}, "bogus")
    assert out["__config__"]["persistence_mode"] == "everything"
    assert "dave" in out


def test_restore_round_trips_stats_and_arena_character():
    from server.restore import _restore_chat_participants

    serialized = _serialize_chat_participants({"dave": _participant()}, "everything")
    session = Session(id="t1", player_invite="P", viewer_invite="V", created_at=0)
    _restore_chat_participants(session, {"chat_participants": serialized})

    assert "__config__" not in session.chat_participants
    restored = session.chat_participants["dave"]
    assert restored.lifetime_stats["damage_dealt"] == 40
    assert restored.arena_character["class"] == "Barbarian"
    assert restored.twitch_user_id == "12345"
    assert restored.character_name == "Grognak"
    assert session.chat_persistence_mode == "everything"


def test_restore_reads_persistence_mode_from_config_entry():
    from server.restore import _restore_chat_participants

    session = Session(id="t2", player_invite="P", viewer_invite="V", created_at=0)
    _restore_chat_participants(session, {"chat_participants": {
        "__config__": {"persistence_mode": "stats"},
    }})
    assert session.chat_persistence_mode == "stats"


# ---------------------------------------------------------------------------
# Arena character sanitization
# ---------------------------------------------------------------------------

def test_sanitize_arena_character_clamps_and_whitelists():
    clean = _sanitize_arena_character({
        "class": "<script>Wizard</script>",
        "level": 9999,
        "xp": -5,
        "gold": 2.5,
        "abilities": {"str": 99, "dex": "nope", "con": 14},
        "items": [
            {"name": "Axe<script>", "slot": "weapon", "rarity": "RARE",
             "bonuses": {"str": 99, "hp": -99, "bogus": 5}},
            {"no_name": True},
        ],
        "equipped": {"weapon": "axe1", "hat": "x"},
        "unknown_field": "dropped",
    })
    assert "<" not in clean["class"] and ">" not in clean["class"]
    assert clean["level"] == 99
    assert clean["xp"] == 0
    assert clean["gold"] == 2
    assert clean["abilities"]["str"] == 30
    assert clean["abilities"]["dex"] == 10  # non-numeric → default
    assert clean["abilities"]["con"] == 14
    assert len(clean["items"]) == 1
    item = clean["items"][0]
    assert "<" not in item["name"] and ">" not in item["name"]
    assert item["bonuses"]["str"] == 10
    assert item["bonuses"]["hp"] == -10
    assert "bogus" not in item["bonuses"]
    assert clean["equipped"]["weapon"] == "axe1"
    assert set(clean["equipped"].keys()) == {"weapon", "armor", "trinket"}
    assert "unknown_field" not in clean


def test_sanitize_arena_character_rejects_non_dict():
    assert _sanitize_arena_character(None) is None
    assert _sanitize_arena_character("junk") is None


def test_sanitize_arena_character_caps_item_count():
    raw = {"items": [{"name": f"i{i}", "slot": "trinket"} for i in range(80)]}
    clean = _sanitize_arena_character(raw)
    assert len(clean["items"]) == 50


# ---------------------------------------------------------------------------
# Bridge request correlation + identity
# ---------------------------------------------------------------------------

def test_echo_req_copies_request_id():
    out = _echo_req({"_req_id": "br_1_99"}, {"success": True})
    assert out["_req_id"] == "br_1_99"


def test_echo_req_absent_when_not_supplied():
    out = _echo_req({}, {"success": True})
    assert "_req_id" not in out


def test_find_participant_by_user_id_matches_renamed_user():
    session = Session(id="t3", player_invite="P", viewer_invite="V", created_at=0)
    session.chat_participants["oldname"] = _participant(twitch_username="oldname")
    assert _find_participant_key_by_user_id(session, "12345") == "oldname"
    assert _find_participant_key_by_user_id(session, "999") is None
    assert _find_participant_key_by_user_id(session, "") is None


# ---------------------------------------------------------------------------
# Role isolation: the bridge can only do chat-participant/arena actions
# ---------------------------------------------------------------------------

def test_chat_bridge_role_cannot_touch_campaign_or_dm_actions():
    for msg_type in (
        "token_move", "token_delete", "combat_update", "fog_paint",
        "dm_chat_participant_reset", "dm_chat_persistence_mode",
        "dm_grant_chat_participant_item", "dm_chat_bridge_kill_switch",
        "map_settings_save", "inventory_update_item_weight",
    ):
        assert not is_ws_message_allowed_for_role(msg_type, "chat_bridge").allowed, msg_type


def test_chat_bridge_role_allows_arena_persistence_types():
    for msg_type in (
        "chat_participant_arena_load",
        "chat_participant_arena_sync",
        "chat_participant_arena_leaderboard",
    ):
        assert msg_type in CHAT_BRIDGE_ALLOWED_MESSAGE_TYPES
        assert is_ws_message_allowed_for_role(msg_type, "chat_bridge").allowed


def test_overlay_role_is_read_only():
    for msg_type in ("chat_participant_join", "token_move", "poll_create", "chat_message"):
        assert not is_ws_message_allowed_for_role(msg_type, "overlay").allowed, msg_type
