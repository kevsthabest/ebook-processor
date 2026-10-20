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

F1b (the owner's ruling that followed): `evidence_verified` is not
true by construction. F1 left the stamp untouched because mined
text never passed verify_all_evidence; the ruling is that mined
quotes run through the same checker as extracted quotes — the
normalized/fuzzy match verify_all_evidence performs, against the
chapter the sentence was mined from. A mined sentence that passes
earns verified=True like any extracted quote; one that fails never
attaches, and the claim keeps the stamps extraction and competition
left, which truthfully describe an evidence-less claim. The two F1
mined tests below therefore assert verified=True (their sentences
are verbatim from their chapters and pass the check), superseding
the F1-era assertFalse pins.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

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


class _DropFirstValidator:
    """Drops the first claim it judges (P below the drop
    threshold), keeps everything after. Validation judges winners
    before any revival runs, so the first call is deterministically
    the winning claim's — the shape the revival test needs."""
    model = "stub"

    def __init__(self):
        self.calls = []

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls.append((frm, to, rtype, quote))
        if len(self.calls) == 1:
            return False, 0.10
        return True, 0.95


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
        # F1b: the mined sentence is verbatim from its chapter, so
        # it passes the same checker extracted evidence passes and
        # earns the verified stamp through the check — never by
        # construction.
        self.assertTrue(rel["evidence_verified"])

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
        # F1b: verbatim from its chapter, the mined sentence passes
        # the checker and is verified through it.
        self.assertTrue(rel["evidence_verified"])

    def test_doctored_mined_sentence_never_attaches(self):
        # F1b failure path: a "mined" sentence that cannot match its
        # source chapter (here the miner is stubbed to hand back
        # text that appears nowhere in the book) must not ship.
        # Mirroring the checker — whose failed quotes are emptied
        # and never become a claim's evidence — the attach refuses
        # the sentence wholesale: the claim keeps the stamps
        # extraction and competition left, which truthfully say it
        # has no evidence. The judge still kept the claim (it judged
        # the sentence it was shown); only the write evidence is
        # refused, so the claim stays preview-only at write.
        text = "Fen Riorson met Xaden Riorson at the wall before dawn."
        chs = _chs(text)
        chapter_as = _chapter_a([{
            "from": "Fen Riorson", "to": "Xaden Riorson",
            "type": "friend", "evidence": ""}])
        roster = _roster("Fen Riorson", "Xaden Riorson")
        doctored = {"sentence": "Fen Riorson swore a blood oath "
                                "against Xaden Riorson beneath a "
                                "black moon.",
                    "grade": "plain", "chapter": 1}
        with mock.patch.object(pp, "_mine_pair_evidence",
                               return_value=doctored):
            red = pp.v2_reduce(chapter_as, {}, roster, chs,
                               preview=False)
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(rel["evidence"], "")
        self.assertNotEqual(rel.get("evidence_source"), "mined")
        self.assertFalse(rel["evidence_verified"])
        # The failed sentence must not raise the stamps F1 would
        # have set for a real attach: nothing was offered that
        # survived, and zero quotes support the claim.
        self.assertFalse(rel["evidence_offered"])
        self.assertEqual(rel["evidence_count"], 0)

    def test_revival_mined_attach_keeps_existing_count(self):
        # The Advisor's F1-gate nit, pinned: a claim that reaches
        # the attach with a competition count already >= 1 keeps
        # that count — the attach floors, it never increments.
        # Construction: the pair's quoted candidate wins, the judge
        # drops it, and the quote-less runner-up revives carrying
        # the type's quoted-candidate count (1) from competition.
        # The miner then fills its write evidence; the count must
        # still read 1, not 2. (This pins F1 behaviour and passes
        # on F1 alone; the F1b verified assertions live in the
        # mined tests above.)
        quote = "Fen Riorson raised Xaden Riorson in the border keep."
        text1 = quote + " The yard was cold that year."
        text2 = "Fen Riorson met Xaden Riorson at the wall before dawn."
        chs = _chs(text1, text2)
        chapter_as = {
            1: {"characters": [], "task_status": {"identity": "ok"},
                "relationships": [{
                    "from": "Fen Riorson", "to": "Xaden Riorson",
                    "type": "friend", "evidence": quote}]},
            2: {"characters": [], "task_status": {"identity": "ok"},
                "relationships": [{
                    "from": "Fen Riorson", "to": "Xaden Riorson",
                    "type": "friend", "evidence": ""}]},
        }
        roster = _roster("Fen Riorson", "Xaden Riorson")
        pp._DECISION_VALIDATOR = _DropFirstValidator()
        red = pp.v2_reduce(chapter_as, {}, roster, chs, preview=False)
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertTrue(rel.get("revived_runner_up"))
        self.assertTrue(rel["evidence"])
        self.assertEqual(rel["evidence_source"], "mined")
        self.assertTrue(rel["evidence_offered"])
        self.assertEqual(rel["evidence_count"], 1)

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
