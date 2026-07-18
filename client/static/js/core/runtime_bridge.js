(function (global) {
  'use strict';
  // Live ownership boundary:
  // - This module is compatibility/handoff glue only.
  // - It adapts store + play.html globals into env contracts for live shell modules.
  // - It must not become a second gameplay source-of-truth.

  function pick(name, fallback) {
    const value = global[name];
    return typeof value === 'function' ? value.bind(global) : (fallback || function () {});
  }

  function getStore() {
    return global.AppStateStore || global.AppStore || null;
  }

  function storeGet(path, fallback) {
    const store = getStore();
    return store && typeof store.get === 'function' ? store.get(path, fallback) : fallback;
  }

  function storeSet(path, value) {
    const store = getStore();
    return store && typeof store.set === 'function' ? store.set(path, value) : value;
  }

  function readQueryParam(keys) {
    try {
      const search = String(global.location && global.location.search ? global.location.search : '');
      if (!search) return '';
      const params = new global.URLSearchParams(search);
      for (const key of keys) {
        const value = String(params.get(key) || '').trim();
        if (value) return value;
      }
    } catch (_err) {}
    return '';
  }

  function createMessageDispatchEnv() {
    return {
      reportClientRuntimeError: pick('reportClientRuntimeError'),
      handleLegacyMessage: pick('handleLegacyMessage', pick('handleMessage')),
    };
  }

  function createWsConfig() {
    const wsConfig = {
      getSessionId: function () {
        const fromStore = storeGet('session.id', global.SESSION_ID || '');
        if (String(fromStore || '').trim()) return fromStore;
        return readQueryParam(['session_id', 'session', 'sid']);
      },
      getUserId: function () {
        const resolved = typeof global.getEffectiveUserId === 'function' ? global.getEffectiveUserId() : '';
        const fromStore = storeGet('user.id', global.USER_ID || '');
        const fromGlobal = String(global.USER_ID || '').trim();
        if (String(resolved || '').trim()) return resolved;
        if (String(fromStore || '').trim()) return String(fromStore || '').trim();
        if (fromGlobal) return fromGlobal;
        const fromQuery = readQueryParam(['user_id', 'uid', 'user']);
        if (String(fromQuery || '').trim()) return fromQuery;
        return readQueryParam(['user_id', 'uid', 'user']);
      },
      getRole: function () {
        const resolvedRole = typeof global.getEffectiveRole === 'function' ? global.getEffectiveRole() : '';
        const fromStore = storeGet('user.role', global.ROLE || 'viewer');
        const fromGlobal = String(global.ROLE || '').trim();
        const fromQuery = readQueryParam(['role']);
        return String(resolvedRole || fromQuery || fromGlobal || fromStore || 'viewer').toLowerCase();
      },
      getSocket: function () { return storeGet('socket.instance', global.ws || null); },
      setSocket: function (value) { storeSet('socket.instance', value); global.ws = value; },
      getReconnectTimer: function () { return storeGet('socket.reconnectTimer', global.wsReconnectTimer || null); },
      setReconnectTimer: function (value) { storeSet('socket.reconnectTimer', value); global.wsReconnectTimer = value; },
      getPendingMessages: function () { return storeGet('socket.pendingMessages', Array.isArray(global._pendingWSMessages) ? global._pendingWSMessages : []); },
      setPendingMessages: function (value) { const next = Array.isArray(value) ? value : []; storeSet('socket.pendingMessages', next); global._pendingWSMessages = next; },
      getQueuedEditorTypes: function () { return global._queuedEditorTypes || new Set(); },
      onMessage: function (msg) {
        if (global.AppMessageDispatch && typeof global.AppMessageDispatch.handleIncoming === 'function') {
          global.AppMessageDispatch.handleIncoming(msg, createMessageDispatchEnv());
          return;
        }
        const legacy = pick('handleLegacyMessage', pick('handleMessage'));
        legacy(msg);
      },
      onOpen: function () {
        const effectiveRole = String(config.getRole() || 'viewer').toLowerCase();
        const status = global.document.getElementById('ws-status');
        if (status) status.classList.add('connected');
        console.info('[play-boot] websocket open', { role: effectiveRole });
        if (typeof global._setWsStatus === 'function') global._setWsStatus('connected');
        storeSet('socket.connected', true);
        storeSet('socket.status', 'connected');
        if (effectiveRole === 'dm' && typeof global._resyncDmMapNav === 'function') global.setTimeout(global._resyncDmMapNav, 0);
        // Initial authoritative-state requests. These are funnelled through the
        // single WebSocket owner's requestInitialStateOnce() so they are sent
        // exactly once per real socket open — never once per boot module and
        // never repeatedly during a reconnect storm.
        const sendInitialStateRequests = function () {
          // Reconnect delta: echo per-domain revisions from the last sync plus
          // live-tracked counters so the server can omit unchanged heavy
          // domains (tokens/fog/editor/char_profiles/inventory).
          let knownRevisions;
          try {
            if (typeof global.collectKnownStateRevisions === 'function') knownRevisions = global.collectKnownStateRevisions();
          } catch (_err) { knownRevisions = undefined; }
          const requestStatePayload = { reason: 'reconnect' };
          if (knownRevisions && Object.keys(knownRevisions).length) requestStatePayload.known_revisions = knownRevisions;
          if (global.AppWS && typeof global.AppWS.send === 'function') {
            if (global.liveDebugLog) global.liveDebugLog('websocket open/request_state', { role: effectiveRole, reason: 'reconnect' });
            console.debug('[WS] requesting authoritative state_sync after open');
            if (global.__PLAY_BOOT_ROLE === 'player' && typeof global.__playerBootCheckpoint === 'function') global.__playerBootCheckpoint('PLAYER_BOOT_REQUEST_STATE_SENT');
            global.AppWS.send({ type: 'request_state', payload: requestStatePayload });
          } else if (typeof global.sendWS === 'function') {
            if (global.__PLAY_BOOT_ROLE === 'player' && typeof global.__playerBootCheckpoint === 'function') global.__playerBootCheckpoint('PLAYER_BOOT_REQUEST_STATE_SENT');
            global.sendWS({ type: 'request_state', payload: requestStatePayload });
          }
          if (effectiveRole === 'dm' || effectiveRole === 'player') {
            if (global.AppWS && typeof global.AppWS.send === 'function') global.AppWS.send({ type: 'treasury_get', payload: {} });
            else if (typeof global.sendWS === 'function') global.sendWS({ type: 'treasury_get', payload: {} });
            // Reconnect recovery: if combat is active (or its state is unknown on a
            // fresh open), pull the authoritative combat_state so initiative/order/
            // turn are never left stale after a silent drop or heartbeat-driven
            // reconnect. state_sync also carries combat, but this guarantees a
            // redraw even if that race is lost.
            var _combatForResync = global._combat || global.combat || null;
            var _combatKnown = !!(_combatForResync && typeof _combatForResync === 'object' && Object.prototype.hasOwnProperty.call(_combatForResync, 'active'));
            if (!_combatKnown || _combatForResync.active) {
              if (global.liveDebugLog) global.liveDebugLog('websocket open/combat_state_request', { role: effectiveRole, known: _combatKnown, active: !!(_combatForResync && _combatForResync.active) });
              console.debug('[WS] requesting authoritative combat_state after open', { known: _combatKnown, active: !!(_combatForResync && _combatForResync.active) });
              if (global.AppWS && typeof global.AppWS.send === 'function') global.AppWS.send({ type: 'combat_state_request', payload: {} });
              else if (typeof global.sendWS === 'function') global.sendWS({ type: 'combat_state_request', payload: {} });
            }
          }
        };
        if (global.AppWS && typeof global.AppWS.requestInitialStateOnce === 'function') {
          global.AppWS.requestInitialStateOnce(sendInitialStateRequests);
        } else {
          sendInitialStateRequests();
        }
        if (effectiveRole === 'dm' && typeof global.reapplyDmFogPreviewAfterReconnect === 'function') global.setTimeout(global.reapplyDmFogPreviewAfterReconnect, 0);
      },
      onClose: function (_event) {
        const effectiveRole = String(config.getRole() || 'viewer').toLowerCase();
        if (global.liveDebugLog) global.liveDebugLog('websocket close/reconnect pending', { role: effectiveRole, code: _event && _event.code, reason: _event && _event.reason });
        const status = global.document.getElementById('ws-status');
        if (status) status.classList.remove('connected');
        if (typeof global._setWsStatus === 'function') global._setWsStatus('reconnecting');
        storeSet('socket.connected', false);
        storeSet('socket.status', 'disconnected');
      },
      onSeqGap: function (gap) {
        // A missed push means local state may be silently stale even though
        // the socket is healthy. Repair through the same delta path a socket
        // reopen uses: request_state with known per-domain revisions, so the
        // server only resends the domains that actually changed. Invisible to
        // the player — no reconnect, no reload.
        let knownRevisions;
        try {
          if (typeof global.collectKnownStateRevisions === 'function') knownRevisions = global.collectKnownStateRevisions();
        } catch (_err) { knownRevisions = undefined; }
        const payload = {
          reason: 'seq_gap',
          seq_gap: { expected: gap && gap.expected, received: gap && gap.received },
        };
        if (knownRevisions && Object.keys(knownRevisions).length) payload.known_revisions = knownRevisions;
        if (global.liveDebugLog) global.liveDebugLog('websocket seq gap/request_state', payload.seq_gap);
        console.warn('[WS] seq gap — requesting delta state resync', payload.seq_gap);
        if (global.AppWS && typeof global.AppWS.send === 'function') global.AppWS.send({ type: 'request_state', payload });
        else if (typeof global.sendWS === 'function') global.sendWS({ type: 'request_state', payload });
      },
      onCloseExpired: function () {
        const status = global.document.getElementById('ws-status');
        if (status) {
          status.classList.remove('connected');
          status.title = 'Session expired — please return to lobby';
        }
        if (typeof global._setWsStatus === 'function') global._setWsStatus('expired');
        storeSet('socket.connected', false);
        storeSet('socket.status', 'expired');
        if (typeof global.showToast === 'function') global.showToast('Session expired. Please start a new session.');
        global.setTimeout(function () { global.location.href = '/'; }, 3000);
      },
    };
    const config = wsConfig;
    return wsConfig;
  }

  function createBootEnv() {
    return {
      window: global,
      document: global.document,
      location: global.location,
      SESSION_ID: storeGet('session.id', global.SESSION_ID),
      USER_ID: storeGet('user.id', global.USER_ID),
      ROLE: storeGet('user.role', global.ROLE),
      NAME: storeGet('user.name', global.NAME),
      RETURNING: storeGet('session.returning', global.RETURNING),
      safeClientCall: pick('safeClientCall'),
      reportClientRuntimeError: pick('reportClientRuntimeError'),
      runLegacyInitUI: pick('initUI'),
      initCanvas: pick('initCanvas'),
      connectWS: pick('connectWS'),
      syncSessionAuthority: pick('syncSessionAuthority'),
      applyRoleVisibility: pick('__bootApplyRoleVisibility'),
      updateSpellTip: pick('_updateSpellTip'),
      initJournalUI: pick('initJournalPanel'),
      initLibraryUI: pick('initLogsUI'),
      renderItemLibraryList: pick('renderItemLibraryList'),
      renderItemLibraryEditor: pick('renderItemLibraryEditor'),
      refreshInventoryTransferTargets: pick('refreshInventoryTransferTargets'),
      refreshCharProfileSelect: pick('refreshCharProfileSelect'),
      getCharSheet: function () { return global._charSheet || null; },
      buildClassGrid: pick('buildClassGrid'),
      buildPlayerColorSwatches: pick('buildPlayerColorSwatches'),
      getTokens: function () { return global.tokens || {}; },
      toggleFlyout: pick('toggleFlyout'),
      fetchSessionInvites: function (sessionId, userId) {
        return global.fetch(`/api/session/${encodeURIComponent(sessionId)}/invites?user_id=${encodeURIComponent(userId)}`).then(function (resp) {
          if (!resp.ok) throw new Error('Invite fetch failed: ' + resp.status);
          return resp.json();
        });
      },
      sendChat: pick('sendChat'),
      ensureEditorLayerLoaded: pick('ensureEditorLayerLoaded'),
      ensureEditorWallsLoaded: pick('ensureEditorWallsLoaded'),
      ensureEditorPropsLoaded: pick('ensureEditorPropsLoaded'),
      ensureEditorPathsLoaded: pick('ensureEditorPathsLoaded'),
      ensureEditorLabelsLoaded: pick('ensureEditorLabelsLoaded'),
      ensureEditorMarkersLoaded: pick('ensureEditorMarkersLoaded'),
      handleGlobalContextClick: pick('handleGlobalContextClick'),
      getIgnorePropPopupOutsideClickUntil: function () { return Number(global._ignorePropPopupOutsideClickUntil || 0); },
      propPopupClose: pick('propPopupClose'),
    };
  }

  global.AppRuntimeBridge = {
    createBootEnv: createBootEnv,
    createMessageDispatchEnv: createMessageDispatchEnv,
    createWsConfig: createWsConfig,
  };
})(window);
