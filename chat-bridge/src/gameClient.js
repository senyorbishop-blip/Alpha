'use strict';

const WebSocket = require('ws');
const https = require('https');
const http = require('http');

// The server's heartbeat loop (main.py _websocket_heartbeat_loop) sends
// {"type":"ping"} every 30s and closes the socket with "1001 Heartbeat
// timeout" after 60s without any inbound frame. The browser client replies
// {"type":"pong"} (client/static/js/core/ws.js sendPong). We do the same,
// plus a proactive pong on an interval well under the 60s timeout so a
// missed ping can never starve liveness.
const DEFAULT_HEARTBEAT_INTERVAL_MS = 25000;
const DEFAULT_MAX_QUEUED_MESSAGES = 100;

// ACK types the server sends back for each bridge message.
const ACK_TYPES = {
  chat_participant_join: 'chat_participant_join_ack',
  chat_participant_target: 'chat_participant_target_result',
  chat_bridge_loot_grant: 'chat_bridge_loot_grant_result',
  chat_participant_inventory: 'chat_participant_inventory_result',
  chat_bridge_poll_vote: 'chat_bridge_poll_vote_result',
  chat_participant_name_set: 'chat_participant_name_result',
  chat_participant_me: 'chat_participant_me_result',
  chat_participant_arena_load: 'chat_participant_arena_load_result',
  chat_participant_arena_sync: 'chat_participant_arena_sync_result',
  chat_participant_arena_leaderboard: 'chat_participant_arena_leaderboard_result',
  chat_bridge_rewards_get: 'chat_bridge_rewards_result',
};

/**
 * WebSocket client that connects to the game server as the chat_bridge role.
 *
 * Lifecycle:
 *  1. On startup, POST /api/chat-bridge/auth → receive JWT
 *  2. Connect to ws://{host}/ws/{session_id}/chat_bridge?token={jwt}
 *  3. Listen for inbound messages; dispatch to registered handlers
 *  4. Answer server heartbeat pings with pongs (and send interval pongs)
 *  5. On disconnect, reconnect with exponential backoff (capped at 60s);
 *     messages sent while disconnected are queued (bounded, drop-oldest)
 *     and flushed on reconnect
 *
 * Message pattern:
 *  send(type, payload) → Promise<response payload | null>
 *  The server replies with a message whose type matches known ack patterns.
 *  For fire-and-forget messages, the promise resolves with null after a timeout.
 */
class GameClient {
  constructor({ wsUrl, sessionId, bridgeToken, logger, heartbeatIntervalMs, reconnectDelayMs, maxQueuedMessages }) {
    this._wsUrl = wsUrl.replace(/\/$/, '');
    this._sessionId = sessionId;
    this._bridgeToken = bridgeToken;
    this._logger = logger ?? console;
    this._ws = null;
    this._jwt = null;
    this._jwtExpiry = 0;
    this._initialReconnectDelay = reconnectDelayMs ?? 2000;
    this._reconnectDelay = this._initialReconnectDelay;
    this._maxReconnectDelay = 60000;
    this._stopping = false;
    this._pendingRequests = new Map();  // reqId → { resolve, reject, timer }
    this._reqCounter = 0;
    this._handlers = {};  // msgType → callback
    this._connectPromise = null;
    this._heartbeatIntervalMs = heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS;
    this._heartbeatTimer = null;
    this._maxQueuedMessages = maxQueuedMessages ?? DEFAULT_MAX_QUEUED_MESSAGES;
    this._outQueue = [];  // { type, dispatch, cancel } queued while disconnected
  }

  /** Register an inbound message handler. */
  on(msgType, fn) {
    this._handlers[msgType] = fn;
  }

  /** Start connection loop. Resolves once first WS connection is open. */
  async start() {
    this._stopping = false;
    await this._ensureConnected();
  }

  stop() {
    this._stopping = true;
    this._stopHeartbeat();
    for (const entry of this._outQueue.splice(0)) {
      entry.cancel();
    }
    if (this._ws) {
      try { this._ws.close(); } catch (_) {}
    }
  }

