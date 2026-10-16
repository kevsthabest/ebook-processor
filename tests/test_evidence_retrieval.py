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

    def test_prefilter_matches_morphological_variants(self):
        # The depicting window's only hit is "betrayed"; the query
        # term is "betrayal". Stage 1 is recall-oriented by design —
        # a variant miss here is unrecoverable downstream.
        ch = _chapter(5, "Chapter Five",
                      "He had betrayed the watch for gold, and the "
                      "garrison paid for it that night.")
        claim = _claim("Betrayal", "Betrayal", {"betrayal"}, [ch])
        out = pp.retrieve_claim_candidates(claim, FakeEmbedder(), {},
                                           floor=0.3)
        self.assertTrue(out["candidates"])

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


# --- slice 3: trope seam + row attach -------------------------------------

SCENE2_CH = _chapter(5, "Chapter Five",
                     "Years later Petr admitted the truth in the "
                     "square. He had betrayed the watch for the same "
                     "gold, and the garrison had paid for it.")


def _orchestrator(claims, chapters, call=None, embedder=None,
                  catalog=None, summaries=None):
    return pp.evidence_retrieve_result(
        {"claims": claims, "catalog": catalog or {},
         "summaries": summaries or {}},
        chapters, call=call or FakeJudgeCall(),
        embedder=embedder or FakeEmbedder())


class TestTropeSeam(unittest.TestCase):
    CHAPTERS = [SEA_CH, SCENE_CH, TALK_CH, SCENE2_CH]

    def test_supported_claim_attaches_only_depicting_passages(self):
        out = _orchestrator(
            [{"name": "Betrayal",
              "support_refs": ["Chapter Two", "Chapter Three",
                               "Chapter Five"],
              "catalog_id": "t1", "staged": False}],
            self.CHAPTERS,
            catalog={"t1": {"name": "Betrayal",
                            "description": "A trusted ally betrays "
                                           "the group."}})
        ev = out["trope_evidence"]["betrayal"]
        self.assertTrue(ev)
        # The council discussion was judged adjacent: never attached.
        self.assertTrue(all("council" not in p["quote"] for p in ev))
        self.assertTrue(all("betrayed the watch" in p["quote"]
                            for p in ev))
        rec = out["trope_retrieval"]["betrayal"]
        # Support in two distinct chapters (Two + Five) = strong.
        self.assertEqual(rec["support"], "strong")
        self.assertEqual(rec["outcome"], "supported-strong")
        self.assertEqual(rec["chapters_with_support"], 2)
        self.assertEqual(rec["adjacent"], 1)
        self.assertEqual(rec["judge"]["supports"], 2)

    def test_abstention_is_an_explicit_recorded_result(self):
        # Trope amendment B: "searched the claimed chapters, found
        # nothing" is recorded as such — claim name + chapters
        # searched — not left indistinguishable from "never ran".
        out = _orchestrator(
            [{"name": "Dragons", "support_refs": ["Chapter One"],
              "catalog_id": None, "staged": False}],
            self.CHAPTERS)
        rec = out["trope_retrieval"]["dragons"]
        self.assertEqual(rec["outcome"], "abstained")
        self.assertEqual(rec["support"], "none")
        self.assertEqual(rec["chapters_searched"], ["Chapter One"])
        self.assertNotIn("dragons", out["trope_evidence"])
        self.assertEqual(out["marker"]["claims"]["dragons"],
                         "abstained")

    def test_failed_retrieval_is_fail_open_and_unstamped(self):
        def dead_embedder(texts):
            raise RuntimeError("endpoint down")
        out = _orchestrator(
            [{"name": "Betrayal", "support_refs": ["Chapter Two"],
              "catalog_id": None, "staged": False},
             {"name": "Dragons", "support_refs": ["Chapter One"],
              "catalog_id": None, "staged": False}],
            self.CHAPTERS, embedder=dead_embedder)
        self.assertEqual(out["trope_evidence"], {})
        for rec in out["trope_retrieval"].values():
            self.assertEqual(rec["outcome"], "failed")
            self.assertIsNone(rec["support"])
        self.assertTrue(out["marker"]["failed"])
        self.assertEqual(out["marker"]["totals"]["failed"], 2)

    def test_result_keys_round_trip_through_json(self):
        out = _orchestrator(
            [{"name": "Betrayal",
              "support_refs": ["Chapter Two", "Chapter Five"],
              "catalog_id": None, "staged": True}],
            self.CHAPTERS)
        blob = json.dumps({"trope_evidence": out["trope_evidence"],
                           "trope_retrieval": out["trope_retrieval"],
                           "evidence_retrieval": out["marker"]})
        back = json.loads(blob)
        self.assertEqual(back["trope_evidence"],
                         out["trope_evidence"])
        self.assertEqual(back["trope_retrieval"],
                         out["trope_retrieval"])
        self.assertEqual(back["evidence_retrieval"], out["marker"])

    def test_marker_reports_floor_and_totals(self):
        out = _orchestrator(
            [{"name": "Betrayal", "support_refs": ["Chapter Two"],
              "catalog_id": None, "staged": False}],
            self.CHAPTERS)
        self.assertEqual(out["marker"]["method"], "targeted-v1")
        self.assertIn("floor", out["marker"])
        self.assertEqual(out["marker"]["totals"]["attempted"], 1)


