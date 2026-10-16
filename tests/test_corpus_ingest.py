"""Corpus store, slice 2: ingest phase 1 (eval/corpus-design.md §3,
§1.6). Pins: the one-parse-path guarantee (corpus text IS what a run
would parse, by construction), the §3.2 losslessness self-check, the
§3.3 completeness state machine (a half-ingested book never reads as
whole), A9 bookkeeping-before-phase-1, A7's FTS integrity gate, A10's
full-scope removal, and idempotent re-ingest.
"""
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _xhtml(body):
    return ("<?xml version='1.0' encoding='utf-8'?>\n"
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            + body + "</body></html>")


def _make_epub(path, files, title="Corpus Fixture", author="Test Author",
               isbn="9780306406157", series=None, series_index=None):
    manifest = "\n".join(
        f'    <item id="i{n}" href="{name.split("/", 1)[1]}" '
        'media-type="application/xhtml+xml"/>'
        for n, name in enumerate(files))
    spine = "\n".join(
        f'    <itemref idref="i{n}"/>' for n in files)
    extra_meta = ""
    if series:
        extra_meta = (
            f'\n    <meta name="calibre:series" content="{series}"/>'
            f'\n    <meta name="calibre:series_index" '
            f'content="{series_index}"/>')
    opf = ("<?xml version='1.0' encoding='utf-8'?>\n"
           "<package xmlns='http://www.idpf.org/2007/opf' version='3.0' "
           "unique-identifier='bid'>\n"
           "  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>\n"
           f"    <dc:title>{title}</dc:title>\n"
           f"    <dc:creator>{author}</dc:creator>\n"
           f"    <dc:identifier>{isbn}</dc:identifier>"
           + extra_meta + "\n  </metadata>\n  <manifest>\n" + manifest
           + "\n  </manifest>\n  <spine>\n" + spine
           + "\n  </spine>\n</package>")
    container = ("<?xml version='1.0' encoding='utf-8'?>\n"
                 "<container xmlns='urn:oasis:names:tc:opendocument:"
                 "xmlns:container' version='1.0'>\n"
                 "  <rootfiles><rootfile full-path='OEBPS/content.opf'/>"
                 "</rootfiles>\n</container>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, content in files.items():
            z.writestr(name, content)
    return path


_STORY = ("Mira Solace drew her sword and faced the dawn over the "
          "valley while the river ran cold below. ")
_VELVET = ("At noon the velvet hammer falls upon the anvil of the "
           "old smithy, and the whole village hears it ring. ")
_ROAD = ("The road wound on through pine and stone while the company "
         "argued about maps, meals and old debts. ")


def _fixture_files(velvet=True, chapter3_extra="", bell=True):
    dedication = ("<h1>Dedication</h1><p>The ZQXJKV marker sentence "
                  "stands here.</p>" + "<p>For Anna, who believed in "
                  "this story when no one else did, thank you for "
                  "every late night and kind word.</p>" * 18)
    ch1 = ("<h1>Chapter 1</h1><p>" + _STORY * 40 + "</p>"
           + ("<p>" + _VELVET * 12 + "</p>" if velvet else ""))
    ch2 = "<h1>Chapter 2</h1><p>" + _ROAD * 210 + "</p>"  # >16k: splits
    ch3 = ("<h1>Chapter 3</h1><p>" + _STORY * 30 + chapter3_extra
           + "</p>" + ("<p>The iron bell of the harbour tolled "
                       "across the grey water at first light.</p>" * 8
                      if bell else ""))
    return {"OEBPS/p01.xhtml": _xhtml(dedication),
            "OEBPS/c1.xhtml": _xhtml(ch1),
            "OEBPS/c2.xhtml": _xhtml(ch2),
            "OEBPS/c3.xhtml": _xhtml(ch3)}


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.learn = self.dir / "learned"
        self.learn.mkdir()

    def tearDown(self):
        pp._close_corpus_dbs()
        pp._close_state_dbs()
        self._td.cleanup()

    def make_book(self, name="fixture.epub", **kw):
        return _make_epub(self.dir / name, _fixture_files(**kw))

    def make_db(self):
        return pp.CorpusDB.create(self.learn, mode="importer")

    def ingest(self, db, path, **kw):
        return pp.corpus_ingest_book(db, Path(path), **kw)


class TestIngestLossless(_Base):
    def test_corpus_roundtrip_chapter_text(self):
        # §1.6: every chapter read back hashes to its stored
        # text_hash; the root hash equals a fresh parse's.
        book = self.make_book()
        db = self.make_db()
        res = self.ingest(db, book)
        self.assertEqual(res["outcome"], "partial")  # phase 1 only
        fp = res["fingerprint"]
        units, _t, _a, _ids = pp.extract_epub_units(book)
        pp._DIAG.chapter_detection_fallback = False
        fresh = pp.split_chapters(units)
        rows = db.query(
            "SELECT chapter_index, label, text, text_hash, "
            "is_frontmatter FROM corpus_chapters WHERE fingerprint=? "
            "ORDER BY chapter_index", (fp,))
        self.assertEqual(len(rows), len(fresh))
        hashes = []
        for row, ch in zip(rows, fresh):
            self.assertEqual(row[0], ch["index"])
            self.assertEqual(row[1], ch["label"])
            self.assertEqual(row[2], ch["text"])
            h = hashlib.sha256(ch["text"].encode("utf-8")).hexdigest()
            self.assertEqual(row[3], h)
            self.assertEqual(bool(row[4]), bool(ch.get("is_frontmatter")))
            hashes.append(h)
        work = db.query(
            "SELECT text_root_hash, chapter_count, status FROM "
            "corpus_works WHERE fingerprint=?", (fp,))[0]
        self.assertEqual(work[0], pp._corpus_root_hash(hashes))
        self.assertEqual(work[1], len(fresh))
        self.assertEqual(work[2], "text_complete")
        # The frontmatter chapter survived and is tagged.
        self.assertTrue(any(r[4] for r in rows))

    def test_passage_slice_identity(self):
        # §1.2/§1.6 pin: every stored span, re-sliced from its
        # chapter, hashes to the passage's text_hash (100%, A2's
        # ingest-side shape).
        book = self.make_book()
        db = self.make_db()
        res = self.ingest(db, book)
        fp = res["fingerprint"]
        chapters = {r[0]: r[1] for r in db.query(
            "SELECT chapter_index, text FROM corpus_chapters "
            "WHERE fingerprint=?", (fp,))}
        passages = db.query(
            "SELECT chapter_index, start_offset, end_offset, "
            "text_hash FROM corpus_passages WHERE fingerprint=?",
            (fp,))
        self.assertGreater(len(passages), 50)
        for ci, start, end, th in passages:
            span = chapters[ci][start:end]
            self.assertEqual(
                hashlib.sha256(span.encode("utf-8")).hexdigest(), th)

    def test_ingest_records_full_acquisition_surface(self):
        # §3.1: the ingest record carries the fallback flag and the
        # run's word_count formula, so a corpus-sourced run can
        # reproduce the :acquisition block's full output surface.
        book = self.make_book()
        db = self.make_db()
        res = self.ingest(db, book)
        payload = json.loads(db.query(
            "SELECT payload FROM corpus_works WHERE fingerprint=?",
            (res["fingerprint"],))[0][0])
        self.assertIn("chapter_detection_fallback", payload)
        units, _t, _a, _ids = pp.extract_epub_units(book)
        self.assertEqual(payload["word_count"],
                         sum(len(u["text"].split()) for u in units))
        self.assertEqual(payload["source_name"], "fixture.epub")

    def test_ingest_series_from_calibre_metadata(self):
        book = _make_epub(self.dir / "saga.epub", _fixture_files(),
                          series="Fixture Saga", series_index=2)
        db = self.make_db()
        res = self.ingest(db, book)
        row = db.query(
            "SELECT series, series_index FROM corpus_works "
            "WHERE fingerprint=?", (res["fingerprint"],))[0]
        self.assertEqual(row[0], "Fixture Saga")
        self.assertEqual(row[1], 2.0)


class TestFallbackFixture(_Base):
    def test_single_file_book_uses_fallback_split(self):
        # §3.5 fixture shape: one spine item holding the whole book
        # -> heading fallback split; the flag lands in the payload.
        body = "".join(
            f"<h1>Chapter {k}</h1><p>" + _STORY * 40 + "</p>"
            for k in range(1, 4))
        book = _make_epub(self.dir / "onefile.epub",
                          {"OEBPS/all.xhtml": _xhtml(body)})
        db = self.make_db()
        res = self.ingest(db, book)
        self.assertEqual(res["outcome"], "partial")
        payload = json.loads(db.query(
            "SELECT payload FROM corpus_works WHERE fingerprint=?",
            (res["fingerprint"],))[0][0])
        self.assertTrue(payload["chapter_detection_fallback"])
        self.assertGreaterEqual(res["chapters"], 3)


class TestIdempotencyAndState(_Base):
    def test_ingest_idempotent_noop(self):
        # §3.5: second ingest writes nothing (dump-hash identical)
        # and reports noop.
        book = self.make_book()
        db = self.make_db()
        self.ingest(db, book)
        h = db.dump_hash()
        res = self.ingest(db, book)
        self.assertEqual(res["outcome"], "noop")
        self.assertEqual(db.dump_hash(), h)

    def test_corpus_ids_stable_across_reingest(self):
        # §1.6: same file, same versions, forced re-ingest ->
        # identical passage id set.
        book = self.make_book()
        db = self.make_db()
        res = self.ingest(db, book)
        fp = res["fingerprint"]
        ids1 = {r[0] for r in db.query(
            "SELECT passage_id FROM corpus_passages "
            "WHERE fingerprint=?", (fp,))}
        res2 = self.ingest(db, book, force=True)
        self.assertNotEqual(res2["outcome"], "noop")
        ids2 = {r[0] for r in db.query(
            "SELECT passage_id FROM corpus_passages "
            "WHERE fingerprint=?", (fp,))}
        self.assertEqual(ids1, ids2)

    def test_half_ingested_never_read_as_complete(self):
        # §3.5/§3.3: consumers gate on status by family.
        book = self.make_book()
        db = self.make_db()
        res = self.ingest(db, book)
        fp = res["fingerprint"]
        with db.write_txn("text") as conn:
            conn.execute(
                "UPDATE corpus_works SET status='vectors_partial' "
                "WHERE fingerprint=?", (fp,))
        self.assertIsNotNone(pp.corpus_chapters_for(db, fp))
        self.assertFalse(pp.corpus_retrieval_ready(db, fp))
        with db.write_txn("text") as conn:
            conn.execute(
                "UPDATE corpus_works SET status='ingesting' "
                "WHERE fingerprint=?", (fp,))
        self.assertIsNone(pp.corpus_chapters_for(db, fp))
        self.assertEqual(pp.corpus_work_state(db, fp), "ingesting")
        with db.write_txn("text") as conn:
            conn.execute(
                "UPDATE corpus_works SET status='complete' "
                "WHERE fingerprint=?", (fp,))
        self.assertTrue(pp.corpus_retrieval_ready(db, fp))

    def test_parser_bump_stales_and_replaces(self):
        # §3.5: version bump => stale composition; re-ingest =>
        # new passage population, old vectors gone, supersession
        # recorded in ingest_runs.
        book = self.make_book()
        db = self.make_db()
        res = self.ingest(db, book)
        fp = res["fingerprint"]
        old_ids = {r[0] for r in db.query(
            "SELECT passage_id FROM corpus_passages "
            "WHERE fingerprint=?", (fp,))}
        some_pid = next(iter(old_ids))
        with db.write_txn("vectors") as conn:
            conn.execute(
                "INSERT INTO passage_vectors (passage_id, "
                "fingerprint, model, format, dim, vector) VALUES "
                "(?, ?, 'm', 'raw-f32-v1', 1, X'0000')",
                (some_pid, fp))
        saved = pp.CORPUS_PARSER_VERSION
        pp.CORPUS_PARSER_VERSION = "2"
        try:
            self.assertEqual(pp.corpus_work_state(db, fp), "stale")
            self.assertIsNone(pp.corpus_chapters_for(db, fp))
            # Edit a late chapter (fingerprint samples the first
            # substantial chapter; identity must survive).
            book2 = _make_epub(
                self.dir / "fixture.epub",
                _fixture_files(chapter3_extra=" A brand new final "
                               "sentence changes this chapter."))
            res2 = self.ingest(db, book2)
        finally:
            pp.CORPUS_PARSER_VERSION = saved
        self.assertEqual(res2["fingerprint"], fp)
        self.assertEqual(res2["outcome"], "partial")
        new_ids = {r[0] for r in db.query(
            "SELECT passage_id FROM corpus_passages "
            "WHERE fingerprint=?", (fp,))}
        self.assertNotEqual(old_ids, new_ids)
        self.assertEqual(db.query(
            "SELECT COUNT(*) FROM passage_vectors "
            "WHERE fingerprint=?", (fp,))[0][0], 0)
        runs = db.query(
            "SELECT detail FROM ingest_runs WHERE fingerprint=?",
            (fp,))
        self.assertTrue(any("supersede" in (r[0] or "") for r in runs))
        row = db.query(
            "SELECT parser_version FROM corpus_works "
            "WHERE fingerprint=?", (fp,))[0]
        self.assertEqual(row[0], "2")


class TestBookkeepingAndGates(_Base):
    def test_ingest_bookkeeping_survives_crash(self):
        # §3.5/A9: bookkeeping commits BEFORE phase 1; a crash in
        # between leaves the 'ingesting' row observable, and the
        # next ingest re-enters cleanly.
        book = self.make_book()
        db = self.make_db()
        parsed = pp._corpus_parse_book(book)
        fp = pp._checkpoint_fingerprint(
            parsed["identifiers"], parsed["title"], parsed["author"],
            pp._checkpoint_text_sample(parsed["chapters"]))
        pp._corpus_ingest_bookkeeping(
            db, parsed, fp, "a" * 64, reason="crash drill")
        self.assertEqual(pp.corpus_work_state(db, fp), "ingesting")
        self.assertIn("ingesting", pp._corpus_status_text(self.learn))
        res = self.ingest(db, book)
        self.assertEqual(res["outcome"], "partial")
        self.assertEqual(pp.corpus_work_state(db, fp), "text_complete")

    def test_phase1_failure_marks_failed_and_stores_nothing(self):
        book = self.make_book()
        db = self.make_db()
        orig = pp._corpus_phase1_write
        pp._corpus_phase1_write = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("simulated phase-1 death"))
        try:
            res = self.ingest(db, book)
        finally:
            pp._corpus_phase1_write = orig
        self.assertEqual(res["outcome"], "failed")
        fp = res["fingerprint"]
        self.assertEqual(pp.corpus_work_state(db, fp), "failed")
        self.assertEqual(db.query(
            "SELECT COUNT(*) FROM corpus_chapters "
            "WHERE fingerprint=?", (fp,))[0][0], 0)
        run = db.query(
            "SELECT outcome FROM ingest_runs WHERE fingerprint=?",
            (fp,))[0]
        self.assertEqual(run[0], "failed")

    def test_fts_integrity_check_gates_ingest(self):
        # §3.5/A7: a corrupted FTS index at self-check => ingest
        # failed, no text_complete flip, no committed chapters.
        book = self.make_book()
        db = self.make_db()
        orig = pp._corpus_fts_integrity_ok
        pp._corpus_fts_integrity_ok = lambda conn: False
        try:
            res = self.ingest(db, book)
        finally:
            pp._corpus_fts_integrity_ok = orig
        self.assertEqual(res["outcome"], "failed")
        fp = res["fingerprint"]
        self.assertNotEqual(pp.corpus_work_state(db, fp),
                            "text_complete")
        self.assertEqual(db.query(
            "SELECT COUNT(*) FROM corpus_chapters "
            "WHERE fingerprint=?", (fp,))[0][0], 0)

    def test_fts_reingest_finds_new_not_removed(self):
        # §3.5/§1.3 pin: after a re-ingest that removes text, FTS
        # finds the new generation's text and not the removed text
        # (triggers keep the index honest through the rewrite).
        # The edit lands in Chapter 3: the fingerprint samples
        # Chapter 1, so the work's identity survives the edit —
        # this is a generation replacement, not a new work.
        book = self.make_book()
        db = self.make_db()
        res = self.ingest(db, book)
        fp = res["fingerprint"]
        velvet_ci = db.query(
            "SELECT chapter_index FROM corpus_chapters WHERE "
            "fingerprint=? AND text LIKE '%velvet hammer%'",
            (fp,))[0][0]
        self.assertEqual(db.fts_search_chapters("velvet hammer"),
                         [(fp, velvet_ci)])
        self.assertTrue(db.fts_search_chapters("iron bell"))
        book2 = _make_epub(self.dir / "fixture.epub",
                           _fixture_files(bell=False))
        res2 = self.ingest(db, book2)
        self.assertEqual(res2["fingerprint"], fp)
        self.assertEqual(db.fts_search_chapters("iron bell"), [])
        self.assertEqual(db.fts_search_chapters("velvet hammer"),
                         [(fp, velvet_ci)])


