"""Tests for server action acknowledgements on quick-action mutation streams
(Realtime Sync Engine v1 — follow-up patch to the token_move action_ack).

Covers the server-authoritative quick-action paths a player can trigger from
the combat quick bar / item action picker:

- ``combat_attack_request`` (weapon attack request and spell cast request —
  both share the same handler, distinguished by ``attack_kind``)
- ``inventory_cast_item_spell`` (magic item / wand / staff spell cast,
  spends item charges)
- ``inventory_use_item_action`` (generic item-use action, may spend charges)

Each handler may now receive an optional ``client_action_id`` on its
payload. The server preserves that id only for a sender-only ``action_ack``
reply (confirmed/denied/failed) via the shared ``_send_action_ack`` helper;
it never broadcasts the id to other clients, and it never replaces the
authoritative ``combat_state`` / inventory-sync broadcasts as the source of
truth for state.
"""
import pytest

from server.handlers import combat as combat_handlers
from server.handlers import inventory as inventory_handlers
from server.session import Session, Token, User, normalize_profile_owner_key


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def patched(monkeypatch):
    """Stub out IO (websocket sends/broadcasts and save-to-disk) on both
    handler modules, while capturing every call so tests can assert on the
    exact messages sent.
    """
    sent = []
    broadcasts = []

    async def _fake_send_to(sid, uid, msg):
        sent.append((sid, uid, msg))
        return True

    async def _fake_broadcast(sid, msg):
        broadcasts.append((sid, msg))

    async def _noop(*args, **kwargs):
        return None

    for mod in (combat_handlers, inventory_handlers):
        monkeypatch.setattr(mod.manager, "send_to", _fake_send_to)
        monkeypatch.setattr(mod.manager, "broadcast", _fake_broadcast)

    monkeypatch.setattr(combat_handlers, "_broadcast_combat", _noop)
    monkeypatch.setattr(inventory_handlers, "save_campaign_async", _noop)
    monkeypatch.setattr(inventory_handlers, "_broadcast_token_state_sync", _noop)

    return type("Patched", (), {"sent": sent, "broadcasts": broadcasts})()


def _acks(patched_obj, *, user_id=None, action=None):
    out = []
    for sid, uid, msg in patched_obj.sent:
        if msg.get("type") != "action_ack":
            continue
        if user_id is not None and uid != user_id:
            continue
        payload = msg["payload"]
        if action is not None and payload.get("action") != action:
            continue
        out.append(payload)
    return out


def _build_combat_session():
    session = Session(id="s-qa-combat")
    player = User(id="player-1", name="Player", role="player")
    other = User(id="player-2", name="Other Player", role="player")
    session.users[player.id] = player
    session.users[other.id] = other
    attacker = Token(id="attacker", name="Hero", x=0, y=0, width=1, height=1, color="#fff", shape="circle", owner_id=player.id)
    target = Token(id="target", name="Goblin", x=2, y=2, width=1, height=1, color="#fff", shape="circle", owner_id=other.id)
    session.tokens[attacker.id] = attacker
    session.tokens[target.id] = target
    session.combat = {
        "active": True,
        "turn": 0,
        "combatants": [{"token_id": attacker.id, "owner_id": player.id}],
        "pending_attack": None,
        "revision": 0,
    }
    return session, player, other, attacker, target


def _setup_inventory_session(items):
    session = Session(id="s-qa-inv")
    user = User(id="u-qa", name="Inv Hero", role="player")
    dm = User(id="u-qa-dm", name="DM", role="dm")
    session.users[user.id] = user
    session.users[dm.id] = dm
    owner_key = normalize_profile_owner_key(user.name) or user.id
    session.player_inventories = {owner_key: items}
    return session, user, dm, owner_key


def _make_wand(charges=3, charges_max=3, equipped=True, attuned=True):
    return {
        "name": "Wand of Secrets",
        "id": "wand-of-secrets",
        "item_type": "wand",
        "equipped": equipped,
        "attunement_required": True,
        "attuned": attuned,
        "charges_max": charges_max,
        "charges_current": charges,
        "recharge_type": "long_rest",
        "qty": 1,
        "granted_spells": [
            {"id": "detect-magic", "name": "Detect Magic", "charge_cost": 1, "cast_level": 1},
        ],
    }


# ---------------------------------------------------------------------------
# 1 & 2. Magic item spell cast — with and without client_action_id
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_item_spell_cast_with_client_action_id_sends_confirmed_ack(patched):
    session, user, dm, owner_key = _setup_inventory_session([_make_wand()])
    before_rev = session.inventory_revision

    await inventory_handlers.handle_inventory_cast_item_spell(
        {"item_index": 0, "item_id": "wand-of-secrets", "spell_id": "detect-magic", "client_action_id": "ck-1"},
        session, user,
    )

    acks = _acks(patched, user_id=user.id, action="inventory_cast_item_spell")
    assert len(acks) == 1
    ack = acks[0]
    assert ack["status"] == "confirmed"
    assert ack["client_action_id"] == "ck-1"
    assert ack["item_id"] == "wand-of-secrets"
    assert ack["spell_key"] == "detect-magic"
    assert ack["inventory_revision"] == before_rev + 1 == session.inventory_revision


