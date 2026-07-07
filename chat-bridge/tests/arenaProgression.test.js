'use strict';

const {
  CharacterManager,
  CLASSES,
  STATS,
  abilityModifier,
  describeBonuses,
  todayString,
} = require('../src/arenaProgression');

// ── Helpers ──────────────────────────────────────────────────────────────────

const BALANCE = {
  xp: {
    win: 50, loss: 15,
    curve_base: 100, curve_exponent: 1.5,
    level_cap: 20,
    underdog_level_gap: 3, underdog_attack_bonus: 2, underdog_xp_bonus: 25,
  },
  gold: { win: 25, loss: 5, streak_bonus: 5, streak_cap: 25, first_of_day: 20 },
  levelup: { stat_point_every_n_levels: 2, choice_window_seconds: 60 },
  drops: {
    chance: { common: 0.25, uncommon: 0.12, rare: 0.05, legendary: 0.01 },
    pity_rare_every_n_wins: 5,
    tables: {
      common:    [{ name: 'Rusty Dagger', slot: 'weapon', bonuses: { atk: 1 } }],
      uncommon:  [{ name: 'Cloak of the Cat', slot: 'trinket', bonuses: { dex: 2 } }],
      rare:      [{ name: 'Flaming Blade', slot: 'weapon', bonuses: { atk: 3 } }],
      legendary: [{ name: 'Phoenix Feather Charm', slot: 'trinket', bonuses: { hp: 5 }, effect: 'second_wind' }],
    },
  },
  reroll: { policy: 'free_once', price: 100 },
  shop: {
    rotation_size: 3,
    stock: [
      { name: 'Iron Sword', slot: 'weapon', bonuses: { atk: 1 }, price: 60 },
      { name: 'Padded Vest', slot: 'armor', bonuses: { ac: 1 }, price: 60 },
      { name: 'Health Charm', slot: 'trinket', bonuses: { hp: 4 }, price: 50 },
      { name: 'Boots of Haste', slot: 'trinket', bonuses: { dex: 2 }, price: 90 },
      { name: 'Dragonscale Mail', slot: 'armor', bonuses: { ac: 3, con: 2 }, price: 400, premium: true },
    ],
  },
  duel: { interactive_mode: false, wagers_enabled: true, max_wager: 100 },
  fallback: { hp: 20, ac: 12, damage_die: 6 },
  damage_die_by_hit_die: { 12: 8, 10: 8, 8: 6, 6: 10 },
};

function makeManager(opts = {}) {
  return new CharacterManager({
    balance: BALANCE,
    logger: { info: () => {}, warn: () => {}, error: () => {} },
    ...opts,
  });
}

/** rng that cycles through a fixed sequence of values. */
function seqRng(values) {
  let i = 0;
  return () => values[i++ % values.length];
}

/** Build a character with explicit fields on top of clean defaults. */
function makeChar(mgr, overrides = {}) {
  return mgr.fromJSON({
    class: 'Fighter',
    level: 1,
    abilities: { str: 16, dex: 14, con: 14, int: 10, wis: 10, cha: 10 },
    ...overrides,
  });
}

// ── Character creation ───────────────────────────────────────────────────────

describe('CharacterManager — character creation', () => {
  test('creates a valid class and six ability scores in 4d6-drop-lowest bounds', () => {
    const mgr = makeManager();
    for (let i = 0; i < 25; i++) {
      const char = mgr.createCharacter(`user${i}`);
      expect(Object.keys(CLASSES)).toContain(char.class);
      for (const stat of STATS) {
        expect(char.abilities[stat]).toBeGreaterThanOrEqual(3);
        expect(char.abilities[stat]).toBeLessThanOrEqual(18);
      }
    }
  });

  test('4d6-drop-lowest hits the extremes with rigged rng', () => {
    const low = makeManager({ rng: () => 0 });         // all 1s → 3
    expect(low.rollAbilityScore()).toBe(3);
    const high = makeManager({ rng: () => 0.999999 }); // all 6s → 18
    expect(high.rollAbilityScore()).toBe(18);
  });

  test('creation announcement includes class and all six stats', () => {
    const mgr = makeManager();
    const char = mgr.createCharacter('dave');
    const msg = mgr.creationAnnouncement(char, 'Dave');
    expect(msg).toContain('@Dave');
    expect(msg).toContain(char.class.toUpperCase());
    for (const stat of STATS) expect(msg).toContain(stat.toUpperCase());
    expect(msg.length).toBeLessThan(450);
  });
});

