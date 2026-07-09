'use strict';

const {
  ArenaProgression,
  CLASSES,
  ABILITIES,
  SHOP_CATALOG,
  xpForNextLevel,
  rollAbilityScore,
  XP_WIN,
  GOLD_WIN,
  STARTING_GOLD,
} = require('../src/arenaProgression');

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

function makeGameClient(responses = {}) {
  return {
    calls: [],
    async send(type, payload) {
      this.calls.push({ type, payload });
      const r = responses[type];
      return typeof r === 'function' ? r(payload) : (r ?? null);
    },
  };
}

function makeTwitch() {
  return {
    said: [],
    replied: [],
    say(channel, msg)         { this.said.push({ channel, msg }); },
    reply(channel, user, msg) { this.replied.push({ channel, user, msg }); },
  };
}

function makeProgression(gameResponses = {}) {
  const game = makeGameClient(gameResponses);
  const twitch = makeTwitch();
  const progression = new ArenaProgression({
    gameClient: game,
    twitchClient: twitch,
    logger: { info: () => {}, warn: () => {}, error: () => {} },
  });
  return { progression, game, twitch };
}

const CH = '#test';

function allMessages(twitch) {
  return [...twitch.said.map(s => s.msg), ...twitch.replied.map(r => r.msg)].join('\n');
}

// ---------------------------------------------------------------------------
// Character creation
// ---------------------------------------------------------------------------

describe('ArenaProgression — character creation', () => {
  test('4d6-drop-lowest rolls stay in the 3–18 range', () => {
    for (let i = 0; i < 200; i++) {
      const score = rollAbilityScore();
      expect(score).toBeGreaterThanOrEqual(3);
      expect(score).toBeLessThanOrEqual(18);
    }
  });

  test('new character has a valid random class, all abilities, and starting gold', () => {
    const { progression } = makeProgression();
    const sheet = progression.createCharacter();
    expect(Object.keys(CLASSES)).toContain(sheet.class);
    for (const a of ABILITIES) {
      expect(sheet.abilities[a]).toBeGreaterThanOrEqual(3);
      expect(sheet.abilities[a]).toBeLessThanOrEqual(18);
    }
    expect(sheet.level).toBe(1);
    expect(sheet.xp).toBe(0);
    expect(sheet.gold).toBe(STARTING_GOLD);
    expect(sheet.equipped).toEqual({ weapon: null, armor: null, trinket: null });
  });

  test('first !stats creates + announces a character and syncs it to the server', async () => {
    const { progression, game, twitch } = makeProgression({
      chat_participant_arena_load: { found: false },
    });
    await progression.handleCommand(CH, 'alice', 'Alice', 'stats', []);
    // Announced the new roll
    expect(twitch.said.some(s => s.msg.includes('rolls a new arena character'))).toBe(true);
    // Load then sync
    expect(game.calls.map(c => c.type)).toContain('chat_participant_arena_load');
    expect(game.calls.map(c => c.type)).toContain('chat_participant_arena_sync');
    // Stats reply includes class, XP, and gold
    const reply = twitch.replied[0].msg;
    expect(reply).toMatch(/L1/);
    expect(reply).toContain('XP 0/100');
    expect(reply).toContain(`${STARTING_GOLD} gold`);
  });

  test('existing server sheet is loaded, not recreated', async () => {
    const { progression, game } = makeProgression({
      chat_participant_arena_load: {
        found: true,
        arena_character: {
          class: 'Wizard', level: 3, xp: 50, gold: 200,
          abilities: { str: 8, dex: 12, con: 14, int: 17, wis: 10, cha: 11 },
          items: [], equipped: {},
          wins: 4, losses: 1,
        },
      },
    });
    const { sheet, isNew } = await progression.getSheet('mage');
    expect(isNew).toBe(false);
    expect(sheet.class).toBe('Wizard');
    expect(sheet.level).toBe(3);
    expect(sheet.abilities.int).toBe(17);
    // No sync for an already-persisted sheet
    expect(game.calls.map(c => c.type)).not.toContain('chat_participant_arena_sync');
  });
});

// ---------------------------------------------------------------------------
// XP / levels
// ---------------------------------------------------------------------------

