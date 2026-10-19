"""Issue #10 (false-merge class): the spelling-variant exemption
merged distinct standard name pairs (Sara/Sarah) past the surname
trap.

Mechanism (verified against master): `_is_spelling_variant` is a pure
shape test (shared 3+ prefix, edit distance <= 2) and cannot tell a
distinct standard pair (Sara/Sarah) from one person spelled two ways
(Joseph/Josef) — they are the same shape. Two deterministic steps
rode on it: the Tier-2 spelling YES in `_merge_tier`, evaluated
before the Tier-1 cannot-link and the surname trap, and the exemption
inside `_surname_trap_blocked`, which let a variant pair escape the
trap as well.

Decision (Shape 3, 2026-10-19): given-name spelling-variant pairs are
demoted to "ambiguous". No deterministic merge, no trap exemption
carrying them, and no hard block either — the Tier 3 judge decides on
evidence. Surname-position variants keep their deterministic YES,
nickname pairs keep theirs, and genuinely incompatible given names
(the Sebeck control) stay hard-blocked by the trap.

The invariants this file pins:
  * _merge_tier returns "ambiguous" for a given-name variant pair —
    co-listed or not (the demotion outranks the Tier-1 vote too,
    because the judge, not the tiers, owns this shape now).
  * _surname_trap_blocked flags the pair (the exemption is gone),
    yet the tier verdict still ends "ambiguous", never "no": the
    trap flags the shape, it does not decide it.
  * The demoted pair genuinely reaches post_pass_merge's Tier 3:
    the judge is asked, a judged NO lands in possible_merges, a
    judged YES merges, and with the model off the pair surfaces as
    an unresolved possible-merge instead of vanishing or merging.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest tests.test_issue10_spelling_variant -v
"""
import importlib.util
import tempfile
import unittest
from collections import Counter
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class TestGivenNameVariantDemoted(unittest.TestCase):
    """Regression: the filed Sara/Sarah construction must not merge."""

    def test_co_listed_pair_is_ambiguous_not_yes(self):
        # The issue's exact construction: both names listed as
        # separate characters in the same chapter, and Tier 2 still
        # hard-merged them before the cannot-link got a vote.
        self.assertEqual(
            pp._merge_tier("sara smith", "sarah smith",
                           chapter_keys={"sara smith", "sarah smith"}),
            "ambiguous")

    def test_plain_pair_is_ambiguous_not_yes(self):
        self.assertEqual(pp._merge_tier("sara smith", "sarah smith"),
                         "ambiguous")

    def test_joseph_josef_reversed_to_ambiguous(self):
        # The deliberate reversal: Joseph/Josef is the same formal
        # shape as Sara/Sarah, so the demotion covers it too. Its
        # deterministic YES was pinned behaviour until the Shape 3
        # decision; the judge owns it now.
        self.assertEqual(pp._merge_tier("joseph pavlos", "josef pavlos"),
                         "ambiguous")

    def test_trap_flags_variant_pair(self):
        # The exemption is gone: the trap reports the pair as a
        # same-surname / different-given-name shape...
        blocked, reason = pp._surname_trap_blocked("sara smith",
                                                   "sarah smith")
        self.assertTrue(blocked)
        self.assertIn("surname trap", reason)

    def test_trap_path_end_state_is_ambiguous_not_no(self):
        # ...but flagging is not deciding. An unequal-length variant
        # pair (which reaches the trap instead of the Tier-2 spelling
        # branch) must still end judge-bound: never "no".
        self.assertEqual(pp._merge_tier("sara smith", "sarah ann smith"),
                         "ambiguous")
        self.assertEqual(pp._merge_tier("sarah smith", "sara ann smith"),
                         "ambiguous")


