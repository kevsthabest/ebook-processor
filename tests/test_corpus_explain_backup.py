"""Corpus slice 8: --explain forensics + unified backup/restore
(corpus-design §7.1, §7.2, §11 slice 8).

--explain joins both stores read-only for one work: claim history
(staging sightings, promotion, suppression, verdict cache, the
retrieve stage record with corpus-span citations), character
lineage (roster + FTS occurrences), and the work's quotes audit
surface. Either store may be absent; the report says which half
is missing and still produces the other. It writes nothing.

--backup-state / --restore-state cover the PAIR (state.db +
corpus.db) with a manifest of logical dump hashes and schema
versions. Restore verifies hashes BEFORE touching a live file,
refuses a major schema-version mismatch up front, and keeps the
replaced live pair as <name>.pre-restore-<date>.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                              _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_corpus_ingest import _make_epub, _fixture_files  # noqa: E402
from test_corpus_stages import _g_files  # noqa: E402

_DAWN = ("He betrayed the watch and the guard fell silent "
         "before dawn.")


class _ExplainBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.learn = self.dir / "learned"
        self.learn.mkdir()
        self.book_a = _make_epub(self.dir / "a.epub", _g_files(),
                                 title="G Fixture")
        self.book_b = _make_epub(self.dir / "b.epub",
                                 _fixture_files(),
                                 title="Corpus Test Book")
        imp = pp.CorpusDB.create(self.learn, mode="importer")
        self.fp_a = pp.corpus_ingest_book(imp, self.book_a)[
            "fingerprint"]
        self.fp_b = pp.corpus_ingest_book(imp, self.book_b)[
            "fingerprint"]
        pp._close_corpus_dbs()
        self.db = pp.CorpusDB.open(self.learn, mode="processor")
        pp.corpus_verify_quotes(self.db, self.fp_a, [
            {"text": _DAWN, "kind": "evidence", "chapter_index": 1,
             "source": "run"}])
        # A retrieve stage record for the claim (the shape the
        # slice-4 adoption settles).
        pp.corpus_stage_settle(
            self.db, self.fp_a, "retrieve", stage_version="1",
            model="test-judge",
            outputs={"betrayal": {
                "input_hash": "h-betrayal",
                "payload": {
                    "record": {
                        "name": "Betrayal", "staged": False,
                        "chapters_searched": ["Chapter 1",
                                              "Chapter 3"],
                        "outcome": "supported-strong",
                        "support": "strong", "passages": 1,
                        "chapters_with_support": 2, "adjacent": 0,
                        "judge": {"supports": 2, "adjacent": 0,
                                  "contradicts": 0}},
                    "evidence": [{
                        "chapter": "Chapter 1", "offset": 120,
                        "text": _DAWN}],
                    "verify_failures": 0}}})
        self._seed_state()

    def tearDown(self):
        pp._close_corpus_dbs()
        pp._close_state_dbs()
        self._td.cleanup()

    def _seed_state(self):
        db = pp.StateDB.create(self.learn)
        with db.write_txn() as conn:
            conn.execute(
                "INSERT INTO staging_works VALUES "
                "(?,?,?,?,?,?,?)",
                ("wk-a", "w-a", "G Fixture", "Test Author", 2, 1,
                 "{}"))
            conn.execute(
                "INSERT INTO staging_works VALUES "
                "(?,?,?,?,?,?,?)",
                ("wk-b", "w-b", "Corpus Test Book", "Test Author",
                 1, 0, "{}"))
            conn.execute(
                "INSERT INTO staging_claims VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("wk-a", "betrayal", "trope", "Betrayal",
                 "promoted", 2, "recurrence", 2, 1,
                 "2026-10-01", "2026-10-15",
                 json.dumps([{"run": 1, "chapters": [1]},
                             {"run": 2, "chapters": [1, 3]}]),
                 json.dumps(["Betrayal"]), "{}"))
            conn.execute(
                "INSERT INTO staging_claims VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("wk-a", "dark secret", "trope", "Dark Secret",
                 "suppressed", None, None, 1, 0,
                 "2026-10-02", "2026-10-02",
                 json.dumps([{"run": 1, "chapters": [2]}]),
                 json.dumps(["Dark Secret"]), "{}"))
            conn.execute(
                "INSERT INTO rejection_events VALUES "
                "(?,?,?,?,?,?)",
                (None, "run-2", "wk-a", "trope", "dark secret",
                 "2026-10-15"))
            conn.execute(
                "INSERT INTO verdicts_cache VALUES (?,?,?,?,?)",
                ("w-a", "trope", "betrayal", "confirmed",
                 "2026-10-15"))
            conn.execute(
                "INSERT INTO roster_works VALUES "
                "(?,?,?,?,?,?,?,?,?)",
                ("wk-b", "w-b", "Corpus Test Book", "Test Author",
                 1, None, 2, 1, "{}"))
            conn.execute(
                "INSERT INTO roster_entities VALUES (?,?,?,?,?)",
                ("wk-b", "e1", "active", "Mira Solace",
                 json.dumps({"name_forms": ["Mira Solace", "Mira"],
                             "minted_run": 1, "anchor": "stable"})))
            conn.execute(
                "INSERT INTO roster_no_pairs VALUES (?,?,?,?,?)",
                ("wk-b", "mira solace", "petr valen", "judged",
                 "{}"))
        return db


class TestExplain(_ExplainBase):
    def test_claim_block_joins_both_stores(self):
        data = pp.explain_data(self.learn, "G Fixture",
                               claim="betrayal")
        self.assertEqual(data["absent"], [])
        claim = data["claim"]
        self.assertEqual(claim["state"], "promoted")
        self.assertEqual(claim["promotion"],
                         {"run": 2, "via": "recurrence"})
        self.assertEqual(len(claim["sightings"]), 2)
        self.assertEqual(claim["sightings"][1]["chapters"], [1, 3])
        self.assertEqual(claim["verdict"]["verdict"], "confirmed")
        retrieve = claim["retrieve"]
        self.assertEqual(retrieve["outcome"], "supported-strong")
        self.assertEqual(retrieve["support"], "strong")
        self.assertEqual(retrieve["chapters_searched"],
                         ["Chapter 1", "Chapter 3"])
        self.assertEqual(retrieve["passages"][0]["chapter"],
                         "Chapter 1")
        self.assertEqual(retrieve["passages"][0]["offset"], 120)

    def test_suppressed_claim_shows_rejection_events(self):
        data = pp.explain_data(self.learn, "G Fixture",
                               claim="dark secret")
        claim = data["claim"]
        self.assertEqual(claim["state"], "suppressed")
        self.assertEqual(len(claim["suppression"]), 1)
        self.assertEqual(claim["suppression"][0]["run_ref"],
                         "run-2")

    def test_character_block_lineage_and_occurrences(self):
        data = pp.explain_data(self.learn, "Corpus Test Book",
                               character="Mira Solace")
        char = data["character"]
        self.assertEqual(char["canonical"], "Mira Solace")
        self.assertEqual(char["entity_id"], "e1")
        self.assertIn("Mira", char["name_forms"])
        self.assertTrue(char["no_pairs"])
        occ = char["occurrences"]
        self.assertGreater(occ["by_form"]["Mira Solace"], 0)
        self.assertTrue(occ["chapters"])
        self.assertEqual(occ["first"]["chapter_index"],
                         occ["chapters"][0])
        self.assertIn("offset", occ["first"])
        self.assertIn("offset", occ["last"])

    def test_character_absent_on_known_work_keeps_roster_line(self):
        # The work HAS a persisted roster and the character is
        # simply not on it: the original line stands.
        data = pp.explain_data(self.learn, "Corpus Test Book",
                               character="Petr Valen")
        self.assertFalse(data["character"]["found"])
        self.assertNotIn("no_roster", data["character"])
        text = pp.explain_text(self.learn, "Corpus Test Book",
                               character="Petr Valen")
        self.assertIn("character Petr Valen: not on the work's "
                      "roster", text)

    def test_character_check_unavailable_without_any_roster(self):
        # "G Fixture" is known to state.db through staging only —
        # no roster_works row, no roster_entities: the report must
        # name the missing precondition instead of blaming the
        # character.
        data = pp.explain_data(self.learn, "G Fixture",
                               character="Mira Solace")
        self.assertTrue(data["character"]["no_roster"])
        text = pp.explain_text(self.learn, "G Fixture",
                               character="Mira Solace")
        self.assertIn("character Mira Solace: check unavailable",
                      text)
        self.assertIn("no persisted roster in state.db", text)
        self.assertNotIn("not on the work's roster", text)

    def test_character_check_unavailable_when_state_never_matched(
            self):
        # The drill-7 shape: the work lives in the corpus, state.db
        # has no matching work at all (every run was a preview or
        # replay, which persist no roster).
        learn2 = self.dir / "learn2"
        learn2.mkdir()
        imp = pp.CorpusDB.create(learn2, mode="importer")
        pp.corpus_ingest_book(imp, self.book_a)
        db = pp.StateDB.create(learn2)
        with db.write_txn() as conn:
            conn.execute(
                "INSERT INTO staging_works VALUES "
                "(?,?,?,?,?,?,?)",
                ("wk-x", "w-x", "Some Other Book", "An Author",
                 1, 0, "{}"))
        text = pp.explain_text(learn2, "G Fixture",
                               character="Mira Solace")
        self.assertIn("state.db: no matching work", text)
        self.assertIn("character Mira Solace: check unavailable",
                      text)
        self.assertNotIn("not on the work's roster", text)

    def test_work_block_lists_quotes_audit_surface(self):
        data = pp.explain_data(self.learn, "G Fixture")
        quotes = data["corpus"]["quotes"]
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["text"], _DAWN)
        self.assertEqual(quotes[0]["status"], "verified")
        self.assertEqual(quotes[0]["kind"], "evidence")

    def test_explain_writes_nothing(self):
        state = pp._state_db_for(self.learn)
        before = (state.dump_hash(), self.db.dump_hash())
        pp.explain_data(self.learn, "G Fixture", claim="betrayal")
        pp.explain_data(self.learn, "Corpus Test Book",
                        character="Mira Solace")
        after = (state.dump_hash(), self.db.dump_hash())
        self.assertEqual(before, after)

    def test_state_only(self):
        learn2 = self.dir / "learn2"
        learn2.mkdir()
        db = pp.StateDB.create(learn2)
        with db.write_txn() as conn:
            conn.execute(
                "INSERT INTO staging_works VALUES "
                "(?,?,?,?,?,?,?)",
                ("wk-x", "w-x", "Solo Book", "An Author", 1, 0,
                 "{}"))
        data = pp.explain_data(learn2, "Solo Book")
        self.assertEqual(data["absent"], ["corpus"])
        self.assertIsNotNone(data["state"])
        self.assertIsNone(data["corpus"])

    def test_corpus_only(self):
        learn2 = self.dir / "learn2"
        learn2.mkdir()
        imp = pp.CorpusDB.create(learn2, mode="importer")
        pp.corpus_ingest_book(imp, self.book_a)
        data = pp.explain_data(learn2, "G Fixture")
        self.assertEqual(data["absent"], ["state.db"])
        self.assertIsNotNone(data["corpus"])
        self.assertIsNone(data["state"])

    def test_neither_store(self):
        learn2 = self.dir / "learn2"
        learn2.mkdir()
        data = pp.explain_data(learn2, "Ghost Book")
        self.assertEqual(sorted(data["absent"]),
                         ["corpus", "state.db"])
        text = pp.explain_text(learn2, "Ghost Book")
        self.assertIn("corpus: absent", text)
        self.assertIn("state.db: absent", text)


class TestBackupRestore(_ExplainBase):
    def _hashes(self):
        pp._close_corpus_dbs()
        pp._close_state_dbs()
        cdb = pp.CorpusDB.open(self.learn, mode="report",
                               readonly=True)
        sdb = pp.StateDB.open(self.learn)
        return sdb.dump_hash(), cdb.dump_hash()

    def test_backup_restore_round_trip(self):
        before = self._hashes()
        dest = self.dir / "backup"
        report = pp.cmd_backup_state(dest, learn_dir=self.learn)
        self.assertTrue(report["ok"])
        man = json.loads((dest / "manifest.json").read_text())
        self.assertEqual(man["stores"]["corpus"]["dump_hash"],
                         before[1])
        self.assertEqual(man["stores"]["state"]["dump_hash"],
                         before[0])
        self.assertEqual(man["portable_version"],
                         pp.PORTABLE_VERSION)
        # Mutate both stores.
        mdb = pp.CorpusDB.open(self.learn, mode="processor")
        pp.corpus_stage_settle(
            mdb, self.fp_a, "retrieve", stage_version="1",
            model="m", outputs={"x": {"input_hash": "h",
                                      "payload": {}}})
        state = pp._state_db_for(self.learn)
        with state.write_txn() as conn:
            conn.execute(
                "INSERT INTO rejection_events VALUES "
                "(?,?,?,?,?,?)",
                (None, "run-9", "wk-a", "trope", "betrayal",
                 "2026-10-16"))
        self.assertNotEqual(self._hashes(), before)
        report = pp.cmd_restore_state(dest, learn_dir=self.learn)
        self.assertTrue(report["ok"])
        self.assertEqual(self._hashes(), before)
        self.assertTrue(list(self.learn.glob(
            "state.db.pre-restore-*")))
        self.assertTrue(list(self.learn.glob(
            "corpus.db.pre-restore-*")))

    def test_corrupted_backup_refused_before_touching_live(self):
        dest = self.dir / "backup"
        pp.cmd_backup_state(dest, learn_dir=self.learn)
        before = self._hashes()
        blob = bytearray((dest / "corpus.db").read_bytes())
        blob[len(blob) // 2] ^= 0xFF
        blob[len(blob) // 3] ^= 0xFF
        (dest / "corpus.db").write_bytes(bytes(blob))
        report = pp.cmd_restore_state(dest, learn_dir=self.learn)
        self.assertFalse(report["ok"])
        self.assertEqual(self._hashes(), before)
        self.assertFalse(list(self.learn.glob("*.pre-restore-*")))

    def test_major_version_mismatch_refused(self):
        dest = self.dir / "backup"
        pp.cmd_backup_state(dest, learn_dir=self.learn)
        before = self._hashes()
        man_path = dest / "manifest.json"
        man = json.loads(man_path.read_text())
        man["stores"]["corpus"]["user_version"] = 99
        man_path.write_text(json.dumps(man))
        report = pp.cmd_restore_state(dest, learn_dir=self.learn)
        self.assertFalse(report["ok"])
        self.assertIn("version", report["reason"])
        self.assertEqual(self._hashes(), before)
        self.assertFalse(list(self.learn.glob("*.pre-restore-*")))

    def test_backup_with_no_stores_is_not_a_backup(self):
        learn2 = self.dir / "learn2"
        learn2.mkdir()
        dest = self.dir / "backup2"
        report = pp.cmd_backup_state(dest, learn_dir=learn2)
        self.assertFalse(report["ok"])
        self.assertFalse((dest / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
