'use strict';

const { Arena } = require('../src/arena');
const { CharacterManager } = require('../src/arenaProgression');

// ── Helpers ──────────────────────────────────────────────────────────────────

const silentLogger = { info: () => {}, warn: () => {}, error: () => {} };

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
    pity_rare_every_n_wins: 100, // effectively off for these tests
    tables: {
      common: [{ name: 'Rusty Dagger', slot: 'weapon', bonuses: { atk: 1 } }],
      rare:   [{ name: 'Flaming Blade', slot: 'weapon', bonuses: { atk: 3 } }],
    },
  },
  reroll: { policy: 'free_once', price: 100 },
  shop: { rotation_size: 2, stock: [{ name: 'Iron Sword', slot: 'weapon', bonuses: { atk: 1 }, price: 60 }] },
  duel: { interactive_mode: false, wagers_enabled: true, max_wager: 100 },
  fallback: { hp: 20, ac: 12, damage_die: 6 },
  damage_die_by_hit_die: { 12: 8, 10: 8, 8: 6, 6: 10 },
};

function makeTwitch() {
  return {
    said: [],
    replied: [],
    say(channel, msg)         { this.said.push({ channel, msg }); },
    reply(channel, user, msg) { this.replied.push({ channel, user, msg }); },
    reset()                   { this.said = []; this.replied = []; },
  };
}

// Collect stats updates without touching a real game client
function makeStatsCollector() {
  const updates = [];
  return {
    updates,
    fn: async (username, stats) => { updates.push({ username, stats }); },
  };
}

const createdArenas = [];
const createdManagers = [];

function makeArena(opts = {}) {
  const twitch = makeTwitch();
  const collector = makeStatsCollector();
  const displayEvents = [];
  const arena = new Arena({
    twitchClient: twitch,
    config: { duel_cooldown_seconds: 0, message_interval_ms: 1, wagers_enabled: true, max_wager: 100, ...opts.config },
    logger: silentLogger,
    onStatsUpdate: collector.fn,
    onDisplayEvent: (e) => displayEvents.push(e),
    characterManager: opts.characterManager,
    onCharacterSync: opts.onCharacterSync,
    rng: opts.rng,
  });
  createdArenas.push(arena);
  return { arena, twitch, collector, displayEvents };
}

function makeManager(opts = {}) {
  const mgr = new CharacterManager({ balance: BALANCE, logger: silentLogger, ...opts });
  createdManagers.push(mgr);
  return mgr;
}

/** Pre-load a character into the manager cache (bypasses server load). */
function seedChar(mgr, username, overrides = {}) {
  const char = mgr.fromJSON({
    class: 'Fighter',
    level: 1,
    abilities: { str: 16, dex: 14, con: 14, int: 10, wis: 10, cha: 10 },
    ...overrides,
  });
  mgr._cache.set(username, char);
  return char;
}

function waitFor(cond, timeoutMs = 2000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const iv = setInterval(() => {
      if (cond()) { clearInterval(iv); resolve(); }
      else if (Date.now() - start > timeoutMs) { clearInterval(iv); reject(new Error('waitFor timeout')); }
    }, 5);
  });
}

afterEach(() => {
  while (createdArenas.length) createdArenas.pop().destroy();
  while (createdManagers.length) createdManagers.pop().destroy();
});

const CH = '#test';

// ── Tests ────────────────────────────────────────────────────────────────────

describe('Arena — challenge lifecycle', () => {
  test('!duel creates a pending challenge', () => {
    const { arena, twitch } = makeArena();
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    expect(twitch.said.some(s => s.msg.includes('alice') || s.msg.includes('bob'))).toBe(true);
    expect(arena._challenges.has('bob')).toBe(true);
  });

  test('!accept starts duel and clears challenge', () => {
    const { arena, twitch } = makeArena();
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    twitch.reset();
    arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);
    expect(arena._challenges.has('bob')).toBe(false);
  });

  test('!decline removes challenge without starting duel', () => {
    const { arena } = makeArena();
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    arena.handleCommand(CH, 'bob', 'Bob', 'decline', []);
    expect(arena._challenges.has('bob')).toBe(false);
    expect(arena._activeDuels.size).toBe(0);
  });

  test('expired challenge is cleaned up', () => {
    const { arena } = makeArena();
    // Manually inject an already-expired challenge
    arena._challenges.set('charlie', {
      challenger: 'alice',
      challengerDisplay: 'Alice',
      challenged: 'charlie',
      channel: CH,
      expiresAt: Date.now() - 1000,
    });
    arena._cleanupExpiredChallenges();
    expect(arena._challenges.has('charlie')).toBe(false);
  });

  test('!accept with no challenge replies with message', () => {
    const { arena, twitch } = makeArena();
    arena.handleCommand(CH, 'nobody', 'Nobody', 'accept', []);
    expect(twitch.replied.some(r => r.msg.toLowerCase().includes('no pending'))).toBe(true);
  });

  test('cannot challenge yourself', () => {
    const { arena, twitch } = makeArena();
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['alice']);
    expect(arena._challenges.size).toBe(0);
    expect(twitch.replied.length).toBeGreaterThan(0);
  });
});

