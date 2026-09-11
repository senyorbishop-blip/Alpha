"""PR 8 — Hidden NPC and player visibility enforcement tests.

Proves (and locks in via regression tests) that hidden NPCs, fog-hidden
NPCs, wall/door-LOS-hidden NPCs, staged/off-map tokens, and secret/hidden
door metadata are never leaked to Player or Viewer clients across every
live pathway: reconnect snapshot v2, legacy state_sync, tokens_sync,
single-token events (token_created/token_moved/token_hidden_changed/
token_sent_to_staging/token_removed_hidden), combat_state, and the
editor props (door) sync used for secret-door filtering.

Two real leaks were found and fixed while building this suite (in
addition to the three bestiary-spawn / summon / combat-move-commit
broadcast leaks fixed earlier in this PR):

  * ``_can_user_see_token`` (server/handlers/common.py) never checked the
    ``staged`` flag, so a staged token (a hidden ambush NPC the DM pulls
    off the map, or another player's token) stayed visible to anyone who
    already had visibility into its map context. Fixed to treat staged
    tokens as invisible to everyone except the DM and the token's own
    owning player.
  * ``handle_token_send_to_staging`` (server/handlers/tokens.py)
    unconditionally ``manager.broadcast``ed the full raw token payload —
    including hidden-NPC names/positions — to every connection. Fixed to
    route through the existing ``_broadcast_token_visibility`` filtered
    helper, the same pattern already used by ``handle_token_edit`` and
    ``handle_toggle_hidden``.

These tests exercise the real handler entry points (not just the
visibility helpers in isolation) so a future change that bypasses the
filtered broadcast path will fail loudly here.
"""
import asyncio

import pytest

from server.handlers import combat as combat_handlers
from server.handlers import content as content_handlers
from server.handlers import map_editor
from server.handlers import tokens as token_handlers
from server.handlers.combat import _broadcast_combat, sync_combat_visibility
from server.handlers.common import (
    _broadcast_token_state_sync,
    _broadcast_token_visibility,
    _can_user_see_token,
    _combat_state_payload_for_user,
)
from server.session import Session, Token, User, filter_editor_props_for_role


def assert_no_hidden_token_payload(message: dict, hidden_token_id: str):
    payload = message.get("payload") if isinstance(message, dict) else message
    assert payload is not None
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if isinstance(tokens, dict):
        assert hidden_token_id not in tokens
    token_field = payload.get("token") if isinstance(payload, dict) else None
    if isinstance(token_field, dict):
        assert token_field.get("id") != hidden_token_id


def assert_no_hidden_npc_name(message: dict, name: str):
    assert name not in str(message)


def assert_dm_payload_contains_hidden_when_allowed(message: dict, hidden_token_id: str):
    payload = message.get("payload") if isinstance(message, dict) else message
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    assert isinstance(tokens, dict)
    assert hidden_token_id in tokens


def _fog_entry(*, enabled=True, cols=4, rows=4, revealed_cells=()):
    total = cols * rows
    cells = ["0"] * total
    for idx in revealed_cells:
        cells[idx] = "1"
    return {"enabled": enabled, "cols": cols, "rows": rows, "cells": "".join(cells)}


def _build_full_session():
    session = Session(id="s-hide")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    other_player = User(id="player-2", name="Other Player", role="player")
    viewer = User(id="viewer-1", name="Viewer", role="viewer")
    session.users[dm.id] = dm
    session.users[player.id] = player
    session.users[other_player.id] = other_player
    session.users[viewer.id] = viewer

    session.fog_maps = {"world": _fog_entry(revealed_cells=range(16))}

    pc = Token(id="tok-pc", name="Hero", x=0, y=0, width=40, height=40, color="#fff", shape="circle", owner_id=player.id, token_type="player", vision_enabled=True, vision_radius=2000)
    hidden_npc = Token(id="tok-hidden-npc", name="Shadow Assassin", x=10, y=10, width=40, height=40, color="#f00", shape="circle", owner_id=None, token_type="npc", hidden=True)
    staged_npc = Token(id="tok-staged-npc", name="Ambush Wolves", x=20, y=20, width=40, height=40, color="#a00", shape="circle", owner_id=None, token_type="monster", staged=True)
    staged_other_player_token = Token(id="tok-staged-other-pc", name="Other Player Companion", x=5, y=5, width=40, height=40, color="#00a", shape="circle", owner_id=other_player.id, token_type="player", staged=True)
    visible_npc = Token(id="tok-visible-npc", name="Goblin Scout", x=0, y=0, width=20, height=20, color="#0f0", shape="circle", owner_id=None, token_type="npc")
    session.tokens[pc.id] = pc
    session.tokens[hidden_npc.id] = hidden_npc
    session.tokens[staged_npc.id] = staged_npc
    session.tokens[staged_other_player_token.id] = staged_other_player_token
    session.tokens[visible_npc.id] = visible_npc

    session.editor_props = {"world": [{"id": "door-1", "kind": "door", "x": 9999, "y": 9999, "facing": "v", "state": "closed", "locked": False, "blocks_vision": True, "secret": True, "revealed": False}]}
    return session, dm, player, other_player, viewer


