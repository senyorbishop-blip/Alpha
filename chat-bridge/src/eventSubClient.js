'use strict';

const WebSocket = require('ws');
const https = require('https');

const EVENTSUB_WS_URL = 'wss://eventsub.wss.twitch.tv/ws';
const HELIX_BASE = 'https://api.twitch.tv/helix';

/**
 * Connects to the Twitch EventSub WebSocket transport.
 *
 * Handles:
 *  - channel.subscribe
 *  - channel.subscription.gift
 *  - channel.subscription.message (resub)
 *  - channel.cheer
 *  - channel.raid
 *
 * Emits events via registered handlers:
 *   on('sub',   { username, displayName, months, gifted })
 *   on('bits',  { username, displayName, amount, message })
 *   on('raid',  { fromUsername, fromDisplayName, viewers })
 */
class EventSubClient {
  constructor({ clientId, oauthToken, broadcasterId, logger }) {
    this._clientId = clientId;
    this._oauthToken = oauthToken;
    this._broadcasterId = broadcasterId;
    this._logger = logger ?? console;
    this._ws = null;
    this._sessionId = null;
    this._reconnectDelay = 2000;
    this._stopping = false;
    this._handlers = {};
  }

  on(event, fn) {
    this._handlers[event] = fn;
  }

  async start() {
    this._stopping = false;
    await this._connect();
  }

  stop() {
    this._stopping = true;
    if (this._ws) try { this._ws.close(); } catch (_) {}
  }

  _emit(event, data) {
    const fn = this._handlers[event];
    if (fn) {
      try { fn(data); } catch (err) {
        this._logger.error(`[EventSub] handler error for ${event}:`, err);
      }
    }
  }

  async _connect() {
    while (!this._stopping) {
      try {
        await new Promise((resolve, reject) => {
          const ws = new WebSocket(EVENTSUB_WS_URL);
          this._ws = ws;

          ws.once('open', () => {
            this._logger.info('[EventSub] WebSocket connected');
            this._reconnectDelay = 2000;
          });

          ws.on('message', raw => {
            try { this._onMessage(JSON.parse(raw)); } catch (_) {}
          });

          ws.once('close', (code, reason) => {
            this._logger.warn(`[EventSub] disconnected (${code} ${reason})`);
            resolve();  // exit inner promise → retry
          });

          ws.once('error', err => {
            this._logger.error('[EventSub] WS error:', err.message);
            resolve();
          });
        });

        if (!this._stopping) {
          const delay = Math.min(this._reconnectDelay * 1.5, 120000);
          this._reconnectDelay = delay;
          this._logger.info(`[EventSub] reconnecting in ${Math.round(delay / 1000)}s…`);
          await new Promise(r => setTimeout(r, delay));
        }
      } catch (err) {
        this._logger.error('[EventSub] connect error:', err);
        await new Promise(r => setTimeout(r, this._reconnectDelay));
      }
    }
  }

  async _onMessage(msg) {
    const msgType = msg.metadata?.message_type;

    if (msgType === 'session_welcome') {
      this._sessionId = msg.payload?.session?.id;
      this._logger.info(`[EventSub] session_id=${this._sessionId}`);
      await this._subscribe();
      return;
    }

    if (msgType === 'session_reconnect') {
      const reconnectUrl = msg.payload?.session?.reconnect_url;
      this._logger.info(`[EventSub] reconnect requested to ${reconnectUrl}`);
      // Close current; the loop will reconnect
      if (this._ws) this._ws.close();
      return;
    }

    if (msgType === 'notification') {
      this._dispatchNotification(msg);
    }
  }

  _dispatchNotification(msg) {
    const subType = msg.metadata?.subscription_type;
    const event = msg.payload?.event ?? {};

    switch (subType) {
      case 'channel.subscribe':
      case 'channel.subscription.message': {
        const username = String(event.user_login ?? '').toLowerCase();
        const displayName = event.user_name ?? username;
        const months = event.cumulative_months ?? event.streak_months ?? 1;
        this._emit('sub', { username, displayName, months, gifted: false });
        break;
      }
      case 'channel.subscription.gift': {
        const gifterUsername = String(event.user_login ?? '').toLowerCase();
        const gifterDisplay = event.user_name ?? gifterUsername;
        const recipientUsername = String(event.recipient_user_login ?? '').toLowerCase();
        const recipientDisplay = event.recipient_user_name ?? recipientUsername;
        // Grant loot to the recipient
        this._emit('sub', {
          username: recipientUsername,
          displayName: recipientDisplay,
          months: 1,
          gifted: true,
          gifterUsername,
          gifterDisplay,
        });
        break;
      }
      case 'channel.cheer': {
        const username = String(event.user_login ?? '').toLowerCase();
        const displayName = event.user_name ?? username;
        const amount = Number(event.bits ?? 0);
        const message = String(event.message ?? '');
        this._emit('bits', { username, displayName, amount, message });
        break;
      }
      case 'channel.raid': {
        const fromUsername = String(event.from_broadcaster_user_login ?? '').toLowerCase();
        const fromDisplayName = event.from_broadcaster_user_name ?? fromUsername;
        const viewers = Number(event.viewers ?? 0);
        this._emit('raid', { fromUsername, fromDisplayName, viewers });
        break;
      }
    }
  }

  async _subscribe() {
    if (!this._sessionId || !this._broadcasterId) return;
    const subscriptions = [
      'channel.subscribe',
      'channel.subscription.gift',
      'channel.subscription.message',
      'channel.cheer',
      'channel.raid',
    ];

    for (const type of subscriptions) {
      const condition = type === 'channel.raid'
        ? { to_broadcaster_user_id: this._broadcasterId }
        : { broadcaster_user_id: this._broadcasterId };

      try {
        await this._helixPost('/eventsub/subscriptions', {
          type,
          version: '1',
          condition,
          transport: { method: 'websocket', session_id: this._sessionId },
        });
        this._logger.info(`[EventSub] subscribed to ${type}`);
      } catch (err) {
        this._logger.error(`[EventSub] failed to subscribe to ${type}:`, err.message);
      }
    }
  }

  _helixPost(path, body) {
    return new Promise((resolve, reject) => {
      const data = JSON.stringify(body);
      const req = https.request(`${HELIX_BASE}${path}`, {
        method: 'POST',
        headers: {
          'Client-Id': this._clientId,
          'Authorization': `Bearer ${this._oauthToken}`,
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(data),
        },
      }, res => {
        let body = '';
        res.on('data', c => { body += c; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try { resolve(JSON.parse(body)); } catch { resolve({}); }
          } else {
            reject(new Error(`Helix ${res.statusCode}: ${body}`));
          }
        });
      });
      req.on('error', reject);
      req.write(data);
      req.end();
    });
  }
}

module.exports = { EventSubClient };