// ── Modifier / derived-stat math ─────────────────────────────────────────────

describe('CharacterManager — modifiers and derived stats', () => {
  test('ability modifier is floor((score - 10) / 2)', () => {
    expect(abilityModifier(3)).toBe(-4);
    expect(abilityModifier(8)).toBe(-1);
    expect(abilityModifier(9)).toBe(-1);
    expect(abilityModifier(10)).toBe(0);
    expect(abilityModifier(11)).toBe(0);
    expect(abilityModifier(15)).toBe(2);
    expect(abilityModifier(18)).toBe(4);
    expect(abilityModifier(20)).toBe(5);
  });

  test('max HP: hit die max + CON mod at L1, + (avg + CON mod) per level', () => {
    const mgr = makeManager();
    // Fighter d10, CON 14 (+2): L1 = 10 + 2 = 12
    const l1 = makeChar(mgr);
    expect(mgr.maxHp(l1)).toBe(12);
    // L4 = 12 + 3 * (6 + 2) = 36
    const l4 = makeChar(mgr, { level: 4 });
    expect(mgr.maxHp(l4)).toBe(36);
  });

  test('max HP gains a minimum of 1 per level even with terrible CON', () => {
    const mgr = makeManager();
    // Wizard d6, CON 3 (-4): avg (4) + (-4) = 0 → clamped to 1/level
    const char = makeChar(mgr, { class: 'Wizard', level: 3, abilities: { str: 10, dex: 10, con: 3, int: 16, wis: 10, cha: 10 } });
    expect(mgr.maxHp(char)).toBe(Math.max(1, 6 - 4) + 2 * 1); // 2 + 2 = 4
  });

  test('AC = 10 + DEX mod capped at +5, plus equipped armor', () => {
    const mgr = makeManager();
    const char = makeChar(mgr); // DEX 14 → +2
    expect(mgr.armorClass(char)).toBe(12);
    const speedy = makeChar(mgr, { abilities: { str: 10, dex: 30, con: 10, int: 10, wis: 10, cha: 10 } });
    expect(mgr.armorClass(speedy)).toBe(15); // DEX +10 capped at +5
  });

  test('attack mod = primary mod + level/4 + equipped atk', () => {
    const mgr = makeManager();
    // Fighter STR 16 (+3), level 8 (+2)
    const char = makeChar(mgr, { level: 8 });
    expect(mgr.attackMod(char)).toBe(5);
  });

  test('equipped item bonuses flow through HP/AC/attack; unequipped items do not', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, {
      items: [
        { name: 'Flaming Blade', slot: 'weapon', bonuses: { atk: 3 } },
        { name: 'Guardian Plate', slot: 'armor', bonuses: { ac: 3, con: 2 } },
        { name: 'Lucky Pebble', slot: 'trinket', bonuses: { hp: 2 } },
      ],
    });
    const baseHp = mgr.maxHp(char);
    const baseAc = mgr.armorClass(char);
    const baseAtk = mgr.attackMod(char);

    // Nothing equipped yet — items in the bag change nothing.
    expect(mgr.maxHp(char)).toBe(baseHp);

    expect(mgr.equip(char, 'flaming').success).toBe(true);
    expect(mgr.attackMod(char)).toBe(baseAtk + 3);

    expect(mgr.equip(char, 'guardian plate').success).toBe(true);
    expect(mgr.armorClass(char)).toBe(baseAc + 3);
    // +2 CON raises CON 14→16, +1 mod → +1 HP per level (1 level here)
    expect(mgr.maxHp(char)).toBe(baseHp + 1);

    expect(mgr.equip(char, 'pebble').success).toBe(true);
    expect(mgr.maxHp(char)).toBe(baseHp + 1 + 2);
  });

  test('damage die follows the hit-die family map', () => {
    const mgr = makeManager();
    expect(mgr.damageDie(makeChar(mgr, { class: 'Barbarian' }))).toBe(8);
    expect(mgr.damageDie(makeChar(mgr, { class: 'Fighter' }))).toBe(8);
    expect(mgr.damageDie(makeChar(mgr, { class: 'Rogue' }))).toBe(6);
    expect(mgr.damageDie(makeChar(mgr, { class: 'Wizard' }))).toBe(10); // spell blast
  });
});

