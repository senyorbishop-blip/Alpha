'use strict';

const { sanitize, validateUsername, sanitizeArg } = require('./sanitizer');
const { describeBonuses } = require('./arenaProgression');

const DEFAULT_RESPONSES = {
  join_success:       '{user} has entered the tavern! Type !inventory to see your items.',
  join_already:       "You're already in the tavern, {user}!",
  join_cooldown:      'Hold on, {user} — you can rejoin in {seconds}s.',
  leave_success:      'Farewell, {user}! You\'ve left the tavern.',
  target_hit:         '{user} hurls a {item} at {target} — direct hit for {damage} damage!',
  target_heal:        '{user} uses {item} on {target}, restoring {healed} HP!',
  target_kill:        '💀 {target} falls! Final blow by {user}!',
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
  me_arena:           '{class} Lv{level} ({xp}/{xpNext} XP) | {gold}g | Gear: {gear}',
  bag_list:           '@{user} 🎒 {items}',
  bag_empty:          '@{user} Your arena bag is empty — win duels to earn loot!',
  equip_success:      '@{user} Equipped {item} ({slot}).',
  equip_not_found:    "@{user} No item matching '{query}' in your bag. Type !bag to see it.",
  equip_wrong_item:   "@{user} {item} can't be equipped.",
  equip_usage:        '@{user} usage: !equip <item name>',
  shop_list:          "🛒 Today's arena shop: {items} — buy with !buy <number or name>",
  shop_empty:         '@{user} The arena shop is closed today.',
  buy_success:        '@{user} Bought {item} for {price}g! ({gold}g left) Equip it with !equip {item}.',
  buy_insufficient:   '@{user} Not enough gold — {item} costs {price}g, you have {gold}g.',
  buy_not_found:      "@{user} No shop item matching '{query}'. Type !shop to see today's stock.",
  leaderboard_header: '🏆 Arena leaderboard: {entries}',
  leaderboard_empty:  'No arena champions yet — start a !duel!',
  reroll_result:      '{announcement}',
  reroll_denied:      '@{user} {reason}',
  levelup_applied:    '@{user} +1 {stat}! ({stat} is now {value})',
  levelup_invalid_stat: '@{user} Pick one of: str dex con int wis cha.',
  levelup_no_points:  '@{user} No pending stat points. Win duels to level up!',
  no_character:       '@{user} You have no arena character yet — type !join to get started!',
  wager_disabled:     'Wagers are disabled in this arena.',
  wager_invalid:      'Wager must be a positive whole number of gold.',
  wager_too_high:     'Max wager is {max}g.',
  wager_insufficient: "{user}, you only have {gold}g — you can't stake {wager}g.",
};

/** Trim a chat message to stay well under Twitch's 500-char limit. */
function clipMessage(msg, max = 440) {
  const str = String(msg ?? '');
  return str.length > max ? `${str.slice(0, max - 1)}…` : str;
}

/**
 * Parse and dispatch Twitch chat commands.
 *
 * Commands are loaded from config/commands.json:
 *   { "!join": "join", "!leave": "leave", "!inventory": "inventory", "!target": "target", "!help": "help" }
 *
 * Response templates are loaded from config/responses.json.
 */
class CommandParser {
  /**
   * @param {object} opts
   * @param {object} [opts.characterManager] — CharacterManager (arena progression)
   * @param {function} [opts.onJoined]       — async (channel, username, displayName), called after a successful !join
   */
  constructor({ commandMap, responses, rateLimiter, gameClient, twitchClient, arenaHandler, characterManager, onJoined }) {
    this._commandMap = commandMap;
    this._responses = { ...DEFAULT_RESPONSES, ...(responses ?? {}) };
    this._rate = rateLimiter;
    this._game = gameClient;
    this._twitch = twitchClient;
    this._arenaHandler = arenaHandler || null;
    this._charMgr = characterManager || null;
    this._onJoined = onJoined || null;
    this._voteWarnedUsers = new Map();
  }

