"""
server/auth/models.py — User model + DB helpers for the auth system.

Tables managed here:
  users           — registered accounts
  reset_backlog   — host-manual password-reset requests
"""
import sqlite3
import time
import secrets
import hashlib
from pathlib import Path
from typing import Optional

from server.paths import DB_PATH
from server.paths import REPO_ROOT


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_auth_db() -> None:
    """Create auth tables and apply any pending column migrations."""
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id              TEXT PRIMARY KEY,
            username        TEXT NOT NULL UNIQUE,
            email           TEXT NOT NULL UNIQUE,
            password_hash   TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'player',
            character_name  TEXT,
            created_at      REAL NOT NULL,
            last_login      REAL,
            reset_backlog   TEXT
        );

        CREATE TABLE IF NOT EXISTS reset_backlog (
            id              TEXT PRIMARY KEY,
            username        TEXT NOT NULL,
            email           TEXT NOT NULL,
            requested_at    REAL NOT NULL,
            resolved        INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id                  TEXT PRIMARY KEY,
            user_id             TEXT NOT NULL,
            username_snapshot   TEXT NOT NULL,
            reset_token_hash    TEXT NOT NULL UNIQUE,
            expires_at          REAL NOT NULL,
            used_at             REAL,
            created_at          REAL NOT NULL,
            request_ip          TEXT,
            request_user_agent  TEXT,
            delivery_method     TEXT NOT NULL DEFAULT 'dev_in_app',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_assets (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            filename    TEXT NOT NULL,
            url         TEXT NOT NULL,
            asset_type  TEXT NOT NULL DEFAULT 'token',
            created_at  REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_powers (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            damage_dice TEXT NOT NULL DEFAULT '',
            tags        TEXT NOT NULL DEFAULT '[]',
            created_at  REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_inventory (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            item_name   TEXT NOT NULL,
            quantity    INTEGER NOT NULL DEFAULT 1,
            weight      REAL NOT NULL DEFAULT 0,
            notes       TEXT NOT NULL DEFAULT '',
            created_at  REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_stats (
            user_id     TEXT PRIMARY KEY,
            str         INTEGER NOT NULL DEFAULT 10,
            dex         INTEGER NOT NULL DEFAULT 10,
            con         INTEGER NOT NULL DEFAULT 10,
            int_        INTEGER NOT NULL DEFAULT 10,
            wis         INTEGER NOT NULL DEFAULT 10,
            cha         INTEGER NOT NULL DEFAULT 10,
            hp          INTEGER NOT NULL DEFAULT 10,
            max_hp      INTEGER NOT NULL DEFAULT 10,
            ac          INTEGER NOT NULL DEFAULT 10,
            speed       INTEGER NOT NULL DEFAULT 30,
            updated_at  REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_followed_campaigns (
            user_id     TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            followed_at REAL NOT NULL,
            PRIMARY KEY (user_id, campaign_id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS user_entitlements (
            user_id              TEXT PRIMARY KEY,
            plan_code            TEXT NOT NULL DEFAULT 'community',
            subscription_status  TEXT NOT NULL DEFAULT 'inactive',
            subscription_provider TEXT NOT NULL DEFAULT 'manual',
            subscription_ref     TEXT NOT NULL DEFAULT '',
            support_tier         TEXT NOT NULL DEFAULT '',
            feature_overrides    TEXT NOT NULL DEFAULT '{}',
            effective_at         REAL,
            expires_at           REAL,
            updated_at           REAL NOT NULL,
            updated_by           TEXT NOT NULL DEFAULT 'system',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        """)
        # Column migrations (added if absent). Includes ``twitch_id`` on users —
        # the identity link the "Sign in with Twitch" flow populates and that the
        # Twitch Extension EBS uses to map a Twitch viewer to a local account.
        for migration in [
            "ALTER TABLE campaigns ADD COLUMN owner_user_id TEXT",
            "ALTER TABLE campaigns ADD COLUMN owner_username TEXT",
            "ALTER TABLE campaigns ADD COLUMN claimed_at REAL",
            "ALTER TABLE campaigns ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'",
            "ALTER TABLE users ADD COLUMN twitch_id TEXT",
        ]:
            try:
                conn.execute(migration)
                conn.commit()
            except Exception:
                pass
        # Best-effort uniqueness/lookup index for the Twitch identity link.
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_twitch_id ON users(twitch_id) WHERE twitch_id IS NOT NULL")
            conn.commit()
        except Exception:
            pass


def merge_legacy_users_from_db(legacy_db_path: Optional[str] = None) -> dict:
    """Best-effort migration of legacy auth users from a repo-local database into
    the current persistent auth database.

    This protects older installs where accounts were created against a snapshot-local
    campaigns.db before the app switched to the shared persistent data directory.
    Missing users are inserted. Existing users are preserved as-is.
    """
    report = {
        "checked": False,
        "legacy_db": "",
        "found": False,
        "copied": 0,
        "skipped": 0,
        "errors": [],
    }
    current_db = Path(DB_PATH).expanduser().resolve()
    legacy_db = Path(legacy_db_path).expanduser() if legacy_db_path else (REPO_ROOT / 'campaigns.db')
    legacy_db = legacy_db.resolve()
    report['checked'] = True
    report['legacy_db'] = str(legacy_db)
    if not legacy_db.exists() or legacy_db == current_db:
        return report

    report['found'] = True
    try:
        legacy = sqlite3.connect(str(legacy_db))
        legacy.row_factory = sqlite3.Row
        current = get_conn()
        try:
            legacy_tables = {r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if 'users' not in legacy_tables:
                return report
            rows = legacy.execute(
                "SELECT id, username, email, password_hash, role, character_name, created_at, last_login, reset_backlog FROM users"
            ).fetchall()
            for row in rows:
                payload = dict(row)
                username = str(payload.get('username') or '').strip()
                email = str(payload.get('email') or '').strip().lower()
                if not username or not email or not payload.get('password_hash'):
                    report['skipped'] += 1
                    continue
                existing = current.execute(
                    "SELECT id FROM users WHERE LOWER(username)=LOWER(?) OR LOWER(email)=LOWER(?)",
                    (username, email),
                ).fetchone()
                if existing:
                    report['skipped'] += 1
                    continue
                current.execute(
                    "INSERT INTO users (id, username, email, password_hash, role, character_name, created_at, last_login, reset_backlog) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        payload.get('id'),
                        username,
                        email,
                        payload.get('password_hash'),
                        (payload.get('role') or 'player'),
                        payload.get('character_name'),
                        payload.get('created_at') or time.time(),
                        payload.get('last_login'),
                        payload.get('reset_backlog'),
                    ),
                )
                report['copied'] += 1
            current.commit()
        finally:
            try:
                legacy.close()
            except Exception:
                pass
            try:
                current.close()
            except Exception:
                pass
    except Exception as exc:
        report['errors'].append(str(exc))
    return report


# ── User CRUD ────────────────────────────────────────────────────────────────

def create_user(username: str, email: str, password_hash: str, role: str) -> dict:
    uid = secrets.token_hex(16)
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role, created_at) VALUES (?,?,?,?,?,?)",
            (uid, username, email, password_hash, role, now),
        )
    return {"id": uid, "username": username, "email": email, "role": role, "created_at": now}


def get_user_by_id(user_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(email)=LOWER(?)", (email,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_twitch_id(twitch_id: str) -> Optional[dict]:
    """Return the local account linked to a Twitch user id, or None.

    Defensive against installs where the ``twitch_id`` column has not been
    migrated yet (the "Sign in with Twitch" flow owns populating it): a missing
    column degrades to "no match" instead of raising.
    """
    tid = str(twitch_id or "").strip()
    if not tid:
        return None
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE twitch_id=?", (tid,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def set_user_twitch_id(user_id: str, twitch_id: str) -> bool:
    """Link a local account to a Twitch user id. Best-effort; returns success."""
    uid = str(user_id or "").strip()
    tid = str(twitch_id or "").strip()
    if not uid or not tid:
        return False
    try:
        with get_conn() as conn:
            conn.execute("UPDATE users SET twitch_id=? WHERE id=?", (tid, uid))
        return True
    except sqlite3.OperationalError:
        return False


def get_user_by_username_or_email(identifier: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(?) OR LOWER(email)=LOWER(?)",
            (identifier, identifier),
        ).fetchone()
    return dict(row) if row else None


def update_last_login(user_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user_id))


def update_password(user_id: str, password_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))


def safe_user(row: dict) -> dict:
    """Strip sensitive fields before returning to the client."""
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "character_name": row.get("character_name"),
        "created_at": row["created_at"],
        "last_login": row.get("last_login"),
    }


# ── Reset backlog ─────────────────────────────────────────────────────────────

def add_reset_request(username: str, email: str) -> str:
    rid = secrets.token_hex(8)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO reset_backlog (id, username, email, requested_at, resolved) VALUES (?,?,?,?,0)",
            (rid, username, email, time.time()),
        )
    return rid


def list_reset_requests(resolved: bool = False) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reset_backlog WHERE resolved=? ORDER BY requested_at DESC",
            (1 if resolved else 0,),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_reset_request(username: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE reset_backlog SET resolved=1 WHERE LOWER(username)=LOWER(?)", (username,)
        )


# ── Self-service password reset tokens ───────────────────────────────────────

def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_password_reset_token(
    user_id: str,
    username: str,
    *,
    ttl_seconds: int,
    request_ip: str = "",
    request_user_agent: str = "",
    delivery_method: str = "dev_in_app",
) -> str:
    token = secrets.token_urlsafe(18)
    token_hash = _hash_reset_token(token)
    now = time.time()
    expires_at = now + max(60, int(ttl_seconds))
    token_id = secrets.token_hex(16)
    with get_conn() as conn:
        # Keep only one active token per user to reduce attack surface.
        conn.execute(
            "UPDATE password_reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL",
            (now, user_id),
        )
        conn.execute(
            """
            INSERT INTO password_reset_tokens
                (id, user_id, username_snapshot, reset_token_hash, expires_at, used_at, created_at, request_ip, request_user_agent, delivery_method)
            VALUES
                (?,?,?,?,?,NULL,?,?,?,?)
            """,
            (
                token_id,
                user_id,
                username,
                token_hash,
                expires_at,
                now,
                request_ip[:128],
                request_user_agent[:512],
                delivery_method[:32] or "dev_in_app",
            ),
        )
    return token


def consume_password_reset_token(user_id: str, token: str) -> bool:
    now = time.time()
    token_hash = _hash_reset_token(token)
    with get_conn() as conn:
        result = conn.execute(
            """
            UPDATE password_reset_tokens
               SET used_at=?
             WHERE user_id=?
               AND reset_token_hash=?
               AND used_at IS NULL
               AND expires_at > ?
            """,
            (now, user_id, token_hash, now),
        )
        return result.rowcount > 0


def prune_password_reset_tokens() -> None:
    cutoff = time.time() - (60 * 60 * 24 * 7)  # keep recent history for 7 days
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM password_reset_tokens WHERE used_at IS NOT NULL OR expires_at < ? OR created_at < ?",
            (time.time(), cutoff),
        )


# ── Legacy migration helpers ──────────────────────────────────────────────────

def find_legacy_campaigns(username: str) -> list:
    """Return all campaigns whose dm_name matches username (case-insensitive)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name FROM campaigns WHERE LOWER(dm_name)=LOWER(?)", (username,)
        ).fetchall()
    return [dict(r) for r in rows]


def migrate_campaigns_to_user(username: str, user_id: str) -> int:
    """Assign unclaimed campaigns matching dm_name to the new user account."""
    with get_conn() as conn:
        result = conn.execute(
            """UPDATE campaigns
               SET owner_user_id=?, owner_username=?, claimed_at=?, visibility='private'
               WHERE LOWER(dm_name)=LOWER(?) AND owner_user_id IS NULL""",
            (user_id, username, time.time(), username),
        )
        return result.rowcount


# ── Campaign ownership queries ────────────────────────────────────────────────

def list_my_campaigns(user_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, dm_name, created_at, updated_at, owner_username, claimed_at, visibility "
            "FROM campaigns WHERE owner_user_id=? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_unclaimed_campaigns() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, dm_name, created_at, updated_at "
            "FROM campaigns WHERE owner_user_id IS NULL ORDER BY updated_at DESC",
        ).fetchall()
    return [dict(r) for r in rows]


def claim_campaign(campaign_id: str, user_id: str, username: str) -> bool:
    """Atomically claim a campaign. Returns True on success, False if already claimed."""
    with get_conn() as conn:
        result = conn.execute(
            """UPDATE campaigns
               SET owner_user_id=?, owner_username=?, claimed_at=?, visibility='private'
               WHERE id=? AND owner_user_id IS NULL""",
            (user_id, username, time.time(), campaign_id),
        )
        return result.rowcount > 0


def get_campaign_owner_user_id(campaign_id: str) -> Optional[str]:
    """Return the owning account's user id for a campaign, or None if unclaimed."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
    if not row:
        return None
    owner = row["owner_user_id"]
    return str(owner) if owner else None


# ── User stats ────────────────────────────────────────────────────────────────

def get_user_stats(user_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM user_stats WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def upsert_user_stats(user_id: str, stats: dict) -> None:
    # Map client-facing key names to their corresponding DB column names.
    # Both sets are fully static — no user-supplied column names are ever
    # interpolated into SQL.
    _COL_MAP = {
        "str": "str", "dex": "dex", "con": "con",
        "int_": "int_", "wis": "wis", "cha": "cha",
        "hp": "hp", "max_hp": "max_hp", "ac": "ac", "speed": "speed",
    }
    safe_vals: dict = {}
    for client_key, col in _COL_MAP.items():
        if client_key in stats:
            try:
                safe_vals[col] = int(stats[client_key])
            except (TypeError, ValueError):
                pass
    if not safe_vals:
        return
    # Columns are sourced entirely from the static _COL_MAP above, not from
    # user input, so f-string interpolation here is safe.
    with get_conn() as conn:
        existing = conn.execute("SELECT user_id FROM user_stats WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            set_clause = ", ".join(f"{col}=?" for col in safe_vals)
            conn.execute(
                f"UPDATE user_stats SET {set_clause}, updated_at=? WHERE user_id=?",  # noqa: S608
                list(safe_vals.values()) + [time.time(), user_id],
            )
        else:
            col_names = ["user_id"] + list(safe_vals.keys()) + ["updated_at"]
            placeholders = ", ".join("?" * len(col_names))
            conn.execute(
                f"INSERT INTO user_stats ({', '.join(col_names)}) VALUES ({placeholders})",  # noqa: S608
                [user_id] + list(safe_vals.values()) + [time.time()],
            )


# ── User assets ───────────────────────────────────────────────────────────────

def add_user_asset(user_id: str, filename: str, url: str, asset_type: str = "token") -> dict:
    aid = secrets.token_hex(12)
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO user_assets (id, user_id, filename, url, asset_type, created_at) VALUES (?,?,?,?,?,?)",
            (aid, user_id, filename, url, asset_type, now),
        )
    return {"asset_id": aid, "url": url, "filename": filename, "asset_type": asset_type}


def list_user_assets(user_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM user_assets WHERE user_id=? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── User commercial entitlements ─────────────────────────────────────────────

def get_user_entitlement(user_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM user_entitlements WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    record = dict(row)
    try:
        import json

        record["feature_overrides"] = json.loads(record.get("feature_overrides") or "{}")
    except Exception:
        record["feature_overrides"] = {}
    return record


def upsert_user_entitlement(
    user_id: str,
    *,
    plan_code: str,
    subscription_status: str = "active",
    subscription_provider: str = "manual",
    subscription_ref: str = "",
    support_tier: str = "",
    feature_overrides: Optional[dict] = None,
    effective_at: Optional[float] = None,
    expires_at: Optional[float] = None,
    updated_by: str = "admin",
) -> None:
    import json

    now = time.time()
    overrides_json = json.dumps(feature_overrides or {}, separators=(",", ":"))
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_entitlements (
                    user_id, plan_code, subscription_status, subscription_provider, subscription_ref,
                    support_tier, feature_overrides, effective_at, expires_at, updated_at, updated_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    plan_code=excluded.plan_code,
                    subscription_status=excluded.subscription_status,
                    subscription_provider=excluded.subscription_provider,
                    subscription_ref=excluded.subscription_ref,
                    support_tier=excluded.support_tier,
                    feature_overrides=excluded.feature_overrides,
                    effective_at=excluded.effective_at,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by
            """,
            (
                user_id,
                (plan_code or "community").strip().lower(),
                (subscription_status or "inactive").strip().lower(),
                (subscription_provider or "manual").strip().lower(),
                str(subscription_ref or "")[:180],
                str(support_tier or "")[:32],
                overrides_json,
                effective_at,
                expires_at,
                now,
                str(updated_by or "admin")[:64],
            ),
        )
