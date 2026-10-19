"""Regression tests for the tier3_store multi-match adoption
gate (issue #56, Decision 2026-10-18).

The shipped adoption step matched a work_id-less import against
stored books by normalized title and adopted matches[0] after a
sort — deterministic, but arbitrary when several stored books
share the title and the author guard cannot separate them (an
author-less import is the canonical case). A wrong adoption
replaces the adopted book's Tier 3 rows.

The ruling: multi-match adoption KEEPS existing, but only behind
the explicit --allow-multi-match-adoption flag, and it warns
loudly either way. Without the flag the import mints its own
canonical fallback identity instead of adopting; single-match
adoption and the author guard are unchanged in both modes.

Hermetic: synthetic artefacts, temp SQLite stores, no network.

Stdlib unittest only.
"""
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store = _load("tier3_store_multi_match", "tier3_store.py")

FLAG = "--allow-multi-match-adoption"
FALLBACK_AUTHORLESS = "title:the stand|"


def _char(name, appearances=3):
    return {"name": name, "aliases": [], "unresolved_mentions": [],
            "role": "minor", "description": f"About {name}.",
            "appearances": appearances, "chapters_present": 2,
            "grounded": True, "evidence": f"{name} appeared."}


def _claim(frm, to, ctype="friend", state=None, p=None, note=None):
    claim = {"from": frm, "to": to, "type": ctype,
             "evidence": "A quote.", "chapter": "Chapter 1",
             "source": "extracted"}
    if state is not None:
        claim["validation_state"] = state
    if p is not None:
        claim["validation_p"] = p
    if note is not None:
        claim["validation_note"] = note
    return claim


def _artefact(minors=("Minor B", "Sidekick"), **over):
    """Principals Hero/Heroine; Minor B earns a stored row under
    derivation (validated claim), the other minors do not."""
    art = {
        "title": "The Stand", "author": "An Author",
        "work_id": "W-42",
        "characters": [_char("Hero", 40), _char("Heroine", 30)],
        "minor_characters": [_char(n, 5) for n in minors],
        "relationships": [],
        "minor_sourced_relationships": [
            _claim("Minor B", "Hero", "child", state="validated",
                   p=0.9, note="kept"),
        ],
        "dropped_relationships": [],
        "possible_merges": [],
    }
    art.update(over)
    return art


class MultiMatchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "tier3.db")
        self.missing_config = str(self.dir / "no-config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write_artefact(self, art, name="book.json"):
        path = self.dir / name
        path.write_text(json.dumps(art), encoding="utf-8")
        return str(path)

    def import_it(self, art=None, name="book.json", **kw):
        path = self.write_artefact(art or _artefact(), name=name)
        kw.setdefault("config_path", self.missing_config)
        return store.import_preview(self.db, path, **kw)

    def books(self):
        conn = sqlite3.connect(self.db)
        try:
            return sorted(r[0] for r in conn.execute(
                "SELECT work_id FROM tier3_books"))
        finally:
            conn.close()

    def tier3_names(self, work_id):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM tier3_characters "
                "WHERE work_id = ?", (work_id,))}
        finally:
            conn.close()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = store.main(argv)
        return code, out.getvalue() + err.getvalue()

    def seed_two_same_title_books(self):
        """Two stored books, one title, different authors: the
        pair an author-less import cannot tell apart."""
        self.import_it(
            _artefact(work_id="W-A", author="First Author"),
            name="a.json")
        self.import_it(
            _artefact(work_id="W-B", author="Second Author"),
            name="b.json")

    def authorless_import(self, **over):
        art = _artefact(**over)
        del art["work_id"]
        del art["author"]
        return art


