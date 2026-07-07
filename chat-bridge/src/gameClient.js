'use strict';

const WebSocket = require('ws');
const https = require('https');
const http = require('http');

/**
 * WebSocket client that connects to the game server as the chat_bridge role.
 *
 * Lifecycle:
 *  1. On startup, POST /api/chat-bridge/auth → receive JWT
 *  2. Connect to ws://{host}/ws/{session_id}/chat_bridge?token={jwt}
 *  3. Listen for inbound messages; dispatch to registered handlers
 *  4. On disconnect, reconnect with exponential backoff (capped at 60s)
 *
 * Message pattern:
 *  send(type, payload) → Promise<response payload | null>
 *  The server replies with a message whose type matches known ack patterns.
 *  For fire-and-forget messages, the promise resolves with null after a timeout.
 */
class GameClient {
  constructor({ wsUrl, sessionId, bridgeToken, logger }) {
    this._wsUrl = wsUrl.replace(/\/$/, '');
    this._sessionId = sessionId;
    this._bridgeToken = bridgeToken;
    this._logger = logger ?? console;
    this._ws = null;
    this._jwt = null;
    this._jwtExpiry = 0;
    this._reconnectDelay = 2000;
    this._maxReconnectDelay = 60000;
    this._stopping = false;
    this._pendingRequests = new Map();  // reqId → { resolve, reject, timer }
    this._reqCounter = 0;
    this._handlers = {};  // msgType → callback
    this._connectPromise = null;
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
          ws.once('open', () => {
            this._ws = ws;
            this._reconnectDelay = 2000;
            attempts = 0;
            this._logger.info('[GameClient] connected');
            resolve();
          });
          ws.once('error', err => {
            try { ws.terminate(); } catch (_) {}
            reject(err);
          });
          ws.once('close', () => {
            if (!resolve._called) reject(new Error('WebSocket closed before open'));
          });

          ws.on('message', data => this._onMessage(data));
          ws.on('close', (code, reason) => {
            this._logger.warn(`[GameClient] disconnected (${code} ${reason}) — reconnecting…`);
            if (!this._stopping) this._scheduleReconnect();
          });
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
      if (!this._stopping) this._connect().catch(err => this._logger.error('[GameClient] reconnect error:', err));
    }, delay);
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
   * ACK types the server sends back for each bridge message:
   *   chat_participant_join    → chat_participant_join_ack
   *   chat_participant_target  → chat_participant_target_result
   *   chat_bridge_loot_grant   → chat_bridge_loot_grant_result
   *   chat_participant_inventory → chat_participant_inventory_result
   *   chat_participant_arena_load → chat_participant_arena_load_result
   *   chat_participant_arena_sync → chat_participant_arena_sync_result
   *   chat_participant_arena_leaderboard → chat_participant_arena_leaderboard_result
   */
  async send(type, payload = {}, timeoutMs = 5000) {
    await this._ensureConnected();
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
      this._logger.warn(`[GameClient] cannot send ${type} — not connected`);
      return null;
    }

    return new Promise(resolve => {
      const reqId = `br_${++this._reqCounter}_${Date.now()}`;
      const augmented = { ...payload, _req_id: reqId };

      // Determine the expected ack message type
      const ackType = {
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
      }[type];

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
      } else {
        resolve(null);  // fire-and-forget
      }

      try {
        this._ws.send(JSON.stringify({ type, payload: augmented }));
      } catch (err) {
        this._logger.error(`[GameClient] send error for ${type}:`, err);
        if (ackType && this._pendingRequests.has(reqId)) {
          const { timer } = this._pendingRequests.get(reqId);
          clearTimeout(timer);
          this._pendingRequests.delete(reqId);
          resolve(null);
        }
      }
    });
  }
}

module.exports = { GameClient };
