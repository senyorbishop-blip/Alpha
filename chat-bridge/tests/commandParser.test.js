'use strict';

const { CommandParser } = require('../src/commandParser');
const { RateLimiter }   = require('../src/rateLimiter');

// ---------------------------------------------------------------------------
// Lightweight mocks
// ---------------------------------------------------------------------------
function makeGameClient(responses = {}) {
  return {
    calls: [],
    async send(type, payload) {
      this.calls.push({ type, payload });
      return responses[type] ?? null;
    },
  };
}

function makeTwitchClient() {
  return {
    said: [],
    replied: [],
    say(channel, msg)           { this.said.push({ channel, msg }); },
    reply(channel, user, msg)   { this.replied.push({ channel, user, msg }); },
  };
}

function makeParser(gameResponses = {}, cooldownConfig = {}) {
  const commandMap = { '!join': 'join', '!leave': 'leave', '!inventory': 'inventory', '!target': 'target' };
  const rateLimiter = new RateLimiter({ default_seconds: 0, ...cooldownConfig });
  const gameClient = makeGameClient(gameResponses);
  const twitchClient = makeTwitchClient();
  const parser = new CommandParser({ commandMap, rateLimiter, gameClient, twitchClient });
  return { parser, gameClient, twitchClient };
}

const TAGS = (username, display) => ({ username, 'display-name': display ?? username });
const CH = '#test';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('CommandParser — !join', () => {
  test('sends chat_participant_join to game server', async () => {
    const { parser, gameClient } = makeParser({ chat_participant_join: { already_joined: false } });
    await parser.handle(CH, TAGS('viewer1', 'Viewer1'), '!join');
    expect(gameClient.calls[0].type).toBe('chat_participant_join');
    expect(gameClient.calls[0].payload.twitch_username).toBe('viewer1');
  });

  test('says welcome message on first join', async () => {
    const { parser, twitchClient } = makeParser({ chat_participant_join: { already_joined: false } });
    await parser.handle(CH, TAGS('alice', 'Alice'), '!join');
    expect(twitchClient.said.some(s => s.msg.includes('joined the tavern'))).toBe(true);
  });

  test('replies already_joined on idempotent join', async () => {
    const { parser, twitchClient } = makeParser({ chat_participant_join: { already_joined: true } });
    await parser.handle(CH, TAGS('bob'), '!join');
    expect(twitchClient.replied.some(r => r.msg.includes('already'))).toBe(true);
  });

  test('ignores messages not starting with !', async () => {
    const { parser, gameClient } = makeParser();
    await parser.handle(CH, TAGS('bob'), 'hello everyone');
    expect(gameClient.calls).toHaveLength(0);
  });

  test('ignores unknown commands', async () => {
    const { parser, gameClient } = makeParser();
    await parser.handle(CH, TAGS('bob'), '!unknown');
    expect(gameClient.calls).toHaveLength(0);
  });
});

describe('CommandParser — !target', () => {
  test('sends chat_participant_target with correct target_name', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'Fireball', target_name: 'Goblin' },
    });
    await parser.handle(CH, TAGS('charlie', 'Charlie'), '!target Goblin');
    expect(gameClient.calls[0].type).toBe('chat_participant_target');
    expect(gameClient.calls[0].payload.target_name).toBe('Goblin');
  });

  test('announces success in chat', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'Arrow', target_name: 'Orc Chief' },
    });
    await parser.handle(CH, TAGS('dave'), '!target Orc Chief');
    expect(twitchClient.said.some(s => s.msg.includes('Orc Chief'))).toBe(true);
  });

  test('replies with error message on failure', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_target: { success: false, message: 'You have no usable items.' },
    });
    await parser.handle(CH, TAGS('eve'), '!target Dragon');
    expect(twitchClient.replied.some(r => r.msg.includes('no usable items'))).toBe(true);
  });

  test('replies usage hint when no target provided', async () => {
    const { parser, twitchClient } = makeParser();
    await parser.handle(CH, TAGS('frank'), '!target');
    expect(twitchClient.replied.some(r => r.msg.includes('Usage'))).toBe(true);
  });

  test('strips HTML from target name', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'x', target_name: 'script' },
    });
    await parser.handle(CH, TAGS('hacker'), '!target <script>');
    const call = gameClient.calls[0];
    expect(call.payload.target_name).not.toContain('<');
    expect(call.payload.target_name).not.toContain('>');
  });
});

describe('CommandParser — !inventory', () => {
  test('replies with item list when items present', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_inventory: {
        found: true,
        items: [{ name: 'Potion', qty: 1, charges_current: 0, charges_max: 0 }],
      },
    });
    await parser.handle(CH, TAGS('grace'), '!inventory');
    expect(twitchClient.replied.some(r => r.msg.includes('Potion'))).toBe(true);
  });

  test('replies empty message when no items', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_inventory: { found: true, items: [] },
    });
    await parser.handle(CH, TAGS('henry'), '!inventory');
    expect(twitchClient.replied.some(r => r.msg.includes('empty'))).toBe(true);
  });

  test('prompts !join when participant not found', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_inventory: { found: false },
    });
    await parser.handle(CH, TAGS('iris'), '!inventory');
    expect(twitchClient.replied.some(r => r.msg.includes('!join'))).toBe(true);
  });
});

describe('CommandParser — rate limiting', () => {
  test('blocks repeated !join within cooldown', async () => {
    const { parser, gameClient, twitchClient } = makeParser(
      { chat_participant_join: { already_joined: false } },
      { default_seconds: 60, commands: { join: 60 } },
    );
    await parser.handle(CH, TAGS('jake'), '!join');
    await parser.handle(CH, TAGS('jake'), '!join');
    // Second !join should be rate-limited: no second game call, but a reply
    expect(gameClient.calls).toHaveLength(1);
    expect(twitchClient.replied.some(r => r.msg.includes('rejoin in'))).toBe(true);
  });

  test('allows a second user to join independently', async () => {
    const { parser, gameClient } = makeParser(
      { chat_participant_join: { already_joined: false } },
      { default_seconds: 60, commands: { join: 60 } },
    );
    await parser.handle(CH, TAGS('user1'), '!join');
    await parser.handle(CH, TAGS('user2'), '!join');
    expect(gameClient.calls).toHaveLength(2);
  });
});

describe('CommandParser — input sanitization', () => {
  test('rejects invalid Twitch username (special chars)', async () => {
    const { parser, gameClient } = makeParser();
    await parser.handle(CH, { username: '<script>', 'display-name': 'hacker' }, '!join');
    expect(gameClient.calls).toHaveLength(0);
  });

  test('target name truncated to 64 chars', async () => {
    const longName = 'A'.repeat(100);
    const { parser, gameClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'x', target_name: 'x' },
    });
    await parser.handle(CH, TAGS('user3'), `!target ${longName}`);
    if (gameClient.calls.length > 0) {
      expect(gameClient.calls[0].payload.target_name.length).toBeLessThanOrEqual(64);
    }
  });
});
