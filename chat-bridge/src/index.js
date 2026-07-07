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
const { LootRoller }    = require('./lootRoller');
const { EventSubClient } = require('./eventSubClient');
const { sanitize, validateUsername } = require('./sanitizer');
const { Arena }         = require('./arena');

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

const commandMap = loadJson('config/commands.json')  ?? { '!join': 'join', '!leave': 'leave', '!inventory': 'inventory', '!target': 'target', '!help': 'help' };
const lootTables = loadJson('config/loot-tables.json') ?? {};
const cooldowns  = loadJson('config/cooldowns.json')   ?? {};
const responses  = loadJson('config/responses.json')   ?? {};

// ---------------------------------------------------------------------------
// Env validation
// ---------------------------------------------------------------------------
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
    join:      cooldowns.join_seconds      ?? 10,
    leave:     cooldowns.leave_seconds     ?? 5,
    inventory: cooldowns.inventory_seconds ?? 10,
    target:    cooldowns.target_seconds    ?? 30,
    help:      cooldowns.help_seconds      ?? 5,
    vote:      cooldowns.vote_seconds      ?? 2,
    name:      cooldowns.name_seconds      ?? 10,
    me:        cooldowns.me_seconds        ?? 10,
    ...(cooldowns.commands ?? {}),
  },
  default_seconds:       cooldowns.default_seconds       ?? 5,
  global_max:            cooldowns.global_max            ?? 30,
  global_window_seconds: cooldowns.global_window_seconds ?? 10,
});

const lootRoller = new LootRoller(lootTables);

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
    twitchClient.say(channel, announcement(displayName, safe));
  }
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
let twitchClient = null;
let commandParser = null;
let eventSubClient = null;
let arena = null;

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

  const channel = `#${twitchChannel.replace(/^#/, '')}`;

  arena = new Arena({
    twitchClient,
    config: {
      duel_cooldown_seconds: cooldowns.duel_cooldown_seconds ?? 120,
      interactiveMode: false,
    },
    logger,
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
    arenaHandler: (ch, username, displayName, action, args) => {
      arena.handleCommand(ch, username, displayName, action, args);
    },
  });

  twitchClient.onMessage((ch, tags, message) => commandParser.handle(ch, tags, message));

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
  // EventSub (optional — requires clientId and broadcasterId)
  // ---------------------------------------------------------------------------
  if (clientId && broadcasterId) {
    eventSubClient = new EventSubClient({
      clientId,
      oauthToken: twitchOAuthToken.replace(/^oauth:/, ''),
      broadcasterId,
      logger,
    });

    eventSubClient.on('sub', async ({ username, displayName, months, gifted, gifterDisplay }) => {
      const valid = validateUsername(username);
      if (!valid) return;
      const trigger   = gifted ? 'gifted_sub' : months > 1 ? 'resub' : 'sub';
      const tableName = lootTables.sub ? 'sub' : 'default';
      const announce  = gifted
        ? (dn, item) => `${gifterDisplay ?? '?'} gifted a sub to ${dn}! They receive ${item}!`
        : (dn, item) => `${dn} subscribed${months > 1 ? ` (${months} months!)` : ''}! They receive ${item}!`;
      await grantLoot(valid, displayName, tableName, trigger, announce, channel);
    });

    eventSubClient.on('bits', async ({ username, displayName, amount }) => {
      const valid = validateUsername(username);
      if (!valid) return;
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
      twitchClient.say(channel, `${displayName} cheered ${amount} bits and received ${result.item}!`);
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

  // Handle server-side kill-switch events
  gameClient.on('chat_bridge_status', ({ paused, arena_enabled }) => {
    logger.info(`[ChatBridge] Kill switch: bridge is now ${paused ? 'PAUSED' : 'ACTIVE'}`);
    if (paused && twitchClient) {
      twitchClient.say(channel, 'Chat bridge is temporarily paused by the DM. Hang tight!');
    }
    if (arena_enabled !== undefined) arena.setEnabled(arena_enabled);
  });

  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
}

function shutdown() {
  logger.info('[ChatBridge] Shutting down…');
  if (twitchClient) twitchClient.disconnect();
  if (arena) arena.destroy();
  gameClient.stop();
  if (eventSubClient) eventSubClient.stop();
  process.exit(0);
}

main().catch(err => {
  logger.error('[ChatBridge] Fatal startup error:', err);
  process.exit(1);
});
