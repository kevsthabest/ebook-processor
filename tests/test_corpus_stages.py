"""Corpus store, slice 4: the stage contract + retrieve adoption
(eval/corpus-design.md §4, §6.4 retraction/currency, §6.6).

Pins: §4.5's stage invariants (replace-own-only, same-inputs no-op,
A4's full-key-space ghost deletion, input hash covering upstream),
A6 (judge config inside the retrieve input hash: a prompt/model
change re-derives), the two-tier window cache (§4.4's content key:
corpus serves, gaps embed per-run, write-back only against stored
passages, never from a read-only cache), retraction as a named
operation (record + replacement + envelope trace, one transaction),
and retrieval reuse: a re-run over unchanged inputs re-reads
yesterday's verdicts instead of re-judging, while failed claims are
never settled as if they were results.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_corpus_ingest import _make_epub, _xhtml  # noqa: E402
from test_evidence_retrieval import FakeEmbedder, FakeJudgeCall  # noqa: E402

_MODEL = "fake-embed-v1"

_FILLER = ("The road wound on through pine and stone while the "
           "company argued about maps, meals and old debts. ")


def _g_files():
    ch1 = ("<h1>Chapter 1</h1><p>The fleet rode out the storm beyond "
           "the harbour wall and the sea ran high. He betrayed the "
           "watch and the guard fell silent before dawn. Then the "
           "storm broke and the sea calmed over the harbour. "
           + _FILLER * 45 + "</p>")
    ch2 = ("<h1>Chapter 2</h1><p>The council debated betrayal as law "
           "in the high hall while the storm wind shook the "
           "shutters. Scrolls were read aloud and old debts were "
           "named one by one. " + _FILLER * 45 + "</p>")
    ch3 = ("<h1>Chapter 3</h1><p>He betrayed the watch again when "
           "the second bell rang over the water. The harbour bell "
           "answered and the whole town woke to the news. "
           + _FILLER * 45 + "</p>")
    return {"OEBPS/c1.xhtml": _xhtml(ch1),
            "OEBPS/c2.xhtml": _xhtml(ch2),
            "OEBPS/c3.xhtml": _xhtml(ch3)}


class CountingEmbedder:
    """embedder(texts) wrapper recording every text it embeds."""

    def __init__(self, inner):
        self.inner = inner
        self.texts = []

    def __call__(self, texts):
        self.texts.extend(texts)
        return self.inner(texts)


def _claim(name, chapters, definition=""):
    return {"key": pp._tnorm(name), "name": name,
            "query_text": pp.evidence_query_text(name, definition),
            "terms": pp.evidence_query_terms(name, definition),
            "support": chapters, "definition": definition,
            "staged": False}


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.learn = self.dir / "learned"
        self.learn.mkdir()
        self._cfg = {k: pp.CONFIG.get(k) for k in
                     ("embed_model", "model", "llm")}
        pp.CONFIG["embed_model"] = _MODEL
        pp.CONFIG["model"] = "test-judge"
        pp.CONFIG["llm"] = "test-backend"

    def tearDown(self):
        pp._close_corpus_dbs()
        pp._close_state_dbs()
        for k, v in self._cfg.items():
            if v is None:
                pp.CONFIG.pop(k, None)
            else:
                pp.CONFIG[k] = v
        self._td.cleanup()

    def make_db(self, mode="processor"):
        return pp.CorpusDB.create(self.learn, mode=mode)

    def envelope(self, db):
        rows = db.query(
            "SELECT envelope FROM work_envelope WHERE fingerprint=?",
            (self.fp,))
        return json.loads(rows[0][0]) if rows else {}


class TestStageSettle(_Base):
    """The §4.1 contract at the storage seam, no stages attached."""

    def setUp(self):
        super().setUp()
        self.fp = "fp-stage-test"
        self.db = self.make_db()

    def settle(self, outputs, stage="retrieve"):
        return pp.corpus_stage_settle(
            self.db, self.fp, stage, stage_version="1",
            model="test-judge",
            outputs={k: {"input_hash": h, "payload": p}
                     for k, (h, p) in outputs.items()})

    def test_stage_rerun_replaces_own_outputs_only(self):
        # §4.5: stage A re-run with changed inputs replaces A's rows;
        # stage B's rows and hashes are untouched (scoped hash over
        # B's key space is identical across A's re-run).
        self.settle({"k1": ("h1", {"record": {"outcome": "abstained"}})},
                    stage="retrieve")
        self.settle({"k1": ("hb", {"record": {"outcome": "abstained"}})},
                    stage="quote-verify")
        b_hash = pp.corpus_scoped_dump_hash(self.db, self.fp,
                                            "quote-verify")
        self.settle({"k1": ("h2", {"record": {
            "outcome": "supported-strong"}})}, stage="retrieve")
        self.assertEqual(
            pp.corpus_scoped_dump_hash(self.db, self.fp, "quote-verify"),
            b_hash)
        out = pp.corpus_stage_outputs(self.db, self.fp, "retrieve")
        self.assertEqual(out["k1"]["input_hash"], "h2")
        self.assertEqual(out["k1"]["payload"]["record"]["outcome"],
                         "supported-strong")

    def test_stage_rerun_same_inputs_is_noop(self):
        # §4.5: identical input hashes => zero output writes, outputs
        # reused, and the stage_runs row records the reuse count.
        payload = {"record": {"outcome": "abstained"}, "evidence": []}
        r1 = self.settle({"k1": ("h1", payload), "k2": ("h9", payload)})
        before = pp.corpus_scoped_dump_hash(self.db, self.fp, "retrieve")
        run_ids = {k: v["run_id"] for k, v in
                   pp.corpus_stage_outputs(self.db, self.fp,
                                           "retrieve").items()}
        r2 = self.settle({"k1": ("h1", payload), "k2": ("h9", payload)})
        self.assertEqual(r2["written"], 0)
        self.assertEqual(r2["deleted"], 0)
        self.assertEqual(r2["reused"], 2)
        self.assertNotEqual(r1["run_id"], r2["run_id"])
        self.assertEqual(
            pp.corpus_scoped_dump_hash(self.db, self.fp, "retrieve"),
            before)
        self.assertEqual(
            {k: v["run_id"] for k, v in
             pp.corpus_stage_outputs(self.db, self.fp,
                                     "retrieve").items()}, run_ids)
        row = self.db.query(
            "SELECT payload FROM stage_runs WHERE run_id=?",
            (r2["run_id"],))[0][0]
        self.assertEqual(json.loads(row)["reused"], 2)

    def test_stage_rerun_deletes_unproduced_outputs(self):
        # §4.5 / A4: a re-run producing a strict subset deletes the
        # missing keys in the same transaction — no ghost rows.
        self.settle({"k1": ("h1", {"record": {"outcome": "abstained"}}),
                     "k2": ("h2", {"record": {"outcome": "abstained"}})})
        r = self.settle({"k1": ("h1",
                                 {"record": {"outcome": "abstained"}})})
        self.assertEqual(r["deleted"], 1)
        out = pp.corpus_stage_outputs(self.db, self.fp, "retrieve")
        self.assertEqual(sorted(out), ["k1"])
        env = self.envelope(self.db)
        self.assertEqual(
            env["sections"]["retrieval"]["value"]["outputs"], 1)

    def test_input_hash_covers_upstream(self):
        # §4.5: changing an upstream stage's output changes the
        # downstream stage's input hash (the hash material names its
        # upstream inputs, so the dependency is mechanical).
        base = {"stage": "retrieve", "text": "aaa",
                "upstream": {"embed": "hash-one"}}
        moved = {**base, "upstream": {"embed": "hash-two"}}
        self.assertNotEqual(pp._corpus_stage_input_hash(base),
                            pp._corpus_stage_input_hash(moved))
        self.assertEqual(pp._corpus_stage_input_hash(base),
                         pp._corpus_stage_input_hash(dict(base)))


class TestRetraction(_Base):
    def setUp(self):
        super().setUp()
        self.fp = "fp-retract-test"
        self.db = self.make_db()
        pp.corpus_stage_settle(
            self.db, self.fp, "retrieve", stage_version="1",
            model="test-judge",
            outputs={
                "k1": {"input_hash": "h1", "payload": {
                    "record": {"outcome": "supported-strong"},
                    "evidence": []}},
                "k2": {"input_hash": "h2", "payload": {
                    "record": {"outcome": "abstained"},
                    "evidence": []}}})

    def test_retraction_records_traces_and_removes(self):
        # §6.4: one transaction — explicit retraction record, the
        # output removed, the envelope trace (retracted_at + reason)
        # stamped; a sibling output is untouched.
        ok = pp.corpus_retract_stage_output(
            self.db, self.fp, "retrieve", "k1", "human correction")
        self.assertTrue(ok)
        out = pp.corpus_stage_outputs(self.db, self.fp, "retrieve")
        self.assertEqual(sorted(out), ["k2"])
        rows = self.db.query(
            "SELECT status, payload FROM stage_runs WHERE fingerprint=?"
            " AND stage='retrieve' AND status='retracted'", (self.fp,))
        self.assertEqual(len(rows), 1)
        rec = json.loads(rows[0][1])
        self.assertEqual(rec["output_key"], "k1")
        self.assertEqual(rec["reason"], "human correction")
        self.assertEqual(rec["withdrawn_input_hash"], "h1")
        section = self.envelope(self.db)["sections"]["retrieval"]
        self.assertEqual(section["state"], "present")
        self.assertEqual(section["reason"], "human correction")
        self.assertTrue(section["retracted_at"])

    def test_retraction_of_last_output_leaves_trace_not_none(self):
        # Never-assessed and assessed-then-withdrawn must stay
        # distinguishable (§6.4): the section renders unknown, but
        # the retraction trace survives on the record.
        pp.corpus_retract_stage_output(
            self.db, self.fp, "retrieve", "k1", "r1")
        pp.corpus_retract_stage_output(
            self.db, self.fp, "retrieve", "k2", "r2")
        section = self.envelope(self.db)["sections"]["retrieval"]
        self.assertEqual(section["state"], "unknown")
        self.assertEqual(section["reason"], "r2")
        self.assertTrue(section["retracted_at"])

    def test_retraction_of_missing_output_records_nothing(self):
        ok = pp.corpus_retract_stage_output(
            self.db, self.fp, "retrieve", "nope", "r")
        self.assertFalse(ok)
        rows = self.db.query(
            "SELECT COUNT(*) FROM stage_runs WHERE status='retracted'")
        self.assertEqual(rows[0][0], 0)


class _CorpusBook(_Base):
    """An ingested + embedded fixture book, reopened processor-side."""

    def setUp(self):
        super().setUp()
        self.book = _make_epub(self.dir / "gfixture.epub", _g_files(),
                               title="G Fixture", author="Test Author")
        idb = self.make_db(mode="importer")
        res = pp.corpus_ingest_book(idb, self.book)
        self.assertEqual(res["outcome"], "partial")
        self.fp = res["fingerprint"]
        out = pp.corpus_embed_work(idb, self.fp, FakeEmbedder(), _MODEL)
        self.assertEqual(out, "complete")
        pp._close_corpus_dbs()
        self.db = pp.CorpusDB.open(self.learn, mode="processor")
        self.chapters = pp.corpus_chapters_for(self.db, self.fp)
        self.by_index = {c["index"]: c for c in self.chapters}

    def ctx(self, **over):
        ctx = pp.corpus_retrieve_context(self.learn, self.fp)
        self.assertIsNotNone(ctx)
        ctx.update(over)
        return ctx

    def betrayal(self):
        return _claim("Betrayal",
                      [self.by_index[1], self.by_index[3]],
                      "A trusted ally betrays the group.")

    def storm(self):
        return _claim("Storm", [self.by_index[2]])


class TestRetrieveAdoption(_CorpusBook):
    def run_g(self, claims, ctx, judge=None, embedder=None):
        return pp._evidence_run(
            claims, call=judge or FakeJudgeCall(),
            embedder=embedder or CountingEmbedder(FakeEmbedder()),
            stage_ctx=ctx)

    def test_first_run_settles_stage_outputs(self):
        ctx = self.ctx()
        ev, records, marker = self.run_g(
            [self.betrayal(), self.storm()], ctx)
        self.assertEqual(records["betrayal"]["outcome"],
                         "supported-strong")
        self.assertEqual(len(ev["betrayal"]), 2)
        self.assertEqual(records["storm"]["outcome"], "adjacent-only")
        out = pp.corpus_stage_outputs(self.db, self.fp, "retrieve")
        self.assertEqual(sorted(out), ["betrayal", "storm"])
        self.assertEqual(
            out["betrayal"]["payload"]["record"]["outcome"],
            "supported-strong")
        self.assertEqual(len(out["betrayal"]["payload"]["evidence"]), 2)
        section = self.envelope(self.db)["sections"]["retrieval"]
        self.assertEqual(section["state"], "present")
        self.assertEqual(section["value"]["outputs"], 2)

    def test_rerun_reuses_verdicts_without_rejudging(self):
        # §4.2: text and stamps unchanged => yesterday's verdicts are
        # re-read; the embedder and the judge are never invoked.
        ctx = self.ctx()
        claims = [self.betrayal(), self.storm()]
        ev1, rec1, _ = self.run_g(claims, ctx)
        emb2 = CountingEmbedder(FakeEmbedder())
        judge2 = FakeJudgeCall()
        ev2, rec2, marker2 = self.run_g(claims, ctx, judge=judge2,
                                        embedder=emb2)
        self.assertEqual(emb2.texts, [])
        self.assertEqual(judge2.calls, 0)
        self.assertEqual(rec2, rec1)
        self.assertEqual(ev2, ev1)
        self.assertEqual(marker2["totals"]["supported_strong"], 1)
        self.assertEqual(marker2["totals"]["adjacent_only"], 1)
        self.assertEqual(marker2["totals"]["attempted"], 0)

    def test_text_change_rederives_only_the_affected_claim(self):
        # §4.5's covering property at the adopter: one claim's
        # support text moves => its input hash moves => it alone
        # re-derives; the untouched claim is reused.
        ctx = self.ctx()
        self.run_g([self.betrayal(), self.storm()], ctx)
        moved = dict(self.by_index[3])
        moved["text"] = moved["text"].replace(
            "betrayed the watch again", "sighted the far watch")
        emb2 = CountingEmbedder(FakeEmbedder())
        judge2 = FakeJudgeCall()
        moved_claim = _claim(
            "Betrayal", [self.by_index[1], moved],
            "A trusted ally betrays the group.")
        ev2, rec2, _ = self.run_g(
            [moved_claim, _claim("Storm", [self.by_index[2]])],
            ctx, judge=judge2, embedder=emb2)
        # The storm claim was reused: no work spent on it. Betrayal
        # re-derived against the moved text (its second chapter no
        # longer depicts betrayal; grade drops to single).
        self.assertEqual(judge2.calls, 1)
        self.assertTrue(all("council debated" not in t
                            for t in emb2.texts))
        self.assertEqual(rec2["storm"]["outcome"], "adjacent-only")
        self.assertEqual(rec2["betrayal"]["outcome"], "supported-single")

    def test_judge_config_change_rederives(self):
        # A6: the judge's identity is inside the input hash — a
        # different judge model leaves nothing "current".
        ctx = self.ctx()
        self.run_g([self.betrayal()], ctx)
        judge2 = FakeJudgeCall()
        ev, rec, _ = self.run_g([self.betrayal()],
                                self.ctx(judge_model="other-judge"),
                                judge=judge2)
        self.assertEqual(judge2.calls, 1)
        self.assertEqual(rec["betrayal"]["outcome"], "supported-strong")

    def test_stage_version_bump_rederives(self):
        # A6's mechanism: the stage_version itself is hashed, so a
        # judge-prompt/contract edit that bumps it re-derives.
        ctx = self.ctx()
        self.run_g([self.betrayal()], ctx)
        orig = pp.RETRIEVE_STAGE_VERSION
        pp.RETRIEVE_STAGE_VERSION = "2"
        try:
            judge2 = FakeJudgeCall()
            self.run_g([self.betrayal()], ctx, judge=judge2)
        finally:
            pp.RETRIEVE_STAGE_VERSION = orig
        self.assertEqual(judge2.calls, 1)

    def test_failed_claims_are_never_settled(self):
        # A failure is not a verdict: nothing is stored, and the
        # next run retries instead of reusing the failure.
        ctx = self.ctx()

        def dead_call(prompt, chunk, i, n):
            return None

        ev, rec, marker = self.run_g([self.betrayal()], ctx,
                                     judge=dead_call)
        self.assertEqual(rec["betrayal"]["outcome"], "failed")
        self.assertEqual(
            pp.corpus_stage_outputs(self.db, self.fp, "retrieve"), {})
        judge2 = FakeJudgeCall()
        ev2, rec2, _ = self.run_g([self.betrayal()], ctx, judge=judge2)
        self.assertEqual(judge2.calls, 1)
        self.assertEqual(rec2["betrayal"]["outcome"],
                         "supported-strong")

    def test_no_context_no_stage_writes(self):
        # Without a corpus context the engine is exactly today's:
        # verdicts computed, nothing recorded anywhere.
        ev, rec, marker = pp._evidence_run(
            [self.betrayal()], call=FakeJudgeCall(),
            embedder=CountingEmbedder(FakeEmbedder()))
        self.assertEqual(rec["betrayal"]["outcome"],
                         "supported-strong")
        self.assertEqual(
            pp.corpus_stage_outputs(self.db, self.fp, "retrieve"), {})


class TestWindowCache(_CorpusBook):
    def _drop_chapter_vectors(self, chapter_index):
        with self.db.write_txn("vectors") as conn:
            conn.execute(
                "DELETE FROM passage_vectors WHERE passage_id IN "
                "(SELECT passage_id FROM corpus_passages WHERE "
                "fingerprint=? AND chapter_index=?)",
                (self.fp, chapter_index))
            conn.execute(
                "UPDATE corpus_works SET status='vectors_partial' "
                "WHERE fingerprint=?", (self.fp,))

    def _n_vectors(self, chapter_index=None):
        if chapter_index is None:
            return self.db.query(
                "SELECT COUNT(*) FROM passage_vectors WHERE "
                "fingerprint=?", (self.fp,))[0][0]
        return self.db.query(
            "SELECT COUNT(*) FROM passage_vectors pv JOIN "
            "corpus_passages p ON p.passage_id = pv.passage_id "
            "WHERE pv.fingerprint=? AND p.chapter_index=?",
            (self.fp, chapter_index))[0][0]

    def test_corpus_tier_serves_without_embedding(self):
        # §4.4: memory miss -> corpus hit by content key; the run's
        # embedder is spent only on the claim's query vector.
        cache = pp._CorpusWindowCache(
            self.db, self.fp, self.chapters, _MODEL, writable=False)
        emb = CountingEmbedder(FakeEmbedder())
        claim = self.betrayal()
        out = pp.retrieve_claim_candidates(claim, emb, cache,
                                           floor=0.25)
        self.assertTrue(out["candidates"])
        self.assertEqual(emb.texts, [claim["query_text"]])

    def test_gap_fill_writes_back_against_stored_passages(self):
        # §4.4: a window the corpus holds as a passage but not as a
        # vector embeds per-run and the fill is written back under
        # the content key.
        before = self._n_vectors(1)
        self.assertGreater(before, 0)
        self._drop_chapter_vectors(1)
        self.assertEqual(self._n_vectors(1), 0)
        cache = pp._CorpusWindowCache(
            self.db, self.fp, self.chapters, _MODEL, writable=True)
        claim = self.betrayal()
        out = pp.retrieve_claim_candidates(
            claim, CountingEmbedder(FakeEmbedder()), cache, floor=0.25)
        self.assertTrue(out["candidates"])
        flushed = cache.flush()
        self.assertGreaterEqual(flushed, 1)
        self.assertEqual(self._n_vectors(1), flushed)
        vec = pp.corpus_vector_by_text_hash(
            self.db, self.fp, self._first_window_hash(1), _MODEL)
        self.assertIsNotNone(vec)

    def _first_window_hash(self, chapter_index):
        text = self.by_index[chapter_index]["text"]
        w = pp._evidence_windows(text)[0]
        return pp._corpus_text_hash(w["text"])

    def test_read_only_cache_never_writes(self):
        # §4.3's rule, at the cache: reads serve, fills stay in
        # memory; flush is a no-op and the corpus is untouched.
        self._drop_chapter_vectors(1)
        total = self._n_vectors()
        cache = pp._CorpusWindowCache(
            self.db, self.fp, self.chapters, _MODEL, writable=False)
        claim = self.betrayal()
        out = pp.retrieve_claim_candidates(
            claim, CountingEmbedder(FakeEmbedder()), cache, floor=0.25)
        self.assertTrue(out["candidates"])
        self.assertEqual(cache.flush(), 0)
        self.assertEqual(self._n_vectors(), total)
        self.assertEqual(self._n_vectors(1), 0)

    def test_foreign_text_window_never_written_back(self):
        # A window whose span is not a stored passage of this work
        # (foreign text) has nowhere honest to land: no write-back.
        foreign = [{"index": 9, "label": "Elsewhere",
                    "text": "He betrayed the watch under a foreign "
                            "sky, far from any harbour. " * 4}]
        cache = pp._CorpusWindowCache(
            self.db, self.fp, foreign, _MODEL, writable=True)
        claim = {"key": "x", "name": "X",
                 "query_text": "betrayed the watch",
                 "terms": pp.evidence_query_terms("Betrayal"),
                 "support": foreign}
        pp.retrieve_claim_candidates(
            claim, CountingEmbedder(FakeEmbedder()), cache, floor=0.25)
        total = self._n_vectors()
        self.assertEqual(cache.flush(), 0)
        self.assertEqual(self._n_vectors(), total)


if __name__ == "__main__":
    unittest.main()
