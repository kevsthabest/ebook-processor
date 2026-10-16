"""Measurement spine, slice 3: snapshot capture + deterministic
replay (spine-design §3.4, amendments A2 + A5).

Pins: the snapshot store is keyed by book FINGERPRINT (A5); capture
copies payloads verbatim and writes a manifest pinning both code
shas and config digests; replay refuses a fingerprint mismatched
book (the batch-1 hard-refusal shape); the replay sandbox is seeded
through SQLite's backup API from the live DB opened immutable and
the LIVE stores' LOGICAL content (canonical dump hash, A2) is
unchanged by a replay whose stages write staging/roster state;
checkpoint writes are suppressed during replay so the snapshot
payloads stay pristine.
"""
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_FILLER = ("The road wound on through pine and stone while the "
           "company argued about maps, meals and old debts. ")


def _make_epub(path, title, author, isbn, chapter_text):
    container = ("<?xml version='1.0' encoding='utf-8'?>\n"
                 "<container xmlns='urn:oasis:names:tc:opendocument:"
                 "xmlns:container' version='1.0'>\n"
                 "  <rootfiles><rootfile full-path='OEBPS/content.opf'/>"
                 "</rootfiles>\n</container>")
    opf = ("<?xml version='1.0' encoding='utf-8'?>\n"
           "<package xmlns='http://www.idpf.org/2007/opf' version='3.0' "
           "unique-identifier='bid'>\n"
           "  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>\n"
           f"    <dc:title>{title}</dc:title>\n"
           f"    <dc:creator>{author}</dc:creator>\n"
           f"    <dc:identifier>{isbn}</dc:identifier>\n"
           "  </metadata>\n"
           "  <manifest>\n"
           "    <item id='c1' href='c1.xhtml' "
           "media-type='application/xhtml+xml'/>\n"
           "  </manifest>\n"
           "  <spine><itemref idref='c1'/></spine>\n</package>")
    xhtml = ("<?xml version='1.0' encoding='utf-8'?>\n"
             "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
             f"<h1>Chapter 1</h1><p>{chapter_text}</p>"
             "</body></html>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c1.xhtml", xhtml)
    return path


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.learn = self.dir / "learned"
        self.learn.mkdir()
        self._saved_learned = pp._LEARNED
        self._saved_replay = pp._REPLAY_CTX
        self._saved_capture = pp._CAPTURE_CTX
        self._saved_ckpt_dir = pp._checkpoint_dir

    def tearDown(self):
        pp._LEARNED = self._saved_learned
        pp._REPLAY_CTX = self._saved_replay
        pp._CAPTURE_CTX = self._saved_capture
        pp._checkpoint_dir = self._saved_ckpt_dir
        pp._close_state_dbs()
        self._td.cleanup()

    def _book(self, name="book.epub", text=None):
        return _make_epub(self.dir / name, "Daemon", "Daniel Suarez",
                          "9780525951623",
                          (text or _FILLER * 30))

    def _fake_checkpoint_dir(self, fingerprint):
        ck = self.dir / "ckpt"
        ck.mkdir(exist_ok=True)
        (ck / "meta.json").write_text(json.dumps(
            {"book_key": "book", "total_chapters": 1,
             "flags_hash": "x", "version": pp.PORTABLE_VERSION,
             "fingerprint": fingerprint}), encoding="utf-8")
        (ck / "chapter_1.json").write_text(json.dumps(
            {"index": 1, "a": {"chars": []}, "b": {"tropes": []}}),
            encoding="utf-8")
        (ck / "roster.json").write_text("{}", encoding="utf-8")
        return ck


class TestCapture(_Base):
    def test_capture_layout_manifest_and_registry(self):
        book = self._book()
        fp = pp._presented_fingerprint(book)
        self.assertTrue(fp)
        pp.StateDB.create(self.learn)
        pp._LEARNED = pp.LearnedState(learn_dir=self.learn,
                                      enabled=True)
        ck = self._fake_checkpoint_dir(fp)
        pp._checkpoint_dir = lambda key: ck
        sid = pp.capture_snapshot(
            "snap-1", book_key="book", fingerprint=fp, title="Daemon",
            author="Daniel Suarez",
            identifiers={"isbn": "9780525951623"}, chapter_count=1,
            reason="gate baseline")
        self.assertEqual(sid, "snap-1")
        snap_dir = self.learn / "snapshots" / fp / "snap-1"
        self.assertTrue((snap_dir / "chapter_1.json").exists())
        self.assertTrue((snap_dir / "roster.json").exists())
        man = json.loads((snap_dir / "manifest.json").read_text())
        self.assertEqual(man["fingerprint"], fp)
        self.assertEqual(man["snapshot_id"], "snap-1")
        self.assertEqual(man["chapter_count"], 1)
        self.assertEqual(man["reason"], "gate baseline")
        self.assertTrue(man["payload_sha256"])
        self.assertEqual(man["extraction"]["portable_version"],
                         pp.PORTABLE_VERSION)
        self.assertIn("config_digest", man["extraction"])
        self.assertIn("flags_hash", man["extraction"])
        db = pp._state_db_for(self.learn)
        rows = db.query("SELECT snapshot_id, fingerprint FROM "
                        "snapshots")
        self.assertEqual(rows, [("snap-1", fp)])

    def test_capture_refuses_duplicate_id(self):
        fp = "fp-abc"
        pp._LEARNED = pp.LearnedState(learn_dir=self.learn,
                                      enabled=True)
        ck = self._fake_checkpoint_dir(fp)
        pp._checkpoint_dir = lambda key: ck
        kw = dict(book_key="book", fingerprint=fp, title="T",
                  author="A", identifiers={}, chapter_count=1)
        self.assertEqual(pp.capture_snapshot("snap-1", **kw), "snap-1")
        self.assertIsNone(pp.capture_snapshot("snap-1", **kw))


