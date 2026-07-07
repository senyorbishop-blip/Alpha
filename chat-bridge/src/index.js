'use strict';

require('dotenv').config();
const path = require('path');
const fs = require('fs');

const { TwitchClient } = require('./twitchClient');
const { GameClient } = require('./gameClient');
const { CommandParser } = require('./commandParser');
const { RateLimiter } = require('./rateLimiter');
const { LootRoller } = require('./lootRoller');
const { EventSubClient } = require('./eventSubClient');
const { sanitize, validateUsername } = require('./sanitizer');

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

const commandMap   = loadJson('config/commands.json') ?? { '!join': 'join', '!leave': 'leave', '!inventory': 'inventory', '!target': 'target' };
const lootTables   = loadJson('config/loot-tables.json') ?? {};
const cooldowns    = loadJson('config/cooldowns.json') ?? {};

// ---------------------------------------------------------------------------
// Env validation
// ---------------------------------------------------------------------------
const REQUIRED_ENV = ['TWITCH_BOT_USERNAME', 'TWITCH_OAUTH_TOKEN', 'TWITCH_CHANNEL', 'GAME_SERVER_WS_URL', 'GAME_SESSION_ID', 'CHAT_BRIDGE_TOKEN'];
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
// Component construction
// ---------------------------------------------------------------------------
const gameClient = new GameClient({
  wsUrl: GAME_SERVER_WS_URL,
  sessionId: GAME_SESSION_ID,
  bridgeToken: CHAT_BRIDGE_TOKEN,
  logger,
});

const twitchClient = new TwitchClient({
  username: TWITCH_BOT_USERNAME,
  oauthToken: TWITCH_OAUTH_TOKEN,
  channel: TWITCH_CHANNEL,
  logger,
});

const rateLimiter = new RateLimiter({
  ...cooldowns,
  commands: {
    join: cooldowns.join_seconds ?? 10,
    leave: cooldowns.leave_seconds ?? 5,
    inventory: cooldowns.inventory_seconds ?? 10,
    target: cooldowns.target_seconds ?? 30,
    ...(cooldowns.commands ?? {}),
  },
  default_seconds: cooldowns.default_seconds ?? 5,
  global_max: cooldowns.global_max ?? 30,
  global_window_seconds: cooldowns.global_window_seconds ?? 10,
});

const commandParser = new CommandParser({
  commandMap,
  rateLimiter,
  gameClient,
  twitchClient,
});

const lootRoller = new LootRoller(lootTables);

// ---------------------------------------------------------------------------
// EventSub loot event handlers
// ---------------------------------------------------------------------------
function buildItemEntry(itemName, opts = {}) {
  return {
    name: String(itemName),
    qty: 1,
    notes: opts.notes ?? '',
    effect: opts.effect ?? '',
    is_magic: opts.is_magic ?? false,
    charges_current: opts.charges_current ?? 0,
    charges_max: opts.charges_max ?? 0,
  };
}

async function grantLoot(username, displayName, tableName, trigger, announcement) {
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

  if (announcement) {
    twitchClient.say(`#${TWITCH_CHANNEL.replace(/^#/, '')}`, announcement(displayName, safe));
  }
}

// ---------------------------------------------------------------------------
// EventSub wiring (optional — only if TWITCH_CLIENT_ID is set)
// ---------------------------------------------------------------------------
let eventSubClient = null;

if (TWITCH_CLIENT_ID && process.env.TWITCH_OAUTH_TOKEN && process.env.TWITCH_BROADCASTER_ID) {
  eventSubClient = new EventSubClient({
    clientId: TWITCH_CLIENT_ID,
    oauthToken: TWITCH_OAUTH_TOKEN,
    broadcasterId: process.env.TWITCH_BROADCASTER_ID,
    logger,
  });

  const channel = `#${TWITCH_CHANNEL.replace(/^#/, '')}`;

  eventSubClient.on('sub', async ({ username, displayName, months, gifted, gifterDisplay }) => {
    const valid = validateUsername(username);
    if (!valid) return;
    const trigger = gifted ? 'gifted_sub' : months > 1 ? 'resub' : 'sub';
    const tableName = lootTables.sub ? 'sub' : 'default';
    const announce = gifted
      ? (dn, item) => `${gifterDisplay ?? '?'} gifted a sub to ${dn}! They receive ${item}!`
      : (dn, item) => `${dn} subscribed${months > 1 ? ` (${months} months!)` : ''}! They receive ${item}!`;
    await grantLoot(valid, displayName, tableName, trigger, announce);
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
    const tableName = 'raid';
    const announce = (dn, item) => `${dn} raided with ${viewers} viewers! They receive ${item}!`;
    await grantLoot(valid, fromDisplayName, tableName, 'raid', announce);
  });
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
async function main() {
  logger.info('[ChatBridge] Starting Twitch Chat Bridge…');

  await gameClient.start();
  logger.info('[ChatBridge] Game server client connected');

  twitchClient.onMessage((channel, tags, message) => commandParser.handle(channel, tags, message));
  await twitchClient.connect();
  logger.info('[ChatBridge] Twitch IRC client connected');

  if (eventSubClient) {
    eventSubClient.start().catch(err => logger.error('[EventSub] fatal error:', err));
    logger.info('[ChatBridge] EventSub client started');
  } else {
    logger.warn('[ChatBridge] EventSub disabled — set TWITCH_CLIENT_ID and TWITCH_BROADCASTER_ID to enable sub/bits/raid loot');
  }

  // Handle server-side events (kill switch, etc.)
  gameClient.on('chat_bridge_status', ({ paused }) => {
    logger.info(`[ChatBridge] Kill switch: bridge is now ${paused ? 'PAUSED' : 'ACTIVE'}`);
    if (paused) {
      const ch = `#${TWITCH_CHANNEL.replace(/^#/, '')}`;
      twitchClient.say(ch, 'Chat bridge is temporarily paused by the DM. Hang tight!');
    }
  });

  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
}

function shutdown() {
  logger.info('[ChatBridge] Shutting down…');
  twitchClient.disconnect();
  gameClient.stop();
  if (eventSubClient) eventSubClient.stop();
  process.exit(0);
}

main().catch(err => {
  logger.error('[ChatBridge] Fatal startup error:', err);
  process.exit(1);
});
