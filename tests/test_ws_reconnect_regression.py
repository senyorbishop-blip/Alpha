from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAY = ROOT / "client/templates/play.html"
WS = ROOT / "client/static/js/core/ws.js"
BRIDGE = ROOT / "client/static/js/core/runtime_bridge.js"
MAIN = ROOT / "main.py"
COMBAT = ROOT / "server/handlers/combat.py"


def test_ws_core_logs_version_and_ping_pong_flow():
    src = WS.read_text(encoding="utf-8")
    assert "heartbeat-pong-v6-seq-gap-watchdog" in src
    assert "console.info('[WS] core loaded version', CORE_WS_VERSION);" in src
    assert "wsDebugLog('[WS] received ping');" in src
    assert "wsDebugLog('[WS] sent pong');" in src
    assert "console.warn('[WS] pong send failed', err);" in src
    assert "if (msg && msg.type === 'ping')" in src
    assert "sendPong(socket);" in src


def test_server_logs_pong_and_updates_last_seen_for_any_frame():
    src = MAIN.read_text(encoding="utf-8")
    assert 'logger.debug("[WS] sending ping user_id=%s session_id=%s connection_id=%s"' in src
    assert 'logger.debug("[WS] received frame type=%s user_id=%s connection_id=%s last_seen updated"' in src
    assert 'logger.debug("[WS] pong received user_id=%s connection_id=%s"' in src
    assert '_last_pong["t"] = asyncio.get_running_loop().time()' in src
    assert src.index('_last_pong["t"] = asyncio.get_running_loop().time()') < src.index('if msg_type == "pong":')


def test_play_loads_ws_with_cache_busting_and_no_inline_websocket_bypass():
    src = PLAY.read_text(encoding="utf-8")
    assert '<script src="/static/js/core/ws.js?v=heartbeat-pong-v6-seq-gap-watchdog"></script>' in src
    assert "new WebSocket" not in src
    ping_case = src[src.index("case 'ping': {"):src.index("case 'user_joined': {", src.index("case 'ping': {"))]
    assert "Transport heartbeat pongs are handled in core/ws.js" in ping_case
    assert "sendWS({ type: 'pong' })" not in ping_case


def test_reconnect_requests_state_then_combat_and_reapplies_dm_fog_preview():
    src = BRIDGE.read_text(encoding="utf-8")
    onopen = src[src.index("onOpen: function () {"):src.index("onClose: function")]
    assert "type: 'request_state'" in onopen
    assert "reason: 'reconnect'" in onopen
    assert "type: 'combat_state_request'" in onopen
    assert onopen.index("type: 'request_state'") < onopen.index("type: 'combat_state_request'")
    assert "reapplyDmFogPreviewAfterReconnect" in onopen


def test_empty_no_revision_combat_state_is_noop_unless_explicit_clear():
    src = PLAY.read_text(encoding="utf-8")
    assert "function _isAccidentalEmptyCombatState(state)" in src
    helper = src[src.index("function _isAccidentalEmptyCombatState(state)"):src.index("function combatApplyState(state, source)")]
    assert "incomingRevision === null" in helper
    assert "incomingCombatants.length === 0" in helper
    assert "_combat && _combat.active" in helper
    assert "reason === 'clear_combat'" in helper
    start = src.index("function combatApplyState(state, source)")
    apply = src[start:start + 2500]
    assert "_isAccidentalEmptyCombatState(state)" in apply
    assert "ignored empty/no-revision combat_state during reconnect" in apply
    assert "return false;" in apply


def test_explicit_combat_clear_payload_marks_reason():
    src = COMBAT.read_text(encoding="utf-8")
    assert '"reason": "clear_combat"' in src
    assert '"clear_reason": "clear_combat"' in src


def test_state_sync_applies_ordered_fog_tokens_combat_and_dm_preview_persistence():
    src = PLAY.read_text(encoding="utf-8")
    sync_block = src[src.index("case 'state_sync': {"):src.index("requestRenderFrame('state_sync render')")]
    assert "applyAuthoritativeTokenSnapshot({ tokens: p.tokens || {}, token_state_revision: _lastTokenStateRevision }, 'state_sync');" in sync_block
    # PR 6: state_sync now routes its fog block through the single
    # applyAuthoritativeFogState() entry point instead of calling
    # fogApplyState() directly.
    assert "applyAuthoritativeFogState(p, 'state_sync');" in sync_block
    assert "combatApplyState(p.combat, 'state_sync');" in sync_block
    assert sync_block.index("applyAuthoritativeFogState(p, 'state_sync');") < sync_block.index("combatApplyState(p.combat, 'state_sync');")
    assert "DM_FOG_PREVIEW_STORAGE_KEY" in src
    assert "function reapplyDmFogPreviewAfterReconnect()" in src
    assert "window.localStorage.setItem(DM_FOG_PREVIEW_STORAGE_KEY" in src
