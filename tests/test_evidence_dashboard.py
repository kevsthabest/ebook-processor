"""Option G slice 5: dashboard trope-evidence section.

The dashboard's legacy output is FROZEN: the section appears only
when the new preview carries an evidence_retrieval marker, appended
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


_MARKER = {
    "method": "targeted-v1",
    "claims": {"betrayal": "supported-strong",
               "dragons": "abstained",
               "found family": "adjacent-only",
               "trigger:arson": "supported-single"},
    "totals": {"attempted": 4, "supported_strong": 1,
               "supported_single": 0, "adjacent_only": 1,
               "abstained": 1, "failed": 0, "skipped": 0},
}


class TestDashboardTropeEvidenceSection(unittest.TestCase):
    def test_no_marker_no_section(self):
        lines = pv.compare_dashboard(_preview(), _preview())
        self.assertFalse(any("Trope evidence" in ln for ln in lines))
        self.assertEqual(
            pv.trope_evidence_section(_preview(), _preview()), [])

    def test_section_reads_the_new_runs_marker(self):
        new = _preview(
            tropes=["Betrayal", "Dragons", "Found Family"],
            evidence_retrieval=_MARKER)
        lines = pv.trope_evidence_section(_preview(), new)
        text = "\n".join(lines)
        self.assertIn("Trope evidence (option G):", text)
        # 1 of 3 shipped tropes has passage support; the trigger
        # key in the marker must not pollute the trope counts.
        self.assertIn("shipped with passage support: 1/3 (33%)",
                      text)
        self.assertIn("zero-support shipped", text)
        self.assertIn("- Dragons", text)
        self.assertIn("abstentions (all trope claims): 1; "
                      "adjacent-only: 1", text)

    def test_section_absent_when_only_the_old_run_has_a_marker(self):
        old = _preview(evidence_retrieval=_MARKER)
        self.assertEqual(pv.trope_evidence_section(old, _preview()),
                         [])

    def test_failed_and_recording_lines(self):
        marker = dict(_MARKER, failed=True, evidence_recorded=2,
                      promotes_next=["Betrayal"])
        new = _preview(tropes=["Betrayal"],
                       evidence_retrieval=marker)
        text = "\n".join(pv.trope_evidence_section(_preview(), new))
        self.assertIn("retrieval FAILED", text)
        self.assertIn("staging evidence recorded: 2 staged "
                      "claim(s)", text)

    def test_hyphenated_names_meet_across_normalizers(self):
        # Marker keys are the pipeline's _tnorm keys (hyphens kept);
        # the dashboard's norm() strips punctuation. Both sides go
        # through norm(), so a hyphenated trope still matches.
        marker = {"claims": {"enemies-to-lovers": "supported-strong"}}
        new = _preview(tropes=["Enemies-to-Lovers"],
                       evidence_retrieval=marker)
        text = "\n".join(pv.trope_evidence_section(_preview(), new))
        self.assertIn("shipped with passage support: 1/1 (100%)",
                      text)


if __name__ == "__main__":
    unittest.main()