  /** Clear the per-poll invalid-vote warning state (index.js calls this on 'poll_created'). */
  resetVoteWarnings() {
    this._voteWarnedUsers.clear();
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
    const userId = tags['user-id'] ?? '';

    switch (action) {
      case 'join':      await this._handleJoin(channel, userId, username, displayName); break;
      case 'leave':     await this._handleLeave(channel, userId, username, displayName); break;
      case 'inventory': await this._handleInventory(channel, userId, username, displayName); break;
      case 'target':    await this._handleTarget(channel, userId, username, displayName, args); break;
      case 'help':      this._handleHelp(channel, username); break;
      case 'vote':      await this._handleVote(channel, username, displayName, args); break;
      case 'name':      await this._handleName(channel, userId, username, displayName, args); break;
      case 'me':        await this._handleMe(channel, userId, username, displayName); break;
      case 'bag':       await this._handleBag(channel, username, displayName); break;
      case 'equip':     await this._handleEquip(channel, username, displayName, args); break;
      case 'shop':      await this._handleShop(channel, username, displayName); break;
      case 'buy':       await this._handleBuy(channel, username, displayName, args); break;
      case 'leaderboard': await this._handleLeaderboard(channel, username, displayName); break;
      case 'reroll':    await this._handleReroll(channel, username, displayName); break;
      case 'levelup':   await this._handleLevelup(channel, username, displayName, args); break;
      case 'arena':
        if (this._arenaHandler) {
          const arenaAction = cmdRaw.slice(1); // 'duel', 'accept', 'decline', 'attack', 'defend', 'flee'
          this._arenaHandler(channel, username, displayName, arenaAction, args);
        }
        break;
      default:          break;
    }
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

    // Post-join hook: index.js loads/creates the arena character here.
    if (this._onJoined) {
      try {
        await this._onJoined(channel, username, displayName);
      } catch (err) {
        // Never let arena character setup break the join flow.
      }
    }
  }

  async _handleLeave(channel, userId, username, displayName) {
    if (!this._rate.check(username, 'leave')) return;

    await this._game.send('chat_participant_leave', {
      twitch_username: username,
      twitch_user_id: userId,
      display_name: displayName,
    });
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

    const items = result.items ?? [];
    if (items.length === 0) {
      this._twitch.reply(channel, username, this._t('inventory_empty', { user: displayName }));
    } else {
      const targetCmd = this._targetCommand();
      const list = items.map(i => {
        const count = i.charges_max > 0
          ? `${i.charges_current}/${i.charges_max}`
          : `x${i.qty}`;
        const hint = this._usageHint(i, targetCmd);
        return `${i.name} ${count}${hint ? ` (${hint})` : ''}`;
      }).join(', ');
      this._twitch.reply(channel, username, clipMessage(this._t('inventory_list', { user: displayName, items: list })));
    }
  }

