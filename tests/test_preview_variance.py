"""Tests for preview_variance.py. Stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "preview_variance.py")
_spec = importlib.util.spec_from_file_location("preview_variance", _MOD_PATH)
pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pv)


def _preview(**overrides):
    d = {
        "title": "Daemon",
        "author": "Daniel Suarez",
        "spice_level": 2,
        "tropes": ["Secret Identity", "Redemption"],
        "triggers": [
            {"warning": "violence", "prominence": "high"},
            {"warning": "child_abuse", "prominence": "low"},
        ],
        "characters": [{"name": "Matthew Sobol"}, {"name": "Jon Ross"}],
        "minor_characters": [{"name": "Roy Merritt"}],
        "relationships": [
            {"from": "Jon Ross", "to": "Matthew Sobol", "type": "enemy"},
        ],
        "povs": ["Brian Gragg"],
    }
    d.update(overrides)
    return d


class TestJaccard(unittest.TestCase):
    def test_identical_sets_score_one(self):
        self.assertEqual(pv.jaccard({"a", "b"}, {"a", "b"}), 1.0)

    def test_disjoint_sets_score_zero(self):
        self.assertEqual(pv.jaccard({"a"}, {"b"}), 0.0)

    def test_both_empty_score_one(self):
        self.assertEqual(pv.jaccard(set(), set()), 1.0)

    def test_one_empty_scores_zero(self):
        self.assertEqual(pv.jaccard({"a"}, set()), 0.0)


class TestComparePreviews(unittest.TestCase):
    def test_identical_previews_perfect_scores(self):
        lines = pv.compare_previews(_preview(), _preview())
        text = "\n".join(lines)
        self.assertIn("overall (mean)", text)
        self.assertIn("1.000", text)
        self.assertNotIn("WARNING", text)
        self.assertIn("delta +0", text)

    def test_disjoint_tropes_score_zero(self):
        old = _preview(tropes=["Alpha", "Beta"])
        new = _preview(tropes=["Gamma", "Delta"])
        _, score = pv.compare_set_field(
            old, new, "tropes", pv.extract_tropes, "Tropes")
        self.assertEqual(score, 0.0)

    def test_trope_normalization_case_and_punctuation(self):
        old = _preview(tropes=["Secret Identity"])
        new = _preview(tropes=["secret-identity"])
        _, score = pv.compare_set_field(
            old, new, "tropes", pv.extract_tropes, "Tropes")
        self.assertEqual(score, 1.0)

    def test_prominence_change_detected(self):
        new = _preview(triggers=[
            {"warning": "violence", "prominence": "low"},
            {"warning": "child_abuse", "prominence": "low"},
        ])
        lines, score = pv.compare_triggers(_preview(), new)
        text = "\n".join(lines)
        self.assertIn("prominence changes (1)", text)
        self.assertIn("high -> low", text)
        # Labels are identical, so the set score stays 1.0.
        self.assertEqual(score, 1.0)

    def test_spice_delta(self):
        lines, score = pv.compare_spice(_preview(), _preview(spice_level=4))
        self.assertIn("delta +2", "\n".join(lines))
        self.assertEqual(score, 0.0)

    def test_missing_field_reported(self):
        old = _preview()
        del old["tropes"]
        lines, score = pv.compare_set_field(
            old, _preview(), "tropes", pv.extract_tropes, "Tropes")
        self.assertIsNone(score)
        self.assertIn("absent in old", "\n".join(lines))

    def test_absent_in_both(self):
        old, new = _preview(), _preview()
        del old["povs"], new["povs"]
        lines, score = pv.compare_set_field(
            old, new, "povs", pv.extract_povs, "POVs")
        self.assertIsNone(score)
        self.assertIn("absent in both", "\n".join(lines))

    def test_character_name_normalization(self):
        old = _preview(characters=[{"name": "The Daemon"}])
        new = _preview(characters=[{"name": "daemon"}])
        keys_o, _ = pv.extract_characters(old, "characters")
        keys_n, _ = pv.extract_characters(new, "characters")
        self.assertEqual(keys_o, keys_n)

    def test_relationship_pair_is_order_insensitive(self):
        old = _preview(relationships=[
            {"from": "Jon Ross", "to": "Matthew Sobol", "type": "enemy"}])
        new = _preview(relationships=[
            {"from": "Matthew Sobol", "to": "Jon Ross", "type": "enemy"}])
        ko, _ = pv.extract_relationships(old)
        kn, _ = pv.extract_relationships(new)
        self.assertEqual(ko, kn)

    def test_relationship_type_change_is_a_diff(self):
        old = _preview(relationships=[
            {"from": "A", "to": "B", "type": "friend"}])
        new = _preview(relationships=[
            {"from": "A", "to": "B", "type": "enemy"}])
        _, score = pv.compare_set_field(
            old, new, "relationships", pv.extract_relationships,
            "Relationships")
        self.assertEqual(score, 0.0)

    def test_identity_mismatch_warns(self):
        lines = pv.compare_previews(_preview(), _preview(title="Freedom(TM)"))
        self.assertIn("WARNING: identity differs", "\n".join(lines))

    def test_full_roster_merges_principals_and_minors(self):
        keys, _ = pv.extract_full_roster(_preview())
        self.assertIn("matthew sobol", keys)
        self.assertIn("roy merritt", keys)

    def test_long_lists_capped(self):
        many = [f"Trope {i}" for i in range(30)]
        lines, _ = pv.compare_set_field(
            _preview(tropes=[]), _preview(tropes=many),
            "tropes", pv.extract_tropes, "Tropes")
        text = "\n".join(lines)
        self.assertIn("... and 10 more", text)


if __name__ == "__main__":
    unittest.main()
