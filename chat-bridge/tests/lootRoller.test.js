'use strict';

const { LootRoller, rollRewardEntry, pickRewardTier } = require('../src/lootRoller');

const TABLES = {
  sub: [
    { item: 'Potion of Healing', weight: 60 },
    { item: 'Magic Sword', weight: 30 },
    { item: 'Artifact', weight: 10 },
  ],
  single: [
    { item: 'Only Item', weight: 100 },
  ],
};

const BITS_CONFIG = [
  { threshold: 1000, table: [{ item: 'Big Prize', weight: 100 }] },
  { threshold: 100,  table: [{ item: 'Small Prize', weight: 100 }] },
];

describe('LootRoller', () => {
  test('roll returns a string from the table', () => {
    const roller = new LootRoller(TABLES);
    const result = roller.roll('sub');
    expect(['Potion of Healing', 'Magic Sword', 'Artifact']).toContain(result);
  });

  test('roll single-item table always returns that item', () => {
    const roller = new LootRoller(TABLES);
    for (let i = 0; i < 20; i++) {
      expect(roller.roll('single')).toBe('Only Item');
    }
  });

  test('roll unknown table returns null', () => {
    const roller = new LootRoller(TABLES);
    expect(roller.roll('nonexistent')).toBeNull();
  });

  test('roll empty table returns null', () => {
    const roller = new LootRoller({ empty: [] });
    expect(roller.roll('empty')).toBeNull();
  });

  test('distribution is roughly proportional over many rolls', () => {
    const roller = new LootRoller(TABLES);
    const counts = { 'Potion of Healing': 0, 'Magic Sword': 0, 'Artifact': 0 };
    const N = 10000;
    for (let i = 0; i < N; i++) {
      counts[roller.roll('sub')]++;
    }
    // 60/100 weight → ~60% of rolls; allow ±10%
    expect(counts['Potion of Healing'] / N).toBeGreaterThan(0.50);
    expect(counts['Potion of Healing'] / N).toBeLessThan(0.70);
    expect(counts['Artifact'] / N).toBeLessThan(0.20);
  });

  test('rollBits returns correct tier', () => {
    const roller = new LootRoller({});
    expect(roller.rollBits(1000, BITS_CONFIG)).toEqual({ item: 'Big Prize',   tableUsed: 1000 });
    expect(roller.rollBits(500,  BITS_CONFIG)).toEqual({ item: 'Small Prize', tableUsed: 100 });
    expect(roller.rollBits(200,  BITS_CONFIG)).toEqual({ item: 'Small Prize', tableUsed: 100 });
    expect(roller.rollBits(50,   BITS_CONFIG)).toBeNull();
  });

  test('rollBits with empty config returns null', () => {
    const roller = new LootRoller({});
    expect(roller.rollBits(500, [])).toBeNull();
  });

  test('hasTable returns correct boolean', () => {
    const roller = new LootRoller(TABLES);
    expect(roller.hasTable('sub')).toBe(true);
    expect(roller.hasTable('missing')).toBe(false);
  });

  test('zero-weight items are excluded', () => {
    const roller = new LootRoller({ t: [{ item: 'A', weight: 0 }, { item: 'B', weight: 10 }] });
    for (let i = 0; i < 50; i++) {
      expect(roller.roll('t')).toBe('B');
    }
  });
});

describe('Server-managed reward tables (rollRewardEntry / pickRewardTier)', () => {
  const SUB_TABLE = [
    { power_id: 'healing_spark', name: 'Healing Spark', weight: 40 },
    { power_id: 'give_potion', name: 'Give Potion', weight: 60 },
  ];

  test('rollRewardEntry returns an entry from the table', () => {
    for (let i = 0; i < 30; i++) {
      const entry = rollRewardEntry(SUB_TABLE);
      expect(['healing_spark', 'give_potion']).toContain(entry.power_id);
    }
  });

  test('rollRewardEntry skips zero-weight and malformed entries', () => {
    const table = [
      { power_id: 'never', weight: 0 },
      { weight: 100 },                       // no power_id
      { power_id: 'always', weight: 5 },
    ];
    for (let i = 0; i < 30; i++) {
      expect(rollRewardEntry(table).power_id).toBe('always');
    }
  });

  test('rollRewardEntry returns null for empty/missing tables', () => {
    expect(rollRewardEntry([])).toBeNull();
    expect(rollRewardEntry(null)).toBeNull();
    expect(rollRewardEntry([{ power_id: 'x', weight: 0 }])).toBeNull();
  });

  test('pickRewardTier picks the highest qualifying gift tier (1 / 5 / 10+)', () => {
    const tiers = [
      { min_count: 1, table: [{ power_id: 'small', weight: 1 }] },
      { min_count: 5, table: [{ power_id: 'medium', weight: 1 }] },
      { min_count: 10, table: [{ power_id: 'big', weight: 1 }] },
    ];
    expect(pickRewardTier(tiers, 'min_count', 1).min_count).toBe(1);
    expect(pickRewardTier(tiers, 'min_count', 4).min_count).toBe(1);
    expect(pickRewardTier(tiers, 'min_count', 5).min_count).toBe(5);
    expect(pickRewardTier(tiers, 'min_count', 25).min_count).toBe(10);
    expect(pickRewardTier(tiers, 'min_count', 0)).toBeNull();
  });

  test('pickRewardTier works for bits thresholds', () => {
    const tiers = [
      { threshold: 100, table: [{ power_id: 'a', weight: 1 }] },
      { threshold: 500, table: [{ power_id: 'b', weight: 1 }] },
    ];
    expect(pickRewardTier(tiers, 'threshold', 250).threshold).toBe(100);
    expect(pickRewardTier(tiers, 'threshold', 500).threshold).toBe(500);
    expect(pickRewardTier(tiers, 'threshold', 50)).toBeNull();
  });
});
