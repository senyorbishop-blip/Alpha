'use strict';

require('dotenv').config();
const path = require('path');
const fs   = require('fs');
const http  = require('http');
const https = require('https');

const { TwitchClient }  = require('./twitchClient');
const { GameClient }    = require('./gameClient');
const { CommandParser } = require('./commandParser');
const { RateLimiter }   = require('./rateLimiter');
const { LootRoller, rollRewardEntry, pickRewardTier } = require('./lootRoller');
const { EventSubClient } = require('./eventSubClient');
const { sanitize, validateUsername } = require('./sanitizer');
const { Arena }         = require('./arena');
const { ArenaProgression } = require('./arenaProgression');
const { QuestManager }  = require('./quests');
const { MockTwitchClient, MockEventSubClient, startMockRepl } = require('./mockChat');

// ---------------------------------------------------------------------------
// Logger
// ---------------------------------------------------------------------------
const logger = {
  info:  (...args) => console.log('[INFO] ', ...args),
  warn:  (...args) => console.warn('[WARN] ', ...args),
  error: (...args) => console.error('[ERROR]', ...args),
};

// ---------------------------------------------------------------------------
// Config loading
// ---------------------------------------------------------------------------
function loadJson(relPath) {
  const full = path.resolve(__dirname, '..', relPath);
  try {
    return JSON.parse(fs.readFileSync(full, 'utf8'));
  } catch (err) {
    logger.warn(`Could not load config ${relPath}: ${err.message}`);
    return null;
  }
}

const commandMap = loadJson('config/commands.json')  ?? {
  '!join': 'join', '!leave': 'leave', '!inventory': 'inventory', '!inv': 'inventory',
  '!bag': 'bag', '!target': 'target', '!use': 'use',
  '!powers': 'powers', '!power': 'powers',
  '!help': 'help', '!commands': 'help', '!command': 'help',
  '!vote': 'vote', '!name': 'name', '!me': 'me', '!character': 'me',
  '!stats': 'stats', '!sheet': 'stats', '!equip': 'equip', '!shop': 'shop',
  '!buy': 'buy', '!levelup': 'levelup', '!leaderboard': 'leaderboard',
  '!quest': 'quest', '!heal': 'heal', '!rebirth': 'rebirth', '!graveyard': 'graveyard',
  '!duel': 'arena', '!accept': 'arena', '!decline': 'arena',
};
const lootTables = loadJson('config/loot-tables.json') ?? {};
const cooldowns  = loadJson('config/cooldowns.json')   ?? {};
const responses  = loadJson('config/responses.json')   ?? {};
// Quest definitions, drop tables, flavor pools, permadeath/legacy + potion
// toggles — themeable per campaign. Merged over DEFAULT_QUEST_CONFIG.
const questConfig = loadJson('config/quests.json')     ?? {};
// Optional arena shop override (array of catalog entries).
const shopConfig  = loadJson('config/shop.json');
// Stream/overlay narration config. Default is SLIM chat: play-by-play (duel
// rounds, etc.) goes to the overlay event ticker and chat gets one line per
// completed event, rewards included. Streamers who want the classic
// round-by-round chat narration back set full_chat_narration=true here (or
// CHAT_FULL_NARRATION=true in the environment).
const streamConfig = loadJson('config/stream.json') ?? {};
const FULL_CHAT_NARRATION =
  /^(1|true|yes)$/i.test(process.env.CHAT_FULL_NARRATION ?? '') ||
  !!streamConfig.full_chat_narration;

// ---------------------------------------------------------------------------
// Env validation
// ---------------------------------------------------------------------------
// Mock chat mode: no Twitch account/tokens/internet needed — chat is simulated
// on stdin and bot replies print to the console. Only the game-server vars are
// required. Activate with MOCK_CHAT=true or `npm run mock`.
const MOCK_CHAT = /^(1|true|yes)$/i.test(process.env.MOCK_CHAT ?? '') || process.argv.includes('--mock');

const REQUIRED_ENV = ['GAME_SERVER_WS_URL', 'GAME_SESSION_ID', 'CHAT_BRIDGE_TOKEN'];
for (const key of REQUIRED_ENV) {
  if (!process.env[key]) {
    logger.error(`Missing required environment variable: ${key}`);
    process.exit(1);
  }
}

