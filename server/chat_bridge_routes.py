"""
server/chat_bridge_routes.py — HTTP endpoints for the Twitch chat bridge.

POST /api/chat-bridge/auth
  Accepts the CHAT_BRIDGE_TOKEN shared secret and returns a short-lived JWT
  with role="chat_bridge" that the bridge uses on its WebSocket connection.

The token is valid for 30 days; the bridge should call this endpoint on
startup and re-auth before expiry.
"""
from __future__ import annotations

import os
import time
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.auth.jwt_utils import create_token

router = APIRouter()
logger = logging.getLogger(__name__)

_CHAT_BRIDGE_USER_ID = "chat_bridge"
_CHAT_BRIDGE_USERNAME = "chat_bridge"


def _get_bridge_token() -> str:
    return os.environ.get("CHAT_BRIDGE_TOKEN", "").strip()


@router.post("/api/chat-bridge/auth")
async def chat_bridge_auth(request: Request):
    """Mint a JWT for the chat bridge service.

    Body: { "token": "<CHAT_BRIDGE_TOKEN>" }
    Returns: { "jwt": "...", "user_id": "chat_bridge" }
    """
    bridge_token = _get_bridge_token()
    if not bridge_token:
        return JSONResponse(
            status_code=503,
            content={"error": "Chat bridge is not configured on this server (CHAT_BRIDGE_TOKEN not set)."},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    provided = str(body.get("token") or "").strip()
    if not provided or provided != bridge_token:
        logger.warning("[chat_bridge] auth attempt with invalid token from %s", request.client.host if request.client else "unknown")
        return JSONResponse(status_code=401, content={"error": "Invalid bridge token."})

    jwt = create_token(
        user_id=_CHAT_BRIDGE_USER_ID,
        username=_CHAT_BRIDGE_USERNAME,
        role="chat_bridge",
    )

    logger.info("[chat_bridge] issued JWT for chat_bridge service")
    return JSONResponse(content={"jwt": jwt, "user_id": _CHAT_BRIDGE_USER_ID})


@router.get("/api/chat-bridge/status")
async def chat_bridge_status(request: Request):
    """Health check: returns whether the bridge is configured."""
    configured = bool(_get_bridge_token())
    return JSONResponse(content={"configured": configured})


@router.get("/api/chat-bridge/credentials/{session_id}")
async def chat_bridge_credentials(session_id: str, request: Request):
    """Return decrypted Twitch credentials for a campaign.

    The bridge calls this on startup to get the per-campaign Twitch channel and
    access token so it can connect to the correct channel without requiring env
    vars to be set for each deployment.

    Auth: X-Bridge-Token header must match CHAT_BRIDGE_TOKEN.
    """
    bridge_token = _get_bridge_token()
    if not bridge_token:
        return JSONResponse(status_code=503, content={"error": "Chat bridge not configured."})

    provided = (request.headers.get("x-bridge-token") or "").strip()
    if not provided or provided != bridge_token:
        logger.warning("[chat_bridge] credentials request with invalid token from %s", request.client.host if request.client else "unknown")
        return JSONResponse(status_code=401, content={"error": "Invalid bridge token."})

    from server.twitch_oauth_routes import get_campaign_twitch_credentials
    creds = await get_campaign_twitch_credentials(session_id)
    if not creds:
        return JSONResponse(status_code=404, content={"error": "No Twitch credentials configured for this campaign."})

    return JSONResponse(content=creds)
