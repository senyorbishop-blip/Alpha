'use strict';

/**
 * chat-bridge/src/arenaProgression.js — Arena character progression system.
 *
 * Owns the persistent arena_character objects used by the PvP arena:
 *   - 12 SRD classes with 4d6-drop-lowest ability rolls
 *   - Standard 5e-style derived stats (HP / AC / attack mod / damage die)
 *   - Config-driven XP curve, level cap, stat points, underdog bonuses
 *   - Gold payouts (streak bonus, first-duel-of-the-day bonus)
 *   - Item drops with pity counter, equip slots, daily rotating shop, rerolls
 *   - toJSON / fromJSON for the server arena_character shape
 *
 * ISOLATION: this module never talks to the game server directly — it only
 * calls the injected loadCharacter / syncCharacter functions, which index.js
 * wires to the arena-specific message types.
 *
 * All numeric tuning comes from config/arena-balance.json (passed in as
 * `balance`). All randomness goes through the injectable `rng` (defaults to
 * Math.random) so tests can make rolls deterministic.
 */

const STATS = ['str', 'dex', 'con', 'int', 'wis', 'cha'];
const SLOTS = ['weapon', 'armor', 'trinket'];
const BONUS_KEYS = ['str', 'dex', 'con', 'int', 'wis', 'cha', 'hp', 'atk', 'ac'];

const CLASSES = {
  Barbarian: { hitDie: 12, primary: 'str', emoji: '🪓', priorities: ['str', 'con', 'dex'] },
  Bard:      { hitDie: 8,  primary: 'cha', emoji: '🎵', priorities: ['cha', 'dex', 'con'] },
  Cleric:    { hitDie: 8,  primary: 'wis', emoji: '✨', priorities: ['wis', 'con', 'str'] },
  Druid:     { hitDie: 8,  primary: 'wis', emoji: '🌿', priorities: ['wis', 'con', 'dex'] },
  Fighter:   { hitDie: 10, primary: 'str', emoji: '⚔️', priorities: ['str', 'con', 'dex'] },
  Monk:      { hitDie: 8,  primary: 'dex', emoji: '👊', priorities: ['dex', 'wis', 'con'] },
  Paladin:   { hitDie: 10, primary: 'str', emoji: '🛡️', priorities: ['str', 'cha', 'con'] },
  Ranger:    { hitDie: 10, primary: 'dex', emoji: '🏹', priorities: ['dex', 'wis', 'con'] },
  Rogue:     { hitDie: 8,  primary: 'dex', emoji: '🗡️', priorities: ['dex', 'con', 'cha'] },
  Sorcerer:  { hitDie: 6,  primary: 'cha', emoji: '🔥', priorities: ['cha', 'con', 'dex'] },
  Warlock:   { hitDie: 8,  primary: 'cha', emoji: '🔮', priorities: ['cha', 'con', 'dex'] },
  Wizard:    { hitDie: 6,  primary: 'int', emoji: '📖', priorities: ['int', 'con', 'dex'] },
};

const CLASS_NAMES = Object.keys(CLASSES);
const MAX_ITEMS = 50;

// ── Small helpers ─────────────────────────────────────────────────────────────

function abilityModifier(score) {
  return Math.floor(((Number(score) || 0) - 10) / 2);
}

function todayString(now = new Date()) {
  return now.toISOString().slice(0, 10); // YYYY-MM-DD (UTC)
}

