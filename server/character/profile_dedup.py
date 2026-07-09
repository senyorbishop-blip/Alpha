"""Storage-layout dedup for persisted char_profiles.

PDF-imported profiles store byte-identical subtrees in more than one place —
``charSheet.spellAccess`` vs ``nativeRuntime.spellAccess``, passives, class
features, etc. — which alone pushed the persisted ``char_profiles`` campaign
field past 3MB. This module deduplicates those subtrees at the persistence
boundary ONLY:

- ``dedupe_char_profiles_for_storage`` runs inside the DB serializer; the
  in-memory ``session.char_profiles`` is never touched, so every runtime
  reader keeps seeing plain hydrated profiles.
- ``hydrate_char_profiles_from_storage`` runs inside the DB deserializer, so
  every consumer of ``load_campaign`` receives fully hydrated content.

The transform changes storage layout, never content: hydrate(dedupe(x)) is
JSON-identical to x, and the startup migration verifies that per campaign
before rewriting a row (see ``server.db.migrate_char_profiles_dedup``).

Storage format inside one profile document::

    {
      ...,
      "charSheet":     {"spellAccess": {"__dedup_ref__": "<hash>"}, ...},
      "nativeRuntime": {"spellAccess": {"__dedup_ref__": "<hash>"}, ...},
      "_dedup": {"v": 1, "store": {"<hash>": <the subtree, stored once>}}
    }

Refs are only ever placed at the top level of the profile itself or of its
charSheet / nativeRuntime / nativeCharacter containers, and only for
subtrees that occur at least twice, so hydration stays a cheap, symmetric
walk.
"""
from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

DEDUP_REF_KEY = "__dedup_ref__"
DEDUP_META_KEY = "_dedup"
DEDUP_VERSION = 1

# Containers whose top-level children are dedup candidates. The containers
# themselves are never dedup candidates so refs can't nest.
_CONTAINER_KEYS = ("charSheet", "nativeRuntime", "nativeCharacter", "charBook")

# Only bother content-addressing subtrees at least this large (canonical JSON
# bytes); below this the ref + store overhead isn't worth it.
MIN_SUBTREE_BYTES = 2048


def _canonical(value: Any) -> str | None:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except Exception:
        # Non-JSON-serializable subtrees can't round-trip safely; skip them.
        return None


def _subtree_hash(canonical: str) -> str:
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:20]


def _is_ref(value: Any) -> bool:
    return isinstance(value, dict) and DEDUP_REF_KEY in value


def _candidate_sites(profile: dict):
    """Yield (container_key_or_None, child_key, value) for every dedup site.

    ``container_key_or_None`` is None for profile-top-level children.
    """
    for key, value in profile.items():
        if key in _CONTAINER_KEYS or key == DEDUP_META_KEY:
            continue
        if isinstance(value, (dict, list)):
            yield None, key, value
    for container_key in _CONTAINER_KEYS:
        container = profile.get(container_key)
        if not isinstance(container, dict):
            continue
        for key, value in container.items():
            if isinstance(value, (dict, list)):
                yield container_key, key, value


