"""Corpus store, slice 3: passage vectors (eval/corpus-design.md §2,
§3.3 phase 2, §4.4's content key). Pins: BLOB-canonical storage
readable with no extension loaded, stamp discipline (a foreign
model's rows are never served), the A1 drift canary (a generation
whose endpoint drifts reads as misses until re-embedded clean),
numpy/pure-Python search parity with the pure path FORCED, gap-fill
resume, and phase 2 completing the ingest state machine.
"""
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_corpus_ingest import _make_epub, _fixture_files  # noqa: E402

_DIM = 8
_MODEL = "fake-embed-v1"


def _fake_vec(text, dim=_DIM):
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [((h[i % len(h)] / 255.0) - 0.5) for i in range(dim)]


def fake_embed(texts):
    return [_fake_vec(t) for t in texts]


def shifted_embed(texts):
    """Same claimed model, different space: every text gets the
    NEXT text's vector — the endpoint-drift shape A1 exists for."""
    vecs = [_fake_vec(t) for t in texts]
    return vecs[1:] + vecs[:1]


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.learn = self.dir / "learned"
        self.learn.mkdir()
        self.book = _make_epub(self.dir / "fixture.epub",
                               _fixture_files())
        self.db = pp.CorpusDB.create(self.learn, mode="importer")
        res = pp.corpus_ingest_book(self.db, self.book)
        self.fp = res["fingerprint"]

    def tearDown(self):
        pp._close_corpus_dbs()
        pp._close_state_dbs()
        self._td.cleanup()

    def embed(self, fn=fake_embed, model=_MODEL):
        return pp.corpus_embed_work(self.db, self.fp, fn, model)

    def n_passages(self):
        return self.db.query(
            "SELECT COUNT(*) FROM corpus_passages WHERE fingerprint=?",
            (self.fp,))[0][0]


class TestBlobStorage(_Base):
    def test_vector_blob_roundtrip(self):
        # §2.5 'BLOB round-trips bit-identical': the stored bytes
        # are float32-LE, and reading them back is lossless
        # relative to what is stored — pack∘unpack is the identity
        # on stored blobs.
        vec = [0.125, -0.5, 3.25, 1e-4, 0.0, -2.75, 42.0, 0.999]
        import struct
        blob = pp._corpus_pack_vector(vec)
        self.assertEqual(blob, struct.pack("<8f", *vec))
        back = pp._corpus_unpack_vector(blob, len(vec))
        self.assertEqual(back, list(struct.unpack("<8f", blob)))
        self.assertEqual(pp._corpus_pack_vector(back), blob)

    def test_corpus_readable_without_extension(self):
        # §2.5: a bare sqlite3 connection — no extension loading of
        # any kind — reads every corpus table incl. vectors.
        self.embed()
        raw = sqlite3.connect(str(self.learn / "corpus.db"))
        try:
            pid, dim, blob = raw.execute(
                "SELECT passage_id, dim, vector FROM passage_vectors "
                "LIMIT 1").fetchone()
            self.assertEqual(len(pp._corpus_unpack_vector(blob, dim)),
                             dim)
            n = raw.execute("SELECT COUNT(*) FROM corpus_chapters"
                            ).fetchone()[0]
            self.assertGreater(n, 0)
        finally:
            raw.close()


