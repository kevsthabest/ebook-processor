"""Tests for scaledown_lab.py, the experimental ScaleDown harness.

Hermetic throughout: the transport is injected into the experiment
runners, so every test drives the real payload builders, parsers,
and metric maths with a fake post_json and never touches the
network. The metric tests pin the lab's scoring to the same
definitions evaluate-relationships.py reports, because numbers
that drift from the harness's definitions would look comparable
while not being comparable.

Stdlib unittest only. Run: python -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lab = _load("scaledown_lab_under_test", "scaledown_lab.py")

SCORING = {"keep": 0.70, "drop": 0.30,
           "family": frozenset({"spouse", "parent", "child", "sibling"}),
           "glosses": {"spouse": "Married partners."}}


class ScoringMirrorTests(unittest.TestCase):
    def test_family_bands_are_three_tier(self):
        self.assertEqual(lab.band(0.70, "spouse", SCORING), "keep")
        self.assertEqual(lab.band(0.69, "spouse", SCORING), "middle")
        self.assertEqual(lab.band(0.30, "spouse", SCORING), "middle")
        self.assertEqual(lab.band(0.29, "spouse", SCORING), "drop")

    def test_nonfamily_bands_are_two_tier(self):
        self.assertEqual(lab.band(0.30, "friend", SCORING), "keep")
        self.assertEqual(lab.band(0.29, "friend", SCORING), "drop")

    def test_unanswered_score_bands_unanswered(self):
        self.assertEqual(lab.band(None, "spouse", SCORING), "unanswered")

    def test_draw_count_near_edge_earns_five(self):
        # 0.85 sits exactly on the 0.15 margin of the 0.70 edge;
        # binary floating point makes it 0.15000000000000008, and
        # the case at the margin is the case the draws exist for.
        self.assertEqual(lab.draw_count(0.85, "spouse", SCORING), 5)
        # Non-family claims have only the drop edge at 0.30.
        self.assertEqual(lab.draw_count(0.20, "friend", SCORING), 5)

    def test_draw_count_far_from_edges_stays_three(self):
        self.assertEqual(lab.draw_count(0.97, "spouse", SCORING), 3)
        self.assertEqual(lab.draw_count(0.50, "friend", SCORING), 3)
        self.assertEqual(lab.draw_count(0.10, "friend", SCORING), 3)
        self.assertEqual(lab.draw_count(None, "friend", SCORING), 3)

    def test_case_outcome_agreement_disagreement_and_silence(self):
        outcome, median, spread = lab.case_outcome(
            [0.9, 0.92, 0.88], "spouse", SCORING)
        self.assertEqual(outcome, "keep")
        self.assertAlmostEqual(median, 0.9)
        self.assertAlmostEqual(spread, 0.04)
        outcome, _, _ = lab.case_outcome([0.9, 0.1, 0.85], "spouse",
                                         SCORING)
        self.assertEqual(outcome, "unstable")
        outcome, median, spread = lab.case_outcome([], "spouse", SCORING)
        self.assertEqual(outcome, "unanswered")
        self.assertIsNone(median)
        self.assertIsNone(spread)


class MetricsTests(unittest.TestCase):
    def test_four_metrics_with_abstention(self):
        rows = [
            {"expect": "keep", "outcome": "keep"},
            {"expect": "keep", "outcome": "drop"},
            {"expect": "drop", "outcome": "drop"},
            {"expect": "drop", "outcome": "keep"},
            {"expect": "keep", "outcome": "unstable"},
            {"expect": "drop", "outcome": "middle"},
        ]
        m = lab.compute_metrics(rows)
        self.assertEqual(m["cases"], 6)
        # Predicted keeps: rows 1 and 4; the true one is row 1.
        self.assertEqual(m["precision"], (1, 2))
        # Expected keeps: rows 1, 2, 5; the landed one is row 1.
        self.assertEqual(m["recall"], (1, 3))
        # Decided rows: 1, 2, 3, 4; exact matches: 1 and 3.
        self.assertEqual(m["type_accuracy"], (2, 4))
        # Unstable + middle abstain.
        self.assertEqual(m["abstention"], (2, 6))

    def test_metrics_empty_scope_is_safe(self):
        m = lab.compute_metrics([])
        self.assertEqual(m["precision"], (0, 0))
        self.assertIn("n/a", lab.fmt_metric("precision", m["precision"]))


class ClassifyPayloadTests(unittest.TestCase):
    CASE = {"from": "Laura Sebeck", "to": "Peter Sebeck",
            "type": "spouse", "quote": "His wife, Laura, smiled.",
            "expect": "keep", "work": "Daemon"}

    def test_payload_shape(self):
        payload = lab.build_classify_payload(
            self.CASE, ["spouse", "friend"], SCORING)
        self.assertEqual(payload["model"], "scaledown/classify-1")
        self.assertEqual(payload["state"],
                         "Quote:\nHis wife, Laura, smiled.")
        question = payload["questions"]["rel_type"]
        self.assertEqual(question["type"], "choice")
        self.assertIn("Laura Sebeck", question["instructions"])
        self.assertIn("Peter Sebeck", question["instructions"])
        # The label set is the candidate types plus the catch-all.
        self.assertEqual(sorted(question["criteria"]),
                         ["friend", "none", "spouse"])

    def test_rubric_carries_gloss_and_pair(self):
        payload = lab.build_classify_payload(
            self.CASE, ["spouse"], SCORING)
        rubric = payload["questions"]["rel_type"]["criteria"]["spouse"]
        self.assertIn("Married partners.", rubric)
        self.assertIn("Laura Sebeck is the spouse of Peter Sebeck",
                      rubric)
        none_rubric = payload["questions"]["rel_type"]["criteria"]["none"]
        self.assertIn("does not state", none_rubric)

    def test_state_carries_context_window_when_present(self):
        case = dict(self.CASE, context="Earlier sentences here.")
        payload = lab.build_classify_payload(case, ["spouse"], SCORING)
        self.assertEqual(
            payload["state"],
            "Context:\nEarlier sentences here.\n\nQuote:\n"
            "His wife, Laura, smiled.")

    def test_candidate_types_stable_order_and_dedup(self):
        cases = [{"type": "child"}, {"type": "spouse"},
                 {"type": "child"}, {"type": "friend"}]
        self.assertEqual(lab.candidate_types(cases),
                         ["child", "spouse", "friend"])


class ParseChoiceAnswerTests(unittest.TestCase):
    def test_probabilities_distribution(self):
        dist, picked = lab.parse_choice_answer(
            {"type": "choice", "choice": "spouse",
             "probabilities": {"spouse": 0.8, "none": 0.2}})
        self.assertEqual(dist, {"spouse": 0.8, "none": 0.2})
        self.assertEqual(picked, "spouse")

    def test_picked_falls_back_to_argmax(self):
        dist, picked = lab.parse_choice_answer(
            {"probabilities": {"friend": 0.3, "none": 0.7}})
        self.assertEqual(picked, "none")

    def test_bare_pick_has_no_distribution(self):
        # A pick without per-option probabilities cannot be
        # banded; the parser must not invent a distribution.
        dist, picked = lab.parse_choice_answer(
            {"choice": "spouse", "confidence": 0.9})
        self.assertIsNone(dist)
        self.assertEqual(picked, "spouse")

    def test_garbage_answers(self):
        self.assertEqual(lab.parse_choice_answer(None), (None, None))
        self.assertEqual(lab.parse_choice_answer("spouse"), (None, None))
        self.assertEqual(
            lab.parse_choice_answer({"probabilities": {"x": "high"}}),
            (None, None))


class FakeTransport:
    """Stands in for http_post_json. distributions maps a quote
    substring to either one distribution dict (returned every
    draw) or a list of dicts (one per draw, in order)."""

    def __init__(self, distributions):
        self.distributions = distributions
        self.calls = []

    def __call__(self, url, payload, api_key):
        self.calls.append(payload)
        state = payload["state"]
        for needle, dist in self.distributions.items():
            if needle in state:
                if isinstance(dist, list):
                    return {"answers": {"rel_type": {
                        "type": "choice",
                        "probabilities": dist.pop(0)}},
                        "usage": {"prompt_tokens": 100}}
                return {"answers": {"rel_type": {
                    "type": "choice", "probabilities": dict(dist)}},
                    "usage": {"prompt_tokens": 100}}
        raise AssertionError(f"no canned distribution for {state!r}")


class ClassifyRunTests(unittest.TestCase):
    CASES = [
        {"from": "A", "to": "B", "type": "spouse", "quote": "wife quote",
         "expect": "keep", "work": "Book"},
        {"from": "C", "to": "D", "type": "friend", "quote": "junk quote",
         "expect": "drop", "work": "Book"},
        {"from": "E", "to": "F", "type": "sibling",
         "quote": "flip quote", "expect": "keep", "work": "Book"},
    ]

    def test_end_to_end_metrics(self):
        transport = FakeTransport({
            "wife quote": {"spouse": 0.9, "none": 0.1},
            "junk quote": {"friend": 0.05, "none": 0.95},
            # Draws straddle the family keep edge: unstable.
            "flip quote": [{"sibling": 0.95, "none": 0.05},
                           {"sibling": 0.2, "none": 0.8},
                           {"sibling": 0.9, "none": 0.1}],
        })
        budget = lab.TokenBudget(100000)
        records, types = lab.run_classify(
            self.CASES, transport, "test-key", "http://example.invalid",
            SCORING, budget)
        self.assertEqual(types, ["spouse", "friend", "sibling"])
        by_quote = {r["quote_excerpt"]: r for r in records}
        self.assertEqual(by_quote["wife quote"]["outcome"], "keep")
        self.assertEqual(by_quote["wife quote"]["predicted_type"],
                         "spouse")
        self.assertEqual(by_quote["junk quote"]["outcome"], "drop")
        self.assertEqual(by_quote["flip quote"]["outcome"], "unstable")
        reports = dict(lab.classify_reports(records))
        pooled = reports["all books"]
        self.assertEqual(pooled["precision"], (1, 1))
        self.assertEqual(pooled["recall"], (1, 2))
        self.assertEqual(pooled["type_accuracy"], (2, 2))
        self.assertEqual(pooled["abstention"], (1, 3))
        # 3 draws per case at the canned 100 prompt tokens each.
        self.assertEqual(budget.spent, 900)

    def test_budget_stop_marks_remaining_cases(self):
        transport = FakeTransport({
            "wife quote": {"spouse": 0.9, "none": 0.1},
            "junk quote": {"friend": 0.05, "none": 0.95},
            "flip quote": {"sibling": 0.9, "none": 0.1},
        })
        budget = lab.TokenBudget(250)
        records, _ = lab.run_classify(
            self.CASES, transport, "test-key", "http://example.invalid",
            SCORING, budget)
        # The first case spends 300 of actuals across 3 draws
        # (charges land after each call); later cases cannot run.
        self.assertEqual(records[0]["outcome"], "keep")
        self.assertEqual(records[1]["outcome"], "unanswered")
        self.assertIn("budget", records[1]["error"])

    def test_transport_error_is_unanswered_not_a_crash(self):
        def exploding(url, payload, api_key):
            raise lab.LabError("HTTP 500 from the API endpoint")

        records, _ = lab.run_classify(
            self.CASES[:1], exploding, "test-key",
            "http://example.invalid", SCORING, lab.TokenBudget(1000))
        self.assertEqual(records[0]["outcome"], "unanswered")
        self.assertIn("HTTP 500", records[0]["error"])


class ExtractPayloadTests(unittest.TestCase):
    def test_payload_shape_and_entities(self):
        payload = lab.build_extract_payload("Chapter text here.", [])
        self.assertEqual(payload["model"], "scaledown/extract")
        self.assertEqual(payload["messages"][0]["content"],
                         "Chapter text here.")
        entities = payload["provider"]["options"]["scaledown"]["entities"]
        names = [e["name"] for e in entities]
        self.assertEqual(names, ["character_name", "character_alias",
                                 "relationship_phrase"])
        for entry in entities:
            self.assertTrue(entry["description"])

    def test_roster_rides_the_message(self):
        payload = lab.build_extract_payload(
            "Text.", ["Peter Sebeck", "Laura Sebeck"])
        content = payload["messages"][0]["content"]
        self.assertTrue(content.startswith("Known characters in this "
                                           "book: Peter Sebeck, "
                                           "Laura Sebeck"))
        self.assertTrue(content.endswith("Text."))


class ParseExtractContentTests(unittest.TestCase):
    def test_clean_json(self):
        entities, note = lab.parse_extract_content(json.dumps({
            "entities": [
                {"type": "character_name", "text": "Peter Sebeck",
                 "start": 0, "end": 12, "confidence": 0.9},
                {"type": "character_name"},
            ]}))
        self.assertEqual(note, "")
        # The textless entry is dropped, not carried as a name.
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["text"], "Peter Sebeck")

    def test_prose_wrapped_json_is_salvaged(self):
        content = ("Here is what I found: {\"entities\": [{\"type\": "
                   "\"character_name\", \"text\": \"Laura\", \"start\": "
                   "5, \"end\": 10, \"confidence\": 0.5}]} — done.")
        entities, note = lab.parse_extract_content(content)
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["text"], "Laura")

    def test_unparseable_reply_reports_a_note(self):
        entities, note = lab.parse_extract_content("no json at all")
        self.assertEqual(entities, [])
        self.assertTrue(note)


class GroupNamesTests(unittest.TestCase):
    def test_grouping_counts_and_confidence(self):
        entities = [
            {"type": "character_name", "text": "Peter Sebeck",
             "start": 0, "end": 12, "confidence": 0.7},
            {"type": "character_name", "text": "peter  sebeck",
             "start": 40, "end": 52, "confidence": 0.9},
            {"type": "character_name", "text": "Laura",
             "start": 60, "end": 65, "confidence": None},
            {"type": "relationship_phrase", "text": "his wife",
             "start": 60, "end": 68, "confidence": 0.5},
        ]
        names = lab.group_character_names(entities)
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0]["name"], "Peter Sebeck")
        self.assertEqual(names[0]["mentions"], 2)
        self.assertEqual(names[0]["max_confidence"], 0.9)
        self.assertEqual(names[1]["name"], "Laura")
        self.assertIsNone(names[1]["max_confidence"])


class SummarizePayloadTests(unittest.TestCase):
    def test_bare_text_by_default(self):
        payload = lab.build_summarize_payload("A long chapter.")
        self.assertEqual(payload["model"], "scaledown/summarize")
        self.assertEqual(payload["messages"][0]["content"],
                         "A long chapter.")

    def test_instruction_prefix_when_given(self):
        payload = lab.build_summarize_payload(
            "A long chapter.", "Summarize this chapter in 3 sentences.")
        self.assertEqual(
            payload["messages"][0]["content"],
            "Summarize this chapter in 3 sentences.\n\nA long chapter.")


def _write_epub(path):
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="cover"/><itemref idref="c1"/>
  <itemref idref="c2"/></spine>
</package>"""
    body = ("<p>Peter Sebeck opened the door &amp; stared. "
            "Laura was waiting for him, and the night was cold.</p>")
    chapter = ("<html><head><title>t</title><style>p{}</style></head>"
               f"<body>{body * 12}</body></html>")
    cover = "<html><body><p>Title page</p></body></html>"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/cover.xhtml", cover)
        zf.writestr("OEBPS/chapter1.xhtml", chapter)
        zf.writestr("OEBPS/chapter2.xhtml", chapter)