async def _capture(monkeypatch, target_module):
    sent = []

    async def _send_to(session_id, user_id, message):
        sent.append((session_id, user_id, message))

    async def _broadcast(session_id, message, exclude_user=None):
        sent.append((session_id, None, message))

    monkeypatch.setattr(target_module.manager, "send_to", _send_to)
    monkeypatch.setattr(target_module.manager, "broadcast", _broadcast)
    return sent


def _stub_connections_for(monkeypatch, target_module, session):
    monkeypatch.setattr(target_module.manager, "get_session_connections", lambda sid: {uid: object() for uid in session.users})


def _messages_to(sent, user_id, msg_type=None):
    out = [m for sid, uid, m in sent if uid == user_id]
    if msg_type is not None:
        out = [m for m in out if m.get("type") == msg_type]
    return out


@pytest.mark.anyio
async def test_reconnect_snapshot_hides_staged_and_hidden_tokens_from_player(monkeypatch):
    session, _dm, player, _other, _viewer = _build_full_session()
    sent = await _capture(monkeypatch, content_handlers)
    await content_handlers.handle_request_state({}, session, player)
    state_msg = _messages_to(sent, player.id, "state_sync")[0]
    assert_no_hidden_token_payload(state_msg, "tok-hidden-npc")
    assert_no_hidden_token_payload(state_msg, "tok-staged-npc")
    assert_no_hidden_token_payload(state_msg, "tok-staged-other-pc")
    assert_no_hidden_npc_name(state_msg, "Shadow Assassin")
    assert_no_hidden_npc_name(state_msg, "Ambush Wolves")
    assert_no_hidden_npc_name(state_msg, "Other Player Companion")
    tokens = state_msg["payload"]["tokens"]
    assert "tok-pc" in tokens
    assert "tok-visible-npc" in tokens
    snapshot_msg = _messages_to(sent, player.id, "authoritative_snapshot")[0]
    assert_no_hidden_npc_name(snapshot_msg, "Shadow Assassin")
    assert_no_hidden_npc_name(snapshot_msg, "Ambush Wolves")


@pytest.mark.anyio
async def test_reconnect_snapshot_gives_dm_everything(monkeypatch):
    session, dm, _player, _other, _viewer = _build_full_session()
    sent = await _capture(monkeypatch, content_handlers)
    await content_handlers.handle_request_state({}, session, dm)
    state_msg = _messages_to(sent, dm.id, "state_sync")[0]
    assert_dm_payload_contains_hidden_when_allowed(state_msg, "tok-hidden-npc")
    assert_dm_payload_contains_hidden_when_allowed(state_msg, "tok-staged-npc")
    assert_dm_payload_contains_hidden_when_allowed(state_msg, "tok-staged-other-pc")


@pytest.mark.anyio
async def test_reconnect_snapshot_lets_owner_see_own_staged_token(monkeypatch):
    session, _dm, _player, other_player, _viewer = _build_full_session()
    sent = await _capture(monkeypatch, content_handlers)
    await content_handlers.handle_request_state({}, session, other_player)
    state_msg = _messages_to(sent, other_player.id, "state_sync")[0]
    tokens = state_msg["payload"]["tokens"]
    assert "tok-staged-other-pc" in tokens
    assert "tok-staged-npc" not in tokens
    assert "tok-hidden-npc" not in tokens


