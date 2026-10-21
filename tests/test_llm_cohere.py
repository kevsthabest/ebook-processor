"""Cohere --llm provider tests: the extraction LLM family gains a
hosted sibling next to openrouter. Stdlib unittest only; transport is
mocked at _post_json, so no network and no real API key is involved.
Run: python -m unittest discover -s tests -v
"""
import io
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import importlib.util

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _v2_response(text="{}", finish="COMPLETE", usage=None, extra_items=()):
    """A Cohere v2 chat response in the documented wire shape: the
    completion lives in message.content items typed "text"; reasoning
    arrives as separate items typed "thinking"."""
    items = list(extra_items) + [{"type": "text", "text": text}]
    resp = {"id": "test-id", "finish_reason": finish,
            "message": {"role": "assistant", "content": items}}
    if usage is not None:
        resp["usage"] = usage
    return resp


class _CohereCase(unittest.TestCase):
    """Base: isolate CONFIG keys and the environment, and swap in a
    recording fake transport for the duration of each test."""

    _CFG_KEYS = ("cohere_api_key", "cohere_key", "cohere_model", "llm",
                 "llm_max_tokens", "llm_system_prefix", "llm_think_suffix")

    def setUp(self):
        self._saved_cfg = {k: pp.CONFIG.get(k) for k in self._CFG_KEYS}
        pp.CONFIG.update({"cohere_api_key": "", "cohere_key": "",
                          "cohere_model": "command-a-plus-05-2026",
                          "llm_max_tokens": 8000, "llm_system_prefix": "",
                          "llm_think_suffix": ""})
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("COHERE_API_KEY", None)
        self._orig_post = pp._post_json
        self.calls = []

    def tearDown(self):
        pp._post_json = self._orig_post
        self._env.stop()
        for k, v in self._saved_cfg.items():
            if v is None:
                pp.CONFIG.pop(k, None)
            else:
                pp.CONFIG[k] = v
        pp._DIAG.token_boost = False

    def fake_post(self, response=None, exc=None):
        def fake(url, payload, headers, timeout):
            self.calls.append({"url": url, "payload": payload,
                               "headers": headers, "timeout": timeout})
            if exc is not None:
                raise exc
            return response
        pp._post_json = fake
        return fake


class TestKeyResolution(_CohereCase):
    def test_env_var_wins_over_config(self):
        os.environ["COHERE_API_KEY"] = "env-key"
        pp.CONFIG["cohere_api_key"] = "cfg-key"
        self.assertEqual(pp._cohere_api_key(), "env-key")

    def test_config_key_when_no_env(self):
        pp.CONFIG["cohere_api_key"] = "cfg-key"
        self.assertEqual(pp._cohere_api_key(), "cfg-key")

    def test_cohere_key_alias_accepted(self):
        pp.CONFIG["cohere_key"] = "alias-key"
        self.assertEqual(pp._cohere_api_key(), "alias-key")

    def test_unset_everywhere_is_empty(self):
        self.assertEqual(pp._cohere_api_key(), "")

    def test_default_config_ships_the_keys(self):
        self.assertEqual(pp.DEFAULT_CONFIG["cohere_api_key"], "")
        self.assertEqual(pp.DEFAULT_CONFIG["cohere_model"],
                         "command-a-plus-05-2026")


class TestRequestShape(_CohereCase):
    def test_url_auth_and_default_model(self):
        os.environ["COHERE_API_KEY"] = "env-key"
        self.fake_post(_v2_response("{}"))
        pp.llm_cohere("PROMPT", "chunk text", 1, 2)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["url"], "https://api.cohere.com/v2/chat")
        self.assertEqual(call["headers"]["Authorization"], "Bearer env-key")
        self.assertEqual(call["payload"]["model"], "command-a-plus-05-2026")

    def test_config_key_used_and_model_override(self):
        pp.CONFIG.update({"cohere_api_key": "cfg-key",
                          "cohere_model": "command-a-03-2025"})
        self.fake_post(_v2_response("{}"))
        pp.llm_cohere("PROMPT", "chunk text", 1, 2)
        call = self.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer cfg-key")
        self.assertEqual(call["payload"]["model"], "command-a-03-2025")

    def test_messages_temperature_and_max_tokens(self):
        pp.CONFIG["cohere_api_key"] = "k"
        self.fake_post(_v2_response("{}"))
        pp.llm_cohere("SYS PROMPT", "chunk text", 3, 7)
        payload = self.calls[0]["payload"]
        self.assertEqual(payload["messages"][0],
                         {"role": "system", "content": "SYS PROMPT"})
        self.assertEqual(payload["messages"][1],
                         {"role": "user", "content": "Excerpt 3/7:\n\nchunk text"})
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["max_tokens"], 8000)

    def test_system_prefix_applied_like_openai_path(self):
        pp.CONFIG.update({"cohere_api_key": "k",
                          "llm_system_prefix": "{REASON:ilow}"})
        self.fake_post(_v2_response("{}"))
        pp.llm_cohere("PROMPT", "chunk", 1, 2)
        sysmsg = self.calls[0]["payload"]["messages"][0]["content"]
        self.assertTrue(sysmsg.startswith("{REASON:ilow}"))


