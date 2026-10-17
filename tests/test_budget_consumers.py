"""Consumer accounting for the shared decision-model budget, the
trigger-gate floor, and principal-relationship judged coverage
(docs/discovery-and-budget-spec.md §1.1, §1.3, §1.5).

Until now the 50-call pool recorded only a single total: run 17
exhausted it inside relationship validation and every trigger
judgment fell back to regex, and nothing in the run record could
say who had spent what. These tests pin the per-consumer
accounting (granted / denied / spent, carried on the decision
event), the reserve that keeps relationship and merge consumers
from spending the trigger gate's floor, and the coverage metric
that counts how many principal claims the judge actually reached.

Stdlib unittest only. Budget functions are pure counter logic;
no network, no real models.
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("pp_budget_consumers",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class ConsumerAccountingTests(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()
        self.events = []
        self._orig_event = pp._run_event
        pp._run_event = self.events.append

    def tearDown(self):
        pp._run_event = self._orig_event
        pp._dm_budget_reset()

    def test_accounting_accumulates_per_consumer(self):
        pp._dm_budget_claim(1, consumer="rel_family")
        pp._dm_budget_claim(1, consumer="rel_family")
        pp._dm_budget_claim(2, consumer="merge_tier3")
        snap = pp._dm_budget_snapshot()
        self.assertEqual(snap["rel_family"],
                         {"granted": 2, "denied": 0, "spent": 2})
        self.assertEqual(snap["merge_tier3"],
                         {"granted": 1, "denied": 0, "spent": 2})

    def test_untagged_claims_accounted_as_untagged(self):
        pp._dm_budget_claim(1)
        snap = pp._dm_budget_snapshot()
        self.assertEqual(snap["untagged"],
                         {"granted": 1, "denied": 0, "spent": 1})

    def test_denied_claims_counted_per_consumer(self):
        # Amended 2026-10-17 (2.13.20): hard reserves (spec §1.3)
        # give every consumer a protected slice, so a family claim's
        # ceiling is the cap minus the OTHER unfinished reserves.
        # Releasing the reserves this test does not measure (merge,
        # non-family, quote poll) leaves the trigger reserve as the
        # binding one — the same 35-call ceiling this test pinned
        # under the provisional floor. Assertions unchanged.
        for _c in ("merge_tier3", "rel_nonfamily", "quote_poll"):
            pp._dm_budget_finish(_c)
        for _ in range(35):
            self.assertTrue(pp._dm_budget_claim(1, consumer="rel_family"))
        self.assertFalse(pp._dm_budget_claim(1, consumer="rel_family"))
        snap = pp._dm_budget_snapshot()
        self.assertEqual(snap["rel_family"]["granted"], 35)
        self.assertEqual(snap["rel_family"]["denied"], 1)
        self.assertEqual(snap["rel_family"]["spent"], 35)

    def test_reset_clears_accounting(self):
        pp._dm_budget_claim(1, consumer="rel_family")
        pp._dm_budget_reset()
        self.assertEqual(pp._dm_budget_snapshot(), {})

    def test_decision_event_carries_consumer(self):
        pp._dm_budget_claim(1, consumer="trigger_gate")
        pp._dm_budget_claim(1)
        tagged = [e for e in self.events if e.get("event") == "decision"]
        self.assertEqual(tagged[0]["consumer"], "trigger_gate")
        self.assertEqual(tagged[1]["consumer"], "untagged")

    def test_snapshot_is_a_copy(self):
        pp._dm_budget_claim(1, consumer="rel_family")
        snap = pp._dm_budget_snapshot()
        snap["rel_family"]["spent"] = 999
        self.assertEqual(pp._dm_budget_snapshot()["rel_family"]["spent"], 1)


class TriggerFloorTests(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()

    def tearDown(self):
        pp._dm_budget_reset()

    def test_floor_value(self):
        self.assertEqual(pp._TRIGGER_GATE_FLOOR, 15)

    def test_non_trigger_consumers_cannot_spend_the_floor(self):
        # Amended 2026-10-17 (2.13.20): as in
        # test_denied_claims_counted_per_consumer — the reserves not
        # under test are released so the trigger slice is the one
        # that binds, exactly as the provisional floor did.
        for _c in ("merge_tier3", "rel_nonfamily", "quote_poll"):
            pp._dm_budget_finish(_c)
        for _ in range(35):
            self.assertTrue(pp._dm_budget_claim(1, consumer="rel_family"))
        self.assertFalse(pp._dm_budget_claim(1, consumer="rel_family"))
        self.assertFalse(pp._dm_budget_claim(1, consumer="merge_tier3"))
        self.assertFalse(pp._dm_budget_claim(1, consumer="quote_poll"))

    def test_trigger_gate_can_spend_into_its_floor(self):
        # Amended 2026-10-17 (2.13.20): pipeline order made
        # explicit — merges and family finish before non-family
        # runs (their real order), the quote reserve (not under
        # test) is released, and non-family finishes before the
        # trigger gate borrows what it left. Assertions unchanged.
        for _c in ("merge_tier3", "rel_family", "quote_poll"):
            pp._dm_budget_finish(_c)
        for _ in range(35):
            pp._dm_budget_claim(1, consumer="rel_nonfamily")
        pp._dm_budget_finish("rel_nonfamily")
        for _ in range(15):
            self.assertTrue(pp._dm_budget_claim(1, consumer="trigger_gate"))
        self.assertFalse(pp._dm_budget_claim(1, consumer="trigger_gate"))

    def test_trigger_spending_shrinks_the_general_pool(self):
        # Amended 2026-10-17 (2.13.20): merges finish before
        # validation (their real order); family and non-family
        # finish before the trigger gate runs (also their real
        # order). Assertions unchanged.
        pp._dm_budget_finish("merge_tier3")
        for _ in range(20):
            pp._dm_budget_claim(1, consumer="rel_family")
        pp._dm_budget_finish("rel_family")
        pp._dm_budget_finish("rel_nonfamily")
        for _ in range(15):
            pp._dm_budget_claim(1, consumer="trigger_gate")
        # 35 spent in total; the general pool's ceiling is 35, so
        # relationship consumers are done even though the trigger
        # gate spent from its own reserve.
        self.assertFalse(pp._dm_budget_claim(1, consumer="rel_family"))

    def test_untagged_claims_cannot_defeat_the_reserves(self):
        # Amended 2026-10-17 (2.13.20): this test used to pin the
        # opposite — untagged claims kept whole-pool semantics and
        # only the named consumers were bound by the floor. That
        # was the phase-1 bypass (recorded PASS_WITH_RISK in
        # 2.13.18): any caller that forgot its tag could silently
        # spend the trigger reserve. Hard reserves close it:
        # untagged is an accounted consumer with zero reserve, so
        # on a fresh budget it can spend only the general pool
        # (cap minus every reserve slice), and it reaches the full
        # pool only once every named consumer has finished.
        _general = pp._MERGE_TIER3_CAP - sum(
            pp._DM_BUDGET_RESERVES.values())
        for _ in range(_general):
            self.assertTrue(pp._dm_budget_claim(1))
        self.assertFalse(pp._dm_budget_claim(1))
        for _c in pp._DM_BUDGET_ORDER:
            pp._dm_budget_finish(_c)
        for _ in range(pp._MERGE_TIER3_CAP - _general):
            self.assertTrue(pp._dm_budget_claim(1))
        self.assertFalse(pp._dm_budget_claim(1))
        snap = pp._dm_budget_snapshot()
        self.assertEqual(snap["untagged"]["spent"],
                         pp._MERGE_TIER3_CAP)


class PrincipalCoverageTests(unittest.TestCase):
    def _rel(self, frm, to, p, note=None):
        r = {"from": frm, "to": to, "type": "friend",
             "validation_p": p}
        if note:
            r["validation_note"] = note
        return r

    def test_coverage_counts_judged_principal_claims(self):
        principals = ["A", "B", "C", "D", "E"]
        rels = [
            self._rel("A", "B", 0.9, "kept"),
            self._rel("A", "C", None, "budget_exhausted"),
            self._rel("X", "Y", 0.5, "kept"),  # not a principal pair
        ]
        dropped = [
            self._rel("A", "D", 0.1, "dropped"),       # judged, dropped
            self._rel("A", "E", None, "no_evidence"),  # ruled, unjudged
            {"from": "A", "to": "E", "type": "friend"},  # no note: not
            # a validation disposal (ghost/family-guard) — excluded
        ]
        cov = pp._principal_rel_coverage(principals, rels, dropped)
        self.assertEqual(cov, {"judged": 2, "total": 4})

    def test_coverage_without_principal_filter_counts_all(self):
        rels = [self._rel("A", "B", 0.9, "kept"),
                self._rel("X", "Y", None, "validator_error")]
        cov = pp._principal_rel_coverage(None, rels, [])
        self.assertEqual(cov, {"judged": 1, "total": 2})


if __name__ == "__main__":
    unittest.main()