describe('ArenaProgression — XP and !levelup', () => {
  test('!levelup below the XP threshold shows progress', async () => {
    const { progression, twitch } = makeProgression({ chat_participant_arena_load: { found: false } });
    await progression.handleCommand(CH, 'bob', 'Bob', 'levelup', []);
    expect(allMessages(twitch)).toContain('0/100 XP');
  });

  test('!levelup with enough XP raises level and abilities', async () => {
    const { progression, twitch, game } = makeProgression({ chat_participant_arena_load: { found: false } });
    const { sheet } = await progression.getSheet('carl');
    sheet.xp = xpForNextLevel(1) + 30;
    const primary = CLASSES[sheet.class];
    const beforePrimary = sheet.abilities[primary];
    const abilitySumBefore = ABILITIES.reduce((s, a) => s + sheet.abilities[a], 0);

    await progression.handleCommand(CH, 'carl', 'Carl', 'levelup', []);

    expect(sheet.level).toBe(2);
    expect(sheet.xp).toBe(30);
    expect(sheet.abilities[primary]).toBe(beforePrimary + 1);
    expect(ABILITIES.reduce((s, a) => s + sheet.abilities[a], 0)).toBe(abilitySumBefore + 2);
    expect(twitch.said.some(s => s.msg.includes('Level 2'))).toBe(true);
    // Synced after the level-up
    expect(game.calls.filter(c => c.type === 'chat_participant_arena_sync').length).toBeGreaterThanOrEqual(2);
  });

  test('duel results award XP and gold to both fighters', async () => {
    const { progression, twitch } = makeProgression({ chat_participant_arena_load: { found: false } });
    const { sheet: winner } = await progression.getSheet('winna');
    const { sheet: loser } = await progression.getSheet('losah');

    await progression.recordDuelResult('winna', 'losah', CH);

    expect(winner.wins).toBe(1);
    expect(winner.streak).toBe(1);
    expect(winner.xp).toBe(XP_WIN);
    expect(winner.gold).toBe(STARTING_GOLD + GOLD_WIN);
    expect(loser.losses).toBe(1);
    expect(loser.streak).toBe(0);
    expect(loser.xp).toBeGreaterThan(0);
    expect(allMessages(twitch)).toContain('XP');
  });
});

// ---------------------------------------------------------------------------
// Shop / bag / equip / stats
// ---------------------------------------------------------------------------

describe('ArenaProgression — shop, buy, equip', () => {
  test('!shop lists items with prices', async () => {
    const { progression, twitch } = makeProgression();
    await progression.handleCommand(CH, 'dee', 'Dee', 'shop', []);
    const msg = twitch.replied[0].msg;
    for (const item of SHOP_CATALOG.slice(0, 3)) {
      expect(msg).toContain(item.name);
      expect(msg).toContain(`${item.cost}g`);
    }
    expect(msg).toContain('!buy');
  });

  test('!buy with insufficient gold is refused', async () => {
    const { progression, twitch } = makeProgression({ chat_participant_arena_load: { found: false } });
    const { sheet } = await progression.getSheet('poor');
    sheet.gold = 0;
    await progression.handleCommand(CH, 'poor', 'Poor', 'buy', ['Steel', 'Sword']);
    expect(allMessages(twitch)).toContain('you have 0');
    expect(sheet.items).toHaveLength(0);
  });

  test('!buy deducts gold, adds the item, and auto-equips an empty slot', async () => {
    const { progression, twitch } = makeProgression({ chat_participant_arena_load: { found: false } });
    const { sheet } = await progression.getSheet('rich');
    sheet.gold = 500;
    await progression.handleCommand(CH, 'rich', 'Rich', 'buy', ['Rusty', 'Blade']);
    expect(sheet.gold).toBe(475);
    expect(sheet.items.map(i => i.id)).toContain('rusty_blade');
    expect(sheet.equipped.weapon).toBe('rusty_blade');
    expect(allMessages(twitch)).toContain('equipped');
  });

  test('!equip swaps gear and reports new combat stats', async () => {
    const { progression, twitch } = makeProgression({ chat_participant_arena_load: { found: false } });
    const { sheet } = await progression.getSheet('gearhead');
    sheet.gold = 1000;
    await progression.handleCommand(CH, 'gearhead', 'G', 'buy', ['rusty_blade']);
    await progression.handleCommand(CH, 'gearhead', 'G', 'buy', ['steel']);
    // steel sword was bought second — weapon slot already filled by rusty blade
    expect(sheet.equipped.weapon).toBe('rusty_blade');
    await progression.handleCommand(CH, 'gearhead', 'G', 'equip', ['steel']);
    expect(sheet.equipped.weapon).toBe('steel_sword');
    expect(allMessages(twitch)).toContain('Equipped Steel Sword');
  });

  test('!bag lists owned items and marks equipped gear', async () => {
    const { progression, twitch } = makeProgression({ chat_participant_arena_load: { found: false } });
    const { sheet } = await progression.getSheet('hoarder');
    sheet.gold = 500;
    await progression.handleCommand(CH, 'hoarder', 'H', 'buy', ['iron', 'ring']);
    twitch.replied.length = 0;
    await progression.handleCommand(CH, 'hoarder', 'H', 'bag', []);
    const msg = twitch.replied[0].msg;
    expect(msg).toContain('Iron Ring✦');
    expect(msg).toContain('equipped');
  });

  test('!stats shows ability totals including item bonuses', async () => {
    const { progression, twitch } = makeProgression({
      chat_participant_arena_load: {
        found: true,
        arena_character: {
          class: 'Barbarian', level: 1, xp: 0, gold: 0,
          abilities: { str: 14, dex: 10, con: 12, int: 8, wis: 10, cha: 10 },
          items: [{ id: 'giants_belt', name: "Giant's Belt", slot: 'trinket', rarity: 'uncommon', bonuses: { str: 2, hp: 4 } }],
          equipped: { weapon: null, armor: null, trinket: 'giants_belt' },
          wins: 0, losses: 0,
        },
      },
    });
    await progression.handleCommand(CH, 'belted', 'B', 'stats', []);
    const msg = twitch.replied[0].msg;
    // STR 14 base + 2 from Giant's Belt = 16 shown with breakdown
    expect(msg).toContain('STR 16 (14+2)');
    // Belt hp bonus is reflected in max HP: 20 + con mod (1)*2 + 4 = 26
    expect(msg).toContain('HP 26');
  });
});