@pytest.mark.anyio
async def test_reconnect_snapshot_hides_secret_undiscovered_door_for_player(monkeypatch):
    session, _dm, player, _other, _viewer = _build_full_session()
    sent = await _capture(monkeypatch, content_handlers)
    await content_handlers.handle_request_state({}, session, player)
    snapshot_msg = _messages_to(sent, player.id, "authoritative_snapshot")[0]
    assert snapshot_msg["payload"]["map"]["doors"] == []


@pytest.mark.anyio
async def test_reconnect_snapshot_gives_dm_secret_door_details(monkeypatch):
    session, dm, _player, _other, _viewer = _build_full_session()
    sent = await _capture(monkeypatch, content_handlers)
    await content_handlers.handle_request_state({}, session, dm)
    snapshot_msg = _messages_to(sent, dm.id, "authoritative_snapshot")[0]
    assert len(snapshot_msg["payload"]["map"]["doors"]) == 1


@pytest.mark.anyio
async def test_reconnect_snapshot_viewer_safe(monkeypatch):
    session, _dm, _player, _other, viewer = _build_full_session()
    sent = await _capture(monkeypatch, content_handlers)
    await content_handlers.handle_request_state({}, session, viewer)
    state_msg = _messages_to(sent, viewer.id, "state_sync")[0]
    assert_no_hidden_npc_name(state_msg, "Shadow Assassin")
    assert_no_hidden_npc_name(state_msg, "Ambush Wolves")
    tokens = state_msg["payload"]["tokens"]
    assert "tok-staged-other-pc" not in tokens


@pytest.mark.anyio
async def test_tokens_sync_filters_hidden_staged_and_fog_npcs_per_role(monkeypatch):
    session, dm, player, other_player, viewer = _build_full_session()
    sent = await _capture(monkeypatch, token_handlers)
    _stub_connections_for(monkeypatch, token_handlers, session)
    await _broadcast_token_state_sync(session)
    dm_msg = _messages_to(sent, dm.id, "tokens_sync")[0]
    assert_dm_payload_contains_hidden_when_allowed(dm_msg, "tok-hidden-npc")
    assert_dm_payload_contains_hidden_when_allowed(dm_msg, "tok-staged-npc")
    player_msg = _messages_to(sent, player.id, "tokens_sync")[0]
    assert_no_hidden_token_payload(player_msg, "tok-hidden-npc")
    assert_no_hidden_token_payload(player_msg, "tok-staged-npc")
    assert_no_hidden_token_payload(player_msg, "tok-staged-other-pc")
    other_player_msg = _messages_to(sent, other_player.id, "tokens_sync")[0]
    assert "tok-staged-other-pc" in other_player_msg["payload"]["tokens"]
    viewer_msg = _messages_to(sent, viewer.id, "tokens_sync")[0]
    assert_no_hidden_token_payload(viewer_msg, "tok-hidden-npc")
    assert_no_hidden_token_payload(viewer_msg, "tok-staged-npc")


@pytest.mark.anyio
async def test_token_created_event_not_delivered_to_player_for_hidden_npc(monkeypatch):
    session = Session(id="s-create")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    sent = await _capture(monkeypatch, token_handlers)
    await token_handlers.handle_token_create({"name": "Lurking Horror", "x": 0, "y": 0, "hidden": True, "tokenType": "monster"}, session, dm)
    assert _messages_to(sent, player.id, "token_created") == []
    dm_events = _messages_to(sent, dm.id, "token_created")
    assert len(dm_events) == 1
    assert dm_events[0]["payload"]["token"]["name"] == "Lurking Horror"


@pytest.mark.anyio
async def test_token_created_staged_npc_not_delivered_to_player(monkeypatch):
    session = Session(id="s-create-staged")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    sent = await _capture(monkeypatch, token_handlers)
    await token_handlers.handle_token_create({"name": "Staged Ambusher", "x": 0, "y": 0, "staged": True, "tokenType": "monster"}, session, dm)
    assert _messages_to(sent, player.id, "token_created") == []
    assert len(_messages_to(sent, dm.id, "token_created")) == 1


@pytest.mark.anyio
async def test_send_to_staging_does_not_leak_full_payload_to_other_players(monkeypatch):
    session = Session(id="s-stage")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    npc = Token(id="tok-npc", name="Secret Boss", x=0, y=0, width=40, height=40, color="#f00", shape="circle", owner_id=None, token_type="npc")
    session.tokens[npc.id] = npc
    sent = await _capture(monkeypatch, token_handlers)
    await token_handlers.handle_token_send_to_staging({"token_id": npc.id}, session, dm)
    player_messages = _messages_to(sent, player.id)
    assert_no_hidden_npc_name({"messages": player_messages}, "Secret Boss")
    for msg in player_messages:
        assert msg.get("type") != "token_sent_to_staging"
    dm_events = _messages_to(sent, dm.id, "token_sent_to_staging")
    assert len(dm_events) == 1
    assert dm_events[0]["payload"]["name"] == "Secret Boss"


