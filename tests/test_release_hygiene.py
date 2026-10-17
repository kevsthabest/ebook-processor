"""Release-hygiene tests: the README and the shipped defaults must
not drift from the code they describe.

The README version line is hand-maintained and drifted eleven
releases behind before an external review caught it (it read
2.13.5 at the 2.13.16 review, and two of its stale sentences —
relationships "no longer written", family-only validation —
actively misled that review). These tests make that class of
drift fail the suite instead of reaching a reader.

The pipeline default is pinned for the mirror-image reason: the
quality gates all measure v2, so the default must BE v2 — a run
that forgets the flag must not silently take the legacy path the
gates never measure (Decision, 2026-10-17: D2 default flip).
"""
import importlib.util
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PP_PATH = _ROOT / "process-portable.py"
_spec = importlib.util.spec_from_file_location("pp_release_hygiene",
                                               _PP_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class ReadmeVersionTests(unittest.TestCase):
    def test_readme_version_matches_portable_version(self):
        readme = (_ROOT / "README.md").read_text(encoding="utf-8")
        m = re.search(r"Current version: (\d+\.\d+\.\d+)", readme)
        self.assertIsNotNone(m, "README has no 'Current version:' line")
        self.assertEqual(m.group(1), pp.PORTABLE_VERSION)


class DefaultPipelineTests(unittest.TestCase):
    def test_default_pipeline_is_v2(self):
        self.assertEqual(pp.DEFAULT_CONFIG["pipeline"], "v2")

    def test_example_config_pipeline_matches_default(self):
        # The example file is what a fresh setup copies into
        # config.json, and config.json overrides the code default —
        # if the two disagree, new installs silently run the
        # pipeline the default was flipped away from.
        import json
        example = json.loads(
            (_ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["pipeline"],
                         pp.DEFAULT_CONFIG["pipeline"])


if __name__ == "__main__":
    unittest.main()
