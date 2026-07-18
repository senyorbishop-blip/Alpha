'use strict';

/**
 * chat-bridge/src/arenaProgression.js — Arena RPG progression for chatters.
 *
 * Gives every chatter a persistent arena character: a random class, 4d6
 * (drop lowest) ability scores, XP/levels, gold, and equippable items bought
 * from a shop. Powers the !stats !bag !equip !shop !buy !levelup and
 * !leaderboard commands and feeds derived combat stats into the arena duels.
 *
 * ISOLATION GUARANTEE (same contract as arena.js):
 *   - Never touches campaign tokens, inventory, combat, or DM state.
 *   - Persists ONLY through chat_participant_arena_load / _arena_sync, which
 *     the server whitelists to participant.arena_character.
 *
 * The character sheet mirrors the server's _sanitize_arena_character shape:
 *   { class, level, xp, gold, abilities{str,dex,con,int,wis,cha}, items[],
 *     equipped{weapon,armor,trinket}, wins, losses, streak, best_streak,
 *     last_duel_day, wins_since_rare, rerolled, pending_stat_points,
 *     created_at }
 */

const ABILITIES = ['str', 'dex', 'con', 'int', 'wis', 'cha'];

// class → primary ability (drives attack bonus and level-up gains)
const CLASSES = {
  Barbarian: 'str',
  Fighter:   'str',
  Paladin:   'str',
  Rogue:     'dex',
  Ranger:    'dex',
  Monk:      'dex',
  Wizard:    'int',
  Sorcerer:  'cha',
  Bard:      'cha',
  Warlock:   'cha',
  Cleric:    'wis',
  Druid:     'wis',
};
const CLASS_NAMES = Object.keys(CLASSES);

// Arena shop — the GOOD gear (uncommon/rare/epic) plus Healing Potions,
// priced as a meaningful gold sink. Basic (common) gear is intentionally NOT
// sold here: it drops (rarely) from quests. Override via config/shop.json.
// Bonuses: atk/ac/hp are flat combat bonuses; ability keys (str/dex/…) raise
// the effective ability score while equipped.
const SHOP_CATALOG = [
  { id: 'steel_sword',    name: 'Steel Sword',          slot: 'weapon',  cost: 160,  rarity: 'uncommon', bonuses: { atk: 2 } },
  { id: 'flamebrand',     name: 'Flamebrand',           slot: 'weapon',  cost: 420,  rarity: 'rare',     bonuses: { atk: 3, hp: 2 } },
  { id: 'starfall_edge',  name: 'Starfall Edge',        slot: 'weapon',  cost: 1000, rarity: 'epic',     bonuses: { atk: 5, hp: 4 } },
  { id: 'chain_shirt',    name: 'Chain Shirt',          slot: 'armor',   cost: 160,  rarity: 'uncommon', bonuses: { ac: 2 } },
  { id: 'dragonhide',     name: 'Dragonhide Cloak',     slot: 'armor',   cost: 420,  rarity: 'rare',     bonuses: { ac: 3, hp: 2 } },
  { id: 'aegis_colossus', name: 'Aegis of the Colossus', slot: 'armor',  cost: 1000, rarity: 'epic',     bonuses: { ac: 4, hp: 6 } },
  { id: 'giants_belt',    name: "Giant's Belt",         slot: 'trinket', cost: 180,  rarity: 'uncommon', bonuses: { str: 2, hp: 4 } },
  { id: 'cat_charm',      name: 'Cat Charm',            slot: 'trinket', cost: 180,  rarity: 'uncommon', bonuses: { dex: 2, ac: 1 } },
  { id: 'phoenix_plume',  name: 'Phoenix Plume',        slot: 'trinket', cost: 450,  rarity: 'rare',     bonuses: { hp: 8 } },
  { id: 'crown_of_ages',  name: 'Crown of Ages',        slot: 'trinket', cost: 1100, rarity: 'epic',     bonuses: { atk: 2, ac: 2, hp: 4 } },
  // Consumable: cures quest injuries between duels (!heal); with the DM's
  // auto_potion toggle on, also auto-drinks on a death-blow in duels.
  { id: 'healing_potion', name: 'Healing Potion',       slot: 'potion',  cost: 40,   rarity: 'common',   consumable: true },
];

