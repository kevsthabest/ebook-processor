"""Run-history backfill tests (docs/run-history-spec.md §8).

§8 names the phase-1.1 remedy for a lost history flush: a
`--run-history-backfill <jsonl>` re-send tool that ships a run's
history rows from its surviving run-events artifact. The artifact
is the ground truth the backfill reads; the payloads it posts are
built by the live writer's own builders (_history_book_payload /
_history_book_claims / _history_totals), so a backfilled row
carries exactly the fields those builders produce — and every
field the artifact never recorded stays None (the spec's
"unknown is NULL" rule), never a value reconstructed from the
machine the backfill happens to run on.

The transport seam is the same recording-fake pattern as
tests/test_run_history.py, except this fake is STATEFUL (rows
persist across calls, keyed by the tables' upsert targets), so
the idempotence assertions measure what a second invocation
actually writes, not what it attempts.

Fixture artifacts are written by the processor's own run-events
sink (the real emit code), so the event shape under test is the
shape production artifacts carry, not a hand-invented one.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PP_PATH = Path(__file__).resolve().parent.parent / "process-portable.py"
_spec = importlib.util.spec_from_file_location("pp_run_history_backfill",
                                               _PP_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

RUN_UUID = "aaaaaaaa-1111-2222-3333-444444444444"
BOOK_UUID = "bbbbbbbb-1111-2222-3333-444444444444"
BOOK_BYTES = b"fake epub bytes for backfill tests"
BOOK2_BYTES = b"second fake epub bytes for backfill tests"
CATALOG = [("trope-123", "Enemies To Lovers", [1.0, 0.0]),
           ("trope-456", "Found Family", [0.0, 1.0])]


class StatefulSB:
    """Stand-in for pp.sb with a memory: GETs answer from the
    rows earlier POSTs stored, POSTs upsert on the tables'
    conflict keys and echo ids back, the way PostgREST's
    merge-duplicates upsert behaves for the writer's calls."""

    def __init__(self):
        self.calls = []
        self.runs = {}      # run_key -> row (with id)
        self.books = {}     # (run_id, book_key) -> row (with id)
        self.claims = {}    # (run_book_id, axis, claim_key) -> row
        self._book_seq = 0

    def __call__(self, table, method="GET", data=None, params="",
                 prefer=None, schema=None):
        self.calls.append({"table": table, "method": method,
                           "data": data, "params": params,
                           "prefer": prefer, "schema": schema})
        if table == "processor_runs" and method == "GET":
            key = params.split("run_key=eq.", 1)[1].split("&", 1)[0]
            row = self.runs.get(key)
            return [dict(row)] if row else []
        if table == "processor_runs" and method == "POST":
            out = []
            for row in data:
                stored = dict(row)
                stored.setdefault("id", RUN_UUID)
                self.runs[stored["run_key"]] = stored
                out.append({"id": stored["id"]})
            return out
        if table == "processor_run_books" and method == "POST":
            out = []
            for row in data:
                k = (row["run_id"], row["book_key"])
                stored = dict(row)
                if k not in self.books:
                    self._book_seq += 1
                    stored["id"] = (BOOK_UUID if self._book_seq == 1
                                     else f"book-{self._book_seq}")
                else:
                    stored["id"] = self.books[k]["id"]
                self.books[k] = stored
                out.append({"id": stored["id"],
                            "book_key": row["book_key"]})
            return out
        if table == "processor_run_claim_outcomes" and method == "POST":
            for row in data:
                k = (row["run_book_id"], row["axis"], row["claim_key"])
                self.claims[k] = dict(row)
            return [{} for _ in data]
        return []

    def posts(self, table=None):
        return [c for c in self.calls if c["method"] == "POST"
                and (table is None or c["table"] == table)]


