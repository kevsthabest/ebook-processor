"""Write-path compensation (issue #5): a failed insert must never leave
a claims table worse than the section found it.

Each write_claims section deletes the work's processor rows and then
inserts the new run's rows as a separate call. sb() returns None on
any error, so a failed POST after a committed DELETE used to wipe the
book's processor claims for that table until reprocessing. The fix
holds the rows the DELETE returned and re-inserts them when the new
rows cannot land; if that restore also fails, a loud RESTORE FAILED
line names the table and work so the operator knows the book needs
reprocessing.

Companion shapes pinned here:
  * sb() serializes its JSON body inside its failure discipline, so a
    non-serializable row value is a counted failure, not a raise out
    of write_claims mid-sequence;
  * a truthy non-dict book_meta blob is a counted, contained failure
    (the stored blob is not overwritten and later sections still run).

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest tests.test_write_path_compensation -v
"""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from urllib.parse import unquote

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable_comp", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _CompSB:
    """In-memory Supabase stand-in (the _StatefulSB subset from
    test_replace_wholesale) plus scripted POST failures: a table in
    fail_first_post fails that many initial POSTs with None (the
    transport-error shape), a table in fail_all_posts fails every
    POST. Failed POSTs mutate nothing, exactly like sb() returning
    None before the server commits anything."""

    def __init__(self, tables=None, fail_first_post=None, fail_all_posts=()):
        self.tables = {t: [dict(r) for r in rows]
                       for t, rows in (tables or {}).items()}
        self.calls = []
        self.fail_first_post = dict(fail_first_post or {})
        self.fail_all_posts = set(fail_all_posts)
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

    def __call__(self, table, params=None, method="GET", data=None, prefer=None):
        self.calls.append((table, method, params or "", data))
        rows = self.tables.setdefault(table, [])
        if method == "GET":
            return [dict(r) for r in rows if self._match(r, params)]
        if method == "POST":
            if table in self.fail_all_posts:
                return None
            if self.fail_first_post.get(table, 0) > 0:
                self.fail_first_post[table] -= 1
                return None
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
            self.tables[table] = [r for r in rows if not self._match(r, params)]
            return [dict(r) for r in gone]
        return []

    def sequence(self):
        return [(t, m) for t, m, _p, _d in self.calls]


def _result(**over):
    r = {"title": "Test Book", "isbn": "9780000000001", "tropes": [],
         "trope_confidence": {}, "trope_catalog_map": {}, "trope_chapters": {},
         "triggers": [], "characters": [], "relationships": [], "quotes": []}
    r.update(over)
    return r


def _trig(warning, conf=0.8):
    return {"warning": warning, "severity": "mentioned",
            "severity_claimed": "mentioned", "chapters": ["A"],
            "chapter_count": 1, "frequency": 0.5, "prominence": "low",
            "evidence": [{"quote": "q", "chapter": "A"}], "confidence": conf}


class _CompBase(unittest.TestCase):
    def setUp(self):
        self._old_sb = pp.sb

    def tearDown(self):
        pp.sb = self._old_sb

    def install(self, fake):
        pp.sb = fake
        self.fake = fake
        return fake


