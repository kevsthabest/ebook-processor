#!/usr/bin/env python3
"""Tier 3 local store for the ebook processor programme.

This is a standalone lab tool, not part of the shipped pipeline.
Nothing in process-portable.py or portal.py imports it, it carries
no version of its own, and it never writes to production: it reads
preview artefacts and writes only inside its own SQLite database
on the User's machine.

It exists because of a direction the User set on 2026-10-18: the
roster remainder (Tier 3 in the tiered-storage model) lives LOCAL
only, never in prod by default, and every unconfirmed candidate a
run produces is saved with it instead of being discarded, so he can
review the list and push the acceptable items himself. The design
is specified in the Tier 3 local store design note
(2026-10-18), under the curation-console brief and its tiers
addendum.

Three tables:

  tier3_characters   one row per (work, roster character who was
                     NOT in the run's stored prod set): the
                     character as the run recorded them, including
                     their full claim set — judged, unjudged and
                     dropped claims alike.
  tier3_candidates   one row per unconfirmed item the run left
                     behind: possible merges the judge scored but
                     did not take, claims that never shipped,
                     unresolved mentions, and relationship types
                     the extractor proposed that are not in the
                     run's vocabulary (found in the per-chapter
                     deltas — a type that appears there but in no
                     final claim list and not in the built-in
                     vocabulary).
  push_ledger        the User's push decisions (work, name,
                     decided-at). This is his curation data: an
                     import NEVER modifies it, so decisions survive
                     re-runs and re-imports.

The push itself does not happen here. Marking a character as
"other cast" in prod needs the tier representation the
curation-console programme is designing (a book_characters change
is a migration, and migrations stop for the User's explicit
approval). This tool records the decisions the moment he makes
them; the console build executes them later. `approve` therefore
writes only to the local ledger.

`alerts` is the digest the User asked for on 2026-10-18: the
results that scored below a certain score — possible but
unconfirmed, for whatever reason they failed to confirm. It is a
read-only view over what the store already holds: scored claim
candidates that did not ship, merge candidates in the ambiguous
band, and scored claims inside Tier 3 characters' recorded claim
sets that did not ship as validated (a family claim downgraded to
`other` on a mid-band draw is the canonical case). The floor is
configurable (default 0.30) and the digest sorts by score
descending, closest calls first, each entry carrying the state
or reason that left it unconfirmed. Scores are read out of the
recorded payloads at alert time; the digest never writes.

The stored prod set for a work — the names excluded from Tier 3 —
is resolved in this order:

  1. --stored-names (a file, one name per line) always wins.
  2. Supabase book_characters for the artefact's work, read with
     the processor config's own credentials, when a config with
     supabase_url/supabase_key is present. A config that is present
     but whose query fails is a loud error, never a silent
     fallback — deriving a different stored set than prod reality
     would corrupt the review list.
  3. Derivation from the artefact itself: the principals (its
     `characters` list) plus claim-earners under the storage rule
     the shipped processor applies (a minor character who sources
     a shipped claim the validator actually scored: state
     `validated`, or `review_required` carrying a non-null
     validation_p). If the artefact lacks the claim lists this
     derivation needs, the import fails loudly rather than
     guessing.

Usage:
    python3 tier3_store.py import --preview "preview/<book>.json"
    python3 tier3_store.py list [--work <id-or-title>]
    python3 tier3_store.py alerts [--work <id-or-title>]
                                  [--floor 0.30]
    python3 tier3_store.py approve --work <w> --name <n>
    python3 tier3_store.py unapprove --work <w> --name <n>
    python3 tier3_store.py report

Stdlib only, Python 3.8+, Windows and Linux: the User runs this
from the repo root on his own machine, where his preview
artefacts and his config live. The database defaults to
~/.ebook-processor/tier3.db, beside the learned state.
"""
import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = Path.home() / ".ebook-processor" / "tier3.db"
DEFAULT_CONFIG = _SCRIPT_DIR / "config.json"

# The processor's built-in relationship vocabulary, mirrored from
# _BUILTIN_REL_TYPES in process-portable.py (the fallback set the
# Supabase registry was seeded from). It is used for exactly one
# judgement: a type proposed in a chapter delta counts as an
# unregistered proposal only when it is neither in this list nor
# used by any of the run's final claim lists — a type the run
# itself shipped or judged is registered for that run by
# definition, whatever list it came from.
BUILTIN_REL_TYPES = frozenset((
    "spouse", "parent", "child", "sibling", "romantic", "friend",
    "colleague", "enemy", "mentor", "rival", "ally", "neighbor",
    "other", "bonded", "mate", "sire", "packmate", "crewmate",
    "sworn", "step_parent", "step_child", "ex", "handler",
))

