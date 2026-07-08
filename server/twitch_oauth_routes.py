"""
server/twitch_oauth_routes.py — DM in-app Twitch OAuth connection.

Routes:
  GET  /api/twitch/connect/{session_id}    — build Twitch OAuth URL, redirect DM
  GET  /api/twitch/callback                — exchange code, store encrypted tokens
  GET  /api/twitch/status/{session_id}     — return {connected, channel, enabled}
  POST /api/twitch/disconnect/{session_id} — revoke token, clear DB columns
  POST /api/twitch/toggle/{session_id}     — toggle twitch_chat_enabled

All endpoints except /callback verify the caller is the session DM (by
dm_id or by owning the campaign via owner_user_id) via JWT.
The /callback endpoint verifies an HMAC-signed state parameter generated at
connect-time.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from server.db import get_conn
from server.http.auth import is_session_dm
from server.session import get_session
from server.token_enc import encrypt, decrypt

router = APIRouter()
logger = logging.getLogger(__name__)

_TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
_TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_TWITCH_REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"
_TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"

_OAUTH_SCOPES = "chat:read chat:edit channel:read:subscriptions bits:read channel:read:raids"

# CSRF state expiry
_STATE_TTL_SECONDS = 600


def _client_id() -> str:
    return os.environ.get("TWITCH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("TWITCH_CLIENT_SECRET", "").strip()


def _state_signing_key() -> bytes:
    """Derive a per-process HMAC key from JWT secret so state tokens survive restarts."""
    jwt_secret = os.environ.get("DND_JWT_SECRET", "insecure-default").encode()
    return hashlib.sha256(b"twitch_oauth_state:" + jwt_secret).digest()


def _make_state(session_id: str) -> str:
    ts = int(time.time())
    payload = f"{session_id}:{ts}"
    sig = hmac.new(_state_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.quote(f"{payload}:{sig}", safe="")


def _verify_state(state_param: str) -> str | None:
    """Return the session_id if the state is valid and fresh, else None."""
    try:
        raw = urllib.parse.unquote(state_param)
        parts = raw.rsplit(":", 1)
        if len(parts) != 2:
            return None
        payload, sig = parts
        expected = hmac.new(_state_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        session_id, ts_str = payload.rsplit(":", 1)
        if int(time.time()) - int(ts_str) > _STATE_TTL_SECONDS:
            return None
        return session_id
    except Exception:
        return None


def _public_base_url(request: Request) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def _resolve_dm(request: Request, session_id: str) -> bool:
    """Return True if the request JWT belongs to the DM (or owner account) of this session."""
    return is_session_dm(request, session_id)


def _get_twitch_row(session_id: str) -> dict | None:
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT twitch_channel, twitch_channel_id, twitch_access_token_enc, "
                "twitch_refresh_token_enc, twitch_token_expires_at, twitch_chat_enabled "
                "FROM campaigns WHERE id=?",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None
    except Exception as exc:
        logger.error("[twitch_oauth] _get_twitch_row error: %s", exc)
        return None


def _save_twitch_tokens(
    session_id: str,
    channel: str,
    channel_id: str,
    access_token: str,
    refresh_token: str,
    expires_at: float,
) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET "
            "twitch_channel=?, twitch_channel_id=?, "
            "twitch_access_token_enc=?, twitch_refresh_token_enc=?, "
            "twitch_token_expires_at=?, twitch_chat_enabled=1 "
            "WHERE id=?",
            (
                channel,
                channel_id,
                encrypt(access_token),
                encrypt(refresh_token),
                expires_at,
                session_id,
            ),
        )
        conn.commit()


def _clear_twitch_tokens(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET "
            "twitch_channel='', twitch_channel_id='', "
            "twitch_access_token_enc='', twitch_refresh_token_enc='', "
            "twitch_token_expires_at=0, twitch_chat_enabled=0 "
            "WHERE id=?",
            (session_id,),
        )
        conn.commit()


async def _exchange_code(code: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(_TWITCH_TOKEN_URL, data={
            "client_id":     _client_id(),
            "client_secret": _client_secret(),
            "code":          code,
            "grant_type":    "authorization_code",
            "redirect_uri":  redirect_uri,
        })
        resp.raise_for_status()
        return resp.json()


async def _refresh_access_token(refresh_token: str) -> dict | None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(_TWITCH_TOKEN_URL, data={
                "client_id":     _client_id(),
                "client_secret": _client_secret(),
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
            })
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.error("[twitch_oauth] token refresh failed: %s", exc)
        return None


async def _get_user_info(access_token: str) -> dict | None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _TWITCH_USERS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id": _client_id(),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [None])[0]
    except Exception as exc:
        logger.error("[twitch_oauth] _get_user_info failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/twitch/connect/{session_id}")
async def twitch_connect(session_id: str, request: Request):
    """Initiate the Twitch OAuth flow for the DM of this session."""
    if not _client_id() or not _client_secret():
        return JSONResponse(status_code=503, content={
            "error": "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set to use in-app Twitch connection."
        })

    if not _resolve_dm(request, session_id):
        return JSONResponse(status_code=403, content={"error": "Only the session DM can connect a Twitch channel."})

    redirect_uri = f"{_public_base_url(request)}/api/twitch/callback"
    state = _make_state(session_id)

    params = urllib.parse.urlencode({
        "client_id":     _client_id(),
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         _OAUTH_SCOPES,
        "state":         state,
        "force_verify":  "true",
    })
    return RedirectResponse(url=f"{_TWITCH_AUTH_URL}?{params}")


@router.get("/api/twitch/callback")
async def twitch_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Receive the OAuth callback from Twitch, exchange code for tokens."""
    if error:
        logger.warning("[twitch_oauth] OAuth error from Twitch: %s", error)
        return RedirectResponse(url="/?twitch_error=denied")

    session_id = _verify_state(state)
    if not session_id:
        return JSONResponse(status_code=400, content={"error": "Invalid or expired OAuth state."})

    if not _client_id() or not _client_secret():
        return JSONResponse(status_code=503, content={"error": "Twitch app credentials not configured."})

    redirect_uri = f"{_public_base_url(request)}/api/twitch/callback"

    try:
        token_data = await _exchange_code(code, redirect_uri)
    except Exception as exc:
        logger.error("[twitch_oauth] code exchange failed: %s", exc)
        return JSONResponse(status_code=502, content={"error": "Token exchange with Twitch failed."})

    access_token  = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in    = int(token_data.get("expires_in") or 14400)
    expires_at    = time.time() + expires_in

    user_info = await _get_user_info(access_token)
    if not user_info:
        return JSONResponse(status_code=502, content={"error": "Could not fetch Twitch user info."})

    channel    = user_info.get("login", "")
    channel_id = user_info.get("id", "")

    _save_twitch_tokens(session_id, channel, channel_id, access_token, refresh_token, expires_at)
    logger.info("[twitch_oauth] connected channel=%s session=%s", channel, session_id)

    # Redirect DM back to the play page
    play_url = f"/play/{session_id}?twitch_connected=1"
    return RedirectResponse(url=play_url)


