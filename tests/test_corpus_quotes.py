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
import json
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
# The G fixture's first prose sentence (§10.4(a), Advisor
# Ruling-2): the opening line EXCLUDES the chapter heading —
# "Chapter 1" is the chapter's stored label, shipped separately
# as chapter_label (§10.6), never part of the opening span.
_OPENING = ("The fleet rode out the storm beyond the harbour "
            "wall and the sea ran high.")


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
        self.assertEqual(res["text"], _OPENING)
        rows = self.db.query(
            "SELECT status, kind FROM quotes WHERE fingerprint=? "
            "AND kind='opening'", (self.fp_a,))
        self.assertEqual(rows, [("verified", "opening")])
        res2 = pp.corpus_generate_opening_line(self.db, self.fp_a)
        self.assertEqual(res2["quote_id"], res["quote_id"])
        self.assertEqual(self.db.query(
            "SELECT COUNT(*) FROM quotes WHERE fingerprint=?",
            (self.fp_a,))[0][0], 1)

    def test_opening_line_excludes_chapter_heading(self):
        # Advisor Ruling-2 pin: the fixture's chapter 1 text
        # begins with its stored label as a heading line
        # ("Chapter 1\nThe fleet rode out…"). The opening line
        # is the first PROSE sentence, taken from after the
        # heading — the heading is an exclusion of the span's
        # start offset, and the shipped text must still slice
        # exactly from the stored chapter text (A2 discipline).
        chapter = self.chapters_a[1]
        self.assertEqual(chapter["label"], "Chapter 1")
        self.assertTrue(
            chapter["text"].startswith("Chapter 1\n"))
        res = pp.corpus_generate_opening_line(self.db, self.fp_a)
        self.assertIsNotNone(res)
        self.assertEqual(res["text"], _OPENING)
        self.assertNotIn("Chapter 1", res["text"])
        row = self.db.query(
            "SELECT start_offset, end_offset FROM quotes "
            "WHERE fingerprint=? AND kind='opening'",
            (self.fp_a,))[0]
        self.assertEqual(
            chapter["text"][row[0]:row[1]], res["text"])
        self.assertGreater(
            row[0], chapter["text"].index("\n"))

    def test_cmd_ingest_generates_opening_line(self):
        learn2 = self.dir / "learned2"
        learn2.mkdir()
        pp.cmd_ingest([self.book_a], learn_dir=learn2)
        db = pp.CorpusDB.open(learn2, mode="report", readonly=True)
        rows = db.query(
            "SELECT kind, status FROM quotes WHERE kind='opening'")
        self.assertEqual(rows, [("opening", "verified")])


_HARBOUR = ("He betrayed the watch again when the second bell "
            "rang over the water.")
_TWO_FILLER = _FILLER.strip() + " " + _FILLER.strip()


def _fake_ask(votes):
    """A stand-in decision-model poll: votes maps a text fragment
    to {"quotability","representative","spoiler","heat"} p_yes
    values; anything unlisted gets middling quotability and clean
    screens. The questions dict keys are q<i>_<aspect>; the state
    lists each candidate's text under its index."""

    def ask(questions, state):
        out = {}
        lines = state.splitlines()
        texts = {}
        for line in lines:
            if line.startswith("CANDIDATE "):
                idx, text = line[len("CANDIDATE "):].split(" ", 1)
                texts[int(idx)] = text
        for key in questions:
            qpart, aspect = key.rsplit("_", 1)
            i = int(qpart[1:])
            text = texts.get(i, "")
            v = {"quotability": 0.5, "representative": 0.5,
                 "spoiler": 0.05, "heat": 0.05}
            for frag, vals in votes.items():
                if frag in text:
                    v.update(vals)
            out[key] = v[aspect]
        return out

    return ask


