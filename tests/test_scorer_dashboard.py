"""Tests for the quality-dashboard sections of preview_variance.py.

The dashboard is measurement tooling added AFTER the legacy Jaccard
report: every legacy line must stay byte-identical for the same inputs,
and the new sections are appended below the Summary block. Stdlib
unittest only. Run: python -m unittest discover -s tests -v
"""
import glob
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "preview_variance.py")
_spec = importlib.util.spec_from_file_location("preview_variance", _MOD_PATH)
pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pv)


# The legacy report for the Daemon pair below, captured from the scorer
# before the dashboard existed. compare_previews must keep producing
# exactly these lines, in this order, as its prefix.
LEGACY_PIN = [
    "Book: Daemon by Daniel Suarez", "",
    "Spice:", "  old 2, new 4, delta +2", "",
    "Tropes:", "  old 2, new 2, shared 1, Jaccard 0.333",
    "  added (1):", "    Heist", "  removed (1):", "    Redemption", "",
    "Triggers:", "  old 1, new 1, shared 1, Jaccard 1.000",
    "  prominence: no changes on shared triggers",
    "  identical (labels + prominence)", "",
    "Characters (principals):", "  old 1, new 1, shared 1, Jaccard 1.000",
    "  identical sets", "",
    "Characters (full roster):", "  old 2, new 2, shared 2, Jaccard 1.000",
    "  identical sets", "",
    "Relationships:", "  old 1, new 1, shared 1, Jaccard 1.000",
    "  identical sets", "",
    "POVs:", "  old 1, new 1, shared 1, Jaccard 1.000",
    "  identical sets", "",
    "Summary (1.000 = identical):",
    "  spice                      0.000",
    "  tropes                     0.333",
    "  triggers                   1.000",
    "  characters                 1.000",
    "  characters (full roster)   1.000",
    "  relationships              1.000",
    "  povs                       1.000",
    "  overall (mean)             0.762",
]


def _daemon_pair():
    old = {"title": "Daemon", "author": "Daniel Suarez", "spice_level": 2,
           "tropes": ["Secret Identity", "Redemption"],
           "triggers": [{"warning": "violence", "prominence": "high"}],
           "characters": [{"name": "Matthew Sobol"}],
           "minor_characters": [{"name": "Roy Merritt"}],
           "relationships": [
               {"from": "Jon Ross", "to": "Matthew Sobol", "type": "enemy"}],
           "povs": ["Brian Gragg"]}
    new = dict(old)
    new["spice_level"] = 4
    new["tropes"] = ["Secret Identity", "Heist"]
    return old, new


LABELS = {
    "book": "Test Book - Some Author",
    "work_id": "w-test",
    "true_pairs": [
        {"from": "Brennan Sorrengail", "to": "Violet Sorrengail",
         "type": "sibling"},
        {"from": "Dad Sorrengail", "to": "Violet Sorrengail",
         "type": "parent"},
    ],
    "false_pairs": [
        {"from": "King Tauri", "to": "Violet Sorrengail", "type": "parent"},
    ],
}


def _preview(**over):
    d = {
        "title": "Test Book", "author": "Some Author", "spice_level": 2,
        "tropes": ["Alpha"],
        "triggers": [{"warning": "violence", "prominence": "high"}],
        "characters": [
            {"name": "Violet Sorrengail", "aliases": ["Vi"]},
            {"name": "Brennan Sorrengail", "aliases": ["Brennan"]},
            {"name": "Dad Sorrengail", "aliases": ["Dad"]},
            {"name": "King Tauri", "aliases": []},
        ],
        "minor_characters": [],
        "relationships": [],
        "dropped_relationships": [],
        "povs": ["Violet Sorrengail"],
    }
    d.update(over)
    return d


def _outcomes(d, labels=LABELS):
    return pv.score_labelled_pairs(d, labels)


