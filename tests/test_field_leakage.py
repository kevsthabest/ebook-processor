"""Field-leakage filter tests (processor<->app quality loop findings).

- Bug 1: POV labels ("Xaden's POV", "Villain POV", ...) leaked into the
  trope list. They are POV metadata, never tropes, and must be dropped
  where candidate/final trope lists are assembled so they cannot reach
  the trope gate or trope_proposals either.
- Bug 2: self-relationships (from == to). Origins, traced on the Fourth
  Wing -12 preview: the filed pairs were REMAPPED pairs — valid claims
  (Violet->Mom "child", Violet->Mira "sibling", Violet->Dain "friend")
  whose endpoint canonicalised onto Violet herself because her roster
  entry carried the other people's names as aliases. v2_reduce counts
  the two origins separately; the dedupe remap and the legacy reduce
  carry the same backstop so a self-pair never reaches output.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _ch(idx, label, text="x " * 200):
    return {"index": idx, "label": label, "text": text}


def _b(tropes=None, summary="sum"):
    full = {c: "none" for c in pp.TRIGGER_CATEGORIES}
    return {"summary": summary, "spice_level": 0, "triggers": full,
            "trigger_evidence": {}, "trope_candidates": tropes or [],
            "quotes": []}


def _entry(name, alias_keys=()):
    nk = pp.norm_name(name)
    return {"name": name, "aliases": set(),
            "alias_keys": set(alias_keys) | {nk},
            "primary_keys": {nk}, "appearances": 5, "last_seen": 3,
            "chapters": {1, 2, 3}, "roles": Counter({"protagonist": 3}),
            "descriptions": [], "evidence": ""}


def _fw_roster():
    """Fourth Wing scenario: Violet's roster entry carries other people's
    names as aliases (Mom / Lilith / Mira / Dain) — the pollution that
    turned valid pairs into self-pairs in the -12 preview."""
    polluted = {pp.norm_name(a) for a in (
        "Mom", "Mother", "Lilith Sorrengail", "Mira Sorrengail",
        "Dain Aetos", "Violet's mother")}
    roster = {
        pp.norm_name("Violet Sorrengail"):
            _entry("Violet Sorrengail", polluted),
        pp.norm_name("Xaden Riorson"):
            _entry("Xaden Riorson"),
    }
    return roster


class TestPovTropeLabel(unittest.TestCase):
    def test_pov_labels_dropped(self):
        for label in ("Xaden's POV", "POV: Xaden", "Villain POV",
                      "Dual POV", "POV", "Multiple POVs",
                      "first-person pov", "xaden_pov"):
            self.assertTrue(pp._is_pov_trope_label(label), label)

    def test_legitimate_labels_survive(self):
        # "Point of View" names a narrative technique: no standalone
        # pov token, survives deliberately. Substrings never count.
        for label in ("Point of View", "Enemies to Lovers", "Poverty",
                      "Found Family", "Slow Burn", ""):
            self.assertFalse(pp._is_pov_trope_label(label), label)


class TestV2TropeCandidates(unittest.TestCase):
    def test_pov_candidates_never_assembled(self):
        bs = {1: _b(tropes=["Xaden's POV", "Enemies to Lovers",
                            "Villain POV"]),
              2: _b(tropes=["Dual POV", "Point of View", "POV: Xaden",
                            "enemies_to_lovers"])}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(red["trope_candidates"],
                         ["Enemies to Lovers", "Point of View"])
        for k in red["trope_candidate_counts"]:
            self.assertFalse(pp._is_pov_trope_label(k), k)
        for k in red["trope_chapters"]:
            self.assertFalse(pp._is_pov_trope_label(k), k)

    def test_pov_labels_cannot_reach_gate_or_proposals(self):
        # The gate (and trope_proposals downstream) only ever see
        # v2_reduce's candidate list — a label filtered there has no
        # path back in, even with a yes-to-everything gate.
        bs = {1: _b(tropes=["Xaden's POV", "Found Family"])}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A")])
        gate_json = {"verdicts": [
            {"trope": t, "verdict": "yes", "justification": "Ch 1"}
            for t in red["trope_candidates"]]}

        def fake_call(prompt, chunk, i, n):
            return json.dumps(gate_json)

        summaries = [{"index": 1, "label": "A", "summary": "s"}]
        confirmed, _conf = pp.v2_trope_gate(
            fake_call, summaries, red["trope_candidates"],
            Counter(red["trope_candidate_counts"]))
        self.assertEqual(confirmed, ["Found Family"])


class TestV2SelfPairs(unittest.TestCase):
    def test_remapped_and_extracted_self_pairs_dropped(self):
        chapter_as = {1: {
            "pov_character": "Violet Sorrengail", "characters": [],
            "relationships": [
                # Remapped: valid pairs eaten by alias pollution.
                {"from": "Violet Sorrengail", "to": "Mom",
                 "type": "child",
                 "evidence": "Mom shrugs, the lines of her fitted black "
                             "uniform rising and falling with the motion."},
                {"from": "Violet Sorrengail", "to": "Mira Sorrengail",
                 "type": "sibling",
                 "evidence": "She's everything I'm not."},
                {"from": "Violet Sorrengail", "to": "Dain Aetos",
                 "type": "friend",
                 "evidence": "Find Dain. He'll put you in his squad"},
                # Extracted: the LLM emitted the self-pair verbatim.
                {"from": "Violet Sorrengail", "to": "Violet Sorrengail",
                 "type": "friend", "evidence": ""},
                # Normal pair: untouched.
                {"from": "Violet Sorrengail", "to": "Xaden Riorson",
                 "type": "enemy", "evidence": "He glares at her."},
            ]}}
        red = pp.v2_reduce(chapter_as, {}, _fw_roster(),
                           [_ch(1, "A"), _ch(2, "B"), _ch(3, "C")])
        pairs = {(r["from"], r["to"], r["type"])
                 for r in red["relationships"]}
        self.assertEqual(pairs, {("Violet Sorrengail", "Xaden Riorson",
                                  "enemy")})
        self.assertEqual(red["self_pairs_dropped"],
                         {"extracted": 1, "remapped": 3})

    def test_no_self_pairs_means_zero_counts(self):
        chapter_as = {1: {
            "pov_character": None, "characters": [],
            "relationships": [
                {"from": "Violet Sorrengail", "to": "Xaden Riorson",
                 "type": "enemy", "evidence": "He glares at her."}]}}
        red = pp.v2_reduce(chapter_as, {}, _fw_roster(), [_ch(1, "A")])
        self.assertEqual(red["self_pairs_dropped"],
                         {"extracted": 0, "remapped": 0})
        self.assertEqual(len(red["relationships"]), 1)

    def test_self_pairs_json_round_trip(self):
        # The preview result carries self_pairs_dropped alongside
        # relationships; both must survive JSON serialization intact.
        chapter_as = {1: {
            "pov_character": None, "characters": [],
            "relationships": [
                {"from": "Violet Sorrengail", "to": "Mom",
                 "type": "child", "evidence": "Mom shrugs."},
                {"from": "Violet Sorrengail", "to": "Xaden Riorson",
                 "type": "enemy", "evidence": "He glares at her."}]}}
        red = pp.v2_reduce(chapter_as, {}, _fw_roster(), [_ch(1, "A")])
        payload = {"relationships": red["relationships"],
                   "self_pairs_dropped": red["self_pairs_dropped"]}
        back = json.loads(json.dumps(payload))
        self.assertEqual(back["self_pairs_dropped"],
                         {"extracted": 0, "remapped": 1})
        self.assertEqual(len(back["relationships"]), 1)
        self.assertNotEqual(back["relationships"][0]["from"],
                            back["relationships"][0]["to"])


class TestDedupeRemapSelfPairs(unittest.TestCase):
    def test_merge_cannot_create_self_pair(self):
        chars = [
            {"name": "Violet Sorrengail", "description": "d1",
             "confidence": 0.9},
            {"name": "violet sorrengail", "description": "d2",
             "confidence": 0.8},
            {"name": "Xaden Riorson", "description": "d3",
             "confidence": 0.9},
        ]
        rels = [
            # Between the two entries Signal 1 merges: becomes a
            # self-pair under remap and must be dropped, not emitted.
            {"from": "Violet Sorrengail", "to": "violet sorrengail",
             "type": "friend"},
            {"from": "Violet Sorrengail", "to": "Xaden Riorson",
             "type": "enemy"},
        ]
        with mock.patch.object(pp, "embed_vectors",
                               return_value=[[0.0], [0.0], [0.0]]):
            _chars, new_rels, _povs, report = pp.dedupe_characters(
                chars, rels, [])
        self.assertEqual(len(report), 1)  # the merge really happened
        self.assertEqual(new_rels, [{"from": "Violet Sorrengail",
                                     "to": "Xaden Riorson",
                                     "type": "enemy"}])
        for r in new_rels:
            self.assertNotEqual(pp.norm_name(r["from"]),
                                pp.norm_name(r["to"]))


if __name__ == "__main__":
    unittest.main()