// ── XP curve & level-ups ─────────────────────────────────────────────────────

describe('CharacterManager — XP and leveling', () => {
  test('xpToNext follows base * level^exponent', () => {
    const mgr = makeManager();
    expect(mgr.xpToNext(1)).toBe(100);
    expect(mgr.xpToNext(4)).toBe(800);
  });

  test('grantXp levels up, carries XP, and announces', () => {
    const mgr = makeManager();
    const char = makeChar(mgr);
    const res = mgr.grantXp(char, 120, { displayName: 'Grognak' });
    expect(char.level).toBe(2);
    expect(char.xp).toBe(20);
    expect(res.levels).toEqual([2]);
    expect(res.announcements.some(a => a.includes('Grognak the Fighter reached level 2'))).toBe(true);
  });

  test('stat point granted every N levels and auto-assigns to class priority', () => {
    const mgr = makeManager();
    const char = makeChar(mgr);
    mgr.grantXp(char, 100); // → level 2 (every 2 levels → 1 point)
    expect(char.pending_stat_points).toBe(1);

    const before = char.abilities.str;
    const assigned = mgr.autoAssignPending(char);
    expect(assigned).toEqual(['str']); // Fighter priority: str first
    expect(char.abilities.str).toBe(before + 1);
    expect(char.pending_stat_points).toBe(0);
  });

  test('user can choose the stat with !levelup; invalid stats rejected', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, { pending_stat_points: 1 });
    expect(mgr.applyStatChoice(char, 'luck').error).toBe('invalid_stat');
    const res = mgr.applyStatChoice(char, 'CHA');
    expect(res.success).toBe(true);
    expect(char.abilities.cha).toBe(11);
    expect(char.pending_stat_points).toBe(0);
    expect(mgr.applyStatChoice(char, 'cha').error).toBe('no_points');
  });

  test('level cap stops level-ups', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, { level: 20 });
    mgr.grantXp(char, 1000000);
    expect(char.level).toBe(20);
  });
});

// ── Underdog ─────────────────────────────────────────────────────────────────

describe('CharacterManager — underdog bonus', () => {
  test('underdog detected at the configured level gap', () => {
    const mgr = makeManager();
    const low = makeChar(mgr, { level: 2 });
    const high = makeChar(mgr, { level: 5 });
    expect(mgr.isUnderdog(low, high)).toBe(true);
    expect(mgr.isUnderdog(high, low)).toBe(false);
    const close = makeChar(mgr, { level: 4 });
    expect(mgr.isUnderdog(close, high)).toBe(false);
  });

  test('underdog win grants bonus XP', () => {
    const mgr = makeManager({ rng: () => 0.999 }); // no drops interference
    const a = makeChar(mgr);
    const b = makeChar(mgr);
    const normal = mgr.recordWin(a, {});
    const boosted = mgr.recordWin(b, { underdog: true });
    expect(boosted.xp).toBe(normal.xp + 25);
    expect(mgr.underdogAttackBonus()).toBe(2);
  });
});

// ── Gold payouts ─────────────────────────────────────────────────────────────

describe('CharacterManager — gold payouts', () => {
  test('win pays base + first-of-day; streak bonus grows and caps', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, { last_duel_day: todayString() }); // not first of day
    const first = mgr.recordWin(char, {});
    expect(first.gold).toBe(25);          // streak 1 → no streak bonus
    expect(first.firstOfDay).toBe(false);

    const second = mgr.recordWin(char, {});
    expect(second.streakBonus).toBe(5);   // streak 2
    expect(second.gold).toBe(30);

    char.streak = 10; // next win → streak 11 → 50, capped at 25
    const capped = mgr.recordWin(char, {});
    expect(capped.streakBonus).toBe(25);
    expect(capped.gold).toBe(50);
  });

  test('first duel of the day pays the bonus and stamps the day', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, { last_duel_day: '2020-01-01' });
    const res = mgr.recordWin(char, {});
    expect(res.firstOfDay).toBe(true);
    expect(res.gold).toBe(45); // 25 + 20
    expect(char.last_duel_day).toBe(todayString());
  });

  test('loss pays consolation gold and resets the streak', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, { streak: 4, last_duel_day: todayString() });
    const res = mgr.recordLoss(char, {});
    expect(res.gold).toBe(5);
    expect(res.xp).toBe(15);
    expect(char.streak).toBe(0);
    expect(char.losses).toBe(1);
  });
});

