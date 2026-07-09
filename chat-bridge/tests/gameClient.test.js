'use strict';

/**
 * Integration tests for GameClient heartbeat + disconnect resilience.
 *
 * The real game server (main.py _websocket_heartbeat_loop) sends
 * {"type":"ping"} every 30s and closes sockets with "1001 Heartbeat timeout"
 * after 60s without an inbound frame. These tests run a local mock of the
 * auth endpoint + WebSocket server and assert that the bridge:
 *   - replies to server pings with {"type":"pong"} (same as the browser client)
 *   - proactively sends pongs on an interval while connected
 *   - queues outbound messages while disconnected and flushes on reconnect
 *   - bounds the queue by dropping the oldest message
 */

const http = require('http');
const WebSocket = require('ws');
const { GameClient } = require('../src/gameClient');

const silentLogger = { info: () => {}, warn: () => {}, error: () => {} };

function fakeJwt() {
  const b64 = (obj) => Buffer.from(JSON.stringify(obj)).toString('base64url');
  const header = b64({ alg: 'HS256', typ: 'JWT' });
  const payload = b64({ sub: 'chat_bridge', role: 'chat_bridge', exp: Math.floor(Date.now() / 1000) + 3600 });
  return `${header}.${payload}.fakesig`;
}

/** Mock game server: POST /api/chat-bridge/auth + a WebSocket endpoint. */
function startMockServer() {
  return new Promise((resolve) => {
    const state = {
      sockets: [],          // all WS connections, oldest first
      messages: [],         // every parsed inbound frame across all connections
      waiters: [],          // { predicate, resolve } for messageWaiter()
    };

    const httpServer = http.createServer((req, res) => {
      if (req.method === 'POST' && req.url === '/api/chat-bridge/auth') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ jwt: fakeJwt() }));
        return;
      }
      res.writeHead(404);
      res.end();
    });

    const wss = new WebSocket.Server({ server: httpServer });
    wss.on('connection', (socket) => {
      state.sockets.push(socket);
      socket.on('message', (raw) => {
        let msg;
        try { msg = JSON.parse(raw); } catch { return; }
        state.messages.push(msg);
        for (const waiter of state.waiters.splice(0)) {
          if (waiter.predicate(msg)) waiter.resolve(msg);
          else state.waiters.push(waiter);
        }
      });
    });

    httpServer.listen(0, '127.0.0.1', () => {
      const port = httpServer.address().port;
      resolve({
        url: `ws://127.0.0.1:${port}`,
        state,
        currentSocket: () => state.sockets[state.sockets.length - 1],
        waitForConnection: (afterCount) => new Promise((res) => {
          if (state.sockets.length > afterCount) { res(state.sockets[state.sockets.length - 1]); return; }
          wss.on('connection', function onConn(socket) {
            if (state.sockets.length > afterCount) { wss.off('connection', onConn); res(socket); }
          });
        }),
        waitForMessage: (predicate, timeoutMs = 3000) => new Promise((res, rej) => {
          const existing = state.messages.find(predicate);
          if (existing) { res(existing); return; }
          const timer = setTimeout(() => rej(new Error('timed out waiting for message')), timeoutMs);
          state.waiters.push({ predicate, resolve: (m) => { clearTimeout(timer); res(m); } });
        }),
        close: () => new Promise((res) => {
          for (const s of state.sockets) { try { s.terminate(); } catch (_) {} }
          wss.close(() => httpServer.close(() => res()));
        }),
      });
    });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

