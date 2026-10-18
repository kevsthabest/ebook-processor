"""Corpus-routing Phase 0 telemetry (docs/corpus-routing-spec.md
§6, scoped by adopted amendment A2: TELEMETRY ONLY).

Per-book accounting for a v2 run, emitted in the preview JSON and
in the run-event book-end payload:

* Call A / Call B, separately: request count, prompt tokens from
  server-reported usage when the endpoint supplies it (otherwise
  the UTF-8 prompt character count under prompt_chars_estimate —
  an estimate never dressed as a token measurement), completion
  tokens when reported, and elapsed seconds.
* Decision work split by stage (merge / relationship / trigger /
  quote): call counts derived from the EXISTING per-consumer
  budget accounting (never a second count of the same calls), so
  the split sums to the decision-call total the run already
  reports, plus elapsed seconds per stage.

The per-book arming discipline is the budget's: telemetry arms
where _dm_budget_reset arms (book start in process_file_v2), so a
book's totals can never be borrowed from a prior book — the
2.13.49/2.13.50 stamp-leak class. No routing fields are emitted:
routing is not built in this phase.

Stdlib unittest only. LLM backends are stubbed at the _post_json /
backend-function boundary; no network.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location(
    "process_portable_corpus_routing_telemetry", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _run_task_json(call, prompt, chunk, task_name):
    return pp._run_task(call, prompt, chunk, 1, 1, task_name,
                        sanitize_fn=lambda d: d)


class TestCallAccounting(unittest.TestCase):
    """_run_task-level accounting for Call A / Call B."""

    def setUp(self):
        pp._dm_budget_reset()
        pp._DIAG.telemetry_usage = None

    def tearDown(self):
        pp._dm_budget_reset()
        pp._DIAG.telemetry_usage = None

    def test_usage_reported_populates_token_fields(self):
        def fake(prompt, chunk, i, n):
            pp._DIAG.telemetry_usage = {
                "prompt_tokens": 120, "completion_tokens": 30}
            return '{"ok": true}'

        _run_task_json(fake, "sys prompt", "chapter text",
                       "v2-ch1-identity")
        _run_task_json(fake, "sys prompt", "chapter text",
                       "v2-ch1-identity")
        _run_task_json(fake, "sys prompt", "chapter text",
                       "v2-ch1-content")
        snap = pp._telemetry_snapshot()
        a, b = snap["call_a"], snap["call_b"]
        self.assertEqual(a["requests"], 2)
        self.assertEqual(a["prompt_tokens"], 240)
        self.assertEqual(a["completion_tokens"], 60)
        # Every request reported usage: no estimate was needed and
        # none may be presented.
        self.assertIsNone(a["prompt_chars_estimate"])
        self.assertGreaterEqual(a["elapsed_sec"], 0)
        self.assertEqual(b["requests"], 1)
        self.assertEqual(b["prompt_tokens"], 120)
        self.assertEqual(b["completion_tokens"], 30)
        self.assertIsNone(b["prompt_chars_estimate"])

    def test_no_usage_records_chars_estimate_and_null_tokens(self):
        def fake(prompt, chunk, i, n):
            return '{"ok": true}'

        _run_task_json(fake, "prompt", "chunk text",
                       "v2-ch2-content")
        snap = pp._telemetry_snapshot()
        b = snap["call_b"]
        self.assertEqual(b["requests"], 1)
        # Honest absence: unmeasured tokens are None, never 0.
        self.assertIsNone(b["prompt_tokens"])
        self.assertIsNone(b["completion_tokens"])
        self.assertEqual(b["prompt_chars_estimate"],
                         len("prompt") + len("chunk text"))
        self.assertEqual(snap["call_a"]["requests"], 0)
        self.assertIsNone(snap["call_a"]["prompt_tokens"])
        self.assertIsNone(snap["call_a"]["prompt_chars_estimate"])

    def test_fallback_task_counts_as_call_a(self):
        def fake(prompt, chunk, i, n):
            return '{"ok": true}'

        _run_task_json(fake, "p", "c", "v2-ch4-identity-fallback")
        self.assertEqual(
            pp._telemetry_snapshot()["call_a"]["requests"], 1)

    def test_non_ab_tasks_are_not_counted(self):
        def fake(prompt, chunk, i, n):
            return '{"ok": true}'

        for task in ("discipline", "identity", "identity-fallback",
                     "v2-trope-gate-b1", "evidence-judge-b1"):
            _run_task_json(fake, "p", "c", task)
        snap = pp._telemetry_snapshot()
        self.assertEqual(snap["call_a"]["requests"], 0)
        self.assertEqual(snap["call_b"]["requests"], 0)

    def test_nonfinite_tokens_recorded_as_unreported(self):
        # _post_json parses with json.loads, which accepts the
        # NaN / Infinity literals, so non-finite floats can arrive
        # from the wire in a usage block. int() raises on them
        # (ValueError for NaN, OverflowError for the infinities),
        # and the recorder runs in _run_task's finally — a raise
        # there destroys a successful result. A non-finite value
        # must degrade to "unreported": its token field stays
        # null and (for prompt tokens) the character estimate is
        # populated, exactly as if no usage had been reported.
        for bad in (float("nan"), float("inf"), float("-inf")):
            for field in ("prompt_tokens", "completion_tokens"):
                with self.subTest(bad=bad, field=field):
                    pp._dm_budget_reset()
                    pp._DIAG.telemetry_usage = None
                    usage = {"prompt_tokens": 120,
                             "completion_tokens": 30}
                    usage[field] = bad

                    def fake(prompt, chunk, i, n, usage=usage):
                        pp._DIAG.telemetry_usage = usage
                        return '{"ok": true}'

                    _run_task_json(fake, "prompt", "chunk text",
                                   "v2-ch1-content")
                    b = pp._telemetry_snapshot()["call_b"]
                    self.assertEqual(b["requests"], 1)
                    if field == "prompt_tokens":
                        self.assertIsNone(b["prompt_tokens"])
                        self.assertEqual(
                            b["prompt_chars_estimate"],
                            len("prompt") + len("chunk text"))
                        self.assertEqual(b["completion_tokens"], 30)
                    else:
                        self.assertEqual(b["prompt_tokens"], 120)
                        self.assertIsNone(b["prompt_chars_estimate"])
                        self.assertIsNone(b["completion_tokens"])

    def test_run_task_result_survives_nonfinite_usage(self):
        # End to end: the backend call succeeds and its content
        # parses, so _run_task must return that content intact
        # even though the stashed usage is non-finite — the
        # recorder's finally must never mask a good result.
        def fake(prompt, chunk, i, n):
            pp._DIAG.telemetry_usage = {
                "prompt_tokens": float("nan"),
                "completion_tokens": float("inf")}
            return '{"ok": true}'

        result = _run_task_json(fake, "prompt", "chunk",
                                "v2-ch1-content")
        self.assertEqual(result, {"ok": True})

    def test_stale_usage_from_a_prior_call_never_leaks(self):
        # A backend that reported usage once must not have that
        # usage stamped onto the NEXT call, whose endpoint
        # reported nothing.
        state = {"first": True}

        def fake(prompt, chunk, i, n):
            if state["first"]:
                state["first"] = False
                pp._DIAG.telemetry_usage = {
                    "prompt_tokens": 50, "completion_tokens": 5}
            return '{"ok": true}'

        _run_task_json(fake, "p", "c", "v2-ch1-content")
        _run_task_json(fake, "prompt!", "chunk!", "v2-ch1-content")
        b = pp._telemetry_snapshot()["call_b"]
        self.assertEqual(b["requests"], 2)
        self.assertEqual(b["prompt_tokens"], 50)
        self.assertEqual(b["completion_tokens"], 5)
        self.assertEqual(b["prompt_chars_estimate"],
                         len("prompt!") + len("chunk!"))


class TestBackendUsageStash(unittest.TestCase):
    """Each LLM backend stashes its response's server-reported
    usage for the recorder; an endpoint that reports nothing
    stashes nothing measurable."""

    def setUp(self):
        pp._DIAG.telemetry_usage = None

    def tearDown(self):
        pp._DIAG.telemetry_usage = None

    def test_openai_compat_stashes_usage(self):
        resp = {"choices": [{"message": {"content": "{}"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 111,
                          "completion_tokens": 22}}
        with mock.patch.dict(pp.CONFIG, {
                "openai_base_url": "http://127.0.0.1:9999/v1",
                "openai_model": "x", "llm_max_tokens": 8000,
                "llm_response_format": "none"}), \
                mock.patch.object(pp, "_post_json",
                                  return_value=resp):
            pp.llm_openai_compat("PROMPT", "chunk", 1, 2)
        self.assertEqual(pp._DIAG.telemetry_usage,
                         {"prompt_tokens": 111,
                          "completion_tokens": 22})

    def test_openai_compat_stashes_timings_prompt_n(self):
        # llama.cpp-style servers report prompt tokens under
        # timings.prompt_n when usage carries no prompt_tokens.
        resp = {"choices": [{"message": {"content": "{}"},
                             "finish_reason": "stop"}],
                "usage": {"completion_tokens": 7},
                "timings": {"prompt_n": 55, "cache_n": 3}}
        with mock.patch.dict(pp.CONFIG, {
                "openai_base_url": "http://127.0.0.1:9999/v1",
                "openai_model": "x", "llm_max_tokens": 8000,
                "llm_response_format": "none"}), \
                mock.patch.object(pp, "_post_json",
                                  return_value=resp):
            pp.llm_openai_compat("PROMPT", "chunk", 1, 2)
        self.assertEqual(pp._DIAG.telemetry_usage,
                         {"prompt_tokens": 55,
                          "completion_tokens": 7})

    def test_openai_compat_no_usage_stashes_nulls(self):
        resp = {"choices": [{"message": {"content": "{}"},
                             "finish_reason": "stop"}]}
        with mock.patch.dict(pp.CONFIG, {
                "openai_base_url": "http://127.0.0.1:9999/v1",
                "openai_model": "x", "llm_max_tokens": 8000,
                "llm_response_format": "none"}), \
                mock.patch.object(pp, "_post_json",
                                  return_value=resp):
            pp.llm_openai_compat("PROMPT", "chunk", 1, 2)
        usage = pp._DIAG.telemetry_usage
        self.assertIsNotNone(usage)
        self.assertIsNone(usage["prompt_tokens"])
        self.assertIsNone(usage["completion_tokens"])

    def test_openrouter_stashes_usage(self):
        resp = {"choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 90,
                          "completion_tokens": 12}}
        with mock.patch.dict(pp.CONFIG, {
                "openrouter_model": "m", "openrouter_key": "k"}), \
                mock.patch.object(pp, "_post_json",
                                  return_value=resp):
            pp.llm_openrouter("PROMPT", "chunk", 1, 2)
        self.assertEqual(pp._DIAG.telemetry_usage,
                         {"prompt_tokens": 90,
                          "completion_tokens": 12})

    def test_openrouter_non_dict_usage_returns_content(self):
        # A truthy non-dict usage block (a string, from a
        # malformed or merely eccentric endpoint) must not cost
        # the response: the stash degrades to null token fields
        # and the content comes back exactly as the pre-telemetry
        # code returned it. Before the isinstance guard, .get on
        # the string raised AttributeError into the function's
        # broad except and the valid response was discarded.
        resp = {"choices": [{"message": {"content": "{}"}}],
                "usage": "not-a-dict"}
        with mock.patch.dict(pp.CONFIG, {
                "openrouter_model": "m", "openrouter_key": "k"}), \
                mock.patch.object(pp, "_post_json",
                                  return_value=resp):
            out = pp.llm_openrouter("PROMPT", "chunk", 1, 2)
        self.assertEqual(out, "{}")
        self.assertEqual(pp._DIAG.telemetry_usage,
                         {"prompt_tokens": None,
                          "completion_tokens": None})

    def test_ollama_stashes_eval_counts(self):
        resp = {"message": {"content": "{}"},
                "prompt_eval_count": 77, "eval_count": 9}
        with mock.patch.dict(pp.CONFIG, {
                "ollama_host": "http://127.0.0.1:9999",
                "ollama_model": "m"}), \
                mock.patch.object(pp, "_post_json",
                                  return_value=resp):
            pp.llm_ollama("PROMPT", "chunk", 1, 2)
        self.assertEqual(pp._DIAG.telemetry_usage,
                         {"prompt_tokens": 77,
                          "completion_tokens": 9})


class TestDecisionSplit(unittest.TestCase):
    """The decision-stage split is DERIVED from the existing
    per-consumer budget accounting: it sums to the run's
    decision-call total by construction, never by recounting."""

    def setUp(self):
        pp._dm_budget_reset()

    def tearDown(self):
        pp._dm_budget_reset()

    def test_split_sums_to_existing_decision_total(self):
        pp._dm_budget_claim(1, consumer="merge_tier3")
        pp._dm_budget_claim(1, consumer="merge_tier3")
        pp._dm_budget_claim(1, consumer="rel_family")
        pp._dm_budget_claim(1, consumer="rel_nonfamily")
        pp._dm_budget_claim(1, consumer="rel_nonfamily")
        for _ in range(3):
            pp._dm_budget_claim(1, consumer="trigger_gate")
        pp._dm_budget_claim(1, consumer="quote_poll")
        dec = pp._telemetry_snapshot()["decision"]
        self.assertEqual(dec["merge"]["calls"], 2)
        # relationship = family + non-family consumers, one stage.
        self.assertEqual(dec["relationship"]["calls"], 3)
        self.assertEqual(dec["trigger"]["calls"], 3)
        self.assertEqual(dec["quote"]["calls"], 1)
        total = sum(s["calls"] for s in dec.values())
        self.assertEqual(total, pp._DM_BUDGET_USED)
        self.assertEqual(total, 9)

    def test_unspent_stages_report_zero_calls(self):
        dec = pp._telemetry_snapshot()["decision"]
        for stage in ("merge", "relationship", "trigger", "quote"):
            self.assertEqual(dec[stage]["calls"], 0)
            self.assertEqual(dec[stage]["elapsed_sec"], 0)

    def test_decision_elapsed_accumulates_per_stage(self):
        pp._telemetry_decision_elapsed("merge", 0.25)
        pp._telemetry_decision_elapsed("merge", 0.25)
        pp._telemetry_decision_elapsed("quote", 1.5)
        dec = pp._telemetry_snapshot()["decision"]
        self.assertAlmostEqual(dec["merge"]["elapsed_sec"], 0.5)
        self.assertAlmostEqual(dec["quote"]["elapsed_sec"], 1.5)
        self.assertEqual(dec["trigger"]["elapsed_sec"], 0)

    def test_merge_stage_elapsed_and_count_from_a_real_judgment(self):
        # Drive the real Tier 3 site: an ambiguous pair with a
        # name signal reaches the (stub) validator exactly once.
        from collections import Counter

        def entry(name, chapters):
            nk = pp.norm_name(name)
            return {nk: {
                "name": name, "aliases": set(), "alias_keys": {nk},
                "primary_keys": {nk}, "appearances": 2,
                "chapters": set(chapters), "roles": Counter(),
                "descriptions": [(min(chapters), "d", False)],
                "evidence": ""}}

        roster = {}
        roster.update(entry("Robert Smith", [1]))
        roster.update(entry("Robert Jones", [2]))

        class StubValidator:
            disabled = False
            model = "stub"

            def ask_same_person(self, *a):
                time.sleep(0.02)
                return False, 0.1

        pp._dm_budget_reset()
        pp.post_pass_merge(roster, validator=StubValidator())
        dec = pp._telemetry_snapshot()["decision"]
        self.assertEqual(dec["merge"]["calls"], 1)
        self.assertGreater(dec["merge"]["elapsed_sec"], 0)
        self.assertEqual(pp._DM_BUDGET_USED, 1)


# --- Two-book v2 run (frontmatter-test EPUB pattern) --------------

_STORY = "Mira Solace drew her sword and faced the dawn."


def _xhtml(body_inner):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            "<title>ch</title></head><body>"
            + body_inner + "</body></html>")


def _make_epub(path, files):
    items = "".join(
        f'<item id="f{i}" href="{name.split("/", 1)[1]}" '
        f'media-type="application/xhtml+xml"/>'
        for i, name in enumerate(files))
    refs = "".join(f'<itemref idref="f{i}"/>'
                   for i in range(len(files)))
    opf = ("""<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='bid'>
  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
    <dc:title>Telemetry Test Book</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:identifier>9780306406157</dc:identifier>
  </metadata>
  <manifest>""" + items + """
  </manifest>
  <spine>""" + refs + """
  </spine>
