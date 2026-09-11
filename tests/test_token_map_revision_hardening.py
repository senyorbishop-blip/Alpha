import asyncio
from types import SimpleNamespace

from server.handlers import common as common_handlers
from server.handlers import map_editor
from server.session import POI, Session, User, Token


class _CaptureManager:
    def __init__(self):
        self.sent = []

    def get_session_connections(self, _session_id):
        return {"dm1": object(), "pl1": object()}

    async def send_to(self, session_id, user_id, message):
        self.sent.append((session_id, user_id, message))


def test_broadcast_token_event_bumps_and_stamps_token_state_revision(monkeypatch):
    session = Session(id='TESTREV1')
    dm = User(id='dm1', name='DM', role='dm')
    player = User(id='pl1', name='Player', role='player')
    session.users[dm.id] = dm
    session.users[player.id] = player

    token = Token(
        id='tok1', name='Wolf', x=0, y=0, width=40, height=40,
        color='#fff', shape='circle', owner_id=None, map_context='world',
    )
    session.tokens[token.id] = token

    assert session.token_state_revision == 0
    assert token.revision == 0

    capture = _CaptureManager()
    rev = asyncio.run(common_handlers._broadcast_token_event(
        capture, session, 'token_moved', {'id': token.id}, token,
    ))

    assert rev == 1
    assert session.token_state_revision == 1
    assert token.revision == 1
    assert capture.sent, 'expected broadcast messages'
    for _sid, _uid, msg in capture.sent:
        payload = msg.get('payload') or {}
        assert payload.get('token_state_revision') == 1
        assert payload.get('token_id') == token.id
        assert payload.get('map_context') == 'world'
        assert 'visibility_revision' in payload

    capture2 = _CaptureManager()
    rev2 = asyncio.run(common_handlers._broadcast_token_event(
        capture2, session, 'token_moved', {'id': token.id}, token,
    ))
    assert rev2 == 2
    assert session.token_state_revision == 2
    assert token.revision == 2


def test_broadcast_token_visibility_includes_revisions_and_hides_from_players(monkeypatch):
    session = Session(id='TESTREV2')
    dm = User(id='dm1', name='DM', role='dm')
    player = User(id='pl1', name='Player', role='player')
    session.users[dm.id] = dm
    session.users[player.id] = player

    token = Token(
        id='tok-hide', name='Goblin', x=0, y=0, width=40, height=40,
        color='#fff', shape='circle', owner_id=None, map_context='world', hidden=True,
    )
    session.tokens[token.id] = token

    capture = _CaptureManager()
    monkeypatch.setattr(common_handlers, 'manager', capture)
    asyncio.run(common_handlers._broadcast_token_visibility(session, token, msg_type='token_hidden_changed'))

    by_user = {uid: msg for _sid, uid, msg in capture.sent}
    assert dm.id in by_user
    assert by_user[dm.id]['type'] == 'token_hidden_changed'
    dm_payload = by_user[dm.id]['payload']
    assert dm_payload.get('token_state_revision') == 1
    assert dm_payload.get('visibility_revision') == 1

    assert player.id in by_user
    assert by_user[player.id]['type'] == 'token_removed_hidden'
    player_payload = by_user[player.id]['payload']
    assert player_payload.get('token_id') == token.id
    assert player_payload.get('token_state_revision') == 1
    assert player_payload.get('visibility_revision') == 1
    assert 'name' not in player_payload
    assert 'x' not in player_payload
    assert 'hp' not in player_payload


def test_tokens_sync_payload_includes_token_state_revision_and_map_mode(monkeypatch):
    session = Session(id='TESTREV3')
    dm = User(id='dm1', name='DM', role='dm')
    player = User(id='pl1', name='Player', role='player')
    session.users[dm.id] = dm
    session.users[player.id] = player
    session.dm_id = dm.id
    session.token_state_revision = 5
    session.dm_map_context = 'poi-inn'

    capture = _CaptureManager()
    monkeypatch.setattr(common_handlers, 'manager', capture)
    asyncio.run(common_handlers._broadcast_token_state_sync(session))

    sent_by_user = {uid: msg for _sid, uid, msg in capture.sent if msg.get('type') == 'tokens_sync'}
    for msg in sent_by_user.values():
        payload = msg.get('payload') or {}
        assert payload.get('token_state_revision') == 5
        assert payload.get('map_mode') == 'local'
        assert payload.get('dm_map_context') == 'poi-inn'


def test_stamp_token_revision_and_bump_token_state_revision_are_independent_of_visibility(monkeypatch):
    session = Session(id='TESTREV4')
    token = Token(
        id='tok-x', name='X', x=0, y=0, width=40, height=40,
        color='#fff', shape='circle', owner_id=None,
    )
    session.tokens[token.id] = token

    assert session.visibility_revision == 0
    assert session.token_state_revision == 0

    rev = common_handlers._stamp_token_revision(session, token)
    assert rev == 1
    assert token.revision == 1
    assert session.visibility_revision == 0

    vis_rev = common_handlers.bump_visibility_revision(session)
    assert vis_rev == 1
    assert session.token_state_revision == 1


def test_local_map_nav_includes_revision_fields_and_resyncs_tokens(monkeypatch):
    session = Session(id="NAVREV1")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="pl-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    session.dm_id = dm.id
    session.pois["poi-1"] = POI(id="poi-1", x=0, y=0, name="Inn", local_map_url="/static/maps/inn.png")
    session.token_state_revision = 3
    session.visibility_revision = 2

    broadcast = []
    sent = []

    async def _broadcast(*args, **kwargs):
        broadcast.append((args, kwargs))

    async def _send_to(*args, **kwargs):
        sent.append((args, kwargs))

    def _connections(_session_id):
        return {dm.id: object(), player.id: object()}

    async def _save_campaign_async(_session):
        return True

    token_sync_calls = []
    orig_token_sync = map_editor._broadcast_token_state_sync

    async def _spy_token_sync(sess):
        token_sync_calls.append(sess)
        return await orig_token_sync(sess)

    manager = SimpleNamespace(broadcast=_broadcast, send_to=_send_to, get_session_connections=_connections)
    monkeypatch.setattr(map_editor, "manager", manager)
    monkeypatch.setattr(common_handlers, "manager", manager)
    monkeypatch.setattr(map_editor, "save_campaign_async", _save_campaign_async)
    monkeypatch.setattr(map_editor, "_broadcast_token_state_sync", _spy_token_sync)

    asyncio.run(map_editor.handle_local_map_nav({
        "dm_map_context": "poi-1",
        "client_nav_intent": 1,
    }, session, dm))

    assert token_sync_calls == [session], "expected handle_local_map_nav to trigger a token resync"
    assert broadcast, "expected a local_map_enter broadcast"

    _args, _kwargs = broadcast[0]
    msg = _args[1] if len(_args) > 1 else _kwargs.get("message")
    assert msg["type"] == "local_map_enter"
    nav_payload = msg["payload"]
    assert nav_payload["map_nav_version"] == 1
    assert nav_payload["map_context_revision"] == 1
    assert nav_payload["map_mode"] == "local"
    assert nav_payload["token_state_revision"] == 3
    assert nav_payload["visibility_revision"] == 2

    assert sent, "expected a tokens_sync resync to be sent"
    tokens_sync_msgs = [
        _kw.get("message") if not _a or len(_a) < 3 else _a[2]
        for _a, _kw in sent
    ]
    assert any(m.get("type") == "tokens_sync" for m in tokens_sync_msgs)
