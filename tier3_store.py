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

Four tables:

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
                     vocabulary). One refinement on "never
                     shipped": scored claims that shipped embedded
                     on a STORED character's row without
                     confirming under their claimed type —
                     downgrades above all — are recorded too,
                     flagged stored_source. A stored character's
                     claims otherwise live only on his prod row,
                     which this tool never mirrors, and a
                     downgrade happens to a shipped claim whose
                     source earns a stored row by construction,
                     so without the flag the store would be blind
                     to the run's closest family calls.
  push_ledger        the User's push decisions (work, name,
                     decided-at). This is his curation data: an
                     import NEVER modifies it, so decisions survive
                     re-runs and re-imports.
  rejected_types     type strings the User has rejected as
                     registry proposals, each with its mandatory
                     reason and the date he decided it. This is
                     the rejected-wins record for types — the same
                     doctrine the rest of the system applies to
                     human rejections — and an import NEVER
                     modifies it either. A later run that
                     re-proposes a rejected string does not get
                     to re-ask the question: the proposal row is
                     kept (its occurrence evidence matters — a
                     string proposed again and again is signal)
                     but annotated previously_rejected with the
                     recorded reason and date, and the list groups
                     it apart from fresh proposals.

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
candidates (dropped claims, and the flagged stored-source claims
that shipped only in downgraded or still-unconfirmed form),
merge candidates in the ambiguous band, and scored claims inside
Tier 3 characters' recorded claim sets that did not ship as
validated (a family claim downgraded to `other` on a mid-band
draw is the canonical case). The floor is
configurable (default 0.30) and the digest sorts by score
descending, closest calls first, each entry carrying the state
or reason that left it unconfirmed. Scores are read out of the
recorded payloads at alert time; the digest never writes. (Type
proposals are not alerts and the digest never reads them,
previously-rejected or not — a vocabulary question is not a
near-miss claim.)

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

Two stored-set refinements keep that order honest. A Supabase
answer of ZERO rows falls through to derivation, with a note on
the import: principals earn a stored row whenever a book is
written at all, so an empty answer means the work has no prod
rows yet, not that the whole roster is unstored — and taking it
literally would make every principal Tier 3 and approvable. And
a config that is present but unparseable (or carries only half
the Supabase credentials) is a loud error naming the file, for
the same reason a failed query is: the mistake is the
operator's to fix, not the store's to derive around.

Book identity is the artefact's work_id when it carries one.
Without one, every import derives the same fallback — the
normalized `title:{title}|{author}` shape the staging gate
uses — and before minting it the importer checks the store for
a book whose normalized title (and, when both sides name one,
author) already matches, and adopts that book's identity
instead; an explicit work_id arriving after a fallback import
renames the fallback book onto it, push-ledger decisions
included. One book therefore keeps one store identity across
artefact variants, and curation recorded under either form
stays attached to it.