CLAIM_LIST_KEYS = ("relationships", "minor_sourced_relationships",
                   "dropped_relationships")


class Tier3Error(Exception):
    """A user-facing failure: bad input, an indeterminable stored
    set, an unknown work. Printed as one line; no traceback for
    expected cases."""


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Database. The push ledger is created alongside the rest but is
# only ever written by approve/unapprove: imports replace a book's
# characters and candidates inside one transaction and never
# reference the ledger at all, which is what makes "imports never
# modify the ledger" a structural fact rather than a promise.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tier3_books (
    work_id TEXT PRIMARY KEY,
    title TEXT,
    author TEXT,
    run_key TEXT,
    stored_set_source TEXT,
    stored_names_json TEXT,
    roster_count INTEGER,
    imported_at TEXT
);
CREATE TABLE IF NOT EXISTS tier3_characters (
    work_id TEXT NOT NULL,
    name TEXT NOT NULL,
    aliases_json TEXT,
    description TEXT,
    role TEXT,
    appearances INTEGER,
    chapters_present INTEGER,
    grounded INTEGER,
    claims_json TEXT,
    run_key TEXT,
    imported_at TEXT,
    PRIMARY KEY (work_id, name)
);
CREATE TABLE IF NOT EXISTS tier3_candidates (
    work_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT,
    payload_json TEXT,
    run_key TEXT,
    imported_at TEXT
);
CREATE INDEX IF NOT EXISTS tier3_candidates_work_idx
    ON tier3_candidates (work_id, kind);
