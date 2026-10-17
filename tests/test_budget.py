"""Tests for Tier-3 decision-model budget exhaustion.

The 50-call per-book budget is shared between merge decisions and
relationship validation. Merges claim first; validation gets leftovers.
These tests pin the priority ordering and the exhaustion behavior.

Stdlib unittest only. No network, no real models — budget functions
are pure counter logic.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _finish_all_consumers():
    # Amended 2026-10-17 (2.13.20): hard reserves (spec §1.3) mean a
    # claim on a fresh budget can only spend the general pool plus
    # its own reserve until the other consumers finish. The classes
    # below pin the per-book CEILING, so their setup declares every
    # pipeline stage finished — reserves released — and their
    # assertions are unchanged. Reserve discipline itself is
    # pinned in test_budget_consumers.py / test_budget_reserves.py.
    for _c in pp._DM_BUDGET_ORDER:
        pp._dm_budget_finish(_c)


class TestBudgetBasics(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()
        _finish_all_consumers()

    def test_cap_is_50(self):
        self.assertEqual(pp._MERGE_TIER3_CAP, 50)

    def test_claim_within_budget(self):
        for _ in range(50):
            self.assertTrue(pp._dm_budget_claim(1))

    def test_claim_beyond_budget_fails(self):
        for _ in range(50):
            pp._dm_budget_claim(1)
        self.assertFalse(pp._dm_budget_claim(1))

    def test_reset_restores_budget(self):
        for _ in range(50):
            pp._dm_budget_claim(1)
        self.assertFalse(pp._dm_budget_claim(1))
        pp._dm_budget_reset()
        self.assertTrue(pp._dm_budget_claim(1))

    def test_multi_claim(self):
        self.assertTrue(pp._dm_budget_claim(10))
        self.assertTrue(pp._dm_budget_claim(40))
        self.assertFalse(pp._dm_budget_claim(1))

    def test_multi_claim_atomic(self):
        # Claiming 51 at once fails entirely (doesn't partially consume)
        self.assertFalse(pp._dm_budget_claim(51))
        # Budget untouched — all 50 still available
        self.assertTrue(pp._dm_budget_claim(50))


class TestBudgetPriority(unittest.TestCase):
    """Merges claim first; validation starves when merges exhaust the pool."""

    def setUp(self):
        pp._dm_budget_reset()
        _finish_all_consumers()  # see _finish_all_consumers (2.13.20)

    def test_merges_exhaust_starves_validation(self):
        # Simulate 50 merge decisions consuming the entire budget
        merges_done = sum(1 for _ in range(60) if pp._dm_budget_claim(1))
        self.assertEqual(merges_done, 50)
        # Validation now gets nothing
        self.assertFalse(pp._dm_budget_claim(1))
        self.assertFalse(pp._dm_budget_claim(1))

    def test_partial_merges_leave_room_for_validation(self):
        # 30 merges → 20 left for validation
        for _ in range(30):
            self.assertTrue(pp._dm_budget_claim(1))
        validations_done = sum(1 for _ in range(25) if pp._dm_budget_claim(1))
        self.assertEqual(validations_done, 20)

    def test_no_merges_full_validation_budget(self):
        # Zero merges → all 50 available for validation
        validations_done = sum(1 for _ in range(60) if pp._dm_budget_claim(1))
        self.assertEqual(validations_done, 50)

    def test_interleaved_ordering(self):
        # Even interleaved, total is capped at 50 regardless of order
        total = 0
        for i in range(100):
            if pp._dm_budget_claim(1):
                total += 1
        self.assertEqual(total, 50)


class TestBudgetExhaustionNote(unittest.TestCase):
    """The 'budget_exhausted' validation note is set when starved.

    Behavioural (QA-scout remediation, 2026-10-14): drives the real
    v2_reduce family-validation loop with a stub validator. Replaces the
    old tautological test, which asserted
    "budget_exhausted" == "budget_exhausted" plus a source-string
    search; the source-presence check was dropped — once the behaviour
    is pinned end-to-end it adds nothing.
    """

    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        _finish_all_consumers()  # see _finish_all_consumers (2.13.20)
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

    @staticmethod
    def _reduce_spouse():
        chs = [{"index": 1, "label": "A", "text": "x " * 200},
               {"index": 2, "label": "B", "text": "x " * 200}]
        chapter_as = {1: {"characters": [],
                          "task_status": {"identity": "ok"},
                          "relationships": [{
                              "from": "Alice", "to": "Bob",
                              "type": "spouse",
                              "evidence": "my wife Alice"}]}}
        return pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)

    def test_starved_validation_notes_budget_exhausted(self):
        # Exhaust the shared per-book budget (merges claim first in a
        # real run; here the pool is simply drained before reduce).
        for _ in range(pp._MERGE_TIER3_CAP):
            self.assertTrue(pp._dm_budget_claim(1))
        self.assertFalse(pp._dm_budget_claim(1))
        red = self._reduce_spouse()
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        # The note lands, the ORIGINAL type survives unvalidated, and
        # the validator was never consulted.
        self.assertEqual(rel["validation_note"], "budget_exhausted")
        self.assertEqual(rel["type"], "spouse")
        self.assertNotIn("validation_p", rel)
        self.assertEqual(self.calls, [])

    def test_available_budget_validates_normally(self):
        # Contrast: with budget available the same input validates —
        # the note above is specifically about starvation.
        red = self._reduce_spouse()
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(self.calls, [("Alice", "Bob", "spouse")])
        self.assertEqual(rel["validation_p"], 0.95)
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["type"], "spouse")


if __name__ == "__main__":
    unittest.main()
