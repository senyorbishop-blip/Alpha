'use strict';

/**
 * Weighted-random loot table roller.
 *
 * Table format (from loot-tables.json):
 *   [{ "item": "Potion of Healing", "weight": 60 }, ...]
 *
 * Usage:
 *   const roller = new LootRoller(tables);
 *   const item = roller.roll('sub');   // returns item name string or null
 */
class LootRoller {
  constructor(tables = {}) {
    // Pre-compute prefix-sum arrays for O(log n) rolls
    this._compiled = {};
    for (const [key, entries] of Object.entries(tables)) {
      if (Array.isArray(entries)) {
        this._compiled[key] = this._compile(entries);
      }
    }
  }

  _compile(entries) {
    const items = [];
    const cumWeights = [];
    let total = 0;
    for (const e of entries) {
      const w = Number(e.weight ?? 1);
      if (w <= 0) continue;
      total += w;
      items.push(e.item);
      cumWeights.push(total);
    }
    return { items, cumWeights, total };
  }

  _pick(compiled) {
    if (!compiled || compiled.total === 0) return null;
    const r = Math.random() * compiled.total;
    // Binary search for the bucket
    let lo = 0, hi = compiled.cumWeights.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (compiled.cumWeights[mid] <= r) lo = mid + 1;
      else hi = mid;
    }
    return compiled.items[lo] ?? null;
  }

  /**
   * Roll on a named table.
   * Returns an item-name string, or null if the table is empty / unknown.
   */
  roll(tableName) {
    return this._pick(this._compiled[tableName]);
  }

  /**
   * Roll on an ad-hoc entry array ([{item, weight}, …]) — e.g. a gift-tier
   * table from loot-tables.json. Returns an item-name string or null.
   */
  rollTable(entries) {
    if (!Array.isArray(entries) || entries.length === 0) return null;
    return this._pick(this._compile(entries));
  }

  /**
   * Roll on bits tier tables.
   * bitsConfig: [{ threshold: 100, table: [...] }, ...]  (sorted ascending by threshold)
   * Returns { item, tableUsed } or null.
   */
  rollBits(amount, bitsConfig) {
    if (!Array.isArray(bitsConfig) || bitsConfig.length === 0) return null;
    const sorted = [...bitsConfig].sort((a, b) => b.threshold - a.threshold);
    for (const tier of sorted) {
      if (amount >= tier.threshold) {
        const compiled = this._compile(tier.table ?? []);
        if (compiled.total === 0) continue;
        const item = this._pick(compiled);
        return item ? { item, tableUsed: tier.threshold } : null;
      }
    }
    return null;
  }

  hasTable(name) {
    return !!this._compiled[name];
  }
}

/**
 * Weighted pick from server-resolved reward entries
 * ([{power_id, name, weight}, …]). Returns the entry or null.
 */
function rollRewardEntry(entries) {
  const valid = (entries || []).filter(e => e && e.power_id && Number(e.weight ?? 1) > 0);
  if (!valid.length) return null;
  const total = valid.reduce((sum, e) => sum + Number(e.weight ?? 1), 0);
  let r = Math.random() * total;
  for (const e of valid) {
    r -= Number(e.weight ?? 1);
    if (r <= 0) return e;
  }
  return valid[valid.length - 1];
}

/**
 * Highest reward tier whose bound (`key`, e.g. 'threshold' or 'min_count')
 * is ≤ value. Returns the tier or null.
 */
function pickRewardTier(tiers, key, value) {
  const sorted = (tiers || [])
    .filter(t => t && Array.isArray(t.table))
    .sort((a, b) => Number(b[key] ?? 0) - Number(a[key] ?? 0));
  for (const tier of sorted) {
    if (value >= Number(tier[key] ?? 0)) return tier;
  }
  return null;
}

module.exports = { LootRoller, rollRewardEntry, pickRewardTier };