  /** Fetch a fresh JWT from the game server if needed. */
  async _fetchJwt() {
    if (this._jwt && Date.now() / 1000 < this._jwtExpiry - 300) {
      return this._jwt;
    }

    const url = new URL(`${this._wsUrl.replace(/^ws/, 'http')}/api/chat-bridge/auth`);
    const body = JSON.stringify({ token: this._bridgeToken });

    return new Promise((resolve, reject) => {
      const mod = url.protocol === 'https:' ? https : http;
      const req = mod.request(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      }, res => {
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          try {
            const parsed = JSON.parse(data);
            if (res.statusCode !== 200 || !parsed.jwt) {
              reject(new Error(`Auth failed (${res.statusCode}): ${data}`));
              return;
            }
            this._jwt = parsed.jwt;
            // Decode exp from JWT payload (base64 middle segment)
            const payloadB64 = this._jwt.split('.')[1];
            const payload = JSON.parse(Buffer.from(payloadB64, 'base64url').toString('utf8'));
            this._jwtExpiry = payload.exp ?? (Date.now() / 1000 + 86400 * 7);
            resolve(this._jwt);
          } catch (err) {
            reject(err);
          }
        });
      });
      req.on('error', reject);
      req.write(body);
      req.end();
    });
  }

  async _ensureConnected() {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) return;
    if (this._connectPromise) return this._connectPromise;

    this._connectPromise = this._connect().finally(() => {
      this._connectPromise = null;
    });
    return this._connectPromise;
  }

  async _connect() {
    let attempts = 0;
    while (!this._stopping) {
      try {
        const jwt = await this._fetchJwt();
        const wsTarget = `${this._wsUrl.replace(/^http/, 'ws')}/ws/${this._sessionId}/chat_bridge?token=${jwt}&reason=chat_bridge`;
        this._logger.info(`[GameClient] connecting to ${wsTarget.replace(/token=[^&]+/, 'token=***')}`);

        await new Promise((resolve, reject) => {
          const ws = new WebSocket(wsTarget);
          let opened = false;
          ws.once('open', () => {
            opened = true;
            this._ws = ws;
            this._reconnectDelay = this._initialReconnectDelay;
            attempts = 0;
            this._logger.info('[GameClient] connected');
            this._startHeartbeat(ws);
            this._flushQueue();
            resolve();
          });
          ws.on('error', err => {
            if (opened) return;  // post-open errors are followed by 'close'
            try { ws.terminate(); } catch (_) {}
            reject(err);
          });
          ws.on('close', (code, reason) => {
            if (!opened) {
              // Pre-open failure: the surrounding retry loop handles it.
              reject(new Error('WebSocket closed before open'));
              return;
            }
            if (this._ws !== ws) return;  // a newer socket already took over
            this._stopHeartbeat();
            this._ws = null;
            this._logger.warn(`[GameClient] disconnected (${code} ${reason}) — reconnecting…`);
            if (!this._stopping) this._scheduleReconnect();
          });

          ws.on('message', data => this._onMessage(data));
        });
        return;  // connected successfully
      } catch (err) {
        attempts++;
        this._logger.error(`[GameClient] connection attempt ${attempts} failed: ${err.message}`);
        if (this._stopping) return;
        const delay = Math.min(this._reconnectDelay * Math.pow(1.5, Math.min(attempts, 8)), this._maxReconnectDelay);
        this._reconnectDelay = delay;
        this._logger.info(`[GameClient] retrying in ${Math.round(delay / 1000)}s…`);
        await new Promise(r => setTimeout(r, delay));
      }
    }
  }

  _scheduleReconnect() {
    const delay = Math.min(this._reconnectDelay * 1.5, this._maxReconnectDelay);
    this._reconnectDelay = delay;
    setTimeout(() => {
      // Route through _ensureConnected so a send()-triggered connect and a
      // scheduled reconnect never run two connect loops at once.
      if (!this._stopping) this._ensureConnected().catch(err => this._logger.error('[GameClient] reconnect error:', err));
    }, delay);
  }

  /**
   * Application-level heartbeat. The server closes any socket that stays
   * silent for 60s ("1001 Heartbeat timeout"); any inbound frame refreshes
   * its liveness clock. Send a pong on a fixed interval while connected.
   */
  _startHeartbeat(ws) {
    this._stopHeartbeat();
    this._heartbeatTimer = setInterval(() => {
      if (ws.readyState !== WebSocket.OPEN) return;
      try {
        ws.send(JSON.stringify({ type: 'pong' }));
      } catch (err) {
        this._logger.warn('[GameClient] heartbeat send failed:', err.message);
      }
    }, this._heartbeatIntervalMs);
    if (typeof this._heartbeatTimer.unref === 'function') this._heartbeatTimer.unref();
  }

  _stopHeartbeat() {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
  }

  _sendPong() {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    try {
      this._ws.send(JSON.stringify({ type: 'pong' }));
    } catch (err) {
      this._logger.warn('[GameClient] pong send failed:', err.message);
    }
  }

  /** Queue an outbound message while disconnected. Bounded; drops the oldest. */
  _enqueue(entry) {
    if (this._outQueue.length >= this._maxQueuedMessages) {
      const dropped = this._outQueue.shift();
      this._logger.warn(`[GameClient] outbound queue full — dropping oldest queued ${dropped.type}`);
      dropped.cancel();
    }
    this._outQueue.push(entry);
  }

  /** Flush messages queued during a disconnect. Called once the socket opens. */
  _flushQueue() {
    if (!this._outQueue.length) return;
    const queued = this._outQueue.splice(0);
    this._logger.info(`[GameClient] flushing ${queued.length} queued message(s)`);
    for (const entry of queued) {
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        entry.dispatch();
      } else {
        // Connection dropped mid-flush — requeue the remainder.
        this._enqueue(entry);
      }
    }
  }

  _onMessage(raw) {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    const type = msg.type ?? '';
    const payload = msg.payload ?? {};

    // Server heartbeat: reply immediately, same as the browser client
    // (client/static/js/core/ws.js). Never dispatch pings to handlers.
    if (type === 'ping') {
      this._sendPong();
      return;
    }

    // Resolve pending request if this is a known ack type
    const reqId = payload._req_id;
    if (reqId && this._pendingRequests.has(reqId)) {
      const { resolve, timer } = this._pendingRequests.get(reqId);
      clearTimeout(timer);
      this._pendingRequests.delete(reqId);
      resolve(payload);
      return;
    }

    // Dispatch to registered handler
    if (this._handlers[type]) {
      try {
        this._handlers[type](payload);
      } catch (err) {
        this._logger.error(`[GameClient] handler error for ${type}:`, err);
      }
    }
  }

  /**
   * Send a message to the game server.
   * Returns the server's ack payload (for messages with known response types),
   * or null after a timeout for fire-and-forget messages.
   *
   * While disconnected, messages are queued (bounded, drop-oldest) and flushed
   * on reconnect, so a brief heartbeat/network gap never loses a chat command.
   * For ack-type messages the response timeout starts when the message is
   * actually sent, not while it sits in the queue.
   */
  async send(type, payload = {}, timeoutMs = 5000) {
    const reqId = `br_${++this._reqCounter}_${Date.now()}`;
    const augmented = { ...payload, _req_id: reqId };
    const frame = JSON.stringify({ type, payload: augmented });
    const ackType = ACK_TYPES[type];

    return new Promise(resolve => {
      const dispatch = () => {
        if (ackType) {
          const timer = setTimeout(() => {
            if (this._pendingRequests.has(reqId)) {
              this._pendingRequests.delete(reqId);
              resolve(null);
            }
          }, timeoutMs);
          this._pendingRequests.set(reqId, { resolve, timer });

          // Register a one-time handler for the ack type that checks _req_id
          const prev = this._handlers[ackType];
          this._handlers[ackType] = (p) => {
            if (p._req_id === reqId && this._pendingRequests.has(reqId)) {
              const { resolve: res, timer: t } = this._pendingRequests.get(reqId);
              clearTimeout(t);
              this._pendingRequests.delete(reqId);
              res(p);
              this._handlers[ackType] = prev;
            } else if (prev) {
              prev(p);
            }
          };
        }

        try {
          this._ws.send(frame);
        } catch (err) {
          this._logger.error(`[GameClient] send error for ${type}:`, err);
          if (ackType && this._pendingRequests.has(reqId)) {
            const { timer } = this._pendingRequests.get(reqId);
            clearTimeout(timer);
            this._pendingRequests.delete(reqId);
            resolve(null);
          }
          return;
        }

        if (!ackType) resolve(null);  // fire-and-forget
      };

      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        dispatch();
        return;
      }

      if (this._stopping) {
        resolve(null);
        return;
      }

      this._logger.warn(`[GameClient] not connected — queueing ${type}`);
      this._enqueue({ type, dispatch, cancel: () => resolve(null) });
      this._ensureConnected().catch(() => {});
    });
  }
}

module.exports = { GameClient };