class TestLegacyOutputFrozen(unittest.TestCase):
    def test_legacy_prefix_byte_identical(self):
        old, new = _daemon_pair()
        lines = pv.compare_previews(old, new)
        self.assertEqual(lines[:len(LEGACY_PIN)], LEGACY_PIN)

    def test_dashboard_appended_after_summary(self):
        old, new = _daemon_pair()
        lines = pv.compare_previews(old, new)
        self.assertGreater(len(lines), len(LEGACY_PIN))
        self.assertEqual(lines[len(LEGACY_PIN)], "")
        self.assertEqual(lines[len(LEGACY_PIN) + 1], "Quality dashboard:")


class TestLabelledScoring(unittest.TestCase):
    def test_kept_at_type_via_short_forms(self):
        d = _preview(relationships=[
            {"from": "Brennan", "to": "Violet", "type": "sibling",
             "evidence": "quote"}])
        out = _outcomes(d)
        self.assertEqual(len(out["true"]["kept"]), 1)
        self.assertIn("Brennan Sorrengail", out["true"]["kept"][0])

    def test_downgrade_to_other_is_not_kept(self):
        d = _preview(relationships=[
            {"from": "Dad", "to": "Violet Sorrengail", "type": "other",
             "evidence": "quote"}])
        out = _outcomes(d)
        self.assertEqual(len(out["true"]["kept"]), 0)
        self.assertEqual(len(out["true"]["downgraded"]), 1)

    def test_wrong_family_type_is_not_kept(self):
        d = _preview(relationships=[
            {"from": "Dad Sorrengail", "to": "Violet Sorrengail",
             "type": "sibling", "evidence": "quote"}])
        out = _outcomes(d)
        self.assertEqual(len(out["true"]["kept"]), 0)
        self.assertEqual(len(out["true"]["wrong_type"]), 1)

    def test_swapped_parent_child_is_kept(self):
        # Canonical storage may flip direction; child of the reversed
        # pair is the same claim as parent in label orientation.
        d = _preview(relationships=[
            {"from": "Violet Sorrengail", "to": "Dad Sorrengail",
             "type": "child", "evidence": "quote"}])
        out = _outcomes(d)
        self.assertEqual(len(out["true"]["kept"]), 1)

    def test_dropped_with_no_evidence_note_counts_dropped(self):
        d = _preview(dropped_relationships=[
            {"from": "Brennan Sorrengail", "to": "Violet Sorrengail",
             "type": "sibling", "evidence": "",
             "validation_note": "no_evidence"}])
        out = _outcomes(d)
        self.assertEqual(len(out["true"]["dropped"]), 1)
        self.assertEqual(len(out["true"]["absent"]), 1)  # the Dad pair:
        # its endpoints are distinct entries here, so no claim matches

    def test_absent_pair(self):
        out = _outcomes(_preview())
        self.assertEqual(len(out["true"]["absent"]), 2)
        self.assertEqual(len(out["false"]["absent"]), 1)

    def test_nonfamily_claim_between_endpoints_is_not_the_pair(self):
        d = _preview(relationships=[
            {"from": "Brennan Sorrengail", "to": "Violet Sorrengail",
             "type": "friend", "evidence": "quote"}])
        out = _outcomes(d)
        self.assertEqual(len(out["true"]["kept"]), 0)
        self.assertEqual(len(out["true"]["absent"]), 2)

    def test_false_pair_kept_at_type_is_precision_failure(self):
        d = _preview(relationships=[
            {"from": "King Tauri", "to": "Violet Sorrengail",
             "type": "parent", "evidence": "quote"}])
        out = _outcomes(d)
        self.assertEqual(len(out["false"]["kept"]), 1)

    def test_false_pair_downgraded_still_counts_kept(self):
        d = _preview(relationships=[
            {"from": "King Tauri", "to": "Violet Sorrengail",
             "type": "other", "evidence": "quote",
             "validation_note": "budget_exhausted"}])
        out = _outcomes(d)
        self.assertEqual(len(out["false"]["kept"]), 0)
        self.assertEqual(len(out["false"]["downgraded"]), 1)
        kept_total = sum(len(out["false"][k]) for k in
                         ("kept", "downgraded", "wrong_type"))
        self.assertEqual(kept_total, 1)

    def test_dropped_attributed_by_raw_form_when_roster_conflates(self):
        # The run's roster put Brennan Sorrengail's full name on the
        # 'Dad' entry as an alias, so the label endpoint resolves to
        # the wrong entry and the resolved pair key misses. The dropped
        # claim's raw endpoint strings still attribute it.
        d = _preview(
            chapter_data={"chapters": [], "roster": {
                "dad": {"name": "Dad", "aliases": [],
                        "alias_keys": ["dad", "brennan sorrengail"],
                        "primary_keys": ["dad", "brennan sorrengail"]},
                "brennan": {"name": "Brennan", "aliases": [],
                            "alias_keys": ["brennan"],
                            "primary_keys": ["brennan"]},
                "violet sorrengail": {
                    "name": "Violet Sorrengail", "aliases": [],
                    "alias_keys": ["violet sorrengail"],
                    "primary_keys": ["violet sorrengail"]},
            }},
            dropped_relationships=[
                {"from": "Brennan", "to": "Violet Sorrengail",
                 "type": "sibling", "evidence": "",
                 "validation_note": "no_evidence"}])
        out = _outcomes(d)
        self.assertEqual(len(out["true"]["dropped"]), 1)
        self.assertIn("Brennan Sorrengail", out["true"]["dropped"][0])

    def test_dashboard_labelled_lines(self):
        d = _preview(relationships=[
            {"from": "Brennan", "to": "Violet", "type": "sibling",
             "evidence": "quote"}])
        lines = pv.compare_dashboard(d, d, LABELS, "labels.json")
        text = "\n".join(lines)
        self.assertIn("true pairs: kept 1/2 (recall 0.500)", text)
        self.assertIn("false pairs: kept 0/1", text)

    def test_no_labels_renders_na(self):
        lines = pv.compare_dashboard(_preview(), _preview(), None, None)
        text = "\n".join(lines)
        self.assertIn("Labelled family pairs: n/a (no labels file", text)
        self.assertIn("Roster identity: n/a (not determinable from "
                      "preview without labels)", text)