class MultiMatchBlockedTests(MultiMatchCase):
    """Without the flag: no adoption, the fallback identity is
    minted instead, and the skip is reported by title with its
    match count and counted separately."""

    def test_multi_match_without_flag_does_not_adopt(self):
        self.seed_two_same_title_books()
        art = self.authorless_import(minors=(
            "Minor B", "Sidekick", "Newcomer"))
        result = self.import_it(art, name="multi.json")
        # Minted its own fallback identity; neither stored book
        # was adopted, so both survive untouched alongside it.
        self.assertEqual(result["work_id"], FALLBACK_AUTHORLESS)
        self.assertEqual(self.books(),
                         ["W-A", "W-B", FALLBACK_AUTHORLESS])
        # The harm the issue names — a wrong adoption REPLACES
        # the adopted book's Tier 3 rows — must not happen: W-A
        # keeps exactly its seeded rows, and the new roster
        # landed under the fallback identity instead.
        self.assertEqual(self.tier3_names("W-A"), {"Sidekick"})
        self.assertEqual(self.tier3_names("W-B"), {"Sidekick"})
        self.assertEqual(self.tier3_names(FALLBACK_AUTHORLESS),
                         {"Sidekick", "Newcomer"})

    def test_multi_match_without_flag_is_counted_separately(self):
        self.seed_two_same_title_books()
        result = self.import_it(self.authorless_import(),
                                name="multi.json")
        self.assertEqual(result["multi_match_count"], 2)
        self.assertEqual(result["multi_match_blocked"], 1)
        self.assertEqual(result["multi_match_adopted"], 0)
        self.assertEqual(result["multi_match_matches"],
                         ["W-A", "W-B"])

    def test_multi_match_without_flag_cli_warns_and_exits_zero(self):
        self.seed_two_same_title_books()
        path = self.write_artefact(self.authorless_import(),
                                   name="multi.json")
        code, out = self.run_cli(
            ["import", "--db", self.db, "--preview", path,
             "--config", self.missing_config])
        # The store's partial-skip convention: a loud warning on
        # an otherwise successful import (exit 0), not a failure.
        self.assertEqual(code, 0)
        self.assertIn("WARNING", out)
        self.assertIn("The Stand", out)
        self.assertIn("2", out)
        self.assertIn("multi-match", out)
        self.assertIn("skipped: 1", out)
        self.assertIn(FLAG, out)
        self.assertIn(FALLBACK_AUTHORLESS, out)
        self.assertEqual(self.books(),
                         ["W-A", "W-B", FALLBACK_AUTHORLESS])


class MultiMatchAllowedTests(MultiMatchCase):
    """With the flag: adoption proceeds exactly as shipped
    (matches[0] after the existing sort), and the warning still
    prints — the flag enables, it does not silence."""

    def test_multi_match_with_flag_adopts_sorted_first(self):
        self.seed_two_same_title_books()
        result = self.import_it(
            self.authorless_import(), name="multi.json",
            allow_multi_match_adoption=True)
        self.assertEqual(result["work_id"], "W-A")
        self.assertEqual(self.books(), ["W-A", "W-B"])
        self.assertEqual(result["multi_match_count"], 2)
        self.assertEqual(result["multi_match_adopted"], 1)
        self.assertEqual(result["multi_match_blocked"], 0)

    def test_multi_match_with_flag_cli_still_warns(self):
        self.seed_two_same_title_books()
        path = self.write_artefact(self.authorless_import(),
                                   name="multi.json")
        code, out = self.run_cli(
            ["import", "--db", self.db, "--preview", path,
             "--config", self.missing_config, FLAG])
        self.assertEqual(code, 0)
        self.assertIn("WARNING", out)
        self.assertIn("The Stand", out)
        self.assertIn("2", out)
        self.assertIn("multi-match adoptions: 1", out)
        self.assertEqual(self.books(), ["W-A", "W-B"])


