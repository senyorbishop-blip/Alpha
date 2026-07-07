'use strict';

/**
 * chat-bridge/src/arena.js — Isolated chatter-vs-chatter PvP arena.
 *
 * ISOLATION GUARANTEE:
 *   - Never modifies campaign tokens, inventory, combat, or DM state.
 *   - Only contacts the game server via the injected callbacks
 *     (onStatsUpdate / onDisplayEvent / onCharacterSync — arena-only types).
 *   - All state is internal; an arena crash cannot propagate to the main game.
 *
 * Supports:
 *   !duel <name> [wager] — challenge another joined chatter (60s expiry),
 *                          optionally staking arena gold (winner takes pot)
 *   !accept              — accept pending challenge (matches the stake)
 *   !decline             — decline pending challenge (stake refunded)
 *
 * Fighters use their arena characters (from the injected CharacterManager):
 * derived HP / AC / attack mod / damage die, crits on natural 20 (double
 * damage dice), underdog attack bonus, and legendary second_wind survival.
 * Falls back to legacy flat stats when no character manager is available.
 *
 * Auto-resolve mode (default): 3–5 dramatic messages over ~20s.
 * Interactive mode (config.interactiveMode): !attack / !defend / !flee.
 */

const { describeBonuses } = require('./arenaProgression');

const ARENA_HP       = 20;
const ARENA_AC       = 12;  // hit on d20 >= 12
const ARENA_DMG_DIE  = 6;   // 1d6 damage
const CHALLENGE_TTL  = 60 * 1000;  // ms
const TURN_TIMER_MS  = 15 * 1000;
const MSG_INTERVAL   = 5 * 1000;  // ms between dramatic messages

const ARENA_DEFAULT_RESPONSES = {
  wager_disabled:     'Wagers are disabled in this arena.',
  wager_invalid:      'Wager must be a positive whole number of gold.',
  wager_too_high:     'Max wager is {max}g.',
  wager_insufficient: "{user}, you only have {gold}g — you can't stake {wager}g.",
};

/** Trim a chat message to stay well under Twitch's 500-char limit. */
function clip(msg, max = 440) {
  return msg.length > max ? `${msg.slice(0, max - 1)}…` : msg;
}

