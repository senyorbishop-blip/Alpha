"""Reconnect delta filtering for state_sync payloads.

The bridge and game clients reconnect often, and until now every reconnect
re-shipped the complete role-filtered state snapshot. This module lets a
reconnecting client report what it already has, and strips the heavy
domains that have not changed since.

How a domain is judged unchanged (two independent proofs, either suffices):

1. Counter match — for domains guarded by an existing monotonic server
   counter that clients already track live from push messages
   (`token_state_revision`, `visibility_revision`, `inventory_revision`).
   A client that saw every push holds the current counter value, so a
   mid-combat reconnect skips tokens/fog/inventory even though its stored
   content CRC (from the last full sync) is long stale.
2. Content CRC match — a checksum of the exact role-filtered domain
   content that WOULD be sent. This needs no bump-site bookkeeping at all
   (any server-side mutation changes the bytes), so it can never wrongly
   skip a changed domain; at worst a stale client CRC causes a harmless
   re-send.

Domains omitted from the payload are listed in ``omitted_domains``; the
full ``domain_revisions`` map is always included so the client can seed its
store for the next reconnect. Everything not listed in DELTA_DOMAIN_KEYS
(users, log, combat, journal, quests, handouts, nav/meta fields …) is always
sent — those are the small, hard-to-track parts of the payload.
"""
from __future__ import annotations

import json
import logging
import zlib
from typing import Any

logger = logging.getLogger(__name__)

# payload keys belonging to each delta-capable domain. A domain is omitted
# atomically — either every key ships or none — because client apply code
# reads them together (e.g. applyPlayerInventoryState resets party_loot_log).
DELTA_DOMAIN_KEYS: dict[str, tuple[str, ...]] = {
    "tokens": ("tokens",),
    "fog": ("fog_maps",),
    "editor": (
        "editor_layers", "editor_walls", "editor_props", "editor_paths",
        "editor_labels", "editor_markers", "editor_lights", "map_settings",
    ),
    "char_profiles": ("char_profiles",),
    "inventory": (
        "player_inventories", "player_inventory", "party_stash",
        "player_gold", "party_loot_log",
    ),
}

# domain -> (state field holding the server counter, known_revisions field the
# client echoes from its live trackers)
_DOMAIN_COUNTERS: dict[str, tuple[str, str]] = {
    "tokens": ("token_state_revision", "token_state_revision"),
    "fog": ("visibility_revision", "visibility_revision"),
    "inventory": ("inventory_revision", "inventory_revision"),
}


def domain_crc(state: dict, domain: str) -> int:
    keys = DELTA_DOMAIN_KEYS.get(domain, ())
    try:
        raw = json.dumps([state.get(key) for key in keys], sort_keys=True,
                         separators=(",", ":"), default=str)
    except Exception:
        return 0
    return (zlib.crc32(raw.encode("utf-8", errors="ignore")) & 0xFFFFFFFF) or 1


def compute_domain_revisions(state: dict) -> dict[str, int]:
    """Per-recipient content revisions for every delta-capable domain."""
    return {domain: domain_crc(state, domain) for domain in DELTA_DOMAIN_KEYS}


def _known_int(known: dict, key: str) -> int:
    try:
        value = int(known.get(key))
    except Exception:
        return 0
    return value if value > 0 else 0


def apply_reconnect_delta(state: dict, known_revisions: Any) -> dict:
    """Strip unchanged heavy domains from a role-filtered state dict, in place.

    ``known_revisions`` is the client-reported map: per-domain content CRCs
    (echoed from the previous sync's ``domain_revisions``) plus live-tracked
    counters. Returns the state dict with ``domain_revisions``,
    ``omitted_domains`` and ``delta`` fields populated.
    """
    revisions = compute_domain_revisions(state)
    state["domain_revisions"] = revisions
    known = known_revisions if isinstance(known_revisions, dict) else None
    state["delta"] = bool(known)
    state["omitted_domains"] = []
    if not known:
        return state

    omitted: list[str] = []
    for domain, crc in revisions.items():
        crc_match = _known_int(known, domain) == crc
        counter_match = False
        counter = _DOMAIN_COUNTERS.get(domain)
        if counter:
            state_field, known_field = counter
            server_value = int(state.get(state_field) or 0)
            counter_match = server_value > 0 and _known_int(known, known_field) == server_value
        if crc_match or counter_match:
            for key in DELTA_DOMAIN_KEYS[domain]:
                state.pop(key, None)
            omitted.append(domain)
    state["omitted_domains"] = omitted
    return state
