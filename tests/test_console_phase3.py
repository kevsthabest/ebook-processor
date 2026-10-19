"""Phase 3 verdict streaming: pipeline hooks publish merge,
relationship, trigger, trope, and evidence outcomes to the console
store at the point each fact becomes true — and the reduce result
is byte-identical with and without an observer attached.

Stdlib unittest only. Pilot tests are skip-guarded on the optional
textual package, as in Phase 2.
Run: python -m unittest discover -s tests -v
"""
import asyncio
import importlib.util
import json
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

import console_events as ce  # noqa: E402

try:
    import console_app as capp  # noqa: E402
except Exception:  # pragma: no cover
    capp = None

try:
    import textual  # noqa: F401
    _HAVE_TEXTUAL = True
except Exception:
    _HAVE_TEXTUAL = False


class _Validator:
    """Relationship judge stub: P by (from, to)."""

    model = "stub"

    def __init__(self, ps):
        self.ps = ps

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        return True, self.ps.get((frm, to), 0.95)


def _roster(*names):
    roster = {}
    for name in names:
        pp._roster_update(
            roster,
            [{"name": name, "aliases": [], "role": "supporting",
              "description": "", "evidence": ""}],
            1)
    return roster


_CHS = [
    {"index": 1, "label": "Ch1",
     "text": "Alice said to Bob, 'I will always love you.' " * 30},
    {"index": 2, "label": "Ch2",
     "text": "Carol glared at Dave. 'You ruined everything.' " * 30},
    {"index": 3, "label": "Ch3",
     "text": "Eve and Frank renewed their vows at dawn. " * 30},
    {"index": 4, "label": "Ch4",
     "text": "Gina watched Henry fix the gate in silence. " * 30},
]

_RELS = {1: {"characters": [], "pov_character": None, "relationships": [
    {"from": "Alice", "to": "Bob", "type": "spouse",
     "evidence": "I will always love you."},
    {"from": "Alice", "to": "Bob", "type": "friend",
     "evidence": "I will always love you."},
    {"from": "Carol", "to": "Dave", "type": "enemy",
     "evidence": "You ruined everything."},
    {"from": "Eve", "to": "Frank", "type": "spouse",
     "evidence": "renewed their vows"},
    {"from": "Gina", "to": "Henry", "type": "spouse",
     "evidence": "watched Henry fix the gate"},
]}}


