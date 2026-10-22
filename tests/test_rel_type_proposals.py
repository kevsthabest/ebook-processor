"""Relationship types the vocabulary lacks are reviewable, not lost
(2026-10-22).

The report that started this: a scan detected relationships the
operator had no type for ("employer", a bond vocabulary from another
series) and the reduce dropped their claims with one console line.
Nothing recorded what the book needed, and adding a type meant knowing
that the vocabulary is a Supabase table and writing the row by hand.

The contract pinned here:

  1. an unknown type is still dropped from the run (the registry is the
     contract the prompts, judge and app share), but every claim behind
     it is recorded with provenance;
  2. proposals are one row per type id, most frequent first, with the
     least invasive registry defaults — no invented gloss, family flag,
     direction or precedence;
  3. the run writes a small editable companion file beside the preview
     and names it in the markdown;
  4. --apply-rel-types adds the kept rows, skipping existing ids and
     refusing when the registry cannot be read (a write that lands
     nowhere must not report success).

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

TEXT = "Xaden Riorson hired Ann Lee. Ann Lee ran the shop for him."


def _entry(name):
    key = pp.norm_name(name)
    return {"name": name, "aliases": set(), "alias_keys": {key},
            "primary_keys": {key}, "_generic_keys": set(),
            "appearances": 1, "last_seen": 1, "chapters": {1},
            "roles": Counter(), "descriptions": [], "evidence": ""}


def _roster():
    return {pp.norm_name(n): _entry(n)
            for n in ("Xaden Riorson", "Ann Lee")}


def _reduce(rel_type, evidence=TEXT):
    a = {"characters": [{"name": "Xaden Riorson"}, {"name": "Ann Lee"}],
         "relationships": [{"from": "Xaden Riorson", "to": "Ann Lee",
                            "type": rel_type, "evidence": evidence}],
         "task_status": {"identity": "ok"}}
    b = {"summary": "They meet.", "spice_level": 0,
         "triggers": {c: "none" for c in pp.TRIGGER_CATEGORIES},
         "trigger_evidence": {}, "trope_candidates": [], "quotes": []}
    return pp.v2_reduce({1: a}, {1: b}, _roster(),
                        [{"index": 1, "label": "Ch 1", "text": TEXT}],
                        preview=True, n_chapters_total=1)


class TestProposalAccumulator(unittest.TestCase):
    def setUp(self):
        pp._rel_type_proposals_reset()
        self.addCleanup(pp._rel_type_proposals_reset)

    def test_ids_fold_spacing_and_punctuation(self):
        self.assertEqual(pp._rel_type_proposal_id("Commanding Officer"),
                         "commanding_officer")
        self.assertEqual(pp._rel_type_proposal_id("co-worker!"),
                         "co_worker")
        self.assertEqual(pp._rel_type_proposal_id("  "), "")

    def test_one_row_per_id_with_counts_and_examples(self):
        for i in range(5):
            pp._rel_type_proposal_note("Employer", {
                "from": "A", "to": "B", "evidence": f"q{i}", "_idx": i})
        pp._rel_type_proposal_note("employer", {"from": "C", "to": "D"})
        rows = pp._rel_type_proposal_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "employer")
        self.assertEqual(rows[0]["seen"], 6)
        self.assertEqual(len(rows[0]["examples"]), 3,
                         "examples are capped, counts are not")

    def test_rows_are_least_invasive_defaults(self):
        pp._rel_type_proposal_note("employer", {"from": "A", "to": "B"})
        row = pp._rel_type_proposal_rows()[0]
        self.assertEqual(row["gloss"], "",
                         "no invented definition: the judge weighs glosses")
        self.assertFalse(row["is_family"])
        self.assertEqual(row["exclusivity"], "extra")
        self.assertIsNone(row["precedence"])
        self.assertEqual(row["direction"], "symmetric")
        self.assertTrue(row["suggestible"])

    def test_rows_sort_by_frequency(self):
        pp._rel_type_proposal_note("rare", {})
        for _ in range(3):
            pp._rel_type_proposal_note("common", {})
        self.assertEqual([r["id"] for r in pp._rel_type_proposal_rows()],
                         ["common", "rare"])


class TestReduceRecordsUnknownTypes(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset(chapter_count=1)
        self.addCleanup(pp._dm_budget_reset)

    def test_unknown_type_is_dropped_and_recorded(self):
        red = _reduce("employer")
        self.assertEqual(red["relationships"], [],
                         "an unknown type still never ships")
        props = red["relationship_type_proposals"]
        self.assertEqual([p["id"] for p in props], ["employer"])
        self.assertEqual(props[0]["seen"], 1)
        self.assertEqual(props[0]["examples"][0]["chapter"], 1)

    def test_known_type_ships_and_proposes_nothing(self):
        red = _reduce("friend")
        self.assertEqual(len(red["relationships"]), 1)
        self.assertEqual(red["relationship_type_proposals"], [])

    def test_proposals_do_not_leak_between_reduces(self):
        _reduce("employer")
        red = _reduce("friend")
        self.assertEqual(red["relationship_type_proposals"], [])


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        self._orig = dict(pp.CONFIG)
        self.addCleanup(lambda: pp.CONFIG.clear())
        self.addCleanup(lambda: pp.CONFIG.update(self._orig))


class TestApplyRelTypes(_ConfigCase):
    def _file(self, rows):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            d, ignore_errors=True))
        p = Path(d) / "book--rel-types.json"
        p.write_text(json.dumps({"book": "B",
                                 "relationship_types": rows}),
                     encoding="utf-8")
        return p

    def _rows(self):
        return [{"id": "employer", "label": "Employer", "gloss": "",
                 "direction": "symmetric", "is_family": False,
                 "exclusivity": "extra", "precedence": None,
                 "graph_group": "friend", "suggestible": True,
                 "seen": 2, "examples": []}]

    def test_refuses_without_supabase_config(self):
        pp.CONFIG.pop("supabase_url", None)
        pp.CONFIG.pop("supabase_key", None)
        self.assertEqual(pp.cmd_apply_rel_types(self._file(self._rows())), 1)

    def test_refuses_when_registry_cannot_be_read(self):
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "k"
        with mock.patch.object(pp, "sb", return_value=None):
            self.assertEqual(
                pp.cmd_apply_rel_types(self._file(self._rows())), 1)

    def test_adds_new_rows_and_skips_existing(self):
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "k"
        rows = self._rows() + [{"id": "friend", "label": "Friend"}]
        posted = {}

        def fake_sb(table, method="GET", data=None, params="", **kw):
            if method == "GET":
                return [{"id": "friend"}]
            posted["rows"] = data
            return data

        with mock.patch.object(pp, "sb", fake_sb), \
                mock.patch.object(pp, "_load_rel_registry",
                                  return_value={"types": 1}):
            self.assertEqual(
                pp.cmd_apply_rel_types(self._file(rows)), 0)
        self.assertEqual([r["id"] for r in posted["rows"]], ["employer"])

    def test_nothing_new_is_success_and_writes_nothing(self):
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "k"
        calls = []

        def fake_sb(table, method="GET", data=None, params="", **kw):
            calls.append(method)
            return [{"id": "employer"}]

        with mock.patch.object(pp, "sb", fake_sb):
            self.assertEqual(pp.cmd_apply_rel_types(
                self._file(self._rows())), 0)
        self.assertEqual(calls, ["GET"], "no POST for a duplicate id")

    def test_row_fields_are_normalized(self):
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "k"
        posted = {}

        def fake_sb(table, method="GET", data=None, params="", **kw):
            if method == "GET":
                return []
            posted["rows"] = data
            return data

        rows = [{"id": "Boss Figure", "label": "Boss Figure",
                 "gloss": "  ", "direction": "sideways",
                 "is_family": 0, "exclusivity": "weird",
                 "precedence": 9, "graph_group": "", "suggestible": 0,
                 "seen": 1, "examples": []}]
        with mock.patch.object(pp, "sb", fake_sb), \
                mock.patch.object(pp, "_load_rel_registry",
                                  return_value={"types": 1}):
            pp.cmd_apply_rel_types(self._file(rows))
        row = posted["rows"][0]
        self.assertEqual(row["id"], "boss_figure")
        self.assertEqual(row["direction"], "symmetric")
        self.assertEqual(row["exclusivity"], "extra")
        self.assertEqual(row["graph_group"], "friend")
        self.assertFalse(row["is_family"])
        self.assertFalse(row["suggestible"])
        self.assertEqual(row["gloss"], "")
        self.assertNotIn("examples", row)


class TestPreviewArtifacts(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self._tmp, ignore_errors=True))
        self._orig_script = pp.SCRIPT_DIR
        pp.SCRIPT_DIR = self._tmp
        self.addCleanup(setattr, pp, "SCRIPT_DIR", self._orig_script)
        for attr, val in (("_HISTORY", None), ("_RUN_SINK", None)):
            self.addCleanup(setattr, pp, attr, getattr(pp, attr))
            setattr(pp, attr, val)
        self.addCleanup(pp._rel_type_proposals_reset)
        pp._rel_type_proposals_reset()
        pp._rel_type_proposal_note("employer", {
            "from": "A", "to": "B", "evidence": "A hired B.", "_idx": 4})

    def _result(self):
        return {
            "title": "Book", "author": "Writer", "word_count": 10,
            "reading_time_mins": 1, "tasks": {}, "spice_level": 0,
            "povs": [], "tropes": [], "trope_confidence": {},
            "triggers": [], "quotes": [], "isbn": None, "asin": None,
            "characters": [], "relationships": [], "dedup_merges": [],
            "possible_merges": [], "series_name": None, "stories": [],
            "verdicts_note": None,
            "relationship_type_proposals": pp._rel_type_proposal_rows(),
        }

    def test_companion_file_and_markdown_section_are_written(self):
        pp.save_preview(Path(self._tmp) / "Book.epub", self._result())
        files = sorted(p.name for p in (self._tmp / "preview").iterdir())
        rel = [f for f in files if f.endswith("--rel-types.json")]
        self.assertEqual(len(rel), 1, files)
        doc = json.loads(
            (self._tmp / "preview" / rel[0]).read_text(encoding="utf-8"))
        self.assertEqual([r["id"] for r in doc["relationship_types"]],
                         ["employer"])
        md = (self._tmp / "preview" /
              [f for f in files if f.endswith(".md")
               and not f.endswith("_summary.md")][0]).read_text(
                  encoding="utf-8")
        self.assertIn("Proposed relationship types", md)
        self.assertIn("--apply-rel-types", md)
        self.assertIn("employer", md)

    def test_no_companion_file_without_proposals(self):
        result = self._result()
        result["relationship_type_proposals"] = []
        pp.save_preview(Path(self._tmp) / "Book.epub", result)
        names = [p.name for p in (self._tmp / "preview").iterdir()]
        self.assertFalse(any(n.endswith("--rel-types.json")
                             for n in names), names)


if __name__ == "__main__":
    unittest.main()