</package>""")
    container = ("""<?xml version='1.0' encoding='utf-8'?>
<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' version='1.0'>
  <rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles>
</container>""")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, content in files.items():
            z.writestr(name, content)


class _FakeBackend:
    """Call A / Call B answers in the frontmatter tests' style.
    Reports no usage, so books record prompt_chars_estimate."""

    def __call__(self, prompt, chunk, i, n):
        if prompt == pp.PROMPT_V2_CONTENT:
            return json.dumps({
                "summary": "A chapter summary.",
                "spice_level": 0, "triggers": {},
                "trigger_evidence": {},
                "trope_candidates": ["Found Family"], "quotes": []})
        if "confirming trope candidates" in prompt:
            return json.dumps({"verdicts": [{
                "trope": "Found Family", "verdict": "yes",
                "justification": "Ch 1"}]})
        return json.dumps({
            "characters": [{
                "name": "Mira Solace", "role": "protagonist",
                "description": "A swordswoman of the valley.",
                "evidence": _STORY}],
            "relationships": [], "pov_character": "Mira Solace"})


class TestTwoBookRun(unittest.TestCase):
    """Book 2's counters start at zero and reflect only book 2's
    work — in the preview JSON and in the book-end payload."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        story = f"<p>{_STORY}</p>" + (
            "<p>The valley was quiet and the river ran cold and "
            "bright under a wide northern sky full of slow "
            "clouds.</p>" * 22)
        self.books = []
        for stem, n_ch in (("bookone", 3), ("booktwo", 2)):
            epub = root / f"{stem}.epub"
            files = {f"OEBPS/ch{k:02d}.xhtml": _xhtml(
                f"<h1>Chapter {k}</h1>" + story)
                for k in range(1, n_ch + 1)}
            _make_epub(str(epub), files)
            self.books.append(epub)
            for ext in ("json", "md"):
                self.addCleanup(lambda s=stem, e=ext: (
                    pp.SCRIPT_DIR / "preview" / f"{s}.{e}"
                ).unlink(missing_ok=True))
            self.addCleanup(lambda s=stem: shutil.rmtree(
                pp._checkpoint_dir(pp._checkpoint_book_key(
                    root / f"{s}.epub")), ignore_errors=True))
        self.learn_dir = root / "learned"

    def _run_books(self, jobs=1):
        backend = _FakeBackend()
        with mock.patch.object(pp, "llm_ollama", backend), \
                mock.patch.object(
                    pp, "_lookup_series_openlibrary",
                    lambda *a, **k: (None, None)), \
                mock.patch.object(
                    pp, "resolve_work",
                    lambda *a, **k: (None, "test")), \
                mock.patch.dict(pp.CONFIG, {
                    "llm": "ollama", "pipeline": "v2", "jobs": jobs}):
            path = pp._run_events_begin(self.learn_dir,
                                         preview=True)
            self.assertIsNotNone(path)
            try:
                for epub in self.books:
                    with pp._run_events_book(epub):
                        ok = pp.process_file_v2(epub, preview=True)
                    self.assertTrue(ok)
            finally:
                pp._run_events_finish(failures=0)
        return path

    def _preview(self, stem):
        return json.loads((pp.SCRIPT_DIR / "preview"
                           / f"{stem}.json").read_text(
                               encoding="utf-8"))

    def test_preview_telemetry_is_per_book(self):
        self._run_books()
        t1 = self._preview("bookone")["telemetry"]
        t2 = self._preview("booktwo")["telemetry"]
        # Book 1: 3 chapters -> 3 Call A + 3 Call B requests,
        # all nonzero.
        self.assertEqual(t1["call_a"]["requests"], 3)
        self.assertEqual(t1["call_b"]["requests"], 3)
        self.assertGreater(t1["call_a"]["prompt_chars_estimate"], 0)
        self.assertGreater(t1["call_b"]["prompt_chars_estimate"], 0)
        # Book 2: 2 chapters. Its counters started at zero — had
        # they been borrowed from book 1 they would read 5/5 (or
        # carry book 1's character totals).
        self.assertEqual(t2["call_a"]["requests"], 2)
        self.assertEqual(t2["call_b"]["requests"], 2)
        self.assertLess(t2["call_a"]["prompt_chars_estimate"],
                        t1["call_a"]["prompt_chars_estimate"])
        # The fake endpoint reports no usage: tokens stay null in
        # both books, never zeros dressed as measurements.
        for t in (t1, t2):
            self.assertIsNone(t["call_a"]["prompt_tokens"])
            self.assertIsNone(t["call_b"]["prompt_tokens"])
            self.assertIsNone(t["call_a"]["completion_tokens"])
            # No decision validator in this run: a measured zero
            # split that sums to the run's decision total (0).
            dec = t["decision"]
            self.assertEqual(
                sum(s["calls"] for s in dec.values()), 0)
        # Routing is NOT built in Phase 0: no routing marker
        # anywhere on the preview's face.
        for stem in ("bookone", "booktwo"):
            self.assertNotIn("corpus_routing", self._preview(stem))

    def test_preview_telemetry_is_null_under_parallel_jobs(self):
        # The counters are process-global, so under --jobs > 1
        # the interleave the book-end and history stamps already
        # guard against applies to the preview stamp too: book 1
        # arms and records, book 2's arm clears those totals and
        # book 2's own records land in the same counters, and only
        # then does book 1 stamp its preview — the snapshot would
        # carry another book's work under book 1's name. Driven
        # sequentially here with jobs=2 set, the guard condition
        # itself is what is pinned: the stamp must be null rather
        # than a racy snapshot, while the jobs<=1 tests above pin
        # that sequential behaviour is untouched.
        self._run_books(jobs=2)
        for stem in ("bookone", "booktwo"):
            self.assertIsNone(self._preview(stem)["telemetry"])

    def test_book_end_payload_telemetry_is_per_book(self):
        path = self._run_books()
        evs = [json.loads(line) for line in
               Path(path).read_text(encoding="utf-8").splitlines()
               if line.strip()]
        ends = [e for e in evs if e["event"] == "book_end"]
        self.assertEqual(len(ends), 2)
        self.assertEqual(ends[0]["book"], "bookone.epub")
        self.assertEqual(ends[0]["telemetry"]["call_a"]["requests"],
                         3)
        self.assertEqual(ends[0]["telemetry"]["call_b"]["requests"],
                         3)
        self.assertEqual(ends[1]["book"], "booktwo.epub")
        self.assertEqual(ends[1]["telemetry"]["call_a"]["requests"],
                         2)
        self.assertEqual(ends[1]["telemetry"]["call_b"]["requests"],
                         2)
        # The additive field reshapes nothing the backfill writer
        # reads: the existing book-end fields are untouched.
        for e in ends:
            self.assertIn("decision_calls", e)
            self.assertIn("decision_budget", e)

    def test_unarmed_book_end_stamps_no_telemetry(self):
        # A book that exits before the pipeline arms it must stamp
        # None — never a predecessor's numbers (the events-side
        # attribution guard, issues #13/#14, applied to telemetry).
        pp._dm_budget_reset()
        pp._dm_budget_claim(2)
        with mock.patch.dict(pp.CONFIG, {"pipeline": "v2",
                                         "jobs": 1}):
            path = pp._run_events_begin(self.learn_dir,
                                        preview=True)
            self.assertIsNotNone(path)
            try:
                with pp._run_events_book(Path("Unarmed.epub")):
                    pass
            finally:
                pp._run_events_finish(failures=0)
        evs = [json.loads(line) for line in
               Path(path).read_text(encoding="utf-8").splitlines()
               if line.strip()]
        end = [e for e in evs if e["event"] == "book_end"][0]
        self.assertIsNone(end["telemetry"])
        self.assertIsNone(end["decision_calls"])


if __name__ == "__main__":
    unittest.main()