class TestReplay(_Base):
    def _seed_live(self):
        """A live substrate with a marker learned entry + a captured
        snapshot; returns (book, fingerprint, live_hash)."""
        book = self._book()
        fp = pp._presented_fingerprint(book)
        db = pp.StateDB.create(self.learn)
        learned = pp.LearnedState(learn_dir=self.learn, enabled=True)
        learned.record_merge("pete", "peter")
        pp._LEARNED = learned
        ck = self._fake_checkpoint_dir(fp)
        pp._checkpoint_dir = lambda key: ck
        pp.capture_snapshot("snap-1", book_key="book", fingerprint=fp,
                            title="Daemon", author="Daniel Suarez",
                            identifiers={}, chapter_count=1)
        pp._checkpoint_dir = self._saved_ckpt_dir
        return book, fp, db.dump_hash()

    def test_replay_never_writes_live_stores(self):
        book, fp, live_hash = self._seed_live()
        seen = {}

        def fake_process(fpath, dry_run=False, preview=False):
            seen["preview"] = preview
            seen["ckpt_dir"] = str(pp._checkpoint_dir("book"))
            seen["learn_dir"] = str(pp._learn().dir)
            # Stages write staging + learned state during a replay;
            # every write must land in the sandbox.
            st = pp.StagingState(learn_dir=pp._learn().dir,
                                 enabled=True)
            st.observe_work(
                work_key="wk", title="T", author="A",
                tropes=[{"name": "Found Family", "n_ch": 1,
                         "catalog_id": "t-1"}], auto_min=3,
                today="2026-10-15")
            pp._learn().record_merge("jonas", "jonas k")
            pp._learn().save()
            # Checkpoint writes are suppressed: the snapshot's
            # payload files must stay pristine.
            pp._checkpoint_save_chapter("book", 9, {"x": 1}, None)
            return True

        code, report = pp.run_replay("snap-1", book, self.learn,
                                     process_fn=fake_process)
        self.assertEqual(code, 0)
        self.assertTrue(seen["preview"])
        self.assertIn("snapshots", seen["ckpt_dir"])
        self.assertNotEqual(seen["learn_dir"], str(self.learn))
        live = pp._state_db_for(self.learn)
        self.assertEqual(live.dump_hash(), live_hash)  # A2: logical
        self.assertTrue(report["replay_run_recorded"])
        self.assertTrue(report["sandbox_deleted"])
        snap_dir = self.learn / "snapshots" / fp / "snap-1"
        self.assertFalse((snap_dir / "chapter_9.json").exists())

    def test_replay_refuses_fingerprint_mismatch(self):
        book, fp, _ = self._seed_live()
        other = self._book("other.epub",
                            text="Completely different prose. "
                                 + _FILLER * 40)
        called = []
        code, report = pp.run_replay(
            "snap-1", other, self.learn,
            process_fn=lambda *a, **k: called.append(1) or True)
        self.assertEqual(code, 1)
        self.assertEqual(called, [])
        self.assertIn("fingerprint", report["refusal"])

    def test_replay_unknown_snapshot(self):
        code, report = pp.run_replay("nope", self._book(), self.learn,
                                     process_fn=lambda *a, **k: True)
        self.assertEqual(code, 1)
        self.assertIn("not found", report["refusal"])


class TestConfigDigest(unittest.TestCase):
    def test_digest_tracks_output_config_only(self):
        saved = dict(pp.CONFIG)
        try:
            pp.CONFIG["pipeline"] = "v2"
            d1 = pp._config_digest()
            self.assertEqual(d1, pp._config_digest())
            pp.CONFIG["pipeline"] = "legacy"
            self.assertNotEqual(d1, pp._config_digest())
        finally:
            pp.CONFIG.clear()
            pp.CONFIG.update(saved)


if __name__ == "__main__":
    unittest.main()
