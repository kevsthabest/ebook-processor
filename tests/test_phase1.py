"""Phase 1 tests: evidence verification + empty-response diagnostics.
Stdlib unittest only. No network, no Supabase, no real LLM calls.
Run: python -m unittest discover -s tests -v
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import importlib.util

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class TestVerifyEvidence(unittest.TestCase):
    SRC = ("Shirley Jackson was born in San Francisco in 1916. "
           "Our son Laurie was three and a half, and our daughter "
           "Jannie was six months old. \u201cMake it really forceful,\u201d "
           "my husband said.")

    def test_exact_match(self):
        self.assertTrue(pp.verify_evidence(
            "our son Laurie was three and a half", self.SRC))

    def test_normalization_curly_quotes_and_case(self):
        # straight quotes + different case still match curly-quoted source
        self.assertTrue(pp.verify_evidence(
            '"make it really forceful," my husband said', self.SRC))

    def test_whitespace_collapsed(self):
        self.assertTrue(pp.verify_evidence(
            "our  son\nLaurie\twas three and a half", self.SRC))

    def test_trailing_punctuation_stripped(self):
        self.assertTrue(pp.verify_evidence(
            "our son Laurie was three and a half...", self.SRC))

    def test_fabricated_quote_rejected(self):
        self.assertFalse(pp.verify_evidence(
            "Laurie rode his bicycle to the haunted mansion", self.SRC))

    def test_too_short_rejected(self):
        self.assertFalse(pp.verify_evidence("Laurie was", self.SRC))

    def test_fuzzy_minor_difference_accepted(self):
        # dropped short word (typical transcription slip) still verifies
        self.assertTrue(pp.verify_evidence(
            "our son Laurie was three and half", self.SRC))

    def test_fuzzy_major_difference_rejected(self):
        self.assertFalse(pp.verify_evidence(
            "our daughter Jannie was sixteen years old today", self.SRC))


class TestVerifyAllEvidence(unittest.TestCase):
    SRC = "Our son Laurie was three and a half. The sky was blue."

    def test_flow(self):
        chars = [
            {"name": "Laurie", "evidence": "our son Laurie was three and a half"},
            {"name": "Ghost", "evidence": "the ghost haunted the attic nightly"},
            {"name": "NoEv", "evidence": ""},
        ]
        rels = [{"from": "A", "to": "B", "type": "friend",
                 "evidence": "A and B were friends forevermore"}]
        quotes = [
            {"text": "our son Laurie was three and a half", "spoiler": False},
            {"text": "a completely invented memorable line here", "spoiler": True},
        ]
        vc, vr, vq, stats = pp.verify_all_evidence(chars, rels, quotes, self.SRC)
        # verified character keeps evidence + flag
        self.assertEqual(vc[0]["evidence"], "our son Laurie was three and a half")
        self.assertTrue(vc[0]["evidence_verified"])
        # fabricated character evidence dropped, claim kept + flagged
        self.assertEqual(vc[1]["evidence"], "")
        self.assertFalse(vc[1]["evidence_verified"])
        self.assertEqual(vc[1]["name"], "Ghost")  # claim survives
        # empty evidence: not checked, not flagged as failed
        self.assertEqual(vc[2]["evidence"], "")
        self.assertFalse(vc[2]["evidence_verified"])
        # fabricated relationship evidence dropped
        self.assertEqual(vr[0]["evidence"], "")
        self.assertFalse(vr[0]["evidence_verified"])
        # fabricated quote dropped entirely, real one kept
        self.assertEqual(len(vq), 1)
        self.assertTrue(vq[0]["evidence_verified"])
        # stats
        self.assertEqual(stats["evidence_checked"], 5)  # 2 char + 1 rel + 2 quotes; empty evidence not checked
        self.assertEqual(stats["evidence_verified"], 2)
        self.assertEqual(stats["quotes_dropped"], 1)


class TestEmptyResponseDiagnostics(unittest.TestCase):
    def _fake_post(self, response):
        def fake(url, payload, headers, timeout):
            # capture the payload for assertions
            fake.last_payload = payload
            return response
        return fake

    def test_diag_captured_on_empty(self):
        pp.CONFIG.update({"openai_base_url": "http://127.0.0.1:9999/v1",
                          "openai_model": "x", "llm_max_tokens": 4000,
                          "llm_system_prefix": ""})
        resp = {"choices": [{"message": {"content": "",
                                         "reasoning_content": "<think>...</think>"},
                             "finish_reason": "length"}],
                "usage": {"completion_tokens": 3892}}
        orig = pp._post_json
        pp._post_json = self._fake_post(resp)
        try:
            out = pp.llm_openai_compat("sys", "chunk", 1, 2)
        finally:
            pp._post_json = orig
        self.assertEqual(out, "")
        d = pp._DIAG.info
        self.assertEqual(d["finish_reason"], "length")
        self.assertEqual(d["completion_tokens"], 3892)
        self.assertTrue(d["reasoning_content"])

    def test_run_task_reports_empty_diag(self):
        pp.CONFIG.update({"debug": False})
        pp._DIAG.info = {"finish_reason": "length", "completion_tokens": 4000,
                         "reasoning_content": True}
        calls = {"n": 0}

        def fake_call(prompt, chunk, i, n):
            calls["n"] += 1
            return ""  # empty both attempts

        buf = io.StringIO()
        with redirect_stdout(buf):
            r = pp._run_task(fake_call, "p", "chunk text", 1, 2, "identity")
        self.assertIsNone(r)
        out = buf.getvalue()
        self.assertIn("finish_reason=length", out)
        self.assertIn("reasoning_content=yes", out)
        self.assertEqual(calls["n"], 2)

    def test_system_prefix_and_max_tokens_applied(self):
        pp.CONFIG.update({"openai_base_url": "http://127.0.0.1:9999/v1",
                          "openai_model": "x", "llm_max_tokens": 8000,
                          "llm_system_prefix": "{REASON:ilow}"})
        fake = self._fake_post({"choices": [{"message": {"content": "{}"},
                                             "finish_reason": "stop"}]})
        orig = pp._post_json
        pp._post_json = fake
        try:
            pp.llm_openai_compat("PROMPT", "chunk", 1, 2)
        finally:
            pp._post_json = orig
        payload = fake.last_payload
        self.assertEqual(payload["max_tokens"], 8000)
        self.assertTrue(payload["messages"][0]["content"].startswith("{REASON:ilow}"))


if __name__ == "__main__":
    unittest.main()