CREATE TABLE IF NOT EXISTS push_ledger (
    work_id TEXT NOT NULL,
    name TEXT NOT NULL,
    decided_at TEXT,
    pushed_at TEXT,
    prod_row_id TEXT,
    PRIMARY KEY (work_id, name)
);
"""


def _connect(db_path):
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Artefact reading. A preview artefact carries the roster as two
# lists (characters = the principals, minor_characters = the rest)
# and the run's claims as three (relationships,
# minor_sourced_relationships, dropped_relationships). Everything
# the store knows comes from those lists and from chapter_data's
# per-chapter deltas; when a list the importer needs is absent the
# import says so instead of approximating.
# ---------------------------------------------------------------------------

def _load_artefact(preview_path):
    path = Path(preview_path)
    if not path.is_file():
        raise Tier3Error(f"preview artefact not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise Tier3Error(f"preview artefact is not valid JSON: "
                         f"{path} ({e})")
    if not isinstance(data, dict):
        raise Tier3Error(f"preview artefact is not a JSON object: "
                         f"{path}")
    return data


def _roster(artefact):
    """name -> character dict, principals first. Raises when the
    artefact carries no roster lists at all."""
    if "characters" not in artefact and \
            "minor_characters" not in artefact:
        raise Tier3Error(
            "artefact carries neither a characters nor a "
            "minor_characters list — the roster, and therefore "
            "Tier 3, cannot be determined from it")
    roster = {}
    for key in ("characters", "minor_characters"):
        for entry in artefact.get(key) or []:
            if isinstance(entry, dict) and entry.get("name"):
                roster.setdefault(entry["name"], entry)
    if not roster:
        raise Tier3Error("artefact's roster lists are empty — "
                         "nothing to import")
    return roster


def _claims_by_source(artefact):
    """from-name -> [claim dicts as recorded], across all three
    claim lists. Claims whose source is a name form with no roster
    entry simply attach nowhere; they still surface as candidates
    when dropped."""
    by_source = {}
    for key in CLAIM_LIST_KEYS:
        for claim in artefact.get(key) or []:
            if isinstance(claim, dict) and claim.get("from"):
                by_source.setdefault(claim["from"], []).append(claim)
    return by_source


# ---------------------------------------------------------------------------
# The stored prod set. Resolution order is documented in the
# header: explicit names file, then Supabase, then derivation from
# the artefact under the shipped storage rule.
# ---------------------------------------------------------------------------

def _supabase_stored_names(config_path, work_id):
    """Names stored in prod book_characters for this work, or
    None when no usable config exists. A present-but-failing
    query raises: falling back silently would derive a stored set
    that may not match prod, and the review list is only worth
    anything if its exclusions are true."""
    path = Path(config_path)
    if not path.exists():
        return None
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    url = (cfg.get("supabase_url") or "").rstrip("/")
    key = cfg.get("supabase_key") or ""
    if not url or not key:
        return None
    req = urllib.request.Request(
        f"{url}/rest/v1/book_characters"
        f"?select=name&work_id=eq.{work_id}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise Tier3Error(
            f"Supabase stored-set query failed (HTTP {e.code}) — "
            f"refusing to fall back to derivation, because the "
            f"stored set must match prod. Fix the config or pass "
            f"--stored-names.")
    except urllib.error.URLError as e:
        raise Tier3Error(
            f"Supabase stored-set query unreachable ({e.reason}) "
            f"— refusing to fall back to derivation. Fix the "
            f"connection or pass --stored-names.")
    return sorted({r["name"] for r in rows if r.get("name")})


def _derive_stored_names(artefact):
    """Principals plus claim-earners, from the artefact's own
    records, under the storage rule the shipped processor
    applies: a non-principal earns a stored row by sourcing a
    shipped claim the validator actually scored — validation
    state `validated`, or `review_required` carrying a non-null
    validation_p (review_required alone also collects claims
    nobody judged, which is why the probability is the proof)."""
    for key in CLAIM_LIST_KEYS:
        if key not in artefact:
            raise Tier3Error(
                f"artefact lacks the {key} list — the claim-"
                f"earner half of the stored set cannot be derived "
                f"from it. Read the stored set from Supabase or "
                f"pass --stored-names.")
    stored = {c["name"] for c in artefact.get("characters") or []
              if isinstance(c, dict) and c.get("name")}
    shipped = (artefact.get("relationships") or []) + \
        (artefact.get("minor_sourced_relationships") or [])
    for claim in shipped:
        if not isinstance(claim, dict) or not claim.get("from"):
            continue
        state = claim.get("validation_state")
        if state == "validated" or (
                state == "review_required"
                and claim.get("validation_p") is not None):
            stored.add(claim["from"])
    return sorted(stored)


def _resolve_stored_set(artefact, work_id, stored_names_path,
                        config_path):
    """Returns (names, source) — source is 'stored-names',
    'supabase' or 'derived', and is recorded on the book row so a
    later reader knows how much authority the exclusion set has."""
    if stored_names_path:
        path = Path(stored_names_path)
        if not path.is_file():
            raise Tier3Error(f"stored-names file not found: {path}")
        names = sorted({
            line.strip() for line in
            path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")})
        if not names:
            raise Tier3Error(f"stored-names file is empty: {path}")
        return names, "stored-names"
    names = _supabase_stored_names(config_path, work_id)
    if names is not None:
        return names, "supabase"
    return _derive_stored_names(artefact), "derived"


# ---------------------------------------------------------------------------
# Candidates. Four kinds, each the record of something the run
# produced but did not confirm. Payloads are the artefact's own
# dicts wherever the artefact has one — the store's job is to keep
# them reviewable, not to reinterpret them.
# ---------------------------------------------------------------------------

def _candidates(artefact):
    """Yields (kind, state, payload) for one artefact."""
    # Merges the judge scored but the run did not take; the
    # recorded reason ("below threshold (0.85)") is the state that
    # left the pair unconfirmed.
    for entry in artefact.get("possible_merges") or []:
        if isinstance(entry, dict):
            yield ("merge", entry.get("reason") or "left_for_review",
                   entry)

    # Claims that never shipped: the dropped list, plus any
    # rejected-state claim recorded in the shipped lists (the two
    # can overlap in principle, so dedupe on the claim identity).
    seen = set()

    def _claim_key(c):
        return (c.get("from"), c.get("to"), c.get("type"),
                c.get("chapter"))

    for key in CLAIM_LIST_KEYS:
        for claim in artefact.get(key) or []:
            if not isinstance(claim, dict):
                continue
            is_dropped = key == "dropped_relationships" or \
                claim.get("validation_state") == "rejected"
            if is_dropped and _claim_key(claim) not in seen:
                seen.add(_claim_key(claim))
                state = claim.get("validation_note") or \
                    claim.get("validation_state") or "dropped"
                yield ("claim", state, claim)

    # Unresolved mentions, from every roster character (a
    # principal's unresolved mentions are review items too — the
    # candidates belong to the work, not to a tier). Context is
    # the owning character and their grounding evidence, which is
    # the context the artefact records for a mention.
    roster = {}
    for key in ("characters", "minor_characters"):
        for entry in artefact.get(key) or []:
            if isinstance(entry, dict) and entry.get("name"):
                roster.setdefault(entry["name"], entry)
    for name, entry in roster.items():
        for mention in entry.get("unresolved_mentions") or []:
            yield ("mention", "unresolved", {
                "character": name,
                "mention": mention,
                "character_evidence": entry.get("evidence"),
            })

    # Relationship types the extractor proposed that the run
    # dropped as unregistered. They survive only in the
    # per-chapter deltas: a delta claim whose type appears in no
    # final claim list and is outside the built-in vocabulary.
    # Without chapter_data there is no record to draw from, and
    # the import says so rather than emitting none in silence.
    chapters = ((artefact.get("chapter_data") or {})
                .get("chapters") or {})
    if not chapters:
        return
    final_types = set()
    for key in CLAIM_LIST_KEYS:
        for claim in artefact.get(key) or []:
            if isinstance(claim, dict) and claim.get("type"):
                final_types.add(claim["type"])
    registered = BUILTIN_REL_TYPES | final_types
    seen = set()
    for chnum, chapter in chapters.items():
        deltas = ((chapter or {}).get("a") or {}) \
            .get("relationships") or []
        for claim in deltas:
            if not isinstance(claim, dict):
                continue
            ctype = claim.get("type")
            if not ctype or ctype in registered:
                continue
            ident = (claim.get("from"), claim.get("to"), ctype)
            if ident in seen:
                continue
            seen.add(ident)
            payload = dict(claim)
            payload["chapter_number"] = chnum
            payload["detection"] = (
                "type appears in the run's chapter deltas but in "
                "none of its final claim lists, and is outside "
                "the built-in relationship vocabulary")
            yield ("type_proposal", "unknown_type", payload)


# ---------------------------------------------------------------------------
# Import. Replaces one book's characters and candidates inside a
# single transaction; the push ledger is not referenced here at
# all. The book row records how the stored set was resolved, so
# the report can reconcile against it later.
# ---------------------------------------------------------------------------

def import_preview(db_path, preview_path, stored_names_path=None,
                   run_key=None, config_path=DEFAULT_CONFIG):
    artefact = _load_artefact(preview_path)
    roster = _roster(artefact)
    work_id = artefact.get("work_id") or \
        f"title:{artefact.get('title') or Path(preview_path).stem}"
    title = artefact.get("title") or Path(preview_path).stem
    run_key = run_key or Path(preview_path).stem
    stored_names, source = _resolve_stored_set(
        artefact, work_id, stored_names_path, config_path)
    stored_set = set(stored_names)
    tier3_names = sorted(set(roster) - stored_set)
    absent = sorted(stored_set - set(roster))
    by_source = _claims_by_source(artefact)
    now = _now_iso()

    conn = _connect(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM tier3_characters "
                         "WHERE work_id = ?", (work_id,))
            conn.execute("DELETE FROM tier3_candidates "
                         "WHERE work_id = ?", (work_id,))
            for name in tier3_names:
                entry = roster[name]
                conn.execute(
                    "INSERT INTO tier3_characters (work_id, name, "
                    "aliases_json, description, role, appearances, "
                    "chapters_present, grounded, claims_json, "
                    "run_key, imported_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (work_id, name,
                     json.dumps(entry.get("aliases") or []),
                     entry.get("description"),
                     entry.get("role"),
                     entry.get("appearances"),
                     entry.get("chapters_present"),
                     None if entry.get("grounded") is None
                     else int(bool(entry.get("grounded"))),
                     json.dumps(by_source.get(name, [])),
                     run_key, now))
            cand_counts = {}
            for kind, state, payload in _candidates(artefact):
                conn.execute(
                    "INSERT INTO tier3_candidates (work_id, kind, "
                    "state, payload_json, run_key, imported_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (work_id, kind, state, json.dumps(payload),
                     run_key, now))
                cand_counts[kind] = cand_counts.get(kind, 0) + 1
            conn.execute(
                "INSERT INTO tier3_books (work_id, title, author, "
                "run_key, stored_set_source, stored_names_json, "
                "roster_count, imported_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(work_id) DO UPDATE SET "
                "title=excluded.title, author=excluded.author, "
                "run_key=excluded.run_key, "
                "stored_set_source=excluded.stored_set_source, "
                "stored_names_json=excluded.stored_names_json, "
                "roster_count=excluded.roster_count, "
                "imported_at=excluded.imported_at",
                (work_id, title, artefact.get("author"), run_key,
                 source, json.dumps(stored_names), len(roster), now))
    finally:
        conn.close()

    return {
        "work_id": work_id, "title": title, "run_key": run_key,
        "roster_count": len(roster),
        "stored_source": source, "stored_count": len(stored_names),
        "stored_absent_from_roster": absent,
        "tier3_count": len(tier3_names),
        "candidates": cand_counts,
        "has_chapter_data": bool(
            (artefact.get("chapter_data") or {}).get("chapters")),
    }


# ---------------------------------------------------------------------------
# Work resolution for list/approve: an exact work_id, or a
# case-insensitive title substring that matches exactly one book.
# Ambiguity and absence are both loud — an approval recorded
# against the wrong book would be worse than no approval.
# ---------------------------------------------------------------------------

def _resolve_work(conn, work):
    rows = conn.execute("SELECT work_id, title FROM tier3_books") \
        .fetchall()
    for row in rows:
        if row["work_id"] == work:
            return row["work_id"]
    matches = [r for r in rows
               if work.lower() in (r["title"] or "").lower()]
    if len(matches) == 1:
        return matches[0]["work_id"]
    if not matches:
        raise Tier3Error(
            f"no book in the store matches {work!r} — import its "
            f"preview first (books present: "
            f"{', '.join(r['title'] or r['work_id'] for r in rows) or 'none'})")
    raise Tier3Error(
        f"{work!r} matches more than one book: "
        f"{', '.join(m['title'] for m in matches)} — use a work id")


def _book_lines(conn, work_id):
    book = conn.execute(
        "SELECT * FROM tier3_books WHERE work_id = ?",
        (work_id,)).fetchone()
    chars = conn.execute(
        "SELECT name, appearances FROM tier3_characters "
        "WHERE work_id = ? ORDER BY appearances DESC, name",
        (work_id,)).fetchall()
    cand = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM tier3_candidates "
        "WHERE work_id = ? GROUP BY kind ORDER BY kind",
        (work_id,)).fetchall()
    ledger = {r["name"] for r in conn.execute(
        "SELECT name FROM push_ledger WHERE work_id = ?",
        (work_id,))}
    lines = [
        f"{book['title']}  ({book['work_id']})",
        f"  run: {book['run_key']}  ·  stored set via "
        f"{book['stored_set_source']} ({len(json.loads(book['stored_names_json']))} names)",
        f"  Tier 3: {len(chars)} character(s)",
    ]
    if cand:
        lines.append("  candidates: " + ", ".join(
            f"{r['kind']} {r['n']}" for r in cand))
    else:
        lines.append("  candidates: none")
    for row in chars:
        mark = "  [approved for push]" if row["name"] in ledger \
            else ""
        lines.append(f"    {row['name']} — "
                     f"{row['appearances']} appearance(s){mark}")
    return lines


def cmd_import(args):
    result = import_preview(
        args.db, args.preview, stored_names_path=args.stored_names,
        run_key=args.run_key, config_path=args.config)
    print(f"imported {result['title']} ({result['work_id']})")
    print(f"  run key: {result['run_key']}")
    print(f"  roster: {result['roster_count']}  ·  stored set: "
          f"{result['stored_count']} via {result['stored_source']}  "
          f"·  Tier 3: {result['tier3_count']}")
    if result["stored_absent_from_roster"]:
        print(f"  WARNING: {len(result['stored_absent_from_roster'])} "
              f"stored name(s) absent from the roster: "
              f"{', '.join(result['stored_absent_from_roster'])} — "
              f"the report's reconciliation will fail for this book "
              f"until that is explained")
    cand = result["candidates"]
    if cand:
        print("  candidates: " + ", ".join(
            f"{k} {v}" for k, v in sorted(cand.items())))
    else:
        print("  candidates: none")
    if not result["has_chapter_data"]:
        print("  note: artefact carries no chapter_data — dropped "
              "type proposals cannot be determined from it, so "
              "none were recorded")
    return 0


def cmd_list(args):
    conn = _connect(args.db)
    try:
        if args.work:
            work_ids = [_resolve_work(conn, args.work)]
        else:
            work_ids = [r["work_id"] for r in conn.execute(
                "SELECT work_id FROM tier3_books ORDER BY title")]
        if not work_ids:
            print("the store is empty — import a preview first")
            return 0
        for i, work_id in enumerate(work_ids):
            if i:
                print()
            print("\n".join(_book_lines(conn, work_id)))
        return 0
    finally:
        conn.close()


def cmd_approve(args):
    conn = _connect(args.db)
    try:
        work_id = _resolve_work(conn, args.work)
        row = conn.execute(
            "SELECT name FROM tier3_characters "
            "WHERE work_id = ? AND name = ?",
            (work_id, args.name)).fetchone()
        if row is None:
            near = [r["name"] for r in conn.execute(
                "SELECT name FROM tier3_characters "
                "WHERE work_id = ?", (work_id,))
                if args.name.lower() in r["name"].lower()]
            hint = f" (close: {', '.join(near)})" if near else ""
            raise Tier3Error(
                f"{args.name!r} is not a Tier 3 character for "
                f"this work{hint} — approvals are recorded only "
                f"for names on the Tier 3 list")
        with conn:
            conn.execute(
                "INSERT INTO push_ledger (work_id, name, "
                "decided_at, pushed_at, prod_row_id) "
                "VALUES (?,?,?,NULL,NULL) "
                "ON CONFLICT(work_id, name) DO UPDATE SET "
                "decided_at=excluded.decided_at",
                (work_id, args.name, _now_iso()))
        print(f"approved for push: {args.name} ({work_id}) — "
              f"recorded in the local ledger; the push itself "
              f"executes with the console build")
        return 0
    finally:
        conn.close()


def cmd_unapprove(args):
    conn = _connect(args.db)
    try:
        work_id = _resolve_work(conn, args.work)
        with conn:
            cur = conn.execute(
                "DELETE FROM push_ledger "
                "WHERE work_id = ? AND name = ?",
                (work_id, args.name))
        if cur.rowcount == 0:
            raise Tier3Error(
                f"{args.name!r} has no push approval recorded "
                f"for this work — nothing to remove")
        print(f"push approval removed: {args.name} ({work_id})")
        return 0
    finally:
        conn.close()


def build_report(db_path):
    conn = _connect(db_path)
    try:
        books = conn.execute(
            "SELECT * FROM tier3_books ORDER BY title").fetchall()
        total_chars = conn.execute(
            "SELECT COUNT(*) AS n FROM tier3_characters") \
            .fetchone()["n"]
        cand_totals = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM tier3_candidates "
            "GROUP BY kind ORDER BY kind").fetchall()
        ledger_n = conn.execute(
            "SELECT COUNT(*) AS n FROM push_ledger") \
            .fetchone()["n"]
        lines = [
            "Tier 3 local store — report",
            f"  books: {len(books)}  ·  Tier 3 characters: "
            f"{total_chars}  ·  push ledger: {ledger_n} "
            f"decision(s)",
        ]
        if cand_totals:
            lines.append("  candidates: " + ", ".join(
                f"{r['kind']} {r['n']}" for r in cand_totals))
        else:
            lines.append("  candidates: none")
        verdicts = []
        for book in books:
            stored = json.loads(book["stored_names_json"])
            tier3_n = conn.execute(
                "SELECT COUNT(*) AS n FROM tier3_characters "
                "WHERE work_id = ?",
                (book["work_id"],)).fetchone()["n"]
            rhs = len(stored) + tier3_n
            ok = book["roster_count"] == rhs
            verdicts.append(ok)
            lines.append(
                f"  {book['title']}: roster "
                f"{book['roster_count']} == stored "
                f"{len(stored)} + Tier 3 {tier3_n} = {rhs} — "
                f"{'PASS' if ok else 'FAIL'}")
        lines.append(
            "verdict: " + ("RECONCILED" if all(verdicts)
                           else "NOT RECONCILED"))
        return "\n".join(lines)
    finally:
        conn.close()


def cmd_report(args):
    print(build_report(args.db))
    return 0


# ---------------------------------------------------------------------------
# Alerts — the digest the User asked for (2026-10-18): results
# below a certain score, possible but unconfirmed for whatever
# reason they failed to confirm. Three sources already in the
# store: scored claim candidates (they did not ship by
# construction), merge candidates in the ambiguous band, and
# scored claims inside Tier 3 characters' recorded claim sets
# that did not ship as validated. Scores live inside the
# recorded payloads — validation_p on a claim, p_yes on a merge
# — and are read out at alert time; the candidates table
# deliberately grows no score column for this. The digest opens
# the database read-only and never writes.
# ---------------------------------------------------------------------------

# The processor's auto-merge threshold: a scored pair at or
# above it merges, so the ambiguous band for merges is the
# stretch between the floor and this. (Artefact merge reasons
# name the same number, "below threshold (0.85)".)
MERGE_AUTO_THRESHOLD = 0.85

DEFAULT_ALERT_FLOOR = 0.30


def _connect_readonly(db_path):
    path = Path(db_path)
    if not path.is_file():
        raise Tier3Error(
            "no Tier 3 store at that path — import a preview "
            "first")
    conn = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _alert_claim_key(claim):
    """Claim identity for dedupe — the same fields the importer
    dedupes candidates on, so a claim recorded both as a
    candidate and inside its source's claim set alerts once."""
    return (claim.get("from"), claim.get("to"), claim.get("type"),
            claim.get("chapter"))


