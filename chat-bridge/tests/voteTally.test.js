'use strict';

const { CommandParser } = require('../src/commandParser');
const { RateLimiter } = require('../src/rateLimiter');

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
    say(channel, msg) { this.said.push({ channel, msg }); },
    reply(channel, user, msg) { this.replied.push({ channel, user, msg }); },
  };
}

function makeParser(gameResponses = {}) {
  const commandMap = { '!vote': 'vote', '!join': 'join' };
  const rateLimiter = new RateLimiter({ default_seconds: 0 });
  const gameClient = makeGameClient(gameResponses);
  const twitchClient = makeTwitchClient();
  const parser = new CommandParser({ commandMap, rateLimiter, gameClient, twitchClient, responses: {} });
  return { parser, gameClient, twitchClient };
}

const TAGS = (username, display) => ({ username, 'display-name': display ?? username, 'user-id': `uid_${username}` });
const CH = '#test';

describe('Vote command — basic', () => {
  test('sends chat_bridge_poll_vote with 0-indexed option', async () => {
    const { parser, gameClient } = makeParser({
      chat_bridge_poll_vote: { success: true, option_label: 'Left', changed: false },
    });
    await parser.handle(CH, TAGS('alice'), '!vote 1');
    expect(gameClient.calls[0].type).toBe('chat_bridge_poll_vote');
    expect(gameClient.calls[0].payload.option_index).toBe(0);
    expect(gameClient.calls[0].payload.twitch_username).toBe('alice');
  });

  test('replies with confirmation on successful vote', async () => {
    const { parser, twitchClient } = makeParser({
      chat_bridge_poll_vote: { success: true, option_label: 'Right', changed: false },
    });
    await parser.handle(CH, TAGS('bob'), '!vote 2');
    expect(twitchClient.replied.some(r => r.msg.includes('Right') || r.msg.includes('2'))).toBe(true);
  });

  test('re-vote changes existing vote', async () => {
    const { parser, gameClient, twitchClient } = makeParser({
      chat_bridge_poll_vote: { success: true, option_label: 'Door B', changed: true },
    });
    await parser.handle(CH, TAGS('carol'), '!vote 2');
    expect(gameClient.calls[0].payload.option_index).toBe(1);
    expect(twitchClient.replied.some(r => r.msg.toLowerCase().includes('changed') || r.msg.includes('Door B'))).toBe(true);
  });

  test('invalid option number replies with correction once', async () => {
    const { parser, twitchClient } = makeParser({
      chat_bridge_poll_vote: { success: false, error_code: 'invalid_option', max_valid: 3 },
    });
    await parser.handle(CH, TAGS('dave'), '!vote 9');
    expect(twitchClient.replied.length).toBe(1);
    // Second invalid attempt is silent
    await parser.handle(CH, TAGS('dave'), '!vote 9');
    expect(twitchClient.replied.length).toBe(1);
  });

  test('no active poll replies with message', async () => {
    const { parser, twitchClient } = makeParser({
      chat_bridge_poll_vote: { success: false, error_code: 'no_active_poll' },
    });
    await parser.handle(CH, TAGS('eve'), '!vote 1');
    expect(twitchClient.replied.some(r => r.msg.toLowerCase().includes('no') || r.msg.toLowerCase().includes('vote'))).toBe(true);
  });

  test('non-numeric vote argument is rejected without server call', async () => {
    const { parser, gameClient, twitchClient } = makeParser();
    await parser.handle(CH, TAGS('frank'), '!vote banana');
    // No server call for invalid input
    expect(gameClient.calls.length).toBe(0);
    expect(twitchClient.replied.length).toBe(1);
  });

  test('multiple users can vote independently', async () => {
    const { parser, gameClient } = makeParser({
      chat_bridge_poll_vote: { success: true, option_label: 'A', changed: false },
    });
    await parser.handle(CH, TAGS('user1'), '!vote 1');
    await parser.handle(CH, TAGS('user2'), '!vote 2');
    expect(gameClient.calls.length).toBe(2);
    expect(gameClient.calls[0].payload.twitch_username).toBe('user1');
    expect(gameClient.calls[1].payload.twitch_username).toBe('user2');
    expect(gameClient.calls[1].payload.option_index).toBe(1);
  });
});
