# Tavern Tabletop — Twitch Chat Bridge

Connects Twitch chat and EventSub events to the Tavern Tabletop game server so stream viewers can participate directly from chat without a game account.

---

## Architecture

```
Twitch Chat (tmi.js) ─────────────┐
                                   ▼
Twitch EventSub (WebSocket) ──► chat-bridge (Node.js)
                                   │  WebSocket (chat_bridge role JWT)
                                   ▼
                        Tavern Tabletop game server (Python/FastAPI)
```

The bridge connects to the game server as a **privileged service account** (`role=chat_bridge`). It can only perform chat-participant actions — it has no DM permissions. All enforcement is server-side.

---

## Prerequisites

- Node.js ≥ 18
- A running Tavern Tabletop game server
- A Twitch bot account (free — you can use your main account during development)
- *(For sub/bits/raid loot only)* A Twitch app registered at [dev.twitch.tv](https://dev.twitch.tv/console)

---

> **Recommended: connect Twitch from inside the app.** If the DM connects their channel via **campaign settings → Connect Twitch** (OAuth), the bridge fetches the channel and tokens per campaign from the game server automatically — steps 1–2 below then only matter as an env-var fallback, and multiple DMs can each run their own channel. The env vars `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` remain the app-level client credentials used by that flow.

## 1. Create a Twitch Bot Account (for chat)

1. Log in to [twitch.tv](https://twitch.tv) with a **separate bot account** (e.g. `MyTavernBot`). Using your main avoids spam-filter issues.
2. Go to [twitchapps.com/tmi](https://twitchapps.com/tmi/) while logged in as the bot.
3. Click **Connect** → copy the OAuth token (starts with `oauth:`).
4. Set `TWITCH_BOT_USERNAME` and `TWITCH_OAUTH_TOKEN` in your `.env`.

---

## 2. Get a User Access Token (for EventSub)

EventSub subscriptions for sub/bits/raid require a **user access token** for the broadcaster's account (not the bot account) with the following scopes:

```
channel:read:subscriptions
bits:read
channel:read:raids
```

Steps:

1. Create an app at [dev.twitch.tv/console](https://dev.twitch.tv/console) → **Register Your Application**.
   - OAuth Redirect URLs: `http://localhost:3000` (for local token generation)
   - Category: Chat Bot
   - Copy **Client ID** and **Client Secret**.

2. Generate a user token using the [Twitch CLI](https://dev.twitch.tv/docs/cli) or the Token Generator:
   ```bash
   twitch token -u -s 'channel:read:subscriptions bits:read channel:read:raids'
   ```
   Copy the resulting **User Access Token** → set as `TWITCH_OAUTH_TOKEN`.

3. Find your broadcaster user ID:
   ```bash
   curl -H "Client-Id: <CLIENT_ID>" \
        -H "Authorization: Bearer <USER_TOKEN>" \
        "https://api.twitch.tv/helix/users?login=your_channel"
   ```
   Copy the `id` field → set as `TWITCH_BROADCASTER_ID`.

---

## 3. Configure the Game Server

In the game server's `.env`:

```
CHAT_BRIDGE_TOKEN=<your_shared_secret>
```

Generate a secret:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## 4. Configure the Bridge

```bash
cp .env.example .env
```

Fill in all values in `.env`:

| Variable | Description |
|---|---|
| `TWITCH_BOT_USERNAME` | Bot account username |
| `TWITCH_OAUTH_TOKEN` | OAuth token from step 1 (or user token from step 2) |
| `TWITCH_CHANNEL` | Your channel name (without `#`) |
| `TWITCH_CLIENT_ID` | App client ID (EventSub only) |
| `TWITCH_CLIENT_SECRET` | App client secret (EventSub only) |
| `TWITCH_BROADCASTER_ID` | Your Twitch user ID (EventSub only) |
| `GAME_SERVER_WS_URL` | WebSocket URL of game server (e.g. `ws://localhost:8000`) |
| `GAME_SESSION_ID` | Session ID from the DM's browser URL |
| `CHAT_BRIDGE_TOKEN` | Shared secret (must match game server's `CHAT_BRIDGE_TOKEN`) |

---

## 5. Customize Loot Tables

Edit `config/loot-tables.json` to change what items are rolled for subs, bits, and raids. Each entry has a `weight` (higher = more common):

```json
"sub": [
  { "item": "Potion of Healing", "weight": 60 },
  { "item": "Wand of Fireballs", "weight": 5 }
]
```

Bits tiers are configured as an array with `threshold` values — the highest threshold the bit amount meets is used.

---

## 6. Add Custom Chat Commands

Edit `config/commands.json`. Supported actions are `join`, `leave`, `inventory`, `target`, `help`, `vote`, `name`, `me`, `bag`, `equip`, `shop`, `buy`, `leaderboard`, `reroll`, `levelup`, and `arena` (routes `!duel` / `!accept` / `!decline` / `!attack` / `!defend` / `!flee`). Map any command prefix to one of these actions:

```json
{
  "!join":    "join",
  "!enter":   "join",
  "!inv":     "inventory",
  "!shoot":   "target"
}
```

The optional `_usage_hints` map adds per-item usage hints to `!inventory` output (matched by item-name substring):

```json
"_usage_hints": {
  "potion": "!target <ally> to heal",
  "fireball": "!target <enemy>"
}
```

All bot reply text lives in `config/responses.json` with `{user}` / `{target}` / `{item}` / `{damage}` / `{count}`-style placeholders, so flavor text is fully customizable per campaign.

---

## 7. Running the Bridge

```bash
npm install
npm start
```

For development with auto-restart:
```bash
npm run dev
```

---

## 8. Running with Docker

```dockerfile
FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
CMD ["node", "src/index.js"]
```

```bash
docker build -t chat-bridge .
docker run --env-file .env chat-bridge
```

---

## Chat Commands Reference

### Game commands

| Command | Description |
|---|---|
| `!join` | Register as a chat participant. Idempotent; returning chatters get a welcome-back with their stats. |
| `!leave` | Leave the session roster. |
| `!inventory` / `!inv` | See your current items with quantities and usage hints. |
| `!target <name>` / `!use <name>` | Use your first available item on a map token matching `<name>` (fuzzy, case-insensitive). Executes real game rolls — damage/heal results come back to chat. |
| `!name <character name>` | Set your character name (sanitized, unique per channel). |
| `!me` | Your character summary: name, class, level, XP, gold, gear, lifetime stats, arena W/L. |
| `!vote <number>` | Vote in the DM's active poll (no `!join` required; re-voting changes your vote). |
| `!help` | List available commands. |

### Arena commands (isolated PvP sandbox — never touches the campaign)

| Command | Description |
|---|---|
| `!duel <name> [gold]` | Challenge another chatter (60s expiry), optionally wagering arena gold. |
| `!accept` / `!decline` | Respond to a pending challenge. |
| `!attack` / `!defend` / `!flee` | Interactive-mode turn actions (when `interactive_mode` is enabled). |
| `!bag` | Full arena inventory with each item's stat bonuses. |
| `!equip <item>` | Equip gear (one item each in weapon / armor / trinket slots). |
| `!shop` | Rotating daily shop stock with prices. |
| `!buy <number or name>` | Buy with arena gold. |
| `!leaderboard` | Top arena fighters. |
| `!reroll` | Reroll your class and stats (policy configurable: free once / gold purchase / off). |
| `!levelup <stat>` | Spend a pending level-up stat point (auto-assigns to class priorities after 60s). |

On first join every chatter is assigned a random SRD class and rolls 4d6-drop-lowest ability scores (announced in chat). Duels use real derived stats — 5e modifiers, class hit die HP, DEX-based AC, crits on natural 20 — and award XP, gold, and item drops. All economy numbers live in `config/arena-balance.json`.

---

## DM Controls

The DM connects their own Twitch channel from **campaign settings → Twitch Integration** (OAuth flow; tokens are stored encrypted server-side and auto-refreshed). The same panel has:

- **Chat bridge enabled** toggle — the kill switch; pauses all chat interactions instantly (enforced server-side).
- **Persist between sessions** — everything / stats only / nothing.
- **Chat characters** — view, rename, or reset any chat character.
- **OBS Overlays** — copyable URLs with regenerate buttons (see below).

WebSocket message types available to DM tooling: `dm_chat_bridge_kill_switch` (also accepts `arena_enabled` / `arena_quiet`), `dm_grant_chat_participant_item` (an optional `power_id` on the item picks which viewer power it executes as), `dm_chat_participants_get`, `dm_chat_participant_update`, `dm_chat_participant_reset`, `dm_chat_persistence_mode`.

---

## OBS Setup

1. In campaign settings → **OBS Overlays**, copy an overlay URL (each contains a secret key — click **Regen** if it ever leaks on stream).
2. In OBS: **Sources → + → Browser**, paste the URL.
3. Set the size — Game Overlay: **1920×1080**; Arena Panel: **520×300** (position it wherever you like).
4. Done. Overlays render on a transparent background, show nothing while idle, and auto-reconnect if the connection drops. They are read-only: overlay pages cannot send commands to the game server.

---

## Security Notes

- The bridge authenticates with a **shared secret** (`CHAT_BRIDGE_TOKEN`). Rotate it if compromised.
- The `chat_bridge` role is enforced **server-side** — the bridge cannot invoke DM actions even if the code is modified.
- All usernames and command arguments are sanitized (HTML-escaped, length-limited) before entering game state or overlays.
- Per-user and global rate limits prevent chat floods from reaching the game server.

---

## Running Tests

```bash
npm test
```

Tests cover the command parser (all commands, rate limiting, sanitization), the loot roller (distribution, tiers, edge cases), vote tallying, arena duel resolution and wagers, arena progression (classes, stat math, XP/gold/drops, shop), and arena isolation — asserting a full duel lifecycle only ever sends arena-scoped message types to the game server, never campaign-mutating ones.
