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
  const commandMap = { '!join': 'join', '!leave': 'leave', '!inventory': 'inventory', '!target': 'target', '!use': 'use' };
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
    expect(twitchClient.said.some(s => s.msg.includes('entered the tavern'))).toBe(true);
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
    expect(twitchClient.replied.some(r => r.msg.toLowerCase().includes('usage'))).toBe(true);
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

describe('CommandParser — @-mention normalization', () => {
  test('strips a single leading @ from every arg', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'x', target_name: 'Goblin' },
    });
    await parser.handle(CH, TAGS('alice'), '!target @Goblin');
    expect(gameClient.calls[0].payload.target_name).toBe('Goblin');
  });

  test('arena commands receive @-stripped args', async () => {
    const commandMap = { '!duel': 'arena', '!accept': 'arena' };
    const rateLimiter = new RateLimiter({ default_seconds: 0 });
    const arenaCalls = [];
    const parser = new CommandParser({
      commandMap,
      rateLimiter,
      gameClient: makeGameClient(),
      twitchClient: makeTwitchClient(),
      arenaHandler: (ch, username, displayName, action, args) => {
        arenaCalls.push({ action, args });
      },
    });
    await parser.handle(CH, TAGS('alice', 'Alice'), '!duel @PotatoWizard');
    expect(arenaCalls).toEqual([{ action: 'duel', args: ['PotatoWizard'] }]);
  });

  test('!use with an @-mentioned target keeps the item name intact', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'Fireball', target_name: 'PotatoWizard' },
    });
    await parser.handle(CH, TAGS('alice'), '!use fireball @PotatoWizard');
    expect(gameClient.calls[0].payload.item_name).toBe('fireball');
    expect(gameClient.calls[0].payload.target_name).toBe('PotatoWizard');
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

// Full command map with aliases + progression commands, mirroring
// config/commands.json.
const FULL_COMMAND_MAP = {
  '!join': 'join', '!leave': 'leave', '!inventory': 'inventory', '!inv': 'inventory',
  '!bag': 'bag', '!target': 'target', '!use': 'use',
  '!help': 'help', '!commands': 'help', '!command': 'help',
  '!vote': 'vote', '!name': 'name', '!me': 'me', '!character': 'me',
  '!stats': 'stats', '!sheet': 'stats', '!equip': 'equip', '!shop': 'shop',
  '!buy': 'buy', '!levelup': 'levelup', '!leaderboard': 'leaderboard',
  '!duel': 'arena', '!accept': 'arena', '!decline': 'arena',
};

function makeFullParser(gameResponses = {}, cooldownConfig = {}) {
  const rateLimiter = new RateLimiter({ default_seconds: 0, ...cooldownConfig });
  const gameClient = makeGameClient(gameResponses);
  const twitchClient = makeTwitchClient();
  const progressionCalls = [];
  const parser = new CommandParser({
    commandMap: FULL_COMMAND_MAP,
    rateLimiter,
    gameClient,
    twitchClient,
    progressionHandler: async (ch, username, displayName, action, args) => {
      progressionCalls.push({ username, action, args });
    },
  });
  return { parser, gameClient, twitchClient, progressionCalls };
}

describe('CommandParser — command aliases', () => {
  test('!commands and !command act as !help', async () => {
    const { parser, twitchClient } = makeFullParser();
    await parser.handle(CH, TAGS('alice'), '!commands');
    await parser.handle(CH, TAGS('bob'), '!command');
    expect(twitchClient.replied).toHaveLength(2);
    for (const r of twitchClient.replied) {
      expect(r.msg).toContain('!join');
      expect(r.msg).toContain('!help');
    }
  });

  test('!character acts as !me', async () => {
    const { parser, gameClient } = makeFullParser({
      chat_participant_me: { found: true, lifetime_stats: {}, arena_stats: {} },
    });
    await parser.handle(CH, TAGS('carol'), '!character');
    expect(gameClient.calls[0].type).toBe('chat_participant_me');
  });

  test('!sheet dispatches the stats progression action', async () => {
    const { parser, progressionCalls } = makeFullParser();
    await parser.handle(CH, TAGS('dana'), '!sheet');
    expect(progressionCalls).toEqual([{ username: 'dana', action: 'stats', args: [] }]);
  });

  test('!inv acts as !inventory', async () => {
    const { parser, gameClient } = makeFullParser({
      chat_participant_inventory: { found: true, items: [] },
    });
    await parser.handle(CH, TAGS('erin'), '!inv');
    expect(gameClient.calls[0].type).toBe('chat_participant_inventory');
  });
});