One exception, ruled on 2026-10-18 (issue #56): when the title
matches MORE THAN ONE stored book the author guard cannot
separate, there is no fact of the matter about which book the
import means — the sorted-first pick the store used to make is
deterministic but arbitrary, and a wrong adoption replaces
that book's Tier 3 rows. A multi-match adoption therefore
never happens by default: without the import command's
--allow-multi-match-adoption flag the import mints its own
fallback identity instead and says so loudly, with the match
counted separately in the import summary; with the flag the
adoption proceeds as before (sorted first, author guard
unchanged) and the warning still prints — the flag enables
the adoption, it does not silence it. Single-match adoption
and the author guard itself are unchanged in both modes.

`list` and `report` are read-only: they never create the
database file, and a store holding no books reports NOT
IMPORTED rather than RECONCILED — an empty reconciliation is
not a pass.

Usage:
    python3 tier3_store.py import --preview "preview/<book>.json"
                                  [--allow-multi-match-adoption]
    python3 tier3_store.py list [--work <id-or-title>]
    python3 tier3_store.py alerts [--work <id-or-title>]
                                  [--floor 0.30]
    python3 tier3_store.py approve --work <w> --name <n>
    python3 tier3_store.py unapprove --work <w> --name <n>
    python3 tier3_store.py reject-type --type <s> --reason <r>
    python3 tier3_store.py unreject-type --type <s>
    python3 tier3_store.py report

Stdlib only, Python 3.8+, Windows and Linux: the User runs this
from the repo root on his own machine, where his preview
artefacts and his config live. The database defaults to
~/.ebook-processor/tier3.db, beside the learned state.
"""
import argparse
import http.client
import json
import math
import re
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
# Database. The push ledger and the rejected-types record are
# created alongside the rest but are only ever written by their
# own subcommands (approve/unapprove, reject-type/unreject-type):
# imports replace a book's characters and candidates inside one
# transaction and never reference either record at all, which is
# what makes "imports never modify the User's curation data" a
# structural fact rather than a promise.
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
CREATE TABLE IF NOT EXISTS rejected_types (
    type TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    decided_at TEXT
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
        # utf-8-sig for the same reason as the stored-names file:
        # an editor-saved BOM is not part of the JSON, and a
        # principal-bearing artefact should not fail (or worse,
        # misread) over an invisible prefix.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
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
    anything if its exclusions are true. The same rule covers a
    config that is present but cannot be read, and one that names
    only half the Supabase credentials: both are operator errors
    in a hand-edited file, and silently deriving around them
    would hide the mistake behind a plausible-looking list. A
    config carrying neither credential is a different case — it
    makes no claim about Supabase at all — so it alone falls
    through to derivation."""
    path = Path(config_path)
    if not path.exists():
        return None
    try:
        # utf-8-sig: an editor-written BOM must not turn a valid
        # config into a spurious parse failure.
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise Tier3Error(
            f"processor config is not valid JSON: {path} ({e}) — "
            f"refusing to derive a stored set around a config "
            f"that cannot be read. Fix the config or pass "
            f"--stored-names.")
    if not isinstance(cfg, dict):
        raise Tier3Error(
            f"processor config is not a JSON object: {path} — "
            f"refusing to derive a stored set around it. Fix the "
            f"config or pass --stored-names.")
    url = (cfg.get("supabase_url") or "").rstrip("/")
    key = cfg.get("supabase_key") or ""
    if bool(url) != bool(key):
        raise Tier3Error(
            f"processor config {path} carries only one of "
            f"supabase_url / supabase_key — refusing to guess "
            f"whether the stored set should come from prod. "
            f"Complete the config or pass --stored-names.")
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
    except (http.client.HTTPException, ConnectionError) as e:
        # A connection that drops mid-request raises
        # RemoteDisconnected — a ConnectionResetError and an
        # http.client.HTTPException, but neither an HTTPError nor
        # a URLError — so without this catch it escaped the
        # fail-closed contract above as a traceback. It is the
        # same operator situation as an unreachable host (the
        # stored set could not be read from prod), so it earns
        # the same one-line error.
        raise Tier3Error(
            f"Supabase stored-set query failed — the connection "
            f"dropped mid-request ({e}) — refusing to fall back "
            f"to derivation. Fix the connection or pass "
            f"--stored-names.")
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


def _resolve_stored_set_detailed(artefact, work_id,
                                 stored_names_path, config_path):
    """Returns (names, source, note) — source is 'stored-names',
    'supabase' or 'derived', and is recorded on the book row so a
    later reader knows how much authority the exclusion set has.
    The note is set when the resolution had to say something out
    loud (today: the empty-Supabase fallback below)."""
    if stored_names_path:
        path = Path(stored_names_path)
        if not path.is_file():
            raise Tier3Error(f"stored-names file not found: {path}")
        names = sorted({
            line.strip() for line in
            # utf-8-sig: Windows editors save UTF-8 with a BOM,
            # and under plain utf-8 the BOM would glue itself to
            # the first name, which would then match no roster
            # entry and silently land a stored principal in
            # Tier 3.
            path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.strip().startswith("#")})
        if not names:
            raise Tier3Error(f"stored-names file is empty: {path}")
        return names, "stored-names", None
    names = _supabase_stored_names(config_path, work_id)
    if names is not None:
        if names:
            return names, "supabase", None
        # An empty prod answer cannot be the whole truth for a
        # book with principals: principals earn a stored row
        # whenever the book is written at all, so an empty result
        # means the work has no prod rows yet (a preview-only run
        # is the normal posture), not that the whole roster is
        # unstored. Taking it literally would put every principal
        # in Tier 3 and make them approvable for push. Derivation
        # from the artefact is the verified floor instead; if the
        # artefact cannot support derivation it fails loudly
        # there, which is the fail-closed outcome.
        return _derive_stored_names(artefact), "derived", (
            "Supabase returned no stored rows for this work, so "
            "the stored set was derived from the artefact "
            "instead of treating the whole roster as unstored")
    return _derive_stored_names(artefact), "derived", None


def _resolve_stored_set(artefact, work_id, stored_names_path,
                        config_path):
    names, source, _note = _resolve_stored_set_detailed(
        artefact, work_id, stored_names_path, config_path)
    return names, source


# ---------------------------------------------------------------------------
# Candidates. Four kinds, each the record of something the run
# produced but did not confirm. Payloads are the artefact's own
# dicts wherever the artefact has one — the store's job is to keep
# them reviewable, not to reinterpret them.
# ---------------------------------------------------------------------------

def _chapter_map(artefact):
    """The artefact's chapter map (chapter number -> chapter
    entry), validated on the way out. Every consumer of
    chapter_data goes through here because the shape is nested
    hand-adjacent data: a level arriving as a list instead of an
    object used to crash the import with an AttributeError,
    which escapes main's Tier3Error handling as a traceback.
    A malformed level is a named, one-line error instead, and
    because candidate generation runs inside the import
    transaction, the store is left untouched."""
    chapter_data = artefact.get("chapter_data")
    if chapter_data is None:
        return {}
    if not isinstance(chapter_data, dict):
        raise Tier3Error(
            f"artefact's chapter_data is a "
            f"{type(chapter_data).__name__}, not an object — the "
            f"per-chapter deltas cannot be read from it")
    chapters = chapter_data.get("chapters")
    if chapters is None:
        return {}
    if not isinstance(chapters, dict):
        raise Tier3Error(
            f"artefact's chapter_data.chapters is a "
            f"{type(chapters).__name__}, not an object — the "
            f"per-chapter deltas cannot be read from it")
    for chnum, chapter in chapters.items():
        if not isinstance(chapter, dict):
            raise Tier3Error(
                f"artefact's chapter_data.chapters[{chnum!r}] is "
                f"a {type(chapter).__name__}, not an object — "
                f"that chapter's deltas cannot be read from it")
    return chapters


def _candidates(artefact, stored_set=()):
    """Yields (kind, state, payload) for one artefact. The
    stored set matters to exactly one pass: the stored-source
    claims below, which are identified by their source being a
    name prod kept."""
    # Merges the judge scored but the run did not take; the
    # recorded reason ("below threshold (0.85)") is the state that
    # left the pair unconfirmed. Deduped on the sorted pair —
    # the same key the alerts digest groups by — because the
    # artefact can record a pair in both orders, and two rows
    # for one pair would make the candidate counts disagree
    # with the alerts that read them. First occurrence wins, the
    # claim passes' convention below. An entry missing either
    # name cannot form the key, so it yields as recorded: no
    # rejection is invented for a shape nothing asked about.
    # The same treatment covers a non-string name: sorted() on
    # mixed types raises TypeError, which would escape as a
    # traceback and lose a row the store could have kept
    # inspectable, so dedupe applies only when both names are
    # strings — the only shape that can form the key honestly.
    seen_merges = set()
    for entry in artefact.get("possible_merges") or []:
        if isinstance(entry, dict):
            if isinstance(entry.get("name_a"), str) and \
                    isinstance(entry.get("name_b"), str) and \
                    entry["name_a"] and entry["name_b"]:
                pair = tuple(sorted(
                    (entry["name_a"], entry["name_b"])))
                if pair in seen_merges:
                    continue
                seen_merges.add(pair)
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

    # Scored claims that shipped embedded on a STORED character's
    # row without confirming under their claimed type — above all
    # the downgrade, which lands in state `validated` with its
    # note reading "downgraded" and its type rewritten to
    # `other`. A Tier 3 character's claims are recorded on his
    # row in this store, so his downgrades reach the alerts
    # digest through it; a stored character's claims live only on
    # his prod row, which this tool never mirrors. Without this
    # pass the digest is blind to exactly the near-misses it
    # exists for, because the blind spot is systematic: a
    # downgrade happens to a shipped claim, and a shipped claim
    # earns its source a stored row — family downgrades land on
    # stored sources by construction. The payload is the claim as
    # recorded, flagged stored_source so the surfaces can
    # annotate it; note the record itself no longer carries the
    # pre-downgrade type (a downgrade's type reads `other`, its
    # origin surviving only in the note). Claims already yielded
    # above are skipped via the shared identity set — a claim
    # lives in one list or the other in a real artefact, but the
    # invariant is asserted here, not assumed.
    for key in ("relationships", "minor_sourced_relationships"):
        for claim in artefact.get(key) or []:
            if not isinstance(claim, dict):
                continue
            if claim.get("from") not in stored_set:
                continue
            if claim.get("validation_p") is None:
                continue
            if _shipped_confirmed(claim):
                continue
            if _claim_key(claim) in seen:
                continue
            seen.add(_claim_key(claim))
            payload = dict(claim)
            payload["stored_source"] = True
            state = claim.get("validation_note") or \
                claim.get("validation_state") or "unconfirmed"
            yield ("claim", state, payload)

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
    # A list, not a set: a mention is free text as recorded, and
    # the dedupe must never fail on a value it cannot hash.
    seen_mentions = []
    for name, entry in roster.items():
        mentions = entry.get("unresolved_mentions") or []
        # The pipeline writes this field as a list of free-text
        # mention strings, but a bare string is one mention, not
        # a sequence of them: iterating it would yield one
        # candidate per character, each a single letter. A list
        # is used as recorded; mentions are never comma-joined
        # by the pipeline, so a string is never split either —
        # splitting would invent mentions nobody recorded. Any
        # other shape is named and refused, for the same reason
        # malformed chapter_data is: a guess here becomes
        # plausible-looking review rows.
        if isinstance(mentions, str):
            mentions = [mentions]
        elif not isinstance(mentions, list):
            raise Tier3Error(
                f"artefact's unresolved_mentions for character "
                f"{name!r} is a {type(mentions).__name__}, not a "
                f"list or a single mention string — its mentions "
                f"cannot be read from it")
        for mention in mentions:
            # The same mention recorded twice for one character
            # is one review item, not two: dedupe on the
            # (character, mention) pair, first occurrence wins,
            # so the candidate counts match what a reader sees.
            ident = (name, mention)
            if ident in seen_mentions:
                continue
            seen_mentions.append(ident)
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
    chapters = _chapter_map(artefact)
    if not chapters:
        return
    final_types = set()
    for key in CLAIM_LIST_KEYS:
        for claim in artefact.get(key) or []:
            if isinstance(claim, dict) and claim.get("type"):
                final_types.add(claim["type"])
    registered = BUILTIN_REL_TYPES | final_types
    # Proposals are gathered before yielding because recurrence
    # is part of the record: a type proposed for the same pair
    # in chapters 3 and 7 is one proposal, but its payload must
    # say it recurred — occurrence is signal, the same doctrine
    # the rejected-types handling at insert applies when it
    # keeps a re-proposed string's row. Collapsing to the first
    # chapter alone threw that signal away. chapter_number keeps
    # its first-occurrence meaning for existing readers;
    # occurrences and chapters are always present alongside it
    # (1 and [first chapter] for a lone proposal), so a reader
    # never has to guess whether their absence means "once".
    proposals = {}
    for chnum, chapter in chapters.items():
        delta_block = chapter.get("a")
        if delta_block is None:
            deltas = []
        elif not isinstance(delta_block, dict):
            raise Tier3Error(
                f"artefact's chapter_data.chapters[{chnum!r}].a "
                f"is a {type(delta_block).__name__}, not an "
                f"object — that chapter's deltas cannot be read "
                f"from it")
        else:
            deltas = delta_block.get("relationships") or []
        for claim in deltas:
            if not isinstance(claim, dict):
                continue
            ctype = claim.get("type")
            if not ctype or ctype in registered:
                continue
            ident = (claim.get("from"), claim.get("to"), ctype)
            if ident in proposals:
                payload = proposals[ident]
                payload["occurrences"] += 1
                if chnum not in payload["chapters"]:
                    payload["chapters"].append(chnum)
                continue
            payload = dict(claim)
            payload["chapter_number"] = chnum
            payload["occurrences"] = 1
            payload["chapters"] = [chnum]
            payload["detection"] = (
                "type appears in the run's chapter deltas but in "
                "none of its final claim lists, and is outside "
                "the built-in relationship vocabulary")
            proposals[ident] = payload
    for payload in proposals.values():
        yield ("type_proposal", "unknown_type", payload)


# ---------------------------------------------------------------------------
# The rejected-types record. A flat map by type string, global
# to the store (vocabulary is global, like the registry it feeds).
# Matching is by exact string: the importer derives a proposal
# string from the artefact's recorded type verbatim, with no
# case-folding or other normalization anywhere in that pass, so
# an exact match IS the importer's normalization. (The
# subcommands strip surrounding whitespace from --type — the
# same trim stored-names lines get — because a type string
# recorded in JSON never carries edge whitespace, so the trim
# cannot diverge from a recorded string.)
# ---------------------------------------------------------------------------

def _rejected_types_map(conn):
    """type string -> {"reason": ..., "decided_at": ...}."""
    return {r["type"]: {"reason": r["reason"],
                         "decided_at": r["decided_at"]}
            for r in conn.execute(
                "SELECT type, reason, decided_at "
                "FROM rejected_types")}


# ---------------------------------------------------------------------------
# Import. Replaces one book's characters and candidates inside a
# single transaction; the push ledger is not referenced here at
# all. The book row records how the stored set was resolved, so
# the report can reconcile against it later.
# ---------------------------------------------------------------------------

def _tnorm(text):
    """The pipeline's title/author normalization (the same shape
    process-portable's _tnorm and staging_work_key use): lower-
    case, underscores become spaces, whitespace tidied. Book
    identity has to fold case and spacing variants of the same
    title onto one key, or each variant mints its own book."""
    return re.sub(r"\s+", " ", (text or "").lower()
                  .replace("_", " ")).strip()


def _fallback_work_id(artefact, preview_path):
    """The ONE fallback identity for an artefact with no work_id,
    shared by every import: the staging shape, normalized
    title|author. When the artefact carries no title its file
    stem stands in for the title — the stem is an input to this
    one derivation, never a separate identity of its own."""
    title = artefact.get("title") or Path(preview_path).stem
    return f"title:{_tnorm(title)}|{_tnorm(artefact.get('author'))}"


def _matching_store_books(rows, title, author):
    """Every stored work_id matching this title, in adoption
    order. Matching is on the normalized title, with the author
    as a guard: two genuinely different books can share a title,
    so an author that is present on both sides and differs
    blocks the match, while a missing author on either side
    cannot prove a difference. An explicit work_id sorts before
    a fallback identity when both somehow exist — the explicit
    id is the identity prod knows. The full list, not just the
    first entry, is what the identity resolution needs: whether
    the list holds more than one book decides if an adoption is
    a plain match or a multi-match the operator must opt into
    (see _resolve_import_identity)."""
    wanted = _tnorm(title)
    if not wanted:
        return []
    matches = []
    for row in rows:
        if _tnorm(row["title"]) != wanted:
            continue
        have, given = _tnorm(row["author"]), _tnorm(author)
        if have and given and have != given:
            continue
        matches.append(row["work_id"])
    matches.sort(key=lambda w: (w.startswith("title:"), w))
    return matches


def _matching_store_book(rows, title, author):
    """The work_id of an existing store book for this title, or
    None: the first of _matching_store_books, kept for callers
    that only need the pick itself."""
    matches = _matching_store_books(rows, title, author)
    return matches[0] if matches else None


def _resolve_import_identity(db_path, candidate, explicit, title,
                             author,
                             allow_multi_match_adoption=False):
    """Returns (work_id, adopt_from, multi_match) for an import,
    checking the store BEFORE anything is written. A
    work_id-less artefact whose book is already in the store
    adopts that book's identity, so a re-import replaces the
    same book instead of minting a second one whose rows never
    reconcile with the first. An explicit work_id that arrives
    after a fallback import renames the fallback book onto it
    (adopt_from), so the push ledger decisions recorded under
    the fallback key move with the book instead of being
    stranded. The peek is read-only: resolving identity must
    never create the store, because the stored-set resolution
    that follows is allowed to fail and must fail with nothing
    written.

    multi_match is None unless the title matched more than one
    stored book AND an adoption was at stake, in which case it
    is {"matches": [...], "adopted": bool, "target": work_id}:
    the full match list, whether the adoption went ahead, and
    the book it would have adopted. One shape carries "noted":
    True in place of an adoption outcome: an explicit candidate
    whose sorted-first match is another explicit identity. No
    adoption is applicable there in either mode — two explicit
    identities for one title are never merged, so the flag has
    nothing to enable — but the multi-match still happened, and
    it is reported in its own terms rather than silently
    dropped or dressed in blocked/adopted wording that would
    promise an adoption the flag cannot perform. A multi-match adoption —
    either direction: a fallback candidate adopting a stored
    identity, or an explicit identity renaming a fallback book
    onto itself — happens only when the operator opted in with
    allow_multi_match_adoption (the import command's
    --allow-multi-match-adoption flag), because among several
    guard-passing books the sorted-first pick is arbitrary and
    a wrong adoption replaces that book's Tier 3 rows. Without
    the opt-in the candidate keeps its own identity: a fallback
    candidate mints the canonical fallback book, an explicit
    candidate simply starts its own book and renames nothing.
    The author guard runs before any of this, exactly as
    before, in both modes — the flag never overrides it."""
    if not Path(db_path).is_file():
        return candidate, None, None
    try:
        conn = _connect_readonly(db_path)
    except sqlite3.Error:
        return candidate, None, None
    try:
        try:
            rows = conn.execute(
                "SELECT work_id, title, author "
                "FROM tier3_books").fetchall()
        except sqlite3.Error:
            return candidate, None, None
    finally:
        conn.close()
    if any(r["work_id"] == candidate for r in rows):
        return candidate, None, None
    matches = _matching_store_books(rows, title, author)
    if not matches:
        return candidate, None, None
    match = matches[0]
    multi = None
    if len(matches) > 1:
        multi = {"matches": matches,
                 "adopted": bool(allow_multi_match_adoption),
                 "target": match}
    if explicit:
        # Two explicit identities for one title are not merged:
        # they may be genuinely different works. Only a fallback
        # identity folds into the explicit one.
        if not match.startswith("title:"):
            if multi is not None:
                # No adoption is applicable on this path in
                # either mode — the flag has nothing to enable —
                # so the multi-match is carried back as noted,
                # not dropped (where it read as no match at all)
                # and not dressed as an adoption outcome.
                multi["adopted"] = False
                multi["noted"] = True
            return candidate, None, multi
        if multi is not None and not multi["adopted"]:
            return candidate, None, multi
        return candidate, match, multi
    if multi is not None and not multi["adopted"]:
        return candidate, None, multi
    return match, None, multi


def _rename_work_identity(conn, old, new):
    """Move every row keyed by a fallback identity onto the
    explicit work_id that supersedes it. The push ledger is the
    load-bearing table here — its decisions are the User's
    curation data — but characters, candidates and the book row
    move too, so no surface keeps a ghost of the old identity.
    A ledger row already recorded under the new id wins a
    collision; the stale copy under the old id is then dropped."""
    for table in ("tier3_books", "tier3_characters",
                  "tier3_candidates"):
        conn.execute(
            f"UPDATE {table} SET work_id = ? WHERE work_id = ?",
            (new, old))
    conn.execute(
        "UPDATE OR IGNORE push_ledger SET work_id = ? "
        "WHERE work_id = ?", (new, old))
    conn.execute("DELETE FROM push_ledger WHERE work_id = ?",
                 (old,))


def import_preview(db_path, preview_path, stored_names_path=None,
                   run_key=None, config_path=DEFAULT_CONFIG,
                   allow_multi_match_adoption=False):
    artefact = _load_artefact(preview_path)
    roster = _roster(artefact)
    title = artefact.get("title") or Path(preview_path).stem
    run_key = run_key or Path(preview_path).stem
    explicit_id = artefact.get("work_id")
    candidate = explicit_id or _fallback_work_id(
        artefact, preview_path)
    work_id, adopt_from, multi_match = _resolve_import_identity(
        db_path, candidate, bool(explicit_id), title,
        artefact.get("author"),
        allow_multi_match_adoption=allow_multi_match_adoption)
    stored_names, source, stored_note = \
        _resolve_stored_set_detailed(
            artefact, work_id, stored_names_path, config_path)
    stored_set = set(stored_names)
    tier3_names = sorted(set(roster) - stored_set)
    absent = sorted(stored_set - set(roster))
    by_source = _claims_by_source(artefact)
    now = _now_iso()

    conn = _connect(db_path)
    try:
        with conn:
            if adopt_from is not None:
                _rename_work_identity(conn, adopt_from, work_id)
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
            previously_rejected = []
            rejected = _rejected_types_map(conn)
            for kind, state, payload in _candidates(
                    artefact, stored_set):
                if kind == "type_proposal":
                    # Rejected-wins: a string the User already
                    # rejected is kept as a candidate (its
                    # occurrence is evidence) but annotated, so
                    # no surface re-queues it as a fresh
                    # proposal. The record itself is not
                    # modified here — annotation copies it into
                    # the payload, never the other way round.
                    hit = rejected.get(payload.get("type"))
                    if hit is not None:
                        payload = dict(payload)
                        payload["previously_rejected"] = hit
                        previously_rejected.append(
                            payload["type"])
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
        "previously_rejected_types": sorted(
            set(previously_rejected)),
        # Through the validated chapter map, not a raw nested
        # .get chain: this line runs after candidate generation,
        # but it must not be a second, unguarded reading of the
        # same malformed shape.
        "has_chapter_data": bool(_chapter_map(artefact)),
        "stored_set_note": stored_note,
        "adopted_from": adopt_from,
        # The multi-match gate (#56): the matched identities
        # when an adoption was at stake among several books,
        # and the outcome as its own counts, so the import
        # summary can report a skipped, flagged or noted
        # multi-match separately from the ordinary book counts
        # instead of folding it into them invisibly. The noted
        # count is the #70 state: a multi-match where no
        # adoption was applicable (explicit candidate, explicit
        # first match), carried so the match is never silent.
        # All empty/zero when no multi-match arose —
        # single-match and no-match imports report exactly
        # what they reported before.
        "multi_match_matches": list(multi_match["matches"])
        if multi_match else [],
        "multi_match_count": len(multi_match["matches"])
        if multi_match else 0,
        "multi_match_target": multi_match["target"]
        if multi_match else None,
        "multi_match_blocked": 1 if multi_match
        and not multi_match["adopted"]
        and not multi_match.get("noted") else 0,
        "multi_match_adopted": 1 if multi_match
        and multi_match["adopted"] else 0,
        "multi_match_noted": 1 if multi_match
        and multi_match.get("noted") else 0,
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
    cand = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM tier3_candidates "
        "WHERE work_id = ? GROUP BY kind ORDER BY kind",
        (work_id,))}
    # Previously rejected type proposals stay in the candidates
    # table (their occurrences are evidence) but are not fresh
    # proposals: they come out of the kind counts and get their
    # own grouping below, with the reason he recorded.
    prev_rejected = []
    for r in conn.execute(
            "SELECT payload_json FROM tier3_candidates "
            "WHERE work_id = ? AND kind = 'type_proposal'",
            (work_id,)):
        payload = json.loads(r["payload_json"])
        hit = payload.get("previously_rejected")
        if hit:
            prev_rejected.append((payload.get("type"), hit))
    if prev_rejected:
        cand["type_proposal"] -= len(prev_rejected)
        if cand["type_proposal"] <= 0:
            del cand["type_proposal"]
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
            f"{k} {n}" for k, n in sorted(cand.items())))
    else:
        lines.append("  candidates: none")
    if prev_rejected:
        lines.append("  previously rejected type proposals: "
                     + "; ".join(
                         f"{t} — {h['reason']}"
                         for t, h in prev_rejected))
    for row in chars:
        mark = "  [approved for push]" if row["name"] in ledger \
            else ""
        lines.append(f"    {row['name']} — "
                     f"{row['appearances']} appearance(s){mark}")
    return lines


