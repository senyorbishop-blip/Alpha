'use strict';

/**
 * chat-bridge/src/arena.js — Isolated chatter-vs-chatter PvP arena.
 *
 * ISOLATION GUARANTEE:
 *   - Never modifies campaign tokens, inventory, combat, or DM state.
 *   - Only contacts game server via the onStatsUpdate callback (arena stats only).
 *   - All state is internal; an arena crash cannot propagate to the main game.
 *
 * Supports:
 *   !duel <name>   — challenge another joined chatter (60s expiry)
 *   !accept        — accept pending challenge directed at you
 *   !decline       — decline pending challenge
 *
 * Auto-resolve mode (default):
 *   Fight plays out as 3–5 messages over ~20s using simple d20 rolls.
 *
 * Interactive mode (config.interactiveMode = true):
 *   !attack / !defend / !flee with 15s turn timers.
 */

const ARENA_HP       = 20;
const ARENA_AC       = 12;  // hit on d20 >= 12
const ARENA_DMG_DIE  = 6;   // 1d6 damage
const CHALLENGE_TTL  = 60 * 1000;  // ms
const DUEL_COOLDOWN  = 2 * 60 * 1000;  // ms default
const TURN_TIMER_MS  = 15 * 1000;
const MSG_INTERVAL   = 5 * 1000;  // ms between dramatic messages

function d(sides) { return Math.floor(Math.random() * sides) + 1; }