class TestIdentityIndicators(unittest.TestCase):
    def _roster_preview(self, roster):
        return _preview(chapter_data={"chapters": [], "roster": roster})

    def test_false_merge_detected(self):
        d = self._roster_preview({
            "brennan sorrengail": {
                "name": "Brennan", "aliases": ["Dad"],
                "alias_keys": ["brennan", "brennan sorrengail", "dad"],
                "primary_keys": ["brennan", "brennan sorrengail", "dad"]},
            "violet sorrengail": {
                "name": "Violet Sorrengail", "aliases": [],
                "alias_keys": ["violet sorrengail"],
                "primary_keys": ["violet sorrengail"]},
        })
        merges, splits = pv.identity_indicators(d, LABELS)
        self.assertEqual(len(merges), 1)
        self.assertIn("Brennan Sorrengail", merges[0])
        self.assertIn("Dad Sorrengail", merges[0])
        self.assertEqual(splits, [])

    def test_false_split_detected(self):
        d = self._roster_preview({
            "brennan": {
                "name": "Brennan", "aliases": [],
                "alias_keys": ["brennan"], "primary_keys": ["brennan"]},
            "dad": {
                "name": "Dad", "aliases": ["Brennan Sorrengail"],
                "alias_keys": ["dad", "brennan sorrengail"],
                "primary_keys": ["dad", "brennan sorrengail"]},
            "violet sorrengail": {
                "name": "Violet Sorrengail", "aliases": [],
                "alias_keys": ["violet sorrengail"],
                "primary_keys": ["violet sorrengail"]},
        })
        merges, splits = pv.identity_indicators(d, LABELS)
        self.assertEqual(len(splits), 1)
        self.assertIn("Brennan Sorrengail", splits[0])

    def test_clean_roster_no_indicators(self):
        d = self._roster_preview({
            "brennan sorrengail": {
                "name": "Brennan Sorrengail", "aliases": ["Brennan"],
                "alias_keys": ["brennan", "brennan sorrengail"],
                "primary_keys": ["brennan sorrengail"]},
            "violet sorrengail": {
                "name": "Violet Sorrengail", "aliases": ["Vi"],
                "alias_keys": ["vi", "violet sorrengail"],
                "primary_keys": ["violet sorrengail"]},
        })
        merges, splits = pv.identity_indicators(d, LABELS)
        self.assertEqual(merges, [])
        self.assertEqual(splits, [])