@pytest.mark.anyio
async def test_item_spell_cast_without_client_action_id_still_works(patched):
    session, user, dm, owner_key = _setup_inventory_session([_make_wand()])

    await inventory_handlers.handle_inventory_cast_item_spell(
        {"item_index": 0, "item_id": "wand-of-secrets", "spell_id": "detect-magic"},
        session, user,
    )

    assert session.player_inventories[owner_key][0]["charges_current"] == 2
    assert _acks(patched) == []


# ---------------------------------------------------------------------------
# 3. Not enough charges -> denied ack with safe reason
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_item_spell_cast_not_enough_charges_sends_denied_ack(patched):
    session, user, dm, owner_key = _setup_inventory_session([_make_wand(charges=0, charges_max=3)])

    await inventory_handlers.handle_inventory_cast_item_spell(
        {"item_index": 0, "item_id": "wand-of-secrets", "spell_id": "detect-magic", "client_action_id": "ck-2"},
        session, user,
    )

    acks = _acks(patched, user_id=user.id, action="inventory_cast_item_spell")
    assert len(acks) == 1
    assert acks[0]["status"] == "denied"
    assert acks[0]["reason"] == "Not enough charges"
    # Position/charges untouched on denial.
    assert session.player_inventories[owner_key][0]["charges_current"] == 0


# ---------------------------------------------------------------------------
# 4. Invalid item -> failed/denied ack with safe reason
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_item_spell_cast_invalid_item_index_sends_failed_ack(patched):
    session, user, dm, owner_key = _setup_inventory_session([])

    await inventory_handlers.handle_inventory_cast_item_spell(
        {"item_index": 5, "item_id": "no-such-item", "spell_id": "detect-magic", "client_action_id": "ck-3"},
        session, user,
    )

    acks = _acks(patched, user_id=user.id, action="inventory_cast_item_spell")
    assert len(acks) == 1
    assert acks[0]["status"] == "failed"
    assert acks[0]["reason"] == "Item not found"
    assert "no-such-item" not in acks[0]["reason"]


# ---------------------------------------------------------------------------
# 5. Player cannot use another player's item -> denied ack
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_item_spell_cast_cannot_use_another_players_item(patched):
    # Item lives in `user`'s own inventory; `other` has an empty inventory of
    # their own, so requesting item_index 0 against their own (empty) store
    # naturally fails closed — item_index never resolves cross-player.
    session, user, dm, owner_key = _setup_inventory_session([_make_wand()])
    other = User(id="u-qa-other", name="Other Hero", role="player")
    session.users[other.id] = other

    await inventory_handlers.handle_inventory_cast_item_spell(
        {"item_index": 0, "item_id": "wand-of-secrets", "spell_id": "detect-magic", "client_action_id": "ck-4"},
        session, other,
    )

    acks = _acks(patched, user_id=other.id, action="inventory_cast_item_spell")
    assert len(acks) == 1
    assert acks[0]["status"] in ("denied", "failed")
    assert acks[0]["reason"] == "Item not found"
    # The original owner's charges are untouched.
    assert session.player_inventories[owner_key][0]["charges_current"] == 3


# ---------------------------------------------------------------------------
# 6. client_action_id is never leaked into broadcasts to other users
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_client_action_id_not_leaked_in_inventory_broadcast(patched):
    session, user, dm, owner_key = _setup_inventory_session([_make_wand()])

    await inventory_handlers.handle_inventory_cast_item_spell(
        {"item_index": 0, "item_id": "wand-of-secrets", "spell_id": "detect-magic", "client_action_id": "secret-id"},
        session, user,
    )

    # No broadcast (chat or otherwise) carries the client_action_id.
    for _sid, msg in patched.broadcasts:
        assert "secret-id" not in str(msg)
    # Other users' sent messages (inventory_state syncs) also never carry it.
    for sid, uid, msg in patched.sent:
        if uid == user.id and msg.get("type") == "action_ack":
            continue
        assert "secret-id" not in str(msg)


@pytest.mark.anyio
async def test_client_action_id_not_leaked_for_item_use_action(patched):
    session, user, dm, owner_key = _setup_inventory_session([_make_wand(charges=3, charges_max=3)])
    # Wand grants no runtime "action" by default (only a spell), so this
    # exercises the early not-found/no-action denial path while still
    # confirming no leak occurs.
    await inventory_handlers.handle_inventory_use_item_action(
        {"item_index": 0, "client_action_id": "secret-id-2"}, session, user,
    )
    for sid, uid, msg in patched.sent:
        if uid == user.id and msg.get("type") == "action_ack":
            continue
        assert "secret-id-2" not in str(msg)
    for _sid, msg in patched.broadcasts:
        assert "secret-id-2" not in str(msg)


