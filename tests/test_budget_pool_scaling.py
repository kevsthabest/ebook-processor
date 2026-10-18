"""Scaled decision pool (v2.13.33, Decision 2026-10-18).

The per-book decision-model pool used to be a flat 50 calls
regardless of book size. Fourth Wing run 24 (115 chapters, 70
characters) had roughly 130 draws of real demand, got 49, and
shipped 56 of 68 relationship claims unjudged (audit F-24-2 /
F-24-3). The ruling: the pool scales with the book — floor 50,
one per chapter, ceiling 250. The §1.3 hard reserves stay
ABSOLUTE floors carved from whatever pool the book gets; the
scaled increment lands in the shared remainder and flows
through the existing borrowing rules unchanged. There is no
operator config key for the pool (none existed before this
release), so the formula is the only source — no override test
applies.

Stdlib unittest only. No network, no real models — budget
functions are pure counter logic, and the validation-seam test
drives the real v2_reduce loop with a stub validator.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("pp_pool_scaling",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _finish_all_consumers():
    for _c in pp._DM_BUDGET_ORDER:
        pp._dm_budget_finish(_c)


class PoolFormulaTests(unittest.TestCase):
    def test_formula_floor_linear_and_ceiling(self):
        # (chapters, pool): below the floor the floor governs,
        # between floor and ceiling the pool is one per chapter,
        # above the ceiling the ceiling governs.
        cases = [(30, 50), (73, 73), (115, 115), (400, 250)]
        for chapters, expected in cases:
            with self.subTest(chapters=chapters):
                self.assertEqual(
                    pp._decision_pool_for_chapters(chapters), expected)

    def test_formula_unknown_count_is_the_floor(self):
        # A consumer that draws before any chapter count exists
        # (a direct reduce / merge call outside process_file_v2's
        # arming) is governed by the floor, never by a guess.
        self.assertEqual(pp._decision_pool_for_chapters(None), 50)
        self.assertEqual(pp._decision_pool_for_chapters(0), 50)

    def tearDown(self):
        pp._dm_budget_reset()


class ArmedPoolTests(unittest.TestCase):
    def tearDown(self):
        pp._dm_budget_reset()

    def test_reset_without_a_count_arms_the_floor(self):
        pp._dm_budget_reset()
        self.assertEqual(pp._dm_budget_pool(), 50)
        _finish_all_consumers()
        for _ in range(50):
            self.assertTrue(pp._dm_budget_claim(1))
        self.assertFalse(pp._dm_budget_claim(1))

    def test_armed_pool_governs_the_ceiling(self):
        pp._dm_budget_reset(chapter_count=115)
        self.assertEqual(pp._dm_budget_pool(), 115)
        _finish_all_consumers()
        for _ in range(115):
            self.assertTrue(pp._dm_budget_claim(1))
        self.assertFalse(pp._dm_budget_claim(1))
        self.assertEqual(pp._DM_BUDGET_USED, 115)

    def test_per_consumer_accounting_works_at_a_scaled_pool(self):
        # The 2.13.18 tag accounting must keep working at pool
        # 250: granted / denied / spent stay per-consumer exact.
        pp._dm_budget_reset(chapter_count=400)
        self.assertEqual(pp._dm_budget_pool(), 250)
        _finish_all_consumers()
        for _ in range(120):
            self.assertTrue(
                pp._dm_budget_claim(1, consumer="rel_family"))
        for _ in range(130):
            self.assertTrue(
                pp._dm_budget_claim(1, consumer="rel_nonfamily"))
        self.assertFalse(pp._dm_budget_claim(1, consumer="trigger_gate"))
        snap = pp._dm_budget_snapshot()
        self.assertEqual(snap["rel_family"]["spent"], 120)
        self.assertEqual(snap["rel_nonfamily"]["spent"], 130)
        self.assertEqual(snap["trigger_gate"]["denied"], 1)

    def test_rearming_replaces_the_previous_books_pool(self):
        pp._dm_budget_reset(chapter_count=115)
        pp._dm_budget_reset(chapter_count=30)
        self.assertEqual(pp._dm_budget_pool(), 50)


class ValidationSeamScaledPoolTests(unittest.TestCase):
    """Pair-winner demand beyond 50 actually gets judged when
    the book's pool scales past 50 (fake judge counts its calls).
    """

    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        self.calls = []

        outer = self

        class StubValidator:
            model = "stub"

            def validate_relationship(self, frm, to, rtype, quote,
                                      context=""):
                outer.calls.append((frm, to, rtype))
                return True, 0.95

        pp._DECISION_VALIDATOR = StubValidator()

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_sixty_claims_are_judged_at_a_73_chapter_pool(self):
        # Reserves are released up front (the test_budget.py
        # convention) so this test pins the CEILING at the seam,
        # not the reserve discipline — that is pinned separately
        # below and in test_budget_reserves.py.
        pp._dm_budget_reset(chapter_count=73)
        _finish_all_consumers()
        claims = [{"from": f"Alice{i}", "to": f"Bob{i}",
                   "type": "spouse",
                   "evidence": "my wife Alice"} for i in range(60)]
        chs = [{"index": 1, "label": "A", "text": "x " * 200}]
        chapter_as = {1: {"characters": [],
                          "task_status": {"identity": "ok"},
                          "relationships": claims}}
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        # Demand past the old flat 50 was really served: the
        # judge saw every claim, and none shipped unjudged.
        self.assertEqual(len(self.calls), 60)
        self.assertEqual(len(red["relationships"]), 60)
        for rel in red["relationships"]:
            self.assertEqual(rel["validation_note"], "kept")


class ScaledReserveTests(unittest.TestCase):
    def tearDown(self):
        pp._dm_budget_reset()

    def test_trigger_reserve_honoured_at_a_scaled_pool(self):
        # Relationship demand is huge: family claims spend
        # everything the scaled pool lets them touch (general
        # remainder plus their own reserve, at pool 115). The
        # trigger reserve is an absolute floor, so after the
        # earlier stages finish, the trigger gate still draws
        # its full 15.
        pp._dm_budget_reset(chapter_count=115)
        pp._dm_budget_finish("merge_tier3")
        granted = 0
        while pp._dm_budget_claim(1, consumer="rel_family"):
            granted += 1
        self.assertGreater(granted, 50)  # the increment is real
        pp._dm_budget_finish("rel_family")
        pp._dm_budget_finish("rel_nonfamily")
        for _ in range(pp._DM_BUDGET_RESERVES["trigger_gate"]):
            self.assertTrue(
                pp._dm_budget_claim(1, consumer="trigger_gate"))

    def test_reserves_are_absolute_not_proportional(self):
        # The reserve slices do not grow with the pool: at the
        # ceiling they are still the §1.3 constants.
        pp._dm_budget_reset(chapter_count=400)
        self.assertEqual(pp._DM_BUDGET_RESERVES, {
            "merge_tier3": 10, "rel_family": 10, "rel_nonfamily": 5,
            "trigger_gate": 15, "quote_poll": 1, "discovery": 0,
            "untagged": 0})


class PoolStampTests(unittest.TestCase):
    def tearDown(self):
        pp._dm_budget_reset()

    def test_scheme_stamps_the_computed_pool(self):
        pp._dm_budget_reset(chapter_count=115)
        scheme = pp._dm_budget_scheme()
        self.assertEqual(scheme["pool"], 115)
        self.assertEqual(scheme["cap"], 115)
        self.assertEqual(scheme["chapter_count"], 115)
        self.assertEqual(scheme["reserves"], pp._DM_BUDGET_RESERVES)

    def test_history_thresholds_carry_no_pool(self):
        # v2.13.35 (Advisor R2 on the 2.13.33 stamp below): the
        # thresholds dict is the config fingerprint's input, and
        # the armed pool is per-book runtime state, not config —
        # it no longer rides this dict at all. The run-history
        # book row carries its own stamp since the Option A
        # column landed (v2.13.36); the artefact stamp below is
        # unchanged and stays the per-book record of record.
        pp._dm_budget_reset(chapter_count=115)
        thresholds = pp._history_thresholds()
        self.assertNotIn("decision_pool", thresholds)
        # The old constant survives as the floor it now is.
        self.assertEqual(thresholds["merge_tier3_cap"],
                         pp._MERGE_TIER3_CAP)
        self.assertEqual(thresholds["budget_reserves"],
                         pp._DM_BUDGET_RESERVES)

    def test_reduce_artefact_stamps_the_computed_pool(self):
        pp._dm_budget_reset(chapter_count=115)
        chs = [{"index": 1, "label": "A", "text": "x " * 200}]
        chapter_as = {1: {"characters": [],
                          "task_status": {"identity": "ok"},
                          "relationships": []}}
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(red["decision_pool"], 115)


if __name__ == "__main__":
    unittest.main()
