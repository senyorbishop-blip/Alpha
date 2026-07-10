'use strict';

/**
 * chat-bridge/src/quests.js — idle quests for chat arena characters.
 *
 * Chatters send their character off on a timed quest (!quest <n>); while away
 * the character is BUSY (no duels, no item use). On return they earn XP and
 * gold, rarely a basic-tier gear drop, sometimes an injury — and hard quests
 * carry a real (config-visible) death chance that feeds the permadeath flow.
 *
 * ISOLATION GUARANTEE (same contract as arena.js / arenaProgression.js):
 *   - Never touches campaign tokens, inventory, combat, or DM state.
 *   - Persists only through the chat_participant_* arena endpoints.
 *
 * TIMERS ARE SERVER-SIDE. The server stamps quest start/end with ITS clock
 * (chat_participant_quest_start) and only it decides when a quest is complete
 * (chat_participant_quest_resolve). Local setTimeout handles here exist purely
 * so returns get announced promptly; if the bridge restarts, resume() re-arms
 * them from the server's chat_bridge_active_quests list, and every progression
 * command also lazily resolves overdue quests.
 *
 * All tuning lives in config/quests.json (merged over DEFAULT_QUEST_CONFIG):
 * quest definitions, durations, reward ranges, drop/injury/death odds, flavor
 * text pools (themeable per campaign), quest cooldown, and the permadeath
 * legacy bonus.
 */

const { xpForNextLevel } = require('./arenaProgression');

/** Render a chat mention — never produces '@@' even if the value has an '@'. */
function mention(name) { return `@${String(name ?? '').replace(/^@+/, '')}`; }

const DEFAULT_QUEST_CONFIG = {
  // Duration/risk tuned for stream pacing: short / medium / long.
  quests: [
    {
      id: 'rat_cellar',
      name: 'Rats in the Cellar',
      risk: 'easy',
      duration_minutes: 15,
      xp: [15, 30],
      gold: [8, 18],
      drop_chance: 0.08,
      injury_chance: 0.10,
      death_chance: 0,
      flavor: {
        depart: ['The tavernkeeper hands over a lantern and a broom.'],
        success: ['The cellar is quiet again — mostly.'],
        injury: ['One of the rats was NOT a rat.'],
        death: [],
      },
    },
    {
      id: 'goblin_warrens',
      name: 'The Goblin Warrens',
      risk: 'medium',
      duration_minutes: 45,
      xp: [35, 65],
      gold: [20, 40],
      drop_chance: 0.15,
      injury_chance: 0.25,
      death_chance: 0,
      flavor: {
        depart: ['Goblin drums echo from the hills…'],
        success: ['The warrens fall silent behind them.'],
        injury: ['Goblins fight dirty. Very dirty.'],
        death: [],
      },
    },
    {
      id: 'sunken_crypt',
      name: 'The Sunken Crypt',
      risk: 'hard',
      duration_minutes: 120,
      xp: [80, 140],
      gold: [50, 90],
      drop_chance: 0.25,
      injury_chance: 0.35,
      death_chance: 0.07,
      flavor: {
        depart: ['Locals say nobody who enters the crypt at night returns.'],
        success: ['Dripping wet and grinning, they climb out of the crypt.'],
        injury: ['Something in the dark got a piece of them.'],
        death: [
          'The crypt claims another name for its walls.',
          'Only their lantern was found, still burning, at the water’s edge.',
        ],
      },
    },
  ],

  // Quest gear drops are BASIC tier only — commons with the occasional
  // uncommon. The good stuff (rare/epic) is shop-exclusive. Weighted.
  drops: [
    { id: 'rusty_blade',        name: 'Rusty Blade',         slot: 'weapon',  rarity: 'common',   bonuses: { atk: 1 }, weight: 20 },
    { id: 'padded_vest',        name: 'Padded Vest',         slot: 'armor',   rarity: 'common',   bonuses: { ac: 1 },  weight: 20 },
    { id: 'rusty_buckler',      name: 'Rusty Buckler',       slot: 'armor',   rarity: 'common',   bonuses: { ac: 1 },  weight: 14 },
    { id: 'lucky_coin',         name: 'Lucky Coin',          slot: 'trinket', rarity: 'common',   bonuses: { hp: 3 },  weight: 14 },
    { id: 'iron_ring',          name: 'Iron Ring',           slot: 'trinket', rarity: 'common',   bonuses: { ac: 1 },  weight: 14 },
    { id: 'worn_steel_sword',   name: 'Worn Steel Sword',    slot: 'weapon',  rarity: 'uncommon', bonuses: { atk: 2 }, weight: 6 },
    { id: 'patched_chain_shirt', name: 'Patched Chain Shirt', slot: 'armor',  rarity: 'uncommon', bonuses: { ac: 2 },  weight: 6 },
  ],

  // Per-user cooldown between quests (applies from the moment of return).
  quest_cooldown_minutes: 10,

  // Injuries: on a bad-but-survivable roll the hero returns at reduced arena
  // HP — a random deficit between these fractions of their max HP.
  injury: { min_fraction: 0.25, max_fraction: 0.5 },

  // Auto-drink a held Healing Potion on a death-blow in duels (DM toggle).
  auto_potion: false,

  // Permadeath legacy bonus (default OFF = pure permadeath):
  //   gold_percent — reborn characters inherit this % of the dead one's gold
  //   heirloom     — reborn characters keep the dead one's equipped weapon
  //                  (or first equipped item)
  legacy: { gold_percent: 0, heirloom: false },
};

