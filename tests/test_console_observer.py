"""Phase 1 tests for the inspection-console seam (console_events.py
plus the PipelineUI observer hooks). Stdlib unittest only.

The contract under test: publishing never raises into pipeline
code, sequence numbers are monotonic across threads, a saturated
observer marks books stale instead of blocking, the store never
promotes an interim judgment to a final state on its own, and pair
losers never acquire a verdict by display logic.
Run: python -m unittest discover -s tests -v
"""
import io
import sys
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import importlib.util

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import console_events as ce  # noqa: E402

_MOD_PATH = str(_ROOT / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _ev(kind, book="b1", stage=None, payload=None):
    return {"v": ce.EVENT_VERSION, "seq": 0, "ts": 0.0, "run_id": None,
            "book_id": book, "stage": stage, "kind": kind,
            "payload": payload or {}}


class TestRegistry(unittest.TestCase):
    def test_stage_registry_shape(self):
        ids = [sid for sid, _ in ce.STAGES]
        self.assertEqual(ids, ["acquisition", "identity", "content",
                               "merge_review", "relationship_validation",
                               "content_gates", "evidence_retrieval",
                               "final_gates"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_phase_map(self):
        self.assertEqual(ce.PHASE_TO_STAGE["characters"], "identity")
        self.assertEqual(ce.PHASE_TO_STAGE["content"], "content")


class TestObserver(unittest.TestCase):
    def test_seq_monotonic_across_threads(self):
        obs = ce.ConsoleObserver()
        seqs = []
        lock = threading.Lock()

        def worker():
            for _ in range(50):
                s = obs.publish("log", book_id="b1", payload={"text": "x"})
                with lock:
                    seqs.append(s)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(seqs), list(range(1, 201)))
        drained = obs.drain()
        self.assertEqual([e["seq"] for e in drained], list(range(1, 201)))
        self.assertEqual(len(obs), 0)

    def test_unknown_kind_rejected_without_raising(self):
        obs = ce.ConsoleObserver()
        self.assertIsNone(obs.publish("nonsense", book_id="b1"))
        self.assertEqual(obs.warnings, 1)
        self.assertEqual(len(obs), 0)

    def test_saturation_marks_stale_and_never_blocks(self):
        obs = ce.ConsoleObserver(max_queue=3)
        for _ in range(3):
            obs.publish("verdict_upsert", book_id="b1", payload={})
        # Queue full of verdicts: the next verdict has nothing it may
        # displace, so it drops and the book is marked stale.
        self.assertIsNone(obs.publish("verdict_upsert", book_id="b1",
                                      payload={}))
        self.assertIn("b1", obs.stale_books)

    def test_verdict_displaces_counts_snapshot(self):
        obs = ce.ConsoleObserver(max_queue=2)
        obs.publish("counts_snapshot", book_id="b1", payload={"counts": {}})
        obs.publish("log", book_id="b1", payload={"text": "x"})
        seq = obs.publish("verdict_upsert", book_id="b1", payload={})
        self.assertIsNotNone(seq)
        kinds = [e["kind"] for e in obs.drain()]
        self.assertEqual(kinds, ["log", "verdict_upsert"])

    def test_broken_payload_never_raises(self):
        obs = ce.ConsoleObserver()
        # Unorderable / exotic payloads still publish fine; the store
        # is responsible for tolerating shapes, not the publisher.
        self.assertIsNotNone(obs.publish("log", book_id="b1",
                                         payload={"text": object()}))


class TestStore(unittest.TestCase):
    def test_book_and_stage_flow(self):
        store = ce.ConsoleStore()
        store.apply(_ev("book_state", payload={"state": "active",
                                               "title": "Daemon"}))
        store.apply(_ev("stage_state", stage="identity",
                        payload={"state": "active"}))
        book = store.books["b1"]
        self.assertEqual(book.state, "active")
        self.assertEqual(book.title, "Daemon")
        self.assertEqual(book.stages["identity"], "active")
        self.assertEqual(book.stages["content"], "not_started")
        self.assertEqual(store.live_book_id(), "b1")

    def test_interim_verdict_keeps_final_state_none(self):
        store = ce.ConsoleStore()
        store.apply(_ev("verdict_upsert", payload={
            "row_key": "pair:x|type:sibling",
            "decision": "downgraded", "p": 0.68, "final_state": None}))
        row = store.books["b1"].rows["relationships"]["pair:x|type:sibling"]
        self.assertEqual(row["decision"], "downgraded")
        self.assertEqual(row["p"], 0.68)
        self.assertNotIn("final_state", row)

    def test_final_state_lands_only_when_carried(self):
        store = ce.ConsoleStore()
        key = "pair:x|type:sibling"
        store.apply(_ev("verdict_upsert", payload={
            "row_key": key, "decision": "kept", "p": 0.9}))
        store.apply(_ev("verdict_upsert", payload={
            "row_key": key, "final_state": "review_required"}))
        row = store.books["b1"].rows["relationships"][key]
        self.assertEqual(row["decision"], "kept")
        self.assertEqual(row["final_state"], "review_required")

    def test_nested_record_cannot_smuggle_verdict_fields(self):
        store = ce.ConsoleStore()
        key = "pair:x|type:mentor"
        store.apply(_ev("verdict_upsert", payload={
            "row_key": key,
            "record": {"decision": "kept", "final_state": "validated",
                       "note": "n"}}))
        row = store.books["b1"].rows["relationships"][key]
        self.assertNotIn("decision", row)
        self.assertNotIn("final_state", row)
        self.assertEqual(row["note"], "n")

    def test_pair_loser_never_keeps_a_verdict(self):
        store = ce.ConsoleStore()
        key = "pair:x|type:romantic"
        store.apply(_ev("candidate_upsert", payload={
            "table": "relationships", "key": key,
            "record": {"not_selected": True}}))
        store.apply(_ev("verdict_upsert", payload={
            "row_key": key, "decision": "kept", "p": 0.99,
            "final_state": "validated"}))
        row = store.books["b1"].rows["relationships"][key]
        self.assertNotIn("decision", row)
        self.assertNotIn("final_state", row)

    def test_unknown_events_ignored(self):
        store = ce.ConsoleStore()
        store.apply({"v": 999, "kind": "book_state", "book_id": "b1",
                     "payload": {"state": "active"}})
        store.apply(_ev("mystery_kind"))
        store.apply(_ev("stage_state", stage="not_a_stage",
                        payload={"state": "active"}))
        self.assertEqual(store.books["b1"].state, "pending")
        self.assertTrue(all(s == "not_started"
                            for s in store.books["b1"].stages.values()))

    def test_row_cap_bounds_rows_but_counts_stay_exact(self):
        store = ce.ConsoleStore()
        cap = ce.ROW_CAPS["triggers"]
        for i in range(cap + 5):
            store.apply(_ev("candidate_upsert", payload={
                "table": "triggers", "key": f"t{i}", "record": {}}))
        book = store.books["b1"]
        self.assertEqual(len(book.rows["triggers"]), cap)
        self.assertEqual(book.row_counts["triggers"], cap + 5)
        self.assertIn("triggers", book.capped)

    def test_book_final_reconciles(self):
        store = ce.ConsoleStore()
        store.apply(_ev("book_final", payload={
            "rows": {"relationships": {"k1": {"decision": "kept"}}}}))
        book = store.books["b1"]
        self.assertEqual(book.state, "complete")
        self.assertIn("k1", book.rows["relationships"])


class TestUIFacade(unittest.TestCase):
    def _ui_with_store(self):
        obs = ce.ConsoleObserver()
        store = ce.ConsoleStore()
        ui = pp.PipelineUI()
        ui.attach_console(obs)
        return ui, obs, store

    def _pump(self, obs, store):
        for event in obs.drain():
            store.apply(event)

    def test_hooks_flow_into_store(self):
        ui, obs, store = self._ui_with_store()
        with redirect_stdout(io.StringIO()):
            ui.book_start("bookA", "Daemon - Daniel Suarez.epub", 73)
            ui.set_phase("bookA", "characters")
            ui.viz_chapter("bookA", "Chapter 1", "text", chap_idx=1)
            ui.book_done("bookA")
        self._pump(obs, store)
        book = store.books["bookA"]
        self.assertEqual(book.state, "complete")
        self.assertEqual(book.stages["identity"], "active")
        self.assertIn(("identity", 1), book.chapters)

    def test_no_observer_is_a_noop(self):
        ui = pp.PipelineUI()
        with redirect_stdout(io.StringIO()):
            ui.book_start("bookA", "Daemon", 73)
            ui.set_phase("bookA", "characters")
            ui.viz_chapter("bookA", "Chapter 1", "text", chap_idx=1)
            ui.book_done("bookA")
        self.assertIsNone(ui._console_observer)

    def test_exploding_observer_cannot_break_the_ui(self):
        class Boom:
            def publish(self, *a, **k):
                raise RuntimeError("boom")

        ui = pp.PipelineUI()
        ui.attach_console(Boom())
        with redirect_stdout(io.StringIO()):
            ui.book_start("bookA", "Daemon", 73)
            ui.set_phase("bookA", "content")
            ui.book_done("bookA")  # must not raise

    def test_unknown_phase_publishes_nothing(self):
        ui, obs, store = self._ui_with_store()
        ui.set_phase("bookA", "some_future_phase")
        self.assertEqual(len(obs), 0)


if __name__ == "__main__":
    unittest.main()
