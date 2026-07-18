"""
server/twitch_ext/granting.py — map a verified Twitch transaction to a viewer power.

Responsibilities:
  * SKU_TO_POWER — one Bits SKU per KNOWN power (compliance: no random outcomes).
  * Persisted dedupe of processed transaction ids (reject replays/retries).
  * Persisted channel -> session binding (set by the broadcaster config page).
  * resolve_target() — turn (channel, twitch_user_id) into the local viewer.
  * grant_known_power() — reuse the existing viewer-power profile shape + grant
    path so the power lands exactly as a DM grant would (approval/cooldown kept).
  * In-memory per-viewer / per-session rate limiting (anti-grief).

The dedupe store, channel bindings, and sub-claim cooldowns live in a small
SQLite database at ``<DATA_DIR>/twitch_ext.db`` (see DEDUPE_DB_PATH).
"""
import time
import sqlite3
import threading
from collections import deque

from server.paths import DATA_DIR
from server.session import get_session
from server.auth.models import get_user_by_twitch_id
from server.http.auth import auth_player_key
from server.handlers.viewer_powers import (
    VIEWER_POWER_DEFS,
    _viewer_power_defs,
    _viewer_key_for_user,
    _get_or_create_viewer_profile,
    _broadcast_viewer_profiles,
)

# ── SKU -> power map (compliance: each SKU buys ONE specific, named power) ──────
# Only ids that exist in VIEWER_POWER_DEFS are valid; unknown SKUs are rejected.
SKU_TO_POWER = {
    "power_pebble_toss": "pebble_toss",
    "power_arcane_zap": "arcane_zap",
    "power_healing_spark": "healing_spark",
    "power_battle_blessing": "battle_blessing",
    "power_fireball": "fireball",
    "power_meteor_pop": "meteor_pop",
    "power_trip_hex": "trip_hex",
    "power_flash_freeze": "flash_freeze",
    "power_goo_burst": "goo_burst",
    "power_smoke_burst": "smoke_burst",
    "power_knockback": "knockback",
    "power_give_potion": "give_potion",
    "power_chain_lightning": "chain_lightning",
    "power_give_random_item": "give_random_item",
    "power_magic_missile": "magic_missile",
    "power_shield": "shield",
    "power_ray_of_frost": "ray_of_frost",
    "power_sleep": "sleep",
    "power_lightning_bolt": "lightning_bolt",
}
# Defensive: drop any mapping whose target is not a real base power.
SKU_TO_POWER = {sku: pid for sku, pid in SKU_TO_POWER.items() if pid in VIEWER_POWER_DEFS}

DEDUPE_DB_PATH = DATA_DIR / "twitch_ext.db"

