import pytest

from server.handlers import inventory as inventory_handlers
from server.handlers import content as content_handlers
from server.session import Session, User, Token, normalize_profile_owner_key


def _setup_player_with_profile(class_name: str = "Wizard", dex_score: int = 14):
    session = Session(id="s-inv")
    user = User(id="u1", name="Arcane Hero", role="player")
    session.users[user.id] = user
    owner_key = normalize_profile_owner_key(user.name) or user.id
    session.char_profiles = {
        owner_key: [{
            "id": "profile-1",
            "name": "Arcane Hero",
            "updated_at": 10.0,
            "charBook": {
                "abilities": {"dex": {"score": dex_score}},
                "classes": [{"name": class_name, "level": 5}],
            },
            "charSheet": {},
        }]
    }
    return session, user


def _set_inventory(session: Session, user: User, items: list[dict]):
    session.player_inventories = {normalize_profile_owner_key(user.name) or user.id: items}


def test_no_armor_uses_ten_plus_dex_mod():
    session, user = _setup_player_with_profile("Wizard", dex_score=14)
    _set_inventory(session, user, [])
    assert inventory_handlers._calculate_ac_for_user(session, user, []) == 12


def test_light_armor_uses_full_dex():
    session, user = _setup_player_with_profile("Rogue", dex_score=16)
    items = [{"name": "Leather Armor", "equipment_kind": "armor", "armor_type": "light", "base_ac": 11, "equipped": True}]
    assert inventory_handlers._calculate_ac_for_user(session, user, items) == 14


def test_medium_armor_uses_dex_cap():
    session, user = _setup_player_with_profile("Ranger", dex_score=18)
    items = [{"name": "Scale Mail", "equipment_kind": "armor", "armor_type": "medium", "base_ac": 14, "equipped": True}]
    assert inventory_handlers._calculate_ac_for_user(session, user, items) == 16


def test_heavy_armor_ignores_dex():
    session, user = _setup_player_with_profile("Fighter", dex_score=18)
    items = [{"name": "Chain Mail", "equipment_kind": "armor", "armor_type": "heavy", "base_ac": 16, "equipped": True}]
    assert inventory_handlers._calculate_ac_for_user(session, user, items) == 16


def test_shield_adds_bonus():
    session, user = _setup_player_with_profile("Fighter", dex_score=14)
    items = [
        {"name": "Chain Mail", "equipment_kind": "armor", "armor_type": "heavy", "base_ac": 16, "equipped": True},
        {"name": "Shield", "equipment_kind": "shield", "ac_bonus": 2, "equipped": True},
    ]
    assert inventory_handlers._calculate_ac_for_user(session, user, items) == 18


def test_armor_bonus_without_base_ac_preserves_existing_ac_baseline():
    session, user = _setup_player_with_profile("Wizard", dex_score=10)
    owner_key = normalize_profile_owner_key(user.name) or user.id
    session.char_profiles[owner_key][0]["ac"] = 15
    session.char_profiles[owner_key][0]["charSheet"] = {"ac": 15}
    items = [
        {"name": "Mystic Bracers", "equipment_kind": "armor", "ac_bonus": 2, "equipped": True},
    ]
    assert inventory_handlers._calculate_ac_for_user(session, user, items) == 17


def test_armor_bonus_without_base_ac_uses_owned_token_baseline_when_profile_flag_is_stale():
    session, user = _setup_player_with_profile("Wizard", dex_score=10)
    owner_key = normalize_profile_owner_key(user.name) or user.id
    session.char_profiles[owner_key][0]["ac"] = 19
    session.char_profiles[owner_key][0]["ac_from_equipment"] = True
    session.char_profiles[owner_key][0]["charSheet"] = {"ac": 19}
    session.tokens = {
        "tok-ac": Token(
            id="tok-ac",
            name="Arcane Hero",
            x=0.0,
            y=0.0,
            width=40.0,
            height=40.0,
            color="#fff",
            shape="circle",
            owner_id=user.id,
            ac=19,
            ac_from_equipment=False,
        )
    }
    items = [
        {"name": "Mystic Ward", "equipment_kind": "armor", "ac_bonus": 2, "equipped": True},
    ]
    assert inventory_handlers._calculate_ac_for_user(session, user, items) == 21


