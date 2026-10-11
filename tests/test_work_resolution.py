"""Work-resolution regression tests: author-titled works, duplicate work rows,
edition/variant splitting, invisible characters, and title-casing consistency.
Stdlib unittest only. No network, no Supabase (sb is mocked), no real ebooks.
Run: python -m unittest discover -s tests -v
"""
import json
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
