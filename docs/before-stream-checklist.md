# Before Stream Checklist

Use this short pass after the multi-role live-session smoke test and before going live on OBS/browser capture.

## 1. Start the app

```bash
python -m pytest tests/ -v --tb=short
uvicorn main:app --reload
```

Confirm the server prints the DM/local URL and does not show startup errors.

## 2. Open the live table roles

1. Open the DM link in your main browser.
2. Open one Player test browser/profile and join the same session.
3. Open one Viewer test browser/profile and join the same session.
4. Confirm the DM top bar shows the lightweight **Stream readiness** panel.
5. Confirm Player and Viewer do **not** see DM-only rails/panels.

## 3. Run the smoke test

```bash
npm run test:e2e -- tests/e2e/live-session-regression.spec.ts
```

Treat any Playwright failure as stream-blocking until investigated.

## 4. Check stream readiness signals

In the DM-only status area, verify:

- connected player count is correct;
- connected viewer count is correct;
- WebSocket status is connected;
- session id and current map id look correct;
- payload warning count is zero or understood;
- reconnect warning count is zero before intentional reconnect testing;
- last save/autosave changes after using the Save button.

## 5. Reconnect check

1. Refresh the Player tab.
2. Wait for live sync to reconnect without a blank screen.
3. Confirm the player token, HP, combat state, and Quick Actions return.
4. Confirm the DM status area reflects reconnect warnings if a reconnect happened.

## 6. Quick Actions check

1. Select/confirm the Player has an active character profile.
2. Confirm weapon, spell, and item-granted actions render.
3. If metadata is missing, confirm a friendly diagnostic/toast appears instead of a raw JavaScript crash.

## 7. Fog and hidden-token safety

1. Place a hidden/staged/fog-hidden NPC token as DM.
2. Confirm Player and Viewer cannot see the token name on the map, in visible logs, or in diagnostics.
3. Reveal only what you intend to show on stream.

## 8. Rest check

1. Trigger short rest and confirm the Player sees a friendly rest result.
2. Trigger long rest and confirm HP/resources/Quick Actions resync.
3. If rest sync fails, retry or reconnect before stream.

## 9. Twitch Stream panel expand/collapse check

With Twitch connected, open DM Modes ▸ **Stream** and verify the collapsible
rows (automated coverage: `tests/e2e/twitch-rewards-panel.spec.ts`):

1. In **Rewards — subs, gifts & bits**, click each tier row ("Single sub /
   resub", "Gift subs ×5+", "Bits 1000+", …) — clicking anywhere on the row
   must expand the full viewer-power checkbox list, and the arrow must rotate.
2. Tick/untick a power and confirm the "(N powers)" count in the row header
   updates immediately; **Save rewards** persists and the bridge log shows the
   new config applied without a restart.
3. Expand **Recent bridge log** (same row pattern) and confirm the log lines
   appear.
4. Click elsewhere on the page (e.g. the map) and confirm expanded rows stay
   open — a stray click must not collapse the panel or discard unsaved ticks.

## 10. OBS / browser performance

- Capture the browser/window you intend to stream.
- Verify readable zoom, stable FPS, and no flashing reconnect/blank screen.
- Close extra debug tabs and keep `dnd_live_debug` disabled unless actively diagnosing.
