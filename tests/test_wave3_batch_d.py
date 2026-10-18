"""Wave 3 batch D: six restorations (issues #11, #12, #27, #55, #57, #61).

  #11 A decision validator that has disabled itself makes no calls,
      so the relationship passes must stop claiming budget for it —
      spent stays 0 and the claim still ships as validator_error.
  #12 The run-history sanitizer must fully neutralise spaced,
      punctuation-bearing ebook filenames, and the denylist scan
      must be able to catch that class of leak instead of sharing
      the sanitizer's blind spot.
  #27 Portal _recent_runs: a preview file deleted mid-scan is
      skipped, never an exception out of the Home assembly.
  #55 The trigger gate and entity grounding resolve chapter text
      through the chapter index (the #43 discipline), falling back
      to first-wins by label — never silent last-wins. Plus #43's
      R1 hardening: _claim_chapter logs when its label fallback
      fires after an index lookup missed.
  #57 A trope answer of 'none' (and its null kin) is no-candidate
      at collection time: it never reaches candidate counts,
      confidence, or the confirmed list. Real trope names that
      merely contain a sentinel as a substring are untouched.
  #61 cmd_retry_failed refuses when the checkpoint meta.json
      exists but cannot be parsed to a dict — the corrupt state a
      crash mid-save leaves behind — instead of failing open and
      clearing manually recoverable checkpoint files.

Stdlib unittest only. Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
_MOD_PATH = str(_ROOT / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                              _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_spec_p = importlib.util.spec_from_file_location(
    "portal_batch_d", str(_ROOT / "portal.py"))
portal = importlib.util.module_from_spec(_spec_p)
_spec_p.loader.exec_module(portal)


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def _roster(*names):
    roster = {}
    for name in names:
        pp._roster_update(
            roster,
            [{"name": name, "aliases": [], "role": "supporting",
              "description": "", "evidence": ""}],
            1)
    return roster


def _ch(idx, label, text="x " * 200):
    return {"index": idx, "label": label, "text": text}


def _b(triggers=None, trigger_evidence=None, spice=0, tropes=None,
       summary="sum", quotes=None):
    full = {c: "none" for c in pp.TRIGGER_CATEGORIES}
    full.update(triggers or {})
    return {"summary": summary, "spice_level": spice,
            "triggers": full,
            "trigger_evidence": trigger_evidence or {},
            "trope_candidates": tropes or [], "quotes": quotes or []}


# Two volumes, one printed label — the #43 shape. Vocabularies are
# disjoint so any resolved text is attributable to one chapter.
_FIRST_TEXT = ("The harbour bell rang over the quiet town. "
               "Alice Marlow promised Bob she would come home "
               "before winter. "
               "The ferry left at dawn without a sound.")
_FIRST_QUOTE = ("Alice Marlow promised Bob she would come home "
                "before winter.")
_SECOND_TEXT = ("The desert wind scoured the empty camp. "
                "Rosa Delgado promised Tomas she would cross the "
                "dunes before winter. "
                "The camels rested beside the dry well.")
_SECOND_QUOTE = ("Rosa Delgado promised Tomas she would cross the "
                 "dunes before winter.")


def _dup_chapters():
    return [_ch(5, "Chapter 5", _FIRST_TEXT),
            _ch(14, "Chapter 5", _SECOND_TEXT)]


# Grounding texts: both endpoints appear under their FULL names,
# because grounding checks whole-name co-occurrence in the chapter.
_G_FIRST_TEXT = ("The harbour bell rang over the quiet town. "
                 "Alice Marlow promised Bob Marlow she would come "
                 "home before winter. "
                 "The ferry left at dawn without a sound.")
_G_FIRST_QUOTE = ("Alice Marlow promised Bob Marlow she would "
                  "come home before winter.")
_G_SECOND_TEXT = ("The desert wind scoured the empty camp. "
                  "Rosa Delgado promised Tomas Delgado she would "
                  "cross the dunes before winter. "
                  "The camels rested beside the dry well.")
_G_SECOND_QUOTE = ("Rosa Delgado promised Tomas Delgado she would "
                   "cross the dunes before winter.")


def _dup_grounding_chapters():
    return [_ch(5, "Chapter 5", _G_FIRST_TEXT),
            _ch(14, "Chapter 5", _G_SECOND_TEXT)]


# ----------------------------------------------------------------------
# #11: disabled validator — relationship passes stop spending
# ----------------------------------------------------------------------

class _CountingValidator:
    """Validator stub counting the calls it is actually asked for.

    disabled=True mirrors DecisionValidator after its breaker trips:
    the real one's _ask_noul returns None without any HTTP request.
    """

    model = "stub"

    def __init__(self, disabled=False, p=0.95):
        self.disabled = disabled
        self.p = p
        self.calls = 0

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls += 1
        if self.p is None:
            return None, None
        return True, self.p


class TestDisabledValidatorBudget(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def _reduce_family(self, validator):
        pp._DECISION_VALIDATOR = validator
        roster = _roster("Alice Marlow", "Bob Marlow")
        chapters = [_ch(1, "Chapter 1", _FIRST_TEXT)]
        chapter_as = {1: {"relationships": [{
            "from": "Alice Marlow", "to": "Bob Marlow",
            "type": "spouse", "evidence": _FIRST_QUOTE}],
            "pov_character": None}}
        return pp.v2_reduce(chapter_as, {}, roster, chapters,
                            preview=False)

    def test_disabled_validator_spends_no_budget_and_is_not_called(self):
        validator = _CountingValidator(disabled=True, p=None)
        red = self._reduce_family(validator)
        snap = pp._dm_budget_snapshot()
        # Pre-fix: spent == 2 (initial claim + K4 retry claim) with
        # the calls landing on a validator that makes none.
        self.assertEqual(
            snap.get("rel_family", {}).get("spent", 0), 0)
        self.assertEqual(validator.calls, 0)
        # The claim outcome is unchanged: it ships as a validator
        # error, exactly as when the calls were (not) made.
        self.assertEqual(red["relationships"][0]["validation_note"],
                         "validator_error")

    def test_enabled_validator_spends_as_before_on_success(self):
        validator = _CountingValidator(disabled=False, p=0.95)
        red = self._reduce_family(validator)
        snap = pp._dm_budget_snapshot()
        self.assertEqual(snap["rel_family"]["spent"], 1)
        self.assertEqual(validator.calls, 1)
        self.assertEqual(red["relationships"][0]["validation_note"],
                         "kept")

    def test_enabled_validator_error_retry_still_spends_two(self):
        # An enabled validator that errors is really called twice
        # (initial + one K4 retry): the accounting for that path
        # must not move.
        validator = _CountingValidator(disabled=False, p=None)
        red = self._reduce_family(validator)
        snap = pp._dm_budget_snapshot()
        self.assertEqual(snap["rel_family"]["spent"], 2)
        self.assertEqual(validator.calls, 2)
        self.assertEqual(red["relationships"][0]["validation_note"],
                         "validator_error")


# ----------------------------------------------------------------------
# #12: history sanitizer / denylist scan punctuation blind spot
# ----------------------------------------------------------------------

class TestHistorySanitizePunctuation(unittest.TestCase):
    ACOTAR = (r"C:\Users\me\import\A Court of Thorns and Roses "
              r"(ACOTAR, Book 1).epub")
    FOURTH_WING = r"C:\Users\me\import\Fourth Wing - The Empyrean, #1.epub"
    DAEMON = r"C:\Users\me\import\Daemon - Daniel Suarez.epub"

    def test_windows_path_with_parenthesised_spaced_name_fully_redacted(self):
        cleaned = pp._history_sanitize(self.ACOTAR)
        for fragment in ("Court", "Thorns", "ACOTAR", ".epub"):
            self.assertNotIn(fragment, cleaned)
        self.assertEqual(cleaned, "[path] [file]")

    def test_windows_path_with_comma_and_hash_name_fully_redacted(self):
        cleaned = pp._history_sanitize(self.FOURTH_WING)
        for fragment in ("Fourth", "Wing", "Empyrean", "#1", ".epub"):
            self.assertNotIn(fragment, cleaned)
        self.assertEqual(cleaned, "[path] [file]")

    def test_bare_punctuation_names_fully_redacted(self):
        cleaned = pp._history_sanitize(
            "error opening Fourth Wing - The Empyrean, #1.epub now")
        for fragment in ("Fourth", "Empyrean", "#1", ".epub"):
            self.assertNotIn(fragment, cleaned)
        self.assertEqual(cleaned, "[file] now")
        cleaned = pp._history_sanitize(
            "Book [Special Edition].epub failed")
        self.assertEqual(cleaned, "[file] failed")

    def test_plain_spaced_name_behaviour_unchanged(self):
        # Preservation pin: the Daemon example sanitised fully
        # before this fix and must stay byte-identical.
        self.assertEqual(pp._history_sanitize(self.DAEMON),
                         "[path] [file]")
        self.assertEqual(
            pp._history_sanitize("could not open Some Book.epub at all"),
            "[file] at all")

    def test_denylist_scan_catches_the_pre_fix_leaks(self):
        leaked_1 = "[path] Court of Thorns and Roses (ACOTAR, Book 1).epub"
        leaked_2 = "[path] Wing - The Empyrean, #[file]"
        self.assertNotEqual(pp._history_denylist_scan({"x": leaked_1}), [])
        self.assertNotEqual(pp._history_denylist_scan({"x": leaked_2}), [])

    def test_denylist_scan_clean_on_correctly_sanitised_text(self):
        for raw in (self.ACOTAR, self.FOURTH_WING, self.DAEMON,
                    "error opening Fourth Wing - The Empyrean, #1.epub now"):
            cleaned = pp._history_sanitize(raw)
            self.assertEqual(
                pp._history_denylist_scan({"x": cleaned}), [], raw)
        self.assertEqual(pp._history_denylist_scan({"x": "[path] [file]"}),
                         [])
        self.assertEqual(pp._history_denylist_scan({"x": "[path] missing"}),
                         [])


class TestHistorySanitizeAdversarialShape(unittest.TestCase):
    """Issue #64: the widened bare-filename pattern backtracked
    quadratically on a crafted shape — a dense punctuation-token
    stream with no book extension anywhere in it. (Realistic
    punctuated error text was never slow; this is the crafted
    shape only.) The bound is on CPU time, not wall time, so a
    throttled machine cannot flake it: the pre-fix pattern
    measures ~2.0 CPU-seconds at this size (and quadruples per
    doubling), the bounded pattern finishes in milliseconds."""

    def test_dense_punctuation_stream_stays_fast(self):
        adversarial = "a,b#c!d&e+f-g" * 600  # 7,800 chars, no extension
        start = time.process_time()
        cleaned = pp._history_sanitize(adversarial)
        elapsed = time.process_time() - start
        self.assertIsInstance(cleaned, str)
        self.assertLess(elapsed, 1.0)


# ----------------------------------------------------------------------
# #27: portal _recent_runs stat race
# ----------------------------------------------------------------------

class TestRecentRunsStatRace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "book1.json").write_text(
            json.dumps({"title": "Book One", "tropes": [1, 2]}),
            encoding="utf-8")
        (self.dir / "book2.json").write_text(
            json.dumps({"title": "Book Two", "tropes": [1]}),
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_stat(self, fail_after):
        """Path.stat raising FileNotFoundError for book2.json once it
        has been called fail_after times for that path."""
        orig_stat = Path.stat
        counts = {}

        def flaky(self, *args, **kwargs):
            if self.name == "book2.json":
                counts[self.name] = counts.get(self.name, 0) + 1
                if counts[self.name] > fail_after:
                    raise FileNotFoundError("vanished mid-scan")
            return orig_stat(self, *args, **kwargs)

        return mock.patch.object(Path, "stat", flaky)

    def test_file_vanishing_before_sort_is_skipped(self):
        with self._patch_stat(fail_after=0):
            runs = portal._recent_runs(None, {}, self.dir)
        works = [r["work"] for r in runs]
        self.assertIn("Book One", works)
        self.assertNotIn("Book Two", works)

    def test_file_vanishing_between_sort_and_loop_is_skipped(self):
        # The sort's stat succeeds; the loop's own stat is the one
        # that meets the deletion.
        with self._patch_stat(fail_after=1):
            runs = portal._recent_runs(None, {}, self.dir)
        works = [r["work"] for r in runs]
        self.assertIn("Book One", works)
        self.assertNotIn("Book Two", works)

    def test_all_files_present_output_unchanged(self):
        runs = portal._recent_runs(None, {}, self.dir)
        works = sorted(r["work"] for r in runs)
        self.assertEqual(works, ["Book One", "Book Two"])
        self.assertTrue(all(r["kind"] == "preview" for r in runs))


# ----------------------------------------------------------------------
# #55: sibling last-wins label maps (trigger gate + grounding) + R1 log
# ----------------------------------------------------------------------

class _TriggerValidator:
    model = "stub"

    def __init__(self):
        self.calls = []

    def validate(self, trigger, quote, context=""):
        self.calls.append({"trigger": trigger, "quote": quote,
                           "context": context})
        return True, 0.95


class TestTriggerGateDuplicateLabels(unittest.TestCase):
    CAT = "graphic_violence"

    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _TriggerValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def _reduce(self, chapters):
        chapter_bs = {5: _b(triggers={self.CAT: "on_page"},
                            trigger_evidence={self.CAT: _FIRST_QUOTE})}
        return pp.v2_reduce({}, chapter_bs, {}, chapters, preview=False)

    def test_first_chapter_evidence_gated_against_first_chapter_text(self):
        red = self._reduce(_dup_chapters())
        self.assertEqual(len(self.validator.calls), 1)
        ctx = self.validator.calls[0]["context"]
        # Pre-fix: the label map kept only the SECOND "Chapter 5",
        # where this quote does not exist — the context came back
        # empty and the quote was gated against nothing of its own.
        self.assertIn("harbour bell", ctx)
        self.assertNotIn("desert wind", ctx)
        trig = [t for t in red["triggers"] if t["warning"] == self.CAT]
        self.assertEqual(len(trig), 1)
        # The chapter index is internal plumbing: it must not leak
        # into the shipped trigger evidence shape.
        self.assertNotIn("_idx", trig[0]["evidence"][0])
        self.assertEqual(trig[0]["evidence"][0]["chapter"], "Chapter 5")

    def test_unique_labels_context_byte_identical(self):
        chapters = [_ch(5, "Chapter 5", _FIRST_TEXT),
                    _ch(14, "Chapter 6", _SECOND_TEXT)]
        self._reduce(chapters)
        self.assertEqual(len(self.validator.calls), 1)
        self.assertEqual(self.validator.calls[0]["context"],
                         pp._extract_quote_context(_FIRST_TEXT,
                                                   _FIRST_QUOTE))


class TestGroundingDuplicateLabels(unittest.TestCase):
    def test_first_chapter_relationship_grounds_in_first_text_by_index(self):
        rels = [{"from": "Alice Marlow", "to": "Bob Marlow",
                 "chapter": "Chapter 5", "_idx": 5}]
        g = pp._ground_entities([], rels, _dup_grounding_chapters())
        self.assertTrue(rels[0]["grounded"])
        self.assertEqual(g["relationships_grounded"], 1)

    def test_first_chapter_relationship_grounds_in_first_text_by_label(self):
        # A claim carrying no index falls back to the label — and
        # the label resolves to the FIRST chapter carrying it, so
        # the earliest text is never silently replaced.
        rels = [{"from": "Alice Marlow", "to": "Bob Marlow",
                 "chapter": "Chapter 5"}]
        pp._ground_entities([], rels, _dup_grounding_chapters())
        self.assertTrue(rels[0]["grounded"])

    def test_second_chapter_relationship_grounds_in_second_text(self):
        rels = [{"from": "Rosa Delgado", "to": "Tomas Delgado",
                 "chapter": "Chapter 5", "_idx": 14}]
        pp._ground_entities([], rels, _dup_grounding_chapters())
        self.assertTrue(rels[0]["grounded"])

    def test_reduce_grounding_uses_each_claims_own_chapter(self):
        # Through the real reduce, both volumes' claims ground
        # against their own chapter's text.
        old = pp._DECISION_VALIDATOR
        pp._DECISION_VALIDATOR = None
        try:
            roster = _roster("Alice Marlow", "Bob Marlow",
                             "Rosa Delgado", "Tomas Delgado")
            chapter_as = {
                5: {"relationships": [{
                    "from": "Alice Marlow", "to": "Bob Marlow",
                    "type": "spouse", "evidence": _G_FIRST_QUOTE}],
                    "pov_character": None},
                14: {"relationships": [{
                    "from": "Rosa Delgado", "to": "Tomas Delgado",
                    "type": "spouse", "evidence": _G_SECOND_QUOTE}],
                    "pov_character": None},
            }
            red = pp.v2_reduce(chapter_as, {}, roster, _dup_grounding_chapters(),
                               preview=False)
        finally:
            pp._DECISION_VALIDATOR = old
        self.assertEqual(red["grounding"]["relationships_grounded"], 2)
        self.assertEqual(red["grounding"]["relationships_total"], 2)


class TestClaimChapterFallbackLog(unittest.TestCase):
    def test_index_miss_logs_the_label_fallback(self):
        # A claim whose chapter index is absent from the chapter
        # list (a subset artefact) falls back to its label — and
        # the fallback must say so, not fire silently (R1).
        old = pp._DECISION_VALIDATOR
        validator = _CountingValidator(disabled=False, p=0.95)
        pp._DECISION_VALIDATOR = validator
        pp._dm_budget_reset()
        try:
            roster = _roster("Alice Marlow", "Bob Marlow")
            chapter_as = {99: {"relationships": [{
                "from": "Alice Marlow", "to": "Bob Marlow",
                "type": "spouse", "evidence": _FIRST_QUOTE}],
                "pov_character": None}}
            _red, out = _capture(
                pp.v2_reduce, chapter_as, {}, roster,
                [_ch(5, "Chapter 5", _FIRST_TEXT)], preview=False)
        finally:
            pp._DECISION_VALIDATOR = old
            pp._dm_budget_reset()
        self.assertIn("falling back to label", out)


# ----------------------------------------------------------------------
# #57: trope candidate 'none' is a null answer, not a trope
# ----------------------------------------------------------------------

class TestNullTropeCandidates(unittest.TestCase):
    def setUp(self):
        self._old_embed = pp.CONFIG.get("embed_url")
        pp.CONFIG["embed_url"] = ""

    def tearDown(self):
        pp.CONFIG["embed_url"] = self._old_embed

    def test_none_and_kin_contribute_zero_candidates(self):
        chapters = [_ch(i, f"Ch {i}") for i in range(1, 6)]
        chapter_bs = {
            1: _b(tropes=["none"]),
            2: _b(tropes=["None", " N/A "]),
            3: _b(tropes=["null", ""]),
            4: _b(tropes=["found family"]),
            5: _b(tropes=["nonexistent hero"]),
        }
        red = pp.v2_reduce({}, chapter_bs, {}, chapters, preview=False)
        counts = red["trope_candidate_counts"]
        # Pre-fix: counts["none"] == 2 and 'none' confirmed downstream
        # at 0.9 like a real trope (Daemon run 22).
        for sentinel in ("none", "n/a", "null", ""):
            self.assertNotIn(sentinel, counts)
            self.assertNotIn(sentinel, red["trope_confidence"])
        self.assertNotIn("none", [pp._tnorm(t)
                                  for t in red["trope_candidates"]])
        # Real tropes in the same run still collect normally —
        # including one that merely CONTAINS a sentinel as a
        # substring ("nonexistent" is not the token "none").
        self.assertEqual(counts.get("found family"), 1)
        self.assertEqual(counts.get("nonexistent hero"), 1)
        self.assertIn("found family", red["trope_candidates"])


# ----------------------------------------------------------------------
# #61: corrupt checkpoint meta must fail closed in cmd_retry_failed
# ----------------------------------------------------------------------

class TestRetryFailedCorruptMeta(unittest.TestCase):
    def setUp(self):
        self._cfg = dict(pp.CONFIG)
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self._cwd = os.getcwd()
        os.chdir(self.td)
        self._orig_dir = pp._checkpoint_dir
        td = self.td

        def _tmp_dir(book_key, _t=td):
            return _t / "checkpoints" / book_key
        pp._checkpoint_dir = _tmp_dir
        self.calls = []
        self._pfv = mock.patch.object(
            pp, "process_file_v2",
            lambda fpath, dry_run=False, preview=False:
                self.calls.append(fpath))
        self._pfv.start()
        self._imp = mock.patch.object(pp, "IMPORT_DIR", self.td / "import")
        self._imp.start()
        self.book_key = pp._checkpoint_book_key("TestBook.epub")
        (self.td / "TestBook.epub").write_bytes(b"fake-epub")
        chapters = {"0": {"a": {"x": 1}, "b": {"y": 1}},
                    "1": {"a": None, "b": None}}
        preview = {"file": "TestBook.epub", "title": "TestBook",
                   "chapter_data": {"chapters": chapters, "roster": {}}}
        self.preview_path = self.td / "preview.json"
        self.preview_path.write_text(json.dumps(preview),
                                     encoding="utf-8")

    def tearDown(self):
        self._imp.stop()
        self._pfv.stop()
        pp._checkpoint_dir = self._orig_dir
        pp.CONFIG.clear()
        pp.CONFIG.update(self._cfg)
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def _write_checkpoint(self, meta_bytes):
        d = pp._checkpoint_dir(self.book_key)
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_bytes(meta_bytes)
        (d / "chapter_0.json").write_bytes(b'{"index": 0, "a": {}, "b": {}}')
        (d / "roster.json").write_bytes(b'{"Hero": {"name": "Hero"}}')
        return {str(p.relative_to(d)): p.read_bytes()
                for p in sorted(d.rglob("*")) if p.is_file()}

    def test_corrupt_meta_refuses_and_preserves_checkpoint(self):
        before = self._write_checkpoint(b'{"fingerprint": "abc", "tot')
        _r, out = _capture(pp.cmd_retry_failed, str(self.preview_path))
        self.assertIn("Refusing to retry", out)
        self.assertIn(self.book_key, out)
        self.assertEqual(self.calls, [])
        after = {str(p.relative_to(pp._checkpoint_dir(self.book_key))):
                 p.read_bytes()
                 for p in sorted(
                     pp._checkpoint_dir(self.book_key).rglob("*"))
                 if p.is_file()}
        self.assertEqual(after, before)

    def test_non_dict_meta_refuses_and_preserves_checkpoint(self):
        before = self._write_checkpoint(b'["not", "a", "dict"]')
        _r, out = _capture(pp.cmd_retry_failed, str(self.preview_path))
        self.assertIn("Refusing to retry", out)
        self.assertEqual(self.calls, [])
        after = {str(p.relative_to(pp._checkpoint_dir(self.book_key))):
                 p.read_bytes()
                 for p in sorted(
                     pp._checkpoint_dir(self.book_key).rglob("*"))
                 if p.is_file()}
        self.assertEqual(after, before)

    def test_absent_meta_retries_as_before(self):
        _r, out = _capture(pp.cmd_retry_failed, str(self.preview_path))
        self.assertIn("Retrying 1 failed chapters: [1]", out)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