/** Deep-ish merge: user config overrides defaults; quests/drops arrays replace. */
function mergeQuestConfig(defaults, overrides) {
  if (!overrides || typeof overrides !== 'object') return { ...defaults };
  const out = { ...defaults, ...overrides };
  out.injury = { ...defaults.injury, ...(overrides.injury || {}) };
  out.legacy = { ...defaults.legacy, ...(overrides.legacy || {}) };
  if (!Array.isArray(out.quests) || !out.quests.length) out.quests = defaults.quests;
  if (!Array.isArray(out.drops) || !out.drops.length) out.drops = defaults.drops;
  return out;
}

function formatDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return rem ? `${h}h${rem}m` : `${h}h`;
}

function pick(rand, pool) {
  if (!Array.isArray(pool) || !pool.length) return '';
  return pool[Math.floor(rand() * pool.length)] || '';
}

function randInt(rand, [lo, hi]) {
  const a = Math.min(lo ?? 0, hi ?? 0);
  const b = Math.max(lo ?? 0, hi ?? 0);
  return a + Math.floor(rand() * (b - a + 1));
}

class QuestManager {
  /**
   * @param {object} opts
   * @param {object} opts.gameClient   — GameClient (send(type, payload))
   * @param {object} opts.twitchClient — { say, reply }
   * @param {object} opts.progression  — ArenaProgression (sheet cache + sync)
   * @param {object} [opts.config]     — config/quests.json contents
   * @param {string} [opts.channel]    — channel for proactive announcements
   * @param {object} [opts.logger]
   * @param {function} [opts.rand]     — RNG override for tests
   */
  constructor({ gameClient, twitchClient, progression, config, channel, logger, rand }) {
    this._game = gameClient;
    this._twitch = twitchClient;
    this._progression = progression;
    this._logger = logger || console;
    this._config = mergeQuestConfig(DEFAULT_QUEST_CONFIG, config);
    this._channel = channel || null;
    this._rand = rand || Math.random;

    this._timers = new Map();      // username → setTimeout handle
    this._resolving = new Set();   // usernames with an in-flight resolve
    this._quiet = false;
    this._queued = [];             // announcements held back in quiet mode
  }

  destroy() {
    for (const t of this._timers.values()) clearTimeout(t);
    this._timers.clear();
  }

  get config() { return this._config; }

  // ── Quiet mode (announcement queue) ─────────────────────────────────────────

  setQuiet(quiet) {
    this._quiet = !!quiet;
    if (!this._quiet && this._queued.length) {
      this._logger.info(`[Quests] quiet mode off — announcing ${this._queued.length} queued returns`);
      const queued = this._queued.splice(0);
      for (const { channel, msg } of queued) this._twitch.say(channel, msg);
    }
  }

  _announce(channel, msg) {
    if (this._quiet) this._queued.push({ channel, msg });
    else this._twitch.say(channel, msg);
  }

  // ── Startup resume (bridge restart mid-quest) ───────────────────────────────

  /** Re-arm return timers for every quest in flight, from the server's list. */
  async resume() {
    try {
      const result = await this._game.send('chat_bridge_active_quests', {});
      const entries = result?.entries || [];
      for (const entry of entries) {
        const username = String(entry.twitch_username || '').toLowerCase();
        if (!username) continue;
        this._schedule(username, Math.max(0, Number(entry.remaining_s) || 0) * 1000);
      }
      if (entries.length) this._logger.info(`[Quests] resumed ${entries.length} in-flight quest(s)`);
    } catch (err) {
      this._logger.error('[Quests] resume error:', err);
    }
  }

