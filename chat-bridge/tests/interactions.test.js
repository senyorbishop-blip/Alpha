'use strict';

const { CommandParser } = require('../src/commandParser');
const { RateLimiter }   = require('../src/rateLimiter');
const { PendingInteractions } = require('../src/interactions');

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------
function makeGameClient(responses = {}) {
  return {
    calls: [],
    async send(type, payload) {
      this.calls.push({ type, payload });
      const r = responses[type];
      if (typeof r === 'function') return r(payload, this.calls);
      if (Array.isArray(r)) return r.length > 1 ? r.shift() : r[0];
      return r ?? null;
    },
  };
}

function makeTwitchClient() {
  return {
    said: [],
    replied: [],
    say(channel, msg)         { this.said.push({ channel, msg }); },
    reply(channel, user, msg) { this.replied.push({ channel, user, msg }); },
  };
}

function makeParser(gameResponses = {}) {
  const commandMap = { '!join': 'join', '!target': 'target', '!use': 'use', '!powers': 'powers' };
  const rateLimiter = new RateLimiter({ default_seconds: 0 });
  const gameClient = makeGameClient(gameResponses);
  const twitchClient = makeTwitchClient();
  const parser = new CommandParser({ commandMap, rateLimiter, gameClient, twitchClient });
  return { parser, gameClient, twitchClient };
}

const TAGS = (username, display) => ({ username, 'display-name': display ?? username });
const CH = '#test';

const DIRECTION_SPEC = {
  id: 'direction',
  prompt: 'which direction? Reply: left / right / up / down',
  options: [
    { value: 'left',  aliases: ['l', 'west', 'w', '←', '⬅', '⬅️'] },
    { value: 'right', aliases: ['r', 'east', 'e', '→', '➡', '➡️'] },
    { value: 'up',    aliases: ['u', 'north', 'n', '↑', '⬆', '⬆️'] },
    { value: 'down',  aliases: ['d', 'south', 's', '↓', '⬇', '⬇️'] },
  ],
};

function needsFollowUp(targetName = 'Goblin') {
  return {
    success: false,
    error_code: 'needs_follow_up',
    power_id: 'lightning_bolt',
    power_name: 'Lightning Bolt',
    item_name: 'Lightning Bolt',
    target_name: targetName,
    follow_up: DIRECTION_SPEC,
  };
}

/** chat_participant_target responder: asks for direction until one arrives. */
function lightningResponder(payload) {
  if (payload.follow_up?.direction) {
    return {
      success: true,
      message: `⚡ Lightning Bolt tears ${payload.follow_up.direction}ward through Goblin and Skeleton — 8 damage each!`,
      item_name: 'Lightning Bolt',
      target_name: payload.target_name,
    };
  }
  return needsFollowUp(payload.target_name);
}

// ---------------------------------------------------------------------------
// PendingInteractions unit tests
// ---------------------------------------------------------------------------
describe('PendingInteractions.matchAnswer', () => {
  test('matches canonical values case-insensitively', () => {
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, ' Right ')).toBe('right');
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, 'LEFT')).toBe('left');
  });

  test('matches single-letter and arrow aliases', () => {
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, 'r')).toBe('right');
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, 'u')).toBe('up');
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, '→')).toBe('right');
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, '⬇️')).toBe('down');
  });

  test('recognizes cancel words', () => {
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, 'cancel')).toBe('cancel');
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, 'nevermind')).toBe('cancel');
  });

  test('returns null for ordinary chat', () => {
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, 'lol nice')).toBe(null);
    expect(PendingInteractions.matchAnswer(DIRECTION_SPEC, '')).toBe(null);
  });
});

