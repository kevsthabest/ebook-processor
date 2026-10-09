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

    def test_primary_not_absorbed_as_alias(self):
        roster = {}
        # Model wrongly lists Sally as Laurie's alias in ch1...
        pp._roster_update(roster, [
            {"name": "Laurie", "aliases": ["Sally", "Sarah"],
             "role": "supporting", "description": "d", "evidence": ""}], 1)
        # ...but Sally stands as her own character in ch2: keep separate.
        pp._roster_update(roster, [
            {"name": "Sally", "aliases": [],
             "role": "supporting", "description": "d", "evidence": ""}], 2)
        self.assertEqual(len(roster), 2)
        self.assertIn(pp.norm_name("sally"), roster)
        # And a later chapter re-asserting the bad alias doesn't re-merge.
        pp._roster_update(roster, [
            {"name": "Laurie", "aliases": ["Sally"],
             "role": "supporting", "description": "d", "evidence": ""}], 3)
        self.assertEqual(len(roster), 2)
        laurie = roster[pp.norm_name("laurie")]
        self.assertEqual(laurie["appearances"], 2)

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


class TestNamesOverlap(unittest.TestCase):
    def test_title_surname_match(self):
        # "General Melgren" is Augustine Melgren with a title
        self.assertTrue(pp._names_overlap("general melgren", "augustine melgren"))

    def test_bare_surname_match(self):
        self.assertTrue(pp._names_overlap("melgren", "augustine melgren"))

    def test_prefix_match(self):
        self.assertTrue(pp._names_overlap("bodhi", "bodhi durran"))
        self.assertTrue(pp._names_overlap("garrick", "garrick tavis"))

    def test_exact_match(self):
        self.assertTrue(pp._names_overlap("violet sorrengail", "violet sorrengail"))

    def test_siblings_do_not_merge(self):
        # Two full names sharing only a surname are different people
        self.assertFalse(pp._names_overlap("brennan sorrengail", "violet sorrengail"))
        self.assertFalse(pp._names_overlap("mira sorrengail", "violet sorrengail"))

    def test_distinct_names(self):
        self.assertFalse(pp._names_overlap("xaden riorson", "dain aetos"))
        self.assertFalse(pp._names_overlap("", "violet"))
        self.assertFalse(pp._names_overlap("violet", ""))

    def test_roster_keeps_siblings_separate(self):
        roster = {}
        pp._roster_update(roster, [
            {"name": "Violet Sorrengail", "aliases": [], "role": "protagonist",
             "description": "d1", "evidence": ""}], 1)
        pp._roster_update(roster, [
            {"name": "Brennan Sorrengail", "aliases": [], "role": "supporting",
             "description": "d2", "evidence": ""}], 2)
        pp._roster_update(roster, [
            {"name": "Mira Sorrengail", "aliases": [], "role": "supporting",
             "description": "d3", "evidence": ""}], 3)
        # Three siblings stay three roster entries
        self.assertEqual(len(roster), 3)


class TestReduce(unittest.TestCase):
    def _full_triggers(self, **over):
        t = {c: "none" for c in pp.TRIGGER_CATEGORIES}
        t.update(over)
        return t

    def test_trigger_max_severity(self):
        bs = {1: _b(self._full_triggers(explicit_sex="mentioned"), spice=1),
              2: _b(self._full_triggers(explicit_sex="on_page"), spice=3)}
        # add verified evidence so on_page stands
        bs[2]["trigger_evidence"] = {"explicit_sex": "quote here"}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(len(red["triggers"]), 1)
        t = red["triggers"][0]
        self.assertEqual(t["warning"], "explicit_sex")
        self.assertEqual(t["severity"], "on_page")  # max wins
        self.assertEqual(t["chapters"], ["A", "B"])  # both chapters flagged
        self.assertIn("confidence", t)

    def test_trigger_downgrade_without_evidence(self):
        # on_page claimed but no verified quote -> downgraded to mentioned
        # (2 chapters so it passes the 2+ chapter minimum)
        bs = {1: _b(self._full_triggers(murder="on_page"), spice=1),
              2: _b(self._full_triggers(murder="on_page"), spice=1)}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        t = red["triggers"][0]
        self.assertEqual(t["severity"], "mentioned")

    def test_single_chapter_graphic_kept(self):
        # A graphic scene in one chapter with evidence is kept — that's
        # exactly what a trigger warning is for.
        bs = {1: _b(self._full_triggers(murder="graphic"), spice=1),
              2: _b(self._full_triggers(), spice=1)}
        bs[1]["trigger_evidence"] = {"murder": "quote here"}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(len(red["triggers"]), 1)
        self.assertEqual(red["triggers"][0]["severity"], "graphic")
        self.assertEqual(red["triggers"][0]["severity_claimed"], "graphic")

    def test_single_chapter_on_page_kept(self):
        bs = {1: _b(self._full_triggers(torture="on_page"), spice=1),
              2: _b(self._full_triggers(), spice=1)}
        bs[1]["trigger_evidence"] = {"torture": "quote here"}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(len(red["triggers"]), 1)
        self.assertEqual(red["triggers"][0]["severity"], "on_page")

    def test_single_chapter_mentioned_dropped(self):
        # Lone "mentioned" is noise — still dropped.
        bs = {1: _b(self._full_triggers(bullying="mentioned"), spice=1),
              2: _b(self._full_triggers(), spice=1)}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(len(red["triggers"]), 0)

    def test_single_chapter_unevidenced_on_page_dropped(self):
        # on_page without evidence downgrades to mentioned, then the
        # single-chapter rule drops it.
        bs = {1: _b(self._full_triggers(murder="on_page"), spice=1),
              2: _b(self._full_triggers(), spice=1)}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(len(red["triggers"]), 0)

    def test_trigger_single_chapter_mentioned_dropped(self):
        # Single-chapter "mentioned" is noise — dropped entirely
        bs = {1: _b(self._full_triggers(murder="mentioned"), spice=1)}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A")])
        self.assertEqual(len(red["triggers"]), 0)

    def test_spice_matrix_all_cells(self):
        # All 12 matrix cells + the (none, none) -> 0 case.
        cases = [
            # (peak_band, freq_band) -> level; constructed via peak + frac.
            # mild x rare/occasional -> 1
            ([1] + [0] * 99, 100, 1),       # peak 1, 1% spicy
            ([1] * 10 + [0] * 90, 100, 1),  # peak 1, 10% spicy
            # mild x frequent/pervasive -> 2
            ([1] * 20 + [0] * 80, 100, 2),
            ([1] * 50 + [0] * 50, 100, 2),
            # moderate x rare -> 1
            ([3] + [0] * 99, 100, 1),
            # moderate x occasional -> 2
            ([3] * 10 + [0] * 90, 100, 2),
            # moderate x frequent -> 3
            ([2] * 20 + [0] * 80, 100, 3),
            # moderate x pervasive -> 4
            ([3] * 50 + [0] * 50, 100, 4),
            # explicit x rare -> 2
            ([5] + [0] * 99, 100, 2),
            # explicit x occasional -> 3
            ([5] * 10 + [0] * 90, 100, 3),
            # explicit x frequent -> 4
            ([4] * 20 + [0] * 80, 100, 4),
            # explicit x pervasive -> 5
            ([5] * 50 + [0] * 50, 100, 5),
            # (none, none) -> 0
            ([0] * 10, 10, 0),
            ([], 0, 0),
        ]
        for spices, total, expected in cases:
            with self.subTest(spices=spices[:3], total=total):
                self.assertEqual(
                    pp._spice_level_from_chapters(spices, total), expected)

    def test_spice_daemon_acceptance(self):
        # Daemon: peak 5, 2-4 spicy chapters of 73 -> level 2 (was 3).
        for n_spicy in (2, 3, 4):
            spices = [5] * n_spicy + [0] * (73 - n_spicy)
            level = pp._spice_level_from_chapters(spices, 73)
            self.assertEqual(level, 2,
                             f"n_spicy={n_spicy} should give level 2")

    def test_spice_single_explicit_chapter_not_five(self):
        # One explicit scene in a long book is not a 5 (old peak logic
        # would have needed 2+ chapters; the matrix handles it via frequency).
        spices = [5] + [0] * 72
        self.assertEqual(pp._spice_level_from_chapters(spices, 73), 2)

    def test_spice_bands(self):
        self.assertEqual(pp._spice_band_peak(0), "none")
        self.assertEqual(pp._spice_band_peak(1), "mild")
        self.assertEqual(pp._spice_band_peak(3), "moderate")
        self.assertEqual(pp._spice_band_peak(5), "explicit")
        self.assertEqual(pp._spice_band_freq(0), "none")
        self.assertEqual(pp._spice_band_freq(0.03), "rare")
        self.assertEqual(pp._spice_band_freq(0.10), "occasional")
        self.assertEqual(pp._spice_band_freq(0.20), "frequent")
        self.assertEqual(pp._spice_band_freq(0.50), "pervasive")

    def test_spice_mixed_intensity_regression(self):
        # P0-1: one explicit chapter + many mild chapters must NOT be 5.
        # Frequency counts chapters AT the peak intensity (spice>=4), not
        # just any spice. 1/100 explicit -> rare -> explicit x rare = 2.
        spices = [5] + [1] * 40 + [0] * 59
        self.assertEqual(pp._spice_level_from_chapters(spices, 100), 2)

    def test_spice_mixed_intensity_moderate(self):
        # Peak moderate (3): frequency counts spice>=2, ignoring mild (1s).
        # 2 chapters at 3, 30 chapters at 1 -> 2/100 moderate -> rare -> 1.
        spices = [3] * 2 + [1] * 30 + [0] * 68
        self.assertEqual(pp._spice_level_from_chapters(spices, 100), 1)

    def test_spice_incomplete_extraction(self):
        # P0-2: 73 chapters, only 30 successful, 2 spicy of 30.
        # Denominator is 30 (successful), not 73 (total).
        # 2/30 = 0.067 -> occasional -> explicit x occasional = 3.
        spices = [5] * 2 + [0] * 28
        self.assertEqual(pp._spice_level_from_chapters(spices, 30), 3)

    def test_spice_boundary_fractions(self):
        # Exact boundary values for _spice_band_freq.
        self.assertEqual(pp._spice_band_freq(0.06), "occasional")  # not rare
        self.assertEqual(pp._spice_band_freq(0.059), "rare")
        self.assertEqual(pp._spice_band_freq(0.15), "frequent")  # not occasional
        self.assertEqual(pp._spice_band_freq(0.149), "occasional")
        self.assertEqual(pp._spice_band_freq(0.35), "pervasive")  # not frequent
        self.assertEqual(pp._spice_band_freq(0.349), "frequent")

    def test_spice_invalid_inputs(self):
        # Empty list, zero chapters -> 0.
        self.assertEqual(pp._spice_level_from_chapters([], 0), 0)
        self.assertEqual(pp._spice_level_from_chapters([], 100), 0)
        self.assertEqual(pp._spice_level_from_chapters([3, 4], 0), 0)
        # Out-of-range values are clamped: 7->5, -1->0.
        # [7, -1] clamps to [5, 0]: peak explicit, 1/2 at peak -> pervasive -> 5.
        self.assertEqual(pp._spice_level_from_chapters([7, -1], 2), 5)
        # All negative -> clamped to 0 -> none -> 0.
        self.assertEqual(pp._spice_level_from_chapters([-3, -1], 2), 0)

    def test_spice_daemon_acceptance_still_passes(self):
        # Daemon: peak 5, 2-4 spicy chapters of 73 -> level 2.
        # With P0-1 fix: counts chapters with spice>=4 (all are 5s).
        for n_spicy in (2, 3, 4):
            spices = [5] * n_spicy + [0] * (73 - n_spicy)
            level = pp._spice_level_from_chapters(spices, 73)
            self.assertEqual(level, 2,
                             f"n_spicy={n_spicy} should give level 2")

    def test_trigger_prominence_fields(self):
        # graphic in few chapters -> medium; mentioned everywhere -> medium
        self.assertEqual(
            pp._trigger_prominence("graphic", 2, 73), "medium")
        self.assertEqual(
            pp._trigger_prominence("mentioned", 20, 73), "medium")
        self.assertEqual(
            pp._trigger_prominence("on_page", 20, 73), "high")
        self.assertEqual(
            pp._trigger_prominence("graphic", 10, 73), "high")
        self.assertEqual(
            pp._trigger_prominence("mentioned", 1, 73), "low")
        self.assertEqual(
            pp._trigger_prominence("on_page", 1, 73), "low")
        # Edge cases
        self.assertEqual(pp._trigger_prominence("graphic", 0, 73), "low")
        self.assertEqual(pp._trigger_prominence("graphic", 5, 0), "low")

    def test_trigger_reduce_includes_prominence(self):
        bs = {1: _b(self._full_triggers(murder="graphic"), spice=0),
              2: _b(self._full_triggers(murder="graphic"), spice=0)}
        for b in bs.values():
            b["trigger_evidence"] = {"murder": "he killed him"}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(len(red["triggers"]), 1)
        t = red["triggers"][0]
        self.assertEqual(t["chapter_count"], 2)
        self.assertEqual(t["frequency"], 1.0)
        # graphic in 2/2 chapters (frequent) -> high
        self.assertEqual(t["prominence"], "high")

    def test_coverage_warning_low(self):
        # P0-2: <50% successful chapters -> coverage_warning True.
        # 4 chapters, only 1 successful (bs has 1 entry, 3 are None).
        bs = {1: _b(spice=5)}
        chapters = [_ch(i, str(i)) for i in range(1, 5)]
        # chapter_bs needs entries for all; None = failed.
        full_bs = {1: bs[1], 2: None, 3: None, 4: None}
        red = pp.v2_reduce({}, full_bs, {}, chapters)
        self.assertTrue(red["coverage_warning"])
        self.assertEqual(red["chapters_successful"], 1)
        self.assertEqual(red["chapters_total"], 4)

    def test_coverage_warning_ok(self):
        # >=50% successful -> no warning.
        bs = {1: _b(spice=5), 2: _b(spice=0), 3: _b(spice=0)}
        chapters = [_ch(i, str(i)) for i in range(1, 5)]
        full_bs = {1: bs[1], 2: bs[2], 3: bs[3], 4: None}
        red = pp.v2_reduce({}, full_bs, {}, chapters)
        self.assertFalse(red["coverage_warning"])
        self.assertEqual(red["chapters_successful"], 3)

    def test_trigger_prominence_uses_successful_denominator(self):
        # P0-2: trigger in 2 chapters, but only 4 of 10 succeeded.
        # 2/4 = 0.5 -> frequent (not 2/10 = 0.2 -> occasional).
        bs = {1: _b(self._full_triggers(murder="on_page"), spice=0),
              2: _b(self._full_triggers(murder="on_page"), spice=0),
              3: _b(spice=0),
              4: _b(spice=0)}
        for i in (1, 2):
            bs[i]["trigger_evidence"] = {"murder": "he killed him"}
        chapters = [_ch(i, str(i)) for i in range(1, 11)]
        full_bs = {i: bs.get(i) for i in range(1, 11)}  # 6 are None
        red = pp.v2_reduce({}, full_bs, {}, chapters)
        t = red["triggers"][0]
        self.assertEqual(t["chapter_count"], 2)
        self.assertEqual(t["frequency"], 0.5)  # 2/4 successful, not 2/10
        # on_page x frequent -> high
        self.assertEqual(t["prominence"], "high")

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


