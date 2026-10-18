"""Tests for tier3_store.py, the standalone Tier 3 local store
(design: tier3-local-store-design-2026-10-18.md).

Hermetic throughout: synthetic preview artefacts and SQLite
databases in temp dirs, no network, no Supabase. The Supabase
stored-set path is exercised only in its config-absent form
(fall through to derivation), because the contract that must
never regress is the resolution order and the loud failures.

The central pins: the stored set is excluded from Tier 3 however
it was resolved; an import replaces a book's characters and
candidates atomically while the push ledger — the User's own
curation data — survives untouched; unconfirmed candidates of
all four kinds are captured, including a dropped type proposal
found only in the chapter deltas; and the report's
reconciliation equation (roster == stored + Tier 3) is checked,
not assumed.

Stdlib unittest only. Run: python -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store = _load("tier3_store_under_test", "tier3_store.py")


def _char(name, appearances=3, mentions=None, **kw):
    entry = {
        "name": name, "aliases": kw.get("aliases", []),
        "unresolved_mentions": mentions or [],
        "role": kw.get("role", "minor"),
        "description": kw.get("description", f"About {name}."),
        "appearances": appearances,
        "chapters_present": kw.get("chapters_present", 2),
        "grounded": kw.get("grounded", True),
        "evidence": kw.get("evidence", f"{name} appeared."),
    }
    return entry


def _claim(frm, to, ctype="friend", state=None, p=None,
           note=None, chapter="Chapter 1"):
    claim = {"from": frm, "to": to, "type": ctype,
             "evidence": "A quote.", "chapter": chapter,
             "source": "extracted"}
    if state is not None:
        claim["validation_state"] = state
    if p is not None:
        claim["validation_p"] = p
    if note is not None:
        claim["validation_note"] = note
    return claim


def _artefact(**over):
    """A small synthetic book: two principals, three minors.
    Minor B earns a stored row (validated claim); Minor C's only
    claim is review_required WITHOUT a probability (unscored —
    no row under the shipped rule); Minor D sources only a
    budget_exhausted claim."""
    art = {
        "title": "Test Book", "author": "An Author",
        "work_id": "work-test-1",
        "characters": [
            _char("Principal A", appearances=40,
                  mentions=["the stranger"]),
            _char("Principal B", appearances=30),
        ],
        "minor_characters": [
            _char("Minor B", appearances=5),
            _char("Minor C", appearances=4),
            _char("Minor D", appearances=2),
        ],
        "relationships": [],
        "minor_sourced_relationships": [
            _claim("Minor B", "Principal A", "child",
                   state="validated", p=0.9, note="kept"),
            _claim("Minor C", "Principal A", "friend",
                   state="review_required"),
            _claim("Minor D", "Principal B", "crewmate",
                   state="budget_exhausted"),
        ],
        "dropped_relationships": [
            _claim("Minor D", "Principal A", "spouse",
                   state="rejected", note="no_evidence"),
        ],
        "possible_merges": [
            {"name_a": "Minor C", "name_b": "Minor Cee",
             "p_yes": 0.72, "reason": "below threshold (0.85)"},
        ],
        "chapter_data": {"chapters": {
            "1": {"a": {"relationships": [
                # registered type, delta-only: NOT a proposal
                _claim("Principal A", "Minor B", "sire"),
                # unregistered type: a dropped proposal
                _claim("Minor D", "Principal A", "boss"),
            ]}},
        }},
    }
    art.update(over)
    return art


class Tier3Case(unittest.TestCase):
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

    def import_it(self, art=None, **kw):
        path = self.write_artefact(art or _artefact())
        kw.setdefault("config_path", self.missing_config)
        return store.import_preview(self.db, path, **kw)

    def tier3_names(self, work_id="work-test-1"):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM tier3_characters "
                "WHERE work_id = ?", (work_id,))}
        finally:
            conn.close()

    def candidate_kinds(self, work_id="work-test-1"):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT kind, COUNT(*) FROM tier3_candidates "
                "WHERE work_id = ? GROUP BY kind", (work_id,))}
        finally:
            conn.close()

    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = store.main(argv)
        return code, out.getvalue()


class ImportTests(Tier3Case):
    def test_derivation_excludes_principals_and_scored_earners(self):
        result = self.import_it()
        self.assertEqual(result["stored_source"], "derived")
        # 2 principals + Minor B (validated) stored; C and D not.
        self.assertEqual(result["stored_count"], 3)
        self.assertEqual(self.tier3_names(), {"Minor C", "Minor D"})
        self.assertEqual(result["tier3_count"], 2)

    def test_unscored_review_required_does_not_earn(self):
        # Minor C's claim is review_required with no probability:
        # the probability is the proof a draw happened, so C
        # stays in Tier 3 carrying that claim.
        self.import_it()
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT claims_json FROM tier3_characters "
                "WHERE name = 'Minor C'").fetchone()
        finally:
            conn.close()
        claims = json.loads(row[0])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["validation_state"],
                         "review_required")

    def test_candidates_of_all_four_kinds(self):
        self.import_it()
        kinds = self.candidate_kinds()
        self.assertEqual(kinds.get("merge"), 1)
        self.assertEqual(kinds.get("claim"), 1)   # the no_evidence drop
        self.assertEqual(kinds.get("mention"), 1)  # "the stranger"
        self.assertEqual(kinds.get("type_proposal"), 1)  # "boss"

    def test_registered_delta_only_type_is_not_a_proposal(self):
        self.import_it()
        conn = sqlite3.connect(self.db)
        try:
            payloads = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM tier3_candidates "
                "WHERE kind = 'type_proposal'")]
        finally:
            conn.close()
        self.assertEqual([p["type"] for p in payloads], ["boss"])

    def test_no_chapter_data_means_no_type_proposals(self):
        art = _artefact()
        del art["chapter_data"]
        result = self.import_it(art)
        self.assertFalse(result["has_chapter_data"])
        self.assertNotIn("type_proposal", self.candidate_kinds())

    def test_character_rows_carry_the_record(self):
        self.import_it()
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM tier3_characters "
                "WHERE name = 'Minor D'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["appearances"], 2)
        self.assertEqual(row["grounded"], 1)
        self.assertEqual(row["description"], "About Minor D.")
        claims = json.loads(row["claims_json"])
        # D's budget_exhausted shipped claim AND his dropped one.
        self.assertEqual(len(claims), 2)


class StoredSetResolutionTests(Tier3Case):
    def test_stored_names_file_wins_over_derivation(self):
        names_file = self.dir / "stored.txt"
        names_file.write_text("Principal A\nMinor B\nMinor C\n",
                              encoding="utf-8")
        result = self.import_it(stored_names_path=str(names_file))
        self.assertEqual(result["stored_source"], "stored-names")
        # Derivation would also store Principal B; the file does
        # not name B, so B lands in Tier 3 — the file wins.
        self.assertEqual(self.tier3_names(),
                         {"Principal B", "Minor D"})

    def test_missing_roster_lists_fail_loudly(self):
        art = _artefact()
        del art["characters"]
        del art["minor_characters"]
        with self.assertRaises(store.Tier3Error):
            self.import_it(art)

    def test_derivation_without_claim_lists_fails_loudly(self):
        art = _artefact()
        del art["minor_sourced_relationships"]
        with self.assertRaises(store.Tier3Error):
            self.import_it(art)

    def test_missing_stored_names_file_fails_loudly(self):
        with self.assertRaises(store.Tier3Error):
            self.import_it(
                stored_names_path=str(self.dir / "nope.txt"))


class LedgerTests(Tier3Case):
    def test_approve_marks_the_list_and_unapprove_removes(self):
        self.import_it()
        code, _ = self.run_cli(
            ["approve", "--db", self.db, "--work", "Test Book",
             "--name", "Minor C"])
        self.assertEqual(code, 0)
        code, out = self.run_cli(
            ["list", "--db", self.db, "--work", "test book"])
        self.assertEqual(code, 0)
        self.assertIn("Minor C — 4 appearance(s)  "
                      "[approved for push]", out)
        code, _ = self.run_cli(
            ["unapprove", "--db", self.db, "--work", "Test Book",
             "--name", "Minor C"])
        self.assertEqual(code, 0)
        code, out = self.run_cli(
            ["list", "--db", self.db, "--work", "Test Book"])
        self.assertNotIn("[approved for push]", out)

    def test_approve_requires_a_tier3_name(self):
        self.import_it()
        code, _ = self.run_cli(
            ["approve", "--db", self.db, "--work", "Test Book",
             "--name", "Principal A"])  # stored, not Tier 3
        self.assertEqual(code, 2)
        code, _ = self.run_cli(
            ["unapprove", "--db", self.db, "--work", "Test Book",
             "--name", "Minor C"])  # never approved
        self.assertEqual(code, 2)

    def test_reimport_replaces_book_rows_but_ledger_survives(self):
        self.import_it()
        self.run_cli(["approve", "--db", self.db,
                      "--work", "Test Book", "--name", "Minor C"])
        # Mark the ledger row as pushed, as a future console
        # build would; an import must not touch any of it.
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(
                "UPDATE push_ledger SET pushed_at = 'T', "
                "prod_row_id = 'prod-123' WHERE name = 'Minor C'")
        conn.close()
        # A newer run of the same book: roster changed — Minor D
        # gone, Minor E new; C still Tier 3.
        art2 = _artefact()
        art2["minor_characters"] = [
            _char("Minor B", appearances=5),
            _char("Minor C", appearances=9),
            _char("Minor E", appearances=1),
        ]
        art2["minor_sourced_relationships"] = [
            c for c in art2["minor_sourced_relationships"]
            if c["from"] != "Minor D"]
        art2["dropped_relationships"] = []
        self.import_it(art2)
        self.assertEqual(self.tier3_names(),
                         {"Minor C", "Minor E"})
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT decided_at, pushed_at, prod_row_id "
                "FROM push_ledger WHERE name = 'Minor C'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "T")
        self.assertEqual(row[2], "prod-123")


class ReportTests(Tier3Case):
    def test_report_reconciles(self):
        self.import_it()
        report = store.build_report(self.db)
        self.assertIn("roster 5 == stored 3 + Tier 3 2 = 5 — PASS",
                      report)
        self.assertIn("verdict: RECONCILED", report)

    def test_report_fails_when_stored_names_leave_the_roster(self):
        names_file = self.dir / "stored.txt"
        names_file.write_text(
            "Principal A\nPrincipal B\nMinor B\nGhost Name\n",
            encoding="utf-8")
        result = self.import_it(stored_names_path=str(names_file))
        self.assertEqual(result["stored_absent_from_roster"],
                         ["Ghost Name"])
        report = store.build_report(self.db)
        self.assertIn("FAIL", report)
        self.assertIn("verdict: NOT RECONCILED", report)


if __name__ == "__main__":
    unittest.main()
