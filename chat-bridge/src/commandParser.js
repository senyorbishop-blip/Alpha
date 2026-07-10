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
  target_multiple_items: "@{user} you're holding: {items} — use {useCmd} <item> <target>",
  use_no_args:        '{user}, usage: {useCmd} <item> <target> — e.g. {useCmd} fireball goblin, or {useCmd} "healing spark" grognak',
  use_ambiguous:      '@{user} which item? Matching: {items} — be more specific (quotes work: {useCmd} "healing spark" <target>).',
  use_not_owned:      "@{user} you don't have '{item}'. You have: {items}",
  inventory_empty:    '{user}, your satchel is empty. Keep watching — loot may come!',
  inventory_list:     '{user}, your items: {items}',
  inventory_not_joined: "{user}, you're not in the tavern yet — type !join first.",
  inventory_cooldown: '{user}, please wait {seconds}s before checking inventory again.',
  help_header:        'Available commands: {commands}',
  bridge_paused:      '{user}, chat interactions are paused by the DM. Hang tight!',
  vote_registered:    '@{user} Your vote for option {number} ({option}) has been recorded!',
  vote_changed:       '@{user} Changed your vote to option {number} ({option}).',
  vote_invalid:       '@{user} Invalid option. Type !vote 1 through !vote {max}.',
  vote_no_poll:       "@{user} There's no active vote right now.",
  vote_cooldown:      '@{user} Wait {seconds}s before voting again.',
  name_set:           '@{user} You are now known as {name}!',
  name_taken:         '@{user} That name is already taken. Choose another.',
  name_invalid:       "@{user} Name must use letters, numbers, spaces, hyphens, or apostrophes (max 32 chars).",
  name_not_joined:    '@{user} Type !join first.',
  name_cooldown:      '@{user} Wait {seconds}s before changing your name again.',
  me_summary:         '@{user} — {charName} | Items: {items} | Sessions: {sessions} | Damage dealt: {damage} | Arena: {wins}W/{losses}L',
  me_not_joined:      '@{user} Type !join first to enter the game.',
  me_cooldown:        '@{user} Wait {seconds}s.',
  unknown_command:    "@{user} I don't know that one — try !help for the command list.",
  progression_cooldown: '@{user} Wait {seconds}s.',
};

// Usage hint per action, in display order, for the compact !help reply. Only
// actions actually present in the command map are listed, and aliases collapse
// to a single entry (the first command key mapped to that action).
const ACTION_USAGE = [
  ['join',        ''],
  ['leave',       ''],
  ['inventory',   ''],
  ['target',      '<name>'],
  ['use',         '<item> <target>'],
  ['stats',       ''],
  ['bag',         ''],
  ['equip',       '<item>'],
  ['shop',        ''],
  ['buy',         '<item>'],
  ['levelup',     ''],
  ['quest',       '<n>'],
  ['heal',        ''],
  ['rebirth',     ''],
  ['leaderboard', ''],
  ['graveyard',   ''],
  ['vote',        '<n>'],
  ['name',        '<name>'],
  ['me',          ''],
  ['help',        ''],
];

const PROGRESSION_ACTIONS = new Set([
  'stats', 'bag', 'equip', 'shop', 'buy', 'levelup', 'leaderboard',
  'quest', 'heal', 'rebirth', 'graveyard',
]);

/**
 * Parse and dispatch Twitch chat commands.
 *
 * Commands are loaded from config/commands.json:
 *   { "!join": "join", "!leave": "leave", "!inventory": "inventory", "!target": "target", "!help": "help" }
 *
 * Response templates are loaded from config/responses.json.
 */
