"""Client push-loss defenses in the websocket core (client/static/js/core/ws.js).

Two independent layers, both required for the mid-session desync fix:

  * seq gap detection — every server push carries a per-connection ``seq``;
    a jump means a push was lost, and the client must ask for a delta resync
    (via the onSeqGap hook) without tearing the socket down;
  * silence watchdog — a half-open socket looks OPEN to the browser forever;
    if no server frame of any kind arrives for 45s the client closes the
    socket itself so the normal onclose → reconnect path runs.

The tests load the real ws.js into a Node sandbox with controllable time and
timers, mirroring tests/test_ws_heartbeat_pong_client.py.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WS = ROOT / "client/static/js/core/ws.js"


def _run(harness_js: str, *, clear_timeout_noop: bool = False) -> dict:
    src = WS.read_text(encoding="utf-8")
    clear_timeout_impl = "() => {}" if clear_timeout_noop else "(id) => { timers.delete(id); }"
    code = f"""
const sends = [];
const dispatched = [];
const gaps = [];
const closes = [];

// Controllable clock + timers so the 45s watchdog can be driven directly.
let now = 0;
Date.now = () => now;
const timers = new Map();
let nextTimerId = 1;
function fakeSetTimeout(fn, ms) {{ const id = nextTimerId++; timers.set(id, {{ fn, at: now + Number(ms || 0) }}); return id; }}
const fakeClearTimeout = {clear_timeout_impl};
function advance(ms) {{
  const target = now + ms;
  // Fire due timers in time order, allowing re-arms scheduled during firing.
  for (;;) {{
    let dueId = null, dueAt = Infinity;
    for (const [id, t] of timers) {{ if (t.at <= target && t.at < dueAt) {{ dueAt = t.at; dueId = id; }} }}
    if (dueId === null) break;
    const t = timers.get(dueId);
    timers.delete(dueId);
    now = t.at;
    t.fn();
  }}
  now = target;
}}

class FakeSocket {{
  constructor() {{ this.readyState = 1; this.onopen = null; this.onclose = null; this.onerror = null; this.onmessage = null; }}
  send(data) {{ sends.push(JSON.parse(data)); }}
  close(code, reason) {{ closes.push({{ code: code, reason: reason }}); this.readyState = 3; }}
}}
const WebSocketCtor = function () {{ return new FakeSocket(); }};
WebSocketCtor.CONNECTING = 0; WebSocketCtor.OPEN = 1; WebSocketCtor.CLOSING = 2; WebSocketCtor.CLOSED = 3;
const win = {{
  WebSocket: WebSocketCtor,
  location: {{ protocol: 'http:', host: 'localhost' }},
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  addEventListener: () => {{}},
  sessionStorage: {{ getItem: () => '' }},
  console: console,
}};
win.window = win;
const window = win;
{src}
let _socketRef = null;
window.AppWS.configure({{
  getSessionId: () => 's1',
  getUserId: () => 'u1',
  getRole: () => 'player',
  getSocket: () => _socketRef,
  setSocket: (v) => {{ _socketRef = v; }},
  getReconnectTimer: () => null,
  setReconnectTimer: () => {{}},
  getPendingMessages: () => [],
  setPendingMessages: () => {{}},
  getQueuedEditorTypes: () => new Set(),
  onMessage: (m) => {{ dispatched.push(m); }},
  onOpen: () => {{}},
  onClose: () => {{}},
  onSeqGap: (gap) => {{ gaps.push(gap); }},
}});
const sock = window.AppWS.connectWS();
function deliver(msg) {{ sock.onmessage({{ data: JSON.stringify(msg) }}); }}
{harness_js}
console.log(JSON.stringify({{ sends, dispatched, gaps, closes, timerCount: timers.size }}));
"""
    out = subprocess.check_output(["node", "-e", code], cwd=ROOT, text=True, timeout=30)
    return json.loads(out.strip().splitlines()[-1])


# ── seq gap detection ────────────────────────────────────────────────────────

def test_contiguous_seqs_do_not_trigger_gap():
    result = _run(
        """
sock.onopen();
deliver({ type: 'state_sync', seq: 1 });
deliver({ type: 'token_moved', seq: 2 });
deliver({ type: 'ping', seq: 3 });
"""
    )
    assert result["gaps"] == []
    assert [m["type"] for m in result["dispatched"]] == ["state_sync", "token_moved"]


def test_seq_gap_triggers_resync_hook_with_expected_and_received():
    result = _run(
        """
sock.onopen();
deliver({ type: 'state_sync', seq: 1 });
deliver({ type: 'token_moved', seq: 2 });
deliver({ type: 'token_moved', seq: 4 });
"""
    )
    assert result["gaps"] == [{"expected": 3, "received": 4}]
    # The gap message itself is still dispatched — resync repairs the hole, the
    # newest event should not be dropped while waiting for it.
    assert [m["type"] for m in result["dispatched"]] == ["state_sync", "token_moved", "token_moved"]


def test_first_seq_on_fresh_socket_sets_baseline_without_false_gap():
    # The server resets seq per connection, but a defensive client must not
    # treat an unexpected starting value as a gap — there is no baseline yet.
    result = _run(
        """
