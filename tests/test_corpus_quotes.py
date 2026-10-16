"""Corpus slice 6: quote VERIFY (corpus-design §10.1, §10.7
verify half, §11 slice 6).

VERIFY is a pure corpus function: fingerprint-scoped, token-
sequence matching — tolerant of whitespace/punctuation/case
drift, STRICT on words (a substitution, insertion, omission or
"…" elision fails closed; the legacy fuzzy matcher is not used).
The shipped text is always the corpus span bytes; the candidate
is preserved in provenance with its snapped flag and drift
class. Chapter assertions are assertions (A3): an evidence quote
asserted to a chapter is searched there only — a match elsewhere
records drift class "chapter" and FAILS. Occurrence discipline:
occurrences counted, earliest taken. Sentence-boundary rule:
signature spans must align with sentence boundaries; fragments
verify as evidence but are signature-ineligible.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

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

_FILLER = ("The road wound on through pine and stone while the "
           "company argued about maps, meals and old debts. ")
_DAWN = ("He betrayed the watch and the guard fell silent "
         "before dawn.")


class _QuoteBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.learn = self.dir / "learned"
        self.learn.mkdir()
        self.book_a = _make_epub(self.dir / "a.epub", _g_files(),
                                 title="G Fixture")
        self.book_b = _make_epub(self.dir / "b.epub",
                                 _fixture_files())
        imp = pp.CorpusDB.create(self.learn, mode="importer")
        self.fp_a = pp.corpus_ingest_book(imp, self.book_a)[
            "fingerprint"]
        self.fp_b = pp.corpus_ingest_book(imp, self.book_b)[
            "fingerprint"]
        pp._close_corpus_dbs()
        self.db = pp.CorpusDB.open(self.learn, mode="processor")
        self.chapters_a = {c["index"]: c
                           for c in pp.corpus_chapters_for(
                               self.db, self.fp_a)}

    def tearDown(self):
        pp._close_corpus_dbs()
        self._td.cleanup()


class TestVerifyQuote(_QuoteBase):
    def test_snap_records_provenance_and_ships_span(self):
        # Candidate with punctuation drift (comma added, terminal
        # period dropped): verifies, and the shipped text is the
        # corpus span — with its own punctuation.
        cand = ("He betrayed the watch, and the guard fell silent "
                "before dawn")
        rec = pp.verify_quote(self.db, self.fp_a, cand,
                              asserted_chapter=1)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["text"], _DAWN)
        self.assertEqual(rec["chapter_index"], 1)
        self.assertTrue(rec["snapped"])
        self.assertEqual(rec["drift"], "punctuation")
        span = self.chapters_a[1]["text"][rec["start"]:rec["end"]]
        self.assertEqual(span, rec["text"])

    def test_exact_candidate_is_not_snapped(self):
        rec = pp.verify_quote(self.db, self.fp_a, _DAWN,
                              asserted_chapter=1)
        self.assertIsNotNone(rec)
        self.assertFalse(rec["snapped"])
        self.assertIsNone(rec["drift"])

    def test_word_substitution_fails_closed(self):
        for bad in (
                # one word substituted
                "He betrayed the watch and the guard fell quiet "
                "before dawn.",
                # one word inserted
                "He betrayed the old watch and the guard fell "
                "silent before dawn.",
                # one word omitted
                "He betrayed the watch and the guard fell before "
                "dawn.",
                # elision (the accepted fail-closed trade, §10.1)
                "He betrayed the watch … before dawn.",
                ):
            self.assertIsNone(
                pp.verify_quote(self.db, self.fp_a, bad,
                                asserted_chapter=1), bad)

    def test_cross_book_quote_never_verifies(self):
        self.assertIsNone(pp.verify_quote(self.db, self.fp_b, _DAWN))
        self.assertIsNone(pp.verify_quote(self.db, self.fp_a,
                                          "Mira Solace drew her "
                                          "sword and faced the "
                                          "dawn."))

    def test_evidence_quote_wrong_chapter_fails(self):
        # The dawn line lives in chapter 1; asserted to chapter 3
        # as evidence it FAILS (A3) — the stage records why.
        self.assertIsNone(pp.verify_quote(self.db, self.fp_a, _DAWN,
                                          kind="evidence",
                                          asserted_chapter=3))
        out = pp.corpus_verify_quotes(self.db, self.fp_a, [{
            "text": _DAWN, "kind": "evidence", "chapter_index": 3}])
        self.assertEqual(out["verified"], 0)
        self.assertEqual(out["rejected"], 1)
        payloads = pp.corpus_stage_outputs(self.db, self.fp_a,
                                           "quote-verify")
        self.assertEqual(len(payloads), 1)
        verify = list(payloads.values())[0]["payload"]["verify"]
        self.assertEqual(verify["reason"], "chapter")
        self.assertEqual(verify["found_chapter_index"], 1)

    def test_unasserted_search_adopts_found_chapter(self):
        rec = pp.verify_quote(self.db, self.fp_a, _DAWN,
                              kind="signature")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["chapter_index"], 1)

    def test_verify_records_occurrence_preference(self):
        # The filler sentence repeats 45x per chapter x 3
        # chapters; book-wide search counts all and takes the
        # earliest; an asserted chapter counts within it.
        rec = pp.verify_quote(self.db, self.fp_a, _FILLER.strip(),
                              kind="signature")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["occurrences"], 135)
        self.assertEqual(rec["chapter_index"], 1)
        self.assertEqual(
            rec["start"],
            self.chapters_a[1]["text"].find("The road wound"))
        rec2 = pp.verify_quote(self.db, self.fp_a, _FILLER.strip(),
                               asserted_chapter=2)
        self.assertEqual(rec2["occurrences"], 45)
        self.assertEqual(rec2["chapter_index"], 2)

    def test_signature_span_sentence_bounded(self):
        whole = pp.verify_quote(self.db, self.fp_a, _DAWN,
                                kind="signature")
        self.assertTrue(whole["sentence_bounded"])
        frag = pp.verify_quote(
            self.db, self.fp_a,
            "betrayed the watch and the guard fell silent",
            kind="signature")
        self.assertIsNotNone(frag)  # verifies as a span…
        self.assertFalse(frag["sentence_bounded"])  # …evidence-only

    def test_outside_support_chapters_diagnostic(self):
        rec = pp.verify_quote(self.db, self.fp_a, _DAWN,
                              asserted_chapter=1,
                              support_chapters={2, 3})
        self.assertIsNotNone(rec)
        self.assertTrue(rec["outside_support_chapters"])


class TestVerifyStage(_QuoteBase):
    def test_stage_settles_records_and_reuses(self):
        cands = [{"text": _DAWN, "kind": "evidence",
                  "chapter_index": 1, "source": "run"},
                 {"text": "No such sentence exists anywhere.",
                  "kind": "evidence", "chapter_index": 1}]
        out = pp.corpus_verify_quotes(self.db, self.fp_a, cands)
        self.assertEqual((out["verified"], out["rejected"]), (1, 1))
        rows = self.db.query(
            "SELECT kind, status, text FROM quotes "
            "WHERE fingerprint=? ORDER BY status", (self.fp_a,))
        self.assertEqual(len(rows), 2)
        by_status = {r[1]: r for r in rows}
        self.assertEqual(by_status["verified"][2], _DAWN)
        self.assertEqual(by_status["rejected"][2],
                         "No such sentence exists anywhere.")
        env = self.db.query(
            "SELECT envelope FROM work_envelope WHERE fingerprint=?",
            (self.fp_a,))[0][0]
        import json
        quotes_section = json.loads(env)["sections"]["quotes"]
        self.assertEqual(quotes_section["state"], "present")
        self.assertEqual(quotes_section["value"]["by_status"],
                         {"verified": 1, "rejected": 1})
        # Second run over the same candidates: outputs reused,
        # nothing re-derived, quote rows unchanged.
        before = pp.corpus_stage_outputs(self.db, self.fp_a,
                                         "quote-verify")
        out2 = pp.corpus_verify_quotes(self.db, self.fp_a, cands)
        self.assertEqual(out2["reused"], 2)
        after = pp.corpus_stage_outputs(self.db, self.fp_a,
                                        "quote-verify")
        self.assertEqual(
            {k: v["run_id"] for k, v in before.items()},
            {k: v["run_id"] for k, v in after.items()})
        self.assertEqual(self.db.query(
            "SELECT COUNT(*) FROM quotes WHERE fingerprint=?",
            (self.fp_a,))[0][0], 2)

    def test_opening_line_generated_and_idempotent(self):
        res = pp.corpus_generate_opening_line(self.db, self.fp_a)
        self.assertIsNotNone(res)
        first = pp._sentence_spans(self.chapters_a[1]["text"])[0]
        expected = self.chapters_a[1]["text"][first[0]:first[1]]
        self.assertEqual(res["text"], expected)
        rows = self.db.query(
            "SELECT status, kind FROM quotes WHERE fingerprint=? "
            "AND kind='opening'", (self.fp_a,))
        self.assertEqual(rows, [("verified", "opening")])
        res2 = pp.corpus_generate_opening_line(self.db, self.fp_a)
        self.assertEqual(res2["quote_id"], res["quote_id"])
        self.assertEqual(self.db.query(
            "SELECT COUNT(*) FROM quotes WHERE fingerprint=?",
            (self.fp_a,))[0][0], 1)

    def test_cmd_ingest_generates_opening_line(self):
        learn2 = self.dir / "learned2"
        learn2.mkdir()
        pp.cmd_ingest([self.book_a], learn_dir=learn2)
        db = pp.CorpusDB.open(learn2, mode="report", readonly=True)
        rows = db.query(
            "SELECT kind, status FROM quotes WHERE kind='opening'")
        self.assertEqual(rows, [("opening", "verified")])


if __name__ == "__main__":
    unittest.main()
