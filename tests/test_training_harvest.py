"""Tests for training_harvest.py, the standalone training-data
harvester (spec: training-data-collection-spec-2026-10-18.md).

Hermetic throughout: synthetic preview artefacts, a synthetic
corpus.db and state.db in temp dirs, and no network — the gold
Supabase path is exercised only in its no-config form here, because
its contract in that case (empty export, gap recorded, nothing
fabricated) is the part that must never regress.

The reconciliation tests pin the harvester's central promise: the
judge-pair counts account for every validation record in the
source artefact across all dispositions, and the exclusions the
spec names (formula trope confidences, heuristic trigger
confidences, fallback trigger evidence) are refused as labels and
counted, never silently dropped.

Stdlib unittest only. Run: python -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


harvest = _load("training_harvest_under_test", "training_harvest.py")

CHAPTER_TEXT = ("Alice met Bob at the gate. Bob smiled at her. "
                "'This is my sister, Carol,' Bob said. Carol waved. "
                "The sun set behind the hills.")
SISTER_QUOTE = "'This is my sister, Carol,' Bob said."
CHAPTER_LABEL = "Chapter 1:// Start"


def _preview():
    """A minimal preview artefact with one of everything the
    harvester must handle: a judged keep, a downgrade, a
    budget-exhausted ship, a judged drop, a no-evidence drop,
    trigger evidence across three gates, formula trope
    confidences, and a merge judge record."""
    return {
        "title": "Daemon",
        "author": "Daniel Suarez",
        "work_id": "w-1",
        "relationships": [
            {"from": "Alice", "to": "Carol", "type": "sibling",
             "evidence": SISTER_QUOTE, "evidence_verified": True,
             "chapter": CHAPTER_LABEL, "source": "extracted",
             "evidence_grade": "plain",
             "endpoint_provenance": {
                 "from": {"endpoint": "Alice",
                          "matched_form": "unresolved",
                          "mention_text": None, "basis": None},
                 "to": {"endpoint": "Carol",
                        "matched_form": "unresolved",
                        "mention_text": None, "basis": None}},
             "validation_p": 0.97,
             "validation_model": "typesafe/jev-1.13",
             "validation_note": "kept",
             "validation_state": "validated"},
            {"from": "Dan", "to": "Erin", "type": "other",
             "original_type": "child",
             "evidence": "some quote", "chapter": CHAPTER_LABEL,
             "validation_p": 0.5, "validation_model": "typesafe/jev-1.13",
             "validation_note": "kept", "validation_state": "validated"},
            {"from": "Bob", "to": "Alice", "type": "friend",
             "evidence": "another quote", "chapter": CHAPTER_LABEL,
             "validation_note": "budget_exhausted",
             "validation_state": "budget_exhausted"},
        ],
        "dropped_relationships": [
            {"from": "Zed", "to": "Alice", "type": "spouse",
             "evidence": "a thin quote", "chapter": CHAPTER_LABEL,
             "validation_p": 0.1, "validation_model": "typesafe/jev-1.13",
             "validation_note": "dropped",
             "validation_state": "rejected"},
            {"from": "Yan", "to": "Bob", "type": "parent",
             "evidence": "", "chapter": CHAPTER_LABEL,
             "validation_p": None,
             "validation_note": "no_evidence",
             "validation_state": "rejected"},
        ],
        "triggers": [
            {"warning": "graphic_violence", "confidence": 0.85,
             "evidence_verified": True,
             "evidence": [
                 {"quote": "A judged quote.", "chapter": CHAPTER_LABEL,
                  "decision_p": 0.9, "decision_gate": "decision"},
                 {"quote": "A fallback quote.", "chapter": CHAPTER_LABEL,
                  "decision_p": None, "decision_gate": "fallback"},
                 {"quote": "An ungated quote.", "chapter": CHAPTER_LABEL,
                  "decision_p": None, "decision_gate": "none"},
             ]},
        ],
        "trope_confidence": {"spies": 0.75, "heist": 0.85},
        "possible_merges": [
            {"name_a": "Al", "name_b": "Alice", "p_yes": 0.3,
             "reason": "below threshold (0.85)"},
        ],
        "chapter_summaries": [
            {"index": 1, "label": CHAPTER_LABEL, "summary": "s1"},
            {"index": 2, "label": "Chapter 2:// Next", "summary": "s2"},
        ],
        "chapter_data": {
            "chapters": {
                "1": {"a": {"characters": [
                                {"name": "Alice", "aliases": ["Al"],
                                 "role": "protagonist",
                                 "description": "Lead.",
                                 "status": "alive",
                                 "evidence": "Alice met Bob."},
                                {"name": "Bob", "aliases": [],
                                 "role": "supporting", "status": "alive",
                                 "evidence": ""}],
                            "relationships": [
                                {"from": "Alice", "to": "Carol",
                                 "type": "sibling",
                                 "evidence": SISTER_QUOTE},
                                {"from": "Alice", "to": "Zed",
                                 "type": "friend", "evidence": "x"}],
                            "pov_character": "Alice"},
                      "b": {"summary": "s1"}},
                "2": {"a": {"characters": [
                                {"name": "Carol", "aliases": [],
                                 "role": "supporting", "status": "alive",
                                 "evidence": "Carol waved."}],
                            "relationships": []},
                      "b": {"summary": "s2"}},
            },
            "roster": {"alice": {"name": "Alice"},
                       "bob": {"name": "Bob"},
                       "carol": {"name": "Carol"}},
        },
    }


def _make_corpus(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE corpus_works (fingerprint TEXT "
                 "PRIMARY KEY, book_key TEXT, work_id TEXT, "
                 "title TEXT, author TEXT)")
    conn.execute("CREATE TABLE corpus_chapters (fingerprint TEXT, "
                 "chapter_index INTEGER, label TEXT, text TEXT)")
    conn.execute("INSERT INTO corpus_works VALUES "
                 "('fp1', 'bk', 'w-1', 'Daemon', 'Daniel Suarez')")
    conn.execute("INSERT INTO corpus_chapters VALUES "
                 "('fp1', 1, ?, ?)", (CHAPTER_LABEL, CHAPTER_TEXT))
    conn.execute("INSERT INTO corpus_chapters VALUES "
                 "('fp1', 2, 'Chapter 2:// Next', 'Carol waved.')")
    conn.commit()
    conn.close()


def _run_main(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = harvest.main(argv)
    return rc, buf.getvalue()


class WindowMirrorTests(unittest.TestCase):
    def test_window_is_two_sentences_each_side(self):
        w = harvest._evidence_window(SISTER_QUOTE, CHAPTER_TEXT)
        self.assertIn("Alice met Bob at the gate.", w)
        self.assertIn("Bob smiled at her.", w)
        self.assertIn("Carol waved.", w)
        self.assertIn("The sun set behind the hills.", w)
        self.assertNotIn("my sister", w)

    def test_window_empty_when_quote_not_located(self):
        self.assertEqual(
            harvest._evidence_window("not in the text", CHAPTER_TEXT),
            "")

    def test_resolution_line_only_for_unique_resolution(self):
        prov = {"from": {"endpoint": "Carol",
                         "matched_form": "resolved-surname",
                         "mention_text": "Carol",
                         "basis": {"kinds": ["focal_character"]}},
                "to": {"endpoint": "Bob",
                       "matched_form": "unresolved",
                       "mention_text": None, "basis": None}}
        lines = harvest._resolution_context_lines(prov)
        self.assertEqual(len(lines), 1)
        self.assertIn("the scene follows them", lines[0])


class DispositionTests(unittest.TestCase):
    def test_signals(self):
        self.assertEqual(harvest._disposition(
            {"type": "friend", "validation_note": "kept"}, True),
            "shipped")
        self.assertEqual(harvest._disposition(
            {"type": "other", "validation_note": "downgraded"}, True),
            "downgraded")
        self.assertEqual(harvest._disposition(
            {"type": "other", "original_type": "child",
             "validation_note": "kept"}, True), "downgraded")
        self.assertEqual(harvest._disposition(
            {"type": "spouse", "validation_note": "dropped"}, False),
            "dropped")


class HarvestFixtureBase(unittest.TestCase):
    """A temp world: preview dir with the synthetic artefact (and a
    non-artefact JSON), a run-events dir, a corpus, an empty learn
    dir, and a store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.preview_dir = root / "preview"
        self.preview_dir.mkdir()
        self.artefact_path = self.preview_dir / "Daemon - Daniel Suarez.json"
        self.artefact_bytes = json.dumps(_preview()).encode("utf-8")
        self.artefact_path.write_bytes(self.artefact_bytes)
        (self.preview_dir / "stageabort.json").write_text(
            json.dumps({"aborted": True}), encoding="utf-8")
        self.events_dir = root / "run-events"
        self.events_dir.mkdir()
        (self.events_dir / "run-20260101-000000-1.jsonl").write_text(
            '{"event": "run_start"}\n', encoding="utf-8")
        self.learn_dir = root / "learned"
        self.learn_dir.mkdir()
        _make_corpus(self.learn_dir / "corpus.db")
        self.store = root / "store"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        return ["--store", str(self.store),
                "--preview-dir", str(self.preview_dir),
                "--run-events-dir", str(self.events_dir)] + list(args)

    def dataset_records(self, dataset):
        files = sorted((self.store / "datasets" / dataset).glob("*.jsonl"))
        self.assertTrue(files, f"no export for {dataset}")
        return [json.loads(line) for line in
                files[-1].read_text(encoding="utf-8").splitlines()
                if line.strip()]


