"""Measurement spine, slice 7: the uncertainty queue
(spine-design §3.5/§3.7 in the design's numbering: queue classes,
binding 4:1 batching controls (OC-4), self-contained packets,
label intake into eval/merge_pairs.json, and A6 — emissions
during a replay land in the sandbox substrate and are discarded
with it).
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _result():
    return {
        "title": "Daemon", "author": "Daniel Suarez",
        "possible_merges": [
            {"name_a": "Pete", "name_b": "Peter Sebeck",
             "p_yes": 0.79, "reason": "nickname"},
            {"name_a": "A", "name_b": "B", "p_yes": 0.30,
             "reason": "far from the bar — not near-threshold"},
        ],
        "relationships": [
            {"from": "Alice", "to": "Bob", "type": "parent",
             "evidence": "", "evidence_grade": "binding",
             "evidence_mined": "Alice raised Bob.",
             "validation_p": 0.12, "validation_note": "would_drop"},
            {"from": "Carol", "to": "Dan", "type": "sibling",
             "evidence": "Carol, Dan's sister, laughed.",
             "validation_p": 0.66, "validation_note": "downgraded"},
            {"from": "Eve", "to": "Finn", "type": "parent",
             "evidence": "Eve held Finn.",
             "validation_p": 0.95, "validation_note": "kept"},
        ],
        "chapter_data": {"roster": {
            "pete": {"name": "Pete", "chapters": [1, 2],
                     "descriptions": [[1, "A detective.", True]]},
            "peter sebeck": {"name": "Peter Sebeck",
                             "chapters": [2, 3],
                             "descriptions": [[2, "The detective.",
                                                True]]},
        }},
        "staging": {"promoted": [
            {"key": "trope:id:t1", "display": "Found Family",
             "via": "persistence", "form_changed": True},
            {"key": "trope:id:t2", "display": "Heist",
             "via": "persistence", "form_changed": False}]},
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        pp._close_state_dbs()
        self._td.cleanup()


class TestCollectors(unittest.TestCase):
    def test_all_four_classes_collected(self):
        items = pp.collect_uncertainty_items(
            _result(), book_label="Daemon (Daniel Suarez)",
            work_key="wk")
        classes = [i["class"] for i in items]
        self.assertIn("near-threshold", classes)       # p_yes .79
        self.assertIn("tier-judge-disagreement", classes)  # binding vs .12
        self.assertIn("family-band", classes)          # 0.66 near edge
        self.assertIn("form-changed-promotion", classes)
        merge = [i for i in items
                 if i["class"] == "near-threshold"][0]
        packet = merge["packet"]
        # Self-contained: both forms, the score, chapter co-listing,
        # descriptions as evidence, provenance.
        self.assertEqual(packet["name_a"], "Pete")
        self.assertEqual(packet["p_yes"], 0.79)
        self.assertEqual(packet["chapters_both"], [2])
        self.assertTrue(packet["desc_a"])
        self.assertIn("book", packet["provenance"])
        # The far pair and the clean control are NOT uncertainty.
        subjects = " ".join(i["subject"] for i in items)
        self.assertNotIn("A ~ B", subjects)

    def test_controls_collected_separately(self):
        items = pp.collect_uncertainty_items(
            _result(), book_label="Daemon", work_key="wk")
        controls = [i for i in items if i["class"] == "control"]
        self.assertEqual(len(controls), 1)  # Eve/Finn at 0.95


class TestQueue(_Base):
    def test_emit_and_batch_mix_4_to_1(self):
        pp.StateDB.create(self.dir)
        items = []
        for i in range(10):
            items.append({"class": "near-threshold",
                          "subject": f"merge {i}",
                          "packet": {"name_a": f"A{i}",
                                     "name_b": f"B{i}"}})
        for i in range(5):
            items.append({"class": "control", "subject": f"ctl {i}",
                          "packet": {"name_a": f"C{i}",
                                     "name_b": f"D{i}"}})
        n = pp.emit_uncertainty(self.dir, "run-1", items)
        self.assertEqual(n, 15)
        batch = pp.build_labelling_batch(self.dir, cap=10)
        kinds = [b["class"] for b in batch]
        self.assertEqual(len(batch), 10)
        self.assertEqual(kinds.count("control"), 2)   # 4:1 of 10
        self.assertEqual(kinds.count("near-threshold"), 8)

    def test_apply_labels_updates_rows_and_merge_pairs(self):
        pp.StateDB.create(self.dir)
        pp.emit_uncertainty(self.dir, "run-1", [
            {"class": "near-threshold", "subject": "merge",
             "packet": {"kind": "merge_pair",
                        "name_a": "Pete", "name_b": "Peter Sebeck",
                        "book": "Daemon (Daniel Suarez)",
                        "desc_a": "A detective.", "desc_b": "The one.",
                        "evidence_a": "q1", "evidence_b": "q2"}}])
        db = pp._state_db_for(self.dir)
        qid = db.query("SELECT id FROM uncertainty_queue")[0][0]
        labels = self.dir / "labels.json"
        labels.write_text(json.dumps(
            [{"queue_id": qid, "label": "same", "labeller": "user"}]),
            encoding="utf-8")
        pairs = self.dir / "merge_pairs.json"
        pairs.write_text("[]", encoding="utf-8")
        rep = pp.apply_uncertainty_labels(self.dir, labels,
                                          merge_pairs_path=pairs)
        self.assertEqual(rep["labelled"], 1)
        row = db.query("SELECT status, label, labelled_by FROM "
                       "uncertainty_queue")[0]
        self.assertEqual(row, ("labelled", "same", "user"))
        entries = json.loads(pairs.read_text())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], "same")
        self.assertEqual(entries[0]["name_a"], "Pete")
        self.assertEqual(entries[0]["source"],
                         "uncertainty queue (spine)")

    def test_split_suspects_from_substrate(self):
        pp.StateDB.create(self.dir)
        store = pp.RosterStore(learn_dir=self.dir, work_key="wk")
        store.data["runs_completed"] = 4
        store.data["suspects"] = [
            {"a": "pete", "b": "peter", "run": 2,
             "reason": "form overlap"},
            {"a": "new", "b": "fresh", "run": 4,
             "reason": "just appeared"}]
        store.save()
        items = pp.roster_split_suspects(self.dir)
        self.assertEqual(len(items), 1)  # aged >= 1 run only
        self.assertEqual(items[0]["class"], "split-suspect")


if __name__ == "__main__":
    unittest.main()