const {
  TWITCH_BOT_USERNAME,
  TWITCH_OAUTH_TOKEN,
  TWITCH_CHANNEL,
  TWITCH_CLIENT_ID,
  GAME_SERVER_WS_URL,
  GAME_SESSION_ID,
  CHAT_BRIDGE_TOKEN,
} = process.env;

// ---------------------------------------------------------------------------
// Per-campaign credentials fetch
// Fetches channel / OAuth token from the game server if the DM has connected
// their Twitch account from within the app.  Falls back to env vars.
// ---------------------------------------------------------------------------
async function fetchCampaignCredentials() {
  const baseUrl = GAME_SERVER_WS_URL.replace(/^ws/, 'http').replace(/\/$/, '');
  const url = `${baseUrl}/api/chat-bridge/credentials/${GAME_SESSION_ID}`;

  return new Promise((resolve) => {
    const mod = url.startsWith('https') ? https : http;
    const req = mod.get(url, {
      headers: {
        'X-Bridge-Token': CHAT_BRIDGE_TOKEN,
        'Accept': 'application/json',
      },
    }, res => {
      let data = '';
      res.on('data', c => { data += c; });
      res.on('end', () => {
        if (res.statusCode === 200) {
          try {
            resolve(JSON.parse(data));
          } catch {
            resolve(null);
          }
        } else {
          resolve(null);
        }
      });
    });
    req.on('error', () => resolve(null));
    req.setTimeout(5000, () => { req.destroy(); resolve(null); });
  });
}

// ---------------------------------------------------------------------------
// Component construction
// ---------------------------------------------------------------------------
const gameClient = new GameClient({
  wsUrl: GAME_SERVER_WS_URL,
  sessionId: GAME_SESSION_ID,
  bridgeToken: CHAT_BRIDGE_TOKEN,
  logger,
});

const rateLimiter = new RateLimiter({
  ...cooldowns,
  commands: {
    join:        cooldowns.join_seconds        ?? 10,
    leave:       cooldowns.leave_seconds       ?? 5,
    inventory:   cooldowns.inventory_seconds   ?? 10,
    target:      cooldowns.target_seconds      ?? 30,
    powers:      cooldowns.powers_seconds      ?? 10,
    help:        cooldowns.help_seconds        ?? 5,
    vote:        cooldowns.vote_seconds        ?? 2,
    name:        cooldowns.name_seconds        ?? 10,
    me:          cooldowns.me_seconds          ?? 10,
    stats:       cooldowns.stats_seconds       ?? 10,
    bag:         cooldowns.bag_seconds         ?? 10,
    equip:       cooldowns.equip_seconds       ?? 5,
    shop:        cooldowns.shop_seconds        ?? 15,
    buy:         cooldowns.buy_seconds         ?? 5,
    levelup:     cooldowns.levelup_seconds     ?? 10,
    leaderboard: cooldowns.leaderboard_seconds ?? 30,
    // Low: "!quest" (list) then "!quest <n>" (start) is the normal flow.
    // Real pacing is enforced server-side (one active quest + return cooldown).
    quest:       cooldowns.quest_seconds       ?? 3,
    heal:        cooldowns.heal_seconds        ?? 5,
    rebirth:     cooldowns.rebirth_seconds     ?? 10,
    graveyard:   cooldowns.graveyard_seconds   ?? 30,
    unknown_cmd: cooldowns.unknown_cmd_seconds ?? 60,
    ...(cooldowns.commands ?? {}),
  },
  default_seconds:       cooldowns.default_seconds       ?? 5,
  global_max:            cooldowns.global_max            ?? 30,
  global_window_seconds: cooldowns.global_window_seconds ?? 10,
});

const lootRoller = new LootRoller(lootTables);

// ---------------------------------------------------------------------------
// Overlay event ticker
//
// Rule of thumb (also encoded in arena.js / quests.js / commandParser.js):
//   Continuous or multi-step events → ticker. Results and personal replies →
//   chat. One chat line per completed event, rewards included.
//
// The server stamps every relayed event with a seq number so overlays can
// detect gaps instead of silently missing events.
// ---------------------------------------------------------------------------
function emitTicker(category, text, icon = '') {
  gameClient.send('ticker_event', {
    category: String(category || 'event'),
    text: String(text || ''),
    icon: String(icon || ''),
  }).catch(err => logger.error('[ticker] emit error:', err));
}

