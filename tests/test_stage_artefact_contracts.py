"""Stage-split step 1: artefact contracts + flags separation.

Spec: stage-split-spec-2026-10-21.md §1, §3, §8 step 1.

Two contracts land here, both purely additive:

1. The extraction identity no longer includes the judge. Today's
   _CHECKPOINT_FLAGS welds decision_model / decision_model_provider
   into the hash that validates a stored extraction, so re-judging a
   book under a different judge reads as a different extraction.
   _EXTRACTION_FLAGS is the extraction-affecting subset and
   _extraction_flags_hash() hashes it WITHOUT the decision keys and
   WITHOUT the git SHA (artefacts are data; their identity must
   survive a build change). The legacy hash is untouched — the weld
   tests below pin both sides of that contrast.

2. A completed extraction stamps extraction.json into its checkpoint
   directory. The stamp claims completeness; the reader
   (_load_extraction_stamp) never trusts the claim alone — it
   re-counts the chapter payloads on disk against the stamp's
   recorded expectation and the meta total, so a stamp copied onto a
   partial directory, or a directory that lost payloads afterwards,
   reads incomplete. A missing or corrupt stamp reads as None.
"""

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _CheckpointDirCase(unittest.TestCase):
    """Redirect the checkpoint root into a temp dir (the pattern from
    test_checkpoint_fingerprint.py)."""

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

    def _seed(self, book="book", total=3, chapters=(1, 2, 3), fp="fp-1"):
        pp._checkpoint_save_meta(book, total, fingerprint=fp)
        for i in chapters:
            pp._checkpoint_save_chapter(book, i, {"x": i}, {"y": i})
        pp._checkpoint_save_roster(book, {"alice": {"name": "Alice"}})
        return fp


class TestExtractionFlags(_CheckpointDirCase):
    def test_extraction_flags_are_checkpoint_flags_minus_decision_keys(self):
        self.assertEqual(
            set(pp._EXTRACTION_FLAGS),
            set(pp._CHECKPOINT_FLAGS)
            - {"decision_model", "decision_model_provider"})
        # only_chunks shapes what gets extracted, so it stays.
        self.assertIn("only_chunks", pp._EXTRACTION_FLAGS)

    def test_decision_flip_does_not_move_extraction_hash(self):
        # THE weld test: flipping the judge must not re-identify the
        # extraction — while the legacy checkpoint hash (untouched)
        # still moves, pinning that step 1 cuts the weld only in the
        # new hash.
        with mock.patch.dict(pp.CONFIG, {
                "decision_model": True,
                "decision_model_provider": "openrouter"}):
            ext_a = pp._extraction_flags_hash()
            ck_a = pp._checkpoint_flags_hash()
        with mock.patch.dict(pp.CONFIG, {
                "decision_model": True,
                "decision_model_provider": "local"}):
            ext_b = pp._extraction_flags_hash()
            ck_b = pp._checkpoint_flags_hash()
        self.assertEqual(ext_a, ext_b)
        self.assertNotEqual(ck_a, ck_b)

    def test_extraction_flag_change_moves_extraction_hash(self):
        with mock.patch.dict(pp.CONFIG, {"llm": "ollama"}):
            h_a = pp._extraction_flags_hash()
        with mock.patch.dict(pp.CONFIG, {"llm": "openai"}):
            h_b = pp._extraction_flags_hash()
        self.assertNotEqual(h_a, h_b)

    def test_git_sha_does_not_move_extraction_hash(self):
        with mock.patch.object(pp, "_git_commit_sha", lambda: "aaaa1111"):
            ext_a = pp._extraction_flags_hash()
            ck_a = pp._checkpoint_flags_hash()
        with mock.patch.object(pp, "_git_commit_sha", lambda: "bbbb2222"):
            ext_b = pp._extraction_flags_hash()
            ck_b = pp._checkpoint_flags_hash()
        self.assertEqual(ext_a, ext_b)
        self.assertNotEqual(ck_a, ck_b)

    def test_only_chunks_change_moves_extraction_hash(self):
        with mock.patch.dict(pp.CONFIG, {"only_chunks": None}):
            h_a = pp._extraction_flags_hash()
        with mock.patch.dict(pp.CONFIG, {"only_chunks": "1-3"}):
            h_b = pp._extraction_flags_hash()
        self.assertNotEqual(h_a, h_b)


