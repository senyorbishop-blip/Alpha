import asyncio

from server.session import Session, User, get_player_inventory_for_user, get_player_gold_for_user
from server.handlers import inventory as inventory_handlers


class _FakeManager:
    def __init__(self):
        self.sent = []

    def get_session_connections(self, _session_id):
        return {"p1": object(), "dm1": object()}

    async def send_to(self, session_id, user_id, message):
        self.sent.append((session_id, user_id, message))

    async def broadcast(self, session_id, message):
        self.sent.append((session_id, "*", message))


def _build_session_with_player(name: str = "Hero"):
    session = Session(id="equip-session")
    player = User(id="p1", name=name, role="player")
    dm = User(id="dm1", name="DM", role="dm")
    session.users[player.id] = player
    session.users[dm.id] = dm
    session.char_profiles = {
        "hero": [{"id": "prof-1", "name": name, "charBook": {"abilities": {"dex": {"score": 14}}}}]
    }
    return session, player, dm


def _patch_runtime(monkeypatch):
    fake_manager = _FakeManager()

    async def _fake_save(_session):
        return None

    monkeypatch.setattr(inventory_handlers, "manager", fake_manager)
    monkeypatch.setattr(inventory_handlers, "save_campaign_async", _fake_save)
    return fake_manager


def _equip(session, player, idx):
    asyncio.run(inventory_handlers.handle_inventory_equip_item({"item_index": idx}, session, player))


def _unequip(session, player, idx):
    asyncio.run(inventory_handlers.handle_inventory_unequip_item({"item_index": idx}, session, player))


def test_equip_one_armor_item(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [{"name": "Leather", "qty": 1, "equipment_kind": "armor", "armor_type": "light", "base_ac": 11}]}

    _equip(session, player, 0)
    item = get_player_inventory_for_user(session, player.id)[0]
    assert item["equipped"] is True
    assert item["equip_slot"] == "armor"


def test_second_armor_is_blocked(monkeypatch):
    fake = _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [
        {"name": "Leather", "qty": 1, "equipment_kind": "armor", "armor_type": "light", "base_ac": 11},
        {"name": "Chain Shirt", "qty": 1, "equipment_kind": "armor", "armor_type": "medium", "base_ac": 13},
    ]}

    _equip(session, player, 0)
    _equip(session, player, 1)

    items = get_player_inventory_for_user(session, player.id)
    assert items[0]["equipped"] is True
    assert not items[1].get("equipped")
    assert any("Unequip your current armor first" in m[2].get("payload", {}).get("message", "") for m in fake.sent)


