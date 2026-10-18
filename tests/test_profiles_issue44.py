"""Issue #44: saved profiles persist one-shot commands and replay them
on every later --profile invocation.

_PROFILE_EXCLUDE named only a handful of dests, while _save_profile
persisted every other non-default value — including one-shot command
dests such as ingest, corpus_remove, restore_state, backup_state and
retry_failed. _apply_profile restored the whole namespace and main
applies the profile before the entire dispatch chain, so a profile
saved during a one-shot session (STEP 1 of the issue reproduction)
dispatched that one-shot again on every later plain profile run
(STEP 2) and the requested run never happened. With corpus_remove or
restore_state in a profile, a destructive or state-replacing command
fired on every profile invocation.

The invariant these tests pin: a saved profile contains ONLY
run-shaping settings — never a one-shot command dest, never a
per-run selector (file / overrides / concrete chunk or book counts),
never a companion argument whose only job is to aim a one-shot, and
never a profile meta dest. The classification is exhaustive: every
dest the parser defines is classified exactly once, so a future flag
shipped unclassified fails here loudly instead of silently deciding
for itself whether profiles persist it. Saving gates on the
persistable allowlist, so an unclassified new dest is dropped from
profiles (the safe failure) rather than persisted (the dangerous
one). A profile file written before this fix that already contains a
one-shot dest has that dest IGNORED on apply, with a printed note —
its run-shaping settings still apply.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest tests.test_profiles_issue44 -v
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("pp_profiles44", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


# One-shot commands and per-run selectors a profile must never carry.
# Hard-coded here (not read back out of the implementation) so this
# list is an independent statement of the invariant: if a future
# change reclassifies one of these as persistable, this test is the
# argument that change has to win.
NEVER_PERSIST = (
    # one-shot commands that dispatch and exit
    "ingest", "corpus_remove", "corpus_reembed", "corpus_status",
    "corpus_snapshot", "restore_state", "backup_state", "retry_failed",
    "trope_map", "validate", "push_preview", "verify",
    "preview_diff", "recalibrate_spice", "corpus_verify_quote",
    "apply_uncertainty_labels", "capture_snapshot", "replay",
    "learn_status", "staging_report", "roster_report", "roster_link",
    "roster_unlink", "roster_split", "calibration_report",
    "explain", "watch", "auto_update",
    # per-run selectors / overrides for a single run
    "file", "isbn", "series", "series_position", "chunks", "max_books",
    "alias", "resume", "import_labels", "import_rejections",
    "from_supabase",
    # companion arguments that only aim a one-shot command
    "quote", "work", "explain_claim", "explain_character",
    "ingest_reason", "ingest_force", "snapshot_reason", "json",
    "watch_ingest",
    # profile / mode meta dests, and the state-location path selector
    "profile", "save_profile", "list_profiles", "guided", "learn_dir",
)


def _sentinel(default):
    """A non-default, JSON-serializable value for a parser dest."""
    if isinstance(default, bool):
        return not default
    if isinstance(default, list):
        return ["sentinel"]
    if isinstance(default, (int, float)):
        return default + 1
    return "sentinel"


class _ProfileDirGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = mock.patch.object(
            pp, "PROFILES_DIR", Path(self.tmp.name))
        self.patcher.start()
        self.parser = pp._build_parser()
        self.defaults = self.parser.parse_args([])

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _save(self, name, **overrides):
        args = self.parser.parse_args([])
        for dest, val in overrides.items():
            setattr(args, dest, val)
        path = pp._save_profile(name, args, self.defaults)
        return json.loads(path.read_text(encoding="utf-8"))


class TestSaveNeverPersistsOneShots(_ProfileDirGuard):
    def test_corpus_remove_invocation_saves_no_corpus_remove(self):
        # Issue reproduction, STEP 1 shape: the save invocation itself
        # carries the one-shot flag.
        data = self._save("base", corpus_remove="deadbeefcafe",
                          llm="openai")
        self.assertNotIn("corpus_remove", data)
        self.assertEqual(data.get("llm"), "openai")

    def test_ingest_invocation_saves_no_ingest(self):
        data = self._save("base", ingest="book.epub", preview=True)
        self.assertNotIn("ingest", data)
        self.assertTrue(data.get("preview"))

    def test_no_never_persist_dest_is_saved(self):
        for dest in NEVER_PERSIST:
            with self.subTest(dest=dest):
                default = getattr(self.defaults, dest)
                data = self._save("p", **{dest: _sentinel(default)})
                self.assertNotIn(dest, data)

    def test_run_shaping_settings_still_save(self):
        data = self._save("shaping", llm="openai", batch=8, preview=True,
                          pipeline="v2", decision_model=True)
        self.assertEqual(data, {"llm": "openai", "batch": 8,
                                "preview": True, "pipeline": "v2",
                                "decision_model": True})


class TestExhaustiveClassification(unittest.TestCase):
    def setUp(self):
        self.parser = pp._build_parser()

    def test_every_parser_dest_classified_exactly_once(self):
        dests = set(vars(self.parser.parse_args([])))
        persistable = set(pp._PROFILE_PERSISTABLE)
        excluded = set(pp._PROFILE_EXCLUDE)
        self.assertEqual(persistable & excluded, set(),
                         "a dest cannot be both persistable and excluded")
        self.assertEqual(persistable | excluded, dests,
                         "unclassified parser dests: "
                         f"{sorted(dests - persistable - excluded)}; "
                         "stale classifications: "
                         f"{sorted((persistable | excluded) - dests)}")

    def test_never_persist_list_is_excluded(self):
        for dest in NEVER_PERSIST:
            with self.subTest(dest=dest):
                self.assertIn(dest, pp._PROFILE_EXCLUDE)
                self.assertNotIn(dest, pp._PROFILE_PERSISTABLE)

    def test_builtin_profiles_use_only_persistable_keys(self):
        for name, profile in pp.BUILTIN_PROFILES.items():
            with self.subTest(profile=name):
                self.assertLessEqual(set(profile),
                                     set(pp._PROFILE_PERSISTABLE))


class TestLegacyProfileOneShotsIgnored(_ProfileDirGuard):
    def _write_legacy(self, name, data):
        path = Path(self.tmp.name) / f"{name}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_legacy_one_shot_dests_ignored_with_note(self):
        self._write_legacy("legacy", {
            "corpus_remove": "deadbeefcafe",
            "restore_state": "/some/backup/dir",
            "ingest": "book.epub",
            "llm": "openai",
            "batch": 4,
        })
        profile = pp._load_profile("legacy")
        args = self.parser.parse_args([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pp._apply_profile(args, self.defaults, profile)
        # The one-shots are NOT restored onto the namespace, so no
        # dispatch in main can fire them.
        self.assertIsNone(args.corpus_remove)
        self.assertIsNone(args.restore_state)
        self.assertIsNone(args.ingest)
        # Run-shaping settings from the same legacy file still apply.
        self.assertEqual(args.llm, "openai")
        self.assertEqual(args.batch, 4)
        # And the operator can see what was ignored and why.
        printed = out.getvalue()
        for dest in ("corpus_remove", "restore_state", "ingest"):
            self.assertIn(dest, printed)


class TestProfileInvocationDoesNotDispatch(_ProfileDirGuard):
    """The issue reproduction, STEP 2, driven through real main()."""

    def setUp(self):
        super().setUp()
        self._cfg = dict(pp.CONFIG)
        self._old_learned = pp._LEARNED
        self._load = mock.patch.object(pp, "load_config", lambda: None)
        self._load.start()

    def tearDown(self):
        self._load.stop()
        pp._LEARNED = self._old_learned
        pp.CONFIG.clear()
        pp.CONFIG.update(self._cfg)
        super().tearDown()

    def _main(self, argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(buf):
            pp.main()
        return buf.getvalue()

    def test_plain_profile_run_does_not_dispatch_corpus_remove(self):
        # STEP 1: save a profile during a corpus_remove invocation.
        save_args = self.parser.parse_args(
            ["--corpus-remove", "deadbeefcafe"])
        pp._save_profile("base", save_args, self.defaults)
        # STEP 2: a plain profile invocation must run the requested
        # run, not replay corpus_remove.
        with mock.patch.object(pp, "cmd_corpus_remove") as m_remove, \
                mock.patch.object(pp, "run_once", return_value=0) as m_run:
            with self.assertRaises(SystemExit) as cm:
                self._main(["process-portable.py", "--profile", "base",
                            "--file", "other.epub", "--preview"])
        self.assertEqual(cm.exception.code, 0)
        m_remove.assert_not_called()
        m_run.assert_called_once_with(False, True, None, "other.epub")


if __name__ == "__main__":
    unittest.main()
