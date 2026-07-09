"""Lightweight character-profile stubs for WebSocket state sync.

Full character profiles (charSheet / nativeCharacter / nativeRuntime bodies)
routinely reach hundreds of KB each, and shipping every profile body in the
initial ``state_sync`` is what pushed connects past the payload error
threshold. The roster/party UI only ever renders a handful of scalar fields,
so state sync now carries *stubs* built here; the full body is fetched on
demand via the ``char_profile_fetch`` message (see
``server.handlers.content.handle_char_profile_fetch``).

Each stub carries a content-derived ``revision`` so clients can cache a
fetched full body and re-fetch only when the profile actually changed.
Deriving the revision from content (rather than a manually bumped counter)
means every server-side mutation path — rests, pets, summons, token HP sync,
imports — invalidates client caches without needing an explicit bump call.
"""
from __future__ import annotations

import json
import logging
import zlib
from typing import Any

from server.character.profile_assets import (
    DATA_IMAGE_RE,
    ensure_profile_portrait_thumbnail,
)

logger = logging.getLogger(__name__)

# Scalar fields the client roster/party/profile-select UI reads without a
# sheet open (see client _resolveProfileImageFields / partyRows / upsert
# quick-panel stats). Everything else is body data fetched on demand.
STUB_SCALAR_KEYS: tuple[str, ...] = (
    "id", "name", "updated_at", "created_at",
    "curhp", "hp", "tempHp", "initiative", "ac", "speed", "level", "passive",
    "faction", "classId", "color", "accentColor", "diceTheme",
    "portraitFrame", "tagline", "sourceMode", "ac_from_equipment",
    "libraryId", "characterId",
)


def _looks_like_plain_url(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not value.startswith("data:")


def char_profile_revision(profile: Any) -> int:
    """Content-derived revision for one stored profile.

    A stable checksum of the canonical JSON serialization. Any mutation of the
    stored profile (by any handler) yields a new revision, so clients can use
    equality as their body-cache validity check. Collisions only cause a
    skipped re-fetch, and CRC32 over sorted-key JSON is cheap enough to run at
    connect/upsert frequency.
    """
    if not isinstance(profile, dict):
        return 0
    try:
        raw = json.dumps(profile, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        raw = str(profile)
    # Avoid 0 so clients can treat 0/missing as "unknown revision".
    return (zlib.crc32(raw.encode("utf-8", errors="ignore")) & 0xFFFFFFFF) or 1


def _stub_portrait_urls(profile: dict) -> tuple[str, str]:
    """Resolve (portrait_url, thumb_url) from the same source order the full
    sanitizer uses, skipping inline data: blobs entirely."""
    candidates = (
        (((profile.get("nativeCharacter") or {}).get("identity") or {}).get("portraitUrl")
         if isinstance(profile.get("nativeCharacter"), dict) else ""),
        ((profile.get("charSheet") or {}).get("avatarUrl")
         if isinstance(profile.get("charSheet"), dict) else ""),
        ((profile.get("charBook") or {}).get("avatarUrl")
         if isinstance(profile.get("charBook"), dict) else ""),
        profile.get("avatarUrl"),
        profile.get("portraitUrl"),
        profile.get("tokenImageUrl"),
    )
    for candidate in candidates:
        if _looks_like_plain_url(candidate) and not DATA_IMAGE_RE.match(candidate):
            url = str(candidate).strip()
            try:
                thumb = ensure_profile_portrait_thumbnail(url)
            except Exception:
                thumb = url
            return url, thumb or url
    return "", ""


def build_char_profile_stub(profile: Any) -> Any:
    """Build the lightweight sync stub for one stored profile.

    The stub deliberately mirrors the top-level scalar fields of the full
    profile so existing client readers keep working; ``stub: true`` is the
    discriminator clients use to know the body is absent.
    """
    if not isinstance(profile, dict):
        return profile
    stub: dict[str, Any] = {}
    for key in STUB_SCALAR_KEYS:
        if key in profile:
            value = profile.get(key)
            if value is None or isinstance(value, (str, int, float, bool)):
                stub[key] = value
    class_summary = profile.get("classSummary")
    if isinstance(class_summary, (str, int, float, bool)):
        stub["classSummary"] = class_summary
    if _looks_like_plain_url(profile.get("avatarUrl")):
        stub["avatarUrl"] = str(profile.get("avatarUrl")).strip()
    # Preserve the client's sheet-mode resolution (`_profileHasNativePayload`
    # treats a non-empty nativeCharacter as "native mode") without shipping
    # the native document itself.
    native = profile.get("nativeCharacter")
    if isinstance(native, dict) and native:
        native_stub: dict[str, Any] = {"stub": True}
        source_mode = native.get("sourceMode")
        if isinstance(source_mode, str) and source_mode:
            native_stub["sourceMode"] = source_mode[:40]
        stub["nativeCharacter"] = native_stub
    char_book = profile.get("charBook")
    if isinstance(char_book, dict):
        class_name = char_book.get("className")
        if isinstance(class_name, str) and class_name:
            stub["charBook"] = {"className": class_name[:80]}
    portrait_url, thumb_url = _stub_portrait_urls(profile)
    stub["portrait_url"] = portrait_url
    stub["thumb_url"] = thumb_url
    stub["revision"] = char_profile_revision(profile)
    stub["stub"] = True
    return stub


def build_char_profile_stub_list(rows: Any) -> list:
    if not isinstance(rows, list):
        return []
    return [build_char_profile_stub(row) for row in rows if isinstance(row, dict)]


def build_char_profile_stub_map(profiles: Any) -> dict:
    if not isinstance(profiles, dict):
        return {}
    return {
        str(owner_key): build_char_profile_stub_list(rows)
        for owner_key, rows in profiles.items()
    }


def char_profiles_fingerprint(session) -> int:
    """Session-wide fingerprint over every profile revision + active map.

    Used by the reconnect delta path: if a client's stored fingerprint matches,
    the char_profiles domain is unchanged and can be omitted from the resync.
    """
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    parts: list[str] = []
    for owner_key in sorted(profiles.keys(), key=str):
        rows = profiles.get(owner_key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                parts.append(f"{owner_key}:{row.get('id')}:{char_profile_revision(row)}")
    try:
        active = json.dumps(dict(getattr(session, "active_char_profiles", {}) or {}), sort_keys=True, default=str)
    except Exception:
        active = ""
    raw = "|".join(parts) + "#" + active
    return (zlib.crc32(raw.encode("utf-8", errors="ignore")) & 0xFFFFFFFF) or 1
