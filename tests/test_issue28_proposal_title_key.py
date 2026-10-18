"""Issue #28: trope_proposals replace-DELETE keyed by ISBN-or-title.

trope_proposals has no work_id: its only book identity is book_key,
which the write path sets to the book's ISBN, or — when the book has
no ISBN — its raw title. Titles are not unique across works, so
reprocessing one no-ISBN book used to delete every processor pending
proposal carrying that title, including a different work's rows.

Pinned behaviour after the fix:
  * a title-only book never issues the replace-DELETE at all: its new
    proposals are inserted additively, the surviving pending rows are
    deduped against (never duplicated beside), and a printed line
    names the book and says the replacement was skipped because its
    identity is title-only;
  * an ISBN-keyed book replaces exactly as before — DELETE + insert,
    its own ISBN-keyed pending rows only — even when its title
    collides with another work's title;
  * the ISBN delete key uses the cleaned ISBN-13 form the proposal
    writer itself stores, so a hyphenated ISBN in the result cannot
    make the fetch/delete miss the rows earlier writes stored.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest tests.test_issue28_proposal_title_key -v
"""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from urllib.parse import unquote

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("pp_issue28", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _StatefulSB:
    """In-memory Supabase stand-in implementing the PostgREST subset the
    write path uses: GET/POST/PATCH/DELETE with eq/neq/ilike filters."""

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
            if key in ("select", "limit", "on_conflict", "order", "offset"):
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

    def deletes_for(self, table):
        return [c for c in self.calls if c[0] == table and c[1] == "DELETE"]


def _result(**over):
    r = {"title": "Test Book", "isbn": "9780000000001", "tropes": [],
         "trope_confidence": {}, "trope_catalog_map": {}, "trope_chapters": {},
         "triggers": [], "characters": [], "relationships": [], "quotes": []}
    r.update(over)
    return r


def _conf(*names):
    return {pp._tnorm(n): 0.95 for n in names}


_APP_UID = "aaaaaaaa-1111-2222-3333-444444444444"


class _Base(unittest.TestCase):
    def setUp(self):
        self._old_sb = pp.sb
        self._old_cfg = {k: pp.CONFIG.get(k)
                         for k in ("supabase_url", "supabase_key",
                                   "proposer_user_id")}

    def tearDown(self):
        pp.sb = self._old_sb
        for k, v in self._old_cfg.items():
            if v is None:
                pp.CONFIG.pop(k, None)
            else:
                pp.CONFIG[k] = v

    def install(self, fake):
        pp.sb = fake
        self.fake = fake
        return fake


class TestTitleOnlyProposalReplace(_Base):
    """The issue reproduction: two distinct no-ISBN works that share a
    byte-identical title. Processing the second must leave the first
    work's proposals exactly as they were."""

    def _seed(self):
        return _StatefulSB({
            "trope_proposals": [
                # The FIRST book's own pending processor proposal.
                {"id": 1, "name": "First Book Trope",
                 "name_key": pp._tnorm("First Book Trope"),
                 "book_key": "Crescent City",
                 "proposed_by": None, "status": "pending"},
                # A human/app suggestion for the first book, same key.
                {"id": 2, "name": "App Suggested Trope",
                 "name_key": pp._tnorm("App Suggested Trope"),
                 "book_key": "Crescent City",
                 "proposed_by": _APP_UID, "status": "pending"},
                # A human-reviewed row for the first book.
                {"id": 3, "name": "Reviewed Trope",
                 "name_key": pp._tnorm("Reviewed Trope"),
                 "book_key": "Crescent City",
                 "proposed_by": None, "status": "rejected"},
            ]})

    def _second_book_result(self):
        return _result(
            title="Crescent City", isbn=None,
            tropes=["First Book Trope", "Second Book Trope"],
            trope_confidence=_conf("First Book Trope", "Second Book Trope"))

    def test_second_same_title_book_leaves_first_books_rows_intact(self):
        fake = self.install(self._seed())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            errors = pp.write_claims(2, self._second_book_result())
        self.assertEqual(errors, 0)
        out = buf.getvalue()

        props = fake.tables["trope_proposals"]
        # Every seeded row survives untouched, ids and all — the first
        # book's processor row, its app suggestion, and its reviewed row.
        self.assertEqual(sorted(r["id"] for r in props
                                if r["id"] < 1000), [1, 2, 3])
        # The first book's pending trope is still the original row: the
        # second book re-detecting the same trope name must not delete
        # it and re-insert a copy under a new id.
        first = [r for r in props
                 if r["name_key"] == pp._tnorm("First Book Trope")]
        self.assertEqual([r["id"] for r in first], [1])
        # The second book's genuinely new trope ships additively.
        second = [r for r in props
                  if r["name_key"] == pp._tnorm("Second Book Trope")]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["book_key"], "Crescent City")
        # No replace-DELETE was issued for proposals at all.
        self.assertEqual(fake.deletes_for("trope_proposals"), [])
        # The skip is visible to the operator and names the book.
        self.assertIn("Crescent City", out)
        self.assertIn("skipped", out)
        self.assertIn("title-only", out)

    def test_plan_for_title_only_book_deletes_no_proposals(self):
        fake = self.install(self._seed())
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "test-key"
        plan = pp.plan_replace(2, self._second_book_result())
        self.assertTrue(plan)
        # The plan mirrors the write: nothing would be deleted, and
        # only the genuinely new trope would be inserted (the surviving
        # pending trope is not planned as a duplicate insert).
        self.assertEqual(plan["would_delete"]["trope_proposals"], 0)
        self.assertEqual(plan["would_insert"]["proposals"], 1)
        self.assertEqual(fake.deletes_for("trope_proposals"), [])


