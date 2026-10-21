"""Stage-split step 3: Stage D (Decide) factored behind one function,
a standalone --stage decide entry, and run-history stage linkage.

Spec: stage-split-spec-2026-10-21.md §1, §2 (Stage D), §5, §7, §8
step 3.

The contracts pinned here:

1. ONE implementation, two drivers (§2): process_file_v2's
   downstream segment and the standalone stage entry both call
   stage_decide_book; given identical artefacts and an identical
   judge, they produce an identical validated claim set and book
   record.

2. THE HARD STOP (§2): Stage D refuses a merged roster whose
   extraction fingerprint or extraction-flags hash differs from
   the extraction stamp's — deciding against a roster merged from
   a different extraction is the silent mix the split exists to
   prevent. Missing/incomplete/chunk-limited/format-mismatched
   artefacts refuse the same legible way Stage M's do (R4).

3. Preview-only: the standalone stage emits the preview artefact
   and never writes app data; an invocation that asks for a live
   write (no --preview) or a dry run is refused by name.

4. Run-history linkage: the stage run is recorded through the same
   recorder as any run, with the stage named in the row's existing
   mode field (no schema change); the preview's decide_report
   carries all three stage fingerprints.

5. The decide-config fingerprint (§3) covers what Stage D
   consumes: flipping the judge moves it; flipping a merge-only
   flag does not.

Hermetic: the relationship judge and the trope-gate backend are
doubles; the book acquisition is doubled at the _acquire_chapters
seam (the fingerprint + chapter-count checks inside cmd_stage_decide
tie the doubled acquisition to the seeded stamp). Stdlib unittest
only.
Run: python -m unittest discover -s tests -v
"""

import contextlib
import copy
import importlib.util
import inspect
import io
import json
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

_CH1 = ("Fen Riorson was Xaden's father, and the rebellion died "
        "with him. Xaden Riorson bowed his head in the courtyard "
        "while the wardens watched from the wall above them all.")
_CH2 = ("Xaden Riorson trained at dawn while Fen Riorson watched "
        "from the wall. Ann Lee timed the passes and called the "
        "count aloud for each form the blade described.")
_CH3 = ("The rebellion embers glowed in the grate. Xaden Riorson "
        "slept at last, and the house stood quiet over the valley "
        "until the morning bells rang across the water.")

CHAPTERS = [
    {"index": 1, "label": "Ch 1", "text": _CH1},
    {"index": 2, "label": "Ch 2", "text": _CH2},
    {"index": 3, "label": "Ch 3", "text": _CH3},
]


def _b(summary, tropes=(), quotes=(), spice=0):
    full = {c: "none" for c in pp.TRIGGER_CATEGORIES}
    return {"summary": summary, "spice_level": spice,
            "triggers": full, "trigger_evidence": {},
            "trope_candidates": list(tropes),
            "quotes": list(quotes)}


def _a(characters=(), relationships=()):
    return {"characters": list(characters),
            "relationships": list(relationships),
            "task_status": {"identity": "ok"}}


_CHAR_FEN = {"name": "Fen Riorson", "aliases": [], "role": "supporting",
             "description": "", "evidence": ""}
_CHAR_XADEN = {"name": "Xaden Riorson", "aliases": [],
               "role": "protagonist", "description": "", "evidence": ""}
_CHAR_ANN = {"name": "Ann Lee", "aliases": [], "role": "supporting",
             "description": "", "evidence": ""}

PAYLOADS = {
    1: (_a(characters=[_CHAR_FEN, _CHAR_XADEN],
           relationships=[{"from": "Fen Riorson", "to": "Xaden Riorson",
                           "type": "parent", "evidence": ""}]),
        _b("Fen dies.", tropes=["Found Family"],
           quotes=[{"text": "Fen Riorson was Xaden's father, and the "
                            "rebellion died with him.",
                    "speaker": "Narration", "spoiler": False}])),
    2: (_a(characters=[_CHAR_ANN],
           relationships=[{"from": "Xaden Riorson", "to": "Ann Lee",
                           "type": "friend",
                           "evidence": "Ann Lee timed the passes"}]),
        _b("Xaden trains.", tropes=["Found Family"])),
    3: (_a(), _b("Embers.")),
}


