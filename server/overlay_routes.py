"""
server/overlay_routes.py — OBS browser-source overlay pages and URL management.

Routes:
  GET  /overlay/game?session=<id>&key=<token>   — serve game overlay HTML
  GET  /overlay/arena?session=<id>&key=<token>  — serve arena overlay HTML
  GET  /api/overlay/urls/{session_id}            — return overlay URLs (DM only)
  POST /api/overlay/regenerate/{session_id}      — regenerate a token (DM only)
                                                   query: ?type=game|arena

Overlay pages are transparent, idle-invisible OBS browser sources.  Each URL
embeds a per-campaign secret token that the server validates before issuing a
short-lived overlay-role JWT; the JWT is embedded in the rendered HTML and used
to authenticate the WebSocket connection.

Overlay WebSocket connections use role="overlay" which is read-only — the WS
permission policy prevents them from sending any commands.
"""
from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from server.auth.jwt_utils import _JWT_SECRET, _ALGORITHM
from server.auth.models import get_campaign_owner_user_id
from server.db import get_conn
from server.http.auth import get_request_token_sub, get_request_user
from server.http.session_access import get_or_restore_session, request_has_dm_access

try:
    import jwt as _jwt
except ImportError:
    _jwt = None

router = APIRouter()
logger = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "client" / "templates"))

# Overlay JWT TTL — long enough for OBS to stay connected without reloads
_OVERLAY_JWT_TTL = 60 * 60 * 24 * 7  # 7 days


def _make_overlay_jwt(user_id: str, session_id: str) -> str:
    """Create a JWT for an overlay WebSocket connection."""
    payload = {
        "sub": user_id,
        "username": "Overlay",
        "role": "overlay",
        "session_id": session_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + _OVERLAY_JWT_TTL,
    }
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_ALGORITHM)


def _resolve_dm(request: Request, session_id: str) -> bool:
    """Return True if the request is authorized as this session's DM.

    Delegates to the canonical resolver (request_has_dm_access), which matches
    the session dm_id, a participant with role=dm, or a session user linked to
    the authenticated account via auth_player_key. The JWT subject is passed
    as fallback_user_id so legacy session-scoped tokens (sub == per-session
    dm_id, no registered account) keep working. Falls back to the campaign
    owner account (campaigns.owner_user_id) for claimed campaigns whose owner
    never joined the live session.
    """
    session = get_or_restore_session(session_id)
    if session and request_has_dm_access(
        request, session, fallback_user_id=get_request_token_sub(request)
    ):
        return True
    auth_user = get_request_user(request)
    if not auth_user:
        return False
    owner_id = get_campaign_owner_user_id(session_id)
    return bool(owner_id) and str(auth_user.get("id") or "") == owner_id