class TestRosterKeepsPairSeparate(unittest.TestCase):
    """The roster path itself: the pair arrives as two entries."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_learned = pp._LEARNED
        self.addCleanup(self._restore_learned)
        pp._LEARNED = pp.LearnedState(learn_dir=Path(self._tmp.name),
                                      enabled=True)
        self.roster = {}

    def _restore_learned(self):
        pp._LEARNED = self._old_learned

    def test_roster_update_keeps_sara_and_sarah_separate(self):
        pp._roster_update(self.roster,
                          [{"name": "Sara Smith", "aliases": []}], 0)
        pp._roster_update(self.roster,
                          [{"name": "Sarah Smith", "aliases": []}], 1)
        self.assertIn("sara smith", self.roster)
        self.assertIn("sarah smith", self.roster)
        self.assertEqual(self.roster["sara smith"]["appearances"], 1)
        self.assertEqual(self.roster["sarah smith"]["appearances"], 1)


def _entry(name, chapters):
    key = pp.norm_name(name)
    return {"name": name, "aliases": set(), "alias_keys": {key},
            "primary_keys": {key}, "_generic_keys": set(),
            "appearances": 1, "last_seen": max(chapters),
            "chapters": set(chapters), "roles": Counter(),
            "descriptions": []}


class _RecordingJudge:
    """Tier 3 stand-in: records what it was asked, returns a verdict."""

    disabled = False

    def __init__(self, verdict, p=0.5):
        self._verdict = verdict
        self._p = p
        self.asked = []

    def ask_same_person(self, na, _da, _ea, nb, _db, _eb):
        self.asked.append(tuple(sorted((na, nb))))
        return self._verdict, self._p


class TestPairReachesTheJudge(unittest.TestCase):
    """Step-4 chain: 'ambiguous' must deliver the pair to Tier 3."""

    def setUp(self):
        pp._dm_budget_reset()
        self.addCleanup(pp._dm_budget_reset)
        self.roster = {"sara smith": _entry("Sara Smith", {0}),
                       "sarah smith": _entry("Sarah Smith", {1})}

    def test_judge_is_asked_and_no_lands_in_possible_merges(self):
        judge = _RecordingJudge(verdict=False, p=0.2)
        n_merged, possible = pp.post_pass_merge(self.roster,
                                                validator=judge)
        self.assertIn(("Sara Smith", "Sarah Smith"), judge.asked)
        self.assertEqual(n_merged, 0)
        pairs = {tuple(sorted((m["name_a"], m["name_b"])))
                 for m in possible}
        self.assertIn(("Sara Smith", "Sarah Smith"), pairs)

    def test_judge_yes_merges_on_evidence(self):
        judge = _RecordingJudge(verdict=True, p=0.9)
        n_merged, _ = pp.post_pass_merge(self.roster, validator=judge)
        self.assertIn(("Sara Smith", "Sarah Smith"), judge.asked)
        self.assertEqual(n_merged, 1)
        self.assertEqual(len(self.roster), 1)

    def test_model_off_surfaces_unresolved_possible_merge(self):
        n_merged, possible = pp.post_pass_merge(self.roster,
                                                validator=None)
        self.assertEqual(n_merged, 0)
        pairs = {tuple(sorted((m["name_a"], m["name_b"])))
                 for m in possible}
        self.assertIn(("Sara Smith", "Sarah Smith"), pairs)


class TestPreservedBehaviour(unittest.TestCase):
    """Everything the decision said must NOT change."""

    def test_spelling_helper_stays_a_shape_test(self):
        self.assertTrue(pp._is_spelling_variant("joseph", "josef"))
        self.assertTrue(pp._is_spelling_variant("sara", "sarah"))
        self.assertTrue(pp._is_spelling_variant("mosely", "mosley"))
        self.assertFalse(pp._is_spelling_variant("chris", "peter"))

    def test_surname_position_variant_still_deterministic_yes(self):
        self.assertEqual(pp._merge_tier("john mosely", "john mosley"),
                         "yes")

    def test_nickname_pair_still_deterministic_yes(self):
        # Pete/Peter is ALSO spelling-shaped; the nickname class is
        # a separate, curated signal and keeps its deterministic YES.
        self.assertEqual(pp._merge_tier("pete sebeck", "peter sebeck"),
                         "yes")
        blocked, _ = pp._surname_trap_blocked("pete sebeck",
                                              "peter sebeck")
        self.assertFalse(blocked)

    def test_sebeck_control_still_hard_no(self):
        blocked, reason = pp._surname_trap_blocked("chris sebeck",
                                                   "peter sebeck")
        self.assertTrue(blocked)
        self.assertIn("surname trap", reason)
        self.assertEqual(pp._merge_tier("chris sebeck", "peter sebeck"),
                         "no")

    def test_title_strip_still_deterministic_yes(self):
        self.assertEqual(
            pp._merge_tier("special agent neal decker", "neal decker"),
            "yes")


if __name__ == "__main__":
    unittest.main()
