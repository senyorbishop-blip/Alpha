import asyncio

from server.handlers import common as common_handlers
from server.session import Session, User, Token


class _CaptureManager:
    def __init__(self):
        self.sent = []

    def get_session_connections(self, _session_id):
        return {"dm1": object(), "pl1": object()}

    async def send_to(self, session_id, user_id, message):
        self.sent.append((session_id, user_id, message))


def test_tokens_sync_includes_corpse_states(monkeypatch):
    session = Session(id='TESTSYNC')
    dm = User(id='dm1', name='DM', role='dm')
    player = User(id='pl1', name='Player', role='player')
    session.users[dm.id] = dm
    session.users[player.id] = player

    token = Token(
        id='tok1', name='Wolf', x=0, y=0, width=40, height=40,
        color='#fff', shape='circle', owner_id=None, hp=0, max_hp=12,
        token_type='monster',
    )
    session.tokens[token.id] = token
    session.corpse_states[token.id] = {
        'corpse_id': token.id,
        'token_id': token.id,
        'token_name': token.name,
        'depleted': False,
        'search_attempts': {},
        'harvest_attempts': {},
    }

    capture = _CaptureManager()
    monkeypatch.setattr(common_handlers, 'manager', capture)

    asyncio.run(common_handlers._broadcast_token_state_sync(session))

    assert capture.sent, 'expected tokens_sync messages'
    for _sid, _uid, msg in capture.sent:
        assert msg.get('type') == 'tokens_sync'
        payload = msg.get('payload') or {}
        assert payload.get('corpse_states') == session.corpse_states


def test_tokens_sync_visibility_matches_state_bootstrap_filters(monkeypatch):
    session = Session(id='TESTSYNCCTX')
    dm = User(id='dm1', name='DM', role='dm')
    player = User(id='pl1', name='Player', role='player')
    session.users[dm.id] = dm
    session.users[player.id] = player
    session.dm_id = dm.id
    session.set_user_subgroup_id(player.id, 'party-a', actor_id=dm.id)
    session.set_subgroup_map_context('party-a', 'poi-inn', actor_id=dm.id)
    session.dm_map_context = 'poi-inn'

    session.tokens['tok-world'] = Token(
        id='tok-world', name='World NPC', x=0, y=0, width=40, height=40,
        color='#fff', shape='circle', owner_id=None, map_context='world',
    )
    session.tokens['tok-local'] = Token(
        id='tok-local', name='Inn NPC', x=0, y=0, width=40, height=40,
        color='#fff', shape='circle', owner_id=None, map_context='poi-inn',
    )
    session.tokens['tok-hidden'] = Token(
        id='tok-hidden', name='Hidden NPC', x=0, y=0, width=40, height=40,
        color='#fff', shape='circle', owner_id=None, hidden=True, map_context='poi-inn',
    )

    capture = _CaptureManager()
    monkeypatch.setattr(common_handlers, 'manager', capture)
    asyncio.run(common_handlers._broadcast_token_state_sync(session))

    sent_by_user = {uid: msg for _sid, uid, msg in capture.sent if msg.get('type') == 'tokens_sync'}
    assert dm.id in sent_by_user
    assert player.id in sent_by_user

    dm_tokens = set((sent_by_user[dm.id].get('payload') or {}).get('tokens', {}).keys())
    player_tokens = set((sent_by_user[player.id].get('payload') or {}).get('tokens', {}).keys())
    player_bootstrap_tokens = set(session.to_state_dict_for_role("player", player.id).get("tokens", {}).keys())

    assert dm_tokens == {'tok-world', 'tok-local', 'tok-hidden'}
    assert player_tokens == player_bootstrap_tokens
    assert player_tokens == {'tok-world', 'tok-local'}


def test_token_event_visibility_matches_context_filters(monkeypatch):
    session = Session(id='TESTEVENTCTX')
    dm = User(id='dm1', name='DM', role='dm')
    player = User(id='pl1', name='Player', role='player')
    session.users[dm.id] = dm
    session.users[player.id] = player
    session.dm_id = dm.id
    session.set_user_subgroup_id(player.id, 'party-a', actor_id=dm.id)
    session.set_subgroup_map_context('party-a', 'poi-inn', actor_id=dm.id)

    token = Token(
        id='tok-world', name='World NPC', x=0, y=0, width=40, height=40,
        color='#fff', shape='circle', owner_id=None, map_context='world',
    )

    capture = _CaptureManager()
    asyncio.run(common_handlers._broadcast_token_event(capture, session, 'token_moved', {'id': token.id}, token))
    sent_by_user = {uid: msg for _sid, uid, msg in capture.sent}

    assert dm.id in sent_by_user
    assert player.id in sent_by_user

    session.set_subgroup_map_context('party-a', 'poi-crypt', actor_id=dm.id)
    capture = _CaptureManager()
    asyncio.run(common_handlers._broadcast_token_event(capture, session, 'token_moved', {'id': token.id}, token))
    sent_by_user = {uid: msg for _sid, uid, msg in capture.sent}

    assert dm.id in sent_by_user
    assert player.id in sent_by_user, "players should still receive world-map token updates"

    local_token = Token(
        id='tok-local', name='Local NPC', x=0, y=0, width=40, height=40,
        color='#fff', shape='circle', owner_id=None, map_context='poi-inn',
    )
    capture = _CaptureManager()
    asyncio.run(common_handlers._broadcast_token_event(capture, session, 'token_moved', {'id': local_token.id}, local_token))
    sent_user_ids = {uid for _sid, uid, _msg in capture.sent}
    assert dm.id in sent_user_ids
    assert player.id not in sent_user_ids