def cmd_import(args):
    result = import_preview(
        args.db, args.preview, stored_names_path=args.stored_names,
        run_key=args.run_key, config_path=args.config,
        allow_multi_match_adoption=args.allow_multi_match_adoption)
    print(f"imported {result['title']} ({result['work_id']})")
    print(f"  run key: {result['run_key']}")
    print(f"  roster: {result['roster_count']}  ·  stored set: "
          f"{result['stored_count']} via {result['stored_source']}  "
          f"·  Tier 3: {result['tier3_count']}")
    if result.get("stored_set_note"):
        print(f"  note: {result['stored_set_note']}")
    if result.get("adopted_from"):
        print(f"  note: this book was in the store under the "
              f"fallback identity {result['adopted_from']} — its "
              f"rows and push decisions were moved onto "
              f"{result['work_id']}")
    if result.get("multi_match_matches"):
        # Loud in both modes, by the #56 ruling: a skipped
        # adoption the operator never hears about is just a
        # quieter wrong book, and an adoption the flag enabled
        # is still an arbitrary pick he should see happen.
        matches = ", ".join(result["multi_match_matches"])
        if result["multi_match_noted"]:
            # No adoption was applicable (#70): both sides of
            # the first match carry explicit identities, and
            # two explicit identities for one title are never
            # merged — the flag could not have enabled anything
            # here, so the wording says exactly that instead of
            # borrowing the blocked/adopted text, which would
            # promise a skipped or performed adoption.
            print(f"  WARNING: {result['title']!r} matched "
                  f"{result['multi_match_count']} stored books "
                  f"({matches}) — no adoption applies: this "
                  f"import and its first match both carry "
                  f"explicit identities, and two explicit "
                  f"identities for one title are never merged. "
                  f"The import was recorded under its own "
                  f"identity {result['work_id']}, and the "
                  f"matched books were left untouched.")
            print(f"  multi-match noted (no adoption "
                  f"applicable): {result['multi_match_noted']}")
        elif result["multi_match_blocked"]:
            print(f"  WARNING: {result['title']!r} matched "
                  f"{result['multi_match_count']} stored books "
                  f"({matches}) — multi-match adoption skipped "
                  f"without --allow-multi-match-adoption: no "
                  f"stored book was adopted, and this import "
                  f"was recorded under its own identity "
                  f"{result['work_id']} instead. Re-run with "
                  f"--allow-multi-match-adoption to adopt "
                  f"{result['multi_match_target']}.")
            print(f"  multi-match adoptions skipped: "
                  f"{result['multi_match_blocked']}")
        else:
            print(f"  WARNING: {result['title']!r} matched "
                  f"{result['multi_match_count']} stored books "
                  f"({matches}) — adopting "
                  f"{result['multi_match_target']} because "
                  f"--allow-multi-match-adoption was given. The "
                  f"pick is the first in the store's adoption "
                  f"order and may not be the book meant; the "
                  f"other match(es) were left untouched.")
            print(f"  multi-match adoptions: "
                  f"{result['multi_match_adopted']}")
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
    if result["previously_rejected_types"]:
        print("  previously rejected type proposal(s), kept "
              "annotated rather than re-queued: "
              + ", ".join(result["previously_rejected_types"]))
    if not result["has_chapter_data"]:
        print("  note: artefact carries no chapter_data — dropped "
              "type proposals cannot be determined from it, so "
              "none were recorded")
    return 0


