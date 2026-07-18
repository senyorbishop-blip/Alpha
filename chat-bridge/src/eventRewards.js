'use strict';

const { rollRewardEntry, pickRewardTier } = require('./lootRoller');
const { validateUsername } = require('./sanitizer');

/**
 * Sub / gift-sub reward logic for EventSub events.
 *
 * Twitch fires channel.subscription.gift ONCE for the gifter of a batch AND
 * channel.subscribe once per recipient with is_gift=true. Reward rules:
 *
 *   - Genuine self-subs / resubs → one roll on the single-sub table.
 *   - Gift recipients (is_gift=true) → NO reward roll. At most one collective
 *     welcome line per batch for recipients who haven't !join-ed.
 *   - The GIFTER → one roll from the gift-count tier (x1/x5/x10 tables)
 *     matching the batch size (or the session-cumulative total when
 *     `cumulative` is enabled).
 *   - Anonymous gifter → configurable: 'skip' (default; chat thank-you only)
 *     or 'bank' (roll is held for the gifter to claim if they de-anonymize).
 *   - A resub renewal that arrives as BOTH channel.subscribe and
 *     channel.subscription.message rolls only once (per-user dedup window).
 *
 * Config (config/stream.json → "gifts"):
 *   { "anonymous_mode": "skip" | "bank",   // default "skip"
 *     "cumulative": false,                 // tier by session-total gifts
 *     "welcome_recipients": true }         // collective welcome line
 */

const DEFAULT_GIFT_CONFIG = {
  anonymous_mode: 'skip',
  cumulative: false,
  welcome_recipients: true,
};

// Same-renewal channel.subscribe + channel.subscription.message pairs arrive
// within seconds; a legitimate second sub from the same user can't happen
// inside this window.
const SUB_DEDUP_WINDOW_MS = 120_000;

// Recipient channel.subscribe events for one gift batch trail the batch event
// by well under a second each; this collects them into one welcome line.
const WELCOME_BATCH_DELAY_MS = 2_500;

class EventRewards {
  /**
   * @param {object} deps
   * @param {() => object|null} deps.getRewardConfig  server reward config (sub / gift_tiers)
   * @param {object} deps.lootRoller                  LootRoller over local loot-tables.json
   * @param {object} deps.lootTables                  raw local loot-tables.json object
   * @param {Function} deps.grantPowerReward          (username, displayName, entry, trigger, announce)
   * @param {Function} deps.grantItemReward           (username, displayName, itemName, trigger, announce)
   * @param {(username: string) => Promise<boolean>} deps.isJoined
   * @param {(line: string) => void} deps.say         send a plain chat line
   * @param {object} deps.logger
   * @param {object} [deps.giftConfig]                overrides for DEFAULT_GIFT_CONFIG
   * @param {number} [deps.welcomeDelayMs]
   * @param {number} [deps.dedupWindowMs]
   */
  constructor({
    getRewardConfig,
    lootRoller,
    lootTables,
    grantPowerReward,
    grantItemReward,
    isJoined,
    say,
    logger,
    giftConfig,
    welcomeDelayMs,
    dedupWindowMs,
  }) {
    this._getRewardConfig = getRewardConfig ?? (() => null);
    this._lootRoller = lootRoller;
    this._lootTables = lootTables ?? {};
    this._grantPowerReward = grantPowerReward;
    this._grantItemReward = grantItemReward;
    this._isJoined = isJoined ?? (async () => true);
    this._say = say ?? (() => {});
    this._logger = logger ?? console;
    this._giftConfig = { ...DEFAULT_GIFT_CONFIG, ...(giftConfig ?? {}) };
    this._welcomeDelayMs = welcomeDelayMs ?? WELCOME_BATCH_DELAY_MS;
    this._dedupWindowMs = dedupWindowMs ?? SUB_DEDUP_WINDOW_MS;

    this._recentSubRolls = new Map();   // username → last roll timestamp
    this._sessionGiftTotals = new Map(); // username → cumulative gifts this session
    this._pendingWelcome = [];           // recipient usernames awaiting the collective line
    this._welcomeTimer = null;
    this._bankedGiftRewards = [];        // anonymous-gift rewards held for claiming
  }

  attach(eventSubClient) {
    eventSubClient.on('sub', payload => this.handleSub(payload));
    eventSubClient.on('giftsub', payload => this.handleGiftSub(payload));
  }

  destroy() {
    if (this._welcomeTimer) {
      clearTimeout(this._welcomeTimer);
      this._welcomeTimer = null;
    }
  }

  // -------------------------------------------------------------------------
  // channel.subscribe / channel.subscription.message
  // -------------------------------------------------------------------------
  async handleSub({ username, displayName, months, gifted }) {
    const valid = validateUsername(username);
    if (!valid) return;

    if (gifted) {
      // Gift recipients never roll — the gifter's batch event carries the
      // reward. Recipients only feed the collective welcome line.
      this._noteGiftRecipient(valid);
      return;
    }

    if (this._isDuplicateSub(valid)) {
      this._logger.info(`[rewards] duplicate sub event for ${valid} within dedup window — skipping roll`);
      return;
    }

    const trigger = months > 1 ? 'resub' : 'sub';
    const announce = (dn, item) =>
      `${dn} subscribed${months > 1 ? ` (${months} months!)` : ''}! They receive ${item}!`;

    // Server-managed reward table first; local loot-tables.json fallback.
    const rewardConfig = this._getRewardConfig();
    const entry = rewardConfig ? rollRewardEntry(rewardConfig.sub) : null;
    if (entry) {
      await this._grantPowerReward(valid, displayName, entry, trigger, announce);
      return;
    }
    const tableName = this._lootTables.sub ? 'sub' : 'default';
    const itemName = this._lootRoller?.hasTable(tableName) ? this._lootRoller.roll(tableName) : null;
    if (!itemName) {
      this._logger.warn(`[loot] No item rolled from table "${tableName}" for ${valid}`);
      return;
    }
    await this._grantItemReward(valid, displayName, itemName, trigger, announce);
  }

