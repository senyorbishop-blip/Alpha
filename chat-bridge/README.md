# Tavern Tabletop — Twitch Chat Bridge

Connects Twitch chat and EventSub events to the Tavern Tabletop game server so stream viewers can participate directly from chat without a game account.

---

## Quickstart: Mock Chat Mode (recommended first test)

You can test the **entire pipeline** — commands, loot, rate limiting, EventSub rewards — with **no Twitch account, no tokens, and no internet**. Only the game server needs to be running.

```bash
cd chat-bridge
npm install

# Only the game-server vars are needed — nothing Twitch-related:
export GAME_SERVER_WS_URL=ws://localhost:8000
export GAME_SESSION_ID=<session id from the DM's browser URL>
export CHAT_BRIDGE_TOKEN=<must match the game server's CHAT_BRIDGE_TOKEN>

npm run mock        # or: MOCK_CHAT=true npm start
```

You get an interactive prompt that simulates Twitch chat. Type `<username>: <message>` to speak as a viewer; a bare message comes from `testuser`. Bot replies that would go to Twitch chat print as `[BOT→chat]`.

```
mock> alice: !join
[BOT→chat] Alice has entered the tavern! Type !inventory to see your items.
mock> !inventory
mock> /sub alice          # simulate a subscription (same handler as real EventSub)
mock> /giftsub bob 5      # bob gifts 5 subs
mock> /bits carol 500     # carol cheers 500 bits
mock> /raid dan 20        # dan raids with 20 viewers
mock> /spam 25            # 25 rapid commands from fake users — exercises rate limiting
mock> /help
mock> /quit
```

Everything routes through the exact same command parser and EventSub handler paths as real traffic — only the Twitch transport is simulated. When it all works here, follow the sections below to connect a real channel.

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

## 1. Create a Twitch Bot Account (for chat)