class CommandParser {
  constructor({ commandMap, responses, rateLimiter, gameClient, twitchClient, arenaHandler, progressionHandler }) {
    this._commandMap = commandMap;
    this._responses = { ...DEFAULT_RESPONSES, ...(responses ?? {}) };
    this._rate = rateLimiter;
    this._game = gameClient;
    this._twitch = twitchClient;
    this._arenaHandler = arenaHandler || null;
    this._progressionHandler = progressionHandler || null;
    // Chatters who have !joined this session — used to decide whether an
    // unknown !command deserves a gentle "!help" nudge. Only joined chatters
    // get the nudge so the bot never replies to other bots' commands
    // (!discord, !uptime, …) from random viewers.
    this._joinedUsers = new Set();
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
    // Twitch @-autocomplete prefixes names with '@' ("!duel @PotatoWizard").
    // Strip a single leading '@' from every arg so all commands match names
    // the same way chatters type them.
    const args = parts.slice(1).map(a => a.replace(/^@/, ''));

    const username = validateUsername(tags.username ?? tags['display-name'] ?? '');
    if (!username) return;

    const displayName = sanitize(String(tags['display-name'] ?? tags.username ?? username), 32);
    const userId = tags['user-id'] ?? '';

    const action = this._commandMap[cmdRaw];
    if (!action) {
      this._handleUnknown(channel, username, displayName);
      return;
    }

    if (PROGRESSION_ACTIONS.has(action)) {
      if (!this._progressionHandler) return;
      if (!this._rate.check(username, action)) {
        const rem = this._rate.remaining(username, action);
        this._twitch.reply(channel, username, this._t('progression_cooldown', { user: displayName, seconds: rem }));
        return;
      }
      await this._progressionHandler(channel, username, displayName, action, args);
      return;
    }

    switch (action) {
      case 'join':      await this._handleJoin(channel, userId, username, displayName); break;
      case 'leave':     await this._handleLeave(channel, userId, username, displayName); break;
      case 'inventory': await this._handleInventory(channel, userId, username, displayName); break;
      case 'target':    await this._handleTarget(channel, userId, username, displayName, args); break;
      case 'use':       await this._handleUse(channel, userId, username, displayName, args); break;
      case 'help':      this._handleHelp(channel, username); break;
      case 'vote':      await this._handleVote(channel, username, displayName, args); break;
      case 'name':      await this._handleName(channel, userId, username, displayName, args); break;
      case 'me':        await this._handleMe(channel, username, displayName); break;
      case 'arena':
        if (this._arenaHandler) {
          const arenaAction = cmdRaw.slice(1); // 'duel', 'accept', 'decline'
          this._arenaHandler(channel, username, displayName, arenaAction, args);
        }
        break;
      default:          break;
    }
  }