def cmd_list(args):
    # Read-only: listing a store that does not exist must say so
    # without creating one — a mistyped --db path that silently
    # materializes an empty store reads as an empty library, and
    # the file then lingers as if it were the real store.
    if not Path(args.db).is_file():
        print(f"no Tier 3 store at {args.db} — "
              f"import a preview first")
        return 0
    conn = _connect_readonly(args.db)
    try:
        try:
            if args.work:
                work_ids = [_resolve_work(conn, args.work)]
            else:
                work_ids = [r["work_id"] for r in conn.execute(
                    "SELECT work_id FROM tier3_books "
                    "ORDER BY title")]
        except sqlite3.OperationalError:
            raise Tier3Error(
                "that file is not a Tier 3 store — import a "
                "preview first")
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


def cmd_reject_type(args):
    # The reason is the ruling, not paperwork: it is the text a
    # future surface shows when the string resurfaces, and the
    # User's curation rule is that every rejection carries one.
    # argparse already refuses a missing --reason; a blank one
    # is refused here for the same reason.
    reason = (args.reason or "").strip()
    if not reason:
        raise Tier3Error(
            "--reason must say why the type is rejected — every "
            "rejection carries a reason, and this one is what a "
            "future reader sees when the string resurfaces")
    type_str = (args.type or "").strip()
    if not type_str:
        raise Tier3Error("--type must not be empty")
    conn = _connect(args.db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO rejected_types (type, reason, "
                "decided_at) VALUES (?,?,?) "
                "ON CONFLICT(type) DO UPDATE SET "
                "reason=excluded.reason, "
                "decided_at=excluded.decided_at",
                (type_str, reason, _now_iso()))
        print(f"rejected type: {type_str} — {reason}")
        print("  recorded locally; imports from now on keep "
              "proposals of this string annotated as previously "
              "rejected instead of re-queueing them")
        return 0
    finally:
        conn.close()


def cmd_unreject_type(args):
    type_str = (args.type or "").strip()
    conn = _connect(args.db)
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM rejected_types WHERE type = ?",
                (type_str,))
        if cur.rowcount == 0:
            raise Tier3Error(
                f"{type_str!r} is not in the rejected-types "
                f"record — nothing to remove")
        print(f"rejection removed: {type_str} — the next import "
              f"queues proposals of it as fresh again (rows "
              f"already imported keep their annotation until "
              f"then)")
        return 0
    finally:
        conn.close()


