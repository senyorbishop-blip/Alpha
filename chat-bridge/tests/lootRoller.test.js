'use strict';

const { LootRoller } = require('../src/lootRoller');

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
