from fastapi import Request

from server.auth.jwt_utils import verify_token
from server.auth.models import get_user_by_id

AUTH_COOKIE = "dnd_session"


def _request_token(request: Request) -> str:
    """Return the raw JWT from the auth cookie or Authorization header, or ''."""
    cookies = getattr(request, "cookies", {}) or {}
    headers = getattr(request, "headers", {}) or {}
    token = cookies.get(AUTH_COOKIE)
    if not token:
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    return token or ""


def get_request_user(request: Request):
    """Return the authenticated user from cookie or Authorization header, or None."""
    token = _request_token(request)
    if not token:
        return None
    payload = verify_token(token)
    if not payload:
        return None
    return get_user_by_id(payload["sub"])


def get_request_token_sub(request: Request) -> str:
    """Return the verified JWT 'sub' claim from the request, or ''.

    Unlike get_request_user this does not require the subject to be a
    registered account — legacy session-scoped JWTs carry a per-session
    dm_id as their subject rather than an account id.
    """
    token = _request_token(request)
    payload = verify_token(token) if token else None
    return str((payload or {}).get("sub") or "")


def auth_player_key(user_id: str) -> str:
    """Return the stable player_key for an authenticated account."""
    return "auth_" + str(user_id)


def auth_display_name(auth_user: dict, fallback: str = "Adventurer") -> str:
    """Return the display name for an authenticated account."""
    return str(
        auth_user.get("character_name") or auth_user.get("username") or fallback
    ).strip()[:40] or fallback