@router.get("/api/twitch/status/{session_id}")
async def twitch_status(session_id: str, request: Request):
    """Return the Twitch connection status for this session (DM only)."""
    if not _resolve_dm(request, session_id):
        return JSONResponse(status_code=403, content={"error": "Forbidden."})

    row = _get_twitch_row(session_id)
    session = get_session(session_id)
    persistence_mode = str(getattr(session, "chat_persistence_mode", "everything") or "everything")

    # Whether the chat bridge service currently holds a live WebSocket to this
    # session — tells the DM instantly whether !join etc. will work.
    from server.connections import manager
    bridge_connected = manager.is_connected(session_id, "chat_bridge")

    if not row or not row.get("twitch_channel"):
        return JSONResponse(content={
            "connected": False, "channel": None, "enabled": True,
            "persistence_mode": persistence_mode,
            "bridge_connected": bridge_connected,
        })

    return JSONResponse(content={
        "connected": True,
        "channel":   row["twitch_channel"],
        "enabled":   bool(row.get("twitch_chat_enabled", 1)),
        "persistence_mode": persistence_mode,
        "bridge_connected": bridge_connected,
    })


@router.post("/api/twitch/disconnect/{session_id}")
async def twitch_disconnect(session_id: str, request: Request):
    """Revoke the stored Twitch token and clear the connection."""
    if not _resolve_dm(request, session_id):
        return JSONResponse(status_code=403, content={"error": "Forbidden."})

    row = _get_twitch_row(session_id)
    if row and row.get("twitch_access_token_enc"):
        access_token = decrypt(row["twitch_access_token_enc"])
        if access_token:
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(_TWITCH_REVOKE_URL, data={
                        "client_id": _client_id(),
                        "token":     access_token,
                    })
            except Exception as exc:
                logger.warning("[twitch_oauth] token revoke failed (ignoring): %s", exc)

    _clear_twitch_tokens(session_id)
    logger.info("[twitch_oauth] disconnected session=%s", session_id)
    return JSONResponse(content={"ok": True})