class Arena {
  /**
   * @param {object} opts
   * @param {object} opts.twitchClient     — { say(channel, msg), reply(channel, username, msg) }
   * @param {object} [opts.config={}]      — arena config flags
   * @param {object} [opts.logger]         — { info, warn, error }
   * @param {function} [opts.onStatsUpdate]    — (username, {wins, losses, arena_gold}) → Promise<void>
   * @param {function} [opts.onDisplayEvent]   — (eventPayload) → void  (for OBS overlay)
   */
  constructor({ twitchClient, config = {}, logger, onStatsUpdate, onDisplayEvent, progression }) {
    this._twitch      = twitchClient;
    this._config      = config;
    this._logger      = logger || console;
    this._onStats     = onStatsUpdate  || (() => Promise.resolve());
    this._onDisplay   = onDisplayEvent || (() => {});
    // Optional ArenaProgression: when attached, duels use each fighter's
    // class/level/gear-derived HP/AC/attack and award XP + gold on finish.
    this._progression = progression || null;

    this._enabled     = true;
    this._quietMode   = false;

    // cooldownMs per user
    this._cooldownMs  = (config.duel_cooldown_seconds ?? 120) * 1000;

    // State maps
    this._challenges  = new Map();  // challenged_username → { challenger, challenged, expiresAt, channel }
    this._activeDuels = new Map();  // username → duelState (one entry per participant)
    this._cooldowns   = new Map();  // username → lastDuelEndedAt
    this._queue       = [];         // [{channel, challenger, challenged}] for quiet mode
    this._interStats  = new Map();  // username → { wins, losses, arena_gold }

    // Cleanup expired challenges every 10s. This is background housekeeping —
    // it must not keep the process alive on its own (nor hold Jest open).
    this._cleanupTimer = setInterval(() => this._cleanupExpiredChallenges(), 10_000);
    if (typeof this._cleanupTimer.unref === 'function') this._cleanupTimer.unref();

    // All pending duel-message timeouts, so destroy() can cancel in-flight duels
    this._messageTimers = new Set();
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
        this._startDuel(entry.channel, entry.challenger, entry.challenged);
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
      switch (action) {
        case 'duel':    this._handleDuel(channel, username, displayName, args); break;
        case 'accept':  this._handleAccept(channel, username, displayName); break;
        case 'decline': this._handleDecline(channel, username, displayName); break;
        case 'attack':
        case 'defend':
        case 'flee':
          if (this._config.interactiveMode) this._handleInteractiveAction(channel, username, displayName, action);
          break;
        default: break;
      }
    } catch (err) {
      this._logger.error('[Arena] handleCommand error:', err);
    }
  }

  // ── Duel lifecycle ───────────────────────────────────────────────────────────

  _handleDuel(channel, username, displayName, args) {
    if (!this._enabled) {
      this._twitch.reply(channel, username, 'The arena is closed right now.');
      return;
    }

    const targetName = (args[0] || '').toLowerCase().trim();
    if (!targetName) {
      this._twitch.reply(channel, username, 'Usage: !duel <username>');
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

    const expiresAt = Date.now() + CHALLENGE_TTL;
    this._challenges.set(targetName, { challenger: username, challengerDisplay: displayName, challenged: targetName, channel, expiresAt });

    this._twitch.say(channel, `@${targetName} ${displayName} challenges you to a duel! Type !accept or !decline in ${Math.round(CHALLENGE_TTL / 1000)}s.`);
  }

  _handleAccept(channel, username, displayName) {
    const challenge = this._challenges.get(username);
    if (!challenge || Date.now() > challenge.expiresAt) {
      this._twitch.reply(channel, username, "No pending challenge for you.");
      return;
    }

    this._challenges.delete(username);

    if (this._quietMode) {
      this._queue.push({ channel, challenger: challenge.challenger, challenged: username });
      this._twitch.say(channel, `${displayName} accepted! Duel queued — arena is in downtime mode.`);
      return;
    }

    if (this._activeDuels.has(challenge.challenger) || this._activeDuels.has(username)) {
      this._twitch.reply(channel, username, "One of you is already in a duel.");
      return;
    }

    this._startDuel(channel, challenge.challenger, username);
  }

  _handleDecline(channel, username, displayName) {
    const challenge = this._challenges.get(username);
    if (!challenge) {
      this._twitch.reply(channel, username, "No pending challenge for you.");
      return;
    }
    this._challenges.delete(username);
    this._twitch.say(channel, `${displayName} declined the duel challenge from @${challenge.challenger}.`);
  }

  // ── Auto-resolve fight ───────────────────────────────────────────────────────

  _startDuel(channel, challenger, challenged) {
    // Fire-and-forget: challenge flow is synchronous, but fighter stats may
    // need an async progression-sheet load before the duel can begin.
    this._startDuelAsync(channel, challenger, challenged).catch(err =>
      this._logger.error('[Arena] duel start error:', err)
    );
  }

  async _fighterStats(username) {
    const fallback = { maxHp: ARENA_HP, ac: ARENA_AC, atkBonus: 0 };
    if (!this._progression) return fallback;
    try {
      const stats = await this._progression.combatStatsFor(username);
      return {
        maxHp: Math.max(1, Number(stats.maxHp) || ARENA_HP),
        ac: Math.max(1, Number(stats.ac) || ARENA_AC),
        atkBonus: Number(stats.atkBonus) || 0,
      };
    } catch (err) {
      this._logger.error('[Arena] progression stats error:', err);
      return fallback;
    }
  }

  async _startDuelAsync(channel, challenger, challenged) {
    const [chStats, cdStats] = await Promise.all([
      this._fighterStats(challenger),
      this._fighterStats(challenged),
    ]);

    // A duel may have started for either fighter while stats loaded.
    if (this._activeDuels.has(challenger) || this._activeDuels.has(challenged)) return;

    const state = {
      channel,
      participants: {
        [challenger]: { username: challenger, hp: chStats.maxHp, name: challenger, ...chStats },
        [challenged]: { username: challenged, hp: cdStats.maxHp, name: challenged, ...cdStats },
      },
      round: 0,
      finished: false,
      interactive: !!this._config.interactiveMode,
    };

    this._activeDuels.set(challenger, state);
    this._activeDuels.set(challenged, state);

    this._twitch.say(channel, `⚔️ ARENA DUEL: @${challenger} vs @${challenged} — Fight begins!`);

    this._onDisplay({
      event_type: 'duel_start',
      challenger,
      opponent: challenged,
      challenger_hp: chStats.maxHp,
      opponent_hp: cdStats.maxHp,
      challenger_max_hp: chStats.maxHp,
      opponent_max_hp: cdStats.maxHp,
      round: 0,
    });

    if (state.interactive) {
      this._runInteractiveRound(state, challenger, challenged);
    } else {
      this._runAutoResolve(state, challenger, challenged);
    }
  }

  _runAutoResolve(state, challenger, challenged) {
    const participants = [challenger, challenged];
    const messages = [];

    // Simulate 3–5 rounds
    const maxRounds = 3 + Math.floor(Math.random() * 3);
    let attacker = challenger;
    let defender = challenged;

    for (let r = 0; r < maxRounds && !state.finished; r++) {
      const p = state.participants;
      const roll = d(20);
      const atk = p[attacker].atkBonus || 0;
      const targetAc = p[defender].ac || ARENA_AC;

      if (roll + atk >= targetAc) {
        const dmg = d(ARENA_DMG_DIE);
        p[defender].hp = Math.max(0, p[defender].hp - dmg);
        const flavour = roll >= 18 ? 'critical strike' : roll >= 14 ? 'solid blow' : 'hit';
        const rollLabel = atk ? `${roll}+${atk}` : `${roll}`;
        messages.push(`Round ${r + 1}: @${attacker} rolled ${rollLabel} — ${flavour}! @${defender} takes ${dmg} damage. (${p[defender].hp}/${p[defender].maxHp || ARENA_HP} HP remaining)`);
        if (p[defender].hp <= 0) {
          messages.push(`💀 @${defender} has been defeated! @${attacker} wins the duel!`);
          state.finished = true;
          state.winner = attacker;
          state.loser = defender;
          break;
        }
      } else {
        messages.push(`Round ${r + 1}: @${attacker} rolled ${roll} — miss! @${defender} dodges.`);
      }

      // Swap attacker/defender
      [attacker, defender] = [defender, attacker];
    }

    if (!state.finished) {
      // Tie — higher HP wins
      const hpA = state.participants[challenger].hp;
      const hpB = state.participants[challenged].hp;
      if (hpA !== hpB) {
        state.winner = hpA > hpB ? challenger : challenged;
        state.loser  = hpA > hpB ? challenged : challenger;
        messages.push(`Time's up! @${state.winner} wins by HP advantage! (${Math.max(hpA, hpB)} vs ${Math.min(hpA, hpB)} HP)`);
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
        this._messageTimers.delete(timer);
        this._twitch.say(state.channel, msg);
        const p = state.participants;
        this._onDisplay({
          event_type: 'round',
          challenger,
          opponent: challenged,
          challenger_hp: p[challenger].hp,
          opponent_hp: p[challenged].hp,
          challenger_max_hp: p[challenger].maxHp || ARENA_HP,
          opponent_max_hp: p[challenged].maxHp || ARENA_HP,
          round: i + 1,
          message: msg,
        });
        if (i === messages.length - 1) {
          this._finishDuel(state, challenger, challenged);
        }
      }, i * MSG_INTERVAL);
      this._messageTimers.add(timer);
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

    let msg;
    if (action === 'flee') {
      msg = `@${attacker} fled the arena! @${defender} wins by default!`;
      state.winner = defender;
      state.loser  = attacker;
      state.finished = true;
      this._twitch.say(state.channel, msg);
      const [ch, cd] = [Object.keys(state.participants)[0], Object.keys(state.participants)[1]];
      this._finishDuel(state, ch, cd);
      return;
    }

    const roll = d(20);
    const p = state.participants;
    state.round = (state.round || 0) + 1;

    if (action === 'defend') {
      msg = `@${attacker} braces for impact (AC+4 this round). @${defender} attacks — rolled ${roll}...`;
      const effectiveAC = (p[attacker].ac || ARENA_AC) + 4;
      if (roll + (p[defender].atkBonus || 0) >= effectiveAC) {
        const dmg = Math.max(1, d(ARENA_DMG_DIE) - 2);
        p[attacker].hp = Math.max(0, p[attacker].hp - dmg);
        msg += ` still hits for ${dmg} damage. (${p[attacker].hp}/${p[attacker].maxHp || ARENA_HP} HP)`;
      } else {
        msg += ` blocked!`;
      }
    } else {
      // attack
      if (roll + (p[attacker].atkBonus || 0) >= (p[defender].ac || ARENA_AC)) {
        const dmg = d(ARENA_DMG_DIE);
        p[defender].hp = Math.max(0, p[defender].hp - dmg);
        msg = `@${attacker} attacks (rolled ${roll}) — hits for ${dmg} damage! @${defender}: ${p[defender].hp}/${p[defender].maxHp || ARENA_HP} HP`;
      } else {
        msg = `@${attacker} attacks (rolled ${roll}) — miss!`;
      }
    }

    this._twitch.say(state.channel, msg);

    // Check for KO
    const loser = Object.values(p).find(pp => pp.hp <= 0);
    if (loser) {
      const winner = Object.values(p).find(pp => pp.username !== loser.username);
      state.winner = winner?.username ?? null;
      state.loser  = loser.username;
      state.finished = true;
      this._twitch.say(state.channel, `💀 @${loser.username} is down! @${winner?.username ?? '?'} wins!`);
      const parts = Object.keys(p);
      this._finishDuel(state, parts[0], parts[1]);
    } else {
      // Next round — swap attacker/defender
      this._runInteractiveRound(state, defender, attacker);
    }
  }

  // ── Duel end ─────────────────────────────────────────────────────────────────

  _finishDuel(state, challenger, challenged) {
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

    if (state.winner && state.loser) {
      if (this._progression) {
        // Progression owns persistent stats: XP, gold, streaks, win/loss all
        // land on the arena character sheet and sync to the server there.
        this._progression.recordDuelResult(state.winner, state.loser, state.channel)
          .catch(err => this._logger.error('[Arena] progression result error:', err));
      } else {
        // Legacy in-memory stats path
        const wStats = this._getStats(state.winner);
        const lStats = this._getStats(state.loser);
        wStats.wins++;
        lStats.losses++;

        // Notify via callback (index.js syncs to game server)
        this._onStats(state.winner, { ...wStats }).catch(err =>
          this._logger.error('[Arena] stats sync error:', err)
        );
        this._onStats(state.loser, { ...lStats }).catch(err =>
          this._logger.error('[Arena] stats sync error:', err)
        );
      }
    }
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
        this._twitch.say(ch.channel,
          `@${ch.challenged} — the duel challenge from @${ch.challenger} has expired.`
        );
      }
    }
  }

  destroy() {
    clearInterval(this._cleanupTimer);
    for (const timer of this._messageTimers) clearTimeout(timer);
    this._messageTimers.clear();
    for (const state of new Set([...this._activeDuels.values()])) {
      if (state.turnTimer) clearTimeout(state.turnTimer);
    }
  }
}

module.exports = { Arena };