  /** Called by ArenaProgression when a loaded sheet carries an active quest. */
  scheduleFromSheet(username, sheet) {
    const q = sheet?.quest;
    if (!q) return;
    this._schedule(username, Math.max(0, (q.local_ends_at || 0) - Date.now()));
  }

  _schedule(username, delayMs) {
    const existing = this._timers.get(username);
    if (existing) clearTimeout(existing);
    // Small buffer so the server clock is definitely past ends_at when asked.
    const timer = setTimeout(() => {
      this._timers.delete(username);
      this._attemptResolve(this._channel, username).catch(err =>
        this._logger.error('[Quests] timer resolve error:', err));
    }, delayMs + 1500);
    if (typeof timer.unref === 'function') timer.unref();
    this._timers.set(username, timer);
  }

  // ── Commands ────────────────────────────────────────────────────────────────

  async handleCommand(channel, username, displayName, action, args) {
    try {
      if (action === 'graveyard') {
        await this._handleGraveyard(channel, username);
        return;
      }
      await this.maybeResolve(channel, username);
      const { sheet, isNew } = await this._progression.getSheet(username);
      if (isNew) this._progression.announceNewCharacter(channel, username, sheet);

      if (sheet.dead) {
        this._twitch.reply(channel, username,
          'You are dead! 💀 Type !stats (or !rebirth) to roll a new hero.');
        return;
      }

      const remainingMs = this._progression.questRemainingMs(sheet);
      if (remainingMs > 0) {
        this._twitch.reply(channel, username,
          `You're off questing — ${sheet.quest.name} (returns in ${formatDuration(remainingMs / 1000)}).`);
        return;
      }

      const arg = (args || []).join(' ').trim();
      if (!arg) {
        this._twitch.reply(channel, username, this.questListText());
        return;
      }

      const quest = this._findQuest(arg);
      if (!quest) {
        this._twitch.reply(channel, username,
          `No such quest. ${this.questListText()}`);
        return;
      }
      await this._startQuest(channel, username, displayName, quest);
    } catch (err) {
      this._logger.error(`[Quests] ${action} error:`, err);
    }
  }

  /** "1) Rats in the Cellar (15m, Easy) 2) … (2h, Hard ☠ 7% death)" — death
   * odds are always visible so a death never feels like cheating. */
  questListText() {
    const list = this._config.quests.map((q, i) => {
      const dur = formatDuration((q.duration_minutes || 0) * 60);
      return `${i + 1}) ${q.name} (${dur}, ${this._riskLabel(q)})`;
    }).join(' ');
    return `Quests: ${list} — !quest <number> to set off.`;
  }

  _riskLabel(q) {
    const risk = String(q.risk || 'easy');
    const riskName = risk.charAt(0).toUpperCase() + risk.slice(1);
    if ((q.death_chance || 0) > 0) {
      return `${riskName} ☠ ${Math.round(q.death_chance * 100)}% death risk`;
    }
    if ((q.injury_chance || 0) > 0.15) return `${riskName} ⚠ injury risk`;
    return riskName;
  }

  _findQuest(arg) {
    const idx = parseInt(arg, 10);
    if (Number.isFinite(idx) && idx >= 1 && idx <= this._config.quests.length) {
      return this._config.quests[idx - 1];
    }
    const query = arg.toLowerCase();
    return this._config.quests.find(q =>
      q.id === query || String(q.name || '').toLowerCase().includes(query)) || null;
  }

  async _startQuest(channel, username, displayName, quest) {
    const durationS = Math.max(30, Math.round((quest.duration_minutes || 1) * 60));
    const cooldownS = Math.max(0, Math.round((this._config.quest_cooldown_minutes || 0) * 60));

    let result = null;
    try {
      result = await this._game.send('chat_participant_quest_start', {
        twitch_username: username,
        display_name: displayName,
        quest: {
          id: quest.id,
          name: quest.name,
          risk: quest.risk,
          duration_s: durationS,
          cooldown_s: cooldownS,
        },
      });
    } catch (err) {
      this._logger.error('[Quests] start error:', err);
    }
    if (!result) return;

    if (result.success) {
      const remainingS = Math.max(0, Number(result.remaining_s) || durationS);
      const sheet = this._progression.peekSheet(username);
      if (sheet) sheet.quest = { ...result.quest, local_ends_at: Date.now() + remainingS * 1000 };
      this._schedule(username, remainingS * 1000);
      const flavor = pick(this._rand, quest.flavor?.depart);
      this._twitch.say(channel,
        `🗺️ ${mention(username)} sets off on ${quest.name} (${this._riskLabel(quest)}) — ` +
        `back in ${formatDuration(remainingS)}.${flavor ? ` ${flavor}` : ''}`);
      return;
    }

    switch (result.error_code) {
      case 'already_questing':
        this._twitch.reply(channel, username,
          `You're already on a quest (returns in ${formatDuration(result.remaining_s || 0)}).`);
        break;
      case 'unresolved_quest':
        // Finished but not yet rolled — resolve now (announces the return).
        await this._attemptResolve(channel, username);
        this._twitch.reply(channel, username, 'Welcome back! Try !quest again.');
        break;
      case 'cooldown':
        this._twitch.reply(channel, username,
          `Your hero is resting after the last quest — ready in ${formatDuration(result.remaining_s || 0)}.`);
        break;
      case 'dead':
        this._twitch.reply(channel, username,
          'You are dead! 💀 Type !stats (or !rebirth) to roll a new hero.');
        break;
      default:
        break; // bridge_paused etc. — stay silent
    }
  }