  /**
   * Unknown !command from a joined chatter → one gentle pointer to !help,
   * per-user rate-limited so a spammer can't turn the bot into an echo.
   * Non-joined chatters are ignored entirely.
   */
  _handleUnknown(channel, username, displayName) {
    if (!this._joinedUsers.has(username)) return;
    if (!this._rate.check(username, 'unknown_cmd')) return;
    this._twitch.reply(channel, username, this._t('unknown_command', { user: displayName }));
  }

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  async _handleJoin(channel, userId, username, displayName) {
    if (!this._rate.check(username, 'join')) {
      const rem = this._rate.remaining(username, 'join');
      this._twitch.reply(channel, username, this._t('join_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    const result = await this._game.send('chat_participant_join', {
      twitch_username: username,
      twitch_user_id: userId,
      display_name: displayName,
    });

    if (result) this._joinedUsers.add(username);

    if (result?.already_joined) {
      this._twitch.reply(channel, username, this._t('join_already', { user: displayName }));
    } else if (result?.returning && (result?.sessions_joined || 0) > 1) {
      const charName = result.character_name || displayName;
      const stats = result.lifetime_stats || {};
      const sess = result.sessions_joined;
      const dmg = stats.damage_dealt > 0 ? `, ${stats.damage_dealt} damage dealt` : '';
      this._twitch.say(channel, `Welcome back, ${charName}! (${sess} sessions${dmg}).`);
    } else {
      this._twitch.say(channel, this._t('join_success', { user: displayName }));
    }
  }

  async _handleLeave(channel, userId, username, displayName) {
    if (!this._rate.check(username, 'leave')) return;

    await this._game.send('chat_participant_leave', {
      twitch_username: username,
      twitch_user_id: userId,
      display_name: displayName,
    });
    this._joinedUsers.delete(username);
    this._twitch.reply(channel, username, this._t('leave_success', { user: displayName }));
  }

  async _handleInventory(channel, userId, username, displayName) {
    if (!this._rate.check(username, 'inventory')) {
      const rem = this._rate.remaining(username, 'inventory');
      this._twitch.reply(channel, username, this._t('inventory_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    const result = await this._game.send('chat_participant_inventory', {
      twitch_username: username,
      twitch_user_id: userId,
    });

    if (!result?.found) {
      this._twitch.reply(channel, username, this._t('inventory_not_joined', { user: displayName }));
      return;
    }
    this._joinedUsers.add(username);

    const items = result.items ?? [];
    if (items.length === 0) {
      this._twitch.reply(channel, username, this._t('inventory_empty', { user: displayName }));
    } else {
      const targetCmd = this._targetCommand();
      const useCmd = this._commandFor('use');
      // With 2+ items !target would ask which one — hint the !use form instead.
      const multi = items.length >= 2 && !!useCmd;
      const list = items.map(i => {
        const count = i.charges_max > 0
          ? `${i.charges_current}/${i.charges_max}`
          : `x${i.qty}`;
        let hint = '';
        if (multi) {
          const ref = /\s/.test(i.name) ? `"${i.name.toLowerCase()}"` : i.name.toLowerCase();
          hint = ` (use: ${useCmd} ${ref} <target>)`;
        } else if (targetCmd) {
          hint = ` (use: ${targetCmd} <name>)`;
        }
        return `${i.name} ${count}${hint}`;
      }).join(', ');
      this._twitch.reply(channel, username, this._t('inventory_list', { user: displayName, items: list }));
    }
  }

  async _handleTarget(channel, userId, username, displayName, args) {
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

    await this._sendTargetRequest(channel, userId, username, displayName, targetName, '');
  }

  /**
   * !use <item> <target> — pick which held item to use. The item may be
   * quoted ("healing spark") to span multiple words; unquoted, the first word
   * is the item and the rest is the target. The server matches the item
   * against the chatter's inventory (prefix/fuzzy) and validates ownership.
   */
  async _handleUse(channel, userId, username, displayName, args) {
    // Shares the 'target' cooldown bucket — same game action.
    if (!this._rate.check(username, 'target')) {
      const rem = this._rate.remaining(username, 'target');
      this._twitch.reply(channel, username, this._t('target_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    const raw = args.join(' ');
    const m = raw.match(/^\s*(?:"([^"]+)"|'([^']+)'|(\S+))\s+(\S.*)$/);
    if (!m) {
      this._twitch.reply(channel, username, this._t('use_no_args', {
        user: displayName, useCmd: this._useCommand(),
      }));
      return;
    }

    const itemName = sanitizeArg(m[1] ?? m[2] ?? m[3], 80);
    const targetName = sanitizeArg(m[4], 64);
    if (!itemName || !targetName) {
      this._twitch.reply(channel, username, this._t('target_invalid', { user: displayName }));
      return;
    }

    await this._sendTargetRequest(channel, userId, username, displayName, targetName, itemName);
  }

  /** Send chat_participant_target (optionally with an item choice) and render the reply. */
  async _sendTargetRequest(channel, userId, username, displayName, targetName, itemName) {
    const payload = {
      twitch_username: username,
      twitch_user_id: userId,
      display_name: displayName,
      target_name: targetName,
    };
    if (itemName) payload.item_name = itemName;

    const result = await this._game.send('chat_participant_target', payload);

    if (result?.success) {
      this._twitch.say(channel, this._t('target_hit', {
        user: displayName,
        item: result.item_name ?? 'item',
        target: result.target_name ?? targetName,
      }));
    } else if (result?.error_code === 'no_item') {
      this._twitch.reply(channel, username, this._t('target_no_item', { user: displayName }));
    } else if (result?.error_code === 'not_joined') {
      this._twitch.reply(channel, username, this._t('target_not_joined', { user: displayName }));
    } else if (result?.error_code === 'multiple_items') {
      this._twitch.reply(channel, username, this._t('target_multiple_items', {
        user: displayName,
        items: (result.items ?? []).join(', '),
        useCmd: this._useCommand(),
      }));
    } else if (result?.error_code === 'item_ambiguous') {
      this._twitch.reply(channel, username, this._t('use_ambiguous', {
        user: displayName,
        items: (result.matching_items ?? []).join(', '),
        useCmd: this._useCommand(),
      }));
    } else if (result?.error_code === 'item_not_owned') {
      this._twitch.reply(channel, username, this._t('use_not_owned', {
        user: displayName,
        item: itemName,
        items: (result.owned_items ?? []).join(', '),
      }));
    } else {
      this._twitch.reply(channel, username, this._t('target_miss', {
        user: displayName,
        message: result?.message ?? 'Could not use item.',
      }));
    }
  }

  _handleHelp(channel, username) {
    // Compact list with usage hints. Aliases collapse to one entry per action
    // (first mapped command wins), arena commands get a combined hint.
    const byAction = {};
    for (const [cmd, action] of Object.entries(this._commandMap)) {
      if (cmd.startsWith('_')) continue;
      if (!byAction[action]) byAction[action] = cmd;
    }

    const parts = [];
    for (const [action, usage] of ACTION_USAGE) {
      const cmd = byAction[action];
      if (!cmd) continue;
      parts.push(usage ? `${cmd} ${usage}` : cmd);
    }
    if (byAction.arena) {
      parts.push('!duel <name>', '!accept', '!decline');
    }
    this._twitch.reply(channel, username, this._t('help_header', { commands: parts.join(', ') }));
  }

  async _handleVote(channel, username, displayName, args) {
    if (!this._rate.check(username, 'vote')) {
      const rem = this._rate.remaining(username, 'vote');
      this._twitch.reply(channel, username, this._t('vote_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    const numStr = (args[0] || '').replace(/\D/g, '');
    const num = parseInt(numStr, 10);
    if (!numStr || isNaN(num) || num < 1) {
      this._twitch.reply(channel, username, this._t('vote_invalid', { user: displayName, max: '?' }));
      return;
    }

    // Check per-user invalid-option warning state
    if (!this._voteWarnedUsers) this._voteWarnedUsers = new Map();

    const optionIndex = num - 1;  // convert to 0-indexed
    const result = await this._game.send('chat_bridge_poll_vote', {
      twitch_username: username,
      option_index: optionIndex,
    });

    if (!result) {
      // no active poll or timeout
      return;
    }

    if (result.success) {
      this._voteWarnedUsers.delete(username);
      if (result.changed) {
        this._twitch.reply(channel, username, this._t('vote_changed', {
          user: displayName, number: num, option: result.option_label || String(num),
        }));
      } else {
        this._twitch.reply(channel, username, this._t('vote_registered', {
          user: displayName, number: num, option: result.option_label || String(num),
        }));
      }
    } else {
      const code = result.error_code;
      if (code === 'no_active_poll' || code === 'poll_id_mismatch') {
        this._twitch.reply(channel, username, this._t('vote_no_poll', { user: displayName }));
      } else if (code === 'invalid_option') {
        // Only warn once per user per active poll
        if (!this._voteWarnedUsers.get(username)) {
          this._voteWarnedUsers.set(username, true);
          this._twitch.reply(channel, username, this._t('vote_invalid', {
            user: displayName, max: result.max_valid || '?',
          }));
        }
        // subsequent invalid attempts are silently ignored
      }
      // bridge_paused → silently ignore
    }
  }

  async _handleName(channel, userId, username, displayName, args) {
    if (!this._rate.check(username, 'name')) {
      const rem = this._rate.remaining(username, 'name');
      this._twitch.reply(channel, username, this._t('name_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    const charName = args.join(' ').trim();
    if (!charName) {
      this._twitch.reply(channel, username, this._t('name_invalid', { user: displayName }));
      return;
    }

    const result = await this._game.send('chat_participant_name_set', {
      twitch_username: username,
      twitch_user_id: userId || '',
      character_name: charName,
    });

    if (!result) return;

    if (result.success) {
      this._twitch.reply(channel, username, this._t('name_set', {
        user: displayName, name: result.character_name || charName,
      }));
    } else {
      const code = result.error_code;
      if (code === 'name_taken') {
        this._twitch.reply(channel, username, this._t('name_taken', { user: displayName }));
      } else if (code === 'not_joined') {
        this._twitch.reply(channel, username, this._t('name_not_joined', { user: displayName }));
      } else {
        this._twitch.reply(channel, username, this._t('name_invalid', { user: displayName }));
      }
    }
  }

  async _handleMe(channel, username, displayName) {
    if (!this._rate.check(username, 'me')) {
      const rem = this._rate.remaining(username, 'me');
      this._twitch.reply(channel, username, this._t('me_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    const result = await this._game.send('chat_participant_me', {
      twitch_username: username,
    });

    if (!result?.found) {
      this._twitch.reply(channel, username, this._t('me_not_joined', { user: displayName }));
      return;
    }

    const stats = result.lifetime_stats || {};
    const arena = result.arena_stats || {};
    const charName = result.character_name || result.display_name || displayName;

    this._twitch.reply(channel, username, this._t('me_summary', {
      user: displayName,
      charName,
      items: result.inventory_count || 0,
      sessions: stats.sessions_joined || 0,
      damage: stats.damage_dealt || 0,
      wins: arena.wins || 0,
      losses: arena.losses || 0,
    }));
  }

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  /** Return the first command key that maps to the given action, or null. */
  _commandFor(action) {
    for (const [cmd, mapped] of Object.entries(this._commandMap)) {
      if (mapped === action) return cmd;
    }
    return null;
  }

  /** Return the first command key that maps to the 'target' action, or null. */
  _targetCommand() {
    return this._commandFor('target');
  }

  /** Command for the 'use' action, for reply templates (falls back to !use). */
  _useCommand() {
    return this._commandFor('use') || '!use';
  }
}

module.exports = { CommandParser };
