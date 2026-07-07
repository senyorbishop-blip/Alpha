'use strict';

const tmi = require('tmi.js');

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
      this._logger.info(`[TwitchClient] connected to ${addr}:${port} as ${tags?.username ?? ''}`);
    });
    this._client.on('disconnected', reason => {
      this._logger.warn('[TwitchClient] disconnected:', reason);
    });

    await this._client.connect();
    this._logger.info(`[TwitchClient] joined channel ${this._channel}`);
  }

  /** Send a message to the channel. */
  say(channel, message) {
    this._client.say(channel, message).catch(err =>
      this._logger.error('[TwitchClient] say error:', err)
    );
  }

  /** Reply with @mention. */
  reply(channel, username, message) {
    this.say(channel, `@${username} ${message}`);
  }

  disconnect() {
    this._client.disconnect().catch(() => {});
  }
}

module.exports = { TwitchClient };
