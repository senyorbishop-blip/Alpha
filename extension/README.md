# Twitch Extension — Viewer Powers

A Twitch Extension that lets viewers **use Bits to trigger a specific, named
power** (Fireball, Chain Lightning, …) or — if subscribed — **claim a free sub
power** on a cooldown. The Extension's backend (EBS) is this app: it verifies the
Twitch transaction, then routes the power through the *same* grant path the DM
uses, so it lands in the viewer's in-game profile and animates on the map.

## Compliance (do not deviate)

Twitch's Bits-in-Extensions policy forbids exchanging Bits for random/unknown
outcomes (the loot-box rule):

- ✅ Bits always buy a **known** power the viewer selected — one SKU per power.
- ❌ No "spend Bits → random power" path exists anywhere.
- Randomness, if ever wanted, must be a **free** action (or Channel Points).
- UI copy uses **"use Bits to trigger"** — never "cheer", "donate", "purchase",
  or "spend".

---

## Part A — Register the Extension (Twitch Developer Console)

This Extension is **separate** from the "Sign in with Twitch" OAuth app, but
lives under the same developer account.

1. **Create the Extension** at <https://dev.twitch.tv/console/extensions>.
   Record the **Extension Client ID**, **Version**, and **Extension Secret
   (base64)** — the secret signs all Extension JWTs.
2. **Enable Bits Monetization** and complete the tax/identity onboarding
   (required before Bits work at all).
3. **Enable Identity Linking** — required so the EBS receives the real Twitch
   user id to match the local account's `twitch_id`.
4. **Enable Subscription Status** permission — for the sub-power path.
5. **Create Bits Products (SKUs)** — one per purchasable power, with Bit costs:
   - `power_fireball`, `power_chain_lightning`, `power_healing_spark`,
     `power_battle_blessing`, `power_pebble_toss`, `power_arcane_zap`,
     `power_meteor_pop`, `power_trip_hex`, `power_flash_freeze`,
     `power_goo_burst`, `power_smoke_burst`, `power_knockback`,
     `power_give_potion`, `power_give_random_item`, `power_magic_missile`,
     `power_shield`, `power_ray_of_frost`, `power_sleep`,
     `power_lightning_bolt`.
   - The SKU → power map lives in `server/twitch_ext/granting.py` (`SKU_TO_POWER`)
     and is mirrored in `extension/config.js` (`POWERS`). Keep them in sync.
6. **Asset hosting / Views**: set the viewer file to `viewer.html` and the
   config file to `config.html`. Build for **Panel** and **Component** (these
   reach mobile). **Overlay** is desktop-only and optional.
7. **Allowlist / CSP**: add your EBS HTTPS domain to the Extension's allowed
   URLs so the frontend can call it.

### Frontend config to edit

Set `EBS_BASE` at the top of **both** `extension/viewer.js` and
`extension/config.js` to your EBS (this app's) HTTPS domain.

### Environment variables (`.env`)

```
TWITCH_EXT_CLIENT_ID=        # Extension Client ID
TWITCH_EXT_SECRET=           # base64 extension secret from the console
TWITCH_EXT_OWNER_ID=         # your Twitch user id (extension owner)

# Optional — only for the subscriber "claim sub power" path (server-verifies
# sub status via Helix; refuses rather than trusting the client if unset):
TWITCH_EXT_CLIENT_SECRET=
TWITCH_EXT_SUB_POWERS=healing_spark,battle_blessing
TWITCH_EXT_SUB_COOLDOWN_SEC=900
```

If any of the three required vars is missing, the EBS endpoints return a clear
`extension_not_configured` response (HTTP 503) instead of crashing.

---

## Part B — Frontend (this directory)

| File          | Role                                                           |
|---------------|---------------------------------------------------------------|
| `viewer.html` / `viewer.js` / `style.css` | viewer-facing Panel/Component  |
| `config.html` / `config.js`               | broadcaster config (run once)  |

The viewer flow:

1. `onAuthorized` captures the Helper JWT, `channelId`, and `userId`. The JWT is
   sent on every EBS call as `Authorization: Bearer <token>`.
2. `requestIdShare()` runs once so the EBS gets the real Twitch user id. Power
   buttons are gated behind identity share; if declined, a "link your Twitch
   identity" prompt is shown.
3. The catalog is fetched from `GET /api/twitch/ext/catalog`; `bits.getProducts()`
   supplies live SKU prices; one button is rendered per power.
4. On click → `bits.useBits(sku)`. `onTransactionComplete` POSTs the signed
   receipt to `POST /api/twitch/ext/transaction`; `onTransactionCancelled`
   resets the button. Bit buttons are hidden when `features.isBitsEnabled` is
   false.
5. Subscribers (when `features.isSubscriptionStatusAvailable` and
   `viewer.subscriptionStatus` is active) see a "Claim sub power" button →
   `POST /api/twitch/ext/sub-claim`.

The broadcaster (DM) config page binds the channel to the live game `session_id`
and optionally selects which powers are purchasable, persisting via
`configuration.set('broadcaster', …)` **and** `POST /api/twitch/ext/bind-session`.

---

## Part C — EBS (`server/twitch_ext/`)

| Module           | Responsibility                                                    |
|------------------|-------------------------------------------------------------------|
| `config.py`      | env-driven config + `ext_configured()` gate                       |
| `jwt_verify.py`  | `verify_ext_jwt`, `verify_bits_receipt` (HS256 + `exp`)           |
| `granting.py`    | `SKU_TO_POWER`, dedupe store, `resolve_target`, `grant_known_power`|
| `helix.py`       | server-side subscription verification (App Access Token)           |
| `routes.py`      | the four endpoints, wired in `main.py`                             |

Endpoints (all under `/api/twitch/ext`, JWT-verified on every call):

- `POST /bind-session` — broadcaster JWT (`role == "broadcaster"`); stores
  `channel_id → session_id`.
- `GET /catalog` — viewer JWT; purchasable powers (name, sku, cooldown,
  approval) for the bound session, plus the sub-power list.
- `POST /transaction` — viewer JWT + verified Bits receipt; dedupes by
  `transaction_id`; `SKU_TO_POWER[sku] → power_id`; resolves target; grants.
  Unknown SKUs and replays are rejected.
- `POST /sub-claim` — viewer JWT; re-checks sub status via Helix; enforces a
  per-viewer cooldown; grants the chosen known power.

**Dedupe / binding storage:** a small SQLite database at
`<DATA_DIR>/twitch_ext.db` (`granting.DEDUPE_DB_PATH`), with tables
`processed_transactions`, `channel_bindings`, and `sub_claims`.

Powers whose `approval_default` is `True` (Fireball, Flash Freeze, Meteor Pop,
Goo Burst, Smoke Burst, Chain Lightning) keep `requires_approval = True` on
grant, so they still route through the DM's existing pending-approval gate before
hitting the map.

---

## Tooling / process

The old Developer Rig is retired. Develop with the Extension's **Local Test**
mode pointing at your local servers, then **Hosted Test**, then **submit for
review** before public release. Bits/monetization require the console onboarding
to be completed first. Overlay extensions are desktop-only; Panel/Component
reach mobile.
