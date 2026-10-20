"""Scaffolding prep: per-section prompt telemetry, the
definitions-diet flag, and roster name-scan inclusion.

Three changes share this file because they share one subject —
what Call A / Call B prompts are made of and what they cost:

* Per-section prompt telemetry: run totals alone cannot say
  where a prompt's mass sits, so Call A records estimated tokens
  per section (relationship definitions, base template, roster
  render, chapter text) and Call B records its own split
  (content prompt, chapter text) under prompt_section_est.
  Estimates are char-based (chars / 3.6) and named as estimates;
  they never dress up as server-reported token counts.
* v2_rel_definitions = "ids": an experiment flag that drops the
  boundary-definitions block from Call A's extraction prompt
  only. Judge prompts must be untouched — they never went
  through the renderer this flag switches.
* Roster name-scan inclusion: a roster name attested in the
  current chapter is always rendered for Call A, even when the
  recency-ranked render cap would have dropped it.

Stdlib unittest only; LLM backends are stubbed callables.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location(
    "process_portable_scaffolding_prep", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_STORY = "Mira Solace drew her sword and faced the dawn."
_CHAPTER_TEXT = (
    _STORY + " The valley was quiet and the river ran cold and "
    "bright under a wide northern sky. Kael Vane watched her "
    "from the ridge and said nothing at all.")

_CALL_A_PAYLOAD = {
    "characters": [{
        "name": "Mira Solace", "role": "protagonist",
        "description": "A swordswoman of the valley.",
        "evidence": _STORY}],
    "relationships": [], "pov_character": "Mira Solace"}

_CALL_B_PAYLOAD = {
    "summary": "A chapter summary.", "spice_level": 0,
    "triggers": {}, "trigger_evidence": {},
    "trope_candidates": [], "quotes": []}


def _chapter(text=_CHAPTER_TEXT, index=1):
    return {"index": index, "label": f"Chapter {index}",
            "text": text}


class _CaptureBackend:
    """A stand-in LLM call: records what it was sent, answers
    with a fixed JSON payload, reports no usage (so telemetry
    falls back to its character estimates)."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, prompt, chunk, i, n):
        self.calls.append((prompt, chunk))
        return json.dumps(self.payload)


