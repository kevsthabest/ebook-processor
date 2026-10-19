"""Runner-up revival (F-24-1): a rejected pair winner no longer
annihilates the pair.

Motivation (Fourth Wing run 24, 2026-10-18 audit): reduce keeps ONE
winner per pair. For the book's central pair, a single evidence-free
hallucinated "parent" claim scored confidence HIGH (co-occurrence
across 49 chapters plus the family confidence floor, evidence_count
0), won the pair over 17 evidence-bearing "romantic" claims, then
died at evidence mining (no_evidence). The pair shipped NOTHING,
and the losing claims left no trace anywhere — the reduce discarded
them silently.

The fix, two halves:
  (c) pair_losers: every candidate that passed the unknown-type
      filter but lost the per-pair competition is recorded on the
      result (from/to/type/confidence/evidence_count/
      cooccurrence_count/chapter/lost_to_type). Losers are NOT put
      in dropped_relationships — they were never validated, and
      that list's semantics (validation drops + family-guard
      suppressions) feed history outcomes and the Tier 3 importer.
      The family-guard suppressed claim keeps its own existing
      record and is excluded from pair_losers.
  (a) revival: when a pair's winner is REJECTED in validation —
      fail-closed at the evidence stage (no_evidence) or drawn and
      dropped — the pair's best losing candidate re-enters
      validation through the normal path for its class. The
      family-guard suppressed claim, when one exists, IS the
      revival candidate (it was the would-be winner). If the
      budget cannot pay for the revived claim's draw it ships
      unjudged as review_required instead of vanishing. One
      revival per pair; a kept or downgraded winner never
      triggers one.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _chs(*texts):
    return [{"index": i + 1, "label": f"Ch{i + 1}", "text": t}
            for i, t in enumerate(texts)]


def _chapter_as(by_chapter):
    """{chapter_index: [relationship dicts]} in Call-A shape."""
    return {idx: {"characters": [], "relationships": rels,
                  "task_status": {"identity": "ok"}}
            for idx, rels in by_chapter.items()}


class _MapValidator:
    """Stub judge: per-type probabilities, records every call."""
    model = "stub"

    def __init__(self, p_by_type=None, default=0.95):
        self.p_by_type = p_by_type or {}
        self.default = default
        self.calls = []  # (from, to, type, quote)

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls.append((frm, to, rtype, quote))
        return True, self.p_by_type.get(rtype, self.default)


def _pair_rels(red, frm, to):
    names = {frm, to}
    return [r for r in red["relationships"]
            if {r.get("from"), r.get("to")} == names]


_FILLER = "x " * 200


class TestRunnerUpRevival(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def _fw_shape(self):
        """The run-24 shape in miniature: an evidence-free parent
        claim outranks evidence-bearing romantic claims for one
        pair (co-occurrence 3 chapters + family floor beats them
        on the specificity tiebreak)."""
        chs = _chs(_FILLER, _FILLER, _FILLER)
        chapter_as = _chapter_as({
            1: [{"from": "Anna", "to": "Ben", "type": "parent",
                 "evidence": ""},
                {"from": "Anna", "to": "Ben", "type": "romantic",
                 "evidence": "Anna laughed with Ben in the yard."}],
            2: [{"from": "Anna", "to": "Ben", "type": "romantic",
                 "evidence": "Anna kissed Ben at the gate."}],
            3: [{"from": "Anna", "to": "Ben", "type": "romantic",
                 "evidence": "Anna chose Ben, again and always."}],
        })
        return chapter_as, chs

    def test_fw_shape_winner_dies_no_evidence_runner_up_revives(self):
        # Test (1): the parent winner has no evidence and no
        # mineable sentence (filler text) -> no_evidence death.
        # The romantic runner-up revives, is drawn, and ships
        # validated with the revival stamp. Pre-fix the pair
        # yields nothing at all.
        pp._DECISION_VALIDATOR = _MapValidator({"romantic": 0.9})
        chapter_as, chs = self._fw_shape()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        rels = _pair_rels(red, "Anna", "Ben")
        self.assertEqual(len(rels), 1)
        rel = rels[0]
        self.assertEqual(rel["type"], "romantic")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["validation_p"], 0.9)
        self.assertEqual(rel["validation_state"], "validated")
        self.assertTrue(rel["revived_runner_up"])
        self.assertEqual(rel["revived_over_type"], "parent")
        self.assertEqual(rel["revived_over_fate"], "no_evidence")
        # The judge was asked about the romantic claim exactly once
        # (the parent spent no call: fail-closed at evidence).
        self.assertEqual([c[2] for c in
                          pp._DECISION_VALIDATOR.calls], ["romantic"])
        # The dead winner is in dropped_relationships; the revived
        # claim is NOT (losers/revivals never pollute that list).
        dropped = red["dropped_relationships"]
        self.assertEqual([d["type"] for d in dropped], ["parent"])
        self.assertEqual(dropped[0]["validation_note"], "no_evidence")
        # pair_losers records the competition: all three romantic
        # candidates lost to the parent.
        losers = red["pair_losers"]
        self.assertEqual(len(losers), 3)
        self.assertTrue(all(l["type"] == "romantic" for l in losers))
        self.assertTrue(all(l["lost_to_type"] == "parent"
                            for l in losers))
        self.assertTrue(all(l["from"] == "Anna" and l["to"] == "Ben"
                            for l in losers))
        for l in losers:
            self.assertEqual(l["confidence"], "high")
            self.assertEqual(l["evidence_count"], 3)
            self.assertEqual(l["cooccurrence_count"], 3)
            self.assertIn("chapter", l)
        # Console: one revival line naming pair, types, fate.
        out = buf.getvalue()
        self.assertIn("Runner-up revival", out)
        self.assertIn("romantic", out)
        self.assertIn("parent", out)

    def test_winner_drawn_and_dropped_revives(self):
        # Test (2): the family winner HAS evidence, is drawn, and
        # the judge drops it (P below the drop threshold). The
        # friend runner-up revives and is kept.
        pp._DECISION_VALIDATOR = _MapValidator(
            {"spouse": 0.1, "friend": 0.9})
        chs = _chs(_FILLER)
        chapter_as = _chapter_as({
            1: [{"from": "Anna", "to": "Ben", "type": "spouse",
                 "evidence": "Anna and Ben exchanged vows."},
                {"from": "Anna", "to": "Ben", "type": "friend",
                 "evidence": "Anna and Ben shared a joke."}],
        })
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        rels = _pair_rels(red, "Anna", "Ben")
        self.assertEqual(len(rels), 1)
        rel = rels[0]
        self.assertEqual(rel["type"], "friend")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertTrue(rel["revived_runner_up"])
        self.assertEqual(rel["revived_over_type"], "spouse")
        self.assertEqual(rel["revived_over_fate"], "dropped")
        self.assertEqual([d["type"] for d in
                          red["dropped_relationships"]], ["spouse"])

    def test_revival_with_empty_pool_ships_review_required(self):
        # Test (3): the pool is empty when revival runs — the
        # revived romantic claim cannot be drawn, so it ships
        # unjudged with state review_required (NOT
        # budget_exhausted), carrying the revival stamp.
        # Amended 2026-10-19 (2.13.66, candidate-aware sizing —
        # Decision 2026-10-19): draining the provisional pool
        # before the run no longer empties the pool at revival
        # time, because the reduce resizes it to spent + census
        # demand. The empty pool is now manufactured honestly:
        # the same pre-drain (50 spent), plus 250 filler family
        # winners whose judging burns the resized pool to its
        # 250 ceiling during the family pass — the parent winner
        # still dies fail-closed without a draw, and by revival
        # time there is nothing left.
        pp._DECISION_VALIDATOR = _MapValidator({"romantic": 0.9})
        for _c in pp._DM_BUDGET_ORDER:
            pp._dm_budget_finish(_c)
        for _ in range(pp._MERGE_TIER3_CAP):
            self.assertTrue(pp._dm_budget_claim(1))
        self.assertFalse(pp._dm_budget_claim(1))
        chapter_as, chs = self._fw_shape()
        chapter_as[1]["relationships"].extend(
            {"from": f"Filler{i}", "to": f"Spouse{i}",
             "type": "spouse",
             "evidence": "an evidenced spouse claim"}
            for i in range(250))
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        rels = _pair_rels(red, "Anna", "Ben")
        self.assertEqual(len(rels), 1)
        rel = rels[0]
        self.assertEqual(rel["type"], "romantic")
        self.assertEqual(rel["validation_state"], "review_required")
        self.assertNotEqual(rel.get("validation_note"),
                            "budget_exhausted")
        self.assertTrue(rel["revived_runner_up"])
        # The judge was consulted for the filler winners that
        # fit (200 of them) — but never for the revived claim:
        # the revival draw was denied before any call.
        self.assertEqual(len(pp._DECISION_VALIDATOR.calls), 200)
        self.assertEqual(
            [c for c in pp._DECISION_VALIDATOR.calls
             if c[2] == "romantic"], [])

    def test_winner_kept_no_revival_losers_recorded(self):
        # Test (4): a kept winner never triggers revival, but the
        # competition is still recorded in pair_losers.
        pp._DECISION_VALIDATOR = _MapValidator({"spouse": 0.95})
        chs = _chs(_FILLER)
        chapter_as = _chapter_as({
            1: [{"from": "Anna", "to": "Ben", "type": "spouse",
                 "evidence": "Anna and Ben exchanged vows."},
                {"from": "Anna", "to": "Ben", "type": "friend",
                 "evidence": "Anna and Ben shared a joke."}],
        })
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        rels = _pair_rels(red, "Anna", "Ben")
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["type"], "spouse")
        self.assertNotIn("revived_runner_up", rels[0])
        # Only the winner was judged.
        self.assertEqual([c[2] for c in
                          pp._DECISION_VALIDATOR.calls], ["spouse"])
        losers = red["pair_losers"]
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0]["type"], "friend")
        self.assertEqual(losers[0]["lost_to_type"], "spouse")
        self.assertEqual(losers[0]["from"], "Anna")
        self.assertEqual(losers[0]["to"], "Ben")

    def test_revived_claim_rejected_no_second_revival(self):
        # Test (5): parent (evidence-free) wins and dies; romantic
        # revives and the judge drops it too; the third candidate
        # (friend) is NEVER revived or judged — one revival max.
        pp._DECISION_VALIDATOR = _MapValidator({"romantic": 0.1})
        chs = _chs(_FILLER, _FILLER, _FILLER)
        chapter_as = _chapter_as({
            1: [{"from": "Anna", "to": "Ben", "type": "parent",
                 "evidence": ""},
                {"from": "Anna", "to": "Ben", "type": "romantic",
                 "evidence": "Anna kissed Ben at the gate."},
                {"from": "Anna", "to": "Ben", "type": "friend",
                 "evidence": "Anna and Ben shared a joke."}],
            2: [{"from": "Anna", "to": "Ben", "type": "romantic",
                 "evidence": "Anna chose Ben, again and always."}],
            3: [{"from": "Anna", "to": "Ben", "type": "romantic",
                 "evidence": "Anna laughed with Ben in the yard."}],
        })
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(_pair_rels(red, "Anna", "Ben"), [])
        # Only the romantic revival was drawn; friend never judged.
        self.assertEqual([c[2] for c in
                          pp._DECISION_VALIDATOR.calls], ["romantic"])
        dropped_types = sorted(d["type"] for d in
                               red["dropped_relationships"])
        self.assertEqual(dropped_types, ["parent", "romantic"])
        romantic_drop = [d for d in red["dropped_relationships"]
                         if d["type"] == "romantic"][0]
        self.assertTrue(romantic_drop["revived_runner_up"])

    def test_guard_suppressed_claim_is_the_revival_candidate(self):
        # Test (6): a strong enemy claim outranks an evidence-free
        # parent claim, so the family guard suppresses the enemy
        # and the parent wins. The parent dies no_evidence; the
        # SUPPRESSED enemy claim — not a fresh loser — is what
        # revives, and it must not also remain in
        # dropped_relationships as the guard's audit record.
        pp._DECISION_VALIDATOR = _MapValidator({"enemy": 0.9})
        chs = _chs(_FILLER, _FILLER)
        chapter_as = _chapter_as({
            1: [{"from": "Anna", "to": "Ben", "type": "parent",
                 "evidence": ""},
                {"from": "Anna", "to": "Ben", "type": "enemy",
                 "evidence": "Anna swore Ben would pay."}],
            2: [{"from": "Anna", "to": "Ben", "type": "enemy",
                 "evidence": "Anna and Ben fought at dawn."}],
        })
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        rels = _pair_rels(red, "Anna", "Ben")
        self.assertEqual(len(rels), 1)
        rel = rels[0]
        self.assertEqual(rel["type"], "enemy")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertTrue(rel["revived_runner_up"])
        self.assertEqual(rel["revived_over_type"], "parent")
        # The guard's annotation rides along as provenance.
        self.assertEqual(rel.get("family_guard"),
                         "suppressed_by_family")
        # dropped holds ONLY the dead parent — no guard record
        # duplicating the shipped enemy claim.
        self.assertEqual([d["type"] for d in
                          red["dropped_relationships"]], ["parent"])
        # The suppressed claim has its guard record, so it is
        # excluded from pair_losers; the second enemy candidate is
        # an ordinary loser.
        losers = red["pair_losers"]
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0]["type"], "enemy")
        self.assertEqual(losers[0]["chapter"], "Ch2")

    def test_single_candidate_pair_records_nothing(self):
        # Test (7): one candidate, no competition — no loser
        # records, no revival machinery, the claim behaves exactly
        # as before.
        pp._DECISION_VALIDATOR = _MapValidator({"friend": 0.9})
        chs = _chs(_FILLER)
        chapter_as = _chapter_as({
            1: [{"from": "Anna", "to": "Ben", "type": "friend",
                 "evidence": "Anna and Ben shared a joke."}],
        })
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(red["pair_losers"], [])
        rels = _pair_rels(red, "Anna", "Ben")
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["validation_note"], "kept")
        self.assertNotIn("revived_runner_up", rels[0])

    def test_preview_mode_revival_mirrors_without_dropping(self):
        # Test (8): preview records but changes nothing — the dead
        # parent stays with its no_evidence note, and the revived
        # romantic appears alongside it with its preview state and
        # the revival stamp. Nothing lands in dropped.
        pp._DECISION_VALIDATOR = _MapValidator({"romantic": 0.9})
        chapter_as, chs = self._fw_shape()
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=True)
        rels = _pair_rels(red, "Anna", "Ben")
        by_type = {r["type"]: r for r in rels}
        self.assertEqual(set(by_type), {"parent", "romantic"})
        self.assertEqual(by_type["parent"]["validation_note"],
                         "no_evidence")
        self.assertEqual(by_type["romantic"]["validation_note"],
                         "kept")
        self.assertTrue(by_type["romantic"]["revived_runner_up"])
        self.assertEqual(red["dropped_relationships"], [])


    def test_no_validator_no_revival(self):
        # Test (9): revival is a validation-stage mechanism. With
        # no validator configured, nothing was rejected BY
        # validation — the fail-closed evidence drop stands as
        # the pair's whole outcome and the run is unchanged from
        # before revival existed (the family guard's pinned
        # contract in test_relationship_integrity).
        pp._DECISION_VALIDATOR = None
        chapter_as, chs = self._fw_shape()
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(_pair_rels(red, "Anna", "Ben"), [])
        self.assertEqual([d["type"] for d in
                          red["dropped_relationships"]], ["parent"])
        # The competition is still recorded (pair_losers is an
        # artefact record, not a validation outcome).
        self.assertEqual(len(red["pair_losers"]), 3)


if __name__ == "__main__":
    unittest.main()
