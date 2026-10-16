"""Measurement spine, slice 1: the SQLite substrate core + LearnedState
on the persistence seam (eval/spine-design.md §3, rulings addendum).

Covered: StateDB open/schema/pragmas, F8 corruption discipline on a
corrupt state.db, the canonical dump hash (A2: logical content, not
file bytes), the backup-API copy, and LearnedState round-tripping
through the substrate with its in-memory model and every invariant
unchanged. JSON mode (no state.db present) is untouched — the rest
of the suite pins it; one test here pins that bare store
construction does NOT create a state.db.
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
        pp._close_state_dbs()
        self._td.cleanup()

    def make_db(self):
        return pp.StateDB.create(self.dir)


class TestStateDBCore(_Base):
    def test_create_schema_and_pragmas(self):
        db = self.make_db()
        self.assertTrue((self.dir / "state.db").exists())
        tables = {r[0] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("meta", "learned_nicknames", "learned_titles",
                  "learned_threshold_stats", "learned_rejected_tropes",
                  "series_state", "staging_works", "staging_claims",
                  "staging_expired", "roster_works", "roster_entities",
                  "roster_retired", "roster_no_pairs", "roster_adoptions",
                  "roster_anchor_retirements", "snapshots", "replay_runs",
                  "verdicts_cache", "verdict_fetches", "fixture_scores",
                  "uncertainty_queue", "rejection_events"):
            self.assertIn(t, tables)
        # D1 seam: the corrections table is RESERVED, not created.
        self.assertNotIn("correction_events", tables)
        self.assertEqual(db.query("PRAGMA user_version")[0][0],
                         pp.SUBSTRATE_SCHEMA_VERSION)
        self.assertEqual(
            db.query("PRAGMA journal_mode")[0][0].lower(), "wal")
        self.assertEqual(db.query("PRAGMA synchronous")[0][0], 2)  # FULL

    def test_open_without_create_returns_none(self):
        self.assertIsNone(pp.StateDB.open(self.dir))
        self.assertIsNone(pp._state_db_for(self.dir))

    def test_dump_hash_is_logical_not_bytes(self):
        db = self.make_db()
        h0 = db.dump_hash()
        with db.write_txn() as conn:
            conn.execute(
                "INSERT INTO learned_nicknames VALUES (?,?,?,?,?,?,?)",
                ("pete", "peter", 2, '["isbn1"]', 0, 0, '{"full": "peter"}'))
        h1 = db.dump_hash()
        self.assertNotEqual(h0, h1)
        db.close()
        pp._close_state_dbs()
        db2 = pp.StateDB.open(self.dir)
        self.assertEqual(db2.dump_hash(), h1)

    def test_backup_api_copy_matches(self):
        db = self.make_db()
        with db.write_txn() as conn:
            conn.execute(
                "INSERT INTO meta VALUES ('k', 'v')")
        dest = self.dir / "copy.db"
        db.backup_to(dest)
        other = sqlite3.connect(str(dest))
        try:
            self.assertEqual(
                other.execute("SELECT v FROM meta WHERE k='k'")
                .fetchone()[0], "v")
        finally:
            other.close()

    def test_corrupt_db_f8_discipline(self):
        db = self.make_db()
        with db.write_txn() as conn:
            conn.execute("INSERT INTO meta VALUES ('k', 'v')")
        db.close()
        pp._close_state_dbs()
        # Corrupt the file's header pages beyond recognition.
        p = self.dir / "state.db"
        p.write_bytes(b"NOT A DATABASE" + b"\x00" * 4096)
        out = io.StringIO()
        with redirect_stdout(out):
            db2 = pp.StateDB.open(self.dir)
        self.assertIsNotNone(db2)
        self.assertTrue(db2.corrupt_at_open)
        self.assertIn("WARNING", out.getvalue())
        preserved = sorted(x.name for x in self.dir.glob("state.db.corrupt*"))
        self.assertTrue(preserved, "corrupt original preserved")
        # Stores start empty on the fresh DB.
        self.assertEqual(db2.query("SELECT COUNT(*) FROM meta")[0][0], 0)


class TestLearnedStateOnSubstrate(_Base):
    def test_bare_construction_creates_no_db(self):
        pp.LearnedState(learn_dir=self.dir, enabled=True)
        self.assertFalse((self.dir / "state.db").exists())

    def test_roundtrip_through_substrate(self):
        self.make_db()
        st = pp.LearnedState(learn_dir=self.dir, enabled=True)
        st.record_alias("Pete", "Peter Sebeck")
        st.record_merge("sebeck", "peter sebeck", isbn="isbn-1")
        st.record_token_merge("pete", "peter", isbn="isbn-1")
        st.observe_title("Captain", "someone")
        st.observe_title("Captain", "another")
        st.record_threshold("gate_a", 0.9, True)
        st.record_threshold("gate_a", 0.4, False)
        st.record_trope_rejection("trope-id-1", "Daemon", "not in book")
        st.save()
        # JSON files are NOT written while the substrate is active.
        self.assertFalse((self.dir / "nicknames.json").exists())
        self.assertFalse((self.dir / "rejected_tropes.json").exists())
        fresh = pp.LearnedState(learn_dir=self.dir, enabled=True)
        self.assertEqual(fresh.nicknames, st.nicknames)
        self.assertEqual(fresh.titles, st.titles)
        self.assertEqual(fresh.threshold_stats, st.threshold_stats)
        self.assertEqual(fresh.rejected_tropes, st.rejected_tropes)

    def test_series_flat_and_named_roundtrip(self):
        self.make_db()
        st = pp.LearnedState(learn_dir=self.dir, enabled=True)
        chars = [{"name": "Peter Sebeck", "aliases": ["Pete"]}]
        key = st.series_key("Daniel Suarez")
        st.save_series_roster(key, "Daemon", "Daniel Suarez",
                              "isbn-1", chars)
        hints = st.load_series_hints(key)
        self.assertEqual(hints.get("pete"), "peter sebeck")
        nkey = st.series_name_key("Daemon")
        st.save_series_name_roster(nkey, "Daemon", "Daniel Suarez",
                                   "isbn-1", 1, chars)
        hints2 = st.load_series_name_hints(nkey)
        self.assertEqual(hints2.get("pete"), "peter sebeck")
        self.assertFalse((self.dir / "series").exists() and
                         any((self.dir / "series").rglob("*.json")))

    def test_db_size_warning_data_available(self):
        db = self.make_db()
        info = db.size_info()
        self.assertIn("bytes", info)
        self.assertGreaterEqual(info["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
