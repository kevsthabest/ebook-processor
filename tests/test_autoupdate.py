"""Auto-update tests: --auto-update flag, git helpers, checkpoint SHA.
Stdlib unittest only. No network, no real git operations (all mocked).
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable_autoupdate", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _mock_run(returncode=0, stdout="", stderr=""):
    m = mock.Mock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestGitCommitSha(unittest.TestCase):
    def test_returns_sha(self):
        with mock.patch("subprocess.run",
                        return_value=_mock_run(0, "abc123def456\n")):
            self.assertEqual(pp._git_commit_sha(), "abc123def456")

    def test_not_a_repo(self):
        with mock.patch("subprocess.run",
                        return_value=_mock_run(128, "", "not a git repo")):
            self.assertIsNone(pp._git_commit_sha())

    def test_git_missing(self):
        with mock.patch("subprocess.run",
                        side_effect=FileNotFoundError("git")):
            self.assertIsNone(pp._git_commit_sha())

    def test_timeout(self):
        import subprocess
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("git", 10)):
            self.assertIsNone(pp._git_commit_sha())


class TestCheckForUpdates(unittest.TestCase):
    def test_up_to_date(self):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[1] == "fetch":
                return _mock_run(0)
            return _mock_run(0, "0\n")
        with mock.patch("subprocess.run", side_effect=fake_run):
            behind, err = pp._check_for_updates()
        self.assertEqual(behind, 0)
        self.assertIsNone(err)

    def test_behind(self):
        def fake_run(cmd, **kw):
            if cmd[1] == "fetch":
                return _mock_run(0)
            return _mock_run(0, "5\n")
        with mock.patch("subprocess.run", side_effect=fake_run):
            behind, err = pp._check_for_updates()
        self.assertEqual(behind, 5)
        self.assertIsNone(err)

    def test_fetch_fails(self):
        with mock.patch("subprocess.run",
                        return_value=_mock_run(1, "", "network unreachable")):
            behind, err = pp._check_for_updates()
        self.assertEqual(behind, 0)
        self.assertIn("fetch failed", err)

    def test_not_a_repo(self):
        with mock.patch("subprocess.run",
                        side_effect=FileNotFoundError("git")):
            behind, err = pp._check_for_updates()
        self.assertEqual(behind, 0)
        self.assertIn("not installed", err)


class TestDoAutoUpdate(unittest.TestCase):
    def test_up_to_date_continues(self):
        with mock.patch.object(pp, "_check_for_updates",
                               return_value=(0, None)):
            with mock.patch("builtins.print") as mock_print:
                result = pp._do_auto_update()
        self.assertFalse(result)
        printed = " ".join(c.args[0] for c in mock_print.call_args_list)
        self.assertIn("up to date", printed.lower())

    def test_check_fails_continues(self):
        with mock.patch.object(pp, "_check_for_updates",
                               return_value=(0, "no network")):
            with mock.patch("builtins.print") as mock_print:
                result = pp._do_auto_update()
        self.assertFalse(result)
        printed = " ".join(c.args[0] for c in mock_print.call_args_list)
        self.assertIn("continuing", printed.lower())

    def test_pull_fails_continues(self):
        with mock.patch.object(pp, "_check_for_updates",
                               return_value=(3, None)):
            with mock.patch("subprocess.run",
                            return_value=_mock_run(1, "", "conflict")):
                with mock.patch("builtins.print") as mock_print:
                    result = pp._do_auto_update()
        self.assertFalse(result)
        printed = " ".join(c.args[0] for c in mock_print.call_args_list)
        self.assertIn("continuing", printed.lower())

    def test_pull_succeeds_reexecs(self):
        # os.execv replaces the process; mock it to verify args.
        with mock.patch.object(pp, "_check_for_updates",
                               return_value=(2, None)):
            with mock.patch("subprocess.run",
                            return_value=_mock_run(0, "updating")):
                with mock.patch("os.execv") as mock_execv:
                    with mock.patch("builtins.print"):
                        with mock.patch.object(pp.sys, "argv",
                                               ["process-portable.py",
                                                "--auto-update", "--preview",
                                                "--file", "book.epub"]):
                            pp._do_auto_update()
        # execv called with python + script + args minus --auto-update
        exec_path, arg_list = mock_execv.call_args[0]
        self.assertEqual(exec_path, pp.sys.executable)
        self.assertEqual(arg_list[0], pp.sys.executable)
        self.assertEqual(arg_list[1], "process-portable.py")
        self.assertNotIn("--auto-update", arg_list)
        self.assertIn("--preview", arg_list)
        self.assertIn("book.epub", arg_list)


class TestCheckpointSha(unittest.TestCase):
    def test_hash_includes_sha(self):
        # Two different SHAs produce different hashes.
        with mock.patch.object(pp, "_git_commit_sha",
                               return_value="aaa111"):
            h1 = pp._checkpoint_flags_hash()
        with mock.patch.object(pp, "_git_commit_sha",
                               return_value="bbb222"):
            h2 = pp._checkpoint_flags_hash()
        self.assertNotEqual(h1, h2)

    def test_hash_stable_same_sha(self):
        with mock.patch.object(pp, "_git_commit_sha",
                               return_value="aaa111"):
            h1 = pp._checkpoint_flags_hash()
            h2 = pp._checkpoint_flags_hash()
        self.assertEqual(h1, h2)

    def test_hash_no_git(self):
        # Graceful fallback when git unavailable.
        with mock.patch.object(pp, "_git_commit_sha", return_value=None):
            h = pp._checkpoint_flags_hash()
        self.assertEqual(len(h), 16)


class TestAutoUpdateFlag(unittest.TestCase):
    def test_flag_exists(self):
        ap = pp._build_parser()
        args = ap.parse_args(["--auto-update"])
        self.assertTrue(args.auto_update)

    def test_flag_default_off(self):
        ap = pp._build_parser()
        args = ap.parse_args([])
        self.assertFalse(args.auto_update)


if __name__ == "__main__":
    unittest.main()
