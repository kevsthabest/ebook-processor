"""--corpus-only folder filter: admission, skipping, ordering.

The filter and corpus-sourced acquisition share one membership
test (corpus_servable_fingerprint), and these tests pin both
levels: the predicate itself against acquisition's verdicts, and
the folder scan at the run_once seam with _run_once_inner mocked
(the boundary the watch-cycle and dispatch tests also use — no
LLM, no network, no Supabase).

Stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location(
    "process_portable_corpus_only", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_corpus_ingest import _make_epub, _fixture_files  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.learn = self.dir / "learned"
        self.learn.mkdir()
        self.imports = self.dir / "import"
        self.imports.mkdir()
        self._saved = (pp._LEARNED, pp.IMPORT_DIR, pp.DONE_DIR,
                       pp.FAILED_DIR)
        pp._LEARNED = pp.LearnedState(learn_dir=self.learn,
                                      enabled=True)
        pp.IMPORT_DIR = self.imports
        pp.DONE_DIR = self.imports / "done"
        pp.FAILED_DIR = self.imports / "failed"

    def tearDown(self):
        (pp._LEARNED, pp.IMPORT_DIR, pp.DONE_DIR,
         pp.FAILED_DIR) = self._saved
        pp._close_corpus_dbs()
        pp._close_state_dbs()
        self._td.cleanup()

    def _book(self, name, title, **kw):
        return _make_epub(self.imports / name, _fixture_files(**kw),
                          title=title)

    def _ingest(self, path):
        db = pp.CorpusDB.create(self.learn, mode="importer")
        res = pp.corpus_ingest_book(db, Path(path))
        self.assertIn(res["outcome"], ("partial", "complete"))
        return res["fingerprint"]

    def _scan(self, max_books=None, corpus_only=True, file=None):
        """One folder scan with processing mocked out; returns
        (exit code, inner mock, printed output)."""
        out = io.StringIO()
        with mock.patch.dict(pp.CONFIG,
                             {"corpus_only": corpus_only}), \
                mock.patch.object(pp, "_run_once_inner",
                                  return_value=0) as inner, \
                contextlib.redirect_stdout(out):
            rc = pp.run_once(False, False, max_books, file=file)
        return rc, inner, out.getvalue()

    @staticmethod
    def _processed(inner):
        """The files list a scan handed to processing."""
        if not inner.call_args_list:
            return None
        return inner.call_args_list[0][0][2]


class TestPredicateSharing(_Base):
    """The filter's admission test IS acquisition's membership
    test — for the same file, both must give the same answer."""

    def test_helper_agrees_with_acquisition(self):
        book = self._book("a.epub", "Admitted Fixture")
        fp = self._ingest(book)
        db = pp._corpus_db_for(self.learn, mode="processor")
        self.assertEqual(pp.corpus_servable_fingerprint(db, book),
                         fp)
        acq = pp.corpus_acquire_for_run(self.learn, book)
        self.assertIsNotNone(acq)
        self.assertEqual(acq["fingerprint"], fp)

    def test_helper_agrees_on_uningested(self):
        self._ingest(self._book("a.epub", "Admitted Fixture"))
        other = self._book("b.epub", "Never Ingested")
        db = pp._corpus_db_for(self.learn, mode="processor")
        self.assertIsNone(pp.corpus_servable_fingerprint(db, other))
        self.assertIsNone(
            pp.corpus_acquire_for_run(self.learn, other))

    def test_helper_rejects_nonservable_state(self):
        book = self._book("a.epub", "Half Ingested Fixture")
        self._ingest(book)
        db = pp._corpus_db_for(self.learn, mode="importer")
        with db.write_txn() as conn:
            conn.execute(
                "UPDATE corpus_works SET status='ingesting'")
        self.assertIsNone(pp.corpus_servable_fingerprint(db, book))
        self.assertIsNone(
            pp.corpus_acquire_for_run(self.learn, book))


class TestFolderFilter(_Base):
    def test_ingested_book_is_admitted(self):
        book = self._book("a.epub", "Admitted Fixture")
        self._ingest(book)
        rc, inner, out = self._scan()
        self.assertEqual(rc, 0)
        self.assertEqual(self._processed(inner), [book])
        self.assertIn("1 of 1 books are in the corpus", out)

    def test_uningested_book_skipped_and_left_in_place(self):
        admitted = self._book("a.epub", "Admitted Fixture")
        self._ingest(admitted)
        skipped = self._book("b.epub", "Never Ingested")
        rc, inner, out = self._scan()
        self.assertEqual(rc, 0)
        self.assertEqual(self._processed(inner), [admitted])
        self.assertIn("Skipped (not in the corpus): b.epub", out)
        # Exclusion before processing is the only protection a
        # skipped file needs: it is still in the folder, and the
        # folders processing would have moved it to are empty.
        self.assertTrue(skipped.exists())
        self.assertFalse((pp.DONE_DIR / "b.epub").exists())
        self.assertFalse((pp.FAILED_DIR / "b.epub").exists())

    def test_nonservable_work_is_skipped(self):
        book = self._book("a.epub", "Half Ingested Fixture")
        self._ingest(book)
        db = pp._corpus_db_for(self.learn, mode="importer")
        with db.write_txn() as conn:
            conn.execute(
                "UPDATE corpus_works SET status='ingesting'")
        rc, inner, out = self._scan()
        self.assertEqual(rc, 0)
        self.assertIsNone(self._processed(inner))
        self.assertIn("Skipped (not in the corpus): a.epub", out)
        self.assertIn("none of the 1 books", out)

    def test_filter_runs_before_max_books(self):
        # The uningested book sorts FIRST: if limiting ran before
        # filtering, the limit would spend itself on it and no
        # admitted book would be processed at all.
        self._book("0-uningested.epub", "Never Ingested")
        first = self._book("a.epub", "Admitted Fixture")
        second = self._book("b.epub", "Second Fixture",
                            velvet=False, bell=False)
        self._ingest(first)
        self._ingest(second)
        rc, inner, out = self._scan(max_books=1)
        self.assertEqual(rc, 0)
        self.assertEqual(self._processed(inner), [first])

    def test_all_skipped_processes_nothing(self):
        self._book("a.epub", "Never Ingested")
        self._book("b.epub", "Also Never Ingested")
        rc, inner, out = self._scan()
        self.assertEqual(rc, 0)
        self.assertEqual(inner.call_args_list, [])
        self.assertIn("none of the 2 books in the import folder "
                      "are in the corpus", out)

    def test_no_corpus_skips_everything(self):
        # No corpus.db in the learn dir at all: _corpus_db_for
        # returns None and every book reads as not in the corpus.
        self._book("a.epub", "Never Ingested")
        rc, inner, out = self._scan()
        self.assertEqual(rc, 0)
        self.assertEqual(inner.call_args_list, [])
        self.assertIn("Skipped (not in the corpus): a.epub", out)

    def test_flag_off_processes_everything(self):
        admitted = self._book("a.epub", "Admitted Fixture")
        self._ingest(admitted)
        other = self._book("b.epub", "Never Ingested")
        rc, inner, out = self._scan(corpus_only=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self._processed(inner), [admitted, other])
        self.assertNotIn("Skipped (not in the corpus)", out)

    def test_explicit_file_bypasses_the_filter(self):
        target = self._book("b.epub", "Never Ingested")
        rc, inner, out = self._scan(file=str(target))
        self.assertEqual(rc, 0)
        self.assertEqual(self._processed(inner), [target])
        self.assertNotIn("Skipped (not in the corpus)", out)


class TestFlagWiring(unittest.TestCase):
    """--corpus-only implies corpus-sourced acquisition: the flag
    switches CONFIG on in main(), beside --use-corpus's own
    threading (acquisition itself reads CONFIG in
    process_file_v2)."""

    def setUp(self):
        self._cfg = {k: pp.CONFIG.get(k)
                     for k in ("use_corpus", "corpus_only")}

    def tearDown(self):
        for k, v in self._cfg.items():
            if v is None:
                pp.CONFIG.pop(k, None)
            else:
                pp.CONFIG[k] = v

    def test_parser_flag_and_default(self):
        args = pp._build_parser().parse_args([])
        self.assertFalse(args.corpus_only)
        args = pp._build_parser().parse_args(["--corpus-only"])
        self.assertTrue(args.corpus_only)

    def test_main_threads_corpus_only_into_config(self):
        # --preview rides along so main() passes its Supabase
        # config gate (no config.json in the test environment);
        # the flag threading under test runs before that gate.
        out = io.StringIO()
        with mock.patch.object(sys, "argv",
                               ["process-portable.py",
                                "--corpus-only", "--preview"]), \
                mock.patch.object(pp, "run_once",
                                  return_value=0) as scan, \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                pp.main()
        self.assertEqual(cm.exception.code, 0)
        scan.assert_called_once_with(False, True, None, None)
        self.assertTrue(pp.CONFIG.get("corpus_only"))
        self.assertTrue(pp.CONFIG.get("use_corpus"))


if __name__ == "__main__":
    unittest.main()
