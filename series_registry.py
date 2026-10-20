#!/usr/bin/env python3
"""Series character registry for the ebook processor.

Phase 2 of the series-linking programme (spec:
hardcover-series-linking-spec-2026-10-19.md, section 5): the
WRITE side only. Each finished book folds its shipped cast into a
per-series entry here; nothing in the pipeline reads the registry
yet. Phase 3 will consume it as roster priors, behind its flag.

The registry is DERIVED state. Every row traces to a finished
book record, and the whole store can be rebuilt by re-folding
those records, so a registry failure must never fail a book: the
pipeline seam wraps population, logs one warning, and proceeds.
The store lives on the user's machine only (ruling R-H1),
beside the Hardcover cache, because series data already lives in
the shared library — what is local here is the character memory,
not the bibliography.

Five tables:

  registry_series      one row per series_key: the display name
                       and the source that named it, refreshed
                       by whichever book folded most recently
                       (the key is the identity; the name is
                       cosmetic — Hardcover renames series).
  registry_characters  one row per (series, character): the
                       canonical name, keyed by the normalized
                       name. First writer wins: a later book that
                       ships a different surface for the same key
                       attests that surface as an alias instead
                       of renaming the character, so canonical
                       spellings stay stable across a series.
  registry_presence    one row per (series, character, book):
                       which list the character shipped on in
                       that book (principal / minor) and the
                       book's series position (NULL when the
                       membership carried none — position-unknown
                       is recorded as absence, never as 0).
  registry_aliases     one row per (series, character, alias,
                       book): every alias a book attested for the
                       character, with the attesting book, because
                       an alias's provenance is what Phase 3
                       judges it by.
  registry_family_links
                       one row per (series, book, from, to,
                       type): the family edges a book shipped,
                       endpoints resolved to registry characters,
                       validation_state as shipped — a memory of
                       what was judged, never a source of claims.

Aggregates (first/last position a character appears at, the
books an alias is attested in) are computed on READ from the
presence and alias rows, never stored as counters: re-folding a
book replaces exactly that book's rows inside one transaction,
so totals can never drift from the provenance they summarize.

Book identity: a record's work_id when it carries one; at the
pipeline seam the work_id does not exist yet (work resolution
runs after the identity landing), so the pipeline-time fold keys
by the normalized title|author fallback — the same shape
tier3_store uses for artefact identity. The seam then restates
the fold under the resolved work_id (rekey_book), so one book
has exactly one presence key no matter which artefact a later
fold reads. The store is rebuilt from finished records, so a
key change costs a re-fold, not data.

Stdlib only, Python 3.8+, Windows and Linux.
"""

import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

DEFAULT_REGISTRY_PATH = Path.home() / ".ebook-processor" / \
    "series_registry.db"

