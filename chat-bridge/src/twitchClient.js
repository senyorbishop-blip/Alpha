'use strict';

const tmi = require('tmi.js');

// Twitch allows ~20 messages / 30s for a regular bot account. Keep a safety
// margin and smooth sends out with a minimum gap so bursts (arena narration,
// loot announcements) never trip the limit.
const OUTBOUND_WINDOW_MS = 30_000;
const OUTBOUND_MAX_PER_WINDOW = 18;
const OUTBOUND_MIN_GAP_MS = 1_100;
const OUTBOUND_MAX_QUEUE = 40;

/**
 * Wraps tmi.js to connect the bot to Twitch IRC chat.
 *
 * Emits:
 *   'message' (channel, tags, message, self)
 */
class TwitchClient {
  constructor({ username, oauthToken, channel, logger }) {
    this._channel = channel.startsWith('#') ? channel : `#${channel}`;
    this._logger = logger ?? console;
    this._client = new tmi.Client({
      options: { debug: false },
      identity: { username, password: oauthToken },
      channels: [this._channel],
      connection: { reconnect: true, secure: true },
    });
    this._messageHandler = null;

    // Outbound throttle state
    this._queue = [];          // [{channel, message}]
    this._sentTimestamps = []; // send times within the rate window
    this._drainTimer = null;
    this._lastSentAt = 0;
  }

  onMessage(fn) {
    this._messageHandler = fn;
  }

  async connect() {
    this._client.on('message', (channel, tags, message, self) => {
      if (self) return;
      if (this._messageHandler) {
        this._messageHandler(channel, tags, message).catch(err =>
          this._logger.error('[TwitchClient] message handler error:', err)
        );
      }
    });

    this._client.on('connected', (addr, port) => {
      this._logger.info(`[TwitchClient] connected to ${addr}:${port}`);
    });
    this._client.on('disconnected', reason => {
      this._logger.warn('[TwitchClient] disconnected:', reason);
    });

    await this._client.connect();
    this._logger.info(`[TwitchClient] joined channel ${this._channel}`);
  }

  /** Queue a message to the channel (throttled to respect Twitch limits). */
  say(channel, message) {
    if (this._queue.length >= OUTBOUND_MAX_QUEUE) {
      this._logger.warn('[TwitchClient] outbound queue full — dropping message');
      return;
    }
    this._queue.push({ channel, message });
    this._drainQueue();
  }

  /** Reply with @mention. */
  reply(channel, username, message) {
    this.say(channel, `@${username} ${message}`);
  }

  _drainQueue() {
    if (this._drainTimer || this._queue.length === 0) return;

    const now = Date.now();
    this._sentTimestamps = this._sentTimestamps.filter(t => now - t < OUTBOUND_WINDOW_MS);

    let waitMs = 0;
    if (now - this._lastSentAt < OUTBOUND_MIN_GAP_MS) {
      waitMs = OUTBOUND_MIN_GAP_MS - (now - this._lastSentAt);
    }
    if (this._sentTimestamps.length >= OUTBOUND_MAX_PER_WINDOW) {
      const windowFreesAt = this._sentTimestamps[0] + OUTBOUND_WINDOW_MS;
      waitMs = Math.max(waitMs, windowFreesAt - now);
    }

    if (waitMs > 0) {
      this._drainTimer = setTimeout(() => {
        this._drainTimer = null;
        this._drainQueue();
      }, waitMs);
      if (typeof this._drainTimer.unref === 'function') this._drainTimer.unref();
      return;
    }

    const { channel, message } = this._queue.shift();
    this._lastSentAt = Date.now();
    this._sentTimestamps.push(this._lastSentAt);
    this._client.say(channel, message).catch(err =>
      this._logger.error('[TwitchClient] say error:', err)
    );

    if (this._queue.length > 0) this._drainQueue();
  }

  disconnect() {
    if (this._drainTimer) {
      clearTimeout(this._drainTimer);
      this._drainTimer = null;
    }
    this._queue = [];
    this._client.disconnect().catch(() => {});
  }
}

module.exports = { TwitchClient };
