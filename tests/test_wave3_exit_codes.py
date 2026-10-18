"""Wave 3 batch A: CLI exit-code and one-shot semantics (#45/#46/#47/#51).

The family defect: one-shot / sequential CLI paths could fail, move
things, or ignore flags while the exit code and output claimed
success — and the portal reads rc==0 as "done", so every lie here
became a false completion in the UI.

- #45: the sequential (non-parallel) loop in _run_once_inner moved
  --file books into done/failed/; the parallel wrapper's don't-move
  guard was unreachable for single-file runs (a --file run always
  has exactly one file, so it never takes the parallel branch).
- #46: main() discarded cmd_push_preview write failures and
  cmd_backup_state / cmd_restore_state {"ok": False} refusals and
  exited 0. (The --file/run_once path already propagated failures:
  main exits 1 when run_once returns a failure count — pinned here
  as regression cover, not changed.)
- #47: a decision-model ValueError in main()'s run path printed
  "Error: ..." and returned -> exit 0, nothing processed.
- #51: the one-shot dispatch chain silently ran only the first
  matching one-shot and dropped every other operation/mode flag
  (--validate + --push-preview, --watch + --verify, --push-preview
  + --file). Combinations now refuse loudly at startup.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
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

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable_wave3",
                                              _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _capture(fn, *args, **kwargs):
    """Run fn capturing stdout; return (result, output)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


class _ConfigGuard(unittest.TestCase):
    """Snapshot/restore the module CONFIG dict in place."""

    def setUp(self):
        self._cfg = dict(pp.CONFIG)

    def tearDown(self):
        pp.CONFIG.clear()
        pp.CONFIG.update(self._cfg)


# ----------------------------------------------------------------------
# #45 — sequential path must not move --file books
# ----------------------------------------------------------------------