class TestEmbedPhase(_Base):
    def test_embed_completes_work_and_envelope(self):
        outcome = self.embed()
        self.assertEqual(outcome, "complete")
        self.assertEqual(pp.corpus_work_state(self.db, self.fp),
                         "complete")
        self.assertTrue(pp.corpus_retrieval_ready(self.db, self.fp))
        vecs = pp.corpus_passage_vectors(self.db, self.fp, _MODEL)
        self.assertEqual(len(vecs), self.n_passages())
        env = json.loads(self.db.query(
            "SELECT envelope FROM work_envelope WHERE fingerprint=?",
            (self.fp,))[0][0])
        self.assertEqual(env["sections"]["vectors"]["coverage"], 1.0)
        self.assertEqual(env["sections"]["vectors"]["state"],
                         "present")

    def test_ingest_with_embedder_runs_both_phases(self):
        book2 = _make_epub(self.dir / "second.epub", _fixture_files(),
                           title="Second Book", isbn="9780525951623")
        res = pp.corpus_ingest_book(self.db, book2,
                                    embedder=(fake_embed, _MODEL))
        self.assertEqual(res["outcome"], "complete")
        self.assertEqual(pp.corpus_work_state(self.db,
                                              res["fingerprint"]),
                         "complete")
        run = self.db.query(
            "SELECT outcome, embed_model FROM ingest_runs WHERE "
            "fingerprint=?", (res["fingerprint"],))[0]
        self.assertEqual(run, ("complete", _MODEL))

    def test_embed_resume_fills_gaps(self):
        # §3.5: phase 2 killed mid-way resumes to exactly the full
        # set — gap-filling by content key, never restart-from-zero.
        calls = {"n": 0}
        seen_second_run = []

        def flaky(texts):
            calls["n"] += 1
            # Call 1 is the generation-birth canary, call 2 the
            # first passage batch; the kill lands mid-phase-2.
            if calls["n"] == 3:
                raise RuntimeError("endpoint died")
            return fake_embed(texts)

        outcome = self.embed(fn=flaky)
        self.assertEqual(outcome, "partial")
        self.assertEqual(pp.corpus_work_state(self.db, self.fp),
                         "vectors_partial")
        landed = self.db.query(
            "SELECT COUNT(*) FROM passage_vectors WHERE fingerprint=?",
            (self.fp,))[0][0]
        self.assertGreater(landed, 0)
        self.assertLess(landed, self.n_passages())

        def tracking(texts):
            seen_second_run.extend(texts)
            return fake_embed(texts)

        outcome = self.embed(fn=tracking)
        self.assertEqual(outcome, "complete")
        total = self.db.query(
            "SELECT COUNT(*) FROM passage_vectors WHERE fingerprint=?",
            (self.fp,))[0][0]
        self.assertEqual(total, self.n_passages())
        # The resume embedded only the gap: every text it received
        # was missing after the kill (none of the landed spans).
        self.assertEqual(len(seen_second_run),
                         self.n_passages() - landed)

    def test_vectors_embedded_from_stored_spans(self):
        # A2: the embedder receives text re-sliced from the STORED
        # chapter — exactly the passage spans, nothing else.
        received = []

        def recording(texts):
            received.extend(texts)
            return fake_embed(texts)

        self.embed(fn=recording)
        rows = self.db.query(
            "SELECT c.text, p.start_offset, p.end_offset FROM "
            "corpus_passages p JOIN corpus_chapters c ON "
            "c.fingerprint = p.fingerprint AND c.chapter_index = "
            "p.chapter_index WHERE p.fingerprint=?", (self.fp,))
        spans = {text[s:e] for text, s, e in rows}
        # The only non-passage texts the embedder ever sees are
        # the generation's canary sentinels (§2.4) — nothing else.
        self.assertEqual(
            set(received) - set(pp._CORPUS_CANARY_TEXTS), spans)


class TestStampDiscipline(_Base):
    def test_foreign_model_vector_never_served(self):
        # §2.5: rows stamped with another model are invisible under
        # the configured model — cosine across spaces is meaningless.
        self.embed()
        self.assertEqual(
            pp.corpus_passage_vectors(self.db, self.fp, "other-model"),
            {})
        self.assertEqual(
            pp.corpus_search_passages(self.db, self.fp, _fake_vec("x"),
                                      "other-model"), [])
        self.assertTrue(
            pp.corpus_passage_vectors(self.db, self.fp, _MODEL))

    def test_content_key_lookup_misses_on_text_or_model_change(self):
        # §4.4's content key at the read layer: (passage text hash,
        # model, format) — a mutated text or a foreign model is a
        # miss, never a serve.
        self.embed()
        pid, th = self.db.query(
            "SELECT passage_id, text_hash FROM corpus_passages "
            "WHERE fingerprint=? LIMIT 1", (self.fp,))[0]
        hit = pp.corpus_vector_by_text_hash(self.db, self.fp, th,
                                            _MODEL)
        self.assertIsNotNone(hit)
        edited = pp._corpus_text_hash("completely different words")
        self.assertIsNone(pp.corpus_vector_by_text_hash(
            self.db, self.fp, edited, _MODEL))
        self.assertIsNone(pp.corpus_vector_by_text_hash(
            self.db, self.fp, th, "other-model"))

    def test_model_change_reembed_turns_rows_over(self):
        # §2.4: a model change starts a new generation lazily — the
        # passage's single row is re-stamped by the re-embed; the
        # old model then misses by stamp, the new one serves.
        self.embed()
        outcome = self.embed(model="fake-embed-v2")
        self.assertEqual(outcome, "complete")
        self.assertEqual(
            pp.corpus_passage_vectors(self.db, self.fp, _MODEL), {})
        self.assertEqual(
            len(pp.corpus_passage_vectors(self.db, self.fp,
                                          "fake-embed-v2")),
            self.n_passages())