def _roster():
    roster = {}
    pp._roster_update(roster, [_CHAR_FEN, _CHAR_XADEN], 1)
    pp._roster_update(roster, [_CHAR_ANN], 2)
    return roster


class _RelValidator:
    """Relationship-judge double: keeps everything it is shown and
    records the calls, so equivalence can compare judged sets."""

    model = "jev-test-double"
    disabled = False

    def __init__(self):
        self.calls = []

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls.append((frm, to, rtype, quote))
        return True, 0.95


def _fake_llm(prompt, chunk, i, n):
    """Trope-gate backend double (llm_ollama's call shape)."""
    return json.dumps({"verdicts": [{
        "trope": "Found Family", "verdict": "yes",
        "justification": "recurs across chapters"}]})


_DECIDE_CONFIG = {
    "pipeline": "v2", "llm": "ollama",
    "jev_merge": False, "roster_reconcile": False,
    "decision_model": True,
    "decision_model_provider": "openrouter",
    "decision_model_model": "jev-test",
    "evidence_retrieval": False, "use_corpus": False,
    "staging_gate": False, "corpus_quotes": False,
    "dedupe": False, "principals_only": True, "min_frequency": 0.20,
    "persist_roster": False, "emit_uncertainty": False,
    "show_matrices": False, "series_priors": False,
    "hardcover": False, "discovery_shadow": False,
    "supabase_url": None, "supabase_key": None,
    "run_history": False, "emit_run_events": False,
    "only_chunks": None, "jobs": 1,
}


def _fingerprint():
    return pp._checkpoint_fingerprint(
        IDENTIFIERS, TITLE, AUTHOR,
        pp._checkpoint_text_sample(CHAPTERS))


def _merge_report():
    return {"merge_config": pp._merge_config(),
            "merge_config_fingerprint": pp._merge_config_fingerprint(),
            "reconcile": None,
            "post_pass": {"merged": 0, "possible_merges": []},
            "jev": None, "series_priors_seeded": False,
            "series_priors": None, "verdicts": []}


