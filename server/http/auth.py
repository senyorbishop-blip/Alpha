from fastapi import Request

from server.auth.jwt_utils import verify_token
from server.auth.models import get_campaign_owner_user_id, get_user_by_id

AUTH_COOKIE = "dnd_session"


def get_request_user(request: Request):
    """Return the authenticated user from cookie or Authorization header, or None."""
    cookies = getattr(request, "cookies", {}) or {}
    headers = getattr(request, "headers", {}) or {}
    token = cookies.get(AUTH_COOKIE)
    if not token:
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    payload = verify_token(token)
    if not payload:
        return None
    return get_user_by_id(payload["sub"])


def is_session_dm(request: Request, session_id: str) -> bool:
    """Return True if the request JWT belongs to the DM of this session.

    A caller qualifies either by matching the session's dm_id (the live or
    persisted join-link identity) or by owning the campaign through
    campaigns.owner_user_id — a DM who entered through a join link plays
    under a per-session dm_id that differs from their account id, so the
    account would otherwise never pass the dm_id check.
    """
    cookies = getattr(request, "cookies", {}) or {}
    headers = getattr(request, "headers", {}) or {}
    token = (
        cookies.get(AUTH_COOKIE)
        or (headers.get("authorization") or "").removeprefix("Bearer ").strip()
    )
    if not token:
        return False
    payload = verify_token(token)
    if not payload:
        return False
    user_id = str(payload.get("sub") or "")
    if not user_id:
        return False

    # Lazy imports: server.session and server.db import broadly at module
    # scope and this module is loaded early in their startup path.
    from server.db import load_campaign
    from server.session import get_session

    session = get_session(session_id)
    if session:
        if user_id == session.dm_id:
            return True
    else:
        data = load_campaign(session_id)
        if data and data.get("dm_id") == user_id:
            return True

    owner_id = get_campaign_owner_user_id(session_id)
    return bool(owner_id) and user_id == owner_id


def auth_player_key(user_id: str) -> str:
    """Return the stable player_key for an authenticated account."""
    return "auth_" + str(user_id)


def auth_display_name(auth_user: dict, fallback: str = "Adventurer") -> str:
    """Return the display name for an authenticated account."""
    return str(
        auth_user.get("character_name") or auth_user.get("username") or fallback
    ).strip()[:40] or fallback
