# WS Desync Hardening — Close-on-Reap, Push Seq, Client Watchdog

## The bug

Players desynced mid-session and stayed stale until a manual relaunch.

Root cause (`server/connections.py`): `_gather_sends()` reaped sockets whose
send failed or timed out by removing them from the registry — but never closed
the WebSocket. The client's browser therefore still saw an OPEN socket, its
`onclose` never fired, no reconnect happened, and it silently received nothing
forever. There were also no sequence numbers on pushes, so a client had no way
to detect that it had missed messages.

## The fix — three layers, defense in depth

### 1. Close on reap (server)

When a socket is removed after a failed/timed-out send — in both
`_gather_sends()` and `send_to()` — the server now also closes it:
`ws.close(code=1011)`, best-effort, bounded by `REAP_CLOSE_TIMEOUT_SECONDS`
(3s) and with all errors swallowed, so a wedged close can never stall the
broadcast that reaped it. The close makes the client's `onclose` fire, which
triggers its existing auto-reconnect; reconnect is cheap thanks to the
delta-reconnect path (`server/state_delta.py`).

The close targets the *failed* socket specifically — if the user already
reconnected with a newer socket mid-send, the replacement is untouched
(covered by `tests/test_ws_reap_close_and_seq.py`).

### 2. Sequence numbers + gap detection

- Every pushed frame carries a monotonically increasing `seq`, tracked
  **per connection** (per session+user socket) and reset to 1 whenever a new
  socket is established. Per-connection (not per-game-session) is what makes
  gaps meaningful: targeted `send_to` pushes to one user must not look like
  gaps to everyone else.
- Broadcast paths still `json.dumps` the shared payload once; the seq is
  spliced into the encoded string per recipient (`_stamp_seq_payload`) rather
  than re-serializing a potentially huge message N times.
- Client (`client/static/js/core/ws.js`): tracks the last seq per socket. On a
  gap (`received seq > expected`), it immediately issues a
  `request_state` with `reason: "seq_gap"`, the gap details, and
  `known_revisions` — repairing through the delta path, automatic and
  invisible to the player. Gap resyncs are debounced (5s) and a late
  out-of-order frame (seq ≤ last) is never treated as a gap.
- Server logs every gap-triggered resync at WARNING
  (`[live_state] seq_gap_resync ... expected_seq= received_seq=`) so gap
  frequency is visible in ops logs.

### 3. Client watchdog (dead-downlink detection)

- The server→client heartbeat ping now runs every **20s** (was 30s), seq'd
  like any push. (The server already required client→server heartbeats; this
  strengthens the reverse direction.)
- Client-side: if **no server frame of any kind** arrives for **45s** (more
  than two heartbeat intervals), the client assumes a dead connection, closes
  the socket itself (code 4008), and lets the normal
  `onclose → scheduleReconnect` path recover.
- Background-tab safety: browsers throttle timers in background tabs, but not
  websocket message events. The watchdog therefore re-checks the actual
  elapsed silence when it fires and re-arms instead of closing if frames kept
  arriving.

## Send-timeout sanity for large payloads

The flat 10s per-send timeout was marginal for a 1 MB payload on a mediocre
connection (~1 Mbit/s ⇒ ~8s of pure transfer). The timeout now scales with
frame size: base (10s, env-overridable via `WS_SEND_TIMEOUT_SECONDS`) plus
`byte_size / 128 KiB/s`, capped at 30s (well under the 60s heartbeat timeout).
A 1 MiB sync gets ~18s; tiny frames keep the base. The existing
`outbound_send_timeout` log line already records which user/role and the
message size, so timeouts clustering on large payloads remain visible evidence
for the remaining stage-A payload work.

## Failure/recovery walkthrough

1. A player's laptop sleeps; their socket half-opens.
2. The next broadcast to them times out → socket reaped **and closed**.
3. If the close frame can't reach them (dead link), their client's watchdog
   notices 45s of silence (no 20s heartbeats arriving) and closes/reconnects
   locally.
4. On reconnect the client sends `request_state` with `known_revisions` and
   receives only changed domains.
5. If instead only a *single push window* was lost (e.g. reap raced a healthy
   reconnect), the next received frame's seq jump triggers an immediate,
   invisible delta resync.

## Tests

- `tests/test_ws_reap_close_and_seq.py` — close-on-reap (incl. bounded/
  swallowed close, replacement-socket safety), per-recipient seq contiguity,
  seq reset per socket, seq on role/filtered broadcast paths, timeout scaling.
- `tests/test_ws_client_seq_watchdog.py` — client gap detection (baseline,
  duplicates, debounce), watchdog close on 45s silence, re-arm on any frame,
  throttled-timer safety, superseded-socket no-op, and the runtime-bridge
  `seq_gap → request_state(known_revisions)` hook.
- Existing heartbeat/reconnect/storm suites updated for the 20s ping interval,
  the new ws.js cache-buster version, and watchdog timers being excluded from
  reconnect-timer accounting.