class _Base(unittest.TestCase):
    def setUp(self):
        self._config = dict(pp.CONFIG)
        self._sb = pp.sb
        self._import_dir = pp.IMPORT_DIR
        self.fake = StatefulSB()
        pp.sb = self.fake
        pp.CONFIG.update({
            "supabase_url": "https://example.invalid",
            "supabase_key": "test-key",
            "pipeline": "v2", "jobs": 1,
        })
        self._tmpd = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpd.name)
        self.learn_dir = self.tmp / "learned"
        self.import_dir = self.tmp / "import"
        self.import_dir.mkdir(parents=True)
        pp.IMPORT_DIR = self.import_dir
        self.book = self.import_dir / "Fixture Book.epub"
        self.book.write_bytes(BOOK_BYTES)
        self.book_key = hashlib.sha256(BOOK_BYTES).hexdigest()
        self.book2 = self.import_dir / "Second Book.epub"
        self.book2.write_bytes(BOOK2_BYTES)
        self.book2_key = hashlib.sha256(BOOK2_BYTES).hexdigest()

    def tearDown(self):
        pp.sb = self._sb
        pp.IMPORT_DIR = self._import_dir
        pp.CONFIG.clear()
        pp.CONFIG.update(self._config)
        pp._DM_BUDGET_USED = 0
        if pp._run_events_active():
            pp._run_events_finish()
        self._tmpd.cleanup()

    def make_artifact(self, books=("one",), finish=True,
                      interrupted=False):
        """Write one run-events artifact with the real sink: for
        each named book, a bracket carrying two budget claims, one
        mapped impulse (Enemies To Lovers -> trope-123) and one
        unmapped impulse. Returns the artifact path."""
        path = pp._run_events_begin(self.learn_dir, preview=True)
        self.assertIsNotNone(path)
        for which in books:
            fpath = self.book if which == "one" else self.book2
            with pp._run_events_book(fpath):
                pp._dm_budget_reset()
                pp._dm_budget_claim(1)
                pp._dm_budget_claim(1)
                pp._run_events_impulse(
                    "enemies to lovers dynamic", [1.0, 0.0],
                    CATALOG, "trope-123", 0.95, 0.78)
                pp._run_events_impulse(
                    "some unmapped label", [0.0, 0.0, 1.0],
                    CATALOG, "trope-456", 0.30, 0.78)
        if finish:
            pp._run_events_finish(failures=0, interrupted=interrupted)
        else:
            # A hard-killed run: the sink never wrote run_end.
            pp._RUN_SINK.close()
            pp._RUN_SINK = None
        return path

    def backfill(self, *paths, dry_run=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pp.cmd_run_history_backfill(
                [str(p) for p in paths], dry_run=dry_run)
        return rc, out.getvalue()


class BackfillShipTests(_Base):
    def test_complete_run_backfills_all_three_tables(self):
        path = self.make_artifact()
        rc, text = self.backfill(path)
        self.assertEqual(rc, 0, text)
        # Run row: every field the artifact records, and None for
        # every stamp it does not (version/models/prompts are the
        # live run's, and this artifact never carried them).
        self.assertEqual(len(self.fake.runs), 1)
        run = next(iter(self.fake.runs.values()))
        self.assertEqual(run["run_key"], path.stem)
        self.assertTrue(run["preview"])
        self.assertFalse(run["dry_run"])
        self.assertEqual(run["status"], "completed")
        self.assertIsNotNone(run["started_at"])
        self.assertIsNotNone(run["finished_at"])
        self.assertIsNone(run["processor_version"])
        self.assertIsNone(run["machine_label"])
        self.assertIsNone(run["mode"])
        self.assertIsNone(run["config_fingerprint"])
        self.assertIsNone(run["error_summary"])
        totals = run["totals"]
        self.assertEqual(set(totals), set(pp._HISTORY_TOTAL_KEYS))
        self.assertEqual(totals["claims_derived"], 1)
        # Book row: keyed by the file's source hash (the live
        # writer's book_key for a file-sourced book), decision
        # counts from the book_end event, no filename anywhere.
        self.assertEqual(len(self.fake.books), 1)
        book = next(iter(self.fake.books.values()))
        self.assertEqual(book["book_key"], self.book_key)
        self.assertEqual(book["source"], "file")
        self.assertIsNone(book["title"])
        self.assertEqual(book["decision_calls"], 2)
        blob = json.dumps(self.fake.calls, default=str)
        self.assertNotIn("Fixture Book.epub", blob)
        # Claim rows: the mapped impulse earns exactly the row the
        # live builder keys for a derived trope with that
        # catalogue identity; the unmapped candidate earns none.
        self.assertEqual(len(self.fake.claims), 1)
        (rbid, axis, claim_key), row = next(
            iter(self.fake.claims.items()))
        self.assertEqual(axis, "tropes")
        expect = hashlib.sha256(
            f"tropes|{self.book_key}|trope-123".encode()).hexdigest()
        self.assertEqual(claim_key, expect)
        self.assertEqual(row["verdict"], "derived")
        self.assertIn(row["verdict"], pp._HISTORY_CLAIM_VERDICTS)
        # Confidence is None: the impulse's score is a catalogue
        # similarity, not the claim confidence the live writer
        # stamps, and backfill must not relabel it as one.
        self.assertIsNone(row["confidence"])
        # Upload order and targets match the live flush's.
        seq = [(c["method"], c["table"]) for c in self.fake.calls
               if c["method"] == "POST"]
        self.assertEqual(seq, [("POST", "processor_runs"),
                               ("POST", "processor_run_books"),
                               ("POST", "processor_run_claim_outcomes")])
        for call in self.fake.calls:
            self.assertEqual(call["schema"], "processor")
        self.assertIn("1 run", text)
        self.assertIn("1 book", text)
        self.assertIn("1 claim", text)

    def test_second_invocation_writes_nothing_new(self):
        path = self.make_artifact()
        rc, _ = self.backfill(path)
        self.assertEqual(rc, 0)
        runs = dict(self.fake.runs)
        books = dict(self.fake.books)
        claims = dict(self.fake.claims)
        self.fake.calls.clear()
        rc, text = self.backfill(path)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.fake.posts(), [])
        self.assertIn("already in history", text)
        self.assertEqual(self.fake.runs, runs)
        self.assertEqual(self.fake.books, books)
        self.assertEqual(self.fake.claims, claims)

    def test_live_row_for_same_run_is_never_overwritten(self):
        # A run whose flush actually landed: the stored row's
        # started_at (the recorder's wall clock, a second after
        # the artifact's first event) differs from the artifact's,
        # so the same-run guard cannot prove ownership — and a
        # backfill must still not mint a suffixed duplicate of a
        # run the mirror already holds under its own key.
        path = self.make_artifact()
        events = [json.loads(l) for l in
                  path.read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        import time as _t
        started = _t.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            _t.gmtime(events[0]["ts"] + 1))
        self.fake.runs[path.stem] = {
            "id": RUN_UUID, "run_key": path.stem,
            "started_at": started, "machine_label": "Lazydog",
            "processor_version": pp.PORTABLE_VERSION}
        rc, text = self.backfill(path)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.fake.posts(), [])
        self.assertEqual(self.fake.runs[path.stem]["machine_label"],
                         "Lazydog")

    def test_interrupted_and_unfinished_statuses(self):
        path = self.make_artifact(interrupted=True)
        rc, _ = self.backfill(path)
        self.assertEqual(rc, 0)
        run = next(iter(self.fake.runs.values()))
        self.assertEqual(run["status"], "interrupted")
        self.fake.runs.clear()
        path2 = self.make_artifact(finish=False)
        rc, _ = self.backfill(path2)
        self.assertEqual(rc, 0)
        run = next(iter(self.fake.runs.values()))
        self.assertEqual(run["status"], "interrupted")
        self.assertIsNone(run["finished_at"])

    def test_book_whose_file_is_gone_ships_no_book_row(self):
        path = self.make_artifact(books=("one", "two"))
        self.book2.unlink()
        rc, text = self.backfill(path)
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.fake.books), 1)
        book = next(iter(self.fake.books.values()))
        self.assertEqual(book["book_key"], self.book_key)
        self.assertIn("not backfilled", text)

    def test_dry_run_builds_but_writes_nothing(self):
        path = self.make_artifact()
        rc, text = self.backfill(path, dry_run=True)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.fake.calls, [])
        self.assertIn("would write", text)


