"""A completed ordinary run must leave its extraction behind
(stage-split step 4, 2026-10-22).

The failure this pins: a full run wrote the Stage-E stamp into the
checkpoint directory and then cleared that directory at the end of
the run, so the extraction behind the newest preview was gone the
moment the preview existed. Re-deciding it (--stage analyze) then
required a whole re-extraction — thirty-odd minutes of model calls —
even though the roster, the only part a re-run cannot rebuild, was
sitting in memory at the moment it was thrown away.

The contract now: a non-aborted ordinary run writes the same
Stage-E artefact `--stage extract` writes, under the learn dir
where the checkpoint lifecycle cannot reach it; a failure to write
is named and does not fail the run; and a run whose payloads were
SERVED from an artefact (analyze, replay) writes no new one.

Hermetic: acquisition and both extraction calls are doubles.
Stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

ISBN = "9780306406157"
CH1 = ("Fen Riorson was Xaden's father, and the rebellion died with "
       "him. Xaden Riorson bowed his head in the courtyard while the "
       "wardens watched from the wall above them all.")
CH2 = ("Xaden Riorson trained at dawn while Fen Riorson watched from "
       "the wall. Ann Lee timed the passes and called the count aloud "
       "for each form the blade described.")
CH3 = ("The rebellion embers glowed in the grate. Xaden Riorson slept "
       "at last, and the house stood quiet over the valley until the "
       "morning bells rang across the water.")
CHAPTERS = [{"index": i, "label": f"Ch {i}", "text": t}
            for i, t in enumerate((CH1, CH2, CH3), 1)]

_CALL_A = {
    "characters": [
        {"name": "Xaden Riorson", "aliases": [], "role": "protagonist",
         "description": "", "evidence": ""},
        {"name": "Fen Riorson", "aliases": [], "role": "supporting",
         "description": "", "evidence": ""}],
    # One claim typed with a word the vocabulary does not carry: the
    # reduce records it as a relationship-type proposal, which is what
    # puts rel-types.json into the checkpoint review bundle.
    "relationships": [{"from": "Ann Lee", "to": "Fen Riorson",
                       "type": "nemesis", "evidence": ""}],
    "task_status": {"identity": "ok"},
}
_CALL_B = {"summary": "Something happens.", "spice_level": 0,
           "triggers": {c: "none" for c in pp.TRIGGER_CATEGORIES},
           "trigger_evidence": {}, "trope_candidates": [], "quotes": []}

_CONFIG = {
    "pipeline": "v2", "llm": "ollama", "preview": True,
    "decision_model": False, "evidence_retrieval": False,
    "use_corpus": False, "staging_gate": False, "corpus_quotes": False,
    "dedupe": False, "principals_only": True, "min_frequency": 0.20,
    "persist_roster": False, "emit_uncertainty": False,
    "show_matrices": False, "series_priors": False, "hardcover": False,
    "discovery_shadow": False, "supabase_url": None, "supabase_key": None,
    "run_history": False, "emit_run_events": False, "only_chunks": None,
    "jobs": 1, "jev_merge": False, "roster_reconcile": True,
    "stage": "all", "v2_prompt_cache": False,
}


class TestOrdinaryRunPreservesExtraction(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self._tmp, ignore_errors=True))
        self._orig_script_dir = pp.SCRIPT_DIR
        pp.SCRIPT_DIR = self._tmp
        self.addCleanup(
            lambda: setattr(pp, "SCRIPT_DIR", self._orig_script_dir))
        self._orig_learned = pp._LEARNED
        pp._LEARNED = pp.LearnedState(
            learn_dir=str(self._tmp / "learned"), enabled=True)
        self.addCleanup(
            lambda: setattr(pp, "_LEARNED", self._orig_learned))
        for attr, val in (("_HISTORY", None), ("_RUN_SINK", None),
                          ("_DECISION_VALIDATOR", None),
                          ("_EXTRACTION_CTX", None),
                          ("_REPLAY_CTX", None)):
            self.addCleanup(setattr, pp, attr, getattr(pp, attr))
            setattr(pp, attr, val)
        self.addCleanup(pp._dm_budget_reset)

    def _acq(self):
        return {"chapters": [dict(c) for c in CHAPTERS],
                "title": "Smoke Book", "author": "A. Writer",
                "identifiers": {"isbn": ISBN}, "word_count": 60,
                "chapter_detection_fallback": False,
                "source": {"kind": "file"}}

    def _run(self):
        with mock.patch.dict(pp.CONFIG, _CONFIG), \
                mock.patch.object(pp, "_acquire_chapters",
                                  return_value=self._acq()), \
                mock.patch.object(pp, "_detect_series",
                                  return_value=(None, None, "none")), \
                mock.patch.object(
                    pp, "v2_call_a",
                    lambda call, roster, ch, n:
                        (json.loads(json.dumps(_CALL_A)), False)), \
                mock.patch.object(
                    pp, "v2_call_b",
                    lambda call, ch, n: json.loads(json.dumps(_CALL_B))), \
                mock.patch.object(pp, "embed_vectors",
                                  lambda *a, **k: []):
            return pp.process_file_v2(self._tmp / "book.epub",
                                      dry_run=False, preview=True)

    def _artefacts(self):
        return sorted((self._tmp / "learned" / "artefacts"
                       / "extract").rglob("manifest.json"))

    def test_completed_run_writes_the_stage_e_artefact(self):
        self.assertTrue(self._run())
        hits = self._artefacts()
        self.assertEqual(len(hits), 1, "expected exactly one artefact")
        root = hits[0].parent
        manifest = json.loads(hits[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["stage"], "extract")
        self.assertEqual(manifest["chapter_count"], len(CHAPTERS))
        self.assertEqual(manifest["payload_chapters"], len(CHAPTERS))
        self.assertEqual(manifest["fingerprint"][:32],
                         manifest["fingerprint"])
        for i in range(1, len(CHAPTERS) + 1):
            self.assertTrue((root / f"chapter_{i}.json").is_file(), i)
        self.assertTrue((root / "roster.json").is_file())
        self.assertTrue((root / "meta.json").is_file())

    def test_artefact_lives_outside_the_kept_checkpoints(self):
        # A finished run keeps its checkpoint directory (it is the
        # review bundle); the artefact must not live inside it, so the
        # one command that does purge checkpoints cannot take the
        # extraction with it.
        self.assertTrue(self._run())
        book_key = pp._checkpoint_book_key(self._tmp / "book.epub")
        ckpt = pp._checkpoint_dir(book_key)
        self.assertTrue(ckpt.is_dir(),
                        "the ordinary run keeps its review bundle")
        hits = self._artefacts()
        self.assertEqual(len(hits), 1)
        self.assertNotIn(str(ckpt), str(hits[0]))
        # The kept directory is the run's review bundle: the reduce's
        # analysis document (and the rel-types review when the run
        # proposed types) land beside the chapter payloads.
        for name in ("meta.json", "roster.json", "extraction.json",
                     "analysis.json"):
            self.assertTrue((ckpt / name).is_file(), name)
        self.assertTrue((ckpt / "chapter_1.json").is_file())
        rel_types = ckpt / "rel-types.json"
        self.assertTrue(rel_types.is_file())
        self.assertIn("nemesis", rel_types.read_text(encoding="utf-8"))

    def test_artefact_write_failure_does_not_fail_the_run(self):
        with mock.patch.object(
                pp, "_write_extraction_artefact",
                side_effect=OSError("disk full")):
            self.assertTrue(self._run(),
                            "a bookkeeping failure must not fail the run")
        self.assertEqual(self._artefacts(), [])

    def test_analyse_does_not_mint_a_second_artefact(self):
        # Payloads served from an extraction artefact are that
        # extraction; re-writing them under a new run key would
        # present the source extraction as a new one.
        self.assertTrue(self._run())
        hits = self._artefacts()
        self.assertEqual(len(hits), 1)
        pp._EXTRACTION_CTX = {
            "payload_dir": str(hits[0].parent),
            "manifest": json.loads(hits[0].read_text(encoding="utf-8")),
            "extract_key": hits[0].parent.name,
            "fingerprint": json.loads(
                hits[0].read_text(encoding="utf-8"))["fingerprint"],
        }
        self.assertTrue(self._run())
        self.assertEqual(len(self._artefacts()), 1)
        # And _save_merged_roster_artefact must not write merged-roster.json
        # into the immutable extraction artefact directory when _EXTRACTION_CTX
        # is active.
        self.assertFalse(pp._save_merged_roster_artefact(
            self._tmp / "book.epub", {}, {"stage": "merge"}))
        self.assertFalse((hits[0].parent / "merged-roster.json").exists())


if __name__ == "__main__":
    unittest.main()