1. Log in to [twitch.tv](https://twitch.tv) with a **separate bot account** (e.g. `MyTavernBot`). Using your main avoids spam-filter issues.
2. Go to [twitchapps.com/tmi](https://twitchapps.com/tmi/) while logged in as the bot.
3. Click **Connect** → copy the OAuth token (starts with `oauth:`).
4. Set `TWITCH_BOT_USERNAME` and `TWITCH_OAUTH_TOKEN` in your `.env`.

---

## 2. Get a User Access Token (for EventSub)

EventSub subscriptions for sub/bits require a **user access token** for the broadcaster's account (not the bot account) with the following scopes (raid notifications need no scope — the `channel.raid` EventSub topic works without authorization):

```
channel:read:subscriptions
bits:read
```

Steps:

1. Create an app at [dev.twitch.tv/console](https://dev.twitch.tv/console) → **Register Your Application**.
   - OAuth Redirect URLs: `http://localhost:3000` (for local token generation)
   - Category: Chat Bot
   - Copy **Client ID** and **Client Secret**.

2. Generate a user token using the [Twitch CLI](https://dev.twitch.tv/docs/cli) or the Token Generator:
   ```bash
   twitch token -u -s 'channel:read:subscriptions bits:read'
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

## 5. Customize Loot Tables / Rewards

**Preferred: in-app Rewards panel.** The DM configures sub / gift-sub / bits
rewards directly in the Stream panel ("Rewards — subs, gifts & bits"). Tables
are built from the viewer-power list (Pebble Toss, Healing Spark, Fireball,
Give Potion, …), persist per campaign on the server, and the bridge fetches
them at startup and re-applies them live whenever the DM saves changes — no
restart or JSON editing needed. Defaults ship pre-filled:

- **Single sub / resub** (and each gift recipient) → a "basic" support table
  (heals + potions).
- **Gift subs** → the *gifter* rolls from tiers by batch size (×1, ×5, ×10+ —
  the top tier includes every power).
- **Bits** → threshold tiers (100 / 500 / 1000+).

**Fallback: local JSON.** When the server has no rewards config (or the fetch
fails), the bridge falls back to `config/loot-tables.json` item tables. Each
entry has a `weight` (higher = more common):

```json
"sub": [
  { "item": "Potion of Healing", "weight": 60 },
  { "item": "Wand of Fireballs", "weight": 5 }
]
```

Bits tiers are configured as an array with `threshold` values — the highest threshold the bit amount meets is used. Raid loot always comes from the local `raid` table.

---

## 6. Add Custom Chat Commands

Edit `config/commands.json`. Supported actions: `join`, `leave`, `inventory`,
`target`, `use`, `help`, `vote`, `name`, `me`, `stats`, `bag`, `equip`, `shop`,
`buy`, `levelup`, `leaderboard`, `arena`. Map any command prefix to one of
these actions (multiple prefixes per action = aliases):

```json
{
  "!join":    "join",
  "!enter":   "join",
  "!inv":     "inventory",
  "!shoot":   "target"
}
```

---

## 7. Running the Bridge

### Automatic (default)

You normally don't need to start the bridge yourself. The game server
supervises it as a child process: as soon as a session has a connected Twitch
channel and "Chat bridge enabled" is on in the Stream panel, the server runs
`node src/index.js` in this folder, restarts it with backoff if it crashes,
and stops it when the bridge is toggled off, Twitch is disconnected, or the
server shuts down. The Stream panel shows the live status
(running / starting / failed / stopped), the last error, a recent-log view,
and a "Restart bridge" button.

The bridge still reads this folder's `.env` for its Twitch bot identity
(`TWITCH_BOT_USERNAME`, `TWITCH_OAUTH_TOKEN`, …), but `GAME_SERVER_WS_URL`,
`GAME_SESSION_ID`, and `CHAT_BRIDGE_TOKEN` are injected by the server, so
they never need to be synced by hand. Node.js 18+ must be installed and on
PATH (or set `CHAT_BRIDGE_NODE` in the server environment to the node
executable). Run `npm install` here once so the dependencies exist.

### Manual (development)

Running the bridge by hand still works — the server skips auto-spawning when
a bridge for the session is already connected:

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

| Command | Description |
|---|---|
| `!join` | Register as a chat participant. Idempotent. |
| `!leave` | Leave the session roster. |
| `!inventory` / `!inv` | See your campaign items, each with its exact trigger command. |
| `!target <name>` | Use your item on a token matching `<name>`. If you hold several items, the bot lists them and asks you to pick with `!use`. |
| `!use <item> <target>` | Use a specific held item — e.g. `!use fireball goblin`, `!use "healing spark" grognak`. Item names match by case-insensitive prefix against your own inventory; quote multi-word names. Heals and blessings can target party members too. |
| `!help` / `!commands` / `!command` | Compact command list with usage hints. |
| `!vote <n>` | Vote on the active DM poll. |
| `!name <name>` | Set your in-game character name. |
| `!me` / `!character` | Your character summary (items, sessions, damage, arena record). |
| `!stats` / `!sheet` | Full arena ability scores with item bonuses, HP/AC/ATK, XP, gold. |
| `!bag` | Your arena gear (separate from campaign `!inventory`). |
| `!equip <item>` | Equip arena gear into its slot (weapon / armor / trinket). |
| `!shop` | List arena shop items and prices. |
| `!buy <item>` | Buy arena gear with gold earned from duels. |
| `!levelup` | Level up when you have enough XP (+1 primary stat, +1 random stat). |
| `!leaderboard` | Top arena fighters. |
| `!duel <name>` / `!accept` / `!decline` | Chatter-vs-chatter arena duels. |

On first use of any arena command a character is rolled automatically: a
random class with 4d6-drop-lowest ability scores and 50 starting gold. Duel
wins/losses award XP and gold; class, level, and equipped gear feed into duel
HP / AC / attack rolls. Progress persists on the game server per campaign.

Unknown `!commands` from joined chatters get a single rate-limited pointer to
`!help`; commands from chatters who never joined are ignored so the bot won't
answer other bots' prefixes.

---

## DM Controls

In the game client (DM view), two new WebSocket message types are available:

- **`dm_chat_bridge_kill_switch`** `{ "paused": true/false }` — pause or resume all chat bridge interactions instantly.
- **`dm_chat_friendly_fire`** `{ "enabled": true/false }` — allow chat items to damage party (player-owned) tokens. Default off; heals, blessings, and gifts are never blocked. Also available as a checkbox in the Stream panel.
- **`dm_grant_chat_participant_item`** `{ "twitch_username": "...", "item_entry": { "name": "...", ... } }` — grant an item directly to a chat participant.

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

Tests cover the command parser (all commands, rate limiting, sanitization) and the loot roller (distribution, edge cases).