_db_lock = threading.Lock()
_db_ready = False


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DEDUPE_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    with _db_lock:
        if _db_ready:
            return
        try:
            DEDUPE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        with _conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    sku            TEXT NOT NULL DEFAULT '',
                    viewer_key     TEXT NOT NULL DEFAULT '',
                    session_id     TEXT NOT NULL DEFAULT '',
                    created_at     REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_id  TEXT PRIMARY KEY,
                    session_id  TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    updated_at  REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sub_claims (
                    channel_id     TEXT NOT NULL,
                    twitch_user_id TEXT NOT NULL,
                    last_claim_at  REAL NOT NULL,
                    PRIMARY KEY (channel_id, twitch_user_id)
                );
                """
            )
        _db_ready = True


# ── Channel <-> session binding ────────────────────────────────────────────────

def bind_session(channel_id: str, session_id: str, config: dict | None = None) -> None:
    import json
    _ensure_db()
    cid = str(channel_id or "").strip()
    sid = str(session_id or "").strip()
    if not cid or not sid:
        raise ValueError("channel_id and session_id are required")
    cfg = json.dumps(config or {}, separators=(",", ":"))[:20000]
    with _conn() as conn:
        conn.execute(
            """INSERT INTO channel_bindings (channel_id, session_id, config_json, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   session_id=excluded.session_id,
                   config_json=excluded.config_json,
                   updated_at=excluded.updated_at""",
            (cid, sid, cfg, time.time()),
        )


def lookup_bound_session(channel_id: str) -> str:
    _ensure_db()
    cid = str(channel_id or "").strip()
    if not cid:
        return ""
    with _conn() as conn:
        row = conn.execute(
            "SELECT session_id FROM channel_bindings WHERE channel_id=?", (cid,)
        ).fetchone()
    return str(row["session_id"]) if row else ""


def lookup_binding_config(channel_id: str) -> dict:
    import json
    _ensure_db()
    cid = str(channel_id or "").strip()
    if not cid:
        return {}
    with _conn() as conn:
        row = conn.execute(
            "SELECT config_json FROM channel_bindings WHERE channel_id=?", (cid,)
        ).fetchone()
    if not row:
        return {}
    try:
        return dict(json.loads(row["config_json"] or "{}"))
    except Exception:
        return {}


# ── Transaction dedupe ─────────────────────────────────────────────────────────

def is_transaction_processed(transaction_id: str) -> bool:
    _ensure_db()
    tid = str(transaction_id or "").strip()
    if not tid:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_transactions WHERE transaction_id=?", (tid,)
        ).fetchone()
    return bool(row)


def mark_transaction_processed(transaction_id: str, *, sku: str = "", viewer_key: str = "", session_id: str = "") -> bool:
    """Atomically record a transaction id. Returns False if it was already present
    (i.e. a replay), True if this call was the one that recorded it."""
    _ensure_db()
    tid = str(transaction_id or "").strip()
    if not tid:
        return False
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO processed_transactions (transaction_id, sku, viewer_key, session_id, created_at) VALUES (?,?,?,?,?)",
                (tid, str(sku or "")[:80], str(viewer_key or "")[:80], str(session_id or "")[:80], time.time()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


# ── Sub-claim cooldown ─────────────────────────────────────────────────────────

def sub_claim_remaining(channel_id: str, twitch_user_id: str, cooldown_sec: int) -> int:
    _ensure_db()
    cid = str(channel_id or "").strip()
    uid = str(twitch_user_id or "").strip()
    if not cid or not uid or cooldown_sec <= 0:
        return 0
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_claim_at FROM sub_claims WHERE channel_id=? AND twitch_user_id=?",
            (cid, uid),
        ).fetchone()
    if not row:
        return 0
    elapsed = time.time() - float(row["last_claim_at"] or 0.0)
    return max(0, int(round(cooldown_sec - elapsed)))


def record_sub_claim(channel_id: str, twitch_user_id: str) -> None:
    _ensure_db()
    cid = str(channel_id or "").strip()
    uid = str(twitch_user_id or "").strip()
    if not cid or not uid:
        return
    with _conn() as conn:
        conn.execute(
            """INSERT INTO sub_claims (channel_id, twitch_user_id, last_claim_at)
               VALUES (?,?,?)
               ON CONFLICT(channel_id, twitch_user_id) DO UPDATE SET last_claim_at=excluded.last_claim_at""",
            (cid, uid, time.time()),
        )


# ── In-memory rate limiting (anti-grief; best-effort) ──────────────────────────
# Caps how many powers can be granted per minute, per viewer and per session, so
# a burst of Bits transactions cannot flood the map.
_PER_VIEWER_MAX = 6
_PER_SESSION_MAX = 30
_WINDOW_SEC = 60.0
_viewer_hits: dict[str, deque] = {}
_session_hits: dict[str, deque] = {}
_rate_lock = threading.Lock()


def _prune(dq: deque, now: float) -> None:
    while dq and (now - dq[0]) > _WINDOW_SEC:
        dq.popleft()


def check_rate_limit(session_id: str, viewer_key: str) -> tuple[bool, str]:
    """Return (allowed, reason). Records a hit when allowed."""
    now = time.time()
    sid = str(session_id or "")
    vkey = f"{sid}:{viewer_key}"
    with _rate_lock:
        vdq = _viewer_hits.setdefault(vkey, deque())
        sdq = _session_hits.setdefault(sid, deque())
        _prune(vdq, now)
        _prune(sdq, now)
        if len(vdq) >= _PER_VIEWER_MAX:
            return False, "Too many powers triggered just now — slow down a moment."
        if len(sdq) >= _PER_SESSION_MAX:
            return False, "This stream is receiving a lot of powers right now — try again shortly."
        vdq.append(now)
        sdq.append(now)
    return True, ""


# ── Synthetic viewer (for grants to a not-yet-connected account) ───────────────

class _SyntheticViewer:
    """Minimal User-like object carrying the identity the viewer-power helpers need.

    Used when the local account exists (linked via twitch_id) but is not currently
    connected to the game session. The ``player_key`` mirrors what an authenticated
    viewer gets on connect, so the profile is keyed identically and is waiting for
    them when they next join.
    """
    __slots__ = ("id", "name", "role", "player_key", "connected")

    def __init__(self, user_id: str, name: str, player_key: str):
        self.id = user_id
        self.name = name
        self.role = "viewer"
        self.player_key = player_key
        self.connected = False


def _connected_viewer_for_local_user(session, local_user_id: str):
    """Find a connected viewer in the session that maps to this local account."""
    target_key = auth_player_key(local_user_id)
    users = getattr(session, "users", {}) or {}
    # Fast path: direct id hit.
    direct = users.get(local_user_id)
    if direct is not None and str(getattr(direct, "role", "")).lower() == "viewer":
        return direct
    for u in users.values():
        if str(getattr(u, "role", "")).lower() != "viewer":
            continue
        pk = str(getattr(u, "player_key", "") or "").strip()
        if pk and pk == target_key:
            return u
        if str(getattr(u, "id", "") or "").strip() == str(local_user_id):
            return u
    return None


def resolve_target(channel_id: str, twitch_user_id: str):
    """Resolve (session, viewer_key, user) for a verified Twitch viewer.

    Returns (session, viewer_key, user) on success. ``user`` is either the
    connected viewer User or a _SyntheticViewer for a linked-but-offline account.
    Raises ResolveError with a viewer-facing message on any failure.
    """
    session_id = lookup_bound_session(channel_id)
    if not session_id:
        raise ResolveError("This channel isn't bound to a live game yet. Ask the streamer to set it up.")
    session = get_session(session_id)
    if session is None:
        raise ResolveError("The bound game session is no longer live.")
    local_user = get_user_by_twitch_id(twitch_user_id)
    if not local_user:
        raise ResolveError("Link your Twitch identity to a game account to use powers.")
    local_user_id = str(local_user.get("id") or "").strip()
    display_name = str(local_user.get("character_name") or local_user.get("username") or "Viewer").strip()[:40] or "Viewer"

    connected = _connected_viewer_for_local_user(session, local_user_id)
    if connected is not None:
        return session, _viewer_key_for_user(connected), connected

    # Not connected: grant to a profile keyed off their identity so it's waiting
    # when they next join (they log in with Twitch, so twitch_id -> account holds).
    synthetic = _SyntheticViewer(local_user_id, display_name, auth_player_key(local_user_id))
    return session, _viewer_key_for_user(synthetic), synthetic


class ResolveError(Exception):
    """Raised when a Twitch viewer cannot be mapped to a grantable local viewer."""


# ── Grant ──────────────────────────────────────────────────────────────────────

async def grant_known_power(session, viewer_key: str, user, power_id: str) -> dict:
    """Grant one charge of a KNOWN power, reusing the viewer-power profile shape.

    Mirrors the DM grant path: keeps ``requires_approval`` = the power's
    ``approval_default`` and ``cooldown_sec`` from the def, so approval-gated
    powers still route through the existing pending-approval gate before the map.
    """
    defs = _viewer_power_defs(session)
    if power_id not in defs:
        raise ResolveError("That power is not available.")
    power_def = defs[power_id]

    profiles, profile, key = _get_or_create_viewer_profile(session, user)
    powers = dict(profile.get("powers") or {})
    existing = powers.get(power_id) if isinstance(powers.get(power_id), dict) else {}
    existing_charges = int(existing.get("charges", 0) or 0)
    powers[power_id] = {
        "power_id": power_id,
        "charges": max(1, min(999, existing_charges + 1)),
        "enabled": True,
        "requires_approval": bool(power_def.get("approval_default", False)),
        "cooldown_sec": max(0, min(86400, int(power_def.get("cooldown_sec", 0) or 0))),
        # Preserve any active cooldown the viewer already has on this power.
        "cooldown_until": float(existing.get("cooldown_until", 0.0) or 0.0),
    }
    profile["powers"] = powers
    profiles[key] = profile
    session.viewer_profiles = profiles

    await _broadcast_viewer_profiles(session)
    # Persist via the same async-safe path the DM grant flow uses.
    from server.db import save_campaign_async
    await save_campaign_async(session)

    return {
        "power_id": power_id,
        "power_name": str(power_def.get("name") or power_id),
        "charges": powers[power_id]["charges"],
        "requires_approval": powers[power_id]["requires_approval"],
        "cooldown_sec": powers[power_id]["cooldown_sec"],
        "viewer_key": key,
    }