@pytest.mark.anyio
async def test_send_to_staging_notifies_previous_viewers_with_removal_not_data(monkeypatch):
    session = Session(id="s-stage-removal")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    npc = Token(id="tok-npc", name="Visible Goblin", x=0, y=0, width=40, height=40, color="#0f0", shape="circle", owner_id=None, token_type="npc")
    session.tokens[npc.id] = npc
    sent = await _capture(monkeypatch, token_handlers)
    await token_handlers.handle_token_send_to_staging({"token_id": npc.id}, session, dm)
    player_events = _messages_to(sent, player.id, "token_removed_hidden")
    assert len(player_events) == 1
    assert_no_hidden_npc_name(player_events[0], "Visible Goblin")


def test_staged_token_invisible_to_non_owner_via_can_user_see_token():
    session = Session(id="s-helper")
    owner = User(id="owner-1", name="Owner", role="player")
    other = User(id="other-1", name="Other", role="player")
    dm = User(id="dm-1", name="DM", role="dm")
    token = Token(id="tok-1", name="Companion", x=0, y=0, width=40, height=40, color="#fff", shape="circle", owner_id=owner.id, token_type="player", staged=True)
    assert _can_user_see_token(session, token, dm) is True
    assert _can_user_see_token(session, token, owner) is True
    assert _can_user_see_token(session, token, other) is False


def test_staged_npc_invisible_to_everyone_but_dm():
    session = Session(id="s-helper-2")
    player = User(id="player-1", name="Player", role="player")
    dm = User(id="dm-1", name="DM", role="dm")
    npc = Token(id="npc-1", name="Hidden Ambush", x=0, y=0, width=40, height=40, color="#f00", shape="circle", owner_id=None, token_type="monster", staged=True)
    assert _can_user_see_token(session, npc, dm) is True
    assert _can_user_see_token(session, npc, player) is False


@pytest.mark.anyio
async def test_token_hidden_changed_event_removes_token_from_player_view(monkeypatch):
    session = Session(id="s-hide-toggle")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    npc = Token(id="tok-npc", name="Now Hidden", x=0, y=0, width=40, height=40, color="#f00", shape="circle", owner_id=None, token_type="npc")
    session.tokens[npc.id] = npc
    sent = await _capture(monkeypatch, token_handlers)
    npc.hidden = True
    await _broadcast_token_visibility(session, npc, "token_hidden_changed")
    player_events = _messages_to(sent, player.id)
    assert len(player_events) == 1
    assert player_events[0]["type"] == "token_removed_hidden"
    assert_no_hidden_npc_name(player_events[0], "Now Hidden")
    dm_events = _messages_to(sent, dm.id, "token_hidden_changed")
    assert len(dm_events) == 1
    assert dm_events[0]["payload"]["name"] == "Now Hidden"


def test_combat_state_payload_excludes_hidden_staged_npc_for_player_includes_for_dm():
    session = Session(id="s-combat")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    hidden_npc = Token(id="npc-hidden", name="Hidden Combatant", x=0, y=0, width=40, height=40, color="#f00", shape="circle", owner_id=None, token_type="npc", hidden=True)
    staged_npc = Token(id="npc-staged", name="Staged Combatant", x=0, y=0, width=40, height=40, color="#a00", shape="circle", owner_id=None, token_type="monster", staged=True)
    visible_npc = Token(id="npc-visible", name="Visible Combatant", x=0, y=0, width=40, height=40, color="#0f0", shape="circle", owner_id=None, token_type="npc")
    session.tokens = {hidden_npc.id: hidden_npc, staged_npc.id: staged_npc, visible_npc.id: visible_npc}
    session.combat = {"active": True, "turn": 0, "revision": 1, "combatants": [{"token_id": hidden_npc.id, "id": "c1", "name": hidden_npc.name}, {"token_id": staged_npc.id, "id": "c2", "name": staged_npc.name}, {"token_id": visible_npc.id, "id": "c3", "name": visible_npc.name}], "suspended_combatants": []}
    player_payload = _combat_state_payload_for_user(session, player)
    player_token_ids = {c["token_id"] for c in player_payload["combatants"]}
    assert player_token_ids == {visible_npc.id}
    assert "suspended_combatants" not in player_payload
    assert_no_hidden_npc_name(player_payload, "Hidden Combatant")
    assert_no_hidden_npc_name(player_payload, "Staged Combatant")
    dm_payload = _combat_state_payload_for_user(session, dm)
    dm_token_ids = {c["token_id"] for c in dm_payload["combatants"]}
    assert dm_token_ids == {hidden_npc.id, staged_npc.id, visible_npc.id}


