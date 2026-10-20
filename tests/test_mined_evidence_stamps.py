"""Mined-evidence claims must carry truthful evidence stamps.

Motivation (quote-flow audit 2026-10-20, drop point D3): when the
post-validation miner attaches a verbatim sentence to a surviving
claim whose extraction offered no quote, it set `evidence`,
`evidence_source` and `evidence_grade` — but never updated
`evidence_offered` or `evidence_count`. Those stamps still described
the pre-mining state ("extraction offered nothing, support 0"), so
every mined claim presented as unevidenced in the preview and in
run-history claim rows while shipping a real quote: 67 of 189
validated claims across the previews on file, 63 of them with
`evidence_count == 0`.

The fix propagates the stamps at the attach point: the claim now
offers a quote, so `evidence_offered` is True, and the support count
floors at 1 — the attached quote is one real supporting quote, so a
claim shipping one can no longer report zero (an existing count from
competition-time candidates is kept, not incremented: those
candidates were already counted).

`evidence_verified` is deliberately NOT touched here: mined text
never passed verify_all_evidence, and whether it may carry the
verified stamp is a separate decision this fix does not make.

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
        self.calls = []

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls.append((frm, to, rtype, quote))
        return True, self.p


class TestMinedEvidenceStamps(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _CapturingValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_family_mined_claim_stamps_follow_the_quote(self):
        # Binding-grade mine routes the evidence-less family claim
        # into validation; the miner then attaches the sentence as
        # the write evidence. The stamps must describe THAT state.
        text = ("Fen Riorson was Xaden's father, and the rebellion "
                "died with him.")
        chs = _chs(text)
        chapter_as = _chapter_a([{
            "from": "Fen Riorson", "to": "Xaden Riorson",
            "type": "parent", "evidence": ""}])
        roster = _roster("Fen Riorson", "Xaden Riorson")
        red = pp.v2_reduce(chapter_as, {}, roster, chs, preview=False)
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(rel["evidence"], text)
        self.assertEqual(rel["evidence_source"], "mined")
        # The claim ships a quote, so it no longer reports that
        # nothing was offered and zero quotes support it.
        self.assertTrue(rel["evidence_offered"])
        self.assertEqual(rel["evidence_count"], 1)
        # Verified is a different promise (the mined sentence never
        # passed verify_all_evidence) and stays untouched.
        self.assertFalse(rel["evidence_verified"])

    def test_nonfamily_mined_claim_stamps_follow_the_quote(self):
        # Same attach on the non-family path: the validator judges
        # the mined sentence, the claim keeps, and the miner fills
        # the write evidence afterwards.
        text = "Fen Riorson met Xaden Riorson at the wall before dawn."
        chs = _chs(text)
        chapter_as = _chapter_a([{
            "from": "Fen Riorson", "to": "Xaden Riorson",
            "type": "friend", "evidence": ""}])
        roster = _roster("Fen Riorson", "Xaden Riorson")
        red = pp.v2_reduce(chapter_as, {}, roster, chs, preview=False)
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(rel["evidence"], text)
        self.assertEqual(rel["evidence_source"], "mined")
        self.assertTrue(rel["evidence_offered"])
        self.assertEqual(rel["evidence_count"], 1)
        self.assertFalse(rel["evidence_verified"])

    def test_extracted_claim_stamps_unchanged(self):
        # Control: a claim that ships its extracted quote never
        # passes the attach block, and its stamps keep their
        # competition-time values (offered, competition count).
        quote = "Fen Riorson raised Xaden Riorson as his own son."
        text = quote + " The rebellion took everything else."
        chs = _chs(text)
        chapter_as = _chapter_a([{
            "from": "Fen Riorson", "to": "Xaden Riorson",
            "type": "parent", "evidence": quote}])
        roster = _roster("Fen Riorson", "Xaden Riorson")
        red = pp.v2_reduce(chapter_as, {}, roster, chs, preview=False)
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(rel["evidence"], quote)
        self.assertNotEqual(rel.get("evidence_source"), "mined")
        self.assertTrue(rel["evidence_offered"])
        self.assertEqual(rel["evidence_count"], 1)


if __name__ == "__main__":
    unittest.main()
