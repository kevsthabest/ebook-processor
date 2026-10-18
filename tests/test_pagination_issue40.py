"""Issue #40: replace-wholesale preserved-set fetches truncated at
limit=1000.

The fetches that build replace-wholesale's preserved / rejected /
deletable partition (and the roster curation anchors, which partition
the same way) hard-capped at limit=1000 with no pagination and no
order=. Past the cap the partition silently lost human rows: on
tables with a unique constraint the replacement insert then aborted
the whole section POST, and on tables without one it inserted a
duplicate beside the human row — a rejected trope resurrected as a
candidate.

These tests run against a PostgREST-faithful fake: it HONOURS
limit / offset / order (the suites' older fake ignores limit, which
is exactly why this class shipped green). Rows past position 1000
must reach the partition.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest tests.test_pagination_issue40 -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("pp_pagination40", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _PaginatedSB:
    """PostgREST-faithful stand-in: GET honours eq/neq/ilike/is and
    or=(...) filters, then order=, then offset=, then limit= — in
    that order, like PostgREST. POST models the issue's failure arms:
    book_characters / book_trigger_claims carry a unique constraint
    on (work_id, identity), so a batch that would insert a duplicate
    beside an existing row aborts whole (returns None, inserts
    nothing); the other claim tables insert duplicates silently."""

    _UNIQUE_TABLES = {"book_characters": "name",
                      "book_trigger_claims": "warning"}

    def __init__(self, tables=None):
        self.tables = {t: [dict(r) for r in rows]
                       for t, rows in (tables or {}).items()}
        self.calls = []
        self._next_id = 900000

    # -- filter machinery -------------------------------------------------
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

    def _match_cond(self, row, cond):
        col, _, rest = cond.partition(".")
        op, _, v = rest.partition(".")
        return self._match_val(row.get(col), op, unquote(v))

    def _parse(self, params):
        filters, meta = [], {}
        for part in (params or "").lstrip("?").split("&"):
            if not part or "=" not in part:
                continue
            key, val = part.split("=", 1)
            if key in ("select", "limit", "offset", "order", "on_conflict"):
                meta[key] = val
            else:
                filters.append((key, val))
        return filters, meta

    def _match(self, row, filters):
        for key, val in filters:
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

    # -- sb() interface -----------------------------------------------------
    def __call__(self, table, params=None, method="GET", data=None,
                 prefer=None, schema=None):
        self.calls.append((table, method, params or "", data))
        rows = self.tables.setdefault(table, [])
        filters, meta = self._parse(params)
        if method == "GET":
            out = [dict(r) for r in rows if self._match(r, filters)]
            order = meta.get("order")
            if order:
                for spec in reversed([s for s in order.split(",") if s]):
                    col, _, direction = spec.partition(".")
                    out.sort(key=lambda r, c=col: (r.get(c) is None,
                                                   r.get(c)),
                             reverse=(direction == "desc"))
            offset = int(meta["offset"]) if "offset" in meta else 0
            if offset:
                out = out[offset:]
            if "limit" in meta:
                out = out[:int(meta["limit"])]
            return out
        if method == "POST":
            items = data if isinstance(data, list) else [data]
            id_col = self._UNIQUE_TABLES.get(table)
            if id_col is not None:
                taken = {(r.get("work_id"),
                          str(r.get(id_col) or "").lower()) for r in rows}
                batch = set()
                for d in items:
                    key = (d.get("work_id"),
                           str(d.get(id_col) or "").lower())
                    if key in taken or key in batch:
                        return None  # unique violation: whole batch aborts
                    batch.add(key)
            out = []
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
                if self._match(r, filters):
                    r.update(data or {})
                    out.append(dict(r))
            return out
        if method == "DELETE":
            gone = [r for r in rows if self._match(r, filters)]
            self.tables[table] = [r for r in rows
                                  if not self._match(r, filters)]
            return [dict(r) for r in gone]
        return []

    def get_calls(self, table):
        return [c for c in self.calls if c[0] == table and c[1] == "GET"]


def _result(**over):
    r = {"title": "Test Book", "isbn": "9780000000001", "tropes": [],
         "trope_confidence": {}, "trope_catalog_map": {}, "trope_chapters": {},
         "triggers": [], "characters": [], "relationships": [], "quotes": []}
    r.update(over)
    return r


class _Base(unittest.TestCase):
    def setUp(self):
        self._old_sb = pp.sb

    def tearDown(self):
        pp.sb = self._old_sb

    def install(self, fake):
        pp.sb = fake
        self.fake = fake
        return fake


def _char_rows_past_cap():
    """1002 book_characters rows for work 1: 1000 processor filler
    candidates first, then a human-confirmed and a human-rejected row
    PAST position 1000 — the rows the capped fetch used to lose."""
    rows = [{"id": i + 1, "work_id": 1, "name": f"Filler {i:04d}",
             "status": "candidate", "source_type": "ai"}
            for i in range(1000)]
    rows.append({"id": 1001, "work_id": 1, "name": "Confirmed Hero",
                 "status": "confirmed", "source_type": "ai"})
    rows.append({"id": 1002, "work_id": 1, "name": "Rejected Villain",
                 "status": "rejected", "source_type": "ai"})
    return rows


class TestClaimRowsPastCap(_Base):
    def test_partition_contains_human_rows_past_position_1000(self):
        self.install(_PaginatedSB(
            {"book_characters": _char_rows_past_cap()}))
        rows = pp._claim_rows("book_characters", 1)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 1002)
        preserved, rejected, _ndel = pp._partition_claims(
            "book_characters", rows)
        self.assertIn("confirmed hero", preserved)
        self.assertIn("rejected villain", preserved)
        self.assertEqual(rejected, {"rejected villain"})

    def test_write_claims_neither_aborts_nor_duplicates(self):
        fake = self.install(_PaginatedSB(
            {"book_characters": _char_rows_past_cap()}))
        res = _result(characters=[
            {"name": "Confirmed Hero", "role": "protagonist"},
            {"name": "Rejected Villain", "role": "minor"}])
        errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)
        chars = fake.tables["book_characters"]
        # The 1000 deletable fillers were replaced away; the two human
        # rows survive exactly once each, with their original ids and
        # curation statuses — no aborted POST, no duplicate candidate
        # inserted beside either human row.
        self.assertEqual(
            sorted((r["id"], r["name"], r["status"]) for r in chars),
            [(1001, "Confirmed Hero", "confirmed"),
             (1002, "Rejected Villain", "rejected")])


class TestProposalStatePastCap(_Base):
    def test_global_proposals_returns_all_1001_keys(self):
        rows = [{"id": i + 1, "name_key": f"key-{i:04d}",
                 "book_key": "some-other-book", "proposed_by": None,
                 "status": "pending"} for i in range(1001)]
        self.install(_PaginatedSB({"trope_proposals": rows}))
        all_keys, own = pp._proposal_state(_result())
        self.assertIsNotNone(all_keys)
        self.assertEqual(len(all_keys), 1001)
        self.assertIn("key-1000", all_keys)
        self.assertEqual(own, [])

    def test_own_proposals_past_cap_returned_in_full(self):
        rows = [{"id": i + 1, "name_key": f"own-{i:04d}",
                 "book_key": "9780000000001", "proposed_by": None,
                 "status": "pending"} for i in range(1001)]
        self.install(_PaginatedSB({"trope_proposals": rows}))
        all_keys, own = pp._proposal_state(_result())
        self.assertIsNotNone(all_keys)
        self.assertEqual(len(own), 1001)
        self.assertEqual(own[-1]["name_key"], "own-1000")


class TestRosterAnchorsPastCap(_Base):
    def setUp(self):
        super().setUp()
        self._old_learned = pp._LEARNED
        self._tmp = tempfile.TemporaryDirectory()
        pp._LEARNED = pp.LearnedState(
            learn_dir=Path(self._tmp.name) / "learn", enabled=True)
        self.addCleanup(self._restore)

    def _restore(self):
        pp._LEARNED = self._old_learned
        self._tmp.cleanup()

    def test_roster_anchors_past_cap_return_full_set(self):
        rows = [{"id": i + 1, "work_id": "wid-1",
                 "name": f"Contributor {i:04d}", "status": "candidate",
                 "source_type": "human", "aliases": []}
                for i in range(1000)]
        rows.append({"id": 1001, "work_id": "wid-1",
                     "name": "Confirmed Person", "status": "confirmed",
                     "source_type": "ai", "aliases": []})
        rows.append({"id": 1002, "work_id": "wid-1",
                     "name": "Rejected Person", "status": "rejected",
                     "source_type": "ai", "aliases": []})
        self.install(_PaginatedSB({"book_characters": rows}))
        anchors = pp._roster_fetch_anchors("wid-1")
        self.assertIsNotNone(anchors)
        self.assertEqual(len(anchors), 1002)
        by_name = {a["name"]: a for a in anchors}
        self.assertEqual(by_name["Confirmed Person"]["kind"], "confirmed")
        self.assertEqual(by_name["Rejected Person"]["kind"], "rejected")


class TestBelowCapPin(_Base):
    def test_999_rows_exactly_one_page_identical_result(self):
        seed = [{"id": i + 1, "work_id": 1, "name": f"Char {i:04d}",
                 "status": "candidate", "source_type": "ai"}
                for i in range(999)]
        fake = self.install(_PaginatedSB({"book_characters": seed}))
        rows = pp._claim_rows("book_characters", 1)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 999)
        self.assertEqual(sorted(r["id"] for r in rows),
                         [r["id"] for r in seed])
        gets = fake.get_calls("book_characters")
        self.assertEqual(len(gets), 1)


class TestFetchAllPins(_Base):
    """Pins owed from the v2.13.40 gate: _sb_fetch_all's failure and
    exact-multiple contracts were exercised only implicitly by the
    partition tests above."""

    def test_page2_fetch_failure_returns_none(self):
        # First page full (1000 rows), second page fails: the helper
        # must return None (the read failed, trust nothing), never a
        # silently truncated 1000-row list dressed as complete.
        calls = []

        def flaky_sb(table, params=None, method="GET", data=None,
                     prefer=None, schema=None):
            calls.append(params or "")
            if len(calls) == 1:
                return [{"id": i + 1} for i in range(1000)]
            return None

        pp.sb = flaky_sb
        self.assertIsNone(pp._sb_fetch_all("book_characters",
                                            "?select=id"))
        self.assertEqual(len(calls), 2)

    def test_exact_multiple_2000_rows_terminates_with_full_set(self):
        # 2000 rows is an exact multiple of the 1000-row page size:
        # the helper must fetch one more (empty) page, see it is
        # short, and stop with the full set — not loop forever and
        # not stop a page early at 1000.
        seed = [{"id": i + 1, "work_id": 1, "name": f"Char {i:04d}",
                 "status": "candidate", "source_type": "ai"}
                for i in range(2000)]
        fake = self.install(_PaginatedSB({"book_characters": seed}))
        rows = pp._sb_fetch_all("book_characters", "?select=id,work_id")
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 2000)
        self.assertEqual([r["id"] for r in rows],
                         [r["id"] for r in seed])
        self.assertEqual(len(fake.get_calls("book_characters")), 3)


if __name__ == "__main__":
    unittest.main()
