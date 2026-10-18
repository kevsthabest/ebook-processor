"""Work-resolution regression tests: author-titled works, duplicate work rows,
edition/variant splitting, invisible characters, and title-casing consistency.
Stdlib unittest only. No network, no Supabase (sb is mocked), no real ebooks.
Run: python -m unittest discover -s tests -v
"""
import json
import re
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import unquote
import importlib.util

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _SbMock:
    """Minimal stand-in for pp.sb: canned responses by (table, method, params
    substring). Records every call for assertion."""

    def __init__(self):
        self.calls = []
        self.routes = []

    def __call__(self, table, method="GET", data=None, params="", prefer=None):
        self.calls.append({"table": table, "method": method,
                           "data": data, "params": params})
        for (t, sub, m, resp) in self.routes:
            if t == table and m == method and sub in params:
                return resp() if callable(resp) else resp
        return []

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


class TestWorkKey(unittest.TestCase):
    def test_edition_subtitle_variants_share_key(self):
        self.assertEqual(pp._work_key("Artemis", "Andy Weir"),
                         pp._work_key("Artemis: A Novel", "Andy Weir"))

    def test_stacked_edition_suffixes_collapse(self):
        self.assertEqual(
            pp._work_key("Fourth Wing (Part 1 of 2) [Dramatized Adaptation]",
                         "Rebecca Yarros"),
            pp._work_key("Fourth Wing", "Rebecca Yarros"))

    def test_zero_width_space_neutralized(self):
        self.assertEqual(
            pp._work_key("A Court of Silver\u200bFlames", "Sarah J. Maas"),
            pp._work_key("A Court of Silver Flames", "Sarah J. Maas"))

    def test_zero_width_space_inside_word_keeps_boundary(self):
        # ZWSP is a word-break opportunity: "Silver\u200bFlames" == "Silver Flames"
        self.assertEqual(
            pp._work_key("Silver\u200bFlames", "Anon"),
            pp._work_key("Silver Flames", "Anon"))

    def test_series_paren_stripped(self):
        self.assertEqual(
            pp._work_key("Bitten (Women of the Otherworld, Book 1)",
                         "Kelley Armstrong"),
            pp._work_key("Bitten", "Kelley Armstrong"))

    def test_unabridged_stripped(self):
        self.assertEqual(pp._work_key("Citadel (Unabridged)", "Kate Mosse"),
                         pp._work_key("Citadel", "Kate Mosse"))

    def test_real_subtitle_kept(self):
        self.assertNotEqual(pp._work_key("Dune: House Atreides", "Brian Herbert"),
                            pp._work_key("Dune", "Brian Herbert"))

    def test_author_last_first_canonicalized(self):
        self.assertEqual(pp._work_key("Angels & Demons", "Brown, Dan"),
                         pp._work_key("Angels & Demons", "Dan Brown"))
        self.assertEqual(pp._canonical_author("Brown, Dan"), "Dan Brown")
        self.assertEqual(pp._canonical_author("Dan Brown"), "Dan Brown")


class TestDisplayTitle(unittest.TestCase):
    def test_lowercase_gets_title_case(self):
        self.assertEqual(pp._display_title("radicalized"), "Radicalized")

    def test_already_cased_untouched(self):
        self.assertEqual(pp._display_title("Fourth Wing"), "Fourth Wing")
        self.assertEqual(pp._display_title("IT"), "IT")  # acronym survives

    def test_apostrophe_aware(self):
        self.assertEqual(pp._display_title("o'brien's tale"), "O'Brien's Tale")

    def test_correct_metadata_canonicalizes_display(self):
        # No ISBN -> no OpenLibrary correction, but display forms canonicalized.
        t, a, fixed = pp.correct_metadata("radicalized", "Brown, Dan", {})
        self.assertEqual((t, a, fixed), ("Radicalized", "Dan Brown", False))


