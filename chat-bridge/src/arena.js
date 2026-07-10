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

/** Strip leading '@'s so a value can be safely inserted after a literal '@'. */
function stripAt(name) { return String(name ?? '').replace(/^@+/, ''); }

/** Render a chat mention — never produces '@@' even if the value has an '@'. */
function mention(name) { return `@${stripAt(name)}`; }

class Arena {
  /**
   * @param {object} opts
   * @param {object} opts.twitchClient     — { say(channel, msg), reply(channel, username, msg) }
   * @param {object} [opts.config={}]      — arena config flags
   * @param {object} [opts.logger]         — { info, warn, error }
   * @param {function} [opts.onStatsUpdate]    — (username, {wins, losses, arena_gold}) → Promise<void>
   * @param {function} [opts.onDisplayEvent]   — (eventPayload) → void  (for OBS overlay)
   * @param {function} [opts.resolveOpponent]  — (name) → Promise<{found, twitch_username, error_code, matches}|null>
   *                                             maps a login/display/character name to a login username
   */
  constructor({ twitchClient, config = {}, logger, onStatsUpdate, onDisplayEvent, progression, resolveOpponent }) {
    this._twitch      = twitchClient;
    this._config      = config;
    this._logger      = logger || console;
    this._onStats     = onStatsUpdate  || (() => Promise.resolve());
    this._onDisplay   = onDisplayEvent || (() => {});
    this._resolveOpponent = resolveOpponent || null;
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
        // _handleDuel is async (opponent resolution may query the server);
        // return the promise so callers/tests can await it. Errors are caught
        // here so a rejection can never escape the arena.
        case 'duel':
          return this._handleDuel(channel, username, displayName, args)
            .catch(err => this._logger.error('[Arena] handleCommand error:', err));
        // _handleAccept is async too (re-checks both fighters' availability).
        case 'accept':
          return this._handleAccept(channel, username, displayName)
            .catch(err => this._logger.error('[Arena] handleCommand error:', err));
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

  async _handleDuel(channel, username, displayName, args) {
    if (!this._enabled) {
      this._twitch.reply(channel, username, 'The arena is closed right now.');
      return;
    }

    // Chatters use Twitch @-autocomplete ("!duel @PotatoWizard") — strip a
    // leading '@' here too in case args arrive un-normalized.
    const rawTarget = stripAt((args[0] || '').trim());
    if (!rawTarget) {
      this._twitch.reply(channel, username, 'Usage: !duel <username>');
      return;
    }
    if (rawTarget.toLowerCase() === username) {
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

    // Resolve the opponent reference (login, display name, or !name character
    // name — case-insensitive). The challenge must be keyed by the opponent's
    // login username or their !accept will never find it.
    let targetName = rawTarget.toLowerCase();
    if (this._resolveOpponent) {
      let res = null;
      try {
        res = await this._resolveOpponent(rawTarget);
      } catch (err) {
        this._logger.error('[Arena] opponent resolve error:', err);
      }
      if (res?.found && res.twitch_username) {
        targetName = String(res.twitch_username).toLowerCase();
      } else if (res?.error_code === 'ambiguous') {
        const options = (res.matches || []).join(', ');
        this._twitch.reply(channel, username,
          `Multiple chatters match "${rawTarget}" — which one? ${options}`);
        return;
      }
      // not_found / lookup unavailable → fall back to the raw name so
      // chatters who haven't joined yet can still be challenged by login.
    }

    if (targetName === username) {
      this._twitch.reply(channel, username, "You can't challenge yourself!");
      return;
    }

    // Permadeath / quest busy-state: neither fighter may be dead or away on a
    // quest. The target check never creates a sheet, so challenging a chatter
    // who has never played stays possible (their sheet rolls at duel start).
    // (Guarded so the no-progression flow stays fully synchronous.)
    if (this._progression) {
      if (!await this._checkEligibility(channel, username, username, true)) return;
      if (!await this._checkEligibility(channel, username, targetName, false)) return;
    }

    const expiresAt = Date.now() + CHALLENGE_TTL;
    this._challenges.set(targetName, { challenger: username, challengerDisplay: displayName, challenged: targetName, channel, expiresAt });

    this._twitch.say(channel, `${mention(targetName)} ${displayName} challenges you to a duel! Type !accept or !decline in ${Math.round(CHALLENGE_TTL / 1000)}s.`);
  }

  /**
   * Is `who` free to duel (alive + not questing)? Replies to `replyTo` and
   * returns false when not. selfCheck=true creates a sheet for the asker
   * (they typed the command); target checks never create phantom sheets.
   */
  async _checkEligibility(channel, replyTo, who, selfCheck) {
    if (!this._progression) return true;
    let elig = { ok: true };
    try {
      elig = await this._progression.duelEligibility(who, { createIfMissing: selfCheck });
    } catch (err) {
      this._logger.error('[Arena] eligibility check error:', err);
      return true; // fail open — the duel itself falls back to base stats
    }
    if (elig.ok) return true;
    const whose = selfCheck ? 'You are' : `${mention(who)} is`;
    if (elig.reason === 'dead') {
      this._twitch.reply(channel, replyTo,
        selfCheck
          ? 'You are dead! 💀 Type !stats to be reborn before dueling.'
          : `${mention(who)} is dead — they must !stats to be reborn first.`);
    } else {
      const mins = Math.max(1, Math.ceil((elig.remainingMs || 0) / 60000));
      this._twitch.reply(channel, replyTo, `${whose} off questing (returns in ${mins}m).`);
    }
    return false;
  }

  async _handleAccept(channel, username, displayName) {
    const challenge = this._challenges.get(username);
    if (!challenge || Date.now() > challenge.expiresAt) {
      this._twitch.reply(channel, username, "No pending challenge for you.");
      return;
    }

    // Either fighter may have died or set off on a quest since the challenge.
    // (Guarded so the no-progression flow stays fully synchronous.)
    if (this._progression) {
      if (!await this._checkEligibility(channel, username, username, true)) return;
      if (!await this._checkEligibility(channel, username, challenge.challenger, false)) {
        this._challenges.delete(username);
        return;
      }
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
    this._twitch.say(channel, `${displayName} declined the duel challenge from ${mention(challenge.challenger)}.`);
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
    const fallback = { maxHp: ARENA_HP, ac: ARENA_AC, atkBonus: 0, startHp: ARENA_HP, potions: 0 };
    if (!this._progression) return fallback;
    try {
      const stats = await this._progression.combatStatsFor(username);
      const maxHp = Math.max(1, Number(stats.maxHp) || ARENA_HP);
      return {
        maxHp,
        ac: Math.max(1, Number(stats.ac) || ARENA_AC),
        atkBonus: Number(stats.atkBonus) || 0,
        // Unhealed quest injuries reduce the HP a fighter brings into a duel.
        startHp: Math.min(maxHp, Math.max(1, Number(stats.startHp) || maxHp)),
        potions: Math.max(0, Number(stats.potions) || 0),
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
        [challenger]: { username: challenger, name: challenger, ...chStats, hp: chStats.startHp ?? chStats.maxHp, potionUsed: false },
        [challenged]: { username: challenged, name: challenged, ...cdStats, hp: cdStats.startHp ?? cdStats.maxHp, potionUsed: false },
      },
      round: 0,
      finished: false,
      interactive: !!this._config.interactiveMode,
    };

    this._activeDuels.set(challenger, state);
    this._activeDuels.set(challenged, state);

    const wounded = [challenger, challenged]
      .filter(u => state.participants[u].hp < state.participants[u].maxHp)
      .map(u => `${mention(u)} enters wounded (${state.participants[u].hp}/${state.participants[u].maxHp} HP)`)
      .join(', ');
    this._twitch.say(channel,
      `⚔️ ARENA DUEL: ${mention(challenger)} vs ${mention(challenged)} — Fight begins!${wounded ? ` ${wounded}.` : ''}`);

    this._onDisplay({
      event_type: 'duel_start',
      challenger,
      opponent: challenged,
      challenger_hp: state.participants[challenger].hp,
      opponent_hp: state.participants[challenged].hp,
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
        messages.push(`Round ${r + 1}: ${mention(attacker)} rolled ${rollLabel} — ${flavour}! ${mention(defender)} takes ${dmg} damage. (${p[defender].hp}/${p[defender].maxHp || ARENA_HP} HP remaining)`);
        if (p[defender].hp <= 0) {
          const potionMsg = this._tryAutoPotion(state, defender);
          if (potionMsg) {
            messages.push(potionMsg);
          } else {
            messages.push(`💀 ${mention(defender)} has been defeated! ${mention(attacker)} wins the duel!`);
            state.finished = true;
            state.winner = attacker;
            state.loser = defender;
            break;
          }
        }
      } else {
        messages.push(`Round ${r + 1}: ${mention(attacker)} rolled ${roll} — miss! ${mention(defender)} dodges.`);
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
        messages.push(`Time's up! ${mention(state.winner)} wins by HP advantage! (${Math.max(hpA, hpB)} vs ${Math.min(hpA, hpB)} HP)`);
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

  /**
   * Death-blow save: with the DM's auto_potion config toggle on (default
   * off), a fighter holding a Healing Potion auto-drinks it the first time
   * they would drop, returning to half max HP. The consumed potion syncs to
   * their sheet immediately.
   */
  _tryAutoPotion(state, username) {
    if (!this._config.autoPotion) return null;
    const fighter = state.participants[username];
    if (!fighter || fighter.potionUsed || (fighter.potions || 0) <= 0) return null;
    fighter.potionUsed = true;
    fighter.potions -= 1;
    fighter.hp = Math.max(1, Math.ceil((fighter.maxHp || ARENA_HP) / 2));
    if (this._progression) {
      this._progression.consumePotions(username, 1).catch(err =>
        this._logger.error('[Arena] potion consume error:', err));
    }
    return `🧪 ${mention(username)} gulps a Healing Potion at death's door — back up to ${fighter.hp} HP!`;
  }

  // ── Interactive mode ─────────────────────────────────────────────────────────

  _runInteractiveRound(state, attacker, defender) {
    if (state.finished) return;

    this._twitch.say(state.channel,
      `${mention(attacker)} it's your turn! Type !attack, !defend, or !flee (${Math.round(TURN_TIMER_MS / 1000)}s)`
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
      msg = `${mention(attacker)} fled the arena! ${mention(defender)} wins by default!`;
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
      msg = `${mention(attacker)} braces for impact (AC+4 this round). ${mention(defender)} attacks — rolled ${roll}...`;
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
        msg = `${mention(attacker)} attacks (rolled ${roll}) — hits for ${dmg} damage! ${mention(defender)}: ${p[defender].hp}/${p[defender].maxHp || ARENA_HP} HP`;
      } else {
        msg = `${mention(attacker)} attacks (rolled ${roll}) — miss!`;
      }
    }

    this._twitch.say(state.channel, msg);

    // Death-blow save before the KO check (auto_potion toggle)
    const downed = Object.values(p).find(pp => pp.hp <= 0);
    if (downed) {
      const potionMsg = this._tryAutoPotion(state, downed.username);
      if (potionMsg) this._twitch.say(state.channel, potionMsg);
    }

    // Check for KO
    const loser = Object.values(p).find(pp => pp.hp <= 0);
    if (loser) {
      const winner = Object.values(p).find(pp => pp.username !== loser.username);
      state.winner = winner?.username ?? null;
      state.loser  = loser.username;
      state.finished = true;
      this._twitch.say(state.channel, `💀 ${mention(loser.username)} is down! ${mention(winner?.username ?? '?')} wins!`);
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
          `${mention(ch.challenged)} — the duel challenge from ${mention(ch.challenger)} has expired.`
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
