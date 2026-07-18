(function (global) {
  'use strict';

  const CORE_WS_VERSION = 'heartbeat-pong-v6-seq-gap-watchdog';
  const WS_DEBUG = !!global.__LIVE_DEBUG__ || (global.localStorage && global.localStorage.getItem('dnd_live_debug') === '1');
  function wsDebugLog() { if (WS_DEBUG && global.console && console.debug) console.debug.apply(console, arguments); }
  console.info('[WS] core loaded version', CORE_WS_VERSION);

  let config = {
    getSessionId: () => '',
    getUserId: () => '',
    getRole: () => 'viewer',
    getSocket: () => null,
    setSocket: () => {},
    getReconnectTimer: () => null,
    setReconnectTimer: () => {},
    getPendingMessages: () => [],
    setPendingMessages: () => {},
    getQueuedEditorTypes: () => new Set(),
    onMessage: () => {},
    onOpen: () => {},
    onClose: () => {},
    onCloseExpired: () => {},
    onSeqGap: () => {},
    showToast: () => {},
  };
  let lifecycleHooksInstalled = false;

  // ── Dead-connection defenses ───────────────────────────────────────────────
  // Every server push carries a per-connection monotonic `seq` (reset to 1 on
  // each new socket). A jump means at least one push to this client was lost —
  // onSeqGap() repairs it through the request_state delta path without a
  // reconnect. Independently, the server heartbeats every ~20s, so a socket
  // with NO traffic at all for 45s is a dead downlink (half-open TCP: the
  // browser still reports OPEN and onclose never fires on its own) — the
  // watchdog closes it so the normal onclose → scheduleReconnect path runs.
  const WATCHDOG_SILENCE_MS = 45000;
  const SEQ_GAP_RESYNC_MIN_INTERVAL_MS = 5000;

  // ── Single WebSocket owner state ───────────────────────────────────────────
  // This module is the ONLY place that constructs a WebSocket. All boot modules
  // (boot_shell, player_shell, render/boot, message_dispatch, play.html) must
  // funnel through ensureConnected()/connectWS() so there is exactly one live
  // socket owner per browser tab. The fields below back window.__debugWS().
  let activeClientSocketId = null;
  let connectCallCount = 0;
  let duplicateConnectCount = 0;
  let reconnectAttempts = 0;
  let lastConnectReason = '';
  let lastCloseCode = null;
  let lastCloseReason = '';
  let lastRequestStateSentAt = null;
  const initialStateSentForSockets = new Set();
  const duplicateConnectStacks = [];

  function newClientSocketId() {
    const rand = Math.random().toString(16).slice(2, 10);
    return `cs_${Date.now().toString(16)}_${rand}`;
  }

  function isDevEnv() {
    try {
      const host = String((global.location && global.location.hostname) || '');
      return host === 'localhost' || host === '127.0.0.1' || /\.local$/.test(host);
    } catch (_err) {
      return false;
    }
  }

  // Records a connect() call that was short-circuited because a CONNECTING/OPEN
  // socket already exists. In development we also capture a stack trace so the
  // module that fired the redundant attempt can be identified.
  function recordDuplicateConnect(reason) {
    duplicateConnectCount += 1;
    if (!isDevEnv()) return;
    let stack = '';
    try { stack = (new Error('duplicate connect attempt')).stack || ''; } catch (_err) {}
    duplicateConnectStacks.push({ reason: String(reason || lastConnectReason || ''), stack, at: Date.now() });
    if (duplicateConnectStacks.length > 20) duplicateConnectStacks.shift();
    wsDebugLog('[WS] duplicate connect attempt ignored (socket already live)', { reason: reason || lastConnectReason || '' });
  }

  // A server close with code 1001 + reason "Replaced by a newer connection"
  // means a newer socket already owns this user server-side. Reconnecting here
  // would start a reconnect war (the original storm), so we must NOT reconnect.
  function wasReplacedByNewerConnection(event) {
    if (!event) return false;
    const reason = String(event.reason || '');
    return event.code === 1001 && /replaced by a newer connection/i.test(reason);
  }

  function sendPong(socket) {
    // Heartbeat reply lives at the transport layer so it cannot be starved by a
    // busy/guarded gameplay dispatcher. Never queue a pong on a closed socket and
    // never let a send failure bubble up and crash the message pump.
    try {
      if (socket && socket.readyState === global.WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'pong' }));
        wsDebugLog('[WS] sent pong');
      }
    } catch (err) {
      console.warn('[WS] pong send failed', err);
    }
  }

  function closeSocket(socket, code = 1000, reason = 'Client reconnect cleanup') {
    if (!socket) return;
    try {
      if (socket.readyState === global.WebSocket.OPEN || socket.readyState === global.WebSocket.CONNECTING) {
        socket.close(code, reason);
      }
    } catch (_err) {}
  }

  function clearReconnectTimer() {
    const timer = config.getReconnectTimer();
    if (timer) global.clearTimeout(timer);
    config.setReconnectTimer(null);
  }

  function getReconnectDelayMs() {
    const baseDelay = Math.min(30000, 3000 * 2 ** Math.min(reconnectAttempts, 4));
    const jitterFactor = 0.9 + (Math.random() * 0.2);
    return Math.min(30000, Math.round(baseDelay * jitterFactor));
  }

  function scheduleReconnect() {
    if (config.getReconnectTimer()) return;
    const delayMs = getReconnectDelayMs();
    const timer = global.setTimeout(() => {
      config.setReconnectTimer(null);
      reconnectAttempts += 1;
      lastConnectReason = 'reconnect';
      // Reconnect happens in-place by re-opening the socket via AppWS/ws.js.
      // We never reload or navigate the page to recover the connection.
      console.info('[WS] reconnect in-place', { delayMs, reconnectAttempts });
      // Single-owner guard: never spawn a second socket if one is already
      // CONNECTING/OPEN (e.g. a manual ensureConnected raced the timer).
      const existing = config.getSocket();
      if (existing && (existing.readyState === global.WebSocket.OPEN || existing.readyState === global.WebSocket.CONNECTING)) {
        return;
      }
      connectWS();
    }, delayMs);
    config.setReconnectTimer(timer);
  }

  function installLifecycleHooks() {
    if (lifecycleHooksInstalled) return;
    lifecycleHooksInstalled = true;
    const stopSocketLifecycle = () => {
      clearReconnectTimer();
      const socket = config.getSocket();
      if (socket) {
        config.setSocket(null);
        closeSocket(socket, 1000, 'Page lifecycle teardown');
      }
    };
    global.addEventListener('beforeunload', stopSocketLifecycle);
    global.addEventListener('pagehide', stopSocketLifecycle);
  }

  function configure(nextConfig = {}) {
    config = { ...config, ...nextConfig };
    installLifecycleHooks();
  }

  function buildMessage(msg) {
    return {
      ...msg,
      session_id: config.getSessionId(),
      user_id: config.getUserId(),
    };
  }

  function queueMessage(msg) {
    const built = buildMessage(msg);
    const type = String(built.type || '');
    let pending = Array.isArray(config.getPendingMessages()) ? config.getPendingMessages().slice() : [];
    if (type === 'local_map_enter' || type === 'local_map_exit') {
      pending = pending.filter((entry) => {
        const t = String(entry?.type || '');
        return t !== 'local_map_enter' && t !== 'local_map_exit';
      });
    } else if (config.getQueuedEditorTypes().has(type)) {
      const ctx = String(built?.payload?.map_context || 'world');
      pending = pending.filter((entry) => !(String(entry?.type || '') === type && String(entry?.payload?.map_context || 'world') === ctx));
    }
    pending.push(built);
    if (pending.length > 200) pending = pending.slice(-200);
    config.setPendingMessages(pending);
  }

  function flushPendingMessages() {
    const socket = config.getSocket();
    const pending = Array.isArray(config.getPendingMessages()) ? config.getPendingMessages() : [];
    if (!socket || socket.readyState !== global.WebSocket.OPEN || !pending.length) return;
    config.setPendingMessages([]);
    pending.forEach((msg) => {
      try {
        socket.send(JSON.stringify(msg));
      } catch (_err) {
        queueMessage(msg);
      }
    });
  }

  function connectWS() {
    connectCallCount += 1;
    const sessionId = String(config.getSessionId() || '').trim();
    const userId = String(config.getUserId() || '').trim();
    if (!sessionId || !userId) {
      console.warn('[WS] Missing session or user id, skipping connect', { sessionId, userId });
      config.setSocket(null);
      return null;
    }
    const proto = global.location.protocol === 'https:' ? 'wss' : 'ws';
    // Attach JWT token as query param when available (Part 5.1)
    const token = (global.sessionStorage && global.sessionStorage.getItem('dnd_token')) || '';
    clearReconnectTimer();
    const priorSocket = config.getSocket();
    if (priorSocket) {
      if (priorSocket.readyState === global.WebSocket.OPEN || priorSocket.readyState === global.WebSocket.CONNECTING) {
        // Single-owner contract: an existing CONNECTING/OPEN socket is reused.
        recordDuplicateConnect(lastConnectReason);
        return priorSocket;
      }
      closeSocket(priorSocket, 1000, 'Replacing stale socket');
    }

    // Each socket gets a local client_socket_id so the server can correlate
    // replacements/closes with a specific browser-side socket in its logs.
    const clientSocketId = newClientSocketId();
    activeClientSocketId = clientSocketId;
    const queryParts = [];
    if (token) queryParts.push(`token=${encodeURIComponent(token)}`);
    queryParts.push(`client_socket_id=${encodeURIComponent(clientSocketId)}`);
    if (lastConnectReason) queryParts.push(`reason=${encodeURIComponent(lastConnectReason)}`);
    const queryString = queryParts.length ? `?${queryParts.join('&')}` : '';

    if (global.__PLAY_BOOT_ROLE === 'player' && typeof global.__playerBootCheckpoint === 'function') global.__playerBootCheckpoint('PLAYER_BOOT_WS_CONNECTING');
    const socket = new global.WebSocket(`${proto}://${global.location.host}/ws/${sessionId}/${userId}${queryString}`);
    try { socket.__clientSocketId = clientSocketId; } catch (_err) {}
    config.setSocket(socket);

    // Per-socket push-loss tracking (see WATCHDOG_SILENCE_MS block above).
    // Closure-scoped so a superseded socket's state can never bleed into its
    // replacement: each new socket starts at seq 0 / a fresh watchdog.
    let lastServerSeq = 0;
    let lastSeqGapResyncAt = -Infinity; // first gap must always resync
    let lastMessageAt = 0;
    let watchdogTimer = null;

    const clearWatchdog = () => {
      if (watchdogTimer) { global.clearTimeout(watchdogTimer); watchdogTimer = null; }
    };
    const fireWatchdog = () => {
      watchdogTimer = null;
      if (config.getSocket() !== socket) return;
      if (socket.readyState !== global.WebSocket.OPEN) return;
      // Background tabs throttle timers, so this can fire long after arming
      // even though websocket messages kept flowing (message events are not
      // throttled). Only a genuinely silent socket is treated as dead.
      const silentForMs = Date.now() - lastMessageAt;
      if (silentForMs < WATCHDOG_SILENCE_MS) {
        watchdogTimer = global.setTimeout(fireWatchdog, Math.max(1000, WATCHDOG_SILENCE_MS - silentForMs));
        return;
      }
      console.warn('[WS] watchdog: no server traffic for', silentForMs, 'ms — closing dead socket to force reconnect');
      closeSocket(socket, 4008, 'Client watchdog: no server traffic');
    };
    const armWatchdog = () => {
      lastMessageAt = Date.now();
      clearWatchdog();
      watchdogTimer = global.setTimeout(fireWatchdog, WATCHDOG_SILENCE_MS);
    };
    const trackServerSeq = (msg) => {
      if (!msg || typeof msg.seq !== 'number' || !isFinite(msg.seq)) return;
      if (lastServerSeq > 0 && msg.seq > lastServerSeq + 1) {
        const gap = { expected: lastServerSeq + 1, received: msg.seq };
        console.warn('[WS] server seq gap — at least one push was missed, requesting resync', gap);
        const now = Date.now();
        if (now - lastSeqGapResyncAt >= SEQ_GAP_RESYNC_MIN_INTERVAL_MS) {
          lastSeqGapResyncAt = now;
          try { config.onSeqGap(gap); } catch (err) { console.warn('[WS] seq gap resync handler failed', err); }
        }
      }
      // Never regress: a late out-of-order frame (seq <= last) is not a gap.
      if (msg.seq > lastServerSeq) lastServerSeq = msg.seq;
    };

    socket.onopen = () => {
      if (config.getSocket() !== socket) {
        closeSocket(socket, 1000, 'Superseded socket on open');
        return;
      }
      reconnectAttempts = 0;
      if (global.__PLAY_BOOT_ROLE === 'player') {
        global.__playerBootState = global.__playerBootState || {};
        global.__playerBootState.wsOpened = true;
        if (typeof global.__playerBootCheckpoint === 'function') global.__playerBootCheckpoint('PLAYER_BOOT_WS_OPEN');
      }
      armWatchdog();
      config.onOpen();
      clearReconnectTimer();
      flushPendingMessages();
    };

    socket.onclose = (event) => {
      clearWatchdog();
      if (config.getSocket() !== socket) return;
      lastCloseCode = event && event.code;
      lastCloseReason = (event && event.reason) || '';
      config.setSocket(null);
      if (socket.__clientSocketId) initialStateSentForSockets.delete(socket.__clientSocketId);
      // A websocket close never hard-refreshes the page. We log the reason and
      // recover by scheduling an in-place reconnect (except for explicit
      // session-expiry codes, which hand off to onCloseExpired).
      console.info('[WS] close reason', {
        code: event && event.code,
        reason: (event && event.reason) || '',
        wasClean: !!(event && event.wasClean),
        clientSocketId: socket.__clientSocketId || null,
      });
      config.onClose(event);
      if (event.code === 4001 || event.code === 4003 || event.code === 4004) {
        config.onCloseExpired(event);
        return;
      }
      // Anti-storm: if the server replaced this socket with a newer connection,
      // a newer owner already exists — reconnecting would just fight it.
      if (wasReplacedByNewerConnection(event)) {
        console.info('[WS] not reconnecting: socket was replaced by a newer connection');
        return;
      }
      scheduleReconnect();
    };

    socket.onerror = () => {
      if (config.getSocket() !== socket) return;
      const event = null;
      console.warn('[WS] error', event && event.message ? event.message : event);
      socket.close();
    };
    socket.onmessage = (event) => {
      if (config.getSocket() !== socket) return;
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (_err) {
        return;
      }
      // Any decoded server frame (heartbeat included) proves the downlink is
      // alive; re-arm the silence watchdog and run push-loss seq tracking
      // before ping handling so heartbeats participate in both.
      armWatchdog();
      trackServerSeq(msg);
      // Server heartbeat: respond to {"type":"ping"} immediately with a pong on
      // the same socket, BEFORE any gameplay dispatch. This guarantees the server
      // sees liveness even while the legacy dispatcher is busy or guarded, which
      // is what was causing false "Heartbeat timeout" disconnects and the black
      // flicker/reconnect during active combat. Heartbeat pings must never reach
      // the gameplay handlers, so we return after replying.
      if (msg && msg.type === 'ping') {
        wsDebugLog('[WS] received ping');
        sendPong(socket);
        return;
      }
      if (msg && msg.type === 'combat_state') {
        const payload = msg.payload || {};
        const order = Array.isArray(payload.combatants) ? payload.combatants.map(c => `${c?.name || c?.id || c?.token_id || '?'}:${c?.initiative ?? '--'}`) : [];
        wsDebugLog(`[WS] received combat_state revision=${payload.revision ?? 'none'} order=${JSON.stringify(order).replace(/\"/g, "'")}`);
      }
      config.onMessage(msg);
    };
    return socket;
  }

  function send(msg) {
    const socket = config.getSocket();
    if (!socket || socket.readyState !== global.WebSocket.OPEN) {
      console.warn('[WS] Not connected, queueing message:', msg.type);
      queueMessage(msg);
      return;
    }
    try {
      socket.send(JSON.stringify(buildMessage(msg)));
    } catch (err) {
      console.warn('[WS] Send failed, queueing message:', msg.type, err);
      queueMessage(msg);
    }
  }

  // Single entry point every boot module/event should call to obtain a live
  // socket. Idempotent: if a socket is already CONNECTING or OPEN it is reused
  // (the existing socket is returned), so visibility/focus/boot-retry/state
  // refresh can call this freely without ever creating a duplicate socket.
  function ensureConnected(opts) {
    const options = opts || {};
    if (options.reason) lastConnectReason = String(options.reason);
    const existing = config.getSocket();
    if (existing && (existing.readyState === global.WebSocket.CONNECTING || existing.readyState === global.WebSocket.OPEN)) {
      recordDuplicateConnect(lastConnectReason);
      return existing;
    }
    return connectWS();
  }

  // Sends the initial-state requests exactly once per real socket open. The
  // guard is keyed on the socket's client_socket_id, so repeated calls (from
  // multiple boot modules, focus, or a redundant onOpen) only send once, while
  // a genuine reconnect — which mints a new client_socket_id — sends again.
  function requestInitialStateOnce(sendFn) {
    const socket = config.getSocket();
    if (!socket || socket.readyState !== global.WebSocket.OPEN) return false;
    const id = socket.__clientSocketId || activeClientSocketId;
    if (id && initialStateSentForSockets.has(id)) return false;
    if (id) initialStateSentForSockets.add(id);
    lastRequestStateSentAt = Date.now();
    if (typeof sendFn === 'function') {
      sendFn();
    }
    return true;
  }

  function debugWS() {
    const socket = config.getSocket();
    return {
      role: config.getRole(),
      sessionId: config.getSessionId(),
      userId: config.getUserId(),
      activeSocketId: activeClientSocketId,
      readyState: socket ? socket.readyState : null,
      reconnectAttempts,
      lastConnectReason,
      lastCloseCode,
      lastCloseReason,
      lastRequestStateSentAt,
      connectCallCount,
      duplicateConnectCount,
      duplicateConnectStacks: duplicateConnectStacks.slice(),
    };
  }
  global.__debugWS = debugWS;

  global.AppWS = {
    configure,
    connectWS,
    ensureConnected,
    requestInitialStateOnce,
    debugWS,
    buildMessage,
    queueMessage,
    flushPendingMessages,
    send,
  };
})(window);
