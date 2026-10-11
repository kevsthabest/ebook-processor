"""Evidence/confidence validation tests (Trope Review Agent findings).

- ToC chapters are detected deterministically and excluded from
  trope/trigger evidence collection (a chapter titled "Mind Mapping"
  must not become the Mind Game trope).
- trope_chapters is built even when catalog embeddings are unavailable,
  so trope claims never carry chapter-less evidence.
- Claims with empty evidence carry ~0 confidence (preview reduce and
  the write_claims DB step).

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _ch(idx, label, text="x " * 200, is_toc=False):
    d = {"index": idx, "label": label, "text": text}
    if is_toc:
        d["is_toc"] = True
    return d


def _b(triggers=None, spice=0, tropes=None, summary="sum", quotes=None, tev=None):
    full = {c: "none" for c in pp.TRIGGER_CATEGORIES}
    full.update(triggers or {})
    return {"summary": summary, "spice_level": spice,
            "triggers": full, "trigger_evidence": tev or {},
            "trope_candidates": tropes or [], "quotes": quotes or []}


_TOC_TEXT = "\n".join(
    [f"Chapter {i}: The Adventure Continues Part {i}" for i in range(1, 41)])
_PROSE_TEXT = (" ".join(
    ["She walked into the room and saw the letter on the table."] * 60))
_DIALOGUE_TEXT = "\n".join(
    ['"I cannot believe you did that," she said.',
     '"It had to be done," he replied, turning away.',
     "The rain kept falling against the darkened window."] * 12)


class _FakeSB:
    """Capture Supabase writes; GETs return empty (nothing pre-existing)."""
    def __init__(self):
        self.posts = []

    def __call__(self, table, params=None, method="GET", data=None):
        if method == "POST":
            self.posts.append((table, data))
            return [{"id": 1}]
        return []


def _result(**over):
    r = {"title": "T", "tropes": [], "trope_confidence": {},
         "trope_catalog_map": {}, "trope_chapters": {}, "triggers": [],
         "characters": [], "relationships": [], "quotes": []}
    r.update(over)
    return r


class TestTocDetection(unittest.TestCase):
    def test_toc_title_list_detected(self):
        self.assertTrue(pp._is_toc_chapter(_TOC_TEXT))

    def test_prose_not_toc(self):
        self.assertFalse(pp._is_toc_chapter(_PROSE_TEXT))

    def test_dialogue_heavy_not_toc(self):
        # Short lines, but punctuated -> prose, not a ToC.
        self.assertFalse(pp._is_toc_chapter(_DIALOGUE_TEXT))

    def test_short_text_not_toc(self):
        self.assertFalse(pp._is_toc_chapter("Chapter 1\nChapter 2"))

    def test_split_chapters_tags_toc(self):
        units = [{"spine": "c0", "label": "Contents", "text": _TOC_TEXT}]
        for i in range(1, 6):
            units.append({"spine": f"c{i}", "label": f"Chapter {i}",
                          "text": f"Chapter {i}\n\n" + _PROSE_TEXT})
        chapters = pp.split_chapters(units)
        by_label = {c["label"]: c for c in chapters}
        self.assertTrue(by_label["Contents"]["is_toc"])
        self.assertFalse(by_label["Chapter 1"]["is_toc"])

    def test_chapter_json_roundtrip(self):
        ch = _ch(1, "Contents", text=_TOC_TEXT, is_toc=True)
        self.assertEqual(json.loads(json.dumps(ch))["is_toc"], True)


class TestTocExcludedFromEvidence(unittest.TestCase):
    def test_toc_trope_candidates_dropped(self):
        # The reported bug: a ToC chapter titled "Mind Mapping" fed the
        # Mind Game trope candidate. ToC chapters must not contribute.
        chapters = [_ch(1, "Contents", is_toc=True), _ch(2, "Ch 2")]
        bs = {1: _b(tropes=["Mind Game"]),
              2: _b(tropes=["Enemies to Lovers"])}
        red = pp.v2_reduce({}, bs, {}, chapters)
        self.assertNotIn("Mind Game", red["trope_candidates"])
        self.assertEqual(red["trope_candidates"], ["Enemies to Lovers"])
        for labels in red["trope_chapters"].values():
            self.assertNotIn("Contents", labels)

    def test_toc_excluded_from_trigger_accumulation(self):
        chapters = [_ch(1, "A"), _ch(2, "B"), _ch(3, "Contents", is_toc=True)]
        bs = {1: _b({"murder": "mentioned"}),
              2: _b({"murder": "mentioned"}),
              3: _b({"murder": "mentioned"})}
        red = pp.v2_reduce({}, bs, {}, chapters)
        t = red["triggers"][0]
        self.assertEqual(t["warning"], "murder")
        self.assertEqual(t["chapters"], ["A", "B"])
        self.assertEqual(t["chapter_indices"], [1, 2])


class TestTropeChapterEvidence(unittest.TestCase):
    def test_trope_chapters_built_without_embeddings(self):
        # Without embed_url the old code left trope_chapters empty while
        # gate confidence stayed real. The fallback must always be built.
        self.assertFalse(pp.CONFIG.get("embed_url"))
        chapters = [_ch(1, "A"), _ch(2, "B")]
        bs = {1: _b(tropes=["Secret Relationship"]),
              2: _b(tropes=["Secret Relationship"])}
        red = pp.v2_reduce({}, bs, {}, chapters)
        key = pp._tnorm("Secret Relationship")
        self.assertEqual(red["trope_chapters"][key], ["A", "B"])

    def test_duplicate_candidate_counted_once_per_chapter(self):
        chapters = [_ch(1, "A")]
        bs = {1: _b(tropes=["Secret Relationship", "Secret Relationship"])}
        red = pp.v2_reduce({}, bs, {}, chapters)
        self.assertEqual(red["trope_candidate_counts"][pp._tnorm("Secret Relationship")], 1)


class TestEmptyEvidenceConfidence(unittest.TestCase):
    def _triggers(self, **over):
        t = {c: "none" for c in pp.TRIGGER_CATEGORIES}
        t.update(over)
        return t

    def test_evidenceless_trigger_confidence_zero(self):
        # Two chapters so the claim survives the noise filter; no quotes.
        bs = {1: _b(self._triggers(murder="mentioned")),
              2: _b(self._triggers(murder="mentioned"))}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        t = red["triggers"][0]
        self.assertEqual(t["evidence"], [])
        self.assertEqual(t["confidence"], 0.0)

    def test_evidenced_trigger_keeps_confidence(self):
        bs = {1: _b(self._triggers(murder="mentioned"),
                    tev={"murder": "a dark quote about murder here"}),
              2: _b(self._triggers(murder="mentioned"))}
        red = pp.v2_reduce({}, bs, {}, [_ch(1, "A"), _ch(2, "B")])
        t = red["triggers"][0]
        self.assertTrue(t["evidence"])
        self.assertGreater(t["confidence"], 0.0)

    def test_write_claims_trope_no_chapters_clamped(self):
        fake = _FakeSB()
        old_sb = pp.sb
        pp.sb = fake
        try:
            res = _result(
                tropes=["Mind Game"],
                trope_confidence={pp._tnorm("Mind Game"): 0.75},
                trope_chapters={},  # no supporting chapters recorded
            )
            pp.write_claims(1, res, trope_mappings={"Mind Game": "cat-1"})
        finally:
            pp.sb = old_sb
        rows = [d for tbl, d in fake.posts if tbl == "book_trope_claims"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0]["confidence"], 0.0)

    def test_write_claims_trope_with_chapters_keeps_confidence(self):
        fake = _FakeSB()
        old_sb = pp.sb
        pp.sb = fake
        try:
            res = _result(
                tropes=["Mind Game"],
                trope_confidence={pp._tnorm("Mind Game"): 0.75},
                trope_chapters={pp._tnorm("Mind Game"): ["Ch 3"]},
            )
            pp.write_claims(1, res, trope_mappings={"Mind Game": "cat-1"})
        finally:
            pp.sb = old_sb
        rows = [d for tbl, d in fake.posts if tbl == "book_trope_claims"]
        self.assertEqual(rows[0][0]["confidence"], 0.75)

    def test_write_claims_trigger_no_evidence_clamped(self):
        fake = _FakeSB()
        old_sb = pp.sb
        pp.sb = fake
        try:
            res = _result(triggers=[{
                "warning": "murder", "severity": "mentioned",
                "severity_claimed": "mentioned", "chapters": ["A", "B"],
                "chapter_indices": [1, 2], "chapter_count": 2,
                "frequency": 1.0, "prominence": "low",
                "evidence": [], "evidence_verified": False,
                "evidence_offered": False, "confidence": 0.65,
            }])
            pp.write_claims(1, res)
        finally:
            pp.sb = old_sb
        rows = [d for tbl, d in fake.posts if tbl == "book_trigger_claims"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0]["confidence"], 0.0)
        self.assertEqual(rows[0][0]["evidence"]["quotes"], [])

    def test_write_claims_trigger_with_evidence_keeps_confidence(self):
        fake = _FakeSB()
        old_sb = pp.sb
        pp.sb = fake
        try:
            res = _result(triggers=[{
                "warning": "murder", "severity": "on_page",
                "severity_claimed": "on_page", "chapters": ["A"],
                "chapter_indices": [1], "chapter_count": 1,
                "frequency": 0.5, "prominence": "medium",
                "evidence": [{"quote": "q", "chapter": "A"}],
                "evidence_verified": True, "evidence_offered": True,
                "confidence": 0.8,
            }])
            pp.write_claims(1, res)
        finally:
            pp.sb = old_sb
        rows = [d for tbl, d in fake.posts if tbl == "book_trigger_claims"]
        self.assertEqual(rows[0][0]["confidence"], 0.8)


class TestGatePrompt(unittest.TestCase):
    def test_gate_prompt_forbids_title_inference(self):
        # The LLM-side guard: titles alone are not trope evidence.
        self.assertIn("never from a chapter title", pp.PROMPT_V2_TROPE_GATE)


if __name__ == "__main__":
    unittest.main()
