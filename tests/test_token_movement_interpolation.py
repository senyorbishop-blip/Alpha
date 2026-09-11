"""Tests for remote token-movement interpolation (realtime sync tiny patch).

Server-side `handle_token_move` is unchanged: it remains the sole source of
truth for token x/y and keeps stamping the monotonic `visibility_revision`
counter (see test_token_move_revision_guard.py for that contract).

Client-side, the `token_moved` case in play.html now decides whether a
non-stale remote move should be glided toward (`queueTokenInterpolation`)
or snapped immediately (`shouldSnapTokenMovement`). Either way the final
committed token position must equal the latest server-approved x/y.
"""
import json
import subprocess
from pathlib import Path

import pytest

from server.handlers import tokens as token_handlers
from server.session import Session, Token, User

ROOT = Path(__file__).resolve().parents[1]
PLAY = ROOT / "client/templates/play.html"


def _build_session():
    session = Session(id="s-move-interp")
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
async def test_handle_token_move_still_stamps_increasing_revision(monkeypatch):
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
    assert captured[1]["visibility_revision"] > captured[0]["visibility_revision"]
    assert token.x == 20 and token.y == 20


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


def test_play_html_defines_interpolation_helpers():
    snippet = _stale_guard_snippet()
    assert "function shouldSnapTokenMovement(" in snippet
    assert "function queueTokenInterpolation(" in snippet
    assert "function applyTokenInterpolationFrame(" in snippet


def test_play_html_token_moved_case_still_checks_stale_visibility_payload():
    snippet = _token_moved_case_snippet()
    assert "_isStaleVisibilityPayload(p)" in snippet
    assert "break;" in snippet


def _run_node(initial_tokens, payloads, *, with_raf=True, drag=None, extra_setup=""):
    raf_polyfill = """
function requestAnimationFrame(cb) { return 1; }
function _nowMs() { return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now(); }
function flushRaf(advanceMs) {
  applyTokenInterpolationFrame(_nowMs() + (Number(advanceMs) || 0));
}
""" if with_raf else "function flushRaf(advanceMs) {}"

    drag_obj = drag or {"active": False, "tokenId": ""}

    code = f"""
const ROLE = 'player';
{raf_polyfill}
{_stale_guard_snippet()}

const tokens = {json.dumps(initial_tokens)};
const _stagingTokens = {{}};
let _pendingMoveConfirm = null;
const drag = {json.dumps(drag_obj)};
let _renderLoopStarted = {str(with_raf).lower()};
function _cacheTokenByContext(tok) {{}}
{extra_setup}

const results = [];
const payloads = {json.dumps(payloads)};
for (const p of payloads) {{
  switch ('token_moved') {{
    {_token_moved_case_snippet()}
  }}
  if (p.__advanceMs) flushRaf(p.__advanceMs);
  results.push(JSON.parse(JSON.stringify(tokens)));
}}
console.log(JSON.stringify(results));
"""
    out = subprocess.check_output(["node", "-e", code], cwd=ROOT, text=True, timeout=30)
    return json.loads(out)


def test_valid_token_moved_updates_authoritative_target():
    initial = {"tok-1": {"id": "tok-1", "x": 0, "y": 0}}
    payloads = [{"token_id": "tok-1", "x": 30, "y": 30, "visibility_revision": 1, "__advanceMs": 1000}]
    results = _run_node(initial, payloads)
    assert results[0]["tok-1"]["x"] == 30
    assert results[0]["tok-1"]["y"] == 30


def test_missing_revision_payload_still_applies_backward_compatible():
    initial = {"tok-1": {"id": "tok-1", "x": 0, "y": 0}}
    payloads = [{"token_id": "tok-1", "x": 12, "y": 12, "__advanceMs": 1000}]
    results = _run_node(initial, payloads)
    assert results[0]["tok-1"]["x"] == 12
    assert results[0]["tok-1"]["y"] == 12


