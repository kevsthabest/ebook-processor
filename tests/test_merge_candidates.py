"""Roster merge candidate generation (run-23 failure, v2.13.23).

The failure: "General Sorrengail" and "Lilith Sorrengail" sat
split and the reconcile proposed ZERO merges. Diagnosis, pinned
by the fixtures below: every pre-existing candidate source is
claim-driven — _roster_reconcile merges only on mutual alias
claims or learned links, and post_pass_merge's Tier 3 list comes
from _merge_pair_signal over pairs the tiers called ambiguous.
A title-form/full-name pair with no alias claim between them
generates no candidate anywhere; it is invisible, not rejected.

_merge_name_candidates reads the names themselves (shared
surname, title form, containment) and feeds the EXISTING tiers:
deterministic tiers and judge tier behave exactly as before,
candidates are proposals only, and Tier 1 vetoes (cannot-link,
surname trap) still keep a pair from the judge entirely — an
always-yes judge must not be able to force-merge siblings.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _fold(roster, name, chapter_idx, aliases=None):
    pp._roster_update(
        roster,
        [{"name": name, "aliases": aliases or [],
          "role": "supporting", "description": "", "evidence": ""}],
        chapter_idx)


def _fw_roster():
    """Fourth-Wing-shaped: the general sighted early under her
    title form, her full name revealed much later, family around
    both. Chapter sets for the split pair are disjoint — the
    shape where a candidate CAN legitimately reach the judge."""
    roster = {}
    _fold(roster, "General Sorrengail", 1)
    _fold(roster, "Violet Sorrengail", 2)
    _fold(roster, "Mira Sorrengail", 3)
    _fold(roster, "Brennan Sorrengail", 4)
    _fold(roster, "Lilith Sorrengail", 9)
    _fold(roster, "Xaden Riorson", 5)
    return roster


class RecordingJudge:
    """Tier 3 stub: records the pairs it is asked about."""

    model = "stub"

    def __init__(self, verdict=True, p=0.9):
        self.verdict, self.p = verdict, p
        self.calls = []

    def ask_same_person(self, na, da, ea, nb, db, eb):
        self.calls.append((na, nb))
        return self.verdict, self.p

    def asked_about(self, a, b):
        return any({x, y} == {a, b} for x, y in self.calls)


class TestNameCandidates(unittest.TestCase):
    def test_title_form_pair_is_generated(self):
        cands = {(a, b): s for a, b, s in
                 pp._merge_name_candidates(_fw_roster())}
        key = tuple(sorted(("general sorrengail",
                            "lilith sorrengail")))
        self.assertIn(key, cands)
        self.assertIn("title_form", cands[key])
        self.assertIn("shared_surname", cands[key])

    def test_shared_surname_siblings_are_generated(self):
        cands = {(a, b): s for a, b, s in
                 pp._merge_name_candidates(_fw_roster())}
        key = tuple(sorted(("mira sorrengail",
                            "violet sorrengail")))
        self.assertIn(key, cands)
        self.assertEqual(cands[key], ("shared_surname",))

    def test_containment_pair_is_generated(self):
        roster = {}
        _fold(roster, "Sorrengail", 1)
        _fold(roster, "Lilith Sorrengail", 9)
        cands = {(a, b): s for a, b, s in
                 pp._merge_name_candidates(roster)}
        key = tuple(sorted(("sorrengail", "lilith sorrengail")))
        self.assertIn(key, cands)
        self.assertIn("containment", cands[key])

    def test_unrelated_names_generate_nothing(self):
        roster = _fw_roster()
        pairs = {(a, b) for a, b, _ in
                 pp._merge_name_candidates(roster)}
        self.assertNotIn(tuple(sorted(("violet sorrengail",
                                       "xaden riorson"))), pairs)

    def test_doctor_title_form(self):
        roster = {}
        _fold(roster, "Dr. Reyes", 1)
        _fold(roster, "Tomas Reyes", 5)
        cands = {(a, b): s for a, b, s in
                 pp._merge_name_candidates(roster)}
        key = tuple(sorted(("dr. reyes", "tomas reyes")))
        self.assertIn(key, cands)
        self.assertIn("title_form", cands[key])


class TestFeedingTheTiers(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()

    def tearDown(self):
        pp._dm_budget_reset()

    def test_fw_pair_reaches_the_judge_tier_and_merges_on_yes(self):
        roster = _fw_roster()
        judge = RecordingJudge(verdict=True, p=0.9)
        n_merged, _ = pp.post_pass_merge(roster, validator=judge)
        self.assertTrue(judge.asked_about("General Sorrengail",
                                          "Lilith Sorrengail"))
        # Judged the same person: the split is healed by the
        # existing Tier 3 machinery, not by the generator.
        names = {e["name"] for e in roster.values()}
        self.assertFalse({"General Sorrengail", "Lilith Sorrengail"}
                         <= names)
        self.assertGreaterEqual(n_merged, 1)

    def test_siblings_are_never_judged_and_never_merged(self):
        # Adversarial: an always-yes judge must not get the
        # chance to force-merge two distinct Sorrengails — the
        # surname trap keeps the pair out of Tier 3 entirely.
        roster = {}
        _fold(roster, "Mira Sorrengail", 1)
        _fold(roster, "Violet Sorrengail", 2)
        judge = RecordingJudge(verdict=True, p=0.99)
        n_merged, possible = pp.post_pass_merge(roster,
                                                validator=judge)
        self.assertEqual(n_merged, 0)
        self.assertEqual(len(roster), 2)
        self.assertFalse(judge.asked_about("Mira Sorrengail",
                                           "Violet Sorrengail"))
        # And no review spam: a plain shared-surname block is the
        # trap working as designed, not a proposal.
        self.assertEqual(possible, [])

    def test_unrelated_same_surname_is_never_judged(self):
        roster = {}
        _fold(roster, "John Smith", 1)
        _fold(roster, "Anna Smith", 2)
        judge = RecordingJudge(verdict=True, p=0.99)
        n_merged, _ = pp.post_pass_merge(roster, validator=judge)
        self.assertEqual(n_merged, 0)
        self.assertEqual(len(roster), 2)
        self.assertFalse(judge.asked_about("John Smith",
                                           "Anna Smith"))

    def test_title_form_of_a_different_person_judge_decides(self):
        # Captain Reyes is NOT Tomas Reyes (his commander, a
        # different person): the pair legitimately reaches the
        # judge, the judge says no, and they stay separate with
        # the pair listed for review — proposed, never forced.
        roster = {}
        _fold(roster, "Captain Reyes", 1)
        _fold(roster, "Tomas Reyes", 5)
        judge = RecordingJudge(verdict=False, p=0.2)
        n_merged, possible = pp.post_pass_merge(roster,
                                                validator=judge)
        self.assertEqual(n_merged, 0)
        self.assertEqual(len(roster), 2)
        self.assertTrue(judge.asked_about("Captain Reyes",
                                          "Tomas Reyes"))
        self.assertTrue(any(
            {m["name_a"], m["name_b"]}
            == {"Captain Reyes", "Tomas Reyes"} for m in possible))

    def test_colisted_title_form_is_blocked_not_judged(self):
        # Adversarial: co-listed in one chapter (Tier 1
        # cannot-link). Even an always-yes judge is never asked,
        # nothing merges — but the pair is no longer silent: it
        # surfaces as a blocked proposal for manual review.
        roster = {}
        _fold(roster, "Captain Reyes", 1)
        _fold(roster, "Tomas Reyes", 1)
        judge = RecordingJudge(verdict=True, p=0.99)
        n_merged, possible = pp.post_pass_merge(roster,
                                                validator=judge)
        self.assertEqual(n_merged, 0)
        self.assertEqual(len(roster), 2)
        self.assertFalse(judge.asked_about("Captain Reyes",
                                           "Tomas Reyes"))
        blocked = [m for m in possible
                   if {m["name_a"], m["name_b"]}
                   == {"Captain Reyes", "Tomas Reyes"}]
        self.assertEqual(len(blocked), 1)
        self.assertIn("blocked by Tier 1", blocked[0]["reason"])
        self.assertIsNone(blocked[0]["p_yes"])


if __name__ == "__main__":
    unittest.main()
