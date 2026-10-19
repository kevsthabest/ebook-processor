"""Issue #4 (Medium-High, false-merge class): the generic-alias rule
in `_roster_update` overrides learned and Tier-2 matches.

Mechanism (verified against master): the function's docstring lists
the merge rules in order — 1 primary, 2 learned, 3 tiered
deterministic, 4 generic reference ("i", "the narrator", ...) — but
the generic block ran unconditionally after rules 2-3 and overwrote
their verdict. A properly named character whom some chapter tagged
with a generic alias folded into the book's narrator entry even when
a deterministic match to a different entry existed, and the fold
persisted `record_merge(nkey, 'i')` into the cross-book nickname
store, polluting later books.

The invariant this file pins: the generic rule is a FALLBACK. It may
only fire when rules 1-3 found nothing (found is None). When nothing
else matches, rule 4 still folds a generic reference into the
existing generic entry — that behaviour is by design and is pinned
here too, so the guard cannot quietly grow into a removal of rule 4.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest tests.test_issue4_generic_alias_guard -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _RosterCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_learned = pp._LEARNED
        self.addCleanup(self._restore_learned)
        self.learned = pp.LearnedState(learn_dir=Path(self._tmp.name),
                                       enabled=True)
        pp._LEARNED = self.learned
        self.roster = {}

    def _restore_learned(self):
        pp._LEARNED = self._old_learned

    def _seed_book_opening(self):
        # The issue's trigger shape: a standalone first-person narrator
        # entry and a properly named character, both from chapter 0.
        pp._roster_update(self.roster,
                          [{"name": "I", "aliases": ["the narrator"]}], 0)
        pp._roster_update(self.roster,
                          [{"name": "Jon Smith", "aliases": []}], 0)


class TestGenericRuleDoesNotTrump(_RosterCase):
    """Regression: rules 2 and 3 beat rule 4."""

    def test_tier2_title_match_beats_generic_fold(self):
        # The issue's filed reproduction, verbatim.
        self._seed_book_opening()
        pp._roster_update(self.roster,
                          [{"name": "Dr Jon Smith", "aliases": ["I"]}], 1)
        # Tier-2 title-strip says "dr jon smith" IS "jon smith"; the
        # generic alias "I" must not drag the fold to the narrator.
        self.assertEqual(self.roster["jon smith"]["appearances"], 2)
        self.assertEqual(set(self.roster["jon smith"]["primary_keys"]),
                         {"jon smith", "dr jon smith"})
        self.assertEqual(self.roster["i"]["appearances"], 1)
        self.assertEqual(set(self.roster["i"]["primary_keys"]), {"i"})
        # And the persisted cross-book mapping must point at the real
        # entry, never at the narrator key.
        self.assertEqual(self.learned.nick_lookup("dr jon smith"),
                         "jon smith")

    def test_learned_match_beats_generic_fold(self):
        # Same trump, one rule earlier: a learned cross-book mapping
        # resolves the incoming name, and rule 4 must not replace it.
        self._seed_book_opening()
        self.learned.record_merge("dr jon smith", "jon smith")
        pp._roster_update(self.roster,
                          [{"name": "Dr Jon Smith", "aliases": ["I"]}], 1)
        self.assertEqual(self.roster["jon smith"]["appearances"], 2)
        self.assertEqual(set(self.roster["jon smith"]["primary_keys"]),
                         {"jon smith", "dr jon smith"})
        self.assertEqual(self.roster["i"]["appearances"], 1)
        self.assertEqual(set(self.roster["i"]["primary_keys"]), {"i"})


class TestGenericFallbackPreserved(_RosterCase):
    """Rule 4 still fires when rules 1-3 found nothing."""

    def test_generic_named_incoming_folds_into_narrator(self):
        self._seed_book_opening()
        pp._roster_update(self.roster,
                          [{"name": "The Narrator", "aliases": []}], 1)
        # norm_name strips the article: the incoming key is "narrator".
        self.assertNotIn("narrator", self.roster)
        self.assertEqual(self.roster["i"]["appearances"], 2)
        self.assertIn("narrator",
                      self.roster["i"]["primary_keys"])
        self.assertEqual(self.roster["jon smith"]["appearances"], 1)

    def test_unmatched_named_character_with_generic_alias_folds(self):
        # The adjacent case: a proper-named character carrying a
        # generic alias, with NO learned or tiered match anywhere.
        # Rule 4 as documented sends the fold to the narrator entry.
        self._seed_book_opening()
        pp._roster_update(self.roster,
                          [{"name": "Unnamed Sailor", "aliases": ["I"]}], 1)
        self.assertNotIn("unnamed sailor", self.roster)
        self.assertEqual(self.roster["i"]["appearances"], 2)
        self.assertIn("unnamed sailor",
                      self.roster["i"]["primary_keys"])
        self.assertEqual(self.roster["jon smith"]["appearances"], 1)


if __name__ == "__main__":
    unittest.main()