def test_teleport_distance_snaps_immediately_without_animation():
    initial = {"tok-1": {"id": "tok-1", "x": 0, "y": 0}}
    payloads = [{"token_id": "tok-1", "x": 5000, "y": 5000, "visibility_revision": 1}]
    results = _run_node(initial, payloads, with_raf=True)
    assert results[0]["tok-1"]["x"] == 5000
    assert results[0]["tok-1"]["y"] == 5000


def test_hidden_token_does_not_interpolate_and_snaps():
    initial = {"tok-1": {"id": "tok-1", "x": 0, "y": 0, "hidden": True}}
    payloads = [{"token_id": "tok-1", "x": 15, "y": 15, "visibility_revision": 1}]
    results = _run_node(initial, payloads, with_raf=True)
    assert results[0]["tok-1"]["x"] == 15
    assert results[0]["tok-1"]["y"] == 15


def test_removed_token_path_is_a_noop_not_an_error():
    initial = {}
    payloads = [{"token_id": "tok-1", "x": 15, "y": 15, "visibility_revision": 1}]
    results = _run_node(initial, payloads, with_raf=True)
    assert results[0] == {}


def test_interpolation_retargets_when_newer_token_moved_arrives_mid_flight():
    initial = {"tok-1": {"id": "tok-1", "x": 0, "y": 0}}
    payloads = [
        {"token_id": "tok-1", "x": 40, "y": 0, "visibility_revision": 1},
        {"token_id": "tok-1", "x": 80, "y": 0, "visibility_revision": 2, "__advanceMs": 1000},
    ]
    results = _run_node(initial, payloads, with_raf=True)
    assert results[1]["tok-1"]["x"] == 80
    assert results[1]["tok-1"]["y"] == 0


def test_final_committed_state_equals_latest_server_position_after_full_flight():
    initial = {"tok-1": {"id": "tok-1", "x": 0, "y": 0}}
    payloads = [{"token_id": "tok-1", "x": 25, "y": 50, "visibility_revision": 1, "__advanceMs": 5000}]
    results = _run_node(initial, payloads, with_raf=True)
    assert results[0]["tok-1"]["x"] == 25
    assert results[0]["tok-1"]["y"] == 50


def test_local_drag_path_is_not_delayed_by_interpolation():
    initial = {"tok-1": {"id": "tok-1", "x": 7, "y": 9}}
    payloads = [{"token_id": "tok-1", "x": 999, "y": 999, "visibility_revision": 1}]
    results = _run_node(initial, payloads, with_raf=True, drag={"active": True, "tokenId": "tok-1"})
    assert results[0]["tok-1"]["x"] == 7
    assert results[0]["tok-1"]["y"] == 9


def test_fallback_applies_direct_movement_when_raf_unavailable():
    initial = {"tok-1": {"id": "tok-1", "x": 0, "y": 0}}
    payloads = [{"token_id": "tok-1", "x": 30, "y": 30, "visibility_revision": 1}]
    results = _run_node(initial, payloads, with_raf=False)
    assert results[0]["tok-1"]["x"] == 30
    assert results[0]["tok-1"]["y"] == 30


def test_stale_visibility_revision_payload_still_rejected_with_interpolation_path():
    initial = {"tok-1": {"id": "tok-1", "x": 10, "y": 10}}
    payloads = [
        {"token_id": "tok-1", "x": 10, "y": 10, "visibility_revision": 5, "__advanceMs": 1000},
        {"token_id": "tok-1", "x": 99, "y": 99, "visibility_revision": 3, "__advanceMs": 1000},
        {"token_id": "tok-1", "x": 20, "y": 20, "visibility_revision": 6, "__advanceMs": 1000},
    ]
    results = _run_node(initial, payloads, with_raf=True)
    assert results[1]["tok-1"]["x"] == 10 and results[1]["tok-1"]["y"] == 10
    assert results[2]["tok-1"]["x"] == 20 and results[2]["tok-1"]["y"] == 20
