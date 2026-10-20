#!/usr/bin/env python3
"""Hardcover metadata client + cache for the ebook processor.

Phase 1 of the series-linking programme (spec:
hardcover-series-linking-spec-2026-10-19.md). Hardcover supplies
what the local sources cannot: curated series membership with
reading position, and a canonical bibliographic record. On a
confident match its values are authoritative on the work record
(ruling R-H2); everything about the lookup is therefore built to
refuse rather than guess — a wrong series link would poison every
sibling book's character priors when the registry phases land.

Two match paths only, both exact (Hardcover has disabled every
pattern-matching operator, so there is no fuzzy query to lean on):

  ISBN path   the book's resolved ISBN exact-matches an edition.
              Confident by construction.
  Title path  exact-title candidates are filtered to survivors
              (normalized title equal under the same
              series-prefix-tolerant comparison the Open Library
              resolver uses, author list non-empty, author surname
              set intersecting the book's). One survivor accepts.
              Several accept only under the canonical-dominance
              clause: the highest users_read_count survivor needs
              >= 100 readers AND >= 20x the runner-up. The clause
              exists because exact-title search returns empty shell
              records beside the real book (probe, 2026-10-19:
              "Iron Flame" returned the real book at 3,558 readers
              plus two shells at 0-1, one of them authorless);
              dominance separates canon from shells while genuine
              ambiguity still refuses.

There is deliberately NO series-roster fetch: the probe showed a
roster is variant soup (43 rows for the 9 Empyrean books — every
language's edition a separate row, box sets squatting on position
1). A book's own membership, from its own lookup, is the only
series fact this module consumes.

Fail-soft, always: no token, an auth failure, a rate limit that
survives one honoured Retry-After, a network error, or a malformed
response disables the client for the rest of the process with
exactly one warning, and every lookup returns None so the caller's
chain proceeds on the sources it already had. The token is read
from the environment or config, carried only in the request
header, and never written to the cache, a preview, or a log.
"""

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.hardcover.app/v1/graphql"
USER_AGENT = "ebook-processor-hardcover/1.0 (personal metadata tooling)"
DEFAULT_CACHE_PATH = Path.home() / ".ebook-processor" / "hardcover_cache.db"

# Canonical-dominance clause (spec section 3): the floors that
# separate a canonical book from shell duplicates.
_DOMINANCE_MIN_READERS = 100
_DOMINANCE_MIN_RATIO = 20

_EDITION_QUERY = """
query HcEdition($isbn: String!) {
  editions(where: {isbn_13: {_eq: $isbn}}) {
    id title isbn_10 isbn_13 pages release_date
    publisher { name }
    book {
      id title release_date users_read_count
      contributions { author { name } }
      book_series { position compilation series { id name books_count } }
    }
  }
}
"""

_EDITION_QUERY_10 = """
query HcEdition10($isbn: String!) {
  editions(where: {isbn_10: {_eq: $isbn}}) {
    id title isbn_10 isbn_13 pages release_date
    publisher { name }
    book {
      id title release_date users_read_count
      contributions { author { name } }
      book_series { position compilation series { id name books_count } }
    }
  }
}
"""

_BOOK_TITLE_QUERY = """
query HcBookByTitle($title: String!) {
  books(where: {title: {_eq: $title}}) {
    id title release_date users_read_count
    contributions { author { name } }
    book_series { position compilation series { id name books_count } }
  }
}
"""