class TestFormFlips(unittest.TestCase):
    def test_token_subset_flip(self):
        old = _preview(povs=["Violet"],
                       characters=[{"name": "Violet", "aliases": []}])
        new = _preview(povs=["Violet Sorrengail"])
        flips = pv.form_flips(old, new)
        self.assertEqual(len(flips), 1)
        self.assertIn("Violet", flips[0][0])
        self.assertIn("Violet Sorrengail", flips[0][1])

    def test_alias_linked_flip(self):
        old = _preview(characters=[{"name": "Tairn", "aliases": []}],
                       povs=[])
        new = _preview(characters=[
            {"name": "Tairneanach", "aliases": ["Tairn"]}], povs=[])
        flips = pv.form_flips(old, new)
        self.assertEqual(len(flips), 1)
        self.assertEqual(flips[0][2], "alias-linked")

    def test_unrelated_names_no_flip(self):
        old = _preview(characters=[{"name": "Alpha One", "aliases": []}],
                       povs=[])
        new = _preview(characters=[{"name": "Beta Two", "aliases": []}],
                       povs=[])
        self.assertEqual(pv.form_flips(old, new), [])


class TestPerRunRates(unittest.TestCase):
    def test_unresolved_endpoints(self):
        d = _preview(relationships=[
            {"from": "Ghost Person", "to": "Violet Sorrengail",
             "type": "friend", "evidence": "q"},
            {"from": "Vi", "to": "Brennan Sorrengail", "type": "friend",
             "evidence": "q"},
        ])
        bad, total, names = pv.unresolved_endpoints(d)
        self.assertEqual((bad, total), (1, 2))
        self.assertEqual(names, ["Ghost Person"])

    def test_empty_evidence_split(self):
        d = _preview(relationships=[
            {"from": "Dad Sorrengail", "to": "Violet Sorrengail",
             "type": "parent", "evidence": ""},
            {"from": "Brennan Sorrengail", "to": "Violet Sorrengail",
             "type": "sibling", "evidence": "a real quote"},
            {"from": "Brennan Sorrengail", "to": "King Tauri",
             "type": "friend"},
        ])
        fam, nonfam = pv.empty_evidence_rates(d)
        self.assertEqual(fam, (1, 2))
        self.assertEqual(nonfam, (1, 1))


class TestCrossRunSections(unittest.TestCase):
    def test_spice_crossings(self):
        lines = pv.compare_dashboard(_preview(spice_level=2),
                                     _preview(spice_level=4), None, None)
        self.assertIn("Spice threshold crossings: 2 -> 4, crossed: 3, 4",
                      "\n".join(lines))

    def test_spice_no_change(self):
        lines = pv.compare_dashboard(_preview(spice_level=3),
                                     _preview(spice_level=3), None, None)
        self.assertIn("Spice threshold crossings: 3 -> 3, no boundary "
                      "crossed", "\n".join(lines))

    def test_queue_churn_counts(self):
        old = _preview(tropes=["A", "B"],
                       triggers=[{"warning": "violence"}])
        new = _preview(tropes=["B", "C"],
                       triggers=[{"warning": "violence"},
                                 {"warning": "grief"}])
        lines = pv.compare_dashboard(old, new, None, None)
        self.assertIn("Queue churn: tropes +1/-1, triggers +1/-0",
                      "\n".join(lines))


class TestFindLabels(unittest.TestCase):
    def test_matches_by_work_id(self):
        d = _preview(work_id="a42e27eb-f5c4-4082-be90-31ef9c6183e7")
        found = pv.find_labels(d, d)
        self.assertIsNotNone(found)
        data, _source = found
        self.assertEqual(len(data["true_pairs"]), 5)
        self.assertEqual(len(data["false_pairs"]), 13)

    def test_no_match_returns_none(self):
        d = _preview(work_id="no-such-work", title="Unrelated",
                     author="Nobody")
        self.assertIsNone(pv.find_labels(d, d))


_FW_DIR = Path(__file__).resolve().parents[2] / "user" / "files"