class BackfillIsolationTests(_Base):
    def test_corrupt_file_named_and_skipped_good_file_lands(self):
        good = self.make_artifact()
        bad = self.learn_dir / "run-events" / "run-corrupt.jsonl"
        bad.write_text('{"event": "run_start", "run_id": "x"}\n'
                       '{not json\n', encoding="utf-8")
        rc, text = self.backfill(bad, good)
        self.assertEqual(rc, 1, text)
        self.assertIn("run-corrupt.jsonl", text)
        self.assertIn("skipped", text)
        self.assertEqual(len(self.fake.runs), 1)
        self.assertEqual(len(self.fake.books), 1)

    def test_file_without_run_start_is_corrupt(self):
        bad = self.learn_dir / "run-events"
        bad.mkdir(parents=True, exist_ok=True)
        f = bad / "run-headless.jsonl"
        f.write_text('{"event": "book_start", "book": "X.epub"}\n',
                     encoding="utf-8")
        rc, text = self.backfill(f)
        self.assertEqual(rc, 1, text)
        self.assertIn("run-headless.jsonl", text)
        self.assertEqual(self.fake.posts(), [])

    def test_missing_path_is_named_and_skipped(self):
        rc, text = self.backfill(self.tmp / "nope.jsonl")
        self.assertEqual(rc, 1, text)
        self.assertIn("nope.jsonl", text)

    def test_directory_argument_backfills_every_artifact(self):
        self.make_artifact()
        self.make_artifact(books=("two",))
        rc, text = self.backfill(self.learn_dir / "run-events")
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.fake.runs), 2)
        self.assertEqual(len(self.fake.books), 2)

    def test_denylisted_fields_in_events_never_reach_a_row(self):
        path = self.make_artifact()
        lines = path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["quote"] = "SENTINEL-QUOTE-TEXT"
        first["evidence"] = "SENTINEL-EVIDENCE"
        first["api_key"] = "sk-SENTINELKEY123456"
        lines[0] = json.dumps(first)
        for i, line in enumerate(lines):
            ev = json.loads(line)
            if ev.get("event") == "impulse":
                ev["book_text"] = "SENTINEL-BOOK-TEXT"
                ev["file_path"] = "/home/someone/books/Fake.epub"
                lines[i] = json.dumps(ev)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rc, _ = self.backfill(path)
        self.assertEqual(rc, 0)
        blob = json.dumps(self.fake.calls, default=str)
        for sentinel in ("SENTINEL-QUOTE-TEXT", "SENTINEL-EVIDENCE",
                         "SENTINEL-BOOK-TEXT", "SENTINELKEY",
                         "/home/someone"):
            self.assertNotIn(sentinel, blob)
        for call in self.fake.calls:
            if call["data"]:
                self.assertEqual(
                    pp._history_denylist_scan(call["data"]), [],
                    call["table"])

    def test_missing_supabase_config_writes_nothing(self):
        pp.CONFIG["supabase_url"] = ""
        path = self.make_artifact()
        rc, text = self.backfill(path)
        self.assertEqual(rc, 2, text)
        self.assertEqual(self.fake.calls, [])