/** FNV-1a string hash → 32-bit unsigned int (for deterministic shop rotation). */
function hashString(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** Small deterministic PRNG (mulberry32). */
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function toInt(value, dflt, min, max) {
  const n = Math.floor(Number(value));
  if (!Number.isFinite(n)) return dflt;
  if (min !== undefined && n < min) return min;
  if (max !== undefined && n > max) return max;
  return n;
}

/** Human-readable bonus summary, e.g. "+2 DEX, +1 AC". */
function describeBonuses(item) {
  const parts = [];
  const bonuses = item?.bonuses ?? {};
  for (const key of BONUS_KEYS) {
    const v = Number(bonuses[key]) || 0;
    if (v !== 0) parts.push(`${v > 0 ? '+' : ''}${v} ${key.toUpperCase()}`);
  }
  if (item?.effect === 'second_wind') parts.push('Second Wind');
  return parts.join(', ');
}

// ── CharacterManager ──────────────────────────────────────────────────────────

class CharacterManager {
  /**
   * @param {object} opts
   * @param {object} [opts.balance={}]        — parsed config/arena-balance.json
   * @param {function} [opts.loadCharacter]   — async (username) → arena_character JSON | null
   * @param {function} [opts.syncCharacter]   — async (username, displayName, characterJson) → void
   * @param {function} [opts.announce]        — (message) → void  (chat announcement)
   * @param {object} [opts.logger]
   * @param {function} [opts.rng]             — () → [0,1), injectable for tests
   */
  constructor({ balance = {}, loadCharacter, syncCharacter, announce, logger, rng } = {}) {
    this._balance = balance || {};
    this._loadFn  = loadCharacter || (async () => null);
    this._syncFn  = syncCharacter || (async () => {});
    this._announce = announce || (() => {});
    this._logger  = logger || console;
    this._rng     = rng || Math.random;

    this._cache = new Map();          // username → character
    this._displayNames = new Map();   // username → last known display name
    this._pendingTimers = new Map();  // username → stat-point auto-assign timer
  }

  setAnnounce(fn) { this._announce = fn || (() => {}); }

  // ── Config accessors (with defaults matching arena-balance.json) ───────────

  _xp()     { return this._balance.xp ?? {}; }
  _gold()   { return this._balance.gold ?? {}; }
  _drops()  { return this._balance.drops ?? {}; }
  _shop()   { return this._balance.shop ?? {}; }
  _levelCap() { return toInt(this._xp().level_cap, 20, 1); }

  // ── Dice / creation ─────────────────────────────────────────────────────────

  _d(sides) { return Math.floor(this._rng() * sides) + 1; }

  /** 4d6, drop lowest. */
  rollAbilityScore() {
    const rolls = [this._d(6), this._d(6), this._d(6), this._d(6)];
    rolls.sort((a, b) => a - b);
    return rolls[1] + rolls[2] + rolls[3];
  }

  _randomClass() {
    return CLASS_NAMES[Math.floor(this._rng() * CLASS_NAMES.length)];
  }

  _rollAbilities() {
    const abilities = {};
    for (const stat of STATS) abilities[stat] = this.rollAbilityScore();
    return abilities;
  }

  /** Create (and cache) a brand-new random character for a user. */
  createCharacter(username) {
    const char = this.fromJSON({
      class: this._randomClass(),
      abilities: this._rollAbilities(),
      created_at: new Date().toISOString(),
    });
    this._cache.set(username, char);
    return char;
  }

  /** "@Dave you are... a WARLOCK! 🔮 STR 12 DEX 14 ..." */
  creationAnnouncement(char, displayName) {
    const info = CLASSES[char.class] ?? {};
    const a = char.abilities;
    const statLine = STATS.map(s => `${s.toUpperCase()} ${a[s]}`).join(' ');
    return `@${displayName} you are... a ${char.class.toUpperCase()}! ${info.emoji ?? ''} ${statLine} — HP ${this.maxHp(char)} AC ${this.armorClass(char)}. Fight with !duel <name>!`;
  }

  // ── Cache / persistence ─────────────────────────────────────────────────────

  getCached(username) { return this._cache.get(username) ?? null; }

  /** Load from cache, then server. Returns character or null. */
  async load(username) {
    if (this._cache.has(username)) return this._cache.get(username);
    try {
      const data = await this._loadFn(username);
      if (data) {
        const char = this.fromJSON(data);
        this._cache.set(username, char);
        return char;
      }
    } catch (err) {
      this._logger.error('[CharacterManager] load error:', err);
    }
    return null;
  }

  /** Load-or-create. Returns { character, created }. */
  async getOrCreate(username, displayName) {
    if (displayName) this._displayNames.set(username, displayName);
    const existing = await this.load(username);
    if (existing) return { character: existing, created: false };
    const character = this.createCharacter(username);
    await this.sync(username, displayName);
    return { character, created: true };
  }

  /** Push the cached character to the server via the injected sync function. */
  async sync(username, displayName) {
    const char = this._cache.get(username);
    if (!char) return;
    if (displayName) this._displayNames.set(username, displayName);
    try {
      await this._syncFn(username, this._displayNames.get(username) ?? username, this.toJSON(char));
    } catch (err) {
      this._logger.error('[CharacterManager] sync error:', err);
    }
  }

  // ── Derived stats (5e math; equipped item bonuses included) ────────────────

  classInfo(char) { return CLASSES[char.class] ?? CLASSES.Fighter; }

  _equippedBonus(char, key) {
    let total = 0;
    for (const slot of SLOTS) {
      const item = char.equipped?.[slot];
      if (item && item.bonuses) total += Number(item.bonuses[key]) || 0;
    }
    return total;
  }

  /** Effective ability score = base + equipped item bonuses. */
  abilityScore(char, stat) {
    return (Number(char.abilities?.[stat]) || 10) + this._equippedBonus(char, stat);
  }

  /** Attack mod = primary stat mod + level bonus (+1 per 4 levels) + equipped atk. */
  attackMod(char) {
    const primary = this.classInfo(char).primary;
    return abilityModifier(this.abilityScore(char, primary))
      + Math.floor(char.level / 4)
      + this._equippedBonus(char, 'atk');
  }

  /** Max HP = hit die max + CON mod at L1, + (hit die avg + CON mod) per level; min 1/level. */
  maxHp(char) {
    const hitDie = this.classInfo(char).hitDie;
    const conMod = abilityModifier(this.abilityScore(char, 'con'));
    let hp = Math.max(1, hitDie + conMod);
    const perLevel = Math.max(1, Math.floor(hitDie / 2) + 1 + conMod);
    hp += perLevel * (char.level - 1);
    return hp + this._equippedBonus(char, 'hp');
  }

  /** AC = 10 + DEX mod (capped at +5) + equipped ac bonuses. */
  armorClass(char) {
    const dexMod = Math.min(5, abilityModifier(this.abilityScore(char, 'dex')));
    return 10 + dexMod + this._equippedBonus(char, 'ac');
  }

  /** Damage die by class hit-die family (see damage_die_by_hit_die in balance config). */
  damageDie(char) {
    const map = this._balance.damage_die_by_hit_die ?? { 12: 8, 10: 8, 8: 6, 6: 10 };
    return toInt(map[String(this.classInfo(char).hitDie)], 6, 2);
  }

  /** True if an equipped item grants the second_wind effect. */
  hasSecondWind(char) {
    return SLOTS.some(slot => char.equipped?.[slot]?.effect === 'second_wind');
  }

  // ── XP & leveling ───────────────────────────────────────────────────────────

  /** XP needed to advance FROM the given level to the next. */
  xpToNext(level) {
    const base = Number(this._xp().curve_base ?? 100);
    const exponent = Number(this._xp().curve_exponent ?? 1.5);
    return Math.round(base * Math.pow(level, exponent));
  }

  /**
   * Grant XP; handles level-ups and stat-point grants.
   * Returns { levels: [newLevels], statPointsGranted, announcements: [] }.
   */
  grantXp(char, amount, { username, displayName } = {}) {
    const name = displayName ?? username ?? 'Fighter';
    const result = { levels: [], statPointsGranted: 0, announcements: [] };
    char.xp += Math.max(0, toInt(amount, 0, 0));

    const cap = this._levelCap();
    const every = toInt(this._balance.levelup?.stat_point_every_n_levels, 2, 0);
    while (char.level < cap && char.xp >= this.xpToNext(char.level)) {
      char.xp -= this.xpToNext(char.level);
      char.level++;
      result.levels.push(char.level);
      result.announcements.push(`🎉 ${name} the ${char.class} reached level ${char.level}!`);
      if (every > 0 && char.level % every === 0) {
        char.pending_stat_points++;
        result.statPointsGranted++;
      }
    }

    if (result.statPointsGranted > 0) {
      const windowSec = this._choiceWindowSeconds();
      if (windowSec > 0) {
        result.announcements.push(
          `${name} gains +${result.statPointsGranted} stat point! Type !levelup <str|dex|con|int|wis|cha> within ${windowSec}s or it auto-assigns.`
        );
        if (username) this._scheduleAutoAssign(username);
      } else {
        const assigned = this.autoAssignPending(char);
        result.announcements.push(`${name} auto-assigned: ${assigned.map(s => `+1 ${s.toUpperCase()}`).join(', ')}.`);
      }
    }
    return result;
  }

  _choiceWindowSeconds() {
    return toInt(this._balance.levelup?.choice_window_seconds, 60, 0);
  }

  /** Auto-assign all pending stat points along class priorities. Returns assigned stats. */
  autoAssignPending(char) {
    const priorities = this.classInfo(char).priorities ?? STATS;
    const assigned = [];
    let i = 0;
    while (char.pending_stat_points > 0) {
      const stat = priorities[i % priorities.length];
      char.abilities[stat]++;
      char.pending_stat_points--;
      assigned.push(stat);
      i++;
    }
    return assigned;
  }

  _scheduleAutoAssign(username) {
    this.cancelPendingTimer(username);
    const windowMs = this._choiceWindowSeconds() * 1000;
    const timer = setTimeout(() => {
      this._pendingTimers.delete(username);
      const char = this._cache.get(username);
      if (!char || char.pending_stat_points <= 0) return;
      const assigned = this.autoAssignPending(char);
      const name = this._displayNames.get(username) ?? username;
      try {
        this._announce(`⏫ ${name} auto-assigned: ${assigned.map(s => `+1 ${s.toUpperCase()}`).join(', ')}.`);
      } catch (_) { /* announce is best-effort */ }
      this.sync(username).catch(err => this._logger.error('[CharacterManager] auto-assign sync error:', err));
    }, windowMs);
    if (typeof timer.unref === 'function') timer.unref();
    this._pendingTimers.set(username, timer);
  }

  cancelPendingTimer(username) {
    const timer = this._pendingTimers.get(username);
    if (timer) {
      clearTimeout(timer);
      this._pendingTimers.delete(username);
    }
  }

  /** User-chosen stat point via !levelup <stat>. */
  applyStatChoice(char, statRaw, username) {
    if (!char || char.pending_stat_points <= 0) return { success: false, error: 'no_points' };
    const stat = String(statRaw ?? '').trim().toLowerCase();
    if (!STATS.includes(stat)) return { success: false, error: 'invalid_stat' };
    char.abilities[stat]++;
    char.pending_stat_points--;
    if (username && char.pending_stat_points <= 0) this.cancelPendingTimer(username);
    return { success: true, stat, value: char.abilities[stat] };
  }

  // ── Underdog ────────────────────────────────────────────────────────────────

  underdogGap() { return toInt(this._xp().underdog_level_gap, 3, 1); }
  underdogAttackBonus() { return toInt(this._xp().underdog_attack_bonus, 2, 0); }

  /** True if charA is the underdog against charB (level gap ≥ threshold). */
  isUnderdog(charA, charB) {
    if (!charA || !charB) return false;
    return charB.level - charA.level >= this.underdogGap();
  }

  // ── Duel outcome bookkeeping ────────────────────────────────────────────────

  /**
   * Record a duel win: gold (base + streak + first-of-day), XP (+ underdog), level-ups.
   * Returns { xp, gold, streakBonus, firstOfDay, levels, statPointsGranted, announcements }.
   */
  recordWin(char, { username, displayName, underdog = false } = {}) {
    const g = this._gold();
    const x = this._xp();
    const today = todayString();

    char.wins++;
    char.streak++;
    char.best_streak = Math.max(char.best_streak, char.streak);

    let gold = toInt(g.win, 25, 0);
    const streakBonus = Math.min(
      (char.streak - 1) * toInt(g.streak_bonus, 5, 0),
      toInt(g.streak_cap, 25, 0)
    );
    gold += streakBonus;

    const firstOfDay = char.last_duel_day !== today;
    if (firstOfDay) gold += toInt(g.first_of_day, 20, 0);
    char.last_duel_day = today;
    char.gold += gold;

    const xp = toInt(x.win, 50, 0) + (underdog ? toInt(x.underdog_xp_bonus, 25, 0) : 0);
    const levelRes = this.grantXp(char, xp, { username, displayName });

    return { xp, gold, streakBonus, firstOfDay, ...levelRes };
  }

  /** Record a duel loss: consolation gold + XP, streak reset. */
  recordLoss(char, { username, displayName } = {}) {
    const g = this._gold();
    const today = todayString();

    char.losses++;
    char.streak = 0;

    let gold = toInt(g.loss, 5, 0);
    const firstOfDay = char.last_duel_day !== today;
    if (firstOfDay) gold += toInt(g.first_of_day, 20, 0);
    char.last_duel_day = today;
    char.gold += gold;

    const xp = toInt(this._xp().loss, 15, 0);
    const levelRes = this.grantXp(char, xp, { username, displayName });

    return { xp, gold, streakBonus: 0, firstOfDay, ...levelRes };
  }

  // ── Item drops ──────────────────────────────────────────────────────────────

  /**
   * Roll a post-win item drop (call only for the winner).
   * Handles the pity counter: guaranteed rare+ every N wins.
   * Returns the dropped item (already added to the bag) or null.
   */
  rollDrop(char) {
    const d = this._drops();
    const chance = d.chance ?? {};
    char.wins_since_rare = (char.wins_since_rare || 0) + 1;

    const pityN = toInt(d.pity_rare_every_n_wins, 10, 0);
    let rarity = null;

    if (pityN > 0 && char.wins_since_rare >= pityN) {
      // Pity: force rare+; small chance to upgrade to legendary.
      rarity = this._rng() < Number(chance.legendary ?? 0.01) * 5 ? 'legendary' : 'rare';
    } else {
      const r = this._rng();
      let acc = 0;
      for (const rar of ['legendary', 'rare', 'uncommon', 'common']) {
        acc += Number(chance[rar] ?? 0);
        if (r < acc) { rarity = rar; break; }
      }
    }

    if (!rarity) return null;
    if (rarity === 'rare' || rarity === 'legendary') char.wins_since_rare = 0;

    const table = Array.isArray(d.tables?.[rarity]) ? d.tables[rarity] : [];
    if (table.length === 0) return null;
    const template = table[Math.floor(this._rng() * table.length)];
    const item = this._normalizeItem({ ...template, rarity });
    this.addItem(char, item);
    return item;
  }

  addItem(char, item) {
    if (!Array.isArray(char.items)) char.items = [];
    if (char.items.length >= MAX_ITEMS) char.items.shift(); // drop oldest to make room
    char.items.push(item);
  }

  // ── Equipment ───────────────────────────────────────────────────────────────

  /**
   * Equip an item from the bag by fuzzy name. One item per slot.
   * Returns { success, item?, slot?, error?: 'not_found'|'wrong_item' }.
   */
  equip(char, query) {
    const q = String(query ?? '').trim().toLowerCase();
    if (!q) return { success: false, error: 'not_found' };
    const items = Array.isArray(char.items) ? char.items : [];
    const item = items.find(i => i.name.toLowerCase() === q)
      ?? items.find(i => i.name.toLowerCase().includes(q));
    if (!item) return { success: false, error: 'not_found' };
    if (!SLOTS.includes(item.slot)) return { success: false, error: 'wrong_item', item };
    char.equipped[item.slot] = item;
    return { success: true, item, slot: item.slot };
  }

  // ── Shop ────────────────────────────────────────────────────────────────────

  /**
   * Daily rotating shop selection, deterministic for a given day string.
   * Premium items (premium: true) are always included.
   */
  getShopRotation(dayStr = todayString()) {
    const shop = this._shop();
    const stock = Array.isArray(shop.stock) ? shop.stock : [];
    const size = toInt(shop.rotation_size, 4, 1);
    const premium = stock.filter(i => i.premium);
    const regular = stock.filter(i => !i.premium);

    const rand = mulberry32(hashString(String(dayStr)));
    const shuffled = [...regular];
    for (let i = shuffled.length - 1; i > 0; i--) {
      const j = Math.floor(rand() * (i + 1));
      [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
    }
    return [...shuffled.slice(0, Math.max(0, size - premium.length)), ...premium];
  }

  /**
   * Buy from today's rotation by 1-based number or fuzzy name.
   * Returns { success, item?, price?, gold?, error?: 'not_found'|'insufficient_gold' }.
   */
  buyItem(char, query, dayStr = todayString()) {
    const rotation = this.getShopRotation(dayStr);
    const raw = String(query ?? '').trim();
    let entry = null;

    if (/^\d+$/.test(raw)) {
      const idx = parseInt(raw, 10) - 1;
      if (idx >= 0 && idx < rotation.length) entry = rotation[idx];
    } else if (raw) {
      const q = raw.toLowerCase();
      entry = rotation.find(i => i.name.toLowerCase() === q)
        ?? rotation.find(i => i.name.toLowerCase().includes(q));
    }

    if (!entry) return { success: false, error: 'not_found' };
    const price = toInt(entry.price, 0, 0);
    if (char.gold < price) {
      return { success: false, error: 'insufficient_gold', item: entry.name, price, gold: char.gold };
    }

    char.gold -= price;
    const { price: _price, premium: _premium, ...itemData } = entry;
    const item = this._normalizeItem(itemData);
    this.addItem(char, item);
    return { success: true, item, price, gold: char.gold };
  }

  // ── Reroll ──────────────────────────────────────────────────────────────────

  /**
   * Reroll class AND stats per config policy ('free_once' | 'purchase' | 'off').
   * Returns { success, announcement? , reason? }.
   */
  reroll(char, displayName) {
    const cfg = this._balance.reroll ?? {};
    const policy = cfg.policy ?? 'free_once';

    if (policy === 'off') return { success: false, reason: 'Rerolls are disabled.' };
    if (policy === 'free_once' && char.rerolled) {
      return { success: false, reason: 'You already used your free reroll.' };
    }
    if (policy === 'purchase') {
      const price = toInt(cfg.price, 100, 0);
      if (char.gold < price) {
        return { success: false, reason: `A reroll costs ${price}g — you have ${char.gold}g.` };
      }
      char.gold -= price;
    }

    char.class = this._randomClass();
    char.abilities = this._rollAbilities();
    char.rerolled = true;
    return { success: true, announcement: this.creationAnnouncement(char, displayName) };
  }

  // ── Serialization ───────────────────────────────────────────────────────────

  _normalizeItem(raw) {
    const src = (raw && typeof raw === 'object') ? raw : {};
    const bonuses = {};
    if (src.bonuses && typeof src.bonuses === 'object') {
      for (const key of BONUS_KEYS) {
        const v = Number(src.bonuses[key]);
        if (Number.isFinite(v) && v !== 0) bonuses[key] = Math.floor(v);
      }
    }
    const item = {
      name: String(src.name ?? 'Mystery Item').slice(0, 80),
      slot: SLOTS.includes(src.slot) ? src.slot : 'trinket',
      rarity: typeof src.rarity === 'string' ? src.rarity : 'common',
      bonuses,
    };
    if (src.effect === 'second_wind') item.effect = 'second_wind';
    if (typeof src.flair === 'string' && src.flair) item.flair = String(src.flair).slice(0, 120);
    return item;
  }

  /** Build a character from (possibly missing/malformed) JSON. Never throws. */
  fromJSON(data) {
    const src = (data && typeof data === 'object') ? data : {};
    const abilities = {};
    const srcAbilities = (src.abilities && typeof src.abilities === 'object') ? src.abilities : {};
    for (const stat of STATS) abilities[stat] = toInt(srcAbilities[stat], 10, 1, 30);

    const items = (Array.isArray(src.items) ? src.items : [])
      .filter(i => i && typeof i === 'object' && i.name)
      .slice(0, MAX_ITEMS)
      .map(i => this._normalizeItem(i));

    const equipped = { weapon: null, armor: null, trinket: null };
    const srcEquipped = (src.equipped && typeof src.equipped === 'object') ? src.equipped : {};
    for (const slot of SLOTS) {
      const it = srcEquipped[slot];
      if (it && typeof it === 'object' && it.name) equipped[slot] = this._normalizeItem(it);
    }

    return {
      class: CLASSES[src.class] ? src.class : 'Fighter',
      level: toInt(src.level, 1, 1, this._levelCap()),
      xp: toInt(src.xp, 0, 0),
      gold: toInt(src.gold, 0, 0),
      abilities,
      wins: toInt(src.wins, 0, 0),
      losses: toInt(src.losses, 0, 0),
      streak: toInt(src.streak, 0, 0),
      best_streak: toInt(src.best_streak, 0, 0),
      last_duel_day: typeof src.last_duel_day === 'string' ? src.last_duel_day : '',
      wins_since_rare: toInt(src.wins_since_rare, 0, 0),
      rerolled: !!src.rerolled,
      items,
      equipped,
      pending_stat_points: toInt(src.pending_stat_points, 0, 0),
      created_at: typeof src.created_at === 'string' ? src.created_at : new Date().toISOString(),
    };
  }

  /** Serialize to the arena_character wire shape. */
  toJSON(char) {
    return JSON.parse(JSON.stringify({
      class: char.class,
      level: char.level,
      xp: char.xp,
      gold: char.gold,
      abilities: char.abilities,
      wins: char.wins,
      losses: char.losses,
      streak: char.streak,
      best_streak: char.best_streak,
      last_duel_day: char.last_duel_day,
      wins_since_rare: char.wins_since_rare,
      rerolled: char.rerolled,
      items: char.items,
      equipped: char.equipped,
      pending_stat_points: char.pending_stat_points,
      created_at: char.created_at,
    }));
  }

  destroy() {
    for (const timer of this._pendingTimers.values()) clearTimeout(timer);
    this._pendingTimers.clear();
  }
}

module.exports = {
  CharacterManager,
  CLASSES,
  STATS,
  SLOTS,
  abilityModifier,
  describeBonuses,
  todayString,
};
