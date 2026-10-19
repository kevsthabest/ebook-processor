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
from urllib.parse import unquote
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
        self.fail_books_post = False  # books POST returns None
        self.omit_book_keys = set()   # books POST "loses" these rows

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
            if self.fail_books_post:
                return None
            return [{"id": BOOK_ID, "book_key": row["book_key"]}
                    for row in data
                    if row["book_key"] not in self.omit_book_keys]
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
        # No budget is armed here. Since v2.13.50 the recorder
        # snapshots the arm sequence at construction, so state
        # armed before _history_begin belongs to a predecessor
        # and can never stamp — the harness arms after begin, in
        # drive_one_book, the order production runs in (begin ->
        # the pipeline arms the book -> the outcome stamps it).
        # (v2.13.49's setup armed here, before begin, which the
        # construction snapshot correctly reads as a previous
        # run's arming.)
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
        # Arm the budget, then spend seven calls on it: every
        # drive in this file models a book the pipeline armed,
        # and the arming must postdate the recorder (see setUp)
        # for the outcome below to stamp it.
        pp._dm_budget_reset()
        pp._DM_BUDGET_USED = 7
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

    def test_two_unmapped_tropes_get_distinct_claim_keys(self):
        # RH-F1 regression: Daemon run 17's upload died with
        # Postgres 21000 because two unmapped tropes hashed to the
        # same pending key inside one upsert batch. The first
        # keeps the bare pending key; the second is suffixed.
        result = v2_result()
        result["tropes"] = ["Found Family", "Secret Baby"]
        result["trope_confidence"] = {"found family": 0.7,
                                      "secret baby": 0.6}
        result["trope_catalog_map"] = {}
        result["trope_retrieval"] = {}
        result["trope_persistence"] = {}
        self.drive_one_book(result=result)
        rows = self.fake.posts("processor_run_claim_outcomes")[0]["data"]
        trope_rows = [r for r in rows if r["axis"] == "tropes"]
        self.assertEqual(len(trope_rows), 2)
        bare = hashlib.sha256(
            f"tropes|{self.book_key}|catalogue-pending"
            .encode()).hexdigest()
        suffixed = hashlib.sha256(
            f"tropes|{self.book_key}|catalogue-pending#2"
            .encode()).hexdigest()
        self.assertEqual({r["claim_key"] for r in trope_rows},
                         {bare, suffixed})

    def test_evaluated_dropped_claims_keep_their_own_keys(self):
        # Claims the retrieve stage evaluated but the book dropped
        # still earn rows: a mapped one keys by its catalogue id
        # (not the shared pending marker), and an unmapped one
        # does not collide with anything.
        result = v2_result()
        result["tropes"] = ["Enemies To Lovers"]
        result["trope_confidence"] = {"enemies to lovers": 0.8}
        result["trope_catalog_map"] = {
            "enemies to lovers": "trope-123",
            "found family": "trope-456"}
        result["trope_retrieval"] = {
            "enemies to lovers": {"outcome": "supported-single"},
            "found family": {"outcome": "abstained"},
            "secret baby": {"outcome": "adjacent-only"}}
        result["trope_persistence"] = {}
        self.drive_one_book(result=result)
        rows = self.fake.posts("processor_run_claim_outcomes")[0]["data"]
        trope_keys = {r["claim_key"] for r in rows
                      if r["axis"] == "tropes"}
        expect = {
            hashlib.sha256(f"tropes|{self.book_key}|trope-123"
                           .encode()).hexdigest(),
            hashlib.sha256(f"tropes|{self.book_key}|trope-456"
                           .encode()).hexdigest(),
            hashlib.sha256(f"tropes|{self.book_key}|catalogue-pending"
                           .encode()).hexdigest()}
        self.assertEqual(trope_keys, expect)

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


class BudgetAccountingHistoryTests(_Base):
    """Spec §1.1/§1.5: per-consumer budget accounting and the
    principal-coverage metric land in the run-history upload —
    the breakdown pooled into the run totals, the coverage counts
    on the book row's characters record. Unmeasured stays None."""

    def tearDown(self):
        pp._dm_budget_reset()
        super().tearDown()

    def test_totals_carry_pooled_decision_budget(self):
        # Driven manually rather than via drive_one_book: this
        # test's claims are the book's spend, so they must be
        # armed after _history_begin — the recorder snapshots
        # the arm sequence at construction (v2.13.50), and claims
        # made before it exists are a predecessor's, not this
        # book's. (Before v2.13.50 the arming sat above a
        # drive_one_book call, ahead of the recorder entirely.)
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        pp._dm_budget_reset()
        pp._dm_budget_claim(2, consumer="rel_family")
        pp._dm_budget_claim(1, consumer="trigger_gate")
        pp._dm_budget_claim(1, consumer="rel_family")
        pp._history_note_result(self.book, v2_result())
        pp._history_book_outcome(self.book, True)
        pp._history_finish(failures=0)
        totals = self.run_payload()["totals"]
        self.assertEqual(totals["decision_budget"], {
            "rel_family": {"granted": 2, "denied": 0, "spent": 3},
            "trigger_gate": {"granted": 1, "denied": 0, "spent": 1}})

    def test_totals_decision_budget_null_when_nothing_claimed(self):
        pp._dm_budget_reset()
        self.drive_one_book(result=v2_result())
        self.assertIsNone(self.run_payload()["totals"]["decision_budget"])

    def test_book_characters_carry_principal_coverage(self):
        result = v2_result()
        result["principal_rel_coverage"] = {"judged": 3, "total": 4}
        self.drive_one_book(result=result)
        chars = self.book_payload()["characters"]
        self.assertEqual(chars["principal_rel_judged"], 3)
        self.assertEqual(chars["principal_rel_total"], 4)

    def test_principal_coverage_null_when_result_lacks_it(self):
        self.drive_one_book(result=v2_result())
        chars = self.book_payload()["characters"]
        self.assertIsNone(chars["principal_rel_judged"])
        self.assertIsNone(chars["principal_rel_total"])