// ── Drops & pity ─────────────────────────────────────────────────────────────

describe('CharacterManager — item drops', () => {
  test('pity counter guarantees a rare+ drop within N wins', () => {
    // rng 0.999 → never rolls a natural drop, so only pity can fire.
    const mgr = makeManager({ rng: () => 0.999 });
    const char = makeChar(mgr);
    const drops = [];
    for (let i = 0; i < 5; i++) {
      const d = mgr.rollDrop(char);
      if (d) drops.push(d);
    }
    expect(drops.length).toBe(1);
    expect(['rare', 'legendary']).toContain(drops[0].rarity);
    expect(char.wins_since_rare).toBe(0); // reset after pity drop
  });

  test('natural legendary roll adds the item with its effect', () => {
    const mgr = makeManager({ rng: () => 0.005 }); // < 0.01 → legendary
    const char = makeChar(mgr);
    const drop = mgr.rollDrop(char);
    expect(drop.rarity).toBe('legendary');
    expect(drop.effect).toBe('second_wind');
    expect(char.items).toContainEqual(drop);
  });

  test('no drop on a high roll (outside all chance bands)', () => {
    const mgr = makeManager({ rng: () => 0.9 });
    const char = makeChar(mgr);
    expect(mgr.rollDrop(char)).toBeNull();
    expect(char.wins_since_rare).toBe(1);
  });
});

// ── Shop ─────────────────────────────────────────────────────────────────────

describe('CharacterManager — shop', () => {
  test('rotation is deterministic per day and includes the premium item', () => {
    const mgr = makeManager();
    const a = mgr.getShopRotation('2026-07-07');
    const b = mgr.getShopRotation('2026-07-07');
    expect(a.map(i => i.name)).toEqual(b.map(i => i.name));
    expect(a.length).toBe(3);
    expect(a.some(i => i.name === 'Dragonscale Mail')).toBe(true);
  });

  test('different days can produce different rotations (seeded by day string)', () => {
    const mgr = makeManager();
    const days = ['2026-07-01', '2026-07-02', '2026-07-03', '2026-07-04', '2026-07-05'];
    const rotations = days.map(d => mgr.getShopRotation(d).map(i => i.name).join(','));
    expect(new Set(rotations).size).toBeGreaterThan(1);
  });

  test('buy by 1-based number deducts gold and adds the item', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, { gold: 500 });
    const rotation = mgr.getShopRotation('2026-07-07');
    const res = mgr.buyItem(char, '1', '2026-07-07');
    expect(res.success).toBe(true);
    expect(res.item.name).toBe(rotation[0].name);
    expect(char.gold).toBe(500 - rotation[0].price);
    expect(char.items.some(i => i.name === rotation[0].name)).toBe(true);
    expect(res.item.price).toBeUndefined(); // price stripped from owned item
  });

  test('buy by fuzzy name works', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, { gold: 1000 });
    const res = mgr.buyItem(char, 'dragonscale', '2026-07-07');
    expect(res.success).toBe(true);
    expect(res.item.name).toBe('Dragonscale Mail');
  });

  test('insufficient gold is rejected without deduction', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, { gold: 10 });
    const res = mgr.buyItem(char, 'dragonscale', '2026-07-07');
    expect(res.success).toBe(false);
    expect(res.error).toBe('insufficient_gold');
    expect(char.gold).toBe(10);
    expect(char.items).toHaveLength(0);
  });

  test('unknown item is not_found', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, { gold: 1000 });
    expect(mgr.buyItem(char, 'bazooka', '2026-07-07').error).toBe('not_found');
    expect(mgr.buyItem(char, '99', '2026-07-07').error).toBe('not_found');
  });
});

// ── Equip ────────────────────────────────────────────────────────────────────

