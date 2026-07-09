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

  /**
   * Roll on a named table.
   * Returns an item-name string, or null if the table is empty / unknown.
   */
  roll(tableName) {
    const table = this._compiled[tableName];
    if (!table || table.total === 0) return null;
    const r = Math.random() * table.total;
    // Binary search for the bucket
    let lo = 0, hi = table.cumWeights.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (table.cumWeights[mid] <= r) lo = mid + 1;
      else hi = mid;
    }
    return table.items[lo] ?? null;
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
        const r = Math.random() * compiled.total;
        let lo = 0, hi = compiled.cumWeights.length - 1;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (compiled.cumWeights[mid] <= r) lo = mid + 1;
          else hi = mid;
        }
        const item = compiled.items[lo] ?? null;
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
