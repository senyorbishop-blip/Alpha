import asyncio

from server.handlers import camp_rest
from server.handlers import common as common_handlers
from server.handlers import inventory as inventory_handlers
from server.session import Session, Token, User, normalize_profile_owner_key, build_quick_actions_sync_payload


class CaptureManager:
    def __init__(self):
        self.sent = []
        self.broadcasts = []

    def get_session_connections(self, _session_id):
        return {"dm": object(), "p1": object(), "p2": object(), "v1": object()}

    async def send_to(self, session_id, user_id, message):
        self.sent.append((session_id, user_id, message))

    async def broadcast(self, session_id, message):
        self.broadcasts.append((session_id, message))


async def _noop_save(_session):
    return None


def _token(token_id, owner_id, hp=3, max_hp=10, temp_hp=4):
    return Token(
        id=token_id,
        name=f"Token {token_id}",
        x=0,
        y=0,
        width=50,
        height=50,
        color="#fff",
        shape="circle",
        owner_id=owner_id,
        hp=hp,
        max_hp=max_hp,
        temp_hp=temp_hp,
    )


def _session():
    session = Session(id="rest-session")
    dm = User(id="dm", name="DM", role="dm")
    player = User(id="p1", name="Alice", role="player")
    other = User(id="p2", name="Bob", role="player")
    viewer = User(id="v1", name="Viewer", role="viewer")
    session.users = {u.id: u for u in (dm, player, other, viewer)}
    session.tokens = {
        "alice-1": _token("alice-1", "p1", hp=2, max_hp=12, temp_hp=5),
        "alice-2": _token("alice-2", "p1", hp=1, max_hp=9, temp_hp=0),
        "bob-1": _token("bob-1", "p2", hp=4, max_hp=11, temp_hp=3),
    }
    return session, player, other, dm, viewer


def _patch(monkeypatch):
    capture = CaptureManager()
    monkeypatch.setattr(camp_rest, "manager", capture)
    monkeypatch.setattr(common_handlers, "manager", capture)
    monkeypatch.setattr(inventory_handlers, "manager", capture)
    monkeypatch.setattr(camp_rest, "save_campaign_async", _noop_save)
    return capture


def _messages(capture, msg_type):
    return [m for _, _, m in capture.sent if m.get("type") == msg_type] + [m for _, m in capture.broadcasts if m.get("type") == msg_type]


def test_player_long_rest_restores_owned_token_hp_to_max_and_clears_temp_hp(monkeypatch):
    capture = _patch(monkeypatch)
    session, player, *_ = _session()

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "long", "token_id": "alice-1"}, session, player))

    assert session.tokens["alice-1"].hp == 12
    assert session.tokens["alice-1"].temp_hp == 0
    assert _messages(capture, "character_rest_applied")


def test_player_long_rest_broadcasts_tokens_sync(monkeypatch):
    capture = _patch(monkeypatch)
    session, player, *_ = _session()

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "long", "token_id": "alice-1"}, session, player))

    token_syncs = _messages(capture, "tokens_sync")
    assert token_syncs
    assert any("alice-1" in (msg["payload"].get("tokens") or {}) for msg in token_syncs)


def test_player_long_rest_refreshes_item_charges_and_broadcasts_inventory_if_changed(monkeypatch):
    capture = _patch(monkeypatch)
    session, player, *_ = _session()
    owner_key = normalize_profile_owner_key(player.name) or player.id
    session.player_inventories = {
        owner_key: [{
            "name": "Wand of Sparks",
            "qty": 1,
            "charges_max": 7,
            "charges_current": 2,
            "recharge_type": "long_rest",
            "granted_spells": [{"id": "spark", "name": "Spark", "charge_cost": 1, "consume_spell_slot": False}],
        }]
    }

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "long", "token_id": "alice-1"}, session, player))

    assert session.player_inventories[owner_key][0]["charges_current"] == 7
    assert _messages(capture, "player_inventory_sync")


def test_player_short_rest_without_hit_dice_does_not_heal_hp(monkeypatch):
    _patch(monkeypatch)
    session, player, *_ = _session()

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "short", "token_id": "alice-1"}, session, player))

    assert session.tokens["alice-1"].hp == 2