# ---------------------------------------------------------------------------
# 7. Confirmed item spell action still produces authoritative inventory
#    revision update (ack is additive, not a replacement)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_confirmed_item_spell_still_bumps_inventory_revision(patched):
    session, user, dm, owner_key = _setup_inventory_session([_make_wand()])
    before_rev = session.inventory_revision

    await inventory_handlers.handle_inventory_cast_item_spell(
        {"item_index": 0, "item_id": "wand-of-secrets", "spell_id": "detect-magic", "client_action_id": "ck-7"},
        session, user,
    )

    assert session.inventory_revision == before_rev + 1
    # The dedicated inventory_item_spell_cast confirmation still fires too.
    spell_cast_msgs = [m for _s, u, m in patched.sent if u == user.id and m.get("type") == "inventory_item_spell_cast"]
    assert len(spell_cast_msgs) == 1


# ---------------------------------------------------------------------------
# 8. Confirmed combat-affecting quick action includes combat_revision
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_combat_attack_request_confirmed_ack_includes_combat_revision(patched):
    session, player, other, attacker, target = _build_combat_session()
    session.combat["revision"] = 5

    await combat_handlers.handle_combat_attack_request(
        {"target_id": target.id, "attack_kind": "weapon", "client_action_id": "ck-8"},
        session, player,
    )

    acks = _acks(patched, user_id=player.id, action="combat_attack_request")
    assert len(acks) == 1
    ack = acks[0]
    assert ack["status"] == "confirmed"
    assert ack["client_action_id"] == "ck-8"
    assert ack["target_id"] == target.id
    assert ack["combat_revision"] == 5


@pytest.mark.anyio
async def test_combat_attack_request_not_your_turn_sends_denied_ack(patched):
    session, player, other, attacker, target = _build_combat_session()

    await combat_handlers.handle_combat_attack_request(
        {"target_id": target.id, "attack_kind": "weapon", "client_action_id": "ck-9"},
        session, other,
    )

    acks = _acks(patched, user_id=other.id, action="combat_attack_request")
    assert len(acks) == 1
    assert acks[0]["status"] == "denied"
    assert acks[0]["reason"] == "Not your turn"


@pytest.mark.anyio
async def test_combat_attack_request_invalid_target_sends_denied_ack(patched):
    session, player, other, attacker, target = _build_combat_session()

    await combat_handlers.handle_combat_attack_request(
        {"target_id": "no-such-token", "attack_kind": "weapon", "client_action_id": "ck-10"},
        session, player,
    )

    acks = _acks(patched, user_id=player.id, action="combat_attack_request")
    assert len(acks) == 1
    assert acks[0]["status"] == "denied"
    assert acks[0]["reason"] == "Invalid target"


# ---------------------------------------------------------------------------
# 9. action_ack never replaces the authoritative combat_state/inventory sync
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_attack_request_ack_does_not_replace_combat_broadcast(monkeypatch, patched):
    broadcast_calls = []

    async def _capture_broadcast_combat(session):
        broadcast_calls.append(dict(session.combat or {}))

    monkeypatch.setattr(combat_handlers, "_broadcast_combat", _capture_broadcast_combat)

    session, player, other, attacker, target = _build_combat_session()

    await combat_handlers.handle_combat_attack_request(
        {"target_id": target.id, "attack_kind": "weapon", "client_action_id": "ck-11"},
        session, player,
    )

    assert len(broadcast_calls) == 1
    assert broadcast_calls[0].get("pending_attack") is not None
    assert len(_acks(patched, user_id=player.id, action="combat_attack_request")) == 1


@pytest.mark.anyio
async def test_item_spell_ack_does_not_replace_inventory_state_sync(monkeypatch, patched):
    sync_calls = []

    async def _capture_send_inventory_state(session, uid):
        sync_calls.append(uid)

    monkeypatch.setattr(inventory_handlers, "_send_inventory_state", _capture_send_inventory_state)

    session, user, dm, owner_key = _setup_inventory_session([_make_wand()])
    monkeypatch.setattr(
        inventory_handlers.manager,
        "get_session_connections",
        lambda _sid: {uid: object() for uid in session.users},
    )

    await inventory_handlers.handle_inventory_cast_item_spell(
        {"item_index": 0, "item_id": "wand-of-secrets", "spell_id": "detect-magic", "client_action_id": "ck-12"},
        session, user,
    )

    # _broadcast_inventory_state fans the sync out to every connected session user.
    assert set(sync_calls) == set(session.users.keys())
    assert len(_acks(patched, user_id=user.id, action="inventory_cast_item_spell")) == 1


# ---------------------------------------------------------------------------
# 10. Existing quick-action/item-spell tests still pass — covered by running
#     the full suites listed in the patch deliverable, not duplicated here.
# ---------------------------------------------------------------------------