def _fw_path(tag):
    hits = sorted(glob.glob(str(_FW_DIR / f"Fourth_Wing_*{tag}.json")))
    return hits[0] if hits else None


@unittest.skipUnless(_fw_path("-15") and _fw_path("-16"),
                     "FW -15/-16 preview artefacts not present")
class TestFourthWingIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = pv.load_preview(_fw_path("-15"))
        cls.new = pv.load_preview(_fw_path("-16"))
        found = pv.find_labels(cls.old, cls.new)
        assert found is not None
        cls.labels, cls.source = found
        cls.text = "\n".join(pv.compare_previews(
            cls.old, cls.new, cls.labels, cls.source))

    def test_legacy_headline_scores_reproduce(self):
        self.assertIn("  spice                      1.000", self.text)
        self.assertIn("  characters (full roster)   0.671", self.text)
        self.assertIn("  relationships              0.051", self.text)
        self.assertIn("  overall (mean)             0.481", self.text)

    def test_labelled_outcomes_old_run(self):
        # Independent manual scoring of the -15 artefact: Mira kept;
        # General Sorrengail, Brennan, and Fen's outcomes as below.
        # The Dad pair lands in "dropped", not "absent": in -15 the
        # roster folded Dad into the Brennan entry (alias "Dad" on
        # 'Brennan'), so the dropped Brennan -> Violet claim is the
        # only family claim the merged identity produced — the same
        # conflation the roster-identity section flags as a merge.
        out = pv.score_labelled_pairs(self.old, self.labels)
        self.assertEqual(len(out["true"]["kept"]), 1)
        self.assertEqual(len(out["true"]["dropped"]), 3)
        self.assertEqual(len(out["true"]["absent"]), 1)
        self.assertEqual(sum(len(out["false"][k]) for k in
                             ("kept", "downgraded", "wrong_type")), 0)

    def test_identity_indicators_old_run(self):
        # The -15 roster entry 'Brennan' carries alias "Dad": one entry
        # claiming the two distinct labelled entities Brennan and Dad.
        merges, splits = pv.identity_indicators(self.old, self.labels)
        self.assertTrue(any("Brennan Sorrengail" in m
                            and "Dad Sorrengail" in m for m in merges))

    def test_labelled_outcomes_new_run(self):
        # Hand-verified against the -16 artefact: only the Mira claim
        # survives in the preview's relationships; Brennan and Fen are
        # in dropped_relationships (no_evidence). General Sorrengail
        # and Dad count as never extracted *in the artefact* — their
        # claims passed validation in the run log but no family claim
        # for either pair appears in the preview's kept or dropped
        # lists, which is what the scorer reads.
        out = pv.score_labelled_pairs(self.new, self.labels)
        self.assertEqual(len(out["true"]["kept"]), 1)
        self.assertEqual(len(out["true"]["dropped"]), 2)
        self.assertEqual(len(out["true"]["absent"]), 2)
        self.assertEqual(sum(len(out["false"][k]) for k in
                             ("kept", "downgraded", "wrong_type")), 0)

    def test_identity_indicators_new_run(self):
        # Verified against the -16 roster itself: the 'Dad' entry
        # carries Brennan Sorrengail's name as a primary key while a
        # separate 'Brennan' entry exists (merge + split), and the
        # entries for Sawyer, Heaton, and Garrick Tavis all carry the
        # alias forms 'Jack' / 'Barlowe', claiming Jack Barlowe.
        merges, splits = pv.identity_indicators(self.new, self.labels)
        self.assertTrue(any("Brennan Sorrengail" in m
                            and "Dad Sorrengail" in m for m in merges))
        self.assertTrue(any("Brennan Sorrengail" in s for s in splits))
        self.assertTrue(any("Jack Barlowe" in s for s in splits))

    def test_empty_evidence_old_run(self):
        # Manual scoring of -15: the only evidence-less kept claims are
        # two non-family friend claims.
        fam, nonfam = pv.empty_evidence_rates(self.old)
        self.assertEqual(fam[0], 0)
        self.assertEqual(nonfam[0], 2)


if __name__ == "__main__":
    unittest.main()
