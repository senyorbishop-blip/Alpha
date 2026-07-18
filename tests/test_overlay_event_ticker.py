"""
tests/test_overlay_event_ticker.py — overlay event ticker (server side).

Covers:
  - ticker_event relay broadcasts with a session-scoped monotonic seq number
    (gap detection contract for overlays)
  - seq counters are independent per session
  - text/category/icon sanitization and the empty-text guard
  - DM item grants emit a ticker event (server-originated events share the
    same seq sequence as bridge-relayed ones)
  - chat_bridge role may send ticker_event; read-only/player roles may not
"""
import asyncio
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _patch_manager(monkeypatch):
    import server.handlers.common as common_mod
    sent = []

    async def _broadcast(session_id, message, exclude_user=None):
        sent.append(("broadcast", session_id, message))

    async def _send_to(session_id, user_id, message):
        sent.append(("send_to", session_id, message))

    monkeypatch.setattr(common_mod.manager, "broadcast", _broadcast)
    monkeypatch.setattr(common_mod.manager, "send_to", _send_to)
    return sent


def _make_session(session_id="ticker-1"):
    from server.session import Session, User
    session = Session(id=session_id)
    bridge = User(id="chat_bridge", name="Chat Bridge", role="chat_bridge")
    session.users[bridge.id] = bridge
    return session, bridge


def _ticker_events(sent):
    return [m["payload"] for kind, _sid, m in sent if m.get("type") == "ticker_event"]


def _run(handler, payload, session, user):
    asyncio.run(handler(payload, session, user))


# ---------------------------------------------------------------------------
# Seq numbering (gap-detection contract)
# ---------------------------------------------------------------------------

def test_ticker_events_carry_monotonic_seq(monkeypatch):
    from server.handlers.chat_bridge import handle_ticker_event
    sent = _patch_manager(monkeypatch)
    session, bridge = _make_session()

    for i in range(3):
        _run(handle_ticker_event, {"text": f"event {i}", "category": "duel"}, session, bridge)

    events = _ticker_events(sent)
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert events[0]["category"] == "duel"
    assert events[0]["text"] == "event 0"
    assert all(isinstance(e["ts"], float) for e in events)


def test_ticker_seq_is_per_session(monkeypatch):
    from server.handlers.chat_bridge import handle_ticker_event
    sent = _patch_manager(monkeypatch)
    session_a, bridge_a = _make_session("ticker-a")
    session_b, bridge_b = _make_session("ticker-b")

    _run(handle_ticker_event, {"text": "a1"}, session_a, bridge_a)
    _run(handle_ticker_event, {"text": "a2"}, session_a, bridge_a)
    _run(handle_ticker_event, {"text": "b1"}, session_b, bridge_b)

    by_session = {}
    for kind, sid, m in sent:
        if m.get("type") == "ticker_event":
            by_session.setdefault(sid, []).append(m["payload"]["seq"])
    assert by_session["ticker-a"] == [1, 2]
    assert by_session["ticker-b"] == [1]


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

def test_ticker_event_sanitizes_text_category_and_icon(monkeypatch):
    from server.handlers.chat_bridge import handle_ticker_event
    sent = _patch_manager(monkeypatch)
    session, bridge = _make_session()

    _run(handle_ticker_event, {
        "text": "<script>alert(1)</script> Grognak wins! " + "x" * 500,
        "category": "duel<script>",
        "icon": "⚔️" * 20,
    }, session, bridge)

    events = _ticker_events(sent)
    assert len(events) == 1
    ev = events[0]
    assert "<" not in ev["text"] and ">" not in ev["text"]
    assert len(ev["text"]) <= 220
    assert "<" not in ev["category"] and len(ev["category"]) <= 24
    assert len(ev["icon"]) <= 8


def test_ticker_event_ignores_empty_text(monkeypatch):
    from server.handlers.chat_bridge import handle_ticker_event
    sent = _patch_manager(monkeypatch)
    session, bridge = _make_session()

    _run(handle_ticker_event, {"text": "   ", "category": "duel"}, session, bridge)
    _run(handle_ticker_event, {}, session, bridge)

    assert _ticker_events(sent) == []
    assert int(getattr(session, "ticker_seq", 0) or 0) == 0  # no seq burned


# ---------------------------------------------------------------------------
# Server-originated events share the sequence (DM item grant)
# ---------------------------------------------------------------------------

def test_dm_item_grant_emits_ticker_event(monkeypatch):
    import time as _time
    from server.session import ChatParticipant, User
    import server.handlers.chat_bridge as cb

    sent = _patch_manager(monkeypatch)

    async def _noop(session):
        return True
    monkeypatch.setattr(cb, "save_campaign_async", _noop)

    session, _bridge = _make_session()
    dm = User(id="dm1", name="DM", role="dm")
    session.users[dm.id] = dm
    session.chat_participants["dave"] = ChatParticipant(
        twitch_username="dave", display_name="Dave", joined_at=_time.time())

    _run(cb.handle_dm_grant_chat_participant_item, {
        "twitch_username": "dave",
        "item_entry": {"name": "Fireball Scroll", "qty": 1},
    }, session, dm)

    events = _ticker_events(sent)
    assert len(events) == 1
    assert events[0]["seq"] == 1
    assert events[0]["category"] == "grant"
    assert "Dave" in events[0]["text"] and "Fireball Scroll" in events[0]["text"]


# ---------------------------------------------------------------------------
# Role policy
# ---------------------------------------------------------------------------

def test_ticker_event_role_policy():
    from server.handlers.ws_permissions import is_ws_message_allowed_for_role

    assert is_ws_message_allowed_for_role("ticker_event", "chat_bridge").allowed
    assert is_ws_message_allowed_for_role("ticker_event", "dm").allowed
    assert not is_ws_message_allowed_for_role("ticker_event", "player").allowed
    assert not is_ws_message_allowed_for_role("ticker_event", "viewer").allowed
    # Overlays are read-only consumers — they can never inject ticker events.
    assert not is_ws_message_allowed_for_role("ticker_event", "overlay").allowed