class PoolFingerprintTests(_Base):
    """v2.13.35 (Advisor R2 on v2.13.33): the armed decision pool
    is per-book runtime state, not run configuration, so it leaves
    the thresholds dict — that dict is the config fingerprint's
    input, and a runtime pool made the same configuration
    fingerprint differently in a warm process (and read the floor
    for a whole fresh-process run, because the stamp is taken
    before any book arms its pool). A per-book stamp on the
    run-history book row is NOT part of this fix: the book
    payload's keys are the live processor_run_books columns and
    that table had no decision_pool column, so the placement was
    a separate schema decision — ruled Option A (a real column)
    and landed in v2.13.36; see DecisionPoolHistoryTests. The
    reduce artefact's own decision_pool stamp is unchanged."""

    def tearDown(self):
        pp._dm_budget_reset()
        super().tearDown()

    def test_run_thresholds_carry_no_decision_pool(self):
        self.drive_one_book(result=v2_result())
        self.assertNotIn("decision_pool",
                         self.run_payload()["thresholds"])

    def test_thresholds_dict_has_no_decision_pool_key(self):
        pp._dm_budget_reset(chapter_count=115)
        self.assertNotIn("decision_pool", pp._history_thresholds())

    def test_fingerprint_identical_whatever_pool_is_armed(self):
        # Warm process: the same configuration, fingerprinted
        # with two different pools armed, must hash identically —
        # before the fix the thresholds stamp carried the armed
        # pool straight into the fingerprint's input.
        pp._dm_budget_reset(chapter_count=200)
        rec_a = pp._RunHistoryRecorder(
            "scan", True, False, str(self.tmp))
        pp._dm_budget_reset(chapter_count=60)
        rec_b = pp._RunHistoryRecorder(
            "scan", True, False, str(self.tmp))
        self.assertEqual(rec_a.config_fingerprint,
                         rec_b.config_fingerprint)


class DecisionPoolHistoryTests(_Base):
    """v2.13.36: step 2 of the v2.13.35 fix, unlocked by the
    Option A ruling (the User's call, 2026-10-18): the migration
    adding processor_run_books.decision_pool was approved and
    applied live — and verified from information_schema — before
    this writer shipped, so the book payload may now carry the
    key. The stamp is captured under the recorder's existing
    per-book budget attribution guard (v2 pipeline, single job),
    and a book whose pool was never armed stamps NULL: the column
    reads "not recorded", never a guessed or floor value."""

    def tearDown(self):
        pp._dm_budget_reset()
        super().tearDown()

    def book_payloads(self):
        posts = self.fake.posts("processor_run_books")
        self.assertEqual(len(posts), 1)
        return {row["book_key"]: row for row in posts[0]["data"]}

    def drive_two_books(self):
        book2 = self.tmp / "book2.epub"
        book2.write_bytes(b"second fake epub bytes for history tests")
        key2 = hashlib.sha256(book2.read_bytes()).hexdigest()
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=2)
        pp._history_book_start(self.book)
        pp._dm_budget_reset(chapter_count=60)
        pp._history_note_result(self.book, v2_result())
        pp._history_book_outcome(self.book, True)
        pp._history_book_start(book2)
        pp._dm_budget_reset(chapter_count=200)
        pp._history_note_result(book2, v2_result())
        pp._history_book_outcome(book2, True)
        pp._history_finish(failures=0)
        return key2

    def test_two_books_stamp_their_own_armed_pools(self):
        key2 = self.drive_two_books()
        rows = self.book_payloads()
        self.assertEqual(rows[self.book_key]["decision_pool"], 60)
        self.assertEqual(rows[key2]["decision_pool"], 200)

    def test_unmeasured_book_stamps_null_not_the_floor(self):
        # No book armed a pool: the locked accessor reports the
        # floor for an unarmed book, and stamping that would
        # record a measurement that never happened.
        pp._dm_budget_reset()
        self.assertEqual(pp._dm_budget_pool(), pp._MERGE_TIER3_CAP)
        self.drive_one_book(result=v2_result())
        self.assertIsNone(self.book_payload()["decision_pool"])

    def test_pool_null_when_attribution_refused(self):
        # Parallel mode shares the budget across books, so a
        # per-book pool would be an attribution the run cannot
        # support — the same guard that keeps decision_calls NULL.
        pp.CONFIG["jobs"] = 2
        pp._dm_budget_reset(chapter_count=60)
        self.drive_one_book(result=v2_result())
        self.assertIsNone(self.book_payload()["decision_pool"])

    def test_book_payload_keys_are_the_live_columns(self):
        # The payload's keys are pinned to the migrated table's
        # columns: decision_pool joins the set only because the
        # column now exists live (verified before this shipped).
        pp._dm_budget_reset(chapter_count=60)
        self.drive_one_book(result=v2_result())
        self.assertEqual(set(self.book_payload()), {
            "run_id", "book_key", "title", "work_id", "source",
            "outcome", "skip_reason", "chapters", "duration_ms",
            "decision_calls", "decision_pool", "claims",
            "characters", "quotes", "suppressed_by_verdict",
            "replace_result", "error_class", "error_message"})


