"""Wave 3 batch B: subcommand isolation (issues #48, #49, #50, #52, #60).

One book's / one item's failure must not poison the batch, and a
command must not destroy live state:

  #48 cmd_retry_failed must not clear a checkpoint directory that a
      real (fingerprinted) run left behind — that directory is the
      interrupted run's resumable state. Retry seeds and legacy
      checkpoints carry no fingerprint and stay retryable.
  #49 cmd_corpus_reembed isolates per work: a raising work is named,
      counted, and the remaining works still re-embed.
  #50 cmd_ingest surfaces opening-line stage failures (named book +
      error, recorded on the ingest result) instead of swallowing
      them, and the rest of the ingest completes.
  #52 evaluate-merges-batched: malformed label entries are reported
      and skipped (never a KeyError traceback after the API spend),
      a run without batch size 1 completes and says there is no
      baseline, and the baseline is honestly identified (production
      instructions + production state shape; name-only pairs are
      named as measuring a judge production never runs).
  #60 --save-profile with --preview / --dry-run saves the profile
      carrying those settings again (the v2.13.52 conflict check
      exempted a pairing that was never dropped, only captured);
      other run-mode flags beside --save-profile still refuse.

Stdlib unittest only. Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
_MOD_PATH = str(_ROOT / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                              _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


emb = _load("evaluate_merges_batched_b", "evaluate-merges-batched.py")


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


# ----------------------------------------------------------------------
# #48: cmd_retry_failed vs a live (fingerprinted) checkpoint
# ----------------------------------------------------------------------

class TestRetryFailedLiveCheckpoint(unittest.TestCase):
    def setUp(self):
        self._cfg = dict(pp.CONFIG)
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self._cwd = os.getcwd()
        os.chdir(self.td)
        # Route the checkpoint store into the temp dir, the same seam
        # tests/test_checkpoint_fingerprint.py uses.
        self._orig_dir = pp._checkpoint_dir
        td = self.td

        def _tmp_dir(book_key, _t=td):
            return _t / "checkpoints" / book_key
        pp._checkpoint_dir = _tmp_dir
        self.calls = []
        self._pfv = mock.patch.object(
            pp, "process_file_v2",
            lambda fpath, dry_run=False, preview=False:
                self.calls.append(fpath))
        self._pfv.start()
        self._imp = mock.patch.object(pp, "IMPORT_DIR", self.td / "import")
        self._imp.start()
        self.book_key = pp._checkpoint_book_key("TestBook.epub")
        (self.td / "TestBook.epub").write_bytes(b"fake-epub")
        chapters = {"0": {"a": {"x": 1}, "b": {"y": 1}},
                    "1": {"a": None, "b": None}}
        preview = {"file": "TestBook.epub", "title": "TestBook",
                   "chapter_data": {"chapters": chapters, "roster": {}}}
        self.preview_path = self.td / "preview.json"
        self.preview_path.write_text(json.dumps(preview),
                                     encoding="utf-8")

    def tearDown(self):
        self._imp.stop()
        self._pfv.stop()
        pp._checkpoint_dir = self._orig_dir
        pp.CONFIG.clear()
        pp.CONFIG.update(self._cfg)
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def _snapshot(self):
        d = pp._checkpoint_dir(self.book_key)
        if not d.is_dir():
            return {}
        return {str(p.relative_to(d)): p.read_bytes()
                for p in sorted(d.rglob("*")) if p.is_file()}

    def test_live_fingerprinted_checkpoint_left_byte_identical(self):
        # A real processing run stamps its checkpoint meta with the
        # book fingerprint; a finished run clears the directory, so a
        # fingerprinted directory on disk is an unfinished run's
        # resumable state.
        pp._checkpoint_save_meta(self.book_key, 2,
                                 fingerprint="0123456789abcdef")
        pp._checkpoint_save_chapter(self.book_key, 0,
                                    {"live": "a"}, {"live": "b"})
        pp._checkpoint_save_roster(self.book_key,
                                   {"Hero": {"name": "Hero"}})
        before = self._snapshot()
        self.assertTrue(before)
        _r, out = _capture(pp.cmd_retry_failed, str(self.preview_path))
        self.assertIn("Refusing", out)
        self.assertIn(self.book_key, out)
        self.assertEqual(self.calls, [])  # the retry never ran
        self.assertEqual(self._snapshot(), before)  # byte-identical

    def test_unfingerprinted_checkpoint_retries_as_before(self):
        # A retry seed / legacy checkpoint carries no fingerprint:
        # nothing live owns it, so retry proceeds exactly as before.
        pp._checkpoint_save_meta(self.book_key, 2)
        pp._checkpoint_save_chapter(self.book_key, 0, {"old": 1},
                                    {"old": 2})
        _r, out = _capture(pp.cmd_retry_failed, str(self.preview_path))
        self.assertIn("Retrying 1 failed chapters: [1]", out)
        self.assertEqual(len(self.calls), 1)


# ----------------------------------------------------------------------
# #49: cmd_corpus_reembed per-work isolation
# ----------------------------------------------------------------------

class TestCorpusReembedIsolation(unittest.TestCase):
    FPS = ["a" * 64, "b" * 64, "c" * 64]

    def _run(self):
        class _DB:
            def query(_self, sql, params=()):
                return [(fp,) for fp in self.FPS]

        def fake_embed(db, fp, embed_fn, model):
            if fp == self.FPS[1]:
                raise KeyError("chapter row missing")
            return "complete"

        with mock.patch.object(pp, "_corpus_config_embedder",
                               return_value=(lambda texts: [],
                                             "model-x")), \
                mock.patch.object(pp, "_corpus_db_for",
                                  return_value=_DB()), \
                mock.patch.object(pp, "corpus_embed_work", fake_embed):
            return _capture(pp.cmd_corpus_reembed,
                            learn_dir="/nonexistent-learn")

    def test_middle_work_failure_isolated_named_and_counted(self):
        results, out = self._run()
        by_fp = {r["fingerprint"]: r["outcome"] for r in results}
        self.assertEqual(by_fp[self.FPS[0]], "complete")
        self.assertEqual(by_fp[self.FPS[2]], "complete")
        self.assertEqual(by_fp[self.FPS[1]], "failed")
        # The failure is named with its error, and the summary line
        # counts it so a caller can see the run was not clean.
        self.assertIn(self.FPS[1][:8], out)
        self.assertIn("chapter row missing", out)
        self.assertIn("1 failed", out)


# ----------------------------------------------------------------------
# #50: cmd_ingest surfaces opening-line stage failures
# ----------------------------------------------------------------------

class TestIngestOpeningLineFailureVisible(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        (self.td / "bad.epub").write_bytes(b"fake")
        (self.td / "good.epub").write_bytes(b"fake")
        self.fp_bad = "d" * 64
        self.fp_good = "e" * 64

    def tearDown(self):
        self.tmp.cleanup()

    def test_opening_line_failure_named_recorded_and_isolated(self):
        def fake_ingest(db, f, reason="", force=False, embedder=None):
            fp = self.fp_bad if f.name == "bad.epub" else self.fp_good
            return {"fingerprint": fp, "source_name": f.name,
                    "outcome": "complete", "chapters": 3,
                    "passages": 30, "detail": ""}

        seen = []

        def fake_opening(db, fp):
            seen.append(fp)
            if fp == self.fp_bad:
                raise RuntimeError("stage table locked")
            return {"quote_id": 1, "text": "An opening."}

        with mock.patch.object(pp, "corpus_ingest_book", fake_ingest), \
                mock.patch.object(pp, "_corpus_config_embedder",
                                  return_value=None), \
                mock.patch.object(pp, "_close_corpus_dbs",
                                  lambda: None), \
                mock.patch.object(pp.CorpusDB, "open",
                                  staticmethod(lambda *a, **k: object())), \
                mock.patch.object(pp, "corpus_generate_opening_line",
                                  fake_opening):
            results, out = _capture(pp.cmd_ingest, [self.td],
                                    learn_dir=self.td / "learned")
        # Both books were attempted: one bad book did not abort the
        # stage for the rest.
        self.assertEqual(seen, [self.fp_bad, self.fp_good])
        # The failure is visible: named (book fingerprint) with its
        # error in the printed output...
        self.assertIn(self.fp_bad[:8], out)
        self.assertIn("stage table locked", out)
        self.assertIn("opening line", out.lower())
        # ...and reflected in the returned ingest results.
        by_fp = {r["fingerprint"]: r for r in results}
        self.assertIn("stage table locked",
                      by_fp[self.fp_bad].get("opening_line_error", ""))
        self.assertNotIn("opening_line_error", by_fp[self.fp_good])


# ----------------------------------------------------------------------
# #52: evaluate-merges-batched malformed items + honest baseline
# ----------------------------------------------------------------------

class TestBatchedEvalMalformedAndBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _labels(self):
        labels = {
            "p1": {"a": "Alice Amber", "b": "Alicia Amber",
                   "same_person": True},
            "p2": {"a": "Bob Birch", "b": "Rob Birch",
                   "same_person": False},
            "p3bad": {"a": "No Partner"},  # missing b + same_person
            "p4": {"a": "Carol Cedar", "b": "Carol Cedar-Smith",
                   "same_person": True},
        }
        path = self.td / "labels.json"
        path.write_text(json.dumps(labels), encoding="utf-8")
        return str(path)

    @staticmethod
    def _fake_post(base_url, model, api_key, payload, headers, timeout):
        return {"answers": {k: {"noul": 0.9}
                            for k in payload["questions"]}}

    def _main(self, argv):
        with mock.patch.object(emb, "_post", self._fake_post), \
                mock.patch.object(sys, "argv", argv):
            return _capture(emb.main)

    def test_malformed_skipped_no_traceback_no_batch1_needed(self):
        _r, out = self._main(
            ["evaluate-merges-batched.py", "--labels", self._labels(),
             "--provider", "local", "--batch-sizes", "5,10"])
        # The malformed item is reported by key and counted.
        self.assertIn("p3bad", out)
        self.assertIn("malformed", out.lower())
        # Both requested batches ran and the results printed — the
        # old code crashed with KeyError: 1 here, after all spend.
        self.assertIn("[bs=5]", out)
        self.assertIn("[bs=10]", out)
        self.assertIn("RESULTS", out)
        # No baseline was run, and the output says so instead of
        # inventing one; the API-call tally names only real batches.
        self.assertIn("no single-question baseline", out.lower())
        self.assertNotIn("batch=1:", out)

    def test_baseline_identity_is_production_instructions(self):
        # The baseline judges with the production instruction text —
        # the same constant evaluate-merges.py pins against
        # production — not the old name-only YES_NO_CRITERIA rules.
        self.assertIn("positive evidence of one identity",
                      emb.PRODUCTION_INSTRUCTIONS)
        self.assertFalse(hasattr(emb, "YES_NO_CRITERIA"))
        seen = {}

        def fake_post(base_url, model, api_key, payload, headers,
                      timeout):
            seen.update(payload)
            return {"answers": {"same_person": {"noul": 0.7}}}

        with mock.patch.object(emb, "_post", fake_post):
            emb.ask_single("http://x/v1", "m1", "key",
                           "Alice", "Alicia", {})
        q = seen["questions"]["same_person"]
        self.assertEqual(q["instructions"], emb.PRODUCTION_INSTRUCTIONS)

    def test_single_run_reports_name_only_mode(self):
        _r, out = self._main(
            ["evaluate-merges-batched.py", "--labels", self._labels(),
             "--provider", "local", "--batch-sizes", "1,5"])
        # Labels carry no descriptions/evidence, so every pair is
        # name-only and the report must say the baseline measures a
        # judge production never runs.
        self.assertIn("name-only", out)
        self.assertIn("agreement-vs-single", out)


# ----------------------------------------------------------------------
# #60: --save-profile x {--preview, --dry-run} honoured again
# ----------------------------------------------------------------------

class TestSaveProfilePreviewDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self._profiles = mock.patch.object(pp, "PROFILES_DIR", self.td)
        self._profiles.start()

    def tearDown(self):
        self._profiles.stop()
        self.tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(buf):
            pp.main()
        return buf.getvalue()

    def test_save_profile_with_preview(self):
        self._main(["process-portable.py", "--save-profile", "prof",
                    "--preview"])
        saved = json.loads((self.td / "prof.json").read_text(
            encoding="utf-8"))
        self.assertEqual(saved.get("preview"), True)

    def test_save_profile_with_dry_run(self):
        self._main(["process-portable.py", "--save-profile", "prof",
                    "--dry-run"])
        saved = json.loads((self.td / "prof.json").read_text(
            encoding="utf-8"))
        self.assertEqual(saved.get("dry_run"), True)

    def test_save_profile_with_watch_still_refused(self):
        # The exemption is only for the persistable preview/dry-run
        # pairing; a dropped run mode beside --save-profile still
        # refuses with exit 2 and saves nothing.
        with self.assertRaises(SystemExit) as cm:
            self._main(["process-portable.py", "--save-profile",
                        "prof", "--watch"])
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse((self.td / "prof.json").exists())


if __name__ == "__main__":
    unittest.main()
