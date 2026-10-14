"""Dispatch + cmd_* coverage (QA-scout remediation, 2026-10-14).

The old TestRetryFailed (test_phase4) re-implemented the failed-chapter
detection inline and never called cmd_retry_failed; that class was
removed and the coverage lives here, driving the REAL functions:

- cmd_retry_failed: failed A/B chapters -> checkpoints seeded with the
  successful chapters only -> process_file_v2 invoked in forced preview
  mode with the "_retried" suffix set (the mechanism that names the
  retried output). process_file_v2 itself is the boundary double: the
  full pipeline engine needs EPUBs + an LLM and is exercised by real
  runs, not unit tests.
- cmd_validate / _validate_one: issue detection on fixture previews.
- cmd_push_preview: routing + write_claims replace behaviour behind the
  stateful fake (pattern copied from tests/test_replace_wholesale.py).
- main(): argv dispatch smoke — cmd flags route to the right cmd_*,
  the default path routes to run_once.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
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
    """In-memory Supabase stand-in implementing the PostgREST subset the
    write path uses: GET/POST/PATCH/DELETE with eq/neq/ilike filters.
    (Pattern copied from tests/test_replace_wholesale.py.)"""

    def __init__(self, tables=None, fail_deletes=()):
        self.tables = {t: [dict(r) for r in rows]
                       for t, rows in (tables or {}).items()}
        self.calls = []
        self.fail_deletes = set(fail_deletes)
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
        # PostgREST/SQL semantics: a comparison against NULL never
        # matches (not even neq) — only is.null tests for NULL.
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
        return False  # unknown operator matches nothing

    def __call__(self, table, params=None, method="GET", data=None, prefer=None):
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
            if table in self.fail_deletes:
                return None
            gone = [r for r in rows if self._match(r, params)]
            self.tables[table] = [r for r in rows if not self._match(r, params)]
            return [dict(r) for r in gone]
        return []

    def mutating_calls(self):
        return [c for c in self.calls if c[1] in ("POST", "PATCH", "DELETE")]


class _ConfigGuard(unittest.TestCase):
    """Snapshot/restore the module CONFIG dict in place."""

    def setUp(self):
        self._cfg = dict(pp.CONFIG)

    def tearDown(self):
        pp.CONFIG.clear()
        pp.CONFIG.update(self._cfg)


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


# ----------------------------------------------------------------------
# cmd_retry_failed
# ----------------------------------------------------------------------

class TestCmdRetryFailed(_ConfigGuard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self._cwd = os.getcwd()
        os.chdir(self.td)
        self.calls = []

        def fake_process(fpath, dry_run=False, preview=False):
            self.calls.append({
                "fpath": fpath, "dry_run": dry_run, "preview": preview,
                "suffix": pp.CONFIG.get("preview_name_suffix"),
                "resume": pp.CONFIG.get("resume"),
                "resume_force": pp.CONFIG.get("resume_force"),
            })
        self._pfv = mock.patch.object(pp, "process_file_v2", fake_process)
        self._pfv.start()
        self._imp = mock.patch.object(pp, "IMPORT_DIR", self.td / "import")
        self._imp.start()
        self.book_key = pp._checkpoint_book_key("TestBook.epub")

    def tearDown(self):
        pp._checkpoint_clear(self.book_key)
        self._imp.stop()
        self._pfv.stop()
        os.chdir(self._cwd)
        self.tmp.cleanup()
        super().tearDown()

    def _write(self, preview, with_book=True):
        p = self.td / "preview.json"
        p.write_text(json.dumps(preview), encoding="utf-8")
        if with_book:
            (self.td / "TestBook.epub").write_bytes(b"fake-epub")
        return str(p)

    @staticmethod
    def _preview(chapters, roster=None):
        return {"file": "TestBook.epub", "title": "TestBook",
                "chapter_data": {"chapters": chapters,
                                 "roster": roster or {}}}

    def test_retries_only_failed_and_seeds_successful(self):
        # Chapters 0,2 ok; 1 failed B; 3 failed A and B (the shape the
        # old tautological test described but never ran through the code).
        chapters = {
            "0": {"a": {"x": 1}, "b": {"y": 1}},
            "1": {"a": {"x": 1}, "b": None},
            "2": {"a": {"x": 2}, "b": {"y": 2}},
            "3": {"a": None, "b": None},
        }
        roster = {"Hero": {"appearances": 4}}
        out = _capture(pp.cmd_retry_failed,
                       self._write(self._preview(chapters, roster)))
        self.assertIn("Retrying 2 failed chapters: [1, 3]", out)
        # The pipeline runs exactly once, forced into preview mode, with
        # the "_retried" suffix that names the merged output file.
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["fpath"].name, "TestBook.epub")
        self.assertFalse(call["dry_run"])
        self.assertTrue(call["preview"])
        self.assertEqual(call["suffix"], "_retried")
        self.assertTrue(call["resume"])
        self.assertTrue(call["resume_force"])
        # One-shot flags are popped afterwards; resume stays set (the
        # resumed run is the point of the command).
        self.assertNotIn("preview_name_suffix", pp.CONFIG)
        self.assertNotIn("resume_force", pp.CONFIG)
        self.assertTrue(pp.CONFIG.get("resume"))
        # Checkpoints were seeded with the successful chapters, plus
        # the saved Call A of chapter 1 (A ok / B failed): the seeded
        # roster is the final roster and already contains chapter 1's
        # A fold, so the resume guard must reuse the saved A instead of
        # re-folding it (sweep F5). Chapter 3 (A failed) stays unseeded.
        loaded = pp._checkpoint_load(self.book_key)
        self.assertIsNotNone(loaded)
        meta, saved_roster, saved = loaded
        self.assertEqual(meta["total_chapters"], 4)
        self.assertEqual(sorted(saved), [0, 1, 2])
        self.assertEqual(saved[0], ({"x": 1}, {"y": 1}))
        self.assertEqual(saved[1], ({"x": 1}, None))
        self.assertEqual(saved[2], ({"x": 2}, {"y": 2}))
        # BAR-MOVE (sweep F13, Advisor-approved): the seeded roster is
        # completed at the checkpoint seam — an entry gains the
        # core-field census defaults, the same completion applied to
        # any older roster entry. Exact equality now pins transport
        # PLUS completion; the fixture above stays the raw input.
        self.assertEqual(saved_roster, {"Hero": {
            "appearances": 4, "name": "", "aliases": set(),
            "alias_keys": set(), "primary_keys": set(),
            "_generic_keys": set(), "chapters": set(),
            "roles": pp.Counter(), "descriptions": [], "last_seen": 0}})

    def test_all_ok_does_not_run(self):
        chapters = {"0": {"a": {"x": 1}, "b": {"y": 1}}}
        out = _capture(pp.cmd_retry_failed,
                       self._write(self._preview(chapters)))
        self.assertIn("No failed chapters to retry", out)
        self.assertEqual(self.calls, [])

    def test_missing_chapter_data_does_not_run(self):
        out = _capture(pp.cmd_retry_failed,
                       self._write({"file": "TestBook.epub",
                                    "title": "TestBook"}))
        self.assertIn("No chapter_data", out)
        self.assertEqual(self.calls, [])

    def test_empty_chapters_does_not_run(self):
        out = _capture(pp.cmd_retry_failed,
                       self._write(self._preview({})))
        self.assertIn("No chapter_data", out)
        self.assertEqual(self.calls, [])

    def test_missing_preview_file(self):
        out = _capture(pp.cmd_retry_failed, str(self.td / "nope.json"))
        self.assertIn("Not found", out)
        self.assertEqual(self.calls, [])

    def test_missing_book_file_does_not_run(self):
        chapters = {"0": {"a": None, "b": {"y": 1}}}
        out = _capture(pp.cmd_retry_failed,
                       self._write(self._preview(chapters), with_book=False))
        self.assertIn("Could not find book file", out)
        self.assertEqual(self.calls, [])
        self.assertIsNone(pp._checkpoint_load(self.book_key))


# ----------------------------------------------------------------------
# cmd_validate / _validate_one
# ----------------------------------------------------------------------

def _clean_preview():
    return {
        "characters": [
            {"name": "Alice Smith", "aliases": [], "appearances": 5},
            {"name": "Bob Jones", "aliases": [], "appearances": 3},
        ],
        "tropes": ["Enemies to Lovers"],
        "triggers": [],
        "povs": ["Alice Smith"],
        "verification": {"evidence_checked": 10, "evidence_verified": 9},
        "spice_level": 2,
    }


class TestCmdValidate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data, name="book.json", sub=None):
        d = self.td / sub if sub else self.td
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    def test_clean_preview_has_no_issues(self):
        issues, _notes = pp._validate_one(
            self._write(_clean_preview()), quiet=True)
        self.assertEqual(issues, [])

    def test_bad_merge_and_plot_summary_trope_flagged(self):
        data = _clean_preview()
        # Alice claims Bob's name as an alias while Bob is standalone.
        data["characters"][0]["aliases"] = ["Bob Jones"]
        data["tropes"] = ["Alice And Bob Slowly Fall In Love"]  # 7 words
        issues, _ = pp._validate_one(self._write(data), quiet=True)
        self.assertTrue(any(i.startswith("BAD MERGE") for i in issues),
                        issues)
        self.assertTrue(any(i.startswith("PLOT-SUMMARY") for i in issues),
                        issues)

    def test_trigger_without_evidence_and_low_verification_flagged(self):
        data = _clean_preview()
        data["triggers"] = [{"warning": "violence", "severity": "on_page"}]
        data["verification"] = {"evidence_checked": 10,
                                "evidence_verified": 2}
        issues, _ = pp._validate_one(self._write(data), quiet=True)
        self.assertTrue(
            any(i.startswith("TRIGGERS WITHOUT EVIDENCE") for i in issues),
            issues)
        self.assertTrue(
            any(i.startswith("LOW VERIFICATION RATE") for i in issues),
            issues)

    def test_cmd_validate_single_file_prints_clean(self):
        out = _capture(pp.cmd_validate, self._write(_clean_preview()))
        self.assertIn("No issues detected.", out)

    def test_cmd_validate_directory_summary_skips_trope_maps(self):
        self._write(_clean_preview(), name="good.json", sub="batch")
        bad = _clean_preview()
        bad["tropes"] = ["Alice And Bob Slowly Fall In Love"]
        self._write(bad, name="bad.json", sub="batch")
        self._write({}, name="x-trope-map.json", sub="batch")
        out = _capture(pp.cmd_validate, str(self.td / "batch"))
        self.assertIn("Validating 2 preview files", out)
        self.assertIn("1/2 clean", out)


# ----------------------------------------------------------------------
# cmd_push_preview
# ----------------------------------------------------------------------

class TestCmdPushPreview(_ConfigGuard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self._old_sb = pp.sb
        pp.CONFIG["supabase_url"] = "http://fake.invalid"
        pp.CONFIG["supabase_key"] = "fake-key"
        self.resolve = mock.Mock(return_value=(1, "existing"))
        self._patches = [
            mock.patch.object(pp, "resolve_work", self.resolve),
            mock.patch.object(pp, "correct_metadata",
                              lambda t, a, ids: (t, a)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        pp.sb = self._old_sb
        self.tmp.cleanup()
        super().tearDown()

    def _preview_path(self, **over):
        data = {"file": "PushBook.epub", "title": "Push Book",
                "author": "Some Author", "isbn": "9780000000001",
                "tropes": ["Mind Game"], "trope_confidence": {},
                "trope_catalog_map": {pp._tnorm("Mind Game"): "cat-a"},
                "trope_chapters": {pp._tnorm("Mind Game"): ["Ch 1"]},
                "triggers": [],
                "characters": [{"name": "Hero", "role": "protagonist"}],
                "relationships": [], "quotes": []}
        data.update(over)
        p = self.td / "push.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    def test_push_replaces_processor_rows_preserves_curation(self):
        fake = _StatefulSB({"book_trope_claims": [
            {"id": 1, "work_id": 1, "trope_id": "cat-old",
             "status": "candidate", "source_type": "ai"},
            {"id": 2, "work_id": 1, "trope_id": "cat-keep",
             "status": "confirmed", "source_type": "ai"},
            {"id": 3, "work_id": 1, "trope_id": "cat-rej",
             "status": "rejected", "source_type": "ai"},
            {"id": 4, "work_id": 1, "trope_id": "cat-user",
             "status": "candidate", "source_type": "user"},
        ]})
        pp.sb = fake
        out = _capture(pp.cmd_push_preview, self._preview_path())
        self.assertIn("Push complete, no errors", out)
        # Work resolution was probed without creating.
        _t, _a, _ids, anth = self.resolve.call_args_list[0].args
        self.assertFalse(anth)
        self.assertEqual(self.resolve.call_args_list[0].kwargs,
                         {"create": False})
        # Stale processor row replaced by the pushed claim; human
        # curation (confirmed + rejected) and contributor rows survive.
        ids = sorted(r["trope_id"] for r in fake.tables["book_trope_claims"]
                     if r["work_id"] == 1)
        self.assertEqual(ids, ["cat-a", "cat-keep", "cat-rej", "cat-user"])
        chars = [r for r in fake.tables["book_characters"]
                 if r["work_id"] == 1]
        self.assertEqual([c["name"] for c in chars], ["Hero"])

    def test_push_aborted_preview_skips(self):
        fake = _StatefulSB()
        pp.sb = fake
        out = _capture(pp.cmd_push_preview,
                       self._preview_path(aborted=True))
        self.assertIn("ABORTED", out)
        self.assertEqual(fake.calls, [])
        self.resolve.assert_not_called()

    def test_push_fallback_chapter_detection_skips(self):
        fake = _StatefulSB()
        pp.sb = fake
        out = _capture(pp.cmd_push_preview,
                       self._preview_path(chapter_detection="fallback"))
        self.assertIn("FALLBACK", out)
        self.assertEqual(fake.calls, [])
        self.resolve.assert_not_called()

    def test_push_new_work_declined_writes_nothing(self):
        fake = _StatefulSB()
        pp.sb = fake
        self.resolve = mock.Mock(return_value=(None, "new"))
        with mock.patch.object(pp, "resolve_work", self.resolve), \
                mock.patch("builtins.input", return_value="n"):
            out = _capture(pp.cmd_push_preview, self._preview_path())
        self.assertIn("Skipped (new work not confirmed)", out)
        self.assertEqual(fake.mutating_calls(), [])

    def test_push_new_work_confirmed_creates_and_writes(self):
        fake = _StatefulSB()
        pp.sb = fake
        self.resolve = mock.Mock(side_effect=[(None, "new"), (7, "created")])
        with mock.patch.object(pp, "resolve_work", self.resolve), \
                mock.patch("builtins.input", return_value="y"):
            out = _capture(pp.cmd_push_preview, self._preview_path())
        self.assertIn("Push complete, no errors", out)
        self.assertEqual(self.resolve.call_args_list[1].kwargs,
                         {"create": True})
        chars = [r for r in fake.tables["book_characters"]
                 if r["work_id"] == 7]
        self.assertEqual([c["name"] for c in chars], ["Hero"])

    def test_push_requires_supabase_config(self):
        fake = _StatefulSB()
        pp.sb = fake
        pp.CONFIG.pop("supabase_url", None)
        pp.CONFIG.pop("supabase_key", None)
        out = _capture(pp.cmd_push_preview, self._preview_path())
        self.assertIn("ERROR: supabase_url and supabase_key", out)
        self.assertEqual(fake.calls, [])
        self.resolve.assert_not_called()


# ----------------------------------------------------------------------
# main() dispatch
# ----------------------------------------------------------------------

class TestMainDispatch(_ConfigGuard):
    def setUp(self):
        super().setUp()
        self._old_learned = pp._LEARNED
        self._load = mock.patch.object(pp, "load_config", lambda: None)
        self._load.start()

    def tearDown(self):
        self._load.stop()
        pp._LEARNED = self._old_learned
        super().tearDown()

    def _main(self, argv):
        with mock.patch.object(sys, "argv", argv):
            return _capture(pp.main)

    def test_retry_failed_flag_routes(self):
        with mock.patch.object(pp, "cmd_retry_failed") as m:
            self._main(["process-portable.py", "--retry-failed", "x.json"])
        m.assert_called_once_with("x.json")

    def test_validate_flag_routes(self):
        with mock.patch.object(pp, "cmd_validate") as m:
            self._main(["process-portable.py", "--validate", "x.json"])
        m.assert_called_once_with("x.json")

    def test_push_preview_flag_routes(self):
        with mock.patch.object(pp, "cmd_push_preview") as m:
            self._main(["process-portable.py", "--push-preview", "x.json"])
        m.assert_called_once_with("x.json")

    def test_preview_flag_routes_to_run_once(self):
        with mock.patch.object(pp, "run_once", return_value=0) as m:
            with self.assertRaises(SystemExit) as cm:
                self._main(["process-portable.py", "--preview"])
        self.assertEqual(cm.exception.code, 0)
        m.assert_called_once_with(False, True, None, None)

    def test_run_once_failure_exits_1(self):
        with mock.patch.object(pp, "run_once", return_value=3) as m:
            with self.assertRaises(SystemExit) as cm:
                self._main(["process-portable.py", "--dry-run"])
        self.assertEqual(cm.exception.code, 1)
        m.assert_called_once_with(True, False, None, None)


if __name__ == "__main__":
    unittest.main()