class DuplicateBookKeyTests(_Base):
    """Issue #3: two byte-identical files in one scan are two
    recorder entries (the recorder keys books locally by file
    path) carrying the SAME payload book_key (the file's source
    hash). The flush used to post both book rows in one bulk
    upsert on (run_id, book_key), which Postgres refuses when a
    key repeats inside the batch (error 21000) — sb() returned
    None, the flush returned early, and the claims POST never
    ran, so one duplicated file silently cost the run every
    book and claim row behind a single warning line."""

    def drive_duplicate_files(self):
        """Two different paths, one set of bytes, processed one
        after the other. The copies are told apart on purpose —
        a different title and a different derived-claim
        confidence on the second processing — so the assertions
        can prove WHICH copy shipped, and that its book row and
        its claim rows shipped as one pair."""
        copy_path = self.tmp / "copy.epub"
        copy_path.write_bytes(BOOK_BYTES)
        self.assertEqual(
            hashlib.sha256(copy_path.read_bytes()).hexdigest(),
            self.book_key)
        first = v2_result()
        first["title"] = "First Copy Title"
        second = v2_result()
        second["title"] = "Last Copy Title"
        second["trope_confidence"] = {"enemies to lovers": 0.8,
                                      "found family": 0.33}
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=2)
        for path, result in ((self.book, first),
                             (copy_path, second)):
            pp._history_book_start(path)
            pp._history_note_result(path, result)
            pp._history_book_end(path)
            pp._history_book_outcome(path, True)
        pp._history_finish(failures=0)

    def test_duplicate_book_key_ships_one_book_row_and_its_claims(self):
        self.drive_duplicate_files()
        posts = self.fake.posts("processor_run_books")
        self.assertEqual(len(posts), 1)
        rows = posts[0]["data"]
        # At most one row per (run_id, book_key): the batch that
        # ships can no longer name the shared key twice.
        pairs = [(r["run_id"], r["book_key"]) for r in rows]
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertEqual(pairs, [(RUN_ID, self.book_key)])
        # The last completed processing is the run's final state
        # for this book, so its book row is the one that ships.
        self.assertEqual(rows[0]["title"], "Last Copy Title")
        # And its claim rows ship with it — paired with that same
        # processing, not the first copy's: the derived Found
        # Family claim carries the second copy's confidence.
        claim_posts = self.fake.posts("processor_run_claim_outcomes")
        self.assertEqual(len(claim_posts), 1)
        claim_rows = claim_posts[0]["data"]
        self.assertEqual(len(claim_rows), 3)
        for row in claim_rows:
            self.assertEqual(row["run_book_id"], BOOK_ID)
        confidences = {r["confidence"] for r in claim_rows}
        self.assertIn(0.33, confidences)
        self.assertNotIn(0.7, confidences)

    def test_collapse_prints_a_note_naming_the_book_key(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.drive_duplicate_files()
        text = out.getvalue()
        self.assertIn(self.book_key, text)
        self.assertIn("one book row covers both files", text)

    def test_two_different_books_are_unaffected(self):
        other = self.tmp / "other.epub"
        other.write_bytes(b"different fake epub bytes for history")
        other_key = hashlib.sha256(other.read_bytes()).hexdigest()
        self.assertNotEqual(other_key, self.book_key)
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=2)
        for path in (self.book, other):
            pp._history_book_start(path)
            pp._history_note_result(path, v2_result())
            pp._history_book_end(path)
            pp._history_book_outcome(path, True)
        pp._history_finish(failures=0)
        posts = self.fake.posts("processor_run_books")
        self.assertEqual(len(posts), 1)
        rows = posts[0]["data"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["book_key"] for r in rows},
                         {self.book_key, other_key})
        claim_posts = self.fake.posts("processor_run_claim_outcomes")
        self.assertEqual(len(claim_posts), 1)
        self.assertEqual(len(claim_posts[0]["data"]), 6)


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
        # Amended 2026-10-17 (RH-F1): the original form asserted
        # two unmapped labels hash to the IDENTICAL key. Postgres
        # forbids identical keys in one upsert batch (21000, live
        # on Daemon run 17), so repeated identities now take an
        # ordinal suffix. The property this test exists to guard
        # is unchanged and is asserted in its stronger form: the
        # SET of keys is invariant under relabelling — rename both
        # claims and the same keys come out — so no label material
        # can be entering any key.
        def keys_for(names):
            result = v2_result()
            result["tropes"] = names
            result["trope_catalog_map"] = {}
            result["trope_retrieval"] = {}
            result["trope_confidence"] = {
                n.lower(): 0.5 for n in names}
            claims, rows = pp._history_book_claims(
                {**result, "_history_book_key": self.book_key})
            return {r["claim_key"] for r in rows
                    if r["axis"] == "tropes"}

        bare = hashlib.sha256(
            f"tropes|{self.book_key}|catalogue-pending"
            .encode()).hexdigest()
        suffixed = hashlib.sha256(
            f"tropes|{self.book_key}|catalogue-pending#2"
            .encode()).hexdigest()
        self.assertEqual(keys_for(["Alpha Name", "Beta Name"]),
                         {bare, suffixed})
        self.assertEqual(keys_for(["Gamma Tag", "Delta Tag"]),
                         {bare, suffixed})

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

    def test_http_error_leaves_a_note_for_callers(self):
        # sb() returns None on failure and prints the server's line
        # itself; a caller reporting the failure in its own words
        # (the history flush's warning) reads this note instead of
        # guessing — without it, the live PGRST102 rejection
        # surfaced as a bare "(no-response)". A later success
        # clears the note, so a stale error is never reported
        # against a healthy call.
        def refusing_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(b'{"code":"PGRST102",'
                           b'"message":"All object keys must match"}'))

        with mock.patch.object(urllib.request, "urlopen",
                               refusing_urlopen):
            with contextlib.redirect_stdout(io.StringIO()):
                result = pp.sb("processor_run_claim_outcomes",
                               method="POST", data=[{"a": 1}],
                               schema="processor")
        self.assertIsNone(result)
        note = pp._SB_LAST_ERROR
        self.assertIn("400", note)
        self.assertIn("PGRST102", note)
        pp.sb("processor_runs", method="GET", params="?select=id")
        self.assertIsNone(pp._SB_LAST_ERROR)


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
        preview_dir = pp.SCRIPT_DIR / "preview"
        preview_json = preview_dir / "rhcq.json"
        self.addCleanup(preview_json.unlink, missing_ok=True)
        self.addCleanup((preview_dir / "rhcq.md").unlink,
                        missing_ok=True)
        # A history-armed run names its preview after the run key
        # (rhcq--<run-key>.json/.md — pinned in
        # tests/test_preview_run_key.py); sweep any such artifacts
        # this drive leaves behind too.
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in
                                 preview_dir.glob("rhcq--*.*")])
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
            run_key = None
            if history_on:
                pp._history_begin("scan", preview=True,
                                  learn_dir=str(self.tmp), files_seen=1)
                # Capture the key before _history_finish clears the
                # global: the armed preview is named after it.
                run_key = pp._HISTORY.run_key
                pp._history_book_start(epub)
            ok = pp.process_file_v2(epub, preview=True)
            if history_on:
                pp._history_book_outcome(epub, ok)
                pp._history_finish(failures=0 if ok else 1)
        self.assertTrue(ok)
        if history_on:
            # No run-events sink is live in this drive, so the
            # recorded key is the recorder's own — the same key the
            # preview was named with.
            preview_json = preview_dir / f"rhcq--{run_key}.json"
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


class _ClaimTables:
    """Stateful stand-in for the public claim tables, patterned on
    tests/test_replace_wholesale.py's _StatefulSB: GET/POST/PATCH/
    DELETE with eq/neq/ilike matching and PostgREST NULL semantics
    (a comparison against NULL never matches, not even neq)."""

    def __init__(self, tables=None):
        self.tables = {t: [dict(r) for r in rows]
                       for t, rows in (tables or {}).items()}
        self.calls = []
        self._next_id = 1000

    def _match(self, row, params):
        for part in (params or "").lstrip("?").split("&"):
            if not part or "=" not in part:
                continue
            key, val = part.split("=", 1)
            if key in ("select", "limit", "on_conflict", "order"):
                continue
            if key == "or":
                inner = val.strip()
                if inner.startswith("(") and inner.endswith(")"):
                    inner = inner[1:-1]
                conds = [c for c in inner.split(",") if c]
                if not any(self._match_cond(row, c) for c in conds):
                    return False
                continue
            op, _, v = val.partition(".")
            if not self._match_val(row.get(key), op, unquote(v)):
                return False
        return True

    def _match_cond(self, row, cond):
        col, _, rest = cond.partition(".")
        op, _, v = rest.partition(".")
        return self._match_val(row.get(col), op, unquote(v))

    @staticmethod
    def _match_val(cell, op, v):
        if op == "is":
            if v == "null":
                return cell is None
            return cell is not None and str(cell).lower() == v
        if cell is None:
            return False
        if op == "eq":
            return str(cell) == v
        if op == "neq":
            return str(cell) != v
        if op == "ilike":
            return str(cell).lower() == v.lower()
        return False

    def __call__(self, table, method="GET", data=None, params="",
                 prefer=None, schema=None):
        self.calls.append((table, method))
        rows = self.tables.setdefault(table, [])
        if method == "GET":
            return [dict(r) for r in rows if self._match(r, params)]
        if method == "POST":
            out = []
            items = data if isinstance(data, list) else [data]
            for d in items:
                r = dict(d)
                r.setdefault("id", self._next_id)
                self._next_id += 1
                rows.append(r)
                out.append(dict(r))
            return out
        if method == "PATCH":
            out = []
            for r in rows:
                if self._match(r, params):
                    r.update(data or {})
                    out.append(dict(r))
            return out
        if method == "DELETE":
            gone = [r for r in rows if self._match(r, params)]
            self.tables[table] = [r for r in rows
                                  if not self._match(r, params)]
            return [dict(r) for r in gone]
        return []

    def mutating_calls(self):
        return [c for c in self.calls
                if c[1] in ("POST", "PATCH", "DELETE")]


