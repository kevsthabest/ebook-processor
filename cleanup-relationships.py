#!/usr/bin/env python3
"""Clean up contradictory relationships in book_characters.

Applies the same contradiction filter as the v2.5.8 pipeline:
- parent/child are directional; only one per pair survives (first seen wins)
- Removes (A->B, child) if (A->B, parent) exists, and vice versa
- Removes (B->A, parent) if (A->B, parent) exists (same relation, flipped)

Usage:
    py cleanup-relationships.py --dry-run    # show what would change
    py cleanup-relationships.py              # apply changes

Requires config.json with supabase_url and supabase_key.
"""
import json
import sys
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG = {}


def load_config():
    global CONFIG
    cfg_path = SCRIPT_DIR / "config.json"
    if cfg_path.exists():
        CONFIG = json.loads(cfg_path.read_text(encoding="utf-8"))
    else:
        print("config.json not found")
        sys.exit(1)


def sb(table, method="GET", data=None, params=""):
    url = f"{CONFIG['supabase_url']}/rest/v1/{table}{params}"
    headers = {
        "apikey": CONFIG["supabase_key"],
        "Authorization": f"Bearer {CONFIG['supabase_key']}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8") or "[]")
    except Exception as e:
        print(f"  Supabase {table} error: {e}")
        return None


def clean_relationships(rels):
    """Remove contradictory parent/child relationships. Returns (cleaned, removed_count)."""
    seen = set()
    cleaned = []
    removed = 0
    for r in rels:
        frm = (r.get("from") or "").strip()
        to = (r.get("to") or "").strip()
        rtype = (r.get("type") or "").strip().lower()
        if not frm or not to:
            cleaned.append(r)
            continue
        if rtype in ("parent", "child"):
            contra = {
                (frm.lower(), to.lower(), "parent"),
                (frm.lower(), to.lower(), "child"),
                (to.lower(), frm.lower(), "parent"),
                (to.lower(), frm.lower(), "child"),
            }
            if contra & seen:
                removed += 1
                continue
            seen.add((frm.lower(), to.lower(), rtype))
        else:
            key = (frm.lower(), to.lower(), rtype)
            if key in seen:
                removed += 1
                continue
            seen.add(key)
        cleaned.append(r)
    return cleaned, removed


def main():
    dry_run = "--dry-run" in sys.argv
    load_config()

    print("Fetching book_characters with relationships...")
    rows = sb("book_characters", params="?select=id,name,work_id,relationships&limit=10000")
    if rows is None:
        print("Failed to fetch")
        return

    total_removed = 0
    total_updated = 0
    for row in rows:
        rels = row.get("relationships") or []
        if not rels:
            continue
        cleaned, removed = clean_relationships(rels)
        if removed > 0:
            total_removed += removed
            total_updated += 1
            print(f"  {row.get('name')} ({row.get('id', '')[:8]}): "
                  f"{len(rels)} -> {len(cleaned)} relationships ({removed} contradictions removed)")
            if not dry_run:
                r = sb("book_characters", method="PATCH",
                       params=f"?id=eq.{row['id']}",
                       data={"relationships": cleaned})
                if r is None:
                    print(f"    FAILED to update")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done: {total_updated} characters updated, "
          f"{total_removed} contradictory relationships removed")


if __name__ == "__main__":
    main()