describe('GameClient heartbeat', () => {
  let server, client;

  afterEach(async () => {
    if (client) { client.stop(); client = null; }
    if (server) { await server.close(); server = null; }
  });

  test('replies to a server {"type":"ping"} with {"type":"pong"}', async () => {
    server = await startMockServer();
    client = new GameClient({
      wsUrl: server.url, sessionId: 'sess1', bridgeToken: 'tok', logger: silentLogger,
      heartbeatIntervalMs: 60000,  // effectively off — isolate the ping→pong reply
    });
    await client.start();

    server.currentSocket().send(JSON.stringify({ type: 'ping' }));
    const pong = await server.waitForMessage((m) => m.type === 'pong');
    expect(pong).toEqual({ type: 'pong' });
  });

  test('sends proactive heartbeat pongs on the configured interval while connected', async () => {
    server = await startMockServer();
    client = new GameClient({
      wsUrl: server.url, sessionId: 'sess1', bridgeToken: 'tok', logger: silentLogger,
      heartbeatIntervalMs: 50,
    });
    await client.start();

    await sleep(400);
    const pongs = server.state.messages.filter((m) => m.type === 'pong');
    // 400ms at a 50ms interval — allow generous scheduling slack.
    expect(pongs.length).toBeGreaterThanOrEqual(3);

    // Heartbeat stops on disconnect: kill the socket, ensure no new pongs.
    client.stop();
    await sleep(100);
    const countAfterStop = server.state.messages.filter((m) => m.type === 'pong').length;
    await sleep(200);
    const countLater = server.state.messages.filter((m) => m.type === 'pong').length;
    expect(countLater).toBe(countAfterStop);
    client = null;
  });

  test('never lets the server heartbeat timeout window elapse silently', async () => {
    // Guard against the interval regressing above the server's 60s timeout.
    const c = new GameClient({ wsUrl: 'ws://localhost:9', sessionId: 's', bridgeToken: 't', logger: silentLogger });
    expect(c._heartbeatIntervalMs).toBeLessThan(60000);
    expect(c._heartbeatIntervalMs).toBeGreaterThan(0);
  });
});

describe('GameClient disconnect resilience', () => {
  let server, client;

  afterEach(async () => {
    if (client) { client.stop(); client = null; }
    if (server) { await server.close(); server = null; }
  });

  test('queues messages while disconnected and flushes them on reconnect', async () => {
    server = await startMockServer();
    client = new GameClient({
      wsUrl: server.url, sessionId: 'sess1', bridgeToken: 'tok', logger: silentLogger,
      heartbeatIntervalMs: 60000,
      reconnectDelayMs: 200,  // reconnect fires at ~300ms — sends below land in the gap
    });
    await client.start();

    // Server-side close, exactly like a heartbeat-timeout reap.
    const first = server.currentSocket();
    first.close(1001, 'Heartbeat timeout');
    await sleep(50);  // let the close propagate to the client

    // Sent during the gap — must not be lost.
    const sendPromise = client.send('chat_message_from_viewer', { text: 'hello during gap' });

    await server.waitForConnection(1);
    const flushed = await server.waitForMessage((m) => m.type === 'chat_message_from_viewer');
    expect(flushed.payload.text).toBe('hello during gap');
    await expect(sendPromise).resolves.toBeNull();  // fire-and-forget resolves null
  });

  test('ack-type messages queued during a gap still resolve with the server ack', async () => {
    server = await startMockServer();
    client = new GameClient({
      wsUrl: server.url, sessionId: 'sess1', bridgeToken: 'tok', logger: silentLogger,
      heartbeatIntervalMs: 60000,
      reconnectDelayMs: 200,  // reconnect fires at ~300ms — sends below land in the gap
    });
    await client.start();

    server.currentSocket().close(1001, 'Heartbeat timeout');
    await sleep(50);

    const sendPromise = client.send('chat_participant_join', { twitch_username: 'viewer1' });

    const socket = await server.waitForConnection(1);
    const joinMsg = await server.waitForMessage((m) => m.type === 'chat_participant_join');
    socket.send(JSON.stringify({
      type: 'chat_participant_join_ack',
      payload: { _req_id: joinMsg.payload._req_id, ok: true },
    }));

    const ack = await sendPromise;
    expect(ack).toMatchObject({ ok: true });
  });

  test('bounded queue drops the oldest message when full', async () => {
    server = await startMockServer();
    client = new GameClient({
      wsUrl: server.url, sessionId: 'sess1', bridgeToken: 'tok', logger: silentLogger,
      heartbeatIntervalMs: 60000,
      reconnectDelayMs: 200,  // reconnect fires at ~300ms — sends below land in the gap
      maxQueuedMessages: 2,
    });
    await client.start();

    server.currentSocket().close(1001, 'Heartbeat timeout');
    await sleep(50);

    const p1 = client.send('viewer_cmd', { n: 1 });
    client.send('viewer_cmd', { n: 2 });
    client.send('viewer_cmd', { n: 3 });

    await expect(p1).resolves.toBeNull();  // dropped immediately when 3rd enqueued

    await server.waitForConnection(1);
    await server.waitForMessage((m) => m.type === 'viewer_cmd' && m.payload.n === 3);
    const delivered = server.state.messages
      .filter((m) => m.type === 'viewer_cmd')
      .map((m) => m.payload.n);
    expect(delivered).toEqual([2, 3]);
  });
});