class _WriteSB:
    """One sb for both worlds: processor-schema calls delegate to
    the recording FakeSB, everything else to the stateful claim
    tables — the same split the real sb makes by schema."""

    def __init__(self, tables):
        self.history = FakeSB()
        self.claims = _ClaimTables(tables)

    def __call__(self, table, method="GET", data=None, params="",
                 prefer=None, schema=None):
        if schema == "processor":
            return self.history(table, method=method, data=data,
                                params=params, prefer=prefer,
                                schema=schema)
        return self.claims(table, method=method, data=data,
                           params=params, prefer=prefer)

    def posts(self, table):
        return self.history.posts(table)

    def sequence(self):
        return self.history.sequence()


def _seed_claim_tables():
    """Existing claims for WORK_ID: deletable processor rows, one
    rejected trope, one confirmed trope, one confirmed trigger, one
    rejected character, one stale pipeline quote."""
    return {
        "book_trope_claims": [
            {"id": 1, "work_id": WORK_ID, "trope_id": "cat-old-a",
             "status": "candidate", "source_type": "ai"},
            {"id": 2, "work_id": WORK_ID, "trope_id": "cat-old-b",
             "status": "candidate", "source_type": "ai"},
            {"id": 3, "work_id": WORK_ID, "trope_id": "cat-rej",
             "status": "rejected", "source_type": "ai"},
            {"id": 4, "work_id": WORK_ID, "trope_id": "cat-conf",
             "status": "confirmed", "source_type": "ai"},
        ],
        "book_trigger_claims": [
            {"id": 5, "work_id": WORK_ID, "warning": "old warning",
             "status": "candidate", "source_type": "ai"},
            {"id": 6, "work_id": WORK_ID, "warning": "kept warning",
             "status": "confirmed", "source_type": "ai"},
        ],
        "book_characters": [
            {"id": 7, "work_id": WORK_ID, "name": "Old Char",
             "status": "candidate", "source_type": "ai"},
            {"id": 8, "work_id": WORK_ID, "name": "Rejected Char",
             "status": "rejected", "source_type": "ai"},
        ],
        "book_quotes": [
            {"id": 9, "work_id": WORK_ID,
             "quote": "old pipeline quote", "source_type": "pipeline"},
        ],
    }


def _write_result():
    """A write-shaped result, mirroring test_replace_wholesale's:
    one mapped trope, one trigger, one character, one quote."""
    return {
        "title": "Test Book", "isbn": "9780000000001",
        "tropes": ["Mind Game"], "trope_confidence": {},
        "trope_catalog_map": {},
        "trope_chapters": {pp._tnorm("Mind Game"): ["Ch 1"]},
        "triggers": [{"warning": "murder", "severity": "mentioned",
                      "severity_claimed": "mentioned",
                      "chapters": ["A"], "chapter_count": 1,
                      "frequency": 0.5, "prominence": "low",
                      "evidence": [{"quote": "q", "chapter": "A"}],
                      "confidence": 0.8}],
        "characters": [{"name": "Hero", "role": "protagonist"}],
        "relationships": [],
        "quotes": [{"text": "a brand new quote", "chapter": "Ch 1"}],
    }


