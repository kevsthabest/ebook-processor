"""Family fail-closed: an evidence-less family claim drops, never survives.

Motivation (Fourth Wing -14 live run, 2026-10): the labelled control
set exposed a leak. Family-typed claims (spouse/parent/child/sibling)
with NO evidence quote bypassed the decision-model validator and
default-downgraded to "other" (validation_note="no_evidence"),
surviving unjudged. In -13 the same false claims carried junk quotes,
were validated, and dropped — survival depended on whether extraction
happened to attach a quote.

The rule now: a family claim with no evidence quote AND no
binding-grade mined sentence DROPS at validation. It lands in
dropped_relationships with its ORIGINAL family type and
validation_note="no_evidence" (location + note carry the meaning).
A binding-grade mine still routes the claim into validation
(pair-evidence behaviour unchanged). Claims WITH evidence are
untouched, including the budget boundary: a quoted claim whose
validation is skipped because the decision-model budget is exhausted
keeps its type with validation_note="budget_exhausted" — "validator
wasn't consulted" is never conflated with "no evidence".

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _chs(*texts):
    return [{"index": i + 1, "label": f"Ch{i + 1}", "text": t}
            for i, t in enumerate(texts)]


def _chapter_a(relationships):
    return {1: {"characters": [], "relationships": relationships,
                "task_status": {"identity": "ok"}}}


def _roster(*names):
    """Roster in _roster_update shape (the miner builds its endpoint
    name forms from roster entries, so mining tests need real ones)."""
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
        self.calls = []  # (from, to, type, quote)

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls.append((frm, to, rtype, quote))
        return True, self.p


class TestFamilyFailClosed(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _CapturingValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_evidenceless_family_drops_without_call(self):
        # No quote, chapter text names neither endpoint (mine finds
        # nothing): the claim drops unjudged — it does NOT survive as
        # a downgrade, and no decision-model call is spent on it.
        chs = _chs("x " * 200, "y " * 200)
        chapter_as = _chapter_a([{
            "from": "Alice", "to": "Bob", "type": "spouse",
            "evidence": ""}])
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(self.validator.calls, [])
        self.assertEqual(red["relationships"], [])
        dropped = red["dropped_relationships"]
        self.assertEqual(len(dropped), 1)
        # Original family type stays visible; location + note carry
        # the meaning.
        self.assertEqual(dropped[0]["type"], "spouse")
        self.assertEqual(dropped[0]["validation_note"], "no_evidence")
        self.assertIsNone(dropped[0]["validation_p"])

    def test_evidenceless_family_binding_mine_routes_to_validation(self):
        # Pair-evidence behaviour unchanged: a binding-grade mined
        # sentence routes an evidence-less family claim INTO
        # validation, and the validator judges the mined sentence.
        text = ("Fen Riorson was Xaden's father, and the rebellion "
                "died with him.")
        chs = _chs(text)
        chapter_as = _chapter_a([{
            "from": "Fen Riorson", "to": "Xaden Riorson",
            "type": "parent", "evidence": ""}])
        roster = _roster("Fen Riorson", "Xaden Riorson")
        red = pp.v2_reduce(chapter_as, {}, roster, chs, preview=False)
        self.assertEqual(len(self.validator.calls), 1)
        self.assertEqual(self.validator.calls[0][3], text)
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "parent")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["evidence_grade"], "binding")
        self.assertEqual(red["dropped_relationships"], [])

    def test_evidenceless_family_drops_even_when_budget_exhausted(self):
        # The budget boundary, from the evidence-less side: with the
        # budget drained, an evidence-less family claim still drops
        # with note "no_evidence" — never "budget_exhausted". The
        # fail-closed trigger is evidence-lessness ONLY. (The other
        # side — quoted claim + exhausted budget survives with
        # "budget_exhausted" — is pinned in test_budget.py's
        # TestBudgetExhaustionNote.)
        for _ in range(pp._MERGE_TIER3_CAP):
            self.assertTrue(pp._dm_budget_claim(1))
        self.assertFalse(pp._dm_budget_claim(1))
        chs = _chs("x " * 200, "y " * 200)
        chapter_as = _chapter_a([{
            "from": "Alice", "to": "Bob", "type": "parent",
            "evidence": ""}])
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(self.validator.calls, [])
        self.assertEqual(red["relationships"], [])
        dropped = red["dropped_relationships"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["type"], "parent")
        self.assertEqual(dropped[0]["validation_note"], "no_evidence")

    def test_evidenceless_nonfamily_survives_unchanged(self):
        # Non-family claims are untouched in all paths: an
        # evidence-less friend claim with no mineable sentence
        # survives exactly as before (it is dealt with at write,
        # not by family validation).
        chs = _chs("x " * 200, "y " * 200)
        chapter_as = _chapter_a([{
            "from": "Alice", "to": "Bob", "type": "friend",
            "evidence": ""}])
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(self.validator.calls, [])
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "friend")
        self.assertNotIn("validation_note", rel)
        self.assertEqual(red["dropped_relationships"], [])

    def test_evidenceless_family_preview_records_without_dropping(self):
        # Preview mode records but changes nothing (the standing
        # preview contract): the claim stays in relationships with
        # its original type and the no_evidence note; the drop lands
        # when the same input runs in write mode (pinned above).
        chs = _chs("x " * 200, "y " * 200)
        chapter_as = _chapter_a([{
            "from": "Alice", "to": "Bob", "type": "spouse",
            "evidence": ""}])
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=True)
        self.assertEqual(self.validator.calls, [])
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "spouse")
        self.assertEqual(rel["validation_note"], "no_evidence")
        self.assertFalse(any(
            d.get("validation_note") == "no_evidence"
            for d in red["dropped_relationships"]))


if __name__ == "__main__":
    unittest.main()
