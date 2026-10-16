"""Measurement spine, slice 6: the review fixture builder + scorer
(spine-design §3.6).

Pins: both seat-file formats parse (per-candidate lines, the
claim-ID table, the claim-ID list, escalation ID links); one entry
per (work, claim) with the LATEST seat verdict winning and flips
retained; ABSTAINs counted but excluded from the scored fixture;
the structural classes are the headline and the fine distinctions
are labelled interpretation-only (OC-8); the scorer applies the
eval/README states (a no_evidence-style produced-but-unshipped
claim counts as dropped) and names every structural REJECT that
shipped (target zero).
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load("build_review_fixture", "build_review_fixture.py")
sf = _load("score_review_fixture", "score_review_fixture.py")

SEAT_1 = """# Trope review — 2026-10-10

- Daemon — Enemies to Lovers: REJECT — romance trope on a non-romance book; no central romance arc.
- Artemis: A Novel — Heist: CONFIRM — premise unambiguous: a planned infiltration.
- The Lost Symbol — Sacrifice: ABSTAIN — Peter's suffering is endured/coerced, not clearly voluntary.

## Claim IDs

| claim_id | book | trope | conf | verdict |
|---|---|---|---|---|
| a3ff53a3-aeb0-412b-9e30-1794a2544f72 | Daemon | Enemies to Lovers | 0.85 | REJECT |
"""

SEAT_2 = """# Trope claim review — 2026-10-15

Daemon — Enemies to Lovers: REJECT — Tier 2; rejection-pattern match — romance trope on a non-romance book.
Artemis: A Novel — Heist: ABSTAIN — refs only today; held pending quotes.
The Lost Symbol — Sacrifice: ABSTAIN — stalls on the voluntary-vs-coerced distinction.

## Escalations

- The Lost Symbol — Sacrifice (claim 1acee5b8-f571-406f-b7c5-ad2a2e0e96e3) — abstain count 3 prior.

## Claim IDs (for acting on recommendations)

- a3ff53a3-aeb0-412b-9e30-1794a2544f72 — Daemon — Enemies to Lovers — 0.85 — REJECT (rejection pattern)
"""


class TestBuilder(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        (self.dir / "2026-10-10-recommendations.md").write_text(
            SEAT_1, encoding="utf-8")
        (self.dir / "2026-10-15-recommendations.md").write_text(
            SEAT_2, encoding="utf-8")
        self._saved = bf._kevin_verdicts
        bf._kevin_verdicts = lambda: ({}, {}, "not configured (test)")

    def tearDown(self):
        bf._kevin_verdicts = self._saved
        self._td.cleanup()

    def test_latest_wins_flips_and_abstain_exclusion(self):
        fixture, report = bf.build_fixture(self.dir)
        self.assertEqual(report["scored"], 1)
        self.assertEqual(report["abstains_excluded"], 2)
        (entry,) = fixture["entries"]
        self.assertEqual(entry["book"], "Daemon")
        self.assertEqual(entry["verdict"], "REJECT")
        self.assertEqual(entry["authority"], "reviewer")
        self.assertEqual(entry["claim_ids"],
                         ["a3ff53a3-aeb0-412b-9e30-1794a2544f72"])
        self.assertEqual(len(entry["appearances"]), 2)
        self.assertEqual(entry["failure_class"], "genre-mismatch")
        self.assertFalse(entry["interpretation_only"])
        # Artemis flipped CONFIRM -> ABSTAIN: excluded, flip kept.
        artemis = [a for a in fixture["abstains"]
                   if a["book"].startswith("Artemis")]
        self.assertEqual(len(artemis), 1)
        self.assertEqual(artemis[0]["flips"], 1)
        # Sacrifice stalls on a fine distinction: interpretation.
        sac = [a for a in fixture["abstains"]
               if a["trope"] == "Sacrifice"][0]
        self.assertEqual(sac["failure_class"],
                         "voluntary-vs-coerced")
        self.assertTrue(sac["interpretation_only"])
        self.assertEqual(
            sac["claim_ids"],
            ["1acee5b8-f571-406f-b7c5-ad2a2e0e96e3"])

    def test_kevin_verdict_beats_seat_and_records_disagreement(self):
        bf._kevin_verdicts = lambda: (
            {"a3ff53a3-aeb0-412b-9e30-1794a2544f72": "CONFIRM"}, {},
            "2 curated verdicts")
        fixture, _ = bf.build_fixture(self.dir)
        (entry,) = [e for e in fixture["entries"]
                    if e["book"] == "Daemon"]
        self.assertEqual(entry["verdict"], "CONFIRM")
        self.assertEqual(entry["authority"], "kevin")
        self.assertEqual(entry["seat_verdict"], "REJECT")
        self.assertTrue(entry["disagreement"])


class TestScorer(unittest.TestCase):
    def _fixture(self):
        return {"entries": [
            {"book": "Daemon", "trope": "Enemies to Lovers",
             "verdict": "REJECT", "failure_class": "genre-mismatch",
             "interpretation_only": False},
            {"book": "Daemon", "trope": "Mentorship",
             "verdict": "REJECT", "failure_class": "toc-as-evidence",
             "interpretation_only": False},
            {"book": "Daemon", "trope": "Revenge",
             "verdict": "REJECT",
             "failure_class": "premise-judgment",
             "interpretation_only": False},
        ]}

    def test_states_and_headline(self):
        preview = {"title": "Daemon",
                   "tropes": [{"name": "Enemies to Lovers"}],
                   "staging": {"staged": [{"display": "Mentorship"}]},
                   "trope_candidate_counts": {"revenge": 2,
                                              "mentorship": 1}}
        report = sf.score_fixture(self._fixture(),
                                  {sf._norm("Daemon"): preview})
        states = {r["trope"]: r["state"] for r in report["entries"]}
        self.assertEqual(states["Enemies to Lovers"], "shipped")
        self.assertEqual(states["Mentorship"], "staged")
        self.assertEqual(states["Revenge"], "dropped")
        self.assertEqual(
            report["headline"]["structural_reject_shipped"],
            ["Daemon — Enemies to Lovers"])
        self.assertEqual(
            report["headline"]["regenerated_rejected_count"], 3)


class TestRealSeatFiles(unittest.TestCase):
    """The honest-n pin: the real seats yield a few dozen scored
    entries at most — most lines are re-reported holds. If this
    ever reads ~150, the dedupe is broken, not the corpus big."""

    def test_real_fixture_n_is_honest(self):
        seat = Path.home() / "workspace" / "trope-review"
        if not seat.is_dir():
            self.skipTest("seat files not present on this machine")
        saved = bf._kevin_verdicts
        bf._kevin_verdicts = lambda: ({}, {}, "skipped (test)")
        try:
            fixture, report = bf.build_fixture(seat)
        finally:
            bf._kevin_verdicts = saved
        self.assertGreaterEqual(report["scored"], 10)
        self.assertLessEqual(report["scored"], 60)
        self.assertEqual(report["unresolved"], [])


if __name__ == "__main__":
    unittest.main()