class Test45SingleFileNotMoved(_ConfigGuard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.elsewhere = root / "elsewhere"
        self.elsewhere.mkdir()
        self.imports = root / "import"
        (self.imports / "done").mkdir(parents=True)
        (self.imports / "failed").mkdir(parents=True)
        self._saved_dirs = (pp.IMPORT_DIR, pp.DONE_DIR, pp.FAILED_DIR)
        pp.IMPORT_DIR = self.imports
        pp.DONE_DIR = self.imports / "done"
        pp.FAILED_DIR = self.imports / "failed"
        self._saved_single = getattr(pp._DIAG, "single_file", None)
        pp.CONFIG["jobs"] = 1

    def tearDown(self):
        pp.IMPORT_DIR, pp.DONE_DIR, pp.FAILED_DIR = self._saved_dirs
        pp._DIAG.single_file = self._saved_single
        self.tmp.cleanup()
        super().tearDown()

    def _run_once(self, ok, file=None):
        with mock.patch.object(pp, "process_file", return_value=ok), \
                mock.patch.object(pp.UI, "start", lambda: None), \
                mock.patch.object(pp.UI, "stop", lambda: None):
            return _capture(pp.run_once, False, False, file=file)[0]

    def test_single_file_success_stays_in_place(self):
        book = self.elsewhere / "book.epub"
        book.write_bytes(b"epub")
        rc = self._run_once(True, file=str(book))
        self.assertEqual(rc, 0)
        self.assertTrue(book.exists())
        self.assertFalse((pp.DONE_DIR / "book.epub").exists())
        self.assertFalse((pp.FAILED_DIR / "book.epub").exists())

    def test_single_file_failure_stays_in_place(self):
        book = self.elsewhere / "book.epub"
        book.write_bytes(b"epub")
        rc = self._run_once(False, file=str(book))
        self.assertEqual(rc, 1)
        self.assertTrue(book.exists())
        self.assertFalse((pp.DONE_DIR / "book.epub").exists())
        self.assertFalse((pp.FAILED_DIR / "book.epub").exists())

    def test_single_file_stays_in_place_with_jobs_4(self):
        # A --file run has exactly one file, so it takes the
        # sequential branch at ANY --jobs setting — the parallel
        # wrapper's guard can never protect it.
        pp.CONFIG["jobs"] = 4
        book = self.elsewhere / "book.epub"
        book.write_bytes(b"epub")
        rc = self._run_once(True, file=str(book))
        self.assertEqual(rc, 0)
        self.assertTrue(book.exists())
        self.assertFalse((pp.DONE_DIR / "book.epub").exists())

    def test_folder_book_moves_to_done_on_success(self):
        book = self.imports / "folder.epub"
        book.write_bytes(b"epub")
        rc = self._run_once(True)
        self.assertEqual(rc, 0)
        self.assertFalse(book.exists())
        self.assertTrue((pp.DONE_DIR / "folder.epub").exists())

    def test_folder_book_moves_to_failed_on_failure(self):
        book = self.imports / "folder.epub"
        book.write_bytes(b"epub")
        rc = self._run_once(False)
        self.assertEqual(rc, 1)
        self.assertFalse(book.exists())
        self.assertTrue((pp.FAILED_DIR / "folder.epub").exists())

    def test_parallel_wrapper_and_sequential_agree(self):
        # _process_one (the parallel wrapper) already guards --file
        # books; pin both origins through it so the two paths cannot
        # drift apart again.
        single = self.elsewhere / "single.epub"
        single.write_bytes(b"epub")
        pp._DIAG.single_file = str(single)
        with mock.patch.object(pp, "process_file", return_value=True):
            _capture(pp._process_one, (single, False, False, 1))
        self.assertTrue(single.exists())

        folder = self.imports / "folder.epub"
        folder.write_bytes(b"epub")
        with mock.patch.object(pp, "process_file", return_value=True):
            _capture(pp._process_one, (folder, False, False, 1))
        self.assertFalse(folder.exists())
        self.assertTrue((pp.DONE_DIR / "folder.epub").exists())


# ----------------------------------------------------------------------
# #46 — one-shot failures must exit nonzero
# ----------------------------------------------------------------------

class Test46PushPreviewExitCode(_ConfigGuard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        pp.CONFIG["supabase_url"] = "http://fake.invalid"
        pp.CONFIG["supabase_key"] = "fake-key"
        self._patches = [
            mock.patch.object(pp, "resolve_work",
                              mock.Mock(return_value=(1, "existing"))),
            mock.patch.object(pp, "correct_metadata",
                              lambda t, a, ids: (t, a)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()
        super().tearDown()

    def _preview(self, name, title):
        data = {"file": f"{title}.epub", "title": title,
                "author": "Some Author",
                "tropes": [], "triggers": [], "relationships": [],
                "characters": [{"name": "Hero", "role": "protagonist"}],
                "quotes": []}
        p = self.td / name
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    def test_write_failure_returns_nonzero(self):
        with mock.patch.object(pp, "write_claims", return_value=2):
            rc, out = _capture(pp.cmd_push_preview,
                               self._preview("a.json", "Alpha"))
        self.assertIn("2 database operation(s) failed", out)
        self.assertEqual(rc, 1)

    def test_write_success_returns_zero(self):
        with mock.patch.object(pp, "write_claims", return_value=0):
            rc, out = _capture(pp.cmd_push_preview,
                               self._preview("a.json", "Alpha"))
        self.assertIn("Push complete, no errors", out)
        self.assertEqual(rc, 0)

    def test_directory_mixed_outcome_returns_nonzero(self):
        self._preview("a.json", "Alpha")
        self._preview("b.json", "Beta")

        def fake_write(wid, result):
            return 1 if result.get("title") == "Beta" else 0

        with mock.patch.object(pp, "write_claims",
                               side_effect=fake_write):
            rc, _out = _capture(pp.cmd_push_preview, str(self.td))
        self.assertEqual(rc, 1)

    def test_directory_all_success_returns_zero(self):
        self._preview("a.json", "Alpha")
        self._preview("b.json", "Beta")
        with mock.patch.object(pp, "write_claims", return_value=0):
            rc, _out = _capture(pp.cmd_push_preview, str(self.td))
        self.assertEqual(rc, 0)


class Test46MainDispatchExitCodes(_ConfigGuard):
    def setUp(self):
        super().setUp()
        self._old_learned = pp._LEARNED
        self._load = mock.patch.object(pp, "load_config", lambda: None)
        self._load.start()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._load.stop()
        pp._LEARNED = self._old_learned
        self.tmp.cleanup()
        super().tearDown()

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                pp.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_push_preview_failure_exits_nonzero(self):
        with mock.patch.object(pp, "cmd_push_preview",
                               return_value=1) as m:
            code, _o, _e = self._main(
                ["process-portable.py", "--push-preview", "x.json"])
        self.assertEqual(code, 1)
        m.assert_called_once_with("x.json")

    def test_push_preview_success_exits_zero(self):
        with mock.patch.object(pp, "cmd_push_preview", return_value=0):
            code, _o, _e = self._main(
                ["process-portable.py", "--push-preview", "x.json"])
        self.assertEqual(code, 0)

    def test_backup_refusal_exits_nonzero(self):
        with mock.patch.object(
                pp, "cmd_backup_state",
                return_value={"ok": False,
                              "reason": "no stores present"}) as m:
            code, _o, _e = self._main(
                ["process-portable.py", "--backup-state", "/tmp/bk"])
        self.assertEqual(code, 1)
        m.assert_called_once()

    def test_backup_success_returns_normally(self):
        with mock.patch.object(pp, "cmd_backup_state",
                               return_value={"ok": True}):
            code, _o, _e = self._main(
                ["process-portable.py", "--backup-state", "/tmp/bk"])
        self.assertIsNone(code)

    def test_restore_refusal_exits_nonzero(self):
        with mock.patch.object(
                pp, "cmd_restore_state",
                return_value={"ok": False, "reason": "no manifest"}):
            code, _o, _e = self._main(
                ["process-portable.py", "--restore-state", "/tmp/bk"])
        self.assertEqual(code, 1)

    def test_restore_success_returns_normally(self):
        with mock.patch.object(pp, "cmd_restore_state",
                               return_value={"ok": True}):
            code, _o, _e = self._main(
                ["process-portable.py", "--restore-state", "/tmp/bk"])
        self.assertIsNone(code)

    def test_restore_real_no_manifest_dir_exits_nonzero(self):
        # The verified live reproduction from the issue: a restore
        # pointed at a directory with no manifest prints its refusal
        # and must not exit 0.
        empty = Path(self.tmp.name) / "not-a-backup"
        empty.mkdir()
        code, out, _e = self._main(
            ["process-portable.py", "--restore-state", str(empty)])
        self.assertIn("not a backup", out)
        self.assertEqual(code, 1)


# ----------------------------------------------------------------------
# #47 — decision-model configuration errors must exit nonzero
# ----------------------------------------------------------------------

class Test47DecisionModelConfigError(_ConfigGuard):
    def setUp(self):
        super().setUp()
        self._old_learned = pp._LEARNED
        self._load = mock.patch.object(pp, "load_config", lambda: None)
        self._load.start()
        self._old_validator = pp._DECISION_VALIDATOR

    def tearDown(self):
        self._load.stop()
        pp._LEARNED = self._old_learned
        pp._DECISION_VALIDATOR = self._old_validator
        super().tearDown()

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                pp.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue()

    def test_openrouter_without_key_exits_nonzero(self):
        pp.CONFIG["openrouter_api_key"] = ""
        pp.CONFIG["openrouter_key"] = ""
        env = {k: v for k, v in os.environ.items()
               if k != "OPENROUTER_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(pp, "run_once", return_value=0) as run:
            code, out = self._main(
                ["process-portable.py", "--decision-model",
                 "--decision-model-provider", "openrouter", "--preview"])
        self.assertIn("needs an API key", out)
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_local_without_base_url_exits_nonzero(self):
        pp.CONFIG["openai_base_url"] = ""
        with mock.patch.object(pp, "run_once", return_value=0) as run:
            code, out = self._main(
                ["process-portable.py", "--decision-model", "--preview"])
        self.assertIn("needs a base URL", out)
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_no_decision_model_run_unchanged(self):
        # No --decision-model, no URL, default (local) provider:
        # nothing is resolved and the run proceeds exactly as today.
        with mock.patch.object(pp, "run_once", return_value=0) as run:
            code, _out = self._main(["process-portable.py", "--preview"])
        self.assertEqual(code, 0)
        run.assert_called_once_with(False, True, None, None)

    def test_valid_local_config_proceeds(self):
        pp.CONFIG["openai_base_url"] = "http://127.0.0.1:9/v1"
        with mock.patch.object(pp, "run_once", return_value=0) as run:
            code, out = self._main(
                ["process-portable.py", "--decision-model", "--preview"])
        self.assertEqual(code, 0)
        self.assertIn("Decision model trigger validation", out)
        run.assert_called_once()


# ----------------------------------------------------------------------
# #51 — one-shot combinations refuse loudly; single modes honoured
# ----------------------------------------------------------------------

class Test51FlagConflicts(_ConfigGuard):
    def setUp(self):
        super().setUp()
        self._old_learned = pp._LEARNED
        self._load = mock.patch.object(pp, "load_config", lambda: None)
        self._load.start()

    def tearDown(self):
        self._load.stop()
        pp._LEARNED = self._old_learned
        super().tearDown()

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                pp.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def _conflicts(self, argv):
        parser = pp._build_parser()
        return pp._flag_conflicts(parser.parse_args(argv),
                                  parser.parse_args([]))

    # -- the three combinations named in the issue --

    def test_validate_plus_push_preview_refused(self):
        with mock.patch.object(pp, "cmd_validate") as mv, \
                mock.patch.object(pp, "cmd_push_preview") as mp:
            code, _o, err = self._main(
                ["process-portable.py", "--validate", "a.json",
                 "--push-preview", "b.json"])
        self.assertEqual(code, 2)
        self.assertIn("--validate", err)
        self.assertIn("--push-preview", err)
        mv.assert_not_called()
        mp.assert_not_called()

    def test_watch_plus_verify_refused(self):
        with mock.patch.object(pp, "cmd_verify") as mv:
            code, _o, err = self._main(
                ["process-portable.py", "--watch", "--verify", "x.json"])
        self.assertEqual(code, 2)
        self.assertIn("--verify", err)
        self.assertIn("--watch", err)
        mv.assert_not_called()

    def test_push_preview_plus_file_refused(self):
        with mock.patch.object(pp, "cmd_push_preview") as mp:
            code, _o, err = self._main(
                ["process-portable.py", "--push-preview", "x.json",
                 "--file", "book.epub"])
        self.assertEqual(code, 2)
        self.assertIn("--push-preview", err)
        self.assertIn("--file", err)
        mp.assert_not_called()

    # -- more of the same family --

    def test_backup_plus_restore_refused(self):
        code, _o, err = self._main(
            ["process-portable.py", "--backup-state", "/tmp/a",
             "--restore-state", "/tmp/b"])
        self.assertEqual(code, 2)
        self.assertIn("--backup-state", err)
        self.assertIn("--restore-state", err)

    def test_replay_plus_watch_refused(self):
        code, _o, err = self._main(
            ["process-portable.py", "--replay", "snap1",
             "--file", "book.epub", "--watch"])
        self.assertEqual(code, 2)
        self.assertIn("--replay", err)
        self.assertIn("--watch", err)

    def test_guided_plus_validate_refused_before_prompts(self):
        with mock.patch("builtins.input") as minput:
            code, _o, err = self._main(
                ["process-portable.py", "--guided",
                 "--validate", "x.json"])
        self.assertEqual(code, 2)
        self.assertIn("--validate", err)
        minput.assert_not_called()

    def test_one_shot_plus_auto_update_refused(self):
        code, _o, err = self._main(
            ["process-portable.py", "--validate", "x.json",
             "--auto-update"])
        self.assertEqual(code, 2)
        self.assertIn("--auto-update", err)

    # -- combinations that must keep working (honoured, not refused) --

    def test_single_one_shot_dispatches(self):
        with mock.patch.object(pp, "cmd_validate") as m:
            code, _o, _e = self._main(
                ["process-portable.py", "--validate", "x.json"])
        self.assertIsNone(code)
        m.assert_called_once_with("x.json")

    def test_one_shot_with_companion_flags_dispatches(self):
        with mock.patch.object(pp, "cmd_corpus_reembed") as m:
            code, _o, _e = self._main(
                ["process-portable.py", "--corpus-reembed",
                 "--work", "abc123"])
        self.assertIsNone(code)
        m.assert_called_once_with(learn_dir=None, work="abc123")

    def test_one_shot_with_run_shaping_config_dispatches(self):
        # Run-shaping settings are configuration, not a second
        # operation: a one-shot beside them is the operator's one
        # command and still runs (the snapshot dispatch's exit code
        # passthrough is the existing convention).
        with mock.patch.object(pp, "cmd_corpus_snapshot",
                               return_value=7) as m:
            code, _o, _e = self._main(
                ["process-portable.py", "--corpus-snapshot",
                 "--run-history"])
        self.assertEqual(code, 7)
        m.assert_called_once()

    def test_run_combo_with_run_shaping_flags_honoured(self):
        # The brief's examples: --run-history / --emit-run-events /
        # --batch beside a run are honoured by the run path itself.
        with mock.patch.object(pp, "run_once", return_value=0) as run:
            code, _o, _e = self._main(
                ["process-portable.py", "--preview", "--run-history",
                 "--emit-run-events", "--batch", "4",
                 "--file", "book.epub"])
        self.assertEqual(code, 0)
        run.assert_called_once_with(False, True, None, "book.epub")
        self.assertTrue(pp.CONFIG.get("run_history"))
        self.assertTrue(pp.CONFIG.get("emit_run_events"))
        self.assertEqual(pp.CONFIG.get("batch_size"), 4)

    def test_replay_with_file_dispatches(self):
        pp.CONFIG["supabase_url"] = "http://fake.invalid"
        pp.CONFIG["supabase_key"] = "fake-key"
        with mock.patch.object(pp, "run_replay",
                               return_value=(0, {})) as m:
            code, _o, _e = self._main(
                ["process-portable.py", "--replay", "snap1",
                 "--file", "book.epub"])
        self.assertEqual(code, 0)
        m.assert_called_once()

    def test_helper_clean_combinations(self):
        for argv in (
            [],
            ["--file", "x.epub", "--preview"],
            ["--watch", "--file", "x.epub"],
            ["--watch", "--watch-ingest"],
            ["--ingest", "x.epub", "--ingest-reason", "r",
             "--ingest-force", "--learn-dir", "d"],
            ["--explain", "w", "--explain-claim", "c",
             "--learn-dir", "d"],
            ["--corpus-snapshot", "--json"],
            ["--corpus-verify-quote", "fp", "--quote", "some text"],
            ["--replay", "s", "--file", "b.epub"],
            ["--backup-state", "/tmp/b", "--learn-dir", "d"],
        ):
            self.assertIsNone(self._conflicts(argv), argv)

    def test_helper_names_every_conflicting_flag(self):
        msg = self._conflicts(["--validate", "a", "--push-preview", "b",
                               "--watch"])
        self.assertIn("--validate", msg)
        self.assertIn("--push-preview", msg)
        self.assertIn("--watch", msg)


if __name__ == "__main__":
    unittest.main()