class TestResponseHandling(_CohereCase):
    def setUp(self):
        super().setUp()
        pp.CONFIG["cohere_api_key"] = "k"

    def test_text_items_only_thinking_excluded(self):
        # A prior extractor-seat candidate was disqualified for exactly
        # this: its reasoning channel leaked into the answer text.
        resp = _v2_response("the answer", extra_items=[
            {"type": "thinking", "thinking": "let me reason first"}])
        self.fake_post(resp)
        out = pp.llm_cohere("PROMPT", "chunk", 1, 2)
        self.assertEqual(out, "the answer")
        self.assertNotIn("reason", out)

    def test_multi_part_text_concatenated_in_order(self):
        resp = {"finish_reason": "COMPLETE",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "part one "},
                    {"type": "thinking", "thinking": "aside"},
                    {"type": "text", "text": "part two"}]}}
        self.fake_post(resp)
        self.assertEqual(pp.llm_cohere("P", "c", 1, 2), "part one part two")

    def test_thinking_only_completion_is_empty_not_leaked(self):
        resp = {"finish_reason": "MAX_TOKENS",
                "message": {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "all budget spent here"}]}}
        self.fake_post(resp)
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = pp.llm_cohere("P", "c", 1, 2)
        self.assertEqual(out, "")
        d = pp._DIAG.info
        self.assertEqual(d["finish_reason"], "length")
        self.assertTrue(d["reasoning_content"])

    def test_usage_mapped_into_telemetry(self):
        resp = _v2_response("{}", usage={
            "tokens": {"input_tokens": 1234, "output_tokens": 56},
            "billed_units": {"input_tokens": 1234, "output_tokens": 56}})
        self.fake_post(resp)
        pp.llm_cohere("P", "c", 1, 2)
        self.assertEqual(pp._DIAG.telemetry_usage,
                         {"prompt_tokens": 1234, "completion_tokens": 56})

    def test_usage_absent_or_malformed_never_crashes(self):
        for resp in (_v2_response("{}"),
                     _v2_response("{}", usage="not-a-dict"),
                     _v2_response("{}", usage={"tokens": "nope"})):
            self.fake_post(resp)
            out = pp.llm_cohere("P", "c", 1, 2)
            self.assertEqual(out, "{}")
            self.assertEqual(pp._DIAG.telemetry_usage,
                             {"prompt_tokens": None, "completion_tokens": None})


