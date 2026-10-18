"""Revival draws may borrow the non-family stage's unused reserve
(v2.13.34; the Security Advisor gate's wrinkle on v2.13.32).

Runner-up revival (v2.13.32) runs after both validation passes:
a pair whose winner was rejected gets one revival through the
normal path for its class. But the revival loop originally sat
BEFORE _dm_budget_finish("rel_nonfamily") in v2_reduce, so a
revived FAMILY claim drew while the non-family stage was still
marked unfinished. Under the section 1.3 borrowing rules a
consumer may borrow an unused reserve only once the owning
stage has finished, so the non-family reserve's unused remainder
was still protected from the revival draw: the revival could be
budget-denied (shipping review_required) while up to five
reserve calls sat provably unused. The Decision therefore
ordered the bookkeeping, not the accounting: the non-family
stage is marked finished BEFORE the revival loop runs, so
revival draws see the post-finish pool state the reserve rules
already intend. Totals, reserves, and borrowing semantics are
unchanged; the tests below pin both sides of that line — the
unused reserve is released, a genuinely consumed reserve is
not conjured back, and a book with no revival accounts exactly
as before.

The arithmetic the seam tests lean on (floor pool, no chapter
count armed): the pool is 50, the reserves sum to 41, and once
the merge stage has finished — the real pipeline order, which
these tests reproduce by finishing merge_tier3 before the
reduce — a family claimant is limited to 50 minus the still-
protected reserves of the later stages. Before the non-family
finish that protection is non-family 5 + trigger 15 +
quote_poll 1 = 21, so the family limit is 29; after it, only
trigger + quote_poll remain protected (16), so the limit is
34. Twenty-nine family draws therefore saturate the pre-fix
revival limit exactly, and the five released calls are exactly
the non-family reserve.

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

_FILLER = "x " * 200


def _chs():
    return [{"index": 1, "label": "Ch1", "text": _FILLER}]


class _SeamValidator:
    """Stub judge for the seam tests: the Anna/Ben spouse claim
    is firmly rejected, its sibling runner-up and every filler
    claim are kept. Every call is recorded so a test can prove
    whether the revival claim ever reached the judge."""
    model = "stub"

    def __init__(self):
        self.calls = []  # (from, to, type)

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls.append((frm, to, rtype))
        if (frm, to, rtype) == ("Anna", "Ben", "spouse"):
            return True, 0.1
        if rtype == "sibling":
            return True, 0.9
        return True, 0.95


def _chapter_as(n_family_fillers, n_nonfamily_fillers,
                include_dead_pair=True):
    """One chapter carrying the dead pair plus filler winners.

    The dead pair is Anna/Ben: a spouse claim the judge drops
    and a sibling claim that loses the pair competition (same
    chapter, listed second) and so becomes the revival
    candidate — a FAMILY claim, which is the class the gate's
    wrinkle bites. Filler spouse pairs saturate the family
    stage; filler friend pairs are the non-family stage's
    genuine demand."""
    rels = []
    if include_dead_pair:
        rels.append({"from": "Anna", "to": "Ben", "type": "spouse",
                     "evidence": "Anna and Ben exchanged vows."})
        rels.append({"from": "Anna", "to": "Ben", "type": "sibling",
                     "evidence": "Anna called Ben her brother."})
    for i in range(n_family_fillers):
        rels.append({"from": f"Alpha{i}", "to": f"Beta{i}",
                     "type": "spouse",
                     "evidence": "an evidenced spouse claim"})
    for i in range(n_nonfamily_fillers):
        rels.append({"from": f"Gamma{i}", "to": f"Delta{i}",
                     "type": "friend",
                     "evidence": "an evidenced friend claim"})
    return {1: {"characters": [], "relationships": rels,
                "task_status": {"identity": "ok"}}}


def _pair_rels(red, frm, to):
    names = {frm, to}
    return [r for r in red["relationships"]
            if {r.get("from"), r.get("to")} == names]


class TestRevivalBudgetBorrowing(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        # The real pipeline finishes the merge stage before the
        # reduce runs (post_pass_merge), so the seam tests arm
        # that same state: borrowing is unlocked for validation,
        # and the family limit is the one the header derives.
        pp._dm_budget_finish("merge_tier3")
        self.validator = _SeamValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def _reduce(self, chapter_as):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            red = pp.v2_reduce(chapter_as, {}, {}, _chs(),
                               preview=False)
        return red, buf.getvalue()

    def test_revived_family_claim_borrows_unused_nonfamily_reserve(self):
        # The gate's scenario: 29 family winners are judged (the
        # dropped Anna/Ben spouse plus 28 filler spouses) and the
        # non-family stage presents NO claim at all, so its whole
        # 5-call reserve is provably unused. The revived sibling
        # is the 30th family draw: denied pre-fix, because the
        # family limit was still 29 with the non-family stage
        # unfinished (it shipped review_required and the judge
        # was never asked); drawn post-fix, because finishing
        # the non-family stage first raises the family limit to
        # 34 and the claim ships with the judge's verdict.
        red, out = self._reduce(_chapter_as(28, 0))
        rels = _pair_rels(red, "Anna", "Ben")
        self.assertEqual(len(rels), 1)
        rel = rels[0]
        self.assertEqual(rel["type"], "sibling")
        self.assertEqual(rel["validation_state"], "validated")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["validation_p"], 0.9)
        self.assertTrue(rel["revived_runner_up"])
        self.assertEqual(rel["revived_over_type"], "spouse")
        self.assertEqual(rel["revived_over_fate"], "dropped")
        # The judge was asked about the sibling exactly once —
        # the revival draw really happened, it was not waved in.
        self.assertEqual(
            [c for c in self.validator.calls if c[2] == "sibling"],
            [("Anna", "Ben", "sibling")])
        # Accounting: thirty family draws granted, none denied,
        # and no non-family claim was ever made.
        self.assertEqual(pp._DM_BUDGET_USED, 30)
        self.assertEqual(pp._dm_budget_snapshot(), {
            "rel_family": {"granted": 30, "denied": 0, "spent": 30}})
        self.assertIn("outcome: kept", out)

    def test_revival_still_denied_when_nonfamily_reserve_was_consumed(self):
        # Pin: the fix releases only reserve that is provably
        # unused. Here the non-family stage genuinely spends its
        # reserve — after the same 29 family draws, five friend
        # winners are judged, bringing the book to 34 spent, the
        # post-finish family limit itself. The revived sibling
        # is therefore still denied, exactly as before the fix,
        # and ships unjudged as review_required.
        red, out = self._reduce(_chapter_as(28, 5))
        rels = _pair_rels(red, "Anna", "Ben")
        self.assertEqual(len(rels), 1)
        rel = rels[0]
        self.assertEqual(rel["type"], "sibling")
        self.assertEqual(rel["validation_state"], "review_required")
        self.assertNotIn("validation_p", rel)
        self.assertTrue(rel["revived_runner_up"])
        self.assertEqual(
            [c for c in self.validator.calls if c[2] == "sibling"], [])
        self.assertEqual(pp._DM_BUDGET_USED, 34)
        self.assertEqual(pp._dm_budget_snapshot(), {
            "rel_family": {"granted": 29, "denied": 1, "spent": 29},
            "rel_nonfamily": {"granted": 5, "denied": 0, "spent": 5}})
        self.assertIn("outcome: review_required", out)

    def test_no_revival_book_budget_accounting_is_unchanged(self):
        # Pin: moving a finish mark spends nothing, so a book
        # with no dead winner — nothing to revive — must account
        # byte-identically to before the fix. Two family winners
        # and one non-family winner, all kept: the snapshot's
        # exact representation is the pre-fix value, hard-coded
        # here so any drift in totals, per-consumer accounting,
        # or claim order fails this test.
        chapter_as = _chapter_as(2, 1, include_dead_pair=False)
        red, _out = self._reduce(chapter_as)
        self.assertEqual(len(red["relationships"]), 3)
        for rel in red["relationships"]:
            self.assertEqual(rel["validation_note"], "kept")
            self.assertNotIn("revived_runner_up", rel)
        self.assertEqual(pp._DM_BUDGET_USED, 3)
        snapshot = pp._dm_budget_snapshot()
        self.assertEqual(snapshot, {
            "rel_family": {"granted": 2, "denied": 0, "spent": 2},
            "rel_nonfamily": {"granted": 1, "denied": 0, "spent": 1}})
        self.assertEqual(
            repr(snapshot),
            "{'rel_family': {'granted': 2, 'denied': 0, 'spent': 2}, "
            "'rel_nonfamily': {'granted': 1, 'denied': 0, 'spent': 1}}")


if __name__ == "__main__":
    unittest.main()
