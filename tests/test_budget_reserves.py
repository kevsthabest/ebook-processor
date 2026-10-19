"""Hard budget reserves, validation priority order, the
validator-error circuit breaker, and unverified-claim surfacing
(docs/discovery-and-budget-spec.md §1.2, §1.3, §1.5, §1.6;
Decisions K1, K2, K3, K4).

Phase 1 shipped consumer accounting plus a provisional trigger
floor that bound only the named consumers — an untagged claim
kept whole-pool semantics and could silently spend the floor
(recorded PASS_WITH_RISK in 2.13.18). This release replaces the
floor with hard reserves for every consumer, closes the untagged
bypass, orders validation by stakes / principal status /
provisional confidence-ascending, retries validator errors once
per claim behind a book-level circuit breaker that skips the
book's write, and stamps every claim with its §1.5 curation
state and provenance.

Stdlib unittest only. No network, no real models.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("pp_budget_reserves",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _chs(*texts):
    return [{"index": i + 1, "label": f"Ch{i + 1}", "text": t}
            for i, t in enumerate(texts)]


def _roster(*names):
    roster = {}
    for name in names:
        pp._roster_update(
            roster,
            [{"name": name, "aliases": [], "role": "supporting",
              "description": "", "evidence": ""}],
            1)
    return roster


class _CapturingValidator:
    model = "stub"

    def __init__(self, p=0.95):
        self.p = p
        self.calls = []

    def validate_relationship(self, frm, to, rtype, quote,
                              context=""):
        self.calls.append((frm, to, rtype))
        return True, self.p


def _finish(*consumers):
    for c in consumers:
        pp._dm_budget_finish(c)


class ReserveSchemeTests(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()

    def tearDown(self):
        pp._dm_budget_reset()

    def test_reserves_sum_below_cap_leaving_a_general_pool(self):
        total = sum(pp._DM_BUDGET_RESERVES.values())
        self.assertLess(total, pp._MERGE_TIER3_CAP)
        # The trigger slice IS the old provisional floor, not a
        # second mechanism stacked on top of it.
        self.assertEqual(pp._DM_BUDGET_RESERVES["trigger_gate"],
                         pp._TRIGGER_GATE_FLOOR)

    def test_consumer_can_spend_general_pool_plus_own_reserve(self):
        # rel_family alone on a fresh budget: general pool (9)
        # plus its own slice (10) = 19; every other slice is
        # still protected.
        general = pp._MERGE_TIER3_CAP - sum(
            pp._DM_BUDGET_RESERVES.values())
        own = pp._DM_BUDGET_RESERVES["rel_family"]
        for _ in range(general + own):
            self.assertTrue(
                pp._dm_budget_claim(1, consumer="rel_family"))
        self.assertFalse(pp._dm_budget_claim(1, consumer="rel_family"))

    def test_later_reserve_is_protected_from_the_first_consumer(self):
        # Merges run first, but the trigger reserve survives them:
        # the merge ceiling is the cap minus every later slice.
        protected = sum(pp._DM_BUDGET_RESERVES[c] for c in
                        ("rel_family", "rel_nonfamily",
                         "trigger_gate", "quote_poll"))
        for _ in range(pp._MERGE_TIER3_CAP - protected):
            self.assertTrue(
                pp._dm_budget_claim(1, consumer="merge_tier3"))
        self.assertFalse(pp._dm_budget_claim(1, consumer="merge_tier3"))

    def test_borrowing_opens_only_after_the_earlier_consumer_finishes(self):
        general = pp._MERGE_TIER3_CAP - sum(
            pp._DM_BUDGET_RESERVES.values())
        own = pp._DM_BUDGET_RESERVES["rel_family"]
        for _ in range(general + own):
            pp._dm_budget_claim(1, consumer="rel_family")
        self.assertFalse(pp._dm_budget_claim(1, consumer="rel_family"))
        # Merge (earlier in pipeline order) finishes: its unused
        # slice becomes borrowable and family claims resume.
        pp._dm_budget_finish("merge_tier3")
        for _ in range(pp._DM_BUDGET_RESERVES["merge_tier3"]):
            self.assertTrue(
                pp._dm_budget_claim(1, consumer="rel_family"))
        self.assertFalse(pp._dm_budget_claim(1, consumer="rel_family"))

    def test_borrowing_waits_for_every_earlier_consumer(self):
        # The §1.3 condition, read literally: family has finished,
        # but merges (earlier than non-family) have not — so
        # non-family may NOT yet borrow family's unused reserve.
        general = pp._MERGE_TIER3_CAP - sum(
            pp._DM_BUDGET_RESERVES.values())
        own = pp._DM_BUDGET_RESERVES["rel_family"]
        for _ in range(general + own):
            pp._dm_budget_claim(1, consumer="rel_family")
        pp._dm_budget_finish("rel_family")
        self.assertFalse(
            pp._dm_budget_claim(1, consumer="rel_nonfamily"))
        # Once merges finish too, borrowing unlocks: non-family
        # can spend into the reserves both earlier stages left.
        pp._dm_budget_finish("merge_tier3")
        self.assertTrue(
            pp._dm_budget_claim(1, consumer="rel_nonfamily"))

    def test_single_ceiling_binds_across_all_consumers(self):
        # Walk the pipeline in order, each stage spending what it
        # can: the pool total never exceeds the one per-book cap.
        spent = 0
        for consumer in pp._DM_BUDGET_ORDER:
            while pp._dm_budget_claim(1, consumer=consumer):
                spent += 1
            pp._dm_budget_finish(consumer)
        self.assertEqual(spent, pp._MERGE_TIER3_CAP)
        self.assertEqual(pp._DM_BUDGET_USED, pp._MERGE_TIER3_CAP)

    def test_reset_clears_finished_marks(self):
        pp._dm_budget_finish("merge_tier3")
        pp._dm_budget_reset()
        self.assertEqual(pp._dm_budget_scheme()["finished"], [])

    def test_scheme_records_reserves_and_finishes(self):
        pp._dm_budget_finish("merge_tier3")
        scheme = pp._dm_budget_scheme()
        self.assertEqual(scheme["cap"], pp._MERGE_TIER3_CAP)
        self.assertEqual(scheme["reserves"], pp._DM_BUDGET_RESERVES)
        self.assertEqual(scheme["finished"], ["merge_tier3"])
        self.assertEqual(scheme["pipeline_order"],
                         list(pp._DM_BUDGET_ORDER))

    def test_run_record_carries_order_reserves_and_breaker_bars(self):
        thresholds = pp._history_thresholds()
        self.assertEqual(thresholds["validation_order"],
                         pp._VALIDATION_ORDER_ID)
        self.assertEqual(thresholds["budget_reserves"],
                         pp._DM_BUDGET_RESERVES)
        self.assertEqual(thresholds["validator_error_threshold"],
                         pp._VALIDATOR_ERROR_THRESHOLD)
        self.assertEqual(thresholds["validator_error_min_sample"],
                         pp._VALIDATOR_ERROR_MIN_SAMPLE)


class ValidationOrderKeyTests(unittest.TestCase):
    def _key(self, rtype, conf="high", principal=True, index=0):
        names = {"alice", "bob"} if principal else set()
        rel = {"from": "Alice", "to": "Bob", "type": rtype,
               "confidence": conf}
        return pp._validation_order_key(rel, index, names)

    def test_family_before_nonfamily(self):
        self.assertLess(self._key("sibling"),
                        self._key("romantic"))

    def test_stakes_within_family(self):
        self.assertLess(self._key("spouse"), self._key("sibling"))
        self.assertLess(self._key("parent"), self._key("sibling"))
        self.assertLess(self._key("child"), self._key("sibling"))

    def test_principal_before_minor_within_a_group(self):
        self.assertLess(self._key("spouse", principal=True),
                        self._key("spouse", principal=False))

    def test_confidence_ascending_within_a_group(self):
        # Provisional per Decision K1: low extractor confidence
        # is judged first.
        self.assertLess(self._key("spouse", conf="low"),
                        self._key("spouse", conf="medium"))
        self.assertLess(self._key("spouse", conf="medium"),
                        self._key("spouse", conf="high"))

    def test_nonfamily_has_no_stakes_order(self):
        # §1.2 gives stakes only within family: two non-family
        # claims in the same principal/confidence group order by
        # stored position alone.
        a = self._key("friend", index=0)
        b = self._key("romantic", index=1)
        self.assertEqual(a[:4], b[:4])
        self.assertLess(a, b)


class ValidationOrderReduceTests(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _CapturingValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_reduce_judges_in_priority_order_not_stored_order(self):
        # Stored order is deliberately wrong on every axis: a
        # minor spouse first, a principal spouse second, a
        # sibling third, a friend last by type but stored before
        # nothing else matters — family still precedes it.
        roster = _roster("Alice", "Bob", "Carol", "Dave",
                         "Eve", "Frank")
        chs = _chs("x " * 200, "y " * 200)
        chapter_as = {
            1: {"characters": [], "pov_character": "Alice",
                "task_status": {"identity": "ok"},
                "relationships": [
                    {"from": "Carol", "to": "Dave", "type": "spouse",
                     "evidence": "his wife Carol"},
                    {"from": "Eve", "to": "Frank", "type": "sibling",
                     "evidence": "her brother Frank"}]},
            2: {"characters": [], "pov_character": "Bob",
                "task_status": {"identity": "ok"},
                "relationships": [
                    {"from": "Alice", "to": "Bob", "type": "spouse",
                     "evidence": "his wife Alice"},
                    {"from": "Alice", "to": "Carol", "type": "friend",
                     "evidence": "her friend Carol"}]},
        }
        red = pp.v2_reduce(chapter_as, {}, roster, chs, preview=False)
        judged = [(c[0], c[1], c[2]) for c in self.validator.calls]
        self.assertEqual(judged, [
            ("Alice", "Bob", "spouse"),    # principal spouse first
            ("Carol", "Dave", "spouse"),   # then the minor spouse
            ("Eve", "Frank", "sibling"),   # stakes: sibling last in family
            ("Alice", "Carol", "friend"),  # non-family after all family
        ])
        self.assertEqual(red["validation_order"],
                         pp._VALIDATION_ORDER_ID)

    def test_confidence_ascending_within_a_group_in_reduce(self):
        # Two friend claims (same group): the pair extracted in
        # two chapters carries HIGH extractor confidence, the
        # one-chapter pair MEDIUM — ascending order judges the
        # medium-confidence claim first, though it is stored last.
        chs = _chs("x " * 200, "y " * 200, "z " * 200)
        chapter_as = {
            1: {"characters": [], "task_status": {"identity": "ok"},
                "relationships": [
                    {"from": "Alice", "to": "Bob", "type": "friend",
                     "evidence": "good friends"}]},
            2: {"characters": [], "task_status": {"identity": "ok"},
                "relationships": [
                    {"from": "Alice", "to": "Bob", "type": "friend",
                     "evidence": "good friends"},
                    {"from": "Carol", "to": "Dave", "type": "friend",
                     "evidence": "good friends"}]},
            3: {"characters": [], "task_status": {"identity": "ok"},
                "relationships": []},
        }
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        conf = {(r["from"], r["to"]): r["confidence"]
                for r in red["relationships"]}
        self.assertEqual(conf[("Alice", "Bob")], "high")
        self.assertEqual(conf[("Carol", "Dave")], "medium")
        judged = [(c[0], c[1]) for c in self.validator.calls]
        self.assertEqual(judged, [("Carol", "Dave"),
                                  ("Alice", "Bob")])


class _FlakyValidator:
    model = "stub"

    def __init__(self, fail_first=0, error_pairs=()):
        self.calls = []
        self._fail_first = fail_first
        self._error_pairs = set(error_pairs)

    def validate_relationship(self, frm, to, rtype, quote,
                              context=""):
        self.calls.append((frm, to, rtype))
        if self._fail_first > 0:
            self._fail_first -= 1
            return None, None
        if (frm, to) in self._error_pairs:
            return None, None
        return True, 0.95


def _claims(n, rtype="spouse"):
    return [{"from": f"Alpha{i}", "to": f"Beta{i}", "type": rtype,
             "evidence": "an evidenced claim"} for i in range(n)]


def _reduce(claims, validator):
    chs = _chs("x " * 200, "y " * 200)
    chapter_as = {1: {"characters": [],
                      "task_status": {"identity": "ok"},
                      "relationships": claims}}
    return pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_error_retries_once_and_can_recover(self):
        validator = _FlakyValidator(fail_first=1)
        pp._DECISION_VALIDATOR = validator
        red = _reduce(_claims(1), validator)
        self.assertEqual(len(validator.calls), 2)  # attempt + retry
        rel = red["relationships"][0]
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["validation_p"], 0.95)
        breaker = red["validator_circuit_breaker"]
        self.assertEqual(breaker["attempts"], 1)
        self.assertEqual(breaker["errors"], 0)
        self.assertFalse(breaker["tripped"])

    def test_persistent_error_is_never_recorded_as_success(self):
        validator = _FlakyValidator(error_pairs={("Alpha0", "Beta0")})
        pp._DECISION_VALIDATOR = validator
        red = _reduce(_claims(1), validator)
        self.assertEqual(len(validator.calls), 2)  # exactly one retry
        rel = red["relationships"][0]
        self.assertEqual(rel["validation_note"], "validator_error")
        self.assertEqual(rel["type"], "spouse")  # original type kept
        self.assertNotIn("validation_p", rel)
        self.assertEqual(rel["validation_state"], "validator_error")

    def test_breaker_trips_at_threshold_with_enough_sample(self):
        pairs = {(f"Alpha{i}", f"Beta{i}") for i in range(8)}
        validator = _FlakyValidator(error_pairs=pairs)
        pp._DECISION_VALIDATOR = validator
        red = _reduce(_claims(8), validator)
        breaker = red["validator_circuit_breaker"]
        self.assertEqual(breaker["attempts"], 8)
        self.assertEqual(breaker["errors"], 8)
        self.assertEqual(breaker["error_rate"], 1.0)
        self.assertTrue(breaker["tripped"])
        self.assertEqual(breaker["threshold"],
                         pp._VALIDATOR_ERROR_THRESHOLD)
        self.assertEqual(breaker["min_sample"],
                         pp._VALIDATOR_ERROR_MIN_SAMPLE)
        self.assertEqual(len(validator.calls), 16)  # one retry each

    def test_breaker_needs_the_minimum_sample(self):
        # A 100% error rate on four claims is noise, not a verdict
        # on the validator: the breaker must not trip.
        pairs = {(f"Alpha{i}", f"Beta{i}") for i in range(4)}
        validator = _FlakyValidator(error_pairs=pairs)
        pp._DECISION_VALIDATOR = validator
        red = _reduce(_claims(4), validator)
        breaker = red["validator_circuit_breaker"]
        self.assertEqual(breaker["error_rate"], 1.0)
        self.assertFalse(breaker["tripped"])

    def test_breaker_trips_on_a_half_error_rate(self):
        pairs = {(f"Alpha{i}", f"Beta{i}") for i in range(4)}
        validator = _FlakyValidator(error_pairs=pairs)
        pp._DECISION_VALIDATOR = validator
        red = _reduce(_claims(8), validator)
        breaker = red["validator_circuit_breaker"]
        self.assertEqual(breaker["attempts"], 8)
        self.assertEqual(breaker["errors"], 4)
        self.assertEqual(breaker["error_rate"], 0.5)
        self.assertTrue(breaker["tripped"])

    def test_write_claims_refuses_a_tripped_book(self):
        calls = []

        def fake_sb(*args, **kwargs):
            calls.append((args, kwargs))
            return []

        old_sb = pp.sb
        pp.sb = fake_sb
        try:
            result = {"validator_circuit_breaker": {
                "tripped": True, "attempts": 8, "errors": 8,
                "error_rate": 1.0}}
            self.assertEqual(pp.write_claims("work-x", result), 1)
        finally:
            pp.sb = old_sb
        # Nothing was read, deleted, or inserted: previous rows
        # are untouched because no database call happened at all.
        self.assertEqual(calls, [])

    def test_write_claims_proceeds_when_not_tripped(self):
        def fake_sb(*args, **kwargs):
            return []

        old_sb = pp.sb
        pp.sb = fake_sb
        try:
            result = {"title": "T", "tropes": [], "triggers": [],
                      "characters": [], "relationships": [],
                      "quotes": [], "trope_catalog_map": {},
                      "validator_circuit_breaker": {
                          "tripped": False, "attempts": 0,
                          "errors": 0, "error_rate": 0.0}}
            self.assertEqual(pp.write_claims("work-x", result), 0)
        finally:
            pp.sb = old_sb


class UnverifiedClaimsSurfacingTests(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _CapturingValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_state_name_mapping(self):
        cases = {
            "kept": "validated", "downgraded": "validated",
            "budget_exhausted": "budget_exhausted",
            "validator_error": "validator_error",
            "dropped": "rejected", "would_drop": "rejected",
            "no_evidence": "rejected",
            "ghost_endpoint": "review_required",
            "": "review_required",
        }
        for note, state in cases.items():
            rel = {"validation_note": note} if note else {}
            self.assertEqual(pp._rel_validation_state(rel), state,
                             f"note={note!r}")

    def test_validated_claim_carries_state_and_provenance(self):
        red = _reduce(_claims(1), self.validator)
        rel = red["relationships"][0]
        self.assertEqual(rel["validation_state"], "validated")
        self.assertEqual(rel["source"], "extracted")
        rel_map, _ = pp._build_rel_map(
            {"relationships": red["relationships"]})
        entry = rel_map["alpha0"][0]
        self.assertEqual(entry["validation_state"], "validated")
        self.assertEqual(entry["source"], "extracted")

    def test_no_validator_means_review_required(self):
        pp._DECISION_VALIDATOR = None
        red = _reduce(_claims(1), self.validator)
        rel = red["relationships"][0]
        self.assertEqual(rel["validation_state"], "review_required")
        self.assertEqual(self.validator.calls, [])

    def test_dropped_claim_is_rejected_in_the_dropped_list(self):
        validator = _CapturingValidator(p=0.1)
        pp._DECISION_VALIDATOR = validator
        red = _reduce(_claims(1), validator)
        self.assertEqual(red["relationships"], [])
        dropped = red["dropped_relationships"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["validation_state"], "rejected")
        self.assertEqual(dropped[0]["source"], "extracted")

    def test_budget_exhausted_claims_ship_uncapped_with_state(self):
        # Amended 2026-10-19 (2.13.66, candidate-aware sizing —
        # Decision 2026-10-19): draining the provisional pool no
        # longer manufactures exhausted claims, because the pool
        # resizes at the reduce to spent + measured census demand.
        # Exhaustion now requires demand above the 250 ceiling:
        # drain first (merges claim first in a real run), then
        # present 260 claims — 200 are judged, 60 exhaust. The
        # §1.5 pin is unchanged: every exhausted claim keeps its
        # type and ships with its state; there is NO per-book cap
        # on how many ship.
        for c in pp._DM_BUDGET_ORDER:
            pp._dm_budget_finish(c)
        for _ in range(pp._MERGE_TIER3_CAP):
            self.assertTrue(pp._dm_budget_claim(1))
        claims = _claims(260, rtype="friend")
        red = _reduce(claims, self.validator)
        self.assertEqual(len(red["relationships"]), 260)
        exhausted = [r for r in red["relationships"]
                     if r.get("validation_note") == "budget_exhausted"]
        self.assertEqual(len(exhausted), 60)
        self.assertEqual(len(self.validator.calls), 200)
        for rel in exhausted:
            self.assertEqual(rel["type"], "friend")
            self.assertEqual(rel["validation_state"],
                             "budget_exhausted")
            self.assertEqual(rel["source"], "extracted")

    def test_existing_provenance_is_preserved_not_overwritten(self):
        claims = _claims(1)
        claims[0]["source"] = "human"
        red = _reduce(claims, self.validator)
        self.assertEqual(red["relationships"][0]["source"], "human")
        rel_map, _ = pp._build_rel_map(
            {"relationships": red["relationships"]})
        self.assertEqual(rel_map["alpha0"][0]["source"], "human")

    def test_preview_markdown_shows_unverified_state(self):
        # Amended 2026-10-19 (2.13.66): as in the ship-uncapped
        # test above, the exhausted claim this test renders is now
        # manufactured above the sizing ceiling (260 claims after
        # a drain: 60 exhaust) instead of by draining the old flat
        # pool under a single claim.
        claims = _claims(260, rtype="friend")
        for c in pp._DM_BUDGET_ORDER:
            pp._dm_budget_finish(c)
        for _ in range(pp._MERGE_TIER3_CAP):
            pp._dm_budget_claim(1)
        red = _reduce(claims, self.validator)
        result = {
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
            "relationships": red["relationships"],
            "dropped_relationships": [], "quotes": [],
            "spice_level": 0, "tasks": {},
            "avg_dialogue_ratio": None,
        }
        old_dir = pp.SCRIPT_DIR
        with tempfile.TemporaryDirectory() as td:
            pp.SCRIPT_DIR = Path(td)
            try:
                pp.save_preview(Path(td) / "Book.epub", result)
                md = (Path(td) / "preview" / "Book.md").read_text(
                    encoding="utf-8")
            finally:
                pp.SCRIPT_DIR = old_dir
        self.assertIn("budget_exhausted", md)


if __name__ == "__main__":
    unittest.main()
