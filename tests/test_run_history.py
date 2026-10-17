"""Run-history writer tests (docs/run-history-spec.md).

The transport seam is process-portable's `sb` helper, replaced with
a recording fake (the pattern tests/test_replace_wholesale.py uses):
every assertion about ordering, id plumbing and payload shape is
made against the calls the writer WOULD have shipped. One transport
class at the bottom exercises the real `sb` against a faked
urlopen to pin the schema-profile headers.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import unittest

_PP_PATH = Path(__file__).resolve().parent.parent / "process-portable.py"
_spec = importlib.util.spec_from_file_location("pp_run_history",
                                               _PP_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_corpus_ingest import _make_epub, _xhtml  # noqa: E402

RUN_ID = "aaaaaaaa-1111-2222-3333-444444444444"
BOOK_ID = "bbbbbbbb-1111-2222-3333-444444444444"
WORK_ID = "11111111-2222-3333-4444-555555555555"
BOOK_BYTES = b"fake epub bytes for history tests"


class FakeSB:
    """Recording stand-in for pp.sb. Default behaviour mirrors a
    healthy empty mirror: no row under any run_key, run/book POSTs
    echo back ids. Tests override handlers per case."""

    def __init__(self):
        self.calls = []
        self.get_runs = []            # rows for the collision GET
        self.get_runs_queue = None    # per-call GET responses
        self.fail_run_post = False

    def __call__(self, table, method="GET", data=None, params="",
                 prefer=None, schema=None):
        self.calls.append({"table": table, "method": method,
                           "data": data, "params": params,
                           "prefer": prefer, "schema": schema})
        if table == "processor_runs" and method == "GET":
            if self.get_runs_queue is not None:
                return self.get_runs_queue.pop(0)
            return list(self.get_runs)
        if table == "processor_runs" and method == "POST":
            if self.fail_run_post:
                return None
            return [{"id": RUN_ID}]
        if table == "processor_run_books" and method == "POST":
            return [{"id": BOOK_ID, "book_key": row["book_key"]}
                    for row in data]
        if table == "processor_run_claim_outcomes" and method == "POST":
            return [{} for _ in data]
        return []

    def posts(self, table):
        return [c for c in self.calls
                if c["table"] == table and c["method"] == "POST"]

    def sequence(self):
        return [(c["method"], c["table"]) for c in self.calls]


def v2_result():
    """A v2-shaped result record, as process_file_v2 assembles it:
    two tropes (one catalogue-mapped, one not), one retrieval grade,
    one trigger, corpus quote counts, a verdict suppression."""
    return {
        "title": "Test Title", "pipeline": "v2", "chunks": 12,
        "work_id": WORK_ID,
        "tropes": ["Enemies To Lovers", "Found Family"],
        "trope_confidence": {"enemies to lovers": 0.8,
                             "found family": 0.7},
        "trope_catalog_map": {"enemies to lovers": "trope-123"},
        "trope_retrieval": {
            "enemies to lovers": {"outcome": "supported-single"}},
        "trigger_retrieval": {},
        "evidence_retrieval": {"attempted": 1},
        "trope_persistence": {"found family": {"stamp": 1}},
        "triggers": [{"warning": "Violence", "confidence": 0.9}],
        "characters": [{}, {}], "minor_characters": [{}],
        "relationships": [{}], "dropped_relationships": [],
        "roster_size": 5, "possible_merges": [{}],
        # post_pass_merge returns a count: real v2 results carry
        # the int, not a list of merges.
        "post_pass_merged": 1,
        "quotes": [{}, {}],
        "corpus_quotes": {"verified": 3, "rejected": 1,
                          "selected": 2},
        "suppressed_rejected": ["Some Rejected"],
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._config = dict(pp.CONFIG)
        self._sb = pp.sb
        self._dm = pp._DM_BUDGET_USED
        self.fake = FakeSB()
        pp.sb = self.fake
        pp.CONFIG.update({
            "run_history": True,
            "supabase_url": "https://example.invalid",
            "supabase_key": "test-key",
            "machine_label": "Lazydog",
            "pipeline": "v2", "llm": "ollama",
            "ollama_model": "qwen-test", "jobs": 1,
            "corpus_only": False, "use_corpus": False,
            "evidence_retrieval": False,
        })
        pp._DM_BUDGET_USED = 7
        self.tmp = Path(self.id().replace(".", "_"))
        import tempfile
        self._tmpd = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpd.name)
        self.book = self.tmp / "book.epub"
        self.book.write_bytes(BOOK_BYTES)
        self.book_key = hashlib.sha256(BOOK_BYTES).hexdigest()

    def tearDown(self):
        pp.sb = self._sb
        pp.CONFIG.clear()
        pp.CONFIG.update(self._config)
        pp._DM_BUDGET_USED = self._dm
        pp._HISTORY = None
        pp._HISTORY_WATCH = None
        pp._HISTORY_INGEST_CAPTURE = False
        pp._HISTORY_NOSB_WARNED = False
        self._tmpd.cleanup()

    def drive_one_book(self, result=None, ok=True):
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        if result is not None:
            pp._history_note_result(self.book, result)
        pp._history_book_outcome(self.book, ok)
        pp._history_finish(failures=0 if ok else 1)

    def run_payload(self):
        posts = self.fake.posts("processor_runs")
        self.assertEqual(len(posts), 1)
        return posts[0]["data"][0]

    def book_payload(self):
        posts = self.fake.posts("processor_run_books")
        self.assertEqual(len(posts), 1)
        return posts[0]["data"][0]


class FlagAndFailureTests(_Base):
    def test_flag_off_makes_zero_calls(self):
        pp.CONFIG["run_history"] = False
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        pp._history_note_result(self.book, v2_result())
        pp._history_book_outcome(self.book, True)
        pp._history_finish(failures=0)
        self.assertEqual(self.fake.calls, [])

    def test_missing_supabase_config_warns_and_stays_silent(self):
        pp.CONFIG["supabase_url"] = ""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pp._history_begin("scan", learn_dir=str(self.tmp))
            pp._history_finish()
        self.assertIn("unmirrored", out.getvalue())
        self.assertEqual(self.fake.calls, [])
        self.assertIsNone(pp._HISTORY)

    def test_run_post_failure_warns_once_and_run_unaffected(self):
        self.fake.fail_run_post = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            # finish must not raise: the run's own result is
            # untouched by a dead mirror.
            self.drive_one_book(result=v2_result())
        text = out.getvalue()
        self.assertEqual(text.count("Run history: upload failed"), 1)
        self.assertIn("run unaffected", text)
        self.assertEqual(self.fake.posts("processor_run_books"), [])

    def test_interrupted_status(self):
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=0)
        pp._history_finish(interrupted=True)
        payload = self.run_payload()
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["mode"], "scan")


class PayloadTests(_Base):
    def test_ordering_schema_and_id_plumbing(self):
        self.drive_one_book(result=v2_result())
        self.assertEqual(
            self.fake.sequence(),
            [("GET", "processor_runs"), ("POST", "processor_runs"),
             ("POST", "processor_run_books"),
             ("POST", "processor_run_claim_outcomes")])
        for call in self.fake.calls:
            self.assertEqual(call["schema"], "processor")
        run_post = self.fake.posts("processor_runs")[0]
        self.assertEqual(run_post["params"], "?on_conflict=run_key")
        self.assertEqual(run_post["prefer"],
                         "resolution=merge-duplicates")
        book_post = self.fake.posts("processor_run_books")[0]
        self.assertEqual(book_post["params"],
                         "?on_conflict=run_id,book_key")
        book = book_post["data"][0]
        self.assertEqual(book["run_id"], RUN_ID)
        claims = self.fake.posts("processor_run_claim_outcomes")[0]
        self.assertEqual(claims["params"],
                         "?on_conflict=run_book_id,axis,claim_key")
        for row in claims["data"]:
            self.assertEqual(row["run_book_id"], BOOK_ID)

    def test_run_and_book_payload_fields(self):
        self.drive_one_book(result=v2_result())
        run = self.run_payload()
        self.assertEqual(run["machine_label"], "Lazydog")
        self.assertEqual(run["processor_version"], pp.PORTABLE_VERSION)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["models"]["llm_model"], "qwen-test")
        self.assertIn("extractor_content", run["prompt_variants"])
        self.assertEqual(
            len(run["prompt_variants"]["extractor_content"]["hash"]),
            12)
        totals = run["totals"]
        self.assertEqual(totals["files_seen"], 1)
        self.assertEqual(totals["processed"], 1)
        self.assertEqual(totals["succeeded"], 1)
        self.assertEqual(totals["claims_derived"], 2)
        self.assertEqual(totals["claims_supported_single"], 1)
        self.assertIsNone(totals["claims_reused"])
        self.assertIsNone(totals["skipped_not_in_corpus"])
        self.assertEqual(totals["quotes_verified"], 3)

        book = self.book_payload()
        self.assertEqual(book["book_key"], self.book_key)
        self.assertEqual(book["source"], "file")
        self.assertEqual(book["title"], "Test Title")
        self.assertEqual(book["work_id"], WORK_ID)
        self.assertEqual(book["chapters"], 12)
        self.assertEqual(book["decision_calls"], 7)
        self.assertEqual(book["suppressed_by_verdict"], 1)
        self.assertIsNone(book["replace_result"])
        tropes = book["claims"]["tropes"]
        self.assertEqual(tropes["derived"], 1)
        self.assertEqual(tropes["supported_single"], 1)
        self.assertEqual(tropes["promoted"], 1)
        self.assertIsNone(tropes["reused"])
        self.assertIsNone(tropes["tier1"])
        chars = book["characters"]
        self.assertEqual(chars["roster_size"], 5)
        self.assertEqual(chars["characters"], 2)
        self.assertEqual(chars["merges_applied"], 1)
        quotes = book["quotes"]
        self.assertEqual(quotes, {"extracted": 2, "verified": 3,
                                  "rejected": 1, "selected": 2})

    def test_claim_rows_verdicts_and_keys(self):
        self.drive_one_book(result=v2_result())
        rows = self.fake.posts("processor_run_claim_outcomes")[0]["data"]
        self.assertEqual(len(rows), 3)
        by_verdict = {}
        for r in rows:
            by_verdict.setdefault(r["verdict"], []).append(r)
            self.assertIn(r["verdict"], pp._HISTORY_CLAIM_VERDICTS)
        self.assertEqual(len(by_verdict["supported-single"]), 1)
        self.assertEqual(len(by_verdict["derived"]), 2)
        mapped = hashlib.sha256(
            f"tropes|{self.book_key}|trope-123".encode()).hexdigest()
        pending = hashlib.sha256(
            f"tropes|{self.book_key}|catalogue-pending"
            .encode()).hexdigest()
        keys = {r["claim_key"] for r in rows}
        self.assertIn(mapped, keys)
        self.assertIn(pending, keys)

    def test_explicit_null_inner_keys_for_failed_book(self):
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        pp._history_book_outcome(self.book, False)
        pp._history_finish(failures=1)
        book = self.book_payload()
        self.assertEqual(book["outcome"], "failed")
        for axis in ("tropes", "triggers"):
            self.assertEqual(set(book["claims"][axis]),
                             set(pp._HISTORY_AXIS_KEYS))
            self.assertTrue(all(
                v is None for v in book["claims"][axis].values()))
        self.assertEqual(set(book["characters"]),
                         set(pp._HISTORY_CHARACTER_KEYS))
        self.assertEqual(set(book["quotes"]),
                         set(pp._HISTORY_QUOTE_KEYS))
        totals = self.run_payload()["totals"]
        self.assertEqual(set(totals), set(pp._HISTORY_TOTAL_KEYS))
        self.assertEqual(totals["failed"], 1)
        self.assertIsNone(totals["claims_derived"])

    def test_decision_calls_null_when_unattributable(self):
        # Parallel mode shares the counter across books, and the
        # legacy pipeline never resets it per book: both must read
        # NULL rather than an attribution the run cannot support.
        pp.CONFIG["jobs"] = 2
        self.drive_one_book(result=v2_result())
        self.assertIsNone(self.book_payload()["decision_calls"])
        self.fake.calls.clear()
        pp.CONFIG["jobs"] = 1
        pp.CONFIG["pipeline"] = "legacy"
        self.drive_one_book(result=v2_result())
        self.assertIsNone(self.book_payload()["decision_calls"])


class CollisionTests(_Base):
    def test_collision_suffixes_and_warns(self):
        foreign = [{"id": "zzzz", "started_at":
                    "1999-01-01T00:00:00+00:00",
                    "machine_label": "OtherMachine"}]
        self.fake.get_runs_queue = [foreign, []]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.drive_one_book(result=v2_result())
        run = self.run_payload()
        self.assertTrue(run["run_key"].endswith("-2"),
                        run["run_key"])
        self.assertIn("already belongs to another run",
                      out.getvalue())

    def test_own_row_resend_keeps_key(self):
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        rec = pp._HISTORY
        import time as _time
        stamp = _time.strftime("%Y-%m-%dT%H:%M:%S",
                               _time.gmtime(rec.started_wall))
        self.fake.get_runs = [{
            "id": RUN_ID, "started_at": stamp + "+00:00",
            "machine_label": "Lazydog"}]
        pp._history_book_start(self.book)
        pp._history_note_result(self.book, v2_result())
        pp._history_book_outcome(self.book, True)
        pp._history_finish(failures=0)
        run = self.run_payload()
        self.assertEqual(run["run_key"], rec.run_key)
        self.assertFalse(run["run_key"].endswith("-2"))


class DisciplineTests(_Base):
    def test_sanitizer_fixtures(self):
        cases = [
            (r"boom at C:\Users\someone\secret\file.txt",
             ["someone", "C:\\", "file.txt"]),
            ("GET https://api.example.com/v1/x?token=abc123 failed",
             ["api.example.com", "abc123"]),
            ("contact admin@example.com for help",
             ["admin@example.com"]),
            ("could not open Some Book.epub at all",
             ["Some Book.epub"]),
            ("/home/user/books/thing.epub missing",
             ["/home/user"]),
            ("Authorization: Bearer abcdefghij123456",
             ["abcdefghij123456"]),
        ]
        for raw, forbidden in cases:
            cleaned = pp._history_sanitize(raw)
            for bad in forbidden:
                self.assertNotIn(bad, cleaned, raw)
        self.assertLessEqual(
            len(pp._history_sanitize("x" * 900)), 500)

    def test_denylist_scan_over_built_payloads(self):
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        pp._HISTORY.book_end(self.book, ValueError(
            r"failed C:\Users\someone\x.epub via "
            "https://api.example.com/?k=1 for a@b.co"))
        pp._history_book_outcome(self.book, False)
        pp._history_finish(failures=1)
        for call in self.fake.calls:
            if call["data"]:
                self.assertEqual(
                    pp._history_denylist_scan(call["data"]), [],
                    call["table"])
        book = self.book_payload()
        self.assertIn("[path]", book["error_message"])
        self.assertEqual(book["error_class"], "ValueError")

    def test_fingerprint_input_has_no_secret_named_fields(self):
        setup = pp._history_setup_stamps("scan")
        bad = re.compile(r"key|token|secret|url|endpoint", re.I)

        def walk(node, path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    self.assertIsNone(bad.search(str(k)),
                                      f"{path}/{k}")
                    walk(v, f"{path}/{k}")
        walk(setup)
        canon = json.dumps(setup, sort_keys=True,
                           separators=(",", ":"), default=str)
        expected = hashlib.sha256(canon.encode()).hexdigest()
        pp._history_begin("scan", learn_dir=str(self.tmp))
        self.assertEqual(pp._HISTORY.config_fingerprint, expected)
        pp._history_finish()

    def test_claim_key_never_uses_free_text_label(self):
        result = v2_result()
        result["tropes"] = ["Alpha Name", "Beta Name"]
        result["trope_catalog_map"] = {}
        result["trope_retrieval"] = {}
        result["trope_confidence"] = {"alpha name": 0.5,
                                      "beta name": 0.6}
        claims, rows = pp._history_book_claims(
            {**result, "_history_book_key": self.book_key})
        trope_rows = [r for r in rows if r["axis"] == "tropes"]
        self.assertEqual(len(trope_rows), 2)
        expected = hashlib.sha256(
            f"tropes|{self.book_key}|catalogue-pending"
            .encode()).hexdigest()
        for r in trope_rows:
            self.assertEqual(r["claim_key"], expected)
            self.assertNotIn("alpha", r["claim_key"])

    def test_verdict_allowlist_blocks_offlist_outcomes(self):
        result = v2_result()
        result["tropes"] = []
        result["trope_retrieval"] = {
            "x": {"outcome": "bogus-value"},
            "y": {"outcome": "supported-single"}}
        claims, rows = pp._history_book_claims(
            {**result, "_history_book_key": self.book_key})
        trope_rows = [r for r in rows if r["axis"] == "tropes"]
        self.assertEqual(len(trope_rows), 1)
        self.assertEqual(trope_rows[0]["verdict"], "supported-single")
        for r in rows:
            self.assertIn(r["verdict"], pp._HISTORY_CLAIM_VERDICTS)


class RunShapeTests(_Base):
    def _prep_folder_run(self):
        folder = self.tmp / "import"
        folder.mkdir()
        (folder / "one.epub").write_bytes(BOOK_BYTES)
        self._import_dir = pp.IMPORT_DIR
        pp.IMPORT_DIR = folder
        self._inner = pp._run_once_inner
        pp._run_once_inner = lambda d, p, files: 0

    def tearDown(self):
        if hasattr(self, "_import_dir"):
            pp.IMPORT_DIR = self._import_dir
            pp._run_once_inner = self._inner
        super().tearDown()

    def test_scan_via_run_once(self):
        self._prep_folder_run()
        rc = pp.run_once(False, True)
        self.assertEqual(rc, 0)
        run = self.run_payload()
        self.assertEqual(run["mode"], "scan")
        self.assertEqual(run["totals"]["files_seen"], 1)
        self.assertEqual(run["totals"]["admitted"], 1)

    def test_watch_cycle_gets_its_own_row(self):
        self._prep_folder_run()
        args = SimpleNamespace(watch_ingest=False, dry_run=False,
                               preview=True, max_books=None,
                               file=None, learn_dir=None)
        pp._watch_cycle(args)
        run = self.run_payload()
        self.assertEqual(run["mode"], "watch_cycle")
        self.assertTrue(run["flags"]["watch"])

    def test_main_threads_the_flag(self):
        args = pp._build_parser().parse_args([])
        self.assertFalse(args.run_history)
        args = pp._build_parser().parse_args(["--run-history"])
        self.assertTrue(args.run_history)
        with mock.patch.object(pp, "run_once", return_value=0):
            with mock.patch.object(
                    sys, "argv",
                    ["proc", "--preview", "--run-history"]):
                with self.assertRaises(SystemExit) as cm:
                    pp.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(pp.CONFIG["run_history"])


class TransportTests(unittest.TestCase):
    """The real sb() with a faked urlopen: schema targeting adds
    exactly the profile headers; default calls stay byte-identical
    in shape (no profile headers at all)."""

    def setUp(self):
        self._config = dict(pp.CONFIG)
        pp.CONFIG.update({"supabase_url": "https://example.invalid",
                          "supabase_key": "test-key"})
        self.requests = []

        def fake_urlopen(req, timeout=None):
            self.requests.append(req)

            class _Resp:
                status = 200

                def read(self):
                    return b"[]"

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return _Resp()

        self._patch = mock.patch.object(urllib.request, "urlopen",
                                        fake_urlopen)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        pp.CONFIG.clear()
        pp.CONFIG.update(self._config)

    def test_schema_headers_on_reads_and_writes(self):
        pp.sb("processor_runs", method="GET", params="?select=id",
              schema="processor")
        req = self.requests[-1]
        self.assertEqual(req.get_header("Accept-profile"),
                         "processor")
        self.assertIsNone(req.get_header("Content-profile"))
        pp.sb("processor_runs", method="POST", data=[{"a": 1}],
              params="?on_conflict=run_key",
              prefer="resolution=merge-duplicates", schema="processor")
        req = self.requests[-1]
        self.assertEqual(req.get_header("Content-profile"),
                         "processor")
        self.assertIn("return=representation",
                      req.get_header("Prefer"))
        self.assertIn("resolution=merge-duplicates",
                      req.get_header("Prefer"))

    def test_default_callers_get_no_profile_headers(self):
        pp.sb("book_meta", method="GET", params="?select=id")
        req = self.requests[-1]
        self.assertIsNone(req.get_header("Accept-profile"))
        self.assertIsNone(req.get_header("Content-profile"))
        self.assertIn("/rest/v1/book_meta", req.full_url)


class CharacterMergeShapeTests(unittest.TestCase):
    """merges_applied counts each merge source in the shape it
    actually arrives in: post_pass_merged is an int count in real
    v2 results (post_pass_merge returns a number), dedup_merges a
    list of records. A list-shaped post_pass fixture once hid that
    every real run would have shipped merges_applied 0."""

    def test_post_pass_int_is_the_only_merge_source(self):
        chars = pp._history_characters(
            {"post_pass_merged": 1, "dedup_merges": []})
        self.assertEqual(chars["merges_applied"], 1)

    def test_post_pass_int_plus_dedup_records(self):
        chars = pp._history_characters(
            {"post_pass_merged": 2, "dedup_merges": [{}, {}]})
        self.assertEqual(chars["merges_applied"], 4)

    def test_dedup_alone_measures_with_post_pass_absent(self):
        chars = pp._history_characters({"dedup_merges": [{}]})
        self.assertEqual(chars["merges_applied"], 1)

    def test_no_merge_data_stays_none(self):
        chars = pp._history_characters({})
        self.assertIsNone(chars["merges_applied"])
        chars = pp._history_characters({"post_pass_merged": None})
        self.assertIsNone(chars["merges_applied"])


_DRIVE_SENTENCE = "Mira Solace drew her sword and faced the dawn."


class _DriveBackend:
    """Fake LLM backend for a full process_file_v2 drive (the
    test_frontmatter pattern): content calls get a canned chapter
    payload; every other call gets one character whose evidence
    sentence appears in every chapter."""

    def __call__(self, prompt, chunk, i, n):
        if prompt == pp.PROMPT_V2_CONTENT:
            return json.dumps({
                "summary": "A chapter summary.", "spice_level": 0,
                "triggers": {}, "trigger_evidence": {},
                "trope_candidates": [], "quotes": []})
        return json.dumps({
            "characters": [{"name": "Mira Solace",
                            "role": "protagonist",
                            "description": "A swordswoman of the valley.",
                            "evidence": _DRIVE_SENTENCE}],
            "relationships": [], "pov_character": "Mira Solace"})


def _drive_files():
    story = f"<p>{_DRIVE_SENTENCE}</p>" + (
        "<p>The valley was quiet and the river ran cold and bright "
        "under a wide northern sky full of slow clouds.</p>" * 22)
    return {f"OEBPS/ch{k:02d}.xhtml": _xhtml(
        f"<h1>Chapter {k}</h1>" + story) for k in range(1, 4)}


class CorpusQuotesAttachTests(_Base):
    """The corpus_quotes key on the result record: the corpus
    stage's counts attach only when run history is armed, so a
    history-off preview stays byte-identical to master even when
    the corpus-quotes stage ran. Drives the real process_file_v2
    with the corpus store stubbed at its boundary functions — the
    stage itself runs and produces its stats either way."""

    def _drive(self, history_on):
        epub = _make_epub(self.tmp / "rhcq.epub", _drive_files(),
                          title="RH Corpus Quotes")
        preview_json = pp.SCRIPT_DIR / "preview" / "rhcq.json"
        self.addCleanup(preview_json.unlink, missing_ok=True)
        self.addCleanup((pp.SCRIPT_DIR / "preview" / "rhcq.md").unlink,
                        missing_ok=True)
        self.addCleanup(lambda: shutil.rmtree(
            pp._checkpoint_dir(pp._checkpoint_book_key(epub)),
            ignore_errors=True))
        out = io.StringIO()
        with mock.patch.object(pp, "llm_ollama", _DriveBackend()), \
                mock.patch.object(pp, "_lookup_series_openlibrary",
                                  lambda *a, **k: (None, None)), \
                mock.patch.object(pp, "resolve_work",
                                  lambda *a, **k: (None, "test")), \
                mock.patch.object(pp, "_corpus_db_for",
                                  lambda *a, **k: object()), \
                mock.patch.object(pp, "corpus_work_state",
                                  lambda *a, **k: "complete"), \
                mock.patch.object(pp, "corpus_verify_run_quotes",
                                  lambda *a, **k: {"verified": 2,
                                                   "rejected": 1}), \
                mock.patch.object(pp, "corpus_select_quotes",
                                  lambda *a, **k: {"poll": "ok",
                                                   "selected": 1}), \
                mock.patch.dict(pp.CONFIG,
                                {"llm": "ollama", "corpus_quotes": True,
                                 "run_history": history_on}), \
                contextlib.redirect_stdout(out):
            if history_on:
                pp._history_begin("scan", preview=True,
                                  learn_dir=str(self.tmp), files_seen=1)
                pp._history_book_start(epub)
            ok = pp.process_file_v2(epub, preview=True)
            if history_on:
                pp._history_book_outcome(epub, ok)
                pp._history_finish(failures=0 if ok else 1)
        self.assertTrue(ok)
        # The corpus-quotes stage really ran in both cases — the
        # flag-off assertion below is about the result record, not
        # about the stage being skipped.
        self.assertIn("Corpus quotes: 2 verified, 1 rejected",
                      out.getvalue())
        result = json.loads(preview_json.read_text(encoding="utf-8"))
        return result

    def test_flag_off_preview_has_no_corpus_quotes_key(self):
        result = self._drive(history_on=False)
        self.assertNotIn("corpus_quotes", result)
        history_calls = [c for c in self.fake.calls
                         if c["table"].startswith("processor_")]
        self.assertEqual(history_calls, [])

    def test_flag_on_key_present_and_consumed(self):
        result = self._drive(history_on=True)
        self.assertEqual(result["corpus_quotes"],
                         {"verified": 2, "rejected": 1, "selected": 1})
        quotes = self.book_payload()["quotes"]
        self.assertEqual(quotes["verified"], 2)
        self.assertEqual(quotes["rejected"], 1)
        self.assertEqual(quotes["selected"], 1)


if __name__ == "__main__":
    unittest.main()