def test_wizard_cannot_equip_heavy_armor_by_default():
    session, user = _setup_player_with_profile("Wizard")
    _set_inventory(session, user, [{"name": "Chain Mail", "equipment_kind": "armor", "armor_type": "heavy", "base_ac": 16}])
    ok, msg = inventory_handlers._equip_item(session, user, 0)
    assert ok is False
    assert "heavy armor" in msg.lower() or "chain mail" in msg.lower()


def test_wizard_cannot_equip_shield_by_default():
    session, user = _setup_player_with_profile("Wizard")
    _set_inventory(session, user, [{"name": "Shield", "equipment_kind": "shield", "ac_bonus": 2}])
    ok, msg = inventory_handlers._equip_item(session, user, 0)
    assert ok is False
    assert "shield" in msg.lower()


def test_fighter_can_equip_heavy_armor_and_shield():
    session, user = _setup_player_with_profile("Fighter")
    _set_inventory(session, user, [
        {"name": "Chain Mail", "equipment_kind": "armor", "armor_type": "heavy", "base_ac": 16},
        {"name": "Shield", "equipment_kind": "shield", "ac_bonus": 2},
    ])
    ok_armor, _ = inventory_handlers._equip_item(session, user, 0)
    ok_shield, _ = inventory_handlers._equip_item(session, user, 1)
    assert ok_armor is True
    assert ok_shield is True


def test_rogue_can_equip_light_but_not_heavy_by_default():
    session, user = _setup_player_with_profile("Rogue")
    _set_inventory(session, user, [
        {"name": "Leather", "equipment_kind": "armor", "armor_type": "light", "base_ac": 11},
        {"name": "Chain Mail", "equipment_kind": "armor", "armor_type": "heavy", "base_ac": 16},
    ])
    ok_light, _ = inventory_handlers._equip_item(session, user, 0)
    ok_heavy, msg_heavy = inventory_handlers._equip_item(session, user, 1)
    assert ok_light is True
    assert ok_heavy is False
    assert "heavy armor" in msg_heavy.lower() or "chain mail" in msg_heavy.lower()


def test_equipping_and_unequipping_updates_ac():
    session, user = _setup_player_with_profile("Fighter", dex_score=14)
    _set_inventory(session, user, [{"name": "Chain Mail", "equipment_kind": "armor", "armor_type": "heavy", "base_ac": 16}])
    ac_before = inventory_handlers._recompute_equipment_effects(session, user)
    assert ac_before == 12
    ok, _ = inventory_handlers._equip_item(session, user, 0)
    assert ok is True
    ac_after = inventory_handlers._recompute_equipment_effects(session, user)
    assert ac_after == 16
    ok_un, _ = inventory_handlers._unequip_item(session, user, 0)
    assert ok_un is True
    ac_reset = inventory_handlers._recompute_equipment_effects(session, user)
    assert ac_reset == 12


@pytest.mark.anyio
async def test_server_blocks_invalid_equip_attempt(monkeypatch):
    session, user = _setup_player_with_profile("Wizard")
    _set_inventory(session, user, [{"name": "Chain Mail", "equipment_kind": "armor", "armor_type": "heavy", "base_ac": 16}])
    sent = []

    async def _send_to(*args, **kwargs):
        sent.append((args, kwargs))
        return None

    monkeypatch.setattr(inventory_handlers.manager, "send_to", _send_to)
    await inventory_handlers.handle_inventory_equip_item({"item_index": 0}, session, user)
    assert sent
    last_payload = sent[-1][0][2]
    assert last_payload["type"] == "inventory_action_result"
    assert "trained" in str(last_payload["payload"]["message"]).lower() or "chain mail" in str(last_payload["payload"]["message"]).lower()


def test_legacy_item_without_equipment_metadata_does_not_crash():
    session, user = _setup_player_with_profile("Wizard")
    legacy_item = {"name": "Old Rope", "qty": 1, "notes": "legacy"}
    _set_inventory(session, user, [legacy_item])
    ok, msg = inventory_handlers._equip_item(session, user, 0)
    assert ok is False
    assert "cannot be equipped" in msg.lower()
    ac_value = inventory_handlers._recompute_equipment_effects(session, user)
    assert isinstance(ac_value, int)


