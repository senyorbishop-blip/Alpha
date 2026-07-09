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

Edit `config/commands.json`. Currently supported actions are `join`, `leave`, `inventory`, `target`. Map any command prefix to one of these actions:

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
| `!inventory` | See your current items (bot replies with @mention). |
| `!target <name>` | Use your first available item on a token matching `<name>`. |

---

## DM Controls

In the game client (DM view), two new WebSocket message types are available:

- **`dm_chat_bridge_kill_switch`** `{ "paused": true/false }` — pause or resume all chat bridge interactions instantly.
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