class TestFirstAppearance(unittest.TestCase):
    def test_first_appearance_is_1based(self):
        # Roster chapters are 1-based; first_appearance_chapter must not add 1.
        from collections import Counter
        roster = {
            "violet": {"name": "Violet", "aliases": set(), "alias_keys": {"violet"},
                       "primary_keys": {"violet"}, "appearances": 3, "last_seen": 5,
                       "chapters": {5, 3, 8}, "roles": Counter({"protagonist": 3}),
                       "descriptions": ["d"], "evidence": ""},
        }
        red = pp.v2_reduce({}, {}, roster, [_ch(1, "A"), _ch(2, "B")])
        chars = {c["name"]: c for c in red["characters"]}
        # Lowest chapter is 3, not 4
        self.assertEqual(chars["Violet"]["first_appearance_chapter"], 3)

    def test_first_appearance_none_when_no_chapters(self):
        from collections import Counter
        roster = {
            "x": {"name": "X", "aliases": set(), "alias_keys": {"x"},
                  "primary_keys": {"x"}, "appearances": 1, "last_seen": 1,
                  "chapters": set(), "roles": Counter(), "descriptions": [],
                  "evidence": ""},
        }
        red = pp.v2_reduce({}, {}, roster, [_ch(1, "A")])
        # X has no proper name and 1 appearance < threshold, may be filtered;
        # if present, first_appearance_chapter must be None
        for c in red["characters"]:
            if c["name"] == "X":
                self.assertIsNone(c["first_appearance_chapter"])


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
        self.assertEqual(conf[pp._tnorm("Found Family")], 0.75)  # 0.55 + 0.1*2

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


class TestParallelJobs(unittest.TestCase):
    def test_effective_batch_size_default(self):
        # No override: falls back to CONFIG
        if hasattr(pp._DIAG, "batch_size_override"):
            delattr(pp._DIAG, "batch_size_override")
        pp.CONFIG["batch_size"] = 4
        self.assertEqual(pp.effective_batch_size(), 4)

    def test_effective_batch_size_override(self):
        pp._DIAG.batch_size_override = 2
        try:
            self.assertEqual(pp.effective_batch_size(), 2)
        finally:
            delattr(pp._DIAG, "batch_size_override")

    def test_effective_batch_size_min_one(self):
        pp._DIAG.batch_size_override = 0
        try:
            self.assertEqual(pp.effective_batch_size(), 1)
        finally:
            delattr(pp._DIAG, "batch_size_override")


class TestCharacterEnrichment(unittest.TestCase):
    def test_status_sanitizer_valid(self):
        r = pp._sanitize_v2a({"characters": [
            {"name": "A", "status": "dead"},
            {"name": "B", "status": "ALIVE"},
            {"name": "C", "status": "missing"},
        ], "relationships": []})
        statuses = [c["status"] for c in r["characters"]]
        self.assertEqual(statuses, ["dead", "alive", "missing"])

    def test_status_synonyms(self):
        r = pp._sanitize_v2a({"characters": [
            {"name": "A", "status": "deceased"},
            {"name": "B", "status": "vanished"},
        ], "relationships": []})
        statuses = [c["status"] for c in r["characters"]]
        self.assertEqual(statuses, ["dead", "missing"])

    def test_status_defaults_unknown(self):
        r = pp._sanitize_v2a({"characters": [
            {"name": "A", "status": "probably fine"},
            {"name": "B"},
        ], "relationships": []})
        statuses = [c["status"] for c in r["characters"]]
        self.assertEqual(statuses, ["unknown", "unknown"])

    def test_appearance_capped(self):
        r = pp._sanitize_v2a({"characters": [
            {"name": "A", "appearance": "x" * 900},
        ], "relationships": []})
        self.assertEqual(len(r["characters"][0]["appearance"]), 500)

    def test_prompt_requests_enrichment_fields(self):
        self.assertIn('"appearance"', pp.PROMPT_V2_CHARACTERS)
        self.assertIn('"status"', pp.PROMPT_V2_CHARACTERS)
        self.assertIn("alive", pp.PROMPT_V2_CHARACTERS)


class TestMetadataCorrection(unittest.TestCase):
    def test_no_isbn_noop(self):
        title, author, corrected = pp.correct_metadata("Some Book", "Some Author", {})
        self.assertEqual((title, author, corrected), ("Some Book", "Some Author", False))

    def test_no_isbn_none_identifiers(self):
        title, author, corrected = pp.correct_metadata("Some Book", "Some Author", None)
        self.assertFalse(corrected)

    def test_network_failure_keeps_parsed(self):
        # Unreachable ISBN service (invalid URL via monkeypatch) -> silent fallback
        import urllib.request
        orig = urllib.request.urlopen
        def boom(*a, **k):
            raise ConnectionError("nope")
        urllib.request.urlopen = boom
        try:
            title, author, corrected = pp.correct_metadata(
                "Parsed Title", "Parsed Author", {"isbn": "9780000000000"})
        finally:
            urllib.request.urlopen = orig
        self.assertEqual((title, author, corrected),
                         ("Parsed Title", "Parsed Author", False))


class TestMetadataCorrectionPath(unittest.TestCase):
    def _mock_openlibrary(self, title, author_name):
        import urllib.request, json
        orig = urllib.request.urlopen
        payload = json.dumps({
            "docs": [{"title": title, "author_name": [author_name] if author_name else []}]
        }).encode("utf-8")
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return payload
        urllib.request.urlopen = lambda *a, **k: FakeResp()
        return orig

    def test_title_equals_author_gets_fixed(self):
        # The Change Agent bug: parsed title == author name
        import urllib.request
        orig = self._mock_openlibrary("Change Agent", "Daniel Suarez")
        try:
            title, author, corrected = pp.correct_metadata(
                "Daniel Suarez", "Daniel Suarez", {"isbn": "9783121019618"})
        finally:
            urllib.request.urlopen = orig
        self.assertTrue(corrected)
        self.assertEqual(title, "Change Agent")
        self.assertEqual(author, "Daniel Suarez")

    def test_matching_title_not_touched(self):
        import urllib.request
        orig = self._mock_openlibrary("Fourth Wing", "Rebecca Yarros")
        try:
            title, author, corrected = pp.correct_metadata(
                "Fourth Wing", "Rebecca Yarros", {"isbn": "9780000000000"})
        finally:
            urllib.request.urlopen = orig
        self.assertFalse(corrected)
        self.assertEqual(title, "Fourth Wing")

    def test_empty_title_gets_filled(self):
        import urllib.request
        orig = self._mock_openlibrary("Daemon", "Daniel Suarez")
        try:
            title, author, corrected = pp.correct_metadata(
                "", "", {"isbn": "9780000000000"})
        finally:
            urllib.request.urlopen = orig
        self.assertTrue(corrected)
        self.assertEqual(title, "Daemon")
        self.assertEqual(author, "Daniel Suarez")


class TestVerifyFlags(unittest.TestCase):
    def test_new_and_missing_characters(self):
        flags = pp._verify_flags(
            [{"name": "Violet"}, {"name": "Xaden"}],
            [{"name": "Violet"}, {"name": "Mira"}],
            [], {}, set(), set())
        names = {f[0]: f[2] for f in flags}
        self.assertIn("NEW CHARACTERS", names)
        self.assertIn("Xaden", names["NEW CHARACTERS"])
        self.assertIn("MISSING CHARACTERS", names)
        self.assertIn("Mira", names["MISSING CHARACTERS"])

    def test_noise_filtered(self):
        flags = pp._verify_flags(
            [], [{"name": "Dad"}, {"name": "Jason"}, {"name": "Mira"}],
            [], {}, set(), set())
        names = {f[0]: f[2] for f in flags}
        self.assertNotIn("Dad", str(names.get("MISSING CHARACTERS", [])))
        self.assertIn("DROPPED NOISE", names)

    def test_duplicate_trope_ids(self):
        flags = pp._verify_flags(
            [], [],
            [{"trope_id": "abc", "status": "candidate"},
             {"trope_id": "abc", "status": "confirmed"},
             {"trope_id": "def", "status": "candidate"}],
            {}, set(), set())
        names = {f[0]: f[2] for f in flags}
        # Same trope_id in different statuses IS a dupe (write-path invariant)
        self.assertIn("DUPLICATE TROPE CLAIMS", names)

    def test_no_dupe_different_ids(self):
        flags = pp._verify_flags(
            [], [],
            [{"trope_id": "abc", "status": "candidate"},
             {"trope_id": "def", "status": "candidate"}],
            {}, set(), set())
        self.assertNotIn("DUPLICATE TROPE CLAIMS", {f[0] for f in flags})

    def test_suspicious_names(self):
        flags = pp._verify_flags(
            [{"name": "Gryphon Rider"}, {"name": "General X's husband"}, {"name": "Violet"}],
            [], [], {}, set(), set())
        names = {f[0]: f[2] for f in flags}
        self.assertIn("SUSPICIOUS NAMES", names)

    def test_trigger_reject_overlap(self):
        flags = pp._verify_flags(
            [], [], [],
            {"murder": "rejected", "war": "candidate"},
            set(), {"murder", "theft"})
        names = {f[0]: f[2] for f in flags}
        self.assertIn("TRIGGERS ALREADY REJECTED", names)
        self.assertIn("murder", names["TRIGGERS ALREADY REJECTED"])
        self.assertNotIn("theft", str(names.get("TRIGGERS ALREADY REJECTED", [])))

    def test_name_stripping(self):
        flags = pp._verify_flags(
            [{"name": "Mira "}], [{"name": " Mira"}],
            [], {}, set(), set())
        # Same name with whitespace differences -> no flags
        self.assertNotIn("NEW CHARACTERS", {f[0] for f in flags})
        self.assertNotIn("MISSING CHARACTERS", {f[0] for f in flags})

    def test_null_names_safe(self):
        flags = pp._verify_flags(
            [{"name": None}], [{"name": None}],
            [], {}, set(), set())
        # Should not crash
        self.assertIsInstance(flags, list)


class TestTropeChapters(unittest.TestCase):
    def test_paraphrase_maps_to_display_key(self):
        # A paraphrase ("dragon bonding") that maps to a catalog trope
        # ("Dragons") must have its chapters keyed by the display name,
        # not the raw paraphrase text.
        import unittest.mock as mock
        # Mock catalog: one trope "Dragons" with fake vector
        fake_catalog = [("cid1", "Dragons", [1.0, 0.0])]
        # Mock embeddings: "dragon bonding" -> vector close to "Dragons"
        def fake_embed(base_url, model, texts):
            return [[1.0, 0.0] for _ in texts]
        orig_config = dict(pp.CONFIG)
        pp.CONFIG["embed_url"] = "http://fake"
        pp.CONFIG["trope_map_threshold"] = 0.5
        try:
            with mock.patch.object(pp, "trope_catalog_vectors", return_value=fake_catalog), \
                 mock.patch.object(pp, "embed_vectors", side_effect=fake_embed):
                bs = {
                    1: {"summary": "s", "spice_level": 1,
                        "triggers": {c: "none" for c in pp.TRIGGER_CATEGORIES},
                        "trigger_evidence": {}, "trope_candidates": ["dragon bonding"],
                        "quotes": []},
                    2: {"summary": "s", "spice_level": 1,
                        "triggers": {c: "none" for c in pp.TRIGGER_CATEGORIES},
                        "trigger_evidence": {}, "trope_candidates": ["dragon bonding"],
                        "quotes": []},
                }
                red = pp.v2_reduce({}, bs, {}, [_ch(1, "Ch1"), _ch(2, "Ch2")])
                # Chapters keyed by display name "dragons", not raw "dragon bonding"
                self.assertIn("dragons", red["trope_chapters"])
                self.assertNotIn("dragon bonding", red["trope_chapters"])
                self.assertEqual(len(red["trope_chapters"]["dragons"]), 2)
        finally:
            pp.CONFIG.clear()
            pp.CONFIG.update(orig_config)


class TestGrounding(unittest.TestCase):
    def test_grounded_character(self):
        chars = [{"name": "Violet", "aliases": ["Violence"]}]
        rels = []
        chapters = [{"label": "Ch 1", "text": "Violet walked in. Violence was her nickname."}]
        g = pp._ground_entities(chars, rels, chapters)
        self.assertTrue(chars[0]["grounded"])
        self.assertEqual(g["characters_grounded"], 1)
        self.assertEqual(g["ungrounded_names"], [])

    def test_hallucinated_character(self):
        chars = [{"name": "Jason", "aliases": []}]
        rels = []
        chapters = [{"label": "Ch 1", "text": "Violet and Xaden talked."}]
        g = pp._ground_entities(chars, rels, chapters)
        self.assertFalse(chars[0]["grounded"])
        self.assertEqual(chars[0]["variants_matched"], 0)
        self.assertIn("Jason", g["ungrounded_names"])

    def test_alias_grounds_character(self):
        chars = [{"name": "Tairneanach", "aliases": ["Tairn"]}]
        rels = []
        chapters = [{"label": "Ch 1", "text": "Tairn roared."}]
        g = pp._ground_entities(chars, rels, chapters)
        self.assertTrue(chars[0]["grounded"])

    def test_relationship_grounded(self):
        chars = []
        rels = [{"from": "Violet", "to": "Xaden", "chapter": "Ch 1"}]
        chapters = [{"label": "Ch 1", "text": "Violet looked at Xaden."}]
        g = pp._ground_entities(chars, rels, chapters)
        self.assertTrue(rels[0]["grounded"])
        self.assertEqual(g["relationships_grounded"], 1)

    def test_relationship_not_cogrounded(self):
        chars = []
        rels = [{"from": "Violet", "to": "Jason", "chapter": "Ch 1"}]
        chapters = [{"label": "Ch 1", "text": "Violet looked at Xaden."}]
        g = pp._ground_entities(chars, rels, chapters)
        self.assertFalse(rels[0]["grounded"])

    def test_case_insensitive(self):
        chars = [{"name": "VIOLET", "aliases": []}]
        rels = []
        chapters = [{"label": "Ch 1", "text": "violet whispered."}]
        pp._ground_entities(chars, rels, chapters)
        self.assertTrue(chars[0]["grounded"])

    def test_word_boundary_no_substring(self):
        # "Bo" must not match inside "book" — false grounded is the
        # dangerous direction
        chars = [{"name": "Bo", "aliases": []}]
        rels = []
        chapters = [{"label": "Ch 1", "text": "She opened the book."}]
        pp._ground_entities(chars, rels, chapters)
        self.assertFalse(chars[0]["grounded"])

    def test_word_boundary_with_punctuation(self):
        chars = [{"name": "St. John", "aliases": []}]
        rels = []
        chapters = [{"label": "Ch 1", "text": "St. John arrived."}]
        pp._ground_entities(chars, rels, chapters)
        self.assertTrue(chars[0]["grounded"])