def _shipped_confirmed(claim):
    """True when a recorded claim shipped as validated under its
    claimed type. A downgrade also lands in state `validated`,
    but with its note reading "downgraded" and its type
    rewritten to `other` — the claimed type never confirmed, so
    it does not count as shipped. A claim recorded as type
    `other` with no "kept" note is read the same way: nothing
    in the record shows it confirming as anything specific."""
    if claim.get("validation_state") != "validated":
        return False
    if claim.get("validation_note") == "downgraded":
        return False
    if claim.get("type") == "other" and \
            claim.get("validation_note") != "kept":
        return False
    return True


def collect_alerts(db_path, work=None, floor=DEFAULT_ALERT_FLOOR):
    """The alert digest as data: one dict per alert, grouped by
    book (title order), each book's alerts sorted by score
    descending — closest calls first. Read-only."""
    if not 0.0 <= floor <= 1.0:
        raise Tier3Error(
            f"--floor must be between 0 and 1 (got {floor})")
    conn = _connect_readonly(db_path)
    try:
        try:
            books = conn.execute(
                "SELECT work_id, title FROM tier3_books "
                "ORDER BY title, work_id").fetchall()
        except sqlite3.OperationalError:
            raise Tier3Error(
                "that file is not a Tier 3 store — import a "
                "preview first")
        if work:
            wanted = _resolve_work(conn, work)
            books = [b for b in books if b["work_id"] == wanted]
        alerts = []
        for book in books:
            work_id, title = book["work_id"], book["title"]
            found, seen = [], set()

            def add(kind, label, ctype, score, reason, ident):
                if ident in seen:
                    return
                seen.add(ident)
                found.append({
                    "work_id": work_id, "title": title,
                    "kind": kind, "label": label, "type": ctype,
                    "score": score, "reason": reason,
                })

            # (a) Scored claim candidates: being a candidate is
            # the non-shipment — the claim was dropped or
            # rejected, however close the judge called it.
            for row in conn.execute(
                    "SELECT state, payload_json FROM "
                    "tier3_candidates WHERE work_id = ? "
                    "AND kind = 'claim'", (work_id,)):
                claim = json.loads(row["payload_json"])
                p = claim.get("validation_p")
                if p is None or p < floor:
                    continue
                add("claim",
                    f"{claim.get('from')} → {claim.get('to')}",
                    claim.get("type"), p,
                    row["state"] or claim.get("validation_note")
                    or claim.get("validation_state") or "dropped",
                    ("claim",) + _alert_claim_key(claim))

            # (b) Merge candidates in the ambiguous band:
            # judged, scored at or above the floor, but below
            # the threshold that would have merged the pair.
            # Unjudged name candidates carry no score and so
            # have nothing to band.
            for row in conn.execute(
                    "SELECT state, payload_json FROM "
                    "tier3_candidates WHERE work_id = ? "
                    "AND kind = 'merge'", (work_id,)):
                entry = json.loads(row["payload_json"])
                p = entry.get("p_yes")
                if p is None or p < floor or \
                        p >= MERGE_AUTO_THRESHOLD:
                    continue
                pair = tuple(sorted(
                    (entry.get("name_a"), entry.get("name_b")),
                    key=lambda n: n or ""))
                add("merge", f"{pair[0]} ↔ {pair[1]}", "merge",
                    p, row["state"] or entry.get("reason")
                    or "left for review", ("merge",) + pair)

            # (c) Scored claims inside Tier 3 characters'
            # recorded claim sets that did not ship as
            # validated — downgrades to `other`, scored claims
            # left in review_required, dropped claims already
            # seen as candidates (deduped by identity above).
            for row in conn.execute(
                    "SELECT claims_json FROM tier3_characters "
                    "WHERE work_id = ?", (work_id,)):
                for claim in json.loads(row["claims_json"]
                                        or "[]"):
                    p = claim.get("validation_p")
                    if p is None or p < floor:
                        continue
                    if _shipped_confirmed(claim):
                        continue
                    add("claim",
                        f"{claim.get('from')} → {claim.get('to')}",
                        claim.get("type"), p,
                        claim.get("validation_note")
                        or claim.get("validation_state")
                        or "unconfirmed",
                        ("claim",) + _alert_claim_key(claim))

            found.sort(key=lambda a: (-a["score"], a["label"]))
            alerts.extend(found)
        return alerts
    finally:
        conn.close()


