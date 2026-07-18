# Changelog

All notable release-facing changes are documented here.

This project currently uses a practical founder-beta version format:

- `v0.9.x-beta` for founder beta drops
- `v0.9.x-rcN` for release candidates
- `v1.0.0` when public launch criteria are met

## [Unreleased]

### Added
- Overlay event ticker: a rolling play-by-play feed (last ~5 lines, newest on top, ~8s fade, burst pacing) built into the Game Overlay browser source and also available as its own "Event Ticker" browser-source URL in the Stream panel. It carries duel round-by-round narration, quest departures/returns, reward rolls, level-ups, deaths, DM item grants, and chat item uses. Events carry server-stamped seq numbers so the overlay shows a "missed events" marker instead of silently dropping lines; the DM's arena quiet mode queues ticker events along with the chat announcements.

### Changed
- Chat narration is now slim by default: duels post ONE chat line total — the result with rewards folded in (e.g. "🏆 alice defeats bob in 4 rounds! +60 XP, +15 gold") — while the start and round-by-round messages appear on the ticker instead. Direct replies to a chatter's own commands stay in chat. Streamers can restore the classic round-by-round chat narration with `full_chat_narration: true` in `chat-bridge/config/stream.json` (or `CHAT_FULL_NARRATION=true`).
- The game server now auto-starts the Twitch chat bridge as a supervised child process — no manual `npm start`. It spawns when a session has a connected Twitch channel with the chat bridge enabled, restarts crashed bridges with capped exponential backoff (marked failed after 5 retries), pipes bridge output into the server log with a `[chat-bridge]` prefix, and cleanly terminates the process tree (Windows-safe) on toggle-off, Twitch disconnect, or server shutdown.
- Stream panel bridge status now shows running / starting / failed (with the last error line) / stopped, a recent-bridge-log view, and a "Restart bridge" button. A clear panel error appears when Node.js is not installed.
- Manual `npm start` keeps working for development: the server skips auto-spawn when a bridge for the session is already connected.

## [v0.9.1-beta] - 2026-03-30

### Added
- Focused release-readiness regression tests for founder-beta handoff docs so critical operator links and version/release-note pointers stay valid.

### Changed
- Hardened operator handoff docs with explicit smoke-test and support-boundary guidance in `START_HERE.md`.
- Tightened backup/update/rollback instructions with concrete post-update and rollback validation checkpoints.
- Clarified founder-beta known limitations to avoid overpromising mobile/hosting/provider support.
- Updated release operations baseline with explicit critical-path checks and lightweight diagnostics guidance.
- Bumped release metadata to `v0.9.1-beta` and added matching release note entry.

## [v0.9.0-beta] - 2026-03-28

### Added
- Founder beta packaging docs:
  - `START_HERE.md`
  - setup/install guide
  - DM guide
  - player guide
  - admin guide
  - hosting/access guide
  - known issues/beta notes
  - backup/update/rollback guide
  - founder beta readiness checklist
- Release metadata files:
  - `VERSION`
  - `docs/releases/FOUNDER_BETA_v0.9.0-beta.md`
- Configuration templates expanded for safer onboarding:
  - `.env.example` updates
  - `config.txt.example`

### Changed
- Top-level `README.md` now points directly to founder-beta handoff docs and release docs.

### Notes
- This release intentionally focuses on packaging/readiness/documentation.
- Core runtime/gameplay logic was not refactored for this release package.