const MAX_POTIONS = 10;

const XP_WIN = 60;
const XP_LOSS = 20;
const GOLD_WIN = 15;
const GOLD_LOSS = 5;
const STARTING_GOLD = 50;

function xpForNextLevel(level) { return Math.max(1, level) * 100; }

function d6() { return Math.floor(Math.random() * 6) + 1; }

/** 4d6 drop lowest. */
function rollAbilityScore() {
  const rolls = [d6(), d6(), d6(), d6()].sort((a, b) => a - b);
  return rolls[1] + rolls[2] + rolls[3];
}

function abilityMod(score) { return Math.floor((score - 10) / 2); }

/** Render a chat mention — never produces '@@' even if the value has an '@'. */
function mention(name) { return `@${String(name ?? '').replace(/^@+/, '')}`; }

class ArenaProgression {
  /**
   * @param {object} opts
   * @param {object} opts.gameClient   — GameClient (send(type, payload))
   * @param {object} opts.twitchClient — { say, reply }
   * @param {object} [opts.logger]
   */
  constructor({ gameClient, twitchClient, logger, shopCatalog, legacyConfig, onTickerEvent }) {
    this._game = gameClient;
    this._twitch = twitchClient;
    this._logger = logger || console;
    this._onTicker = onTickerEvent || (() => {});
    this._sheets = new Map();      // username → sheet (cache of server state)
    this._loading = new Map();     // username → Promise (de-dupe concurrent loads)
    this._shop = Array.isArray(shopCatalog) && shopCatalog.length ? shopCatalog : SHOP_CATALOG;
    // Optional permadeath "legacy bonus" (default off = pure permadeath):
    // { gold_percent: 0-100, heirloom: bool }
    this._legacy = { gold_percent: 0, heirloom: false, ...(legacyConfig || {}) };
    this._quests = null;           // QuestManager, attached by index.js
  }

  /** Attach the QuestManager (owns !quest/!graveyard + lazy resolution). */
  attachQuests(questManager) { this._quests = questManager; }

  /** Mirror a broadcast event to the overlay ticker ('@' is chat-only). */
  _ticker(category, text) {
    try {
      this._onTicker(category, String(text).replace(/@/g, ''));
    } catch (err) {
      this._logger.error('[ArenaProgression] ticker emit error:', err);
    }
  }

  // ── Character sheet lifecycle ───────────────────────────────────────────────

  /** Create a fresh level-1 character with a random class and 4d6 abilities. */
  createCharacter() {
    const cls = CLASS_NAMES[Math.floor(Math.random() * CLASS_NAMES.length)];
    const abilities = {};
    for (const a of ABILITIES) abilities[a] = rollAbilityScore();
    return {
      class: cls,
      level: 1,
      xp: 0,
      gold: STARTING_GOLD,
      abilities,
      wins: 0,
      losses: 0,
      streak: 0,
      best_streak: 0,
      last_duel_day: '',
      wins_since_rare: 0,
      rerolled: false,
      items: [],
      equipped: { weapon: null, armor: null, trinket: null },
      pending_stat_points: 0,
      injuries: 0,   // HP deficit carried into duels until healed (!heal)
      potions: 0,    // Healing Potions held (bought in the shop)
      created_at: Date.now(),
    };
  }