class SingleMatchUnchangedTests(MultiMatchCase):
    """A single stored match adopts exactly as shipped, with
    and without the flag, and no multi-match surface fires."""

    def _single_match_result(self, **kw):
        self.import_it()  # W-42, "The Stand" / "An Author"
        return self.import_it(self.authorless_import(),
                              name="reimport.json", **kw)

    def test_single_match_without_flag_still_adopts(self):
        result = self._single_match_result()
        self.assertEqual(result["work_id"], "W-42")
        self.assertEqual(self.books(), ["W-42"])
        self.assertEqual(result["multi_match_count"], 0)
        self.assertEqual(result["multi_match_blocked"], 0)
        self.assertEqual(result["multi_match_adopted"], 0)

    def test_single_match_with_flag_still_adopts(self):
        result = self._single_match_result(
            allow_multi_match_adoption=True)
        self.assertEqual(result["work_id"], "W-42")
        self.assertEqual(self.books(), ["W-42"])
        self.assertEqual(result["multi_match_count"], 0)

    def test_single_match_cli_has_no_multi_match_warning(self):
        self.import_it()
        path = self.write_artefact(self.authorless_import(),
                                   name="reimport.json")
        code, out = self.run_cli(
            ["import", "--db", self.db, "--preview", path,
             "--config", self.missing_config])
        self.assertEqual(code, 0)
        self.assertNotIn("multi-match", out)


class AuthorGuardUnchangedTests(MultiMatchCase):
    """The author guard blocks a match when both sides name
    differing authors — in both modes. The flag enables a
    multi-match adoption; it never overrides the guard."""

    def _guarded_import(self, **kw):
        self.import_it(
            _artefact(work_id="W-A", author="First Author"),
            name="a.json")
        art = _artefact(author="Second Author")
        del art["work_id"]
        return self.import_it(art, name="other.json", **kw)

    def test_author_guard_blocks_without_flag(self):
        result = self._guarded_import()
        self.assertEqual(result["work_id"],
                         "title:the stand|second author")
        self.assertEqual(self.books(),
                         ["W-A", "title:the stand|second author"])
        self.assertEqual(result["multi_match_count"], 0)

    def test_author_guard_blocks_with_flag(self):
        result = self._guarded_import(
            allow_multi_match_adoption=True)
        self.assertEqual(result["work_id"],
                         "title:the stand|second author")
        self.assertEqual(self.books(),
                         ["W-A", "title:the stand|second author"])
        self.assertEqual(result["multi_match_count"], 0)
        self.assertEqual(result["multi_match_adopted"], 0)


class ExplicitMultiMatchTests(MultiMatchCase):
    """The rename direction is an adoption too: an explicit
    work_id folding a fallback book onto itself must not pick
    one of several fallback matches silently either."""

    def seed_two_fallback_books(self):
        art1 = _artefact(author="First Author")
        del art1["work_id"]
        self.import_it(art1, name="f1.json")
        art2 = _artefact(author="Second Author")
        del art2["work_id"]
        self.import_it(art2, name="f2.json")

    def explicit_authorless(self):
        art = _artefact(work_id="W-NEW")
        del art["author"]
        return art

    def test_explicit_multi_match_without_flag_does_not_rename(self):
        self.seed_two_fallback_books()
        result = self.import_it(self.explicit_authorless(),
                                name="new.json")
        self.assertEqual(result["work_id"], "W-NEW")
        self.assertIsNone(result["adopted_from"])
        self.assertEqual(result["multi_match_blocked"], 1)
        self.assertEqual(len(self.books()), 3)

    def test_explicit_multi_match_with_flag_renames_sorted_first(self):
        self.seed_two_fallback_books()
        result = self.import_it(
            self.explicit_authorless(), name="new.json",
            allow_multi_match_adoption=True)
        self.assertEqual(result["work_id"], "W-NEW")
        self.assertEqual(result["adopted_from"],
                         "title:the stand|first author")
        self.assertEqual(result["multi_match_adopted"], 1)
        self.assertEqual(self.books(),
                         ["W-NEW", "title:the stand|second author"])


if __name__ == "__main__":
    unittest.main()
