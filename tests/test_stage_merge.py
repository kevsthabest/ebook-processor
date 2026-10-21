"""Stage-split step 2: Stage M (Merge) factored behind one function,
a standalone --stage merge entry, and full packet persistence.

Spec: stage-split-spec-2026-10-21.md §1, §2 (Stage M), §5, §8 step 2.

The contracts pinned here:

1. ONE implementation, two drivers (§2): process_file_v2's merge
   segment and the standalone stage entry both call stage_merge_book;
   given identical state and an identical judge, they produce an
   identical canonical roster, verdict table and packet texts.

2. Packets persist: every judged jev pair's verdict carries the FULL
   packet text it was judged on (the v1/v2 passes built the brief and
   discarded it), and merged-roster.json carries those verdicts, so a
   packet can be re-judged offline.

3. The standalone entry refuses legibly (spec R4): no stamp, an
   incomplete extraction, a chunk-limited extraction (complete only
   relative to a --chunks subset — merging it would canonicalize a
   partial book), an artefact-format mismatch, or a book file that
   parses to a different fingerprint than the stamp's. It never runs
   an upstream stage to fill the gap.

4. Two write postures for the same artefact: in the monolith the
   merged-roster write is best-effort bookkeeping (a failure prints
   one line and never changes the run's outcome — the step-1 stamp
   lesson); standalone, the artefact IS the product, so a failed
   write fails the stage.

Hermetic: the judge is a transport double; the book acquisition is
doubled at the _acquire_chapters seam (the fingerprint check inside
cmd_stage_merge is what ties the doubled acquisition to the seeded
stamp). Stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""

import contextlib
import copy
import importlib.util
import inspect
import io
import json
import re
import shutil
import sys
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TITLE = "Daemon Test"
AUTHOR = "A. Writer"
IDENTIFIERS = {"isbn": "9780000000001"}

_CHAPTERS = [
    {"index": 0, "label": "Ch 1",
     "text": "Heinrich Boerner waited by the gate. Boerner checked his "
             "watch twice before the train arrived."},
    {"index": 1, "label": "Ch 2",
     "text": "Karl Devon briefed the Pentagon staff. Devon frowned "
             "at the report."},
    {"index": 2, "label": "Ch 3",
     "text": "Ann Lee met Lee Park at the archive. Ann Lee Park "
             "signed the register before leaving."},
]


def _entry(name, aliases=(), chapters=(0,), appearances=3, mentions=()):
    """A roster entry in the shape _roster_update produces."""
    return {
        "name": name,
        "aliases": set(aliases),
        "alias_keys": {pp.norm_name(a) for a in aliases},
        "primary_keys": {pp.norm_name(name)},
        "appearances": appearances,
        "chapters": set(chapters),
        "roles": Counter(),
        "descriptions": [],
        "evidence": "",
        "statuses": Counter(),
        "death_reports": [],
        "alive_reports": set(),
        "unresolved_mentions": set(mentions),
        "appearances_desc": [],
        "_generic_keys": set(),
    }


def _jev_roster():
    return {pp.norm_name(e["name"]): e for e in [
        _entry("Boerner", chapters=(0,), appearances=5),
        _entry("Heinrich Boerner", chapters=(0,), appearances=3),
        _entry("Karl Devon", chapters=(1,), appearances=4),
    ]}


class _FakeValidator:
    """Decision-model double for BOTH judge interfaces. Packet answers
    are keyed by the names parsed out of the packet itself, so a test
    can only pass if the packet really carried both names; every
    packet seen is recorded for the byte-identity comparisons."""

    disabled = False

    def __init__(self, packet_answers=None, default=(False, 0.10)):
        self.packet_answers = packet_answers or {}
        self.default = default
        self.states = []
        self.same_person_calls = []

    def ask_same_person_packet(self, state):
        self.states.append(state)
        a = re.search(r"^Character A: (.+)$", state, re.M).group(1)
        b = re.search(r"^Character B: (.+)$", state, re.M).group(1)
        return self.packet_answers.get(frozenset((a, b)), self.default)

    def ask_same_person(self, name_a, desc_a, ev_a, name_b, desc_b, ev_b):
        self.same_person_calls.append((name_a, name_b))
        return (True, 0.90)


_BOERNER_YES = {frozenset(("Boerner", "Heinrich Boerner")): (True, 0.93)}

_JEV_CONFIG = {"jev_merge": True, "roster_reconcile": False,
               "decision_model": True,
               "decision_model_provider": "openrouter",
               "decision_model_model": "jev-test",
               "only_chunks": None, "series_priors": False}
_LEGACY_CONFIG = {"jev_merge": False, "roster_reconcile": True,
                  "decision_model": False,
                  "decision_model_provider": "local",
                  "decision_model_model": None,
                  "only_chunks": None, "series_priors": False}


def _fingerprint():
    return pp._checkpoint_fingerprint(
        IDENTIFIERS, TITLE, AUTHOR,
        pp._checkpoint_text_sample(_CHAPTERS))


class _TmpCheckpointCase(unittest.TestCase):
    """Redirect the checkpoint root into a temp dir (the step-1
    pattern) and reset the per-book decision budget per test."""

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
        pp._dm_budget_reset(chapter_count=len(_CHAPTERS))
        self.addCleanup(pp._dm_budget_reset)

    def _seed_extraction(self, book="stagebook", roster=None, fp=None,
                         total=3):
        """A complete Stage-E artefact set for the fixture book."""
        fp = fp if fp is not None else _fingerprint()
        pp._checkpoint_save_meta(book, total, fingerprint=fp)
        for i in range(total):
            pp._checkpoint_save_chapter(book, i, {"characters": []}, {})
        pp._checkpoint_save_roster(book, roster or _jev_roster())
        ok = pp._checkpoint_save_extraction_stamp(book, fp, total)
        assert ok, "fixture stamp failed to land"
        return fp

    def _edit_stamp(self, book, **changes):
        p = Path(self._tmp) / book / "extraction.json"
        stamp = json.loads(p.read_text(encoding="utf-8"))
        stamp.update(changes)
        p.write_text(json.dumps(stamp), encoding="utf-8")
        return stamp


# ---------------------------------------------------------------------------
# Merge-config fingerprint
# ---------------------------------------------------------------------------

class TestMergeConfigFingerprint(unittest.TestCase):
    def test_jev_flip_changes_fingerprint(self):
        with mock.patch.dict(pp.CONFIG, {"jev_merge": False,
                                         "roster_reconcile": True}):
            a = pp._merge_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {"jev_merge": True,
                                         "roster_reconcile": True}):
            b = pp._merge_config_fingerprint()
        self.assertNotEqual(a, b)

    def test_reconcile_flip_changes_fingerprint(self):
        with mock.patch.dict(pp.CONFIG, {"jev_merge": False,
                                         "roster_reconcile": False}):
            a = pp._merge_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {"jev_merge": False,
                                         "roster_reconcile": True}):
            b = pp._merge_config_fingerprint()
        self.assertNotEqual(a, b)

    def test_judge_identity_changes_fingerprint_when_jev_on(self):
        base = {"jev_merge": True, "roster_reconcile": False}
        with mock.patch.dict(pp.CONFIG, {
                **base, "decision_model_provider": "openrouter",
                "decision_model_model": "jev-a"}):
            a = pp._merge_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {
                **base, "decision_model_provider": "local",
                "decision_model_model": "jev-a"}):
            b = pp._merge_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {
                **base, "decision_model_provider": "openrouter",
                "decision_model_model": "jev-b"}):
            c = pp._merge_config_fingerprint()
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_judge_identity_ignored_when_jev_off(self):
        # The fingerprint's contract (step-2 brief): provider/model
        # count only under --jev-merge, where the judge IS the merge.
        base = {"jev_merge": False, "roster_reconcile": True}
        with mock.patch.dict(pp.CONFIG, {
                **base, "decision_model_provider": "openrouter",
                "decision_model_model": "jev-a"}):
            a = pp._merge_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {
                **base, "decision_model_provider": "local",
                "decision_model_model": None}):
            b = pp._merge_config_fingerprint()
        self.assertEqual(a, b)

    def test_unrelated_flags_do_not_move_fingerprint(self):
        base = {"jev_merge": True, "roster_reconcile": False,
                "decision_model_provider": "openrouter",
                "decision_model_model": "jev-a"}
        with mock.patch.dict(pp.CONFIG, {**base, "llm": "ollama",
                                         "show_matrices": False}):
            a = pp._merge_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {**base, "llm": "openai",
                                         "show_matrices": True}):
            b = pp._merge_config_fingerprint()
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# The shared stage function
# ---------------------------------------------------------------------------

class TestStageMergeBook(_TmpCheckpointCase):
    def test_jev_path_folds_and_reports_packets(self):
        with mock.patch.dict(pp.CONFIG, _JEV_CONFIG):
            roster = _jev_roster()
            judge = _FakeValidator(_BOERNER_YES)
            seg = pp.stage_merge_book(
                roster, _CHAPTERS, validator=judge, no_pairs=(),
                jev_no_pairs=(), book_key="book")
            self.assertEqual(sorted(seg["roster"]),
                             ["heinrich boerner", "karl devon"])
            self.assertEqual(
                seg["roster"]["heinrich boerner"]["appearances"], 8)
            # The monolith's segment outputs are all present.
            self.assertIsNotNone(seg["merge_judge"])
            self.assertEqual(seg["post_pass_merged"], 0)
            self.assertEqual(seg["post_pass_possible"], [])
            # Janitor ran (prune-only under jev): stats exist.
            self.assertIsNotNone(seg["reconcile"])
            report = seg["merge_report"]
            self.assertEqual(
                report["merge_config_fingerprint"],
                pp._merge_config_fingerprint())
            verdicts = report["verdicts"]
            self.assertEqual(len(verdicts), 1)
            v = verdicts[0]
            self.assertEqual(v["mechanism"], "jev_merge")
            self.assertEqual(v["verdict"], "yes")
            self.assertEqual(v["confidence"], 0.93)
            # THE packet contract: the full brief the judge read is
            # on the verdict, byte-identical to what it received.
            self.assertEqual(v["packet"], judge.states[0])
            self.assertIn("HOW TO READ THIS BRIEF", v["packet"])
            self.assertIn("Boerner", v["packet"])

    def test_every_judged_verdict_carries_its_packet(self):
        with mock.patch.dict(pp.CONFIG, _JEV_CONFIG):
            judge = _FakeValidator(_BOERNER_YES)
            seg = pp.stage_merge_book(
                _jev_roster(), _CHAPTERS, validator=judge, no_pairs=(),
                jev_no_pairs=(), book_key="book")
            judged = [v for v in seg["merge_report"]["verdicts"]
                      if v["verdict"] in ("yes", "no", "error")]
            self.assertTrue(judged)
            for v in judged:
                self.assertIn("packet", v)
                self.assertTrue(v["packet"])

    def test_deterministic_over_identical_state(self):
        # Equivalence core: the same state + the same judge answers
        # through the shared function twice -> identical canonical
        # roster and identical verdict table, packets included.
        with mock.patch.dict(pp.CONFIG, _JEV_CONFIG):
            judge_a = _FakeValidator(_BOERNER_YES)
            seg_a = pp.stage_merge_book(
                copy.deepcopy(_jev_roster()), _CHAPTERS,
                validator=judge_a, no_pairs=(), jev_no_pairs=(),
                book_key="book")
            pp._dm_budget_reset(chapter_count=len(_CHAPTERS))
            judge_b = _FakeValidator(_BOERNER_YES)
            seg_b = pp.stage_merge_book(
                copy.deepcopy(_jev_roster()), _CHAPTERS,
                validator=judge_b, no_pairs=(), jev_no_pairs=(),
                book_key="book")
            self.assertEqual(pp._ckpt_json_safe(seg_a["roster"]),
                             pp._ckpt_json_safe(seg_b["roster"]))
            self.assertEqual(seg_a["merge_report"]["verdicts"],
                             seg_b["merge_report"]["verdicts"])
            self.assertEqual(judge_a.states, judge_b.states)

    def test_legacy_path_records_reconcile_and_postpass_folds(self):
        # Reconcile fold: mutual alias claim across disjoint chapters.
        roster = {pp.norm_name(e["name"]): e for e in [
            _entry("Jon Smith", aliases=("Smith",), chapters=(0,),
                   appearances=6),
            _entry("Smith", chapters=(1,), appearances=2,
                   mentions=("Jon Smith",)),
            _entry("Boerner", chapters=(0,), appearances=5),
            _entry("Heinrich Boerner", chapters=(2,), appearances=3),
        ]}
        with mock.patch.dict(pp.CONFIG, _LEGACY_CONFIG):
            judge = _FakeValidator()
            seg = pp.stage_merge_book(
                roster, _CHAPTERS, validator=judge, no_pairs=(),
                book_key="book")
            self.assertIsNone(seg["merge_judge"])
            self.assertEqual(seg["reconcile"]["variants_merged"], 1)
            # Post-pass Tier 3 fold (Boerner pair, disjoint chapters,
            # judge says yes): one more merge beyond the reconcile's.
            self.assertEqual(seg["post_pass_merged"], 1)
            self.assertEqual(sorted(seg["roster"]),
                             ["boerner", "jon smith"])
            folds = [v for v in seg["merge_report"]["verdicts"]
                     if v["verdict"] == "merged"]
            self.assertEqual(
                {v["mechanism"] for v in folds},
                {"roster_reconcile", "post_pass_merge"})
            for v in folds:
                # A fold names its survivor; a guessed survivor would
                # be worse than none.
                self.assertIn(v["b_key"], seg["roster"])
                self.assertNotIn(v["a_key"], seg["roster"])
            self.assertEqual(seg["merge_report"]["post_pass"]["merged"],
                             1)

    def test_series_priors_seed_runs_inside_the_stage(self):
        # The monolith seeds registry forms between the janitor and
        # the merge passes; the factored stage must keep that seam —
        # a seeded mention is part of Stage M's state change.
        char = {"name_key": "boerner", "canonical_name": "Boerner",
                "forms": {"boerner": "Boerner",
                          "h boerner": "H. Boerner"}}
        ctx = {"key_fn": pp.norm_name,
               "form_index": {"boerner": char},
               "matched": {}, "matched_chars": set(), "seeded": set(),
               "counts": {"aliases_seeded": 0, "pairs_nominated": 0,
                          "form_tiebreaks": 0}}
        roster = {pp.norm_name(e["name"]): e for e in [
            _entry("Boerner", chapters=(0,), appearances=5),
            _entry("Karl Devon", chapters=(1,), appearances=4),
        ]}
        with mock.patch.dict(pp.CONFIG, _LEGACY_CONFIG):
            seg = pp.stage_merge_book(
                roster, _CHAPTERS, validator=None, no_pairs=(),
                series_priors_ctx=ctx, book_key="book")
            self.assertIn("H. Boerner",
                          seg["roster"]["boerner"]
                          ["unresolved_mentions"])
            self.assertTrue(seg["merge_report"]["series_priors_seeded"])

    def test_process_file_v2_routes_through_the_shared_function(self):
        # §2's structural pin: the monolith's merge segment calls the
        # shared function and no longer calls the passes directly.
        src = inspect.getsource(pp.process_file_v2)
        self.assertIn("stage_merge_book(", src)
        self.assertNotIn("jev_merge_pass(", src)
        self.assertNotIn("post_pass_merge(", src)
        self.assertNotIn("_roster_reconcile(", src)


# ---------------------------------------------------------------------------
# The artefact writer: two postures
# ---------------------------------------------------------------------------

class TestMergedRosterWriter(_TmpCheckpointCase):
    def _seg(self):
        with mock.patch.dict(pp.CONFIG, _JEV_CONFIG):
            return pp.stage_merge_book(
                _jev_roster(), _CHAPTERS,
                validator=_FakeValidator(_BOERNER_YES), no_pairs=(),
                jev_no_pairs=(), book_key="book")

    def test_write_round_trip_and_schema(self):
        seg = self._seg()
        ok = pp._save_merged_roster_artefact(
            "stagebook", seg, book_fingerprint="fp-9",
            extraction_flags_hash="efh-9",
            extraction_processor_version="2.13.83")
        self.assertTrue(ok)
        p = Path(self._tmp) / "stagebook" / "merged-roster.json"
        art = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(art["book_key"], "stagebook")
        self.assertEqual(art["book_fingerprint"], "fp-9")
        self.assertEqual(art["extraction_flags_hash"], "efh-9")
        self.assertEqual(art["extraction_processor_version"], "2.13.83")
        self.assertEqual(art["processor_version"], pp.PORTABLE_VERSION)
        self.assertEqual(art["artefact_format_version"],
                         pp._ARTEFACT_FORMAT_VERSION)
        self.assertEqual(art["merge_config_fingerprint"],
                         seg["merge_report"]["merge_config_fingerprint"])
        self.assertEqual(sorted(art["roster"]),
                         ["heinrich boerner", "karl devon"])
        self.assertEqual(art["merge_report"]["verdicts"][0]["packet"],
                         seg["merge_report"]["verdicts"][0]["packet"])

    def test_write_failure_is_bookkeeping_not_an_exception(self):
        # The monolith posture (step-1 lesson): a failing artefact
        # write returns False and leaves neither file nor temp
        # litter; it must never raise into a finished run.
        seg = self._seg()
        with mock.patch.object(pp.os, "replace",
                               side_effect=OSError("disk full")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ok = pp._save_merged_roster_artefact(
                    "stagebook", seg, book_fingerprint="fp-9",
                    extraction_flags_hash="efh-9",
                    extraction_processor_version="2.13.83")
        self.assertFalse(ok)
        d = Path(self._tmp) / "stagebook"
        self.assertFalse((d / "merged-roster.json").exists())
        self.assertFalse((d / "merged-roster.json.tmp").exists())
        self.assertIn("merged-roster", out.getvalue())

    def test_replay_never_writes(self):
        seg = self._seg()
        payload_dir = Path(self._tmp) / "snap"
        payload_dir.mkdir()
        old = pp._REPLAY_CTX
        pp._REPLAY_CTX = {"payload_dir": str(payload_dir)}
        try:
            ok = pp._save_merged_roster_artefact(
                "stagebook", seg, book_fingerprint="fp-9",
                extraction_flags_hash="efh-9",
                extraction_processor_version="2.13.83")
        finally:
            pp._REPLAY_CTX = old
        self.assertFalse(ok)
        self.assertFalse((payload_dir / "merged-roster.json").exists())


# ---------------------------------------------------------------------------
# The standalone entry
# ---------------------------------------------------------------------------

class TestCmdStageMerge(_TmpCheckpointCase):
    def _acquire(self, **over):
        acq = {"chapters": copy.deepcopy(_CHAPTERS), "title": TITLE,
               "author": AUTHOR, "identifiers": dict(IDENTIFIERS),
               "word_count": 42, "chapter_detection_fallback": False}
        acq.update(over)
        return acq

    def _run_cmd(self, book="stagebook", config=None, validator=None,
                 acquire=None):
        fpath = Path(self._tmp) / "stagebook.epub"
        fpath.write_bytes(b"fixture")
        out = io.StringIO()
        with mock.patch.dict(pp.CONFIG, config or _JEV_CONFIG), \
                mock.patch.object(
                    pp, "_acquire_chapters",
                    return_value=acquire or self._acquire()), \
                mock.patch.object(pp, "_detect_series",
                                  return_value=(None, None, "none")), \
                mock.patch.object(pp, "_DECISION_VALIDATOR",
                                  validator if validator is not None
                                  else _FakeValidator(_BOERNER_YES)), \
                contextlib.redirect_stdout(out):
            rc = pp.cmd_stage_merge(fpath)
        return rc, out.getvalue()

    def test_happy_path_matches_the_shared_function_run(self):
        # Driver equivalence (spec §7, mocked-transport form): the
        # standalone entry over the seeded artefact set produces the
        # same roster, verdicts and PACKET TEXTS as the shared
        # function driven directly on the same state. Driver A loads
        # its roster through the checkpoint loader too — the same
        # restore seam a resumed monolith consumes (it completes
        # core fields an older roster.json predates, by design), so
        # both drivers start from byte-identical state.
        self._seed_extraction()
        _meta, roster_a, _payloads = pp._checkpoint_load("stagebook")
        with mock.patch.dict(pp.CONFIG, _JEV_CONFIG):
            judge_a = _FakeValidator(_BOERNER_YES)
            seg = pp.stage_merge_book(
                roster_a, _CHAPTERS, validator=judge_a,
                no_pairs=(), jev_no_pairs=(), book_key="book")
        judge_b = _FakeValidator(_BOERNER_YES)
        rc, _out = self._run_cmd(validator=judge_b)
        self.assertEqual(rc, 0)
        # The judge standalone read byte-identical packets.
        self.assertEqual(judge_a.states, judge_b.states)
        art = json.loads((Path(self._tmp) / "stagebook"
                          / "merged-roster.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(art["roster"], pp._ckpt_json_safe(seg["roster"]))
        self.assertEqual(art["merge_report"]["verdicts"],
                         seg["merge_report"]["verdicts"])
        self.assertEqual(art["book_fingerprint"], _fingerprint())
        stamp = pp._load_extraction_stamp("stagebook")
        self.assertEqual(art["extraction_flags_hash"],
                         stamp["extraction_flags_hash"])
        self.assertEqual(art["extraction_processor_version"],
                         stamp["processor_version"])
        self.assertEqual(art["processor_version"], pp.PORTABLE_VERSION)

    def test_no_stamp_refuses(self):
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("no extraction stamp", out)
        self.assertFalse((Path(self._tmp) / "stagebook"
                          / "merged-roster.json").exists())

    def test_incomplete_extraction_refuses(self):
        self._seed_extraction()
        (Path(self._tmp) / "stagebook" / "chapter_2.json").unlink()
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("not complete", out)

    def _tear(self, book, name):
        """Truncate an artefact file mid-content — the shape a
        write killed halfway leaves behind. The checkpoint
        writers write in place, not atomically, so a concurrent
        re-extraction of the same book can present exactly this
        to a stage reading the set."""
        p = Path(self._tmp) / book / name
        text = p.read_text(encoding="utf-8")
        p.write_text(text[: max(1, len(text) // 2)],
                     encoding="utf-8")
        return p

    def test_torn_roster_refuses_legibly(self):
        # roster.json is the one artefact no upstream guard
        # covers: coverage never reads it, so a set torn mid-write
        # still stamps complete — and the checkpoint loader then
        # RAISES on the truncated JSON instead of returning its
        # "no set" signal. The entry must turn that raise into
        # its own "no readable checkpoint set" refusal (spec R4:
        # a stage never crashes on the artefacts it consumes),
        # not propagate a traceback, and must not touch the set.
        self._seed_extraction()
        torn = self._tear("stagebook", "roster.json")
        before = torn.read_bytes()
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("no readable checkpoint set", out)
        self.assertFalse((Path(self._tmp) / "stagebook"
                          / "merged-roster.json").exists())
        self.assertEqual(torn.read_bytes(), before)

    def test_truncated_chapter_payload_refuses_legibly(self):
        # A truncated payload never reaches the loader: coverage
        # counts parseable payloads only, so the stamp reads
        # incomplete and the entry pre-refuses. Pinned so the
        # end-to-end contract (rc 1, a legible refusal, no crash)
        # is on record for the plain torn-write shape too.
        self._seed_extraction()
        self._tear("stagebook", "chapter_1.json")
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("not complete", out)

    def test_unusable_chapter_payload_refuses_legibly(self):
        # The torn shape that DOES reach the loader: a payload
        # replaced between the coverage read and the load — the
        # TOCTOU residue of a concurrent re-extraction — can
        # still parse (coverage counts it, the stamp stays
        # complete) yet not be a payload object at all, and the
        # loader's field access raises on it. Same refusal as a
        # torn roster: the stage refuses, it does not crash.
        self._seed_extraction()
        (Path(self._tmp) / "stagebook" / "chapter_1.json") \
            .write_text("[1, 2]", encoding="utf-8")
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("no readable checkpoint set", out)
        self.assertFalse((Path(self._tmp) / "stagebook"
                          / "merged-roster.json").exists())

    def test_checkpoint_load_still_raises_on_a_torn_set(self):
        # The refusal lives at the stage ENTRIES, not in the
        # loader: _checkpoint_load is shared with --resume and
        # the replay path, whose semantics this fix must not
        # change — called directly on a torn set it still raises.
        self._seed_extraction()
        self._tear("stagebook", "roster.json")
        with self.assertRaises(json.JSONDecodeError):
            pp._checkpoint_load("stagebook")

    def test_chunk_limited_extraction_refuses_by_name(self):
        self._seed_extraction()
        stamp = pp._load_extraction_stamp("stagebook")
        flags = dict(stamp["extraction_flags"])
        flags["only_chunks"] = "1,2"
        self._edit_stamp("stagebook", extraction_flags=flags)
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("chunk", out.lower())
        self.assertIn("--chunks", out)

    def test_format_mismatch_refuses(self):
        self._seed_extraction()
        self._edit_stamp("stagebook", artefact_format_version=999)
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("format", out.lower())

    def test_fingerprint_mismatch_refuses(self):
        other = pp._checkpoint_fingerprint(
            {"isbn": "9780000000002"}, TITLE, AUTHOR,
            pp._checkpoint_text_sample(_CHAPTERS))
        self._seed_extraction(fp=other)
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("fingerprint", out.lower())

    def test_chapter_count_mismatch_refuses(self):
        # The edition attack the fingerprint cannot see: it samples
        # only the opening text, so the same book under a different
        # edition — identical opening, extra chapters behind it —
        # recomputes the SAME fingerprint (asserted here, so the
        # refusal below can only have come from the count check).
        # The stamp's total is the extraction's chapter-slot count;
        # a re-derived set with a different count is not the chapter
        # set the extraction produced, and merging against it would
        # build packets from text the extraction never saw.
        self._seed_extraction()
        longer = copy.deepcopy(_CHAPTERS) + [
            {"index": 3, "label": "Ch 4",
             "text": "An extra chapter the extraction never saw, "
                     "appended behind an identical opening."}]
        self.assertEqual(
            pp._checkpoint_fingerprint(
                IDENTIFIERS, TITLE, AUTHOR,
                pp._checkpoint_text_sample(longer)),
            _fingerprint())
        rc, out = self._run_cmd(acquire=self._acquire(chapters=longer))
        self.assertEqual(rc, 1)
        self.assertIn("4 chapters", out)
        self.assertIn("covers 3", out)
        self.assertFalse((Path(self._tmp) / "stagebook"
                          / "merged-roster.json").exists())

    def test_cross_version_stamp_warns_and_proceeds(self):
        self._seed_extraction()
        self._edit_stamp("stagebook", processor_version="2.13.50")
        rc, out = self._run_cmd()
        self.assertEqual(rc, 0)
        self.assertIn("2.13.50", out)
        art = json.loads((Path(self._tmp) / "stagebook"
                          / "merged-roster.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(art["extraction_processor_version"], "2.13.50")
        self.assertEqual(art["processor_version"], pp.PORTABLE_VERSION)

    def test_standalone_write_failure_fails_the_stage(self):
        # The standalone posture: the artefact is the product, so a
        # failed write is a failed stage — legibly, exit 1.
        self._seed_extraction()
        with mock.patch.object(pp, "_save_merged_roster_artefact",
                               return_value=False):
            rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("merged-roster", out)

    def test_legacy_config_runs_the_algorithmic_path(self):
        roster = {pp.norm_name(e["name"]): e for e in [
            _entry("Jon Smith", aliases=("Smith",), chapters=(0,),
                   appearances=6),
            _entry("Smith", chapters=(1,), appearances=2,
                   mentions=("Jon Smith",)),
        ]}
        self._seed_extraction(roster=roster)
        rc, _out = self._run_cmd(config=_LEGACY_CONFIG,
                                 validator=_FakeValidator())
        self.assertEqual(rc, 0)
        art = json.loads((Path(self._tmp) / "stagebook"
                          / "merged-roster.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(sorted(art["roster"]), ["jon smith"])
        self.assertEqual(
            art["merge_report"]["reconcile"]["stats"]["variants_merged"],
            1)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

class TestStageCli(unittest.TestCase):
    def setUp(self):
        # main() assigns the process-global _LEARNED; the dispatch
        # tests' convention is to hand it back afterwards.
        self._old_learned = pp._LEARNED
        self.addCleanup(lambda: setattr(pp, "_LEARNED",
                                        self._old_learned))

    def test_stage_defaults_to_all(self):
        args = pp._build_parser().parse_args([])
        self.assertEqual(args.stage, "all")

    def test_stage_merge_beside_watch_conflicts(self):
        parser = pp._build_parser()
        defaults = parser.parse_args([])
        args = parser.parse_args(["--stage", "merge", "--watch"])
        self.assertIsNotNone(pp._flag_conflicts(args, defaults))

    def test_stage_merge_with_file_is_coherent(self):
        parser = pp._build_parser()
        defaults = parser.parse_args([])
        args = parser.parse_args(
            ["--stage", "merge", "--file", "book.epub"])
        self.assertIsNone(pp._flag_conflicts(args, defaults))

    def test_stage_extract_refuses_without_running(self):
        # Superseded by stage-split step 4: 'extract' no longer
        # refuses as unshipped — it dispatches (after state
        # migration, like merge/decide) and demands its --file
        # target, the same shape as test_stage_merge_needs_a_file.
        # Step 4's own suite (tests/test_stage_orchestrator.py)
        # pins the extract dispatch and its refusals in full.
        with mock.patch.object(sys, "argv",
                               ["process-portable.py", "--stage",
                                "extract"]), \
                mock.patch.object(pp, "ensure_state_migrated",
                                  lambda *a, **k: None), \
                mock.patch.object(pp, "LearnedState",
                                  lambda *a, **k: mock.Mock(
                                      dir="/tmp", nicknames={},
                                      trusted_titles=lambda: [],
                                      rejected_tropes=[])):
            with self.assertRaises(SystemExit) as cm:
                pp.main()
        self.assertTrue(cm.exception.code)
        self.assertIn("--file", str(cm.exception.code))

    def test_stage_decide_refuses_without_running(self):
        # Superseded by stage-split step 3: 'decide' no longer
        # refuses as unshipped — it dispatches (after state
        # migration, like merge) and demands its --file target,
        # the same shape as test_stage_merge_needs_a_file. The
        # refusal pin for a stage that has not landed moves to
        # 'extract' (still pinned above). Step 3's own suite
        # (tests/test_stage_decide.py) pins the decide dispatch
        # and its refusals in full.
        with mock.patch.object(sys, "argv",
                               ["process-portable.py", "--stage",
                                "decide"]), \
                mock.patch.object(pp, "ensure_state_migrated",
                                  lambda *a, **k: None), \
                mock.patch.object(pp, "LearnedState",
                                  lambda *a, **k: mock.Mock(
                                      dir="/tmp", nicknames={},
                                      trusted_titles=lambda: [],
                                      rejected_tropes=[])):
            with self.assertRaises(SystemExit) as cm:
                pp.main()
        self.assertTrue(cm.exception.code)
        self.assertIn("--file", str(cm.exception.code))

    def test_stage_merge_needs_a_file(self):
        with mock.patch.object(sys, "argv",
                               ["process-portable.py", "--stage",
                                "merge"]), \
                mock.patch.object(pp, "ensure_state_migrated",
                                  lambda *a, **k: None), \
                mock.patch.object(pp, "LearnedState",
                                  lambda *a, **k: mock.Mock(
                                      dir="/tmp", nicknames={},
                                      trusted_titles=lambda: [],
                                      rejected_tropes=[])):
            with self.assertRaises(SystemExit) as cm:
                pp.main()
        self.assertTrue(cm.exception.code)
        self.assertIn("--file", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