def test_sword_and_shield_valid(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [
        {"name": "Longsword", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
        {"name": "Shield", "qty": 1, "equipment_kind": "shield", "handedness": "shield", "ac_bonus": 2},
    ]}

    _equip(session, player, 0)
    _equip(session, player, 1)
    items = get_player_inventory_for_user(session, player.id)
    assert items[0]["equip_slot"] == "main_hand"
    assert items[1]["equip_slot"] == "shield"


def test_dual_daggers_valid(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [
        {"name": "Dagger A", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
        {"name": "Dagger B", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
    ]}

    _equip(session, player, 0)
    _equip(session, player, 1)
    items = get_player_inventory_for_user(session, player.id)
    slots = {items[0].get("equip_slot"), items[1].get("equip_slot")}
    assert slots == {"main_hand", "off_hand"}


def test_two_handed_weapon_alone_valid(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [{"name": "Greatsword", "qty": 1, "equipment_kind": "weapon", "handedness": "two_handed"}]}

    _equip(session, player, 0)
    item = get_player_inventory_for_user(session, player.id)[0]
    assert item["equipped"] is True


def test_two_handed_plus_shield_blocked(monkeypatch):
    fake = _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [
        {"name": "Shield", "qty": 1, "equipment_kind": "shield", "handedness": "shield", "ac_bonus": 2},
        {"name": "Greatsword", "qty": 1, "equipment_kind": "weapon", "handedness": "two_handed"},
    ]}

    _equip(session, player, 0)
    _equip(session, player, 1)
    items = get_player_inventory_for_user(session, player.id)
    assert items[0]["equipped"] is True
    assert not items[1].get("equipped")
    assert any("two-handed weapon while a shield is equipped" in m[2].get("payload", {}).get("message", "") for m in fake.sent)


def test_two_handed_plus_dagger_blocked(monkeypatch):
    fake = _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [
        {"name": "Dagger", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
        {"name": "Greatsword", "qty": 1, "equipment_kind": "weapon", "handedness": "two_handed"},
    ]}

    _equip(session, player, 0)
    _equip(session, player, 1)
    items = get_player_inventory_for_user(session, player.id)
    assert items[0]["equipped"] is True
    assert not items[1].get("equipped")
    assert any("Unequip your other weapon first" in m[2].get("payload", {}).get("message", "") for m in fake.sent)


def test_third_weapon_blocked(monkeypatch):
    fake = _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [
        {"name": "Sword", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
        {"name": "Dagger", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
        {"name": "Mace", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
    ]}

    _equip(session, player, 0)
    _equip(session, player, 1)
    _equip(session, player, 2)

    items = get_player_inventory_for_user(session, player.id)
    assert not items[2].get("equipped")
    assert any("at most two weapons" in m[2].get("payload", {}).get("message", "") for m in fake.sent)


def test_unequip_frees_slot(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [
        {"name": "Sword", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
        {"name": "Dagger", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
        {"name": "Mace", "qty": 1, "equipment_kind": "weapon", "handedness": "one_handed"},
    ]}

    _equip(session, player, 0)
    _equip(session, player, 1)
    _unequip(session, player, 1)
    _equip(session, player, 2)

    items = get_player_inventory_for_user(session, player.id)
    assert items[2].get("equipped") is True


def test_ac_light_armor_and_no_armor_fallback(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [{"name": "Leather", "qty": 1, "equipment_kind": "armor", "armor_type": "light", "base_ac": 11}]}

    base = inventory_handlers._calculate_ac_for_user(session, player, get_player_inventory_for_user(session, player.id))
    _equip(session, player, 0)
    armored = inventory_handlers._calculate_ac_for_user(session, player, get_player_inventory_for_user(session, player.id))

    assert base == 12
    assert armored == 13


def test_ac_medium_and_heavy(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [
        {"name": "Chain Shirt", "qty": 1, "equipment_kind": "armor", "armor_type": "medium", "base_ac": 13},
        {"name": "Plate", "qty": 1, "equipment_kind": "armor", "armor_type": "heavy", "base_ac": 18},
    ]}

    _equip(session, player, 0)
    ac_medium = inventory_handlers._calculate_ac_for_user(session, player, get_player_inventory_for_user(session, player.id))
    _unequip(session, player, 0)
    _equip(session, player, 1)
    ac_heavy = inventory_handlers._calculate_ac_for_user(session, player, get_player_inventory_for_user(session, player.id))

    assert ac_medium == 15
    assert ac_heavy == 18


def test_shield_adds_ac_bonus(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [{"name": "Shield", "qty": 1, "equipment_kind": "shield", "handedness": "shield", "ac_bonus": 2}]}

    _equip(session, player, 0)
    ac_value = inventory_handlers._calculate_ac_for_user(session, player, get_player_inventory_for_user(session, player.id))
    assert ac_value == 14


def test_player_cannot_add_item_or_gold_directly(monkeypatch):
    fake = _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()

    asyncio.run(inventory_handlers.handle_inventory_add_item({"entry": {"name": "Torch", "qty": 1}}, session, player))
    asyncio.run(inventory_handlers.handle_inventory_add_gold({"amount": "10 gp"}, session, player))
    asyncio.run(inventory_handlers.handle_inventory_remove_gold({"amount": "1 gp"}, session, player))

    assert get_player_inventory_for_user(session, player.id) == []
    assert any("Only the DM can add items directly." in m[2].get("payload", {}).get("message", "") for m in fake.sent)
    assert any("Only the DM can add gold directly." in m[2].get("payload", {}).get("message", "") for m in fake.sent)
    assert any("Only the DM can remove gold directly." in m[2].get("payload", {}).get("message", "") for m in fake.sent)


def test_dm_can_add_item_and_adjust_gold(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, dm = _build_session_with_player()

    asyncio.run(inventory_handlers.handle_inventory_add_item(
        {"entry": {"name": "Torch", "qty": 2}},
        session,
        dm,
    ))
    asyncio.run(inventory_handlers.handle_inventory_add_gold(
        {"amount": "10 gp", "target_user_id": player.id},
        session,
        dm,
    ))
    asyncio.run(inventory_handlers.handle_inventory_remove_gold(
        {"amount": "3 gp", "target_user_id": player.id},
        session,
        dm,
    ))

    items = get_player_inventory_for_user(session, dm.id)
    assert len(items) == 1
    assert items[0]["name"] == "Torch"
    assert items[0]["qty"] == 2
    assert get_player_gold_for_user(session, player.id) == 700


def test_item_type_weapon_can_equip_without_equipment_kind(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [{"name": "Longsword", "qty": 1, "item_type": "weapon", "handedness": "one_handed"}]}

    _equip(session, player, 0)
    item = get_player_inventory_for_user(session, player.id)[0]
    assert item["equipped"] is True
    assert item["equipment_kind"] == "weapon"


def test_item_type_shield_infers_handedness(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [{"name": "+1 Shield", "qty": 1, "item_type": "shield", "ac_bonus": 1}]}

    _equip(session, player, 0)
    item = get_player_inventory_for_user(session, player.id)[0]
    assert item["equipped"] is True
    assert item["equipment_kind"] == "shield"
    assert item["handedness"] == "shield"


def test_name_only_weapon_can_equip_via_inference(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [{"name": "Longsword", "qty": 1}]}

    _equip(session, player, 0)
    item = get_player_inventory_for_user(session, player.id)[0]
    assert item["equipped"] is True
    assert item["equipment_kind"] == "weapon"


def test_name_only_armor_can_equip_via_inference(monkeypatch):
    _patch_runtime(monkeypatch)
    session, player, _ = _build_session_with_player()
    session.player_inventories = {"hero": [{"name": "Leather Armor", "qty": 1, "base_ac": 11, "armor_type": "light"}]}

    _equip(session, player, 0)
    item = get_player_inventory_for_user(session, player.id)[0]
    assert item["equipped"] is True
    assert item["equipment_kind"] == "armor"
