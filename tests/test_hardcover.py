"""Hardcover Phase 1 tests (series-linking spec): the client, the
cache, the match discipline, the series chain slot, and the
authoritative record landing.

Fixtures are built from the Phase 0 probe's live captures
(2026-10-19): Fourth Wing ISBN 9781649374080 -> one edition,
book 714600, The Empyrean #1 (series 41764); Daemon ISBN
9781101007518 -> one edition, book 377567, Daemon #1 (series
1056); "Iron Flame" by exact title -> three books (747398 the
real one at 3,558 readers, Empyrean #2; 1494709 a Yarros shell at
0 readers; 2703783 an authorless shell at 1 reader).

Stdlib unittest only. No network: the transport is a fake opener.
Run: python -m unittest tests.test_hardcover -v
"""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, str(_ROOT / fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _load("hardcover", "hardcover.py")
pp = _load("pp_hardcover", "process-portable.py")


# --- probe-derived fixtures ----------------------------------------

FW_EDITION = {
    "id": 31440211, "title": "Fourth Wing", "isbn_10": "1649374046",
    "isbn_13": "9781649374080", "pages": 530,
    "release_date": "2023-05-02",
    "publisher": {"name": "Entangled: Red Tower Books"},
    "book": {
        "id": 714600, "title": "Fourth Wing",
        "release_date": "2023-05-02", "users_read_count": 5189,
        "contributions": [{"author": {"name": "Rebecca Yarros"}}],
        "book_series": [{
            "position": 1, "compilation": False,
            "series": {"id": 41764, "name": "The Empyrean",
                       "books_count": 9}}],
    },
}

DAEMON_EDITION = {
    "id": 2660973, "title": "Daemon", "isbn_10": "1101007518",
    "isbn_13": "9781101007518", "pages": 640,
    "release_date": "2009-06-23",
    "publisher": {"name": "Penguin"},
    "book": {
        "id": 377567, "title": "Daemon",
        "release_date": "2009-06-23", "users_read_count": 904,
        "contributions": [{"author": {"name": "Daniel Suarez"}}],
        "book_series": [{
            "position": 1, "compilation": False,
            "series": {"id": 1056, "name": "Daemon",
                       "books_count": 2}}],
    },
}


def _book(book_id, title, authors, read_count, series=None):
    return {
        "id": book_id, "title": title, "release_date": "2023-11-07",
        "users_read_count": read_count,
        "contributions": [{"author": {"name": a}} for a in authors],
        "book_series": [series] if series else [],
    }


EMPYREAN_2 = {"position": 2, "compilation": False,
              "series": {"id": 41764, "name": "The Empyrean",
                         "books_count": 9}}

IRON_FLAME_BOOKS = [
    _book(1494709, "Iron Flame", ["Rebecca Yarros"], 0),
    _book(2703783, "Iron Flame", [], 1),
    _book(747398, "Iron Flame", ["Rebecca Yarros"], 3558, EMPYREAN_2),
]


# --- fake transport ---------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()
        self.status = 200
        self.headers = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return self.handler(req)

    @property
    def calls(self):
        return len(self.requests)


def _router(editions=None, books=None):
    """Route a GraphQL request to canned probe-shaped payloads.
    editions/books are callables taking the query variable."""
    def handler(req):
        payload = json.loads(req.data.decode())
        query, var = payload["query"], payload["variables"]
        if "editions" in query and editions is not None:
            return _FakeResp({"data": {"editions": editions(var["isbn"])}})
        if "books" in query and books is not None:
            return _FakeResp({"data": {"books": books(var["title"])}})
        raise AssertionError(f"unexpected query: {query}")
    return handler


def _raising(exc):
    def handler(req):
        raise exc
    return handler


def _http(code, headers=None):
    return urllib.error.HTTPError(
        "https://api.hardcover.app/v1/graphql", code, "err",
        headers or {}, None)


def _client(opener, token="tok", cache_path=":memory:", **kw):
    kw.setdefault("sleep", lambda _s: None)
    warnings = []
    client = hc.HardcoverClient(
        token=token, cache=hc.HardcoverCache(cache_path),
        warn=warnings.append, opener=opener, **kw)
    return client, warnings


# --- client: parsing and the ISBN path --------------------------------

class TestIsbnPath(unittest.TestCase):
    def test_fourth_wing_match(self):
        opener = _FakeOpener(_router(editions=lambda _i: [FW_EDITION]))
        client, _ = _client(opener)
        m = client.lookup(isbn="9781649374080")
        self.assertIsNotNone(m)
        self.assertEqual(m["book_id"], 714600)
        self.assertEqual(m["title"], "Fourth Wing")
        self.assertEqual(m["authors"], ["Rebecca Yarros"])
        self.assertEqual(m["series_id"], 41764)
        self.assertEqual(m["series_name"], "The Empyrean")
        self.assertEqual(m["series_position"], 1.0)
        self.assertEqual(m["pages"], 530)
        self.assertEqual(m["publisher"], "Entangled: Red Tower Books")
        self.assertEqual(m["match_path"], "isbn")
        self.assertFalse(m["dominance"])
        self.assertEqual(m["alternates"], 0)

    def test_daemon_match(self):
        opener = _FakeOpener(_router(editions=lambda _i: [DAEMON_EDITION]))
        client, _ = _client(opener)
        m = client.lookup(isbn="9781101007518")
        self.assertEqual(m["book_id"], 377567)
        self.assertEqual(m["series_id"], 1056)
        self.assertEqual(m["series_name"], "Daemon")

    def test_multi_edition_prefers_series_member(self):
        bare = copy.deepcopy(FW_EDITION)
        bare["id"] = 999
        bare["book"] = copy.deepcopy(FW_EDITION["book"])
        bare["book"]["id"] = 555
        bare["book"]["book_series"] = []
        for order in ([bare, FW_EDITION], [FW_EDITION, bare]):
            opener = _FakeOpener(_router(editions=lambda _i, o=order: o))
            client, _ = _client(opener)
            m = client.lookup(isbn="9781649374080")
            self.assertEqual(m["book_id"], 714600)
            self.assertEqual(m["alternates"], 1)

    def test_no_editions_no_match(self):
        opener = _FakeOpener(_router(editions=lambda _i: [],
                                      books=lambda _t: []))
        client, _ = _client(opener)
        self.assertIsNone(client.lookup(isbn="9780000000000",
                                        title="Nope", author="Nobody"))
        self.assertTrue(client.available)


# --- client: the title path and the dominance clause -------------------

class TestTitlePath(unittest.TestCase):
    def _iron_client(self, books):
        opener = _FakeOpener(_router(books=lambda _t: books))
        return _client(opener)

    def test_iron_flame_dominance_accept(self):
        client, _ = self._iron_client(IRON_FLAME_BOOKS)
        m = client.lookup(title="Iron Flame", author="Rebecca Yarros")
        self.assertIsNotNone(m)
        self.assertEqual(m["book_id"], 747398)
        self.assertTrue(m["dominance"])
        self.assertEqual(m["match_path"], "title")
        self.assertEqual(m["series_id"], 41764)
        self.assertEqual(m["series_position"], 2.0)

    def test_authorless_shell_never_survives(self):
        shell = _book(2703783, "Iron Flame", [], 99999)
        client, _ = self._iron_client([shell])
        self.assertIsNone(client.lookup(title="Iron Flame",
                                        author="Rebecca Yarros"))

    def test_single_survivor_accepts_without_dominance(self):
        client, _ = self._iron_client([IRON_FLAME_BOOKS[2]])
        m = client.lookup(title="Iron Flame", author="Rebecca Yarros")
        self.assertEqual(m["book_id"], 747398)
        self.assertFalse(m["dominance"])

    def test_dominance_refused_when_counts_close(self):
        rival = _book(4242, "Iron Flame", ["Rebecca Yarros"], 2000,
                      EMPYREAN_2)
        client, _ = self._iron_client([IRON_FLAME_BOOKS[2], rival])
        self.assertIsNone(client.lookup(title="Iron Flame",
                                        author="Rebecca Yarros"))

    def test_dominance_refused_below_reader_floor(self):
        top = _book(4243, "Iron Flame", ["Rebecca Yarros"], 90)
        low = _book(4244, "Iron Flame", ["Rebecca Yarros"], 1)
        client, _ = self._iron_client([top, low])
        self.assertIsNone(client.lookup(title="Iron Flame",
                                        author="Rebecca Yarros"))

    def test_author_mismatch_refuses(self):
        client, _ = self._iron_client(IRON_FLAME_BOOKS)
        self.assertIsNone(client.lookup(title="Iron Flame",
                                        author="Brandon Sanderson"))

    def test_comma_form_author_matches(self):
        client, _ = self._iron_client([IRON_FLAME_BOOKS[2]])
        m = client.lookup(title="Iron Flame", author="Yarros, Rebecca")
        self.assertIsNotNone(m)


class TestHelpers(unittest.TestCase):
    def test_surname_forms(self):
        self.assertEqual(hc._surname("Rebecca Yarros"), "yarros")
        self.assertEqual(hc._surname("Yarros, Rebecca"), "yarros")
        self.assertEqual(hc._surname("Daniel Suarez"), "suarez")

    def test_titles_match(self):
        self.assertTrue(hc._titles_match("Freedom", "[Daemon 02] - Freedom"))
        self.assertTrue(hc._titles_match("Iron Flame", "Iron Flame"))
        self.assertFalse(hc._titles_match("Iron Flame", "Onyx Storm"))


# --- cache ---------------------------------------------------------------

class TestCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "hc.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_second_lookup_served_from_cache(self):
        opener = _FakeOpener(_router(editions=lambda _i: [FW_EDITION]))
        client, _ = _client(opener, cache_path=self.db)
        client.lookup(isbn="9781649374080")
        client.lookup(isbn="9781649374080")
        self.assertEqual(opener.calls, 1)

    def test_negative_is_cached(self):
        opener = _FakeOpener(_router(editions=lambda _i: [],
                                     books=lambda _t: []))
        client, _ = _client(opener, cache_path=self.db)
        self.assertIsNone(client.lookup(isbn="9780000000000",
                                        title="Nope", author="Nobody"))
        calls = opener.calls
        self.assertIsNone(client.lookup(isbn="9780000000000",
                                        title="Nope", author="Nobody"))
        self.assertEqual(opener.calls, calls)

    def test_refresh_bypasses_and_rewrites(self):
        opener = _FakeOpener(_router(editions=lambda _i: [FW_EDITION]))
        client, _ = _client(opener, cache_path=self.db)
        client.lookup(isbn="9781649374080")
        fresh, _ = _client(opener, cache_path=self.db, refresh=True)
        fresh.lookup(isbn="9781649374080")
        self.assertEqual(opener.calls, 2)
        third, _ = _client(opener, cache_path=self.db)
        third.lookup(isbn="9781649374080")
        self.assertEqual(opener.calls, 2)

    def test_outage_is_not_cached_as_negative(self):
        down = _FakeOpener(_raising(urllib.error.URLError("down")))
        client, _ = _client(down, cache_path=self.db)
        self.assertIsNone(client.lookup(isbn="9781649374080"))
        up = _FakeOpener(_router(editions=lambda _i: [FW_EDITION]))
        client2, _ = _client(up, cache_path=self.db)
        self.assertIsNotNone(client2.lookup(isbn="9781649374080"))


# --- fail-soft latching ---------------------------------------------------

class TestFailSoft(unittest.TestCase):
    def test_401_latches_and_warns_once(self):
        opener = _FakeOpener(_raising(_http(401)))
        client, warnings = _client(opener)
        for _ in range(3):
            self.assertIsNone(client.lookup(isbn="9781649374080"))
        self.assertEqual(opener.calls, 1)
        self.assertEqual(len(warnings), 1)
        self.assertFalse(client.available)
        self.assertEqual(client.disabled_reason, "auth")

    def test_network_latch(self):
        opener = _FakeOpener(_raising(urllib.error.URLError("down")))
        client, warnings = _client(opener)
        self.assertIsNone(client.lookup(title="X", author="Y"))
        self.assertIsNone(client.lookup(title="X", author="Y"))
        self.assertEqual(opener.calls, 1)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(client.disabled_reason, "network")

    def test_429_retry_then_success(self):
        state = {"n": 0}

        def handler(req):
            state["n"] += 1
            if state["n"] == 1:
                raise _http(429, {"Retry-After": "0"})
            return _FakeResp({"data": {"editions": [FW_EDITION]}})

        opener = _FakeOpener(handler)
        client, warnings = _client(opener)
        m = client.lookup(isbn="9781649374080")
        self.assertIsNotNone(m)
        self.assertEqual(opener.calls, 2)
        self.assertTrue(client.available)
        self.assertEqual(warnings, [])

    def test_429_twice_latches(self):
        opener = _FakeOpener(_raising(_http(429, {"Retry-After": "0"})))
        client, warnings = _client(opener)
        self.assertIsNone(client.lookup(isbn="9781649374080"))
        self.assertEqual(opener.calls, 2)
        self.assertEqual(client.disabled_reason, "rate_limited")
        self.assertEqual(len(warnings), 1)

    def test_graphql_errors_latch_bad_response(self):
        opener = _FakeOpener(lambda _req: _FakeResp(
            {"errors": [{"message": "schema moved"}]}))
        client, _ = _client(opener)
        self.assertIsNone(client.lookup(isbn="9781649374080"))
        self.assertEqual(client.disabled_reason, "bad_response")

    def test_no_token_disables_with_one_warning(self):
        opener = _FakeOpener(_router(editions=lambda _i: [FW_EDITION]))
        client, warnings = _client(opener, token="")
        self.assertFalse(client.available)
        self.assertIsNone(client.lookup(isbn="9781649374080"))
        self.assertIsNone(client.lookup(isbn="9781649374080"))
        self.assertEqual(opener.calls, 0)
        self.assertEqual(len(warnings), 1)


# --- pipeline seam: chain + landing ---------------------------------------

HC_MATCH = {
    "book_id": 747398, "title": "Iron Flame",
    "authors": ["Rebecca Yarros"], "publisher": None,
    "release_date": "2023-11-07", "pages": None,
    "series_id": 41764, "series_name": "The Empyrean",
    "series_position": 2.0, "match_path": "title", "dominance": True,
    "alternates": 2,
}


class _FakeClient:
    def __init__(self, match, available=True):
        self._match = match
        self.available = available
        self.calls = 0

    def lookup(self, isbn=None, title=None, author=None):
        self.calls += 1
        return self._match


class _PpCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: pp.CONFIG.get(k) for k in
                       ("hardcover", "hardcover_refresh",
                        "series_override", "series_position_override")}
        pp.CONFIG["hardcover"] = True
        pp.CONFIG.pop("series_override", None)
        pp.CONFIG.pop("series_position_override", None)
        pp._HC_MEMO.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                pp.CONFIG.pop(k, None)
            else:
                pp.CONFIG[k] = v
        pp._HC_MEMO.clear()

    def _stub(self, match, available=True):
        fake = _FakeClient(match, available)
        patcher = mock.patch.object(pp, "_hardcover_client",
                                    lambda: fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class TestChain(_PpCase):
    def test_hardcover_beats_filename(self):
        self._stub(HC_MATCH)
        name, pos, src = pp._detect_series(
            "[Other 09] - Iron Flame.epub",
            {"isbn": "9781649374110"}, "Iron Flame", "Rebecca Yarros")
        self.assertEqual((name, pos, src),
                         ("The Empyrean", 2.0, "hardcover"))

    def test_flag_beats_hardcover_and_never_consults(self):
        fake = self._stub(HC_MATCH)
        pp.CONFIG["series_override"] = "FlagSeries"
        name, pos, src = pp._detect_series(
            "x.epub", {"isbn": "9781649374110"},
            "Iron Flame", "Rebecca Yarros")
        self.assertEqual(src, "flag")
        self.assertEqual(fake.calls, 0)

    def test_epub_beats_hardcover_and_never_consults(self):
        fake = self._stub(HC_MATCH)
        name, pos, src = pp._detect_series(
            "[Other 09] - Iron Flame.epub",
            {"isbn": "9781649374110", "series": "EpubSeries",
             "series_index": 1.0},
            "Iron Flame", "Rebecca Yarros")
        self.assertEqual((name, src), ("EpubSeries", "epub"))
        self.assertEqual(fake.calls, 0)

    def test_no_match_falls_through_to_filename(self):
        self._stub(None)
        name, pos, src = pp._detect_series(
            "[Daemon 02] - Freedom.epub",
            {"isbn": "9780000000000"}, "Freedom", "Daniel Suarez")
        self.assertEqual((name, pos, src), ("Daemon", 2.0, "filename"))

    def test_compilation_membership_claims_no_position(self):
        # Spec §4: a compilation (omnibus/box set) is a series
        # member with NO position claim — the source hangs position
        # 1 on Empyrean box sets, and claiming it would register
        # the set as volume 1.
        m = dict(HC_MATCH, compilation=True, series_position=1.0)
        self._stub(m)
        name, pos, src = pp._detect_series(
            "Empyrean Collection.epub", {"isbn": "9781649374998"},
            "The Empyrean Collection", "Rebecca Yarros")
        self.assertEqual((name, pos, src),
                         ("The Empyrean", None, "hardcover"))

    def test_match_without_series_falls_through(self):
        m = dict(HC_MATCH, series_name=None, series_id=None,
                 series_position=None)
        self._stub(m)
        name, pos, src = pp._detect_series(
            "[Daemon 02] - Freedom.epub",
            {"isbn": "9781649374110"}, "Freedom", "Daniel Suarez")
        self.assertEqual((name, src), ("Daemon", "filename"))

    def test_flag_off_never_consults(self):
        pp.CONFIG["hardcover"] = False
        with mock.patch.object(pp, "_hardcover_client",
                               side_effect=AssertionError("consulted")):
            self.assertIsNone(pp._hardcover_lookup(
                {"isbn": "9781649374110"}, "Iron Flame",
                "Rebecca Yarros"))


class TestLanding(_PpCase):
    def _result(self, **over):
        result = {"title": "Iron Flame (The Empyrean, #2)",
                  "author": "Yarros, Rebecca",
                  "series_name": "The Empyrean",
                  "series_position": 2.0,
                  "series_source": "hardcover"}
        result.update(over)
        return result

    def test_match_lands_authoritative_with_audit(self):
        self._stub(HC_MATCH)
        result = self._result()
        pp._land_series_identity(result, {"isbn": "9781649374110"},
                                 result["title"], result["author"])
        self.assertEqual(result["title"], "Iron Flame")
        self.assertEqual(result["author"], "Rebecca Yarros")
        block = result["hardcover"]
        self.assertEqual(block["book_id"], 747398)
        self.assertEqual(block["series_id"], 41764)
        self.assertEqual(block["match_path"], "title")
        self.assertTrue(block["dominance"])
        self.assertEqual(block["file_title"],
                         "Iron Flame (The Empyrean, #2)")
        self.assertEqual(block["file_author"], "Yarros, Rebecca")
        self.assertEqual(result["series_key"], "hc-series:41764")

    def test_compilation_flag_lands_in_block(self):
        m = dict(HC_MATCH, compilation=True)
        self._stub(m)
        result = self._result()
        pp._land_series_identity(result, {"isbn": "9781649374110"},
                                 result["title"], result["author"])
        self.assertTrue(result["hardcover"]["compilation"])

    def test_seriesless_match_lands_none_series_key(self):
        # A confident Hardcover match on a book with no series
        # membership still lands — with series_key explicitly None,
        # so the key's presence means the identity model ran.
        m = dict(HC_MATCH, series_name=None, series_id=None,
                 series_position=None)
        self._stub(m)
        result = self._result(series_name=None, series_position=None,
                              series_source="none")
        pp._land_series_identity(result, {"isbn": "9781649374110"},
                                 result["title"], result["author"])
        self.assertIn("hardcover", result)
        self.assertIn("series_key", result)
        self.assertIsNone(result["series_key"])
        self.assertEqual(result["title"], "Iron Flame")

    def test_fallback_series_key_for_other_sources(self):
        self._stub(None)
        result = self._result(title="Freedom", author="Daniel Suarez",
                              series_name="Daemon", series_position=2.0,
                              series_source="filename")
        pp._land_series_identity(result, {"isbn": "9780000000000"},
                                 "Freedom", "Daniel Suarez")
        self.assertEqual(result["series_key"], "series:daemon|suarez")
        self.assertNotIn("hardcover", result)
        self.assertEqual(result["title"], "Freedom")

    def test_flag_off_leaves_record_byte_identical(self):
        pp.CONFIG["hardcover"] = False
        result = self._result()
        before = copy.deepcopy(result)
        pp._land_series_identity(result, {"isbn": "9781649374110"},
                                 "T", "A")
        self.assertEqual(result, before)

    def test_client_down_leaves_record_byte_identical(self):
        self._stub(None, available=False)
        result = self._result(series_name="Daemon",
                              series_source="filename")
        before = copy.deepcopy(result)
        pp._land_series_identity(result, {"isbn": "9781649374110"},
                                 "T", "A")
        self.assertEqual(result, before)

    def test_no_series_no_match_adds_no_keys(self):
        self._stub(None)
        result = self._result(series_name=None, series_position=None,
                              series_source="none")
        before = copy.deepcopy(result)
        pp._land_series_identity(result, {"isbn": "9780000000000"},
                                 "T", "A")
        self.assertEqual(result, before)
        self.assertNotIn("series_key", result)


class TestTokenHygiene(unittest.TestCase):
    def test_token_only_reaches_the_api_header(self):
        token = "SECRET-TOKEN-XYZ"
        seen = {}

        def handler(req):
            # urllib capitalizes header keys on the Request object.
            seen["auth"] = next(
                (v for k, v in req.headers.items()
                 if k.lower() == "authorization"), None)
            return _FakeResp({"data": {"editions": [FW_EDITION]}})

        opener = _FakeOpener(handler)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "hc.db"
            client = hc.HardcoverClient(
                token=token, cache=hc.HardcoverCache(db),
                warn=lambda _m: None, opener=opener,
                sleep=lambda _s: None)
            m = client.lookup(isbn="9781649374080")
            self.assertIsNotNone(m)
            self.assertEqual(seen["auth"], f"Bearer {token}")
            self.assertNotIn(token, json.dumps(m))
            self.assertNotIn(token.encode(), db.read_bytes())


if __name__ == "__main__":
    unittest.main()