describe('CommandParser — !help usage hints', () => {
  test('lists every implemented command with usage hints, aliases collapsed', async () => {
    const { parser, twitchClient } = makeFullParser();
    await parser.handle(CH, TAGS('frank'), '!help');
    const msg = twitchClient.replied[0].msg;
    // usage hints
    expect(msg).toContain('!target <name>');
    expect(msg).toContain('!use <item> <target>');
    expect(msg).toContain('!equip <item>');
    expect(msg).toContain('!buy <item>');
    expect(msg).toContain('!vote <n>');
    expect(msg).toContain('!duel <name>');
    // all progression + arena commands present
    for (const cmd of ['!stats', '!bag', '!shop', '!levelup', '!leaderboard', '!accept', '!decline', '!me', '!name']) {
      expect(msg).toContain(cmd);
    }
    // aliases collapse — the alias forms don't appear as separate entries
    expect(msg).not.toContain('!sheet');
    expect(msg).not.toContain('!character');
    expect(msg).not.toContain('!commands');
  });
});

describe('CommandParser — unknown command nudge', () => {
  test('unknown command from a joined chatter gets one !help pointer', async () => {
    const { parser, twitchClient } = makeFullParser(
      { chat_participant_join: { already_joined: false } },
      { default_seconds: 0, commands: { unknown_cmd: 60 } },
    );
    await parser.handle(CH, TAGS('gail'), '!join');
    twitchClient.replied.length = 0;
    twitchClient.said.length = 0;

    await parser.handle(CH, TAGS('gail'), '!dance');
    expect(twitchClient.replied).toHaveLength(1);
    expect(twitchClient.replied[0].msg).toContain('!help');

    // Per-user cooldown: repeated unknown commands stay silent
    await parser.handle(CH, TAGS('gail'), '!flkjsdf');
    await parser.handle(CH, TAGS('gail'), '!dance');
    expect(twitchClient.replied).toHaveLength(1);
  });

  test('unknown command from a NON-joined chatter is silently ignored', async () => {
    const { parser, twitchClient, gameClient } = makeFullParser();
    await parser.handle(CH, TAGS('lurker'), '!discord');
    expect(twitchClient.replied).toHaveLength(0);
    expect(twitchClient.said).toHaveLength(0);
    expect(gameClient.calls).toHaveLength(0);
  });
});

describe('CommandParser — progression command rate limiting', () => {
  test('progression commands respect per-command cooldowns', async () => {
    const { parser, progressionCalls, twitchClient } = makeFullParser(
      {},
      { default_seconds: 60 },
    );
    await parser.handle(CH, TAGS('hank'), '!stats');
    await parser.handle(CH, TAGS('hank'), '!stats');
    expect(progressionCalls).toHaveLength(1);
    expect(twitchClient.replied.some(r => r.msg.toLowerCase().includes('wait'))).toBe(true);
  });
});

describe('CommandParser — !target usability (no item / not joined)', () => {
  test("chatter with no usable item gets the 'nothing to use yet' reply", async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_target: { success: false, error_code: 'no_item', message: 'Grace has no usable items.' },
    });
    await parser.handle(CH, TAGS('grace'), '!target Goblin');
    expect(twitchClient.replied).toHaveLength(1);
    expect(twitchClient.replied[0].msg).toContain("don't have anything to use yet");
    expect(twitchClient.replied[0].msg.toLowerCase()).toContain('subs and cheers');
  });

  test('not-joined chatter is pointed to !join', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_target: { success: false, error_code: 'not_joined', message: 'x has not joined.' },
    });
    await parser.handle(CH, TAGS('newbie'), '!target Goblin');
    expect(twitchClient.replied[0].msg).toContain('!join');
  });
});

describe('CommandParser — !inventory trigger hints', () => {
  test('single item keeps the simple !target hint', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_inventory: {
        found: true,
        items: [{ name: 'Fireball', qty: 1, charges_current: 0, charges_max: 0 }],
      },
    });
    await parser.handle(CH, TAGS('ivy'), '!inventory');
    expect(twitchClient.replied[0].msg).toContain('Fireball x1 (use: !target <name>)');
  });

  test('2+ items switch hints to the !use form, quoting multi-word names', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_inventory: {
        found: true,
        items: [
          { name: 'Fireball', qty: 1, charges_current: 0, charges_max: 0 },
          { name: 'Healing Spark', qty: 2, charges_current: 0, charges_max: 0 },
          { name: 'Wand of Zaps', qty: 1, charges_current: 2, charges_max: 3 },
        ],
      },
    });
    await parser.handle(CH, TAGS('ivy'), '!inventory');
    const msg = twitchClient.replied[0].msg;
    expect(msg).toContain('Fireball x1 (use: !use fireball <target>)');
    expect(msg).toContain('Healing Spark x2 (use: !use "healing spark" <target>)');
    expect(msg).toContain('Wand of Zaps 2/3 (use: !use "wand of zaps" <target>)');
    expect(msg).not.toContain('!target <name>');
  });
});