class ReplaceResultTests(_Base):
    """replace_result stops carrying NULLs: write_claims and
    plan_replace stash the replace accounting they already compute,
    and book_outcome reads it into the book payload."""

    def setUp(self):
        super().setUp()
        self.fake = _WriteSB(_seed_claim_tables())
        pp.sb = self.fake

    def test_write_run_replace_result_carries_real_counts(self):
        pp._history_begin("scan", preview=False,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        errors = pp.write_claims(
            WORK_ID, _write_result(),
            trope_mappings={"Mind Game": "cat-a"})
        self.assertEqual(errors, 0)
        pp._history_note_result(self.book, v2_result())
        pp._history_book_outcome(self.book, True)
        pp._history_finish(failures=0)
        # Deleted 5 (2 tropes, 1 trigger, 1 character, 1 quote),
        # inserted 4 (one per section), preserved 2 rejected
        # (trope + character) and 2 confirmed (trope + trigger).
        self.assertEqual(self.book_payload()["replace_result"], {
            "inserted": 4, "deleted": 5, "would_delete": None,
            "preserved_rejected": 2, "preserved_confirmed": 2})
        # The write itself behaved exactly as before: stale
        # processor rows replaced, curated rows untouched.
        tables = self.fake.claims.tables
        self.assertEqual(
            sorted(r["trope_id"] for r in tables["book_trope_claims"]),
            ["cat-a", "cat-conf", "cat-rej"])
        self.assertEqual(
            sorted(r["warning"]
                   for r in tables["book_trigger_claims"]),
            ["kept warning", "murder"])
        self.assertEqual(
            sorted(r["name"] for r in tables["book_characters"]),
            ["Hero", "Rejected Char"])
        self.assertEqual(
            [r["quote"] for r in tables["book_quotes"]],
            ["a brand new quote"])

    def test_preview_replace_result_carries_the_plan(self):
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        plan = pp.plan_replace(
            WORK_ID, _write_result(),
            trope_mappings={"Mind Game": "cat-a"})
        pp._history_note_result(self.book, v2_result())
        pp._history_book_outcome(self.book, True)
        pp._history_finish(failures=0)
        # The payload's numbers ARE the plan's (plan ≡ write): the
        # stash came from plan_replace's own partitions, and the
        # plan was read-only — no mutating claim calls, tables
        # byte-identical to the seed.
        self.assertEqual(sum(plan["would_delete"].values()), 5)
        self.assertEqual(plan["rejections_preserved"], 2)
        self.assertEqual(self.fake.claims.mutating_calls(), [])
        self.assertEqual(self.book_payload()["replace_result"], {
            "inserted": None, "deleted": None, "would_delete": 5,
            "preserved_rejected": 2, "preserved_confirmed": 2})
        self.assertEqual(len(self.fake.claims.tables[
            "book_trope_claims"]), 4)


def _provenance(endpoint, form):
    """One endpoint's provenance record in the shape §2.2 stamps:
    the endpoint, the form that matched, and — for a resolved
    surname — the basis that resolved it (whose verbatim text the
    history mirror must NOT carry)."""
    return {"endpoint": endpoint, "matched_form": form,
            "mention_text": "Sebeck",
            "basis": {"kinds": ["antecedent"], "chapter": 22,
                      "position": 3,
                      "text": "Sebeck's son, Chris, stood next "
                              "to him."} if form == "resolved_surname"
            else None}


def rel_result():
    """A v2 result whose relationships carry endpoint provenance —
    the shape process_file_v2 hands the recorder since §2.2."""
    result = v2_result()
    result["relationships"] = [
        {"from": "Chris Sebeck", "to": "Peter Sebeck",
         "type": "child", "validation_state": "validated",
         "validation_p": 0.46,
         "endpoint_provenance": {
             "from": _provenance("Chris Sebeck", "resolved_surname"),
             "to": _provenance("Peter Sebeck", "full")}},
        {"from": "Laura Sebeck", "to": "Peter Sebeck",
         "type": "spouse", "validation_state": "validated",
         "validation_p": 0.88,
         "endpoint_provenance": {
             "from": _provenance("Laura Sebeck", "full"),
             "to": _provenance("Peter Sebeck", "full")}},
    ]
    result["dropped_relationships"] = [
        {"from": "Jon Ross", "to": "Peter Sebeck",
         "type": "friend", "validation_state": "rejected",
         "validation_p": 0.05,
         "endpoint_provenance": {
             "from": _provenance("Jon Ross", "full"),
             "to": _provenance("Peter Sebeck", "full")}},
    ]
    return result


class LiveSinkFlushTests(_Base):
    """Regression (2.13.26): a run launched with BOTH
    --emit-run-events and --run-history lost its entire history
    upload — the flush read a run_id attribute the event sink has
    never carried (its identity is the artifact's filename stem),
    raised AttributeError before building any payload, and the
    warn-only catch reduced the evidence to the class name. These
    tests pin the combination: sink live at flush time, a preview
    book, relationship outcomes with provenance."""

    def drive_with_live_sink(self, result):
        path = pp._run_events_begin(str(self.tmp), preview=True)
        self.assertIsNotNone(path)
        self.addCleanup(pp._run_events_finish)
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        pp._history_note_result(self.book, result)
        pp._history_book_outcome(self.book, True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pp._history_finish(failures=0)
        return path, out.getvalue()

    def test_preview_flush_with_live_sink_ships_everything(self):
        path, console = self.drive_with_live_sink(rel_result())
        self.assertNotIn("upload failed", console)
        # The history row names the run after the event artifact
        # (spec §3): the artifact's stem IS the run key.
        payload = self.run_payload()
        self.assertEqual(payload["run_key"], path.stem)
        self.assertTrue(payload["preview"])
        self.book_payload()
        # Relationship outcomes — kept AND dropped — arrive as
        # their own rows, provenance mirror attached, the verbatim
        # basis text left behind on the claim where it belongs.
        posts = self.fake.posts("processor_run_claim_outcomes")
        self.assertEqual(len(posts), 1)
        rows = posts[0]["data"]
        rel_rows = [r for r in rows if r["axis"] == "relationships"]
        self.assertEqual(len(rel_rows), 3)
        for row in rel_rows:
            self.assertEqual(row["run_book_id"], BOOK_ID)
            mirror = row["endpoint_provenance"]
            self.assertEqual(
                set(mirror), {"from", "to"})
            self.assertNotIn("text", json.dumps(mirror))
        verdicts = {r["verdict"] for r in rel_rows}
        self.assertEqual(verdicts, {"validated", "rejected"})
        # The other axes' rows carry the provenance key as an
        # explicit null (2.13.27): PostgREST bulk insert refuses a
        # batch whose rows do not all share one key set
        # (PGRST102), so the key cannot simply be absent on the
        # axes that have no provenance — null is the honest shape
        # for "this axis has none" (see UniformOutcomeShapeTests).
        for row in rows:
            if row["axis"] != "relationships":
                self.assertIn("endpoint_provenance", row)
                self.assertIsNone(row["endpoint_provenance"])

    def test_sink_stem_wins_even_when_suffixed(self):
        # A second artifact in the same second takes a -2 suffix;
        # the history key must follow the ACTUAL filename, not the
        # unsuffixed guess a run_id attribute would have held.
        first = pp._run_events_begin(str(self.tmp), preview=True)
        pp._run_events_finish()
        second = pp._run_events_begin(str(self.tmp), preview=True)
        self.addCleanup(pp._run_events_finish)
        self.assertNotEqual(first.stem, second.stem)
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        pp._history_note_result(self.book, rel_result())
        pp._history_book_outcome(self.book, True)
        with contextlib.redirect_stdout(io.StringIO()):
            pp._history_finish(failures=0)
        self.assertEqual(self.run_payload()["run_key"], second.stem)


class OutcomesRefusingSB(FakeSB):
    """The outcomes POST is refused the way PostgREST refused the
    live 2.13.26 run: the transport returns None and leaves the
    server's own words in its last-error note (the real sb()'s
    contract, pinned in TransportTests)."""

    REFUSAL = ('HTTP 400: {"code":"PGRST102","details":null,'
               '"hint":null,"message":"All object keys must match"}')

    def __call__(self, table, method="GET", data=None, params="",
                 prefer=None, schema=None):
        if table == "processor_run_claim_outcomes" and method == "POST":
            pp._SB_LAST_ERROR = self.REFUSAL
            self.calls.append({"table": table, "method": method,
                               "data": data, "params": params,
                               "prefer": prefer, "schema": schema})
            return None
        return super().__call__(table, method=method, data=data,
                                params=params, prefer=prefer,
                                schema=schema)


class UniformOutcomeShapeTests(_Base):
    """Regression (2.13.27): the 2.13.26 Daemon write's run row and
    book rows reached SQL, but its claim outcomes were refused
    wholesale — PostgREST bulk insert demands that every object in
    a posted array carry the SAME keys (PGRST102, "All object keys
    must match"), and the flush appended endpoint_provenance only
    to relationship rows, so a batch mixing axes shipped two key
    sets and lost every row. These tests pin the uniform shape and
    the diagnostic warning its failure produced."""

    def setUp(self):
        super().setUp()
        self._sb_err = getattr(pp, "_SB_LAST_ERROR", None)

    def tearDown(self):
        pp._SB_LAST_ERROR = self._sb_err
        super().tearDown()

    def test_mixed_axis_batch_posts_one_uniform_key_set(self):
        self.drive_one_book(result=rel_result())
        posts = self.fake.posts("processor_run_claim_outcomes")
        self.assertEqual(len(posts), 1)
        rows = posts[0]["data"]
        self.assertTrue(rows)
        # One key set across the whole batch — the property
        # PostgREST refused the live batch for lacking.
        key_sets = {tuple(sorted(r)) for r in rows}
        self.assertEqual(len(key_sets), 1)
        self.assertEqual(
            key_sets.pop(),
            ("axis", "claim_key", "confidence",
             "endpoint_provenance", "run_book_id", "verdict"))
        # The batch really is mixed: relationship rows carry the
        # provenance mirror, the other axes carry an explicit
        # null — present, because absent is what was rejected.
        rel_rows = [r for r in rows if r["axis"] == "relationships"]
        other_rows = [r for r in rows if r["axis"] != "relationships"]
        self.assertTrue(rel_rows)
        self.assertTrue(other_rows)
        for row in rel_rows:
            self.assertIsNotNone(row["endpoint_provenance"])
            self.assertEqual(set(row["endpoint_provenance"]),
                             {"from", "to"})
        for row in other_rows:
            self.assertIn("endpoint_provenance", row)
            self.assertIsNone(row["endpoint_provenance"])

    def test_outcomes_refusal_warn_names_status_and_message(self):
        # Tonight's live failure reached the console as a bare
        # "(no-response)" while the transport's own line above it
        # carried the actual PostgREST error. The warning is the
        # line a paste keeps, so it must name the status and the
        # server's message — and stay warn-only.
        pp.sb = OutcomesRefusingSB()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.drive_one_book(result=rel_result())
        text = out.getvalue()
        self.assertEqual(text.count("Run history: upload failed"), 1)
        self.assertIn("400", text)
        self.assertIn("All object keys must match", text)
        self.assertIn("run unaffected", text)


class ExplodingSB(FakeSB):
    """The transport dies on first contact — the shape a real
    upload failure takes from the flush's point of view."""

    def __call__(self, table, method="GET", **kw):
        raise RuntimeError("mirror exploded — marker-731")


class WarnDetailTests(_Base):
    def test_upload_failure_warn_names_type_and_detail(self):
        # The class alone is not a diagnosis: the console line must
        # carry the exception's message too, so the next failure is
        # diagnosable from the console alone — while staying
        # warn-only (finish never raises, the run is unaffected).
        pp.sb = ExplodingSB()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.drive_one_book(result=v2_result())
        text = out.getvalue()
        self.assertEqual(text.count("Run history: upload failed"), 1)
        self.assertIn("RuntimeError", text)
        self.assertIn("marker-731", text)
        self.assertIn("run unaffected", text)


def _earner_claim(frm="Laura Sebeck", to="Peter Sebeck",
                  typ="spouse", state="validated", p=0.94):
    """One retained minor-sourced claim in the shape the principal
    filter leaves on the result: provenance stamped (the history
    writer's existing gate for relationship rows), a curation
    state, and the validator's score."""
    return {"from": frm, "to": to, "type": typ,
            "validation_state": state, "validation_p": p,
            "endpoint_provenance": {
                "from": _provenance(frm, "resolved_surname"),
                "to": _provenance(to, "full")}}


def earner_only_result():
    """A book whose relationship claims are ONLY retained
    minor-sourced (earner) claims: the filtered principal list and
    the dropped list are both empty."""
    result = v2_result()
    result["relationships"] = []
    result["dropped_relationships"] = []
    result["minor_sourced_relationships"] = [
        _earner_claim(),
        _earner_claim(frm="Chris Sebeck", typ="other",
                      state="validated", p=0.46),
    ]
    return result


class MinorSourcedHistoryTests(_Base):
    """Retained minor-sourced claims (v2.13.28) store in the app,
    so they must also land in run-history outcomes, with exactly
    the treatment a principal claim in the same state receives."""

    def _rel_rows(self):
        posts = self.fake.posts("processor_run_claim_outcomes")
        self.assertEqual(len(posts), 1)
        rows = posts[0]["data"]
        return rows, [r for r in rows if r["axis"] == "relationships"]

    def test_only_earner_claims_post_relationship_rows(self):
        self.drive_one_book(result=earner_only_result())
        _rows, rel_rows = self._rel_rows()
        self.assertEqual(len(rel_rows), 2)
        self.assertEqual({r["verdict"] for r in rel_rows},
                         {"validated"})
        self.assertEqual({r["confidence"] for r in rel_rows},
                         {0.94, 0.46})
        for r in rel_rows:
            self.assertEqual(r["run_book_id"], BOOK_ID)
            self.assertIsNotNone(r["endpoint_provenance"])
        # Keys come from the existing relationship key path:
        # catalogue-style identity here is the normalized
        # canonical pair and type (the pipeline's own pair key,
        # stripped and case-folded), hashed with axis and book
        # fingerprint — never a second, earner-specific
        # derivation.
        expect = {
            hashlib.sha256(
                f"relationships|{self.book_key}|"
                "laura sebeck\x1fpeter sebeck\x1fspouse"
                .encode()).hexdigest(),
            hashlib.sha256(
                f"relationships|{self.book_key}|"
                "chris sebeck\x1fpeter sebeck\x1fother"
                .encode()).hexdigest(),
        }
        self.assertEqual({r["claim_key"] for r in rel_rows}, expect)

    def test_earner_claim_also_in_dropped_list_yields_one_row(self):
        claim = _earner_claim()
        result = earner_only_result()
        result["minor_sourced_relationships"] = [claim]
        result["dropped_relationships"] = [claim]
        self.drive_one_book(result=result)
        _rows, rel_rows = self._rel_rows()
        self.assertEqual(len(rel_rows), 1)
        self.assertEqual(rel_rows[0]["verdict"], "validated")
        self.assertEqual(rel_rows[0]["confidence"], 0.94)

    def test_earner_row_provenance_mirror_strips_basis_text(self):
        self.drive_one_book(result=earner_only_result())
        _rows, rel_rows = self._rel_rows()
        self.assertTrue(rel_rows)
        for row in rel_rows:
            mirror = row["endpoint_provenance"]
            self.assertEqual(set(mirror), {"from", "to"})
            blob = json.dumps(mirror)
            # The verbatim basis sentence from _provenance and the
            # basis/mention fields carrying text never cross.
            self.assertNotIn("Sebeck's son, Chris, stood", blob)
            self.assertNotIn("text", blob)
            self.assertNotIn("mention_text", blob)
            for ep in mirror.values():
                self.assertEqual(
                    set(ep), {"matched_form", "basis_kinds",
                              "basis_chapter", "basis_position"})
        self.assertEqual(
            pp._history_denylist_scan(
                self.fake.posts(
                    "processor_run_claim_outcomes")[0]["data"]), [])

    def test_mixed_batch_with_earner_posts_one_uniform_key_set(self):
        result = rel_result()
        result["minor_sourced_relationships"] = [
            _earner_claim(frm="Joseph Sebeck", typ="child",
                          state="review_required", p=0.5)]
        self.drive_one_book(result=result)
        rows, rel_rows = self._rel_rows()
        self.assertTrue(rows)
        key_sets = {tuple(sorted(r)) for r in rows}
        self.assertEqual(len(key_sets), 1)
        self.assertEqual(
            key_sets.pop(),
            ("axis", "claim_key", "confidence",
             "endpoint_provenance", "run_book_id", "verdict"))
        # Mixed means mixed: trope, trigger and relationship rows
        # all present, and relationships are principal (2) +
        # dropped (1) + earner (1), the earner carrying the same
        # verdict treatment as a principal in its state.
        self.assertEqual({r["axis"] for r in rows},
                         {"tropes", "triggers", "relationships"})
        self.assertEqual(len(rel_rows), 4)
        earner = [r for r in rel_rows
                  if r["verdict"] == "review_required"]
        self.assertEqual(len(earner), 1)
        self.assertEqual(earner[0]["confidence"], 0.5)
        self.assertIsNotNone(earner[0]["endpoint_provenance"])


class ShippedTotalsHistoryTests(_Base):
    """Issues #7 + the v2.13.38 gate WARN: the run row's totals
    must describe exactly the books that shipped — the post-
    collapse survivors whose book rows the mirror actually holds
    — with counts either authoritative or honestly absent, never
    inherited from entries that ship no row. The run row is
    posted before its book rows (they key off its uuid), so a
    books POST that fails or returns only some rows must end
    with the stored totals corrected to the shipped set."""

    def tearDown(self):
        pp._dm_budget_reset()
        super().tearDown()

    def _book_file(self, name, payload):
        path = self.tmp / name
        path.write_bytes(payload)
        return path, hashlib.sha256(payload).hexdigest()

    def _drive_books(self, specs):
        """specs: [(path, result)] processed in order."""
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp),
                          files_seen=len(specs))
        for path, result in specs:
            pp._history_book_start(path)
            if result is not None:
                pp._history_note_result(path, result)
            pp._history_book_end(path)
            pp._history_book_outcome(path, True)
        pp._history_finish(failures=0)

    def _final_run_payload(self):
        posts = self.fake.posts("processor_runs")
        self.assertTrue(posts)
        return posts[-1]["data"][0]

    def test_totals_describe_only_shipped_books_on_partial_books_post(
            self):
        other, other_key = self._book_file(
            "other.epub", b"different fake epub bytes for history")
        self.fake.omit_book_keys = {other_key}
        self._drive_books([(self.book, v2_result()),
                           (other, v2_result())])
        totals = self._final_run_payload()["totals"]
        self.assertEqual(totals["processed"], 1)
        self.assertEqual(totals["succeeded"], 1)
        # The shipped book's claims still ship; the unshipped
        # book's never do (one book = three claim rows here).
        claim_posts = self.fake.posts("processor_run_claim_outcomes")
        self.assertEqual(len(claim_posts), 1)
        self.assertEqual(len(claim_posts[0]["data"]), 3)

    def test_outcomeless_middle_entry_dilutes_nothing(self):
        # Three real books; the middle one's row never comes back
        # from the books POST. Both neighbours' claims must ship
        # and the totals must count the two shipped books only —
        # claim shipping is decided per book against its own row,
        # immune to other entries' presence or order.
        mid, mid_key = self._book_file(
            "mid.epub", b"middle fake epub bytes for history tests")
        last, _last_key = self._book_file(
            "last.epub", b"last fake epub bytes for history tests!")
        self.fake.omit_book_keys = {mid_key}
        self._drive_books([(self.book, v2_result()),
                           (mid, v2_result()),
                           (last, v2_result())])
        totals = self._final_run_payload()["totals"]
        self.assertEqual(totals["processed"], 2)
        self.assertEqual(totals["succeeded"], 2)
        claim_posts = self.fake.posts("processor_run_claim_outcomes")
        self.assertEqual(len(claim_posts), 1)
        self.assertEqual(len(claim_posts[0]["data"]), 6)

    def test_books_post_failure_leaves_no_orphan_totals(self):
        # The stored run row must not claim processed/claims
        # totals for book rows that do not exist: with nothing
        # shipped, the totals are honestly absent, and the flush
        # still warns exactly once.
        self.fake.fail_books_post = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.drive_one_book(result=v2_result())
        self.assertEqual(
            out.getvalue().count("Run history: upload failed"), 1)
        totals = self._final_run_payload()["totals"]
        self.assertIsNone(totals["processed"])
        self.assertIsNone(totals["succeeded"])
        self.assertIsNone(totals["claims_derived"])
        self.assertEqual(len(self.fake.posts("processor_run_books")), 1)
        self.assertEqual(
            self.fake.posts("processor_run_claim_outcomes"), [])

    def test_duplicate_budget_pooled_from_surviving_entry_once(self):
        # WARN: two recorder entries share one book_key and
        # collapse to one shipped row. Pooling the run's
        # decision_budget from every recorder entry keyed by that
        # book_key counts the collapsed copy's spend a second
        # time; the pool must come from the surviving entry only.
        copy_path = self.tmp / "copy.epub"
        copy_path.write_bytes(BOOK_BYTES)
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=2)
        pp._history_book_start(self.book)
        pp._dm_budget_reset(chapter_count=30)
        pp._dm_budget_claim(1, consumer="rel_family")
        pp._history_note_result(self.book, v2_result())
        pp._history_book_end(self.book)
        pp._history_book_outcome(self.book, True)
        pp._history_book_start(copy_path)
        pp._dm_budget_reset(chapter_count=30)
        pp._dm_budget_claim(1, consumer="rel_family")
        pp._dm_budget_claim(1, consumer="rel_family")
        pp._history_note_result(copy_path, v2_result())
        pp._history_book_end(copy_path)
        pp._history_book_outcome(copy_path, True)
        pp._history_finish(failures=0)
        totals = self.run_payload()["totals"]
        self.assertEqual(totals["decision_budget"], {
            "rel_family": {"granted": 2, "denied": 0, "spent": 2}})


