"""Single WebSocket-owner guarantees that defend against the player reconnect storm.

These tests load the real ``client/static/js/core/ws.js`` module into a minimal
Node sandbox and drive its public ``window.AppWS`` API to assert the single-owner
contract:

  1. Loading the player boot path constructs the WebSocket exactly once.
  2. ``ensureConnected()`` called from multiple boot modules still yields one socket.
  3. An old socket's close does NOT reconnect when a newer socket is active.
  4-6. ``request_state`` / ``combat_state_request`` / ``treasury_get`` are sent once
       per real socket open (idempotent via ``requestInitialStateOnce``).
  7. No reconnect happens while the socket is open and the heartbeat is healthy.
  8. Reconnect happens only after a real close (heartbeat timeout / drop), and never
     when the server says the socket was replaced by a newer connection.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WS = ROOT / "client/static/js/core/ws.js"
BRIDGE = ROOT / "client/static/js/core/runtime_bridge.js"


def _run(harness_js: str) -> dict:
    """Run ws.js in a Node sandbox with a counting WebSocket constructor."""
    src = WS.read_text(encoding="utf-8")
    code = f"""
const sends = [];
let constructed = 0;
let setTimeoutCalls = 0;
const timeoutDelays = [];
const timers = [];
class FakeSocket {{
  constructor(url) {{
    this.url = url;
    this.readyState = 0; // CONNECTING
    this.onopen = null; this.onclose = null; this.onerror = null; this.onmessage = null;
    this.closeCalls = [];
  }}
  send(data) {{ sends.push(JSON.parse(data)); }}
  close(code, reason) {{ this.readyState = 3; this.closeCalls.push([code, reason]); }}
  open() {{ this.readyState = 1; if (this.onopen) this.onopen(); }}
}}
const WebSocketCtor = function (url) {{ constructed += 1; return new FakeSocket(url); }};
WebSocketCtor.CONNECTING = 0; WebSocketCtor.OPEN = 1; WebSocketCtor.CLOSING = 2; WebSocketCtor.CLOSED = 3;
let _socketRef = null;
let _reconnectTimer = null;
const win = {{
  WebSocket: WebSocketCtor,
  location: {{ protocol: 'http:', host: 'localhost', hostname: 'localhost' }},
  // The silence watchdog (ws.js) arms a ~45s timer on every open/message; it is
  // covered by tests/test_ws_client_seq_watchdog.py. Exclude it (>= 40s) from
  // the reconnect-timer accounting these storm tests assert on — reconnect
  // backoff delays are capped at 30s so the two ranges cannot collide.
  setTimeout: (fn, ms) => {{ if (ms < 40000) {{ setTimeoutCalls += 1; timeoutDelays.push(ms); }} const id = timers.length + 1; timers.push(fn); return id; }},
  clearTimeout: () => {{}},
  Math: Object.assign(Object.create(globalThis.Math), {{ random: () => 0.5 }}),
  addEventListener: () => {{}},
  sessionStorage: {{ getItem: () => '' }},
  console: console,
}};
win.window = win;
const window = win;
const Math = win.Math;
{src}
window.AppWS.configure({{
  getSessionId: () => 's1',
  getUserId: () => 'u1',
  getRole: () => 'player',
  getSocket: () => _socketRef,
  setSocket: (v) => {{ _socketRef = v; }},
  getReconnectTimer: () => _reconnectTimer,
  setReconnectTimer: (v) => {{ _reconnectTimer = v; }},
  getPendingMessages: () => [],
  setPendingMessages: () => {{}},
  getQueuedEditorTypes: () => new Set(),
  onMessage: () => {{}},
  onOpen: () => {{}},
  onClose: () => {{}},
  onCloseExpired: () => {{}},
}});
const runTimers = () => {{ const pending = timers.splice(0); pending.forEach((fn) => fn()); }};
const getSocket = () => _socketRef;
{harness_js}
console.log('@@RESULT@@' + JSON.stringify(out));
"""
    raw = subprocess.check_output(["node", "-e", code], cwd=ROOT, text=True, timeout=30)
    line = [ln for ln in raw.splitlines() if ln.startswith("@@RESULT@@")][-1]
    return json.loads(line[len("@@RESULT@@"):])


def test_player_page_constructs_websocket_exactly_once():
    out = _run(
        """
        window.AppWS.ensureConnected({ reason: 'boot' });
        const out = { constructed };
        """
    )
    assert out["constructed"] == 1, "Player boot must construct exactly one WebSocket"


def test_ensure_connected_from_multiple_boot_modules_creates_one_socket():
    out = _run(
        """
        // Simulate boot_shell, player_shell, render/boot and a state refresh each
        // calling ensureConnected during startup.
        window.AppWS.ensureConnected({ reason: 'boot_shell' });
        window.AppWS.ensureConnected({ reason: 'player_shell' });
        getSocket().open();
        window.AppWS.ensureConnected({ reason: 'render_boot' });
        window.AppWS.ensureConnected({ reason: 'state_refresh' });
        const out = { constructed, debug: window.__debugWS() };
        """
    )
    assert out["constructed"] == 1, "Multiple ensureConnected callers must share one socket"
    assert out["debug"]["connectCallCount"] >= 1
    assert out["debug"]["duplicateConnectCount"] >= 1, "Redundant connects must be tracked"


def test_old_socket_close_does_not_reconnect_when_newer_socket_active():
    out = _run(
        """
        // First socket opens.
        const first = window.AppWS.connectWS();
        first.open();
        // A newer socket replaces it as the active owner (e.g. forced reconnect).
        _socketRef = null;
        const second = window.AppWS.connectWS();
        second.open();
        const constructedBefore = constructed;
        // The OLD socket now closes late. It is no longer the active socket, so it
        // must neither reconnect nor schedule a reconnect timer.
        first.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const out = { constructedBefore, constructedAfter: constructed, active: getSocket() === second };
        """
    )
    assert out["active"], "The newer socket must remain the active owner"
    assert out["constructedAfter"] == out["constructedBefore"], (
        "A stale old-socket close must not create another socket"
    )


def test_request_state_sent_once_per_real_socket_open():
    out = _run(
        """
        const sock = window.AppWS.connectWS();
        sock.open();
        const send = () => window.AppWS.send({ type: 'request_state', payload: {} });
        // Many modules race to request state on the same open socket.
        window.AppWS.requestInitialStateOnce(send);
        window.AppWS.requestInitialStateOnce(send);
        window.AppWS.requestInitialStateOnce(send);
        const count = sends.filter((m) => m.type === 'request_state').length;
        const out = { count };
        """
    )
    assert out["count"] == 1, "request_state must be sent exactly once per real socket open"


def test_combat_state_request_sent_once_per_real_socket_open():
    out = _run(
        """
        const sock = window.AppWS.connectWS();
        sock.open();
        const send = () => window.AppWS.send({ type: 'combat_state_request', payload: {} });
        window.AppWS.requestInitialStateOnce(send);
        window.AppWS.requestInitialStateOnce(send);
        const out = { count: sends.filter((m) => m.type === 'combat_state_request').length };
        """
    )
    assert out["count"] == 1, "combat_state_request must be sent once per real socket open"


def test_treasury_get_sent_once_per_real_socket_open():
    out = _run(
        """
        const sock = window.AppWS.connectWS();
        sock.open();
        const send = () => window.AppWS.send({ type: 'treasury_get', payload: {} });
        window.AppWS.requestInitialStateOnce(send);
        window.AppWS.requestInitialStateOnce(send);
        const out = { count: sends.filter((m) => m.type === 'treasury_get').length };
        """
    )
    assert out["count"] == 1, "treasury_get must be sent once per real socket open"


def test_initial_state_sent_again_after_a_genuine_reconnect():
    out = _run(
        """
        const send = () => window.AppWS.send({ type: 'request_state', payload: {} });
        const first = window.AppWS.connectWS();
        first.open();
        window.AppWS.requestInitialStateOnce(send);
        // Genuine drop -> reconnect mints a brand new socket (new client_socket_id).
        first.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const second = getSocket();
        second.open();
        window.AppWS.requestInitialStateOnce(send);
        const out = { count: sends.filter((m) => m.type === 'request_state').length, constructed };
        """
    )
    assert out["constructed"] == 2, "A genuine reconnect must create a new socket"
    assert out["count"] == 2, "Each real socket open gets its own initial-state request"


def test_no_reconnect_while_socket_open_and_heartbeat_healthy():
    out = _run(
        """
        const sock = window.AppWS.connectWS();
        sock.open();
        // Healthy heartbeat: server ping -> client pong, repeatedly, no close.
        for (let i = 0; i < 5; i += 1) {
          sock.onmessage({ data: JSON.stringify({ type: 'ping' }) });
        }
        runTimers();
        const pongs = sends.filter((m) => m.type === 'pong').length;
        const out = { constructed, pongs, reconnectTimer: _reconnectTimer };
        """
    )
    assert out["constructed"] == 1, "Healthy heartbeat must not spawn a new socket"
    assert out["pongs"] == 5, "Each ping must be answered with a pong"
    assert out["reconnectTimer"] in (None, 0), "No reconnect must be scheduled while open"


def test_reconnect_happens_after_real_close_but_not_after_replacement():
    out = _run(
        """
        // Case A: a real drop (1006) schedules a reconnect and re-opens a socket.
        const a = window.AppWS.connectWS();
        a.open();
        a.onclose({ code: 1006, reason: '', wasClean: false });
        const scheduledAfterDrop = _reconnectTimer != null && _reconnectTimer !== 0;
        runTimers();
        const constructedAfterDrop = constructed; // should be 2

        // Case B: server replaced this socket with a newer connection -> NO reconnect.
        const b = getSocket();
        b.open();
        b.onclose({ code: 1001, reason: 'Replaced by a newer connection', wasClean: true });
        const scheduledAfterReplace = _reconnectTimer != null && _reconnectTimer !== 0;
        runTimers();
        const constructedAfterReplace = constructed; // should stay 2

        const out = { scheduledAfterDrop, constructedAfterDrop, scheduledAfterReplace, constructedAfterReplace };
        """
    )
    assert out["scheduledAfterDrop"], "A real close must schedule a reconnect"
    assert out["constructedAfterDrop"] == 2, "A real close must reconnect in-place"
    assert not out["scheduledAfterReplace"], (
        "A 'replaced by a newer connection' close must NOT schedule a reconnect"
    )
    assert out["constructedAfterReplace"] == 2, (
        "Replacement close must not start a reconnect war (no new socket)"
    )


def test_reconnect_uses_capped_exponential_backoff_with_jitter_and_resets_on_open():
    out = _run(
        """
        const first = window.AppWS.connectWS();
        first.open();

        // Consecutive failed reconnect attempts should schedule increasing delays.
        first.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const second = getSocket();
        second.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const third = getSocket();
        third.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const fourth = getSocket();
        fourth.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const fifth = getSocket();
        fifth.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const sixth = getSocket();
        sixth.onclose({ code: 1006, reason: '', wasClean: false });

        const beforeReset = timeoutDelays.slice();
        runTimers();
        const seventh = getSocket();
        seventh.open();
        seventh.onclose({ code: 1006, reason: '', wasClean: false });
        const afterResetDelay = timeoutDelays[timeoutDelays.length - 1];

        const out = { beforeReset, afterResetDelay, debug: window.__debugWS() };
        """
    )
    assert out["beforeReset"][:5] == [3000, 6000, 12000, 24000, 30000]
    assert max(out["beforeReset"]) <= 30000, "Reconnect delay must never exceed 30 seconds"
    assert out["afterResetDelay"] == 3000, "Successful open must reset backoff toward 3 seconds"
    assert out["debug"]["reconnectAttempts"] == 0


def test_reconnect_jitter_stays_within_ten_percent_without_exceeding_cap():
    out = _run(
        """
        window.Math.random = () => 1;
        const first = window.AppWS.connectWS();
        first.open();
        first.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const second = getSocket();
        second.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const third = getSocket();
        third.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const fourth = getSocket();
        fourth.onclose({ code: 1006, reason: '', wasClean: false });
        runTimers();
        const fifth = getSocket();
        fifth.onclose({ code: 1006, reason: '', wasClean: false });
        const high = timeoutDelays.slice();

        window.Math.random = () => 0;
        runTimers();
        const sixth = getSocket();
        sixth.open();
        sixth.onclose({ code: 1006, reason: '', wasClean: false });
        const lowAfterReset = timeoutDelays[timeoutDelays.length - 1];

        const out = { high, lowAfterReset };
        """
    )
    assert out["high"][:4] == [3300, 6600, 13200, 26400]
    assert out["high"][4] == 30000
    assert max(out["high"]) <= 30000
    assert out["lowAfterReset"] == 2700


def test_reconnect_close_path_keeps_only_one_timer():
    out = _run(
        """
        const sock = window.AppWS.connectWS();
        sock.open();
        sock.onclose({ code: 1006, reason: '', wasClean: false });
        const timerAfterFirstClose = _reconnectTimer;
        // A duplicate close notification for the same inactive socket must not
        // schedule a second reconnect timer.
        sock.onclose({ code: 1006, reason: '', wasClean: false });
        const timerAfterSecondClose = _reconnectTimer;
        const out = { setTimeoutCalls, timerAfterFirstClose, timerAfterSecondClose, constructed };
        """
    )
    assert out["setTimeoutCalls"] == 1
    assert out["timerAfterSecondClose"] == out["timerAfterFirstClose"]
    assert out["constructed"] == 1


def test_debug_ws_exposes_owner_diagnostics():
    out = _run(
        """
        const sock = window.AppWS.ensureConnected({ reason: 'boot' });
        sock.open();
        window.AppWS.requestInitialStateOnce(() => window.AppWS.send({ type: 'request_state', payload: {} }));
        const out = { debug: window.__debugWS() };
        """
    )
    debug = out["debug"]
    for key in (
        "role", "sessionId", "userId", "activeSocketId", "readyState",
        "reconnectAttempts", "lastConnectReason", "lastCloseCode", "lastCloseReason",
        "lastRequestStateSentAt", "connectCallCount", "duplicateConnectStacks",
    ):
        assert key in debug, f"__debugWS() must expose {key}"
    assert debug["lastConnectReason"] == "boot"
    assert debug["activeSocketId"], "active client_socket_id must be set after connect"
    assert debug["lastRequestStateSentAt"], "lastRequestStateSentAt must be recorded"


def test_runtime_bridge_routes_initial_state_through_request_once():
    src = BRIDGE.read_text(encoding="utf-8")
    onopen = src[src.index("onOpen: function () {"):src.index("onClose: function")]
    # The single-owner once-guard funnels the initial state requests.
    assert "AppWS.requestInitialStateOnce" in onopen
    # And the three initial-state messages still live inside onOpen.
    assert "type: 'request_state'" in onopen
    assert "type: 'treasury_get'" in onopen
    assert "type: 'combat_state_request'" in onopen
    assert onopen.index("type: 'request_state'") < onopen.index("type: 'combat_state_request'")


def test_server_connection_manager_logs_replacement_diagnostics():
    src = (ROOT / "server/connections.py").read_text(encoding="utf-8")
    assert "old_connection_id=%s" in src
    assert "new_connection_id=%s" in src
    assert "old_socket_open=%s" in src
    assert "client_socket_id=%s" in src
    assert "user_agent=%s" in src
    assert "def _socket_is_open(" in src