class TestIsbnKeyedProposalReplace(_Base):
    def test_isbn_book_replaces_exactly_as_today(self):
        isbn = "9780000000001"
        fake = self.install(_StatefulSB({
            "trope_proposals": [
                {"id": 1, "name": "Stale Own Trope",
                 "name_key": pp._tnorm("Stale Own Trope"),
                 "book_key": isbn,
                 "proposed_by": None, "status": "pending"},
                {"id": 2, "name": "Reviewed Trope",
                 "name_key": pp._tnorm("Reviewed Trope"),
                 "book_key": isbn,
                 "proposed_by": None, "status": "rejected"},
                {"id": 3, "name": "Other Book Trope",
                 "name_key": pp._tnorm("Other Book Trope"),
                 "book_key": "9780000000002",
                 "proposed_by": None, "status": "pending"},
            ]}))
        res = _result(
            tropes=["Stale Own Trope", "Fresh Own Trope"],
            trope_confidence=_conf("Stale Own Trope", "Fresh Own Trope"))
        with contextlib.redirect_stdout(io.StringIO()):
            errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)

        props = fake.tables["trope_proposals"]
        # Reviewed and other-book rows survive untouched.
        self.assertEqual(sorted(r["id"] for r in props if r["id"] < 1000),
                         [2, 3])
        # The stale own proposal was replaced: exactly one row, new id.
        stale = [r for r in props
                 if r["name_key"] == pp._tnorm("Stale Own Trope")]
        self.assertEqual(len(stale), 1)
        self.assertNotEqual(stale[0]["id"], 1)
        self.assertEqual(stale[0]["book_key"], isbn)
        # The fresh trope was proposed alongside.
        self.assertEqual(
            len([r for r in props
                 if r["name_key"] == pp._tnorm("Fresh Own Trope")]), 1)
        # Exactly one DELETE, keyed by ISBN with the pending + scope
        # conditions, exactly as before.
        dels = fake.deletes_for("trope_proposals")
        self.assertEqual(len(dels), 1)
        self.assertIn(f"book_key=eq.{isbn}", dels[0][2])
        self.assertIn("status=eq.pending", dels[0][2])
        self.assertIn("proposed_by=is.null", dels[0][2])

    def test_isbn_book_with_colliding_title_deletes_only_isbn_rows(self):
        isbn = "9780000000001"
        fake = self.install(_StatefulSB({
            "trope_proposals": [
                # This book's own ISBN-keyed pending proposal.
                {"id": 1, "name": "Isbn Book Trope",
                 "name_key": pp._tnorm("Isbn Book Trope"),
                 "book_key": isbn,
                 "proposed_by": None, "status": "pending"},
                # Another work whose title-keyed proposals carry the
                # same title this book has — a title-keyed delete
                # would take these; the ISBN delete must not.
                {"id": 2, "name": "Title Book Trope",
                 "name_key": pp._tnorm("Title Book Trope"),
                 "book_key": "Shared Title",
                 "proposed_by": None, "status": "pending"},
                # A third work, keyed by its own different ISBN.
                {"id": 3, "name": "Third Book Trope",
                 "name_key": pp._tnorm("Third Book Trope"),
                 "book_key": "9780000000002",
                 "proposed_by": None, "status": "pending"},
            ]}))
        res = _result(
            title="Shared Title", isbn=isbn,
            tropes=["Isbn Book Trope", "Fresh Trope"],
            trope_confidence=_conf("Isbn Book Trope", "Fresh Trope"))
        with contextlib.redirect_stdout(io.StringIO()):
            errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)

        props = fake.tables["trope_proposals"]
        # Both other works' rows survive with their original ids.
        self.assertEqual(sorted(r["id"] for r in props if r["id"] < 1000),
                         [2, 3])
        # Own stale row replaced exactly once.
        own = [r for r in props
               if r["name_key"] == pp._tnorm("Isbn Book Trope")]
        self.assertEqual(len(own), 1)
        self.assertNotEqual(own[0]["id"], 1)
        # The one DELETE keys on the ISBN, never on the shared title.
        dels = fake.deletes_for("trope_proposals")
        self.assertEqual(len(dels), 1)
        self.assertIn(f"book_key=eq.{isbn}", dels[0][2])
        self.assertNotIn("Shared", dels[0][2])

    def test_isbn_delete_uses_cleaned_form_the_writer_stores(self):
        # The stored rows carry the cleaned ISBN-13; the run's result
        # carries the same ISBN hyphenated. Fetch, delete and insert
        # must all use the cleaned form, or the replace silently
        # misses the book's own earlier rows.
        cleaned = "9780306406157"
        fake = self.install(_StatefulSB({
            "trope_proposals": [
                {"id": 1, "name": "Stale Trope",
                 "name_key": pp._tnorm("Stale Trope"),
                 "book_key": cleaned,
                 "proposed_by": None, "status": "pending"},
            ]}))
        res = _result(
            isbn="978-0-306-40615-7",
            tropes=["Stale Trope", "Fresh Trope"],
            trope_confidence=_conf("Stale Trope", "Fresh Trope"))
        with contextlib.redirect_stdout(io.StringIO()):
            errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)

        props = fake.tables["trope_proposals"]
        # The cleaned-keyed stale row was actually replaced.
        stale = [r for r in props
                 if r["name_key"] == pp._tnorm("Stale Trope")]
        self.assertEqual(len(stale), 1)
        self.assertNotEqual(stale[0]["id"], 1)
        # Every row this run wrote carries the cleaned form.
        self.assertEqual({r["book_key"] for r in props}, {cleaned})
        dels = fake.deletes_for("trope_proposals")
        self.assertEqual(len(dels), 1)
        self.assertIn(f"book_key=eq.{cleaned}", dels[0][2])


if __name__ == "__main__":
    unittest.main()
