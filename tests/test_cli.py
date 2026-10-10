"""CLI tests: profiles (P1), flag groups (P2), guided mode (P3).
Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable_cli", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class TestBuiltinProfiles(unittest.TestCase):
    def test_mass_profile(self):
        mass = pp._load_profile("mass")
        self.assertIsNotNone(mass)
        self.assertTrue(mass["decision_model"])
        self.assertEqual(mass["decision_model_provider"], "openrouter")
        self.assertEqual(mass["llm"], "openai")
        self.assertEqual(mass["pipeline"], "v2")
        self.assertEqual(mass["batch"], 8)
        self.assertTrue(mass["rename_no_isbn"])
        self.assertTrue(mass["show_matrices"])

    def test_quick_profile(self):
        quick = pp._load_profile("quick")
        self.assertIsNotNone(quick)
        self.assertTrue(quick["preview"])
        self.assertEqual(quick["pipeline"], "v2")

    def test_missing_profile(self):
        self.assertIsNone(pp._load_profile("no-such-profile-xyz"))

    def test_available_includes_builtin(self):
        avail = pp._available_profiles()
        self.assertEqual(avail.get("mass"), "builtin")
        self.assertEqual(avail.get("quick"), "builtin")


class TestUserProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = mock.patch.object(pp, "PROFILES_DIR", Path(self.tmp.name))
        self.patcher.start()
        self.parser = pp._build_parser()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _args(self, argv):
        return self.parser.parse_args(argv)

    def test_save_and_load_roundtrip(self):
        args = self._args(["--llm", "openai", "--batch", "8", "--preview"])
        defaults = self.parser.parse_args([])
        path = pp._save_profile("myprof", args, defaults)
        self.assertTrue(path.is_file())
        loaded = pp._load_profile("myprof")
        self.assertEqual(loaded["llm"], "openai")
        self.assertEqual(loaded["batch"], 8)
        self.assertTrue(loaded["preview"])

    def test_save_excludes_meta_flags(self):
        args = self._args(["--file", "book.epub", "--save-profile", "x",
                           "--profile", "mass", "--guided", "--llm", "openai"])
        defaults = self.parser.parse_args([])
        path = pp._save_profile("x", args, defaults)
        data = json.loads(path.read_text(encoding="utf-8"))
        for excluded in ("file", "save_profile", "profile", "guided"):
            self.assertNotIn(excluded, data)
        self.assertEqual(data["llm"], "openai")

    def test_save_only_non_defaults(self):
        args = self._args([])
        defaults = self.parser.parse_args([])
        path = pp._save_profile("empty", args, defaults)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data, {})

    def test_available_lists_user_profile(self):
        args = self._args(["--preview"])
        defaults = self.parser.parse_args([])
        pp._save_profile("mine", args, defaults)
        avail = pp._available_profiles()
        self.assertEqual(avail.get("mine"), "user")
        self.assertEqual(avail.get("mass"), "builtin")

    def test_load_corrupt_profile_returns_none(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(pp._load_profile("bad"))

    def test_apply_profile_fills_unset_flags(self):
        args = self._args([])
        defaults = self.parser.parse_args([])
        pp._apply_profile(args, defaults, pp._load_profile("mass"))
        self.assertEqual(args.llm, "openai")
        self.assertEqual(args.batch, 8)
        self.assertEqual(args.pipeline, "v2")
        self.assertTrue(args.decision_model)
        self.assertTrue(args.rename_no_isbn)

    def test_apply_profile_cli_override_wins(self):
        args = self._args(["--batch", "4", "--llm", "ollama"])
        defaults = self.parser.parse_args([])
        pp._apply_profile(args, defaults, pp._load_profile("mass"))
        self.assertEqual(args.batch, 4)      # CLI wins
        self.assertEqual(args.llm, "ollama")  # CLI wins
        self.assertEqual(args.pipeline, "v2")  # profile fills rest

    def test_apply_profile_unknown_key_warns(self):
        args = self._args([])
        defaults = self.parser.parse_args([])
        with mock.patch("builtins.print") as mprint:
            pp._apply_profile(args, defaults, {"bogus_flag_xyz": 1, "llm": "openai"})
        warned = any("bogus_flag_xyz" in str(c) for c in mprint.call_args_list)
        self.assertTrue(warned)
        self.assertEqual(args.llm, "openai")


class TestFlagGroups(unittest.TestCase):
    def setUp(self):
        self.parser = pp._build_parser()

    def test_all_groups_present(self):
        help_text = self.parser.format_help()
        for group in ("Input:", "Models:", "Pipeline:", "Merging:",
                      "Learning:", "Output:", "ISBN:", "Debug:",
                      "Profiles & modes:"):
            self.assertIn(group, help_text)

    def test_all_expected_flags_exist(self):
        dests = {a.dest for a in self.parser._actions}
        for dest in (
            "file", "watch", "max_books", "isbn", "series", "series_position",
            "llm", "decision_model", "decision_model_provider",
            "decision_model_url", "batch",
            "pipeline", "chunks", "jobs", "sample", "full", "dedupe",
            "alias", "principals_only", "min_frequency",
            "learn", "learn_dir", "import_labels", "import_rejections",
            "from_supabase",
            "preview", "dry_run", "show_matrices", "no_viz",
            "push_preview", "verify", "validate", "trope_map",
            "allow_no_isbn", "rename_no_isbn",
            "debug", "prompt_cache", "shared_system", "task_last",
            "profile", "save_profile", "list_profiles", "guided",
        ):
            self.assertIn(dest, dests, f"missing flag dest: {dest}")

    def test_flag_in_correct_group(self):
        groups = {g.title: {a.dest for a in g._group_actions}
                  for g in self.parser._action_groups}
        self.assertIn("file", groups["Input"])
        self.assertIn("batch", groups["Models"])
        self.assertIn("pipeline", groups["Pipeline"])
        self.assertIn("alias", groups["Merging"])
        self.assertIn("learn", groups["Learning"])
        self.assertIn("preview", groups["Output"])
        self.assertIn("rename_no_isbn", groups["ISBN"])
        self.assertIn("debug", groups["Debug"])
        self.assertIn("profile", groups["Profiles & modes"])
        self.assertIn("guided", groups["Profiles & modes"])


class TestGuidedMode(unittest.TestCase):
    def setUp(self):
        self.parser = pp._build_parser()

    def _guided(self, inputs):
        args = self.parser.parse_args([])
        with mock.patch("builtins.input", side_effect=inputs):
            result = pp._guided_setup(args)
        return result, args

    def test_all_defaults(self):
        # file, then Enter for every default
        result, args = self._guided(["book.epub", "", "", "", ""])
        self.assertTrue(result)
        self.assertEqual(args.file, "book.epub")
        self.assertTrue(args.preview)          # default preview
        self.assertTrue(args.decision_model)   # default y
        self.assertTrue(args.principals_only)  # default y
        self.assertFalse(args.show_matrices)   # default n

    def test_write_mode(self):
        result, args = self._guided(["book.epub", "write", "", "", ""])
        self.assertTrue(result)
        self.assertFalse(args.preview)

    def test_explicit_no_answers(self):
        result, args = self._guided(["book.epub", "preview", "n", "n", "y"])
        self.assertTrue(result)
        self.assertTrue(args.preview)
        self.assertFalse(args.decision_model)
        self.assertFalse(args.principals_only)
        self.assertTrue(args.show_matrices)

    def test_yes_variants(self):
        result, args = self._guided(["b.epub", "", "YES", "Yes", ""])
        self.assertTrue(result)
        self.assertTrue(args.decision_model)
        self.assertTrue(args.principals_only)

    def test_quoted_path_stripped(self):
        result, args = self._guided(['"C:\\books\\my book.epub"', "", "", "", ""])
        self.assertTrue(result)
        self.assertEqual(args.file, "C:\\books\\my book.epub")

    def test_empty_file_aborts(self):
        result, args = self._guided([""])
        self.assertFalse(result)

    def test_keyboard_interrupt_cancels(self):
        args = self.parser.parse_args([])
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            result = pp._guided_setup(args)
        self.assertFalse(result)

    def test_eof_cancels(self):
        args = self.parser.parse_args([])
        with mock.patch("builtins.input", side_effect=EOFError):
            result = pp._guided_setup(args)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