def test_player_short_rest_with_valid_token_id_applies_healing_only_to_that_token(monkeypatch):
    capture = _patch(monkeypatch)
    session, player, *_ = _session()

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "short", "token_id": "alice-2", "healed_amount": 5}, session, player))

    assert session.tokens["alice-2"].hp == 6
    assert session.tokens["alice-1"].hp == 2
    applied = _messages(capture, "character_rest_applied")
    assert applied[-1]["payload"]["token_id"] == "alice-2"


def test_player_cannot_rest_another_players_token(monkeypatch):
    capture = _patch(monkeypatch)
    session, player, *_ = _session()

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "long", "token_id": "bob-1"}, session, player))

    assert session.tokens["bob-1"].hp == 4
    assert session.tokens["bob-1"].temp_hp == 3
    assert not _messages(capture, "character_rest_applied")
    assert _messages(capture, "notification")


def test_non_player_roles_cannot_call_character_rest(monkeypatch):
    capture = _patch(monkeypatch)
    session, _player, _other, dm, viewer = _session()

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "long", "token_id": "alice-1"}, session, dm))
    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "long", "token_id": "alice-1"}, session, viewer))

    assert session.tokens["alice-1"].hp == 2
    assert not capture.sent
    assert not capture.broadcasts


def _attach_active_profile(session, player, *, current_hp=2, max_hp=12, slots=None, item_charges=1):
    owner_key = normalize_profile_owner_key(player.name) or player.id
    profile_id = "char-alice"
    session.active_char_profiles = {player.id: profile_id}
    session.char_profiles = {owner_key: [{
        "id": profile_id,
        "name": "Alice Hero",
        "curhp": current_hp,
        "hp": max_hp,
        "tempHp": 3,
        "charBook": {"currentHp": current_hp, "maxHp": max_hp, "tempHp": 3},
        "charSheet": {"hp": {"current": current_hp, "max": max_hp, "temp": 3}},
        "nativeCharacter": {"spellState": {"slots": slots or {"1": 0, "2": 0}, "slotMaxes": {"1": 4, "2": 2}}},
        "nativeRuntime": {"hp": {"current": current_hp, "max": max_hp, "temp": 3}, "resources": [{"id": "second_wind", "current": 0, "max": 1, "recharge": "short_rest"}]},
    }]}
    session.player_inventories = {f"{owner_key}::profile::{profile_id}": [{
        "name": "Pearl of Power",
        "qty": 1,
        "charges_max": 3,
        "charges_current": item_charges,
        "recharge_type": "long_rest",
        "granted_spells": [{"id": "spark", "name": "Spark", "charge_cost": 1, "consume_spell_slot": False}],
    }]}
    return owner_key, profile_id


def test_player_long_rest_updates_profile_hp_and_restores_spell_slots_server_side(monkeypatch):
    capture = _patch(monkeypatch)
    session, player, *_ = _session()
    owner_key, _profile_id = _attach_active_profile(session, player)

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "long", "token_id": "alice-1"}, session, player))

    profile = session.char_profiles[owner_key][0]
    assert profile["curhp"] == 12
    assert profile["tempHp"] == 0
    assert profile["nativeRuntime"]["hp"]["current"] == 12
    assert profile["nativeCharacter"]["spellState"]["slots"] == {"1": 4, "2": 2}
    assert _messages(capture, "char_profiles_sync")
    assert _messages(capture, "quick_actions_sync")


def test_reconnect_snapshot_after_long_rest_has_restored_slots_and_item_charges(monkeypatch):
    _patch(monkeypatch)
    session, player, *_ = _session()
    owner_key, profile_id = _attach_active_profile(session, player, item_charges=0)

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "long", "token_id": "alice-1"}, session, player))

    # Reconnect snapshots intentionally carry profile stubs. Verify that the
    # stub survives the snapshot while the authoritative full profile keeps
    # the restored spell-slot state for the on-demand profile fetch path.
    snapshot = session.to_state_dict()
    stub = next(row for row in snapshot["char_profiles"][owner_key] if row["id"] == profile_id)
    assert stub["id"] == profile_id
    profile = next(row for row in session.char_profiles[owner_key] if row["id"] == profile_id)
    assert profile["nativeCharacter"]["spellState"]["slots"]["1"] == 4
    assert session.player_inventories[f"{owner_key}::profile::{profile_id}"][0]["charges_current"] == 3
    assert build_quick_actions_sync_payload(session, player.id)["charges"][0]["charges_current"] == 3