sock.onopen();
deliver({ type: 'state_sync', seq: 7 });
deliver({ type: 'token_moved', seq: 8 });
"""
    )
    assert result["gaps"] == []


def test_out_of_order_or_duplicate_seq_is_not_a_gap():
    result = _run(
        """
sock.onopen();
deliver({ type: 'a', seq: 1 });
deliver({ type: 'b', seq: 2 });
deliver({ type: 'late', seq: 2 });
deliver({ type: 'later', seq: 1 });
deliver({ type: 'c', seq: 3 });
"""
    )
    assert result["gaps"] == []


def test_messages_without_seq_do_not_affect_tracking():
    result = _run(
        """
sock.onopen();
deliver({ type: 'state_sync', seq: 1 });
deliver({ type: 'legacy_no_seq' });
deliver({ type: 'token_moved', seq: 2 });
"""
    )
    assert result["gaps"] == []


def test_gap_resyncs_are_debounced_then_allowed_again():
    result = _run(
        """
sock.onopen();
deliver({ type: 'a', seq: 1 });
deliver({ type: 'b', seq: 3 });   // gap #1 -> resync
deliver({ type: 'c', seq: 6 });   // gap #2 within debounce window -> suppressed
advance(6000);
deliver({ type: 'd', seq: 9 });   // gap #3 after window -> resync
"""
    )
    assert result["gaps"] == [
        {"expected": 2, "received": 3},
        {"expected": 7, "received": 9},
    ]


# ── silence watchdog ─────────────────────────────────────────────────────────

def test_watchdog_closes_socket_after_45s_of_silence():
    result = _run(
        """
sock.onopen();
advance(45000);
"""
    )
    assert result["closes"], "watchdog must close a silent socket"
    assert result["closes"][0]["code"] == 4008
    assert result["gaps"] == []


def test_watchdog_rearms_on_any_server_frame_including_ping():
    result = _run(
        """
sock.onopen();
advance(30000);
deliver({ type: 'ping', seq: 1 });   // heartbeat traffic counts as liveness
advance(30000);                       // 30s since last frame: still alive
const closesAt60 = closes.length;
advance(15000);                       // 45s since last frame: dead
console.error('closesAt60=' + closesAt60);
"""
    )
    assert result["closes"] and result["closes"][0]["code"] == 4008
    # The pong reply to the ping proves the heartbeat path still works too.
    assert {"type": "pong"} in result["sends"]


def test_watchdog_does_not_close_while_frames_keep_arriving():
    result = _run(
        """
sock.onopen();
for (let i = 1; i <= 10; i++) { advance(20000); deliver({ type: 'ping', seq: i }); }
advance(20000);
"""
    )
    assert result["closes"] == []


def test_throttled_timer_rechecks_silence_instead_of_closing():
    # Background tabs throttle timers: the armed timeout can fire long after
    # 45s even though frames kept arriving (message events are not throttled).
    # Simulate by making clearTimeout a no-op so stale timers still fire.
    result = _run(
        """
sock.onopen();
advance(40000);
deliver({ type: 'token_moved', seq: 1 });  // re-arms, but stale timer survives
advance(5000);                              // stale timer fires at t=45000
const closedByStaleTimer = closes.length;
console.error('closedByStaleTimer=' + closedByStaleTimer);
advance(40001);                             // now genuinely silent for 45s+
""",
        clear_timeout_noop=True,
    )
    assert result["closes"], "genuine silence must still close the socket"
    assert result["closes"][0]["code"] == 4008


def test_watchdog_ignores_superseded_socket():
    result = _run(
        """
sock.onopen();
_socketRef = null;   // socket was torn down / replaced elsewhere
advance(45001);
"""
    )
    assert result["closes"] == []


# ── runtime bridge: gap → request_state delta resync ─────────────────────────

def test_runtime_bridge_seq_gap_sends_request_state_with_known_revisions():
    bridge = (ROOT / "client/static/js/core/runtime_bridge.js").read_text(encoding="utf-8")
    code = f"""
const sent = [];
const win = {{
  document: {{ getElementById: () => null }},
  location: {{ search: '' }},
  console: console,
  AppWS: {{ send: (m) => sent.push(m) }},
  collectKnownStateRevisions: () => ({{ token_state_revision: 5, inventory_revision: 2 }}),
}};
win.window = win;
const window = win;
{bridge}
const cfg = win.AppRuntimeBridge.createWsConfig();
cfg.onSeqGap({{ expected: 3, received: 7 }});
console.log(JSON.stringify(sent));
"""
    out = subprocess.check_output(["node", "-e", code], cwd=ROOT, text=True, timeout=30)
    sent = json.loads(out.strip().splitlines()[-1])
    assert len(sent) == 1
    msg = sent[0]
    assert msg["type"] == "request_state"
    assert msg["payload"]["reason"] == "seq_gap"
    assert msg["payload"]["seq_gap"] == {"expected": 3, "received": 7}
    assert msg["payload"]["known_revisions"] == {"token_state_revision": 5, "inventory_revision": 2}
