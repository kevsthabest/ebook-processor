"""The gold scorer's matching rules and its deterministic checks.

eval/daemon-labels.json is the machine form of the frozen Daemon pack;
score-gold.py turns one run document into a comparable number. What the
scorer must never get wrong, and what is pinned here:

- pairs are UNORDERED: a claim shipped in the opposite direction still
  matches its gold entry, and its type is read through _REL_INVERSE
  before comparison (child shipped against a parent entry is a match);
- `acceptable` sets decide correctness (a colleague/enemy pair accepts
  either), defaulting to [type] when the entry declares none;
- precision's denominator is LABELLED claims only — a claim on a pair
  the pack never labelled is ignored, not counted wrong;
- each deterministic check (characters, quotes, tropes, triggers)
  fails on the fixture it exists to catch.
"""

import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pp = _load("process_portable", "process-portable.py")
sg = _load("score_gold", "score-gold.py")


GOLD = {
    "target": {"precision": 0.5, "recall": 0.5},
    "must_ship": [
        {"from": "Ann One", "to": "Ben Two", "type": "enemy"},
        {"from": "Cara Three", "to": "Dan Four", "type": "parent"},
        {"from": "Eve Five", "to": "Finn Six", "type": "ally",
         "acceptable": ["ally", "enemy"]},
    ],
    "must_not_ship": [
        {"from": "Gus Seven", "to": "Hana Eight", "type": "colleague",
         "acceptable": []},
    ],
    "either": [],
    "trope_rulings": {"must_not_ship": ["Revenge"]},
}


def _run(**over):
    run = {
        "characters": [{"name": "Ann One", "aliases": []},
                       {"name": "Ben Two", "aliases": []}],
        "relationships": [],
        "quotes": [],
        "tropes": [],
        "trope_chapters": {},
        "triggers": [],
    }
    run.update(over)
    return run


BOOK_TEXT = ("Ann One walked into the room and found Ben Two waiting for "
             "her. \"You are late,\" she said.")
CHAPTERS = [{"index": 0, "label": "Chapter 1", "text": BOOK_TEXT}]