// ---------------------------------------------------------------------------
// Combat stats derivation
// ---------------------------------------------------------------------------

describe('ArenaProgression — combat stats', () => {
  test('combatStats derive from abilities, level, and equipped gear', () => {
    const { progression } = makeProgression();
    const sheet = progression.createCharacter();
    sheet.class = 'Fighter';
    sheet.level = 3;
    sheet.abilities = { str: 16, dex: 14, con: 14, int: 10, wis: 10, cha: 10 };
    sheet.items = [
      { id: 'steel_sword', name: 'Steel Sword', slot: 'weapon', bonuses: { atk: 2 } },
      { id: 'chain_shirt', name: 'Chain Shirt', slot: 'armor', bonuses: { ac: 2 } },
    ];
    sheet.equipped = { weapon: 'steel_sword', armor: 'chain_shirt', trinket: null };

    const stats = progression.combatStats(sheet);
    // HP: 20 + con mod (2)*2 + (3-1)*3 = 30
    expect(stats.maxHp).toBe(30);
    // AC: 12 + dex mod (2) + 2 = 16
    expect(stats.ac).toBe(16);
    // ATK: str mod (3) + 2 = 5
    expect(stats.atkBonus).toBe(5);
  });
});

// ---------------------------------------------------------------------------
// Leaderboard
// ---------------------------------------------------------------------------

describe('ArenaProgression — leaderboard', () => {
  test('!leaderboard formats server entries', async () => {
    const { progression, twitch } = makeProgression({
      chat_participant_arena_leaderboard: {
        entries: [
          { twitch_username: 'a', display_name: 'A', character_name: 'Grognak', class: 'Barbarian', level: 4, wins: 9, losses: 2 },
          { twitch_username: 'b', display_name: 'B', character_name: '', class: 'Wizard', level: 2, wins: 3, losses: 5 },
        ],
      },
    });
    await progression.handleCommand(CH, 'x', 'X', 'leaderboard', []);
    const msg = twitch.said[0].msg;
    expect(msg).toContain('1. Grognak (Barbarian L4) 9W/2L');
    expect(msg).toContain('2. B (Wizard L2) 3W/5L');
  });

  test('!leaderboard with no battles yet nudges toward !duel', async () => {
    const { progression, twitch } = makeProgression({
      chat_participant_arena_leaderboard: { entries: [] },
    });
    await progression.handleCommand(CH, 'x', 'X', 'leaderboard', []);
    expect(twitch.replied[0].msg).toContain('!duel');
  });
});
