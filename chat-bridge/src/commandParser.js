'use strict';

const { sanitize, validateUsername, sanitizeArg } = require('./sanitizer');

/**
 * Parse and dispatch Twitch chat commands.
 *
 * Commands are loaded from config/commands.json:
 *   { "!join": "join", "!leave": "leave", "!inventory": "inventory", "!target": "target" }
 *
 * The gameClient and twitchClient are injected for sending messages.
 */
class CommandParser {
  constructor({ commandMap, rateLimiter, gameClient, twitchClient }) {
    this._commandMap = commandMap;  // e.g. { "!join": "join", ... }
    this._rate = rateLimiter;
    this._game = gameClient;
    this._twitch = twitchClient;
  }

  /**
   * Entry point: called by twitchClient on every chat message.
   * @param {string} channel - Twitch channel (with leading #)
   * @param {object} tags - tmi.js tags object (has tags['display-name'], tags.username)
   * @param {string} message - raw chat message
   */
  async handle(channel, tags, message) {
    const raw = String(message ?? '').trim();
    if (!raw.startsWith('!')) return;

    const parts = raw.split(/\s+/);
    const cmdRaw = parts[0].toLowerCase();
    const args = parts.slice(1);

    const action = this._commandMap[cmdRaw];
    if (!action) return;  // unknown command — ignore silently

    const username = validateUsername(tags.username ?? tags['display-name'] ?? '');
    if (!username) return;

    const displayName = sanitize(String(tags['display-name'] ?? tags.username ?? username), 32);

    switch (action) {
      case 'join':
        await this._handleJoin(channel, username, displayName);
        break;
      case 'leave':
        await this._handleLeave(channel, username, displayName);
        break;
      case 'inventory':
        await this._handleInventory(channel, username, displayName);
        break;
      case 'target':
        await this._handleTarget(channel, username, displayName, args);
        break;
      default:
        // Custom / unknown action — do nothing
        break;
    }
  }

  async _handleJoin(channel, username, displayName) {
    if (!this._rate.check(username, 'join')) {
      const rem = this._rate.remaining(username, 'join');
      this._twitch.reply(channel, username, `You can rejoin in ${rem}s.`);
      return;
    }

    const result = await this._game.send('chat_participant_join', {
      twitch_username: username,
      display_name: displayName,
    });

    if (result?.already_joined) {
      this._twitch.reply(channel, username, `You're already in the tavern!`);
    } else {
      this._twitch.say(channel, `${displayName} has joined the tavern! Type !inventory to see your items.`);
    }
  }

  async _handleLeave(channel, username, displayName) {
    if (!this._rate.check(username, 'leave')) return;

    await this._game.send('chat_participant_leave', {
      twitch_username: username,
      display_name: displayName,
    });
    this._twitch.reply(channel, username, `You have left the tavern. Farewell!`);
  }

  async _handleInventory(channel, username, displayName) {
    if (!this._rate.check(username, 'inventory')) {
      const rem = this._rate.remaining(username, 'inventory');
      this._twitch.reply(channel, username, `Please wait ${rem}s before checking inventory again.`);
      return;
    }

    const result = await this._game.send('chat_participant_inventory', {
      twitch_username: username,
    });

    if (!result?.found) {
      this._twitch.reply(channel, username, `You're not in the tavern yet — type !join first.`);
      return;
    }

    const items = result.items ?? [];
    if (items.length === 0) {
      this._twitch.reply(channel, username, `Your satchel is empty. Keep watching — loot may come!`);
    } else {
      const list = items.map(i => {
        const charges = i.charges_max > 0 ? ` (${i.charges_current}/${i.charges_max})` : '';
        return `${i.name}${charges}`;
      }).join(', ');
      this._twitch.reply(channel, username, `Your items: ${list}`);
    }
  }

  async _handleTarget(channel, username, displayName, args) {
    if (!this._rate.check(username, 'target')) {
      const rem = this._rate.remaining(username, 'target');
      this._twitch.reply(channel, username, `You must wait ${rem}s before targeting again.`);
      return;
    }

    if (args.length === 0) {
      this._twitch.reply(channel, username, `Usage: !target <token name>`);
      return;
    }

    const targetName = sanitizeArg(args.join(' '), 64);
    if (!targetName) {
      this._twitch.reply(channel, username, `Invalid target name.`);
      return;
    }

    const result = await this._game.send('chat_participant_target', {
      twitch_username: username,
      display_name: displayName,
      target_name: targetName,
    });

    if (result?.success) {
      this._twitch.say(channel, `${displayName} used ${result.item_name} on ${result.target_name}!`);
    } else {
      this._twitch.reply(channel, username, result?.message ?? `Could not use item.`);
    }
  }
}

module.exports = { CommandParser };
