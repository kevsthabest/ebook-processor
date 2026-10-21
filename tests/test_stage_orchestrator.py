"""Stage-split step 4: the --stage extract entry, the orchestrator
contract, and the strict end-to-end equivalence proof.

Spec: stage-split-spec-2026-10-21.md §1, §5, §6, §7, §8 step 4.

The contracts pinned here:

1. --stage extract runs Stage E alone over --file: the v2 chapter
   loop lands a COMPLETE extraction stamp and stops — no merge, no
   decide, no preview, no app write — and the artefact set
   PERSISTS (a finished monolith run clears its checkpoints at the
   end; a stage run's checkpoints are the deliverable the later
   stages consume). The run is recorded in run history with the
   stage named in the row's existing mode field, exactly one row.

2. A partial/aborted stage extraction lands no complete stamp and
   exits non-zero, and the downstream stages refuse it through the
   same completeness checks they apply to any incomplete
   extraction. A chunk-limited stage extraction is allowed (the
   stamp records --chunks, as it does for any run) and the
   downstream stages refuse IT, by name, as they already do.

3. THE ORCHESTRATOR CONTRACT (§7, strict mocked form): one fixture
   book driven (a) through the monolith (process_file_v2, preview
   posture) and (b) through the staged sequence (extract -> merge
   -> decide as separate entries), with BOTH the extractor backend
   and the decision judge doubled deterministically, produces the
   same merged-roster artefact (canonical roster + verdict table +
   packets, byte-for-byte apart from the write timestamp), the
   same validated claim set, the same inline pack score, and a
   book record that matches field-for-field — modulo the stamped
   divergences, which are asserted to be the ONLY differences:
   decide_report.budget.basis (shared-run vs per-stage), the
   a_fallback task telemetry (the staged decide stamps 0 — Call
   A's fell_back is a return value, never persisted), and the
   per-invocation telemetry accounting (a staged decide's
   telemetry describes the decide invocation: no extraction
   counters, no merge-stage decision calls — the same per-stage
   basis the budget stamp names).

Hermetic: real tiny EPUB, deterministic backend/judge doubles, no
network, no Supabase. Stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""

import contextlib
import copy
import importlib.util
import io
import json
import re
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


# ---------------------------------------------------------------------------
# Fixture book
# ---------------------------------------------------------------------------

TITLE = "Orchestrator Test"
AUTHOR = "A. Writer"
ISBN = "9780306406157"

_CH1 = ("Fen Riorson was Xaden's father, and the rebellion died "
        "with him. Xaden Riorson bowed his head in the courtyard "
        "while the wardens watched from the wall above them all.")
_CH2 = ("Xaden Riorson trained at dawn while Fen Riorson watched "
        "from the wall. Ann Lee timed the passes and called the "
        "count aloud for each form the blade described.")
_CH3 = ("The rebellion embers glowed in the grate. Xaden slept at "
        "last, and the house stood quiet over the valley until "
        "the morning bells rang across the water.")
_CHAPTER_TEXTS = [_CH1, _CH2, _CH3]


def _make_epub(path):
    files = {}
    manifest, spine = [], []
    for i, text in enumerate(_CHAPTER_TEXTS, 1):
        name = f"ch{i:02d}.xhtml"
        # Repeated past MIN_CHAPTER_CHARS (1500): undersized spine
        # units merge into the next one, and this fixture needs
        # three real chapters.
        body = "".join(f"<p>{text}</p>" for _ in range(10))
        files[f"OEBPS/{name}"] = (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.w3.org/1999/xhtml'>"
            f"<head><title>Chapter {i}</title></head>"
            f"<body><h1>Chapter {i}</h1>{body}</body></html>")
        manifest.append(
            f"<item id='f{i}' href='{name}' "
            "media-type='application/xhtml+xml'/>")
        spine.append(f"<itemref idref='f{i}'/>")
    opf = ("<?xml version='1.0' encoding='utf-8'?>"
           "<package xmlns='http://www.idpf.org/2007/opf' version='3.0' "
           "unique-identifier='bid'>"
           "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
           f"<dc:title>{TITLE}</dc:title>"
           f"<dc:creator>{AUTHOR}</dc:creator>"
           f"<dc:identifier>{ISBN}</dc:identifier></metadata>"
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


def _char(name, role):
    return {"name": name, "aliases": [], "role": role,
            "description": "", "evidence": ""}


_CALL_A = {
    "father": {"characters": [_char("Fen Riorson", "supporting"),
                              _char("Xaden Riorson", "protagonist")],
               "relationships": [{
                   "from": "Fen Riorson", "to": "Xaden Riorson",
                   "type": "parent", "evidence": ""}],
               "pov_character": "Xaden Riorson"},
    "passes": {"characters": [_char("Ann Lee", "supporting")],
               "relationships": [{
                   "from": "Xaden Riorson", "to": "Ann Lee",
                   "type": "friend",
                   "evidence": "Ann Lee timed the passes"}],
               "pov_character": None},
    "quiet": {"characters": [_char("Xaden", "supporting")],
              "relationships": [], "pov_character": None},
}

_CALL_B = {
    "father": {"summary": "Fen dies.", "spice_level": 0,
               "triggers": {}, "trigger_evidence": {},
               "trope_candidates": ["Found Family"],
               "quotes": [{
                   "text": "Fen Riorson was Xaden's father, and the "
                           "rebellion died with him.",
                   "speaker": "Narration", "spoiler": False}]},
    "passes": {"summary": "Xaden trains.", "spice_level": 0,
               "triggers": {}, "trigger_evidence": {},
               "trope_candidates": ["Found Family"], "quotes": []},
    "quiet": {"summary": "Embers.", "spice_level": 0,
              "triggers": {}, "trigger_evidence": {},
              "trope_candidates": [], "quotes": []},
}


def _chapter_key(chunk):
    if "was Xaden's father" in chunk:
        return "father"
    if "timed the passes" in chunk:
        return "passes"
    if "stood quiet" in chunk:
        return "quiet"
    raise AssertionError(f"unrecognised chapter chunk: {chunk[:80]!r}")


class _ExtractorBackend:
    """Deterministic extractor double (llm_ollama's call shape).
    mode 'dead' answers None to everything — the all-calls-failed
    abort shape. Every call is classified and counted, so a test
    can prove WHICH stage spent extraction calls."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = {"a": 0, "b": 0, "gate": 0}

    def __call__(self, prompt, chunk, i, n):
        if self.mode == "dead":
            return None
        if prompt == pp.PROMPT_V2_CONTENT:
            self.calls["b"] += 1
            return json.dumps(_CALL_B[_chapter_key(chunk)])
        if prompt.startswith(pp.PROMPT_V2_TROPE_GATE[:40]):
            self.calls["gate"] += 1
            return json.dumps({"verdicts": [{
                "trope": "Found Family", "verdict": "yes",
                "justification": "recurs across chapters"}]})
        self.calls["a"] += 1
        return json.dumps(_CALL_A[_chapter_key(chunk)])


class _Judge:
    """Decision-judge double for BOTH stage consumers: the jev
    merge pass (packet interface) and relationship validation.
    Deterministic; records what it was asked, in order."""

    disabled = False
    model = "jev-test-double"

    def __init__(self):
        self.packets = []
        self.rel_calls = []

    def ask_same_person_packet(self, state):
        self.packets.append(state)
        a = re.search(r"^Character A: (.+)$", state, re.M).group(1)
        b = re.search(r"^Character B: (.+)$", state, re.M).group(1)
        if {a, b} == {"Xaden", "Xaden Riorson"}:
            return (True, 0.93)
        return (False, 0.10)

    def ask_same_person(self, name_a, desc_a, ev_a,
                        name_b, desc_b, ev_b):
        return (False, 0.10)

    def validate_relationship(self, frm, to, rtype, quote,
                              context=""):
        self.rel_calls.append((frm, to, rtype))
        return True, 0.95


_CONFIG = {
    "pipeline": "v2", "llm": "ollama", "batch_size": 1, "jobs": 1,
    "jev_merge": True, "roster_reconcile": False,
    "decision_model": True,
    "decision_model_provider": "openrouter",
    "decision_model_model": "jev-test",
    "evidence_retrieval": False, "use_corpus": False,
    "staging_gate": False, "corpus_quotes": False, "dedupe": False,
    "principals_only": True, "min_frequency": 0.20,
    "persist_roster": False, "emit_uncertainty": False,
    "show_matrices": False, "series_priors": False,
    "hardcover": False, "discovery_shadow": False,
    "supabase_url": None, "supabase_key": None,
    "run_history": False, "emit_run_events": False,
    "only_chunks": None, "resume": False, "allow_no_isbn": False,
    "rename_no_isbn": False, "v2_prompt_cache": False,
    "debug": False,
}

_BOOK = "eqbook"


class _Env:
    """One phase's hermetic surroundings: temp checkpoint root,
    temp SCRIPT_DIR (preview capture), temp learned state, fresh
    judge + backend doubles. Entering swaps the module globals;
    exiting restores them."""

    def __init__(self, case, root):
        self.root = Path(root)
        self.backend = _ExtractorBackend()
        self.judge = _Judge()
        self._case = case

    def __enter__(self):
        case = self._case
        tmp = str(self.root)
        # save_preview mkdirs SCRIPT_DIR/preview without parents —
        # in real runs SCRIPT_DIR (the install dir) exists.
        (self.root / "script").mkdir(parents=True, exist_ok=True)
        case._push(pp, "_checkpoint_dir",
                   lambda book_key, _t=tmp: Path(_t) / "ck" / book_key)
        case._push(pp, "SCRIPT_DIR", self.root / "script")
        case._push(pp, "_LEARNED",
                   pp.LearnedState(learn_dir=str(self.root / "learned"),
                                   enabled=True))
        case._push(pp, "_DECISION_VALIDATOR", self.judge)
        case._push(pp, "_HISTORY", None)
        case._push(pp, "_RUN_SINK", None)
        case._push(pp, "llm_ollama", self.backend)
        # _DIAG's per-book fields don't exist until a run sets
        # them; set + restore by value (None = unarmed), the
        # step-3 suite's convention.
        for attr, val in (("learn_isbn", None), ("book_author", None),
                          ("learn_series_key", None),
                          ("learn_series_name_key", None),
                          ("learn_series_hints", {}),
                          ("series_priors", None),
                          ("viz_book_key", None), ("name_attest", None)):
            old = getattr(pp._DIAG, attr, None)
            setattr(pp._DIAG, attr, val)
            case._stack.append(
                (pp._DIAG, attr, old))
        pp._dm_budget_reset(chapter_count=len(_CHAPTER_TEXTS))
        return self

    def __exit__(self, *exc):
        self._case._restore_all()
        pp._dm_budget_reset()
        return False

    @property
    def ckpt_dir(self):
        return self.root / "ck" / _BOOK

    def preview(self):
        hits = sorted((self.root / "script" / "preview").glob("*.json"))
        assert hits, "no preview artefact written"
        return json.loads(hits[-1].read_text(encoding="utf-8"))


class _Stage4Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._tmp,
                                              ignore_errors=True))
        self.epub = Path(self._tmp) / f"{_BOOK}.epub"
        _make_epub(self.epub)
        self._stack = []

    def _push(self, obj, attr, val):
        self._stack.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, val)

    def _restore_all(self):
        while self._stack:
            obj, attr, old = self._stack.pop()
            setattr(obj, attr, old)

    def tearDown(self):
        self._restore_all()

    @contextlib.contextmanager
    def _patches(self, config=None):
        with mock.patch.dict(pp.CONFIG, config or _CONFIG), \
                mock.patch.object(
                    pp, "_lookup_series_openlibrary",
                    lambda *a, **k: (None, None)), \
                mock.patch.object(pp, "resolve_work",
                                  lambda *a, **k: (None, "test")):
            yield

    def _run_quiet(self, fn, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fn(*args, **kwargs)
        return rc, out.getvalue()


# ---------------------------------------------------------------------------
# Stage E standalone
# ---------------------------------------------------------------------------

class TestStageExtract(_Stage4Case):
    def test_happy_path_stops_at_the_stamp(self):
        env = _Env(self, Path(self._tmp) / "s")
        with env, self._patches():
            rc, _out = self._run_quiet(pp.cmd_stage_extract, self.epub)
            self.assertEqual(rc, 0)
            # The artefact set is complete and PERSISTS: chapter
            # payloads, the roster, and a complete stamp.
            stamp = pp._load_extraction_stamp(_BOOK)
            self.assertIsNotNone(stamp)
            self.assertTrue(stamp["complete"])
            self.assertTrue(pp._extraction_complete(_BOOK))
            self.assertEqual(
                len(list(env.ckpt_dir.glob("chapter_*.json"))), 3)
            self.assertTrue((env.ckpt_dir / "roster.json").exists())
            # …and nothing downstream happened: no merge artefact,
            # no preview, and Stage E spent the extraction calls
            # (3 A + 3 B) and nothing else.
            self.assertFalse(
                (env.ckpt_dir / "merged-roster.json").exists())
            self.assertFalse(
                (env.root / "script" / "preview").exists())
            self.assertEqual(env.backend.calls, {"a": 3, "b": 3,
                                                 "gate": 0})
            self.assertEqual(env.judge.packets, [])
            self.assertEqual(env.judge.rel_calls, [])
            # The stage marker does not leak into later in-process
            # work (the staged sequence and any later monolith run
            # in this process must see an unmarked CONFIG).
            self.assertFalse(pp.CONFIG.get("stage_extract"))

    def test_aborted_extraction_exits_nonzero_and_merge_refuses(self):
        env = _Env(self, Path(self._tmp) / "s")
        env.backend = _ExtractorBackend(mode="dead")
        with env, self._patches():
            rc, _out = self._run_quiet(pp.cmd_stage_extract, self.epub)
            self.assertEqual(rc, 1)
            self.assertIsNone(pp._load_extraction_stamp(_BOOK))
            # The downstream stage refuses through its existing
            # completeness checks — no stamp, no merge.
            rc, out = self._run_quiet(pp.cmd_stage_merge, self.epub)
            self.assertEqual(rc, 1)
            self.assertIn("no extraction stamp", out)
            self.assertFalse(
                (env.ckpt_dir / "merged-roster.json").exists())

    def test_chunk_limited_extracts_and_downstream_refuses(self):
        env = _Env(self, Path(self._tmp) / "s")
        cfg = dict(_CONFIG, only_chunks="1")
        with env, self._patches(cfg):
            rc, _out = self._run_quiet(pp.cmd_stage_extract, self.epub)
            self.assertEqual(rc, 0)
            stamp = pp._load_extraction_stamp(_BOOK)
            self.assertTrue(stamp["complete"])
            self.assertEqual(
                stamp["extraction_flags"]["only_chunks"], "1")
            rc, out = self._run_quiet(pp.cmd_stage_merge, self.epub)
            self.assertEqual(rc, 1)
            self.assertIn("chunk-limited", out)

    def test_out_of_range_chunk_selection_fails_the_stage(self):
        # A --chunks selection whose indices fall outside the
        # selection's own count (e.g. "3" of a 3-chapter book):
        # the payloads carry the book's indices while meta's total
        # is the selection size, so the stamp's coverage check
        # cannot count them and no stamp lands — a property of
        # the checkpoint machinery a plain chunked monolith run
        # shares (its stamp is silent bookkeeping there). The
        # stage's product IS the stamp, so the entry must report
        # the missing product as a failure, never claim a
        # completion the later stages would refuse anyway.
        env = _Env(self, Path(self._tmp) / "s")
        cfg = dict(_CONFIG, only_chunks="3")
        with env, self._patches(cfg):
            rc, out = self._run_quiet(pp.cmd_stage_extract, self.epub)
            self.assertEqual(rc, 1)
            self.assertIn("no complete extraction stamp", out)
            self.assertIsNone(pp._load_extraction_stamp(_BOOK))

    def test_run_history_row_names_the_stage(self):
        env = _Env(self, Path(self._tmp) / "s")
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

        cfg = dict(_CONFIG, run_history=True,
                   supabase_url="https://fake.invalid",
                   supabase_key="fake-key")
        with env, self._patches(cfg), \
                mock.patch.object(pp, "sb", _fake_sb), \
                mock.patch.object(pp, "_load_rel_registry",
                                  lambda *a, **k: None):
            rc, _out = self._run_quiet(pp.cmd_stage_extract, self.epub)
        self.assertEqual(rc, 0)
        run_posts = [d for (t, m, d) in posts
                     if t == "processor_runs" and m == "POST"]
        # Exactly ONE row, and it names the stage in the existing
        # mode field — no second row from process_file_v2's own
        # surroundings, no schema change.
        self.assertEqual(len(run_posts), 1)
        self.assertEqual(run_posts[0][0]["mode"], "stage_extract")
        book_posts = [d for (t, m, d) in posts
                      if t == "processor_run_books" and m == "POST"]
        self.assertEqual(len(book_posts), 1)
        self.assertEqual(book_posts[0][0]["outcome"], "processed")

    def test_missing_file_refuses(self):
        env = _Env(self, Path(self._tmp) / "s")
        with env, self._patches():
            rc, out = self._run_quiet(
                pp.cmd_stage_extract, Path(self._tmp) / "nope.epub")
            self.assertEqual(rc, 1)
            self.assertIn("file not found", out)

    def test_unsupported_suffix_refuses(self):
        env = _Env(self, Path(self._tmp) / "s")
        txt = Path(self._tmp) / "book.txt"
        txt.write_text("not an ebook", encoding="utf-8")
        with env, self._patches():
            rc, out = self._run_quiet(pp.cmd_stage_extract, txt)
            self.assertEqual(rc, 1)
            self.assertIn("unsupported format", out)

    def test_v1_pipeline_refuses(self):
        # The stage split covers the v2 pipeline only; a v1-shaped
        # invocation (process_file's world) refuses by name rather
        # than running the v1 extractor under a stage label.
        env = _Env(self, Path(self._tmp) / "s")
        cfg = dict(_CONFIG, pipeline="v1")
        with env, self._patches(cfg):
            rc, out = self._run_quiet(pp.cmd_stage_extract, self.epub)
            self.assertEqual(rc, 1)
            self.assertIn("v2", out)
            self.assertFalse(env.ckpt_dir.exists())

    def test_unmarked_run_still_merges_and_decides(self):
        # The seam must be invisible to ordinary runs: without the
        # stage marker, process_file_v2 runs its full length
        # (merge artefact written beside the stamp, decide segment
        # reached — the dry-run drive stops before the checkpoint
        # clear, exactly as the step-1 seam tests drive it).
        env = _Env(self, Path(self._tmp) / "s")
        with env, self._patches():
            rc, _out = self._run_quiet(
                pp.process_file_v2, self.epub, dry_run=True)
            self.assertTrue(rc)
            self.assertTrue(pp._extraction_complete(_BOOK))
            self.assertTrue(
                (env.ckpt_dir / "merged-roster.json").exists())
            self.assertEqual(len(env.judge.packets), 2)
            self.assertTrue(env.judge.rel_calls)


# ---------------------------------------------------------------------------
# The orchestrator contract: monolith == staged sequence
# ---------------------------------------------------------------------------

# The fixture book's inline gold labels: the two claims the
# extraction plants are the must-ship set; precision/recall are
# computed against them inline (the frozen gold packs cover real
# books, not this fixture, and the suite has no external-helper
# import pattern — the brief's fallback). Endpoints are named in
# the forms the pipeline ships them: Call A's evidence grounding
# rewrites the extracted "Xaden Riorson" endpoint to the grounded
# mention "Xaden" (the form the chapter text attests), in both
# drivers identically.
_MUST_SHIP = {("Fen Riorson", "Xaden", "parent"),
              ("Ann Lee", "Xaden", "friend")}
_SYMMETRIC = {"friend"}


def _pack_score(preview):
    shipped = {(r["from"], r["to"], r["type"])
               for r in preview["relationships"]
               if r.get("validation_state") == "validated"}
    found = set()
    for (a, b, t) in _MUST_SHIP:
        if (a, b, t) in shipped or \
                (t in _SYMMETRIC and (b, a, t) in shipped):
            found.add((a, b, t))
    labelled = shipped & (_MUST_SHIP |
                          {(b, a, t) for (a, b, t) in _MUST_SHIP
                           if t in _SYMMETRIC})
    precision = len(found) / len(labelled) if labelled else 1.0
    recall = len(found) / len(_MUST_SHIP)
    return precision, recall


def _validated_set(preview):
    return {(r["from"], r["to"], r["type"],
             r.get("evidence_source"), r.get("evidence_verified"),
             r.get("validation_state"), r.get("validation_p"))
            for r in preview["relationships"]}


def _strip_elapsed(node):
    if isinstance(node, dict):
        return {k: _strip_elapsed(v) for k, v in node.items()
                if k != "elapsed_sec"}
    if isinstance(node, list):
        return [_strip_elapsed(v) for v in node]
    return node


class TestOrchestratorEquivalence(_Stage4Case):
    def _run_staged(self):
        env = _Env(self, Path(self._tmp) / "staged")
        with env, self._patches():
            rc, out = self._run_quiet(pp.cmd_stage_extract, self.epub)
            self.assertEqual(rc, 0, out)
            rc, out = self._run_quiet(pp.cmd_stage_merge, self.epub)
            self.assertEqual(rc, 0, out)
            art = json.loads((env.ckpt_dir / "merged-roster.json")
                             .read_text(encoding="utf-8"))
            rc, out = self._run_quiet(pp.cmd_stage_decide, self.epub,
                                      preview=True)
            self.assertEqual(rc, 0, out)
            return art, env.preview(), env

    def _run_monolith(self):
        env = _Env(self, Path(self._tmp) / "mono")
        captured = {}
        real_save = pp.save_preview

        def _save_and_snapshot(fpath, result):
            # The monolith clears its checkpoints at the end of a
            # finished run — capture the merge artefact at the
            # preview seam, the last point it exists.
            art_path = env.ckpt_dir / "merged-roster.json"
            if art_path.exists():
                captured["art"] = json.loads(
                    art_path.read_text(encoding="utf-8"))
            return real_save(fpath, result)

        with env, self._patches(), \
                mock.patch.object(pp, "save_preview",
                                  _save_and_snapshot):
            # run_once's surroundings, mirrored: a real full run is
            # armed by _history_begin (which is also the seam that
            # attempts the relationship-registry load in both
            # driver shapes — the stage entries arm it the same
            # way). Driving process_file_v2 bare would leave the
            # registry 'not-loaded' in one record and attempted in
            # the other, a difference of the test bench, not of
            # the drivers.
            pp._history_begin("scan", preview=True, files_seen=1)
            rc, out = self._run_quiet(pp.process_file_v2, self.epub,
                                      preview=True)
            pp._history_finish(failures=0 if rc else 1)
            self.assertTrue(rc, out)
            self.assertIn("art", captured)
            return captured["art"], env.preview(), env

    def test_monolith_equals_staged_sequence(self):
        art_m, prev_m, env_m = self._run_monolith()
        art_s, prev_s, env_s = self._run_staged()

        # The merge artefact: canonical roster, verdict table and
        # packets identical — the write timestamp is the only
        # field allowed to differ (it records when, not what).
        a_m, a_s = dict(art_m), dict(art_s)
        a_m.pop("written_at")
        a_s.pop("written_at")
        self.assertEqual(a_m, a_s)
        # Non-vacuous: the jev pass really judged and folded.
        verdicts = art_s["merge_report"]["verdicts"]
        self.assertEqual(
            {(v["a"], v["b"], v["verdict"]) for v in verdicts},
            {("Fen Riorson", "Xaden Riorson", "no"),
             ("Xaden", "Xaden Riorson", "yes")})
        yes = [v for v in verdicts if v["verdict"] == "yes"]
        self.assertEqual(yes[0]["confidence"], 0.93)
        self.assertIn("HOW TO READ THIS BRIEF", yes[0]["packet"])
        self.assertEqual(sorted(art_s["roster"]),
                         ["ann lee", "fen riorson", "xaden riorson"])
        # The judge was asked the same questions in both drivers.
        self.assertEqual(env_m.judge.packets, env_s.judge.packets)
        self.assertEqual(env_m.judge.rel_calls, env_s.judge.rel_calls)

        # The validated claim set and the pack score agree.
        self.assertEqual(_validated_set(prev_m),
                         _validated_set(prev_s))
        self.assertEqual(_pack_score(prev_m), (1.0, 1.0))
        self.assertEqual(_pack_score(prev_m), _pack_score(prev_s))

        # The book record, field for field, modulo ONLY the
        # stamped divergences — handled explicitly, one by one:
        rec_m, rec_s = copy.deepcopy(prev_m), copy.deepcopy(prev_s)
        tel_m = _strip_elapsed(rec_m.pop("telemetry"))
        tel_s = _strip_elapsed(rec_s.pop("telemetry"))
        # (a) decide_report.budget.basis names the driver shape.
        # The chapter count is equal; the POOL is not, and the
        # difference is the basis made visible: in a shared run
        # Stage M's jev pass tops the one pool up by the pairs it
        # judges (and spends them back), so the pool the decide
        # segment inherits — and the reduce's grow-only resize
        # preserves — carries that top-up; per-stage, the top-up
        # happens in Stage M's own pool and cancels there, exactly
        # as _dm_budget_jev_merge_topup promises, and Stage D
        # opens at the provisional pool. Both stamps truthfully
        # record the pool their decide actually ran under.
        bud_m = rec_m["decide_report"].pop("budget")
        bud_s = rec_s["decide_report"].pop("budget")
        self.assertEqual(bud_m.pop("basis"), "shared-run")
        self.assertEqual(bud_s.pop("basis"), "per-stage")
        self.assertEqual(bud_m.pop("chapters"),
                         bud_s.pop("chapters"))
        self.assertEqual(
            bud_m.pop("pool") - bud_s.pop("pool"),
            tel_m["decision"]["merge"]["calls"])
        self.assertEqual(bud_m, bud_s)
        # The record's own decision_pool stamp is the same pool
        # number, carried at the record's top level — it differs
        # by the same top-up, for the same reason.
        self.assertEqual(
            rec_m.pop("decision_pool") - rec_s.pop("decision_pool"),
            tel_m["decision"]["merge"]["calls"])
        # (b) a_fallback: the staged decide stamps 0 (the value is
        # a Call A return, never persisted); this fixture's live
        # count is also 0, so the exemption hides no drift here.
        self.assertEqual(rec_m["tasks"].pop("identity_simple"), 0)
        self.assertEqual(rec_s["tasks"].pop("identity_simple"), 0)
        self.assertEqual(rec_m, rec_s)
        # (c) telemetry is per-invocation accounting: Stage D's own
        # decision calls (relationship / trigger / quote) match
        # the monolith's exactly; the extraction counters and the
        # merge-stage calls belong to the invocations that spent
        # them — the monolith's preview carries them, the staged
        # decide's carries the fresh zeroed shape.
        for stage in ("relationship", "trigger", "quote"):
            self.assertEqual(tel_m["decision"][stage],
                             tel_s["decision"][stage], stage)
        self.assertEqual(tel_m["decision"]["merge"]["calls"], 2)
        self.assertEqual(tel_s["decision"]["merge"]["calls"], 0)
        self.assertEqual(tel_m["call_a"]["requests"], 3)
        self.assertEqual(tel_m["call_b"]["requests"], 3)
        self.assertEqual(tel_s["call_a"],
                         {"requests": 0, "prompt_tokens": None,
                          "prompt_chars_estimate": None,
                          "prompt_section_est": None,
                          "completion_tokens": None})
        self.assertEqual(tel_s["call_b"], tel_s["call_a"])


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

class TestStageExtractCli(unittest.TestCase):
    def setUp(self):
        self._old_learned = pp._LEARNED
        self.addCleanup(lambda: setattr(pp, "_LEARNED",
                                        self._old_learned))

    def test_stage_extract_with_file_and_chunks_is_coherent(self):
        # --chunks shapes the extraction itself, so beside --stage
        # extract it is a served combination (the stamp records it
        # and the downstream stages refuse the partial book), not
        # a silently dropped run-mode flag.
        parser = pp._build_parser()
        defaults = parser.parse_args([])
        args = parser.parse_args(
            ["--stage", "extract", "--file", "book.epub",
             "--chunks", "2"])
        self.assertIsNone(pp._flag_conflicts(args, defaults))

    def test_stage_extract_with_preview_still_conflicts(self):
        # Stage E has no preview posture — its product is the
        # artefact set — so --preview beside it would be dropped,
        # and the conflict check must keep saying so.
        parser = pp._build_parser()
        defaults = parser.parse_args([])
        args = parser.parse_args(
            ["--stage", "extract", "--file", "book.epub",
             "--preview"])
        self.assertIsNotNone(pp._flag_conflicts(args, defaults))

    def test_stage_extract_needs_a_file(self):
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


if __name__ == "__main__":
    unittest.main()