def _trope_result(**extra):
    result = {"tropes": ["Betrayal", "Dragons"],
              "trope_confidence": {"betrayal": 0.85, "dragons": 0.9},
              "trope_chapters": {"betrayal": ["Chapter Two"],
                                 "dragons": ["Chapter One"]}}
    result.update(extra)
    return result


_CAT_MAP = {"betrayal": "t-bet", "dragons": "t-drg"}


class TestRowAttach(unittest.TestCase):
    def _rows(self, result):
        rows, unmatched, failed = pp._build_trope_rows(
            "w1", result, _CAT_MAP, None, "m", set())
        self.assertEqual(failed, [])
        return {r["trope_id"]: r for r in rows}

    def test_supported_claim_ships_quotes_and_stamp(self):
        result = _trope_result(
            trope_evidence={"betrayal": [
                {"quote": "He betrayed the watch.",
                 "chapter": "Chapter Two", "chapter_index": 2,
                 "offset": 40, "score": 0.9}]},
            trope_retrieval={"betrayal": {
                "support": "strong", "chapters_with_support": 2}})
        row = self._rows(result)["t-bet"]
        self.assertEqual(row["evidence"]["quotes"],
                         [{"quote": "He betrayed the watch.",
                           "chapter": "Chapter Two"}])
        self.assertEqual(row["evidence"]["retrieval"],
                         {"method": "targeted-v1", "support": "strong",
                          "passages": 1, "chapters_with_support": 2})
        # Confidence is the pipeline's own — never raised (§6).
        self.assertEqual(row["confidence"], 0.85)

    def test_abstained_row_differs_only_by_the_g1_stamp(self):
        base = self._rows(_trope_result())["t-drg"]
        result = _trope_result(trope_retrieval={"dragons": {
            "support": "none", "chapters_with_support": 0}})
        stamped = self._rows(result)["t-drg"]
        self.assertEqual(stamped["evidence"]["retrieval"],
                         {"method": "targeted-v1", "support": "none",
                          "passages": 0, "chapters_with_support": 0})
        self.assertNotIn("quotes", stamped["evidence"])
        rest = {k: v for k, v in stamped.items() if k != "evidence"}
        base_rest = {k: v for k, v in base.items() if k != "evidence"}
        self.assertEqual(rest, base_rest)
        ev = dict(stamped["evidence"])
        del ev["retrieval"]
        self.assertEqual(ev, base["evidence"])

    def test_failed_claim_gets_no_stamp(self):
        result = _trope_result(trope_retrieval={"dragons": {
            "support": None, "chapters_with_support": 0}})
        row = self._rows(result)["t-drg"]
        self.assertNotIn("retrieval", row["evidence"])
        self.assertNotIn("quotes", row["evidence"])

    def test_flag_off_rows_are_byte_identical(self):
        # No G keys in the result at all: the row builder must
        # produce exactly today's rows.
        rows = self._rows(_trope_result())
        for row in rows.values():
            self.assertEqual(
                set(row["evidence"]),
                {"source", "chapters", "chapter_count"})