class _ConsoleCase(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        pp._DECISION_VALIDATOR = _Validator({
            ("Alice", "Bob"): 0.95,
            ("Carol", "Dave"): 0.10,
            ("Eve", "Frank"): 0.50,
            ("Gina", "Henry"): 0.10,
            ("Gina", "Henry".replace("Henry", "Henry")): 0.10,
        })
        self.obs = ce.ConsoleObserver()
        pp.UI.attach_console(self.obs)
        pp._DIAG.viz_book_key = "book-1"

    def tearDown(self):
        pp.UI.attach_console(None)
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def _store(self):
        store = ce.ConsoleStore()
        for event in self.obs.drain():
            store.apply(event)
        return store


class TestReduceVerdictStreaming(_ConsoleCase):
    def test_verdicts_finals_and_losers(self):
        red = pp.v2_reduce(_RELS, {}, _roster(
            "Alice", "Bob", "Carol", "Dave", "Eve", "Frank",
            "Gina", "Henry"), _CHS, preview=False)
        book = self._store().books["book-1"]
        rows = book.rows["relationships"]
        kept = rows["pair:alice|bob|type:spouse|origin:extracted"]
        self.assertEqual(kept["decision"], "kept")
        self.assertEqual(kept["final_state"], "validated")
        self.assertEqual(kept["p"], 0.95)
        down = rows["pair:eve|frank|type:spouse|origin:extracted"]
        self.assertEqual(down["decision"], "downgraded")
        self.assertEqual(down["final_state"], "validated")
        self.assertEqual(down["shipped_type"], "other")
        dropped = rows["pair:carol|dave|type:enemy|origin:extracted"]
        self.assertEqual(dropped["decision"], "dropped")
        self.assertEqual(dropped["final_state"], "rejected")
        loser = rows["pair:alice|bob|type:friend|origin:loser"]
        self.assertTrue(loser["not_selected"])
        self.assertNotIn("decision", loser)
        self.assertNotIn("final_state", loser)
        # Gina/Henry spouse died at 0.10 and its revival candidate
        # (the same claim shape has no loser here) — counts must
        # reconcile with the result's own states.
        result_states = Counter(
            r.get("validation_state")
            for r in red["relationships"]
            + red["dropped_relationships"]
            if not r.get("family_guard"))
        self.assertEqual(book.counts["rel_validated"],
                         result_states.get("validated", 0))
        self.assertEqual(book.counts["rel_rejected"],
                         result_states.get("rejected", 0))
        self.assertEqual(book.counts["rel_candidates"], 5)
        self.assertEqual(book.counts["reduced_claims"], 4)
        self.assertEqual(book.counts["pair_losers"], 1)

    def test_revival_streams_under_its_own_origin(self):
        rels = {1: {"characters": [], "pov_character": None,
                    "relationships": [
                        {"from": "Gina", "to": "Henry",
                         "type": "spouse",
                         "evidence": "watched Henry fix the gate"},
                        {"from": "Gina", "to": "Henry",
                         "type": "friend",
                         "evidence": "watched Henry fix the gate"},
                    ]}}
        pp._DECISION_VALIDATOR = _Validator({
            ("Gina", "Henry"): 0.10})
        # The spouse dies at 0.10; the friend revival is judged by
        # the non-family stub — give it a keep via a type-aware
        # stub instead.
        class _ByType:
            model = "stub"

            def validate_relationship(self, frm, to, rtype, quote,
                                      context=""):
                return True, 0.10 if rtype == "spouse" else 0.90

        pp._DECISION_VALIDATOR = _ByType()
        pp.v2_reduce(rels, {}, _roster("Gina", "Henry"), _CHS,
                     preview=False)
        book = self._store().books["book-1"]
        revived = book.rows["relationships"][
            "pair:gina|henry|type:friend|origin:revived_runner_up"]
        self.assertEqual(revived["decision"], "kept")
        self.assertEqual(revived["final_state"], "validated")
        self.assertTrue(revived["revived"])

    def test_result_identical_without_observer(self):
        def run():
            pp._dm_budget_reset()
            return pp.v2_reduce(_RELS, {}, _roster(
                "Alice", "Bob", "Carol", "Dave", "Eve", "Frank",
                "Gina", "Henry"), _CHS, preview=False)

        with_obs = run()
        pp.UI.attach_console(None)
        without_obs = run()

        def shape(red):
            return json.dumps({
                "relationships": red["relationships"],
                "dropped": red["dropped_relationships"],
                "losers": red["pair_losers"],
                "pool": red["decision_pool"],
            }, sort_keys=True, default=str)

        self.assertEqual(shape(with_obs), shape(without_obs))


class TestMergeStreaming(unittest.TestCase):
    def setUp(self):
        self.obs = ce.ConsoleObserver()
        pp.UI.attach_console(self.obs)
        pp._DIAG.viz_book_key = "book-1"
        pp._dm_budget_reset()

    def tearDown(self):
        pp.UI.attach_console(None)
        pp._dm_budget_reset()

    def _mk_roster(self):
        roster = {}
        for name, chapters in (("Bill", [1]), ("William Smith", [2])):
            roster[pp.norm_name(name)] = {
                "name": name, "aliases": [], "chapters": set(chapters),
                "appearances": len(chapters), "description": "",
                "evidence": ""}
        return roster

    def test_model_off_unresolved_streams(self):
        n, possible = pp.post_pass_merge(self._mk_roster(),
                                         validator=None)
        self.assertEqual(n, 0)
        self.assertEqual(len(possible), 1)
        store = ce.ConsoleStore()
        for event in self.obs.drain():
            store.apply(event)
        rows = store.books["book-1"].rows["merges"]
        self.assertEqual(len(rows), 1)
        row = next(iter(rows.values()))
        self.assertEqual(row["outcome"], "unresolved_model_off")
        self.assertEqual(row["tier"], "Tier 3")

    def test_tier3_split_and_merge_stream(self):
        class _Judge:
            disabled = False

            def __init__(self, merged):
                self.merged = merged

            def ask_same_person(self, *a):
                return self.merged, 0.9 if self.merged else 0.2

        n, _ = pp.post_pass_merge(self._mk_roster(),
                                  validator=_Judge(False))
        self.assertEqual(n, 0)
        store = ce.ConsoleStore()
        for event in self.obs.drain():
            store.apply(event)
        row = next(iter(store.books["book-1"].rows["merges"]
                        .values()))
        self.assertEqual(row["outcome"], "split")
        self.assertEqual(row["p"], 0.2)


class TestTropeGateStreaming(unittest.TestCase):
    def setUp(self):
        self.obs = ce.ConsoleObserver()
        pp.UI.attach_console(self.obs)
        pp._DIAG.viz_book_key = "book-1"

    def tearDown(self):
        pp.UI.attach_console(None)

    def test_batch_verdicts_stream(self):
        def fake_call(prompt, chunk, i, n):
            return json.dumps({"verdicts": [
                {"trope": "Found Family", "verdict": "yes",
                 "justification": "Ch 1"},
                {"trope": "Secret Identity", "verdict": "no",
                 "justification": "Ch 1"}]})

        summaries = [{"index": 1, "label": "A", "summary": "s"}]
        confirmed, _ = pp.v2_trope_gate(
            fake_call, summaries,
            ["Found Family", "Secret Identity"],
            Counter({"found family": 2, "secret identity": 2}))
        self.assertEqual(confirmed, ["Found Family"])
        store = ce.ConsoleStore()
        for event in self.obs.drain():
            store.apply(event)
        rows = store.books["book-1"].rows["tropes"]
        self.assertEqual(rows["found family"]["decision"],
                         "gate_confirmed")
        self.assertEqual(rows["secret identity"]["decision"],
                         "gate_rejected")


class TestEvidenceStreaming(unittest.TestCase):
    def setUp(self):
        self.obs = ce.ConsoleObserver()
        pp.UI.attach_console(self.obs)
        pp._DIAG.viz_book_key = "book-1"

    def tearDown(self):
        pp.UI.attach_console(None)

    def test_retrieval_outcome_rides_the_trope_row(self):
        from test_evidence_retrieval import (FakeEmbedder,
                                             FakeJudgeCall, SCENE_CH,
                                             TALK_CH)
        claim = {"key": pp._tnorm("Betrayal"), "name": "Betrayal",
                 "query_text": pp.evidence_query_text("Betrayal", ""),
                 "terms": pp.evidence_query_terms("Betrayal"),
                 "support": [SCENE_CH, TALK_CH], "definition": "",
                 "staged": False}
        _ev, records, _marker = pp._evidence_run(
            [claim], call=FakeJudgeCall(), embedder=FakeEmbedder())
        self.assertIn(records["betrayal"]["outcome"],
                      ("supported-strong", "supported-single"))
        store = ce.ConsoleStore()
        for event in self.obs.drain():
            store.apply(event)
        book = store.books["book-1"]
        row = book.rows["tropes"]["betrayal"]
        self.assertEqual(row["evidence_outcome"],
                         records["betrayal"]["outcome"])
        self.assertIsNotNone(row["evidence_support"])
        self.assertEqual(book.counts["retrieval_attempted"], 1)


def _seeded_console():
    obs = ce.ConsoleObserver()
    store = ce.ConsoleStore()
    obs.publish("book_state", book_id="b1",
                payload={"state": "active", "title": "Fourth Wing"})
    obs.publish("stage_state", book_id="b1", stage="merge_review",
                payload={"state": "complete"})
    obs.publish("counts_snapshot", book_id="b1", payload={"counts": {
        "rel_candidates": 3, "reduced_claims": 2,
        "rel_validated": 1, "rel_rejected": 1, "decision_pool": 164,
        "decision_spent": 160, "merges_merged": 1,
        "merges_unresolved": 1}})
    obs.publish("verdict_upsert", book_id="b1", payload={
        "table": "relationships",
        "row_key": "pair:mira sorrengail|tairn|type:bonded"
                   "|origin:extracted",
        "decision": "kept", "p": 0.42, "final_state": "validated",
        "record": {"from": "Mira Sorrengail", "to": "Tairn",
                   "claimed_type": "bonded", "shipped_type": "bonded",
                   "evidence": {"quote": "Don't say [b]it[/b]",
                                "source": "mined",
                                "grade": "co-occurrence",
                                "chapter": "Chapter 9"}}})
    obs.publish("verdict_upsert", book_id="b1", payload={
        "table": "merges",
        "row_key": "merge:garrick|garrick tavis",
        "record": {"name_a": "Garrick", "name_b": "Garrick Tavis",
                   "tier": "Tier 3", "outcome": "merged", "p": 0.91}})
    obs.publish("candidate_upsert", book_id="b1", payload={
        "table": "tropes", "key": "found family",
        "record": {"name": "Found Family", "chapters": 12,
                   "state": "shipped", "confidence": 0.85}})
    obs.publish("candidate_upsert", book_id="b1", payload={
        "table": "triggers", "key": "trig:war",
        "record": {"warning": "war", "severity": "graphic",
                   "severity_claimed": "graphic", "chapters": 43,
                   "gate": "decision", "status": "kept"}})
    return obs, store, {}


@unittest.skipUnless(_HAVE_TEXTUAL and capp is not None,
                     "textual not installed")
class TestPhase3Pilot(unittest.TestCase):
    def test_tabs_fill_and_merges_appears(self):
        async def main():
            from textual.widgets import DataTable
            obs, store, control = _seeded_console()
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertEqual(
                    app.query_one("#rels", DataTable).row_count, 1)
                self.assertEqual(
                    app.query_one("#tropes", DataTable).row_count, 1)
                self.assertEqual(
                    app.query_one("#triggers", DataTable).row_count, 1)
                self.assertEqual(
                    app.query_one("#merges", DataTable).row_count, 1)
                self.assertTrue(app._merges_shown)
                hdr = str(app.query_one("#rels-hdr").render())
                self.assertIn("3 candidates", hdr)
                self.assertIn("1 validated", hdr)
        asyncio.run(main())

    def test_selecting_relationship_pins_evidence(self):
        async def main():
            from textual.widgets import DataTable, TabbedContent
            obs, store, control = _seeded_console()
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.query_one("#tabs", TabbedContent).active = "t-rels"
                await pilot.pause()
                table = app.query_one("#rels", DataTable)
                table.focus()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                detail = str(app.query_one("#detail").render())
                self.assertIn("Judgment kept", detail)
                self.assertIn("Final state validated", detail)
                reader = str(app.query_one("#reader").render())
                self.assertIn("Don't say [b]it[/b]", reader)
                self.assertIn("co-occurrence", reader)
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
