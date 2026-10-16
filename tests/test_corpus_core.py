"""Corpus store, slice 1: CorpusDB core on the shared substrate base,
capability probes, mode-scoped write guards, --corpus-status on an
absent/empty corpus, and the calibration report (eval/corpus-design.md
§1, §5.2, §6.3, §7.3, §7.4).

The substrate refactor itself is pinned by the whole suite (StateDB's
1292-test behaviour must not move); this file pins the corpus handle's
own contract: create-on-ingest posture (open never creates), the F8
corruption divergence (preserve + fall back, never a fresh empty DB),
degraded open without FTS5, and writer-family discipline (§5.2).
"""
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        pp._close_corpus_dbs()
        pp._close_state_dbs()
        self._td.cleanup()


class TestCorpusCreateOpen(_Base):
    def test_create_applies_schema_meta_and_version(self):
        db = pp.CorpusDB.create(self.dir, mode="importer")
        self.assertTrue((self.dir / "corpus.db").exists())
        ver = db.query("PRAGMA user_version")[0][0]
        self.assertEqual(ver, pp.CORPUS_SCHEMA_VERSION)
        meta = dict(db.query("SELECT k, v FROM meta"))
        self.assertEqual(meta.get("corpus_v"), pp.CORPUS_V)
        self.assertEqual(meta.get("created_by"), pp.PORTABLE_VERSION)
        tables = {r[0] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("corpus_works", "corpus_chapters", "corpus_passages",
                  "passage_vectors", "ingest_runs", "stage_runs",
                  "stage_outputs", "quotes", "work_envelope"):
            self.assertIn(t, tables)
        # FTS5 is present on this Python (design §0 verified fact):
        # the index + its maintenance triggers exist after create.
        self.assertTrue(db.fts_available)
        self.assertIn("corpus_fts", tables)
        trigs = {r[0] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertEqual(
            {"corpus_chapters_ai", "corpus_chapters_ad",
             "corpus_chapters_au"}, trigs & {
                 "corpus_chapters_ai", "corpus_chapters_ad",
                 "corpus_chapters_au"})

    def test_open_absent_returns_none_and_creates_nothing(self):
        db = pp.CorpusDB.open(self.dir)
        self.assertIsNone(db)
        self.assertFalse((self.dir / "corpus.db").exists())
        self.assertIsNone(pp._corpus_db_for(self.dir))
        self.assertFalse((self.dir / "corpus.db").exists())

    def test_registry_roundtrip_and_mode(self):
        made = pp.CorpusDB.create(self.dir, mode="importer")
        got = pp._corpus_db_for(self.dir, mode="importer")
        self.assertIs(made, got)
        self.assertEqual(got.mode, "importer")

    def test_busy_timeout_set_on_corpus_conn(self):
        # A9: the corpus handle names its busy_timeout at open.
        db = pp.CorpusDB.create(self.dir, mode="importer")
        ms = db.query("PRAGMA busy_timeout")[0][0]
        self.assertGreater(ms, 0)

    def test_dump_hash_is_logical_and_corpus_scoped(self):
        a = pp.CorpusDB.create(self.dir, mode="importer")
        h0 = a.dump_hash()
        with a.write_txn("text") as conn:
            conn.execute(
                "INSERT INTO corpus_works (fingerprint, status) "
                "VALUES ('fp1', 'complete')")
        self.assertNotEqual(h0, a.dump_hash())
        # StateDB dump hash over the same directory is a different
        # (substrate) hash and is untouched by corpus content.
        sdb = pp.StateDB.create(self.dir)
        self.assertNotEqual(sdb.dump_hash(), a.dump_hash())


class TestCorpusCorruption(_Base):
    def test_corrupt_corpus_preserved_rename_in_place_open_none(self):
        # A11 + §5.3: preservation is a rename, never a copy; a corrupt
        # corpus opens as ABSENT (processing falls back to file
        # parsing) — no fresh empty corpus.db is created in its place.
        p = self.dir / "corpus.db"
        p.write_bytes(b"this is not a sqlite file" * 64)
        out = io.StringIO()
        with redirect_stdout(out):
            db = pp.CorpusDB.open(self.dir)
        self.assertIsNone(db)
        self.assertFalse(p.exists())
        preserved = list(self.dir.glob("corpus.db.corrupt*"))
        self.assertEqual(len(preserved), 1)
        self.assertIn("corrupt", out.getvalue().lower())


class TestDegradedOpen(_Base):
    def test_degraded_open_without_fts(self):
        # §1.5/§1.6 pin: probe forced off -> corpus opens degraded:
        # text/vector paths work, FTS consumers report unavailability,
        # nothing crashes.
        orig = pp._corpus_probe_fts5
        pp._corpus_probe_fts5 = lambda conn: False
        try:
            db = pp.CorpusDB.create(self.dir, mode="importer")
        finally:
            pp._corpus_probe_fts5 = orig
        self.assertFalse(db.fts_available)
        tables = {r[0] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("corpus_fts", tables)
        with db.write_txn("text") as conn:
            conn.execute(
                "INSERT INTO corpus_chapters (fingerprint, "
                "chapter_index, label, text, text_hash) VALUES "
                "('fp1', 0, 'Chapter 1', 'some words', 'h')")
        rows = db.query("SELECT text FROM corpus_chapters")
        self.assertEqual(rows, [("some words",)])
        self.assertIsNone(db.fts_search_chapters("words"))


class TestWriterFamilyDiscipline(_Base):
    # §5.2/§5.4 pin: the table-family writer table is enforced by
    # handle construction (mode), not by convention.
    def test_importer_refuses_stage_writes(self):
        db = pp.CorpusDB.create(self.dir, mode="importer")
        with self.assertRaises(pp.CorpusWriteRefused):
            with db.write_txn("stages"):
                pass
        with db.write_txn("text") as conn:
            conn.execute(
                "INSERT INTO corpus_works (fingerprint) VALUES ('x')")

    def test_processor_refuses_text_writes(self):
        db = pp.CorpusDB.create(self.dir, mode="processor")
        with self.assertRaises(pp.CorpusWriteRefused):
            with db.write_txn("text"):
                pass
        with db.write_txn("stages") as conn:
            conn.execute(
                "INSERT INTO stage_runs (run_id, fingerprint, stage) "
                "VALUES ('r1', 'x', 'retrieve')")

    def test_report_mode_refuses_all_family_writes(self):
        db = pp.CorpusDB.create(self.dir, mode="report")
        for fam in ("text", "vectors", "stages", "envelope"):
            with self.assertRaises(pp.CorpusWriteRefused):
                with db.write_txn(fam):
                    pass


class TestFtsTriggerMaintenance(_Base):
    def test_fts_matches_chapter_text_via_triggers(self):
        # §1.6 pin (mechanism half; the ingest half is slice 2):
        # a phrase planted in one chapter is found in exactly that
        # chapter, maintained by triggers with no application sync.
        db = pp.CorpusDB.create(self.dir, mode="importer")
        with db.write_txn("text") as conn:
            conn.execute(
                "INSERT INTO corpus_chapters (fingerprint, "
                "chapter_index, label, text, text_hash) VALUES "
                "('fp1', 0, 'One', 'the velvet hammer falls', 'a'), "
                "('fp1', 1, 'Two', 'an ordinary morning', 'b')")
        hits = db.fts_search_chapters("velvet hammer")
        self.assertEqual(hits, [("fp1", 0)])
        with db.write_txn("text") as conn:
            conn.execute(
                "DELETE FROM corpus_chapters WHERE chapter_index = 0")
        self.assertEqual(db.fts_search_chapters("velvet hammer"), [])


_STATS = {
    "death": {"kept": [0.91, 0.87, 0.93, 0.78, 0.88, 0.95, 0.81, 0.9],
              "dropped": [0.22, 0.31, 0.18, 0.4, 0.27, 0.35, 0.12, 0.29]},
    "betrayal": {"kept": [0.66, 0.71], "dropped": [0.64, 0.7, 0.6]},
    "empty": {"kept": [], "dropped": []},
}


class TestCalibrationReport(_Base):
    def test_report_values_and_provenance_limit(self):
        # §6.6 pin: the v1 report carries its distributional-only
        # notice and per-trigger n beside every decile table; the
        # decision rule (moves need outcome-joined calibration)
        # prints beside the limit (Trope seat amendment).
        data = pp._calibration_report(_STATS)
        by_trigger = {t["trigger"]: t for t in data["triggers"]}
        self.assertEqual(by_trigger["death"]["kept"]["n"], 8)
        self.assertEqual(by_trigger["death"]["dropped"]["n"], 8)
        self.assertEqual(len(by_trigger["death"]["kept"]["deciles"]), 9)
        self.assertGreater(by_trigger["death"]["separation"]["auc"], 0.9)
        self.assertIsNotNone(
            by_trigger["death"]["crossings"]["0.90"])
        text = pp._calibration_report_text(_STATS)
        self.assertIn("DISTRIBUTIONAL", text)
        self.assertIn("no work, run, or outcome keys", text)
        self.assertIn("outcome-joined", text)
        self.assertIn("death", text)

    def test_report_json_round_trip(self):
        data = pp._calibration_report(_STATS)
        back = json.loads(json.dumps(data))
        self.assertEqual(back["triggers"][0]["trigger"],
                         data["triggers"][0]["trigger"])


class TestCorpusStatus(_Base):
    def test_status_absent_corpus(self):
        text = pp._corpus_status_text(self.dir)
        self.assertIn("absent", text)
        self.assertIn("retained book text, not a cache", text)
        data = pp._corpus_status_data(self.dir)
        self.assertFalse(data["present"])
        self.assertEqual(data["works"], [])

    def test_status_empty_corpus(self):
        pp.CorpusDB.create(self.dir, mode="importer")
        pp._close_corpus_dbs()
        text = pp._corpus_status_text(self.dir)
        self.assertIn("retained book text, not a cache", text)
        data = pp._corpus_status_data(self.dir)
        self.assertTrue(data["present"])
        self.assertEqual(data["works"], [])
        self.assertEqual(data["totals"]["works"], 0)


if __name__ == "__main__":
    unittest.main()