class EpubChapterTests(unittest.TestCase):
    def test_chapters_filtered_and_decoded(self):
        with tempfile.TemporaryDirectory() as td:
            epub = Path(td) / "book.epub"
            _write_epub(epub)
            chapters = lab.epub_chapters(str(epub), min_chars=800)
        # The title page falls under the floor; both chapters land.
        self.assertEqual(len(chapters), 2)
        self.assertEqual([c["index"] for c in chapters], [0, 1])
        self.assertIn("Peter Sebeck opened the door & stared.",
                      chapters[0]["text"])
        self.assertNotIn("<p>", chapters[0]["text"])

    def test_text_file_is_one_chapter(self):
        with tempfile.TemporaryDirectory() as td:
            txt = Path(td) / "chapter.txt"
            txt.write_text("Some chapter text.", encoding="utf-8")
            chapters = lab.text_chapters(str(txt))
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0]["text"], "Some chapter text.")

    def test_pick_chapter_out_of_range_explains(self):
        with self.assertRaises(lab.LabError):
            lab.pick_chapter([{"index": 0, "label": "x", "text": "t"}], 3)


class KeyResolutionTests(unittest.TestCase):
    def _config(self, td, data, name="config.json"):
        cfg = Path(td) / name
        cfg.write_text(json.dumps(data), encoding="utf-8")
        return str(cfg)

    def test_config_wins_over_env_like_the_processor(self):
        # The shipped processor reads config before the
        # environment; the lab mirrors it so the same machine
        # setup drives both tools identically.
        with tempfile.TemporaryDirectory() as td:
            cfg = self._config(td, {"openrouter_api_key": "from-config"})
            with mock.patch.dict(os.environ,
                                 {"OPENROUTER_API_KEY": "from-env"}):
                self.assertEqual(lab.resolve_api_key(cfg), "from-config")

    def test_env_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            absent = str(Path(td) / "absent.json")
            with mock.patch.dict(os.environ,
                                 {"OPENROUTER_API_KEY": "from-env"}):
                self.assertEqual(lab.resolve_api_key(absent), "from-env")

    def test_config_key_fields_in_processor_order(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._config(td, {"openrouter_api_key": "dm-key",
                                    "openrouter_key": "llm-key"},
                               name="both.json")
            cfg2 = self._config(td, {"openrouter_key": "llm-key"},
                                name="llm.json")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(lab.resolve_api_key(cfg), "dm-key")
                self.assertEqual(lab.resolve_api_key(cfg2), "llm-key")
                self.assertEqual(lab.resolve_api_key(
                    str(Path(td) / "absent.json")), "")


class GuardrailTests(unittest.TestCase):
    def test_budget_charges_and_trips(self):
        budget = lab.TokenBudget(100)
        self.assertTrue(budget.charge(60))
        self.assertFalse(budget.exceeded)
        budget.charge(50)
        self.assertTrue(budget.exceeded)

    def test_estimate_and_cost_math(self):
        self.assertEqual(lab.estimate_tokens("x" * 400), 100)
        self.assertAlmostEqual(lab.estimate_cost(1000000), 0.04)
        self.assertEqual(lab.estimate_cost(0, 1000000), 0.0)

    def test_catalog_price_from_list_and_endpoints_shapes(self):
        self.assertEqual(
            lab.catalog_price({"pricing": {"prompt": "0.00000004",
                                           "completion": "0"}}),
            (0.00000004, 0.0))
        self.assertEqual(
            lab.catalog_price({"endpoints": [
                {"pricing": {"prompt": "0.00000004",
                             "completion": "0"}}]}),
            (0.00000004, 0.0))
        self.assertIsNone(lab.catalog_price({"pricing": {}}))
        self.assertIsNone(lab.catalog_price(None))

    def test_excerpt_caps_and_collapses(self):
        self.assertEqual(lab.excerpt("a  b\n c"), "a b c")
        self.assertLessEqual(len(lab.excerpt("x" * 500)), 200)


class CommandWiringTests(unittest.TestCase):
    def test_classify_dry_run_prints_payloads_without_a_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = lab.main(["classify", "--dry-run"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("scaledown/classify-1", text)
        self.assertIn('"criteria"', text)

    def test_extract_without_input_explains_what_is_needed(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = lab.main(["extract"])
        self.assertEqual(rc, 2)
        self.assertIn("--epub", out.getvalue())

    def test_extract_live_without_key_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            txt = Path(td) / "chapter.txt"
            txt.write_text("Peter walked in. " * 50, encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = lab.main(["extract", "--text", str(txt),
                                   "--config", str(Path(td) / "no.json"),
                                   "--out-dir", str(Path(td) / "out")])
        self.assertEqual(rc, 2)
        self.assertIn("no OpenRouter key", out.getvalue())

    def test_summarize_dry_run_prints_payload(self):
        with tempfile.TemporaryDirectory() as td:
            txt = Path(td) / "chapter.txt"
            txt.write_text("Peter walked in.", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                rc = lab.main(["summarize", "--text", str(txt),
                               "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("scaledown/summarize", out.getvalue())


if __name__ == "__main__":
    unittest.main()