describe('Arena — duels with characters use derived stats', () => {
  test('display events carry character-derived HP, and the duel pays out XP/gold', async () => {
    const rng = () => 0.999; // every d20 roll is a nat 20 crit, max damage
    const mgr = makeManager({ rng });
    const { arena, displayEvents, collector } = makeArena({ characterManager: mgr, rng });

    const alice = seedChar(mgr, 'alice');
    const bob = seedChar(mgr, 'bob');
    const expectedMaxHp = mgr.maxHp(alice); // Fighter d10, CON +2, L1 → 12

    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);
    await waitFor(() => displayEvents.some(e => e.event_type === 'duel_end'));

    const start = displayEvents.find(e => e.event_type === 'duel_start');
    expect(start.challenger_max_hp).toBe(expectedMaxHp);
    expect(start.opponent_max_hp).toBe(mgr.maxHp(bob));

    // Alice (challenger) swings first with guaranteed crits — she wins.
    await waitFor(() => collector.updates.length >= 2);
    expect(alice.wins).toBe(1);
    expect(bob.losses).toBe(1);
    expect(alice.xp).toBeGreaterThan(0);
    expect(alice.gold).toBe(45); // 25 win + 20 first-of-day
    expect(bob.gold).toBe(25);   // 5 loss + 20 first-of-day

    // onStatsUpdate mirrors character wins/losses/gold
    const aliceStats = collector.updates.find(u => u.username === 'alice');
    expect(aliceStats.stats).toEqual({ wins: 1, losses: 0, arena_gold: 45 });
  });

  test('falls back to legacy flat stats without a character manager', async () => {
    const { arena, displayEvents } = makeArena({ rng: () => 0.999 });
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);
    await waitFor(() => displayEvents.some(e => e.event_type === 'duel_end'));
    const start = displayEvents.find(e => e.event_type === 'duel_start');
    expect(start.challenger_max_hp).toBe(20);
  });
});

describe('Arena — wagers', () => {
  test('challenge escrows the stake; decline refunds it', async () => {
    const mgr = makeManager({ rng: () => 0.999 });
    const { arena } = makeArena({ characterManager: mgr, rng: () => 0.999 });
    const alice = seedChar(mgr, 'alice', { gold: 100 });
    seedChar(mgr, 'bob', { gold: 100 });

    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob', '30']);
    await waitFor(() => alice.gold === 70); // escrowed

    arena.handleCommand(CH, 'bob', 'Bob', 'decline', []);
    expect(alice.gold).toBe(100); // refunded
  });

  test('expired wagered challenge refunds the challenger', async () => {
    const mgr = makeManager({ rng: () => 0.999 });
    const { arena } = makeArena({ characterManager: mgr, rng: () => 0.999 });
    const alice = seedChar(mgr, 'alice', { gold: 100 });

    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob', '40']);
    await waitFor(() => alice.gold === 60);

    arena._challenges.get('bob').expiresAt = Date.now() - 1;
    arena._cleanupExpiredChallenges();
    expect(alice.gold).toBe(100);
  });

  test('winner takes the pot', async () => {
    const rng = () => 0.999;
    const mgr = makeManager({ rng });
    const { arena, displayEvents } = makeArena({ characterManager: mgr, rng });
    const alice = seedChar(mgr, 'alice', { gold: 100 });
    const bob = seedChar(mgr, 'bob', { gold: 100 });

    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob', '30']);
    await waitFor(() => alice.gold === 70);
    arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);
    await waitFor(() => displayEvents.some(e => e.event_type === 'duel_end'));
    await waitFor(() => alice.gold > 70);

    // Alice: 100 - 30 stake + 60 pot + 25 win + 20 first-of-day = 175
    expect(alice.gold).toBe(175);
    // Bob: 100 - 30 stake + 5 loss + 20 first-of-day = 95
    expect(bob.gold).toBe(95);
  });

  test('wager rejected when challenger cannot afford it', async () => {
    const mgr = makeManager({ rng: () => 0.999 });
    const { arena, twitch } = makeArena({ characterManager: mgr, rng: () => 0.999 });
    const alice = seedChar(mgr, 'alice', { gold: 5 });

    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob', '50']);
    await waitFor(() => twitch.replied.length > 0);
    expect(twitch.replied.some(r => r.msg.includes('5g'))).toBe(true);
    expect(alice.gold).toBe(5);
    expect(arena._challenges.size).toBe(0);
  });

  test('wager above max or with wagers disabled is rejected', async () => {
    const mgr = makeManager({ rng: () => 0.999 });
    const { arena, twitch } = makeArena({
      characterManager: mgr,
      config: { wagers_enabled: false },
    });
    seedChar(mgr, 'alice', { gold: 1000 });
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob', '10']);
    await waitFor(() => twitch.replied.length > 0);
    expect(twitch.replied.some(r => r.msg.toLowerCase().includes('disabled'))).toBe(true);

    const enabled = makeArena({ characterManager: mgr, config: { wagers_enabled: true, max_wager: 100 } });
    enabled.arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob', '5000']);
    await waitFor(() => enabled.twitch.replied.length > 0);
    expect(enabled.twitch.replied.some(r => r.msg.includes('100g'))).toBe(true);
  });
});

