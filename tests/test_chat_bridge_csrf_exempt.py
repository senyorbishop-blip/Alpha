"""
Regression test: the chat-bridge endpoints are machine-to-machine (secured by
the CHAT_BRIDGE_TOKEN shared secret, not browser cookies) and must be exempt
from CSRF protection.

Bug: POST /api/chat-bridge/auth returned 403 {"ok": false, "error": "CSRF
token missing"} because /api/chat-bridge/ was missing from the
CSRFMiddleware exempt_prefixes in main.py — the bridge service could never
authenticate.
"""
import os
import tempfile

# Isolate persistence to a temp data dir BEFORE importing server modules so
# paths.py resolves DB_PATH / DATA_DIR there (outside the repo tree).
_TMP = tempfile.mkdtemp(prefix="chat_bridge_csrf_test_")
os.environ.setdefault("DND_DATA_DIR", _TMP)
os.environ.setdefault("DND_DB_PATH", os.path.join(_TMP, "campaigns.db"))

import pytest
from fastapi.testclient import TestClient

import main

_BRIDGE_TOKEN = "test-bridge-token-abc123"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CHAT_BRIDGE_TOKEN", _BRIDGE_TOKEN)
    # No csrf_token cookie is set — the bridge is a headless service and
    # never carries one.
    return TestClient(main.app, follow_redirects=False)


def test_bridge_auth_succeeds_without_csrf_cookie_or_header(client):
    resp = client.post("/api/chat-bridge/auth", json={"token": _BRIDGE_TOKEN})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == "chat_bridge"
    assert body["jwt"]


def test_bridge_auth_still_rejects_bad_bridge_token(client):
    # The exemption skips CSRF only — the shared-secret check must still run.
    resp = client.post("/api/chat-bridge/auth", json={"token": "wrong-token"})
    assert resp.status_code == 401, resp.text


def test_browser_endpoints_still_require_csrf(client):
    # A state-changing non-bridge endpoint must still be CSRF-protected.
    resp = client.post("/api/overlay/regenerate/some-session")
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"ok": False, "error": "CSRF token missing"}
