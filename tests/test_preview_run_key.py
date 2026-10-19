"""Preview filenames carry the run key (Decision 2026-10-19).

A preview used to be named after its book alone, and every run
overwrote the same file — so afterwards nobody could say which run
produced the artifact on disk, and measurements kept citing files
by a number that turned out not to be a run number at all. When the
run has an identity (run-events sink live, or the run-history
recorder armed), save_preview now names the files
<book>--<run-key>.json/.md: the same key the run-events artifact
and the run-history rows carry, so a preview names its run by
construction. With no run identity in the process, the name is
exactly what it has always been — there is nothing truthful to add.

Precedence pins the flush's own rule: the sink's artifact stem
wins over the recorder's provisional key (the sink may have
suffixed its filename at creation, and the flush adopts the stem),
so a preview written mid-run already carries the identity the run
will be recorded under.

Stdlib unittest only. Run: python -m unittest tests.test_preview_run_key
"""
import contextlib
import importlib.util
import io
import tempfile
import types
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_RUN_KEY = "run-20261018-205806-16724"


def _result():
    # The minimal result shape save_preview renders (mirrors the
    # fixture in tests/test_budget_reserves.py).
    return {
        "title": "T", "author": "A", "word_count": 100,
        "reading_time_mins": 1, "povs": [], "isbn": None,
        "asin": None, "series_name": None,
        "work_resolution": "new", "work_id": None,
        "is_anthology": False, "verification": {},
        "staging": None, "suppressed_rejected": None,
        "verdicts_note": None, "tropes": [],
        "trope_confidence": {}, "triggers": [],
        "dedup_merges": [], "possible_merges": [],
        "characters": [], "minor_characters": [],
        "relationships": [], "dropped_relationships": [],
        "quotes": [], "spice_level": 0, "tasks": {},
        "avg_dialogue_ratio": None,
    }


class _PreviewNamingCase(unittest.TestCase):
    """save_preview against a temp SCRIPT_DIR, with the two
    run-identity globals under the test's control."""

    def setUp(self):
        self._cfg = dict(pp.CONFIG)
        self._script_dir = pp.SCRIPT_DIR
        self._sink = pp._RUN_SINK
        self._history = pp._HISTORY
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        pp.SCRIPT_DIR = self.td
        pp._RUN_SINK = None
        pp._HISTORY = None
        pp.CONFIG.pop("preview_name_suffix", None)

    def tearDown(self):
        pp.SCRIPT_DIR = self._script_dir
        pp._RUN_SINK = self._sink
        pp._HISTORY = self._history
        pp.CONFIG.clear()
        pp.CONFIG.update(self._cfg)
        self.tmp.cleanup()

    def _save(self):
        with contextlib.redirect_stdout(io.StringIO()):
            pp.save_preview(self.td / "Book.epub", _result())
        return sorted(p.name for p in (self.td / "preview").glob("*"))

    def _arm_sink(self, stem=_RUN_KEY):
        path = self.td / "run-events" / f"{stem}.jsonl"
        pp._RUN_SINK = types.SimpleNamespace(path=path)

    def _arm_history(self, run_key="run-20261019-101500-4242"):
        pp._HISTORY = types.SimpleNamespace(run_key=run_key)


class TestUnarmedNaming(_PreviewNamingCase):
    def test_legacy_names_when_no_run_identity(self):
        # No sink, no recorder: nothing truthful to add, so the
        # per-book name is byte-for-byte the historic one.
        self.assertEqual(self._save(), ["Book.json", "Book.md"])

    def test_suffix_alone_keeps_its_legacy_position(self):
        # The _retried flow's suffix still appends to the stem
        # exactly as before when no run identity exists.
        pp.CONFIG["preview_name_suffix"] = "_retried"
        self.assertEqual(self._save(),
                         ["Book_retried.json", "Book_retried.md"])


class TestArmedNaming(_PreviewNamingCase):
    def test_sink_stem_names_both_files(self):
        self._arm_sink()
        self.assertEqual(
            self._save(),
            [f"Book--{_RUN_KEY}.json", f"Book--{_RUN_KEY}.md"])

    def test_armed_run_does_not_touch_the_plain_book_file(self):
        # The honest consequence of run-keyed names: an armed run
        # leaves any older per-book preview alone instead of
        # overwriting it, so the two artifacts stay distinguishable.
        plain = self.td / "preview"
        plain.mkdir()
        (plain / "Book.json").write_text("older run", encoding="utf-8")
        self._arm_sink()
        self._save()
        self.assertEqual((plain / "Book.json").read_text(encoding="utf-8"),
                         "older run")

    def test_suffixed_sink_stem_is_used_verbatim(self):
        # The sink pins its name at creation and adds a numeric
        # suffix when the plain name is taken; that full stem is
        # the run's identity (the flush adopts it), so the preview
        # must carry it whole, not reconstruct the unsuffixed key.
        self._arm_sink(stem=_RUN_KEY + "-2")
        self.assertEqual(
            self._save(),
            [f"Book--{_RUN_KEY}-2.json", f"Book--{_RUN_KEY}-2.md"])

    def test_history_only_run_uses_recorder_key(self):
        self._arm_history()
        self.assertEqual(
            self._save(),
            ["Book--run-20261019-101500-4242.json",
             "Book--run-20261019-101500-4242.md"])

    def test_sink_stem_wins_over_recorder_key(self):
        # Both armed, disagreeing keys (the recorder's is stamped
        # at construction, the sink's at open — they can straddle a
        # second boundary): the flush records the run under the
        # sink's stem, so the preview must too.
        self._arm_sink()
        self._arm_history()
        names = self._save()
        self.assertEqual(
            names, [f"Book--{_RUN_KEY}.json", f"Book--{_RUN_KEY}.md"])

    def test_suffix_composes_before_the_run_key(self):
        # Rare (the _retried flow runs outside a run), but defined:
        # the configured suffix stays glued to the stem and the
        # run-key segment follows it.
        pp.CONFIG["preview_name_suffix"] = "_retried"
        self._arm_sink()
        self.assertEqual(
            self._save(),
            [f"Book_retried--{_RUN_KEY}.json",
             f"Book_retried--{_RUN_KEY}.md"])


class TestCurrentRunKey(_PreviewNamingCase):
    def test_none_when_nothing_armed(self):
        self.assertIsNone(pp._current_run_key())

    def test_history_key_when_no_sink(self):
        self._arm_history()
        self.assertEqual(pp._current_run_key(),
                         "run-20261019-101500-4242")

    def test_sink_stem_when_sink_live(self):
        self._arm_sink()
        self.assertEqual(pp._current_run_key(), _RUN_KEY)


if __name__ == "__main__":
    unittest.main()
