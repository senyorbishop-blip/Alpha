#!/usr/bin/env python3
"""Audit the char_profiles dedup migration against a COPY of a campaign DB.

For every profile (or one selected profile), this dumps the fully-hydrated
JSON before and after the dedup storage transform and verifies they are
semantically identical — the dedup changes storage layout, never content.

Usage (always point it at a copy, never the live DB):

    cp ~/.casual-dnd/campaigns.db /tmp/campaigns-copy.db
    python scripts/audit_char_profile_dedup.py --db /tmp/campaigns-copy.db
    python scripts/audit_char_profile_dedup.py --db /tmp/campaigns-copy.db \
        --owner sezzypantz --profile pdf-seraphine-crowe --dump-dir /tmp/audit

Exit code 0 = every audited profile round-trips identically.
"""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import sqlite3
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from server.character.profile_dedup import (  # noqa: E402
    dedupe_char_profiles_for_storage,
    hydrate_char_profiles_from_storage,
)


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, indent=1, default=str)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to a COPY of campaigns.db")
    parser.add_argument("--campaign", default="", help="Restrict to one campaign id")
    parser.add_argument("--owner", default="", help="Restrict to one owner key")
    parser.add_argument("--profile", default="", help="Restrict to one profile id")
    parser.add_argument("--dump-dir", default="", help="Write before/after hydrated JSON dumps here")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if args.campaign:
        rows = conn.execute("SELECT id, char_profiles FROM campaigns WHERE id=?", (args.campaign,)).fetchall()
    else:
        rows = conn.execute("SELECT id, char_profiles FROM campaigns").fetchall()

    dump_dir = pathlib.Path(args.dump_dir) if args.dump_dir else None
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)

    audited = mismatches = 0
    for row in rows:
        raw = row["char_profiles"]
        if not raw or raw in ("{}", "null"):
            continue
        stored = json.loads(raw)
        hydrated_before = hydrate_char_profiles_from_storage(stored)
        deduped = dedupe_char_profiles_for_storage(hydrated_before)
        # Simulate the real persistence round trip: serialize + reload + hydrate.
        hydrated_after = hydrate_char_profiles_from_storage(json.loads(json.dumps(deduped, default=str)))

        raw_after = json.dumps(deduped, separators=(",", ":"), default=str)
        print(f"campaign={row['id']} stored_bytes {len(raw)} -> {len(raw_after)} "
              f"(saved {len(raw) - len(raw_after)})")

        for owner_key, profiles in (hydrated_before or {}).items():
            if args.owner and str(owner_key) != args.owner:
                continue
            if not isinstance(profiles, list):
                continue
            after_bucket = (hydrated_after or {}).get(owner_key) or []
            for idx, before_profile in enumerate(profiles):
                if not isinstance(before_profile, dict):
                    continue
                pid = str(before_profile.get("id") or f"index-{idx}")
                if args.profile and pid != args.profile:
                    continue
                audited += 1
                after_profile = after_bucket[idx] if idx < len(after_bucket) else None
                before_text = _canon(before_profile)
                after_text = _canon(after_profile)
                label = f"{row['id']}/{owner_key}/{pid}"
                if dump_dir:
                    safe = label.replace("/", "__")
                    (dump_dir / f"{safe}.before.json").write_text(before_text)
                    (dump_dir / f"{safe}.after.json").write_text(after_text)
                if before_text != after_text:
                    mismatches += 1
                    print(f"  MISMATCH {label}")
                    diff = difflib.unified_diff(
                        before_text.splitlines(), after_text.splitlines(),
                        fromfile=f"{label} before", tofile=f"{label} after", lineterm="", n=2,
                    )
                    for i, line in enumerate(diff):
                        if i > 60:
                            print("   ... (diff truncated)")
                            break
                        print("  " + line)
                else:
                    print(f"  ok {label} ({len(before_text)} bytes hydrated)")

    print(f"\naudited_profiles={audited} mismatches={mismatches}")
    return 1 if mismatches or not audited else 0


if __name__ == "__main__":
    raise SystemExit(main())
