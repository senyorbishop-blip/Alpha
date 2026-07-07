"""
server/token_enc.py — Symmetric encryption for OAuth tokens stored at rest.

Uses AES-256-GCM via the `cryptography` package.  The key is derived from the
TWITCH_TOKEN_ENC_KEY environment variable (a URL-safe base64-encoded 32-byte
value).  If the env var is absent on first use, a random key is auto-generated
and printed to stderr so the operator can persist it.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets

logger = logging.getLogger(__name__)

_KEY: bytes | None = None


def _load_key() -> bytes:
    global _KEY
    if _KEY is not None:
        return _KEY

    raw = os.environ.get("TWITCH_TOKEN_ENC_KEY", "").strip()
    if raw:
        try:
            key = base64.urlsafe_b64decode(raw + "==")
            if len(key) == 32:
                _KEY = key
                return _KEY
        except Exception:
            pass
        logger.error(
            "[token_enc] TWITCH_TOKEN_ENC_KEY is set but not valid URL-safe base64-32-bytes. "
            "Generate one with: python -c \"import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\""
        )

    # Auto-generate a key and warn loudly — tokens will be unreadable after restart
    # unless the operator sets this env var.
    key = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(key).decode()
    logger.warning(
        "[token_enc] TWITCH_TOKEN_ENC_KEY is not set. A temporary key was generated. "
        "Stored Twitch tokens will be lost on restart. "
        "Set TWITCH_TOKEN_ENC_KEY=%s in your .env to persist them.", encoded
    )
    _KEY = key
    return _KEY


def encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* and return a URL-safe base64-encoded ciphertext string."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not plaintext:
        return ""
    key = _load_key()
    nonce = secrets.token_bytes(12)  # 96-bit nonce
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a ciphertext produced by :func:`encrypt`. Returns "" on any error."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not ciphertext:
        return ""
    try:
        key = _load_key()
        raw = base64.urlsafe_b64decode(ciphertext + "==")
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception:
        logger.warning("[token_enc] decrypt failed — token may have been encrypted with a different key")
        return ""
