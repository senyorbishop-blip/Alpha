'use strict';

const { sanitize, validateUsername, sanitizeArg } = require('./sanitizer');

const DEFAULT_RESPONSES = {
  join_success:       '{user} has entered the tavern! Type !inventory to see your items.',
  join_already:       "You're already in the tavern, {user}!",
  join_cooldown:      'Hold on, {user} — you can rejoin in {seconds}s.',
  leave_success:      'Farewell, {user}! You\'ve left the tavern.',
  target_hit:         '{user} uses {item} on {target}!',
  target_no_item:     "{user}, you don't have anything to use yet. Subs and cheers earn items!",
  target_not_joined:  '{user}, type !join first to enter the game.',
  target_no_name:     '{user}, usage: !target <name>',
  target_invalid:     '{user}, invalid target name.',
  target_miss:        '{user}: {message}',
  target_cooldown:    '{user}, catch your breath — try again in {seconds}s.',
  inventory_empty:    '{user}, your satchel is empty. Keep watching — loot may come!',
  inventory_list:     '{user}, your items: {items}',
  inventory_not_joined: "{user}, you're not in the tavern yet — type !join first.",
  inventory_cooldown: '{user}, please wait {seconds}s before checking inventory again.',
  help_header:        'Available commands: {commands}',
  bridge_paused:      '{user}, chat interactions are paused by the DM. Hang tight!',
};

/**
 * Parse and dispatch Twitch chat commands.
 *
 * Commands are loaded from config/commands.json:
 *   { "!join": "join", "!leave": "leave", "!inventory": "inventory", "!target": "target", "!help": "help" }
 *
 * Response templates are loaded from config/responses.json.
 */
class CommandParser {
  constructor({ commandMap, responses, rateLimiter, gameClient, twitchClient }) {
    this._commandMap = commandMap;
    this._responses = { ...DEFAULT_RESPONSES, ...(responses ?? {}) };
    this._rate = rateLimiter;
    this._game = gameClient;
    this._twitch = twitchClient;
  }

  // ---------------------------------------------------------------------------
  // Template helper
  // ---------------------------------------------------------------------------

  _t(key, vars = {}) {
    let tpl = this._responses[key] ?? key;
    for (const [k, v] of Object.entries(vars)) {
      tpl = tpl.replaceAll(`{${k}}`, String(v ?? ''));
    }
    return tpl;
  }

  // ---------------------------------------------------------------------------
  // Entry point
  // ---------------------------------------------------------------------------

  async handle(channel, tags, message) {
    const raw = String(message ?? '').trim();
    if (!raw.startsWith('!')) return;

    const parts = raw.split(/\s+/);
    const cmdRaw = parts[0].toLowerCase();
    const args = parts.slice(1);

    const action = this._commandMap[cmdRaw];
    if (!action) return;

    const username = validateUsername(tags.username ?? tags['display-name'] ?? '');
    if (!username) return;

    const displayName = sanitize(String(tags['display-name'] ?? tags.username ?? username), 32);

    switch (action) {
      case 'join':      await this._handleJoin(channel, username, displayName); break;
      case 'leave':     await this._handleLeave(channel, username, displayName); break;
      case 'inventory': await this._handleInventory(channel, username, displayName); break;
      case 'target':    await this._handleTarget(channel, username, displayName, args); break;
      case 'help':      this._handleHelp(channel, username); break;
      default:          break;
    }
  }

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  async _handleJoin(channel, username, displayName) {
    if (!this._rate.check(username, 'join')) {
      const rem = this._rate.remaining(username, 'join');
      this._twitch.reply(channel, username, this._t('join_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    const result = await this._game.send('chat_participant_join', {
      twitch_username: username,
      display_name: displayName,
    });

    if (result?.already_joined) {
      this._twitch.reply(channel, username, this._t('join_already', { user: displayName }));
    } else {
      this._twitch.say(channel, this._t('join_success', { user: displayName }));
    }
  }

  async _handleLeave(channel, username, displayName) {
    if (!this._rate.check(username, 'leave')) return;

    await this._game.send('chat_participant_leave', {
      twitch_username: username,
      display_name: displayName,
    });
    this._twitch.reply(channel, username, this._t('leave_success', { user: displayName }));
  }

  async _handleInventory(channel, username, displayName) {
    if (!this._rate.check(username, 'inventory')) {
      const rem = this._rate.remaining(username, 'inventory');
      this._twitch.reply(channel, username, this._t('inventory_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    const result = await this._game.send('chat_participant_inventory', {
      twitch_username: username,
    });

    if (!result?.found) {
      this._twitch.reply(channel, username, this._t('inventory_not_joined', { user: displayName }));
      return;
    }

    const items = result.items ?? [];
    if (items.length === 0) {
      this._twitch.reply(channel, username, this._t('inventory_empty', { user: displayName }));
    } else {
      const targetCmd = this._targetCommand();
      const list = items.map(i => {
        const count = i.charges_max > 0
          ? `${i.charges_current}/${i.charges_max}`
          : `x${i.qty}`;
        const hint = targetCmd ? ` (use: ${targetCmd} <name>)` : '';
        return `${i.name} ${count}${hint}`;
      }).join(', ');
      this._twitch.reply(channel, username, this._t('inventory_list', { user: displayName, items: list }));
    }
  }

  async _handleTarget(channel, username, displayName, args) {
    if (!this._rate.check(username, 'target')) {
      const rem = this._rate.remaining(username, 'target');
      this._twitch.reply(channel, username, this._t('target_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    if (args.length === 0) {
      this._twitch.reply(channel, username, this._t('target_no_name', { user: displayName }));
      return;
    }

    const targetName = sanitizeArg(args.join(' '), 64);
    if (!targetName) {
      this._twitch.reply(channel, username, this._t('target_invalid', { user: displayName }));
      return;
    }

    const result = await this._game.send('chat_participant_target', {
      twitch_username: username,
      display_name: displayName,
      target_name: targetName,
    });

    if (result?.success) {
      this._twitch.say(channel, this._t('target_hit', {
        user: displayName,
        item: result.item_name ?? 'item',
        target: result.target_name ?? targetName,
      }));
    } else {
      this._twitch.reply(channel, username, this._t('target_miss', {
        user: displayName,
        message: result?.message ?? 'Could not use item.',
      }));
    }
  }

  _handleHelp(channel, username) {
    const cmds = Object.keys(this._commandMap)
      .filter(k => !k.startsWith('_'))
      .join(' ');
    this._twitch.reply(channel, username, this._t('help_header', { commands: cmds }));
  }

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  /** Return the first command key that maps to the 'target' action, or null. */
  _targetCommand() {
    for (const [cmd, action] of Object.entries(this._commandMap)) {
      if (action === 'target') return cmd;
    }
    return null;
  }
}

module.exports = { CommandParser };
