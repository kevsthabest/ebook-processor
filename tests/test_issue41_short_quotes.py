"""Issue #41: notable quotes under 3 words bypass evidence verification.

The evidence module's docstring promises "Unverified quotes are dropped
rather than shipped", but a quote shorter than 3 normalized words could
not be checked (the check excludes trivially short strings from the
stats), and the quotes handling treated "not checked and failed" as
"keep" — so an invented 1-2-word "quote" shipped to book_quotes and
book_meta.notable_quotes with no verification at any gate.

Pinned behaviour after the fix:
  * verify_all_evidence drops every quote it cannot verify, including
    one that is too short to check — genuine-but-short included: the
    documented rule is the rule, there is no verbatim-presence exception
    for short quotes at this boundary.
  * The write boundary (_build_quote_rows and the book_meta
    notable_quotes builder, via the shared shippable-quote filter)
    drops short quotes and quotes stamped evidence_verified=False even
    if a payload reaches it without passing through verification.
  * Character/relationship evidence is unchanged: a short evidence
    string still keeps its claim, with the evidence cleared, flagged
    unverified, and excluded from the checked/verified stats — those
    families have their own downstream backstops and are out of scope.
  * Quotes of 3+ words verify and ship exactly as before; a 3+-word
    quote with no verification stamp (the shape older fixtures and
    payloads carry) still ships at the write boundary.

Stdlib unittest only. No network, no Supabase, no real LLM calls.
Run: python -m unittest tests.test_issue41_short_quotes -v
"""
import importlib.util
import unittest
from pathlib import Path
from urllib.parse import unquote

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


SRC = ("Real words from text here. "
       "She stayed because the house remembered her name.")


class TestVerifyDropsUnverifiableQuotes(unittest.TestCase):
    def test_issue_reproduction(self):
        quotes = [
            {"text": "Zqx", "spoiler": False},                # 1 word, invented
            {"text": "Blood calls", "spoiler": False},        # 2 words, invented
            {"text": "Real words", "spoiler": False},         # 2 words, genuine
            {"text": "Real words from text here", "spoiler": False},
            {"text": "a completely invented memorable line here",
             "spoiler": False},                               # 3+ words, invented
        ]
        _, _, vq, stats = pp.verify_all_evidence([], [], quotes, SRC)
        # Only the verifiable 3+-word genuine quote ships.
        self.assertEqual([q["text"] for q in vq],
                         ["Real words from text here"])
        self.assertTrue(vq[0]["evidence_verified"])
        # Every unverifiable quote counts as dropped: the two invented
        # short quotes, the genuine short quote, and the invented long
        # quote. Short quotes stay out of the checked/verified stats
        # (that exclusion is what the stats are for); dropping them at
        # the quotes boundary does not inflate the ratio either way.
        self.assertEqual(stats["quotes_dropped"], 4)
        self.assertEqual(stats["evidence_checked"], 2)
        self.assertEqual(stats["evidence_verified"], 1)

    def test_genuine_short_quote_is_dropped_per_the_documented_rule(self):
        # "Real words" IS in the source verbatim — but at under 3 words
        # the verification check cannot check it, and the module's
        # documented rule is that unverified quotes are dropped rather
        # than shipped. No verbatim-presence exception is invented here.
        _, _, vq, stats = pp.verify_all_evidence(
            [], [], [{"text": "Real words", "spoiler": False}], SRC)
        self.assertEqual(vq, [])
        self.assertEqual(stats["quotes_dropped"], 1)

    def test_three_plus_word_quotes_ship_exactly_as_today(self):
        quotes = [
            {"text": "the house remembered her name", "spoiler": False},
            {"text": "a completely invented memorable line here",
             "spoiler": True},
        ]
        _, _, vq, stats = pp.verify_all_evidence([], [], quotes, SRC)
        self.assertEqual(len(vq), 1)
        self.assertEqual(vq[0]["text"], "the house remembered her name")
        self.assertTrue(vq[0]["evidence_verified"])
        self.assertEqual(stats["quotes_dropped"], 1)
        self.assertEqual(stats["evidence_checked"], 2)
        self.assertEqual(stats["evidence_verified"], 1)