class ArchiveTests(HarvestFixtureBase):
    def test_archive_is_idempotent_and_never_touches_sources(self):
        rc, out = _run_main(["archive"] + self.cli())
        self.assertEqual(rc, 0)
        self.assertIn("1 preview(s) newly archived", out)
        archived = list((self.store / "raw-previews").rglob("*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), self.artefact_bytes)
        rc, out = _run_main(["archive"] + self.cli())
        self.assertEqual(rc, 0)
        self.assertIn("0 preview(s) newly archived", out)
        self.assertIn("1 already archived", out)
        self.assertEqual(len(list(
            (self.store / "raw-previews").rglob("*.json"))), 1)
        self.assertEqual(len(list(
            (self.store / "raw-run-events").glob("*.jsonl"))), 1)
        # Sources untouched, byte for byte.
        self.assertEqual(self.artefact_path.read_bytes(),
                         self.artefact_bytes)


class JudgePairsTests(HarvestFixtureBase):
    def test_counts_dispositions_and_exclusions(self):
        rc, _ = _run_main(["judge-pairs"] + self.cli(
            "--learn-dir", str(self.learn_dir)))
        self.assertEqual(rc, 0)
        recs = self.dataset_records("judge-pairs")
        rels = [r for r in recs if r["claim"]["axis"] == "relationships"]
        trigs = [r for r in recs if r["claim"]["axis"] == "triggers"]
        # Every validation record in the artefact is accounted for.
        self.assertEqual(len(rels), 5)
        self.assertEqual(len(trigs), 1)
        disp = {}
        for r in rels:
            disp[r["disposition"]] = disp.get(r["disposition"], 0) + 1
        self.assertEqual(disp, {"shipped": 2, "downgraded": 1,
                                "dropped": 2})
        draws = [r for r in rels if r["judged"]]
        self.assertEqual(len(draws), 3)
        no_draw = [r for r in rels if not r["judged"]]
        self.assertEqual(len(no_draw), 2)
        for r in no_draw:
            self.assertIsNone(r["validation"]["p"])
        for r in recs:
            self.assertIsNone(r["reason"])
            self.assertEqual(r["meta"]["split"], "holdout")
        # The judged keep carries the reconstructed window.
        keep = [r for r in rels
                if r["validation"]["state"] == "validated"
                and r["disposition"] == "shipped"][0]
        self.assertIn("Alice met Bob at the gate.",
                      keep["evidence"]["window"])
        self.assertEqual(keep["validation"]["p"], 0.97)
        self.assertEqual(keep["meta"]["judge_model"],
                         "typesafe/jev-1.13")
        # The trigger pair is the gate's decision, not the heuristic.
        self.assertEqual(trigs[0]["validation"]["p"], 0.9)
        self.assertEqual(trigs[0]["claim"]["category"],
                         "graphic_violence")
        # Exclusions were counted in the manifest export entry.
        manifest = json.loads(
            (self.store / "manifest.json").read_text(encoding="utf-8"))
        counters = manifest["exports"][-1]["counters"]
        self.assertEqual(
            counters["excluded_trope_formula_confidences"], 2)
        self.assertEqual(
            counters["excluded_trigger_heuristic_confidences"], 1)
        self.assertEqual(
            counters["excluded_trigger_fallback_evidence"], 1)
        self.assertEqual(
            counters["excluded_trigger_unjudged_evidence"],
            {"none": 1})
        self.assertEqual(counters["merge_judge_records_out_of_scope"], 1)


class ExtractionPairsTests(HarvestFixtureBase):
    def test_replay_join_and_fidelity(self):
        rc, _ = _run_main(["extraction-pairs"] + self.cli(
            "--learn-dir", str(self.learn_dir)))
        self.assertEqual(rc, 0)
        recs = self.dataset_records("extraction-pairs")
        self.assertEqual(len(recs), 2)
        first, second = recs
        self.assertEqual(first["meta"]["fidelity"], "reconstructed")
        self.assertEqual(first["input"]["roster_size"], 0)
        self.assertEqual(first["input"]["chapter_text"], CHAPTER_TEXT)
        self.assertEqual(first["input"]["chapter_text_source"],
                         "corpus")
        # Chapter 2 sees the roster chapter 1 built.
        self.assertEqual(second["input"]["roster_size"], 2)
        names = {e["name"] for e in second["input"]["roster_state"]}
        self.assertEqual(names, {"Alice", "Bob"})
        join = first["validation_join"]
        self.assertEqual(join["joined"], 1)
        self.assertEqual(join["unjoined"], 1)
        joined = [j for j in join["relationships"]
                  if j["outcome"] is not None][0]
        self.assertEqual(joined["outcome"]["validation_p"], 0.97)
        self.assertEqual(joined["outcome"]["disposition"], "shipped")
        manifest = json.loads(
            (self.store / "manifest.json").read_text(encoding="utf-8"))
        per_book = manifest["exports"][-1]["counters"]["per_book"]
        self.assertEqual(
            per_book["daemon-daniel-suarez"]["stored_roster_size"], 3)


class GoldTests(HarvestFixtureBase):
    def test_empty_gold_is_valid_and_records_gaps(self):
        rc, out = _run_main(["gold"] + self.cli(
            "--learn-dir", str(self.learn_dir),
            "--config", str(Path(self.tmp.name) / "no-config.json")))
        self.assertEqual(rc, 0)
        self.assertIn("gold: 0 record(s)", out)
        recs = self.dataset_records("gold")
        self.assertEqual(recs, [])
        manifest = json.loads(
            (self.store / "manifest.json").read_text(encoding="utf-8"))
        gaps = manifest["exports"][-1]["counters"]["gaps"]
        self.assertTrue(any("state.db" in g for g in gaps))
        self.assertTrue(any("supabase" in g for g in gaps))

    def test_labelled_uncertainty_packets_are_harvested(self):
        conn = sqlite3.connect(str(self.learn_dir / "state.db"))
        conn.execute("CREATE TABLE uncertainty_queue ("
                     "id INTEGER PRIMARY KEY, created TEXT, "
                     "run_ref TEXT, work_key TEXT, class TEXT, "
                     "subject TEXT, packet TEXT, status TEXT, "
                     "label TEXT, labelled_by TEXT, labelled_at TEXT)")
        packet = {"claim": "X is the sibling of Y",
                  "evidence": ["a quote"]}
        conn.execute("INSERT INTO uncertainty_queue VALUES "
                     "(1, '2026-01-01', 'run-1', 'wk', 'rel', 'X/Y', "
                     "?, 'labelled', 'keep', 'the User', '2026-01-02')",
                     (json.dumps(packet),))
        conn.execute("INSERT INTO uncertainty_queue VALUES "
                     "(2, '2026-01-01', 'run-1', 'wk', 'rel', 'A/B', "
                     "'{}', 'pending', NULL, NULL, NULL)")
        conn.commit()
        conn.close()
        rc, _ = _run_main(["gold"] + self.cli(
            "--learn-dir", str(self.learn_dir),
            "--config", str(Path(self.tmp.name) / "no-config.json")))
        self.assertEqual(rc, 0)
        recs = self.dataset_records("gold")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["verdict"], "keep")
        self.assertEqual(recs[0]["labeller"], "the User")
        self.assertEqual(recs[0]["decided_at"], "2026-01-02")
        self.assertEqual(recs[0]["evidence"], ["a quote"])
        self.assertIsNone(recs[0]["reason"])


class ReportTests(HarvestFixtureBase):
    def test_report_reconciles_and_carries_no_text(self):
        for cmd in ("judge-pairs", "extraction-pairs"):
            rc, _ = _run_main([cmd] + self.cli(
                "--learn-dir", str(self.learn_dir)))
            self.assertEqual(rc, 0)
        rc, out = _run_main(["report", "--store", str(self.store)])
        self.assertEqual(rc, 0)
        self.assertIn("RECONCILED", out)
        self.assertIn("Daemon", out)
        # Counts only: no chapter text, no evidence quotes.
        self.assertNotIn("sun set behind the hills", out)
        self.assertNotIn("my sister", out)


class SplitTaggingTests(HarvestFixtureBase):
    def test_unmapped_book_defaults_to_train(self):
        other = _preview()
        other["title"] = "Some Other Book"
        other["work_id"] = "w-2"
        (self.preview_dir / "Other.json").write_text(
            json.dumps(other), encoding="utf-8")
        rc, _ = _run_main(["judge-pairs"] + self.cli(
            "--learn-dir", str(self.learn_dir)))
        self.assertEqual(rc, 0)
        recs = self.dataset_records("judge-pairs")
        splits = {r["meta"]["book"]: r["meta"]["split"] for r in recs}
        self.assertEqual(splits["Daemon"], "holdout")
        self.assertEqual(splits["Some Other Book"], "train")


if __name__ == "__main__":
    unittest.main()
