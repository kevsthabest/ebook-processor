"""Tests for S0 run-event emission (PORTAL-SPEC §8 GAP-2/GAP-6, §10 S0).

With --emit-run-events, a run writes a per-run JSONL event artifact
under <learn_dir>/run-events/: every resolver pair score + verdict
(dedupe Signal 1/2 + guard blocks), every trope-impulse neighbour
set (v2 catalog mapping), and the decision-call counts as the shared
budget is claimed. The portal (S2/S4) polls this file live and
reopens it after the run; here we pin the schema, the degrade
contract (emission NEVER fails a run), and the standing path's
byte-identity (flag off => no artifact, hooked units' outputs
unchanged).

Stdlib unittest only. Embeddings are stubbed at the embed_vectors
boundary, the catalog at trope_catalog_vectors — no network.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable_run_events",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _read_events(path):
    return [json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


class _SinkCase(unittest.TestCase):
    """Base: a live sink in a temp learn dir, cleaned up after."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.learn_dir = Path(self.tmp.name) / "learned"
        self.path = pp._run_events_begin(self.learn_dir, preview=True)
        self.assertIsNotNone(self.path)

    def tearDown(self):
        pp._run_events_finish(failures=0)
        self.tmp.cleanup()

    def events(self):
        return _read_events(self.path)


class TestFlagWiring(unittest.TestCase):
    def test_flag_parses(self):
        parser = pp._build_parser()
        self.assertFalse(parser.parse_args([]).emit_run_events)
        self.assertTrue(
            parser.parse_args(["--emit-run-events"]).emit_run_events)


class TestSinkBasics(_SinkCase):
    def test_artifact_location_and_run_start(self):
        self.assertEqual(self.path.parent, self.learn_dir / "run-events")
        self.assertTrue(self.path.name.startswith("run-"))
        self.assertEqual(self.path.suffix, ".jsonl")
        first = self.events()[0]
        self.assertEqual(first["event"], "run_start")
        self.assertEqual(first["v"], 1)
        self.assertEqual(first["run_id"], self.path.stem)
        self.assertTrue(first["preview"])
        self.assertFalse(first["dry_run"])

    def test_seq_and_ts_on_every_event(self):
        pp._run_event({"event": "pair", "a": "A", "b": "B", "score": 0.5})
        pp._run_event({"event": "pair", "a": "C", "b": "D", "score": 0.6})
        evs = self.events()
        self.assertEqual([e["seq"] for e in evs], [1, 2, 3])
        self.assertTrue(all("ts" in e for e in evs))

    def test_finish_writes_run_end_and_deactivates(self):
        with pp._run_events_book(Path("Some Book.epub")):
            pass
        pp._run_events_finish(failures=2)
        self.assertFalse(pp._run_events_active())
        evs = self.events()
        self.assertEqual(evs[-1]["event"], "run_end")
        self.assertEqual(evs[-1]["failures"], 2)
        self.assertEqual(evs[-1]["books"], 1)
        # finish is idempotent: a second call appends nothing.
        pp._run_events_finish(failures=0)
        self.assertEqual(len(self.events()), len(evs))


