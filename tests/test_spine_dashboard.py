"""Measurement spine, slice 4: the dashboard's two numbers
(spine-design §3.4.4).

replay_variance_section(previews) renders, for a set of previews
of one book:
- pipeline-delta: a replay preview scored against the CAPTURE
  (baseline) preview of the same snapshot — the same extraction
  re-derived, so every difference is pipeline/decision-draw, and
- draw-variance: across >=2 fresh previews of a book that has a
  snapshot group in the set.
A replay whose capture baseline is absent prints the refusal
reason and is NEVER scored as draw-variance. Marker-less previews
render neither (additions only — legacy output byte-identical).
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


def _preview(tropes, chars, **extra):
    d = {"title": "Daemon", "author": "Daniel Suarez",
         "tropes": [{"name": t} for t in tropes],
         "trope_confidence": {},
         "triggers": [],
         "characters": [{"name": c, "entity_id": f"ent_{i:04d}",
                         "frequency": 0.5}
                        for i, c in enumerate(chars, 1)],
         "relationships": [], "povs": [], "spice_level": 3}
    d.update(extra)
    return d


class TestReplayVarianceSection(unittest.TestCase):
    def test_markerless_set_renders_nothing(self):
        a = _preview(["a"], ["X"])
        b = _preview(["a", "b"], ["X", "Y"])
        self.assertEqual(pv.replay_variance_section([a, b]), [])

    def test_legacy_output_byte_identical(self):
        a = _preview(["a"], ["X"])
        b = _preview(["a", "b"], ["X", "Y"])
        lines = pv.compare_previews(a, b)
        self.assertFalse(any("Replay variance" in ln for ln in lines))

    def test_pipeline_delta_against_capture_baseline(self):
        base = _preview(["a", "b"], ["X", "Y"],
                        snapshot_captured="snap-1")
        replay = _preview(
            ["a"], ["X"],
            replay={"snapshot_id": "snap-1",
                    "source_code_sha": "aaa",
                    "source_config_digest": "d1",
                    "replay_code_sha": "bbb",
                    "replay_config_digest": "d1"})
        lines = pv.replay_variance_section([base, replay])
        text = "\n".join(lines)
        self.assertIn("pipeline-delta", text)
        self.assertIn("snap-1", text)
        self.assertIn("aaa -> bbb", text)
        # Trope Jaccard {a,b} vs {a} = 0.5.
        self.assertIn("0.500", text)

    def test_replay_without_baseline_refuses_not_draw(self):
        fresh = _preview(["a"], ["X"])
        replay = _preview(
            ["a"], ["X"],
            replay={"snapshot_id": "snap-9",
                    "source_code_sha": "aaa",
                    "source_config_digest": "d1",
                    "replay_code_sha": "bbb",
                    "replay_config_digest": "d1"})
        lines = pv.replay_variance_section([fresh, replay])
        text = "\n".join(lines)
        self.assertIn("snap-9", text)
        self.assertIn("not in this set", text)
        self.assertFalse(any("draw-variance (" in ln for ln in lines))
        self.assertFalse(any("pipeline-delta (" in ln for ln in lines))

    def test_draw_variance_across_fresh_previews(self):
        cap = _preview(["a", "b"], ["X", "Y"],
                       snapshot_captured="snap-1")
        fresh = _preview(["a"], ["X", "Z"])
        lines = pv.replay_variance_section([cap, fresh])
        text = "\n".join(lines)
        self.assertIn("draw-variance", text)
        self.assertNotIn("pipeline-delta", text)


if __name__ == "__main__":
    unittest.main()
