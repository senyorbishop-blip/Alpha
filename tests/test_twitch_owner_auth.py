"""
Regression tests for DM guards on the Twitch/overlay management endpoints.

A DM who plays through a join-link identity gets a per-session dm_id that
differs from their account id, so a JWT whose sub == campaigns.owner_user_id
(but != session.dm_id) must still pass the DM guard on /api/twitch/connect
and the other _resolve_dm-protected endpoints.
"""
import os
import secrets
import tempfile

# Isolate persistence to a temp data dir BEFORE importing server modules so
# paths.py resolves DB_PATH / DATA_DIR there (outside the repo tree).
_TMP = tempfile.mkdtemp(prefix="twitch_owner_auth_test_")
os.environ.setdefault("DND_DATA_DIR", _TMP)
os.environ.setdefault("DND_DB_PATH", os.path.join(_TMP, "campaigns.db"))

import pytest
from fastapi.testclient import TestClient

import main
from server.auth.jwt_utils import create_token
from server.auth.models import (
    claim_campaign,
    create_user,
    get_campaign_owner_user_id,
    init_auth_db,
)
from server.db import init_db, save_campaign
from server.http.auth import auth_player_key
from server.session import create_session, get_session, _sessions

_CSRF = "c" * 32


@pytest.fixture(autouse=True)
def _twitch_env(monkeypatch):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "test-client-secret")
    init_db()
    init_auth_db()
    yield


@pytest.fixture
def client():
    c = TestClient(main.app, follow_redirects=False)
    c.cookies.set("csrf_token", _CSRF)
    return c


def _make_claimed_session():
    """Create a live session plus an account that owns its campaign row."""
    session, dm = create_session("Join-Link DM")
    save_campaign(session)
    suffix = secrets.token_hex(4)
    owner = create_user(f"owner_{suffix}", f"{suffix}@example.com", "x", "dm")
    assert claim_campaign(session.id, owner["id"], owner["username"])
    assert owner["id"] != session.dm_id  # the premise of the regression
    return session, dm, owner


def _get(client, url, user_id):
    client.cookies.set("dnd_session", create_token(user_id, "u", "dm"))
    return client.get(url)


def _post(client, url, user_id, **kwargs):
    client.cookies.set("dnd_session", create_token(user_id, "u", "dm"))
    return client.post(url, headers={"X-CSRF-Token": _CSRF}, **kwargs)


# ── /api/twitch/connect ──────────────────────────────────────────────────────

def test_connect_allows_campaign_owner_account(client):
    session, _dm, owner = _make_claimed_session()
    resp = _get(client, f"/api/twitch/connect/{session.id}", owner["id"])
    assert resp.status_code in (302, 307), resp.text
    assert resp.headers["location"].startswith("https://id.twitch.tv/oauth2/authorize")


def test_connect_still_allows_session_dm_id(client):
    session, dm, _owner = _make_claimed_session()
    resp = _get(client, f"/api/twitch/connect/{session.id}", dm.id)
    assert resp.status_code in (302, 307), resp.text


def test_connect_rejects_unrelated_account(client):
    session, _dm, _owner = _make_claimed_session()
    stranger = create_user(f"other_{secrets.token_hex(4)}", f"o{secrets.token_hex(4)}@example.com", "x", "dm")
    resp = _get(client, f"/api/twitch/connect/{session.id}", stranger["id"])
    assert resp.status_code == 403


def test_connect_rejects_anonymous(client):
    session, _dm, _owner = _make_claimed_session()
    resp = client.get(f"/api/twitch/connect/{session.id}")
    assert resp.status_code == 403


def test_connect_allows_owner_when_session_not_in_memory(client):
    """Owner check must also work off the persisted campaign row alone."""
    session, _dm, owner = _make_claimed_session()
    _sessions.pop(session.id, None)
    assert get_session(session.id) is None
    resp = _get(client, f"/api/twitch/connect/{session.id}", owner["id"])
    assert resp.status_code in (302, 307), resp.text


