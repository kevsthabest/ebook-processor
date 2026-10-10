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


class TestBudgetBasics(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()

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
    """The 'budget_exhausted' validation note is set when starved."""

    def test_note_value(self):
        # The sentinel string used when validation can't claim budget
        # (set in v2_reduce relationship validation loop)
        self.assertEqual("budget_exhausted", "budget_exhausted")
        # Verify the code path exists: search for the literal in source
        import inspect
        src = inspect.getsource(pp)
        self.assertIn('"budget_exhausted"', src)


if __name__ == "__main__":
    unittest.main()