  /**
   * Per-item usage hint: server-provided usage_hint, then the optional
   * _usage_hints map in commands.json ({item-name-substring: hint}),
   * then the generic target-command hint.
   */
  _usageHint(item, targetCmd) {
    if (item.usage_hint) return String(item.usage_hint);
    const hints = this._commandMap._usage_hints;
    if (hints && typeof hints === 'object') {
      const lowerName = String(item.name ?? '').toLowerCase();
      for (const [substr, hint] of Object.entries(hints)) {
        if (substr && lowerName.includes(substr.toLowerCase())) return String(hint);
      }
    }
    return targetCmd ? `use: ${targetCmd} <name>` : '';
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

    const result = await this._game.send('chat_participant_target', {
      twitch_username: username,
      twitch_user_id: userId,
      display_name: displayName,
      target_name: targetName,
    });

    if (result?.success) {
      // Real outcome from the game server: prefer the damage/heal templates,
      // fall back to the server's full narration when neither applies.
      const damage = parseInt(result.damage || 0, 10);
      const healed = parseInt(result.healed || 0, 10);
      const vars = {
        user: displayName,
        item: result.item_name ?? 'item',
        target: result.target_name ?? targetName,
        damage,
        healed,
      };
      if (damage > 0) {
        this._twitch.say(channel, this._t('target_hit', vars));
        if (result.killed) {
          this._twitch.say(channel, this._t('target_kill', vars));
        }
      } else if (healed > 0) {
        this._twitch.say(channel, this._t('target_heal', vars));
      } else {
        this._twitch.say(channel, String(result.message || this._t('target_hit', vars)));
      }
      return;
    }

    const code = result?.error_code;
    if (code === 'not_found') {
      const list = (result.valid_targets || []).join(', ') || 'none';
      this._twitch.reply(channel, username, this._t('target_not_found', {
        user: displayName, target: targetName, targetList: list,
      }));
    } else if (code === 'ambiguous') {
      const suggestions = (result.suggestions || []).join(', ');
      this._twitch.reply(channel, username, this._t('target_ambiguous', {
        user: displayName, suggestions,
      }));
    } else if (code === 'no_item') {
      this._twitch.reply(channel, username, this._t('target_no_item', { user: displayName }));
    } else if (code === 'not_joined') {
      this._twitch.reply(channel, username, this._t('target_not_joined', { user: displayName }));
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

    // Sanitize client-side before sending (server remains the authority).
    const charName = sanitizeArg(args.join(' '), 32);
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

  async _handleMe(channel, userId, username, displayName) {
    if (!this._rate.check(username, 'me')) {
      const rem = this._rate.remaining(username, 'me');
      this._twitch.reply(channel, username, this._t('me_cooldown', { user: displayName, seconds: rem }));
      return;
    }

    const result = await this._game.send('chat_participant_me', {
      twitch_username: username,
      twitch_user_id: userId || '',
    });

    if (!result?.found) {
      this._twitch.reply(channel, username, this._t('me_not_joined', { user: displayName }));
      return;
    }

    const stats = result.lifetime_stats || {};
    const arena = result.arena_stats || {};
    const charName = result.character_name || result.display_name || displayName;

    let msg = this._t('me_summary', {
      user: displayName,
      charName,
      items: result.inventory_count || 0,
      sessions: stats.sessions_joined || 0,
      damage: stats.damage_dealt || 0,
      wins: arena.wins || 0,
      losses: arena.losses || 0,
    });

    // Combine with the arena character (class/level/XP/gold/gear) when available.
    const char = this._charMgr ? await this._charMgr.load(username) : null;
    if (char) {
      const gear = ['weapon', 'armor', 'trinket']
        .map(slot => char.equipped?.[slot]?.name)
        .filter(Boolean)
        .join(', ') || 'none';
      msg += ' | ' + this._t('me_arena', {
        class: char.class,
        level: char.level,
        xp: char.xp,
        xpNext: this._charMgr.xpToNext(char.level),
        gold: char.gold,
        gear,
      });
    }

    this._twitch.reply(channel, username, clipMessage(msg));
  }

  // ---------------------------------------------------------------------------
  // Arena progression handlers (!bag, !equip, !shop, !buy, !leaderboard,
  // !reroll, !levelup)
  // ---------------------------------------------------------------------------

  /** Load the caller's arena character, replying with no_character if absent. */
  async _requireCharacter(channel, username, displayName) {
    const char = this._charMgr ? await this._charMgr.load(username) : null;
    if (!char) {
      this._twitch.reply(channel, username, this._t('no_character', { user: displayName }));
      return null;
    }
    return char;
  }

  async _handleBag(channel, username, displayName) {
    if (!this._rate.check(username, 'bag')) return;
    const char = await this._requireCharacter(channel, username, displayName);
    if (!char) return;

    const items = char.items ?? [];
    if (items.length === 0) {
      this._twitch.reply(channel, username, this._t('bag_empty', { user: displayName }));
      return;
    }

    const equippedNames = new Set(
      ['weapon', 'armor', 'trinket'].map(s => char.equipped?.[s]?.name).filter(Boolean)
    );
    const list = items.map(i => {
      const bonuses = describeBonuses(i);
      const star = equippedNames.has(i.name) ? '★' : '';
      return `${star}${i.name}${bonuses ? ` (${bonuses})` : ''}`;
    }).join(', ');
    this._twitch.reply(channel, username, clipMessage(this._t('bag_list', { user: displayName, items: list })));
  }

  async _handleEquip(channel, username, displayName, args) {
    if (!this._rate.check(username, 'equip')) return;
    const query = args.join(' ').trim();
    if (!query) {
      this._twitch.reply(channel, username, this._t('equip_usage', { user: displayName }));
      return;
    }
    const char = await this._requireCharacter(channel, username, displayName);
    if (!char) return;

    const result = this._charMgr.equip(char, query);
    if (result.success) {
      await this._charMgr.sync(username, displayName);
      this._twitch.reply(channel, username, this._t('equip_success', {
        user: displayName, item: result.item.name, slot: result.slot,
      }));
    } else if (result.error === 'wrong_item') {
      this._twitch.reply(channel, username, this._t('equip_wrong_item', {
        user: displayName, item: result.item?.name ?? query,
      }));
    } else {
      this._twitch.reply(channel, username, this._t('equip_not_found', { user: displayName, query }));
    }
  }

  async _handleShop(channel, username, displayName) {
    if (!this._rate.check(username, 'shop')) return;
    if (!this._charMgr) return;

    const rotation = this._charMgr.getShopRotation();
    if (rotation.length === 0) {
      this._twitch.reply(channel, username, this._t('shop_empty', { user: displayName }));
      return;
    }
    const list = rotation
      .map((item, i) => `${i + 1}. ${item.name} — ${item.price}g`)
      .join(' | ');
    this._twitch.say(channel, clipMessage(this._t('shop_list', { items: list })));
  }

  async _handleBuy(channel, username, displayName, args) {
    if (!this._rate.check(username, 'buy')) return;
    const query = args.join(' ').trim();
    const char = await this._requireCharacter(channel, username, displayName);
    if (!char) return;

    const result = this._charMgr.buyItem(char, query);
    if (result.success) {
      await this._charMgr.sync(username, displayName);
      this._twitch.reply(channel, username, this._t('buy_success', {
        user: displayName, item: result.item.name, price: result.price, gold: result.gold,
      }));
    } else if (result.error === 'insufficient_gold') {
      this._twitch.reply(channel, username, this._t('buy_insufficient', {
        user: displayName, item: result.item, price: result.price, gold: result.gold,
      }));
    } else {
      this._twitch.reply(channel, username, this._t('buy_not_found', { user: displayName, query }));
    }
  }

  async _handleLeaderboard(channel, username, displayName) {
    if (!this._rate.check(username, 'leaderboard')) return;

    const result = await this._game.send('chat_participant_arena_leaderboard', { limit: 5 });
    const entries = result?.entries ?? [];
    if (entries.length === 0) {
      this._twitch.say(channel, this._t('leaderboard_empty'));
      return;
    }
    const list = entries.map((e, i) => {
      const name = e.character_name || e.display_name || '?';
      return `${i + 1}. ${name} the ${e.class ?? '?'} Lv${e.level ?? 1} (${e.wins ?? 0}W/${e.losses ?? 0}L)`;
    }).join(' | ');
    this._twitch.say(channel, clipMessage(this._t('leaderboard_header', { entries: list })));
  }

  async _handleReroll(channel, username, displayName) {
    if (!this._rate.check(username, 'reroll')) return;
    const char = await this._requireCharacter(channel, username, displayName);
    if (!char) return;

    const result = this._charMgr.reroll(char, displayName);
    if (result.success) {
      await this._charMgr.sync(username, displayName);
      this._twitch.say(channel, clipMessage(this._t('reroll_result', { announcement: result.announcement })));
    } else {
      this._twitch.reply(channel, username, this._t('reroll_denied', { user: displayName, reason: result.reason }));
    }
  }

  async _handleLevelup(channel, username, displayName, args) {
    if (!this._rate.check(username, 'levelup')) return;
    const char = await this._requireCharacter(channel, username, displayName);
    if (!char) return;

    const result = this._charMgr.applyStatChoice(char, args[0], username);
    if (result.success) {
      await this._charMgr.sync(username, displayName);
      this._twitch.reply(channel, username, this._t('levelup_applied', {
        user: displayName, stat: result.stat.toUpperCase(), value: result.value,
      }));
    } else if (result.error === 'invalid_stat') {
      this._twitch.reply(channel, username, this._t('levelup_invalid_stat', { user: displayName }));
    } else {
      this._twitch.reply(channel, username, this._t('levelup_no_points', { user: displayName }));
    }
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
