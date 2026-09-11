"""Tests for the token_moved stale-packet guard (realtime sync tiny patch).

Server side: handle_token_move now stamps the same monotonic
``visibility_revision`` counter used by tokens_sync onto every token_moved
broadcast. Client side: the token_moved case in play.html drops a payload
whose revision is behind the last one it already applied, so a delayed/
out-of-order move packet can't snap a token back to a stale position.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from server.handlers import tokens as token_handlers
from server.session import Session, Token, User

ROOT = Path(__file__).resolve().parents[1]
PLAY = ROOT / "client/templates/play.html"


def _build_session():
    session = Session(id="s-move-rev")
    dm = User(id="dm-1", name="DM", role="dm")
    player = User(id="player-1", name="Player", role="player")
    session.users[dm.id] = dm
    session.users[player.id] = player
    token = Token(
        id="tok-1", name="Hero", x=0, y=0, width=40, height=40,
        color="#fff", shape="circle", owner_id=player.id,
    )
    session.tokens[token.id] = token
    return session, dm, player, token


@pytest.mark.anyio
async def test_token_moved_broadcast_carries_increasing_revision(monkeypatch):
    session, _dm, player, token = _build_session()

    captured = []

    async def _capture_event(manager, sess, msg_type, payload, tok, exclude_user=None):
        captured.append(payload)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setenv("MOVE_COALESCE_WINDOW_MS", "0")
    monkeypatch.setattr("server.handlers.common._broadcast_token_event", _capture_event)
    monkeypatch.setattr("server.handlers.durability.mark_session_dirty", lambda *a, **k: None)
    monkeypatch.setattr(token_handlers, "_broadcast_token_visibility", _noop)
    monkeypatch.setattr(token_handlers, "_process_hazard_triggers_for_token", _noop)
    monkeypatch.setattr(token_handlers, "_process_scene_triggers_for_token", _noop)
    monkeypatch.setattr(token_handlers, "run_combat_fog_sync", _noop)

    await token_handlers.handle_token_move({"token_id": token.id, "x": 10, "y": 10}, session, player)
    await token_handlers.handle_token_move({"token_id": token.id, "x": 20, "y": 20}, session, player)

    assert len(captured) == 2
    first_rev = captured[0]["visibility_revision"]
    second_rev = captured[1]["visibility_revision"]
    assert isinstance(first_rev, int) and first_rev > 0
    assert second_rev > first_rev
    assert session.visibility_revision >= second_rev


@pytest.mark.anyio
async def test_token_moved_omits_revision_only_if_counter_never_bumped(monkeypatch):
    session, _dm, player, token = _build_session()
    assert session.visibility_revision == 0

    captured = []

    async def _capture_event(manager, sess, msg_type, payload, tok, exclude_user=None):
        captured.append(payload)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setenv("MOVE_COALESCE_WINDOW_MS", "0")
    monkeypatch.setattr("server.handlers.common._broadcast_token_event", _capture_event)
    monkeypatch.setattr("server.handlers.durability.mark_session_dirty", lambda *a, **k: None)
    monkeypatch.setattr(token_handlers, "_broadcast_token_visibility", _noop)
    monkeypatch.setattr(token_handlers, "_process_hazard_triggers_for_token", _noop)
    monkeypatch.setattr(token_handlers, "_process_scene_triggers_for_token", _noop)
    monkeypatch.setattr(token_handlers, "run_combat_fog_sync", _noop)

    await token_handlers.handle_token_move({"token_id": token.id, "x": 5, "y": 5}, session, player)

    assert captured[0]["visibility_revision"] == 1


def _stale_guard_snippet() -> str:
    src = PLAY.read_text(encoding="utf-8")
    start = src.index("const _lastVisibilityRevisionByStream = Object.create(null);")
    end = src.index("let _combatFogSyncTimer", start)
    return src[start:end]


def _token_moved_case_snippet() -> str:
    src = PLAY.read_text(encoding="utf-8")
    start = src.index("case 'token_moved': {")
    end = src.index("case 'token_move_denied':", start)
    return src[start:end]


def test_play_html_token_moved_case_checks_stale_visibility_payload():
    snippet = _token_moved_case_snippet()
    assert "_isStaleVisibilityPayload(p)" in snippet
    assert "break;" in snippet


def _run_token_moved_case(payloads):
    """Runs the real token_moved case body (extracted from play.html) against a
    sequence of payloads in a minimal node harness, returning the resulting
    token positions after each payload is processed.
    """
    code = f"""
const ROLE = 'player';
{_stale_guard_snippet()}

const tokens = {{ 'tok-1': {{ id: 'tok-1', x: 0, y: 0 }} }};
const _stagingTokens = {{}};
let _pendingMoveConfirm = null;
const drag = {{ active: false, tokenId: '' }};
function _cacheTokenByContext(tok) {{}}

const results = [];
const payloads = {json.dumps(payloads)};
for (const p of payloads) {{
  switch ('token_moved') {{
    {_token_moved_case_snippet()}
  }}
  results.push({{ x: tokens['tok-1'].x, y: tokens['tok-1'].y }});
}}
console.log(JSON.stringify(results));
"""
    out = subprocess.check_output(["node", "-e", code], cwd=ROOT, text=True, timeout=30)
    return json.loads(out)


def test_stale_token_moved_payload_is_dropped_by_client():
    payloads = [
        {"token_id": "tok-1", "x": 10, "y": 10, "visibility_revision": 5},
        {"token_id": "tok-1", "x": 99, "y": 99, "visibility_revision": 3},
        {"token_id": "tok-1", "x": 20, "y": 20, "visibility_revision": 6},
    ]
    results = _run_token_moved_case(payloads)
    assert results[0] == {"x": 10, "y": 10}
    assert results[1] == {"x": 10, "y": 10}
    assert results[2] == {"x": 20, "y": 20}


def test_token_moved_payload_without_revision_still_applies():
    payloads = [{"token_id": "tok-1", "x": 42, "y": 42}]
    results = _run_token_moved_case(payloads)
    assert results[0] == {"x": 42, "y": 42}