def test_short_rest_hit_dice_healing_updates_profile_and_quick_actions(monkeypatch):
    capture = _patch(monkeypatch)
    session, player, *_ = _session()
    owner_key, _profile_id = _attach_active_profile(session, player)

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "short", "token_id": "alice-1", "healed_amount": 5}, session, player))

    assert session.tokens["alice-1"].hp == 7
    profile = session.char_profiles[owner_key][0]
    assert profile["curhp"] == 7
    assert profile["nativeRuntime"]["hp"]["current"] == 7
    assert profile["nativeRuntime"]["resources"][0]["current"] == 1
    assert _messages(capture, "quick_actions_sync")


def test_dm_long_rest_updates_all_player_profiles_spell_slots_and_item_charges(monkeypatch):
    capture = _patch(monkeypatch)
    session, player, other, dm, _viewer = _session()
    owner_key, _profile_id = _attach_active_profile(session, player, item_charges=0)
    bob_key = normalize_profile_owner_key(other.name) or other.id
    session.active_char_profiles[other.id] = "bob-char"
    session.char_profiles[bob_key] = [{
        "id": "bob-char",
        "name": "Bob Hero",
        "nativeCharacter": {"spellState": {"slots": {"1": 0}, "slotMaxes": {"1": 2}}},
        "nativeRuntime": {"hp": {"current": 4, "max": 11, "temp": 3}},
    }]

    asyncio.run(camp_rest.handle_camp_rest_take_rest({"rest_type": "long"}, session, dm))

    assert session.tokens["alice-1"].hp == 12
    assert session.tokens["bob-1"].hp == 11
    assert session.char_profiles[owner_key][0]["nativeCharacter"]["spellState"]["slots"]["1"] == 4
    assert session.char_profiles[bob_key][0]["nativeCharacter"]["spellState"]["slots"]["1"] == 2
    assert session.player_inventories[f"{owner_key}::profile::char-alice"][0]["charges_current"] == 3
    assert _messages(capture, "camp_rest_rest_applied")
    assert _messages(capture, "char_profiles_sync")


def test_short_rest_absolute_hp_contract_is_idempotent_after_prior_token_vitals(monkeypatch):
    capture = _patch(monkeypatch)
    session, player, *_ = _session()
    owner_key, _profile_id = _attach_active_profile(session, player)
    session.tokens["alice-1"].hp = 7

    asyncio.run(camp_rest.handle_character_self_rest({
        "rest_type": "short",
        "token_id": "alice-1",
        "healed_amount": 5,
        "current_hp": 7,
        "temp_hp": 5,
        "hit_dice_state": {"total": 5, "available": 4, "dieSize": 10, "spent": 1},
    }, session, player))

    assert session.tokens["alice-1"].hp == 7
    assert session.tokens["alice-1"].temp_hp == 5
    profile = session.char_profiles[owner_key][0]
    assert profile["nativeRuntime"]["hp"]["current"] == 7
    assert profile["charSheet"]["_hitDiceState"] == {"total": 5, "available": 4, "dieSize": 10, "spent": 1}
    applied = _messages(capture, "character_rest_applied")
    assert applied[-1]["payload"]["hp"] == 7


def test_long_rest_recharges_dawn_and_daily_items_for_self_rest(monkeypatch):
    _patch(monkeypatch)
    session, player, *_ = _session()
    owner_key, profile_id = _attach_active_profile(session, player, item_charges=0)
    inv_key = f"{owner_key}::profile::{profile_id}"
    session.player_inventories[inv_key] = [
        {"name": "Dawn Wand", "qty": 1, "charges_max": 4, "charges_current": 0, "recharge_type": "dawn", "granted_spells": [{"id": "spark"}]},
        {"name": "Daily Charm", "qty": 1, "charges_max": 2, "charges_current": 0, "recharge_type": "daily", "granted_spells": [{"id": "spark"}]},
    ]

    asyncio.run(camp_rest.handle_character_self_rest({"rest_type": "long", "token_id": "alice-1"}, session, player))

    assert session.player_inventories[inv_key][0]["charges_current"] == 4
    assert session.player_inventories[inv_key][1]["charges_current"] == 2
    quick = build_quick_actions_sync_payload(session, player.id)
    assert {row["name"]: row["charges_current"] for row in quick["charges"]} == {"Dawn Wand": 4, "Daily Charm": 2}
