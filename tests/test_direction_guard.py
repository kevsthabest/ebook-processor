"""Extraction direction discipline (run-23 inversions, v2.13.23).

The failures: Fen Riorson typed as his son Xaden's "child";
Lilith Sorrengail typed as her daughter's "child". Each inverted
claim was scored alone, so nothing ever noticed the pair's claims
contradicted each other generationally.

Two defences, both pinned here:
- Eval cases in eval/relationship_cases.json capturing both
  inversion shapes as GATED drops (with the correct-direction
  companion keeps), so the judge side of the failure is measured
  by the harness, not by impression.
- A deterministic reduce-level guard: when a pair's canonical
  family claims make EACH endpoint the elder of the other, the
  surviving claim carries a direction_contradiction record and
  ships as review_required instead of validated — the judge
  scored one direction's evidence and never saw the reverse
  claim. Legitimate single-direction claims, including the same
  fact phrased both ways, are untouched.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _claim(frm, to, t):
    return {"from": frm, "to": to, "type": t}


class TestGuardShape(unittest.TestCase):
    def test_single_direction_is_not_a_contradiction(self):
        self.assertIsNone(pp._generational_contradiction(
            [_claim("Fen Riorson", "Xaden Riorson", "parent")]))
        self.assertIsNone(pp._generational_contradiction(
            [_claim("Fen Riorson", "Xaden Riorson", "parent"),
             _claim("Fen Riorson", "Xaden Riorson", "parent")]))

    def test_same_fact_phrased_both_ways_is_not_a_contradiction(self):
        # Canonical form of "Xaden is Fen's child" IS "Fen is
        # Xaden's parent": one elder direction, no flag.
        a = pp._canonical_pair("Fen Riorson", "Xaden Riorson",
                               "parent")
        b = pp._canonical_pair("Xaden Riorson", "Fen Riorson",
                               "child")
        self.assertEqual(a, b)
        self.assertIsNone(pp._generational_contradiction(
            [_claim(*a), _claim(*b)]))

    def test_parent_and_child_same_pair_contradict(self):
        info = pp._generational_contradiction(
            [_claim("Fen Riorson", "Xaden Riorson", "parent"),
             _claim("Fen Riorson", "Xaden Riorson", "child")])
        self.assertIsNotNone(info)
        self.assertEqual(set(info["types"]), {"parent", "child"})

    def test_step_types_contradict_too(self):
        info = pp._generational_contradiction(
            [_claim("A A", "B B", "step_parent"),
             _claim("A A", "B B", "child")])
        self.assertIsNotNone(info)

    def test_nongenerational_claims_never_contradict(self):
        self.assertIsNone(pp._generational_contradiction(
            [_claim("A A", "B B", "spouse"),
             _claim("B B", "A A", "spouse"),
             _claim("A A", "B B", "sibling"),
             _claim("A A", "B B", "friend")]))


def _ch(idx, label, text="x " * 200):
    return {"index": idx, "label": label, "text": text}


def _reduce(rels_by_chapter, preview=False):
    chapter_as = {}
    for idx, rels in rels_by_chapter.items():
        chapter_as[idx] = {"relationships": [
            {"from": f, "to": t, "type": ty, "evidence": ev}
            for f, t, ty, ev in rels], "pov_character": None}
    chs = [_ch(i, f"Ch{i}") for i in sorted(rels_by_chapter)]
    return pp.v2_reduce(chapter_as, {}, {}, chs, preview=preview)


class _KeepValidator:
    model = "stub"

    def validate_relationship(self, frm, to, rtype, quote,
                              context=""):
        return True, 0.95


class TestGuardInReduce(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        for c in pp._DM_BUDGET_ORDER:
            pp._dm_budget_finish(c)
        pp._DECISION_VALIDATOR = _KeepValidator()

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_inverted_pair_is_flagged_and_review_required(self):
        # Fen claimed as Xaden's parent in ch1 and as Xaden's
        # child in ch2 (the run-23 shape, canonicalized): one
        # winner, flagged, and NOT shipped as validated even
        # though the judge kept the direction it was shown.
        red = _reduce({
            1: [("Fen Riorson", "Xaden Riorson", "parent", "ev1")],
            2: [("Fen Riorson", "Xaden Riorson", "child", "ev2")],
        })
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertIn("direction_contradiction", rel)
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["validation_state"], "review_required")
        self.assertEqual(len(red["direction_contradictions"]), 1)

    def test_both_claimed_as_child_is_flagged(self):
        # A -> B child while B -> A child: canonicalizes to one
        # parent and one child claim for the pair — the second
        # contradiction shape in the brief.
        red = _reduce({
            1: [("Fen Riorson", "Xaden Riorson", "child", "ev1")],
            2: [("Xaden Riorson", "Fen Riorson", "child", "ev2")],
        })
        self.assertEqual(len(red["relationships"]), 1)
        self.assertIn("direction_contradiction",
                      red["relationships"][0])
        self.assertEqual(len(red["direction_contradictions"]), 1)

    def test_legitimate_pair_validates_and_is_unflagged(self):
        red = _reduce({
            1: [("Fen Riorson", "Xaden Riorson", "parent", "ev1")],
            2: [("Xaden Riorson", "Fen Riorson", "child", "ev2")],
        })
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertNotIn("direction_contradiction", rel)
        self.assertEqual(rel["validation_state"], "validated")
        self.assertEqual(red["direction_contradictions"], [])


class TestEvalCases(unittest.TestCase):
    """The inversion shapes live in the gated eval fixture."""

    @classmethod
    def setUpClass(cls):
        path = (Path(__file__).resolve().parent.parent / "eval"
                / "relationship_cases.json")
        cls.cases = json.loads(path.read_text(encoding="utf-8"))

    def _find(self, frm, to, t):
        return [c for c in self.cases
                if (c["from"], c["to"], c["type"]) == (frm, to, t)]

    def test_fen_inversion_is_a_gated_drop(self):
        found = self._find("Fen Riorson", "Xaden Riorson", "child")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["expect"], "drop")
        self.assertLess(found[0]["stub_score"],
                        pp._REL_DROP_THRESHOLD)

    def test_lilith_inversion_is_a_gated_drop(self):
        found = self._find("Lilith Sorrengail", "Violet Sorrengail",
                           "child")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["expect"], "drop")
        self.assertLess(found[0]["stub_score"],
                        pp._REL_DROP_THRESHOLD)

    def test_lilith_correct_direction_companion_keeps(self):
        found = self._find("Lilith Sorrengail", "Violet Sorrengail",
                           "parent")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["expect"], "keep")
        self.assertGreaterEqual(found[0]["stub_score"],
                                pp._REL_VALIDATION_THRESHOLD)
        # Same quote as the inversion case: the pair separates a
        # direction failure from an evidence failure.
        inv = self._find("Lilith Sorrengail", "Violet Sorrengail",
                         "child")
        self.assertEqual(found[0]["quote"], inv[0]["quote"])


if __name__ == "__main__":
    unittest.main()