class StickyBudgetStampTests(_Base):
    """Issue #14: the decision-budget globals are armed per book
    but were never disarmed between books, so a book that exits
    before arming (no-ISBN skip, unreadable file, ingest
    exception) inherited its predecessor's pool / calls / budget
    snapshot in its history stamps. Captures may only be taken
    from state armed FOR THAT BOOK; a skipped book stamps NULL
    for pool and calls — not 0, not the predecessor's numbers."""

    def tearDown(self):
        pp._dm_budget_reset()
        super().tearDown()

    def test_book_exiting_before_arming_stamps_null(self):
        book2 = self.tmp / "book2.epub"
        book2.write_bytes(b"second fake epub bytes for history tests")
        key2 = hashlib.sha256(book2.read_bytes()).hexdigest()
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=2)
        # Book A arms a pool of 120 (one per chapter) and spends
        # three calls on it.
        pp._history_book_start(self.book)
        pp._dm_budget_reset(chapter_count=120)
        pp._dm_budget_claim(3, consumer="rel_family")
        pp._history_note_result(self.book, v2_result())
        pp._history_book_outcome(self.book, True)
        # Book B exits before arming — no reset intervenes, which
        # is exactly a pre-arming exit's state.
        pp._history_book_start(book2)
        pp._history_book_outcome(book2, False)
        pp._history_finish(failures=1)
        posts = self.fake.posts("processor_run_books")
        self.assertEqual(len(posts), 1)
        rows = {r["book_key"]: r for r in posts[0]["data"]}
        self.assertEqual(rows[self.book_key]["decision_pool"], 120)
        self.assertEqual(rows[self.book_key]["decision_calls"], 3)
        self.assertIsNone(rows[key2]["decision_pool"])
        self.assertIsNone(rows[key2]["decision_calls"])
        # B must not re-pool A's snapshot into the run totals
        # either: the pooled budget is A's spend, counted once.
        totals = self.run_payload()["totals"]
        self.assertEqual(totals["decision_budget"], {
            "rel_family": {"granted": 1, "denied": 0, "spent": 3}})