class TestAliasValidation(unittest.TestCase):
    def test_reject_possessive(self):
        self.assertFalse(pp._is_valid_alias("Sebeck's wife"))
        self.assertFalse(pp._is_valid_alias("Sebeck's son"))

    def test_reject_relationship_words(self):
        self.assertFalse(pp._is_valid_alias("wife"))
        self.assertFalse(pp._is_valid_alias("son"))
        self.assertFalse(pp._is_valid_alias("Mrs. Sebeck"))

    def test_reject_generic_descriptors(self):
        self.assertFalse(pp._is_valid_alias("the major"))
        self.assertFalse(pp._is_valid_alias("the narrator"))

    def test_accept_real_aliases(self):
        self.assertTrue(pp._is_valid_alias("Pete Sebeck"))
        self.assertTrue(pp._is_valid_alias("Trip"))
        self.assertTrue(pp._is_valid_alias("Leonard Littleton"))

    def test_rejected_aliases_preserved(self):
        roster = {}
        pp._roster_update(roster, [
            {"name": "Peter Sebeck", "aliases": ["Pete Sebeck", "Sebeck's wife", "wife"]}
        ], 0)
        e = list(roster.values())[0]
        self.assertIn("Pete Sebeck", e["aliases"])
        self.assertNotIn("Sebeck's wife", e["aliases"])
        self.assertNotIn("wife", e["aliases"])
        self.assertIn("Sebeck's wife", e.get("unresolved_mentions", set()))

    def test_pete_not_merged_without_validation(self):
        # "Pete" alone should not merge two named characters
        roster = {}
        pp._roster_update(roster, [{"name": "Peter Sebeck", "aliases": []}], 0)
        pp._roster_update(roster, [{"name": "Jon Ross", "aliases": ["Pete"]}], 1)
        # Two separate roster entries (no merge on weak alias)
        self.assertEqual(len(roster), 2)


class TestAreaA(unittest.TestCase):
    def _roster_with(self, entries):
        """Build a roster from [(chapter_idx, char_dict), ...]."""
        roster = {}
        for idx, c in entries:
            pp._roster_update(roster, [c], idx)
        return roster

    def _reduce_chars(self, roster, n_chapters=5):
        chapters = [{"index": i, "label": f"Ch {i}", "text": "x" * 2000}
                    for i in range(1, n_chapters + 1)]
        red = pp.v2_reduce({}, {}, roster, chapters)
        return {c["name"]: c for c in red["characters"]}

    def test_sticky_death(self):
        # Reported dead ch2 with evidence, never acts again -> dead
        roster = self._roster_with([
            (1, {"name": "Bob", "status": "alive", "evidence": "Bob walked in."}),
            (2, {"name": "Bob", "status": "dead", "evidence": "Bob was killed."}),
            (3, {"name": "Bob", "status": "alive"}),  # no evidence = just mentioned
        ])
        chars = self._reduce_chars(roster)
        self.assertEqual(chars["Bob"]["status"], "dead")

    def test_sticky_death_cleared_by_action(self):
        # Dead ch2, but acts on page ch4 with evidence -> not dead
        roster = self._roster_with([
            (1, {"name": "Bob", "status": "alive", "evidence": "Bob walked in."}),
            (2, {"name": "Bob", "status": "dead", "evidence": "Bob was killed."}),
            (4, {"name": "Bob", "status": "alive", "evidence": "Bob stood up, alive."}),
        ])
        chars = self._reduce_chars(roster)
        self.assertNotEqual(chars["Bob"]["status"], "dead")

    def test_minor_appearance_blanked(self):
        roster = self._roster_with([
            (1, {"name": "Timmy", "role": "minor",
                 "description": "a young boy of ten",
                 "appearance": "blond hair, blue eyes",
                 "evidence": "Timmy ran."}),
        ])
        chars = self._reduce_chars(roster)
        self.assertEqual(chars["Timmy"]["appearance"], "")

    def test_sexualized_appearance_blanked(self):
        roster = self._roster_with([
            (1, {"name": "Jane", "role": "supporting",
                 "description": "a woman",
                 "appearance": "voluptuous figure in a tight dress",
                 "evidence": "Jane entered."}),
        ])
        chars = self._reduce_chars(roster)
        self.assertEqual(chars["Jane"]["appearance"], "")

    def test_adult_appearance_kept(self):
        roster = self._roster_with([
            (1, {"name": "Jane", "role": "supporting",
                 "description": "a woman",
                 "appearance": "tall, dark hair",
                 "evidence": "Jane entered."}),
        ])
        chars = self._reduce_chars(roster)
        self.assertEqual(chars["Jane"]["appearance"], "tall, dark hair")

    def test_description_prefers_earliest_evidence(self):
        roster = self._roster_with([
            (1, {"name": "Bob", "description": "short",
                 "evidence": "Bob spoke."}),
            (2, {"name": "Bob",
                 "description": "a much longer description of Bob here",
                 "evidence": "Bob acted again."}),
            (3, {"name": "Bob",
                 "description": "the longest description of all, but no evidence"}),
        ])
        chars = self._reduce_chars(roster)
        # Earliest evidence-backed wins over longer later ones
        self.assertEqual(chars["Bob"]["description"], "short")

    def test_description_falls_back_to_longest(self):
        roster = self._roster_with([
            (1, {"name": "Bob", "description": "short"}),
            (2, {"name": "Bob", "description": "a longer description here"}),
        ])
        chars = self._reduce_chars(roster)
        self.assertEqual(chars["Bob"]["description"], "a longer description here")