class TestFailedInsertRestoresDeletedRows(_CompBase):
    """Issue #5 reproduction, across all five delete/insert windows:
    every section's first POST fails after its DELETE committed, so
    every table must end holding its ORIGINAL processor rows again,
    with human rows untouched and one error counted per section."""

    def test_all_sections_restore_on_failed_insert(self):
        fake = self.install(_CompSB(
            {
                "book_trope_claims": [
                    {"id": 1, "work_id": 1, "trope_id": "cat-old",
                     "status": "candidate", "source_type": "ai"},
                    {"id": 2, "work_id": 1, "trope_id": "cat-human",
                     "status": "candidate", "source_type": "user"},
                    {"id": 3, "work_id": 1, "trope_id": "cat-rej",
                     "status": "rejected", "source_type": "ai"},
                ],
                "trope_proposals": [
                    {"id": 4, "name": "Stale Own Trope",
                     "name_key": pp._tnorm("Stale Own Trope"),
                     "book_key": "9780000000001",
                     "proposed_by": None, "status": "pending"},
                    {"id": 5, "name": "App Trope",
                     "name_key": "app trope",
                     "book_key": "9780000000001",
                     "proposed_by": "aaaaaaaa-1111-2222-3333-444444444444",
                     "status": "pending"},
                ],
                "book_trigger_claims": [
                    {"id": 6, "work_id": 1, "warning": "old warning",
                     "status": "candidate", "source_type": "ai"},
                    {"id": 7, "work_id": 1, "warning": "human warning",
                     "status": "candidate", "source_type": "user"},
                ],
                "book_characters": [
                    {"id": 8, "work_id": 1, "name": "Old Char",
                     "status": "candidate", "source_type": "ai"},
                    {"id": 9, "work_id": 1, "name": "Human Char",
                     "status": "candidate", "source_type": "user"},
                ],
                "book_quotes": [
                    {"id": 10, "work_id": 1, "quote": "old pipeline quote",
                     "source_type": "pipeline"},
                    {"id": 11, "work_id": 1, "quote": "human quote",
                     "source_type": "user"},
                ],
            },
            fail_first_post={"book_trope_claims": 1, "trope_proposals": 1,
                             "book_trigger_claims": 1, "book_characters": 1,
                             "book_quotes": 1}))
        res = _result(
            tropes=["Mind Game", "Zqx Wobble"],
            trope_confidence={pp._tnorm("Zqx Wobble"): 0.95},
            trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]},
            triggers=[_trig("violence")],
            characters=[{"name": "Hero", "role": "protagonist"}],
            quotes=[{"text": "a brand new quote", "chapter": "Ch 1"}],
        )
        with contextlib.redirect_stdout(io.StringIO()):
            errors = pp.write_claims(
                1, res, trope_mappings={"Mind Game": "cat-a"})

        # One counted error per failed insert; the restores landed.
        self.assertEqual(errors, 5)
        # Original processor rows are back, ids and all; no new rows.
        self.assertEqual(
            sorted((r["id"], r["trope_id"])
                   for r in fake.tables["book_trope_claims"]),
            [(1, "cat-old"), (2, "cat-human"), (3, "cat-rej")])
        self.assertEqual(
            sorted(r["id"] for r in fake.tables["trope_proposals"]),
            [4, 5])
        self.assertEqual(
            sorted((r["id"], r["warning"])
                   for r in fake.tables["book_trigger_claims"]),
            [(6, "old warning"), (7, "human warning")])
        self.assertEqual(
            sorted((r["id"], r["name"])
                   for r in fake.tables["book_characters"]),
            [(8, "Old Char"), (9, "Human Char")])
        self.assertEqual(
            sorted((r["id"], r["quote"])
                   for r in fake.tables["book_quotes"]),
            [(10, "old pipeline quote"), (11, "human quote")])


class TestRestoreFailureIsLoud(_CompBase):
    def test_restore_failure_prints_restore_failed_and_counts(self):
        fake = self.install(_CompSB(
            {"book_trigger_claims": [
                {"id": 1, "work_id": 1, "warning": "old warning",
                 "status": "candidate", "source_type": "ai"},
                {"id": 2, "work_id": 1, "warning": "human warning",
                 "status": "candidate", "source_type": "user"}]},
            fail_all_posts={"book_trigger_claims"}))
        res = _result(triggers=[_trig("violence")])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            errors = pp.write_claims(1, res)
        out = buf.getvalue()
        # The loud signal names the table and the work, beyond the count.
        self.assertIn("RESTORE FAILED", out)
        self.assertIn("book_trigger_claims", out)
        # Insert failure + restore failure are both counted.
        self.assertGreaterEqual(errors, 2)
        # The human row was never in scope and survives even this.
        self.assertEqual(
            [r["warning"] for r in fake.tables["book_trigger_claims"]],
            ["human warning"])


