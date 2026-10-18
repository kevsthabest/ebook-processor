"""Regression tests for issues #37 and #38: a corrupt state file
must fail loud, and state saves must be atomic.

Both standalone tools accumulated hand-edited / curated state in a
JSON file and, on a parse failure, treated that file as empty and
then overwrote it — one torn write or one stray edit destroyed the
accumulated labels, the harvest manifest, and the deliberate
Daemon + Fourth Wing holdout split assignments, silently. These
tests pin the replacement discipline, mirrored from tier3_store:
an existing-but-unparseable file is a loud stop that writes
nothing, a missing file is the normal fresh start, a load never
writes, and a save goes through a temp file plus os.replace so a
crash mid-save leaves the previous good file intact.

Hermetic: temp dirs only, no network. Stdlib unittest only.
"""
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lm = _load("label_merges_corrupt", "label-merges.py")
harvest = _load("training_harvest_corrupt", "training_harvest.py")

CORRUPT = b"{ this is not json at all"


class _FailingTempFile:
    """A temp file whose write dies immediately — the stand-in for
    a crash mid-save. The name points inside the target directory
    so the atomic helper's cleanup path is exercised for real."""

    def __init__(self, *args, **kwargs):
        self.name = str(Path(kwargs.get("dir", ".")) / "failing.tmp")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data):
        raise OSError("simulated crash mid-write")

    def flush(self):
        pass

    def fileno(self):
        raise OSError("simulated crash mid-write")


def _fail_temp_writes():
    # Both tools resolve tempfile.NamedTemporaryFile at call time,
    # so patching the stdlib module attribute reaches both helpers.
    return mock.patch.object(
        tempfile, "NamedTemporaryFile", _FailingTempFile)


# ----------------------------------------------------------------------
# label-merges.py (#37)
# ----------------------------------------------------------------------

class LabelMergesCorruptStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.labels = self.td / "merge-labels.json"
        self.preview = self.td / "preview.json"
        self.preview.write_text(
            json.dumps({"characters": []}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_labels_raise_and_leave_bytes_unchanged(self):
        self.labels.write_bytes(CORRUPT)
        with self.assertRaises(lm.LabelError):
            lm._load_merges(self.labels)
        self.assertEqual(self.labels.read_bytes(), CORRUPT)

    def test_corrupt_labels_via_main_exit_nonzero_and_no_overwrite(self):
        self.labels.write_bytes(CORRUPT)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = lm.main(["--json", str(self.preview),
                          "--output", str(self.labels)])
        self.assertEqual(rc, 2)
        self.assertIn(str(self.labels), err.getvalue())
        self.assertEqual(self.labels.read_bytes(), CORRUPT)

    def test_non_object_labels_raise(self):
        # Valid JSON, wrong shape: still curated state in a form
        # the tool cannot use, so it gets the same loud stop.
        self.labels.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(lm.LabelError):
            lm._load_merges(self.labels)
        self.assertEqual(self.labels.read_text(encoding="utf-8"),
                         "[1, 2]")

    def test_missing_labels_are_a_fresh_start(self):
        self.assertEqual(lm._load_merges(self.labels), {})
        self.assertFalse(self.labels.exists())
        with redirect_stdout(io.StringIO()):
            rc = lm.main(["--json", str(self.preview),
                          "--output", str(self.labels)])
        self.assertEqual(rc, 0)

    def test_save_replaces_existing_labels(self):
        self.labels.write_text(json.dumps({"a|||b": {"same_person": True}}),
                               encoding="utf-8")
        lm._save_merges(self.labels, {"c|||d": {"same_person": False}})
        self.assertEqual(json.loads(self.labels.read_text(encoding="utf-8")),
                         {"c|||d": {"same_person": False}})

    def test_interrupted_save_leaves_original_intact(self):
        original = json.dumps({"a|||b": {"same_person": True}}).encode()
        self.labels.write_bytes(original)
        with _fail_temp_writes():
            with self.assertRaises(OSError):
                lm._save_merges(self.labels, {"c|||d": {}})
        self.assertEqual(self.labels.read_bytes(), original)
        self.assertEqual(
            sorted(p.name for p in self.td.iterdir()),
            ["merge-labels.json", "preview.json"])


# ----------------------------------------------------------------------
# training_harvest.py (#38)
# ----------------------------------------------------------------------

class HarvestCorruptStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.store = self.td / "store"
        self.paths = harvest._ensure_store(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    # -- manifest -------------------------------------------------

    def test_corrupt_manifest_raises_and_leaves_bytes_unchanged(self):
        self.paths["manifest"].write_bytes(CORRUPT)
        with self.assertRaises(harvest.HarvestError):
            harvest._load_manifest(self.paths)
        self.assertEqual(self.paths["manifest"].read_bytes(), CORRUPT)

    def test_corrupt_manifest_via_archive_command_writes_nothing(self):
        self.paths["manifest"].write_bytes(CORRUPT)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = harvest.main([
                "archive", "--store", str(self.store),
                "--preview-dir", str(self.td / "no-previews"),
                "--run-events-dir", str(self.td / "no-events")])
        self.assertEqual(rc, 2)
        self.assertIn(str(self.paths["manifest"]), err.getvalue())
        self.assertEqual(self.paths["manifest"].read_bytes(), CORRUPT)

    def test_missing_manifest_is_a_fresh_start_and_load_writes_nothing(self):
        manifest = harvest._load_manifest(self.paths)
        self.assertEqual(manifest["archives"], [])
        self.assertEqual(manifest["exports"], [])
        self.assertFalse(self.paths["manifest"].exists())

    def test_interrupted_manifest_save_leaves_original_intact(self):
        original = json.dumps({"version": 1, "archives": [],
                               "exports": []}).encode()
        self.paths["manifest"].write_bytes(original)
        with _fail_temp_writes():
            with self.assertRaises(OSError):
                harvest._save_manifest(
                    self.paths, {"version": 1, "archives": [],
                                 "exports": []})
        self.assertEqual(self.paths["manifest"].read_bytes(), original)
        self.assertEqual(
            [p.name for p in self.store.iterdir()
             if p.suffix == ".tmp"], [])

    # -- split map --------------------------------------------------

    def test_corrupt_split_map_raises_and_leaves_bytes_unchanged(self):
        self.paths["split_map"].write_bytes(CORRUPT)
        with self.assertRaises(harvest.HarvestError):
            harvest._load_split_map(self.paths)
        self.assertEqual(self.paths["split_map"].read_bytes(), CORRUPT)

    def test_non_object_split_map_raises_and_is_not_reseeded(self):
        self.paths["split_map"].write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(harvest.HarvestError):
            harvest._load_split_map(self.paths)
        self.assertEqual(
            self.paths["split_map"].read_text(encoding="utf-8"),
            "[1, 2]")

    def test_missing_split_map_derives_in_memory_and_writes_nothing(self):
        split = harvest._load_split_map(self.paths)
        self.assertEqual(split, {"Daemon": "holdout",
                                 "Fourth Wing": "holdout"})
        self.assertFalse(self.paths["split_map"].exists())

    def test_interrupted_split_save_leaves_original_intact(self):
        original = json.dumps({"Daemon": "holdout"}).encode()
        self.paths["split_map"].write_bytes(original)
        with _fail_temp_writes():
            with self.assertRaises(OSError):
                harvest._save_split_map(
                    self.paths, {"Daemon": "train"})
        self.assertEqual(self.paths["split_map"].read_bytes(), original)

    def test_report_does_not_create_state_files(self):
        # A report is a read-only command: deriving the split in
        # memory must not persist either state file as a side effect.
        with redirect_stdout(io.StringIO()):
            rc = harvest.main(["report", "--store", str(self.store)])
        self.assertEqual(rc, 0)
        self.assertFalse(self.paths["split_map"].exists())
        self.assertFalse(self.paths["manifest"].exists())

    def test_saving_command_persists_seeded_split_map(self):
        # The seed reaches disk only when a command actually saves
        # state — archive is such a command, so after it runs the
        # operator has a real file to edit deliberately.
        with redirect_stdout(io.StringIO()):
            rc = harvest.main([
                "archive", "--store", str(self.store),
                "--preview-dir", str(self.td / "no-previews"),
                "--run-events-dir", str(self.td / "no-events")])
        self.assertEqual(rc, 0)
        self.assertTrue(self.paths["split_map"].exists())
        self.assertEqual(
            json.loads(self.paths["split_map"].read_text(encoding="utf-8")),
            {"Daemon": "holdout", "Fourth Wing": "holdout"})


if __name__ == "__main__":
    unittest.main()