class _Disabled(Exception):
    """Internal: the client latched off; carries the reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def resolve_token(config):
    """The Hardcover token: HARDCOVER_TOKEN env first, then the
    config.json key. The env wins because a token pasted into a
    shell for one run should not require editing the config file
    that other tools read."""
    env = (os.environ.get("HARDCOVER_TOKEN") or "").strip()
    if env:
        return env
    return ((config or {}).get("hardcover_token") or "").strip()


def _tnorm(t):
    """Title/name normalization, same semantics as the pipeline's:
    lowercase, underscores to spaces, tidy whitespace."""
    return re.sub(r"\s+", " ", (t or "").lower().replace("_", " ")).strip()


def _surname(name):
    """Surname key for one author name. Handles both 'First Last'
    and 'Last, First' forms, because EPUB metadata uses both and a
    surname gate that only understood one would refuse real books."""
    n = (name or "").strip()
    if not n:
        return ""
    if "," in n:
        head = n.split(",", 1)[0]
    else:
        head = n.split()[-1]
    return re.sub(r"[^a-z0-9]", "", head.lower())


def _surname_set(names):
    if isinstance(names, str):
        names = [names]
    return {s for s in (_surname(n) for n in names or []) if s}


def _titles_match(candidate, local):
    """Series-prefix-tolerant title equality, mirroring the Open
    Library resolver's confidence test: normalized titles agree,
    tolerating a '[Series 02] - ' style prefix on the local title,
    or the candidate appearing whole inside a longer local title."""
    cand = _tnorm(candidate)
    loc = _tnorm(local)
    if not cand or not loc:
        return False
    bare = re.sub(r"^\[.*?\]\s*[-–:]\s*", "", loc).strip()
    if cand in (loc, bare):
        return True
    return len(cand) > 3 and cand in loc


def _parse_book(book):
    """One Hardcover book object -> flat record. Returns None when
    the object is too broken to trust (no id or no title): a
    partial record must refuse, not half-land."""
    if not isinstance(book, dict):
        return None
    book_id = book.get("id")
    title = (book.get("title") or "").strip()
    if book_id is None or not title:
        return None
    authors = []
    for contrib in book.get("contributions") or []:
        name = ((contrib or {}).get("author") or {}).get("name")
        if name and name.strip():
            authors.append(name.strip())
    rec = {
        "book_id": book_id,
        "title": title,
        "authors": authors,
        "release_date": book.get("release_date"),
        "users_read_count": book.get("users_read_count") or 0,
        "series_id": None,
        "series_name": None,
        "series_position": None,
        "compilation": False,
    }
    # A book can sit in several series (a main series plus a
    # universe/omnibus grouping). The FIRST membership is
    # Hardcover's primary placement; taking a later one would
    # file books under tie-in collections.
    for membership in book.get("book_series") or []:
        series = (membership or {}).get("series") or {}
        if series.get("id") is None:
            continue
        rec["series_id"] = series["id"]
        rec["series_name"] = (series.get("name") or "").strip() or None
        pos = membership.get("position")
        rec["series_position"] = (float(pos) if isinstance(pos, (int, float))
                                  else None)
        rec["compilation"] = bool(membership.get("compilation"))
        break
    return rec


class HardcoverCache:
    """SQLite memo of lookup outcomes, positive and negative.

    A folder scan re-resolves every book's identity; without the
    cache, a library re-run would spend one request per book per
    run against a 5,000/day budget. Negatives are cached too —
    'Hardcover has never heard of this book' is exactly the answer
    a re-run would otherwise pay to learn again. A cache failure
    (locked, corrupt) is a miss, never an exception: the cache is
    an optimisation, not a dependency.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_CACHE_PATH
        self._conn = None

    def _connect(self):
        if self._conn is not None:
            return self._conn
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path))
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS hc_cache ("
                " cache_key TEXT PRIMARY KEY, matched INTEGER NOT NULL,"
                " payload TEXT, fetched_at REAL NOT NULL)")
            self._conn.commit()
        except Exception:
            self._conn = None
        return self._conn

    def get(self, key):
        """(found, match_or_None). found=False means no row."""
        conn = self._connect()
        if conn is None:
            return False, None
        try:
            row = conn.execute(
                "SELECT matched, payload FROM hc_cache WHERE cache_key=?",
                (key,)).fetchone()
        except Exception:
            return False, None
        if row is None:
            return False, None
        if not row[0]:
            return True, None
        try:
            return True, json.loads(row[1])
        except Exception:
            return False, None

    def put(self, key, match):
        conn = self._connect()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT OR REPLACE INTO hc_cache"
                " (cache_key, matched, payload, fetched_at)"
                " VALUES (?, ?, ?, ?)",
                (key, 1 if match else 0,
                 json.dumps(match) if match else None, time.time()))
            conn.commit()
        except Exception:
            pass


