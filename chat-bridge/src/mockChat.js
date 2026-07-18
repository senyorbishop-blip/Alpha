'use strict';

/**
 * chat-bridge/src/mockChat.js — Mock chat mode (MOCK_CHAT=true / npm run mock).
 *
 * Lets the whole bridge pipeline be exercised locally with no Twitch account,
 * tokens, or internet. Provides drop-in replacements for TwitchClient and
 * EventSubClient plus an interactive stdin REPL:
 *
 *   alice: !join          — simulate a chat line from user "alice"
 *   !join                 — bare messages default to user "testuser"
 *   /sub alice            — simulate a subscription EventSub event
 *   /giftsub bob 5        — bob gifts 5 subs to fake recipients
 *   /bits carol 500       — carol cheers 500 bits
 *   /raid dan 20          — dan raids with 20 viewers
 *   /spam 25              — 25 random commands from 25 fake users (rate-limit test)
 *   /help, /quit
 *
 * Bot replies that would go to Twitch chat print to the console prefixed
 * [BOT→chat]. Everything routes through the exact same CommandParser and
 * EventSub handler paths as real traffic — only the transport is faked.
 */

const readline = require('readline');

// ---------------------------------------------------------------------------
// MockTwitchClient — same interface as TwitchClient (onMessage / connect /
// say / reply / disconnect) plus injectMessage() for the REPL.
// ---------------------------------------------------------------------------
class MockTwitchClient {
  constructor({ channel = '#mock', logger } = {}) {
    this._channel = channel.startsWith('#') ? channel : `#${channel}`;
    this._logger = logger ?? console;
    this._messageHandler = null;
  }

  onMessage(fn) {
    this._messageHandler = fn;
  }

  async connect() {
    this._logger.info(`[MockTwitch] mock chat connected (channel ${this._channel}) — no Twitch connection made`);
  }

  say(channel, message) {
    console.log(`[BOT→chat] ${message}`);
  }

  reply(channel, username, message) {
    this.say(channel, `@${username} ${message}`);
  }

  disconnect() {}

  /** Simulate an incoming chat line, exactly as tmi.js would deliver it. */
  injectMessage(username, message) {
    if (!this._messageHandler) return;
    const uname = String(username).toLowerCase();
    const tags = {
      username: uname,
      'display-name': String(username),
      'user-id': `mock-${uname}`,
    };
    Promise.resolve(this._messageHandler(this._channel, tags, message)).catch(err =>
      this._logger.error('[MockTwitch] message handler error:', err)
    );
  }
}

// ---------------------------------------------------------------------------
// MockEventSubClient — same interface as EventSubClient (on / start / stop)
// plus emit() so the REPL can fire simulated sub/bits/raid events.
// ---------------------------------------------------------------------------
class MockEventSubClient {
  constructor({ logger } = {}) {
    this._logger = logger ?? console;
    this._handlers = new Map(); // event → [fn]
  }

  on(event, fn) {
    if (!this._handlers.has(event)) this._handlers.set(event, []);
    this._handlers.get(event).push(fn);
  }

  emit(event, payload) {
    const fns = this._handlers.get(event) ?? [];
    if (fns.length === 0) {
      this._logger.warn(`[MockEventSub] no handler registered for "${event}"`);
    }
    for (const fn of fns) {
      Promise.resolve(fn(payload)).catch(err =>
        this._logger.error(`[MockEventSub] ${event} handler error:`, err)
      );
    }
  }

  async start() {
    this._logger.info('[MockEventSub] mock EventSub ready — fire events with /sub /giftsub /bits /raid');
  }

  stop() {}
}

// ---------------------------------------------------------------------------
// REPL
// ---------------------------------------------------------------------------
const FAKE_USERS = [
  'aria', 'borin', 'cassia', 'dax', 'edda', 'fenwick', 'grix', 'hilda',
  'ilyan', 'jorra', 'krag', 'lumen', 'mira', 'nyx', 'ophid', 'pippa',
];

const SPAM_COMMANDS = [
  '!join', '!join', '!inventory', '!target Goblin', '!target Dragon',
  '!help', '!leave', '!vote 1', '!vote 2', '!me',
];

const HELP_TEXT = `
Mock chat mode — commands:
  <username>: <message>   simulate a chat line, e.g.  alice: !join
  <message>               bare messages come from "testuser", e.g.  !join
  /sub <user> [months]    simulate a subscription (months > 1 = resub)
  /resub <user> [months]  simulate a resub whose renewal arrives as BOTH the
                          subscribe and message events (dedup check: one roll)
  /giftsub <gifter> <n>   simulate <n> gifted subs from <gifter> ("anon" = anonymous)
  /bits <user> <amount>   simulate a cheer
  /raid <user> <viewers>  simulate an incoming raid
  /spam <n>               fire n random commands from n fake users rapidly
  /help                   show this help
  /quit                   exit
`.trim();