// ---------------------------------------------------------------------------
// Server-managed reward tables (DM-configurable in the Stream panel)
//
// The server persists a per-campaign rewards config mapping Twitch events to
// weighted tables of viewer powers:
//   { sub: [{power_id, name, weight}, …],
//     gift_tiers: [{min_count, table: […]}, …],
//     bits_tiers: [{threshold, table: […]}, …] }
// Fetched at startup and refreshed whenever the DM saves changes
// (chat_rewards_updated broadcast). Falls back to the local
// config/loot-tables.json item tables when the server has no config.
// ---------------------------------------------------------------------------
let rewardConfig = null;

function applyRewardConfig(config) {
  if (!config || typeof config !== 'object') return;
  rewardConfig = {
    sub: Array.isArray(config.sub) ? config.sub : [],
    gift_tiers: Array.isArray(config.gift_tiers) ? config.gift_tiers : [],
    bits_tiers: Array.isArray(config.bits_tiers) ? config.bits_tiers : [],
  };
  logger.info('[rewards] Applied server reward config '
    + `(sub: ${rewardConfig.sub.length} powers, gift tiers: ${rewardConfig.gift_tiers.length}, bits tiers: ${rewardConfig.bits_tiers.length})`);
}

async function refreshRewardConfig() {
  try {
    const result = await gameClient.send('chat_bridge_rewards_get', {});
    if (result?.config) applyRewardConfig(result.config);
    else logger.info('[rewards] No server reward config — using local loot-tables.json');
  } catch (err) {
    logger.warn('[rewards] Could not fetch reward config:', err.message);
  }
}

/** Grant a rolled viewer-power reward as a chat-participant item. */
async function grantPowerReward(username, displayName, entry, trigger, announcement, channel) {
  const itemName = sanitize(String(entry.name || entry.power_id), 80);
  logger.info(`[rewards] ${trigger} → ${username} receives ${itemName} (${entry.power_id})`);
  await gameClient.send('chat_bridge_loot_grant', {
    twitch_username: username,
    display_name: displayName,
    item_entry: { ...buildItemEntry(itemName), power_id: String(entry.power_id) },
    trigger,
  });
  if (announcement && channel && twitchClient) {
    // One chat line per reward roll; the ticker mirrors it for the overlay.
    const line = announcement(displayName, itemName);
    twitchClient.say(channel, line);
    emitTicker('reward', line.replace(/@/g, ''));
  }
}

// ---------------------------------------------------------------------------
// EventSub loot event handlers
// ---------------------------------------------------------------------------
function buildItemEntry(itemName, opts = {}) {
  return {
    name:             String(itemName),
    qty:              1,
    notes:            opts.notes          ?? '',
    effect:           opts.effect         ?? '',
    is_magic:         opts.is_magic       ?? false,
    charges_current:  opts.charges_current ?? 0,
    charges_max:      opts.charges_max    ?? 0,
  };
}