class TestStampWriterReader(_CheckpointDirCase):
    def test_writer_stamps_and_reader_verifies(self):
        fp = self._seed()
        ok = pp._checkpoint_save_extraction_stamp(
            "book", fingerprint=fp, payload_chapters=3)
        self.assertTrue(ok)
        raw = json.load(open(pp._checkpoint_dir("book") / "extraction.json",
                             encoding="utf-8"))
        self.assertEqual(raw["book_fingerprint"], fp)
        self.assertEqual(raw["artefact_format_version"],
                         pp._ARTEFACT_FORMAT_VERSION)
        self.assertEqual(raw["processor_version"], pp.PORTABLE_VERSION)
        self.assertEqual(raw["extraction_flags_hash"],
                         pp._extraction_flags_hash())
        self.assertEqual(raw["flags_hash"], pp._checkpoint_flags_hash())
        self.assertEqual(raw["total_chapters"], 3)
        self.assertEqual(raw["payload_chapters"], 3)
        self.assertEqual(raw["chapters_present"], 3)
        self.assertTrue(raw["complete"])
        self.assertIn("written_at", raw)
        models = pp._history_models()
        self.assertEqual(raw["extractor"],
                         {"backend": models["llm_backend"],
                          "model": models["llm_model"]})
        stamp = pp._load_extraction_stamp("book")
        self.assertIsNotNone(stamp)
        self.assertTrue(stamp["complete"])
        self.assertTrue(pp._extraction_complete("book"))

    def test_partial_dir_gets_no_stamp(self):
        self._seed(chapters=(1, 2))  # meta says 3, only 2 payloads
        ok = pp._checkpoint_save_extraction_stamp(
            "book", fingerprint="fp-1", payload_chapters=3)
        self.assertFalse(ok)
        self.assertFalse(
            (pp._checkpoint_dir("book") / "extraction.json").exists())
        self.assertIsNone(pp._load_extraction_stamp("book"))
        self.assertFalse(pp._extraction_complete("book"))

    def test_payload_lost_after_stamp_reads_incomplete(self):
        fp = self._seed()
        self.assertTrue(pp._checkpoint_save_extraction_stamp(
            "book", fingerprint=fp, payload_chapters=3))
        (pp._checkpoint_dir("book") / "chapter_2.json").unlink()
        stamp = pp._load_extraction_stamp("book")
        self.assertIsNotNone(stamp)
        self.assertFalse(stamp["complete"])
        self.assertEqual(stamp["chapters_present"], 2)
        self.assertFalse(pp._extraction_complete("book"))

    def test_stamp_with_payload_expectation_below_total(self):
        # Front/back-matter chapters hold slots in meta's total but
        # never produce payload files: a book with 4 slots and 3
        # payload chapters completes with payload_chapters=3, and the
        # reader must honour the recorded expectation rather than
        # demanding payloads meta's total cannot produce.
        self._seed(total=4, chapters=(2, 3, 4))
        self.assertTrue(pp._checkpoint_save_extraction_stamp(
            "book", fingerprint="fp-1", payload_chapters=3))
        stamp = pp._load_extraction_stamp("book")
        self.assertTrue(stamp["complete"])
        # Losing one of the real payloads still reads incomplete.
        (pp._checkpoint_dir("book") / "chapter_3.json").unlink()
        self.assertFalse(pp._load_extraction_stamp("book")["complete"])

    def test_missing_stamp_returns_none(self):
        self._seed()
        self.assertIsNone(pp._load_extraction_stamp("book"))
        self.assertIsNone(pp._load_extraction_stamp("no-such-book"))

    def test_corrupt_stamp_returns_none(self):
        self._seed()
        p = pp._checkpoint_dir("book") / "extraction.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertIsNone(pp._load_extraction_stamp("book"))
        p.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(pp._load_extraction_stamp("book"))

    def test_format_version_mismatch_reads_incomplete(self):
        fp = self._seed()
        self.assertTrue(pp._checkpoint_save_extraction_stamp(
            "book", fingerprint=fp, payload_chapters=3))
        p = pp._checkpoint_dir("book") / "extraction.json"
        raw = json.load(open(p, encoding="utf-8"))
        raw["artefact_format_version"] = 999
        p.write_text(json.dumps(raw), encoding="utf-8")
        stamp = pp._load_extraction_stamp("book")
        self.assertIsNotNone(stamp)
        self.assertFalse(stamp["complete"])

    def test_meta_total_mismatch_reads_incomplete(self):
        fp = self._seed()
        self.assertTrue(pp._checkpoint_save_extraction_stamp(
            "book", fingerprint=fp, payload_chapters=3))
        pp._checkpoint_save_meta("book", 4, fingerprint=fp)
        stamp = pp._load_extraction_stamp("book")
        self.assertIsNotNone(stamp)
        self.assertFalse(stamp["complete"])

    def test_foreign_fingerprint_reads_incomplete(self):
        fp = self._seed()
        self.assertTrue(pp._checkpoint_save_extraction_stamp(
            "book", fingerprint=fp, payload_chapters=3))
        pp._checkpoint_save_meta("book", 3, fingerprint="fp-other-book")
        stamp = pp._load_extraction_stamp("book")
        self.assertIsNotNone(stamp)
        self.assertFalse(stamp["complete"])

    def test_writer_is_noop_under_replay(self):
        self._seed()
        with mock.patch.object(pp, "_REPLAY_CTX",
                               {"payload_dir": self._tmp}):
            ok = pp._checkpoint_save_extraction_stamp(
                "book", fingerprint="fp-1", payload_chapters=3)
        self.assertFalse(ok)
        self.assertFalse(
            (pp._checkpoint_dir("book") / "extraction.json").exists())


# ---------------------------------------------------------------------------
# End-to-end seam tests: a real (tiny) book through process_file_v2.
# dry_run is used so the run stops before the end-of-run checkpoint
# clear — the checkpoint directory (and the stamp) survive for
# inspection, exactly as they would for a later --resume.
# ---------------------------------------------------------------------------

