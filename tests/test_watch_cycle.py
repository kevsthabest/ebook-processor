"""Watch-cycle tests: the --watch-ingest seam in main()'s loop.

Stdlib unittest only. cmd_ingest and run_once are mocked, so no
corpus, no network, no real ebook files — the tests pin only the
cycle's ordering and failure isolation.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable_watch", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class TestWatchCycle(unittest.TestCase):
    def _args(self, argv):
        return pp._build_parser().parse_args(argv)

    def _cycle(self, args):
        """Run one _watch_cycle with ingest/scan mocked; return the
        shared mock manager so call order can be asserted."""
        manager = mock.Mock()
        with mock.patch.object(pp, "cmd_ingest") as ingest, \
                mock.patch.object(pp, "run_once") as scan:
            manager.attach_mock(ingest, "ingest")
            manager.attach_mock(scan, "run_once")
            pp._watch_cycle(args)
        return manager

    def test_flag_defaults_off(self):
        self.assertFalse(self._args(["--watch"]).watch_ingest)
        self.assertTrue(self._args(["--watch", "--watch-ingest"]).watch_ingest)

    def test_ingest_runs_before_scan_with_flag(self):
        manager = self._cycle(self._args(["--watch", "--watch-ingest"]))
        self.assertEqual(manager.mock_calls, [
            mock.call.ingest([pp.IMPORT_DIR], learn_dir=None,
                             reason="watch cycle"),
            mock.call.run_once(False, False, None, None),
        ])

    def test_no_ingest_without_flag(self):
        manager = self._cycle(self._args(["--watch"]))
        self.assertEqual(manager.mock_calls, [
            mock.call.run_once(False, False, None, None),
        ])

    def test_ingest_failure_still_scans(self):
        args = self._args(["--watch", "--watch-ingest"])
        with mock.patch.object(pp, "cmd_ingest",
                               side_effect=RuntimeError("corpus locked")), \
                mock.patch.object(pp, "run_once") as scan, \
                mock.patch("builtins.print") as mprint:
            pp._watch_cycle(args)  # must return normally, not raise
        scan.assert_called_once_with(False, False, None, None)
        warned = any("ingest failed this cycle" in str(c)
                     for c in mprint.call_args_list)
        self.assertTrue(warned)


if __name__ == "__main__":
    unittest.main()