class TestRemoveAndDir(_Base):
    def test_corpus_remove_full_scope(self):
        # A10: one transaction across every fingerprint-keyed
        # corpus table, counts reported; other works untouched.
        book = self.make_book()
        other = _make_epub(self.dir / "other.epub", _fixture_files(),
                           title="Other Book", isbn="9780525951623")
        db = self.make_db()
        fp = self.ingest(db, book)["fingerprint"]
        fp2 = self.ingest(db, other)["fingerprint"]
        pp._close_corpus_dbs()
        db = pp.CorpusDB.open(self.learn, mode="processor")
        with db.write_txn("stages") as conn:
            conn.execute(
                "INSERT INTO stage_outputs (fingerprint, stage, "
                "output_key, payload) VALUES (?, 'retrieve', 'k', "
                "'{}')", (fp,))
            conn.execute(
                "INSERT INTO quotes (quote_id, fingerprint, kind, "
                "status) VALUES ('q1', ?, 'signature', 'selected')",
                (fp,))
        # (the work_envelope row already exists — ingest's phase 1
        # refresh wrote it; removal must take it too.)
        counts = pp.corpus_remove_work(db, fp)
        self.assertGreater(counts["corpus_works"], 0)
        self.assertGreater(counts["corpus_chapters"], 0)
        self.assertGreater(counts["stage_outputs"], 0)
        self.assertGreater(counts["quotes"], 0)
        self.assertGreater(counts["work_envelope"], 0)
        for t in ("corpus_works", "corpus_chapters", "corpus_passages",
                  "stage_outputs", "quotes", "work_envelope"):
            self.assertEqual(db.query(
                f"SELECT COUNT(*) FROM {t} WHERE fingerprint=?",
                (fp,))[0][0], 0, t)
        self.assertEqual(pp.corpus_work_state(db, fp2),
                         "text_complete")

    def test_ingest_dir_isolates_failures(self):
        good = self.make_book("good.epub")
        bad = self.dir / "bad.epub"
        bad.write_bytes(b"not an epub at all" * 100)
        (self.dir / "notes.txt").write_text("ignored")
        results = pp.cmd_ingest([self.dir], learn_dir=self.learn)
        by_name = {r["source_name"]: r for r in results}
        self.assertEqual(by_name["good.epub"]["outcome"], "partial")
        self.assertEqual(by_name["bad.epub"]["outcome"], "failed")
        self.assertNotIn("notes.txt", by_name)


if __name__ == "__main__":
    unittest.main()