  // ── Resolution ──────────────────────────────────────────────────────────────

  /** Lazy check used before progression/arena commands: resolves an overdue
   * quest (announcing the return) so no command ever sees stale state. */
  async maybeResolve(channel, username) {
    const sheet = this._progression.peekSheet(username);
    if (!sheet || sheet.dead || !sheet.quest) return;
    if ((sheet.quest.local_ends_at || 0) > Date.now()) return;
    await this._attemptResolve(channel, username);
  }

  /**
   * Ask the server whether the quest is done. The server clock decides:
   * 'active' re-arms the timer, 'complete' rolls the outcome here and
   * announces the return (queued while quiet mode is on).
   */
  async _attemptResolve(channel, username) {
    if (this._resolving.has(username)) return;
    this._resolving.add(username);
    try {
      const ch = channel || this._channel;
      const { sheet } = await this._progression.getSheet(username);

      let result = null;
      try {
        result = await this._game.send('chat_participant_quest_resolve', {
          twitch_username: username,
        });
      } catch (err) {
        this._logger.error('[Quests] resolve error:', err);
      }
      if (!result) {
        this._schedule(username, 30_000); // transport hiccup — retry soon
        return;
      }

      if (result.status === 'active') {
        const remainingS = Math.max(0, Number(result.remaining_s) || 0);
        if (sheet.quest) sheet.quest.local_ends_at = Date.now() + remainingS * 1000;
        this._schedule(username, remainingS * 1000);
        return;
      }

      if (result.status === 'idle') {
        delete sheet.quest;
        return;
      }

      // status === 'complete' — the outcome is rolled bridge-side from config.
      const quest = result.quest || {};
      delete sheet.quest;
      if (result.cooldown_s) sheet.quest_cooldown_local = Date.now() + result.cooldown_s * 1000;

      const def = this._config.quests.find(q => q.id === quest.id)
        || { xp: [10, 20], gold: [5, 10], drop_chance: 0, injury_chance: 0, death_chance: 0, flavor: {} };

      if ((def.death_chance || 0) > 0 && this._rand() < def.death_chance) {
        await this._handleQuestDeath(ch, username, sheet, quest, def);
        return;
      }

      const xp = randInt(this._rand, def.xp || [0, 0]);
      const gold = randInt(this._rand, def.gold || [0, 0]);
      sheet.xp += xp;
      sheet.gold += gold;

      let drop = null;
      if ((def.drop_chance || 0) > 0 && this._rand() < def.drop_chance) {
        drop = this._rollDrop(sheet);
        if (drop) {
          sheet.items.push({
            id: drop.id, name: drop.name, slot: drop.slot, rarity: drop.rarity,
            bonuses: { ...(drop.bonuses || {}) }, effect: '', cosmetic: '',
          });
          if (!sheet.equipped[drop.slot]) sheet.equipped[drop.slot] = drop.id;
        }
      }

      let injuryNote = '';
      let flavorPool = def.flavor?.success;
      if ((def.injury_chance || 0) > 0 && this._rand() < def.injury_chance) {
        const combat = this._progression.combatStats(sheet);
        const { min_fraction = 0.25, max_fraction = 0.5 } = this._config.injury || {};
        const frac = min_fraction + this._rand() * Math.max(0, max_fraction - min_fraction);
        const amount = Math.max(1, Math.round(combat.maxHp * frac));
        sheet.injuries = Math.min(combat.maxHp - 1, (sheet.injuries || 0) + amount);
        injuryNote = ` They return wounded (-${amount} HP until healed — !heal).`;
        flavorPool = def.flavor?.injury || flavorPool;
      }

      await this._progression.sync(username);

      const flavor = pick(this._rand, flavorPool);
      const dropNote = drop ? `, and ${drop.name}!` : '!';
      const levelNote = sheet.xp >= xpForNextLevel(sheet.level) ? ' Ready to !levelup!' : '';
      this._announce(ch,
        `⚔️ ${mention(username)} returns from ${quest.name || 'a quest'}: ` +
        `+${xp} XP, ${gold}g${dropNote}${injuryNote}${flavor ? ` ${flavor}` : ''}${levelNote}`);
    } finally {
      this._resolving.delete(username);
    }
  }

