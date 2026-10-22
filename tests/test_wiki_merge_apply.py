"""Wiki-ingestion Slice 1 / Phase 3 tests: --stage merge plan + --apply.

Covers docs/wiki-ingestion-spec.md §2 (D3-D10), §3.3, §3.4, §4, §7 (A2,
A5-A11), §8:
- Full-cast plan generation (principals + minors, tier stamping).
- Evidence-less relationship survivors shipped as type='other',
  claimed_type=<original>, validation_state='needs_evidence'.
- Symmetric vs directed edge direction mapping ('mutual' vs 'a_to_b').
- Cross-book auto-series links (suggested by default, confirmed when
  wiki_links_auto_confirm=True; no cross-author/cross-series links).
- Fingerprint mismatch refusal on analysis artefact load (A11).
- Schema preflight refusal when migration columns are absent.
- Idempotent publish (A9): applying the same plan twice produces zero
  row diffs across all tables, preserving primary keys and FKs.
- Curation safety (A10): rejected characters, edges, links, tropes, and
  triggers are never resurrected; confirmed rows are never demoted;
  canonical duplicate characters are never auto-merged.
"""

import contextlib
import copy
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _StatefulSB:
    """In-memory PostgREST stand-in supporting GET/POST/PATCH/DELETE."""

    def __init__(self, tables=None, fail_preflight_tables=()):
        self.tables = {t: [ copy.deepcopy(r) for r in rows ]
                       for t, rows in (tables or {}).items()}
        self.calls = []
        self.fail_preflight_tables = set(fail_preflight_tables)
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
                 prefer=None, schema=None):
        self.calls.append((table, method, params or "", copy.deepcopy(data)))
        if (method == "GET"
                and table in self.fail_preflight_tables
                and "limit=1" in (params or "")):
            return None
        rows = self.tables.setdefault(table, [])
        if method == "GET":
            return [copy.deepcopy(r) for r in rows if self._match(r, params)]
        if method == "POST":
            out = []
            items = data if isinstance(data, list) else [data]
            for d in items:
                r = copy.deepcopy(d)
                r.setdefault("id", self._next_id)
                self._next_id += 1
                rows.append(r)
                out.append(copy.deepcopy(r))
            return out
        if method == "PATCH":
            out = []
            for r in rows:
                if self._match(r, params):
                    r.update(copy.deepcopy(data or {}))
                    out.append(copy.deepcopy(r))
            return out
        if method == "DELETE":
            gone = [r for r in rows if self._match(r, params)]
            self.tables[table] = [
                r for r in rows if not self._match(r, params)
            ]
            return [copy.deepcopy(r) for r in gone]
        return []

    def mutating_calls(self):
        return [c for c in self.calls if c[1] in ("POST", "PATCH", "DELETE")]