def test_recompute_without_equipped_armor_respects_profile_ac_override():
    session, user = _setup_player_with_profile("Wizard", dex_score=10)
    owner_key = normalize_profile_owner_key(user.name) or user.id
    # Manual sheet AC can differ from 10+dex and should not be forced down.
    session.char_profiles[owner_key][0]["ac"] = 16
    session.char_profiles[owner_key][0]["charSheet"] = {"ac": 16}
    # Gear entries may still carry equipment metadata from imports.
    _set_inventory(session, user, [{"name": "Rope", "equipment_kind": "gear", "equipped": True}])

    ac_value = inventory_handlers._recompute_equipment_effects(session, user)
    assert ac_value == 16


@pytest.mark.anyio
async def test_equip_armor_broadcasts_updated_token_ac(monkeypatch):
    """Equipping armor must push the new AC onto the owned token and broadcast token state."""
    session, user = _setup_player_with_profile("Fighter", dex_score=14)
    # Give the player an owned token with a stale AC of 10
    tok = Token(id="tok1", name="Hero", x=0.0, y=0.0, width=40.0, height=40.0,
                color="#f00", shape="circle", owner_id=user.id, ac=10)
    session.tokens = {"tok1": tok}
    _set_inventory(session, user, [
        {"name": "Chain Mail", "equipment_kind": "armor", "armor_type": "heavy", "base_ac": 16}
    ])

    tokens_synced = []

    async def _send_to(sid, uid, msg):
        if msg.get("type") == "tokens_sync":
            tokens_synced.append(msg)

    monkeypatch.setattr(inventory_handlers.manager, "send_to", _send_to)
    monkeypatch.setattr(inventory_handlers.manager, "get_session_connections", lambda _sid: {user.id: object()})
    await inventory_handlers.handle_inventory_equip_item({"item_index": 0}, session, user)

    # token.ac in server memory must now equal 16
    assert tok.ac == 16
    # a tokens_sync broadcast must have been sent
    assert tokens_synced, "Expected at least one tokens_sync broadcast after equipping armor"
    # the broadcasted token payload must include the updated AC
    synced_tokens = tokens_synced[-1]["payload"]["tokens"]
    assert "tok1" in synced_tokens
    assert synced_tokens["tok1"].get("ac") == 16


@pytest.mark.anyio
async def test_dex_profile_update_refreshes_token_ac(monkeypatch):
    """Saving a character profile with a higher DEX must update the owned token AC."""
    session, user = _setup_player_with_profile("Rogue", dex_score=14)
    # Player has leather armor already equipped
    _set_inventory(session, user, [
        {"name": "Leather Armor", "equipment_kind": "armor", "armor_type": "light",
         "base_ac": 11, "ac_bonus": 0, "equipped": True}
    ])
    # Token starts with the old AC (DEX 14 → mod +2 → 11+2=13)
    tok = Token(id="tok2", name="Rogue", x=0.0, y=0.0, width=40.0, height=40.0,
                color="#0f0", shape="circle", owner_id=user.id, ac=13)
    session.tokens = {"tok2": tok}

    tokens_synced = []

    async def _send_to(sid, uid, msg):
        if msg.get("type") == "tokens_sync":
            tokens_synced.append(msg)

    monkeypatch.setattr(inventory_handlers.manager, "send_to", _send_to)
    monkeypatch.setattr(inventory_handlers.manager, "get_session_connections", lambda _sid: {user.id: object()})

    # Simulate saving an updated profile with DEX 18 (+4 → 11+4=15)
    owner_key = normalize_profile_owner_key(user.name) or user.id
    updated_profile_payload = {
        "id": "profile-1",
        "name": "Rogue Hero",
        "charBook": {
            "abilities": {"dex": {"score": 18}},
            "classes": [{"name": "Rogue", "level": 5}],
        },
        "charSheet": {},
    }
    await content_handlers.handle_char_profile_upsert(updated_profile_payload, session, user)

    # token.ac must now reflect the new DEX (15)
    assert tok.ac == 15
    # a tokens_sync broadcast must have been sent
    assert tokens_synced, "Expected at least one tokens_sync broadcast after profile DEX update"
    # the broadcasted token payload must include the updated AC
    synced_tokens = tokens_synced[-1]["payload"]["tokens"]
    assert "tok2" in synced_tokens
    assert synced_tokens["tok2"].get("ac") == 15