class RelationshipMatchingTest(unittest.TestCase):
    def test_unordered_pairs_match(self):
        run = _run(relationships=[{"from": "Ben Two", "to": "Ann One",
                                   "type": "enemy"}])
        res = sg.score_relationships(GOLD, run)
        self.assertEqual(res["must_ship_hits"], 1)
        self.assertEqual(res["labelled"], 1)
        self.assertEqual(res["correct"], 1)

    def test_reversed_pair_reads_type_through_inverse(self):
        # Gold: Cara Three -> Dan Four [parent]. Shipped the other way
        # round as "child" — the same claim.
        run = _run(relationships=[{"from": "Dan Four", "to": "Cara Three",
                                   "type": "child"}])
        res = sg.score_relationships(GOLD, run)
        self.assertEqual(res["must_ship_hits"], 1)
        self.assertEqual(res["correct"], 1)

    def test_acceptable_set_matches(self):
        run = _run(relationships=[{"from": "Eve Five", "to": "Finn Six",
                                   "type": "enemy"}])
        res = sg.score_relationships(GOLD, run)
        self.assertEqual(res["must_ship_hits"], 1)
        self.assertEqual(res["correct"], 1)

    def test_acceptable_set_rejects_other_types(self):
        # must_not_ship entry is colleague-only acceptable []: an
        # "enemy" typing is a wrong claim, not a match.
        run = _run(relationships=[{"from": "Gus Seven", "to": "Hana Eight",
                                   "type": "enemy"}])
        res = sg.score_relationships(GOLD, run)
        self.assertEqual(res["labelled"], 1)
        self.assertEqual(res["correct"], 0)
        self.assertEqual(res["wrong"], 1)

    def test_unlabelled_claims_are_ignored(self):
        run = _run(relationships=[
            {"from": "Ann One", "to": "Ben Two", "type": "enemy"},
            {"from": "Zed Zed", "to": "Yan Yan", "type": "enemy"},
        ])
        res = sg.score_relationships(GOLD, run)
        self.assertEqual(res["labelled"], 1)  # Zed/Yan not counted
        self.assertEqual(res["precision"], 1.0)

    def test_gold_name_resolves_through_the_runs_roster(self):
        # The run's canonical display name carries a middle initial;
        # the gold names the person without it. The run's own alias
        # set is the identity, so the pair still matches.
        run = _run(
            characters=[{"name": "Matthew A. Sobol",
                         "aliases": ["Matthew Sobol", "Sobol"]},
                        {"name": "Peter Sebeck", "aliases": ["Sebeck"]}],
            relationships=[{"from": "Matthew A. Sobol",
                            "to": "Peter Sebeck", "type": "enemy"}])
        gold = dict(GOLD)
        gold["must_ship"] = [{"from": "Matthew Sobol",
                              "to": "Peter Sebeck", "type": "enemy"}]
        res = sg.score_relationships(gold, run)
        self.assertEqual(res["must_ship_hits"], 1)
        self.assertEqual(res["correct"], 1)

    def test_ambiguous_alias_does_not_resolve(self):
        # A form two entries claim resolves to neither: a false
        # collapse would invent a pair the pipeline never shipped.
        run = _run(
            characters=[{"name": "Alpha One", "aliases": ["Shared"]},
                        {"name": "Beta Two", "aliases": ["Shared"]},
                        {"name": "Gamma Three", "aliases": []}],
            relationships=[{"from": "Alpha One", "to": "Gamma Three",
                            "type": "enemy"}])
        gold = dict(GOLD)
        gold["must_ship"] = [{"from": "Shared", "to": "Gamma Three",
                              "type": "enemy"}]
        res = sg.score_relationships(gold, run)
        self.assertEqual(res["must_ship_hits"], 0)
        self.assertEqual(res["labelled"], 0)

    def test_name_punctuation_does_not_break_a_pair(self):
        # The pack writes "Russell Vanowen Jr."; the pipeline's
        # canonical form keeps the comma. Same person, same pair.
        gold = dict(GOLD)
        gold["must_ship"] = [{"from": "Russell Vanowen Jr.",
                              "to": "Garrett Lindhurst",
                              "type": "colleague"}]
        run = _run(relationships=[{"from": "Garrett Lindhurst",
                                   "to": "Russell Vanowen, Jr.",
                                   "type": "colleague"}])
        res = sg.score_relationships(gold, run)
        self.assertEqual(res["must_ship_hits"], 1)
        self.assertEqual(res["correct"], 1)

    def test_preview_rows_the_write_deletes_are_not_scored(self):
        # A rejected claim is kept in the preview as would_drop for
        # audit; the write path deletes it, so it is neither a hit nor
        # a false positive.
        run = _run(relationships=[
            {"from": "Ann One", "to": "Ben Two", "type": "enemy",
             "validation_note": "would_drop"},
            {"from": "Cara Three", "to": "Dan Four", "type": "parent",
             "validation_note": "kept"},
        ])
        res = sg.score_relationships(GOLD, run)
        self.assertEqual(res["labelled"], 1)
        self.assertEqual(res["must_ship_hits"], 1)
        self.assertEqual(res["excluded_would_drop"], 1)
        self.assertEqual(res["precision"], 1.0)

    def test_precision_none_with_no_labelled_claims(self):
        run = _run(relationships=[{"from": "Zed Zed", "to": "Yan Yan",
                                   "type": "enemy"}])
        res = sg.score_relationships(GOLD, run)
        self.assertIsNone(res["precision"])
        report = sg.score_run(GOLD, run, chapters=CHAPTERS)
        self.assertFalse(report["ok"])
        self.assertFalse(report["relationships_ok"])


class CharacterChecksTest(unittest.TestCase):
    def test_named_characters_pass(self):
        checks = sg.score_characters(GOLD, _run(), BOOK_TEXT)
        self.assertTrue(all(c["ok"] for c in checks), checks)

    def test_invented_name_fails_grounding(self):
        run = _run(characters=[{"name": "Ann One"},
                               {"name": "Ghost Person"}])
        checks = sg.score_characters(GOLD, run, BOOK_TEXT)
        failed = [c for c in checks if not c["ok"]]
        self.assertEqual([c["name"] for c in failed], ["character grounding"])

    def test_count_cap(self):
        run = _run(characters=[{"name": "Ann One %d" % i} for i in range(21)])
        checks = sg.score_characters(GOLD, run,
                                     " ".join("Ann One %d" % i
                                              for i in range(21)))
        self.assertFalse([c for c in checks if c["name"] == "character count"][0]["ok"])

    def test_ruled_away_name_and_identity_split(self):
        run = _run(characters=[
            {"name": "Major Pain in the Ass"},
            {"name": "Loki"}, {"name": "Brian Gragg"},
            {"name": "Karl Devon"}, {"name": "The Major"},
        ])
        text = "Loki Brian Gragg Karl Devon The Major Major Pain in the Ass"
        checks = {c["name"]: c for c in sg.score_characters(GOLD, run, text)}
        self.assertFalse(checks["ruled-away name absent"]["ok"])
        self.assertFalse(checks["identity rulings"]["ok"])

    def test_identity_rulings_tolerate_absence_and_merge(self):
        run = _run(characters=[{"name": "Karl Devon",
                                "aliases": ["The Major"]}])
        checks = {c["name"]: c
                  for c in sg.score_characters(GOLD, run,
                                               "Karl Devon The Major")}
        self.assertTrue(checks["identity rulings"]["ok"])