def build_report(db_path):
    # Read-only, like the alerts digest: a report must never
    # create the store it describes, because a mistyped --db
    # path would otherwise materialize an empty store and the
    # empty verdict below used to read as a clean bill of
    # health for a library that was never checked.
    if not Path(db_path).is_file():
        return "\n".join([
            "Tier 3 local store — report",
            "  books: 0  ·  Tier 3 characters: 0  ·  "
            "push ledger: 0 decision(s)",
            "  candidates: none",
            "  rejected types: 0",
            "verdict: NOT IMPORTED — no store at that path, "
            "so no book has been imported",
        ])
    conn = _connect_readonly(db_path)
    try:
        try:
            books = conn.execute(
                "SELECT * FROM tier3_books ORDER BY title") \
                .fetchall()
        except sqlite3.OperationalError:
            raise Tier3Error(
                "that file is not a Tier 3 store — import a "
                "preview first")
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
        rejected = [r["type"] for r in conn.execute(
            "SELECT type FROM rejected_types ORDER BY type")]
        lines.append(
            f"  rejected types: {len(rejected)}"
            + (f" — {', '.join(rejected)}" if rejected else ""))
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
        if not books:
            # An empty store has nothing to reconcile: all() of
            # no verdicts is vacuously true, and printing
            # RECONCILED for a book that was never imported
            # would turn a missing import into a pass.
            lines.append(
                "verdict: NOT IMPORTED — the store holds no "
                "books, so there is nothing to reconcile")
        else:
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
# store: scored claim candidates (dropped or rejected claims,
# plus the flagged stored-source claims that shipped embedded
# on a prod row without confirming under their claimed type),
# merge candidates in the ambiguous band, and
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
    # sqlite3.connect is lazy: a file that is not a database at
    # all opens without complaint and fails only on the first
    # query, as a DatabaseError at whichever read surface queries
    # first. Probing the schema here means every read path
    # (list, report, alerts) answers for a non-database file
    # with the same named error instead of leaking that
    # traceback. A genuine SQLite file that simply is not a
    # Tier 3 store passes this probe and still fails at the
    # call sites' own table queries, as before.
    try:
        conn.execute(
            "SELECT name FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.DatabaseError:
        conn.close()
        raise Tier3Error(
            f"that file is not a Tier 3 store: {path} — the "
            f"file is not a readable database. Import a preview "
            f"first")
    return conn


def _recorded_score(value):
    """A score as recorded in a payload, as a number — or None
    when the record carries no usable score. The processor's
    contract is numeric-or-None: its score reader validates a
    score as int/float (never bool), rejects non-finite values,
    and returns a float or None, so a string score — even a
    readable one like "0.72" — is outside the artefact contract
    and is read as unscored, the digest's treatment of a missing
    score, rather than coerced into a number the judge never
    emitted. Before this, a string score reached the band
    comparisons directly and one malformed candidate crashed
    the whole digest with a TypeError."""
    if isinstance(value, bool) or \
            not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score):
        return None
    return score