def _get_or_create_overlay_tokens(session_id: str) -> dict | None:
    """Return {game, arena} overlay tokens, generating them if missing."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT overlay_game_token, overlay_arena_token FROM campaigns WHERE id=?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            game_token = (row["overlay_game_token"] or "").strip()
            arena_token = (row["overlay_arena_token"] or "").strip()
            changed = False
            if not game_token:
                game_token = secrets.token_urlsafe(32)
                changed = True
            if not arena_token:
                arena_token = secrets.token_urlsafe(32)
                changed = True
            if changed:
                conn.execute(
                    "UPDATE campaigns SET overlay_game_token=?, overlay_arena_token=? WHERE id=?",
                    (game_token, arena_token, session_id),
                )
                conn.commit()
            return {"game": game_token, "arena": arena_token}
    except Exception as exc:
        logger.error("[overlay] _get_or_create_overlay_tokens error: %s", exc)
        return None


def _validate_overlay_key(session_id: str, key: str, token_type: str) -> bool:
    """Validate that key matches the stored overlay token of the given type."""
    if not key or not session_id:
        return False
    try:
        with get_conn() as conn:
            col = "overlay_game_token" if token_type == "game" else "overlay_arena_token"
            row = conn.execute(
                f"SELECT {col} FROM campaigns WHERE id=?", (session_id,)
            ).fetchone()
            if not row:
                return False
            stored = (row[col] or "").strip()
            return bool(stored) and secrets.compare_digest(stored, key)
    except Exception as exc:
        logger.error("[overlay] _validate_overlay_key error: %s", exc)
        return False


def _public_base_url(request: Request) -> str:
    import os
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


# ---------------------------------------------------------------------------
# Overlay pages
# ---------------------------------------------------------------------------

@router.get("/overlay/game", response_class=HTMLResponse)
async def overlay_game_page(request: Request, session: str = "", key: str = ""):
    """Serve the game overlay page after validating the overlay key."""
    if not session or not key:
        return HTMLResponse("<html><body>Missing session or key.</body></html>", status_code=400)
    if not _validate_overlay_key(session, key, "game"):
        return HTMLResponse("<html><body>Invalid overlay key.</body></html>", status_code=403)

    if _jwt is None:
        return HTMLResponse("<html><body>JWT library not available.</body></html>", status_code=500)

    overlay_jwt = _make_overlay_jwt("overlay_game", session)
    base_url = _public_base_url(request)
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")

    return _TEMPLATES.TemplateResponse(
        "overlay_game.html",
        {
            "request": request,
            "session_id": session,
            "overlay_jwt": overlay_jwt,
            "ws_url": ws_url,
        },
    )


@router.get("/overlay/arena", response_class=HTMLResponse)
async def overlay_arena_page(request: Request, session: str = "", key: str = ""):
    """Serve the arena overlay page after validating the overlay key."""
    if not session or not key:
        return HTMLResponse("<html><body>Missing session or key.</body></html>", status_code=400)
    if not _validate_overlay_key(session, key, "arena"):
        return HTMLResponse("<html><body>Invalid overlay key.</body></html>", status_code=403)

    if _jwt is None:
        return HTMLResponse("<html><body>JWT library not available.</body></html>", status_code=500)

    overlay_jwt = _make_overlay_jwt("overlay_arena", session)
    base_url = _public_base_url(request)
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")

    return _TEMPLATES.TemplateResponse(
        "overlay_arena.html",
        {
            "request": request,
            "session_id": session,
            "overlay_jwt": overlay_jwt,
            "ws_url": ws_url,
        },
    )


# ---------------------------------------------------------------------------
# DM management API
# ---------------------------------------------------------------------------

@router.get("/api/overlay/urls/{session_id}")
async def overlay_get_urls(session_id: str, request: Request):
    """Return the overlay URLs for a session (DM only)."""
    if not _resolve_dm(request, session_id):
        return JSONResponse(status_code=403, content={"error": "Forbidden."})

    tokens = _get_or_create_overlay_tokens(session_id)
    if tokens is None:
        return JSONResponse(status_code=404, content={"error": "Session not found."})

    base = _public_base_url(request)
    game_url  = f"{base}/overlay/game?session={session_id}&key={tokens['game']}"
    arena_url = f"{base}/overlay/arena?session={session_id}&key={tokens['arena']}"

    return JSONResponse(content={
        "game_url":  game_url,
        "arena_url": arena_url,
    })


@router.post("/api/overlay/regenerate/{session_id}")
async def overlay_regenerate(session_id: str, request: Request, type: str = "game"):
    """Regenerate an overlay token (DM only).  type = 'game' | 'arena'."""
    if not _resolve_dm(request, session_id):
        return JSONResponse(status_code=403, content={"error": "Forbidden."})

    if type not in ("game", "arena"):
        return JSONResponse(status_code=400, content={"error": "type must be 'game' or 'arena'."})

    new_token = secrets.token_urlsafe(32)
    col = "overlay_game_token" if type == "game" else "overlay_arena_token"
    try:
        with get_conn() as conn:
            updated = conn.execute(
                f"UPDATE campaigns SET {col}=? WHERE id=?",
                (new_token, session_id),
            ).rowcount
            conn.commit()
        if not updated:
            return JSONResponse(status_code=404, content={"error": "Session not found."})
    except Exception as exc:
        logger.error("[overlay] regenerate error: %s", exc)
        return JSONResponse(status_code=500, content={"error": "Database error."})

    base = _public_base_url(request)
    new_url = f"{base}/overlay/{type}?session={session_id}&key={new_token}"
    return JSONResponse(content={"ok": True, "type": type, "url": new_url})
