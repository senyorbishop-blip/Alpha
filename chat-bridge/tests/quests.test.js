'use strict';

const { QuestManager, DEFAULT_QUEST_CONFIG } = require('../src/quests');
const { ArenaProgression, SHOP_CATALOG, STARTING_GOLD } = require('../src/arenaProgression');
const { Arena } = require('../src/arena');

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const CH = '#test';
const silentLogger = { info: () => {}, warn: () => {}, error: () => {} };

function makeGameClient(responses = {}) {
  return {
    calls: [],
    async send(type, payload) {
      this.calls.push({ type, payload });
      const r = responses[type];
      return typeof r === 'function' ? r(payload) : (r ?? null);
    },
    callsOf(type) { return this.calls.filter(c => c.type === type); },
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

/** Deterministic RNG: pops from `values`, then falls back to 0.999. */
function makeRand(values = []) {
  const queue = [...values];
  return () => (queue.length ? queue.shift() : 0.999);
}

// One test quest with easily-controlled odds.
const TEST_QUEST = {
  id: 'test_grounds',
  name: 'The Testing Grounds',
  risk: 'hard',
  duration_minutes: 15,
  xp: [10, 10],
  gold: [5, 5],
  drop_chance: 0.5,
  injury_chance: 0.5,
  death_chance: 0.5,
  flavor: { depart: ['Off they go.'], success: ['Made it.'], injury: ['Ouch.'], death: ['Gone.'] },
};

function makeQuestSetup({ gameResponses = {}, config = {}, rand } = {}) {
  const game = makeGameClient({
    chat_participant_arena_load: { found: false },
    chat_participant_arena_sync: { success: true },
    ...gameResponses,
  });
  const twitch = makeTwitch();
  const progression = new ArenaProgression({ gameClient: game, twitchClient: twitch, logger: silentLogger });
  const quests = new QuestManager({
    gameClient: game,
    twitchClient: twitch,
    progression,
    config: { quests: [TEST_QUEST], ...config },
    channel: CH,
    logger: silentLogger,
    rand: rand || makeRand(),
  });
  progression.attachQuests(quests);
  managers.push(quests);
  return { game, twitch, progression, quests };
}

const managers = [];
afterEach(() => { while (managers.length) managers.pop().destroy(); });

function allMessages(twitch) {
  return [...twitch.said.map(s => s.msg), ...twitch.replied.map(r => r.msg)].join('\n');
}

/** Put an overdue quest on a cached sheet so the next command resolves it. */
async function questingSheet(progression, username, { overdue = true } = {}) {
  const { sheet } = await progression.getSheet(username);
  sheet.quest = {
    id: TEST_QUEST.id,
    name: TEST_QUEST.name,
    risk: 'hard',
    local_ends_at: Date.now() + (overdue ? -1000 : 60_000),
  };
  return sheet;
}

// ---------------------------------------------------------------------------
// Quest listing + start
// ---------------------------------------------------------------------------

describe('QuestManager — listing and start', () => {
  test('!quest lists quests with duration, risk, and visible death odds', async () => {
    const { twitch, quests } = makeQuestSetup();
    await quests.handleCommand(CH, 'dave', 'Dave', 'quest', []);
    const msg = twitch.replied[twitch.replied.length - 1].msg;
    expect(msg).toContain('1) The Testing Grounds');
    expect(msg).toContain('15m');
    expect(msg).toContain('☠ 50% death risk'); // death odds are config-visible
    expect(msg).toContain('!quest <number>');
  });

  test('default config death odds only on the hard quest, and kept low', () => {
    const hard = DEFAULT_QUEST_CONFIG.quests.filter(q => (q.death_chance || 0) > 0);
    expect(hard.length).toBe(1);
    expect(hard[0].risk).toBe('hard');
    expect(hard[0].death_chance).toBeGreaterThan(0);
    expect(hard[0].death_chance).toBeLessThanOrEqual(0.10);
  });

  test('!quest <n> starts the quest via the server and announces departure', async () => {
    const { game, twitch, progression, quests } = makeQuestSetup({
      gameResponses: {
        chat_participant_quest_start: (payload) => ({
          success: true, quest: { ...payload.quest, started_at: 1, ends_at: 901 }, remaining_s: 900,
        }),
      },
    });
    await quests.handleCommand(CH, 'dave', 'Dave', 'quest', ['1']);

    const started = game.callsOf('chat_participant_quest_start');
    expect(started).toHaveLength(1);
    expect(started[0].payload.quest).toMatchObject({
      id: 'test_grounds', duration_s: 900, risk: 'hard',
    });
    expect(started[0].payload.quest.cooldown_s).toBe(DEFAULT_QUEST_CONFIG.quest_cooldown_minutes * 60);

    const sheet = progression.peekSheet('dave');
    expect(sheet.quest.id).toBe('test_grounds');
    expect(sheet.quest.local_ends_at).toBeGreaterThan(Date.now());
    expect(allMessages(twitch)).toContain('sets off on The Testing Grounds');
    expect(allMessages(twitch)).toContain('Off they go.');
  });

  test('server cooldown / already_questing errors render friendly replies', async () => {
    const { twitch, quests } = makeQuestSetup({
      gameResponses: {
        chat_participant_quest_start: { success: false, error_code: 'cooldown', remaining_s: 300 },
      },
    });
    await quests.handleCommand(CH, 'dave', 'Dave', 'quest', ['1']);
    expect(allMessages(twitch)).toContain('resting after the last quest');
    expect(allMessages(twitch)).toContain('5m');
  });
});

// ---------------------------------------------------------------------------
// Busy state (while questing)
// ---------------------------------------------------------------------------

describe('QuestManager — busy state blocks commands', () => {
  test('!quest while questing reports the return countdown', async () => {
    const { twitch, progression, quests } = makeQuestSetup();
    await questingSheet(progression, 'dave', { overdue: false });
    await quests.handleCommand(CH, 'dave', 'Dave', 'quest', ['1']);
    expect(allMessages(twitch)).toMatch(/off questing — The Testing Grounds \(returns in/);
  });

  test('!duel is blocked for a questing challenger and a questing target', async () => {
    const { twitch, progression } = makeQuestSetup();
    await questingSheet(progression, 'alice', { overdue: false });
    const arena = new Arena({
      twitchClient: twitch, progression, logger: silentLogger,
      config: { duel_cooldown_seconds: 0 },
    });
    try {
      await arena.handleCommand(CH, 'alice', 'Alice', 'duel', ['bob']);
      expect(arena._challenges.size).toBe(0);
      expect(allMessages(twitch)).toContain('You are off questing');

      // A free challenger cannot duel the questing chatter either
      await arena.handleCommand(CH, 'bob', 'Bob', 'duel', ['alice']);
      expect(arena._challenges.size).toBe(0);
      expect(allMessages(twitch)).toContain('@alice is off questing');
    } finally {
      arena.destroy();
    }
  });

  test('!heal (item use) is blocked while questing', async () => {
    const { twitch, progression } = makeQuestSetup();
    const sheet = await questingSheet(progression, 'dave', { overdue: false });
    sheet.potions = 1;
    sheet.injuries = 5;
    await progression.handleCommand(CH, 'dave', 'Dave', 'heal', []);
    expect(sheet.potions).toBe(1); // untouched
    expect(allMessages(twitch)).toContain('no potions until');
  });

  test('overdue quests resolve lazily before a progression command runs', async () => {
    const { game, twitch, progression } = makeQuestSetup({
      gameResponses: {
        chat_participant_quest_resolve: {
          status: 'complete', quest: { id: 'test_grounds', name: 'The Testing Grounds' }, cooldown_s: 60,
        },
      },
      // no death, fixed xp/gold, no drop, no injury
      rand: makeRand([0.9, 0.5, 0.5, 0.9, 0.9]),
    });
    const sheet = await questingSheet(progression, 'dave');
    await progression.handleCommand(CH, 'dave', 'Dave', 'stats', []);

    expect(game.callsOf('chat_participant_quest_resolve')).toHaveLength(1);
    expect(sheet.quest).toBeUndefined();
    expect(sheet.xp).toBe(10);
    expect(sheet.gold).toBe(STARTING_GOLD + 5);
    expect(allMessages(twitch)).toContain('returns from The Testing Grounds: +10 XP, 5g!');
  });

  test('a still-active quest (per the SERVER clock) is not resolved early', async () => {
    const { progression, quests } = makeQuestSetup({
      gameResponses: {
        chat_participant_quest_resolve: { status: 'active', remaining_s: 120 },
      },
    });
    const sheet = await questingSheet(progression, 'dave'); // local clock says overdue
    await quests.maybeResolve(CH, 'dave');
    // Server said active → quest kept, local deadline re-synced to server time
    expect(sheet.quest).toBeDefined();
    expect(sheet.quest.local_ends_at).toBeGreaterThan(Date.now() + 100_000);
  });
});

// ---------------------------------------------------------------------------
// Quest outcomes
// ---------------------------------------------------------------------------

describe('QuestManager — outcomes', () => {
  const completeResponse = {
    chat_participant_quest_resolve: {
      status: 'complete', quest: { id: 'test_grounds', name: 'The Testing Grounds' }, cooldown_s: 60,
    },
  };

  test('drop outcome grants a BASIC-tier item from the config drop table', async () => {
    // death 0.9 (no), xp, gold, drop-check 0.1 (yes), drop-pick 0.0 (first),
    // injury-check 0.9 (no), flavor
    const { twitch, progression, quests } = makeQuestSetup({
      gameResponses: completeResponse,
      rand: makeRand([0.9, 0.5, 0.5, 0.1, 0.0, 0.9, 0.0]),
    });
    const sheet = await questingSheet(progression, 'dave');
    await quests.maybeResolve(CH, 'dave');

    expect(sheet.items).toHaveLength(1);
    const drop = sheet.items[0];
    expect(['common', 'uncommon']).toContain(drop.rarity);
    expect(DEFAULT_QUEST_CONFIG.drops.map(d => d.id)).toContain(drop.id);
    expect(sheet.equipped[drop.slot]).toBe(drop.id); // auto-equips empty slot
    expect(allMessages(twitch)).toContain(`and ${drop.name}!`);
  });

  test('injury outcome reduces arena HP until healed', async () => {
    // death 0.9 (no), xp, gold, drop 0.9 (no), injury-check 0.1 (yes), frac 0.0 (min), flavor
    const { twitch, progression, quests } = makeQuestSetup({
      gameResponses: completeResponse,
      rand: makeRand([0.9, 0.5, 0.5, 0.9, 0.1, 0.0, 0.0]),
    });
    const sheet = await questingSheet(progression, 'dave');
    await quests.maybeResolve(CH, 'dave');

    const maxHp = progression.combatStats(sheet).maxHp;
    const expected = Math.max(1, Math.round(maxHp * DEFAULT_QUEST_CONFIG.injury.min_fraction));
    expect(sheet.injuries).toBe(expected);
    expect(allMessages(twitch)).toContain('wounded');

    // Injuries carry into duels as reduced starting HP
    const stats = await progression.combatStatsFor('dave');
    expect(stats.startHp).toBe(Math.max(1, maxHp - expected));
  });

  test('death outcome archives the character and announces the tragedy', async () => {
    const { game, twitch, progression, quests } = makeQuestSetup({
      gameResponses: {
        ...completeResponse,
        chat_participant_arena_death: { success: true, graveyard_count: 1 },
      },
      rand: makeRand([0.1]), // death roll under 0.5 → DEATH
    });
    const sheet = await questingSheet(progression, 'dave');
    const cls = sheet.class;
    await quests.maybeResolve(CH, 'dave');

    const deaths = game.callsOf('chat_participant_arena_death');
    expect(deaths).toHaveLength(1);
    expect(deaths[0].payload.cause).toContain('The Testing Grounds');
    expect(deaths[0].payload.legacy_gold).toBe(0); // pure permadeath by default

    const tomb = progression.peekSheet('dave');
    expect(tomb.dead).toBe(true);
    const msg = allMessages(twitch);
    expect(msg).toContain('TRAGEDY');
    expect(msg).toContain(cls);
    expect(msg).toContain('!stats to be reborn');
  });

  test('quest-return announcements queue in quiet mode and flush after', async () => {
    const { twitch, progression, quests } = makeQuestSetup({
      gameResponses: completeResponse,
      rand: makeRand([0.9, 0.5, 0.5, 0.9, 0.9]),
    });
    await questingSheet(progression, 'dave');
    quests.setQuiet(true);
    await quests.maybeResolve(CH, 'dave');
    expect(allMessages(twitch)).not.toContain('returns from');

    quests.setQuiet(false);
    expect(allMessages(twitch)).toContain('returns from The Testing Grounds');
  });
});

// ---------------------------------------------------------------------------
// Restart-mid-quest resume
// ---------------------------------------------------------------------------

describe('QuestManager — bridge restart mid-quest', () => {
  test('resume() re-arms timers from the server list and announces the return', async () => {
    jest.useFakeTimers();
    try {
      const { game, twitch, quests } = makeQuestSetup({
        gameResponses: {
          chat_bridge_active_quests: {
            entries: [{ twitch_username: 'dave', display_name: 'Dave',
                        quest: { id: 'test_grounds', name: 'The Testing Grounds' }, remaining_s: 0 }],
          },
          chat_participant_quest_resolve: {
            status: 'complete', quest: { id: 'test_grounds', name: 'The Testing Grounds' }, cooldown_s: 60,
          },
        },
        rand: makeRand([0.9, 0.5, 0.5, 0.9, 0.9]),
      });
      await quests.resume();
      expect(game.callsOf('chat_bridge_active_quests')).toHaveLength(1);

      await jest.advanceTimersByTimeAsync(2000); // past the 1.5s safety buffer
      expect(game.callsOf('chat_participant_quest_resolve')).toHaveLength(1);
      expect(allMessages(twitch)).toContain('returns from The Testing Grounds');
    } finally {
      jest.useRealTimers();
    }
  });

  test('a sheet loaded with an in-flight quest re-arms its return timer', async () => {
    jest.useFakeTimers();
    try {
      const { game, progression } = makeQuestSetup({
        gameResponses: {
          chat_participant_arena_load: {
            found: true,
            arena_character: {
              class: 'Wizard', level: 2, xp: 0, gold: 10,
              quest: { id: 'test_grounds', name: 'The Testing Grounds', ends_at: 12345 },
            },
            quest_remaining_s: 60,
          },
          chat_participant_quest_resolve: { status: 'active', remaining_s: 60 },
        },
      });
      const { sheet } = await progression.getSheet('dave');
      expect(sheet.quest.local_ends_at).toBeGreaterThan(Date.now());

      await jest.advanceTimersByTimeAsync(62_000);
      // Timer fired and asked the server (which said: still active → re-armed)
      expect(game.callsOf('chat_participant_quest_resolve').length).toBeGreaterThanOrEqual(1);
    } finally {
      jest.useRealTimers();
    }
  });
});

// ---------------------------------------------------------------------------
// Permadeath: dead-state gating + rebirth
// ---------------------------------------------------------------------------

describe('Permadeath — dead-state gating and rebirth', () => {
  const deadLoad = {
    chat_participant_arena_load: {
      found: true,
      arena_character: { dead: true, death_cause: 'fell in the crypt', legacy_gold: 0 },
    },
  };

  test('dead chatters are pointed to !stats by every progression command', async () => {
    const { twitch, progression } = makeQuestSetup({ gameResponses: deadLoad });
    for (const action of ['bag', 'shop', 'buy', 'levelup', 'heal', 'equip']) {
      await progression.handleCommand(CH, 'ghost', 'Ghost', action, ['x']);
    }
    const deadReplies = twitch.replied.filter(r => r.msg.includes('You are dead'));
    expect(deadReplies.length).toBe(6);
    expect(deadReplies[0].msg).toContain('!stats');
  });

  test('!quest while dead is blocked too', async () => {
    const { twitch, progression, quests } = makeQuestSetup({ gameResponses: deadLoad });
    await quests.handleCommand(CH, 'ghost', 'Ghost', 'quest', ['1']);
    expect(allMessages(twitch)).toContain('You are dead');
  });

  test('!duel is blocked while dead', async () => {
    const { twitch, progression } = makeQuestSetup({ gameResponses: deadLoad });
    const arena = new Arena({
      twitchClient: twitch, progression, logger: silentLogger,
      config: { duel_cooldown_seconds: 0 },
    });
    try {
      await arena.handleCommand(CH, 'ghost', 'Ghost', 'duel', ['bob']);
      expect(arena._challenges.size).toBe(0);
      expect(allMessages(twitch)).toContain('dead');
    } finally {
      arena.destroy();
    }
  });

  test('!stats triggers rebirth: fresh 4d6 roll, level 1, announced like a first join', async () => {
    const { game, twitch, progression } = makeQuestSetup({ gameResponses: deadLoad });
    await progression.handleCommand(CH, 'ghost', 'Ghost', 'stats', []);

    const sheet = progression.peekSheet('ghost');
    expect(sheet.dead).toBeUndefined();
    expect(sheet.level).toBe(1);
    expect(sheet.gold).toBe(STARTING_GOLD);
    expect(sheet.class).toBeTruthy();
    expect(game.callsOf('chat_participant_arena_sync')).toHaveLength(1);
    expect(allMessages(twitch)).toContain('REBORN');
  });

  test('!rebirth is an explicit alias; alive chatters get a gentle no-op', async () => {
    const { twitch, progression } = makeQuestSetup({ gameResponses: deadLoad });
    await progression.handleCommand(CH, 'ghost', 'Ghost', 'rebirth', []);
    expect(progression.peekSheet('ghost').level).toBe(1);
    expect(allMessages(twitch)).toContain('REBORN');

    twitch.said.length = twitch.replied.length = 0;
    await progression.handleCommand(CH, 'ghost', 'Ghost', 'rebirth', []);
    expect(allMessages(twitch)).toContain("alive and well");
  });

  test('legacy bonus (config, default off) carries gold + heirloom into rebirth', async () => {
    const heirloom = { id: 'steel_sword', name: 'Steel Sword', slot: 'weapon', rarity: 'uncommon', bonuses: { atk: 2 } };
    const game = makeGameClient({
      chat_participant_arena_load: {
        found: true,
        arena_character: { dead: true, death_cause: 'x', legacy_gold: 33, heirloom },
      },
      chat_participant_arena_sync: { success: true },
    });
    const twitch = makeTwitch();
    const progression = new ArenaProgression({ gameClient: game, twitchClient: twitch, logger: silentLogger });
    await progression.handleCommand(CH, 'heir', 'Heir', 'stats', []);

    const sheet = progression.peekSheet('heir');
    expect(sheet.gold).toBe(STARTING_GOLD + 33);
    expect(sheet.items.map(i => i.id)).toContain('steel_sword');
    expect(sheet.equipped.weapon).toBe('steel_sword');
    expect(allMessages(twitch)).toContain('heirloom: Steel Sword');
  });

  test('quest death honours the configured legacy bonus', async () => {
    const { game, progression, quests } = makeQuestSetup({
      gameResponses: {
        chat_participant_quest_resolve: {
          status: 'complete', quest: { id: 'test_grounds', name: 'The Testing Grounds' }, cooldown_s: 60,
        },
        chat_participant_arena_death: { success: true, graveyard_count: 1 },
      },
      config: { legacy: { gold_percent: 10, heirloom: true } },
      rand: makeRand([0.1]), // death
    });
    const sheet = await questingSheet(progression, 'dave');
    sheet.gold = 250;
    sheet.items.push({ id: 'cat_charm', name: 'Cat Charm', slot: 'trinket', rarity: 'uncommon', bonuses: {} });
    sheet.equipped.trinket = 'cat_charm';
    await quests.maybeResolve(CH, 'dave');

    const death = game.callsOf('chat_participant_arena_death')[0];
    expect(death.payload.legacy_gold).toBe(25);
    expect(death.payload.heirloom.id).toBe('cat_charm');
  });
});

// ---------------------------------------------------------------------------
// Economy: shop/drop tier separation + potions
// ---------------------------------------------------------------------------

describe('Economy rebalance — tier separation and potions', () => {
  test('shop sells only uncommon+ gear (plus the potion); quests drop only basic tier', () => {
    const gear = SHOP_CATALOG.filter(i => !i.consumable);
    expect(gear.every(i => ['uncommon', 'rare', 'epic'].includes(i.rarity))).toBe(true);
    expect(gear.some(i => i.rarity === 'epic')).toBe(true);
    expect(SHOP_CATALOG.some(i => i.consumable && /potion/i.test(i.name))).toBe(true);

    expect(DEFAULT_QUEST_CONFIG.drops.every(d => ['common', 'uncommon'].includes(d.rarity))).toBe(true);
    // Commons dominate the drop table (uncommons are the rare treat)
    const weight = r => DEFAULT_QUEST_CONFIG.drops
      .filter(d => d.rarity === r).reduce((s, d) => s + d.weight, 0);
    expect(weight('common')).toBeGreaterThan(weight('uncommon') * 3);
  });

  test('!buy healing potion stacks potions instead of adding bag items', async () => {
    const { twitch, progression } = makeQuestSetup();
    const { sheet } = await progression.getSheet('drinker');
    sheet.gold = 100;
    await progression.handleCommand(CH, 'drinker', 'D', 'buy', ['healing', 'potion']);
    expect(sheet.potions).toBe(1);
    expect(sheet.items).toHaveLength(0);
    expect(sheet.gold).toBe(60);
    await progression.handleCommand(CH, 'drinker', 'D', 'buy', ['healing', 'potion']);
    expect(sheet.potions).toBe(2); // stacks — no "already own" refusal
  });

  test('!heal consumes a potion and cures injuries', async () => {
    const { twitch, progression } = makeQuestSetup();
    const { sheet } = await progression.getSheet('hurt');
    sheet.potions = 1;
    sheet.injuries = 7;
    await progression.handleCommand(CH, 'hurt', 'H', 'heal', []);
    expect(sheet.injuries).toBe(0);
    expect(sheet.potions).toBe(0);
    expect(allMessages(twitch)).toContain('wounds cured');

    await progression.handleCommand(CH, 'hurt', 'H', 'heal', []);
    expect(allMessages(twitch)).toContain('No Healing Potions');
  });

  test('!heal with no injuries refuses to waste the potion', async () => {
    const { twitch, progression } = makeQuestSetup();
    const { sheet } = await progression.getSheet('fine');
    sheet.potions = 2;
    await progression.handleCommand(CH, 'fine', 'F', 'heal', []);
    expect(sheet.potions).toBe(2);
    expect(allMessages(twitch)).toContain("unhurt");
  });
});

// ---------------------------------------------------------------------------
// Arena: injuries + auto-potion death-blow save
// ---------------------------------------------------------------------------

describe('Arena — injuries and auto-potion', () => {
  function statsProgression(byUser) {
    return {
      combatStatsFor: async (u) => byUser[u],
      duelEligibility: async () => ({ ok: true }),
      consumePotions: jest.fn(async () => {}),
      recordDuelResult: jest.fn(async () => {}),
    };
  }

  test('unhealed injuries reduce a fighter’s duel starting HP', async () => {
    const progression = statsProgression({
      alice: { maxHp: 30, ac: 12, atkBonus: 2, startHp: 18, potions: 0 },
      bob:   { maxHp: 25, ac: 12, atkBonus: 1, startHp: 25, potions: 0 },
    });
    const twitch = makeTwitch();
    // Slim narration (default): the duel-start line goes to the ticker, not chat.
    const ticker = [];
    const arena = new Arena({
      twitchClient: twitch, progression, logger: silentLogger, config: {},
      onTickerEvent: (category, text) => ticker.push({ category, text }),
    });
    try {
      await arena._startDuelAsync(CH, 'alice', 'bob');
      const state = arena._activeDuels.get('alice');
      expect(state.participants.alice.hp).toBeLessThanOrEqual(18);
      expect(state.participants.alice.maxHp).toBe(30);
      expect(ticker.some(t => t.text.includes('alice enters wounded (18/30 HP)'))).toBe(true);
      expect(twitch.said.some(s => s.msg.includes('ARENA DUEL'))).toBe(false);
    } finally {
      arena.destroy();
    }
  });

  test('auto_potion=true saves a fighter from the death-blow exactly once', async () => {
    const progression = statsProgression({
      alice: { maxHp: 50, ac: 30, atkBonus: 100, startHp: 50, potions: 0 },
      bob:   { maxHp: 5,  ac: 1,  atkBonus: -100, startHp: 5, potions: 1 },
    });
    const twitch = makeTwitch();
    const arena = new Arena({
      twitchClient: twitch, progression, logger: silentLogger,
      config: { autoPotion: true },
    });
    // Deterministic fight: 5 rounds, alice always crits for 6, bob always misses
    const rolls = [0.99, 0.99, 0.99, 0.0, 0.99, 0.99, 0.99, 0.99];
    const spy = jest.spyOn(Math, 'random').mockImplementation(() => rolls.length ? rolls.shift() : 0.0);
    try {
      const state = {
        channel: CH,
        participants: {
          alice: { username: 'alice', hp: 50, maxHp: 50, ac: 30, atkBonus: 100, potions: 0, potionUsed: false },
          bob:   { username: 'bob',   hp: 5,  maxHp: 5,  ac: 1,  atkBonus: -100, potions: 1, potionUsed: false },
        },
        round: 0, finished: false,
      };
      arena._activeDuels.set('alice', state);
      arena._activeDuels.set('bob', state);
      arena._runAutoResolve(state, 'alice', 'bob');

      // Potion fired once (revive to ceil(5/2)=3), then the next blow ended it
      expect(state.participants.bob.potionUsed).toBe(true);
      expect(state.winner).toBe('alice');
      expect(progression.consumePotions).toHaveBeenCalledWith('bob', 1);
    } finally {
      spy.mockRestore();
      arena.destroy();
    }
  });

  test('auto potion stays OFF by default', async () => {
    const progression = statsProgression({});
    const twitch = makeTwitch();
    const arena = new Arena({ twitchClient: twitch, progression, logger: silentLogger, config: {} });
    try {
      const state = {
        channel: CH,
        participants: {
          bob: { username: 'bob', hp: 0, maxHp: 10, potions: 5, potionUsed: false },
        },
      };
      expect(arena._tryAutoPotion(state, 'bob')).toBeNull();
      expect(state.participants.bob.potions).toBe(5);
    } finally {
      arena.destroy();
    }
  });
});