describe('Arena — isolation guarantee', () => {
  test('across a full duel lifecycle only arena-scoped message types are sent', async () => {
    // Fake game client spy — every external side-effect goes through send().
    const client = {
      calls: [],
      async send(type, payload) { this.calls.push({ type, payload }); return null; },
    };

    const rng = () => 0.999;
    const mgr = makeManager({
      rng,
      // Characters are pre-seeded below, so loadCharacter is never hit.
      loadCharacter: async () => { throw new Error('load should not be called'); },
      syncCharacter: async (username, displayName, arenaCharacter) => {
        await client.send('chat_participant_arena_sync', { twitch_username: username, display_name: displayName, arena_character: arenaCharacter });
      },
    });
    seedChar(mgr, 'alice', { gold: 100 });
    seedChar(mgr, 'bob', { gold: 100 });

    const twitch = makeTwitch();
    const arena = new Arena({
      twitchClient: twitch,
      config: { duel_cooldown_seconds: 0, message_interval_ms: 1, wagers_enabled: true, max_wager: 100 },
      logger: silentLogger,
      characterManager: mgr,
      rng,
      onStatsUpdate: async (username, stats) => {
        await client.send('chat_participant_arena_stats_update', { twitch_username: username, arena_stats: stats });
      },
      onDisplayEvent: (payload) => { client.send('arena_display_event', payload); },
    });
    createdArenas.push(arena);

    // Full lifecycle: wagered challenge → accept → fight → rewards → sync.
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob', '30']);
    await waitFor(() => mgr.getCached('alice').gold === 70);
    arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);
    await waitFor(() => client.calls.some(c => c.type === 'arena_display_event' && c.payload.event_type === 'duel_end'));
    await waitFor(() => client.calls.filter(c => c.type === 'chat_participant_arena_stats_update').length >= 2);
    await new Promise(r => setTimeout(r, 25)); // let any stragglers land

    expect(client.calls.length).toBeGreaterThan(0);
    const ALLOWED = new Set([
      'chat_participant_arena_stats_update',
      'chat_participant_arena_sync',
      'arena_display_event',
    ]);
    for (const call of client.calls) {
      expect(ALLOWED.has(call.type)).toBe(true);
    }
    // Explicitly: no campaign-mutating types, ever.
    const types = client.calls.map(c => c.type);
    expect(types).not.toContain('chat_bridge_loot_grant');
    expect(types).not.toContain('chat_participant_target');
    expect(types).not.toContain('chat_participant_join');
  });

  test('onStatsUpdate is the ONLY legacy external call after a duel', async () => {
    const { arena, collector, displayEvents } = makeArena();
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);
    await waitFor(() => displayEvents.some(e => e.event_type === 'duel_end'));
    await new Promise(r => setTimeout(r, 20));

    // The only external side-effect should be stats updates, not campaign mutations
    for (const u of collector.updates) {
      expect(u.username).toBeTruthy();
      expect(typeof u.stats.wins).toBe('number');
      expect(typeof u.stats.losses).toBe('number');
    }
  });

  test('arena error does not throw (error boundary)', () => {
    const { arena } = makeArena();
    // Inject a bad state that would normally throw
    expect(() => {
      arena.handleCommand(CH, 'alice', 'Alice', 'duel', []);  // missing target — handled gracefully
    }).not.toThrow();
  });
});

describe('Arena — cooldown', () => {
  test('user cannot start another duel immediately after one', async () => {
    const { arena } = makeArena({ config: { duel_cooldown_seconds: 60 } });
    // Set a past-cooldown time
    arena._cooldowns.set('alice', Date.now());  // just ended

    const msgs = [];
    arena._twitch.say = (ch, msg) => msgs.push(msg);
    arena._twitch.reply = (ch, u, msg) => msgs.push(msg);

    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    expect(msgs.some(m => m.toLowerCase().includes('cooldown') || m.toLowerCase().includes('try again'))).toBe(true);
    expect(arena._challenges.size).toBe(0);
  });
});

describe('Arena — quiet mode', () => {
  test('duels are queued in quiet mode', () => {
    const { arena } = makeArena();
    arena.setQuietMode(true);
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);
    // Duel should be queued, not started
    expect(arena._queue.length).toBeGreaterThan(0);
    expect(arena._activeDuels.size).toBe(0);
  });

  test('queued duels start when quiet mode is turned off', () => {
    const { arena } = makeArena();
    arena.setQuietMode(true);
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);
    expect(arena._queue.length).toBeGreaterThan(0);
    arena.setQuietMode(false);
    expect(arena._queue.length).toBe(0);
  });
});

describe('Arena — disabled state', () => {
  test('commands are rejected when arena is disabled', () => {
    const { arena, twitch } = makeArena();
    arena.setEnabled(false);
    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    expect(arena._challenges.size).toBe(0);
    expect(twitch.replied.some(r => r.msg.toLowerCase().includes('closed') || r.msg.toLowerCase().includes('disabled'))).toBe(true);
  });
});