async function grantLoot(username, displayName, tableName, trigger, announcement, channel) {
  const itemName = lootRoller.hasTable(tableName) ? lootRoller.roll(tableName) : null;
  if (!itemName) {
    logger.warn(`[loot] No item rolled from table "${tableName}" for ${username}`);
    return;
  }

  const safe = sanitize(itemName, 80);
  logger.info(`[loot] ${trigger} → ${username} receives ${safe}`);

  await gameClient.send('chat_bridge_loot_grant', {
    twitch_username: username,
    display_name: displayName,
    item_entry: buildItemEntry(safe),
    trigger,
  });

  if (announcement && channel && twitchClient) {
    // One chat line per reward roll; the ticker mirrors it for the overlay.
    const line = announcement(displayName, safe);
    twitchClient.say(channel, line);
    emitTicker('reward', line.replace(/@/g, ''));
  }
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
let twitchClient = null;
let commandParser = null;
let eventSubClient = null;
let arena = null;
let progression = null;
let questManager = null;

async function main() {
  logger.info('[ChatBridge] Starting Twitch Chat Bridge…');

  await gameClient.start();
  logger.info('[ChatBridge] Game server client connected');

  // Attempt to fetch per-campaign Twitch credentials from the game server.
  // If the DM has connected their channel via the in-app OAuth flow, those
  // credentials take precedence over env vars.
  let twitchChannel = TWITCH_CHANNEL ?? '';
  let twitchOAuthToken = TWITCH_OAUTH_TOKEN ?? '';
  let twitchBotUsername = TWITCH_BOT_USERNAME ?? '';
  let broadcasterId = process.env.TWITCH_BROADCASTER_ID ?? '';
  let clientId = TWITCH_CLIENT_ID ?? '';

  if (MOCK_CHAT) {
    // Mock mode: no Twitch connection at all — chat is simulated on stdin.
    twitchChannel = twitchChannel || 'mock';
    twitchClient = new MockTwitchClient({ channel: `#${twitchChannel.replace(/^#/, '')}`, logger });
    await twitchClient.connect();
  } else {
    try {
      const creds = await fetchCampaignCredentials();
      if (creds?.channel && creds?.access_token) {
        twitchChannel    = creds.channel;
        twitchOAuthToken = `oauth:${creds.access_token}`;
        broadcasterId    = creds.channel_id || broadcasterId;
        logger.info(`[ChatBridge] Using per-campaign Twitch credentials for channel: ${twitchChannel}`);
      } else {
        logger.info('[ChatBridge] No per-campaign credentials found; using env-var credentials.');
      }
    } catch (err) {
      logger.warn('[ChatBridge] Could not fetch campaign credentials:', err.message);
    }

    if (!twitchChannel || !twitchOAuthToken) {
      logger.error('[ChatBridge] No Twitch credentials available (set TWITCH_CHANNEL + TWITCH_OAUTH_TOKEN or connect via the DM UI).');
      process.exit(1);
    }

    if (!twitchBotUsername) {
      // Default bot username to channel name when connecting with per-campaign tokens
      twitchBotUsername = twitchChannel.replace(/^#/, '');
      logger.warn(`[ChatBridge] TWITCH_BOT_USERNAME not set; defaulting to channel name: ${twitchBotUsername}`);
    }

    twitchClient = new TwitchClient({
      username: twitchBotUsername,
      oauthToken: twitchOAuthToken,
      channel: twitchChannel,
      logger,
    });

    await twitchClient.connect();
    logger.info('[ChatBridge] Twitch IRC client connected');
  }

  const channel = `#${twitchChannel.replace(/^#/, '')}`;

  progression = new ArenaProgression({
    gameClient,
    twitchClient,
    logger,
    shopCatalog: Array.isArray(shopConfig) ? shopConfig : shopConfig?.catalog,
    legacyConfig: questConfig?.legacy,
    onTickerEvent: emitTicker,
  });

  questManager = new QuestManager({
    gameClient,
    twitchClient,
    progression,
    config: questConfig,
    channel,
    logger,
    onTickerEvent: emitTicker,
  });
  progression.attachQuests(questManager);

  arena = new Arena({
    twitchClient,
    progression,
    config: {
      duel_cooldown_seconds: cooldowns.duel_cooldown_seconds ?? 120,
      interactiveMode: false,
      // Auto-drink a held Healing Potion on a death-blow (default off).
      autoPotion: !!questConfig?.auto_potion,
      // Classic round-by-round chat narration (default off = ticker + one
      // result line in chat). config/stream.json or CHAT_FULL_NARRATION.
      fullChatNarration: FULL_CHAT_NARRATION,
    },
    logger,
    onTickerEvent: emitTicker,
    // Map a chatter-typed opponent reference (login, display name, or !name
    // character name — with or without a leading @) to a login username.
    resolveOpponent: (name) => gameClient.send('chat_participant_resolve', { name }),
    onStatsUpdate: async (username, stats) => {
      await gameClient.send('chat_participant_arena_stats_update', {
        twitch_username: username,
        arena_stats: stats,
      });
    },
    onDisplayEvent: (payload) => {
      gameClient.send('arena_display_event', payload).catch(err =>
        logger.error('[Arena] display event relay error:', err)
      );
    },
  });

  commandParser = new CommandParser({
    commandMap,
    responses,
    rateLimiter,
    gameClient,
    twitchClient,
    onTickerEvent: emitTicker,
    arenaHandler: (ch, username, displayName, action, args) => {
      arena.handleCommand(ch, username, displayName, action, args);
    },
    progressionHandler: (ch, username, displayName, action, args) =>
      progression.handleCommand(ch, username, displayName, action, args),
  });

  twitchClient.onMessage((ch, tags, message) => commandParser.handle(ch, tags, message));

  // Quests survive bridge restarts: the server holds every in-flight quest's
  // (server-clock) deadline — re-arm the return-announcement timers from it.
  await questManager.resume();

  // DM-configured reward tables: fetch now, refresh whenever the DM saves
  // changes in the Stream panel.
  await refreshRewardConfig();
  gameClient.on('chat_rewards_updated', (payload) => {
    if (payload?.config) applyRewardConfig(payload.config);
    else refreshRewardConfig();
  });

  // Listen for poll events and announce them in chat
  gameClient.on('poll_created', (payload) => {
    if (!twitchClient || !twitchChannel) return;
    const ch = `#${twitchChannel.replace(/^#/, '')}`;
    const question = payload.question || 'Vote!';
    const options = (payload.options || []).map((opt, i) => `!vote ${i + 1} (${opt})`).join(' or ');
    const closesAt = payload.closes_at;
    const duration = closesAt ? `${Math.round(closesAt - Date.now() / 1000)}s remaining` : '';
    const suffix = duration ? ` — ${duration}` : '';
    twitchClient.say(ch, `VOTE: ${question} — ${options}${suffix}`);
  });

  gameClient.on('poll_closed', (payload) => {
    if (!twitchClient || !twitchChannel) return;
    const ch = `#${twitchChannel.replace(/^#/, '')}`;
    // Find winner from vote_counts
    const counts = payload.vote_counts || {};
    const options = payload.options || [];
    let winnerIdx = -1;
    let winnerCount = 0;
    let total = 0;
    for (const [idx, count] of Object.entries(counts)) {
      total += count;
      if (count > winnerCount) { winnerCount = count; winnerIdx = parseInt(idx, 10); }
    }
    if (winnerIdx >= 0 && options[winnerIdx]) {
      twitchClient.say(ch, `Vote closed! Result: "${options[winnerIdx]}" won with ${winnerCount} vote(s) out of ${total} total.`);
    } else {
      twitchClient.say(ch, `Vote closed! No votes were cast.`);
    }
  });

  // ---------------------------------------------------------------------------
  // EventSub (optional — requires clientId and broadcasterId).
  // In mock mode a MockEventSubClient is used so /sub /giftsub /bits /raid in
  // the REPL fire the exact same handlers as real EventSub events.
  // ---------------------------------------------------------------------------
  if (MOCK_CHAT) {
    eventSubClient = new MockEventSubClient({ logger });
  } else if (clientId && broadcasterId) {
    eventSubClient = new EventSubClient({
      clientId,
      oauthToken: twitchOAuthToken.replace(/^oauth:/, ''),
      broadcasterId,
      logger,
    });
  }

  if (eventSubClient) {
    eventSubClient.on('sub', async ({ username, displayName, months, gifted, gifterDisplay }) => {
      const valid = validateUsername(username);
      if (!valid) return;
      const trigger   = gifted ? 'gifted_sub' : months > 1 ? 'resub' : 'sub';
      const announce  = gifted
        ? (dn, item) => `${gifterDisplay ?? '?'} gifted a sub to ${dn}! They receive ${item}!`
        : (dn, item) => `${dn} subscribed${months > 1 ? ` (${months} months!)` : ''}! They receive ${item}!`;

      // Server-managed reward table first; local loot-tables.json fallback.
      const entry = rewardConfig ? rollRewardEntry(rewardConfig.sub) : null;
      if (entry) {
        await grantPowerReward(valid, displayName, entry, trigger, announce, channel);
        return;
      }
      const tableName = lootTables.sub ? 'sub' : 'default';
      await grantLoot(valid, displayName, tableName, trigger, announce, channel);
    });

    // Gift-sub batches: the GIFTER earns a reward from the tier matching how
    // many subs they gave (1 / 5 / 10+ by default). Each recipient still gets
    // a normal sub reward via the 'sub' event above.
    eventSubClient.on('giftsub', async ({ username, displayName, total }) => {
      const valid = validateUsername(username);
      if (!valid || !rewardConfig) return;
      const count = Math.max(1, Number(total) || 1);
      const tier = pickRewardTier(rewardConfig.gift_tiers, 'min_count', count);
      const entry = tier ? rollRewardEntry(tier.table) : null;
      if (!entry) return;
      const announce = (dn, item) => `${dn} gifted ${count} sub${count > 1 ? 's' : ''} and receives ${item}!`;
      await grantPowerReward(valid, displayName, entry, `gift_x${count}`, announce, channel);
    });

    eventSubClient.on('bits', async ({ username, displayName, amount }) => {
      const valid = validateUsername(username);
      if (!valid) return;

      // Server-managed bits tiers first; local loot-tables.json fallback.
      if (rewardConfig) {
        const tier = pickRewardTier(rewardConfig.bits_tiers, 'threshold', amount);
        const entry = tier ? rollRewardEntry(tier.table) : null;
        if (entry) {
          const announce = (dn, item) => `${dn} cheered ${amount} bits and received ${item}!`;
          await grantPowerReward(valid, displayName, entry, `bits_${amount}`, announce, channel);
          return;
        }
      }

      const bitsConfig = lootTables.bits;
      if (!Array.isArray(bitsConfig)) return;
      const result = lootRoller.rollBits(amount, bitsConfig);
      if (!result) return;
      logger.info(`[loot] bits ${amount} → ${username} receives ${result.item}`);
      await gameClient.send('chat_bridge_loot_grant', {
        twitch_username: valid,
        display_name: displayName,
        item_entry: buildItemEntry(result.item),
        trigger: `bits_${amount}`,
      });
      const bitsLine = `${displayName} cheered ${amount} bits and received ${result.item}!`;
      twitchClient.say(channel, bitsLine);
      emitTicker('reward', bitsLine.replace(/@/g, ''));
    });

    eventSubClient.on('raid', async ({ fromUsername, fromDisplayName, viewers }) => {
      const valid = validateUsername(fromUsername);
      if (!valid) return;
      if (!lootTables.raid) return;
      const announce = (dn, item) => `${dn} raided with ${viewers} viewers! They receive ${item}!`;
      await grantLoot(valid, fromDisplayName, 'raid', 'raid', announce, channel);
    });

    eventSubClient.start().catch(err => logger.error('[EventSub] fatal error:', err));
    logger.info('[ChatBridge] EventSub client started');
  } else {
    logger.warn('[ChatBridge] EventSub disabled — set TWITCH_CLIENT_ID and TWITCH_BROADCASTER_ID (or connect via DM UI) to enable sub/bits/raid loot');
  }

  if (MOCK_CHAT) {
    startMockRepl({ twitchClient, eventSubClient, logger, onQuit: shutdown });
  }

  // Handle server-side kill-switch events
  gameClient.on('chat_bridge_status', ({ paused, arena_enabled, arena_quiet }) => {
    logger.info(`[ChatBridge] Kill switch: bridge is now ${paused ? 'PAUSED' : 'ACTIVE'}`);
    if (paused && twitchClient) {
      twitchClient.say(channel, 'Chat bridge is temporarily paused by the DM. Hang tight!');
    }
    if (arena_enabled !== undefined) arena.setEnabled(arena_enabled);
    // Quiet mode queues duels AND quest-return announcements until it lifts.
    if (arena_quiet !== undefined) {
      arena.setQuietMode(arena_quiet);
      questManager.setQuiet(arena_quiet);
    }
  });

  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
}

function shutdown() {
  logger.info('[ChatBridge] Shutting down…');
  if (twitchClient) twitchClient.disconnect();
  if (arena) arena.destroy();
  if (questManager) questManager.destroy();
  gameClient.stop();
  if (eventSubClient) eventSubClient.stop();
  process.exit(0);
}

main().catch(err => {
  logger.error('[ChatBridge] Fatal startup error:', err);
  process.exit(1);
});