describe('PendingInteractions state', () => {
  test('one pending interaction per user; begin replaces the old one', async () => {
    const p = new PendingInteractions({ timeoutMs: 30000 });
    const first = [];
    const second = [];
    p.begin('alice', { spec: DIRECTION_SPEC, onAnswer: v => first.push(v) });
    p.begin('alice', { spec: DIRECTION_SPEC, onAnswer: v => second.push(v) });
    expect(p.size()).toBe(1);
    await p.handleMessage('alice', 'left');
    expect(first).toEqual([]);
    expect(second).toEqual(['left']);
    expect(p.has('alice')).toBe(false);
  });

  test('non-matching messages do not consume the interaction', async () => {
    const p = new PendingInteractions({ timeoutMs: 30000 });
    const got = [];
    p.begin('bob', { spec: DIRECTION_SPEC, onAnswer: v => got.push(v) });
    expect(await p.handleMessage('bob', 'hello chat')).toBe(false);
    expect(p.has('bob')).toBe(true);
    expect(await p.handleMessage('bob', 'd')).toBe(true);
    expect(got).toEqual(['down']);
  });
});

// ---------------------------------------------------------------------------
// Full !use lightning bolt interaction through the CommandParser
// ---------------------------------------------------------------------------
describe('CommandParser — multi-step lightning bolt interaction', () => {
  test('!use lightning bolt → prompt → bare direction reply → executes', async () => {
    const { parser, gameClient, twitchClient } = makeParser({
      chat_participant_target: lightningResponder,
    });

    await parser.handle(CH, TAGS('zapfan', 'ZapFan'), '!use "lightning bolt" goblin');

    // Bot asked for the direction
    expect(twitchClient.replied.some(r => r.msg.includes('which direction'))).toBe(true);
    expect(gameClient.calls).toHaveLength(1);
    expect(gameClient.calls[0].payload.follow_up).toBeUndefined();

    // Bare reply (no ! prefix) resolves the interaction
    await parser.handle(CH, TAGS('zapfan', 'ZapFan'), 'right');

    expect(gameClient.calls).toHaveLength(2);
    expect(gameClient.calls[1].payload.follow_up).toEqual({ direction: 'right' });
    expect(gameClient.calls[1].payload.item_name).toBe('lightning bolt');
    expect(twitchClient.said.some(s => s.msg.includes('rightward'))).toBe(true);
  });

  test('direction aliases (r / arrows) resolve like the full word', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: lightningResponder,
    });
    await parser.handle(CH, TAGS('zapfan'), '!use "lightning bolt" goblin');
    await parser.handle(CH, TAGS('zapfan'), 'r');
    expect(gameClient.calls[1].payload.follow_up).toEqual({ direction: 'right' });
  });

  test('ordinary chat while pending is ignored; interaction stays alive', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: lightningResponder,
    });
    await parser.handle(CH, TAGS('zapfan'), '!use "lightning bolt" goblin');
    await parser.handle(CH, TAGS('zapfan'), 'poggers');
    expect(gameClient.calls).toHaveLength(1);
    await parser.handle(CH, TAGS('zapfan'), 'up');
    expect(gameClient.calls).toHaveLength(2);
    expect(gameClient.calls[1].payload.follow_up).toEqual({ direction: 'up' });
  });

  test('another user\'s messages never resolve someone else\'s interaction', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: lightningResponder,
    });
    await parser.handle(CH, TAGS('zapfan'), '!use "lightning bolt" goblin');
    await parser.handle(CH, TAGS('someoneelse'), 'right');
    expect(gameClient.calls).toHaveLength(1);
  });

  test('30s timeout cancels the interaction with a reply', async () => {
    jest.useFakeTimers();
    try {
      const { parser, gameClient, twitchClient } = makeParser({
        chat_participant_target: lightningResponder,
      });
      await parser.handle(CH, TAGS('zapfan', 'ZapFan'), '!use "lightning bolt" goblin');

      await jest.advanceTimersByTimeAsync(30000);
      expect(twitchClient.replied.some(r => r.msg.includes('fizzles'))).toBe(true);

      // Late reply does nothing
      await parser.handle(CH, TAGS('zapfan'), 'right');
      expect(gameClient.calls).toHaveLength(1);
    } finally {
      jest.useRealTimers();
    }
  });

  test('"cancel" reply aborts the interaction', async () => {
    const { parser, gameClient, twitchClient } = makeParser({
      chat_participant_target: lightningResponder,
    });
    await parser.handle(CH, TAGS('zapfan', 'ZapFan'), '!use "lightning bolt" goblin');
    await parser.handle(CH, TAGS('zapfan'), 'cancel');
    expect(twitchClient.replied.some(r => r.msg.includes('cancelled'))).toBe(true);
    await parser.handle(CH, TAGS('zapfan'), 'right');
    expect(gameClient.calls).toHaveLength(1);
  });

  test('a new !use replaces the old pending interaction', async () => {
    const { parser, gameClient, twitchClient } = makeParser({
      chat_participant_target: lightningResponder,
    });
    await parser.handle(CH, TAGS('zapfan'), '!use "lightning bolt" goblin');
    await parser.handle(CH, TAGS('zapfan'), '!use "lightning bolt" skeleton');
    expect(gameClient.calls).toHaveLength(2);

    await parser.handle(CH, TAGS('zapfan'), 'left');
    expect(gameClient.calls).toHaveLength(3);
    // The resolved interaction targets the NEW target
    expect(gameClient.calls[2].payload.target_name).toBe('skeleton');
    expect(gameClient.calls[2].payload.follow_up).toEqual({ direction: 'left' });
    expect(twitchClient.said.some(s => s.msg.includes('leftward'))).toBe(true);
  });

  test('interactions do not survive a bridge restart (fresh parser ignores replies)', async () => {
    const first = makeParser({ chat_participant_target: lightningResponder });
    await first.parser.handle(CH, TAGS('zapfan'), '!use "lightning bolt" goblin');

    // "Restart": a brand-new parser has no pending state.
    const second = makeParser({ chat_participant_target: lightningResponder });
    await second.parser.handle(CH, TAGS('zapfan'), 'right');
    expect(second.gameClient.calls).toHaveLength(0);
    expect(second.twitchClient.replied).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// !powers <name>
// ---------------------------------------------------------------------------
describe('CommandParser — !powers', () => {
  test('replies with the one-line description (aliases included)', async () => {
    const { parser, gameClient, twitchClient } = makeParser({
      chat_bridge_power_info: {
        found: true, power_id: 'battle_blessing', name: 'Battle Blessing',
        aliases: ['Bless'], description: 'Restore 1d8 hit points to one token.',
      },
    });
    await parser.handle(CH, TAGS('curious', 'Curious'), '!powers bless');
    expect(gameClient.calls[0].type).toBe('chat_bridge_power_info');
    expect(gameClient.calls[0].payload.name).toBe('bless');
    const reply = twitchClient.replied.find(r => r.msg.includes('Battle Blessing'));
    expect(reply).toBeTruthy();
    expect(reply.msg).toContain('aka Bless');
    expect(reply.msg).toContain('Restore 1d8');
  });

  test('unknown power → not-found reply', async () => {
    const { parser, twitchClient } = makeParser({
      chat_bridge_power_info: { found: false, query: 'wishcraft' },
    });
    await parser.handle(CH, TAGS('curious'), '!powers wishcraft');
    expect(twitchClient.replied.some(r => r.msg.includes("don't know a power"))).toBe(true);
  });

  test('no args → usage hint', async () => {
    const { parser, gameClient, twitchClient } = makeParser();
    await parser.handle(CH, TAGS('curious'), '!powers');
    expect(gameClient.calls).toHaveLength(0);
    expect(twitchClient.replied.some(r => r.msg.includes('usage'))).toBe(true);
  });

  test('!powers appears in !help', async () => {
    const commandMap = { '!use': 'use', '!powers': 'powers', '!help': 'help' };
    const rateLimiter = new RateLimiter({ default_seconds: 0 });
    const gameClient = makeGameClient();
    const twitchClient = makeTwitchClient();
    const parser = new CommandParser({ commandMap, rateLimiter, gameClient, twitchClient });
    await parser.handle(CH, TAGS('curious'), '!help');
    expect(twitchClient.replied.some(r => r.msg.includes('!powers <name>'))).toBe(true);
  });
});