describe('CharacterManager — equip slot exclusivity', () => {
  test('equipping a second weapon replaces the first (one item per slot)', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, {
      items: [
        { name: 'Rusty Dagger', slot: 'weapon', bonuses: { atk: 1 } },
        { name: 'Steel Longsword', slot: 'weapon', bonuses: { atk: 2 } },
      ],
    });
    expect(mgr.equip(char, 'rusty dagger').success).toBe(true);
    expect(char.equipped.weapon.name).toBe('Rusty Dagger');
    expect(mgr.equip(char, 'longsword').success).toBe(true);
    expect(char.equipped.weapon.name).toBe('Steel Longsword');
    expect(mgr.attackMod(char)).toBe(3 + 2); // STR +3, weapon +2 (not +3)
  });

  test('equipping a missing item fails', () => {
    const mgr = makeManager();
    const char = makeChar(mgr);
    expect(mgr.equip(char, 'excalibur').error).toBe('not_found');
  });
});

// ── Reroll ───────────────────────────────────────────────────────────────────

describe('CharacterManager — reroll', () => {
  test('free_once allows exactly one reroll', () => {
    const mgr = makeManager();
    const char = makeChar(mgr);
    const first = mgr.reroll(char, 'Dave');
    expect(first.success).toBe(true);
    expect(first.announcement).toContain('@Dave');
    expect(char.rerolled).toBe(true);
    expect(mgr.reroll(char, 'Dave').success).toBe(false);
  });

  test('purchase policy charges gold; off policy denies', () => {
    const buyMgr = makeManager({ balance: { ...BALANCE, reroll: { policy: 'purchase', price: 100 } } });
    const rich = makeChar(buyMgr, { gold: 150 });
    expect(buyMgr.reroll(rich, 'Rich').success).toBe(true);
    expect(rich.gold).toBe(50);
    const poor = makeChar(buyMgr, { gold: 10 });
    expect(buyMgr.reroll(poor, 'Poor').success).toBe(false);

    const offMgr = makeManager({ balance: { ...BALANCE, reroll: { policy: 'off' } } });
    expect(offMgr.reroll(makeChar(offMgr), 'X').success).toBe(false);
  });
});

// ── Serialization ────────────────────────────────────────────────────────────

describe('CharacterManager — serialization', () => {
  test('toJSON/fromJSON round-trips the full shape', () => {
    const mgr = makeManager();
    const char = makeChar(mgr, {
      level: 5, xp: 42, gold: 77, wins: 9, losses: 3, streak: 2, best_streak: 4,
      last_duel_day: '2026-07-06', wins_since_rare: 3, rerolled: true,
      pending_stat_points: 1, created_at: '2026-01-01T00:00:00.000Z',
      items: [{ name: 'Flaming Blade', slot: 'weapon', rarity: 'rare', bonuses: { atk: 3 }, flair: 'wreathed in fire' }],
    });
    mgr.equip(char, 'flaming blade');
    const json = JSON.parse(JSON.stringify(mgr.toJSON(char)));
    const restored = mgr.fromJSON(json);
    expect(mgr.toJSON(restored)).toEqual(mgr.toJSON(char));
    expect(restored.equipped.weapon.name).toBe('Flaming Blade');
  });

  test('tolerates null/malformed input with sane defaults', () => {
    const mgr = makeManager();
    for (const bad of [null, undefined, 'garbage', 42, [], { class: 'Hackerman', level: 'NaN', abilities: 'lol', items: 'nope', equipped: 7 }]) {
      const char = mgr.fromJSON(bad);
      expect(Object.keys(CLASSES)).toContain(char.class);
      expect(char.level).toBe(1);
      expect(char.gold).toBe(0);
      for (const stat of STATS) expect(char.abilities[stat]).toBe(10);
      expect(char.items).toEqual([]);
      expect(char.equipped).toEqual({ weapon: null, armor: null, trinket: null });
    }
  });

  test('caps the items array at 50 on load', () => {
    const mgr = makeManager();
    const items = Array.from({ length: 80 }, (_, i) => ({ name: `Trinket ${i}`, slot: 'trinket', bonuses: {} }));
    const char = mgr.fromJSON({ items });
    expect(char.items).toHaveLength(50);
  });
});

// ── Misc ─────────────────────────────────────────────────────────────────────

describe('describeBonuses', () => {
  test('formats bonuses and second wind', () => {
    expect(describeBonuses({ bonuses: { dex: 2, ac: 1 } })).toBe('+2 DEX, +1 AC');
    expect(describeBonuses({ bonuses: { hp: 5 }, effect: 'second_wind' })).toBe('+5 HP, Second Wind');
    expect(describeBonuses({ bonuses: {} })).toBe('');
  });
});