  /**
   * Get a chatter's sheet, loading it from the server (and creating + syncing
   * a new one when none exists). Returns { sheet, isNew }.
   */
  async getSheet(username) {
    if (this._sheets.has(username)) {
      return { sheet: this._sheets.get(username), isNew: false };
    }
    if (this._loading.has(username)) {
      return this._loading.get(username);
    }

    const promise = (async () => {
      let sheet = null;
      try {
        const result = await this._game.send('chat_participant_arena_load', {
          twitch_username: username,
        });
        const raw = result?.found ? result.arena_character : null;
        if (raw?.dead) {
          // Permadeath tombstone — the chatter must be reborn via !stats.
          sheet = {
            dead: true,
            death_cause: String(raw.death_cause || ''),
            died_at: Number(raw.died_at) || 0,
            legacy_gold: Math.max(0, parseInt(raw.legacy_gold, 10) || 0),
            heirloom: raw.heirloom && raw.heirloom.name ? raw.heirloom : null,
          };
        } else if (raw?.class) {
          sheet = this._normalizeLoaded(raw);
          // Quest timestamps are server-owned; convert the server-computed
          // remaining seconds into a local deadline for busy checks/countdowns.
          if (raw.quest && typeof raw.quest === 'object') {
            const remainingMs = Math.max(0, Number(result.quest_remaining_s) || 0) * 1000;
            sheet.quest = { ...raw.quest, local_ends_at: Date.now() + remainingMs };
          }
          const cooldownS = Math.max(0, Number(result.quest_cooldown_remaining_s) || 0);
          if (cooldownS > 0) sheet.quest_cooldown_local = Date.now() + cooldownS * 1000;
        }
      } catch (err) {
        this._logger.error('[ArenaProgression] load error:', err);
      }

      let isNew = false;
      if (!sheet) {
        sheet = this.createCharacter();
        isNew = true;
      }
      this._sheets.set(username, sheet);
      if (isNew) await this._sync(username);
      // Loaded mid-quest (e.g. after a bridge restart) — let the quest
      // manager re-arm its return timer / resolve an overdue quest.
      if (sheet.quest && this._quests) this._quests.scheduleFromSheet(username, sheet);
      return { sheet, isNew };
    })().finally(() => this._loading.delete(username));

    this._loading.set(username, promise);
    return promise;
  }

  /** Cached sheet only — never triggers a server load or creation. */
  peekSheet(username) {
    return this._sheets.get(username) || null;
  }

  /**
   * Load a sheet from the server WITHOUT creating one when none exists.
   * Used to check duel targets who may never have played. Returns null
   * when the chatter has no character.
   */
  async loadSheetIfExists(username) {
    if (this._sheets.has(username)) return this._sheets.get(username);
    try {
      const result = await this._game.send('chat_participant_arena_load', {
        twitch_username: username,
      });
      if (!result?.found || !result.arena_character) return null;
    } catch (err) {
      this._logger.error('[ArenaProgression] loadSheetIfExists error:', err);
      return null;
    }
    // A record exists — run the full (cached, de-duped) load path.
    const { sheet } = await this.getSheet(username);
    return sheet;
  }

  /** Coerce a server-restored sheet into the working shape. */
  _normalizeLoaded(raw) {
    const sheet = this.createCharacter();
    sheet.class = String(raw.class || sheet.class);
    sheet.level = Math.max(1, parseInt(raw.level, 10) || 1);
    sheet.xp = Math.max(0, parseInt(raw.xp, 10) || 0);
    sheet.gold = Math.max(0, parseInt(raw.gold, 10) || 0);
    if (raw.abilities && typeof raw.abilities === 'object') {
      for (const a of ABILITIES) {
        const v = parseInt(raw.abilities[a], 10);
        if (Number.isFinite(v)) sheet.abilities[a] = Math.max(1, Math.min(30, v));
      }
    }
    sheet.wins = Math.max(0, parseInt(raw.wins, 10) || 0);
    sheet.losses = Math.max(0, parseInt(raw.losses, 10) || 0);
    sheet.streak = Math.max(0, parseInt(raw.streak, 10) || 0);
    sheet.best_streak = Math.max(0, parseInt(raw.best_streak, 10) || 0);
    sheet.last_duel_day = String(raw.last_duel_day || '');
    sheet.wins_since_rare = Math.max(0, parseInt(raw.wins_since_rare, 10) || 0);
    sheet.rerolled = !!raw.rerolled;
    sheet.items = Array.isArray(raw.items) ? raw.items.filter(i => i && i.name) : [];
    sheet.equipped = { weapon: null, armor: null, trinket: null };
    if (raw.equipped && typeof raw.equipped === 'object') {
      for (const slot of ['weapon', 'armor', 'trinket']) {
        if (raw.equipped[slot]) sheet.equipped[slot] = String(raw.equipped[slot]);
      }
    }
    sheet.pending_stat_points = Math.max(0, parseInt(raw.pending_stat_points, 10) || 0);
    sheet.injuries = Math.max(0, parseInt(raw.injuries, 10) || 0);
    sheet.potions = Math.max(0, parseInt(raw.potions, 10) || 0);
    sheet.created_at = parseInt(raw.created_at, 10) || Date.now();
    return sheet;
  }

