'use strict';

const { EventRewards } = require('../src/eventRewards');
const { MockEventSubClient } = require('../src/mockChat');
const { LootRoller } = require('../src/lootRoller');

// Silence logger noise in test output.
const quietLogger = { info: () => {}, warn: () => {}, error: () => {} };

// Server reward config with a distinct power per gift tier so tests can
// assert exactly which tier was rolled.
const REWARD_CONFIG = {
  sub: [{ power_id: 'sub_power', name: 'Healing Spark', weight: 1 }],
  gift_tiers: [
    { min_count: 1,  table: [{ power_id: 'gift_x1',  name: 'Tier 1 Power',  weight: 1 }] },
    { min_count: 5,  table: [{ power_id: 'gift_x5',  name: 'Tier 5 Power',  weight: 1 }] },
    { min_count: 10, table: [{ power_id: 'gift_x10', name: 'Tier 10 Power', weight: 1 }] },
  ],
  bits_tiers: [],
};

const LOCAL_TABLES = {
  sub: [{ item: 'Potion of Healing', weight: 1 }],
  gift: [
    { min_count: 10, table: [{ item: 'Legendary Fragment', weight: 1 }] },
    { min_count: 5,  table: [{ item: 'Superior Potion',    weight: 1 }] },
    { min_count: 1,  table: [{ item: 'Plain Potion',       weight: 1 }] },
  ],
};

function makeHarness({ rewardConfig = REWARD_CONFIG, giftConfig, isJoined } = {}) {
  const grantPowerReward = jest.fn().mockResolvedValue(undefined);
  const grantItemReward = jest.fn().mockResolvedValue(undefined);
  const say = jest.fn();
  const eventSub = new MockEventSubClient({ logger: quietLogger });
  const rewards = new EventRewards({
    getRewardConfig: () => rewardConfig,
    lootRoller: new LootRoller(LOCAL_TABLES),
    lootTables: LOCAL_TABLES,
    grantPowerReward,
    grantItemReward,
    isJoined: isJoined ?? (async () => true),
    say,
    logger: quietLogger,
    giftConfig,
    welcomeDelayMs: 5,
    dedupWindowMs: 1000,
  });
  rewards.attach(eventSub);
  return { eventSub, rewards, grantPowerReward, grantItemReward, say };
}

// MockEventSubClient dispatches handlers via Promise.resolve; let them settle.
const settle = (ms = 0) => new Promise(r => setTimeout(r, ms));

function totalRolls(h) {
  return h.grantPowerReward.mock.calls.length + h.grantItemReward.mock.calls.length;
}

/** Fire a gift batch the way real EventSub does: one giftsub event for the
 *  gifter plus a gifted channel.subscribe per recipient. */
function fireGiftBatch(eventSub, gifter, count, { anonymous = false } = {}) {
  eventSub.emit('giftsub', {
    username: anonymous ? '' : gifter,
    displayName: anonymous ? 'An Anonymous Gifter' : gifter,
    total: count,
    anonymous,
  });
  for (let i = 0; i < count; i++) {
    eventSub.emit('sub', {
      username: `recipient${i}`,
      displayName: `Recipient${i}`,
      months: 1,
      gifted: true,
    });
  }
}

describe('EventRewards — gifted subs', () => {
  test('gift of 5 → exactly one roll for the GIFTER from the x5 tier, zero recipient rolls', async () => {
    const h = makeHarness();
    fireGiftBatch(h.eventSub, 'bob', 5);
    await settle();

    expect(totalRolls(h)).toBe(1);
    const [username, , entry, trigger] = h.grantPowerReward.mock.calls[0];
    expect(username).toBe('bob');
    expect(entry.power_id).toBe('gift_x5');
    expect(trigger).toBe('gift_x5');
  });

  test('gift of 1 and gift of 10 pick the x1 / x10 tiers', async () => {
    const h = makeHarness();
    fireGiftBatch(h.eventSub, 'alice', 1);
    fireGiftBatch(h.eventSub, 'carol', 10);
    await settle();

    expect(h.grantPowerReward).toHaveBeenCalledTimes(2);
    const tiers = h.grantPowerReward.mock.calls.map(c => c[2].power_id);
    expect(tiers).toEqual(['gift_x1', 'gift_x10']);
  });

  test('recipient events (is_gift=true) never roll, even standalone', async () => {
    const h = makeHarness();
    h.eventSub.emit('sub', { username: 'lucky', displayName: 'Lucky', months: 1, gifted: true });
    await settle();
    expect(totalRolls(h)).toBe(0);
  });

  test('no server config → gifter rolls from local gift tiers by min_count', async () => {
    const h = makeHarness({ rewardConfig: null });
    fireGiftBatch(h.eventSub, 'bob', 5);
    await settle();

    expect(h.grantItemReward).toHaveBeenCalledTimes(1);
    const [username, , itemName] = h.grantItemReward.mock.calls[0];
    expect(username).toBe('bob');
    expect(itemName).toBe('Superior Potion');
  });
});