def test_unclaimed_campaign_grants_no_owner_access(client):
    """owner_user_id NULL must not let arbitrary accounts through."""
    session, _dm = create_session("Unclaimed DM")
    save_campaign(session)
    assert get_campaign_owner_user_id(session.id) is None
    stranger = create_user(f"nobody_{secrets.token_hex(4)}", f"n{secrets.token_hex(4)}@example.com", "x", "dm")
    resp = _get(client, f"/api/twitch/connect/{session.id}", stranger["id"])
    assert resp.status_code == 403


# ── Other DM-guarded Twitch/overlay endpoints ────────────────────────────────

def test_owner_account_passes_all_dm_guarded_endpoints(client):
    session, _dm, owner = _make_claimed_session()

    resp = _get(client, f"/api/twitch/status/{session.id}", owner["id"])
    assert resp.status_code == 200, resp.text

    resp = _post(client, f"/api/twitch/toggle/{session.id}", owner["id"], json={"enabled": False})
    assert resp.status_code == 200, resp.text

    resp = _post(client, f"/api/twitch/disconnect/{session.id}", owner["id"])
    assert resp.status_code == 200, resp.text

    resp = _get(client, f"/api/overlay/urls/{session.id}", owner["id"])
    assert resp.status_code == 200, resp.text

    resp = _post(client, f"/api/overlay/regenerate/{session.id}?type=game", owner["id"])
    assert resp.status_code == 200, resp.text


def _make_player_key_linked_session():
    """Create a live session whose DM user is linked to an account only via
    auth_player_key — the account id differs from session.dm_id and the
    campaign row is never claimed (owner_user_id stays NULL), so the account
    can only pass the DM guard through the canonical resolver's player_key
    mapping."""
    session, dm = create_session("Player-Key DM")
    suffix = secrets.token_hex(4)
    account = create_user(f"linked_{suffix}", f"l{suffix}@example.com", "x", "dm")
    assert account["id"] != session.dm_id  # the premise of the regression
    assert get_campaign_owner_user_id(session.id) is None
    dm.player_key = auth_player_key(account["id"])
    save_campaign(session)
    return session, dm, account


def test_auth_player_key_mapped_account_passes_dm_guard(client):
    """Regression: an account resolved to the session DM via auth_player_key
    (matched_via=player_key, is_session_dm=True) must pass _resolve_dm."""
    session, _dm, account = _make_player_key_linked_session()
    resp = _get(client, f"/api/twitch/connect/{session.id}", account["id"])
    assert resp.status_code in (302, 307), resp.text
    assert resp.headers["location"].startswith("https://id.twitch.tv/oauth2/authorize")


def test_auth_player_key_mapped_account_passes_all_dm_guarded_endpoints(client):
    session, _dm, account = _make_player_key_linked_session()

    resp = _get(client, f"/api/twitch/status/{session.id}", account["id"])
    assert resp.status_code == 200, resp.text

    resp = _post(client, f"/api/twitch/toggle/{session.id}", account["id"], json={"enabled": True})
    assert resp.status_code == 200, resp.text

    resp = _post(client, f"/api/twitch/disconnect/{session.id}", account["id"])
    assert resp.status_code == 200, resp.text

    resp = _get(client, f"/api/overlay/urls/{session.id}", account["id"])
    assert resp.status_code == 200, resp.text

    resp = _post(client, f"/api/overlay/regenerate/{session.id}?type=game", account["id"])
    assert resp.status_code == 200, resp.text


def test_stranger_rejected_on_all_dm_guarded_endpoints(client):
    session, _dm, _owner = _make_claimed_session()
    stranger = create_user(f"other_{secrets.token_hex(4)}", f"s{secrets.token_hex(4)}@example.com", "x", "dm")

    for method, url in [
        ("get", f"/api/twitch/status/{session.id}"),
        ("post", f"/api/twitch/toggle/{session.id}"),
        ("post", f"/api/twitch/disconnect/{session.id}"),
        ("get", f"/api/overlay/urls/{session.id}"),
        ("post", f"/api/overlay/regenerate/{session.id}?type=game"),
    ]:
        if method == "get":
            resp = _get(client, url, stranger["id"])
        else:
            kwargs = {"json": {"enabled": True}} if "toggle" in url else {}
            resp = _post(client, url, stranger["id"], **kwargs)
        assert resp.status_code == 403, f"{method.upper()} {url} -> {resp.status_code}"