  async _sync(username) {
    const sheet = this._sheets.get(username);
    if (!sheet) return;
    try {
      // quest / local_* fields are server-owned or bridge-local: the server
      // strips them on sync, so the sheet can be sent as-is.
      await this._game.send('chat_participant_arena_sync', {
        twitch_username: username,
        arena_character: sheet,
      });
    } catch (err) {
      this._logger.error('[ArenaProgression] sync error:', err);
    }
  }

  /** Public sync — used by the QuestManager after applying quest outcomes. */
  async sync(username) { return this._sync(username); }

  // ── Availability (permadeath + questing busy-state) ─────────────────────────

  isDead(sheet) { return !!(sheet && sheet.dead); }

  /** Milliseconds until the cached sheet's quest ends (0 = not questing). */
  questRemainingMs(sheet) {
    const q = sheet && sheet.quest;
    if (!q || !q.local_ends_at) return 0;
    return Math.max(0, q.local_ends_at - Date.now());
  }

  /**
   * Can this chatter start/accept a duel right now?
   * Returns { ok } or { ok:false, reason: 'dead'|'questing', remainingMs }.
   * With createIfMissing=false, chatters with no character are eligible
   * (their sheet is created when the duel actually starts) — used for duel
   * targets so a mistyped name never creates a phantom character.
   */
  async duelEligibility(username, { createIfMissing = true } = {}) {
    let sheet = this._sheets.get(username) || null;
    if (!sheet) {
      sheet = createIfMissing
        ? (await this.getSheet(username)).sheet
        : await this.loadSheetIfExists(username);
    }
    if (!sheet) return { ok: true };
    if (this.isDead(sheet)) return { ok: false, reason: 'dead' };
    const remainingMs = this.questRemainingMs(sheet);
    if (remainingMs > 0) return { ok: false, reason: 'questing', remainingMs };
    return { ok: true };
  }

  /** Consume Healing Potions after a duel auto-use; syncs the sheet. */
  async consumePotions(username, count = 1) {
    const sheet = this._sheets.get(username);
    if (!sheet || sheet.dead) return;
    sheet.potions = Math.max(0, (sheet.potions || 0) - count);
    await this._sync(username);
  }

  _deadReply(channel, username) {
    this._twitch.reply(channel, username,
      'You are dead! 💀 Type !stats (or !rebirth) to roll a new hero.');
  }

  // ── Derived stats ───────────────────────────────────────────────────────────

  _equippedItems(sheet) {
    const out = [];
    for (const slot of ['weapon', 'armor', 'trinket']) {
      const id = sheet.equipped?.[slot];
      if (!id) continue;
      const item = (sheet.items || []).find(i => i.id === id || i.name === id);
      if (item) out.push(item);
    }
    return out;
  }

  /** Ability scores with equipped item bonuses applied. */
  effectiveAbilities(sheet) {
    const out = { ...sheet.abilities };
    for (const item of this._equippedItems(sheet)) {
      const bonuses = item.bonuses || {};
      for (const a of ABILITIES) {
        if (bonuses[a]) out[a] = Math.max(1, Math.min(30, out[a] + Number(bonuses[a])));
      }
    }
    return out;
  }

  /** Flat combat stats used by the arena: maxHp, ac, atkBonus. */
  combatStats(sheet) {
    const eff = this.effectiveAbilities(sheet);
    let itemAtk = 0, itemAc = 0, itemHp = 0;
    for (const item of this._equippedItems(sheet)) {
      const b = item.bonuses || {};
      itemAtk += Number(b.atk || 0);
      itemAc  += Number(b.ac || 0);
      itemHp  += Number(b.hp || 0);
    }
    const primary = CLASSES[sheet.class] || 'str';
    return {
      maxHp: 20 + abilityMod(eff.con) * 2 + (sheet.level - 1) * 3 + itemHp,
      ac: 12 + Math.min(3, Math.max(-2, abilityMod(eff.dex))) + itemAc,
      atkBonus: abilityMod(eff[primary]) + itemAtk,
    };
  }