class _StageDecideCase(unittest.TestCase):
    """Temp checkpoint root + temp SCRIPT_DIR + hermetic learned
    state and judge, restored after every test."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        tmp = self._tmp
        self._orig_ckpt_dir = pp._checkpoint_dir
        pp._checkpoint_dir = lambda book_key, _t=tmp: Path(_t) / book_key
        self.addCleanup(
            lambda: setattr(pp, "_checkpoint_dir", self._orig_ckpt_dir))
        self._orig_script_dir = pp.SCRIPT_DIR
        pp.SCRIPT_DIR = Path(tmp)
        self.addCleanup(
            lambda: setattr(pp, "SCRIPT_DIR", self._orig_script_dir))
        self._orig_learned = pp._LEARNED
        pp._LEARNED = pp.LearnedState(learn_dir=str(Path(tmp) / "learned"),
                                      enabled=True)
        self.addCleanup(
            lambda: setattr(pp, "_LEARNED", self._orig_learned))
        self._orig_validator = pp._DECISION_VALIDATOR
        self.validator = _RelValidator()
        pp._DECISION_VALIDATOR = self.validator
        self.addCleanup(
            lambda: setattr(pp, "_DECISION_VALIDATOR",
                            self._orig_validator))
        self._orig_history = pp._HISTORY
        pp._HISTORY = None
        self.addCleanup(lambda: setattr(pp, "_HISTORY", self._orig_history))
        self._orig_sink = pp._RUN_SINK
        pp._RUN_SINK = None
        self.addCleanup(lambda: setattr(pp, "_RUN_SINK", self._orig_sink))
        for attr, val in (("learn_isbn", None), ("book_author", None),
                          ("learn_series_key", None),
                          ("learn_series_name_key", None),
                          ("learn_series_hints", {}),
                          ("series_priors", None),
                          ("viz_book_key", None)):
            old = getattr(pp._DIAG, attr, None)
            setattr(pp._DIAG, attr, val)
            self.addCleanup(
                lambda a=attr, o=old: setattr(pp._DIAG, a, o))
        pp._dm_budget_reset(chapter_count=len(CHAPTERS))
        self.addCleanup(pp._dm_budget_reset)
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

    # -- fixture seeding -------------------------------------------

    def _seed_artefacts(self, book="stagebook"):
        """A complete Stage-E artefact set + a Stage-M artefact
        whose merge folded nothing (the fixture roster is already
        canonical). Returns (fingerprint, stamp)."""
        fp = _fingerprint()
        pp._checkpoint_save_meta(book, len(CHAPTERS), fingerprint=fp)
        for idx, (a, b) in PAYLOADS.items():
            pp._checkpoint_save_chapter(book, idx, a, b)
        pp._checkpoint_save_roster(book, _roster())
        ok = pp._checkpoint_save_extraction_stamp(
            book, fp, len(CHAPTERS))
        assert ok, "fixture stamp failed to land"
        seg = {"roster": _roster(), "merge_report": _merge_report()}
        ok = pp._save_merged_roster_artefact(
            book, seg, book_fingerprint=fp,
            extraction_flags_hash=pp._extraction_flags_hash(),
            extraction_processor_version=pp.PORTABLE_VERSION)
        assert ok, "fixture merged-roster artefact failed to land"
        return fp, pp._load_extraction_stamp(book)

    def _edit_artefact(self, book, **changes):
        p = Path(self._tmp) / book / "merged-roster.json"
        art = json.loads(p.read_text(encoding="utf-8"))
        art.update(changes)
        p.write_text(json.dumps(art), encoding="utf-8")
        return art

    def _edit_stamp(self, book, **changes):
        p = Path(self._tmp) / book / "extraction.json"
        stamp = json.loads(p.read_text(encoding="utf-8"))
        stamp.update(changes)
        p.write_text(json.dumps(stamp), encoding="utf-8")
        return stamp

    def _acquire(self, **over):
        acq = {"chapters": copy.deepcopy(CHAPTERS), "title": TITLE,
               "author": AUTHOR, "identifiers": dict(IDENTIFIERS),
               "word_count": 42, "chapter_detection_fallback": False}
        acq.update(over)
        return acq

    def _run_cmd(self, book_file="stagebook.epub", config=None,
                 preview=True, dry_run=False):
        fpath = Path(self._tmp) / book_file
        if not fpath.exists():
            fpath.write_bytes(b"fixture")
        out = io.StringIO()
        with mock.patch.dict(pp.CONFIG, config or _DECIDE_CONFIG), \
                mock.patch.object(pp, "_acquire_chapters",
                                  return_value=self._acquire()), \
                mock.patch.object(pp, "_detect_series",
                                  return_value=(None, None, "none")), \
                mock.patch.object(pp, "llm_ollama", _fake_llm), \
                contextlib.redirect_stdout(out):
            rc = pp.cmd_stage_decide(fpath, preview=preview,
                                     dry_run=dry_run)
        return rc, out.getvalue()

    def _preview(self, pattern="stagebook*.json"):
        hits = sorted((Path(self._tmp) / "preview").glob(pattern))
        assert hits, "no preview artefact written"
        return json.loads(hits[-1].read_text(encoding="utf-8"))

    def _drive_monolith_side(self):
        """stage_decide_book driven the way process_file_v2 drives
        it: live state, the merge segment's own outputs, shared-run
        budget basis. Seeds the artefact set both drivers consume."""
        fp, stamp = self._seed_artefacts()
        art = json.loads((Path(self._tmp) / "stagebook"
                          / "merged-roster.json")
                         .read_text(encoding="utf-8"))
        roster = pp._artefact_roster(art)
        _meta, _raw, payloads = pp._checkpoint_load("stagebook")
        chapter_as = {i: ab[0] for i, ab in payloads.items()}
        chapter_bs = {i: ab[1] for i, ab in payloads.items()}
        indexed = pp._select_chunks(CHAPTERS, None)
        pp._dm_budget_reset(chapter_count=len(CHAPTERS))
        out = io.StringIO()
        with mock.patch.dict(pp.CONFIG, _DECIDE_CONFIG), \
                contextlib.redirect_stdout(out):
            seg = pp.stage_decide_book(
                roster, CHAPTERS, indexed, chapter_as, chapter_bs,
                fpath=Path(self._tmp) / "stagebook.epub",
                _book_key="book", _ckpt_key="stagebook", _ckpt_fp=fp,
                title=TITLE, author=AUTHOR, identifiers=IDENTIFIERS,
                _isbn_source="epub", _series_name=None, _series_pos=None,
                _series_source="none", word_count=42,
                _chap_fallback=False,
                a_ok=3, b_ok=3, a_failed=0, b_failed=0, a_fallback=0,
                aborted=False, dry_run=False, preview=True,
                call=_fake_llm,
                _recon_stats=None, _pp_merged=0, _pp_possible=[],
                _merge_judge_stats=None,
                priors_report=None, priors_source=None,
                _resumed_chapters=payloads,
                extraction_flags_hash=stamp["extraction_flags_hash"],
                merge_config_fingerprint=art["merge_config_fingerprint"],
                budget_basis="shared-run")
        return seg


# ---------------------------------------------------------------------------
# Decide-config fingerprint
# ---------------------------------------------------------------------------

class TestDecideConfigFingerprint(unittest.TestCase):
    def test_judge_flip_changes_fingerprint(self):
        base = {"decision_model": True,
                "decision_model_provider": "openrouter",
                "decision_model_model": "jev-a"}
        with mock.patch.dict(pp.CONFIG, base):
            a = pp._decide_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {
                **base, "decision_model_provider": "local"}):
            b = pp._decide_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {
                **base, "decision_model_model": "jev-b"}):
            c = pp._decide_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {
                **base, "decision_model": False}):
            d = pp._decide_config_fingerprint()
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)

    def test_decide_toggles_change_fingerprint(self):
        with mock.patch.dict(pp.CONFIG, {"evidence_retrieval": False,
                                         "use_corpus": False}):
            a = pp._decide_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {"evidence_retrieval": True,
                                         "use_corpus": False}):
            b = pp._decide_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {"evidence_retrieval": False,
                                         "use_corpus": True}):
            c = pp._decide_config_fingerprint()
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_gate_backend_change_moves_fingerprint(self):
        # The trope gate is judged by the extractor backend, so the
        # backend is part of Stage D's identity too.
        with mock.patch.dict(pp.CONFIG, {"llm": "ollama"}):
            a = pp._decide_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {"llm": "openai"}):
            b = pp._decide_config_fingerprint()
        self.assertNotEqual(a, b)

    def test_merge_only_flips_do_not_move_fingerprint(self):
        # Stage M's identity is Stage M's: flipping it must not
        # re-identify a decide, or re-deciding after a re-merge
        # under a different merge config would read as a different
        # decide config (the merge fingerprint is stamped
        # separately on the decide_report).
        base = {"jev_merge": False, "roster_reconcile": False}
        with mock.patch.dict(pp.CONFIG, base):
            a = pp._decide_config_fingerprint()
        with mock.patch.dict(pp.CONFIG, {**base, "jev_merge": True}):
            b = pp._decide_config_fingerprint()
        with mock.patch.dict(pp.CONFIG,
                             {**base, "roster_reconcile": True}):
            c = pp._decide_config_fingerprint()
        self.assertEqual(a, b)
        self.assertEqual(a, c)


# ---------------------------------------------------------------------------
# The shared stage function
# ---------------------------------------------------------------------------

class TestStageDecideBook(_StageDecideCase):
    def test_validates_and_stamps_decide_report(self):
        seg = self._drive_monolith_side()
        result = seg["result"]
        self.assertIsNotNone(result)
        rels = {(r["from"], r["to"], r["type"]): r
                for r in result["relationships"]}
        # Both claims judged and kept: the family claim rides the
        # binding mined sentence into validation; the friend claim
        # carries its own quote.
        self.assertEqual(
            {(f, t, ty) for (f, t, ty) in rels},
            {("Fen Riorson", "Xaden Riorson", "parent"),
             ("Ann Lee", "Xaden Riorson", "friend")})
        self.assertEqual(rels[("Fen Riorson", "Xaden Riorson",
                               "parent")]["validation_note"], "kept")
        self.assertEqual(result["tropes"], ["Found Family"])
        report = result["decide_report"]
        self.assertEqual(report["extraction_fingerprint"],
                         _fingerprint())
        self.assertEqual(
            report["merge_config_fingerprint"],
            pp._merge_config_fingerprint())
        with mock.patch.dict(pp.CONFIG, _DECIDE_CONFIG):
            self.assertEqual(report["decide_config_fingerprint"],
                             pp._decide_config_fingerprint())
        self.assertEqual(report["budget"]["basis"], "shared-run")
        self.assertEqual(report["budget"]["chapters"], 3)

    def test_process_file_v2_routes_through_the_shared_function(self):
        # §2's structural pin: the monolith's downstream segment
        # calls the shared function and no longer calls the decide
        # machinery directly.
        src = inspect.getsource(pp.process_file_v2)
        self.assertIn("stage_decide_book(", src)
        self.assertNotIn("v2_reduce(", src)
        self.assertNotIn("v2_trope_gate(", src)
        self.assertNotIn("evidence_retrieve_result(", src)


def _full_entry(name, chapters, appearances, alias_keys=()):
    """A roster entry in the full shape _roster_update builds,
    for a test that must control entries the update-time tiers
    would have merged before the seam ever saw them (the shape
    is test_series_priors' _entry, plus explicit alias claims)."""
    key = pp.norm_name(name)
    return {"name": name, "aliases": set(),
            "alias_keys": {key}
            | {pp.norm_name(a) for a in alias_keys},
            "primary_keys": {key}, "_generic_keys": set(),
            "appearances": appearances,
            "last_seen": max(chapters) if chapters else 0,
            "chapters": set(chapters), "roles": Counter(),
            "descriptions": [], "evidence": ""}


class TestPriorsReportTiming(_StageDecideCase):
    """The monolith's series-priors report must be read AFTER
    the Option B identity seam inside the stage, the point the
    pre-split pipeline always computed it: the seam's folds can
    still move the counters (a canonical tiebreak), so a report
    snapshotted by the driver before the stage runs undercounts
    form_tiebreaks whenever --series-priors and --persist-roster
    are both on and the seam folds a same-registry pair."""

    def test_seam_tiebreak_lands_in_the_book_records_report(self):
        _sr_spec = importlib.util.spec_from_file_location(
            "series_registry",
            str(Path(__file__).resolve().parent.parent
                / "series_registry.py"))
        sr = importlib.util.module_from_spec(_sr_spec)
        sys.modules["series_registry"] = sr
        _sr_spec.loader.exec_module(sr)
        reg = sr.SeriesRegistry(
            path=Path(self._tmp) / "series_registry.db")
        self.addCleanup(reg.close)
        # The earlier book: Sebeck attested under his canonical
        # form, with the nickname as a recorded alias — the
        # registry character both roster entries below match.
        sr.populate_from_record(reg, {
            "title": "Daemon", "author": "Daniel Suarez",
            "work_id": "book-1", "series_name": "Daemon",
            "series_position": 1, "series_source": "hardcover",
            "series_key": "hc-series:999",
            "hardcover": {"compilation": False},
            "characters": [{"name": "Peter Sebeck",
                            "aliases": ["Pete Sebeck", "Sebeck"]}],
            "relationships": []})
        saved_registry = pp._SR_REGISTRY
        pp._SR_REGISTRY = (sr, reg)
        self.addCleanup(setattr, pp, "_SR_REGISTRY", saved_registry)

        cfg = dict(_DECIDE_CONFIG, series_priors=True,
                   persist_roster=True)
        with mock.patch.dict(pp.CONFIG, cfg):
            ctx = pp._load_series_priors("hc-series:999")
        self.assertIsNotNone(ctx)
        pp._DIAG.series_priors = ctx

        # A previous run minted one entity for the canonical
        # form, its extraction claiming the nickname as an alias
        # — the substrate this run's two entries both link to.
        store, _wid = pp._roster_store_for(TITLE, AUTHOR,
                                           IDENTIFIERS)
        self.assertIsNotNone(store)
        roster1 = {"peter sebeck": _full_entry(
            "Peter Sebeck", (1, 2), appearances=40,
            alias_keys=("Pete Sebeck",))}
        with contextlib.redirect_stdout(io.StringIO()):
            pp.roster_assign_entities(roster1, store, anchors=[])

        # This run's post-merge roster still holds the pair as
        # two entries (disjoint chapters, mutual form claims):
        # the seam links both to the minted entity and folds
        # them, keeper = the better-attested nickname form.
        roster = {
            "pete sebeck": _full_entry(
                "Pete Sebeck", (1, 2), appearances=30,
                alias_keys=("Peter Sebeck",)),
            "peter sebeck": _full_entry(
                "Peter Sebeck", (3,), appearances=12),
        }
        _char = {"name": "Pete Sebeck", "aliases": [],
                 "role": "supporting", "description": "",
                 "evidence": ""}
        payloads = {1: (_a(characters=[_char]), _b("One.")),
                    2: (_a(characters=[_char]), _b("Two.")),
                    3: (_a(), _b("Three."))}
        chapter_as = {i: ab[0] for i, ab in payloads.items()}
        chapter_bs = {i: ab[1] for i, ab in payloads.items()}
        indexed = pp._select_chunks(CHAPTERS, None)
        pp._dm_budget_reset(chapter_count=len(CHAPTERS))
        out = io.StringIO()
        with mock.patch.dict(pp.CONFIG, cfg), \
                contextlib.redirect_stdout(out):
            seg = pp.stage_decide_book(
                roster, CHAPTERS, indexed, chapter_as, chapter_bs,
                fpath=Path(self._tmp) / "stagebook.epub",
                _book_key="book", _ckpt_key="stagebook",
                _ckpt_fp=_fingerprint(),
                title=TITLE, author=AUTHOR, identifiers=IDENTIFIERS,
                _isbn_source="epub", _series_name=None,
                _series_pos=None, _series_source="none",
                word_count=42, _chap_fallback=False,
                a_ok=3, b_ok=3, a_failed=0, b_failed=0, a_fallback=0,
                aborted=False, dry_run=False, preview=True,
                call=_fake_llm,
                _recon_stats=None, _pp_merged=0, _pp_possible=[],
                _merge_judge_stats=None,
                # The monolith driver's shape: the live report as
                # a zero-arg callable, resolved by the stage at
                # result assembly — after the seam.
                priors_report=lambda: pp._series_priors_report(ctx),
                priors_source="live",
                _resumed_chapters=payloads,
                extraction_flags_hash=pp._extraction_flags_hash(),
                merge_config_fingerprint=pp._merge_config_fingerprint(),
                budget_basis="shared-run")
        result = seg["result"]
        self.assertIsNotNone(result)
        # The seam really folded the pair, and the registry's
        # canonical spelling won the display-name tie.
        self.assertEqual(len(roster), 1)
        self.assertEqual(next(iter(roster.values()))["name"],
                         "Peter Sebeck")
        self.assertEqual(ctx["counts"]["form_tiebreaks"], 1)
        # …and the book record's report counts that increment:
        # the pre-seam snapshot the buggy driver took read 0.
        self.assertEqual(result["series_priors"]["form_tiebreaks"], 1)
        self.assertEqual(
            result["decide_report"]["series_priors_source"], "live")


# ---------------------------------------------------------------------------
# The standalone entry
# ---------------------------------------------------------------------------

class TestCmdStageDecide(_StageDecideCase):
    def test_happy_path_writes_preview(self):
        self._seed_artefacts()
        rc, _out = self._run_cmd()
        self.assertEqual(rc, 0)
        prev = self._preview()
        self.assertEqual(prev["tropes"], ["Found Family"])
        rels = {(r["from"], r["to"], r["type"])
                for r in prev["relationships"]}
        self.assertEqual(rels, {("Fen Riorson", "Xaden Riorson",
                                 "parent"),
                                ("Ann Lee", "Xaden Riorson",
                                 "friend")})
        report = prev["decide_report"]
        self.assertEqual(report["budget"]["basis"], "per-stage")
        self.assertEqual(report["extraction_fingerprint"],
                         _fingerprint())
        # The standalone stage must NOT clear the artefacts it
        # consumed — they are the other stages' products, and a
        # re-decide under another judge starts from them.
        self.assertTrue((Path(self._tmp) / "stagebook"
                         / "merged-roster.json").exists())
        self.assertTrue((Path(self._tmp) / "stagebook"
                         / "extraction.json").exists())

    def test_equivalence_with_the_monolith_driver(self):
        # Spec §7, mocked-transport form: the standalone stage over
        # the seeded artefacts produces the same validated claim
        # set and book record as the shared function driven with
        # the monolith's own state (the artefact set both drivers
        # consume is seeded once, by the driver-A helper).
        seg = self._drive_monolith_side()
        expected = seg["result"]
        rc, _out = self._run_cmd()
        self.assertEqual(rc, 0)
        prev = self._preview()
        for field in ("relationships", "dropped_relationships",
                      "tropes", "trope_confidence", "characters",
                      "minor_characters", "triggers", "quotes",
                      "chapter_summaries", "spice_level", "povs",
                      "work_id", "work_resolution", "roster_size",
                      "self_pairs_dropped", "possible_merges",
                      "post_pass_merged", "chapter_data"):
            self.assertEqual(prev[field], expected[field],
                             f"field {field} diverged")
        rep_a = dict(prev["decide_report"])
        rep_b = dict(expected["decide_report"])
        basis_a = rep_a.pop("budget")
        basis_b = rep_b.pop("budget")
        self.assertEqual(basis_a.pop("basis"), "per-stage")
        self.assertEqual(basis_b.pop("basis"), "shared-run")
        self.assertEqual(basis_a, basis_b)
        self.assertEqual(rep_a, rep_b)

    # -- the hard stops ---------------------------------------------

    def test_no_stamp_refuses(self):
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("no extraction stamp", out)

    def test_incomplete_extraction_refuses(self):
        self._seed_artefacts()
        (Path(self._tmp) / "stagebook" / "chapter_2.json").unlink()
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("not complete", out)

    def test_chunk_limited_extraction_refuses_by_name(self):
        self._seed_artefacts()
        stamp = pp._load_extraction_stamp("stagebook")
        flags = dict(stamp["extraction_flags"])
        flags["only_chunks"] = "1,2"
        self._edit_stamp("stagebook", extraction_flags=flags)
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("--chunks", out)

    def test_format_mismatch_refuses(self):
        self._seed_artefacts()
        self._edit_stamp("stagebook", artefact_format_version=999)
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("format", out.lower())

    def test_missing_merged_roster_refuses(self):
        self._seed_artefacts()
        (Path(self._tmp) / "stagebook" / "merged-roster.json").unlink()
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("merged-roster", out)

    def test_artefact_format_mismatch_refuses(self):
        self._seed_artefacts()
        self._edit_artefact("stagebook", artefact_format_version=999)
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("format", out.lower())

    def test_fingerprint_mismatch_is_a_hard_stop(self):
        # THE §2 stop: the roster was merged from a DIFFERENT
        # extraction than the stamp's. Deciding anyway would judge
        # one extraction's claims against another's roster.
        self._seed_artefacts()
        other = pp._checkpoint_fingerprint(
            {"isbn": "9780000000002"}, TITLE, AUTHOR,
            pp._checkpoint_text_sample(CHAPTERS))
        self._edit_artefact("stagebook", book_fingerprint=other)
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("fingerprint", out.lower())
        self.assertFalse(
            (Path(self._tmp) / "preview").exists())

    def test_flags_hash_mismatch_is_a_hard_stop(self):
        self._seed_artefacts()
        self._edit_artefact("stagebook",
                            extraction_flags_hash="0" * 16)
        rc, out = self._run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("flags", out.lower())
        self.assertFalse(
            (Path(self._tmp) / "preview").exists())

    def test_cross_version_artefacts_warn_and_proceed(self):
        self._seed_artefacts()
        self._edit_stamp("stagebook", processor_version="2.13.50")
        self._edit_artefact("stagebook", processor_version="2.13.60")
        rc, out = self._run_cmd()
        self.assertEqual(rc, 0)
        self.assertIn("2.13.50", out)
        prev = self._preview()
        self.assertEqual(prev["decide_report"]["processor_version"],
                         pp.PORTABLE_VERSION)

    # -- preview-only posture ---------------------------------------

    def test_live_write_invocation_refuses(self):
        # No --preview in monolith terms is a write run; Stage D
        # never writes app data, so the invocation refuses by name
        # instead of silently producing (or not producing) a write.
        self._seed_artefacts()
        rc, out = self._run_cmd(preview=False)
        self.assertEqual(rc, 1)
        self.assertIn("--preview", out)
        self.assertFalse(
            (Path(self._tmp) / "preview").exists())

    def test_dry_run_invocation_refuses(self):
        self._seed_artefacts()
        rc, out = self._run_cmd(preview=False, dry_run=True)
        self.assertEqual(rc, 1)
        self.assertIn("dry", out.lower())

    # -- run-history linkage ----------------------------------------

    def test_run_history_row_names_the_stage(self):
        self._seed_artefacts()
        posts = []

        def _fake_sb(table, method="GET", data=None, params="",
                     prefer=None, schema=None):
            posts.append((table, method, data))
            if method == "GET":
                return []
            if table == "processor_runs":
                return [{"id": "00000000-0000-0000-0000-000000000001"}]
            if table == "processor_run_books":
                return [{"id": "00000000-0000-0000-0000-000000000002",
                         "book_key": row["book_key"]} for row in data]
            return [{"id": "x"}]

        config = dict(_DECIDE_CONFIG)
        config.update({"run_history": True,
                       "supabase_url": "https://fake.invalid",
                       "supabase_key": "fake-key"})
        fpath = Path(self._tmp) / "stagebook.epub"
        fpath.write_bytes(b"fixture")
        out = io.StringIO()
        with mock.patch.dict(pp.CONFIG, config), \
                mock.patch.object(pp, "_acquire_chapters",
                                  return_value=self._acquire()), \
                mock.patch.object(pp, "_detect_series",
                                  return_value=(None, None, "none")), \
                mock.patch.object(pp, "llm_ollama", _fake_llm), \
                mock.patch.object(pp, "sb", _fake_sb), \
                mock.patch.object(pp, "_load_rel_registry",
                                  lambda *a, **k: None), \
                contextlib.redirect_stdout(out):
            rc = pp.cmd_stage_decide(fpath, preview=True)
        self.assertEqual(rc, 0)
        run_posts = [d for (t, m, d) in posts
                     if t == "processor_runs" and m == "POST"]
        self.assertEqual(len(run_posts), 1)
        row = run_posts[0][0]
        # The stage rides the row's EXISTING mode field — the same
        # column that carries "scan" / "watch_cycle" / "replay" —
        # so no schema change was needed to name it.
        self.assertEqual(row["mode"], "stage_decide")
        self.assertTrue(row["preview"])
        book_posts = [d for (t, m, d) in posts
                      if t == "processor_run_books" and m == "POST"]
        self.assertEqual(len(book_posts), 1)
        self.assertEqual(book_posts[0][0]["outcome"], "processed")
        # The preview the stage emitted is named with the same run
        # key the history row carries — the chain of custody joins.
        prevs = list((Path(self._tmp) / "preview").glob(
            f"stagebook--{row['run_key']}.json"))
        self.assertEqual(len(prevs), 1)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

class TestStageDecideCli(unittest.TestCase):
    def setUp(self):
        self._old_learned = pp._LEARNED
        self.addCleanup(lambda: setattr(pp, "_LEARNED",
                                        self._old_learned))

    def test_stage_decide_with_file_and_preview_is_coherent(self):
        parser = pp._build_parser()
        defaults = parser.parse_args([])
        args = parser.parse_args(
            ["--stage", "decide", "--file", "book.epub", "--preview"])
        self.assertIsNone(pp._flag_conflicts(args, defaults))

    def test_stage_merge_with_preview_still_conflicts(self):
        # The --preview exemption belongs to decide alone: Stage M
        # neither previews nor writes, so the flag beside it would
        # still be silently dropped.
        parser = pp._build_parser()
        defaults = parser.parse_args([])
        args = parser.parse_args(
            ["--stage", "merge", "--file", "book.epub", "--preview"])
        self.assertIsNotNone(pp._flag_conflicts(args, defaults))

    def test_stage_analyze_needs_file_and_extraction(self):
        with mock.patch.object(sys, "argv",
                               ["process-portable.py", "--stage",
                                "analyze"]), \
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
        self.assertIn("--extraction", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