class CrossRecorderBudgetStampTests(_Base):
    """Gate completion for issue #14 (v2.13.50): v2.13.49's
    armed-marker discipline kept the marker on the recorder
    instance, initialised to None, while the arm sequence is
    process-global and only grows — so a FRESH recorder (watch
    mode builds one per cycle) treated a previous run's final
    arming as its own first stamp, and its first book that exited
    before arming inherited that predecessor run's calls, pool
    and snapshot. The marker must be born at the sequence as of
    the recorder's construction: only armings during the
    recorder's own run can stamp its books."""

    def tearDown(self):
        pp._dm_budget_reset()
        super().tearDown()

    def test_second_recorder_first_book_inherits_nothing(self):
        book2 = self.tmp / "book2.epub"
        book2.write_bytes(b"second fake epub bytes for history tests")
        key2 = hashlib.sha256(book2.read_bytes()).hexdigest()
        # Run 1: its book arms a pool of 50 and spends three calls.
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(self.book)
        pp._dm_budget_reset(chapter_count=50)
        pp._dm_budget_claim(3, consumer="rel_family")
        pp._history_note_result(self.book, v2_result())
        pp._history_book_outcome(self.book, True)
        pp._history_finish(failures=0)
        posts = self.fake.posts("processor_run_books")
        self.assertEqual(len(posts), 1)
        rows1 = {r["book_key"]: r for r in posts[0]["data"]}
        self.assertEqual(rows1[self.book_key]["decision_calls"], 3)
        self.assertEqual(rows1[self.book_key]["decision_pool"], 50)
        # Run 2, same process, a brand-new recorder: its first
        # book exits before arming — the no-ISBN / unreadable
        # shapes. Nothing of run 1's budget state is this book's.
        pp._history_begin("scan", preview=True,
                          learn_dir=str(self.tmp), files_seen=1)
        pp._history_book_start(book2)
        pp._history_book_outcome(book2, False)
        pp._history_finish(failures=1)
        posts = self.fake.posts("processor_run_books")
        self.assertEqual(len(posts), 2)
        rows2 = {r["book_key"]: r for r in posts[1]["data"]}
        self.assertIsNone(rows2[key2]["decision_calls"])
        self.assertIsNone(rows2[key2]["decision_pool"])
        # And no pooled budget is inherited into run 2's totals.
        run_posts = self.fake.posts("processor_runs")
        self.assertEqual(len(run_posts), 2)
        self.assertIsNone(run_posts[1]["data"][0]["totals"]
                          ["decision_budget"])


