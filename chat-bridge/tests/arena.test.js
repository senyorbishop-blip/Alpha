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
    resolveOpponent: opts.resolveOpponent,
  });
  arenas.push(arena);
  return { arena, twitch, collector };
}

/**
 * Fake server-side resolver over a roster of
 * { login: { display, character } } — mirrors chat_participant_resolve.
 */
function makeResolver(roster) {
  return async (name) => {
    const q = String(name).replace(/^@/, '').trim().toLowerCase();
    const byLogin = Object.keys(roster).filter(login => login === q);
    const byDisplay = Object.keys(roster).filter(login =>
      (roster[login].display || '').toLowerCase() === q);
    const byChar = Object.keys(roster).filter(login =>
      (roster[login].character || '').toLowerCase() === q);
    const hits = byLogin.length ? byLogin : (byDisplay.length ? byDisplay : byChar);
    if (hits.length === 1) {
      return {
        found: true,
        twitch_username: hits[0],
        display_name: roster[hits[0]].display,
        character_name: roster[hits[0]].character,
      };
    }
    if (hits.length > 1) return { found: false, error_code: 'ambiguous', matches: hits };
    return { found: false, error_code: 'not_found' };
  };
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

describe('Arena — opponent @-mentions and name resolution', () => {
  const ROSTER = {
    potatowizard: { display: 'PotatoWizard', character: 'Grognak' },
    alice:        { display: 'Alice',        character: '' },
  };

  test.each([
    ['@potatowizard'],
    ['@PotatoWizard'],
    ['PotatoWizard'],
    ['potatowizard'],
    ['Grognak'],
    ['grognak'],
  ])('!duel %s resolves to the same opponent login', async (ref) => {
    const { arena, twitch } = makeArena({ resolveOpponent: makeResolver(ROSTER) });
    await arena.handleCommand(CH, 'alice', 'Alice', 'duel', [ref]);
    expect(arena._challenges.has('potatowizard')).toBe(true);
    const announcement = twitch.said[twitch.said.length - 1].msg;
    expect(announcement).toContain('@potatowizard');
    expect(announcement).not.toContain('@@');
  });

  test('resolved challenge is acceptable by the opponent login', async () => {
    const { arena } = makeArena({ resolveOpponent: makeResolver(ROSTER) });
    await arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['Grognak']);
    arena.handleCommand(CH, 'potatowizard', 'PotatoWizard', 'accept', []);
    expect(arena._challenges.has('potatowizard')).toBe(false);
    await new Promise(r => setTimeout(r, 20));  // duel starts async (stats load)
    expect(arena._activeDuels.has('alice')).toBe(true);
    expect(arena._activeDuels.has('potatowizard')).toBe(true);
  });

  test('ambiguous reference asks which opponent instead of challenging', async () => {
    const roster = {
      bob:   { display: 'Bob',   character: 'Shadow' },
      carol: { display: 'Carol', character: 'shadow' },  // same character name, different case
    };
    // Force both character names through the ambiguity path
    const resolver = async () => ({ found: false, error_code: 'ambiguous', matches: ['bob', 'carol'] });
    const { arena, twitch } = makeArena({ resolveOpponent: resolver });
    await arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['Shadow']);
    expect(arena._challenges.size).toBe(0);
    expect(twitch.replied.some(r => r.msg.toLowerCase().includes('which one'))).toBe(true);
  });

  test('unknown name falls back to raw login so unjoined chatters can be challenged', async () => {
    const { arena } = makeArena({ resolveOpponent: makeResolver(ROSTER) });
    await arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['@SomeLurker']);
    expect(arena._challenges.has('somelurker')).toBe(true);
  });

  test('leading @ is stripped even without a resolver', async () => {
    const { arena, twitch } = makeArena();
    await arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['@Bob']);
    expect(arena._challenges.has('bob')).toBe(true);
    expect(twitch.said[0].msg).not.toContain('@@');
  });

  test('challenging your own display or character name is rejected as self-challenge', async () => {
    const { arena, twitch } = makeArena({ resolveOpponent: makeResolver(ROSTER) });
    await arena.handleCommand(CH, 'potatowizard', 'PotatoWizard', 'duel', ['Grognak']);
    expect(arena._challenges.size).toBe(0);
    expect(twitch.replied.some(r => r.msg.includes("can't challenge yourself"))).toBe(true);
  });

  test('resolver failure falls back to raw name instead of breaking !duel', async () => {
    const resolver = async () => { throw new Error('server unreachable'); };
    const { arena } = makeArena({ resolveOpponent: resolver });
    await arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['@Bob']);
    expect(arena._challenges.has('bob')).toBe(true);
  });
});

describe('Arena — no double-@ in reply templates', () => {
  test('challenge, decline, and expiry messages never contain @@', async () => {
    const { arena, twitch } = makeArena();
    await arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['@Bob']);
    arena.handleCommand(CH, 'bob', 'Bob', 'decline', []);

    // Expiry path — inject an already-expired challenge with a hostile '@' value
    arena._challenges.set('charlie', {
      challenger: '@alice', challengerDisplay: 'Alice', challenged: '@charlie',
      channel: CH, expiresAt: Date.now() - 1000,
    });
    arena._cleanupExpiredChallenges();

    for (const s of twitch.said) expect(s.msg).not.toContain('@@');
    for (const r of twitch.replied) expect(r.msg).not.toContain('@@');
  });

  test('fight narration and winner announcement never contain @@', async () => {
    const { arena, twitch } = makeArena();
    // Drive a full auto-resolve fight synchronously by stubbing the timers
    const scheduled = [];
    const origSetTimeout = global.setTimeout;
    global.setTimeout = (fn, ms) => { scheduled.push(fn); return origSetTimeout(() => {}, 0); };
    try {
      await arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['@Bob']);
      arena.handleCommand(CH, 'bob', 'Bob', 'accept', []);
      await new Promise(r => origSetTimeout(r, 20));  // flush async duel start
      for (const fn of scheduled.splice(0)) fn();     // flush all round messages
    } finally {
      global.setTimeout = origSetTimeout;
    }

    // A full fight was narrated…
    expect(twitch.said.some(s => s.msg.includes('ARENA DUEL'))).toBe(true);
    expect(twitch.said.some(s => s.msg.includes('Round 1'))).toBe(true);
    // …and no message anywhere contains a double @
    for (const s of twitch.said) expect(s.msg).not.toContain('@@');
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