class TestResolveWork(unittest.TestCase):
    def setUp(self):
        self.mock = _SbMock()
        self.orig = pp.sb
        pp.sb = self.mock

    def tearDown(self):
        pp.sb = self.orig

    def test_author_titled_work_refused(self):
        wid, how = pp.resolve_work("Dan Brown", "Dan Brown", {})
        self.assertEqual((wid, how), (None, "author_title"))
        self.assertEqual(self.mock.posts(), [],
                         "author-titled work must never be created")

    def test_author_titled_refused_even_with_create(self):
        wid, how = pp.resolve_work("Daniel Suarez", "Daniel Suarez",
                                   {"isbn": "9780000000000"}, create=True)
        self.assertEqual((wid, how), (None, "author_title"))
        self.assertEqual(self.mock.posts(), [])

    def test_edition_variant_matches_existing_work(self):
        self.mock.routes = [
            ("works", "title_norm=eq.artemis", "GET", [{"id": "w-artemis"}]),
        ]
        wid, how = pp.resolve_work("Artemis: A Novel", "Andy Weir", {})
        self.assertEqual((wid, how), ("w-artemis", "title_author"))
        get_calls = [c for c in self.mock.calls
                     if c["table"] == "works" and c["method"] == "GET"]
        self.assertTrue(get_calls)
        self.assertIn("title_norm=eq.artemis&", get_calls[0]["params"])

    def test_created_work_uses_canonical_display_title(self):
        self.mock.routes = [("works", "", "POST", [{"id": "w-new"}])]
        wid, how = pp.resolve_work("radicalized", "Cory Doctorow", {})
        self.assertEqual((wid, how), ("w-new", "created"))
        posted = self.mock.posts()[0]["data"]
        self.assertEqual(posted["title"], "Radicalized")
        self.assertEqual(posted["title_norm"], "radicalized")
        self.assertEqual(posted["authors"], ["Cory Doctorow"])
        # Standing rule: JSON output changes need serialization/round-trip coverage.
        self.assertEqual(json.loads(json.dumps(posted))["title"], "Radicalized")

    def test_created_work_strips_edition_suffix_from_stored_title(self):
        self.mock.routes = [("works", "", "POST", [{"id": "w-new"}])]
        wid, how = pp.resolve_work(
            "Fourth Wing (Part 1 of 2) [Dramatized Adaptation]",
            "Rebecca Yarros", {})
        self.assertEqual(how, "created")
        posted = self.mock.posts()[0]["data"]
        self.assertEqual(posted["title"], "Fourth Wing")
        self.assertEqual(posted["title_norm"], "fourth wing")

    def test_isbn_falls_back_to_provider_ids(self):
        # Same book re-run with a different title form: the edition lookup
        # misses, but the work row carries the ISBN in provider_ids.
        self.mock.routes = [
            ("works", "provider_ids", "GET", [{"id": "w5"}]),
        ]
        wid, how = pp.resolve_work("Artemis", "Andy Weir",
                                   {"isbn": "9780553441746"})
        self.assertEqual(wid, "w5")
        self.assertEqual(how, "work_isbn:9780553441746")

    def test_preview_never_posts(self):
        wid, how = pp.resolve_work("Some New Book", "Some Author", {},
                                   create=False)
        self.assertEqual((wid, how), (None, "new"))
        self.assertEqual(self.mock.posts(), [])

    def test_db_error_propagates(self):
        pp.sb = lambda *a, **k: None  # every call fails
        try:
            self.assertEqual(pp.resolve_work("Dune", "Frank Herbert", {}),
                             (None, "db_error"))
        finally:
            pp.sb = self.mock


def _canonical_norm(s):
    """Verbatim port of the canonical work norm shared by the app
    (js/157 tropeNormIdent in the Cozy Libram repo) and the backfill
    script (scripts/backfill-work-identities.js normIdent) — the two
    reference implementations agree exactly: lowercase, NFKD, strip
    U+0300..U+036F combining marks, map every [^a-z0-9]+ run to one
    space, trim, strip one leading "the"."""
    s = (s or "").lower()
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[\u0300-\u036f]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    return s


class TestCanonicalNorm(unittest.TestCase):
    """Issue #30: the processor's norm must produce byte-for-byte the
    canonical work key for the same input, or the same book mints a
    second works row depending on which surface saw it first."""

    VECTORS = [
        # Leading article: only "the" strips, in any casing, once.
        ("The Hunger Games", "hunger games"),
        ("THE HUNGER GAMES", "hunger games"),
        ("ThE HuNgEr GaMeS", "hunger games"),
        ("The", "the"),              # no trailing space -> nothing strips
        ("The The", "the"),          # exactly one leading article strips
        ("A Tale of Two Cities", "a tale of two cities"),
        ("Naïve—The Beginning", "naive the beginning"),  # inner "the" kept
        # Accents that decompose fold to their base letter.
        ("Café Society", "cafe society"),
        ("The Café Society", "cafe society"),
        ("José Ángel", "jose angel"),
        ("L'Étranger", "l etranger"),
        ("İstanbul", "istanbul"),    # lowercases to i + U+0307, mark stripped
        # Letters with no decomposition become a space, like any other
        # non-[a-z0-9] run — they are never kept as letters.
        ("Søren Kierkegaard", "s ren kierkegaard"),
        ("Æsop's Fables", "sop s fables"),
        ("Œuvres complètes", "uvres completes"),
        ("Faß", "fa"),
        # Punctuation runs collapse to one space; apostrophes split.
        ("Dune: Part—Two!!", "dune part two"),
        ("snake_case_title", "snake case title"),
        # Digits survive; mixed case folds.
        ("Apollo 13", "apollo 13"),
        ("", ""),
        # The same norm keys authors: a leading "the" strips there too.
        ("The Authors", "authors"),
    ]

    def test_norm_matches_canonical_vectors(self):
        for raw, expected in self.VECTORS:
            with self.subTest(raw=raw):
                self.assertEqual(pp.norm(raw), expected)
                self.assertEqual(pp.norm(raw), _canonical_norm(raw))

    def test_norm_none_is_empty(self):
        self.assertEqual(pp.norm(None), "")

    def test_same_book_same_work_key_across_surfaces(self):
        # Title/author forms the app or backfill would key differently
        # from a raw ebook's metadata must still land on one work key.
        self.assertEqual(
            pp._work_key("The Café Society", "Søren Kierkegaard"),
            pp._work_key("Cafe Society", "S Ren Kierkegaard"))
        self.assertEqual(
            pp._work_key("The Hunger Games", "Suzanne Collins"),
            ("hunger games", "suzanne collins"))