def _alert_claim_key(claim):
    """Claim identity for dedupe — the same fields the importer
    dedupes candidates on, so a claim recorded both as a
    candidate and inside its source's claim set alerts once."""
    return (claim.get("from"), claim.get("to"), claim.get("type"),
            claim.get("chapter"))


def _alert_claim_label(claim):
    """The digest's label for one claim: its two endpoints joined
    by an arrow. A claim missing an endpoint must not render
    Python's None into a human-facing label: the present endpoint
    is shown and the missing side is marked as unnamed — the same
    treatment the merge band gives a one-sided pair. A truthy
    non-string endpoint (a malformed record the importer keeps
    as recorded) renders as its text form, so the label cannot
    crash on it. What is stored does not change."""
    shown = [n if isinstance(n, str) and n
             else "(unnamed)" if not n else str(n)
             for n in (claim.get("from"), claim.get("to"))]
    return f"{shown[0]} → {shown[1]}"


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

            def add(kind, label, ctype, score, reason, ident,
                    stored_source=False, claimed_type=None):
                if ident in seen:
                    return
                seen.add(ident)
                found.append({
                    "work_id": work_id, "title": title,
                    "kind": kind, "label": label, "type": ctype,
                    "score": score, "reason": reason,
                    "stored_source": stored_source,
                    "claimed_type": claimed_type,
                })

            # (a) Scored claim candidates: being a candidate is
            # the non-shipment — the claim was dropped or
            # rejected, however close the judge called it. The
            # stored-source candidates the importer records are
            # the one refinement: those claims shipped embedded
            # on a prod row, but only in downgraded or
            # still-unconfirmed form, so they belong in the
            # same band, flagged.
            for row in conn.execute(
                    "SELECT state, payload_json FROM "
                    "tier3_candidates WHERE work_id = ? "
                    "AND kind = 'claim'", (work_id,)):
                claim = json.loads(row["payload_json"])
                p = _recorded_score(claim.get("validation_p"))
                if p is None or p < floor:
                    continue
                add("claim",
                    _alert_claim_label(claim),
                    claim.get("type"), p,
                    row["state"] or claim.get("validation_note")
                    or claim.get("validation_state") or "dropped",
                    ("claim",) + _alert_claim_key(claim),
                    stored_source=claim.get("stored_source")
                    is True,
                    claimed_type=claim.get("claimed_type"))

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
                p = _recorded_score(entry.get("p_yes"))
                if p is None or p < floor or \
                        p >= MERGE_AUTO_THRESHOLD:
                    continue
                pair = tuple(sorted(
                    (entry.get("name_a"), entry.get("name_b")),
                    key=lambda n: n if isinstance(n, str) else ""))
                # A pair missing one side must not render
                # Python's None into a human-facing label: the
                # present name is shown and the missing side is
                # marked as unnamed. A truthy non-string name
                # (a malformed record the importer keeps as
                # recorded) renders as its text form, so neither
                # the sort nor the label can crash on it.
                shown = [n if isinstance(n, str) and n
                         else "(unnamed)" if not n else str(n)
                         for n in pair]
                add("merge", f"{shown[0]} ↔ {shown[1]}", "merge",
                    p, row["state"] or entry.get("reason")
                    or "left for review", ("merge",) + tuple(shown))

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
                    p = _recorded_score(claim.get("validation_p"))
                    if p is None or p < floor:
                        continue
                    if _shipped_confirmed(claim):
                        continue
                    add("claim",
                        _alert_claim_label(claim),
                        claim.get("type"), p,
                        claim.get("validation_note")
                        or claim.get("validation_state")
                        or "unconfirmed",
                        ("claim",) + _alert_claim_key(claim),
                        claimed_type=claim.get("claimed_type"))

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
            if alert.get("claimed_type"):
                # A downgrade's recorded type is "other" for
                # every claim alike; the stamped claimed type is
                # the only place what was actually claimed
                # survives, so the line names the pair of them.
                detail += (f" — claimed {alert['claimed_type']}"
                           f" → other")
            else:
                detail += f" — type {alert['type']}"
        detail += f" — {alert['reason']}"
        if alert.get("stored_source"):
            # The claim shipped on a stored character's prod
            # row; the alert is the only local trace of it.
            detail += "  [stored source]"
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
    p_import.add_argument(
        "--allow-multi-match-adoption", action="store_true",
        default=False,
        help="when the import's title matches more than one "
             "stored book, adopt the first in adoption order "
             "anyway (the default is to adopt none of them and "
             "record the import under its own identity); the "
             "multi-match warning prints either way")
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

    p_reject = sub.add_parser(
        "reject-type", help="record a relationship-type string "
                            "as rejected (with its mandatory "
                            "reason), so future proposals of it "
                            "are annotated, not re-queued")
    _add_db(p_reject)
    p_reject.add_argument("--type", required=True,
                          help="the proposed type string, "
                               "exactly as recorded")
    p_reject.add_argument("--reason", required=True,
                          help="why it is rejected — mandatory; "
                               "shown wherever the string "
                               "resurfaces")
    p_reject.set_defaults(fn=cmd_reject_type)

    p_unreject = sub.add_parser(
        "unreject-type", help="remove a type string from the "
                              "rejected-types record")
    _add_db(p_unreject)
    p_unreject.add_argument("--type", required=True,
                            help="the rejected type string")
    p_unreject.set_defaults(fn=cmd_unreject_type)

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