  /** Async convenience for the arena: load sheet then derive combat stats.
   * startHp reflects unhealed quest injuries; potions feed the optional
   * auto-potion death-blow save. */
  async combatStatsFor(username) {
    const { sheet } = await this.getSheet(username);
    if (sheet.dead) {
      // Shouldn't happen (arena gates dead fighters) — fail safe with base stats.
      return { maxHp: 20, ac: 12, atkBonus: 0, startHp: 20, potions: 0 };
    }
    const combat = this.combatStats(sheet);
    return {
      ...combat,
      startHp: Math.max(1, combat.maxHp - (sheet.injuries || 0)),
      potions: sheet.potions || 0,
    };
  }

  // ── Duel results (called by arena.js when a duel finishes) ──────────────────

  /**
   * Apply and persist a duel result. Returns the rewards summary so the arena
   * can fold it into its single result chat line (slim narration). With
   * announce=true (full narration) the classic separate rewards line is sent.
   */
  async recordDuelResult(winnerUsername, loserUsername, channel, { announce = true } = {}) {
    try {
      const [{ sheet: win }, { sheet: lose }] = await Promise.all([
        this.getSheet(winnerUsername),
        this.getSheet(loserUsername),
      ]);

      win.wins += 1;
      win.streak += 1;
      win.best_streak = Math.max(win.best_streak, win.streak);
      win.xp += XP_WIN;
      win.gold += GOLD_WIN;

      lose.losses += 1;
      lose.streak = 0;
      lose.xp += XP_LOSS;
      lose.gold += GOLD_LOSS;

      await Promise.all([this._sync(winnerUsername), this._sync(loserUsername)]);

      const winnerReady = win.xp >= xpForNextLevel(win.level);
      if (announce) {
        const winReady = winnerReady ? ' Ready to !levelup!' : '';
        this._twitch.say(channel,
          `${mention(winnerUsername)} earns ${XP_WIN} XP and ${GOLD_WIN} gold (${win.xp}/${xpForNextLevel(win.level)} XP).${winReady} ` +
          `${mention(loserUsername)} earns ${XP_LOSS} XP and ${GOLD_LOSS} gold for the effort.`
        );
      }
      return {
        xpWin: XP_WIN, goldWin: GOLD_WIN,
        xpLoss: XP_LOSS, goldLoss: GOLD_LOSS,
        winnerReady,
      };
    } catch (err) {
      this._logger.error('[ArenaProgression] recordDuelResult error:', err);
      return null;
    }
  }

  // ── Command handlers ────────────────────────────────────────────────────────

  /**
   * Entry point from commandParser.
   * @param {string} action — 'stats'|'bag'|'equip'|'shop'|'buy'|'levelup'|
   *                          'leaderboard'|'heal'|'rebirth'|'quest'|'graveyard'
   */
  async handleCommand(channel, username, displayName, action, args) {
    try {
      // Quest board + graveyard live in the QuestManager.
      if (action === 'quest' || action === 'graveyard') {
        if (this._quests) await this._quests.handleCommand(channel, username, displayName, action, args);
        return;
      }

      // Lazy quest resolution: an overdue quest resolves (and announces its
      // return) before the command runs, so state is never stale.
      if (this._quests) await this._quests.maybeResolve(channel, username);

      // Permadeath gate: dead chatters can only look at the leaderboard or
      // be reborn — everything else points them to !stats.
      if (action !== 'leaderboard') {
        const { sheet, isNew } = await this.getSheet(username);
        if (isNew) this._announceNewCharacter(channel, username, sheet);
        if (sheet.dead && action !== 'stats' && action !== 'rebirth') {
          this._deadReply(channel, username);
          return;
        }
        // Questing heroes took their satchel with them — no potion use while away.
        if (action === 'heal') {
          const remainingMs = this.questRemainingMs(sheet);
          if (remainingMs > 0) {
            const mins = Math.max(1, Math.ceil(remainingMs / 60000));
            this._twitch.reply(channel, username,
              `You're off questing (returns in ${mins}m) — no potions until you're back.`);
            return;
          }
        }
      }

      switch (action) {
        case 'stats':
        case 'rebirth':     await this._handleStats(channel, username, displayName, action === 'rebirth'); break;
        case 'bag':         await this._handleBag(channel, username, displayName); break;
        case 'equip':       await this._handleEquip(channel, username, displayName, args); break;
        case 'shop':        this._handleShop(channel, username); break;
        case 'buy':         await this._handleBuy(channel, username, displayName, args); break;
        case 'levelup':     await this._handleLevelup(channel, username, displayName); break;
        case 'heal':        await this._handleHeal(channel, username, displayName); break;
        case 'leaderboard': await this._handleLeaderboard(channel, username); break;
        default: break;
      }
    } catch (err) {
      this._logger.error(`[ArenaProgression] ${action} error:`, err);
    }
  }