def dedupe_profile_for_storage(profile: Any) -> Any:
    """Return a storage-shaped copy of one profile with shared subtrees stored once.

    The input profile is not mutated; unchanged subtrees are shared by
    reference with the input (safe because the result is serialized
    immediately and never handed to runtime code).
    """
    if not isinstance(profile, dict) or DEDUP_META_KEY in profile:
        # Already storage-shaped (or not a profile) — pass through unchanged.
        return profile

    by_hash: dict[str, list[tuple[str | None, str]]] = {}
    values_by_hash: dict[str, Any] = {}
    for container_key, key, value in _candidate_sites(profile):
        if _is_ref(value):
            return profile  # defensive: never double-transform
        canonical = _canonical(value)
        if canonical is None or len(canonical) < MIN_SUBTREE_BYTES:
            continue
        digest = _subtree_hash(canonical)
        by_hash.setdefault(digest, []).append((container_key, key))
        values_by_hash.setdefault(digest, value)

    shared = {digest: sites for digest, sites in by_hash.items() if len(sites) >= 2}
    if not shared:
        return profile

    out = dict(profile)
    replaced_containers: dict[str, dict] = {}
    store: dict[str, Any] = {}
    for digest, sites in shared.items():
        store[digest] = values_by_hash[digest]
        for container_key, key in sites:
            if container_key is None:
                out[key] = {DEDUP_REF_KEY: digest}
            else:
                container = replaced_containers.get(container_key)
                if container is None:
                    container = dict(out[container_key])
                    replaced_containers[container_key] = container
                container[key] = {DEDUP_REF_KEY: digest}
    for container_key, container in replaced_containers.items():
        out[container_key] = container
    out[DEDUP_META_KEY] = {"v": DEDUP_VERSION, "store": store}
    return out


def hydrate_profile_from_storage(profile: Any) -> Any:
    """Resolve dedup refs back into plain subtrees (inverse of dedupe)."""
    if not isinstance(profile, dict) or DEDUP_META_KEY not in profile:
        return profile
    meta = profile.get(DEDUP_META_KEY)
    store = meta.get("store") if isinstance(meta, dict) else {}
    if not isinstance(store, dict):
        store = {}
    out = dict(profile)
    out.pop(DEDUP_META_KEY, None)

    def _resolve(value: Any, site: str) -> Any:
        if not _is_ref(value):
            return value
        digest = str(value.get(DEDUP_REF_KEY) or "")
        if digest in store:
            # Deep-copy per site so hydrated occurrences don't share objects
            # (pre-dedup they were independent subtrees).
            return deepcopy(store[digest])
        logger.error(
            "[char_profile_dedup] missing store entry hash=%s site=%s profile=%s",
            digest, site, str(profile.get("id") or profile.get("name") or "?"),
        )
        return value

    for key in list(out.keys()):
        if key in _CONTAINER_KEYS:
            continue
        out[key] = _resolve(out[key], key)
    for container_key in _CONTAINER_KEYS:
        container = out.get(container_key)
        if not isinstance(container, dict):
            continue
        if any(_is_ref(v) for v in container.values()):
            new_container = dict(container)
            for key in list(new_container.keys()):
                new_container[key] = _resolve(new_container[key], f"{container_key}.{key}")
            out[container_key] = new_container
    return out


def dedupe_char_profiles_for_storage(profiles: Any) -> Any:
    """Storage transform for a whole char_profiles map (owner -> [profiles])."""
    if not isinstance(profiles, dict):
        return profiles
    out: dict[str, Any] = {}
    for owner_key, rows in profiles.items():
        if isinstance(rows, list):
            out[owner_key] = [dedupe_profile_for_storage(row) for row in rows]
        else:
            out[owner_key] = rows
    return out


def hydrate_char_profiles_from_storage(profiles: Any) -> Any:
    """Inverse of dedupe_char_profiles_for_storage."""
    if not isinstance(profiles, dict):
        return profiles
    out: dict[str, Any] = {}
    for owner_key, rows in profiles.items():
        if isinstance(rows, list):
            out[owner_key] = [hydrate_profile_from_storage(row) for row in rows]
        else:
            out[owner_key] = rows
    return out


def char_profiles_roundtrip_ok(hydrated: Any) -> bool:
    """True iff dedupe → JSON → hydrate reproduces the hydrated input exactly."""
    try:
        deduped = dedupe_char_profiles_for_storage(hydrated)
        rehydrated = hydrate_char_profiles_from_storage(json.loads(json.dumps(deduped, default=str)))
        return json.dumps(rehydrated, sort_keys=True, default=str) == json.dumps(
            json.loads(json.dumps(hydrated, default=str)), sort_keys=True, default=str
        )
    except Exception:
        logger.exception("[char_profile_dedup] roundtrip verification crashed")
        return False
