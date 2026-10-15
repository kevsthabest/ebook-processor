"""Regression: checkpoint dirs are keyed by file STEM only, so two
different books sharing a stem (two files both named e.g. "book.epub"),
a chapter count, and a flags hash silently cross-resumed — the second
book's --resume consumed the first book's Call A/B outputs, and a fresh
run left the other book's chapter files beside its own meta for a later
resume to glob up.

The fix gives every checkpoint meta a book fingerprint (ISBN/ASIN +
normalized title/author + a hash of the first substantial chapter's
text), validates it on resume (mismatch = hard refuse, never consume,
no prompt can override identity), and purges another book's chapter
files when a fresh run takes over the directory.
"""

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _ident(isbn="9780000000001"):
    return {"isbn": isbn}


TEXT_A = "Alice walked into the first chapter of book A. " * 30
TEXT_B = "Bob sailed away in the first chapter of book B. " * 30


def _fp(text, isbn="9780000000001", title="Same Name", author="An Author"):
    return pp._checkpoint_fingerprint(_ident(isbn), title, author,
                                      pp._checkpoint_text_sample(
                                          [{"text": text}]))


class _CheckpointDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_dir = pp._checkpoint_dir
        tmp = self._tmp

        def _tmp_dir(book_key, _t=tmp):
            return Path(_t) / book_key
        pp._checkpoint_dir = _tmp_dir
        orig = self._orig_dir
        self.addCleanup(lambda: setattr(pp, "_checkpoint_dir", orig))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))


class TestFingerprint(_CheckpointDirCase):
    def test_stable_for_same_book(self):
        self.assertEqual(_fp(TEXT_A), _fp(TEXT_A))

    def test_discriminates_on_text(self):
        self.assertNotEqual(_fp(TEXT_A), _fp(TEXT_B))

    def test_discriminates_on_isbn(self):
        self.assertNotEqual(_fp(TEXT_A, isbn="9780000000001"),
                            _fp(TEXT_A, isbn="9780000000002"))

    def test_meta_records_fingerprint(self):
        fp = _fp(TEXT_A)
        pp._checkpoint_save_meta("bk", 2, fingerprint=fp)
        meta = json.load(open(pp._checkpoint_dir("bk") / "meta.json",
                              encoding="utf-8"))
        self.assertEqual(meta["fingerprint"], fp)


class TestCrossBookResume(_CheckpointDirCase):
    def _seed_book_a(self):
        fp = _fp(TEXT_A)
        pp._checkpoint_save_meta("book", 2, fingerprint=fp)
        pp._checkpoint_save_chapter("book", 0, {"x": "A0"}, {"y": "A0"})
        pp._checkpoint_save_chapter("book", 1, {"x": "A1"}, {"y": "A1"})
        pp._checkpoint_save_roster("book", {"alice": {"name": "Alice"}})
        return fp

    def test_same_book_resume_accepted(self):
        fp = self._seed_book_a()
        ch, roster = pp._checkpoint_resume_check("book", 2, fingerprint=fp)
        self.assertIsNotNone(ch)
        self.assertEqual(len(ch), 2)

    def test_cross_book_resume_refused_and_cleared(self):
        self._seed_book_a()
        fp_b = _fp(TEXT_B)
        out = io.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            ch, roster = pp._checkpoint_resume_check(
                "book", 2, fingerprint=fp_b)
        finally:
            sys.stdout = old
        self.assertIsNone(ch)
        self.assertIsNone(roster)
        # Loud refusal on the console, and nothing left to consume.
        self.assertIn("different book", out.getvalue())
        self.assertIsNone(pp._checkpoint_load("book"))

    def test_cross_book_refused_even_with_resume_force(self):
        # --retry-failed sets resume_force to bypass the flags PROMPT;
        # identity is not a prompt — a fingerprint mismatch must refuse
        # regardless.
        self._seed_book_a()
        pp.CONFIG["resume_force"] = True
        self.addCleanup(lambda: pp.CONFIG.pop("resume_force", None))
        ch, roster = pp._checkpoint_resume_check(
            "book", 2, fingerprint=_fp(TEXT_B))
        self.assertIsNone(ch)
        self.assertIsNone(roster)

    def test_legacy_meta_without_fingerprint_still_resumes(self):
        # Pre-fix checkpoints carry no fingerprint: fall back to the
        # flags/count validation rather than refusing outright.
        pp._checkpoint_save_meta("book", 2)
        pp._checkpoint_save_chapter("book", 0, {"x": 1}, {"y": 2})
        pp._checkpoint_save_chapter("book", 1, {"x": 1}, {"y": 2})
        ch, roster = pp._checkpoint_resume_check(
            "book", 2, fingerprint=_fp(TEXT_A))
        self.assertIsNotNone(ch)
        self.assertEqual(len(ch), 2)


class TestPurgeStale(_CheckpointDirCase):
    def test_cross_book_files_purged(self):
        fp_a = _fp(TEXT_A)
        pp._checkpoint_save_meta("book", 3, fingerprint=fp_a)
        for i in range(3):
            pp._checkpoint_save_chapter("book", i, {"x": i}, {"y": i})
        pp._checkpoint_save_roster("book", {"alice": {"name": "Alice"}})
        removed = pp._checkpoint_purge_stale("book", 3, _fp(TEXT_B))
        self.assertEqual(removed, 4)  # 3 chapters + roster
        d = pp._checkpoint_dir("book")
        self.assertEqual(list(d.glob("chapter_*.json")), [])
        self.assertFalse((d / "roster.json").exists())
        # Meta stays: the fresh run (re)writes it immediately after.
        self.assertTrue((d / "meta.json").exists())

    def test_same_book_files_kept(self):
        fp_a = _fp(TEXT_A)
        pp._checkpoint_save_meta("book", 2, fingerprint=fp_a)
        pp._checkpoint_save_chapter("book", 0, {"x": 0}, {"y": 0})
        removed = pp._checkpoint_purge_stale("book", 2, fp_a)
        self.assertEqual(removed, 0)
        self.assertEqual(
            len(list(pp._checkpoint_dir("book").glob("chapter_*.json"))), 1)

    def test_legacy_meta_total_mismatch_purged(self):
        pp._checkpoint_save_meta("book", 5)  # no fingerprint recorded
        pp._checkpoint_save_chapter("book", 0, {"x": 0}, {"y": 0})
        removed = pp._checkpoint_purge_stale("book", 3, _fp(TEXT_A))
        self.assertEqual(removed, 1)

    def test_legacy_meta_same_total_kept(self):
        pp._checkpoint_save_meta("book", 3)  # no fingerprint recorded
        pp._checkpoint_save_chapter("book", 0, {"x": 0}, {"y": 0})
        removed = pp._checkpoint_purge_stale("book", 3, _fp(TEXT_A))
        self.assertEqual(removed, 0)

    def test_no_meta_no_purge(self):
        self.assertEqual(pp._checkpoint_purge_stale("nope", 3, _fp(TEXT_A)),
                         0)


class TestLoadHonoursMetaTotal(_CheckpointDirCase):
    def test_chapters_beyond_meta_total_ignored(self):
        pp._checkpoint_save_meta("book", 2, fingerprint=_fp(TEXT_A))
        for i in range(3):
            pp._checkpoint_save_chapter("book", i, {"x": i}, {"y": i})
        _meta, _roster, chapters = pp._checkpoint_load("book")
        self.assertEqual(sorted(chapters), [0, 1])


if __name__ == "__main__":
    unittest.main()