class TestMalformedResponseShapes(_CohereCase):
    """Off-spec response shapes must degrade to an empty completion,
    never raise: llm_cohere runs inside worker threads whose callers
    do not guard result retrieval, so an AttributeError/TypeError
    here aborts the whole book instead of failing one chunk."""

    def setUp(self):
        super().setUp()
        pp.CONFIG["cohere_api_key"] = "k"

    def test_message_not_a_dict_degrades_to_empty(self):
        self.fake_post({"message": "oops", "finish_reason": "COMPLETE"})
        out = pp.llm_cohere("P", "c", 1, 2)
        self.assertEqual(out, "")

    def test_text_item_with_dict_text_is_skipped(self):
        resp = {"finish_reason": "COMPLETE",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": {"unexpected": "object"}},
                    {"type": "text", "text": "real answer"}]}}
        self.fake_post(resp)
        self.assertEqual(pp.llm_cohere("P", "c", 1, 2), "real answer")

    def test_text_item_with_only_dict_text_degrades_to_empty(self):
        resp = {"finish_reason": "COMPLETE",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": {"unexpected": "object"}}]}}
        self.fake_post(resp)
        self.assertEqual(pp.llm_cohere("P", "c", 1, 2), "")

    def test_content_not_a_list_degrades_to_empty(self):
        resp = {"finish_reason": "COMPLETE",
                "message": {"role": "assistant",
                            "content": {"type": "text", "text": "x"}}}
        self.fake_post(resp)
        self.assertEqual(pp.llm_cohere("P", "c", 1, 2), "")


class TestFailureLegibility(_CohereCase):
    def test_missing_key_errors_before_any_http(self):
        self.fake_post(_v2_response("{}"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = pp.llm_cohere("PROMPT", "chunk", 1, 2)
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])
        self.assertIn("cohere_api_key", buf.getvalue())
        self.assertIn("COHERE_API_KEY", buf.getvalue())

    def test_http_500_surfaces_as_failure_not_empty_success(self):
        pp.CONFIG["cohere_api_key"] = "k"
        err = urllib.error.HTTPError(
            "https://api.cohere.com/v2/chat", 500, "Internal Server Error",
            {}, io.BytesIO(b"upstream exploded"))
        self.fake_post(exc=err)
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = pp.llm_cohere("PROMPT", "chunk", 1, 2)
        self.assertIsNone(out)
        self.assertIn("500", buf.getvalue())

    def test_connection_error_surfaces_as_failure(self):
        pp.CONFIG["cohere_api_key"] = "k"
        self.fake_post(exc=urllib.error.URLError("connection refused"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = pp.llm_cohere("PROMPT", "chunk", 1, 2)
        self.assertIsNone(out)
        self.assertIn("Cohere error", buf.getvalue())


class TestCallARoundTrip(_CohereCase):
    def test_identity_shaped_fixture_parses(self):
        pp.CONFIG["cohere_api_key"] = "k"
        body = ('{"characters": [{"name": "Peter Sebeck", '
                '"role": "protagonist", '
                '"evidence": "Detective Sebeck arrived."}], '
                '"relationships": []}')
        self.fake_post(_v2_response(body))
        out = pp.llm_cohere(pp.PROMPT_IDENTITY, "chunk text", 1, 2)
        obj = pp.parse_json_obj(out)
        self.assertIsNotNone(obj)
        self.assertEqual(obj["characters"][0]["name"], "Peter Sebeck")


class TestProviderEnumeration(unittest.TestCase):
    def test_argparse_choices_include_cohere(self):
        parser = pp._build_parser()
        for action in parser._actions:
            if "--llm" in getattr(action, "option_strings", []):
                self.assertIn("cohere", action.choices)
                return
        self.fail("--llm argument not found on the parser")

    def test_both_dispatch_maps_serve_cohere(self):
        # The invariant this pin has always guarded: no dispatch
        # site can drift away from cohere (or from any provider)
        # while another keeps it. It used to pin two literal maps
        # (extract_llm's and process_file_v2's); stage-split step 3
        # would have added a third copy for the standalone Stage D
        # entry, so the maps were deduplicated into the single
        # _llm_backend() dispatch every caller routes through —
        # the invariant is now structural, and the pin retargets:
        # exactly one map exists, it serves cohere, and both
        # original call sites route through the helper.
        src = Path(pp.__file__).read_text(encoding="utf-8")
        self.assertEqual(src.count('"cohere": llm_cohere'), 1)
        self.assertIn("def _llm_backend():", src)
        self.assertEqual(src.count("call = _llm_backend()"), 3)

    def test_extraction_identity_resolution_knows_cohere(self):
        saved = dict(pp.CONFIG)
        try:
            pp.CONFIG.update({"llm": "cohere",
                              "cohere_model": "command-a-plus-05-2026"})
            backend, model = pp._effective_judge_identity()
            self.assertEqual(backend, "cohere")
            self.assertEqual(model, "command-a-plus-05-2026")
            hist = pp._history_models()
            self.assertEqual(hist["llm_backend"], "cohere")
            self.assertEqual(hist["llm_model"], "command-a-plus-05-2026")
        finally:
            pp.CONFIG.clear()
            pp.CONFIG.update(saved)


class TestDispatchRouting(_CohereCase):
    def test_extract_llm_routes_to_cohere_backend(self):
        pp.CONFIG.update({"llm": "cohere", "cohere_api_key": "k"})
        seen = {}

        def sentinel(prompt, chunk, i, n):
            seen.setdefault("calls", []).append(prompt[:12])
            return "{}"

        orig_fn = pp.llm_cohere
        orig_run_task = pp._run_task

        def capture_run_task(call, prompt, chunk, i, n, task_name,
                             sanitize_fn=None):
            seen["backend"] = call
            # The merge keys extract_llm reads from each task result.
            return {"characters": [], "relationships": [], "quotes": []}

        # extract_llm builds its dispatch map from the module globals
        # on every call, so rebinding the name reroutes it.
        pp.llm_cohere = sentinel
        pp._run_task = capture_run_task
        try:
            pp.extract_llm("chunk text", 1, 2)
        finally:
            pp.llm_cohere = orig_fn
            pp._run_task = orig_run_task
        self.assertIs(seen["backend"], sentinel)


if __name__ == "__main__":
    unittest.main()