class TestAreaB(unittest.TestCase):
    def test_frontmatter_content_filter(self):
        units = [
            {"spine": "part0001.html", "label": "Copyright",
             "text": "Copyright 2020 by Author. All rights reserved. ISBN 123-456."},
            {"spine": "part0002.html", "label": "Chapter 1",
             "text": "It was a dark night. " * 200},
            {"spine": "part0003.html", "label": "Also By",
             "text": "Also by the author: Book One, Book Two. " * 50},
        ]
        result = pp.split_chapters(units)
        labels = [c["label"] for c in result]
        # Only Chapter 1 survives (others are frontmatter by content)
        self.assertTrue(any("Chapter 1" in lb for lb in labels))
        self.assertFalse(any("Copyright" in lb or "Also By" in lb for lb in labels))

    def test_fallback_trigger_few_units(self):
        pp._DIAG.chapter_detection_fallback = False  # reset (as process_file_v2 does)
        # 2 units (fewer than 5) with chapter headings -> fallback splits
        ch_text = ""
        for i in range(1, 7):
            ch_text += f"\nChapter {i}\n" + ("Story text here. " * 200) + "\n"
        units = [
            {"spine": "part0001.html", "label": "Part 1", "text": ch_text[:len(ch_text)//2]},
            {"spine": "part0002.html", "label": "Part 2", "text": ch_text[len(ch_text)//2:]},
        ]
        result = pp.split_chapters(units)
        self.assertTrue(getattr(pp._DIAG, 'chapter_detection_fallback', False))
        # Should have split into chapter-ish units
        self.assertGreater(len(result), 2)

    def test_fallback_trigger_dominant_unit(self):
        pp._DIAG.chapter_detection_fallback = False  # reset (as process_file_v2 does)
        # One unit holds >60% of text -> fallback
        big = "\nChapter One\n" + ("Text. " * 1000) + "\nChapter Two\n" + ("More. " * 1000)
        units = [
            {"spine": "a.html", "label": "A", "text": "short " * 100},
            {"spine": "b.html", "label": "B", "text": "short " * 100},
            {"spine": "c.html", "label": "C", "text": "short " * 100},
            {"spine": "d.html", "label": "D", "text": "short " * 100},
            {"spine": "e.html", "label": "E", "text": "short " * 100},
            {"spine": "big.html", "label": "Big", "text": big},
        ]
        result = pp.split_chapters(units)
        self.assertTrue(getattr(pp._DIAG, 'chapter_detection_fallback', False))

    def test_no_fallback_normal(self):
        pp._DIAG.chapter_detection_fallback = False  # reset (as process_file_v2 does)
        units = [
            {"spine": f"ch{i:02d}.html", "label": f"Chapter {i}",
             "text": f"Chapter {i} content. " * 300}
            for i in range(1, 8)
        ]
        result = pp.split_chapters(units)
        self.assertFalse(getattr(pp._DIAG, 'chapter_detection_fallback', False))
        self.assertEqual(len(result), 7)

    def test_heading_re_with_title(self):
        # "Chapter 1: The Beginning" should match
        m = pp._CHAPTER_HEADING_RE.search("Chapter 1: The Beginning")
        self.assertIsNotNone(m)
        m = pp._CHAPTER_HEADING_RE.search("PART II - The Journey")
        self.assertIsNotNone(m)
        m = pp._CHAPTER_HEADING_RE.search("Prologue")
        self.assertIsNotNone(m)
        # TOC-like short lines are matched by RE but filtered by length in split
        m = pp._CHAPTER_HEADING_RE.search("chapter 3")
        self.assertIsNotNone(m)

    def test_heading_re_number_words(self):
        m = pp._CHAPTER_HEADING_RE.search("Chapter Three")
        self.assertIsNotNone(m)
        m = pp._CHAPTER_HEADING_RE.search("Section IV: Revelations")
        self.assertIsNotNone(m)


class TestAreaC(unittest.TestCase):
    def test_trope_conf_tnorm_key(self):
        # trope_conf must be keyed by _tnorm for readers (save_preview, write_claims)
        c = "Enemies to Lovers"
        key = pp._tnorm(c)
        self.assertEqual(key, "enemies to lovers")
        # Simulate what process_file_v2 now writes
        trope_conf = {pp._tnorm(c): 0.85}
        # Readers use _tnorm lookup
        self.assertEqual(trope_conf.get(pp._tnorm(c)), 0.85)
        self.assertEqual(trope_conf.get(pp._tnorm("ENEMIES TO LOVERS")), 0.85)

    def test_denylist(self):
        self.assertTrue(pp._is_denylisted_trope("techno-thriller"))
        self.assertTrue(pp._is_denylisted_trope("Techno-Thriller"))
        self.assertTrue(pp._is_denylisted_trope("sci-fi"))
        self.assertTrue(pp._is_denylisted_trope("dystopia"))
        self.assertFalse(pp._is_denylisted_trope("enemies to lovers"))
        self.assertFalse(pp._is_denylisted_trope("chosen one"))

    def test_denylist_tnorm_variants(self):
        # Underscores/spacing variants still match
        self.assertTrue(pp._is_denylisted_trope("science_fiction"))
        self.assertTrue(pp._is_denylisted_trope("Science Fiction"))


class TestClaudeReviewFixes(unittest.TestCase):
    def test_alias_keys_filtered(self):
        # Bug 1: invalid aliases must not feed merge keys
        roster = {}
        pp._roster_update(roster, [
            {"name": "Peter Sebeck", "aliases": ["wife", "Pete"]}
        ], 0)
        e = list(roster.values())[0]
        self.assertNotIn(pp.norm_name("wife"), e["alias_keys"])
        # "Pete" is valid and should be in keys
        self.assertIn(pp.norm_name("Pete"), e["alias_keys"])

    def test_wife_no_cross_merge(self):
        # Two characters both listing "wife" must NOT merge
        roster = {}
        pp._roster_update(roster, [{"name": "Peter Sebeck", "aliases": ["wife"]}], 0)
        pp._roster_update(roster, [{"name": "John Smith", "aliases": ["wife"]}], 1)
        self.assertEqual(len(roster), 2)

    def test_possessive_pronoun_alias(self):
        self.assertFalse(pp._is_valid_alias("his wife"))
        self.assertFalse(pp._is_valid_alias("her son"))
        self.assertFalse(pp._is_valid_alias("my mother"))

    def test_minor_age_pattern(self):
        self.assertTrue(pp._MINOR_RE.search("seven-year-old boy"))
        self.assertTrue(pp._MINOR_RE.search("16-year-old girl"))

    def test_trope_denylist_hyphen(self):
        self.assertTrue(pp._is_denylisted_trope("sci fi"))
        self.assertTrue(pp._is_denylisted_trope("science-fiction"))
        self.assertTrue(pp._is_denylisted_trope("SCI-FI"))

    def test_narrator_still_merges(self):
        # "the narrator" rejected as display alias but still merges
        roster = {}
        pp._roster_update(roster, [
            {"name": "Shirley Jackson", "aliases": ["the narrator"]}], 0)
        pp._roster_update(roster, [
            {"name": "The Narrator", "aliases": []}], 1)
        self.assertEqual(len(roster), 1)


class TestTriggerNegativeGate(unittest.TestCase):
    def _trig_reduce(self, cat, severity, quotes, n_chapters=2):
        """Run v2_reduce with a single trigger category."""
        triggers = {c: "none" for c in pp.TRIGGER_CATEGORIES}
        triggers[cat] = severity
        bs = {}
        for i in range(1, n_chapters + 1):
            b = _b(triggers, spice=0)
            b["trigger_evidence"] = {cat: quotes[0]} if quotes else {}
            # spread quotes across chapters if multiple
            if len(quotes) > 1 and i <= len(quotes):
                b["trigger_evidence"] = {cat: quotes[i - 1]}
            bs[i] = b
        chs = [_ch(i, f"Ch{i}") for i in range(1, n_chapters + 1)]
        red = pp.v2_reduce({}, bs, {}, chs)
        return [t for t in red["triggers"] if t["warning"] == cat]

    def test_suicide_hypothetical_kept(self):
        # "afraid she might kill herself" is a genuine concern -> kept
        # (the overbroad hypothetical-word filter was removed 2026-10-08)
        res = self._trig_reduce("suicide", "on_page",
                                ["He was afraid she might kill herself"])
        self.assertEqual(len(res), 1)

    def test_suicide_metaphor_dropped(self):
        res = self._trig_reduce("suicide", "graphic",
                                ["It would be suicide to go in there alone"])
        self.assertEqual(len(res), 0)

    def test_suicide_digital_dropped(self):
        res = self._trig_reduce("suicide", "on_page",
                                ["She was committing digital suicide with that post"])
        self.assertEqual(len(res), 0)

    def test_suicide_real_kept(self):
        res = self._trig_reduce("suicide", "graphic",
                                ["He took his own life last winter"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["severity"], "graphic")

    def test_suicide_ambiguous_downgraded(self):
        # Evidence has no suicide language but isn't contradicted ->
        # conservative downgrade to mentioned, claim kept (2 chapters)
        res = self._trig_reduce("suicide", "on_page",
                                ["They walked to the store together"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["severity"], "mentioned")
        self.assertEqual(res[0]["severity_claimed"], "on_page")

    def test_suicide_single_chapter_hypothetical_kept(self):
        # "might kill himself" is ambiguous -> kept (conservative for warnings)
        # The overbroad hypothetical-word filter was removed 2026-10-08.
        res = self._trig_reduce("suicide", "on_page",
                                ["He might kill himself"], n_chapters=1)
        self.assertEqual(len(res), 1)


class TestRemainingFixes(unittest.TestCase):
    # --- Area 1: self-relationship guard + unordered dedupe ---
    def _rel_reduce(self, rels_by_chapter):
        """rel_by_chapter: {idx: [(from, to, type, evidence)]}"""
        chapter_as = {}
        for idx, rels in rels_by_chapter.items():
            chapter_as[idx] = {"relationships": [
                {"from": f, "to": t, "type": ty, "evidence": ev}
                for f, t, ty, ev in rels]}
        chs = [_ch(i, f"Ch{i}") for i in sorted(rels_by_chapter)]
        red = pp.v2_reduce(chapter_as, {}, {}, chs)
        return red["relationships"]

    def test_self_relationship_dropped(self):
        rels = self._rel_reduce({1: [("Alice", "Alice", "friend", "ev")]})
        self.assertEqual(len(rels), 0)

    def test_self_relationship_case_insensitive(self):
        rels = self._rel_reduce({1: [("Alice", "alice", "friend", "ev")]})
        self.assertEqual(len(rels), 0)

    def test_unordered_dedupe_precedence(self):
        # spouse beats friend regardless of direction
        rels = self._rel_reduce({
            1: [("Alice", "Bob", "friend", "they hung out")],
            2: [("Bob", "Alice", "spouse", "they married")],
        })
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["type"], "spouse")

    def test_unordered_dedupe_same_type(self):
        # Same pair, same type, different directions -> one relationship
        rels = self._rel_reduce({
            1: [("Alice", "Bob", "friend", "ev1")],
            2: [("Bob", "Alice", "friend", "ev2")],
        })
        self.assertEqual(len(rels), 1)
        # Importance reflects both chapters (2/2 chapters = max importance)
        self.assertEqual(rels[0]["importance"], 5)

    def test_nonexclusive_extra_with_evidence(self):
        # New behavior: one relationship per pair (highest confidence wins).
        # friend (ch1) vs mentor (ch2), both 1 quote -> tie on confidence,
        # tie on specificity -> earliest chapter wins.
        rels = self._rel_reduce({
            1: [("Alice", "Bob", "friend", "they hung out")],
            2: [("Alice", "Bob", "mentor", "she taught him")],
        })
        types = sorted(r["type"] for r in rels)
        self.assertEqual(types, ["friend"])
        # Winner carries confidence metadata.
        self.assertIn("confidence", rels[0])
        self.assertIn("evidence_count", rels[0])
        self.assertIn("cooccurrence_count", rels[0])

    def test_nonexclusive_extra_without_evidence_dropped(self):
        rels = self._rel_reduce({
            1: [("Alice", "Bob", "friend", "they hung out")],
            2: [("Alice", "Bob", "mentor", "")],
        })
        types = [r["type"] for r in rels]
        self.assertEqual(types, ["friend"])

    # --- Area 2: spice ---
    def test_spice_no_explicit_sex_floor(self):
        # The old explicit_sex consistency floor is gone: spice comes only
        # from the matrix. Triggers warn content EXISTS; spice measures
        # PERVASIVENESS. Here explicit_sex is on_page but chapters have
        # spice 0 and 1 -> matrix gives mild x pervasive = 2.
        triggers = {c: "none" for c in pp.TRIGGER_CATEGORIES}
        triggers["explicit_sex"] = "on_page"
        b1 = _b(triggers, spice=0)
        b1["trigger_evidence"] = {"explicit_sex": "they had explicit sex"}
        b2 = _b(triggers, spice=1)
        b2["trigger_evidence"] = {"explicit_sex": "more explicit sex"}
        bs = {1: b1, 2: b2}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        self.assertEqual(red["spice_level"], 2)

    def test_spice_peak_stored(self):
        bs = {i: _b(spice=s) for i, s in enumerate([0, 2, 4], start=1)}
        red = pp.v2_reduce({}, bs, {}, [_ch(i, str(i)) for i in range(1, 4)])
        self.assertEqual(red["spice_peak"], 4)
        self.assertNotIn("spice_avg_nonzero", red)

    # --- Area 3: quotes ---
    def _quote_reduce(self, quotes_by_chapter, summaries=None):
        bs = {}
        for idx, quotes in quotes_by_chapter.items():
            b = _b(quotes=quotes)
            if summaries and idx in summaries:
                b["summary"] = summaries[idx]
            bs[idx] = b
        chs = [_ch(i, f"Ch{i}") for i in sorted(quotes_by_chapter)]
        return pp.v2_reduce({}, bs, {}, chs)["quotes"]

    def test_quote_length_filter(self):
        quotes = [
            {"text": "Short.", "speaker": "Alice", "spoiler": False},
            {"text": "This is a much longer quote that definitely exceeds forty characters.", "speaker": "Bob", "spoiler": False},
        ]
        res = self._quote_reduce({1: quotes})
        texts = [q["text"] for q in res]
        self.assertNotIn("Short.", texts)
        self.assertEqual(len(texts), 1)

    def test_quote_speaker_required(self):
        quotes = [
            {"text": "This quote has no speaker and is long enough to pass length.", "speaker": None, "spoiler": False},
            {"text": "This quote has a speaker and is also long enough to pass.", "speaker": "Alice", "spoiler": False},
        ]
        res = self._quote_reduce({1: quotes})
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["speaker"], "Alice")

    def test_quote_summary_overlap_dropped(self):
        quotes = [
            {"text": "The dragon burned the village to ashes that night.", "speaker": "Alice", "spoiler": False},
        ]
        summaries = {1: "The dragon burned the village to ashes that night. Everyone fled."}
        res = self._quote_reduce({1: quotes}, summaries)
        self.assertEqual(len(res), 0)

    def test_quote_limit(self):
        # 25 candidates -> keep 8
        quotes = [
            {"text": f"Quote number {i:02d} with enough length to pass the filter here.", "speaker": f"Spk{i}", "spoiler": False}
            for i in range(25)
        ]
        res = self._quote_reduce({1: quotes})
        self.assertEqual(len(res), 8)


class TestTriggerGates(unittest.TestCase):
    def _gate(self, cat, evidence):
        # Replicates the gate logic in v2_reduce
        import re
        pos = pp._TRIGGER_EVIDENCE_RE.get(cat)
        neg = pp._TRIGGER_NEGATIVE_RE.get(cat)
        if neg and neg.search(evidence):
            return "drop"
        if pos and pos.search(evidence):
            return "keep"
        return "downgrade"

    def test_suicide_real_threat_kept(self):
        # "would" must NOT kill this — Advisor's false-negative case
        self.assertEqual(
            self._gate("suicide", "He told her he would kill himself if she left"),
            "keep")

    def test_suicide_from_clause_kept(self):
        # "from" must NOT kill this
        self.assertEqual(
            self._gate("suicide", "He walked away from the building and took his own life"),
            "keep")

    def test_suicide_hyperbole_dropped(self):
        self.assertEqual(
            self._gate("suicide", "He was hitting himself over the mistake"),
            "drop")

    def test_suicide_metaphor_dropped(self):
        self.assertEqual(
            self._gate("suicide", "Taking that job would be suicide"),
            "drop")

    def test_suicide_digital_dropped(self):
        self.assertEqual(
            self._gate("suicide", "He was committing digital suicide by deleting everything"),
            "drop")

    def test_suicide_ambiguous_downgraded(self):
        # No positive or negative match → downgrade, not drop
        self.assertEqual(
            self._gate("suicide", "They talked about death late into the night"),
            "downgrade")


class TestEvidenceAwareDedupe(unittest.TestCase):
    """GPT review: unverified first occurrence must not block verified later one."""

    def test_verified_later_wins(self):
        # Simulate the dedupe logic: Ch2 no evidence, Ch7 with evidence
        # The evidenced candidate must win
        cands = [
            {"from": "Alice", "to": "Bob", "type": "friend",
             "evidence": "", "_idx": 2},
            {"from": "Alice", "to": "Bob", "type": "friend",
             "evidence": "They laughed together.", "_idx": 7},
        ]
        _best = {}
        for c in cands:
            t = c["type"]
            cur = _best.get(t)
            if (cur is None
                    or (bool(c["evidence"]) and not bool(cur["evidence"]))
                    or (bool(c["evidence"]) == bool(cur["evidence"])
                        and c["_idx"] < cur["_idx"])):
                _best[t] = c
        self.assertEqual(_best["friend"]["evidence"], "They laughed together.")
        self.assertEqual(_best["friend"]["_idx"], 7)

    def test_different_types_kept_separate(self):
        # friend and enemy are different types, both kept
        cands = [
            {"from": "Alice", "to": "Bob", "type": "friend",
             "evidence": "They laughed.", "_idx": 2},
            {"from": "Alice", "to": "Bob", "type": "enemy",
             "evidence": "They fought.", "_idx": 7},
        ]
        _best = {}
        for c in cands:
            t = c["type"]
            cur = _best.get(t)
            if (cur is None
                    or (bool(c["evidence"]) and not bool(cur["evidence"]))):
                _best[t] = c
        self.assertIn("friend", _best)
        self.assertIn("enemy", _best)


class TestVisualizer(unittest.TestCase):
    """Chapter visualizer: no-ops without Rich/TTY, state + thread safety."""

    def _ui_rich(self):
        ui = pp.PipelineUI()
        ui.rich = True  # force the rich path to exercise state logic
        return ui

    def test_viz_noop_without_rich(self):
        ui = pp.PipelineUI()
        ui.rich = False
        ui.viz_chapter("b1", "Ch 1", "Some text here. ")
        ui.viz_characters("b1", [{"name": "Peter Sebeck"}])
        ui.viz_event("b1", "+ New: Peter Sebeck")
        self.assertIsNone(ui._viz)

    def test_viz_noop_when_disabled(self):
        ui = self._ui_rich()
        ui.set_viz_enabled(False)
        ui.viz_chapter("b1", "Ch 1", "Some text here. ")
        ui.viz_characters("b1", [{"name": "Peter Sebeck"}])
        ui.viz_event("b1", "+ New: Peter Sebeck")
        self.assertIsNone(ui._viz)

    def test_viz_state_accumulation(self):
        ui = self._ui_rich()
        ui.viz_chapter("b1", "Ch 1", "Peter Sebeck walked in. Pete followed.")
        self.assertEqual(ui._viz["book"], "b1")
        self.assertEqual(ui._viz["label"], "Ch 1")
        self.assertIn("Peter Sebeck", ui._viz["text"])
        self.assertEqual(ui._viz["names"], [])
        ui.viz_characters("b1", [{"name": "Peter Sebeck"}, {"name": "Pete"}])
        self.assertEqual(ui._viz["names"], ["Peter Sebeck", "Pete"])
        ui.viz_event("b1", "+ New: Peter Sebeck")
        ui.viz_event("b1", "→ Merged: Pete → Peter Sebeck")
        self.assertEqual(len(ui._viz["events"]), 2)
        # Event feed caps at 8
        for i in range(10):
            ui.viz_event("b1", f"event {i}")
        self.assertEqual(len(ui._viz["events"]), 8)
        self.assertEqual(ui._viz["events"][-1], "event 9")

    def test_viz_thread_safety(self):
        import threading
        ui = self._ui_rich()
        errors = []

        def worker(n):
            try:
                for i in range(20):
                    ui.viz_chapter(f"b{n}", f"Ch {i}", "text " * 50)
                    ui.viz_event(f"b{n}", f"+ New: Char {n}-{i}")
                    ui.viz_characters(f"b{n}", [{"name": f"Char {n}-{i}"}])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(ui._viz["events"]), 8)

    def test_viz_stats_and_milestone(self):
        ui = self._ui_rich()
        ui.viz_chapter("b1", "Ch 1", "x" * 100, chap_idx=1, chap_total=10)
        self.assertEqual(ui._viz["chap_idx"], 1)
        self.assertEqual(ui._viz["chap_total"], 10)
        self.assertEqual(ui._viz["new_count"], 0)
        # Second chapter carries run-level state across.
        ui.viz_chapter("b1", "Ch 2", "y" * 100, chap_idx=2, chap_total=10)
        self.assertEqual(ui._viz["chars_total"], 200)
        for i in range(25):
            ui.viz_event("b1", "+ New: Char %d" % i)
        self.assertEqual(ui._viz["new_count"], 25)
        self.assertIn(25, ui._viz["milestones"])
        self.assertTrue(any("🎉 25 characters discovered!" in e
                            for e in ui._viz["events"]))
        # Spotlight holds the latest discovery.
        self.assertEqual(ui._viz["spotlight"]["name"], "Char 24")
        # Ticker keeps the last 12 names.
        self.assertEqual(len(ui._viz["ticker"]), 12)
        self.assertEqual(ui._viz["ticker"][-1], "Char 24")
        self.assertEqual(ui._viz["ticker"][0], "Char 13")
        # No double milestone on the 26th.
        ui.viz_event("b1", "+ New: Char 25")
        self.assertEqual(
            sum("🎉" in e for e in ui._viz["events"]), 1)

    def test_viz_event_kind(self):
        self.assertEqual(pp._viz_event_kind("+ New: X"), "new")
        self.assertEqual(pp._viz_event_kind("→ Merged: A → B"), "merged")
        self.assertEqual(
            pp._viz_event_kind("✗ Filtered: \"x\" (not a person)"), "filtered")
        self.assertEqual(
            pp._viz_event_kind("🎉 25 characters discovered!"), "milestone")
        self.assertEqual(pp._viz_event_kind("something else"), "other")

    def test_viz_typing_and_stats_helpers(self):
        # Typing reveal is deterministic given (text, t0, now).
        self.assertEqual(pp._viz_revealed_text("abcdef", 1000.0, 1000.0), "a")
        long_text = "x" * 800
        self.assertEqual(
            len(pp._viz_revealed_text(long_text, 1000.0, 1010.0)), 300)
        self.assertEqual(
            pp._viz_revealed_text(long_text, 1000.0, 1100.0), long_text)
        # Stats line: 600 chars over 60s -> 2.5 tok/s avg.
        st = {"new_count": 7, "chap_idx": 3, "chap_total": 73,
              "run_t0": 1000.0, "chars_total": 600,
              "chap_t0": 1000.0, "chap_chars": 600}
        line = pp._viz_stats_line(st, 1060.0)
        self.assertIn("Characters: 7", line)
        self.assertIn("Chapter 3/73", line)
        self.assertIn("2.5 tok/s", line)


class TestDecisionValidator(unittest.TestCase):
    """Decision-model trigger validation (opt-in via --decision-model-url)."""

    def _mock_validator(self, response=None, exc=None):
        import json as _json
        import urllib.request as _urlreq

        class _FakeResp:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        orig = _urlreq.urlopen
        def fake(req, timeout=None):
            if exc:
                raise exc
            return _FakeResp(_json.dumps(response).encode())
        _urlreq.urlopen = fake
        self.addCleanup(setattr, _urlreq, "urlopen", orig)
        return pp.DecisionValidator("http://127.0.0.1:8888/v1")

    def test_validate_parses_noul(self):
        dv = self._mock_validator(
            {"answers": {"trigger_check": {"type": "noul", "noul": 0.9}}})
        verdict, p = dv.validate("suicide", "he killed himself")
        self.assertTrue(verdict)
        self.assertAlmostEqual(p, 0.9)

    def test_validate_threshold(self):
        # suicide threshold is 0.82; 0.79 must not verify
        dv = self._mock_validator(
            {"answers": {"trigger_check": {"type": "noul", "noul": 0.79}}})
        verdict, p = dv.validate("suicide", "digital suicide")
        self.assertFalse(verdict)
        self.assertAlmostEqual(p, 0.79)

    def test_validate_error_falls_back(self):
        dv = self._mock_validator(exc=ConnectionError("refused"))
        verdict, p = dv.validate("suicide", "anything")
        self.assertIsNone(verdict)
        self.assertIsNone(p)

    def test_url_normalization(self):
        dv = pp.DecisionValidator("http://127.0.0.1:8888/v1")
        self.assertEqual(dv.endpoint, "http://127.0.0.1:8888/v1/systemone")
        dv2 = pp.DecisionValidator("http://127.0.0.1:8888/")
        self.assertEqual(dv2.endpoint, "http://127.0.0.1:8888/v1/systemone")

    def test_default_model(self):
        dv = pp.DecisionValidator("http://x/")
        self.assertEqual(dv.model, "default")

    def test_hi_lo_thresholds_derived(self):
        # hi = base, lo = hi - 0.15 floored at 0.10
        self.assertEqual(pp._DECISION_TRIGGER_THRESHOLDS_HI["suicide"], 0.82)
        self.assertEqual(pp._DECISION_TRIGGER_THRESHOLDS_LO["suicide"], 0.67)
        self.assertEqual(pp._DECISION_TRIGGER_THRESHOLDS_LO["sexual_violence"], 0.10)  # 0.20-0.15 floored
        # Keys match the base dict
        self.assertEqual(set(pp._DECISION_TRIGGER_THRESHOLDS_HI),
                         set(pp._DECISION_TRIGGER_THRESHOLDS))
        self.assertEqual(set(pp._DECISION_TRIGGER_THRESHOLDS_LO),
                         set(pp._DECISION_TRIGGER_THRESHOLDS))

    def test_missing_score_is_error_not_zero(self):
        # Missing noul AND missing probabilities.yes must fall back,
        # not silently score 0.
        dv = self._mock_validator(
            {"answers": {"trigger_check": {"type": "noul"}}})
        verdict, p = dv.validate("suicide", "anything")
        self.assertIsNone(verdict)
        self.assertIsNone(p)

    def test_non_numeric_score_is_error(self):
        dv = self._mock_validator(
            {"answers": {"trigger_check": {"type": "noul", "noul": "high"}}})
        verdict, p = dv.validate("suicide", "anything")
        self.assertIsNone(verdict)
        self.assertIsNone(p)

    def test_out_of_range_score_is_error(self):
        dv = self._mock_validator(
            {"answers": {"trigger_check": {"type": "noul", "noul": 1.5}}})
        verdict, p = dv.validate("suicide", "anything")
        self.assertIsNone(verdict)
        self.assertIsNone(p)

    def test_nan_score_is_error(self):
        import math
        dv = self._mock_validator(
            {"answers": {"trigger_check": {"type": "noul", "noul": float("nan")}}})
        # nan can't survive JSON round-trip, so call the validation inline
        # by patching the parsed value instead
        import json as _json
        import urllib.request as _urlreq

        class _FakeResp:
            def read(self):
                return b'{"answers": {"trigger_check": {"type": "noul", "noul": NaN}}}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        orig = _urlreq.urlopen
        _urlreq.urlopen = lambda req, timeout=None: _FakeResp()
        self.addCleanup(setattr, _urlreq, "urlopen", orig)
        # json.loads accepts NaN by default; the validator must reject it
        verdict, p = dv.validate("suicide", "anything")
        self.assertIsNone(verdict)
        self.assertIsNone(p)

    def test_circuit_breaker_disables_after_3_errors(self):
        dv = self._mock_validator(exc=ConnectionError("refused"))
        for _ in range(3):
            verdict, p = dv.validate("suicide", "anything")
            self.assertIsNone(verdict)
        self.assertTrue(dv.disabled)
        # Further calls short-circuit without hitting the network
        verdict, p = dv.validate("suicide", "anything")
        self.assertIsNone(verdict)
        self.assertIsNone(p)

    def test_circuit_breaker_resets_on_success(self):
        import json as _json
        import urllib.request as _urlreq

        calls = {"n": 0}

        class _FakeResp:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        orig = _urlreq.urlopen

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ConnectionError("refused")
            return _FakeResp(_json.dumps(
                {"answers": {"trigger_check": {"type": "noul", "noul": 0.9}}}
            ).encode())

        _urlreq.urlopen = fake
        self.addCleanup(setattr, _urlreq, "urlopen", orig)
        dv = pp.DecisionValidator("http://127.0.0.1:8888/v1")
        dv.validate("suicide", "x")  # error 1
        dv.validate("suicide", "x")  # error 2
        self.assertFalse(dv.disabled)
        verdict, p = dv.validate("suicide", "x")  # success resets
        self.assertTrue(verdict)
        self.assertFalse(dv.disabled)

    def test_context_included_in_state(self):
        import json as _json
        import urllib.request as _urlreq

        seen = {}

        class _FakeResp:
            def read(self):
                return _json.dumps(
                    {"answers": {"trigger_check": {"type": "noul", "noul": 0.9}}}
                ).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        orig = _urlreq.urlopen

        def fake(req, timeout=None):
            seen["payload"] = _json.loads(req.data.decode())
            return _FakeResp()

        _urlreq.urlopen = fake
        self.addCleanup(setattr, _urlreq, "urlopen", orig)
        dv = pp.DecisionValidator("http://127.0.0.1:8888/v1")
        dv.validate("suicide", "the quote", context="surrounding text here")
        state = seen["payload"]["state"]
        self.assertIn("surrounding text here", state)
        self.assertIn("the quote", state)

    def test_no_context_sends_bare_quote(self):
        import json as _json
        import urllib.request as _urlreq

        seen = {}

        class _FakeResp:
            def read(self):
                return _json.dumps(
                    {"answers": {"trigger_check": {"type": "noul", "noul": 0.9}}}
                ).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        orig = _urlreq.urlopen

        def fake(req, timeout=None):
            seen["payload"] = _json.loads(req.data.decode())
            return _FakeResp()

        _urlreq.urlopen = fake
        self.addCleanup(setattr, _urlreq, "urlopen", orig)
        dv = pp.DecisionValidator("http://127.0.0.1:8888/v1")
        dv.validate("suicide", "just the quote")
        self.assertEqual(seen["payload"]["state"], "just the quote")


class TestExtractQuoteContext(unittest.TestCase):
    """_extract_quote_context: windowed context around evidence quotes."""

    def test_exact_match(self):
        text = "A" * 500 + "the target quote here" + "B" * 500
        ctx = pp._extract_quote_context(text, "the target quote here", window=100)
        self.assertIn("the target quote here", ctx)
        self.assertLessEqual(len(ctx), 100 + len("the target quote here") + 100 + 1)

    def test_quote_at_start(self):
        text = "the quote" + "x" * 1000
        ctx = pp._extract_quote_context(text, "the quote", window=300)
        self.assertTrue(ctx.startswith("the quote"))

    def test_quote_not_found(self):
        ctx = pp._extract_quote_context("some chapter text", "missing quote")
        self.assertEqual(ctx, "")

    def test_empty_inputs(self):
        self.assertEqual(pp._extract_quote_context("", "quote"), "")
        self.assertEqual(pp._extract_quote_context("text", ""), "")

    def test_whitespace_normalized_fallback(self):
        text = "line one\nline two\nline three"
        # Quote with collapsed whitespace should still match
        ctx = pp._extract_quote_context(text, "line one line two")
        self.assertIn("line", ctx)


class TestLearnedState(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        # Fresh enabled instance per test (isolated learn dir).
        self.ls = pp.LearnedState(learn_dir=self.tmp, enabled=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_disabled_is_noop(self):
        ls = pp.LearnedState(enabled=False)
        ls.record_merge("pete", "peter", "isbn1")
        self.assertIsNone(ls.nick_lookup("pete"))
        ls.record_threshold("murder", 0.9, True)
        self.assertEqual(ls.threshold_stats, {})
        self.assertEqual(ls.trusted_titles(), frozenset())
        self.assertEqual(ls.pop_summary(), "")

    def test_nickname_record_and_lookup(self):
        self.ls.record_merge("pete sebeck", "peter sebeck", "isbn1")
        self.assertEqual(self.ls.nick_lookup("pete sebeck"), "peter sebeck")
        self.assertIsNone(self.ls.nick_lookup("unknown name"))
        # Count increments on repeat; books deduped.
        self.ls.record_merge("pete sebeck", "peter sebeck", "isbn1")
        self.ls.record_merge("pete sebeck", "peter sebeck", "isbn2")
        ent = self.ls.nicknames["pete sebeck"]
        self.assertEqual(ent["count"], 3)
        self.assertEqual(sorted(ent["books"]), ["isbn1", "isbn2"])

    def test_token_merge(self):
        self.ls.record_token_merge("pete", "peter", "isbn1")
        self.assertEqual(self.ls.token_lookup("pete"), "peter")
        self.assertIsNone(self.ls.token_lookup("bob"))
        # Too-short tokens are ignored.
        self.ls.record_token_merge("x", "xavier")
        self.assertIsNone(self.ls.token_lookup("x"))

    def test_title_trust_threshold(self):
        # One observation: not trusted.
        self.ls.observe_title("herr", "oberstleutnant heinrich boerner")
        self.assertEqual(self.ls.trusted_titles(), frozenset())
        # Two distinct remainders: trusted.
        self.ls.observe_title("herr", "oberstleutnant schmidt")
        self.assertIn("herr", self.ls.trusted_titles())
        # Same remainder twice doesn't count twice.
        self.ls.observe_title("herr", "oberstleutnant schmidt")
        self.assertEqual(len(self.ls.titles["herr"]["rests"]), 2)

    def test_title_ignores_known_and_stopwords(self):
        self.ls.observe_title("dr", "jane smith")
        self.ls.observe_title("the", "daemon")
        self.assertNotIn("dr", self.ls.titles)
        self.assertNotIn("the", self.ls.titles)

    def test_threshold_stats(self):
        self.ls.record_threshold("murder", 0.46, False)
        self.ls.record_threshold("murder", 0.72, True)
        ent = self.ls.threshold_stats["murder"]
        self.assertEqual(ent["dropped"], [0.46])
        self.assertEqual(ent["kept"], [0.72])

    def test_save_and_reload(self):
        self.ls.record_merge("pete", "peter", "isbn1")
        self.ls.record_threshold("murder", 0.5, True)
        self.ls.save()
        ls2 = pp.LearnedState(learn_dir=self.tmp, enabled=True)
        self.assertEqual(ls2.nick_lookup("pete"), "peter")
        self.assertEqual(ls2.threshold_stats["murder"]["kept"], [0.5])

    def test_unwritable_dir_degrades(self):
        ls = pp.LearnedState(learn_dir="/proc/definitely-not-here", enabled=True)
        # Should not raise; in-memory learning still works, persistence skipped.
        ls.record_merge("pete", "peter")
        self.assertEqual(ls.nick_lookup("pete"), "peter")
        ls.save()  # must not raise

    def test_series_key(self):
        self.assertEqual(pp.LearnedState.series_key("Daniel Suarez"),
                         "author_daniel_suarez")
        self.assertIsNone(pp.LearnedState.series_key(""))
        self.assertIsNone(pp.LearnedState.series_key(None))

    def test_series_roster_roundtrip(self):
        chars = [{"name": "Peter Sebeck", "aliases": ["Pete", "Detective Sebeck"]}]
        self.ls.save_series_roster("author_daniel_suarez", "Daemon",
                                   "Daniel Suarez", "isbn1", chars)
        hints = self.ls.load_series_hints("author_daniel_suarez")
        self.assertEqual(hints.get("pete"), "peter sebeck")
        self.assertEqual(hints.get("detective sebeck"), "peter sebeck")
        # Unknown series -> empty.
        self.assertEqual(self.ls.load_series_hints("author_nobody"), {})

    def test_import_labels(self):
        import tempfile, json, os
        labels = {
            "a|||b": {"a": "Pete Sebeck", "b": "Peter Sebeck",
                      "same_person": True, "source": "alias"},
            "c|||d": {"a": "Foo", "b": "Bar",
                      "same_person": False, "source": "similar"},
            "e|||f": {"a": "Maybe", "b": "Perhaps",
                      "same_person": True, "uncertain": True,
                      "source": "alias"},
        }
        p = os.path.join(self.tmp, "labels.json")
        with open(p, "w") as f:
            json.dump(labels, f)
        n = self.ls.import_labels(p)
        self.assertEqual(n, 1)
        self.assertEqual(self.ls.nick_lookup("pete sebeck"), "peter sebeck")

    def test_pop_summary(self):
        self.ls.record_merge("pete", "peter")
        self.ls.record_threshold("murder", 0.5, True)
        s = self.ls.pop_summary()
        self.assertIn("1 nickname", s)
        self.assertIn("1 threshold sample", s)
        # Second call: counters reset.
        self.assertEqual(self.ls.pop_summary(), "")

    def test_effective_titles_includes_learned(self):
        # Disabled global state -> only hardcoded titles.
        self.assertIn("dr", pp._effective_titles())
        self.assertNotIn("herr", pp._effective_titles())

    def test_thread_safety_smoke(self):
        import threading
        errs = []

        def hammer():
            try:
                for i in range(50):
                    self.ls.record_merge(f"nick{i % 5}", f"full{i % 5}")
                    self.ls.record_threshold("murder", 0.5, True)
                    self.ls.nick_lookup("nick1")
                    self.ls.trusted_titles()
            except Exception as e:
                errs.append(e)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])


class TestDecisionModelProvider(unittest.TestCase):
    """Provider selection for decision models (local vs OpenRouter)."""

    def test_local_uses_url_override(self):
        endpoint, model, key, headers = pp.resolve_decision_model(
            "local", "http://x:1/v1", {})
        self.assertEqual(endpoint, "http://x:1/v1")
        self.assertEqual(model, "default")
        self.assertEqual(key, "")
        self.assertEqual(headers, {})

    def test_local_falls_back_to_config(self):
        endpoint, model, key, headers = pp.resolve_decision_model(
            "local", "", {"openai_base_url": "http://cfg:2/v1"})
        self.assertEqual(endpoint, "http://cfg:2/v1")

    def test_local_missing_url_raises(self):
        with self.assertRaises(ValueError):
            pp.resolve_decision_model("local", "", {})

    def test_openrouter_endpoint_and_pinned_model(self):
        endpoint, model, key, headers = pp.resolve_decision_model(
            "openrouter", "", {"openrouter_api_key": "sk-test"}, env={})
        self.assertEqual(endpoint, "https://openrouter.ai/api/v1/systemone")
        self.assertEqual(model, "typesafe/jev-1.13")
        self.assertNotIn("latest", model)
        self.assertEqual(key, "sk-test")

    def test_openrouter_model_is_pinned_constant(self):
        # Guard against accidentally switching to the moving "latest" alias.
        self.assertEqual(pp.OPENROUTER_DECISION_MODEL, "typesafe/jev-1.13")

    def test_openrouter_key_from_env(self):
        endpoint, model, key, headers = pp.resolve_decision_model(
            "openrouter", "", {}, env={"OPENROUTER_API_KEY": "sk-env"})
        self.assertEqual(key, "sk-env")

    def test_openrouter_config_key_preferred_over_env(self):
        _, _, key, _ = pp.resolve_decision_model(
            "openrouter", "",
            {"openrouter_api_key": "sk-cfg"},
            env={"OPENROUTER_API_KEY": "sk-env"})
        self.assertEqual(key, "sk-cfg")

    def test_openrouter_missing_key_raises_gracefully(self):
        with self.assertRaises(ValueError) as ctx:
            pp.resolve_decision_model("openrouter", "", {}, env={})
        msg = str(ctx.exception)
        self.assertIn("OPENROUTER_API_KEY", msg)
        self.assertIn("openrouter_api_key", msg)

    def test_openrouter_attribution_header(self):
        _, _, _, headers = pp.resolve_decision_model(
            "openrouter", "", {"openrouter_api_key": "sk-test"}, env={})
        self.assertEqual(headers.get("X-Title"), "ebook-processor")

    def test_resolver_never_prints_key(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pp.resolve_decision_model(
                "openrouter", "",
                {"openrouter_api_key": "sk-super-secret-123"}, env={})
        self.assertNotIn("sk-super-secret-123", buf.getvalue())

    def _capture_headers(self, **dv_kwargs):
        import json as _json
        import urllib.request as _urlreq
        captured = {}

        class _FakeResp:
            def read(self):
                return _json.dumps(
                    {"answers": {"trigger_check":
                                 {"type": "noul", "noul": 0.9}}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        orig = _urlreq.urlopen

        def fake(req, timeout=None):
            for k, v in req.header_items():
                captured[k.lower()] = v
            return _FakeResp()

        _urlreq.urlopen = fake
        self.addCleanup(setattr, _urlreq, "urlopen", orig)
        dv = pp.DecisionValidator("http://x/v1", **dv_kwargs)
        dv.validate("suicide", "he killed himself")
        return captured

    def test_validator_sends_extra_headers(self):
        captured = self._capture_headers(
            extra_headers={"X-Title": "ebook-processor"})
        self.assertEqual(captured.get("x-title"), "ebook-processor")
        self.assertNotIn("authorization", captured)

    def test_validator_auth_header_bearer(self):
        captured = self._capture_headers(api_key="sk-test")
        self.assertEqual(captured.get("authorization"), "Bearer sk-test")

    def test_validator_openrouter_endpoint_normalization(self):
        # Full /v1/systemone URL must round-trip to itself.
        dv = pp.DecisionValidator("https://openrouter.ai/api/v1/systemone",
                                  model="typesafe/jev-1.13",
                                  api_key="sk-test",
                                  extra_headers={"X-Title": "ebook-processor"})
        self.assertEqual(dv.endpoint,
                         "https://openrouter.ai/api/v1/systemone")
        self.assertEqual(dv.model, "typesafe/jev-1.13")


class TestHybridMerge(unittest.TestCase):
    """Hybrid deterministic + decision-model character merge."""

    # -- Tier 2 helpers -------------------------------------------------
    def test_strip_titles(self):
        self.assertEqual(pp._strip_merge_noise("special agent neal decker"),
                         ["neal", "decker"])
        self.assertEqual(pp._strip_merge_noise("agent roy merritt"),
                         ["roy", "merritt"])
        self.assertEqual(pp._strip_merge_noise("detective peter sebeck"),
                         ["peter", "sebeck"])

    def test_strip_contextual_prefix(self):
        self.assertEqual(pp._strip_merge_noise("the late matthew sobol"),
                         ["matthew", "sobol"])
        self.assertEqual(pp._strip_merge_noise("official brian gragg"),
                         ["brian", "gragg"])

    def test_strip_middle_initial(self):
        self.assertEqual(pp._strip_merge_noise("matthew a. sobol"),
                         ["matthew", "sobol"])
        self.assertEqual(pp._strip_merge_noise("matthew a sobol"),
                         ["matthew", "sobol"])

    def test_spelling_variant(self):
        self.assertTrue(pp._is_spelling_variant("joseph", "josef"))
        self.assertTrue(pp._is_spelling_variant("mosely", "mosley"))
        self.assertTrue(pp._is_spelling_variant("sebeck", "sebeckx"))
        self.assertFalse(pp._is_spelling_variant("chris", "peter"))
        self.assertFalse(pp._is_spelling_variant("sebeck", "sebickson"))

    def test_levenshtein(self):
        self.assertEqual(pp._levenshtein("joseph", "josef"), 2)
        self.assertEqual(pp._levenshtein("abc", "abc"), 0)
        self.assertEqual(pp._levenshtein("", "abc"), 3)

    # -- Tier decisions --------------------------------------------------
    def test_tier2_title_strip(self):
        self.assertEqual(
            pp._merge_tier("special agent neal decker", "neal decker"), "yes")
        self.assertEqual(
            pp._merge_tier("agent roy merritt", "roy merritt"), "yes")

    def test_tier2_contextual_prefix(self):
        self.assertEqual(
            pp._merge_tier("the late matthew sobol", "matthew sobol"), "yes")

    def test_tier2_middle_initial(self):
        self.assertEqual(
            pp._merge_tier("matthew a. sobol", "matthew sobol"), "yes")

    def test_tier2_spelling_variant(self):
        self.assertEqual(
            pp._merge_tier("joseph pavlos", "josef pavlos"), "yes")

    def test_tier2_nickname(self):
        self.assertEqual(
            pp._merge_tier("pete sebeck", "peter sebeck"), "yes")

    def test_single_token_never_auto(self):
        # The surname-family trap: single-token -> multi-token is NEVER
        # automatic, even when unambiguous.
        self.assertEqual(
            pp._merge_tier("sebeck", "peter sebeck",
                           roster_keys={"peter sebeck"}),
            "ambiguous")
        self.assertEqual(pp._merge_tier("mosely", "charles mosely"), "ambiguous")

    def test_family_trap_hard_no(self):
        self.assertEqual(
            pp._merge_tier("chris sebeck", "peter sebeck"), "no")
        self.assertEqual(
            pp._merge_tier("laura sebeck", "peter sebeck"), "no")

    def test_tier2_beats_tier1(self):
        # Same chapter, but stripping makes them identical -> still merge
        # (Call A title inconsistency, not two people).
        self.assertEqual(
            pp._merge_tier("special agent neal decker", "neal decker",
                           chapter_keys={"neal decker",
                                         "special agent neal decker"}),
            "yes")

    def test_tier1_same_chapter(self):
        self.assertEqual(
            pp._merge_tier("chris sebeck", "peter sebeck",
                           chapter_keys={"chris sebeck", "peter sebeck"}),
            "no")

    def test_tier1_postpass_chapter_sets(self):
        self.assertEqual(
            pp._merge_tier("chris sebeck", "peter sebeck",
                           a_chapters={1, 2}, b_chapters={2, 3}), "no")
        self.assertEqual(
            pp._merge_tier("chris sebeck", "peter sebeck",
                           a_chapters={1}, b_chapters={3}), "no")  # family trap

    def test_ambiguous_by_default(self):
        self.assertEqual(
            pp._merge_tier("jon ross", "jason heider"), "ambiguous")

    # -- Author filter ---------------------------------------------------
    def test_is_author_name(self):
        self.assertTrue(pp._is_author_name("Daniel Suarez", "Daniel Suarez"))
        self.assertTrue(pp._is_author_name("daniel suarez", "Daniel Suarez"))
        self.assertTrue(pp._is_author_name("Suarez, Daniel", "Daniel Suarez"))
        self.assertFalse(pp._is_author_name("Peter Sebeck", "Daniel Suarez"))
        self.assertFalse(pp._is_author_name("", "Daniel Suarez"))

    def test_roster_update_skips_author(self):
        roster = {}
        pp._DIAG.book_author = "Daniel Suarez"
        self.addCleanup(setattr, pp._DIAG, "book_author", None)
        chars = [{"name": "Daniel Suarez", "aliases": [],
                  "role": "supporting", "description": "the author",
                  "appearance": "", "status": "alive", "evidence": ""},
                 {"name": "Peter Sebeck", "aliases": [],
                  "role": "protagonist", "description": "detective",
                  "appearance": "", "status": "alive", "evidence": ""}]
        pp._roster_update(roster, chars, 0)
        self.assertNotIn("daniel suarez", roster)
        self.assertIn("peter sebeck", roster)

    # -- _roster_update integration ---------------------------------------
    def _mk_char(self, name):
        return {"name": name, "aliases": [], "role": "supporting",
                "description": "d", "appearance": "", "status": "alive",
                "evidence": ""}

    def test_roster_update_tier2_merge(self):
        roster = {}
        pp._roster_update(roster, [self._mk_char("Neal Decker")], 0)
        pp._roster_update(roster, [self._mk_char("Special Agent Neal Decker")], 1)
        self.assertEqual(len(roster), 1)
        e = roster["neal decker"]
        self.assertIn("special agent neal decker", e["primary_keys"])

    def test_roster_update_family_trap(self):
        roster = {}
        pp._roster_update(roster, [self._mk_char("Peter Sebeck")], 0)
        # Same chapter lists both -> Tier 1 hard NO (also family trap).
        pp._roster_update(
            roster, [self._mk_char("Peter Sebeck"),
                     self._mk_char("Chris Sebeck")], 1)
        self.assertEqual(len(roster), 2)
        self.assertIn("peter sebeck", roster)
        self.assertIn("chris sebeck", roster)

    def test_roster_update_spelling_merge(self):
        roster = {}
        pp._roster_update(roster, [self._mk_char("Joseph Pavlos")], 0)
        pp._roster_update(roster, [self._mk_char("Josef Pavlos")], 1)
        self.assertEqual(len(roster), 1)

    # -- _merge_roster_entries ---------------------------------------------
    def test_merge_roster_entries(self):
        roster = {
            "matthew sobol": {
                "name": "Matthew Sobol", "aliases": set(),
                "alias_keys": {"matthew sobol"}, "primary_keys": {"matthew sobol"},
                "appearances": 5, "chapters": {1, 2}, "roles": Counter(),
                "descriptions": [(1, "game designer", True)], "evidence": "ev1",
            },
            "matthew a. sobol": {
                "name": "Matthew A. Sobol", "aliases": set(),
                "alias_keys": {"matthew a. sobol"},
                "primary_keys": {"matthew a. sobol"},
                "appearances": 2, "chapters": {3}, "roles": Counter(),
                "descriptions": [(3, "phd", False)], "evidence": "",
            },
        }
        pp._merge_roster_entries(roster, "matthew sobol", "matthew a. sobol")
        self.assertEqual(len(roster), 1)
        e = roster["matthew sobol"]
        self.assertEqual(e["appearances"], 7)
        self.assertEqual(e["chapters"], {1, 2, 3})
        self.assertIn("matthew a. sobol", e["alias_keys"])
        self.assertEqual(len(e["descriptions"]), 2)
        # Display name prefers the cleaner variant.
        self.assertEqual(e["name"], "Matthew Sobol")

    # -- post_pass_merge ----------------------------------------------------
    def _mk_entry(self, name, chapters, desc="d"):
        nk = pp.norm_name(name)
        return {nk: {
            "name": name, "aliases": set(), "alias_keys": {nk},
            "primary_keys": {nk}, "appearances": 1,
            "chapters": set(chapters), "roles": Counter(),
            "descriptions": [(min(chapters), desc, False)], "evidence": "",
        }}

    def test_postpass_tier2(self):
        roster = {}
        roster.update(self._mk_entry("Matthew Sobol", [1, 2]))
        roster.update(self._mk_entry("Matthew A. Sobol", [5]))
        n, possible = pp.post_pass_merge(roster, validator=None)
        self.assertEqual(n, 1)
        self.assertEqual(len(roster), 1)
        self.assertIn("matthew sobol", roster)

    def test_postpass_unresolved_without_validator(self):
        roster = {}
        roster.update(self._mk_entry("Sebeck", [1], "a detective"))
        roster.update(self._mk_entry("Peter Sebeck", [2], "a detective"))
        n, possible = pp.post_pass_merge(roster, validator=None)
        self.assertEqual(n, 0)
        self.assertEqual(len(roster), 2)
        self.assertEqual(len(possible), 1)
        self.assertEqual(possible[0]["reason"],
                         "unresolved (decision model disabled)")
        self.assertIsNone(possible[0]["p_yes"])

    def test_postpass_family_trap_not_candidate(self):
        roster = {}
        roster.update(self._mk_entry("Chris Sebeck", [1]))
        roster.update(self._mk_entry("Peter Sebeck", [2]))
        n, possible = pp.post_pass_merge(roster, validator=None)
        self.assertEqual(n, 0)
        self.assertEqual(possible, [])

    # -- ask_same_person ------------------------------------------------------
    def _mock_merge_validator(self, p_yes=None, exc=None):
        import json as _json
        import urllib.request as _urlreq

        class _FakeResp:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        orig = _urlreq.urlopen
        def fake(req, timeout=None):
            if exc:
                raise exc
            return _FakeResp(_json.dumps(
                {"answers": {"same_person": {"type": "noul",
                                             "noul": p_yes}}}).encode())
        _urlreq.urlopen = fake
        self.addCleanup(setattr, _urlreq, "urlopen", orig)
        return pp.DecisionValidator("http://127.0.0.1:8888/v1")

    def test_ask_same_person_merges(self):
        dv = self._mock_merge_validator(p_yes=0.92)
        merged, p = dv.ask_same_person("Pete", "detective", "", "Peter Sebeck",
                                       "detective", "")
        self.assertTrue(merged)
        self.assertAlmostEqual(p, 0.92)

    def test_ask_same_person_below_threshold(self):
        dv = self._mock_merge_validator(p_yes=0.60)
        merged, p = dv.ask_same_person("Chris Sebeck", "son", "",
                                       "Peter Sebeck", "detective", "")
        self.assertFalse(merged)
        self.assertAlmostEqual(p, 0.60)

    def test_ask_same_person_error(self):
        dv = self._mock_merge_validator(exc=ConnectionError("refused"))
        merged, p = dv.ask_same_person("A", "", "", "B", "", "")
        self.assertIsNone(merged)
        self.assertIsNone(p)

    def test_postpass_tier3_merges(self):
        roster = {}
        roster.update(self._mk_entry("Sebeck", [1], "detective"))
        roster.update(self._mk_entry("Peter Sebeck", [2], "detective"))
        dv = self._mock_merge_validator(p_yes=0.90)
        n, possible = pp.post_pass_merge(roster, validator=dv)
        self.assertEqual(n, 1)
        self.assertEqual(len(roster), 1)
        self.assertEqual(possible, [])

    def test_postpass_tier3_below_threshold(self):
        roster = {}
        roster.update(self._mk_entry("Sebeck", [1], "detective"))
        roster.update(self._mk_entry("Peter Sebeck", [2], "detective"))
        dv = self._mock_merge_validator(p_yes=0.60)
        n, possible = pp.post_pass_merge(roster, validator=dv)
        self.assertEqual(n, 0)
        self.assertEqual(len(possible), 1)
        self.assertAlmostEqual(possible[0]["p_yes"], 0.60)
        self.assertIn("below threshold", possible[0]["reason"])


class TestIsbnHandling(unittest.TestCase):
    """--isbn override, Open Library auto-lookup, skip/allow-no-isbn behavior."""

    def setUp(self):
        # Save CONFIG keys we touch.
        self._saved = {k: pp.CONFIG.get(k) for k in
                       ("isbn_override", "allow_no_isbn", "rename_no_isbn")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                pp.CONFIG.pop(k, None)
            else:
                pp.CONFIG[k] = v

    def _ids(self, isbn=None):
        return {"isbn": isbn, "asin": None, "raw": []}

    # --isbn flag priority (highest)
    def test_isbn_flag_overrides_epub(self):
        pp.CONFIG["isbn_override"] = "9781615871100"
        ids = self._ids(isbn="9780000000000")
        isbn, src = pp._resolve_book_isbn(ids, "Freedom", "Daniel Suarez")
        self.assertEqual(isbn, "9781615871100")
        self.assertEqual(src, "flag")
        self.assertEqual(ids["isbn"], "9781615871100")

    def test_isbn_flag_used_when_epub_has_none(self):
        pp.CONFIG["isbn_override"] = "9781615871100"
        ids = self._ids(isbn=None)
        isbn, src = pp._resolve_book_isbn(ids, "Freedom", "Daniel Suarez")
        self.assertEqual(isbn, "9781615871100")
        self.assertEqual(src, "flag")

    # EPUB metadata priority (second)
    def test_isbn_epub_metadata(self):
        pp.CONFIG.pop("isbn_override", None)
        ids = self._ids(isbn="9781101007518")
        # Stub out network lookup so we know EPUB won without a network call.
        orig = pp._lookup_isbn_openlibrary
        def _fail(t, a):
            raise AssertionError("network should not be called")
        pp._lookup_isbn_openlibrary = _fail
        try:
            isbn, src = pp._resolve_book_isbn(ids, "Daemon", "Daniel Suarez")
        finally:
            pp._lookup_isbn_openlibrary = orig
        self.assertEqual(isbn, "9781101007518")
        self.assertEqual(src, "epub")

    # Open Library auto-lookup (third)
    def test_isbn_openlibrary_lookup(self):
        pp.CONFIG.pop("isbn_override", None)
        ids = self._ids(isbn=None)
        orig = pp._lookup_isbn_openlibrary
        pp._lookup_isbn_openlibrary = lambda t, a: "9781615871100"
        try:
            isbn, src = pp._resolve_book_isbn(ids, "Freedom", "Daniel Suarez")
        finally:
            pp._lookup_isbn_openlibrary = orig
        self.assertEqual(isbn, "9781615871100")
        self.assertEqual(src, "openlibrary")
        self.assertEqual(ids["isbn"], "9781615871100")

    def test_isbn_none_when_all_fail(self):
        pp.CONFIG.pop("isbn_override", None)
        orig = pp._lookup_isbn_openlibrary
        pp._lookup_isbn_openlibrary = lambda t, a: None
        try:
            isbn, src = pp._resolve_book_isbn(
                self._ids(isbn=None), "Unknown Book", "Nobody")
        finally:
            pp._lookup_isbn_openlibrary = orig
        self.assertIsNone(isbn)
        self.assertEqual(src, "none")

    # _lookup_isbn_openlibrary: exact title match
    def test_openlibrary_exact_match(self):
        import json as _json
        import urllib.request as _urlreq

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return _json.dumps({
                    "docs": [{"title": "Freedom",
                              "isbn": ["9781615871100"]}]}).encode()

        orig = _urlreq.urlopen
        _urlreq.urlopen = lambda req, timeout=10: _Resp()
        try:
            got = pp._lookup_isbn_openlibrary("Freedom", "Daniel Suarez")
        finally:
            _urlreq.urlopen = orig
        self.assertEqual(got, "9781615871100")

    def test_openlibrary_series_prefix_tolerated(self):
        import json as _json
        import urllib.request as _urlreq

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return _json.dumps({
                    "docs": [{"title": "Freedom",
                              "isbn": ["978-1-61587-110-0"]}]}).encode()

        orig = _urlreq.urlopen
        _urlreq.urlopen = lambda req, timeout=10: _Resp()
        try:
            got = pp._lookup_isbn_openlibrary("[Daemon 02] - Freedom",
                                              "Daniel Suarez")
        finally:
            _urlreq.urlopen = orig
        self.assertEqual(got, "9781615871100")

    def test_openlibrary_title_mismatch_rejected(self):
        import json as _json
        import urllib.request as _urlreq

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return _json.dumps({
                    "docs": [{"title": "Something Else Entirely",
                              "isbn": ["9780000000000"]}]}).encode()

        orig = _urlreq.urlopen
        _urlreq.urlopen = lambda req, timeout=10: _Resp()
        try:
            got = pp._lookup_isbn_openlibrary("Freedom", "Daniel Suarez")
        finally:
            _urlreq.urlopen = orig
        self.assertIsNone(got)

    def test_openlibrary_network_error_returns_none(self):
        import urllib.request as _urlreq
        orig = _urlreq.urlopen
        def _boom(req, timeout=10):
            raise ConnectionError("offline")
        _urlreq.urlopen = _boom
        try:
            got = pp._lookup_isbn_openlibrary("Freedom", "Daniel Suarez")
        finally:
            _urlreq.urlopen = orig
        self.assertIsNone(got)

    def test_openlibrary_no_title_returns_none(self):
        self.assertIsNone(pp._lookup_isbn_openlibrary("", "Daniel Suarez"))
        self.assertIsNone(pp._lookup_isbn_openlibrary(None, "Daniel Suarez"))

    # Skip vs allow-no-isbn
    def test_missing_isbn_skips_by_default(self):
        import io
        from contextlib import redirect_stdout
        pp.CONFIG.pop("allow_no_isbn", None)
        pp.CONFIG.pop("rename_no_isbn", None)
        fpath = Path("/tmp/noisbn-book.epub")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cont = pp._handle_missing_isbn(fpath)
        self.assertFalse(cont)
        self.assertIn("Skipping", buf.getvalue())
        self.assertIn("--isbn", buf.getvalue())

    def test_missing_isbn_allowed_with_flag(self):
        import io
        from contextlib import redirect_stdout
        pp.CONFIG["allow_no_isbn"] = True
        fpath = Path("/tmp/noisbn-book.epub")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cont = pp._handle_missing_isbn(fpath)
        self.assertTrue(cont)
        self.assertIn("WARNING", buf.getvalue())

    def test_rename_no_isbn(self):
        import io
        import tempfile, os
        from contextlib import redirect_stdout
        pp.CONFIG.pop("allow_no_isbn", None)
        pp.CONFIG["rename_no_isbn"] = True
        with tempfile.TemporaryDirectory() as td:
            fpath = Path(td) / "test-book.epub"
            fpath.write_bytes(b"fake")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cont = pp._handle_missing_isbn(fpath)
            self.assertFalse(cont)
            self.assertFalse(fpath.exists())
            self.assertTrue((Path(td) / "NO-ISBN-test-book.epub").exists())
            self.assertIn("Renamed", buf.getvalue())

    def test_rename_no_isbn_no_delete_on_collision(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        pp.CONFIG.pop("allow_no_isbn", None)
        pp.CONFIG["rename_no_isbn"] = True
        with tempfile.TemporaryDirectory() as td:
            fpath = Path(td) / "test-book.epub"
            fpath.write_bytes(b"fake")
            (Path(td) / "NO-ISBN-test-book.epub").write_bytes(b"existing")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cont = pp._handle_missing_isbn(fpath)
            self.assertFalse(cont)
            # Original still there; existing target untouched.
            self.assertTrue(fpath.exists())

    # _clean_isbn validation for --isbn
    def test_clean_isbn_valid(self):
        self.assertEqual(pp._clean_isbn("9781615871100"), "9781615871100")
        self.assertEqual(pp._clean_isbn("978-1-61587-110-0"), "9781615871100")
        # ISBN-10 -> ISBN-13 conversion (check digit recomputed).
        self.assertEqual(pp._clean_isbn("1615871101"), "9781615871100")

    def test_clean_isbn_invalid(self):
        self.assertIsNone(pp._clean_isbn("garbage"))
        self.assertIsNone(pp._clean_isbn(""))
        self.assertIsNone(pp._clean_isbn(None))
        self.assertIsNone(pp._clean_isbn("123"))


class TestCharacterImportance(unittest.TestCase):
    """Deterministic character roles from observed data (not LLM opinion)."""

    def test_pov_always_protagonist(self):
        # POV characters are protagonist regardless of frequency.
        self.assertEqual(
            pp._char_deterministic_role(1, 73, True), "protagonist")
        self.assertEqual(
            pp._char_deterministic_role(0, 73, True), "protagonist")

    def test_frequent_is_protagonist(self):
        # 40% of chapters (29/73) -> protagonist.
        self.assertEqual(
            pp._char_deterministic_role(29, 73, False), "protagonist")
        # Exactly 30% boundary.
        self.assertEqual(
            pp._char_deterministic_role(30, 100, False), "protagonist")

    def test_occasional_is_supporting(self):
        # 15% of chapters -> supporting.
        self.assertEqual(
            pp._char_deterministic_role(11, 73, False), "supporting")

    def test_rare_is_minor(self):
        # 5% of chapters (4/73) -> minor.
        self.assertEqual(
            pp._char_deterministic_role(4, 73, False), "minor")
        # Single appearance -> minor.
        self.assertEqual(
            pp._char_deterministic_role(1, 73, False), "minor")

    def test_antagonist_preserved(self):
        # LLM said antagonist + deterministic protagonist -> antagonist.
        self.assertEqual(
            pp._char_deterministic_role(30, 73, False, "antagonist"),
            "antagonist")
        # LLM said antagonist + deterministic minor -> minor (too minor).
        self.assertEqual(
            pp._char_deterministic_role(2, 73, False, "antagonist"),
            "minor")

    def test_freq_band_boundaries(self):
        self.assertEqual(pp._char_freq_band(0), "none")
        self.assertEqual(pp._char_freq_band(0.05), "rare")
        self.assertEqual(pp._char_freq_band(0.10), "occasional")
        self.assertEqual(pp._char_freq_band(0.29), "occasional")
        self.assertEqual(pp._char_freq_band(0.30), "frequent")
        self.assertEqual(pp._char_freq_band(1.0), "frequent")


class TestRelationshipConfidence(unittest.TestCase):
    """Relationship confidence: evidence x type specificity."""

    def test_strong_specific_is_high(self):
        # 2 quotes + spouse -> high.
        self.assertEqual(pp._rel_confidence(2, 1, "spouse"), "high")
        self.assertEqual(pp._rel_confidence(3, 5, "parent"), "high")

    def test_weak_vague_is_low(self):
        # 0 quotes, 1 co-occurrence + other -> low.
        self.assertEqual(pp._rel_confidence(0, 1, "other"), "low")
        self.assertEqual(pp._rel_confidence(0, 2, "colleague"), "low")

    def test_moderate_combinations(self):
        # 1 quote + friend (moderate type) -> medium.
        self.assertEqual(pp._rel_confidence(1, 1, "friend"), "medium")
        # 0 quotes + 3 co-occurrences + enemy -> medium.
        self.assertEqual(pp._rel_confidence(0, 3, "enemy"), "medium")
        # 2 quotes + colleague (vague) -> medium.
        self.assertEqual(pp._rel_confidence(2, 1, "colleague"), "medium")

    def test_weak_specific_is_medium(self):
        # 0 quotes + spouse -> medium (specific type saves it).
        self.assertEqual(pp._rel_confidence(0, 1, "spouse"), "medium")

    def test_evidence_band(self):
        self.assertEqual(pp._rel_evidence_band(2, 0), "strong")
        self.assertEqual(pp._rel_evidence_band(1, 0), "moderate")
        self.assertEqual(pp._rel_evidence_band(0, 3), "moderate")
        self.assertEqual(pp._rel_evidence_band(0, 2), "weak")

    def test_type_band(self):
        self.assertEqual(pp._rel_type_band("spouse"), "specific")
        self.assertEqual(pp._rel_type_band("parent"), "specific")
        self.assertEqual(pp._rel_type_band("other"), "vague")
        self.assertEqual(pp._rel_type_band("colleague"), "vague")
        self.assertEqual(pp._rel_type_band("friend"), "moderate")
        self.assertEqual(pp._rel_type_band("enemy"), "moderate")
        self.assertEqual(pp._rel_type_band("mentor"), "moderate")


class TestManualAlias(unittest.TestCase):
    """--alias flag: human-validated cross-book aliases."""

    def test_record_alias(self):
        ln = pp.LearnedState(enabled=False)
        # Disabled stub is no-op; use enabled with temp dir.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ln = pp.LearnedState(learn_dir=td, enabled=True)
            ln.record_alias("Loki", "Brian Gragg")
            # Normalized lookup works.
            self.assertEqual(ln.nick_lookup("loki"), "brian gragg")
            # Marked as human + manual.
            ent = ln.nicknames["loki"]
            self.assertTrue(ent.get("human"))
            self.assertTrue(ent.get("manual"))

    def test_alias_outranks_automatic(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ln = pp.LearnedState(learn_dir=td, enabled=True)
            # Automatic merge first.
            ln.record_merge("loki", "loki stormbringer", "isbn1")
            self.assertEqual(ln.nick_lookup("loki"), "loki stormbringer")
            # Manual alias overwrites.
            ln.record_alias("Loki", "Brian Gragg")
            self.assertEqual(ln.nick_lookup("loki"), "brian gragg")

    def test_alias_invalid_inputs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ln = pp.LearnedState(learn_dir=td, enabled=True)
            ln.record_alias("", "Brian Gragg")  # empty alias
            ln.record_alias("Loki", "")  # empty canonical
            ln.record_alias("Loki", "Loki")  # same name
            self.assertEqual(len(ln.nicknames), 0)


class TestRelationshipValidation(unittest.TestCase):
    """Jev validation for family-type relationships (spouse/parent/child/sibling)."""

    def _make_chapter_a(self, relationships):
        return {"characters": [], "relationships": relationships,
                "task_status": {"identity": "ok"}}

    def _rel(self, frm, to, rtype, evidence=""):
        return {"from": frm, "to": to, "type": rtype, "evidence": evidence}

    def setUp(self):
        # Save and reset module globals.
        self._old_validator = pp._DECISION_VALIDATOR
        self._old_config_principals = pp.CONFIG.get("principals_only", True)
        pp._dm_budget_reset()

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp.CONFIG["principals_only"] = self._old_config_principals
        pp._dm_budget_reset()

    def test_high_p_keeps_spouse(self):
        class FakeValidator:
            model = "fake"
            def validate_relationship(self, frm, to, rtype, quote, context=""):
                return True, 0.9
        pp._DECISION_VALIDATOR = FakeValidator()
        chs = [_ch(1, "A"), _ch(2, "B")]
        chapter_as = {1: self._make_chapter_a(
            [self._rel("Alice", "Bob", "spouse", "my wife Alice")])}
        red = pp.v2_reduce(chapter_as, {}, {}, chs)
        rels = [r for r in red["relationships"] if r["type"] == "spouse"]
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["validation_p"], 0.9)

    def test_low_p_downgrades_to_other(self):
        class FakeValidator:
            model = "fake"
            def validate_relationship(self, frm, to, rtype, quote, context=""):
                return False, 0.3
        pp._DECISION_VALIDATOR = FakeValidator()
        chs = [_ch(1, "A"), _ch(2, "B")]
        chapter_as = {1: self._make_chapter_a(
            [self._rel("Alice", "Bob", "spouse", "his mistress Alice")])}
        # Write mode (preview=False): downgrades.
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(len(red["relationships"]), 1)
        self.assertEqual(red["relationships"][0]["type"], "other")
        self.assertEqual(red["relationships"][0]["validation_p"], 0.3)
        self.assertEqual(red["relationships"][0]["validation_note"], "downgraded")

    def test_low_p_preview_keeps_type(self):
        class FakeValidator:
            model = "fake"
            def validate_relationship(self, frm, to, rtype, quote, context=""):
                return False, 0.3
        pp._DECISION_VALIDATOR = FakeValidator()
        chs = [_ch(1, "A"), _ch(2, "B")]
        chapter_as = {1: self._make_chapter_a(
            [self._rel("Alice", "Bob", "spouse", "his mistress Alice")])}
        # Preview mode: records score but does NOT downgrade.
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=True)
        self.assertEqual(len(red["relationships"]), 1)
        self.assertEqual(red["relationships"][0]["type"], "spouse")
        self.assertEqual(red["relationships"][0]["validation_p"], 0.3)

    def test_no_evidence_downgrades_without_call(self):
        calls = []
        class FakeValidator:
            model = "fake"
            def validate_relationship(self, frm, to, rtype, quote, context=""):
                calls.append((frm, to, rtype))
                return True, 0.95
        pp._DECISION_VALIDATOR = FakeValidator()
        chs = [_ch(1, "A"), _ch(2, "B")]
        chapter_as = {1: self._make_chapter_a(
            [self._rel("Alice", "Bob", "spouse", "")])}  # no evidence
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(len(calls), 0)  # no API call made
        self.assertEqual(red["relationships"][0]["type"], "other")
        self.assertEqual(red["relationships"][0]["validation_note"], "no_evidence")

    def test_non_family_not_validated(self):
        calls = []
        class FakeValidator:
            model = "fake"
            def validate_relationship(self, frm, to, rtype, quote, context=""):
                calls.append((frm, to, rtype))
                return False, 0.1
        pp._DECISION_VALIDATOR = FakeValidator()
        chs = [_ch(1, "A"), _ch(2, "B")]
        chapter_as = {1: self._make_chapter_a(
            [self._rel("Alice", "Bob", "friend", "good friends")])}
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(len(calls), 0)  # friend is not validated
        self.assertEqual(red["relationships"][0]["type"], "friend")
        self.assertNotIn("validation_p", red["relationships"][0])

    def test_validator_error_keeps_type(self):
        class FakeValidator:
            model = "fake"
            def validate_relationship(self, frm, to, rtype, quote, context=""):
                return None, None  # error
        pp._DECISION_VALIDATOR = FakeValidator()
        chs = [_ch(1, "A"), _ch(2, "B")]
        chapter_as = {1: self._make_chapter_a(
            [self._rel("Alice", "Bob", "spouse", "my wife")])}
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        # Fail open: keep original type on validator error.
        self.assertEqual(red["relationships"][0]["type"], "spouse")
        self.assertEqual(red["relationships"][0]["validation_note"],
                         "validator_error")

    def test_no_validator_leaves_as_is(self):
        pp._DECISION_VALIDATOR = None
        chs = [_ch(1, "A"), _ch(2, "B")]
        chapter_as = {1: self._make_chapter_a(
            [self._rel("Alice", "Bob", "spouse", "my wife")])}
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        self.assertEqual(red["relationships"][0]["type"], "spouse")
        self.assertNotIn("validation_p", red["relationships"][0])


class TestSeriesDetection(unittest.TestCase):
    """Series name/position detection from flags, EPUB, filename, Open Library."""

    def setUp(self):
        self._old_overrides = {
            k: pp.CONFIG.get(k) for k in
            ("series_override", "series_position_override")}

    def tearDown(self):
        for k, v in self._old_overrides.items():
            if v is None:
                pp.CONFIG.pop(k, None)
            else:
                pp.CONFIG[k] = v

    def test_filename_bracket_pattern(self):
        name, pos = pp._parse_series_from_filename("[Daemon 02] - Freedom.epub")
        self.assertEqual(name, "Daemon")
        self.assertEqual(pos, 2.0)

    def test_filename_paren_pattern(self):
        name, pos = pp._parse_series_from_filename("(Daemon #2) - Freedom.epub")
        self.assertEqual(name, "Daemon")
        self.assertEqual(pos, 2.0)

    def test_filename_decimal_position(self):
        name, pos = pp._parse_series_from_filename("[Series 1.5] - Novella.epub")
        self.assertEqual(name, "Series")
        self.assertEqual(pos, 1.5)

    def test_filename_no_match(self):
        name, pos = pp._parse_series_from_filename("Just A Book.epub")
        self.assertIsNone(name)
        self.assertIsNone(pos)

    def test_flag_overrides_all(self):
        pp.CONFIG["series_override"] = "MySeries"
        pp.CONFIG["series_position_override"] = 3.0
        identifiers = {"series": "EpubSeries", "series_index": 1.0}
        name, pos, src = pp._detect_series(
            "[EpubSeries 01] - Book.epub", identifiers, "Book", "Author")
        self.assertEqual(name, "MySeries")
        self.assertEqual(pos, 3.0)
        self.assertEqual(src, "flag")

    def test_epub_metadata_second_priority(self):
        identifiers = {"series": "EpubSeries", "series_index": 1.0}
        name, pos, src = pp._detect_series(
            "[Other 05] - Book.epub", identifiers, "Book", "Author")
        self.assertEqual(name, "EpubSeries")
        self.assertEqual(pos, 1.0)
        self.assertEqual(src, "epub")

    def test_filename_third_priority(self):
        identifiers = {}
        # No network: Open Library lookup would fail gracefully, but to keep
        # the test hermetic we only assert filename parsing via _detect_series
        # with a title that won't match (empty author short-circuits).
        name, pos, src = pp._detect_series(
            "[Daemon 02] - Freedom.epub", identifiers, "", "")
        # Empty title -> OL lookup returns (None, None) without network.
        self.assertEqual(name, "Daemon")
        self.assertEqual(pos, 2.0)
        self.assertEqual(src, "filename")

    def test_no_series_anywhere(self):
        identifiers = {}
        name, pos, src = pp._detect_series("Just A Book.epub", identifiers, "", "")
        self.assertIsNone(name)
        self.assertIsNone(pos)
        self.assertEqual(src, "none")

    def test_series_name_key(self):
        self.assertEqual(pp.LearnedState.series_name_key("Daemon"), "daemon")
        self.assertEqual(pp.LearnedState.series_name_key("  The Expanse  "),
                         "the_expanse")
        self.assertIsNone(pp.LearnedState.series_name_key(""))
        self.assertIsNone(pp.LearnedState.series_name_key(None))

    def test_series_roster_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ln = pp.LearnedState(learn_dir=td, enabled=True)
            key = ln.series_name_key("Daemon")
            chars = [{"name": "Brian Gragg", "aliases": ["Loki"]},
                     {"name": "Matthew Sobol", "aliases": []}]
            ln.save_series_name_roster(key, "Freedom", "Daniel Suarez",
                                       "9781615871100", 2.0, chars)
            # Path format: series/{normalized}/roster.json
            p = ln._named_series_path(key)
            self.assertTrue(str(p).endswith("series/daemon/roster.json"))
            self.assertTrue(p.exists())
            hints = ln.load_series_name_hints(key)
            self.assertEqual(hints.get("loki"), "brian gragg")

    def test_series_roster_dedupes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ln = pp.LearnedState(learn_dir=td, enabled=True)
            key = ln.series_name_key("Daemon")
            chars = [{"name": "Brian Gragg", "aliases": []}]
            ln.save_series_name_roster(key, "Daemon", "Daniel Suarez",
                                       "isbn1", 1.0, chars)
            ln.save_series_name_roster(key, "Freedom", "Daniel Suarez",
                                       "isbn2", 2.0, chars)  # same char
            hints = ln.load_series_name_hints(key)
            # No aliases, so no hints — but only one character entry.
            import json
            with open(ln._named_series_path(key), encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["characters"]), 1)
            self.assertEqual(len(data["books"]), 2)


class TestPrincipalFilter(unittest.TestCase):
    """Principal-character filter: POV, frequency, or well-connected."""

    def _char(self, name, frequency=0.0, is_pov=False):
        return {"name": name, "frequency": frequency, "is_pov": is_pov,
                "role": "supporting"}

    def _rel(self, frm, to):
        return {"from": frm, "to": to, "type": "friend"}

    def test_high_frequency_is_principal(self):
        # 25% chapters -> principal.
        chars = [self._char("Alice", frequency=0.25)]
        princs, minors, rels = pp._filter_principals(chars, [])
        self.assertEqual(len(princs), 1)
        self.assertEqual(len(minors), 0)

    def test_low_frequency_is_minor(self):
        # 5% chapters, not POV, 1 relationship -> minor.
        chars = [self._char("Alice", frequency=0.25),
                 self._char("Bob", frequency=0.05)]
        rels_in = [self._rel("Alice", "Bob")]
        princs, minors, rels = pp._filter_principals(chars, rels_in)
        self.assertEqual({c["name"] for c in princs}, {"Alice"})
        self.assertEqual({c["name"] for c in minors}, {"Bob"})

    def test_pov_override(self):
        # POV in 2% of chapters -> still principal.
        chars = [self._char("Alice", frequency=0.02, is_pov=True),
                 self._char("Bob", frequency=0.05)]
        princs, minors, rels = pp._filter_principals(chars, [])
        self.assertEqual({c["name"] for c in princs}, {"Alice"})
        self.assertEqual({c["name"] for c in minors}, {"Bob"})

    def test_well_connected_promoted(self):
        # Charlie at 5% but connected to 3 principals -> promoted.
        chars = [self._char("A", frequency=0.3),
                 self._char("B", frequency=0.3),
                 self._char("C", frequency=0.3),
                 self._char("Charlie", frequency=0.05)]
        rels_in = [self._rel("Charlie", "A"), self._rel("Charlie", "B"),
                   self._rel("Charlie", "C")]
        princs, minors, rels = pp._filter_principals(chars, rels_in)
        self.assertIn("Charlie", {c["name"] for c in princs})
        self.assertEqual(len(minors), 0)

    def test_poorly_connected_stays_minor(self):
        # Dave at 5% with only 2 principal connections -> minor.
        chars = [self._char("A", frequency=0.3),
                 self._char("B", frequency=0.3),
                 self._char("Dave", frequency=0.05)]
        rels_in = [self._rel("Dave", "A"), self._rel("Dave", "B")]
        princs, minors, rels = pp._filter_principals(chars, rels_in)
        self.assertEqual({c["name"] for c in princs}, {"A", "B"})
        self.assertEqual({c["name"] for c in minors}, {"Dave"})

    def test_relationship_both_endpoints_must_be_principal(self):
        chars = [self._char("Alice", frequency=0.3),
                 self._char("Bob", frequency=0.05)]
        rels_in = [self._rel("Alice", "Bob")]
        princs, minors, rels = pp._filter_principals(chars, rels_in)
        # Alice principal, Bob minor -> relationship dropped.
        self.assertEqual(len(rels), 0)

    def test_relationship_between_principals_kept(self):
        chars = [self._char("Alice", frequency=0.3),
                 self._char("Bob", frequency=0.25)]
        rels_in = [self._rel("Alice", "Bob")]
        princs, minors, rels = pp._filter_principals(chars, rels_in)
        self.assertEqual(len(rels), 1)

    def test_custom_min_frequency(self):
        chars = [self._char("Alice", frequency=0.15)]
        # Default 0.20 -> minor.
        _, minors, _ = pp._filter_principals(chars, [], min_frequency=0.20)
        self.assertEqual(len(minors), 1)
        # Custom 0.10 -> principal.
        princs, minors, _ = pp._filter_principals(chars, [], min_frequency=0.10)
        self.assertEqual(len(princs), 1)

    def test_empty_inputs(self):
        princs, minors, rels = pp._filter_principals([], [])
        self.assertEqual((princs, minors, rels), ([], [], []))
        princs, minors, rels = pp._filter_principals(None, None)
        self.assertEqual((princs, minors, rels), ([], [], []))


class TestMatrixViz(unittest.TestCase):
    def test_spice_band_detail_matches_level(self):
        # Detail returns the same level as _spice_level_from_chapters.
        levels = [0, 0, 1, 2, 4, 0, 5, 3]
        peak_band, freq_band, level = pp._spice_band_detail(levels, 8)
        self.assertEqual(level, pp._spice_level_from_chapters(levels, 8))
        self.assertEqual(peak_band, "explicit")
        self.assertIn(freq_band, ("rare", "occasional", "frequent", "pervasive"))

    def test_spice_band_detail_no_spice(self):
        peak_band, freq_band, level = pp._spice_band_detail([0, 0, 0], 3)
        self.assertEqual((peak_band, freq_band, level), ("none", "none", 0))

    def test_spice_band_detail_empty(self):
        peak_band, freq_band, level = pp._spice_band_detail([], 0)
        self.assertEqual((peak_band, freq_band, level), ("none", "none", 0))

    def test_spice_matrix_ascii_highlights_active_cell(self):
        out = pp._spice_matrix_ascii("explicit", "frequent", 4)
        self.assertIn(">>explicit<<", out)
        self.assertIn(">>4<<", out)
        self.assertIn("Book spice: 4", out)
        self.assertIn("peak=explicit", out)
        self.assertIn("freq=frequent", out)

    def test_spice_matrix_ascii_no_spice(self):
        out = pp._spice_matrix_ascii("none", "none", 0)
        self.assertNotIn(">>", out)
        self.assertIn("no spicy content", out)
        self.assertIn("Book spice: 0", out)

    def test_spice_matrix_ascii_all_cells_present(self):
        out = pp._spice_matrix_ascii("mild", "rare", 1)
        # All 12 matrix values should appear.
        for v in ("1", "2", "3", "4", "5"):
            self.assertIn(v, out)

    def test_char_importance_ascii_basic(self):
        princs = [
            {"name": "Alice", "frequency": 0.45, "role": "protagonist",
             "is_pov": True},
            {"name": "Bob", "frequency": 0.22, "role": "supporting",
             "is_pov": False},
        ]
        minors = [{"name": "Zed", "frequency": 0.05, "role": "minor"}]
        out = pp._char_importance_ascii(princs, minors)
        self.assertIn("2 principals, 1 minor", out)
        self.assertIn("Alice", out)
        self.assertIn("45%", out)
        self.assertIn("*", out)  # POV marker
        # Sorted by frequency desc: Alice before Bob.
        self.assertLess(out.index("Alice"), out.index("Bob"))

    def test_char_importance_ascii_empty(self):
        out = pp._char_importance_ascii([], [])
        self.assertIn("0 principals, 0 minor", out)

    def test_char_importance_ascii_none_inputs(self):
        out = pp._char_importance_ascii(None, None)
        self.assertIn("0 principals, 0 minor", out)

    def test_char_importance_ascii_truncates(self):
        princs = [{"name": f"Char{i:02d}", "frequency": 0.5 - i * 0.01,
                   "role": "protagonist"} for i in range(25)]
        out = pp._char_importance_ascii(princs, [], max_rows=20)
        self.assertIn("... and 5 more", out)
        self.assertNotIn("Char24", out)

    def test_rel_confidence_ascii_distribution(self):
        rels = ([{"confidence": "high"}] * 12
                + [{"confidence": "medium"}] * 8
                + [{"confidence": "low"}] * 3)
        out = pp._rel_confidence_ascii(rels)
        self.assertIn("high: 12", out)
        self.assertIn("medium: 8", out)
        self.assertIn("low: 3", out)

    def test_rel_confidence_ascii_empty(self):
        out = pp._rel_confidence_ascii([])
        self.assertIn("high: 0", out)
        self.assertIn("medium: 0", out)
        self.assertIn("low: 0", out)

    def test_rel_confidence_ascii_none(self):
        out = pp._rel_confidence_ascii(None)
        self.assertIn("high: 0", out)

    def test_v2_reduce_accepts_show_matrices(self):
        # show_matrices=True doesn't break reduce; prints are harmless.
        bs = {1: _b(spice=2)}
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            red = pp.v2_reduce({}, bs, {}, [_ch(1, "A")],
                               show_matrices=True)
        out = buf.getvalue()
        self.assertIn("Spice matrix", out)
        self.assertIn("Relationship confidence", out)
        self.assertIn("spice_level", red)