class TestSinkDegrade(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        if pp._run_events_active():
            pp._run_events_finish()
        self.tmp.cleanup()

    def test_unwritable_location_degrades_to_no_artifact(self):
        # A regular FILE as the learn dir's parent: mkdir must fail,
        # and the failure must degrade to "no emission", never raise.
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        result = pp._run_events_begin(blocker / "learned")
        self.assertIsNone(result)
        self.assertFalse(pp._run_events_active())
        pp._run_event({"event": "pair", "a": "A", "b": "B", "score": 1.0})

    def test_mid_run_write_failure_never_raises(self):
        path = pp._run_events_begin(Path(self.tmp.name) / "learned")
        self.assertIsNotNone(path)

        class _Broken:
            def write(self, _s):
                raise OSError("disk gone")

            def flush(self):
                raise OSError("disk gone")

            def close(self):
                pass

        pp._RUN_SINK._fh = _Broken()
        # The write failure is swallowed; later events are no-ops on
        # the dead sink and finish still closes out cleanly.
        pp._run_event({"event": "pair", "a": "A", "b": "B", "score": 1.0})
        pp._run_event({"event": "pair", "a": "C", "b": "D", "score": 0.1})
        pp._run_events_finish(failures=0)
        self.assertFalse(pp._run_events_active())

    def test_emit_without_sink_is_a_noop(self):
        self.assertFalse(pp._run_events_active())
        pp._run_event({"event": "pair", "a": "A", "b": "B", "score": 1.0})
        pp._run_events_finish()


class TestBudgetEvents(_SinkCase):
    def setUp(self):
        super().setUp()
        pp._dm_budget_reset()

    def tearDown(self):
        pp._dm_budget_reset()
        super().tearDown()

    def _decision_events(self):
        return [e for e in self.events() if e["event"] == "decision"]

    def test_claims_emit_running_counts(self):
        self.assertTrue(pp._dm_budget_claim(1))
        self.assertTrue(pp._dm_budget_claim(1))
        self.assertTrue(pp._dm_budget_claim(5))
        evs = self._decision_events()
        self.assertEqual([(e["used"], e["granted"]) for e in evs],
                         [(1, True), (2, True), (7, True)])
        self.assertTrue(all(e["cap"] == pp._MERGE_TIER3_CAP for e in evs))

    def test_refused_claim_emits_granted_false(self):
        self.assertTrue(pp._dm_budget_claim(pp._MERGE_TIER3_CAP))
        self.assertFalse(pp._dm_budget_claim(1))
        last = self._decision_events()[-1]
        self.assertFalse(last["granted"])
        self.assertEqual(last["used"], pp._MERGE_TIER3_CAP)

    def test_no_events_when_sink_inactive(self):
        pp._run_events_finish()
        before = len(self.events())
        self.assertTrue(pp._dm_budget_claim(1))
        self.assertEqual(len(self.events()), before)

    def test_parallel_claims_emit_in_count_order(self):
        # Regression (Advisor F1): the claim and its decision event
        # must be atomic, or parallel books log used-counts out of
        # order and the ticker stream misreports the spend. With the
        # emit inside the budget lock, the file order of `used` is
        # the claim order: exactly 1..N.
        import threading

        def worker():
            for _ in range(10):
                pp._dm_budget_claim(1)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        evs = self._decision_events()
        self.assertEqual([e["used"] for e in evs], list(range(1, 41)))
        self.assertTrue(all(e["granted"] for e in evs))


def _chars(*specs):
    return [{"name": n, "description": d, "confidence": 0.9}
            for n, d in specs]


class TestDedupeEvents(_SinkCase):
    def _run_dedupe(self, chars, vecs):
        with patch.dict(pp.CONFIG, {"embed_url": "http://stub",
                                    "embed_model": "stub",
                                    "dedupe_threshold": 0.85}), \
                patch.object(pp, "embed_vectors", return_value=vecs):
            return pp.dedupe_characters(chars, [], [])

    def _of(self, kind):
        return [e for e in self.events() if e["event"] == kind]

    def test_signal1_verdict_event(self):
        chars = _chars(("Jon Snow", "a knight"), ("Jon Snow", "a knight"))
        # Orthogonal embeddings: Signal 2 must not add a merge; the
        # pair's verdict comes from Signal 1 alone.
        out, _r, _p, _rep = self._run_dedupe(
            chars, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(len(out), 1)
        verdicts = [e for e in self._of("verdict")
                    if e["via"] == "signal1"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "merged")
        self.assertIsNone(verdicts[0]["score"])

    def test_signal2_merged_pair_and_verdict(self):
        chars = _chars(("Jonathan Pryce", "a knight"),
                       ("Jon Pryce", "a knight"))
        vecs = [[1.0, 0.0], [0.9, math.sqrt(1 - 0.81)]]
        out, _r, _p, _rep = self._run_dedupe(chars, vecs)
        self.assertEqual(len(out), 1)
        pairs = self._of("pair")
        self.assertEqual(len(pairs), 1)
        self.assertAlmostEqual(pairs[0]["score"], 0.9, places=5)
        verdicts = [e for e in self._of("verdict")
                    if e["via"] == "signal2"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "merged")
        self.assertAlmostEqual(verdicts[0]["score"], 0.9, places=5)

    def test_signal2_split_below_bar(self):
        chars = _chars(("Jonathan Pryce", "a knight"),
                       ("Jon Pryce", "a knight"))
        vecs = [[1.0, 0.0], [0.5, math.sqrt(0.75)]]
        out, _r, _p, _rep = self._run_dedupe(chars, vecs)
        self.assertEqual(len(out), 2)
        verdicts = [e for e in self._of("verdict")
                    if e["via"] == "signal2"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "split")

    def test_guard_blocked_pair_has_verdict_but_no_score(self):
        # Distinct proper names + differing descriptions: the guard
        # blocks before any cosine exists. The artifact must carry
        # the verdict and the reason, with score null — inventing a
        # score would be theatre.
        chars = _chars(("Alice Hart", "a knight"),
                       ("Alicia Hart", "a healer"))
        vecs = [[1.0, 0.0], [0.99, math.sqrt(1 - 0.99 ** 2)]]
        out, _r, _p, _rep = self._run_dedupe(chars, vecs)
        self.assertEqual(len(out), 2)
        self.assertEqual(self._of("pair"), [])
        verdicts = [e for e in self._of("verdict") if e["via"] == "guard"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "blocked")
        self.assertIsNone(verdicts[0]["score"])
        self.assertTrue(verdicts[0]["reason"])

    def test_adversarial_names_roundtrip(self):
        # Quotes/newlines in a character name must not tear the
        # JSONL stream: every line still parses as one object and
        # the name arrives byte-exact.
        nasty = 'Jo"n\nSnow'
        chars = _chars((nasty, "a knight"), (nasty, "a knight"))
        self._run_dedupe(chars, [[1.0, 0.0], [0.0, 1.0]])
        verdicts = [e for e in self._of("verdict")
                    if e["via"] == "signal1"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["a"], nasty)
        self.assertEqual(verdicts[0]["b"], nasty)

    def test_dedupe_output_identical_with_and_without_emission(self):
        chars = _chars(("Jonathan Pryce", "a knight"),
                       ("Jon Pryce", "a knight"),
                       ("Alice Hart", "a knight"),
                       ("Alicia Hart", "a healer"))
        vecs = [[1.0, 0.0], [0.9, math.sqrt(0.19)],
                [0.0, 1.0], [0.99, math.sqrt(1 - 0.99 ** 2)]]
        on = self._run_dedupe([dict(c) for c in chars], vecs)
        pp._run_events_finish()
        off = self._run_dedupe([dict(c) for c in chars], vecs)
        self.assertEqual(json.dumps(on, sort_keys=True, default=str),
                         json.dumps(off, sort_keys=True, default=str))


class TestImpulseEvents(_SinkCase):
    # Catalog vectors are unit vectors in the plane; the query points
    # along +x, so cosines are the x-components: 0.95 / 0.80 / 0.60 /
    # 0.30 for the four entries below.
    CATALOG = [
        ("t1", "Enemies to Lovers", [0.95, math.sqrt(1 - 0.95 ** 2)]),
        ("t2", "Rivals to Lovers", [0.80, math.sqrt(1 - 0.80 ** 2)]),
        ("t3", "Forced Proximity", [0.60, math.sqrt(1 - 0.60 ** 2)]),
        ("t4", "Quest", [0.30, math.sqrt(1 - 0.30 ** 2)]),
    ]

    def _impulses(self):
        return [e for e in self.events() if e["event"] == "impulse"]

    def test_top3_neighbours_and_mapped_fate(self):
        pp._run_events_impulse("enemies to lovers dynamic", [1.0, 0.0],
                               self.CATALOG, "t1", 0.95, 0.78)
        evs = self._impulses()
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev["label"], "enemies to lovers dynamic")
        self.assertEqual([n["id"] for n in ev["neighbours"]],
                         ["t1", "t2", "t3"])
        self.assertEqual(ev["fate"], "mapped")
        self.assertEqual(ev["catalog_id"], "t1")
        self.assertAlmostEqual(ev["score"], 0.95, places=5)

    def test_unmapped_fate_below_bar(self):
        pp._run_events_impulse("some label", [1.0, 0.0],
                               self.CATALOG, "t3", 0.60, 0.78)
        ev = self._impulses()[0]
        self.assertEqual(ev["fate"], "unmapped")
        self.assertIsNone(ev["catalog_id"])
        self.assertEqual(len(ev["neighbours"]), 3)

    def test_impulse_noop_when_inactive(self):
        pp._run_events_finish()
        before = len(self.events())
        pp._run_events_impulse("x", [1.0, 0.0], self.CATALOG,
                               "t1", 0.95, 0.78)
        self.assertEqual(len(self.events()), before)


def _b(tropes=None):
    full = {c: "none" for c in pp.TRIGGER_CATEGORIES}
    return {"summary": "sum", "spice_level": 0, "triggers": full,
            "trigger_evidence": {}, "trope_candidates": tropes or [],
            "quotes": []}


class TestReduceImpulseWiring(_SinkCase):
    CATALOG = TestImpulseEvents.CATALOG

    def _reduce(self):
        chapters = [{"index": 1, "label": "Ch 1", "text": "x " * 200}]
        bs = {1: _b(tropes=["enemies to lovers dynamic", "a quest"])}
        # Candidate vectors: first maps (cos .95 to t1). The second
        # points along a third axis the planar catalog never touches,
        # so every catalog cosine is 0 — unmapped at the 0.78 bar.
        cand_vecs = [[1.0, 0.0], [0.0, 0.0, 1.0]]
        with patch.dict(pp.CONFIG, {"embed_url": "http://stub",
                                    "embed_model": "stub"}), \
                patch.object(pp, "trope_catalog_vectors",
                             return_value=self.CATALOG), \
                patch.object(pp, "embed_vectors", return_value=cand_vecs):
            return pp.v2_reduce({}, bs, {}, chapters)

    def test_reduce_emits_impulse_per_candidate(self):
        self._reduce()
        evs = [e for e in self.events() if e["event"] == "impulse"]
        self.assertEqual(len(evs), 2)
        by_label = {e["label"]: e for e in evs}
        self.assertEqual(
            by_label["enemies to lovers dynamic"]["fate"], "mapped")
        self.assertEqual(by_label["a quest"]["fate"], "unmapped")

    def test_reduce_output_identical_with_and_without_emission(self):
        on = self._reduce()
        pp._run_events_finish()
        off = self._reduce()
        self.assertEqual(json.dumps(on, sort_keys=True, default=str),
                         json.dumps(off, sort_keys=True, default=str))


class TestRunLifecycle(unittest.TestCase):
    """run_once with process_file stubbed: the artifact brackets the
    run (run_start/book_start/book_end/run_end) and the flag-off run
    leaves no trace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.book = root / "Fixture Book.epub"
        self.book.write_bytes(b"PK fixture")
        self.learn_dir = root / "learned"
        self._old_learned = pp._LEARNED
        pp._LEARNED = pp.LearnedState(learn_dir=self.learn_dir,
                                      enabled=False)
        self._old_config = dict(pp.CONFIG)

    def tearDown(self):
        pp._LEARNED = self._old_learned
        pp.CONFIG.clear()
        pp.CONFIG.update(self._old_config)
        if pp._run_events_active():
            pp._run_events_finish()
        self.tmp.cleanup()

    def _fake_process(self, f, dry_run, preview):
        pp._dm_budget_reset()
        pp._dm_budget_claim(1)
        pp._dm_budget_claim(1)
        return True

    def _run(self, emit):
        pp.CONFIG["emit_run_events"] = emit
        pp.CONFIG["jobs"] = 1
        with patch.object(pp, "process_file",
                          side_effect=self._fake_process), \
                mock.patch.object(pp.UI, "start", lambda: None), \
                mock.patch.object(pp.UI, "stop", lambda: None):
            return pp.run_once(False, True, file=str(self.book))

    def test_flagged_preview_writes_full_artifact(self):
        self.assertEqual(self._run(True), 0)
        artifacts = list((self.learn_dir / "run-events").glob("*.jsonl"))
        self.assertEqual(len(artifacts), 1)
        evs = _read_events(artifacts[0])
        kinds = [e["event"] for e in evs]
        self.assertEqual(kinds[0], "run_start")
        self.assertEqual(kinds[-1], "run_end")
        self.assertIn("book_start", kinds)
        self.assertIn("book_end", kinds)
        book_start = evs[kinds.index("book_start")]
        self.assertEqual(book_start["book"], "Fixture Book.epub")
        decisions = [e for e in evs if e["event"] == "decision"]
        self.assertEqual([e["used"] for e in decisions], [1, 2])
        book_end = evs[kinds.index("book_end")]
        self.assertEqual(book_end["decision_calls"], 2)
        self.assertEqual(evs[-1]["failures"], 0)

    def test_unflagged_preview_writes_no_artifact(self):
        self.assertEqual(self._run(False), 0)
        self.assertFalse((self.learn_dir / "run-events").exists())

    def test_interrupt_finalises_artifact_and_clears_sink(self):
        # Ctrl+C mid-run: the exception still propagates (the CLI's
        # checkpoint contract), the sink is cleared, and the
        # artifact's last line is run_end marked interrupted with
        # failures null, exactly as the §10 addendum specifies.
        pp.CONFIG["emit_run_events"] = True
        pp.CONFIG["jobs"] = 1

        def boom(f, dry_run, preview):
            raise KeyboardInterrupt

        with patch.object(pp, "process_file", side_effect=boom), \
                mock.patch.object(pp.UI, "start", lambda *a: None), \
                mock.patch.object(pp.UI, "stop", lambda *a: None), \
                mock.patch.object(pp.UI, "status", lambda *a: None):
            with self.assertRaises(KeyboardInterrupt):
                pp.run_once(False, True, file=str(self.book))
        self.assertFalse(pp._run_events_active())
        artifacts = list((self.learn_dir / "run-events").glob("*.jsonl"))
        self.assertEqual(len(artifacts), 1)
        evs = _read_events(artifacts[0])
        self.assertEqual(evs[-1]["event"], "run_end")
        self.assertTrue(evs[-1]["interrupted"])
        self.assertIsNone(evs[-1]["failures"])


if __name__ == "__main__":
    unittest.main()
