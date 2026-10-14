"""Option F slice 5: dashboard staged-set section in preview_variance.

The dashboard's legacy output is FROZEN: the staging section appears
only when at least one preview carries a staging marker, appended
after the existing lines. Additions only — the existing dashboard
tests are unmodified and must keep passing byte-for-byte.

Stdlib unittest only. Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "preview_variance.py")
_spec = importlib.util.spec_from_file_location("preview_variance",
                                               _MOD_PATH)
pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pv)


def _preview(**over):
    d = {"title": "Some Book", "author": "Some Author",
         "spice_level": 3, "tropes": [], "triggers": [],
         "characters": [], "minor_characters": [],
         "relationships": [], "chunks": 100,
         "trope_candidate_counts": {}}
    d.update(over)
    return d


def _staged(key, display, cls="trope", sightings=1):
    return {"key": key, "display": display, "class": cls,
            "sightings": sightings}


def _promoted(key, display, via="persistence", sightings=2):
    return {"key": key, "display": display, "via": via,
            "sightings": sightings}


class TestDashboardStagingSection(unittest.TestCase):
    def test_no_markers_no_staging_section(self):
        lines = pv.compare_dashboard(_preview(), _preview())
        self.assertFalse(any("Staging" in ln for ln in lines))

    def test_markerless_output_is_the_legacy_block(self):
        # The section helper itself returns nothing without markers.
        self.assertEqual(pv.staging_section(_preview(), _preview()),
                         [])

    def test_stable_new_promoted_released(self):
        old = _preview(staging={
            "staged": [_staged("trope:name:alpha", "Alpha"),
                       _staged("trope:name:beta", "Beta"),
                       _staged("trope:name:gamma", "Gamma")],
            "promoted": [], "trapped_total": 3})
        new = _preview(staging={
            "staged": [_staged("trope:name:alpha", "Alpha", sightings=2),
                       _staged("trope:name:delta", "Delta")],
            "promoted": [_promoted("trope:name:beta", "Beta")],
            "trapped_total": 2})
        sec = "\n".join(pv.staging_section(old, new))
        self.assertIn("stable traps (held in both runs): 1", sec)
        self.assertIn("Alpha", sec)
        self.assertIn("newly staged (new run): 1", sec)
        self.assertIn("Delta", sec)
        self.assertIn("promoted between runs: 1", sec)
        # "promoted" is always paired with its via.
        self.assertIn("Beta (via persistence)", sec)
        self.assertIn("released (held in old, absent in new): 1", sec)
        self.assertIn("Gamma", sec)
        # The word "verified" appears nowhere in the section.
        self.assertNotIn("verifi", sec.lower())

    def test_dashboard_appends_section_after_legacy_lines(self):
        old = _preview(staging={"staged": [], "promoted": []})
        new = _preview(staging={"staged": [], "promoted": []})
        lines = pv.compare_dashboard(old, new)
        churn = [i for i, ln in enumerate(lines)
                 if ln.startswith("  Queue churn:")]
        staging = [i for i, ln in enumerate(lines)
                   if "Staging gate:" in ln]
        self.assertTrue(churn and staging)
        self.assertGreater(staging[0], churn[0])

    def test_summary_gated_share(self):
        # 100 chunks -> auto_min 5; 3 of 4 candidates below the band.
        d = _preview(chunks=100, trope_candidate_counts={
            "a": 2, "b": 3, "c": 4, "d": 9})
        self.assertEqual(pv.summary_gated_share(d),
                         "3/4 (75% below the auto band)")

    def test_summary_gated_share_na_without_counts(self):
        self.assertTrue(pv.summary_gated_share(
            _preview(trope_candidate_counts={})).startswith("n/a"))
        d = _preview()
        del d["trope_candidate_counts"]
        self.assertTrue(pv.summary_gated_share(d).startswith("n/a"))


if __name__ == "__main__":
    unittest.main()