# The pipeline's family-type vocabulary, mirrored from the
# is_family flags of _BUILTIN_REL_TYPES in process-portable.py —
# the same mirroring tier3_store keeps for its type list. The
# pipeline passes its LIVE family set (_REL_FAMILY_TYPES, which a
# seeded registry can extend beyond the built-ins) when it calls
# populate_from_record; this mirror is only the fallback for
# standalone use, so the two never disagree on the built-ins.
FAMILY_TYPES = frozenset((
    "spouse", "parent", "child", "sibling",
    "step_parent", "step_child",
))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_series (
    series_key TEXT PRIMARY KEY,
    series_name TEXT,
    source TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS registry_characters (
    series_key TEXT NOT NULL,
    name_key TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    PRIMARY KEY (series_key, name_key)
);
CREATE TABLE IF NOT EXISTS registry_presence (
    series_key TEXT NOT NULL,
    name_key TEXT NOT NULL,
    book_key TEXT NOT NULL,
    list_kind TEXT NOT NULL,
    position REAL,
    PRIMARY KEY (series_key, name_key, book_key)
);
CREATE TABLE IF NOT EXISTS registry_aliases (
    series_key TEXT NOT NULL,
    name_key TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    alias TEXT NOT NULL,
    book_key TEXT NOT NULL,
    PRIMARY KEY (series_key, name_key, alias_key, book_key)
);
CREATE TABLE IF NOT EXISTS registry_family_links (
    series_key TEXT NOT NULL,
    book_key TEXT NOT NULL,
    from_key TEXT NOT NULL,
    to_key TEXT NOT NULL,
    type TEXT NOT NULL,
    validation_state TEXT,
    PRIMARY KEY (series_key, book_key, from_key, to_key, type)
);
CREATE INDEX IF NOT EXISTS registry_presence_book_idx
    ON registry_presence (series_key, book_key);
CREATE INDEX IF NOT EXISTS registry_aliases_book_idx
    ON registry_aliases (series_key, book_key);
CREATE INDEX IF NOT EXISTS registry_family_book_idx
    ON registry_family_links (series_key, book_key);
"""


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _name_key(name):
    """The registry's character key: the pipeline norm()'s
    folding (lowercase, NFKD accent fold, non-alphanumerics to
    single spaces) WITHOUT its leading-article strip. The strip
    exists for work titles; character identity must not equate
    'The Major' with 'Major' on a guess — those are different
    roster entries until a book's own merges say otherwise."""
    s = unicodedata.normalize("NFKD", (name or "").lower())
    s = re.sub(r"[\u0300-\u036f]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def name_key(name):
    """The registry's character key for a name surface — the
    public form of _name_key. Phase 3's priors match roster name
    surfaces against registry characters, and the match is only
    meaningful in this key space (the roster's own norm_name
    strips leading articles; this one deliberately does not)."""
    return _name_key(name)


def _tnorm(text):
    """Title/author normalization for the fallback book key —
    the same shape process-portable's _tnorm uses."""
    return re.sub(r"\s+", " ", (text or "").lower()
                  .replace("_", " ")).strip()


def fallback_book_key_for(result):
    """The presence key a record folds under BEFORE work
    resolution: its book_key when it carries one, else the
    normalized title|author fallback. The pipeline seam folds at
    identity-landing time, when no work_id exists yet, so live
    folds land here; _book_key_for on a finished record prefers
    the work_id, so the two disagree for the same book until the
    seam restates the fold (SeriesRegistry.rekey_book — the same
    adopt-after-resolution shape the staging and roster stores
    use for their fallback-keyed state)."""
    value = result.get("book_key")
    if value:
        return str(value)
    return f"title:{_tnorm(result.get('title'))}" \
           f"|{_tnorm(result.get('author'))}"


def _book_key_for(result):
    """The presence key for one finished record: its work_id
    when it carries one, else the pre-resolution fallback."""
    value = result.get("work_id")
    if value:
        return str(value)
    return fallback_book_key_for(result)


class SeriesRegistry:
    """The SQLite store. One instance per process is enough: a
    fold is one short transaction per book."""

    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_REGISTRY_PATH
        self._conn = None

    def _connect(self):
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- write side -------------------------------------------------

    def other_books_state(self, series_key, book_key):
        """(character keys, alias pairs) attested by OTHER books
        of this series: the state a fold's delta is measured
        against. Excluding the folding book's own prior rows is
        what makes a re-fold report the same delta as the first
        fold — the book is compared to the rest of the series,
        never to itself."""
        conn = self._connect()
        chars = {r[0] for r in conn.execute(
            "SELECT DISTINCT name_key FROM registry_presence"
            " WHERE series_key=? AND book_key<>?",
            (series_key, book_key))}
        aliases = {(r[0], r[1]) for r in conn.execute(
            "SELECT name_key, alias_key FROM registry_aliases"
            " WHERE series_key=? AND book_key<>?",
            (series_key, book_key))}
        return chars, aliases

    def canonical_for(self, series_key, name_key):
        """The stored canonical name for one character, or None
        when this series has never seen the key."""
        conn = self._connect()
        row = conn.execute(
            "SELECT canonical_name FROM registry_characters"
            " WHERE series_key=? AND name_key=?",
            (series_key, name_key)).fetchone()
        return row[0] if row else None

    def replace_book_fold(self, series_key, book_key, *,
                          series_name, source, position,
                          characters, alias_rows, family_rows):
        """Replace one book's contribution to a series, in one
        transaction. characters is [(name_key, canonical,
        list_kind)]; alias_rows is [(name_key, alias, alias_key)];
        family_rows is [(from_key, to_key, type,
        validation_state)].

        Delete-then-insert for exactly this book's rows is what
        makes population idempotent: a re-processed book restates
        its provenance instead of doubling it. Character rows use
        INSERT OR IGNORE (first writer keeps the canonical name);
        a differing surface from a later book arrives through
        alias_rows instead. Characters left with no presence,
        alias, or family row from ANY book are removed — a
        re-fold whose roster shrank must not leave ghosts that a
        later read would mistake for cast members."""
        conn = self._connect()
        with conn:
            conn.execute(
                "INSERT INTO registry_series"
                " (series_key, series_name, source, updated_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(series_key) DO UPDATE SET"
                " series_name=excluded.series_name,"
                " source=excluded.source,"
                " updated_at=excluded.updated_at",
                (series_key, series_name, source, _now_iso()))
            for table in ("registry_presence", "registry_aliases",
                          "registry_family_links"):
                conn.execute(
                    f"DELETE FROM {table}"
                    " WHERE series_key=? AND book_key=?",
                    (series_key, book_key))
            for name_key, canonical, list_kind in characters:
                conn.execute(
                    "INSERT OR IGNORE INTO registry_characters"
                    " (series_key, name_key, canonical_name)"
                    " VALUES (?,?,?)",
                    (series_key, name_key, canonical))
                conn.execute(
                    "INSERT OR REPLACE INTO registry_presence"
                    " (series_key, name_key, book_key, list_kind,"
                    "  position) VALUES (?,?,?,?,?)",
                    (series_key, name_key, book_key, list_kind,
                     position))
            for name_key, alias, alias_key in alias_rows:
                conn.execute(
                    "INSERT OR REPLACE INTO registry_aliases"
                    " (series_key, name_key, alias_key, alias,"
                    "  book_key) VALUES (?,?,?,?,?)",
                    (series_key, name_key, alias_key, alias,
                     book_key))
            for from_key, to_key, rtype, state in family_rows:
                conn.execute(
                    "INSERT OR REPLACE INTO registry_family_links"
                    " (series_key, book_key, from_key, to_key,"
                    "  type, validation_state)"
                    " VALUES (?,?,?,?,?,?)",
                    (series_key, book_key, from_key, to_key,
                     rtype, state))
            conn.execute(
                "DELETE FROM registry_characters"
                " WHERE series_key=?"
                " AND name_key NOT IN"
                " (SELECT name_key FROM registry_presence"
                "   WHERE series_key=?)"
                " AND name_key NOT IN"
                " (SELECT name_key FROM registry_aliases"
                "   WHERE series_key=?)"
                " AND name_key NOT IN"
                " (SELECT from_key FROM registry_family_links"
                "   WHERE series_key=?)"
                " AND name_key NOT IN"
                " (SELECT to_key FROM registry_family_links"
                "   WHERE series_key=?)",
                (series_key,) * 5)

    def rekey_book(self, series_key, old_key, new_key):
        """Move one book's fold from old_key to new_key, in one
        transaction. Returns True when rows moved.

        The pipeline folds before work resolution (the fold keys
        by the title|author fallback) and resolves the work_id
        moments later; without this restatement the same book
        would sit under the fallback key from its live fold AND
        under its work_id from any later re-fold of the finished
        record — two presence keys for one book, and every
        per-book aggregate (an alias's attesting books, a
        character's book count) double-counting it.

        Any rows already under new_key are deleted first: they
        are an older fold of this same book (a finished-record
        re-fold), and the fold being restated is the fresher one.
        The fresh fold replaces; it never merges with the stale
        one."""
        if not series_key or not old_key or not new_key \
                or old_key == new_key:
            return False
        conn = self._connect()
        moved = 0
        with conn:
            for table in ("registry_presence", "registry_aliases",
                          "registry_family_links"):
                conn.execute(
                    f"DELETE FROM {table}"
                    " WHERE series_key=? AND book_key=?",
                    (series_key, new_key))
            for table in ("registry_presence", "registry_aliases",
                          "registry_family_links"):
                moved += conn.execute(
                    f"UPDATE {table} SET book_key=?"
                    " WHERE series_key=? AND book_key=?",
                    (new_key, series_key, old_key)).rowcount
            # The stale fold deleted above may have been a
            # character's only attestation; the same orphan
            # cleanup replace_book_fold runs applies here.
            conn.execute(
                "DELETE FROM registry_characters"
                " WHERE series_key=?"
                " AND name_key NOT IN"
                " (SELECT name_key FROM registry_presence"
                "   WHERE series_key=?)"
                " AND name_key NOT IN"
                " (SELECT name_key FROM registry_aliases"
                "   WHERE series_key=?)"
                " AND name_key NOT IN"
                " (SELECT from_key FROM registry_family_links"
                "   WHERE series_key=?)"
                " AND name_key NOT IN"
                " (SELECT to_key FROM registry_family_links"
                "   WHERE series_key=?)",
                (series_key,) * 5)
        return moved > 0

    # -- read side (aggregates computed here, never stored) -------

    def series_keys(self):
        conn = self._connect()
        return [r[0] for r in conn.execute(
            "SELECT series_key FROM registry_series"
            " ORDER BY series_key")]

    def characters(self, series_key):
        """Every registry character of a series with its books,
        aliases, and computed first/last positions — the read
        Phase 3's priors and the Phase 2 gate hand-check use."""
        conn = self._connect()
        out = []
        for row in conn.execute(
                "SELECT name_key, canonical_name"
                " FROM registry_characters WHERE series_key=?"
                " ORDER BY canonical_name", (series_key,)):
            name_key = row["name_key"]
            presence = conn.execute(
                "SELECT book_key, list_kind, position"
                " FROM registry_presence"
                " WHERE series_key=? AND name_key=?",
                (series_key, name_key)).fetchall()
            positions = [p["position"] for p in presence
                         if p["position"] is not None]
            aliases = {}
            for a in conn.execute(
                    "SELECT alias, alias_key, book_key"
                    " FROM registry_aliases"
                    " WHERE series_key=? AND name_key=?",
                    (series_key, name_key)):
                aliases.setdefault(a["alias_key"], {
                    "alias": a["alias"], "books": []})
                aliases[a["alias_key"]]["books"].append(a["book_key"])
            out.append({
                "name_key": name_key,
                "canonical_name": row["canonical_name"],
                "books": [{"book_key": p["book_key"],
                           "list_kind": p["list_kind"],
                           "position": p["position"]}
                          for p in presence],
                "first_position": min(positions) if positions
                else None,
                "last_position": max(positions) if positions
                else None,
                "aliases": sorted(aliases.values(),
                                  key=lambda d: d["alias"]),
            })
        return out

    def family_links(self, series_key):
        conn = self._connect()
        names = {r["name_key"]: r["canonical_name"] for r in
                 conn.execute(
                     "SELECT name_key, canonical_name"
                     " FROM registry_characters"
                     " WHERE series_key=?", (series_key,))}
        return [{"book_key": r["book_key"],
                 "from": names.get(r["from_key"], r["from_key"]),
                 "to": names.get(r["to_key"], r["to_key"]),
                 "type": r["type"],
                 "validation_state": r["validation_state"]}
                for r in conn.execute(
                    "SELECT book_key, from_key, to_key, type,"
                    " validation_state FROM registry_family_links"
                    " WHERE series_key=? ORDER BY book_key, from_key,"
                    " to_key, type", (series_key,))]


def _record_characters(result):
    """The book's shipped cast as {name_key: entry}: principals
    first, so a name shipped on both lists keeps the principal
    kind (the tier3 roster's setdefault order). A second entry
    whose surface differs from the kept one contributes that
    surface as an alias — it is a form the character was seen
    under in this book, which is what the alias table records."""
    cast = {}
    for list_key, kind in (("characters", "principal"),
                           ("minor_characters", "minor")):
        for entry in result.get(list_key) or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name_key = _name_key(name)
            if not name_key:
                continue
            aliases = [a.strip() for a in entry.get("aliases") or []
                       if isinstance(a, str) and a.strip()]
            kept = cast.get(name_key)
            if kept is None:
                cast[name_key] = {
                    "canonical": name.strip(), "list_kind": kind,
                    "aliases": list(aliases)}
                continue
            if kind == "principal":
                kept["list_kind"] = "principal"
            if name.strip() != kept["canonical"] \
                    and name.strip() not in kept["aliases"]:
                kept["aliases"].append(name.strip())
            for a in aliases:
                if a not in kept["aliases"]:
                    kept["aliases"].append(a)
    return cast


def populate_from_record(registry, result, family_types=None):
    """Fold one finished book record into its series' registry
    entry. Returns the preview delta dict, or None when the fold
    does not apply: no series key on the record, or the record is
    a compilation (spec section 4: a compilation's cast is a
    union of volumes, so it neither seeds nor consumes priors).

    The delta is measured against the OTHER books of the series
    (see SeriesRegistry.other_books_state): characters_added are
    this book's characters no other book attests, and so on — a
    re-fold of the same book therefore reports the same delta
    as the first fold, instead of pretending everything is new
    again or nothing is.

    The record is read, never mutated. Store errors propagate to
    the caller — the pipeline seam is the wrap that logs one
    warning and proceeds, because standalone callers (tests, a
    future rebuild tool) deserve the exception, not silence."""
    series_key = result.get("series_key")
    if not series_key:
        return None
    if (result.get("hardcover") or {}).get("compilation"):
        return None
    fam_types = FAMILY_TYPES if family_types is None \
        else family_types
    book_key = _book_key_for(result)
    cast = _record_characters(result)

    # Endpoint resolution for this book's family rows: canonical
    # keys, plus every alias key the book itself shipped — an
    # endpoint recorded under a name form still resolves to the
    # character whose own entry claims that form. Anything else
    # is unresolved: skipped and counted, never guessed.
    resolve = {}
    for name_key, entry in cast.items():
        resolve[name_key] = name_key
        for alias in entry["aliases"]:
            resolve.setdefault(_name_key(alias), name_key)

    family_rows = []
    seen_edges = set()
    skipped = 0
    for rel in result.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        rtype = rel.get("type")
        if rtype not in fam_types:
            continue
        from_key = resolve.get(_name_key(rel.get("from") or ""))
        to_key = resolve.get(_name_key(rel.get("to") or ""))
        if from_key is None or to_key is None \
                or from_key == to_key:
            skipped += 1
            continue
        edge = (from_key, to_key, rtype)
        if edge in seen_edges:
            # One row per edge per book (the table's own key);
            # the first shipped row's state is the record.
            continue
        seen_edges.add(edge)
        family_rows.append((from_key, to_key, rtype,
                            rel.get("validation_state")))

    other_chars, other_aliases = registry.other_books_state(
        series_key, book_key)

    # This book's alias attestations: its shipped aliases, plus —
    # when an earlier book already fixed a different canonical
    # for the key — this book's own canonical surface, which is
    # a form the character was seen under here. A surface variant
    # of the canonical itself (case, spacing) normalizes to the
    # character's own key; it is still recorded, because the
    # alias table is the registry's name-forms record (spec
    # section 5) and the form's attesting book is the fact worth
    # keeping. Resolution maps such a key to the same character,
    # so the row costs nothing downstream.
    alias_rows = []
    for name_key, entry in cast.items():
        surfaces = list(entry["aliases"])
        existing = registry.canonical_for(series_key, name_key)
        if existing and existing != entry["canonical"] \
                and entry["canonical"] not in surfaces:
            surfaces.append(entry["canonical"])
        seen_alias_keys = set()
        for alias in surfaces:
            alias_key = _name_key(alias)
            if not alias_key or alias_key in seen_alias_keys:
                continue
            seen_alias_keys.add(alias_key)
            alias_rows.append((name_key, alias, alias_key))

    registry.replace_book_fold(
        series_key, book_key,
        series_name=result.get("series_name"),
        source=result.get("series_source"),
        position=result.get("series_position"),
        characters=[(k, e["canonical"], e["list_kind"])
                    for k, e in cast.items()],
        alias_rows=alias_rows,
        family_rows=family_rows)

    alias_pairs = {(n, a) for n, _alias, a in alias_rows}
    return {
        "series_key": series_key,
        "characters_added": sum(
            1 for k in cast if k not in other_chars),
        "characters_updated": sum(
            1 for k in cast if k in other_chars),
        "aliases_added": len(alias_pairs - other_aliases),
        "family_links_recorded": len(family_rows),
        "family_links_skipped": skipped,
    }
