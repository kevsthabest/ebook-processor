"""Phase 4 tests: v2 roster, reduce, trope gate, prose chapter splitting.
Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import unittest
from collections import Counter
from pathlib import Path
import importlib.util

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _ch(idx, label, text="x " * 200):
    return {"index": idx, "label": label, "text": text}


def _b(triggers=None, spice=0, tropes=None, summary="sum", quotes=None):
    full = {c: "none" for c in pp.TRIGGER_CATEGORIES}
    full.update(triggers or {})
    return {"summary": summary, "spice_level": spice,
            "triggers": full, "trigger_evidence": {},
            "trope_candidates": tropes or [], "quotes": quotes or []}


class TestRoster(unittest.TestCase):
    def test_alias_merge(self):
        roster = {}
        pp._roster_update(roster, [
            {"name": "Shirley Jackson", "aliases": ["I", "Narrator"],
             "role": "protagonist", "description": "d1", "evidence": "e1"},
        ], 1)
        pp._roster_update(roster, [
            {"name": "I", "aliases": [], "role": "protagonist",
             "description": "d2", "evidence": ""},
            {"name": "Laurie", "aliases": [], "role": "supporting",
             "description": "d3", "evidence": "e3"},
        ], 2)
        self.assertEqual(len(roster), 2)  # "I" merged, not duplicated
        shirley = roster[pp.norm_name("Shirley Jackson")]
        self.assertEqual(shirley["appearances"], 2)
        self.assertEqual(shirley["evidence"], "e1")  # first verified wins
        self.assertIn("I", shirley["aliases"])

    def test_alias_cross_match(self):
        roster = {}
        pp._roster_update(roster, [
            {"name": "Shirley Jackson", "aliases": ["the narrator"],
             "role": "protagonist", "description": "d", "evidence": ""}], 1)
        pp._roster_update(roster, [
            {"name": "The Narrator", "aliases": [], "role": "protagonist",
             "description": "d", "evidence": ""}], 2)
        # "The Narrator" normalizes to "narrator", matching the known alias
        self.assertEqual(len(roster), 1)
        e = roster[pp.norm_name("Shirley Jackson")]
        self.assertEqual(e["appearances"], 2)

    def test_roster_prompt_budget(self):
        roster = {}
        for i in range(20):
            pp._roster_update(roster, [
                {"name": f"Character Number {i} With A Long Name",
                 "aliases": [], "role": "minor", "description": "d",
                 "evidence": ""}], i + 1)
        text, dropped = pp._roster_prompt(roster, budget=300)
        self.assertLessEqual(len(text), 400)
        self.assertTrue(dropped)
        # most recent chapter's character survives the cap
        self.assertIn("Character Number 19", text)


class TestReduce(unittest.TestCase):
    def _full_triggers(self, **over):
        t = {c: "none" for c in pp.TRIGGER_CATEGORIES}
        t.update(over)
        return t

    def test_trigger_max_severity(self):
        bs = {1: _b(self._full_triggers(explicit_sex="mentioned"), spice=1),
              2: _b(self._full_triggers(explicit_sex="on_page"), spice=3)}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(len(red["triggers"]), 1)
        t = red["triggers"][0]
        self.assertEqual(t["warning"], "explicit_sex")
        self.assertEqual(t["severity"], "on_page")  # max wins
        self.assertEqual(t["chapters"], ["A", "B"])  # both chapters flagged
        self.assertIn("confidence", t)

    def test_spice_75th_percentile(self):
        bs = {i: _b(spice=s) for i, s in enumerate([0, 0, 2, 4], start=1)}
        red = pp.v2_reduce({}, bs, {}, [_ch(i, str(i)) for i in range(1, 5)])
        self.assertEqual(red["spice_level"], 2)  # sorted [0,0,2,4], idx ceil(.75*4)-1=2

    def test_pov_threshold(self):
        roster = {}
        pp._roster_update(roster, [{"name": "Amy", "aliases": [], "role": None,
                                   "description": "", "evidence": ""}], 1)
        pp._roster_update(roster, [{"name": "Bob", "aliases": [], "role": None,
                                   "description": "", "evidence": ""}], 1)
        a1 = {"characters": [], "relationships": [], "pov_character": "Amy"}
        a2 = {"characters": [], "relationships": [], "pov_character": "Amy"}
        a3 = {"characters": [], "relationships": [], "pov_character": "Bob"}
        a4 = {"characters": [], "relationships": [], "pov_character": None}
        chapters = [_ch(i, str(i)) for i in range(1, 5)]
        red = pp.v2_reduce({1: a1, 2: a2, 3: a3, 4: a4}, {}, roster, chapters)
        self.assertEqual(red["povs"], ["Amy"])  # Bob only 1 chapter < threshold 2

    def test_pov_threshold_small_book(self):
        roster = {}
        pp._roster_update(roster, [{"name": "Solo", "aliases": [], "role": None,
                                   "description": "", "evidence": ""}], 1)
        a1 = {"characters": [], "relationships": [], "pov_character": "Solo"}
        red = pp.v2_reduce({1: a1}, {}, roster, [_ch(1, "A")])
        self.assertEqual(red["povs"], ["Solo"])  # <4 chapters: threshold 1

    def test_relationship_dedupe_and_canonical(self):
        roster = {}
        pp._roster_update(roster, [{"name": "Shirley Jackson",
                                   "aliases": ["I"], "role": None,
                                   "description": "", "evidence": ""}], 1)
        pp._roster_update(roster, [{"name": "Laurie", "aliases": [],
                                   "role": None, "description": "",
                                   "evidence": ""}], 1)
        rel = {"from": "I", "to": "Laurie", "type": "parent", "evidence": "e"}
        a1 = {"characters": [], "relationships": [rel], "pov_character": None}
        a2 = {"characters": [], "relationships": [dict(rel)], "pov_character": None}
        red = pp.v2_reduce({1: a1, 2: a2}, {}, roster,
                           [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(len(red["relationships"]), 1)
        self.assertEqual(red["relationships"][0]["from"], "Shirley Jackson")

    def test_trope_candidate_union(self):
        bs = {1: _b(tropes=["Enemies to Lovers", "found family"]),
              2: _b(tropes=["enemies_to_lovers", "Slow Burn"])}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(len(red["trope_candidates"]), 3)  # dup merged
        self.assertEqual(red["trope_candidate_counts"][pp._tnorm("enemies to lovers")], 2)

    def test_character_confidence_formula(self):
        self.assertEqual(pp._v2_char_confidence(1, False), 0.6)
        self.assertEqual(pp._v2_char_confidence(5, True), 0.95)  # capped
        self.assertEqual(pp._v2_trigger_confidence(2, 2, 1), 0.9)


class TestTropeGate(unittest.TestCase):
    def test_gate_confirms_yes_only(self):
        gate_json = {"verdicts": [
            {"trope": "Found Family", "verdict": "yes",
             "justification": "Ch 2"},
            {"trope": "Love Triangle", "verdict": "no",
             "justification": "Ch 1"},
            {"trope": "Slow Burn", "verdict": "unsure",
             "justification": "Ch 3"},
        ]}
        import json as _json

        def fake_call(prompt, chunk, i, n):
            return _json.dumps(gate_json)

        summaries = [{"index": 1, "label": "A", "summary": "s1"},
                     {"index": 2, "label": "B", "summary": "s2"}]
        counts = Counter({pp._tnorm("Found Family"): 2})
        confirmed, conf = pp.v2_trope_gate(
            fake_call, summaries, ["Found Family", "Love Triangle", "Slow Burn"],
            counts)
        self.assertEqual(confirmed, ["Found Family"])
        self.assertEqual(conf["Found Family"], 0.75)  # 0.55 + 0.1*2

    def test_gate_empty_candidates(self):
        confirmed, conf = pp.v2_trope_gate(lambda *a: "{}", [], [], Counter())
        self.assertEqual((confirmed, conf), ([], {}))


class TestProseChapters(unittest.TestCase):
    def test_heading_split(self):
        text = ("CHAPTER 1\n\n" + "para one. " * 200 +
                "\n\nCHAPTER 2\n\n" + "para two. " * 200 +
                "\n\nCHAPTER 3\n\n" + "para three. " * 200)
        chapters = pp._prose_chapters(text)
        self.assertEqual(len(chapters), 3)
        self.assertTrue(chapters[0]["label"].upper().startswith("CHAPTER 1"))
        self.assertEqual([c["index"] for c in chapters], [1, 2, 3])

    def test_no_headings_fallback(self):
        text = "word " * 20000  # one blob, no headings
        chapters = pp._prose_chapters(text)
        self.assertGreater(len(chapters), 1)
        self.assertTrue(all(len(c["text"]) <= pp.MAX_CHAPTER_CHARS
                            for c in chapters))


class TestV2Sanitizers(unittest.TestCase):
    def test_sanitize_v2b_defaults_all_triggers(self):
        out = pp._sanitize_v2b({"summary": "s", "spice_level": "3"})
        self.assertEqual(len(out["triggers"]), len(pp.TRIGGER_CATEGORIES))
        self.assertTrue(all(v == "none" for v in out["triggers"].values()))
        self.assertEqual(out["spice_level"], 3)

    def test_sanitize_v2b_bad_severity(self):
        out = pp._sanitize_v2b({"triggers": {"murder": "EXTREME"}})
        self.assertEqual(out["triggers"]["murder"], "none")

    def test_sanitize_v2a_bad_role(self):
        out = pp._sanitize_v2a({"characters": [
            {"name": "X", "role": "hero", "description": "d"}]})
        self.assertIsNone(out["characters"][0]["role"])


if __name__ == "__main__":
    unittest.main()