@pytest.mark.anyio
async def test_broadcast_combat_does_not_leak_hidden_combatant_to_player(monkeypatch):
    session = Session(id="s-combat-broadcast")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    hidden_npc = Token(id="npc-hidden", name="Secret Lich", x=0, y=0, width=40, height=40, color="#f00", shape="circle", owner_id=None, token_type="npc", hidden=True)
    session.tokens[hidden_npc.id] = hidden_npc
    session.combat = {"active": True, "turn": 0, "revision": 0, "combatants": [{"token_id": hidden_npc.id, "id": "c1", "name": hidden_npc.name}], "suspended_combatants": []}
    sent = await _capture(monkeypatch, combat_handlers)
    _stub_connections_for(monkeypatch, combat_handlers, session)
    await _broadcast_combat(session)
    player_msg = _messages_to(sent, player.id, "combat_state")[0]
    assert_no_hidden_npc_name(player_msg, "Secret Lich")
    assert player_msg["payload"]["combatants"] == []
    dm_msg = _messages_to(sent, dm.id, "combat_state")[0]
    assert len(dm_msg["payload"]["combatants"]) == 1


@pytest.mark.anyio
async def test_sync_combat_visibility_suspends_staged_combatant():
    session = Session(id="s-combat-sync")
    viewer = Token(id="pc-1", name="Hero", x=0, y=0, width=40, height=40, color="#fff", shape="circle", owner_id="user-1", token_type="player", vision_enabled=True, vision_radius=2000)
    npc = Token(id="npc-1", name="Staged Wolf", x=10, y=10, width=40, height=40, color="#f00", shape="circle", owner_id=None, token_type="monster", staged=True)
    session.tokens[viewer.id] = viewer
    session.tokens[npc.id] = npc
    session.combat = {"active": True, "combatants": [{"token_id": npc.id, "id": "c1", "name": npc.name, "map_context": "world"}], "turn": 0, "suspended_combatants": [], "fog_suspended_combatants": [], "hidden_suspended_combatants": []}
    result = sync_combat_visibility(session, map_context="world", reason="test")
    assert result["changed"] is True
    assert session.combat["combatants"] == []


@pytest.mark.anyio
async def test_editor_props_sync_filters_secret_door_per_recipient(monkeypatch):
    session = Session(id="s-door-sync")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    session.editor_props = {"world": [{"id": "door-1", "kind": "door", "x": 0, "y": 0, "facing": "v", "state": "closed", "locked": False, "blocks_vision": True, "secret": True, "revealed": False}]}
    sent = await _capture(monkeypatch, map_editor)
    await map_editor._broadcast_editor_props_state(session)
    player_msg = _messages_to(sent, player.id, "editor_props_sync")[0]
    assert player_msg["payload"]["props"]["world"] == []
    dm_msg = _messages_to(sent, dm.id, "editor_props_sync")[0]
    assert len(dm_msg["payload"]["props"]["world"]) == 1
    assert dm_msg["payload"]["props"]["world"][0]["secret"] is True


def test_filter_editor_props_for_role_strips_secret_metadata_even_when_revealed():
    editor_props = {"world": [{"id": "door-1", "kind": "door", "x": 0, "y": 0, "facing": "v", "state": "open", "locked": False, "blocks_vision": True, "secret": True, "revealed": True}]}
    player_view = filter_editor_props_for_role(editor_props, "player")
    assert len(player_view["world"]) == 1
    assert "secret" not in player_view["world"][0]
    assert "revealed" not in player_view["world"][0]
    viewer_view = filter_editor_props_for_role(editor_props, "viewer")
    assert len(viewer_view["world"]) == 1
    assert "secret" not in viewer_view["world"][0]
