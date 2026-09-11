import asyncio

from server.handlers import camp_rest
from server.session import Session, Token, User


class CaptureManager:
    def __init__(self):
        self.sent = []
        self.broadcasts = []

    def get_session_connections(self, session_id):
        """Match the live connection-manager surface used by camp-rest sync.

        These focused unit tests do not need connected recipients, but the
        production handler now queries active session connections before
        broadcasting character/quick-action hydration updates.
        """
        return {}

    async def send_to(self, session_id, user_id, message):
        self.sent.append((session_id, user_id, message))

    async def broadcast(self, session_id, message):
        self.broadcasts.append((session_id, message))


def make_token(token_id, owner_id, hp=5, max_hp=20, name=None):
    return Token(
        id=token_id,
        name=name or token_id,
        x=0,
        y=0,
        width=40,
        height=40,
        color="#fff",
        shape="circle",
        owner_id=owner_id,
        hp=hp,
        max_hp=max_hp,
    )


def make_session():
    session = Session(id="REST")
    player = User(id="p1", name="Alice", role="player")
    other = User(id="p2", name="Bob", role="player")
    session.users[player.id] = player
    session.users[other.id] = other
    session.camp_rest = {"active": True}
    return session, player, other


def patch_side_effects(monkeypatch):
    capture = CaptureManager()
    token_sync_calls = []
    saves = []

    async def fake_token_sync(session):
        token_sync_calls.append(session.id)

    async def fake_save(session):
        saves.append(session.id)

    monkeypatch.setattr(camp_rest, "manager", capture)
    monkeypatch.setattr(camp_rest, "_broadcast_token_state_sync", fake_token_sync)
    monkeypatch.setattr(camp_rest, "save_campaign_async", fake_save)
    return capture, token_sync_calls, saves


def notification_messages(capture):
    return [
        (msg.get("payload") or {}).get("message", "")
        for _sid, _uid, msg in capture.sent
        if msg.get("type") == "notification"
    ]


def test_token_id_heals_selected_owned_token_not_first_owned_token(monkeypatch):
    session, player, _other = make_session()
    session.tokens["first"] = make_token("first", player.id, hp=1, max_hp=20, name="First")
    session.tokens["selected"] = make_token("selected", player.id, hp=2, max_hp=20, name="Selected")
    capture, token_sync_calls, saves = patch_side_effects(monkeypatch)

    asyncio.run(camp_rest.handle_camp_rest_spend_hit_die(
        {"token_id": "selected", "heal_amount": 7}, session, player
    ))

    assert session.tokens["first"].hp == 1
    assert session.tokens["selected"].hp == 9
    assert token_sync_calls == [session.id]
    assert saves == [session.id]
    assert any(msg.get("type") == "camp_rest_hit_die_result" for _sid, _uid, msg in capture.sent)


def test_missing_token_id_keeps_legacy_first_owned_token_fallback(monkeypatch):
    session, player, _other = make_session()
    session.tokens["first"] = make_token("first", player.id, hp=1, max_hp=20)
    session.tokens["second"] = make_token("second", player.id, hp=2, max_hp=20)
    _capture, token_sync_calls, saves = patch_side_effects(monkeypatch)

    asyncio.run(camp_rest.handle_camp_rest_spend_hit_die({"heal_amount": 4}, session, player))

    assert session.tokens["first"].hp == 5
    assert session.tokens["second"].hp == 2
    assert token_sync_calls == [session.id]
    assert saves == [session.id]


def test_other_players_token_id_is_rejected(monkeypatch):
    session, player, other = make_session()
    session.tokens["own"] = make_token("own", player.id, hp=1, max_hp=20)
    session.tokens["other"] = make_token("other", other.id, hp=2, max_hp=20)
    capture, token_sync_calls, saves = patch_side_effects(monkeypatch)

    asyncio.run(camp_rest.handle_camp_rest_spend_hit_die(
        {"token_id": "other", "heal_amount": 5}, session, player
    ))

    assert session.tokens["own"].hp == 1
    assert session.tokens["other"].hp == 2
    assert token_sync_calls == []
    assert saves == []
    assert any("own tokens" in message for message in notification_messages(capture))


def test_non_existent_token_id_is_rejected(monkeypatch):
    session, player, _other = make_session()
    session.tokens["own"] = make_token("own", player.id, hp=1, max_hp=20)
    capture, token_sync_calls, saves = patch_side_effects(monkeypatch)

    asyncio.run(camp_rest.handle_camp_rest_spend_hit_die(
        {"token_id": "missing", "heal_amount": 5}, session, player
    ))

    assert session.tokens["own"].hp == 1
    assert token_sync_calls == []
    assert saves == []
    assert any("Could not find that token" in message for message in notification_messages(capture))


def test_token_state_sync_is_broadcast_after_valid_healing(monkeypatch):
    session, player, _other = make_session()
    session.tokens["selected"] = make_token("selected", player.id, hp=10, max_hp=20)
    _capture, token_sync_calls, _saves = patch_side_effects(monkeypatch)

    asyncio.run(camp_rest.handle_camp_rest_spend_hit_die(
        {"token_id": "selected", "heal_amount": 3}, session, player
    ))

    assert session.tokens["selected"].hp == 13
    assert token_sync_calls == [session.id]