class HardcoverClient:
    """One client per process: it owns the disabled latch, so a
    dead token or a hard rate limit costs one warning per run,
    not one failure per book."""

    def __init__(self, token, cache=None, refresh=False, warn=None,
                 timeout=15, sleep=None, opener=None):
        self._token = (token or "").strip()
        self._cache = cache if cache is not None else HardcoverCache()
        self._refresh = bool(refresh)
        self._warn = warn or (lambda msg: print(msg))
        self._timeout = timeout
        self._sleep = sleep if sleep is not None else time.sleep
        self._opener = opener or urllib.request.urlopen
        self._disabled_reason = None
        self._warned = False
        if not self._token:
            self._disabled_reason = "no_token"

    @property
    def available(self):
        """True while lookups can still contribute. False from
        birth without a token, and permanently after any latch."""
        return self._disabled_reason is None

    @property
    def disabled_reason(self):
        return self._disabled_reason

    def _disable(self, reason):
        self._disabled_reason = reason
        if not self._warned:
            self._warned = True
            self._warn(f"  Hardcover: disabled for this run ({reason}); "
                       f"continuing without it")

    def _post(self, query, variables):
        """One GraphQL round trip. Raises _Disabled (latching) on
        any failure the run cannot recover from."""
        body = json.dumps({"query": query,
                           "variables": variables}).encode("utf-8")
        req = urllib.request.Request(
            API_URL, data=body, method="POST",
            headers={"authorization": f"Bearer {self._token}",
                     "content-type": "application/json",
                     "user-agent": USER_AGENT})
        retried_429 = False
        while True:
            try:
                with self._opener(req, timeout=self._timeout) as resp:
                    raw = resp.read().decode("utf-8", "ignore")
                payload = json.loads(raw)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and not retried_429:
                    # Honour Retry-After once: a burst at scan start
                    # is normal, and giving up on the first 429
                    # would waste the token's daily budget window.
                    retried_429 = True
                    try:
                        wait = float(e.headers.get("Retry-After") or 1)
                    except (TypeError, ValueError):
                        wait = 1.0
                    self._sleep(min(max(wait, 0.0), 30.0))
                    continue
                if e.code == 429:
                    raise _Disabled("rate_limited")
                if e.code in (401, 403):
                    raise _Disabled("auth")
                raise _Disabled(f"http_{e.code}")
            except _Disabled:
                raise
            except Exception:
                raise _Disabled("network")
        if not isinstance(payload, dict) or payload.get("errors"):
            raise _Disabled("bad_response")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _Disabled("bad_response")
        return data

    def _query(self, query, variables):
        """_post with the latch applied: once disabled, every call
        is a local None and the network is never touched again."""
        if self._disabled_reason is not None:
            # no_token disables from birth; that first lookup is
            # the operator-visible moment, so it still warns (once).
            if self._disabled_reason == "no_token":
                self._disable("no_token")
            return None
        try:
            return self._post(query, variables)
        except _Disabled as d:
            self._disable(d.reason)
            return None

    # -- match paths ------------------------------------------------

    def _by_isbn(self, isbn):
        isbn = (isbn or "").strip()
        if not isbn:
            return None
        query = _EDITION_QUERY_10 if len(isbn) == 10 else _EDITION_QUERY
        data = self._query(query, {"isbn": isbn})
        if data is None:
            return None
        editions = [e for e in (data.get("editions") or [])
                    if isinstance(e, dict)]
        if not editions:
            return None
        parsed = []
        for ed in editions:
            rec = _parse_book(ed.get("book"))
            if rec is None:
                continue
            rec["publisher"] = ((ed.get("publisher") or {}).get("name")
                                or None)
            rec["pages"] = ed.get("pages")
            rec["release_date"] = (ed.get("release_date")
                                    or rec["release_date"])
            rec["edition_id"] = ed.get("id")
            parsed.append(rec)
        if not parsed:
            return None
        # Several editions can share an ISBN across formats; prefer
        # the one whose book carries series membership — the series
        # link is the point of the lookup — else the first.
        chosen = next((r for r in parsed if r["series_id"] is not None),
                      parsed[0])
        chosen["match_path"] = "isbn"
        chosen["dominance"] = False
        chosen["alternates"] = len(parsed) - 1
        return chosen

    def _by_title(self, title, author):
        title = (title or "").strip()
        if not title:
            return None
        # Exact _eq is case-sensitive, and EPUB titles are not
        # uniformly cased; try the title as-is, then title-cased.
        # Both attempts together are still one cache row.
        books = None
        for variant in dict.fromkeys([title, title.title()]):
            data = self._query(_BOOK_TITLE_QUERY, {"title": variant})
            if data is None:
                return None
            books = [b for b in (data.get("books") or [])
                     if isinstance(b, dict)]
            if books:
                break
        if not books:
            return None
        book_surnames = _surname_set(author)
        survivors = []
        for raw in books:
            rec = _parse_book(raw)
            if rec is None or not rec["authors"]:
                # Authorless records are shells: with no author to
                # confirm against, they can never be survivors —
                # not even as a dominance candidate.
                continue
            if not _titles_match(rec["title"], title):
                continue
            if not (_surname_set(rec["authors"]) & book_surnames):
                continue
            rec["publisher"] = None
            rec["pages"] = None
            rec["edition_id"] = None
            survivors.append(rec)
        if not survivors:
            return None
        if len(survivors) == 1:
            chosen = survivors[0]
            chosen["match_path"] = "title"
            chosen["dominance"] = False
            chosen["alternates"] = len(books) - 1
            return chosen
        survivors.sort(key=lambda r: r["users_read_count"], reverse=True)
        top, runner_up = survivors[0], survivors[1]
        if (top["users_read_count"] >= _DOMINANCE_MIN_READERS
                and top["users_read_count"]
                >= _DOMINANCE_MIN_RATIO * max(runner_up["users_read_count"], 0)):
            top["match_path"] = "title"
            top["dominance"] = True
            top["alternates"] = len(books) - 1
            return top
        return None

    # -- entry point --------------------------------------------------

    def lookup(self, isbn=None, title=None, author=None):
        """The confident match for one book, or None.

        ISBN first (confident by construction); the title path is
        tried only when the ISBN path found nothing — including
        when the book has no ISBN at all, which is the case the
        title path exists for. Outcomes are cached by ISBN and by
        normalized title+author; a cached negative is honoured
        until --hardcover-refresh."""
        if self._disabled_reason == "no_token":
            self._disable("no_token")
            return None
        isbn_key = f"isbn:{isbn.strip()}" if (isbn or "").strip() else None
        title_key = None
        if title:
            surnames = ",".join(sorted(_surname_set(author)))
            title_key = f"title:{_tnorm(title)}|{surnames}"
        if not self._refresh:
            for key in (isbn_key, title_key):
                if key:
                    found, match = self._cache.get(key)
                    if found:
                        return match
        match = None
        if isbn_key:
            match = self._by_isbn(isbn)
        if match is None and title:
            match = self._by_title(title, author)
        # Cache only a lookup that actually completed. If the
        # client latched mid-lookup (outage, rate limit), the
        # outcome is "unknown", and caching it as a negative would
        # poison every later run with a failure's answer.
        if self.available:
            if isbn_key:
                self._cache.put(isbn_key, match)
            if title_key:
                self._cache.put(title_key, match)
        return match
