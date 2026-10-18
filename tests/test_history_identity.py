"""Issues #32 / #33 — identity normalization and deterministic
book-identity resolution.

#32: the run-history writer hashed relationship claim identity from
the RAW endpoint/type strings, and trigger identity from a
lower-but-unstripped warning, while the pipeline's own identity
(_rel_pair_key) and the staging key both strip and case-fold. The
same claim in a different display form hashed differently across
runs, so cross-run history split one claim into phantom churn.

#33: two corpus lookups by source_hash took rows[0] with no
ORDER BY. One source_hash can name several corpus works (a
metadata correction forks the fingerprint while the file bytes —
and thus the source_hash — stay the same), so the pick depended
on the store's arbitrary row order and could flip between runs.

Stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location(
    "process_portable_history_identity", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _rel(frm, to, typ):
    return {"from": frm, "to": to, "type": typ,
            "validation_state": "validated", "validation_p": 0.5,
            "endpoint_provenance": {"from": {}, "to": {}}}


def _rel_keys(result):
    _claims, rows = pp._history_book_claims(result)
    return [r["claim_key"] for r in rows
            if r["axis"] == "relationships"]


class HistoryClaimKeyNormalizationTests(unittest.TestCase):
    """#32 — history claim identity is the pipeline's normalized
    identity, not the display strings."""

    def test_relationship_variants_share_one_key(self):
        base = {"_history_book_key": "bk",
                "relationships": [_rel("Frodo Baggins",
                                       "Sam Gamgee", "friend")]}
        variant = {"_history_book_key": "bk",
                   "relationships": [_rel(" frodo baggins ",
                                          "SAM GAMGEE", " Friend ")]}
        self.assertEqual(_rel_keys(base), _rel_keys(variant))

    def test_relationship_key_matches_rel_pair_key_identity(self):
        # The pin: history derives the SAME identity the pipeline
        # itself uses — the normalized pair from _rel_pair_key
        # plus the normalized type — hashed the history way.
        result = {"_history_book_key": "bk",
                  "relationships": [_rel(" Frodo Baggins",
                                         "SAM GAMGEE ", "Friend")]}
        pair = pp._rel_pair_key(result["relationships"][0])
        identity = f"{pair[0]}\x1f{pair[1]}\x1ffriend"
        expected = pp._history_claim_key(
            "relationships", "bk", identity)
        self.assertEqual(_rel_keys(result), [expected])

    def test_variant_forms_of_one_claim_dedupe_to_one_row(self):
        # Both display forms in ONE result are still one claim:
        # the writer's own duplicate guard works on the normalized
        # identity, so exactly one row is emitted.
        result = {"_history_book_key": "bk",
                  "relationships": [
                      _rel("Frodo Baggins", "Sam Gamgee", "friend"),
                      _rel(" frodo baggins", "SAM GAMGEE",
                           "Friend")]}
        self.assertEqual(len(_rel_keys(result)), 1)

    def test_trigger_variants_share_one_key(self):
        def trig_keys(warning):
            result = {"_history_book_key": "bk",
                      "triggers": [{"warning": warning,
                                    "confidence": 0.9}]}
            _claims, rows = pp._history_book_claims(result)
            return [r["claim_key"] for r in rows
                    if r["axis"] == "triggers"]
        self.assertEqual(trig_keys("Graphic Violence"),
                         trig_keys(" Graphic Violence "))
        self.assertEqual(trig_keys("Graphic Violence"),
                         trig_keys("GRAPHIC VIOLENCE"))

    def test_trigger_key_matches_staging_key_identity(self):
        # The pin: the history identity for a trigger is the
        # identity inside staging_claim_key — one normalization,
        # shared, so the two writers cannot drift apart again.
        staging = pp.staging_claim_key(
            "trigger", warning=" Graphic Violence ")
        identity = staging[len("trigger:"):]
        expected = pp._history_claim_key("triggers", "bk", identity)
        result = {"_history_book_key": "bk",
                  "triggers": [{"warning": " Graphic Violence ",
                                "confidence": 0.9}]}
        _claims, rows = pp._history_book_claims(result)
        keys = [r["claim_key"] for r in rows
                if r["axis"] == "triggers"]
        self.assertEqual(keys, [expected])


class _ForkedCorpus(unittest.TestCase):
    """A corpus holding TWO works for one source_hash: the
    original ingest and the later, superseding ingest created
    when a metadata correction forked the fingerprint."""

    OLD_FP = "fp-original"
    NEW_FP = "fp-superseding"
    OLD_AT = "2026-01-01T00:00:00+00:00"
    NEW_AT = "2026-06-01T00:00:00+00:00"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.learn = self.dir / "learned"
        self.learn.mkdir()
        self.book = self.dir / "book.epub"
        self.book.write_bytes(b"fork-fixture-bytes")
        self.source_hash = pp._corpus_sha256(self.book.read_bytes())

    def tearDown(self):
        pp._close_corpus_dbs()
        self._td.cleanup()

    def _db_with_rows(self, insert_new_first):
        db = pp.CorpusDB.create(self.learn, mode="importer")
        rows = [(self.OLD_FP, self.OLD_AT), (self.NEW_FP, self.NEW_AT)]
        if insert_new_first:
            rows.reverse()
        with db.write_txn() as conn:
            for fp, at in rows:
                conn.execute(
                    "INSERT INTO corpus_works (fingerprint,"
                    " source_hash, status, parser_version,"
                    " ingested_at, updated_at) VALUES (?, ?,"
                    " 'complete', ?, ?, ?)",
                    (fp, self.source_hash, pp.CORPUS_PARSER_VERSION,
                     at, at))
                conn.execute(
                    "INSERT INTO corpus_chapters (fingerprint,"
                    " chapter_index, label, text, text_hash,"
                    " char_count, word_count, is_frontmatter)"
                    " VALUES (?, 0, 'c', 'text', 'h', 4, 1, 0)",
                    (fp,))
        return db


class BookIdentityDeterminismTests(_ForkedCorpus):
    """#33 — the superseding fingerprint wins at both lookup
    sites, whichever order the rows were inserted in."""

    def _resolve(self, db):
        rec = pp._RunHistoryRecorder.__new__(pp._RunHistoryRecorder)
        rec.mode = "scan"
        rec._corpus_db = db
        rec._corpus_db_tried = True
        record = {"fpath": self.book, "book_key": None,
                  "source": None}
        rec._resolve_book_identity(record)
        return record["book_key"]

    def test_resolver_picks_superseding_either_order(self):
        for new_first in (False, True):
            db = self._db_with_rows(new_first)
            try:
                self.assertEqual(self._resolve(db), self.NEW_FP)
            finally:
                db.close()
                pp._close_corpus_dbs()
                (self.learn / "corpus.db").unlink(missing_ok=True)

    def test_servable_picks_superseding_either_order(self):
        for new_first in (False, True):
            db = self._db_with_rows(new_first)
            try:
                self.assertEqual(
                    pp.corpus_servable_fingerprint(db, self.book),
                    self.NEW_FP)
            finally:
                db.close()
                pp._close_corpus_dbs()
                (self.learn / "corpus.db").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