class TestSelectAndPayload(_QuoteBase):
    def setUp(self):
        super().setUp()
        pp.corpus_generate_opening_line(self.db, self.fp_a)
        pp.corpus_verify_quotes(self.db, self.fp_a, [
            {"text": _DAWN, "kind": "evidence", "chapter_index": 1,
             "source": "run"},
            {"text": _HARBOUR, "kind": "evidence",
             "chapter_index": 3, "source": "run"},
            {"text": _TWO_FILLER, "kind": "evidence",
             "chapter_index": 1, "source": "run"},
        ])
        pp._dm_budget_reset()
        self._saved_validator = pp._DECISION_VALIDATOR

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._saved_validator
        super().tearDown()

    def _selected(self):
        return self.db.query(
            "SELECT text, kind, payload FROM quotes "
            "WHERE fingerprint=? AND status='selected'",
            (self.fp_a,))

    def test_signature_cap_and_rank(self):
        ask = _fake_ask({"before dawn": {"quotability": 0.95,
                                         "representative": 0.9}})
        out = pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        self.assertEqual(out["poll"], "ok")
        sel = self._selected()
        sigs = [r for r in sel
                if json.loads(r[2]).get("selected_as")
                == "signature"]
        self.assertEqual(len(sigs), 1)  # ONE signature per book
        self.assertEqual(sigs[0][0], _DAWN)

    def test_opening_ships_as_its_own_kind(self):
        ask = _fake_ask({"before dawn": {"quotability": 0.95}})
        pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        payload = pp.corpus_build_payload(self.db, self.fp_a)
        kinds = {q["kind"] for q in payload["quotes"]["quotes"]}
        self.assertEqual(kinds, {"signature", "opening"})
        opening = [q for q in payload["quotes"]["quotes"]
                   if q["kind"] == "opening"][0]
        self.assertEqual(opening["text"], _OPENING)

    def test_poll_is_metadata_only(self):
        ask = _fake_ask({"before dawn": {"quotability": 0.95}})
        pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        payload = pp.corpus_build_payload(self.db, self.fp_a)
        blob = json.dumps(payload)
        self.assertNotIn("quotability", blob)
        self.assertNotIn("representative", blob)
        self.assertNotIn("confidence", blob)
        self.assertNotIn("poll", blob)
        # …while the processor-side record keeps the metadata.
        row = [r for r in self._selected()
               if r[0] == _DAWN][0]
        poll = json.loads(row[2])["poll"]
        self.assertEqual(poll["quotability"], 0.95)

    def test_poll_shed_is_fail_closed(self):
        # No validator, no injected ask: the poll is shed, nothing
        # ships, and the envelope says present with zero selected.
        pp._DECISION_VALIDATOR = None
        out = pp.corpus_select_quotes(self.db, self.fp_a)
        self.assertEqual(out["poll"], "skipped")
        self.assertEqual(self._selected(), [])
        payload = pp.corpus_build_payload(self.db, self.fp_a)
        self.assertNotIn("quotes", payload)
        import json
        env = json.loads(self.db.query(
            "SELECT envelope FROM work_envelope WHERE fingerprint=?",
            (self.fp_a,))[0][0])
        section = env["sections"]["quotes"]
        self.assertEqual(section["state"], "present")
        self.assertEqual(section["value"]["selected"], 0)

    def test_budget_exhaustion_sheds(self):
        self.assertTrue(pp._dm_budget_claim(50))
        ask = _fake_ask({})
        out = pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        self.assertEqual(out["poll"], "skipped")
        self.assertEqual(out["reason"], "budget")
        self.assertEqual(self._selected(), [])

    def test_final_quarter_ineligible(self):
        # The chapter-3 line gets the best votes and still cannot
        # be the signature; the dawn line (first half) wins.
        ask = _fake_ask({"second bell": {"quotability": 0.99,
                                         "representative": 0.99},
                         "before dawn": {"quotability": 0.6}})
        pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        sel_texts = [r[0] for r in self._selected()]
        self.assertNotIn(_HARBOUR, sel_texts)
        self.assertIn(_DAWN, sel_texts)
        # The harbour line is untouched: still verified, not
        # rejected — ineligibility is not a screen failure.
        row = self.db.query(
            "SELECT status FROM quotes WHERE fingerprint=? "
            "AND text=?", (self.fp_a, _HARBOUR))
        self.assertEqual(row, [("verified",)])

    def test_quote_never_ships_unverified(self):
        # A rejected (never verified) candidate with irresistible
        # votes is not in the pool at all.
        pp.corpus_verify_quotes(self.db, self.fp_a, [
            {"text": "A stirring line that is not in this book.",
             "kind": "signature", "source": "extraction"}])
        ask = _fake_ask({"stirring line": {"quotability": 0.99},
                         "before dawn": {"quotability": 0.7}})
        pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        sel_texts = [r[0] for r in self._selected()]
        self.assertNotIn("A stirring line that is not in this "
                         "book.", sel_texts)
        payload = pp.corpus_build_payload(self.db, self.fp_a)
        self.assertNotIn("stirring",
                         json.dumps(payload))

    def test_spoiler_screen_rejects(self):
        ask = _fake_ask({"before dawn": {"spoiler": 0.9,
                                         "quotability": 0.95}})
        pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        row = self.db.query(
            "SELECT status, payload FROM quotes WHERE fingerprint=? "
            "AND text=?", (self.fp_a, _DAWN))
        self.assertEqual(row[0][0], "rejected")
        self.assertEqual(
            json.loads(row[0][1])["reason"],
            "spoiler_screen")

    def test_payload_chapter_label_from_heading(self):
        ask = _fake_ask({"before dawn": {"quotability": 0.95}})
        pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        payload = pp.corpus_build_payload(self.db, self.fp_a)
        sig = [q for q in payload["quotes"]["quotes"]
               if q["kind"] == "signature"][0]
        self.assertEqual(sig["chapter_label"], "Chapter 1")
        self.assertEqual(sig["spoiler_tier"], "mild")
        self.assertEqual(sig["screens"],
                         {"spoiler": "pass", "heat": "pass"})
        self.assertEqual(payload["quotes"]["edition"]["fingerprint"],
                         self.fp_a)

    def test_payload_omits_noncurrent_section(self):
        ask = _fake_ask({"before dawn": {"quotability": 0.95}})
        pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        payload = pp.corpus_build_payload(self.db, self.fp_a)
        self.assertIn("quotes", payload)
        # Tamper with the recorded span: the section's currency
        # check must now refuse to ship it.
        with self.db.write_txn("stages") as conn:
            conn.execute(
                "UPDATE quotes SET text='tampered text' "
                "WHERE fingerprint=? AND status='selected' "
                "AND kind='evidence'", (self.fp_a,))
        payload2 = pp.corpus_build_payload(self.db, self.fp_a)
        self.assertNotIn("quotes", payload2)

    def test_payload_omits_section_after_verify_retraction(self):
        ask = _fake_ask({"before dawn": {"quotability": 0.95}})
        pp.corpus_select_quotes(self.db, self.fp_a, ask=ask)
        self.assertIn("quotes",
                      pp.corpus_build_payload(self.db, self.fp_a))
        outputs = pp.corpus_stage_outputs(self.db, self.fp_a,
                                          "quote-verify")
        dawn_key = [k for k, v in outputs.items()
                    if v["payload"].get("candidate_text") == _DAWN][0]
        self.assertTrue(pp.corpus_retract_stage_output(
            self.db, self.fp_a, "quote-verify", dawn_key,
            "test withdrawal"))
        self.assertNotIn("quotes",
                         pp.corpus_build_payload(self.db, self.fp_a))


if __name__ == "__main__":
    unittest.main()
