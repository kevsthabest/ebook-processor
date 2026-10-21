"""--jev-merge bake-off: the decision model replaces both algorithmic
merge stages (roster reconcile + post_pass_merge) for one run.

Design (user-ordered experiment, 2026-10): Call A leaves a raw roster
whose alias claims are still DATA. Deterministic signals nominate
candidate pairs, recorded human NOs (RosterStore no_pairs) veto pairs
outright, and the decision model judges each remaining pair from an
evidence packet (name forms, chapter counts, verbatim snippets).
Only YES verdicts merge — fail-closed: a judge error, an unparseable
answer, a denied budget claim or a disabled model all leave the pair
split and the run continues.

Hermetic: the judge is a transport double implementing the
DecisionValidator packet interface; no network, no Supabase.
Stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import re
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _entry(name, aliases=(), chapters=(0,), appearances=3, evidence="",
           descriptions=()):
    """A roster entry in the shape _roster_update produces."""
    return {
        "name": name,
        "aliases": set(aliases),
        "alias_keys": {pp.norm_name(a) for a in aliases},
        "primary_keys": {pp.norm_name(name)},
        "appearances": appearances,
        "chapters": set(chapters),
        "roles": Counter(),
        "descriptions": list(descriptions),
        "evidence": evidence,
        "statuses": Counter(),
        "death_reports": [],
        "alive_reports": set(),
        "unresolved_mentions": set(),
        "appearances_desc": [],
        "_generic_keys": set(),
    }


def _roster(*entries):
    return {pp.norm_name(e["name"]): e for e in entries}


_CHAPTERS = [
    {"index": 0, "label": "Ch 1",
     "text": "Heinrich Boerner waited by the gate. Boerner checked his "
             "watch twice before the train arrived."},
    {"index": 1, "label": "Ch 2",
     "text": "Major Pain in the Ass logged into the game at midnight. "
             "Across the city, Karl Devon briefed the Pentagon staff. "
             "Devon frowned at the report."},
    {"index": 2, "label": "Ch 3",
     "text": "Ann Lee met Lee Park at the archive. Ann Lee Park signed "
             "the register before leaving."},
]


class _FakeValidator:
    """Decision-model double. Answers are keyed by the unordered pair
    of display names parsed out of the packet's Character A/B lines,
    so a test can only pass if the packet really carries both names."""

    disabled = False

    def __init__(self, answers=None, default=(False, 0.10)):
        self.answers = answers or {}
        self.default = default
        self.states = []

    def ask_same_person_packet(self, state):
        self.states.append(state)
        a = re.search(r"^Character A: (.+)$", state, re.M).group(1)
        b = re.search(r"^Character B: (.+)$", state, re.M).group(1)
        ans = self.answers.get(frozenset((a, b)), self.default)
        if ans == "raise":
            raise RuntimeError("transport down")
        return ans


class _Base(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()


class TestJudgeVerdicts(_Base):
    def test_yes_verdict_unites_split_pair(self):
        """Boerner shape: a bare surname entry and its full-name entry
        are nominated on the shared token; a YES folds them into the
        fullest form."""
        roster = _roster(
            _entry("Boerner", chapters=(0,), appearances=5),
            _entry("Heinrich Boerner", chapters=(0,), appearances=3))
        judge = _FakeValidator(
            answers={frozenset(("Boerner", "Heinrich Boerner")):
                     (True, 0.93)})
        diag = pp.jev_merge_pass(roster, _CHAPTERS, validator=judge)
        self.assertEqual(sorted(roster), ["heinrich boerner"])
        self.assertEqual(roster["heinrich boerner"]["appearances"], 8)
        self.assertEqual(diag["pairs_nominated"], 1)
        self.assertEqual(diag["pairs_judged"], 1)
        self.assertEqual(diag["merges_applied"], 1)
        self.assertEqual(diag["clusters_formed"], 1)
        self.assertEqual(diag["verdicts"][0]["verdict"], "yes")
        self.assertEqual(diag["verdicts"][0]["confidence"], 0.93)

    def test_no_verdict_keeps_pair_split_aliases_untouched(self):
        """Devon / Major Pain shape: the pair IS nominated (the alias
        form 'Major Devon' shares the title token with 'Major Pain in
        the Ass'), the judge says NO, and nothing folds."""
        roster = _roster(
            _entry("Karl Devon", aliases=("The Major", "Major Devon"),
                   chapters=(1,)),
            _entry("Major Pain in the Ass", aliases=("MPITA",),
                   chapters=(1,)))
        judge = _FakeValidator(default=(False, 0.22))
        diag = pp.jev_merge_pass(roster, _CHAPTERS, validator=judge)
        self.assertEqual(diag["pairs_nominated"], 1)
        self.assertEqual(diag["verdicts"][0]["verdict"], "no")
        self.assertEqual(diag["merges_applied"], 0)
        self.assertEqual(sorted(roster),
                         ["karl devon", "major pain in the ass"])
        self.assertEqual(roster["karl devon"]["aliases"],
                         {"The Major", "Major Devon"})
        self.assertEqual(roster["major pain in the ass"]["aliases"],
                         {"MPITA"})

    def test_transitive_yes_chain_merges_one_cluster_once(self):
        """A-B yes and B-C yes unite all three even when A-C is judged
        NO: the cluster forms once, with two folds, under the fullest
        form."""
        roster = _roster(
            _entry("Ann Lee", chapters=(2,), appearances=4),
            _entry("Ann Lee Park", chapters=(2,), appearances=2),
            _entry("Lee Park", chapters=(2,), appearances=3))
        judge = _FakeValidator(answers={
            frozenset(("Ann Lee", "Ann Lee Park")): (True, 0.91),
            frozenset(("Ann Lee Park", "Lee Park")): (True, 0.88),
            frozenset(("Ann Lee", "Lee Park")): (False, 0.30)})
        diag = pp.jev_merge_pass(roster, _CHAPTERS, validator=judge)
        self.assertEqual(sorted(roster), ["ann lee park"])
        self.assertEqual(diag["clusters_formed"], 1)
        self.assertEqual(diag["merges_applied"], 2)
        self.assertEqual(diag["pairs_judged"], 3)


class TestFailClosed(_Base):
    def _devon_roster(self):
        return _roster(
            _entry("Karl Devon", aliases=("Major Devon",), chapters=(1,)),
            _entry("Major Pain in the Ass", aliases=("MPITA",),
                   chapters=(1,)))

    def test_judge_raising_leaves_pair_split_and_pass_returns(self):
        roster = self._devon_roster()
        judge = _FakeValidator(default="raise")
        diag = pp.jev_merge_pass(roster, _CHAPTERS, validator=judge)
        self.assertEqual(len(roster), 2)
        self.assertEqual(diag["merges_applied"], 0)
        self.assertEqual(diag["verdicts"][0]["verdict"], "error")
        self.assertIsNone(diag["verdicts"][0]["confidence"])

    def test_judge_returning_none_is_an_error_not_a_no(self):
        """(None, None) is the transport's error shape; it must record
        as an error verdict, never as evidence the pair is split."""
        roster = self._devon_roster()
        judge = _FakeValidator(default=(None, None))
        diag = pp.jev_merge_pass(roster, _CHAPTERS, validator=judge)
        self.assertEqual(len(roster), 2)
        self.assertEqual(diag["verdicts"][0]["verdict"], "error")

    def test_no_validator_skips_everything(self):
        roster = self._devon_roster()
        diag = pp.jev_merge_pass(roster, _CHAPTERS, validator=None)
        self.assertEqual(len(roster), 2)
        self.assertEqual(diag["pairs_judged"], 0)
        self.assertEqual(diag["verdicts"][0]["verdict"], "skipped")


class TestCannotLinkVeto(_Base):
    def test_vetoed_pair_never_reaches_the_judge(self):
        """A recorded human NO removes the pair before judging: the
        judge (which would say YES to anything) is never asked, and
        the entries stay split."""
        roster = _roster(
            _entry("Karl Devon", aliases=("Major Devon",), chapters=(1,)),
            _entry("Major Pain in the Ass", aliases=("MPITA",),
                   chapters=(1,)))
        judge = _FakeValidator(default=(True, 0.99))
        diag = pp.jev_merge_pass(
            roster, _CHAPTERS, validator=judge,
            no_pairs=[("karl devon", "major pain in the ass")])
        self.assertEqual(judge.states, [])
        self.assertEqual(diag["pairs_judged"], 0)
        self.assertEqual(len(diag["vetoed"]), 1)
        self.assertEqual(diag["vetoed"][0]["no"],
                         ["karl devon", "major pain in the ass"])
        self.assertEqual(len(roster), 2)
        self.assertEqual(diag["merges_applied"], 0)

    def test_veto_survives_transitive_union(self):
        """A YES chain through a third entry must not smuggle a
        vetoed pair into one cluster: the union that would join the
        NO's two sides is refused, the clean union still applies."""
        roster = _roster(
            _entry("Karl Devon", chapters=(1,), appearances=5),
            _entry("Major Devon", chapters=(1,), appearances=2),
            _entry("Major Pain in the Ass", aliases=("MPITA",),
                   chapters=(1,)))
        judge = _FakeValidator(default=(True, 0.95))
        diag = pp.jev_merge_pass(
            roster, _CHAPTERS, validator=judge,
            no_pairs=[("karl devon", "major pain in the ass")])
        self.assertEqual(diag["transitive_veto_blocks"], 1)
        self.assertEqual(diag["merges_applied"], 1)
        self.assertEqual(len(roster), 2)
        self.assertIn("major pain in the ass", roster)
        devon_side = [k for k in roster if "devon" in k]
        self.assertEqual(len(devon_side), 1)
        self.assertEqual(
            roster[devon_side[0]]["appearances"], 7)


class TestNomination(_Base):
    def test_alias_claim_nominates(self):
        """An entry claiming another entry's key as an alias nominates
        the pair even with no shared name token."""
        a = _entry("Zelda Voss", chapters=(0,))
        b = _entry("Miriam Okafor", chapters=(0,))
        b["alias_keys"].add(pp.norm_name("Zelda Voss"))
        roster = _roster(a, b)
        pairs = pp._jev_merge_nominate(roster)
        self.assertIn(("miriam okafor", "zelda voss"), pairs)

    def test_unrelated_entries_not_nominated(self):
        roster = _roster(
            _entry("Zelda Voss", chapters=(0,)),
            _entry("Miriam Okafor", chapters=(0,)))
        self.assertEqual(pp._jev_merge_nominate(roster), {})

    def test_packet_carries_forms_chapters_and_snippets(self):
        roster = _roster(
            _entry("Karl Devon", aliases=("The Major",),
                   chapters=(1,), appearances=4),
            _entry("Major Pain in the Ass", aliases=("MPITA",),
                   chapters=(1,)))
        judge = _FakeValidator(default=(False, 0.1))
        pp.jev_merge_pass(roster, _CHAPTERS, validator=judge)
        self.assertEqual(len(judge.states), 1)
        state = judge.states[0]
        self.assertIn("Name forms A:", state)
        self.assertIn("The Major", state)
        self.assertIn("Chapters A: 1", state)
        self.assertIn("Karl Devon briefed the Pentagon staff", state)
        self.assertIn("Major Pain in the Ass logged into the game",
                      state)


class TestFlagWiring(unittest.TestCase):
    def test_flag_defaults_off_and_parses(self):
        parser = pp._build_parser()
        self.assertFalse(parser.parse_args([]).jev_merge)
        self.assertTrue(parser.parse_args(["--jev-merge"]).jev_merge)

    def test_packet_method_uses_production_instructions(self):
        """The bake-off judge call is the production same-person
        question: same question key, same instruction text as
        ask_same_person (the eval harness pins that text)."""
        seen = {}

        def fake_ask(self, question_key, instructions, state,
                     label="decision"):
            seen.update(key=question_key, instructions=instructions,
                        state=state)
            return 0.9

        validator = pp.DecisionValidator("http://127.0.0.1:9/v1")
        with mock.patch.object(
                pp.DecisionValidator, "_ask_noul", fake_ask):
            merged, p = validator.ask_same_person_packet("PACKET")
        self.assertEqual(seen["key"], "same_person")
        self.assertEqual(seen["instructions"], pp._SAME_PERSON_INSTRUCTIONS)
        self.assertEqual(seen["state"], "PACKET")
        self.assertTrue(merged)
        self.assertEqual(p, 0.9)


if __name__ == "__main__":
    unittest.main()