def _write_analysis_fixture(base_dir, name, *, work_id="w-1",
                            book_key="bk-1", fp="fp-1", ext_fp="fp-1",
                            title="Book One", author="Rebecca Yarros",
                            series_name="The Empyrean", series_pos=1,
                            isbn="9780000000001", characters=None,
                            minor_characters=None, relationships=None,
                            minor_sourced_relationships=None,
                            tropes=None, trope_catalog_map=None,
                            trope_chapters=None, triggers=None,
                            quotes=None):
    d = Path(base_dir) / name
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": pp._ARTEFACT_FORMAT_VERSION,
        "stage": "analyze",
        "analysis_key": name,
        "book_key": book_key,
        "fingerprint": fp,
        "extraction_fingerprint": ext_fp,
        "title": title,
        "author": author,
        "series_name": series_name,
        "series_position": series_pos,
        "work_id": work_id,
        "processor_version": pp.PORTABLE_VERSION,
        "created_at": "2026-10-22T18:00:00Z",
    }
    analysis = {
        "title": title,
        "author": author,
        "isbn": isbn,
        "series_name": series_name,
        "series_position": series_pos,
        "work_id": work_id,
        "spice_level": 3,
        "characters": characters or [],
        "minor_characters": minor_characters or [],
        "relationships": relationships or [],
        "minor_sourced_relationships": minor_sourced_relationships or [],
        "tropes": tropes if tropes is not None else ["Enemies to Lovers"],
        "trope_catalog_map": (
            trope_catalog_map if trope_catalog_map is not None
            else {pp._tnorm("Enemies to Lovers"): "tid-etl"}
        ),
        "trope_chapters": (
            trope_chapters if trope_chapters is not None
            else {pp._tnorm("Enemies to Lovers"): ["Ch 1"]}
        ),
        "trope_confidence": {pp._tnorm("Enemies to Lovers"): 0.85},
        "triggers": triggers if triggers is not None else [
            {
                "warning": "violence",
                "severity": "on_page",
                "severity_claimed": "on_page",
                "chapters": ["Ch 1"],
                "chapter_count": 1,
                "frequency": 0.5,
                "prominence": "medium",
                "evidence": [{"quote": "Swords clashed loudly in the hall.",
                              "chapter": "Ch 1"}],
                "confidence": 0.9,
            }
        ],
        "quotes": quotes if quotes is not None else [
            {
                "text": "I will not die today in the quadrant.",
                "speaker": "Violet Sorrengail",
                "chapter": "Ch 1",
                "chapter_index": 0,
                "spoiler": False,
                "evidence_verified": True,
            }
        ],
        "povs": ["Violet Sorrengail"],
    }
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (d / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    return str(d)


class TestWikiMergePlanAndApply(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self._orig_cfg = dict(pp.CONFIG)
        self._orig_sb = pp.sb
        self._orig_learn = pp._LEARNED
        pp._LEARNED = pp.LearnedState(str(Path(self._tmp) / "learned"))
        pp.CONFIG["supabase_url"] = "http://fake.supabase"
        pp.CONFIG["supabase_key"] = "fake-service-key"
        pp.CONFIG["wiki_links_auto_confirm"] = False

    def tearDown(self):
        pp.CONFIG.clear()
        pp.CONFIG.update(self._orig_cfg)
        pp.sb = self._orig_sb
        pp._LEARNED = self._orig_learn

    def _run_merge(self, paths, apply=False, source_run="merge-test-run"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pp.cmd_stage_merge_plan(
                paths, apply=apply, source_run=source_run)
        plan_path = (Path(self._tmp) / "learned" / "artefacts" / "merge"
                     / source_run / "plan.json")
        receipt_path = plan_path.parent / "receipt.json"
        plan = (json.loads(plan_path.read_text(encoding="utf-8"))
                if plan_path.is_file() else None)
        receipt = (json.loads(receipt_path.read_text(encoding="utf-8"))
                   if receipt_path.is_file() else None)
        return rc, buf.getvalue(), plan, receipt

    def test_fingerprint_mismatch_refuses_by_name(self):
        d = _write_analysis_fixture(
            self._tmp, "bad_fp", fp="fp-book-a", ext_fp="fp-book-b")
        rc, out, plan, _ = self._run_merge([d], apply=False)
        self.assertEqual(rc, 1)
        self.assertIn("fingerprint mismatch", out)
        self.assertIsNone(plan)

    def test_plan_includes_full_cast_and_evidenceless_edges_and_series_links(self):
        b1 = _write_analysis_fixture(
            self._tmp, "b1",
            work_id="w-1", book_key="bk-1", fp="fp-1", ext_fp="fp-1",
            title="Fourth Wing", author="Rebecca Yarros",
            series_name="The Empyrean", series_pos=1,
            characters=[
                {"name": "Violet Sorrengail", "role": "protagonist",
                 "status": "alive", "first_appearance_chapter": 1,
                 "last_appearance_chapter": 39, "aliases": ["Violence"],
                 "confidence": 0.95},
                {"name": "Xaden Riorson", "role": "love_interest",
                 "status": "alive", "first_appearance_chapter": 1,
                 "confidence": 0.95},
            ],
            minor_characters=[
                {"name": "Liam Mairi", "role": "supporting",
                 "status": "dead", "first_appearance_chapter": 5,
                 "confidence": 0.80},
            ],
            relationships=[
                # Validated directed family claim with evidence
                {"from": "Lilith Sorrengail", "to": "Violet Sorrengail",
                 "type": "parent", "evidence": "Her mother General Sorrengail.",
                 "evidence_source": "extracted",
                 "validation_state": "validated", "validation_p": 0.92},
                # Evidence-less claim -> must ship as type='other',
                # claimed_type='sibling', validation_state='needs_evidence'
                {"from": "Violet Sorrengail", "to": "Xaden Riorson",
                 "type": "sibling", "evidence": "",
                 "validation_state": "review_required"},
            ],
            minor_sourced_relationships=[
                {"from": "Liam Mairi", "to": "Violet Sorrengail",
                 "type": "friend",
                 "evidence": "Liam guarded Violet's flank.",
                 "evidence_source": "mined",
                 "validation_state": "validated", "validation_p": 0.88},
            ],
        )
        b2 = _write_analysis_fixture(
            self._tmp, "b2",
            work_id="w-2", book_key="bk-2", fp="fp-2", ext_fp="fp-2",
            title="Iron Flame", author="Rebecca Yarros",
            series_name="The Empyrean", series_pos=2,
            isbn="9780000000002",
            characters=[
                {"name": "Violet Sorrengail", "role": "protagonist",
                 "status": "alive", "first_appearance_chapter": 1},
            ],
            minor_characters=[
                # Minor in book 1, minor in book 2 -> still linked across series!
                {"name": "Liam Mairi", "role": "minor",
                 "status": "dead", "first_appearance_chapter": 12},
            ],
        )
        # Different series/author book -> must NOT auto-link "Violet Sorrengail"
        b3 = _write_analysis_fixture(
            self._tmp, "b3",
            work_id="w-3", book_key="bk-3", fp="fp-3", ext_fp="fp-3",
            title="Other Book", author="Another Author",
            series_name="Other Series", series_pos=1,
            isbn="9780000000003",
            characters=[
                {"name": "Violet Sorrengail", "role": "protagonist",
                 "status": "alive"},
            ],
        )

        rc, out, plan, _ = self._run_merge([b1, b2, b3], apply=False)
        self.assertEqual(rc, 0, out)
        self.assertIsNotNone(plan)

        # Full cast (A6): 3 in b1 (2 principals + 1 minor) + 2 in b2 + 1 in b3 = 6
        self.assertEqual(len(plan["characters"]), 6)
        b1_tiers = {c["name"]: c["tier"] for c in plan["characters"]
                    if c["work_ref"] == "w-1"}
        self.assertEqual(b1_tiers, {
            "Violet Sorrengail": "principal",
            "Xaden Riorson": "principal",
            "Liam Mairi": "minor",
        })

        # Edges (A5, D5): 3 edges in b1
        self.assertEqual(len(plan["edges"]), 3)
        by_pair = {(e["from"], e["to"]): e for e in plan["edges"]}
        parent_edge = by_pair[("Lilith Sorrengail", "Violet Sorrengail")]
        self.assertEqual(parent_edge["type"], "parent")
        self.assertEqual(parent_edge["direction"], "a_to_b")
        self.assertEqual(parent_edge["confidence"], 0.92)

        evless_edge = by_pair[("Violet Sorrengail", "Xaden Riorson")]
        self.assertEqual(evless_edge["type"], "other")
        self.assertEqual(evless_edge["claimed_type"], "sibling")
        self.assertEqual(evless_edge["validation_state"], "needs_evidence")
        self.assertEqual(evless_edge["direction"], "mutual")

        minor_edge = by_pair[("Liam Mairi", "Violet Sorrengail")]
        self.assertEqual(minor_edge["type"], "friend")
        self.assertEqual(minor_edge["direction"], "mutual")
        self.assertEqual(minor_edge["evidence_source"], "mined")

        # Links (D3, A7, A8): Violet and Liam link across w-1 and w-2 (4 link rows),
        # never w-3 (different author/series). Default status is 'suggested'.
        self.assertEqual(len(plan["links"]), 4)
        self.assertTrue(all(l["status"] == "suggested" for l in plan["links"]))
        self.assertEqual({l["work_ref"] for l in plan["links"]}, {"w-1", "w-2"})

        # With wiki_links_auto_confirm=True, status becomes 'confirmed'.
        pp.CONFIG["wiki_links_auto_confirm"] = True
        rc2, _, plan2, _ = self._run_merge(
            [b1, b2], apply=False, source_run="merge-test-confirmed")
        self.assertEqual(rc2, 0)
        self.assertTrue(all(l["status"] == "confirmed" for l in plan2["links"]))

    def test_apply_refuses_when_schema_preflight_fails(self):
        b1 = _write_analysis_fixture(self._tmp, "b1")
        fake = _StatefulSB(
            fail_preflight_tables=("character_relationships",))
        pp.sb = fake
        rc, out, plan, receipt = self._run_merge([b1], apply=True)
        self.assertEqual(rc, 1)
        self.assertIsNotNone(plan)
        self.assertIsNone(receipt)
        self.assertIn(
            "--apply refused: the wiki-ingestion migration is not", out)
        self.assertEqual(fake.mutating_calls(), [])

    def test_apply_is_idempotent_across_repeated_runs(self):
        b1 = _write_analysis_fixture(
            self._tmp, "b1",
            work_id="w-1", book_key="bk-1", fp="fp-1", ext_fp="fp-1",
            title="Fourth Wing", author="Rebecca Yarros",
            series_name="The Empyrean", series_pos=1,
            characters=[
                {"name": "Violet Sorrengail", "role": "protagonist",
                 "status": "alive", "first_appearance_chapter": 1,
                 "last_appearance_chapter": 39},
                {"name": "Xaden Riorson", "role": "love_interest",
                 "status": "alive", "first_appearance_chapter": 1},
            ],
            minor_characters=[
                {"name": "Liam Mairi", "role": "supporting",
                 "status": "dead", "first_appearance_chapter": 5},
            ],
            relationships=[
                {"from": "Violet Sorrengail", "to": "Xaden Riorson",
                 "type": "lover",
                 "evidence": "Violet kissed Xaden.",
                 "evidence_source": "extracted",
                 "validation_state": "validated", "validation_p": 0.95},
            ],
        )
        b2 = _write_analysis_fixture(
            self._tmp, "b2",
            work_id="w-2", book_key="bk-2", fp="fp-2", ext_fp="fp-2",
            title="Iron Flame", author="Rebecca Yarros",
            series_name="The Empyrean", series_pos=2,
            isbn="9780000000002",
            characters=[
                {"name": "Violet Sorrengail", "role": "protagonist",
                 "status": "alive", "first_appearance_chapter": 1},
            ],
        )
        fake = _StatefulSB({
            "works": [
                {"id": "w-1", "title": "Fourth Wing"},
                {"id": "w-2", "title": "Iron Flame"},
            ],
        })
        pp.sb = fake

        # Run 1
        rc1, out1, _, receipt1 = self._run_merge(
            [b1, b2], apply=True, source_run="merge-idem-1")
        self.assertEqual(rc1, 0, out1)
        self.assertIsNotNone(receipt1)
        self.assertEqual(receipt1["totals"]["characters"], 4)
        self.assertEqual(receipt1["totals"]["edges"], 1)
        self.assertEqual(receipt1["totals"]["links"], 2)
        self.assertEqual(receipt1["totals"]["errors"], 0)

        # Verify series_key & series_position landed on works
        w1 = [w for w in fake.tables["works"] if w["id"] == "w-1"][0]
        self.assertEqual(w1["series_key"], "rebecca yarros:empyrean")
        self.assertEqual(w1["series_position"], 1)

        snap1 = copy.deepcopy(fake.tables)

        # Run 2 with the same source_run -> zero row diffs (including IDs!)
        rc2, out2, _, receipt2 = self._run_merge(
            [b1, b2], apply=True, source_run="merge-idem-1")
        self.assertEqual(rc2, 0, out2)
        self.assertEqual(receipt2["totals"]["errors"], 0)
        self.assertEqual(fake.tables, snap1)

    def test_apply_preserves_confirmed_and_rejected_curation(self):
        b1 = _write_analysis_fixture(
            self._tmp, "b1",
            work_id="w-1", book_key="bk-1", fp="fp-1", ext_fp="fp-1",
            title="Fourth Wing", author="Rebecca Yarros",
            series_name="The Empyrean", series_pos=1,
            characters=[
                {"name": "Violet Sorrengail", "role": "protagonist",
                 "status": "alive", "appearance": "Silver-tipped hair"},
                {"name": "Dain Aetos", "role": "supporting",
                 "status": "alive"},
                {"name": "Ghost Walkon", "role": "minor",
                 "status": "unknown"},
            ],
            relationships=[
                # Rejected edge: must be skipped and recorded in receipt
                {"from": "Violet Sorrengail", "to": "Dain Aetos",
                 "type": "lover", "evidence": "Old childhood friends.",
                 "validation_state": "review_required", "validation_p": 0.4},
                # Confirmed edge: must stay confirmed, never demoted to pending
                {"from": "Violet Sorrengail", "to": "Dain Aetos",
                 "type": "friend", "evidence": "Dain smiled at Violet.",
                 "validation_state": "validated", "validation_p": 0.9},
            ],
        )
        fake = _StatefulSB({
            "works": [{"id": "w-1", "title": "Fourth Wing"}],
            "book_characters": [
                # Confirmed character -> never demoted to candidate; NULL appearance backfilled
                {"id": 10, "work_id": "w-1", "name": "Violet Sorrengail",
                 "name_norm": "violet sorrengail", "role": "protagonist",
                 "status": "confirmed", "source_type": "ai",
                 "appearance": None, "vitality": "alive", "aliases": []},
                # Candidate character
                {"id": 11, "work_id": "w-1", "name": "Dain Aetos",
                 "name_norm": "dain aetos", "role": "supporting",
                 "status": "candidate", "source_type": "ai"},
                # Rejected character -> never resurrected
                {"id": 12, "work_id": "w-1", "name": "Ghost Walkon",
                 "name_norm": "ghost walkon", "role": "minor",
                 "status": "rejected", "source_type": "ai"},
            ],
            "character_relationships": [
                # Rejected edge on (10, 11, lover)
                {"id": 50, "character_a_id": 10, "character_b_id": 11,
                 "relationship_type": "lover", "direction": "mutual",
                 "review_status": "rejected"},
                # Confirmed edge on (10, 11, friend)
                {"id": 51, "character_a_id": 10, "character_b_id": 11,
                 "relationship_type": "friend", "direction": "mutual",
                 "review_status": "confirmed"},
            ],
            "book_trope_claims": [
                {"id": 70, "work_id": "w-1", "trope_id": "tid-etl",
                 "status": "rejected", "source_type": "ai"},
            ],
            "book_trigger_claims": [
                {"id": 80, "work_id": "w-1", "warning": "violence",
                 "status": "confirmed", "source_type": "ai"},
            ],
        })
        pp.sb = fake

        rc, out, _, receipt = self._run_merge(
            [b1], apply=True, source_run="merge-curation-1")
        self.assertEqual(rc, 0, out)

        # Violet stayed confirmed and got her NULL appearance backfilled
        violet = [c for c in fake.tables["book_characters"]
                  if c["id"] == 10][0]
        self.assertEqual(violet["status"], "confirmed")
        self.assertEqual(violet["appearance"], "Silver-tipped hair")

        # Ghost Walkon stayed rejected and no duplicate row was minted
        walkons = [c for c in fake.tables["book_characters"]
                   if c["name_norm"] == "ghost walkon"]
        self.assertEqual(len(walkons), 1)
        self.assertEqual(walkons[0]["status"], "rejected")

        # Edges: rejected lover edge stayed rejected (and is listed in
        # receipt["skipped_rejected_edges"]); confirmed friend edge stayed confirmed.
        edges = fake.tables["character_relationships"]
        self.assertEqual(len(edges), 2)
        by_id = {e["id"]: e for e in edges}
        self.assertEqual(by_id[50]["review_status"], "rejected")
        self.assertEqual(by_id[51]["review_status"], "confirmed")
        self.assertEqual(len(receipt["skipped_rejected_edges"]), 1)
        self.assertEqual(
            receipt["skipped_rejected_edges"][0]["type"], "lover")

        # Trope rejection and trigger confirmation survived untouched
        self.assertEqual(
            fake.tables["book_trope_claims"][0]["status"], "rejected")
        self.assertEqual(
            fake.tables["book_trigger_claims"][0]["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
