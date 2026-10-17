"""Corpus snapshot tests (docs/corpus-snapshot-spec.md §9).

The payload builder is exercised against seeded corpora built with
the real CorpusDB; the transport seam is process-portable's `sb`
helper, replaced with a recording fake (the run-history pattern):
every assertion about ordering, chunking, the finalize PATCH and
the delete-on-failure discipline is made against the calls the
command WOULD have shipped.
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PP_PATH = Path(__file__).resolve().parent.parent / "process-portable.py"
_spec = importlib.util.spec_from_file_location("pp_corpus_snapshot",
                                               _PP_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

HEADER_ID = "11111111-2222-3333-4444-555555555555"
WORK_UUID = "22222222-3333-4444-5555-666666666666"
CUR_MODEL = "test-embed"
OLD_MODEL = "old-embed"
FMT = "raw-f32-v1"
SENTINEL_FLOAT = 9.87654321


def _gen(model, canary="ok"):
    return {"model": model, "format": FMT, "dim": 3,
            "canary": canary,
            "born": "2026-01-01T00:00:00+00:00",
            "checked": "2026-01-02T00:00:00+00:00",
            "sentinels": {"vectors": [[SENTINEL_FLOAT, 0.1, 0.2]],
                          "cosines": [[1.0]]}}


def _work_row(fp, **kw):
    row = {"fingerprint": fp, "book_key": None, "work_id": None,
           "title": None, "author": None, "identifiers": None,
           "series": None, "series_index": None,
           "parser_version": "1", "chapter_count": None,
           "text_root_hash": None, "source_hash": None,
           "status": "complete", "ingested_at": "2026-10-01T00:00:00+00:00",
           "updated_at": "2026-10-01T00:00:00+00:00", "payload": None}
    row.update(kw)
    return row


def _insert_work(conn, row):
    cols = list(row)
    conn.execute(
        f"INSERT INTO corpus_works ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        [row[c] for c in cols])


def _insert_passages(conn, fp, n, start=0):
    for i in range(start, start + n):
        conn.execute(
            "INSERT INTO corpus_passages (passage_id, fingerprint, "
            "chapter_index, start_offset, end_offset, text_hash) "
            "VALUES (?, ?, 0, ?, ?, ?)",
            (f"{fp}-p{i}", fp, i * 10, i * 10 + 5, f"th-{fp}-{i}"))


def _insert_vector(conn, fp, idx, model):
    conn.execute(
        "INSERT INTO passage_vectors (passage_id, fingerprint, "
        "model, format, dim, vector) VALUES (?, ?, ?, ?, 3, ?)",
        (f"{fp}-p{idx}", fp, model, FMT,
         pp._corpus_pack_vector([0.1, 0.2, 0.3])))


def _insert_quote(conn, qid, fp, status):
    conn.execute(
        "INSERT INTO quotes (quote_id, fingerprint, chapter_index, "
        "start_offset, end_offset, text, kind, status, payload) "
        "VALUES (?, ?, 0, 0, 5, 'planted quote text', 'line', ?, '{}')",
        (qid, fp, status))


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self._config = dict(pp.CONFIG)
        pp.CONFIG.update({
            "supabase_url": "https://example.invalid",
            "supabase_key": "test-key",
            "machine_label": "Lazydog",
            "run_history": False})

    def tearDown(self):
        pp._close_corpus_dbs()
        pp._close_state_dbs()
        pp.CONFIG.clear()
        pp.CONFIG.update(self._config)
        self._td.cleanup()

    def seed_rich(self):
        """A corpus with a current generation (sentinel-laden), an
        old generation, and three works exercising the projection
        branches. Returns the seeding handle."""
        db = pp.CorpusDB.create(self.dir, mode="importer")
        pp._corpus_store_generation(db, _gen(CUR_MODEL))
        db.meta_set(pp._corpus_generation_key(OLD_MODEL, FMT),
                    json.dumps(_gen(OLD_MODEL, canary="suspect")))
        with db.write_txn() as conn:
            _insert_work(conn, _work_row(
                "fp-alpha", book_key="staging:alpha",
                work_id=WORK_UUID, title="Alpha Title",
                author="Ann Author",
                identifiers=json.dumps({
                    "isbn": "978-3-16-148410-0",
                    "asin": "B00ASINXXX",
                    "raw": ["978-3-16-148410-0", "B00ASINXXX",
                            "https://doi.org/10.1000/xyz123"],
                    "series": "Saga", "series_index": 2.0}),
                series="Saga", series_index=2.0, chapter_count=3,
                text_root_hash="root-alpha", source_hash="src-alpha",
                status="complete",
                payload=json.dumps({
                    "word_count": 12000,
                    "chapter_detection_fallback": False,
                    "source_name": "C:\\Users\\someone\\Books\\"
                                   "Alpha Book.epub",
                    "series_source": "epub"})))
            _insert_passages(conn, "fp-alpha", 4)
            for i in range(3):
                _insert_vector(conn, "fp-alpha", i, CUR_MODEL)
            _insert_vector(conn, "fp-alpha", 3, OLD_MODEL)
            _insert_quote(conn, "q-a1", "fp-alpha", "verified")
            _insert_work(conn, _work_row(
                "fp-beta", book_key="staging:beta",
                work_id="not-a-uuid", title="Beta Title",
                author="Bob Writer",
                identifiers=json.dumps({
                    "isbn": "not-an-isbn",
                    "asin": "B08XXXXXXX",
                    "raw": ["0-306-40615-2",
                            "https://doi.org/10.1000/xyz123",
                            "someone@example.com"]}),
                chapter_count=2, text_root_hash="root-beta",
                source_hash="src-beta", status="vectors_partial",
                payload=json.dumps({"source_name": "Beta.epub"})))
            _insert_passages(conn, "fp-beta", 2)
            _insert_work(conn, _work_row(
                "fp-gamma", title="Gamma Title", author="Gail Pen",
                identifiers="garbage{not json",
                chapter_count=1, text_root_hash="root-gamma",
                source_hash="src-gamma", status="text_complete",
                payload="not json{"))
            for i, st in enumerate(
                    ["verified", "verified", "selected", "rejected"]):
                _insert_quote(conn, f"q-g{i}", "fp-gamma", st)
            conn.execute(
                "INSERT INTO corpus_chapters (fingerprint, "
                "chapter_index, label, text, text_hash) VALUES "
                "('fp-alpha', 0, 'One', 'a distinctive planted "
                "sentence about velvet hammers', 'x')")
            conn.execute(
                "INSERT INTO work_envelope (fingerprint, corpus_v, "
                "envelope, computed_at) VALUES ('fp-alpha', '1', "
                "?, '2026-10-02T00:00:00+00:00')",
                (json.dumps({"sections": {}}),))
            conn.execute(
                "INSERT INTO work_envelope (fingerprint, corpus_v, "
                "envelope, computed_at) VALUES ('fp-gamma', '1', "
                "?, '2026-10-03T00:00:00+00:00')",
                (json.dumps({"sections": {"quotes": {
                    "retracted_at": "2026-10-03T00:00:00+00:00",
                    "reason": "retracted because "
                              "https://example.com/secret "
                              "C:\\Users\\someone\\file.txt"}}}),))
        return db


class PayloadBuilderTests(_Base):
    def test_every_book_field(self):
        self.seed_rich()
        data = pp._corpus_snapshot_data(self.dir)
        self.assertTrue(data["present"])
        books = {b["fingerprint"]: b for b in data["books"]}
        a = books["fp-alpha"]
        self.assertEqual(a["book_key"], "staging:alpha")
        self.assertEqual(a["work_id"], WORK_UUID)
        self.assertEqual(a["title"], "Alpha Title")
        self.assertEqual(a["author"], "Ann Author")
        # Hyphenated stored ISBN ships re-cleaned to digits-only 13.
        self.assertEqual(a["isbn"], "9783161484100")
        self.assertEqual(a["series"], "Saga")
        self.assertEqual(a["series_index"], 2.0)
        self.assertEqual(a["status"], "complete")
        self.assertEqual(a["parser_version"], "1")
        self.assertEqual(a["chapters"], 3)
        self.assertEqual(a["words"], 12000)
        self.assertEqual(a["passages"], 4)
        # Only the CURRENT generation's vectors count (3 of 4; the
        # old-generation vector on passage 3 is not coverage).
        self.assertEqual(a["vectors"], 3)
        self.assertEqual(a["vector_coverage"], 0.75)
        self.assertEqual(a["chapter_detection_fallback"], False)
        self.assertEqual(a["text_root_hash"], "root-alpha")
        self.assertEqual(a["source_hash"], "src-alpha")
        self.assertEqual(a["quotes"], {"total": 1,
                                       "by_status": {"verified": 1},
                                       "selected": 0})
        self.assertEqual(a["envelope_corpus_v"], "1")
        self.assertEqual(a["envelope_computed_at"],
                         "2026-10-02T00:00:00+00:00")
        self.assertEqual(a["ingested_at"], "2026-10-01T00:00:00+00:00")

        b = books["fp-beta"]
        self.assertIsNone(b["work_id"])       # not UUID-shaped
        # The stored "isbn" is garbage; the raw list's ISBN-10
        # re-cleans to its ISBN-13 form.
        self.assertEqual(b["isbn"], "9780306406157")
        self.assertIsNone(b["words"])         # payload carries none
        self.assertIsNone(b["chapter_detection_fallback"])
        self.assertEqual(b["passages"], 2)
        self.assertEqual(b["vectors"], 0)    # measured: none embedded
        self.assertEqual(b["vector_coverage"], 0.0)
        self.assertEqual(b["quotes"], {"total": 0, "by_status": {},
                                       "selected": 0})
        self.assertIsNone(b["envelope_corpus_v"])

        g = books["fp-gamma"]
        self.assertIsNone(g["isbn"])          # malformed identifiers
        self.assertIsNone(g["words"])         # malformed payload
        self.assertEqual(g["passages"], 0)
        self.assertEqual(g["vectors"], 0)
        self.assertIsNone(g["vector_coverage"])  # undefined: 0 passages
        self.assertEqual(g["quotes"],
                         {"total": 4,
                          "by_status": {"verified": 2, "selected": 1,
                                        "rejected": 1},
                          "selected": 1})
        self.assertEqual(g["envelope_corpus_v"], "1")
        self.assertEqual(g["envelope_computed_at"],
                         "2026-10-03T00:00:00+00:00")

    def test_header_fields(self):
        self.seed_rich()
        data = pp._corpus_snapshot_data(self.dir)
        h = data["header"]
        self.assertTrue(h["snapshot_key"].startswith("snapshot-"))
        self.assertEqual(h["machine_label"], "Lazydog")
        self.assertEqual(h["processor_version"], pp.PORTABLE_VERSION)
        self.assertEqual(h["corpus_parser_version"],
                         pp.CORPUS_PARSER_VERSION)
        self.assertEqual(h["corpus_v"], pp.CORPUS_V)
        self.assertEqual(h["works_count"], 3)
        self.assertFalse(h["complete"])
        self.assertTrue(h["fts_available"])
        self.assertEqual(h["corpus_bytes"],
                         h["size_breakdown"]["total"])
        self.assertEqual(set(h["size_breakdown"]),
                         {"text", "fts", "vectors", "other", "total",
                          "via"})
        self.assertEqual(h["totals"], {
            "works": 3,
            "by_status": {"complete": 1, "vectors_partial": 1,
                          "text_complete": 1},
            "quotes": 5,
            "quotes_by_status": {"verified": 3, "selected": 1,
                                 "rejected": 1}})

    def test_no_generation_means_null_vectors(self):
        db = pp.CorpusDB.create(self.dir, mode="importer")
        with db.write_txn() as conn:
            _insert_work(conn, _work_row("fp-solo", title="Solo"))
            _insert_passages(conn, "fp-solo", 3)
        data = pp._corpus_snapshot_data(self.dir)
        self.assertTrue(data["present"])
        self.assertIsNone(data["header"]["current_generation"])
        self.assertEqual(data["header"]["generations"], [])
        book = data["books"][0]
        self.assertEqual(book["passages"], 3)
        # Unknown is NULL, never a fake 0.
        self.assertIsNone(book["vectors"])
        self.assertIsNone(book["vector_coverage"])

    def test_reconciles_with_corpus_status(self):
        # B6: the snapshot's per-work passages/coverage are computed
        # identically to the --corpus-status read model.
        self.seed_rich()
        status = pp._corpus_status_data(self.dir)
        data = pp._corpus_snapshot_data(self.dir)
        books = {b["fingerprint"]: b for b in data["books"]}
        for w in status["works"]:
            book = books[w["fingerprint"]]
            self.assertEqual(book["passages"], w["passages"])
            self.assertEqual(book["vector_coverage"],
                             w["vector_coverage"])
        self.assertEqual(data["header"]["totals"]["quotes"],
                         status["totals"]["quotes"])
        self.assertEqual(data["header"]["totals"]["quotes_by_status"],
                         status["totals"]["quotes_by_status"])


class GenerationProjectionTests(_Base):
    def test_projection_is_exactly_six_keys(self):
        self.seed_rich()
        data = pp._corpus_snapshot_data(self.dir)
        h = data["header"]
        self.assertEqual(h["current_generation"], {
            "model": CUR_MODEL, "format": FMT, "dim": 3,
            "canary": "ok", "born": "2026-01-01T00:00:00+00:00",
            "checked": "2026-01-02T00:00:00+00:00"})
        self.assertEqual(len(h["generations"]), 2)
        for g in h["generations"]:
            self.assertEqual(set(g), {"model", "format", "dim",
                                      "canary", "born", "checked"})
        blob = json.dumps(data, ensure_ascii=False, default=str)
        self.assertNotIn("sentinels", blob)
        self.assertNotIn("cosines", blob)
        self.assertNotIn(str(SENTINEL_FLOAT), blob)


class DenylistTests(_Base):
    def test_hostile_corpus_ships_nothing_forbidden(self):
        self.seed_rich()
        data = pp._corpus_snapshot_data(self.dir)
        payloads = [data["header"]] + data["books"]
        self.assertEqual(pp._history_denylist_scan(payloads), [])
        blob = json.dumps(payloads, ensure_ascii=False, default=str)
        for forbidden in (
                "source_name", "asin", "B00ASINXXX", "B08XXXXXXX",
                "someone@example.com", "doi.org", "Alpha Book.epub",
                "Beta.epub", "velvet hammers", "planted quote text",
                "retracted because", "series_source"):
            self.assertNotIn(forbidden, blob)
        # No raw containers anywhere, at any depth.
        banned_keys = {"source_name", "asin", "raw", "sentinels",
                       "cosines", "payload", "envelope", "dir",
                       "path"}

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    self.assertNotIn(k, banned_keys)
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(payloads)


class FingerprintTests(_Base):
    def test_deterministic_across_capture_times(self):
        self.seed_rich()
        d1 = pp._corpus_snapshot_data(self.dir)
        d2 = pp._corpus_snapshot_data(self.dir)
        self.assertNotEqual(d1["header"]["snapshot_key"],
                            d2["header"]["snapshot_key"])
        self.assertEqual(d1["header"]["corpus_fingerprint"],
                         d2["header"]["corpus_fingerprint"])

    def test_changes_with_source_hash(self):
        self.seed_rich()
        before = pp._corpus_snapshot_data(
            self.dir)["header"]["corpus_fingerprint"]
        db = pp.CorpusDB.open(self.dir, mode="importer")
        with db.write_txn() as conn:
            conn.execute(
                "UPDATE corpus_works SET source_hash='src-changed' "
                "WHERE fingerprint='fp-beta'")
        after = pp._corpus_snapshot_data(
            self.dir)["header"]["corpus_fingerprint"]
        self.assertNotEqual(before, after)

    def test_function_level_properties(self):
        gen = {"model": "m", "format": "f"}
        books = [{"fingerprint": "b", "status": "complete",
                  "parser_version": "1", "text_root_hash": "r2",
                  "source_hash": "s2", "vectors": None},
                 {"fingerprint": "a", "status": "complete",
                  "parser_version": "1", "text_root_hash": "r1",
                  "source_hash": "s1", "vectors": 3}]
        fp1 = pp._corpus_snapshot_fingerprint(gen, books)
        # Input order does not matter; tuples sort by fingerprint.
        fp2 = pp._corpus_snapshot_fingerprint(
            gen, list(reversed(books)))
        self.assertEqual(fp1, fp2)
        changed = [dict(b) for b in books]
        changed[0]["vectors"] = 4
        self.assertNotEqual(
            fp1, pp._corpus_snapshot_fingerprint(gen, changed))
        self.assertNotEqual(
            fp1, pp._corpus_snapshot_fingerprint(None, books))


class FakeSnapshotSB:
    """Recording stand-in for pp.sb over the two snapshot tables."""

    def __init__(self):
        self.calls = []
        self.header_mode = "ok"     # ok | none | empty
        self.probe_rows = None      # GET snapshot_key result
        self.book_mode = "ok"       # ok | none | short
        self.patch_ok = True
        self.delete_ok = True

    def __call__(self, table, method="GET", data=None, params="",
                 prefer=None, schema=None):
        self.calls.append({"table": table, "method": method,
                           "data": data, "params": params,
                           "prefer": prefer, "schema": schema})
        if table == "processor_corpus_snapshots":
            if method == "POST":
                if self.header_mode == "none":
                    return None
                if self.header_mode == "empty":
                    return []
                return [dict(data[0], id=HEADER_ID)]
            if method == "GET":
                return self.probe_rows
            if method == "PATCH":
                return ([{"id": HEADER_ID, "complete": True}]
                        if self.patch_ok else None)
            if method == "DELETE":
                return [{"id": HEADER_ID}] if self.delete_ok else []
        if (table == "processor_corpus_snapshot_books"
                and method == "POST"):
            if self.book_mode == "none":
                return None
            if self.book_mode == "short":
                return [dict(r) for r in data[:-1]]
            return [dict(r, id=f"book-{i}")
                    for i, r in enumerate(data)]
        return []

    def by(self, table, method):
        return [c for c in self.calls
                if c["table"] == table and c["method"] == method]


class TransportTests(_Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeSnapshotSB()
        self._sb = mock.patch.object(pp, "sb", self.fake)
        self._sb.start()

    def tearDown(self):
        self._sb.stop()
        super().tearDown()

    def run_cmd(self, as_json=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pp.cmd_corpus_snapshot(self.dir, as_json=as_json)
        return rc, out.getvalue()

    def test_success_order_and_finalize(self):
        self.seed_rich()
        rc, out = self.run_cmd()
        self.assertEqual(rc, 0)
        self.assertEqual(
            [(c["table"], c["method"]) for c in self.fake.calls],
            [("processor_corpus_snapshots", "POST"),
             ("processor_corpus_snapshot_books", "POST"),
             ("processor_corpus_snapshots", "PATCH")])
        for c in self.fake.calls:
            self.assertEqual(c["schema"], "processor")
        header_post = self.fake.calls[0]["data"][0]
        self.assertFalse(header_post["complete"])
        self.assertEqual(header_post["machine_label"], "Lazydog")
        self.assertEqual(header_post["works_count"], 3)
        book_post = self.fake.calls[1]["data"]
        self.assertEqual(len(book_post), 3)
        for b in book_post:
            self.assertEqual(b["snapshot_id"], HEADER_ID)
        patch = self.fake.calls[2]
        self.assertEqual(patch["data"], {"complete": True})
        self.assertEqual(patch["params"], f"?id=eq.{HEADER_ID}")
        self.assertIn(header_post["snapshot_key"], out)
        self.assertIn("3 works", out)

    def test_books_chunk_at_200(self):
        db = pp.CorpusDB.create(self.dir, mode="importer")
        with db.write_txn() as conn:
            for i in range(205):
                _insert_work(conn, _work_row(f"fp-{i:04d}"))
        rc, _ = self.run_cmd()
        self.assertEqual(rc, 0)
        posts = self.fake.by("processor_corpus_snapshot_books", "POST")
        self.assertEqual([len(c["data"]) for c in posts], [200, 5])
        self.assertEqual(
            len(self.fake.by("processor_corpus_snapshots", "PATCH")),
            1)

    def test_failed_chunk_deletes_header(self):
        self.seed_rich()
        self.fake.book_mode = "none"
        rc, out = self.run_cmd()
        self.assertEqual(rc, 1)
        deletes = self.fake.by("processor_corpus_snapshots", "DELETE")
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["params"], f"?id=eq.{HEADER_ID}")
        self.assertEqual(
            self.fake.by("processor_corpus_snapshots", "PATCH"), [])
        self.assertIn("no partial snapshot", out)

    def test_short_chunk_deletes_header(self):
        self.seed_rich()
        self.fake.book_mode = "short"
        rc, _ = self.run_cmd()
        self.assertEqual(rc, 1)
        self.assertEqual(
            len(self.fake.by("processor_corpus_snapshots", "DELETE")),
            1)

    def test_unconfirmed_delete_names_the_key(self):
        self.seed_rich()
        self.fake.book_mode = "none"
        self.fake.delete_ok = False
        rc, out = self.run_cmd()
        self.assertEqual(rc, 1)
        header_post = self.fake.by(
            "processor_corpus_snapshots", "POST")[0]["data"][0]
        self.assertIn(header_post["snapshot_key"], out)
        self.assertIn("could not be confirmed", out)

    def test_finalize_failure_deletes_header(self):
        self.seed_rich()
        self.fake.patch_ok = False
        rc, _ = self.run_cmd()
        self.assertEqual(rc, 1)
        self.assertEqual(
            len(self.fake.by("processor_corpus_snapshots", "DELETE")),
            1)

    def test_header_no_response_probes_and_deletes(self):
        self.seed_rich()
        self.fake.header_mode = "none"
        self.fake.probe_rows = [{"id": HEADER_ID}]
        rc, out = self.run_cmd()
        self.assertEqual(rc, 1)
        gets = self.fake.by("processor_corpus_snapshots", "GET")
        self.assertEqual(len(gets), 1)
        self.assertIn("snapshot_key=eq.", gets[0]["params"])
        self.assertEqual(
            len(self.fake.by("processor_corpus_snapshots", "DELETE")),
            1)
        self.assertIn("landed header", out)

    def test_header_no_response_nothing_landed(self):
        self.seed_rich()
        self.fake.header_mode = "none"
        self.fake.probe_rows = []
        rc, out = self.run_cmd()
        self.assertEqual(rc, 1)
        self.assertEqual(
            self.fake.by("processor_corpus_snapshots", "DELETE"), [])
        self.assertIn("no landed header", out)

    def test_header_no_response_unconfirmable(self):
        self.seed_rich()
        self.fake.header_mode = "none"
        self.fake.probe_rows = None     # the probe itself gets nothing
        rc, out = self.run_cmd()
        self.assertEqual(rc, 1)
        header_post = self.fake.by(
            "processor_corpus_snapshots", "POST")[0]["data"][0]
        self.assertIn(header_post["snapshot_key"], out)
        self.assertIn("could not be confirmed", out)

    def test_missing_config_makes_zero_calls(self):
        self.seed_rich()
        pp.CONFIG["supabase_url"] = ""
        pp.CONFIG["supabase_key"] = ""
        rc, _ = self.run_cmd()
        self.assertEqual(rc, 2)
        self.assertEqual(self.fake.calls, [])

    def test_json_receipt_is_compact_and_minimal(self):
        self.seed_rich()
        rc, out = self.run_cmd(as_json=True)
        self.assertEqual(rc, 0)
        receipt = json.loads(out)
        self.assertEqual(set(receipt), {"snapshot_key", "captured_at",
                                        "works",
                                        "corpus_fingerprint"})
        self.assertEqual(receipt["works"], 3)
        header_post = self.fake.by(
            "processor_corpus_snapshots", "POST")[0]["data"][0]
        self.assertEqual(receipt["snapshot_key"],
                         header_post["snapshot_key"])
        self.assertEqual(receipt["corpus_fingerprint"],
                         header_post["corpus_fingerprint"])
        self.assertNotIn("\n", out.strip())


class AbsentEmptyCorruptTests(_Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeSnapshotSB()
        self._sb = mock.patch.object(pp, "sb", self.fake)
        self._sb.start()

    def tearDown(self):
        self._sb.stop()
        super().tearDown()

    def run_cmd(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pp.cmd_corpus_snapshot(self.dir)
        return rc, out.getvalue()

    def test_absent_corpus(self):
        rc, out = self.run_cmd()
        self.assertEqual(rc, 2)
        self.assertIn("nothing to snapshot", out)
        self.assertEqual(self.fake.calls, [])

    def test_empty_corpus_is_a_valid_snapshot(self):
        pp.CorpusDB.create(self.dir, mode="importer")
        pp._close_corpus_dbs()
        rc, _ = self.run_cmd()
        self.assertEqual(rc, 0)
        header_post = self.fake.by(
            "processor_corpus_snapshots", "POST")[0]
        self.assertEqual(header_post["data"][0]["works_count"], 0)
        self.assertEqual(
            self.fake.by("processor_corpus_snapshot_books", "POST"),
            [])
        self.assertEqual(
            len(self.fake.by("processor_corpus_snapshots", "PATCH")),
            1)

    def test_corrupt_corpus_is_unavailable_not_absent(self):
        pp.CorpusDB.create(self.dir, mode="importer")
        pp._close_corpus_dbs()
        (self.dir / "corpus.db").write_bytes(
            b"this is not a sqlite database" * 64)
        rc, out = self.run_cmd()
        self.assertEqual(rc, 2)
        self.assertIn("unavailable", out)
        self.assertNotIn("nothing to snapshot", out)
        self.assertEqual(self.fake.calls, [])
        self.assertTrue((self.dir / "corpus.db.corrupt").exists())


class CliTests(_Base):
    def test_parser_accepts_flag(self):
        args = pp._build_parser().parse_args([])
        self.assertFalse(args.corpus_snapshot)
        args = pp._build_parser().parse_args(["--corpus-snapshot"])
        self.assertTrue(args.corpus_snapshot)

    def test_main_dispatches_without_arming_run_history(self):
        pp.CONFIG["run_history"] = False
        with mock.patch.object(pp, "cmd_corpus_snapshot",
                               return_value=7) as cmd:
            with mock.patch.object(sys, "argv",
                                   ["proc", "--corpus-snapshot"]):
                with self.assertRaises(SystemExit) as cm:
                    pp.main()
        self.assertEqual(cm.exception.code, 7)
        cmd.assert_called_once()
        self.assertFalse(pp.CONFIG["run_history"])


class ImmutabilityTests(_Base):
    def test_snapshot_does_not_mutate_the_corpus(self):
        db = self.seed_rich()
        before = db.dump_hash()
        pp._corpus_snapshot_data(self.dir)
        self.assertEqual(db.dump_hash(), before)
        fake = FakeSnapshotSB()
        with mock.patch.object(pp, "sb", fake):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = pp.cmd_corpus_snapshot(self.dir)
        self.assertEqual(rc, 0)
        self.assertEqual(db.dump_hash(), before)


if __name__ == "__main__":
    unittest.main()
