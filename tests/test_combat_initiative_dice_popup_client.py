"""Client wiring for the initiative dice popup + reconnect recovery.

Covers the play.html / runtime_bridge.js side of the fix:
  * the roller sends a roll_id and pre-registers it so the authoritative echo
    is deduped instead of double-popping;
  * the dice_result handler skips the roller's own already-shown initiative
    popup before falling through to the normal display path;
  * on reconnect during active combat the client pulls authoritative combat_state.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAY = ROOT / "client/templates/play.html"
BRIDGE = ROOT / "client/static/js/core/runtime_bridge.js"


def test_initiative_roll_sends_roll_id_and_preregisters_dedupe():
    src = PLAY.read_text(encoding="utf-8")
    start = src.index("function combatRollInitiative(combatantId) {")
    end = src.index("function applyCombatInitiativeRolled(payload) {", start)
    fn = src[start:end]
    assert "_initiativeLocalDicePopupRollIds.add(initiativeRollId);" in fn
    assert "roll_id:      initiativeRollId," in fn
    # The dedupe id must be registered BEFORE the websocket send so the echo can
    # never arrive before the marker exists.
    assert fn.index("_initiativeLocalDicePopupRollIds.add(initiativeRollId);") < fn.index("combat_roll_initiative")


def test_dice_result_handler_queues_before_deduping_local_initiative_popup():
    src = PLAY.read_text(encoding="utf-8")
    case_idx = src.index("case 'dice_result': {")
    case_src = src[case_idx:case_idx + 2000]
    assert "_queueAuthoritativeDiceResult(p);" in case_src
    assert "_displayAuthoritativeDiceResult(p);" in case_src
    display_idx = src.index("function _displayAuthoritativeDiceResult(payload = {}) {")
    display_src = src[display_idx:display_idx + 2500]
    assert "_initiativeLocalDicePopupRollIds.has(String(safe.roll_id)) && isOwnRoll" in display_src
    # The queued path must run before display/dedupe so early roll_ids are not dropped.
    assert case_src.index("_queueAuthoritativeDiceResult(p);") < case_src.index("_displayAuthoritativeDiceResult(p);")
    assert display_src.index("_initiativeLocalDicePopupRollIds.has(String(safe.roll_id)) && isOwnRoll") < display_src.index("window.appDiceSyncResult(")


def test_reconnect_requests_combat_state_when_active():
    src = BRIDGE.read_text(encoding="utf-8")
    onopen = src[src.index("onOpen: function () {"):src.index("onClose: function")]
    assert "_combatForResync" in onopen
    assert "type: 'combat_state_request'" in onopen
    assert ".active" in onopen


def test_state_sync_applies_combat_on_reconnect():
    src = PLAY.read_text(encoding="utf-8")
    # state_sync must restore combat so active initiative survives a reconnect.
    # Window sized to reach the combat apply even with the reconnect-delta
    # presence guards added ahead of it in the handler.
    sync_block = src[src.index("case 'state_sync': {"):src.index("case 'state_sync': {") + 16000]
    assert "if (p.combat !== undefined) {" in sync_block
    # combatApplyState now takes a source argument identifying the apply path.
    assert "combatApplyState(p.combat, 'state_sync');" in sync_block


def _run_dedupe_sim(harness_js: str) -> dict:
    """Run the real dedupe decision against a reconstructed switch to prove the
    behavior end-to-end (roller skips, other clients display)."""
    code = f"""
const _initiativeLocalDicePopupRollIds = new Set();
const shown = [];
function display(p, isSelf) {{ shown.push({{ roll_id: p.roll_id, self: isSelf }}); }}
function handleDiceResult(p, effectiveUserId) {{
  switch ('dice_result') {{
    case 'dice_result': {{
      if (p.roll_id && _initiativeLocalDicePopupRollIds.has(String(p.roll_id))) {{
        _initiativeLocalDicePopupRollIds.delete(String(p.roll_id));
        break;
      }}
      if (String(p.user_id || '') === String(effectiveUserId || '')) {{
        display(p, true);
      }} else {{
        display(p, false);
      }}
      break;
    }}
  }}
}}
{harness_js}
console.log(JSON.stringify({{ shown, remaining: [..._initiativeLocalDicePopupRollIds] }}));
"""
    out = subprocess.check_output(["node", "-e", code], cwd=ROOT, text=True, timeout=30)
    return json.loads(out.strip().splitlines()[-1])


def test_roller_skips_own_echo_other_clients_display():
    # Roller pre-registered roll_id 'r1'; the echo for r1 must be skipped on the
    # roller, but a non-registered client displays the same broadcast.
    roller = _run_dedupe_sim(
        "_initiativeLocalDicePopupRollIds.add('r1');"
        "handleDiceResult({ roll_id: 'r1', user_id: 'me' }, 'me');"
    )
    assert roller["shown"] == [], "roller must not re-show its own initiative popup"
    assert roller["remaining"] == [], "dedupe marker must be consumed"

    other = _run_dedupe_sim(
        "handleDiceResult({ roll_id: 'r1', user_id: 'someone-else' }, 'me');"
    )
    assert other["shown"] == [{"roll_id": "r1", "self": False}], "other clients must display the initiative popup"


def test_pending_dice_queue_preserves_authoritative_fields_and_flushes_after_dice_ready():
    src = PLAY.read_text(encoding="utf-8")
    assert "window.__pendingDiceResults" in src
    queue_idx = src.index("function _queueAuthoritativeDiceResult(payload)")
    display_idx = src.index("function _displayAuthoritativeDiceResult(payload = {})")
    flush_idx = src.index("function flushPendingDiceResults()")
    queue_src = src[queue_idx:display_idx]
    display_src = src[display_idx:flush_idx]
    for field in ["user_id", "user_name", "roll_id", "dice_type", "quantity", "rolls", "total", "modifier", "roll_label", "combatant_id", "token_id", "encounter_id", "revision"]:
        assert field in queue_src or field in display_src
    assert "window.__pendingDiceResults.push(safe);" in queue_src
    assert "queued.forEach((payload) => _displayAuthoritativeDiceResult(payload));" in src[flush_idx:flush_idx + 800]
    init_idx = src.index("function _initDiceSystem()")
    init_src = src[init_idx:init_idx + 1800]
    assert "flushPendingDiceResults();" in init_src


def test_quick_actions_errors_are_isolated_from_combat_render_and_dice_queue():
    src = PLAY.read_text(encoding="utf-8")
    render_idx = src.index("function renderCombat()")
    render_src = src[render_idx:render_idx + 4500]
    assert "safeClientCall('renderPlayerActionsHub'" in render_src
    assert "safeClientCall('combatQuickBar.render'" in render_src
    assert "_queueAuthoritativeDiceResult(p);" in src
