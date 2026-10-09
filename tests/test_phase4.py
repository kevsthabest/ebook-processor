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
