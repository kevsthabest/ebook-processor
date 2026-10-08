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
        self.assertEqual(t["severity_claimed"], "on_page")

    def test_trigger_single_chapter_dropped(self):
        # Single-chapter trigger is noise — dropped entirely
        bs = {1: _b(self._full_triggers(murder="graphic"), spice=1)}
        bs[1]["trigger_evidence"] = {"murder": "quote here"}
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
