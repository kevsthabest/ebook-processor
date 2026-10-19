"""Candidate-aware pool sizing (Decision 2026-10-19, Option B,
sized for today's judging).

The pool used to be whatever the chapter formula armed at book
start — final the moment it was set. M2 measured the consequence:
chapters do not price candidate volume (two 73-chapter books
asking 48 vs 121 relationship questions), so formula-sized pools
starved the relationship tail (run 24: 45 claims budget_exhausted).
The ruling: the budget still OPENS at book start with the legacy
formula as a provisional cap (reserves carve within it as ever),
but at the v2 reduce — once the pair census exists and before any
relationship validation claim — the pool RESIZES to the book's
measured total demand:

    min(250, max(50, spent + ceil(1.05 * census) + trigger reserve))

grow-only: never below the provisional cap, never below what is
already spent. The census counts what today's validator actually
attempts: every pair-competition winner (evidence-backed or not),
the revival bound (at most one runner-up per pair), and the
minor-sourced judging candidates — deduped by claim identity.
At the census seam inside the reduce, the minor-sourced claims
are still inside the winners list (the principal filter splits
them out only later, in process_file_v2), so the deduped union
already holds them exactly once; the census helper takes the
groups explicitly so that union is what is pinned here.

The 1.05 factor is the ruled retry allowance: judgments retry
(a failed call claims again), so a pool sized to exactly the
census would starve the tail on retries alone.

History/result stamps: the reduce result stamps decision_pool
from _dm_budget_pool() at assembly time (after the resize), and
the run-history recorder reads the same accessor at book end —
so the reduce-level tests below assert BOTH the result stamp
and the accessor the recorder will read.

Stdlib unittest only. No network, no real models.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("pp_pool_resizing",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _finish_all_consumers():
    for _c in pp._DM_BUDGET_ORDER:
        pp._dm_budget_finish(_c)


class CensusTests(unittest.TestCase):
    """The census is a deduped union of claim identities over the
    candidate groups validation will attempt."""

    def test_winners_count_including_evidenceless(self):
        # An evidence-less family winner is attempted today (it
        # drops fail-closed inside the family stage), so sizing
        # counts it: the census is about attempts, not outcomes.
        winners = [{"from": "Alice", "to": "Bob", "type": "spouse",
                    "evidence": ""},
                   {"from": "Carol", "to": "Dan", "type": "friend",
                    "evidence": "a quote"}]
        self.assertEqual(pp._relationship_census(winners), 2)

    def test_revival_bound_adds_pairs_runner_up(self):
        winners = [{"from": "Alice", "to": "Bob", "type": "spouse"}]
        revival = [{"from": "Alice", "to": "Bob", "type": "friend"}]
        self.assertEqual(pp._relationship_census(winners, revival), 2)

    def test_identical_runner_up_is_deduped(self):
        # The same claim extracted in two chapters: one wins, the
        # other is the pair's revival candidate. One identity —
        # the census counts it once (the sizing bound is over
        # distinct candidates, and the retry allowance covers the
        # extra call a revival would spend).
        winners = [{"from": "Alice", "to": "Bob", "type": "spouse"}]
        revival = [{"from": "Alice", "to": "Bob", "type": "spouse"}]
        self.assertEqual(pp._relationship_census(winners, revival), 1)

    def test_minor_sourced_group_unions_without_double_counting(self):
        winners = [{"from": "Alice", "to": "Bob", "type": "spouse"},
                   {"from": "Minor", "to": "Bob", "type": "friend"}]
        minor_sourced = [{"from": "Minor", "to": "Bob",
                          "type": "friend"}]
        self.assertEqual(
            pp._relationship_census(winners, [], minor_sourced), 2)

    def test_empty_census_is_zero(self):
        self.assertEqual(pp._relationship_census([]), 0)
        self.assertEqual(pp._relationship_census(), 0)


class ResizeFormulaTests(unittest.TestCase):
    def tearDown(self):
        pp._dm_budget_reset()

    def test_floor_binds_on_an_empty_census(self):
        pp._dm_budget_reset(chapter_count=30)  # provisional 50
        self.assertEqual(pp._dm_budget_resize_pool(0), 50)
        self.assertEqual(pp._dm_budget_pool(), 50)

    def test_census_growth(self):
        pp._dm_budget_reset()  # provisional 50, nothing spent
        # ceil(1.05 * 60) = 63, plus the trigger reserve 15.
        self.assertEqual(pp._dm_budget_resize_pool(60), 63 + 15)
        self.assertEqual(pp._dm_budget_pool(), 78)

    def test_retry_allowance_rounds_up(self):
        pp._dm_budget_reset()
        # 1.05 * 41 = 43.05 -> 44; the allowance is a ceiling, not
        # a rounding — a fractional retry is a whole call.
        self.assertEqual(pp._dm_budget_resize_pool(41), 44 + 15)

    def test_trigger_term_is_the_machinerys_own_reserve(self):
        pp._dm_budget_reset()
        trig = pp._DM_BUDGET_RESERVES["trigger_gate"]
        # ceil(1.05 * 34) = 36; 36 alone is under the floor, so a
        # pool of 51 can only come from the reserve term landing.
        self.assertEqual(pp._dm_budget_resize_pool(34), 36 + trig)

    def test_ceiling_binds(self):
        pp._dm_budget_reset()
        self.assertEqual(pp._dm_budget_resize_pool(300),
                         pp._DECISION_POOL_CEILING)

    def test_grow_only_against_a_larger_provisional(self):
        pp._dm_budget_reset(chapter_count=115)  # provisional 115
        self.assertEqual(pp._dm_budget_resize_pool(10), 115)
        self.assertEqual(pp._dm_budget_pool(), 115)

    def test_spent_is_funded_not_starved(self):
        # Merge judging spent 20 before the reduce: the resized
        # pool covers the spend PLUS the remaining demand — the
        # resize sizes the book's total, it does not hand the
        # relationship stages a pool the merges already ate.
        pp._dm_budget_reset()
        _finish_all_consumers()
        for _ in range(20):
            self.assertTrue(pp._dm_budget_claim(1))
        self.assertEqual(pp._dm_budget_resize_pool(40), 20 + 42 + 15)
        # The remaining headroom is exactly census + reserve.
        for _ in range(42 + 15):
            self.assertTrue(pp._dm_budget_claim(1))
        self.assertFalse(pp._dm_budget_claim(1))

    def test_allowance_is_the_ruled_constant(self):
        self.assertEqual(pp._DECISION_POOL_RETRY_ALLOWANCE, 1.05)


class _StubValidator:
    model = "stub"

    def __init__(self, outer):
        self._outer = outer

    def validate_relationship(self, frm, to, rtype, quote,
                              context=""):
        self._outer.calls.append((frm, to, rtype))
        return True, 0.95


class ReduceResizeTests(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        self.calls = []
        pp._DECISION_VALIDATOR = _StubValidator(self)

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    @staticmethod
    def _reduce(claims, chapters=1):
        chs = [{"index": 1, "label": "A", "text": "x " * 200}]
        chapter_as = {1: {"characters": [],
                          "task_status": {"identity": "ok"},
                          "relationships": claims}}
        return pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)

    def test_resize_lands_before_the_first_relationship_claim(self):
        # Provisional 73 (chapter formula); 10 units already spent
        # (merges, in a real run). Census 60 -> the pool must be
        # 10 + 63 + 15 = 88 BEFORE validation draws, or the tail
        # claims would starve against the provisional remainder.
        pp._dm_budget_reset(chapter_count=73)
        _finish_all_consumers()
        for _ in range(10):
            self.assertTrue(
                pp._dm_budget_claim(1, consumer="merge_tier3"))
        claims = [{"from": f"Alice{i}", "to": f"Bob{i}",
                   "type": "spouse",
                   "evidence": "my wife Alice"} for i in range(60)]
        red = self._reduce(claims)
        self.assertEqual(len(self.calls), 60)
        # The result stamp — and the accessor the run-history
        # recorder reads at book end — carry the FINAL pool, not
        # the provisional 73 the book opened with.
        self.assertEqual(red["decision_pool"], 88)
        self.assertEqual(pp._dm_budget_pool(), 88)

    def test_census_counts_winners_plus_the_revival_bound(self):
        # 40 pairs, each with a family winner and a non-family
        # runner-up: census = 40 winners + 40 revival candidates.
        pp._dm_budget_reset(chapter_count=40)  # provisional 50
        claims = []
        for i in range(40):
            claims.append({"from": f"Alice{i}", "to": f"Bob{i}",
                           "type": "spouse",
                           "evidence": "my wife Alice"})
            claims.append({"from": f"Alice{i}", "to": f"Bob{i}",
                           "type": "friend",
                           "evidence": "her friend Alice"})
        red = self._reduce(claims)
        # ceil(1.05 * 80) = 84, plus the trigger reserve 15.
        self.assertEqual(red["decision_pool"], 84 + 15)

    def test_census_dedupes_an_identical_runner_up(self):
        # 40 pairs whose only competition is the same claim
        # extracted twice: census = 40 identities, not 80.
        pp._dm_budget_reset(chapter_count=40)
        claims = []
        for i in range(40):
            for _ in range(2):
                claims.append({"from": f"Alice{i}", "to": f"Bob{i}",
                               "type": "spouse",
                               "evidence": "my wife Alice"})
        red = self._reduce(claims)
        # ceil(1.05 * 40) = 42, plus the trigger reserve 15.
        self.assertEqual(red["decision_pool"], 42 + 15)

    def test_demand_above_the_ceiling_still_exhausts_visibly(self):
        # Sizing follows demand only up to the hard ceiling: a
        # book whose census blows past it keeps the ruled
        # behaviour — stakes-ordered judging until the pool is
        # gone, the rest budget_exhausted on the record.
        pp._dm_budget_reset()
        _finish_all_consumers()
        claims = [{"from": f"Alice{i}", "to": f"Bob{i}",
                   "type": "spouse",
                   "evidence": "my wife Alice"} for i in range(300)]
        red = self._reduce(claims)
        self.assertEqual(red["decision_pool"],
                         pp._DECISION_POOL_CEILING)
        self.assertEqual(len(self.calls), pp._DECISION_POOL_CEILING)
        exhausted = [r for r in red["relationships"]
                     if r.get("validation_note") == "budget_exhausted"]
        self.assertEqual(len(exhausted), 300 - pp._DECISION_POOL_CEILING)
        for rel in exhausted:
            self.assertEqual(rel["type"], "spouse")
            self.assertNotIn("validation_p", rel)


if __name__ == "__main__":
    unittest.main()