describe('EventRewards — self-subs and resubs', () => {
  test('self-sub → exactly one roll', async () => {
    const h = makeHarness();
    h.eventSub.emit('sub', { username: 'alice', displayName: 'Alice', months: 1, gifted: false });
    await settle();

    expect(totalRolls(h)).toBe(1);
    expect(h.grantPowerReward).toHaveBeenCalledWith(
      'alice', 'Alice', expect.objectContaining({ power_id: 'sub_power' }), 'sub', expect.any(Function));
  });

  test('resub delivered as BOTH subscribe and message events → exactly one roll', async () => {
    const h = makeHarness();
    // Real EventSub can deliver the same renewal via channel.subscribe and
    // channel.subscription.message; both surface as 'sub' events.
    h.eventSub.emit('sub', { username: 'dave', displayName: 'Dave', months: 4, gifted: false });
    h.eventSub.emit('sub', { username: 'dave', displayName: 'Dave', months: 4, gifted: false });
    await settle();

    expect(totalRolls(h)).toBe(1);
    expect(h.grantPowerReward.mock.calls[0][3]).toBe('resub');
  });

  test('dedup is per-user — two different subscribers both roll', async () => {
    const h = makeHarness();
    h.eventSub.emit('sub', { username: 'erin', displayName: 'Erin', months: 1, gifted: false });
    h.eventSub.emit('sub', { username: 'fred', displayName: 'Fred', months: 1, gifted: false });
    await settle();
    expect(totalRolls(h)).toBe(2);
  });

  test('same user can roll again once the dedup window has passed', async () => {
    const h = makeHarness();
    h.rewards._dedupWindowMs = 10;
    h.eventSub.emit('sub', { username: 'gale', displayName: 'Gale', months: 2, gifted: false });
    await settle(20);
    h.eventSub.emit('sub', { username: 'gale', displayName: 'Gale', months: 3, gifted: false });
    await settle();
    expect(totalRolls(h)).toBe(2);
  });
});

describe('EventRewards — anonymous gifts', () => {
  test('anonymous gift → no roll by default, one thank-you line', async () => {
    const h = makeHarness();
    fireGiftBatch(h.eventSub, '', 5, { anonymous: true });
    await settle();

    expect(totalRolls(h)).toBe(0);
    const thankYous = h.say.mock.calls.filter(([line]) => /anonymous hero/i.test(line));
    expect(thankYous).toHaveLength(1);
  });

  test('bank mode holds the roll until the gifter is de-anonymized and claims it', async () => {
    const h = makeHarness({ giftConfig: { anonymous_mode: 'bank' } });
    fireGiftBatch(h.eventSub, '', 5, { anonymous: true });
    await settle();

    expect(totalRolls(h)).toBe(0);
    expect(h.rewards.bankedGiftRewardCount).toBe(1);

    const granted = await h.rewards.claimBankedGiftRewards('bob', 'Bob');
    expect(granted).toBe(1);
    expect(h.rewards.bankedGiftRewardCount).toBe(0);
    expect(totalRolls(h)).toBe(1);
    const [username, , entry] = h.grantPowerReward.mock.calls[0];
    expect(username).toBe('bob');
    expect(entry.power_id).toBe('gift_x5');
  });
});

describe('EventRewards — cumulative gifting', () => {
  test('off by default: 3 gifts then 2 gifts each use their own event count', async () => {
    const h = makeHarness();
    fireGiftBatch(h.eventSub, 'bob', 3);
    fireGiftBatch(h.eventSub, 'bob', 2);
    await settle();

    const tiers = h.grantPowerReward.mock.calls.map(c => c[2].power_id);
    expect(tiers).toEqual(['gift_x1', 'gift_x1']);
  });

  test('enabled: 3 gifts then 2 gifts crosses the x5 tier on the second event', async () => {
    const h = makeHarness({ giftConfig: { cumulative: true } });
    fireGiftBatch(h.eventSub, 'bob', 3);
    fireGiftBatch(h.eventSub, 'bob', 2);
    await settle();

    expect(h.grantPowerReward).toHaveBeenCalledTimes(2);
    const tiers = h.grantPowerReward.mock.calls.map(c => c[2].power_id);
    expect(tiers).toEqual(['gift_x1', 'gift_x5']);
  });
});

describe('EventRewards — recipient welcome', () => {
  test('one collective welcome line for not-joined recipients, no per-recipient messages', async () => {
    const h = makeHarness({ isJoined: async () => false });
    fireGiftBatch(h.eventSub, 'bob', 5);
    await settle(30);

    const welcomes = h.say.mock.calls.filter(([line]) => /welcome/i.test(line));
    expect(welcomes).toHaveLength(1);
    expect(welcomes[0][0]).toContain('5 new tavern patrons');
    expect(welcomes[0][0]).toContain('!join');
    expect(totalRolls(h)).toBe(1); // still just the gifter's roll
  });

  test('already-joined recipients produce no welcome line', async () => {
    const h = makeHarness({ isJoined: async () => true });
    fireGiftBatch(h.eventSub, 'bob', 5);
    await settle(30);

    const welcomes = h.say.mock.calls.filter(([line]) => /welcome/i.test(line));
    expect(welcomes).toHaveLength(0);
  });

  test('welcome can be disabled via config', async () => {
    const h = makeHarness({ isJoined: async () => false, giftConfig: { welcome_recipients: false } });
    fireGiftBatch(h.eventSub, 'bob', 3);
    await settle(30);

    const welcomes = h.say.mock.calls.filter(([line]) => /welcome/i.test(line));
    expect(welcomes).toHaveLength(0);
  });
});
