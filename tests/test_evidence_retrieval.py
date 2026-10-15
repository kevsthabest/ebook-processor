"""Option G — targeted evidence retrieval for trope/trigger claims.

Design: eval/g-evidence-design.md (user-approved open choices OC-1..7
as amended; Security Advisor amendments G1-G5 folded into the design;
Trope Review seat amendments A: passages carry chapter + location
provenance, B: negative retrieval is an explicit recorded result).
Doctrine: retrieval finds passages; it does not manufacture support.
G never raises confidence, never drops a claim, never draws on the
decision-model budget, and attaches only literal spans of the book's
own text that a local-model judge ruled "supports".

Covered here, by build slice:
  1. Retrieval core (pure): sentence-anchored windows with offsets,
     lexical prefilter, embedding rank in the catalog's symmetric
     query shape, verified literal spans, per-run window cache (G5).
  2. Support judge: batched local-model verdicts
     (supports/adjacent/contradicts), grade computation, abstention
     accounting, zero decision-budget draw (the section 9.3 pin),
     judge failure => fail-open.
  3. Trope seam + row attach: result keys, quotes + retrieval stamp
     on shipped rows (G1), flag-off byte-identity, round-trip.
  4. Trigger retrieval + F interplay: regex-gated pre-seam fill,
     staging trope promotion rule 4 with the G2 next-observe lag.
  5. Reporting: preview summary line, accounting identity.
  6. Gate fixtures (in eval/): planted cross-book claims cannot
     bleed passages across books; zero-budget proof over the full
     seam; replayed-extraction invariance (G4).

Stdlib unittest only. No network, no Supabase, no live model: the
embedder and the judge call are deterministic fakes.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                              _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


# --- deterministic fakes -------------------------------------------------

_STEMS = ["betra", "sword", "kiss", "love", "drago", "storm", "sea",
          "magic", "sacri", "weddi", "gate"]


def _fake_vec(text):
    """Bag-of-stems vector: counts of tokens whose 5-char stem is in
    the vocab. Deterministic; windows dense in the query's stems rank
    high, unrelated windows score 0."""
    vec = [0.0] * len(_STEMS)
    for tok in (text or "").lower().split():
        stem = "".join(c for c in tok if c.isalpha())[:5]
        if stem in _STEMS:
            vec[_STEMS.index(stem)] += 1.0
    return vec


class FakeEmbedder:
    """embedder(texts) -> [vectors]; records every text embedded."""

    def __init__(self):
        self.texts = []

    def __call__(self, texts):
        self.texts.extend(texts)
        return [_fake_vec(t) for t in texts]


def _chapter(index, label, text):
    return {"index": index, "label": label, "text": text}


SEA_CH = _chapter(1, "Chapter One",
                  "The fleet rode out the storm beyond the harbour "
                  "wall. Waves hammered the sea gate until dawn. "
                  "The sailors sang to keep the fear down.")
SCENE_CH = _chapter(2, "Chapter Two",
                    "Mara trusted Tomas with the gate keys. In the "
                    "night Tomas opened the gate to the enemy. He "
                    "betrayed the watch, betrayed the guard, and "
                    "betrayed Mara's trust for gold. Betrayed by "
                    "their own gatekeeper, the garrison fell before "
                    "dawn. Mara found the empty treasury and swore "
                    "she would name the betrayal in the square.")
TALK_CH = _chapter(3, "Chapter Three",
                   "The council debated betrayal as a matter of law. "
                   "Old Petr argued that a betrayal undone by sunrise "
                   "was no true betrayal, and that no one in the "
                   "room had wronged anyone. They spoke of swords "
                   "and storms and the sea while they argued.")


def _claim(name, query_text, terms, support):
    return {"key": pp._tnorm(name), "name": name,
            "query_text": query_text, "terms": terms,
            "support": support}


class TestWindows(unittest.TestCase):
    def test_windows_are_literal_spans_with_offsets(self):
        text = SCENE_CH["text"]
        for w in pp._evidence_windows(text):
            self.assertEqual(
                text[w["offset"]:w["offset"] + len(w["text"])],
                w["text"])

    def test_windows_respect_char_cap(self):
        text = " ".join(f"Sentence number {i} rides the storm."
                        for i in range(40))
        wins = pp._evidence_windows(text)
        self.assertGreater(len(wins), 1)
        for w in wins:
            self.assertLessEqual(len(w["text"]),
                                 pp._EVIDENCE_WINDOW_CHARS)

    def test_single_overlong_sentence_is_hard_capped(self):
        text = "word " * 400 + "end."
        wins = pp._evidence_windows(text)
        self.assertTrue(wins)
        self.assertLessEqual(len(wins[0]["text"]),
                             pp._EVIDENCE_WINDOW_CHARS)
        self.assertEqual(text[:len(wins[0]["text"])], wins[0]["text"])

    def test_windows_start_at_sentence_boundaries(self):
        text = SCENE_CH["text"]
        for w in pp._evidence_windows(text):
            self.assertTrue(
                text[:w["offset"]].strip() == ""
                or text[:w["offset"]].rstrip().endswith((".", "!", "?")),
                f"window at {w['offset']} not sentence-anchored")


class TestQueryShapes(unittest.TestCase):
    def test_mapped_query_uses_catalog_shape(self):
        q = pp.evidence_query_text(
            "Betrayal", description="A trusted ally defects.")
        self.assertEqual(q, "Betrayal: A trusted ally defects.")

    def test_unmapped_query_uses_name_plus_summaries(self):
        q = pp.evidence_query_text(
            "Rescue Mission",
            summaries=["Mara storms the gate at dawn."])
        self.assertTrue(q.startswith("Rescue Mission"))
        self.assertIn("storms the gate", q)

    def test_terms_come_from_name_and_description(self):
        terms = pp.evidence_query_terms(
            "Enemies to Lovers", "Rivals fall in love.")
        self.assertIn("enemies", terms)
        self.assertIn("lovers", terms)
        self.assertIn("rivals", terms)
        self.assertIn("love", terms)
        self.assertNotIn("to", terms)


class TestRetrievalCore(unittest.TestCase):
    def _betrayal_claim(self, support):
        return _claim(
            "Betrayal",
            pp.evidence_query_text(
                "Betrayal",
                description="A trusted ally betrays the group."),
            pp.evidence_query_terms(
                "Betrayal", "A trusted ally betrays the group."),
            support)

    def test_known_support_ranks_first(self):
        emb = FakeEmbedder()
        out = pp.retrieve_claim_candidates(
            self._betrayal_claim([SEA_CH, SCENE_CH, TALK_CH]),
            emb, {}, floor=0.3)
        self.assertTrue(out["candidates"])
        top = out["candidates"][0]
        self.assertEqual(top["chapter"], "Chapter Two")
        self.assertIn("betrayed the watch", top["quote"])
        scores = [c["score"] for c in out["candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_discussion_window_survives_prefilter(self):
        # Negation/discussion windows are the JUDGE's problem, not the
        # filter's: the council chapter merely talks about betrayal,
        # and its windows must reach the judge (design section 3).
        out = pp.retrieve_claim_candidates(
            self._betrayal_claim([TALK_CH]), FakeEmbedder(), {},
            floor=0.3)
        self.assertTrue(
            any("council" in c["quote"] for c in out["candidates"]))

    def test_spans_verify_as_literal_book_text(self):
        out = pp.retrieve_claim_candidates(
            self._betrayal_claim([SEA_CH, SCENE_CH, TALK_CH]),
            FakeEmbedder(), {}, floor=0.3)
        by_label = {c["label"]: c for c in (SEA_CH, SCENE_CH, TALK_CH)}
        for cand in out["candidates"]:
            self.assertIn(cand["quote"], by_label[cand["chapter"]]["text"])
            self.assertTrue(pp.verify_evidence(
                cand["quote"], by_label[cand["chapter"]]["text"]))
            # Trope amendment A: provenance is chapter + location.
            self.assertIn("chapter_index", cand)
            self.assertIn("offset", cand)
        self.assertEqual(out["verify_failures"], 0)

    def test_floor_excludes_unrelated_chapters(self):
        claim = _claim(
            "Dragons",
            pp.evidence_query_text("Dragons", "A dragon appears."),
            pp.evidence_query_terms("Dragons", "A dragon appears."),
            [SEA_CH])
        out = pp.retrieve_claim_candidates(claim, FakeEmbedder(), {},
                                           floor=0.3)
        self.assertEqual(out["candidates"], [])

    def test_window_cache_shared_across_claims(self):
        # Amendment G5: window embeddings are per-run, in-memory, and
        # shared across claims whose support sets overlap — a window
        # is embedded once per run, not once per claim.
        emb = FakeEmbedder()
        cache = {}
        c1 = self._betrayal_claim([SCENE_CH, TALK_CH])
        pp.retrieve_claim_candidates(c1, emb, cache, floor=0.3)
        n_after_first = len(emb.texts)
        c2 = _claim(
            "Gate", pp.evidence_query_text("Gate", "The gate opens."),
            pp.evidence_query_terms("Gate", "The gate opens."),
            [SCENE_CH, TALK_CH])
        pp.retrieve_claim_candidates(c2, emb, cache, floor=0.3)
        second = emb.texts[n_after_first:]
        # Only the second claim's query text is newly embedded.
        self.assertEqual(second, [c2["query_text"]])

    def test_judge_candidate_selection_prefers_distinct_chapters(self):
        cands = [
            {"quote": "a", "chapter": "Ch 1", "chapter_index": 1,
             "offset": 0, "score": 0.9},
            {"quote": "b", "chapter": "Ch 1", "chapter_index": 1,
             "offset": 50, "score": 0.85},
            {"quote": "c", "chapter": "Ch 2", "chapter_index": 2,
             "offset": 0, "score": 0.8},
            {"quote": "d", "chapter": "Ch 3", "chapter_index": 3,
             "offset": 0, "score": 0.7},
        ]
        picked = pp._select_judge_candidates(cands, limit=3)
        self.assertEqual([p["quote"] for p in picked], ["a", "c", "d"])


class TestSupportResolution(unittest.TestCase):
    def test_refs_resolve_by_label_and_by_index(self):
        chapters = [SEA_CH, SCENE_CH, TALK_CH]
        by_label = pp._resolve_support_chapters(["Chapter Two"],
                                                chapters)
        by_index = pp._resolve_support_chapters([2], chapters)
        self.assertEqual(by_label, [SCENE_CH])
        self.assertEqual(by_index, [SCENE_CH])

    def test_unknown_refs_are_skipped(self):
        self.assertEqual(
            pp._resolve_support_chapters(["Nowhere", 99],
                                         [SEA_CH]), [])


# --- slice 2: support judge ----------------------------------------------

import re as _re


class FakeJudgeCall:
    """call(prompt, chunk, i, n) -> JSON string, the _run_task shape.
    Rules on passage CONTENT markers, as a depiction judge should:
    the betrayal scene supports; the council discussion is adjacent;
    anything else contradicts. Records call count."""

    def __init__(self, omit=()):
        self.calls = 0
        self.omit = set(omit)  # (claim_idx, passage_idx) to skip

    def __call__(self, prompt, chunk, i, n):
        self.calls += 1
        verdicts = []
        for m in _re.finditer(
                r"PASSAGE (\d+)\.(\d+) \(from [^)]*\): (.*?)(?=\nPASSAGE "
                r"\d+\.\d+ |\nCLAIM \d+: |\Z)", prompt, _re.S):
            ci, pi, text = int(m.group(1)), int(m.group(2)), m.group(3)
            if (ci, pi) in self.omit:
                continue
            if "betrayed the watch" in text:
                v = "supports"
            elif "council debated" in text:
                v = "adjacent"
            else:
                v = "contradicts"
            verdicts.append({"claim": ci, "passage": pi, "verdict": v})
        return json.dumps({"verdicts": verdicts})


def _job(key, name, passages, definition=""):
    return {"key": key, "name": name, "definition": definition,
            "passages": passages}


def _passage(quote, chapter, idx, score=0.9):
    return {"quote": quote, "chapter": chapter, "chapter_index": idx,
            "offset": 0, "score": score}


SCENE_P = _passage("He betrayed the watch and the guard.", "Ch 2", 2)
SCENE_P2 = _passage("Betrayed, the garrison fell.", "Ch 5", 5)
TALK_P = _passage("The council debated betrayal as law.", "Ch 3", 3)
SEA_P = _passage("The fleet rode out the storm.", "Ch 1", 1)


class TestJudge(unittest.TestCase):
    def test_verdict_classes_flow_through(self):
        jobs = [_job("betrayal", "Betrayal", [SCENE_P, TALK_P, SEA_P],
                     "A trusted ally betrays the group.")]
        verdicts, failed = pp.judge_evidence_batch(FakeJudgeCall(),
                                                   jobs)
        self.assertEqual(failed, set())
        self.assertEqual(verdicts["betrayal"],
                         {0: "supports", 1: "adjacent",
                          2: "contradicts"})

    def test_batching_caps_claims_per_call(self):
        call = FakeJudgeCall()
        jobs = [_job(f"c{i}", f"C{i}", [SEA_P]) for i in range(11)]
        pp.judge_evidence_batch(call, jobs)
        self.assertEqual(call.calls, 2)

    def test_judge_failure_is_fail_open(self):
        def dead_call(prompt, chunk, i, n):
            return None
        verdicts, failed = pp.judge_evidence_batch(
            dead_call, [_job("x", "X", [SCENE_P])])
        self.assertEqual(verdicts, {})
        self.assertEqual(failed, {"x"})

    def test_unjudged_claim_in_a_batch_is_failed_not_abstained(self):
        # The model returns verdicts for claim 0 only: claim 1 was
        # never judged, so it must surface as failed (infrastructure
        # honesty), not as a judged abstention.
        def partial_call(prompt, chunk, i, n):
            return json.dumps({"verdicts": [
                {"claim": 0, "passage": 0, "verdict": "supports"}]})
        jobs = [_job("a", "A", [SCENE_P]), _job("b", "B", [SCENE_P])]
        verdicts, failed = pp.judge_evidence_batch(partial_call, jobs)
        self.assertEqual(verdicts.get("a"), {0: "supports"})
        self.assertEqual(failed, {"b"})

    def test_zero_decision_budget_on_the_g_path(self):
        # Design section 9.3 pin: _dm_budget_claim is never called on
        # G's path — trope evidence must not starve family validation.
        def explode(n=1):
            raise AssertionError("_dm_budget_claim called on G path")
        orig = pp._dm_budget_claim
        pp._dm_budget_claim = explode
        try:
            claim = _claim(
                "Betrayal",
                pp.evidence_query_text("Betrayal", "Ally betrays."),
                pp.evidence_query_terms("Betrayal", "Ally betrays."),
                [SCENE_CH, TALK_CH])
            out = pp.retrieve_claim_candidates(
                claim, FakeEmbedder(), {}, floor=0.3)
            cands = pp._select_judge_candidates(out["candidates"])
            jobs = [_job("betrayal", "Betrayal", cands)]
            verdicts, failed = pp.judge_evidence_batch(
                FakeJudgeCall(), jobs)
        finally:
            pp._dm_budget_claim = orig
        self.assertEqual(failed, set())
        self.assertIn("supports", verdicts["betrayal"].values())


class TestClassify(unittest.TestCase):
    def test_strong_grade_needs_two_distinct_chapters(self):
        out = pp.classify_evidence(
            [SCENE_P, SCENE_P2], {0: "supports", 1: "supports"})
        self.assertEqual(out["support"], "strong")
        self.assertEqual(out["outcome"], "supported-strong")
        self.assertEqual(out["chapters_with_support"], 2)
        self.assertEqual(len(out["attached"]), 2)

    def test_single_chapter_support_is_single_grade(self):
        out = pp.classify_evidence(
            [SCENE_P, TALK_P], {0: "supports", 1: "adjacent"})
        self.assertEqual(out["support"], "single")
        self.assertEqual(out["outcome"], "supported-single")
        self.assertEqual([p["quote"] for p in out["attached"]],
                         [SCENE_P["quote"]])

    def test_adjacent_never_attaches(self):
        out = pp.classify_evidence([TALK_P], {0: "adjacent"})
        self.assertEqual(out["outcome"], "adjacent-only")
        self.assertEqual(out["support"], "none")
        self.assertEqual(out["attached"], [])
        self.assertEqual(out["adjacent"], 1)

    def test_no_support_is_abstention(self):
        out = pp.classify_evidence([SEA_P], {0: "contradicts"})
        self.assertEqual(out["outcome"], "abstained")
        self.assertEqual(out["support"], "none")
        self.assertEqual(out["attached"], [])

    def test_missing_verdict_is_not_support(self):
        # A passage the judge never ruled on must not attach
        # (fail-closed for attachment).
        out = pp.classify_evidence([SCENE_P, SCENE_P2],
                                   {0: "supports"})
        self.assertEqual([p["quote"] for p in out["attached"]],
                         [SCENE_P["quote"]])
        self.assertEqual(out["support"], "single")

    def test_attach_cap_and_distinct_chapter_preference(self):
        ps = [_passage(f"q{i}", f"Ch {1 + (i % 2)}", 1 + (i % 2),
                       score=0.9 - i * 0.01) for i in range(5)]
        for i, p in enumerate(ps):
            p["offset"] = i * 100  # real passages have distinct offsets
        out = pp.classify_evidence(ps, {i: "supports" for i in range(5)})
        self.assertEqual(len(out["attached"]), 3)
        self.assertEqual(out["support"], "strong")


if __name__ == "__main__":
    unittest.main()
