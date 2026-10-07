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

    def test_short_quote_exact_match(self):
        # under 12 chars: exact normalized substring verifies...
        self.assertTrue(pp.verify_evidence("Laurie was", self.SRC))
        # ...but a short quote NOT in the source still fails
        self.assertFalse(pp.verify_evidence("Zebedee ran", self.SRC))

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
                          "openai_model": "x", "llm_max_tokens": 8000,
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


class TestPhase1bFixes(unittest.TestCase):
    """Tests for the Phase 1b fix list."""

    def test_simple_prompt_single_braces_no_placeholders(self):
        p = pp.PROMPT_IDENTITY_SIMPLE
        self.assertNotIn("{{", p)
        self.assertNotIn("{chunk_info}", p)
        # the JSON example block must parse as JSON
        start, end = p.index("{"), p.rindex("}")
        obj = __import__("json").loads(p[start:end + 1])
        self.assertIn("characters", obj)
        self.assertIn("relationships", obj)

    def test_fallback_routed_through_run_task(self):
        orig_task, orig_backend = pp._run_task, pp.llm_openai_compat
        seen = []

        def spy(call, prompt, chunk, i, n, task_name):
            seen.append(task_name)
            return orig_task(call, prompt, chunk, i, n, task_name)

        def fake_backend(prompt, chunk, i, n):
            if prompt is pp.PROMPT_IDENTITY_SIMPLE:
                return ('{"characters": [{"name": "Laurie", "role": "supporting", '
                        '"description": "a boy"}], "relationships": []}')
            return ""  # rich prompts fail empty

        pp._run_task, pp.llm_openai_compat = spy, fake_backend
        pp.CONFIG.update({"llm": "openai", "debug": False})
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                r = pp.extract_llm("Our son Laurie was three and a half.", 1, 2)
        finally:
            pp._run_task, pp.llm_openai_compat = orig_task, orig_backend
        self.assertIn("identity-fallback", seen)
        self.assertTrue(r["task_status"]["identity_simple"])
        self.assertEqual(r["characters"][0]["name"], "Laurie")

    def test_default_config_new_keys(self):
        self.assertEqual(pp.DEFAULT_CONFIG["llm_max_tokens"], 8000)
        self.assertEqual(pp.DEFAULT_CONFIG["llm_system_prefix"], "")
        self.assertEqual(pp.DEFAULT_CONFIG["llm_response_format"], "json_object")

    def test_env_int_casting(self):
        import os
        os.environ["EBOOK_LLM_MAX_TOKENS"] = "8000"
        try:
            cfg = pp.load_config()
        finally:
            del os.environ["EBOOK_LLM_MAX_TOKENS"]
        self.assertEqual(cfg["llm_max_tokens"], 8000)
        self.assertIsInstance(cfg["llm_max_tokens"], int)

    def test_env_int_invalid_keeps_default(self):
        import os
        os.environ["EBOOK_BATCH_SIZE"] = "notanint"
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cfg = pp.load_config()
        finally:
            del os.environ["EBOOK_BATCH_SIZE"]
        self.assertEqual(cfg["batch_size"], pp.DEFAULT_CONFIG["batch_size"])
        self.assertIn("Warning", buf.getvalue())

    def test_response_format_none_omits_key(self):
        pp.CONFIG.update({"openai_base_url": "http://127.0.0.1:9999/v1",
                          "openai_model": "x", "llm_response_format": "none"})
        fake = self._fake_post({"choices": [{"message": {"content": "{}"},
                                             "finish_reason": "stop"}]})
        orig = pp._post_json
        pp._post_json = fake
        try:
            pp.llm_openai_compat("PROMPT", "chunk", 1, 2)
        finally:
            pp._post_json = orig
            pp.CONFIG["llm_response_format"] = "json_object"
        self.assertNotIn("response_format", fake.last_payload)

    def test_response_format_json_object_included(self):
        pp.CONFIG.update({"llm_response_format": "json_object"})
        fake = self._fake_post({"choices": [{"message": {"content": "{}"},
                                             "finish_reason": "stop"}]})
        orig = pp._post_json
        pp._post_json = fake
        try:
            pp.llm_openai_compat("PROMPT", "chunk", 1, 2)
        finally:
            pp._post_json = orig
        self.assertEqual(fake.last_payload["response_format"],
                         {"type": "json_object"})

    def _fake_post(self, response):
        def fake(url, payload, headers, timeout):
            fake.last_payload = payload
            return response
        return fake

    def test_per_chunk_verification_in_extract(self):
        chunk = "Our son Laurie was three and a half. The sky was blue."
        disc_json = ('{"tropes": [], "triggers": [], "spice_level": 0, "povs": [], '
                     '"quotes": [{"text": "our son Laurie was three and a half", "spoiler": false}, '
                     '{"text": "a completely fabricated notable line here", "spoiler": false}], '
                     '"is_anthology": false, "stories": []}')
        ident_json = ('{"characters": [{"name": "Laurie", "role": "supporting", '
                      '"description": "a boy", '
                      '"evidence": "our son Laurie was three and a half"}, '
                      '{"name": "Ghost", "role": "minor", "description": "spooky", '
                      '"evidence": "the ghost haunted the attic nightly"}], '
                      '"relationships": []}')

        def fake_backend(prompt, chunk, i, n):
            if prompt is pp.PROMPT_DISCIPLINE:
                return disc_json
            return ident_json

        orig = pp.llm_openai_compat
        pp.llm_openai_compat = fake_backend
        pp.CONFIG.update({"llm": "openai", "debug": False})
        try:
            r = pp.extract_llm(chunk, 1, 2)
        finally:
            pp.llm_openai_compat = orig
        # fabricated character evidence dropped + flagged, claim kept
        by_name = {c["name"]: c for c in r["characters"]}
        self.assertTrue(by_name["Laurie"]["evidence_verified"])
        self.assertEqual(by_name["Ghost"]["evidence"], "")
        self.assertFalse(by_name["Ghost"]["evidence_verified"])
        # fabricated quote dropped before any cut
        self.assertEqual(len(r["quotes"]), 1)
        self.assertTrue(r["quotes"][0]["evidence_verified"])
        # per-chunk stats attached
        vs = r["verification"]
        self.assertEqual(vs["evidence_checked"], 4)  # 2 char + 2 quotes
        self.assertEqual(vs["evidence_verified"], 2)
        self.assertEqual(vs["quotes_dropped"], 1)

    def test_readme_documents_new_keys(self):
        readme = Path(pp.SCRIPT_DIR, "README.md").read_text()
        for key in ("llm_max_tokens", "llm_system_prefix", "llm_response_format"):
            self.assertIn(key, readme)

    def test_trivial_evidence_excluded_from_stats(self):
        src = "Mother was in the kitchen. Mother cooked dinner daily."
        chars = [{"name": "Mother", "evidence": "Mother"},
                 {"name": "Cook", "evidence": "Mother cooked dinner daily"}]
        vc, _, _, stats = pp.verify_all_evidence(chars, [], [], src)
        # one-word evidence: offered but not counted, text dropped
        self.assertTrue(vc[0]["evidence_offered"])
        self.assertFalse(vc[0]["evidence_verified"])
        self.assertEqual(vc[0]["evidence"], "")
        # real evidence still verifies and counts
        self.assertTrue(vc[1]["evidence_verified"])
        self.assertEqual(stats["evidence_checked"], 1)
        self.assertEqual(stats["evidence_verified"], 1)

    def test_spread_quotes(self):
        quotes = [{"text": f"quote {i}"} for i in range(30)]
        picked = pp._spread_quotes(quotes, 10)
        self.assertEqual(len(picked), 10)
        # evenly spaced: first, middle and last regions represented
        self.assertEqual(picked[0]["text"], "quote 0")
        self.assertEqual(picked[5]["text"], "quote 15")
        self.assertEqual(picked[9]["text"], "quote 27")
        # short lists pass through untouched
        short = [{"text": "a"}, {"text": "b"}]
        self.assertEqual(pp._spread_quotes(short, 10), short)

    def test_select_chunks(self):
        chunks = ["a", "b", "c", "d", "e"]
        # no filter: all chunks, 1-based numbering
        self.assertEqual(pp._select_chunks(chunks, None),
                         [(1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e")])
        self.assertEqual(pp._select_chunks(chunks, ""), [(1, "a"), (2, "b"),
                         (3, "c"), (4, "d"), (5, "e")])
        # single + multi select keep original numbering
        self.assertEqual(pp._select_chunks(chunks, "2"), [(2, "b")])
        self.assertEqual(pp._select_chunks(chunks, "2,5"), [(2, "b"), (5, "e")])
        # junk ignored, no match -> empty
        self.assertEqual(pp._select_chunks(chunks, "9"), [])
        self.assertEqual(pp._select_chunks(chunks, "x,3"), [(3, "c")])

    def test_env_bool_casting(self):
        import os
        os.environ["EBOOK_DEDUPE"] = "false"
        os.environ["EBOOK_DEBUG"] = "yes"
        try:
            cfg = pp.load_config()
        finally:
            del os.environ["EBOOK_DEDUPE"]
            del os.environ["EBOOK_DEBUG"]
        self.assertIs(cfg["dedupe"], False)
        self.assertIs(cfg["debug"], True)


if __name__ == "__main__":
    unittest.main()