def build_alerts(db_path, work=None, floor=DEFAULT_ALERT_FLOOR):
    alerts = collect_alerts(db_path, work=work, floor=floor)
    lines = [f"Tier 3 alerts — possible but unconfirmed "
             f"(floor {floor:.2f})"]
    if not alerts:
        lines.append(
            "  no alerts — nothing scored at or above the floor "
            "stayed unconfirmed")
        return "\n".join(lines)
    current = None
    for alert in alerts:
        if alert["work_id"] != current:
            current = alert["work_id"]
            n = sum(1 for a in alerts
                    if a["work_id"] == current)
            lines.append("")
            lines.append(f"{alert['title']} ({current}) — "
                         f"{n} alert(s)")
        detail = f"  {alert['score']:.2f}  {alert['kind']:<5}  " \
                 f"{alert['label']}"
        if alert["kind"] == "claim":
            detail += f" — type {alert['type']}"
        detail += f" — {alert['reason']}"
        lines.append(detail)
    books = {a["work_id"] for a in alerts}
    lines.append("")
    lines.append(f"{len(alerts)} alert(s) across "
                 f"{len(books)} book(s)")
    return "\n".join(lines)


def cmd_alerts(args):
    print(build_alerts(args.db, work=args.work, floor=args.floor))
    return 0


