"""Fix-queue batch 3 (2026-10-15) — final batch of the deep-review
fix queue.

Item 1 (learned-NO replay order; Security Advisor deep-review
finding 2, fixable half): on the streaming roster path, a learned
merge hit was applied without ever consulting the current book's
Tier 1 co-chapter cannot-link — the tiered check ran only when the
learned lookup MISSED (`if found is None:`), so a learned YES could
silently override a cannot-link the same run's extraction had just
established. The reconcile-side learned pass already refuses
co-listed pairs; the streaming path now checks the same way before
applying the hit. (No negative learned store is built here — that
is decision D1, explicitly out of scope.)

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _char(name, role="supporting"):
    return {"name": name, "aliases": [], "role": role}


class _LearnedRosterCase(unittest.TestCase):
    """A roster fed chapter by chapter with a live LearnedState whose
    nickname map carries one confirmed mapping:
    "seb peters" -> "peter sebeck" (learned from prior books, where
    the fold was correct). In THIS book the two names are two people.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_learned = pp._LEARNED
        self.addCleanup(self._restore_learned)
        self.learned = pp.LearnedState(
            learn_dir=Path(self._tmp.name), enabled=True)
        pp._LEARNED = self.learned
        # Two independent sightings confirm the mapping (the learned
        # store's own confirmation bar).
        self.learned.record_merge("seb peters", "peter sebeck",
                                  "isbn-prior-1")
        self.learned.record_merge("seb peters", "peter sebeck",
                                  "isbn-prior-2")
        self.assertEqual(self.learned.nick_lookup("seb peters"),
                         "peter sebeck")
        self.viz = []
        self._viz_patch = mock.patch.object(
            pp.UI, "viz_event",
            lambda key, msg: self.viz.append(msg))
        self._viz_patch.start()
        self.addCleanup(self._viz_patch.stop)

    def _restore_learned(self):
        pp._LEARNED = self._old_learned


class TestLearnedHitCannotLinkVeto(_LearnedRosterCase):

    def test_colisted_learned_hit_is_refused(self):
        roster = {}
        # Chapter 0: Peter Sebeck appears alone -> roster entry.
        pp._roster_update(roster, [_char("Peter Sebeck")], 0)
        self.assertIn("peter sebeck", roster)
        # Chapter 1: the chapter lists BOTH names as separate
        # characters — a Tier 1 co-chapter cannot-link. The learned
        # hit for "Seb Peters" must be refused, not applied.
        pp._roster_update(
            roster, [_char("Peter Sebeck"), _char("Seb Peters")], 1)
        self.assertIn("seb peters", roster,
                      "co-listed learned variant must stay its own "
                      "roster entry (Tier 1 cannot-link)")
        peter = roster["peter sebeck"]
        self.assertNotIn("seb peters", peter["primary_keys"])
        self.assertNotIn("seb peters", peter["alias_keys"])
        # Peter's own two listings only — the refused variant added
        # no appearance to his entry.
        self.assertEqual(peter["appearances"], 2)
        self.assertEqual(roster["seb peters"]["appearances"], 1)
        # The refusal is surfaced on the roster event feed, the
        # streaming path's veto channel.
        self.assertTrue(
            any("Seb Peters" in m and "cannot-link" in m
                for m in self.viz),
            f"veto not surfaced in viz events: {self.viz}")

    def test_learned_hit_without_colisting_still_merges(self):
        # Control: same learned mapping, but the variant appears in
        # a chapter that does NOT co-list the canonical name — the
        # learned merge applies exactly as before.
        roster = {}
        pp._roster_update(roster, [_char("Peter Sebeck")], 0)
        pp._roster_update(roster, [_char("Seb Peters")], 1)
        self.assertNotIn("seb peters", roster)
        peter = roster["peter sebeck"]
        self.assertIn("seb peters", peter["primary_keys"])
        self.assertEqual(peter["appearances"], 2)


if __name__ == "__main__":
    unittest.main()