/**
 * Start the interactive mock-chat REPL.
 *
 * @param {object} opts
 * @param {MockTwitchClient}   opts.twitchClient
 * @param {MockEventSubClient} opts.eventSubClient
 * @param {object}             opts.logger
 * @param {function}           [opts.onQuit] — called on /quit (defaults to process.exit)
 */
function startMockRepl({ twitchClient, eventSubClient, logger, onQuit }) {
  const quit = onQuit ?? (() => process.exit(0));

  console.log('');
  console.log(HELP_TEXT);
  console.log('');

  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    prompt: 'mock> ',
  });

  function fireSub(username, { months = 1, gifted = false, gifterDisplay = null } = {}) {
    eventSubClient.emit('sub', {
      username: username.toLowerCase(),
      displayName: username,
      months,
      gifted,
      gifterDisplay,
    });
  }

  function handleSlash(cmd, args) {
    switch (cmd) {
      case '/help':
        console.log(HELP_TEXT);
        break;

      case '/quit':
      case '/exit':
        rl.close();
        quit();
        break;

      case '/sub': {
        const user = args[0];
        if (!user) { console.log('usage: /sub <user> [months]'); break; }
        const months = Math.max(1, parseInt(args[1], 10) || 1);
        fireSub(user, { months });
        break;
      }

      case '/resub': {
        const user = args[0];
        if (!user) { console.log('usage: /resub <user> [months]'); break; }
        const months = Math.max(2, parseInt(args[1], 10) || 2);
        // Same renewal delivered as BOTH channel.subscribe and
        // channel.subscription.message — the handler must roll only once.
        fireSub(user, { months });
        fireSub(user, { months });
        break;
      }

      case '/giftsub': {
        const gifter = args[0];
        const count = Math.max(1, parseInt(args[1], 10) || 1);
        if (!gifter) { console.log('usage: /giftsub <gifter|anon> <count>'); break; }
        // Mirrors real EventSub: one 'giftsub' batch event for the gifter,
        // plus a gifted 'sub' event per recipient (recipients never roll).
        const anonymous = /^anon(ymous)?$/i.test(gifter);
        eventSubClient.emit('giftsub', {
          username: anonymous ? '' : gifter.toLowerCase(),
          displayName: anonymous ? 'An Anonymous Gifter' : gifter,
          total: count,
          anonymous,
        });
        for (let i = 0; i < count; i++) {
          const recipient = FAKE_USERS[i % FAKE_USERS.length];
          fireSub(recipient, { gifted: true, gifterDisplay: anonymous ? null : gifter });
        }
        break;
      }

      case '/bits': {
        const user = args[0];
        const amount = parseInt(args[1], 10);
        if (!user || !Number.isFinite(amount) || amount <= 0) { console.log('usage: /bits <user> <amount>'); break; }
        eventSubClient.emit('bits', { username: user.toLowerCase(), displayName: user, amount });
        break;
      }

      case '/raid': {
        const user = args[0];
        const viewers = parseInt(args[1], 10);
        if (!user || !Number.isFinite(viewers) || viewers <= 0) { console.log('usage: /raid <user> <viewers>'); break; }
        eventSubClient.emit('raid', { fromUsername: user.toLowerCase(), fromDisplayName: user, viewers });
        break;
      }

      case '/spam': {
        const n = parseInt(args[0], 10);
        if (!Number.isFinite(n) || n <= 0) { console.log('usage: /spam <n>'); break; }
        logger.info(`[MockChat] spamming ${n} random commands from ${n} fake users`);
        for (let i = 0; i < n; i++) {
          const user = `${FAKE_USERS[i % FAKE_USERS.length]}${Math.floor(i / FAKE_USERS.length) || ''}`;
          const command = SPAM_COMMANDS[Math.floor(Math.random() * SPAM_COMMANDS.length)];
          twitchClient.injectMessage(user, command);
        }
        break;
      }

      default:
        console.log(`Unknown command ${cmd} — type /help`);
        break;
    }
  }

  rl.on('line', (line) => {
    const input = line.trim();
    if (!input) { rl.prompt(); return; }

    if (input.startsWith('/')) {
      const [cmd, ...args] = input.split(/\s+/);
      handleSlash(cmd.toLowerCase(), args);
    } else {
      // "<username>: <message>" — or a bare message from "testuser"
      const match = input.match(/^([A-Za-z0-9_]+):\s*(.+)$/);
      if (match) {
        twitchClient.injectMessage(match[1], match[2]);
      } else {
        twitchClient.injectMessage('testuser', input);
      }
    }
    rl.prompt();
  });

  rl.on('close', () => quit());
  rl.prompt();
}

module.exports = { MockTwitchClient, MockEventSubClient, startMockRepl };