  async _handleStats(channel, username, displayName, explicitRebirth = false) {
    let { sheet } = await this.getSheet(username);

    if (sheet.dead) {
      // REBIRTH: the dead chatter's !stats (or !rebirth) rolls a fresh hero.
      sheet = await this._rebirth(channel, username, sheet);
      return;
    }
    if (explicitRebirth) {
      this._twitch.reply(channel, username,
        `You're alive and well — !rebirth only works from beyond the grave. See !stats.`);
      return;
    }

    const eff = this.effectiveAbilities(sheet);
    const combat = this.combatStats(sheet);
    const abilityStr = ABILITIES.map(a => {
      const base = sheet.abilities[a];
      const total = eff[a];
      const label = a.toUpperCase();
      return total !== base ? `${label} ${total} (${base}+${total - base})` : `${label} ${total}`;
    }).join(' ');

    const injured = (sheet.injuries || 0) > 0
      ? ` | 🩸 wounded -${Math.min(sheet.injuries, combat.maxHp - 1)} HP (!heal)` : '';
    const potions = (sheet.potions || 0) > 0 ? ` | 🧪 x${sheet.potions}` : '';
    this._twitch.reply(channel, username,
      `${sheet.class} L${sheet.level} — ${abilityStr} | HP ${combat.maxHp} AC ${combat.ac} ATK +${combat.atkBonus} | ` +
      `XP ${sheet.xp}/${xpForNextLevel(sheet.level)} | ${sheet.gold} gold | ${sheet.wins}W/${sheet.losses}L${injured}${potions}`
    );
  }

  /** Roll a brand-new character for a dead chatter, applying any configured
   * legacy bonus from the tombstone. Announced like a first join. */
  async _rebirth(channel, username, tombstone) {
    const sheet = this.createCharacter();
    const perks = [];
    const legacyGold = Math.max(0, parseInt(tombstone.legacy_gold, 10) || 0);
    if (legacyGold > 0) {
      sheet.gold += legacyGold;
      perks.push(`${legacyGold}g inherited`);
    }
    const heirloom = tombstone.heirloom;
    if (heirloom && heirloom.name) {
      sheet.items.push({ ...heirloom });
      const slot = ['weapon', 'armor', 'trinket'].includes(heirloom.slot) ? heirloom.slot : 'trinket';
      if (!sheet.equipped[slot]) sheet.equipped[slot] = heirloom.id || heirloom.name;
      perks.push(`heirloom: ${heirloom.name}`);
    }
    this._sheets.set(username, sheet);
    await this._sync(username);

    const a = sheet.abilities;
    const legacyNote = perks.length ? ` (${perks.join(', ')})` : '';
    const msg =
      `⚰️✨ ${mention(username)} is REBORN as a ${sheet.class}! ` +
      `STR ${a.str} DEX ${a.dex} CON ${a.con} INT ${a.int} WIS ${a.wis} CHA ${a.cha} — ` +
      `${sheet.gold} starting gold${legacyNote}. A new legend begins!`;
    this._twitch.say(channel, msg);
    this._ticker('levelup', msg);
    return sheet;
  }

  async _handleHeal(channel, username, displayName) {
    const { sheet } = await this.getSheet(username);
    if ((sheet.potions || 0) <= 0) {
      this._twitch.reply(channel, username,
        `No Healing Potions in your satchel — grab one in the !shop.`);
      return;
    }
    if ((sheet.injuries || 0) <= 0) {
      this._twitch.reply(channel, username,
        `You're unhurt — save the potion for when a quest goes wrong. (🧪 x${sheet.potions})`);
      return;
    }
    sheet.potions -= 1;
    const cured = sheet.injuries;
    sheet.injuries = 0;
    await this._sync(username);
    const combat = this.combatStats(sheet);
    this._twitch.reply(channel, username,
      `🧪 You drink a Healing Potion — wounds cured (+${cured} HP, back to ${combat.maxHp}). ${sheet.potions} left.`);
  }