describe('CommandParser — !target with one vs many items', () => {
  test('one-item !target sends no item_name (server picks the single item)', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'Fireball', target_name: 'Goblin' },
    });
    await parser.handle(CH, TAGS('solo'), '!target Goblin');
    expect(gameClient.calls[0].type).toBe('chat_participant_target');
    expect(gameClient.calls[0].payload.item_name).toBeUndefined();
  });

  test('multi-item !target asks the chatter to pick with !use', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_target: {
        success: false,
        error_code: 'multiple_items',
        items: ['Fireball', 'Healing Spark'],
      },
    });
    await parser.handle(CH, TAGS('hoarder', 'Hoarder'), '!target Goblin');
    expect(twitchClient.replied).toHaveLength(1);
    const msg = twitchClient.replied[0].msg;
    expect(msg).toContain("you're holding: Fireball, Healing Spark");
    expect(msg).toContain('!use <item> <target>');
  });
});

describe('CommandParser — !use <item> <target>', () => {
  test('sends item_name and target_name for an exact item', async () => {
    const { parser, gameClient, twitchClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'Fireball', target_name: 'Goblin' },
    });
    await parser.handle(CH, TAGS('kate'), '!use fireball goblin');
    const call = gameClient.calls[0];
    expect(call.type).toBe('chat_participant_target');
    expect(call.payload.item_name).toBe('fireball');
    expect(call.payload.target_name).toBe('goblin');
    expect(twitchClient.said.some(s => s.msg.includes('Fireball') && s.msg.includes('Goblin'))).toBe(true);
  });

  test('quoted multi-word item name spans spaces', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'Healing Spark', target_name: 'Grognak' },
    });
    await parser.handle(CH, TAGS('lena'), '!use "healing spark" grognak');
    const call = gameClient.calls[0];
    expect(call.payload.item_name).toBe('healing spark');
    expect(call.payload.target_name).toBe('grognak');
  });

  test('unquoted first word is the item, rest is the target', async () => {
    const { parser, gameClient } = makeParser({
      chat_participant_target: { success: true, item_name: 'Fireball', target_name: 'Goblin King' },
    });
    await parser.handle(CH, TAGS('mia'), '!use fireball goblin king');
    const call = gameClient.calls[0];
    expect(call.payload.item_name).toBe('fireball');
    expect(call.payload.target_name).toBe('goblin king');
  });

  test('ambiguous item match lists the matching items', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_target: {
        success: false,
        error_code: 'item_ambiguous',
        matching_items: ['Wand of Zaps', 'Wand of Fire'],
      },
    });
    await parser.handle(CH, TAGS('nina'), '!use wand goblin');
    expect(twitchClient.replied).toHaveLength(1);
    expect(twitchClient.replied[0].msg).toContain('Wand of Zaps, Wand of Fire');
  });

  test('unowned item lists what the chatter DOES have', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_target: {
        success: false,
        error_code: 'item_not_owned',
        owned_items: ['Pebble', 'Healing Spark'],
      },
    });
    await parser.handle(CH, TAGS('omar'), '!use excalibur goblin');
    expect(twitchClient.replied).toHaveLength(1);
    const msg = twitchClient.replied[0].msg;
    expect(msg).toContain("don't have 'excalibur'");
    expect(msg).toContain('Pebble, Healing Spark');
  });

  test('!use with no target replies with usage, no game call', async () => {
    const { parser, gameClient, twitchClient } = makeParser();
    await parser.handle(CH, TAGS('pam'), '!use fireball');
    expect(gameClient.calls).toHaveLength(0);
    expect(twitchClient.replied[0].msg).toContain('!use <item> <target>');
  });

  test('friendly-fire block is relayed to the chatter', async () => {
    const { parser, twitchClient } = makeParser({
      chat_participant_target: {
        success: false,
        error_code: 'friendly_fire_blocked',
        message: 'Elowen is in the party — the DM has friendly fire turned off. Heals and blessings still work!',
      },
    });
    await parser.handle(CH, TAGS('quin'), '!use fireball elowen');
    expect(twitchClient.replied[0].msg).toContain('friendly fire turned off');
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