class _IsbnStore:
    """Fake sb() keyed by exact ISBN string, like the real tables: an
    editions row or a works.provider_ids entry matches only the exact
    ISBN form it was stored under. Title/author lookups never match."""

    def __init__(self, editions=None, provider_isbns=None):
        self.editions = dict(editions or {})
        self.provider_isbns = dict(provider_isbns or {})
        self.calls = []

    def __call__(self, table, method="GET", data=None, params="", prefer=None):
        self.calls.append({"table": table, "method": method,
                           "data": data, "params": params})
        if method == "POST":
            return [{"id": "w-created"}]
        m = re.search(r"isbn=eq\.([^&]+)", params or "")
        isbn = unquote(m.group(1)) if m else None
        if table == "editions" and isbn in self.editions:
            return [{"work_id": self.editions[isbn]}]
        if table == "works" and "provider_ids" in (params or "") \
                and isbn in self.provider_isbns:
            return [{"id": self.provider_isbns[isbn]}]
        return []

    def probed_isbns(self):
        out = []
        for c in self.calls:
            if c["method"] != "GET":
                continue
            m = re.search(r"isbn=eq\.([^&]+)", c["params"] or "")
            if m:
                out.append(unquote(m.group(1)))
        return out

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


class TestIsbnForms(unittest.TestCase):
    """Issue #31: ISBN resolution must probe both ISBN forms. Fixture
    pairs are real check-digit-valid conversions (the X pair exercises
    the mod-11 'X' check digit in both directions)."""

    PAIR_A = ("0306406152", "9780306406157")
    PAIR_X = ("080442957X", "9780804429573")
    ISBN_979 = "9791234567896"  # valid 979-13: no ISBN-10 form exists

    def test_fixtures_are_valid_isbns(self):
        for isbn in (*self.PAIR_A, *self.PAIR_X, self.ISBN_979):
            self.assertEqual(pp._validate_isbn(isbn), isbn)
        self.assertEqual(pp._clean_isbn(self.PAIR_A[0]), self.PAIR_A[1])
        self.assertEqual(pp._clean_isbn(self.PAIR_X[0]), self.PAIR_X[1])

    def test_isbn_forms_helper(self):
        self.assertEqual(pp._isbn_forms(self.PAIR_A[1]),
                         [self.PAIR_A[1], self.PAIR_A[0]])
        self.assertEqual(pp._isbn_forms(self.PAIR_A[0]),
                         [self.PAIR_A[0], self.PAIR_A[1]])
        self.assertEqual(pp._isbn_forms(self.PAIR_X[1]),
                         [self.PAIR_X[1], self.PAIR_X[0]])
        self.assertEqual(pp._isbn_forms(self.PAIR_X[0]),
                         [self.PAIR_X[0], self.PAIR_X[1]])
        self.assertEqual(pp._isbn_forms(self.ISBN_979), [self.ISBN_979])


class TestResolveWorkIsbnForms(unittest.TestCase):
    def setUp(self):
        self.orig = pp.sb

    def tearDown(self):
        pp.sb = self.orig

    def _resolve(self, store, isbn):
        pp.sb = store
        return pp.resolve_work("Unmatched Title", "Unmatched Author",
                               {"isbn": isbn})

    def test_isbn13_book_finds_isbn10_edition(self):
        store = _IsbnStore(editions={"080442957X": "w-x"})
        wid, how = self._resolve(store, "9780804429573")
        self.assertEqual((wid, how), ("w-x", "isbn:080442957X"))

    def test_isbn13_book_finds_isbn10_provider_work(self):
        store = _IsbnStore(provider_isbns={"0306406152": "w-10"})
        wid, how = self._resolve(store, "9780306406157")
        self.assertEqual((wid, how), ("w-10", "work_isbn:0306406152"))

    def test_isbn10_book_finds_isbn13_edition(self):
        store = _IsbnStore(editions={"9780306406157": "w-13"})
        wid, how = self._resolve(store, "0306406152")
        self.assertEqual((wid, how), ("w-13", "isbn:9780306406157"))

    def test_isbn10_book_finds_isbn13_provider_work(self):
        store = _IsbnStore(provider_isbns={"9780804429573": "w-13x"})
        wid, how = self._resolve(store, "080442957X")
        self.assertEqual((wid, how), ("w-13x", "work_isbn:9780804429573"))

    def test_979_probes_only_its_own_form(self):
        store = _IsbnStore()
        pp.sb = store
        wid, how = pp.resolve_work("Unmatched Title", "Unmatched Author",
                                   {"isbn": "9791234567896"}, create=False)
        self.assertEqual((wid, how), (None, "new"))
        self.assertEqual(set(store.probed_isbns()), {"9791234567896"})
        self.assertEqual(store.posts(), [])


if __name__ == "__main__":
    unittest.main()