class QuoteChecksTest(unittest.TestCase):
    def _quote(self, text="You are late,", speaker="Ann One", chapter="Chapter 1"):
        return {"text": text, "speaker": speaker, "chapter": chapter}

    def test_verified_quote_with_grounded_speaker_passes(self):
        quotes = [self._quote()] * 4
        checks = {c["name"]: c for c in
                  sg.score_quotes(GOLD, _run(quotes=quotes), CHAPTERS)}
        self.assertTrue(checks["quote verification"]["ok"])
        self.assertTrue(checks["quote speaker grounding"]["ok"])
        self.assertTrue(checks["quote count"]["ok"])

    def test_fabricated_quote_fails(self):
        quotes = [self._quote(text="This line is not in the book at all")] * 4
        checks = {c["name"]: c for c in
                  sg.score_quotes(GOLD, _run(quotes=quotes), CHAPTERS)}
        self.assertFalse(checks["quote verification"]["ok"])

    def test_ungrounded_speaker_fails(self):
        quotes = [self._quote(speaker="Nobody Known")] * 4
        checks = {c["name"]: c for c in
                  sg.score_quotes(GOLD, _run(quotes=quotes), CHAPTERS)}
        self.assertFalse(checks["quote speaker grounding"]["ok"])

    def test_too_few_quotes_fails(self):
        checks = {c["name"]: c for c in
                  sg.score_quotes(GOLD, _run(quotes=[self._quote()]),
                                  CHAPTERS)}
        self.assertFalse(checks["quote count"]["ok"])


class TropeChecksTest(unittest.TestCase):
    def test_supported_trope_passes(self):
        run = _run(tropes=["Reluctant Hero"],
                   trope_chapters={pp._tnorm("Reluctant Hero"):
                                   ["Chapter 1", "Chapter 2"]})
        checks = sg.score_tropes(GOLD, run)
        self.assertTrue(all(c["ok"] for c in checks), checks)

    def test_ruled_trope_ships_fails(self):
        run = _run(tropes=["Revenge"],
                   trope_chapters={pp._tnorm("Revenge"):
                                   ["Chapter 1", "Chapter 2"]})
        checks = {c["name"]: c for c in sg.score_tropes(GOLD, run)}
        self.assertFalse(checks["trope rulings"]["ok"])

    def test_denylisted_trope_fails(self):
        name = sorted(pp._TROPE_DENYLIST)[0]
        run = _run(tropes=[name],
                   trope_chapters={pp._tnorm(name):
                                   ["Chapter 1", "Chapter 2"]})
        checks = {c["name"]: c for c in sg.score_tropes(GOLD, run)}
        self.assertFalse(checks["trope denylist"]["ok"])

    def test_single_chapter_trope_fails(self):
        run = _run(tropes=["Reluctant Hero"],
                   trope_chapters={pp._tnorm("Reluctant Hero"):
                                   ["Chapter 1"]})
        checks = {c["name"]: c for c in sg.score_tropes(GOLD, run)}
        self.assertFalse(checks["trope chapter support"]["ok"])


class TriggerChecksTest(unittest.TestCase):
    def _trig(self, **over):
        t = {"warning": "graphic_violence", "severity": "on_page",
             "chapter_indices": [0],
             "evidence": [{"quote": "q", "decision_p": 0.9}]}
        t.update(over)
        return t

    def test_supported_trigger_passes(self):
        checks = sg.score_triggers(GOLD, _run(triggers=[self._trig()]))
        self.assertTrue(all(c["ok"] for c in checks), checks)

    def test_high_severity_without_decision_p_fails(self):
        t = self._trig(evidence=[{"quote": "q"}])
        checks = {c["name"]: c for c in
                  sg.score_triggers(GOLD, _run(triggers=[t]))}
        self.assertFalse(checks["trigger evidence"]["ok"])

    def test_mentioned_severity_needs_no_decision_p(self):
        t = self._trig(severity="mentioned", evidence=[])
        checks = {c["name"]: c for c in
                  sg.score_triggers(GOLD, _run(triggers=[t]))}
        self.assertTrue(checks["trigger evidence"]["ok"])

    def test_missing_chapter_indices_fails(self):
        t = self._trig(chapter_indices=[])
        checks = {c["name"]: c for c in
                  sg.score_triggers(GOLD, _run(triggers=[t]))}
        self.assertFalse(checks["trigger chapter indices"]["ok"])


if __name__ == "__main__":
    unittest.main()
