"""Corpus slice 5: corpus-sourced runs + snapshot/replay
composition (corpus-design §4.3, §11 slice 5, §13 signals 2a/2b).

Pins:
- A corpus-sourced run reproduces the acquisition block's FULL
  output surface (§3.1's mirror): the same chapters in the same
  order, the same word_count, the same chapter-detection fallback
  flag and diagnostics inputs — read from the corpus, never
  re-parsed (§13 signal 2a at the seam).
- Sourcing is gated: no corpus, an uningested book, or a stale
  generation all fall back to the file; --use-corpus is default
  OFF, so flag-off runs never touch the corpus path.
- A snapshot's manifest gains the corpus block (fingerprint,
  parser version, text root hash, per-chapter hashes) computed
  from the CAPTURE RUN's own parse — never read back from corpus
  rows (§4.3).
- Replay validates the block against the live corpus by hash: a
  match pins READ-ONLY corpus access for the replay (retrieval
  context present, writable False, gap-fill flush lands nothing,
  the live corpus dump hash unchanged across the replay — §4.5's
  test_replay_corpus_ref_validates_by_hash and
  test_replay_corpus_access_read_only); a mismatch refuses the
  corpus path for that replay and falls back.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                              _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_corpus_ingest import _make_epub, _fixture_files  # noqa: E402
from test_corpus_stages import _g_files  # noqa: E402
from test_evidence_retrieval import FakeEmbedder  # noqa: E402


class _FakeBackend:
    def __call__(self, prompt, chunk, i, n):
        if prompt == pp.PROMPT_V2_CONTENT:
            return json.dumps({
                "summary": "A chapter summary.", "spice_level": 0,
                "triggers": {}, "trigger_evidence": {},
                "trope_candidates": [], "quotes": []})
        return json.dumps({
            "characters": [{"name": "Mira Solace",
                            "role": "protagonist",
                            "description": "A swordswoman.",
                            "evidence": "Mira Solace drew her "
                                        "sword."}],
            "relationships": [], "pov_character": "Mira Solace"})


class _SourcingBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.learn = self.dir / "learned"
        self.learn.mkdir()
        self.book = _make_epub(self.dir / "corpus.epub",
                               _fixture_files())
        self._saved = (pp._LEARNED, pp._REPLAY_CTX, pp._CAPTURE_CTX,
                       pp._checkpoint_dir)
        pp._PENDING_CAPTURE_HASHES.clear()

    def tearDown(self):
        (pp._LEARNED, pp._REPLAY_CTX, pp._CAPTURE_CTX,
         pp._checkpoint_dir) = self._saved
        pp._PENDING_CAPTURE_HASHES.clear()
        pp._close_corpus_dbs()
        pp._close_state_dbs()
        self._td.cleanup()

    def _ingest(self):
        db = pp.CorpusDB.create(self.learn, mode="importer")
        res = pp.corpus_ingest_book(db, self.book)
        self.assertIn(res["outcome"], ("partial", "complete"))
        return res["fingerprint"]


class TestAcquireSurface(_SourcingBase):
    def test_surface_matches_file_parse(self):
        fp = self._ingest()
        parsed = pp._corpus_parse_book(self.book)
        acq = pp.corpus_acquire_for_run(self.learn, self.book)
        self.assertIsNotNone(acq)
        self.assertEqual(acq["fingerprint"], fp)
        self.assertEqual([c["text"] for c in acq["chapters"]],
                         [c["text"] for c in parsed["chapters"]])
        self.assertEqual([c["index"] for c in acq["chapters"]],
                         [c["index"] for c in parsed["chapters"]])
        self.assertEqual([c["label"] for c in acq["chapters"]],
                         [c["label"] for c in parsed["chapters"]])
        self.assertEqual(
            [pp._corpus_text_hash(c["text"]) for c in acq["chapters"]],
            [pp._corpus_text_hash(c["text"])
             for c in parsed["chapters"]])
        self.assertEqual(acq["word_count"], parsed["word_count"])
        self.assertEqual(acq["chapter_detection_fallback"],
                         parsed["chapter_detection_fallback"])
        self.assertEqual(acq["title"], parsed["title"])
        self.assertEqual(acq["author"], parsed["author"])

    def test_no_corpus_no_source(self):
        self.assertIsNone(
            pp.corpus_acquire_for_run(self.learn, self.book))

    def test_uningested_book_no_source(self):
        self._ingest()
        other = _make_epub(self.dir / "other.epub", _fixture_files(),
                           title="Another Book Entirely")
        self.assertIsNone(pp.corpus_acquire_for_run(self.learn, other))

    def test_stale_generation_not_sourced(self):
        self._ingest()
        db = pp._corpus_db_for(self.learn, mode="importer")
        with db.write_txn() as conn:
            conn.execute(
                "UPDATE corpus_works SET parser_version='0'")
        self.assertIsNone(
            pp.corpus_acquire_for_run(self.learn, self.book))


class TestUseCorpusWiring(_SourcingBase):
    def _run_preview(self, use_corpus):
        fp = self._ingest()
        pp._close_corpus_dbs()
        pp._LEARNED = pp.LearnedState(learn_dir=self.learn,
                                      enabled=True)
        calls = []
        real_split = pp.split_chapters

        def counting_split(units):
            calls.append(1)
            return real_split(units)

        preview_json = (pp.SCRIPT_DIR / "preview"
                        / "corpus.json")
        self.addCleanup(
            lambda: preview_json.unlink(missing_ok=True))
        self.addCleanup(
            lambda: (pp.SCRIPT_DIR / "preview" / "corpus.md")
            .unlink(missing_ok=True))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            pp._checkpoint_dir(
                pp._checkpoint_book_key(self.book)),
            ignore_errors=True))
        with mock.patch.object(pp, "llm_ollama", _FakeBackend()), \
                mock.patch.object(pp, "split_chapters",
                                  counting_split), \
                mock.patch.object(
                    pp, "_lookup_series_openlibrary",
                    lambda *a, **k: (None, None)), \
                mock.patch.object(pp, "resolve_work",
                                  lambda *a, **k: (None, "test")), \
                mock.patch.dict(pp.CONFIG,
                                {"llm": "ollama",
                                 "use_corpus": use_corpus}):
            ok = pp.process_file_v2(self.book, preview=True)
        return ok, calls

    def test_use_corpus_skips_the_file_parse(self):
        ok, calls = self._run_preview(True)
        self.assertTrue(ok)
        self.assertEqual(calls, [],
                         "corpus-sourced run re-parsed the file")

    def test_flag_off_parses_the_file(self):
        ok, calls = self._run_preview(False)
        self.assertTrue(ok)
        self.assertTrue(calls, "flag-off run never parsed the file")


class TestSnapshotCorpusBlock(_SourcingBase):
    def _capture(self, stash_fp=None, hashes=None):
        pp.StateDB.create(self.learn)
        pp._LEARNED = pp.LearnedState(learn_dir=self.learn,
                                      enabled=True)
        ck = self.dir / "ckpt"
        ck.mkdir(exist_ok=True)
        (ck / "meta.json").write_text("{}", encoding="utf-8")
        (ck / "chapter_1.json").write_text("{}", encoding="utf-8")
        pp._checkpoint_dir = lambda key: ck
        if stash_fp is not None:
            pp._PENDING_CAPTURE_HASHES[stash_fp] = {
                "chapter_hashes": hashes,
                "text_root_hash": pp._corpus_root_hash(hashes)}
        sid = pp.capture_snapshot(
            "snap-1", book_key="book", fingerprint="fp-cap",
            title="T", author="A", identifiers={}, chapter_count=1)
        self.assertEqual(sid, "snap-1")
        man = json.loads(
            (self.learn / "snapshots" / "fp-cap" / "snap-1"
             / "manifest.json").read_text())
        return man

    def test_block_written_from_the_run_stash(self):
        hashes = ["a" * 64, "b" * 64]
        man = self._capture(stash_fp="fp-cap", hashes=hashes)
        self.assertEqual(man["corpus"], {
            "fingerprint": "fp-cap",
            "parser_version": pp.CORPUS_PARSER_VERSION,
            "text_root_hash": pp._corpus_root_hash(hashes),
            "chapter_hashes": hashes})

    def test_no_stash_no_block(self):
        man = self._capture()
        self.assertNotIn("corpus", man)

    def test_stash_for_another_fingerprint_no_block(self):
        man = self._capture(stash_fp="fp-other", hashes=["c" * 64])
        self.assertNotIn("corpus", man)


class TestReplayCorpus(_SourcingBase):
    def _seed(self):
        """Live corpus (text + vectors, state complete) + state +
        a snapshot whose corpus block matches the corpus; returns
        (fingerprint, corpus_handle)."""
        db0 = pp.CorpusDB.create(self.learn, mode="importer")
        res = pp.corpus_ingest_book(
            db0, self.book,
            embedder=(FakeEmbedder(), "fake-embed-v1"))
        self.assertEqual(res["outcome"], "complete")
        fp = res["fingerprint"]
        pp.StateDB.create(self.learn)
        pp._LEARNED = pp.LearnedState(learn_dir=self.learn,
                                      enabled=True)
        db = pp._corpus_db_for(self.learn, mode="processor")
        chapters = pp.corpus_chapters_for(db, fp)
        hashes = [pp._corpus_text_hash(c["text"]) for c in chapters]
        pp._PENDING_CAPTURE_HASHES[fp] = {
            "chapter_hashes": hashes,
            "text_root_hash": pp._corpus_root_hash(hashes)}
        ck = self.dir / "ckpt"
        ck.mkdir(exist_ok=True)
        (ck / "meta.json").write_text("{}", encoding="utf-8")
        (ck / "chapter_1.json").write_text("{}", encoding="utf-8")
        pp._checkpoint_dir = lambda key: ck
        sid = pp.capture_snapshot(
            "snap-1", book_key="book", fingerprint=fp,
            title="Corpus Test Book", author="Test Author",
            identifiers={}, chapter_count=len(chapters))
        self.assertEqual(sid, "snap-1")
        pp._checkpoint_dir = self._saved[3]
        return fp, db

    def test_replay_corpus_ref_validates_by_hash(self):
        fp, db = self._seed()
        seen = {}

        def stub(fpath, dry_run=False, preview=False):
            seen["ctx"] = pp.corpus_retrieve_context(
                Path("ignored-under-replay"), fp)
            return True

        with mock.patch.dict(pp.CONFIG,
                             {"embed_model": "fake-embed-v1",
                              "embed_url": "http://embed.invalid",
                              "model": "test-judge"}):
            code, _report = pp.run_replay(
                "snap-1", self.book, self.learn, process_fn=stub)
        self.assertEqual(code, 0)
        self.assertIsNotNone(seen["ctx"])
        self.assertFalse(seen["ctx"]["writable"])

    def test_replay_corpus_access_read_only(self):
        fp, db = self._seed()
        before = db.dump_hash()
        seen = {}

        def stub(fpath, dry_run=False, preview=False):
            ctx = pp.corpus_retrieve_context(Path("ignored"), fp)
            seen["ctx"] = ctx
            # Gap-fill attempt under replay: read-only cache, the
            # flush lands nothing (§4.3 pinned).
            claims = [{"key": "k", "name": "X", "support": [],
                       "definition": "", "staged": False}]
            cache = pp._CorpusWindowCache(
                ctx["db"], fp, [], ctx["embed_model"],
                ctx["embed_format"], writable=ctx["writable"])
            seen["flushed"] = cache.flush()
            try:
                pp.corpus_stage_settle(
                    ctx["db"], fp, "retrieve", stage_version="1",
                    model="m", outputs={})
                seen["settle"] = "landed"
            except Exception as e:
                seen["settle"] = type(e).__name__
            return True

        with mock.patch.dict(pp.CONFIG,
                             {"embed_model": "fake-embed-v1",
                              "embed_url": "http://embed.invalid",
                              "model": "test-judge"}):
            code, _report = pp.run_replay(
                "snap-1", self.book, self.learn, process_fn=stub)
        self.assertEqual(code, 0)
        self.assertEqual(seen["flushed"], 0)
        self.assertNotEqual(seen["settle"], "landed")
        after_db = pp.CorpusDB.open(self.learn, mode="report",
                                     readonly=True)
        self.assertEqual(after_db.dump_hash(), before)

    def test_replay_corpus_hash_mismatch_refused(self):
        fp, db = self._seed()
        with db.write_txn("text") as conn:
            conn.execute(
                "UPDATE corpus_chapters SET text=text || ' drift' "
                "WHERE fingerprint=? AND chapter_index=2", (fp,))
        seen = {}

        def stub(fpath, dry_run=False, preview=False):
            seen["ctx"] = pp.corpus_retrieve_context(
                Path("ignored"), fp)
            return True

        with mock.patch.dict(pp.CONFIG,
                             {"embed_model": "fake-embed-v1",
                              "embed_url": "http://embed.invalid",
                              "model": "test-judge"}):
            code, _report = pp.run_replay(
                "snap-1", self.book, self.learn, process_fn=stub)
        self.assertEqual(code, 0)
        self.assertIsNone(seen["ctx"],
                          "hash-mismatched corpus was used in replay")

    def test_replay_without_corpus_block_unchanged(self):
        fp, db = self._seed()
        # Strip the corpus block from the manifest: pre-slice-5
        # snapshots replay exactly as before.
        man_path = (self.learn / "snapshots" / fp / "snap-1"
                    / "manifest.json")
        man = json.loads(man_path.read_text())
        man.pop("corpus", None)
        man_path.write_text(json.dumps(man))
        seen = {}

        def stub(fpath, dry_run=False, preview=False):
            seen["ctx"] = pp.corpus_retrieve_context(
                Path("ignored"), fp)
            return True

        with mock.patch.dict(pp.CONFIG,
                             {"embed_model": "fake-embed-v1",
                              "embed_url": "http://embed.invalid",
                              "model": "test-judge"}):
            code, _report = pp.run_replay(
                "snap-1", self.book, self.learn, process_fn=stub)
        self.assertEqual(code, 0)
        self.assertIsNone(seen["ctx"])


if __name__ == "__main__":
    unittest.main()