def _add_db(ap):
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help=f"Tier 3 database (default {DEFAULT_DB})")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tier3_store.py",
        description="Local Tier 3 store: the roster remainder "
                    "and a run's unconfirmed candidates, kept on "
                    "this machine for the User's review and push "
                    "decisions.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser(
        "import", help="populate one book's Tier 3 from a "
                       "preview artefact")
    _add_db(p_import)
    p_import.add_argument("--preview", required=True,
                          help="preview artefact JSON for the book")
    p_import.add_argument(
        "--stored-names", default=None,
        help="file of prod-stored character names, one per line "
             "(wins over Supabase and derivation)")
    p_import.add_argument(
        "--run-key", default=None,
        help="run key to record (default: the preview file's "
             "stem)")
    p_import.add_argument(
        "--config", default=str(DEFAULT_CONFIG),
        help="processor config.json holding the Supabase "
             f"credentials (default {DEFAULT_CONFIG})")
    p_import.set_defaults(fn=cmd_import)

    p_list = sub.add_parser(
        "list", help="per book: Tier 3 count, candidates, and "
                     "the review list")
    _add_db(p_list)
    p_list.add_argument("--work", default=None,
                        help="work id or title substring "
                             "(default: every book)")
    p_list.set_defaults(fn=cmd_list)

    p_alerts = sub.add_parser(
        "alerts", help="digest of scored-but-unconfirmed "
                       "results at or above a floor "
                       "(read-only)")
    _add_db(p_alerts)
    p_alerts.add_argument("--work", default=None,
                          help="work id or title substring "
                               "(default: every book)")
    p_alerts.add_argument(
        "--floor", type=float, default=DEFAULT_ALERT_FLOOR,
        help="lowest score that counts as a possible "
             f"(default {DEFAULT_ALERT_FLOOR})")
    p_alerts.set_defaults(fn=cmd_alerts)

    p_approve = sub.add_parser(
        "approve", help="record a push decision in the ledger "
                        "(no prod write in this version)")
    _add_db(p_approve)
    p_approve.add_argument("--work", required=True,
                           help="work id or title substring")
    p_approve.add_argument("--name", required=True,
                           help="Tier 3 character name, exactly "
                                "as listed")
    p_approve.set_defaults(fn=cmd_approve)

    p_unapprove = sub.add_parser(
        "unapprove", help="remove a push decision from the "
                          "ledger")
    _add_db(p_unapprove)
    p_unapprove.add_argument("--work", required=True,
                             help="work id or title substring")
    p_unapprove.add_argument("--name", required=True,
                             help="character name, exactly as "
                                  "recorded")
    p_unapprove.set_defaults(fn=cmd_unapprove)

    p_report = sub.add_parser(
        "report", help="store-wide counts and per-book "
                       "reconciliation")
    _add_db(p_report)
    p_report.set_defaults(fn=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except Tier3Error as e:
        print(f"tier3_store: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