# --- slice 4: trigger retrieval + F interplay ------------------------------

import tempfile


class MarkerJudgeCall:
    """Judge stub ruling by content markers: first matching
    (marker, verdict) rule wins; default contradicts."""

    def __init__(self, rules, default="contradicts"):
        self.rules = rules
        self.default = default
        self.calls = 0

    def __call__(self, prompt, chunk, i, n):
        self.calls += 1
        verdicts = []
        for m in _re.finditer(
                r"PASSAGE (\d+)\.(\d+) \(from [^)]*\): (.*?)(?=\nPASSAGE "
                r"\d+\.\d+ |\nCLAIM \d+: |\Z)", prompt, _re.S):
            text = m.group(3)
            v = self.default
            for marker, verdict in self.rules:
                if marker in text:
                    v = verdict
                    break
            verdicts.append({"claim": int(m.group(1)),
                             "passage": int(m.group(2)), "verdict": v})
        return json.dumps({"verdicts": verdicts})


class TestStagingEvidenceRule4(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = pp.StagingState(learn_dir=self.tmp)
        self.key = "title:test book|some author"

    def _observe(self, name="Betrayal", n_ch=2, independent=True):
        return self.state.observe_work(
            work_key=self.key, title="Test Book",
            author="Some Author",
            tropes=[{"name": name, "n_ch": n_ch,
                     "catalog_id": None}],
            triggers=[], auto_min=3, independent=independent)

    def _entry(self, name="Betrayal"):
        w = self.state.data["works"][self.key]
        return w["claims"][pp.staging_claim_key("trope", name=name)]

    def test_g2_lag_record_then_promote_at_next_observe(self):
        # Run N: the claim stages; retrieval records strong
        # evidence AFTER the observe. It must NOT promote in run N.
        out = self._observe()
        self.assertEqual(out["trope_actions"][0]["action"], "hold")
        self.assertTrue(self.state.record_trope_evidence(
            self.key, "Betrayal", None, "strong"))
        entry = self._entry()
        self.assertEqual(entry["state"], "staged")
        self.assertTrue(entry["evidence_ever"])
        self.assertEqual(entry["evidence_grade"], "strong")
        # Run N+1's observe: promotion fires, via evidence (OC-7
        # stamp names the mechanism).
        out2 = self._observe()
        act = out2["trope_actions"][0]
        self.assertEqual(act["action"], "ship")
        self.assertEqual(act["stamp"]["via"], "evidence")
        self.assertEqual(out2["promoted"][0]["via"], "evidence")
        self.assertEqual(self._entry()["state"], "promoted")

    def test_single_support_records_but_never_promotes(self):
        self._observe()
        self.assertTrue(self.state.record_trope_evidence(
            self.key, "Betrayal", None, "single"))
        out = self._observe()  # second sighting lands here
        act = out["trope_actions"][0]
        # It ships — but via persistence (2 sightings), proving the
        # single grade itself never promotes via evidence.
        self.assertEqual(act["action"], "ship")
        self.assertEqual(act["stamp"]["via"], "persistence")
        self.assertEqual(self._entry()["evidence_grade"], "single")

    def test_record_requires_a_staged_entry(self):
        self.assertFalse(self.state.record_trope_evidence(
            self.key, "Betrayal", None, "strong"))
        self._observe(n_ch=5)  # auto band: ships, never stages
        self.assertFalse(self.state.record_trope_evidence(
            self.key, "Betrayal", None, "strong"))

    def test_recorded_grade_is_sticky_upward(self):
        self._observe()
        self.state.record_trope_evidence(self.key, "Betrayal",
                                         None, "single")
        self.state.record_trope_evidence(self.key, "Betrayal",
                                         None, "strong")
        self.state.record_trope_evidence(self.key, "Betrayal",
                                         None, "single")
        self.assertEqual(self._entry()["evidence_grade"], "strong")

    def test_fold_carries_the_grade(self):
        dst = {"sightings": [1], "name_forms": ["Betrayal"],
               "best_chapters": 2, "evidence_ever": False,
               "first_seen": "2026-10-01", "last_seen": "2026-10-01",
               "state": "staged", "promoted_run": None,
               "promoted_via": None, "promoted_form_changed": False}
        src = {"sightings": [2], "name_forms": ["Betrayal"],
               "best_chapters": 3, "evidence_ever": True,
               "evidence_grade": "strong",
               "first_seen": "2026-10-02", "last_seen": "2026-10-02",
               "state": "staged", "promoted_run": None,
               "promoted_via": None, "promoted_form_changed": False}
        pp.StagingState._fold_entry_into(dst, src)
        self.assertEqual(dst["evidence_grade"], "strong")
        self.assertTrue(dst["evidence_ever"])


class _TriggerEmbedder:
    """Two-dim fake: [harm, metaphor]. The query (suicide +
    definition) is pure harm; a depicting window is harm-dense; the
    metaphor window mixes both."""

    def __call__(self, texts):
        out = []
        for t in texts:
            low = (t or "").lower()
            harm = sum(low.count(w) for w in
                       ("overdose", "pills", "swallowed", "suicide"))
            meta = 1.0 if "would be suicide" in low else 0.0
            out.append([float(harm), meta])
        return out


_CH9_FILLER = [
    "The house above the harbour kept its shutters closed all winter.",
    "Maren swept the steps each morning before the light came up.",
    "The garden wall had lost two stones to the frost that year.",
    "Boats knocked softly against the pier in the dark hours.",
    "The kettle sang at six and the dog asked to be let out.",
    "Letters from the capital arrived on Thursdays, weather permitting.",
    "The roof leaked over the pantry and nowhere else in the house.",
    "Salt dried on the window glass in white, branching patterns.",
]
CH9 = _chapter(9, "Chapter Nine", " ".join(
    ["Anna counted out the pills and swallowed them all, an "
     "overdose she had planned for weeks; her intent was clear, "
     "and she meant to kill herself before morning."]
    + _CH9_FILLER
    + ["The meeting dragged so long that Tomas joked it would be "
       "suicide to schedule another one."]))


def _suicide_trigger():
    return {"warning": "suicide", "severity": "graphic",
            "severity_claimed": "graphic",
            "chapters": ["Chapter Nine"], "chapter_count": 1,
            "frequency": 0.01, "prominence": "low",
            "evidence": [], "confidence": 0.0}


class TestTriggerRetrieval(unittest.TestCase):
    def test_regex_disqualified_window_never_attaches(self):
        td = _suicide_trigger()
        out = pp.evidence_retrieve_triggers(
            [td], [CH9],
            call=MarkerJudgeCall([("overdose", "supports")]),
            embedder=_TriggerEmbedder())
        self.assertTrue(td["evidence"])
        for ev in td["evidence"]:
            self.assertEqual(ev["decision_gate"], "retrieval")
            self.assertIn("overdose", ev["quote"])
            # The metaphor window would have ranked and might even
            # have been judged — the category's negative regex
            # disqualifies it outright, before judging.
            self.assertNotIn("would be suicide", ev["quote"])
        rec = out["trigger_retrieval"]["suicide"]
        self.assertEqual(rec["support"], "single")
        self.assertEqual(out["marker"]["totals"]["attempted"], 1)

    def test_severity_and_confidence_untouched(self):
        td = _suicide_trigger()
        pp.evidence_retrieve_triggers(
            [td], [CH9],
            call=MarkerJudgeCall([("overdose", "supports")]),
            embedder=_TriggerEmbedder())
        self.assertEqual(td["severity"], "graphic")
        self.assertEqual(td["confidence"], 0.0)

    def test_evidenced_trigger_is_not_reprocessed(self):
        td = _suicide_trigger()
        td["evidence"] = [{"quote": "earlier extraction quote",
                           "chapter": "Chapter Nine",
                           "decision_gate": "regex"}]
        out = pp.evidence_retrieve_triggers(
            [td], [CH9],
            call=MarkerJudgeCall([("overdose", "supports")]),
            embedder=_TriggerEmbedder())
        self.assertEqual(len(td["evidence"]), 1)
        self.assertEqual(td["evidence"][0]["decision_gate"], "regex")
        self.assertEqual(out["trigger_retrieval"], {})
        self.assertEqual(out["marker"]["totals"]["attempted"], 0)

    def test_trigger_row_carries_retrieval_stamp(self):
        result = {"triggers": [_suicide_trigger()],
                  "trigger_retrieval": {"suicide": {
                      "support": "single",
                      "chapters_with_support": 1, "passages": 2}}}
        rows = pp._build_trigger_rows("w1", result, "m", set())
        self.assertEqual(rows[0]["evidence"]["retrieval"],
                         {"method": "targeted-v1",
                          "support": "single", "passages": 2,
                          "chapters_with_support": 1})
        result2 = {"triggers": [_suicide_trigger()]}
        rows2 = pp._build_trigger_rows("w1", result2, "m", set())
        self.assertNotIn("retrieval", rows2[0]["evidence"])

    def test_trigger_half_draws_zero_decision_budget(self):
        def explode(n=1):
            raise AssertionError("_dm_budget_claim called on G path")
        orig = pp._dm_budget_claim
        pp._dm_budget_claim = explode
        try:
            td = _suicide_trigger()
            pp.evidence_retrieve_triggers(
                [td], [CH9],
                call=MarkerJudgeCall([("overdose", "supports")]),
                embedder=_TriggerEmbedder())
        finally:
            pp._dm_budget_claim = orig
        self.assertTrue(td["evidence"])


# --- slice 5: reporting -----------------------------------------------------

class _FlakyEmbedder(FakeEmbedder):
    def __call__(self, texts):
        if any("Explodium" in (t or "") for t in texts):
            raise RuntimeError("embedding endpoint down")
        return super().__call__(texts)


class TestReporting(unittest.TestCase):
    def _mixed_marker(self):
        # The seam's real parts shape: claim specs + catalog +
        # summaries-by-label. The failed claim goes last: the
        # embedding breaker trips on it.
        parts = {
            "claims": [
                {"name": "Betrayal",
                 "support_refs": ["Chapter Two", "Chapter Five"],
                 "catalog_id": None, "staged": False},
                {"name": "Gate", "support_refs": ["Chapter Two"],
                 "catalog_id": None, "staged": False},
                {"name": "Council Politics",
                 "support_refs": ["Chapter Three"],
                 "catalog_id": None, "staged": False},
                {"name": "Dragons", "support_refs": ["Chapter One"],
                 "catalog_id": None, "staged": False},
                {"name": "Explodium Sea",
                 "support_refs": ["Chapter One"],
                 "catalog_id": None, "staged": False},
            ],
            "catalog": {},
            "summaries": {"Chapter Three": "The council debates the "
                                            "old betrayal."},
        }
        chapters = [SEA_CH, SCENE_CH, TALK_CH, SCENE2_CH]
        out = pp.evidence_retrieve_result(
            parts, chapters, call=FakeJudgeCall(),
            embedder=_FlakyEmbedder())
        return out["marker"]

    def test_accounting_identity_closes(self):
        m = self._mixed_marker()
        t = m["totals"]
        self.assertEqual(t["attempted"], 5)
        self.assertEqual(t["supported_strong"], 1)
        self.assertEqual(t["supported_single"], 1)
        self.assertEqual(t["adjacent_only"], 1)
        self.assertEqual(t["abstained"], 1)
        self.assertEqual(t["failed"], 1)
        self.assertEqual(t["attempted"],
                         t["supported_strong"] + t["supported_single"]
                         + t["adjacent_only"] + t["abstained"]
                         + t["failed"])
        # The marker's failed flag means EVERY attempted claim
        # failed; a partial failure keeps it clear.
        self.assertFalse(m["failed"])

    def test_accounting_identity_closes_after_merge(self):
        m = self._mixed_marker()
        trig = pp.evidence_retrieve_triggers(
            [{"warning": "arson", "severity": "moderate",
              "chapters": ["Chapter One"], "evidence": [],
              "confidence": 0.0}],
            [SEA_CH], call=FakeJudgeCall(), embedder=FakeEmbedder())
        merged = pp._merge_evidence_markers(m, trig["marker"])
        t = merged["totals"]
        self.assertEqual(t["attempted"], 6)
        self.assertEqual(t["abstained"], 2)
        self.assertEqual(t["attempted"],
                         t["supported_strong"] + t["supported_single"]
                         + t["adjacent_only"] + t["abstained"]
                         + t["failed"])
        self.assertFalse(merged["failed"])

    def _summary(self, marker):
        result = {"title": "T", "author": "A", "characters": [],
                  "relationships": [], "triggers": [],
                  "tropes": ["Betrayal", "Dragons"]}
        if marker is not None:
            result["evidence_retrieval"] = marker
        return pp._preview_summary(result)

    def test_preview_summary_trope_evidence_line(self):
        marker = {"claims": {"betrayal": "supported-strong",
                             "dragons": "abstained",
                             "trigger:arson": "supported-single"}}
        text = self._summary(marker)
        self.assertIn("Trope evidence: 1 claims with passages "
                      "(1 strong), 1 abstentions", text)

    def test_preview_summary_recording_declaration(self):
        marker = {"claims": {"betrayal": "supported-strong"},
                  "evidence_recorded": 2,
                  "promotes_next": ["Betrayal"]}
        text = self._summary(marker)
        self.assertIn("Staging evidence recorded: this run recorded "
                      "retrieved evidence for 2 staged claim(s)",
                      text)
        self.assertIn("Staged with strong passages: Betrayal "
                      "(promotes on next sighting via evidence)",
                      text)

    def test_preview_summary_failed_marker(self):
        text = self._summary({"failed": True, "claims": {}})
        self.assertIn("Trope evidence: retrieval FAILED", text)
        self.assertNotIn("claims with passages", text)

    def test_preview_summary_absent_without_marker(self):
        self.assertNotIn("Trope evidence", self._summary(None))


# --- slice 6: gate artefacts -------------------------------------------------

import importlib.util as _ilu
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
_gspec = _ilu.spec_from_file_location("g_gate", _ROOT / "eval"
                                      / "g-gate.py")
_gg = _ilu.module_from_spec(_gspec)
_gspec.loader.exec_module(_gg)


class TestGateArtefacts(unittest.TestCase):
    def setUp(self):
        self.fx = _gg.load_fixture()

    def test_fixture_well_formed(self):
        cases = self.fx["cases"]
        self.assertGreaterEqual(len(cases), 24)
        classes = {c["expected"] for c in cases}
        self.assertEqual(classes,
                         {"supports", "adjacent", "contradicts"})
        tags = {c["tag"] for c in cases}
        # The design names these case families explicitly.
        self.assertTrue({"negation", "discussion", "backstory"}
                        <= tags)
        chapters = {(b, c["label"]): c["text"]
                    for b, book in self.fx["books"].items()
                    for c in book["chapters"]}
        for case in cases:
            # Every labelled passage is a literal span of its
            # chapter — the fixture can never drift into quoting
            # text that is not in its book.
            self.assertIn(case["passage"],
                          chapters[(case["book"], case["chapter"])])
            self.assertIn(case["claim"], self.fx["claims"])

    def test_cross_book_bleed_zero_under_adversarial_judge(self):
        res = _gg.run_bleed(pp, self.fx, FakeEmbedder())
        self.assertEqual(res["runs"], 9)
        # The exercise is not vacuous: the supports-everything
        # judge DID attach passages — every one a literal span of
        # the target book it was retrieved from.
        self.assertGreater(res["attached"], 0)
        self.assertEqual(res["bleed"], 0)
        self.assertEqual(res["detail"], [])

    def test_trope_half_draws_zero_decision_budget(self):
        def explode(n=1):
            raise AssertionError("_dm_budget_claim called on G path")
        orig = pp._dm_budget_claim
        pp._dm_budget_claim = explode
        try:
            parts = {"claims": [{"name": "Betrayal",
                                 "support_refs": ["Chapter Two"],
                                 "catalog_id": None, "staged": False}],
                     "catalog": {}, "summaries": {}}
            out = pp.evidence_retrieve_result(
                parts, [SCENE_CH], call=FakeJudgeCall(),
                embedder=FakeEmbedder())
        finally:
            pp._dm_budget_claim = orig
        self.assertEqual(
            out["marker"]["totals"]["supported_single"], 1)


if __name__ == "__main__":
    unittest.main()
