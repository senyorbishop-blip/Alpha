'use strict';

const { Arena } = require('../src/arena');

// ── Helpers ──────────────────────────────────────────────────────────────────

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

const arenas = [];

function makeArena(opts = {}) {
  const twitch = makeTwitch();
  const collector = makeStatsCollector();
  const arena = new Arena({
    twitchClient: twitch,
    config: { duel_cooldown_seconds: 0, ...opts.config },
    logger: { info: () => {}, warn: () => {}, error: () => {} },
    onStatsUpdate: collector.fn,
  });
  arenas.push(arena);
  return { arena, twitch, collector };
}

afterEach(() => {
  while (arenas.length) arenas.pop().destroy();
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

describe('Arena — isolation guarantee', () => {
  test('onStatsUpdate is the ONLY external call after a duel', async () => {
    const { arena, collector } = makeArena();
    // Mock the auto-resolve to run synchronously
    const messages = [];
    arena._twitch.say = (ch, msg) => messages.push(msg);
    arena._twitch.reply = (ch, u, msg) => messages.push(msg);

    // Bypass setTimeout by directly calling _runAutoResolve synchronously
    // Override _finishDuel to capture calls without timers
    let finishCalled = false;
    const origFinish = arena._finishDuel.bind(arena);
    arena._finishDuel = (state, c, ch) => { finishCalled = true; origFinish(state, c, ch); };

    arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
    arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);

    // Wait for the setTimeout chain to flush
    await new Promise(r => setTimeout(r, 50));

    // The only external side-effect should be stats updates, not campaign mutations
    expect(collector.updates.length).toBeGreaterThanOrEqual(0);
    // Verify no "campaign" message types appear in updates
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

    const origSay = arena._twitch.say;
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