class BackfillCliTests(_Base):
    def test_parser_and_classification(self):
        args = pp._build_parser().parse_args(
            ["--run-history-backfill", "some.jsonl"])
        self.assertEqual(args.run_history_backfill, ["some.jsonl"])
        args = pp._build_parser().parse_args([])
        self.assertIsNone(args.run_history_backfill)
        self.assertIn("run_history_backfill", pp._PROFILE_EXCLUDE)
        self.assertNotIn("run_history_backfill",
                         pp._PROFILE_PERSISTABLE)
        self.assertEqual(
            pp._ONE_SHOT_FLAGS.get("run_history_backfill"),
            "--run-history-backfill")

    def test_dry_run_companion_is_not_a_conflict(self):
        ap = pp._build_parser()
        args = ap.parse_args(
            ["--run-history-backfill", "x.jsonl", "--dry-run"])
        defaults = ap.parse_args([])
        self.assertIsNone(pp._flag_conflicts(args, defaults))
        # But a second operation beside it still refuses.
        args = ap.parse_args(
            ["--run-history-backfill", "x.jsonl", "--corpus-status"])
        self.assertIsNotNone(pp._flag_conflicts(args, defaults))

    def test_main_dispatches_and_exits_with_its_code(self):
        with mock.patch.object(pp, "cmd_run_history_backfill",
                               return_value=0) as cmd, \
                mock.patch.object(sys, "argv",
                                  ["proc", "--run-history-backfill",
                                   "some.jsonl"]):
            with self.assertRaises(SystemExit) as cm:
                pp.main()
        self.assertEqual(cm.exception.code, 0)
        cmd.assert_called_once()
        self.assertEqual(cmd.call_args[0][0], ["some.jsonl"])


if __name__ == "__main__":
    unittest.main()
