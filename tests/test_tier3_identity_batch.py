"""Regression tests for the tier3_store identity-and-integrity
batch (issues #9, #15, #34, #17, #18, #19).

One family: the store derived book identity and stored state too
trustingly, so real books landed in wrong buckets or phantom
state reported as reconciled. Each test names its issue and pins
the behaviour the issue's reproduction violated.

Hermetic: synthetic artefacts, temp SQLite stores, and a stubbed
urlopen for the Supabase stored-set path (no network).

Stdlib unittest only.
"""
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store = _load("tier3_store_identity_batch", "tier3_store.py")


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


def _artefact(**over):
    """Two principals, two minors. Minor B earns a stored row
    under derivation (validated claim); Sidekick does not."""
    art = {
        "title": "The Stand", "author": "An Author",
        "work_id": "W-42",
        "characters": [_char("Hero", 40), _char("Heroine", 30)],
        "minor_characters": [_char("Minor B", 5),
                             _char("Sidekick", 4)],
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


class _Resp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class BatchCase(unittest.TestCase):
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
            return conn.execute(
                "SELECT work_id, title FROM tier3_books "
                "ORDER BY work_id").fetchall()
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
        out = io.StringIO()
        with redirect_stdout(out):
            code = store.main(argv)
        return code, out.getvalue()

    def supabase_config(self):
        cfg = self.dir / "config.json"
        cfg.write_text(json.dumps({
            "supabase_url": "https://example.invalid",
            "supabase_key": "test-key"}), encoding="utf-8")
        return str(cfg)


class Issue9EmptyStoredSetTests(BatchCase):
    """#9: an empty Supabase stored set must not put principals
    in Tier 3 and make them approvable. On disk the mechanism is
    a SUCCESSFUL query returning [] being taken as the whole
    truth; derivation (principals + scored earners) is the
    artefact-verified floor that empty answer must fall back to.
    A FAILED query already raises, and must keep doing so with
    nothing written."""

    def test_empty_supabase_result_falls_back_to_derivation(self):
        cfg = self.supabase_config()
        with mock.patch.object(
                store.urllib.request, "urlopen",
                lambda req, timeout=None: _Resp(b"[]")):
            result = self.import_it(config_path=cfg)
        # Derivation excludes the principals and Minor B, so the
        # genuinely unverified names are the only Tier 3 rows.
        self.assertEqual(result["tier3_count"], 1)
        self.assertEqual(self.tier3_names("W-42"), {"Sidekick"})
        self.assertNotEqual(result["stored_source"], "supabase")
        # A principal is not approvable for push.
        code, _ = self.run_cli(
            ["approve", "--db", self.db, "--work", "W-42",
             "--name", "Hero"])
        self.assertEqual(code, 2)

    def test_failed_supabase_fetch_fails_closed_writing_nothing(self):
        cfg = self.supabase_config()

        def boom(req, timeout=None):
            raise urllib.error.URLError("no route")

        with mock.patch.object(store.urllib.request, "urlopen",
                               boom):
            with self.assertRaises(store.Tier3Error):
                self.import_it(config_path=cfg)
        # Fail closed means fail BEFORE any store rows exist:
        # the database file itself was never created.
        self.assertFalse(Path(self.db).exists())

    def test_nonempty_supabase_result_still_wins(self):
        cfg = self.supabase_config()
        body = json.dumps([{"name": "Hero"}, {"name": "Heroine"},
                           {"name": "Minor B"}]).encode()
        with mock.patch.object(
                store.urllib.request, "urlopen",
                lambda req, timeout=None: _Resp(body)):
            result = self.import_it(config_path=cfg)
        self.assertEqual(result["stored_source"], "supabase")
        self.assertEqual(result["stored_count"], 3)
        self.assertEqual(self.tier3_names("W-42"), {"Sidekick"})


class Issue15And34IdentityTests(BatchCase):
    """#15/#34: ONE canonical fallback identity (normalized
    title+author, the staging shape), and a match step that
    adopts an existing store book for the same normalized title
    instead of minting a second identity."""

    def test_fallback_identity_is_normalized_title_and_author(self):
        art = _artefact(title="  The   STAND ", author="An  Author")
        del art["work_id"]
        result = self.import_it(art)
        self.assertEqual(result["work_id"],
                         "title:the stand|an author")

    def test_title_variants_without_work_id_are_one_book(self):
        art1 = _artefact()
        del art1["work_id"]
        self.import_it(art1, name="first-file.json")
        art2 = _artefact(title="the stand ")
        del art2["work_id"]
        result = self.import_it(art2, name="other-name.json")
        self.assertEqual(result["work_id"],
                         "title:the stand|an author")
        self.assertEqual(len(self.books()), 1)

    def test_work_id_less_reimport_adopts_existing_work_id(self):
        self.import_it()  # W-42, "The Stand"
        code, _ = self.run_cli(
            ["approve", "--db", self.db, "--work", "W-42",
             "--name", "Sidekick"])
        self.assertEqual(code, 0)
        art = _artefact(title="the stand ")
        del art["work_id"]
        result = self.import_it(art, name="reimport.json")
        self.assertEqual(result["work_id"], "W-42")
        self.assertEqual(len(self.books()), 1)
        self.assertEqual(self.tier3_names("W-42"), {"Sidekick"})
        # The ledger decision recorded under W-42 still attaches.
        code, out = self.run_cli(
            ["list", "--db", self.db, "--work", "W-42"])
        self.assertEqual(code, 0)
        self.assertIn("[approved for push]", out)

    def test_explicit_import_adopts_prior_fallback_book(self):
        art = _artefact()
        del art["work_id"]
        first = self.import_it(art, name="fallback.json")
        self.assertTrue(first["work_id"].startswith("title:"))
        code, _ = self.run_cli(
            ["approve", "--db", self.db, "--work", first["work_id"],
             "--name", "Sidekick"])
        self.assertEqual(code, 0)
        result = self.import_it(name="with-id.json")  # W-42
        self.assertEqual(result["work_id"], "W-42")
        self.assertEqual(self.books(), [("W-42", "The Stand")])
        code, out = self.run_cli(
            ["list", "--db", self.db, "--work", "W-42"])
        self.assertIn("[approved for push]", out)

    def test_same_title_different_author_stays_two_books(self):
        art1 = _artefact(author="First Author")
        del art1["work_id"]
        self.import_it(art1, name="one.json")
        art2 = _artefact(author="Second Author")
        del art2["work_id"]
        self.import_it(art2, name="two.json")
        self.assertEqual(len(self.books()), 2)


class Issue17ReadOnlyCommandsTests(BatchCase):
    """#17: report/list on a store that does not exist must not
    create the file, and a never-imported book/store must never
    read as RECONCILED."""

    def test_report_on_missing_db_creates_no_file(self):
        missing = str(self.dir / "typo.db")
        report = store.build_report(missing)
        self.assertFalse(Path(missing).exists())
        self.assertIn("NOT IMPORTED", report)
        self.assertNotIn("RECONCILED", report)

    def test_list_on_missing_db_creates_no_file(self):
        missing = str(self.dir / "typo.db")
        code, out = self.run_cli(["list", "--db", missing])
        self.assertFalse(Path(missing).exists())
        self.assertIn("import a preview first", out)

    def test_report_on_empty_existing_store_is_not_imported(self):
        # A store file that exists (schema created by a write
        # command) but holds no books is still not reconciled:
        # an empty verdict set is not a pass.
        conn = store._connect(self.db)
        conn.close()
        report = store.build_report(self.db)
        self.assertIn("NOT IMPORTED", report)
        self.assertNotIn("RECONCILED", report)


class Issue18ConfigTests(BatchCase):
    """#18: a present-but-unparseable processor config is a loud
    error naming the file; a missing config derives silently."""

    def test_invalid_json_config_is_a_loud_named_error(self):
        cfg = self.dir / "config.json"
        cfg.write_text("{not json", encoding="utf-8")
        with self.assertRaises(store.Tier3Error) as cm:
            self.import_it(config_path=str(cfg))
        self.assertIn(str(cfg), str(cm.exception))
        self.assertFalse(Path(self.db).exists())

    def test_partial_supabase_config_is_loud(self):
        cfg = self.dir / "config.json"
        cfg.write_text(json.dumps(
            {"supabase_url": "https://example.invalid"}),
            encoding="utf-8")
        with self.assertRaises(store.Tier3Error):
            self.import_it(config_path=str(cfg))

    def test_missing_config_still_derives_silently(self):
        result = self.import_it()
        self.assertEqual(result["stored_source"], "derived")
        self.assertEqual(result["stored_count"], 3)


class Issue19BomTests(BatchCase):
    """#19: a UTF-8 BOM must not swallow the first stored name.
    The same decoding is applied to the tool's other operator-
    edited text inputs (the preview artefact and the processor
    config), which are hand-handled files of the same class."""

    def test_bom_stored_names_file_keeps_first_name(self):
        names = self.dir / "stored.txt"
        names.write_bytes(
            b"\xef\xbb\xbfHero\nHeroine\nMinor B\n")
        result = self.import_it(stored_names_path=str(names))
        self.assertEqual(result["stored_count"], 3)
        self.assertEqual(result["stored_absent_from_roster"], [])
        self.assertEqual(self.tier3_names("W-42"), {"Sidekick"})

    def test_bom_preview_artefact_loads(self):
        path = self.dir / "bom-book.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            _artefact()).encode("utf-8"))
        result = store.import_preview(
            self.db, str(path), config_path=self.missing_config)
        self.assertEqual(result["work_id"], "W-42")

    def test_bom_config_parses_instead_of_erroring(self):
        cfg = self.dir / "config.json"
        cfg.write_bytes(b"\xef\xbb\xbf{}")
        result = self.import_it(config_path=str(cfg))
        self.assertEqual(result["stored_source"], "derived")


if __name__ == "__main__":
    unittest.main()