@router.post("/api/twitch/toggle/{session_id}")
async def twitch_toggle(session_id: str, request: Request):
    """Enable or disable the Twitch chat integration for a session."""
    if not _resolve_dm(request, session_id):
        return JSONResponse(status_code=403, content={"error": "Forbidden."})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON."})

    enabled = bool(body.get("enabled", True))
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET twitch_chat_enabled=? WHERE id=?",
            (int(enabled), session_id),
        )
        conn.commit()

    # Mirror as a kill-switch event on the live session if it's active,
    # and push the status to the bridge/overlays so the pause is instant.
    session = get_session(session_id)
    if session:
        session.chat_bridge_paused = not enabled
        try:
            from server.handlers.common import manager
            await manager.broadcast(session.id, {
                "type": "chat_bridge_status",
                "payload": {
                    "paused": not enabled,
                    "arena_enabled": bool(getattr(session, "arena_enabled", True)),
                    "arena_quiet": bool(getattr(session, "arena_quiet", False)),
                },
            })
        except Exception:
            logger.exception("Failed to broadcast chat_bridge_status after toggle")

    return JSONResponse(content={"ok": True, "enabled": enabled})


async def get_campaign_twitch_credentials(session_id: str) -> dict | None:
    """
    Return decrypted Twitch credentials for the given campaign.
    Auto-refreshes the access token if it is within 5 minutes of expiry.
    Returns None if no credentials are stored.
    """
    row = _get_twitch_row(session_id)
    if not row or not row.get("twitch_channel") or not row.get("twitch_access_token_enc"):
        return None

    access_token  = decrypt(row["twitch_access_token_enc"])
    refresh_token = decrypt(row["twitch_refresh_token_enc"])
    expires_at    = float(row.get("twitch_token_expires_at") or 0)

    if not access_token:
        return None

    # Refresh if expiring in the next 5 minutes
    if expires_at and time.time() > expires_at - 300:
        refreshed = await _refresh_access_token(refresh_token)
        if refreshed:
            access_token  = refreshed.get("access_token", access_token)
            refresh_token = refreshed.get("refresh_token", refresh_token)
            expires_in    = int(refreshed.get("expires_in") or 14400)
            expires_at    = time.time() + expires_in
            _save_twitch_tokens(
                session_id,
                row["twitch_channel"],
                row.get("twitch_channel_id", ""),
                access_token,
                refresh_token,
                expires_at,
            )

    return {
        "channel":    row["twitch_channel"],
        "channel_id": row.get("twitch_channel_id", ""),
        "access_token": access_token,
        "enabled":    bool(row.get("twitch_chat_enabled", 1)),
    }