class _TelemetryCase(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()
        pp._DIAG.telemetry_usage = None
        pp._DIAG.telemetry_prompt_sections = None

    def tearDown(self):
        pp._dm_budget_reset()
        pp._DIAG.telemetry_usage = None
        pp._DIAG.telemetry_prompt_sections = None


class TestPromptSectionTelemetry(_TelemetryCase):
    """The section split exists, is per-call-kind, and sums to
    (approximately) the whole prompt that was actually sent."""

    def test_unmeasured_sections_are_none_not_zero(self):
        snap = pp._telemetry_snapshot()
        self.assertIsNone(snap["call_a"]["prompt_section_est"])
        self.assertIsNone(snap["call_b"]["prompt_section_est"])

    def test_call_a_records_its_four_sections(self):
        roster = {}
        pp._roster_update(roster, [{
            "name": "Kael Vane", "aliases": ["Kael"],
            "role": "supporting", "description": "d",
            "evidence": ""}], 1)
        backend = _CaptureBackend(_CALL_A_PAYLOAD)
        result, fell_back = pp._v2_call_a_inner(
            backend, roster, _chapter(), 3)
        self.assertIsNotNone(result)
        self.assertFalse(fell_back)
        a = pp._telemetry_snapshot()["call_a"]
        self.assertEqual(a["requests"], 1)
        sections = a["prompt_section_est"]
        self.assertIsNotNone(sections)
        for key in ("rel_definitions", "template", "roster",
                    "chapter"):
            self.assertIn(key, sections)
            self.assertGreater(sections[key], 0)
        # The split describes the request that was actually sent:
        # its sections, converted back at the shared rate, account
        # for the whole prompt + chapter within rounding.
        prompt, chunk = backend.calls[0]
        self.assertEqual(a["prompt_chars_estimate"],
                         len(prompt) + len(chunk))
        whole_est = (len(prompt) + len(chunk)) / 3.6
        self.assertAlmostEqual(sum(sections.values()), whole_est,
                               delta=3)
        # The definitions share matches the block itself, not a
        # guess: it is the one section whose source text is fixed.
        self.assertEqual(
            sections["rel_definitions"],
            pp._telemetry_section_est(
                len(pp._rel_definitions_block())))
        # Call B never ran: its split stays unmeasured.
        self.assertIsNone(
            pp._telemetry_snapshot()["call_b"]["prompt_section_est"])

    def test_call_b_records_its_two_sections(self):
        backend = _CaptureBackend(_CALL_B_PAYLOAD)
        result = pp._v2_call_b_inner(backend, _chapter(), 3)
        self.assertIsNotNone(result)
        b = pp._telemetry_snapshot()["call_b"]
        self.assertEqual(b["requests"], 1)
        sections = b["prompt_section_est"]
        self.assertIsNotNone(sections)
        self.assertEqual(set(sections), {"content_prompt",
                                         "chapter"})
        self.assertEqual(
            sections["content_prompt"],
            pp._telemetry_section_est(len(pp.PROMPT_V2_CONTENT)))
        self.assertGreater(sections["chapter"], 0)
        prompt, chunk = backend.calls[0]
        whole_est = (len(prompt) + len(chunk)) / 3.6
        self.assertAlmostEqual(sum(sections.values()), whole_est,
                               delta=3)
        self.assertIsNone(
            pp._telemetry_snapshot()["call_a"]["prompt_section_est"])


class TestDefinitionsDietFlag(_TelemetryCase):
    """v2_rel_definitions: "full" by default, "ids" thins ONLY
    the Call A extraction prompt. The judges never rendered
    through the switched helper — these tests pin that their
    gloss supply survives the flag."""

    def _run_call_a(self, config_extra=None):
        backend = _CaptureBackend(_CALL_A_PAYLOAD)
        with mock.patch.dict(pp.CONFIG, config_extra or {}):
            result, _ = pp._v2_call_a_inner(
                backend, {}, _chapter(), 3)
        self.assertIsNotNone(result)
        self.assertEqual(len(backend.calls), 1)
        return backend.calls[0][0]

    def test_default_prompt_carries_definitions(self):
        prompt = self._run_call_a()
        self.assertIn("RELATIONSHIP TYPE DEFINITIONS", prompt)

    def test_ids_mode_drops_definitions_keeps_type_list(self):
        prompt = self._run_call_a({"v2_rel_definitions": "ids"})
        self.assertNotIn("RELATIONSHIP TYPE DEFINITIONS", prompt)
        # The vocabulary itself is not thinned: ids mode removes
        # the boundary glosses, never the types the model may use.
        ids = [e["id"] for e in pp._REL_VOCAB["types"]]
        self.assertIn("|".join(ids), prompt)
        # The telemetry split tells the same story as the prompt:
        # no definitions mass was sent.
        sections = pp._telemetry_snapshot()[
            "call_a"]["prompt_section_est"]
        self.assertEqual(sections["rel_definitions"], 0)
        self.assertGreater(sections["template"], 0)

    def test_unrecognised_value_falls_back_to_full(self):
        prompt = self._run_call_a({"v2_rel_definitions": "banana"})
        self.assertIn("RELATIONSHIP TYPE DEFINITIONS", prompt)

    def test_shared_renderer_ignores_the_flag(self):
        # _rel_prompt also serves the legacy identity prompts and
        # the prompt stamps; the flag lives at the Call A call
        # sites, so the renderer's own output must not move.
        with mock.patch.dict(pp.CONFIG,
                             {"v2_rel_definitions": "ids"}):
            rendered = pp._rel_prompt(pp.PROMPT_V2_CHARACTERS)
        self.assertIn("RELATIONSHIP TYPE DEFINITIONS", rendered)

    def test_judge_prompt_keeps_its_gloss_under_ids_mode(self):
        # validate_relationship builds its own instructions and
        # pulls the claimed type's gloss via _rel_gloss — capture
        # the instructions it would send and check the gloss
        # clause is intact while the flag is set.
        validator = pp.DecisionValidator("http://127.0.0.1:9")
        captured = {}

        def fake_ask(question_key, instructions, state, label=""):
            captured["instructions"] = instructions
            return 0.9

        validator._ask_noul = fake_ask
        with mock.patch.dict(pp.CONFIG,
                             {"v2_rel_definitions": "ids"}):
            verdict, _p = validator.validate_relationship(
                "Mira Solace", "Kael Vane", "spouse", _STORY)
        self.assertTrue(verdict)
        gloss = pp._rel_gloss("spouse")
        self.assertTrue(gloss)
        self.assertIn(f'"spouse" means: {gloss}',
                      captured["instructions"])


if __name__ == "__main__":
    unittest.main()