  /** Weighted basic-tier drop; items the hero already owns are excluded. */
  _rollDrop(sheet) {
    const owned = new Set((sheet.items || []).map(i => i.id));
    const pool = this._config.drops.filter(d => d && d.id && !owned.has(d.id));
    if (!pool.length) return null;
    const total = pool.reduce((sum, d) => sum + Math.max(0, Number(d.weight) || 1), 0);
    let r = this._rand() * total;
    for (const d of pool) {
      r -= Math.max(0, Number(d.weight) || 1);
      if (r <= 0) return d;
    }
    return pool[pool.length - 1];
  }

  // ── Permadeath ──────────────────────────────────────────────────────────────

  async _handleQuestDeath(channel, username, sheet, quest, def) {
    const cause = `fell on the quest '${quest.name || 'unknown'}'`;

    // Legacy bonus (config, default off): a sliver of gold and/or one heirloom.
    const legacy = this._config.legacy || {};
    const goldPct = Math.max(0, Math.min(100, Number(legacy.gold_percent) || 0));
    const legacyGold = goldPct > 0 ? Math.floor((sheet.gold || 0) * goldPct / 100) : 0;
    let heirloom = null;
    if (legacy.heirloom) {
      const equippedIds = ['weapon', 'armor', 'trinket']
        .map(slot => sheet.equipped?.[slot]).filter(Boolean);
      const item = (sheet.items || []).find(i => equippedIds.includes(i.id) || equippedIds.includes(i.name));
      if (item) heirloom = { ...item };
    }

    let result = null;
    try {
      result = await this._game.send('chat_participant_arena_death', {
        twitch_username: username,
        cause,
        legacy_gold: legacyGold,
        heirloom,
      });
    } catch (err) {
      this._logger.error('[Quests] death sync error:', err);
    }
    if (!result?.success) {
      // Server refused (e.g. no character) — fail towards mercy: treat as a
      // narrow escape rather than losing state.
      this._logger.warn('[Quests] death not recorded — sparing the character');
      await this._progression.sync(username);
      this._announce(channel,
        `⚔️ ${mention(username)} barely escapes ${quest.name || 'the quest'} with their life!`);
      return;
    }

    const cls = sheet.class || 'hero';
    const level = sheet.level || 1;
    // Mirror the server's tombstone in the local cache.
    this._progression._sheets.set(username, {
      dead: true,
      death_cause: cause,
      died_at: Date.now(),
      legacy_gold: legacyGold,
      heirloom,
    });

    const flavor = pick(this._rand, def.flavor?.death);
    this._announce(channel,
      `☠️ TRAGEDY! ${mention(username)}'s ${cls} (level ${level}) has FALLEN on ${quest.name || 'a quest'}.` +
      `${flavor ? ` ${flavor}` : ''} Their deeds enter the !graveyard. Type !stats to be reborn.`);
  }

  // ── Graveyard ───────────────────────────────────────────────────────────────

  async _handleGraveyard(channel, username) {
    let entries = [];
    try {
      const result = await this._game.send('chat_participant_graveyard', { limit: 5 });
      entries = result?.entries || [];
    } catch (err) {
      this._logger.error('[Quests] graveyard error:', err);
    }
    if (!entries.length) {
      this._twitch.reply(channel, username, 'The graveyard is empty. No heroes have fallen… yet.');
      return;
    }
    const list = entries.map(e => {
      const who = e.character_name || e.display_name || e.twitch_username;
      const record = e.wins || e.losses ? `, ${e.wins}W/${e.losses}L` : '';
      return `${who} the ${e.class} L${e.level}${record} (${e.cause})`;
    }).join(' | ');
    this._twitch.say(channel, `🪦 Fallen heroes: ${list}`);
  }
}

module.exports = { QuestManager, DEFAULT_QUEST_CONFIG, mergeQuestConfig, formatDuration };