  _isDuplicateSub(username) {
    const now = Date.now();
    for (const [user, at] of this._recentSubRolls) {
      if (now - at >= this._dedupWindowMs) this._recentSubRolls.delete(user);
    }
    if (this._recentSubRolls.has(username)) return true;
    this._recentSubRolls.set(username, now);
    return false;
  }

  // -------------------------------------------------------------------------
  // channel.subscription.gift — the GIFTER's batch event
  // -------------------------------------------------------------------------
  async handleGiftSub({ username, displayName, total, anonymous }) {
    const count = Math.max(1, Number(total) || 1);

    if (anonymous) {
      await this._handleAnonymousGift(count);
      return;
    }

    const valid = validateUsername(username);
    if (!valid) return;

    // Cumulative mode: tier by the gifter's running session total, so
    // 3 gifts then 2 gifts crosses the x5 tier on the second event.
    let tierCount = count;
    if (this._giftConfig.cumulative) {
      tierCount = (this._sessionGiftTotals.get(valid) ?? 0) + count;
      this._sessionGiftTotals.set(valid, tierCount);
    }

    const rolled = this._rollGiftReward(tierCount);
    if (!rolled) return;

    const trigger = `gift_x${count}`;
    const announce = (dn, item) =>
      `${dn} gifted ${count} sub${count > 1 ? 's' : ''} and receives ${item}!`;

    if (rolled.kind === 'power') {
      await this._grantPowerReward(valid, displayName, rolled.entry, trigger, announce);
    } else {
      await this._grantItemReward(valid, displayName, rolled.item, trigger, announce);
    }
  }

  /** Roll from the gift-count tier (server gift_tiers, else local "gift" tiers). */
  _rollGiftReward(tierCount) {
    const rewardConfig = this._getRewardConfig();
    if (rewardConfig) {
      const tier = pickRewardTier(rewardConfig.gift_tiers, 'min_count', tierCount);
      const entry = tier ? rollRewardEntry(tier.table) : null;
      if (entry) return { kind: 'power', entry };
    }
    const localTiers = this._lootTables.gift;
    if (Array.isArray(localTiers) && this._lootRoller) {
      const tier = pickRewardTier(localTiers, 'min_count', tierCount);
      const item = tier ? this._lootRoller.rollTable(tier.table) : null;
      if (item) return { kind: 'item', item };
    }
    return null;
  }

  async _handleAnonymousGift(count) {
    const plural = count > 1 ? 's' : '';
    if (this._giftConfig.anonymous_mode === 'bank') {
      const rolled = this._rollGiftReward(count);
      if (rolled) {
        this._bankedGiftRewards.push({ count, rolled });
        this._logger.info(`[rewards] banked anonymous gift reward (x${count}) — ${this._bankedGiftRewards.length} banked`);
        this._say(`An anonymous hero gifted ${count} sub${plural}! A reward awaits them should they reveal themselves.`);
        return;
      }
    }
    // Default 'skip' (and bank mode with nothing rollable): thank-you only.
    this._say(`A round of applause for an anonymous hero who just gifted ${count} sub${plural}!`);
  }

  /**
   * Grant all banked anonymous-gift rewards to a de-anonymized gifter
   * (e.g. wired to a DM action). Returns the number of rewards granted.
   */
  async claimBankedGiftRewards(username, displayName) {
    const valid = validateUsername(username);
    if (!valid) return 0;
    const banked = this._bankedGiftRewards.splice(0);
    for (const { count, rolled } of banked) {
      const trigger = `gift_x${count}`;
      const announce = (dn, item) =>
        `${dn} is revealed as the hero behind ${count} gifted sub${count > 1 ? 's' : ''} and receives ${item}!`;
      if (rolled.kind === 'power') {
        await this._grantPowerReward(valid, displayName, rolled.entry, trigger, announce);
      } else {
        await this._grantItemReward(valid, displayName, rolled.item, trigger, announce);
      }
    }
    return banked.length;
  }

  get bankedGiftRewardCount() {
    return this._bankedGiftRewards.length;
  }

  // -------------------------------------------------------------------------
  // Collective recipient welcome — one line per batch, never per-recipient
  // -------------------------------------------------------------------------
  _noteGiftRecipient(username) {
    if (!this._giftConfig.welcome_recipients) return;
    this._pendingWelcome.push(username);
    if (!this._welcomeTimer) {
      this._welcomeTimer = setTimeout(() => {
        this._flushWelcome().catch(err =>
          this._logger.error('[rewards] welcome flush error:', err));
      }, this._welcomeDelayMs);
      if (typeof this._welcomeTimer.unref === 'function') this._welcomeTimer.unref();
    }
  }

  async _flushWelcome() {
    this._welcomeTimer = null;
    const batch = [...new Set(this._pendingWelcome.splice(0))];
    if (!batch.length) return;
    const joined = await Promise.all(
      // On lookup failure assume joined — a missing welcome beats chat noise.
      batch.map(u => Promise.resolve(this._isJoined(u)).catch(() => true))
    );
    const newcomers = batch.filter((_, i) => !joined[i]);
    if (!newcomers.length) return;
    const line = newcomers.length === 1
      ? 'Welcome to our new tavern patron — type !join to play!'
      : `Welcome to the ${newcomers.length} new tavern patrons — type !join to play!`;
    this._say(line);
  }
}

module.exports = { EventRewards, DEFAULT_GIFT_CONFIG };