class TestShortEvidenceForClaimsUnchanged(unittest.TestCase):
    def test_character_and_relationship_short_evidence_unchanged(self):
        chars = [
            {"name": "Mother", "evidence": "Mother"},
            {"name": "House", "evidence": "the house remembered her name"},
        ]
        rels = [{"from": "A", "to": "B", "type": "friend",
                 "evidence": "Real words"}]
        vc, vr, vq, stats = pp.verify_all_evidence(chars, rels, [], SRC)
        # Short character evidence: claim kept, evidence cleared,
        # offered but unverified, excluded from the stats.
        self.assertEqual(vc[0]["name"], "Mother")
        self.assertEqual(vc[0]["evidence"], "")
        self.assertFalse(vc[0]["evidence_verified"])
        self.assertTrue(vc[0]["evidence_offered"])
        # Real character evidence still verifies.
        self.assertTrue(vc[1]["evidence_verified"])
        # Short relationship evidence behaves the same as before.
        self.assertEqual(vr[0]["evidence"], "")
        self.assertFalse(vr[0]["evidence_verified"])
        self.assertTrue(vr[0]["evidence_offered"])
        # Only the one checkable evidence string was checked.
        self.assertEqual(stats["evidence_checked"], 1)
        self.assertEqual(stats["evidence_verified"], 1)
        self.assertEqual(stats["quotes_dropped"], 0)
        self.assertEqual(vq, [])


class TestQuoteRowBoundary(unittest.TestCase):
    def test_build_quote_rows_drops_short_and_unverified(self):
        result = {"quotes": [
            {"text": "Blood", "chapter": "Ch 1"},
            {"text": "Blood calls", "chapter": "Ch 1"},
            {"text": "Real words", "chapter": "Ch 1"},
            {"text": "an unverified notable line here",
             "evidence_verified": False, "chapter": "Ch 1"},
            {"text": "the house remembered her name",
             "evidence_verified": True, "chapter": "Ch 1"},
            {"text": "a brand new quote", "chapter": "Ch 1"},
        ]}
        rows = pp._build_quote_rows(1, result, set())
        # The verified 3+-word quote ships; so does the unstamped
        # 3+-word quote (the shape payloads carried before the stamp
        # existed). Short quotes and the stamped-unverified quote do
        # not reach book_quotes.
        self.assertEqual([r["quote"] for r in rows],
                         ["the house remembered her name",
                          "a brand new quote"])


# --- write_claims integration: book_quotes AND book_meta --------------
# The in-memory Supabase stand-in follows the same shape as
# tests/test_replace_wholesale.py's (the write path's PostgREST subset).


class _StatefulSB:
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

    def __call__(self, table, params=None, method="GET", data=None,
                 prefer=None):
        self.calls.append((table, method, params or "", data))
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


class TestWriteClaimsShipsOnlyShippableQuotes(unittest.TestCase):
    def setUp(self):
        self._old_sb = pp.sb

    def tearDown(self):
        pp.sb = self._old_sb

    def test_short_and_unverified_quotes_do_not_ship(self):
        fake = _StatefulSB({})
        pp.sb = fake
        result = {
            "title": "Test Book", "isbn": "9780000000001",
            "tropes": [], "trope_confidence": {}, "trope_catalog_map": {},
            "trope_chapters": {}, "triggers": [], "characters": [],
            "relationships": [],
            "quotes": [
                {"text": "Blood", "chapter": "Ch 1"},
                {"text": "Blood calls", "chapter": "Ch 1"},
                {"text": "Real words", "chapter": "Ch 1"},
                {"text": "an unverified notable line here",
                 "evidence_verified": False, "chapter": "Ch 1"},
                {"text": "a brand new quote", "chapter": "Ch 1"},
            ],
        }
        errors = pp.write_claims(1, result)
        self.assertEqual(errors, 0)
        self.assertEqual([r["quote"] for r in fake.tables["book_quotes"]],
                         ["a brand new quote"])
        meta_rows = fake.tables.get("book_meta", [])
        self.assertEqual(len(meta_rows), 1)
        self.assertEqual(meta_rows[0]["data"]["notable_quotes"],
                         [{"text": "a brand new quote",
                           "chapter": "Ch 1"}])


if __name__ == "__main__":
    unittest.main()