class RepostRefusingSB(FakeSB):
    """The run POST that creates the run row succeeds; every
    later run POST — the corrective totals repost is the only
    other one the flush makes — is refused, in the transport's
    failure shape (None)."""

    def __init__(self):
        super().__init__()
        self.run_posts_seen = 0

    def __call__(self, table, method="GET", data=None, params="",
                 prefer=None, schema=None):
        if table == "processor_runs" and method == "POST":
            self.run_posts_seen += 1
            if self.run_posts_seen > 1:
                self.calls.append({"table": table, "method": method,
                                   "data": data, "params": params,
                                   "prefer": prefer, "schema": schema})
                return None
        return super().__call__(table, method=method, data=data,
                                params=params, prefer=prefer,
                                schema=schema)


class RepostWarningTests(_Base):
    """Gate WARN (v2.13.50): when the books POST answered for
    only some rows, the flush corrects the stored run totals via
    _history_repost_totals — but if THAT call failed, no history
    warning fired at all, so the run row kept its inflated totals
    in silence. The repost must warn on its own failure; the
    books-refused branch (where the repost succeeds) keeps its
    exactly-one-warning shape."""

    def test_partial_books_repost_failure_warns(self):
        self.fake = RepostRefusingSB()
        pp.sb = self.fake
        other = self.tmp / "other.epub"
        other.write_bytes(b"different fake epub bytes for history")
        other_key = hashlib.sha256(other.read_bytes()).hexdigest()
        # The books POST answers for the first book only, so the
        # flush takes the partial branch and reposts corrected
        # totals — and the repost is the call this fake refuses.
        self.fake.omit_book_keys = {other_key}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pp._history_begin("scan", preview=True,
                              learn_dir=str(self.tmp), files_seen=2)
            for path in (self.book, other):
                pp._history_book_start(path)
                pp._history_note_result(path, v2_result())
                pp._history_book_end(path)
                pp._history_book_outcome(path, True)
            pp._history_finish(failures=0)
        # The corrective repost was attempted exactly once (the
        # second run POST) ...
        self.assertEqual(self.fake.run_posts_seen, 2)
        # ... and its failure produced a history warning: before
        # the fix this branch warned zero times and the stored
        # run row silently kept its inflated totals.
        self.assertEqual(
            out.getvalue().count("Run history: upload failed"), 1)

    def test_none_books_branch_still_warns_exactly_once(self):
        self.fake.fail_books_post = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.drive_one_book(result=v2_result())
        self.assertEqual(
            out.getvalue().count("Run history: upload failed"), 1)


if __name__ == "__main__":
    unittest.main()