class TestHappyPathCallSequenceUnchanged(_CompBase):
    """No extra transport calls when inserts succeed: compensation is
    built from rows the existing DELETE already returns, so the happy
    path's call sequence is exactly the pre-fix sequence."""

    def test_happy_path_sequence(self):
        fake = self.install(_CompSB({
            "book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-old",
                 "status": "candidate", "source_type": "ai"}],
            "book_trigger_claims": [
                {"id": 2, "work_id": 1, "warning": "old warning",
                 "status": "candidate", "source_type": "ai"}],
            "book_characters": [
                {"id": 3, "work_id": 1, "name": "Old Char",
                 "status": "candidate", "source_type": "ai"}],
            "book_quotes": [
                {"id": 4, "work_id": 1, "quote": "old pipeline quote",
                 "source_type": "pipeline"}],
        }))
        res = _result(
            isbn=None,
            tropes=["Mind Game"],
            trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]},
            triggers=[_trig("murder")],
            characters=[{"name": "Hero", "role": "protagonist"}],
            quotes=[{"text": "a brand new quote", "chapter": "Ch 1"}],
        )
        with contextlib.redirect_stdout(io.StringIO()):
            errors = pp.write_claims(
                1, res, trope_mappings={"Mind Game": "cat-a"})
        self.assertEqual(errors, 0)
        self.assertEqual(fake.sequence(), [
            ("book_trope_claims", "GET"),
            ("book_trope_claims", "DELETE"),
            ("book_trope_claims", "POST"),
            ("trope_proposals", "GET"),
            ("trope_proposals", "GET"),
            ("book_trigger_claims", "GET"),
            ("book_trigger_claims", "DELETE"),
            ("book_trigger_claims", "POST"),
            ("book_characters", "GET"),
            ("book_characters", "DELETE"),
            ("book_characters", "POST"),
            ("characters", "GET"),
            ("book_quotes", "GET"),
            ("book_quotes", "DELETE"),
            ("book_quotes", "POST"),
        ])


class TestNonSerializableRowContained(_CompBase):
    def test_sb_returns_none_for_non_serializable_data(self):
        # The real sb(): serializing the body used to happen outside
        # its try block, so this raised TypeError to the caller. With
        # serialization inside the failure discipline, write_claims
        # sees the None every other transport failure produces (and
        # TestFailedInsertRestoresDeletedRows pins what write_claims
        # then does with that None when a DELETE already committed).
        old_cfg = {k: pp.CONFIG.get(k)
                   for k in ("supabase_url", "supabase_key")}
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "test-key"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                out = pp.sb("book_trope_claims", method="POST",
                            data={"trope_id": object()})
        finally:
            for k, v in old_cfg.items():
                if v is None:
                    pp.CONFIG.pop(k, None)
                else:
                    pp.CONFIG[k] = v
        self.assertIsNone(out)


class TestBookMetaNonDictBlobContained(_CompBase):
    def test_non_dict_stored_blob_counts_and_later_sections_run(self):
        fake = self.install(_CompSB({
            "book_meta": [
                {"isbn": "9780000000001", "data": "a corrupted string blob"}],
            "book_quotes": [
                {"id": 1, "work_id": 1, "quote": "old pipeline quote",
                 "source_type": "pipeline"}],
        }))
        # 2026-10-18 (issue #41): quote text is 3 words because quotes
        # under 3 words no longer ship (unverifiable); the property
        # under test (the quotes section still runs after a contained
        # book_meta failure) is unchanged.
        res = _result(quotes=[{"text": "a fresh quote", "chapter": "Ch 1"}])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            errors = pp.write_claims(1, res)
        # Counted, not raised; the stored blob was NOT overwritten
        # with a partial merge.
        self.assertEqual(errors, 1)
        self.assertEqual(fake.tables["book_meta"][0]["data"],
                         "a corrupted string blob")
        self.assertFalse([c for c in fake.calls
                          if c[0] == "book_meta" and c[1] == "POST"])
        # The quotes section, which runs after book_meta, still ran:
        # the book is not left mixed-generation by an escape.
        self.assertEqual(
            [r["quote"] for r in fake.tables["book_quotes"]],
            ["a fresh quote"])


if __name__ == "__main__":
    unittest.main()