_STORY = ("<p>Mira Solace drew her sword and faced the dawn. "
          "The valley was quiet and the river ran cold and bright "
          "under a wide northern sky full of slow clouds.</p>" * 12)


def _make_epub(path, bodies):
    files = {}
    manifest, spine = [], []
    for i, inner in enumerate(bodies, 1):
        name = f"ch{i:02d}.xhtml"
        files[f"OEBPS/{name}"] = (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.w3.org/1999/xhtml'>"
            f"<head><title>Chapter {i}</title></head>"
            f"<body><h1>Chapter {i}</h1>{inner}</body></html>")
        manifest.append(
            f"<item id='f{i}' href='{name}' "
            "media-type='application/xhtml+xml'/>")
        spine.append(f"<itemref idref='f{i}'/>")
    opf = ("<?xml version='1.0' encoding='utf-8'?>"
           "<package xmlns='http://www.idpf.org/2007/opf' version='3.0' "
           "unique-identifier='bid'>"
           "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
           "<dc:title>Stamp Test Book</dc:title>"
           "<dc:creator>Test Author</dc:creator>"
           "<dc:identifier>9780306406157</dc:identifier></metadata>"
           f"<manifest>{''.join(manifest)}</manifest>"
           f"<spine>{''.join(spine)}</spine></package>")
    container = ("<?xml version='1.0' encoding='utf-8'?>"
                 "<container xmlns='urn:oasis:names:tc:opendocument:"
                 "xmlns:container' version='1.0'><rootfiles>"
                 "<rootfile full-path='OEBPS/content.opf'/>"
                 "</rootfiles></container>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, content in files.items():
            z.writestr(name, content)


class _Backend:
    """Fake LLM backend. mode 'dead' answers None to everything (a
    backend that never answers — the all-chunks-failed abort shape);
    otherwise valid Call A / Call B JSON by prompt."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.chunks = []

    def __call__(self, prompt, chunk, i, n):
        self.chunks.append(chunk)
        if self.mode == "dead":
            return None
        if prompt == pp.PROMPT_V2_CONTENT:
            return json.dumps({
                "summary": "A chapter summary.", "spice_level": 0,
                "triggers": {}, "trigger_evidence": {},
                "trope_candidates": ["Found Family"], "quotes": []})
        return json.dumps({
            "characters": [{"name": "Mira Solace", "role": "protagonist",
                            "description": "A swordswoman of the valley.",
                            "evidence": "Mira Solace drew her sword"}],
            "relationships": [], "pov_character": "Mira Solace"})


class TestStampSeams(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.epub = str(Path(self._tmp.name) / "stampbook.epub")
        _make_epub(self.epub, [_STORY, _STORY, _STORY])
        self.book_key = pp._checkpoint_book_key(Path(self.epub))
        self.addCleanup(lambda: shutil.rmtree(
            pp._checkpoint_dir(self.book_key), ignore_errors=True))

    def _run(self, backend, **config):
        cfg = {"llm": "ollama"}
        cfg.update(config)
        with mock.patch.object(pp, "llm_ollama", backend), \
                mock.patch.object(pp, "_lookup_series_openlibrary",
                                  lambda *a, **k: (None, None)), \
                mock.patch.object(pp, "resolve_work",
                                  lambda *a, **k: (None, "test")), \
                mock.patch.dict(pp.CONFIG, cfg):
            return pp.process_file_v2(Path(self.epub), dry_run=True)

    def test_fresh_run_lands_complete_stamp(self):
        ok = self._run(_Backend())
        self.assertTrue(ok)
        stamp = pp._load_extraction_stamp(self.book_key)
        self.assertIsNotNone(stamp)
        self.assertTrue(stamp["complete"])
        self.assertEqual(stamp["extractor"]["backend"], "ollama")
        self.assertEqual(stamp["total_chapters"], 3)
        self.assertEqual(stamp["payload_chapters"], 3)
        self.assertTrue(pp._extraction_complete(self.book_key))

    def test_resume_that_completes_the_set_lands_stamp(self):
        self.assertTrue(self._run(_Backend()))
        d = pp._checkpoint_dir(self.book_key)
        (d / "extraction.json").unlink()
        payloads = sorted(d.glob("chapter_*.json"))
        self.assertEqual(len(payloads), 3)
        payloads[1].unlink()  # one chapter's payloads lost
        self.assertIsNone(pp._load_extraction_stamp(self.book_key))
        backend = _Backend()
        ok = self._run(backend, resume=True)
        self.assertTrue(ok)
        stamp = pp._load_extraction_stamp(self.book_key)
        self.assertIsNotNone(stamp)
        self.assertTrue(stamp["complete"])

    def test_aborted_run_lands_no_stamp(self):
        ok = self._run(_Backend(mode="dead"))
        self.assertFalse(ok)
        self.assertFalse(
            (pp._checkpoint_dir(self.book_key) / "extraction.json")
            .exists())
        self.assertIsNone(pp._load_extraction_stamp(self.book_key))


if __name__ == "__main__":
    unittest.main()