class Arena {
  /**
   * @param {object} opts
   * @param {object} opts.twitchClient       — { say(channel, msg), reply(channel, username, msg) }
   * @param {object} [opts.config={}]        — { duel_cooldown_seconds, interactiveMode, wagers_enabled, max_wager, message_interval_ms }
   * @param {object} [opts.logger]           — { info, warn, error }
   * @param {function} [opts.onStatsUpdate]  — (username, {wins, losses, arena_gold}) → Promise<void>
   * @param {function} [opts.onDisplayEvent] — (eventPayload) → void  (for OBS overlay)
   * @param {object} [opts.characterManager] — CharacterManager instance (arena progression)
   * @param {function} [opts.onCharacterSync]— (username, displayName) → Promise<void>
   * @param {object} [opts.responses]        — response template overrides (wager errors)
   * @param {function} [opts.rng]            — () → [0,1), injectable for tests
   */
  constructor({ twitchClient, config = {}, logger, onStatsUpdate, onDisplayEvent, characterManager, onCharacterSync, responses, rng }) {
    this._twitch      = twitchClient;
    this._config      = config;
    this._logger      = logger || console;
    this._onStats     = onStatsUpdate  || (() => Promise.resolve());
    this._onDisplay   = onDisplayEvent || (() => {});
    this._charMgr     = characterManager || null;
    this._onCharSync  = onCharacterSync
      || (this._charMgr ? (u, d) => this._charMgr.sync(u, d) : () => Promise.resolve());
    this._responses   = { ...ARENA_DEFAULT_RESPONSES, ...(responses ?? {}) };
    this._rng         = rng || Math.random;

    this._enabled     = true;
    this._quietMode   = false;

    this._cooldownMs  = (config.duel_cooldown_seconds ?? 120) * 1000;
    this._msgInterval = config.message_interval_ms ?? MSG_INTERVAL;
    this._wagersEnabled = config.wagers_enabled ?? false;
    this._maxWager    = config.max_wager ?? 100;

    // State maps
    this._challenges  = new Map();  // challenged_username → { challenger, challenged, wager, expiresAt, channel }
    this._activeDuels = new Map();  // username → duelState (one entry per participant)
    this._cooldowns   = new Map();  // username → lastDuelEndedAt
    this._queue       = [];         // [{channel, challenger, challenged, ...}] for quiet mode
    this._interStats  = new Map();  // username → { wins, losses, arena_gold } (legacy fallback)

    // Cleanup expired challenges every 10s
    this._cleanupTimer = setInterval(() => this._cleanupExpiredChallenges(), 10_000);
    if (typeof this._cleanupTimer.unref === 'function') this._cleanupTimer.unref();
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  setEnabled(enabled) {
    this._enabled = !!enabled;
    if (!enabled) this._logger.info('[Arena] disabled by DM');
  }

  setQuietMode(quiet) {
    this._quietMode = !!quiet;
    if (!quiet && this._queue.length > 0) {
      this._logger.info(`[Arena] quiet mode off — processing ${this._queue.length} queued duels`);
      const queued = this._queue.splice(0);
      for (const entry of queued) {
        this._startDuel(entry).catch(err => this._logger.error('[Arena] queued duel error:', err));
      }
    }
  }

  /**
   * Main entry point called by commandParser for arena-related commands.
   * @param {string} channel
   * @param {string} username       — lowercase Twitch username
   * @param {string} displayName
   * @param {string} action         — 'duel'|'accept'|'decline'|'attack'|'defend'|'flee'
   * @param {string[]} args
   */
  handleCommand(channel, username, displayName, action, args) {
    try {
      let p;
      switch (action) {
        case 'duel':    p = this._handleDuel(channel, username, displayName, args); break;
        case 'accept':  p = this._handleAccept(channel, username, displayName); break;
        case 'decline': this._handleDecline(channel, username, displayName); break;
        case 'attack':
        case 'defend':
        case 'flee':
          if (this._config.interactiveMode) this._handleInteractiveAction(channel, username, displayName, action);
          break;
        default: break;
      }
      if (p && typeof p.catch === 'function') {
        p.catch(err => this._logger.error('[Arena] handleCommand error:', err));
      }
    } catch (err) {
      this._logger.error('[Arena] handleCommand error:', err);
    }
  }

  // ── Templates / dice ─────────────────────────────────────────────────────────

  _t(key, vars = {}) {
    let tpl = this._responses[key] ?? key;
    for (const [k, v] of Object.entries(vars)) {
      tpl = tpl.replaceAll(`{${k}}`, String(v ?? ''));
    }
    return tpl;
  }

  _d(sides) { return Math.floor(this._rng() * sides) + 1; }

  // ── Duel lifecycle ───────────────────────────────────────────────────────────

  async _handleDuel(channel, username, displayName, args) {
    if (!this._enabled) {
      this._twitch.reply(channel, username, 'The arena is closed right now.');
      return;
    }

    const targetName = (args[0] || '').toLowerCase().trim();
    if (!targetName) {
      this._twitch.reply(channel, username, 'Usage: !duel <username> [wager]');
      return;
    }
    if (targetName === username) {
      this._twitch.reply(channel, username, "You can't challenge yourself!");
      return;
    }

    // Cooldown check
    const lastEnd = this._cooldowns.get(username) || 0;
    const rem = Math.ceil((this._cooldownMs - (Date.now() - lastEnd)) / 1000);
    if (rem > 0) {
      this._twitch.reply(channel, username, `Arena cooldown — try again in ${rem}s.`);
      return;
    }

    // Already in a duel?
    if (this._activeDuels.has(username)) {
      this._twitch.reply(channel, username, "You're already in a duel!");
      return;
    }

    // Already challenged someone?
    for (const [, ch] of this._challenges) {
      if (ch.challenger === username) {
        this._twitch.reply(channel, username, "You already have a pending challenge.");
        return;
      }
    }

    // Optional wager
    let wager = 0;
    if (args[1] !== undefined) {
      if (!this._wagersEnabled || !this._charMgr) {
        this._twitch.reply(channel, username, this._t('wager_disabled'));
        return;
      }
      if (!/^\d+$/.test(String(args[1])) || parseInt(args[1], 10) <= 0) {
        this._twitch.reply(channel, username, this._t('wager_invalid'));
        return;
      }
      wager = parseInt(args[1], 10);
      if (wager > this._maxWager) {
        this._twitch.reply(channel, username, this._t('wager_too_high', { max: this._maxWager }));
        return;
      }
      const { character } = await this._charMgr.getOrCreate(username, displayName);
      if (character.gold < wager) {
        this._twitch.reply(channel, username, this._t('wager_insufficient', {
          user: displayName, gold: character.gold, wager,
        }));
        return;
      }
      // Escrow the challenger's stake now; refunded on decline/expiry/draw.
      character.gold -= wager;
      this._syncCharacter(username, displayName);
    }

    const expiresAt = Date.now() + CHALLENGE_TTL;
    this._challenges.set(targetName, {
      challenger: username, challengerDisplay: displayName,
      challenged: targetName, channel, expiresAt, wager,
    });

    const stake = wager > 0 ? ` for a ${wager}g stake` : '';
    this._twitch.say(channel, `@${targetName} ${displayName} challenges you to a duel${stake}! Type !accept or !decline in ${Math.round(CHALLENGE_TTL / 1000)}s.`);
  }

  async _handleAccept(channel, username, displayName) {
    const challenge = this._challenges.get(username);
    if (!challenge || Date.now() > challenge.expiresAt) {
      this._twitch.reply(channel, username, "No pending challenge for you.");
      return;
    }

    const wager = challenge.wager || 0;
    let challengedChar = null;
    if (wager > 0 && this._charMgr) {
      const { character } = await this._charMgr.getOrCreate(username, displayName);
      challengedChar = character;
      if (character.gold < wager) {
        this._twitch.reply(channel, username, this._t('wager_insufficient', {
          user: displayName, gold: character.gold, wager,
        }));
        return; // challenge stays pending until expiry (challenger refunded then)
      }
    }

    this._challenges.delete(username);

    // Escrow the challenged party's stake.
    if (wager > 0 && challengedChar) {
      challengedChar.gold -= wager;
      this._syncCharacter(username, displayName);
    }

    const entry = {
      channel,
      challenger: challenge.challenger,
      challengerDisplay: challenge.challengerDisplay,
      challenged: username,
      challengedDisplay: displayName,
      wager,
    };

    if (this._quietMode) {
      this._queue.push(entry);
      this._twitch.say(channel, `${displayName} accepted! Duel queued — arena is in downtime mode.`);
      return;
    }

    if (this._activeDuels.has(challenge.challenger) || this._activeDuels.has(username)) {
      this._twitch.reply(channel, username, "One of you is already in a duel.");
      if (wager > 0) this._refundStake(username, displayName, wager);
      if (wager > 0) this._refundStake(challenge.challenger, challenge.challengerDisplay, wager);
      return;
    }

    await this._startDuel(entry);
  }

  _handleDecline(channel, username, displayName) {
    const challenge = this._challenges.get(username);
    if (!challenge) {
      this._twitch.reply(channel, username, "No pending challenge for you.");
      return;
    }
    this._challenges.delete(username);
    if (challenge.wager > 0) {
      this._refundStake(challenge.challenger, challenge.challengerDisplay, challenge.wager);
    }
    this._twitch.say(channel, `${displayName} declined the duel challenge from @${challenge.challenger}.`);
  }

  /** Refund an escrowed wager stake to a cached character. */
  _refundStake(username, displayName, amount) {
    if (!this._charMgr || amount <= 0) return;
    const char = this._charMgr.getCached(username);
    if (!char) return;
    char.gold += amount;
    this._syncCharacter(username, displayName);
  }

  _syncCharacter(username, displayName) {
    Promise.resolve(this._onCharSync(username, displayName)).catch(err =>
      this._logger.error('[Arena] character sync error:', err)
    );
  }

  // ── Fighter construction ─────────────────────────────────────────────────────

  /** Build a duel participant from an arena character (or legacy flat stats). */
  _makeFighter(username, displayName, char) {
    if (char && this._charMgr) {
      const mgr = this._charMgr;
      const info = mgr.classInfo(char);
      const maxHp = mgr.maxHp(char);
      return {
        username,
        display: displayName || username,
        name: `${displayName || username} the ${char.class} ${info.emoji ?? ''}`.trim(),
        char,
        hp: maxHp,
        maxHp,
        ac: mgr.armorClass(char),
        atk: mgr.attackMod(char),
        dmgDie: mgr.damageDie(char),
        secondWind: mgr.hasSecondWind(char),
        underdogBonus: 0,
      };
    }
    return {
      username,
      display: displayName || username,
      name: displayName || username,
      char: null,
      hp: ARENA_HP,
      maxHp: ARENA_HP,
      ac: ARENA_AC,
      atk: 0,
      dmgDie: ARENA_DMG_DIE,
      secondWind: false,
      underdogBonus: 0,
    };
  }

  // ── Duel start ───────────────────────────────────────────────────────────────

  async _startDuel(entry) {
    const { channel, challenger, challenged, wager = 0 } = entry;

    let cChar = null;
    let dChar = null;
    if (this._charMgr) {
      try {
        const cRes = await this._charMgr.getOrCreate(challenger, entry.challengerDisplay || challenger);
        const dRes = await this._charMgr.getOrCreate(challenged, entry.challengedDisplay || challenged);
        cChar = cRes.character;
        dChar = dRes.character;
        if (cRes.created) this._twitch.say(channel, clip(this._charMgr.creationAnnouncement(cChar, entry.challengerDisplay || challenger)));
        if (dRes.created) this._twitch.say(channel, clip(this._charMgr.creationAnnouncement(dChar, entry.challengedDisplay || challenged)));
      } catch (err) {
        this._logger.error('[Arena] character load failed — using fallback stats:', err);
      }
    }

    const fighterC = this._makeFighter(challenger, entry.challengerDisplay, cChar);
    const fighterD = this._makeFighter(challenged, entry.challengedDisplay, dChar);

    // Underdog bonus: the lower-level fighter gets extra attack + bonus XP on win.
    let underdogUser = null;
    if (cChar && dChar && this._charMgr) {
      if (this._charMgr.isUnderdog(cChar, dChar)) underdogUser = challenger;
      else if (this._charMgr.isUnderdog(dChar, cChar)) underdogUser = challenged;
      if (underdogUser) {
        const bonus = this._charMgr.underdogAttackBonus();
        (underdogUser === challenger ? fighterC : fighterD).underdogBonus = bonus;
      }
    }

    const state = {
      channel,
      participants: { [challenger]: fighterC, [challenged]: fighterD },
      challenger,
      challenged,
      wager,
      pot: wager * 2,
      underdogUser,
      round: 0,
      finished: false,
      interactive: !!this._config.interactiveMode,
    };

    this._activeDuels.set(challenger, state);
    this._activeDuels.set(challenged, state);

    const vs = `${fighterC.name} vs ${fighterD.name}`;
    const potNote = state.pot > 0 ? ` ${state.pot}g pot on the line!` : '';
    const underdogNote = underdogUser
      ? ` ${state.participants[underdogUser].name} fights as the underdog!`
      : '';
    this._twitch.say(channel, clip(`⚔️ ARENA DUEL: ${vs} — Fight begins!${potNote}${underdogNote}`));

    this._onDisplay({
      event_type: 'duel_start',
      challenger,
      opponent: challenged,
      challenger_hp: fighterC.hp,
      opponent_hp: fighterD.hp,
      challenger_max_hp: fighterC.maxHp,
      opponent_max_hp: fighterD.maxHp,
      round: 0,
    });

    if (state.interactive) {
      this._runInteractiveRound(state, challenger, challenged);
    } else {
      this._runAutoResolve(state, challenger, challenged);
    }
  }

  // ── Attack resolution (shared) ───────────────────────────────────────────────

  /**
   * Resolve one attack. Returns { roll, hit, crit, dmg, secondWind, message }.
   * @param {number} acBonus — situational AC bonus for the defender (defend action)
   * @param {number} dmgPenalty — flat damage reduction (defend action)
   */
  _resolveAttack(attacker, defender, acBonus = 0, dmgPenalty = 0) {
    const roll = this._d(20);
    const crit = roll === 20;
    const effectiveAC = defender.ac + acBonus;
    const hit = crit || (roll + attacker.atk + attacker.underdogBonus >= effectiveAC);

    if (!hit) return { roll, hit, crit, dmg: 0, secondWind: false };

    // Crit on natural 20 → double damage dice.
    let dmg = this._d(attacker.dmgDie) + (crit ? this._d(attacker.dmgDie) : 0);
    dmg = Math.max(1, dmg - dmgPenalty);

    let secondWind = false;
    if (defender.hp - dmg <= 0 && defender.secondWind) {
      defender.secondWind = false;
      defender.hp = 1;
      secondWind = true;
    } else {
      defender.hp = Math.max(0, defender.hp - dmg);
    }
    return { roll, hit, crit, dmg, secondWind };
  }

  // ── Auto-resolve fight ───────────────────────────────────────────────────────

  _runAutoResolve(state, challenger, challenged) {
    const p = state.participants;
    const messages = [];

    // Simulate 3–5 dramatic rounds
    const maxRounds = 3 + Math.floor(this._rng() * 3);
    let attacker = challenger;
    let defender = challenged;

    for (let r = 0; r < maxRounds && !state.finished; r++) {
      const atk = p[attacker];
      const def = p[defender];
      const res = this._resolveAttack(atk, def);

      if (res.hit) {
        const flavour = res.crit ? 'CRITICAL HIT — double dice' : res.roll >= 14 ? 'solid blow' : 'hit';
        let line = `Round ${r + 1}: ${atk.name} rolled ${res.roll} — ${flavour}! ${def.name} takes ${res.dmg} damage. (${def.hp}/${def.maxHp} HP)`;
        if (res.secondWind) {
          line += ` ✨ ${def.name} refuses to fall — second wind at 1 HP!`;
        }
        messages.push(line);
        if (def.hp <= 0) {
          messages.push(`💀 ${def.name} has been defeated! ${atk.name} wins the duel!`);
          state.finished = true;
          state.winner = attacker;
          state.loser = defender;
          break;
        }
      } else {
        const dodge = res.roll <= 3 ? 'a wild swing' : 'a miss';
        messages.push(`Round ${r + 1}: ${atk.name} rolled ${res.roll} — ${dodge}! ${def.name} deftly dodges.`);
      }

      // Swap attacker/defender
      [attacker, defender] = [defender, attacker];
    }

    if (!state.finished) {
      // Tie — higher HP ratio wins (fighters can have different max HP)
      const ratioA = p[challenger].hp / p[challenger].maxHp;
      const ratioB = p[challenged].hp / p[challenged].maxHp;
      if (ratioA !== ratioB) {
        state.winner = ratioA > ratioB ? challenger : challenged;
        state.loser  = ratioA > ratioB ? challenged : challenger;
        const w = p[state.winner];
        messages.push(`Time's up! ${w.name} wins by HP advantage! (${w.hp}/${w.maxHp} HP left)`);
      } else {
        messages.push(`Time's up! It's a DRAW — both fighters remain standing!`);
        state.winner = null;
        state.loser  = null;
      }
      state.finished = true;
    }

    // Send messages with delay, emitting display events alongside each round
    messages.forEach((msg, i) => {
      const timer = setTimeout(() => {
        this._twitch.say(state.channel, clip(msg));
        this._onDisplay({
          event_type: 'round',
          challenger,
          opponent: challenged,
          challenger_hp: p[challenger].hp,
          opponent_hp: p[challenged].hp,
          challenger_max_hp: p[challenger].maxHp,
          opponent_max_hp: p[challenged].maxHp,
          round: i + 1,
          message: msg,
        });
        if (i === messages.length - 1) {
          this._finishDuel(state, challenger, challenged).catch(err =>
            this._logger.error('[Arena] finish duel error:', err)
          );
        }
      }, i * this._msgInterval);
      if (typeof timer.unref === 'function') timer.unref();
    });
  }

  // ── Interactive mode ─────────────────────────────────────────────────────────

  _runInteractiveRound(state, attacker, defender) {
    if (state.finished) return;

    this._twitch.say(state.channel,
      `@${attacker} it's your turn! Type !attack, !defend, or !flee (${Math.round(TURN_TIMER_MS / 1000)}s)`
    );

    state.currentAttacker = attacker;
    state.currentDefender = defender;
    state.turnTimer = setTimeout(() => {
      // Auto-attack on timeout
      this._resolveInteractiveAction(state, attacker, defender, 'attack');
    }, TURN_TIMER_MS);
    if (typeof state.turnTimer.unref === 'function') state.turnTimer.unref();
  }

  _handleInteractiveAction(channel, username, displayName, action) {
    const state = this._activeDuels.get(username);
    if (!state || state.finished) return;
    if (state.currentAttacker !== username) return;  // not their turn

    clearTimeout(state.turnTimer);
    const { currentAttacker, currentDefender } = state;
    this._resolveInteractiveAction(state, currentAttacker, currentDefender, action);
  }

  _resolveInteractiveAction(state, attacker, defender, action) {
    if (state.finished) return;

    const p = state.participants;
    const atk = p[attacker];
    const def = p[defender];

    if (action === 'flee') {
      state.winner = defender;
      state.loser  = attacker;
      state.finished = true;
      this._twitch.say(state.channel, `${atk.name} fled the arena! ${def.name} wins by default!`);
      this._finishDuel(state, state.challenger, state.challenged).catch(err =>
        this._logger.error('[Arena] finish duel error:', err)
      );
      return;
    }

    state.round = (state.round || 0) + 1;
    let msg;

    if (action === 'defend') {
      // The defender counter-attacks into a braced stance (AC+4, damage -2).
      const res = this._resolveAttack(def, atk, 4, 2);
      msg = `${atk.name} braces for impact (AC+4 this round). ${def.name} attacks — rolled ${res.roll}...`;
      if (res.hit) {
        msg += ` still hits for ${res.dmg} damage. (${atk.hp}/${atk.maxHp} HP)`;
        if (res.secondWind) msg += ` ✨ Second wind — ${atk.name} hangs on at 1 HP!`;
      } else {
        msg += ` blocked!`;
      }
    } else {
      // attack
      const res = this._resolveAttack(atk, def);
      if (res.hit) {
        const critNote = res.crit ? ' CRITICAL!' : '';
        msg = `${atk.name} attacks (rolled ${res.roll})${critNote} — hits for ${res.dmg} damage! ${def.name}: ${def.hp}/${def.maxHp} HP`;
        if (res.secondWind) msg += ` ✨ Second wind — ${def.name} hangs on at 1 HP!`;
      } else {
        msg = `${atk.name} attacks (rolled ${res.roll}) — miss!`;
      }
    }

    this._twitch.say(state.channel, clip(msg));

    // Check for KO
    const loser = Object.values(p).find(pp => pp.hp <= 0);
    if (loser) {
      const winner = Object.values(p).find(pp => pp.username !== loser.username);
      state.winner = winner?.username ?? null;
      state.loser  = loser.username;
      state.finished = true;
      this._twitch.say(state.channel, `💀 ${loser.name} is down! ${winner?.name ?? '?'} wins!`);
      this._finishDuel(state, state.challenger, state.challenged).catch(err =>
        this._logger.error('[Arena] finish duel error:', err)
      );
    } else {
      // Next round — swap attacker/defender
      this._runInteractiveRound(state, defender, attacker);
    }
  }

  // ── Duel end ─────────────────────────────────────────────────────────────────

  async _finishDuel(state, challenger, challenged) {
    this._activeDuels.delete(challenger);
    this._activeDuels.delete(challenged);

    const now = Date.now();
    this._cooldowns.set(challenger, now);
    this._cooldowns.set(challenged, now);

    const p = state.participants;
    this._onDisplay({
      event_type:        'duel_end',
      challenger,
      opponent:          challenged,
      winner:            state.winner || '',
      challenger_hp:     p[challenger]?.hp ?? 0,
      opponent_hp:       p[challenged]?.hp ?? 0,
      challenger_max_hp: p[challenger]?.maxHp ?? ARENA_HP,
      opponent_max_hp:   p[challenged]?.maxHp ?? ARENA_HP,
    });

    try {
      if (state.winner && state.loser) {
        await this._awardDuelRewards(state);
      } else if (state.pot > 0) {
        // Draw — refund both stakes.
        this._refundStake(challenger, p[challenger]?.display, state.wager);
        this._refundStake(challenged, p[challenged]?.display, state.wager);
        this._twitch.say(state.channel, `Draw — both ${state.wager}g stakes refunded.`);
      }
    } catch (err) {
      this._logger.error('[Arena] reward error:', err);
    }
  }

  async _awardDuelRewards(state) {
    const p = state.participants;
    const winner = p[state.winner];
    const loser  = p[state.loser];

    // Legacy fallback (no characters): just bump win/loss counters.
    if (!winner.char || !loser.char || !this._charMgr) {
      const wStats = this._getStats(state.winner);
      const lStats = this._getStats(state.loser);
      wStats.wins++;
      lStats.losses++;
      this._onStats(state.winner, { ...wStats }).catch(err => this._logger.error('[Arena] stats sync error:', err));
      this._onStats(state.loser,  { ...lStats }).catch(err => this._logger.error('[Arena] stats sync error:', err));
      return;
    }

    const mgr = this._charMgr;
    const underdog = state.underdogUser === state.winner;

    const winRes = mgr.recordWin(winner.char, {
      username: state.winner, displayName: winner.display, underdog,
    });
    const lossRes = mgr.recordLoss(loser.char, {
      username: state.loser, displayName: loser.display,
    });

    // Wager pot
    if (state.pot > 0) winner.char.gold += state.pot;

    // Item drop for the winner (pity counter guarantees rare+ every N wins)
    const drop = mgr.rollDrop(winner.char);

    // One combined reward line
    const potPart   = state.pot > 0 ? ` +${state.pot}g pot` : '';
    const bonusBits = [];
    if (winRes.firstOfDay) bonusBits.push('first duel of the day');
    if (winRes.streakBonus > 0) bonusBits.push(`${winner.char.streak}-win streak`);
    if (underdog) bonusBits.push('underdog bonus');
    const bonusPart = bonusBits.length > 0 ? ` (${bonusBits.join(', ')})` : '';
    const dropDesc  = drop ? ` and finds ${drop.name}${describeBonuses(drop) ? ` (${describeBonuses(drop)})` : ''}!` : '!';
    this._twitch.say(state.channel, clip(
      `💰 ${winner.display} wins ${winRes.gold}g${potPart} & ${winRes.xp} XP${bonusPart}${dropDesc} ` +
      `${loser.display} earns ${lossRes.gold}g & ${lossRes.xp} XP.`
    ));

    // Level-up announcements
    for (const line of [...winRes.announcements, ...lossRes.announcements]) {
      this._twitch.say(state.channel, clip(line));
    }

    // Persist both fighters (arena-only sync) + keep summary stats fresh
    this._syncCharacter(state.winner, winner.display);
    this._syncCharacter(state.loser, loser.display);

    this._onStats(state.winner, {
      wins: winner.char.wins, losses: winner.char.losses, arena_gold: winner.char.gold,
    }).catch(err => this._logger.error('[Arena] stats sync error:', err));
    this._onStats(state.loser, {
      wins: loser.char.wins, losses: loser.char.losses, arena_gold: loser.char.gold,
    }).catch(err => this._logger.error('[Arena] stats sync error:', err));
  }

  _getStats(username) {
    if (!this._interStats.has(username)) {
      this._interStats.set(username, { wins: 0, losses: 0, arena_gold: 0 });
    }
    return this._interStats.get(username);
  }

  // ── Cleanup ──────────────────────────────────────────────────────────────────

  _cleanupExpiredChallenges() {
    const now = Date.now();
    for (const [key, ch] of this._challenges) {
      if (now > ch.expiresAt) {
        this._challenges.delete(key);
        if (ch.wager > 0) this._refundStake(ch.challenger, ch.challengerDisplay, ch.wager);
        this._twitch.say(ch.channel,
          `@${ch.challenged} — the duel challenge from @${ch.challenger} has expired.`
        );
      }
    }
  }

  destroy() {
    clearInterval(this._cleanupTimer);
    for (const state of new Set([...this._activeDuels.values()])) {
      if (state.turnTimer) clearTimeout(state.turnTimer);
    }
  }
}

module.exports = { Arena };