  async _handleBag(channel, username, displayName) {
    const { sheet, isNew } = await this.getSheet(username);
    if (isNew) this._announceNewCharacter(channel, username, sheet);

    const items = sheet.items || [];
    if (items.length === 0) {
      this._twitch.reply(channel, username,
        `Your arena bag is empty. Win duels for gold, then !shop and !buy gear. You have ${sheet.gold} gold.`);
      return;
    }
    const equippedIds = new Set(Object.values(sheet.equipped || {}).filter(Boolean));
    const list = items.map(i => {
      const mark = (equippedIds.has(i.id) || equippedIds.has(i.name)) ? '✦' : '';
      return `${i.name}${mark} (${i.slot})`;
    }).join(', ');
    this._twitch.reply(channel, username, `Arena bag: ${list} — ✦ = equipped. !equip <item> to swap.`);
  }

  async _handleEquip(channel, username, displayName, args) {
    const query = (args || []).join(' ').trim().toLowerCase();
    if (!query) {
      this._twitch.reply(channel, username, 'Usage: !equip <item name> — see !bag for your items.');
      return;
    }
    const { sheet, isNew } = await this.getSheet(username);
    if (isNew) this._announceNewCharacter(channel, username, sheet);

    const item = (sheet.items || []).find(i => String(i.name || '').toLowerCase().includes(query));
    if (!item) {
      this._twitch.reply(channel, username, `No '${args.join(' ')}' in your bag. Check !bag.`);
      return;
    }
    const slot = ['weapon', 'armor', 'trinket'].includes(item.slot) ? item.slot : 'trinket';
    sheet.equipped[slot] = item.id || item.name;
    await this._sync(username);
    const combat = this.combatStats(sheet);
    this._twitch.reply(channel, username,
      `Equipped ${item.name} (${slot}). Now: HP ${combat.maxHp} AC ${combat.ac} ATK +${combat.atkBonus}.`);
  }

  _handleShop(channel, username) {
    const list = this._shop.map(i => {
      const desc = i.consumable
        ? 'cures wounds'
        : Object.entries(i.bonuses || {})
            .map(([k, v]) => `${k.length <= 3 ? k.toUpperCase() : k}+${v}`).join('/');
      return `${i.name} ${i.cost}g [${i.rarity}] (${desc})`;
    }).join(', ');
    this._twitch.reply(channel, username,
      `Arena shop: ${list} — !buy <item>. Basic gear? Go questing (!quest).`);
  }

  async _handleBuy(channel, username, displayName, args) {
    const query = (args || []).join(' ').trim().toLowerCase();
    if (!query) {
      this._twitch.reply(channel, username, 'Usage: !buy <item name> — see !shop for prices.');
      return;
    }
    const { sheet, isNew } = await this.getSheet(username);
    if (isNew) this._announceNewCharacter(channel, username, sheet);

    const entry = this._shop.find(i => i.name.toLowerCase().includes(query) || i.id === query);
    if (!entry) {
      this._twitch.reply(channel, username, `The shop doesn't sell '${args.join(' ')}'. See !shop.`);
      return;
    }

    if (entry.consumable) {
      // Healing Potions stack (up to MAX_POTIONS) instead of entering the bag.
      if ((sheet.potions || 0) >= MAX_POTIONS) {
        this._twitch.reply(channel, username, `You can't carry more than ${MAX_POTIONS} potions.`);
        return;
      }
      if (sheet.gold < entry.cost) {
        this._twitch.reply(channel, username,
          `${entry.name} costs ${entry.cost} gold — you have ${sheet.gold}. Quests and duels pay gold!`);
        return;
      }
      sheet.gold -= entry.cost;
      sheet.potions = (sheet.potions || 0) + 1;
      await this._sync(username);
      this._twitch.reply(channel, username,
        `Bought ${entry.name} (🧪 x${sheet.potions}) — !heal to cure wounds. ${sheet.gold} gold left.`);
      return;
    }

    if ((sheet.items || []).some(i => i.id === entry.id)) {
      this._twitch.reply(channel, username, `You already own ${entry.name}. !equip ${entry.name} to use it.`);
      return;
    }
    if (sheet.gold < entry.cost) {
      this._twitch.reply(channel, username,
        `${entry.name} costs ${entry.cost} gold — you have ${sheet.gold}. Win duels to earn more!`);
      return;
    }

    sheet.gold -= entry.cost;
    sheet.items.push({
      id: entry.id,
      name: entry.name,
      slot: entry.slot,
      rarity: entry.rarity,
      bonuses: { ...entry.bonuses },
      effect: '',
      cosmetic: '',
    });
    let equippedNote = '';
    if (!sheet.equipped[entry.slot]) {
      sheet.equipped[entry.slot] = entry.id;
      equippedNote = ' (equipped)';
    }
    await this._sync(username);
    this._twitch.reply(channel, username,
      `Bought ${entry.name} for ${entry.cost} gold${equippedNote}. ${sheet.gold} gold left.`);
  }