class TestDriftCanary(_Base):
    def test_drift_canary_marks_generation_suspect(self):
        # §2.5/A1: probe vectors drift beyond the band => the
        # generation is marked suspect, its rows read as misses,
        # and a clean re-embed restores service.
        self.embed()
        self.assertTrue(pp.corpus_passage_vectors(self.db, self.fp,
                                                  _MODEL))
        ok = pp._corpus_canary_check(self.db, shifted_embed, _MODEL)
        self.assertFalse(ok)
        gen = pp._corpus_generation(self.db, _MODEL)
        self.assertEqual(gen["canary"], "suspect")
        self.assertEqual(
            pp.corpus_passage_vectors(self.db, self.fp, _MODEL), {})
        self.assertIn("SUSPECT", pp._corpus_status_text(self.learn))
        outcome = self.embed()  # good embedder again
        self.assertEqual(outcome, "complete")
        gen = pp._corpus_generation(self.db, _MODEL)
        self.assertEqual(gen["canary"], "ok")
        self.assertTrue(pp.corpus_passage_vectors(self.db, self.fp,
                                                  _MODEL))

    def test_canary_stable_endpoint_stays_ok(self):
        self.embed()
        self.assertTrue(pp._corpus_canary_check(self.db, fake_embed,
                                                _MODEL))
        self.assertEqual(pp._corpus_generation(self.db, _MODEL)
                         ["canary"], "ok")


class TestSearch(_Base):
    def test_search_parity_numpy_vs_pure(self):
        # §2.5: top-k identical under both engines, the pure path
        # FORCED (never discovered from ambient numpy presence).
        self.embed()
        q = _fake_vec("Mira Solace drew her sword and faced the dawn")
        pure = pp.corpus_search_passages(
            self.db, self.fp, q, _MODEL, top_k=10, engine="pure")
        self.assertTrue(pure)
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed on this machine")
        fast = pp.corpus_search_passages(
            self.db, self.fp, q, _MODEL, top_k=10, engine="numpy")
        self.assertEqual([p for p, _s in pure],
                         [p for p, _s in fast])
        for (_p1, s1), (_p2, s2) in zip(pure, fast):
            self.assertAlmostEqual(s1, s2, places=5)

    def test_search_default_engine_can_be_forced_pure(self):
        self.embed()
        saved = dict(pp._NUMPY_STATE)
        pp._NUMPY_STATE.update({"mod": None, "tried": True})
        try:
            self.assertEqual(pp._corpus_default_engine(), "pure")
            out = pp.corpus_search_passages(
                self.db, self.fp, _fake_vec("anything"), _MODEL,
                top_k=3)
            self.assertEqual(len(out), 3)
        finally:
            pp._NUMPY_STATE.update(saved)

    def test_search_ranks_identical_text_first(self):
        self.embed()
        rows = self.db.query(
            "SELECT c.text, p.start_offset, p.end_offset, "
            "p.passage_id FROM corpus_passages p JOIN corpus_chapters "
            "c ON c.fingerprint = p.fingerprint AND c.chapter_index "
            "= p.chapter_index WHERE p.fingerprint=? LIMIT 1",
            (self.fp,))
        text, s, e, pid = rows[0]
        out = pp.corpus_search_passages(
            self.db, self.fp, _fake_vec(text[s:e]), _MODEL, top_k=1,
            engine="pure")
        self.assertEqual(out[0][0], pid)
        self.assertAlmostEqual(out[0][1], 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