  async _handleLevelup(channel, username, displayName) {
    const { sheet, isNew } = await this.getSheet(username);
    if (isNew) this._announceNewCharacter(channel, username, sheet);

    const needed = xpForNextLevel(sheet.level);
    if (sheet.xp < needed) {
      this._twitch.reply(channel, username,
        `Level ${sheet.level} — ${sheet.xp}/${needed} XP. Win duels (!duel <name>) to earn more.`);
      return;
    }

    sheet.xp -= needed;
    sheet.level += 1;
    const primary = CLASSES[sheet.class] || 'str';
    const others = ABILITIES.filter(a => a !== primary);
    const bonus = others[Math.floor(Math.random() * others.length)];
    sheet.abilities[primary] = Math.min(30, sheet.abilities[primary] + 1);
    sheet.abilities[bonus] = Math.min(30, sheet.abilities[bonus] + 1);
    await this._sync(username);

    const combat = this.combatStats(sheet);
    // One completed-event chat line; the ticker mirrors it for the overlay.
    const msg =
      `🎉 ${mention(username)} is now a Level ${sheet.level} ${sheet.class}! ` +
      `+1 ${primary.toUpperCase()}, +1 ${bonus.toUpperCase()} — HP ${combat.maxHp} AC ${combat.ac} ATK +${combat.atkBonus}.`;
    this._twitch.say(channel, msg);
    this._ticker('levelup', msg);
  }

  async _handleLeaderboard(channel, username) {
    let entries = [];
    try {
      const result = await this._game.send('chat_participant_arena_leaderboard', { limit: 5 });
      entries = result?.entries || [];
    } catch (err) {
      this._logger.error('[ArenaProgression] leaderboard error:', err);
    }
    if (!entries.length) {
      this._twitch.reply(channel, username, 'No arena battles yet — start one with !duel <name>!');
      return;
    }
    const list = entries.map((e, i) => {
      const name = e.character_name || e.display_name || e.twitch_username;
      const cls = e.class ? ` (${e.class} L${e.level})` : '';
      return `${i + 1}. ${name}${cls} ${e.wins}W/${e.losses}L`;
    }).join(' | ');
    this._twitch.say(channel, `🏆 Arena leaderboard: ${list}`);
  }

  /** Public for the QuestManager (announces first-roll characters). */
  announceNewCharacter(channel, username, sheet) {
    this._announceNewCharacter(channel, username, sheet);
  }

  _announceNewCharacter(channel, username, sheet) {
    const a = sheet.abilities;
    const msg =
      `🎲 ${mention(username)} rolls a new arena character: ${sheet.class}! ` +
      `STR ${a.str} DEX ${a.dex} CON ${a.con} INT ${a.int} WIS ${a.wis} CHA ${a.cha} — ${sheet.gold} starting gold.`;
    this._twitch.say(channel, msg);
    this._ticker('levelup', msg);
  }
}

module.exports = {
  ArenaProgression,
  CLASSES,
  ABILITIES,
  SHOP_CATALOG,
  MAX_POTIONS,
  xpForNextLevel,
  rollAbilityScore,
  abilityMod,
  XP_WIN,
  XP_LOSS,
  GOLD_WIN,
  GOLD_LOSS,
  STARTING_GOLD,
};
