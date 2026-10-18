"""Regression tests for the tier3_store alerts-and-errors batch
(issues #21, #22, #25, #26, plus rider #58 from batch B's gate).

One family: the alerts digest and the stored-names fetch trusted
recorded shapes and transport behaviour more than their own error
contracts allow, so a malformed artefact or a dropped connection
escaped as a traceback instead of the tool's named one-line error
or its honest unscored treatment. Each test names its issue and
pins the behaviour the issue's reproduction violated.

Hermetic: synthetic artefacts, temp SQLite stores, and a stubbed
urlopen for the Supabase stored-set path (no network).

Stdlib unittest only.
"""
import http.client
import importlib.util
import io
import json
import sqlite3
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


store = _load("tier3_store_alerts_errors", "tier3_store.py")


def _char(name, appearances=3):
    return {
        "name": name, "aliases": [], "unresolved_mentions": [],
        "role": "minor", "description": f"About {name}.",
        "appearances": appearances, "chapters_present": 2,
        "grounded": True, "evidence": f"{name} appeared.",
    }


def _artefact(**over):
    art = {
        "title": "Alerts Errors Book", "author": "An Author",
        "work_id": "work-alerts-errors-1",
        "characters": [_char("Principal A", appearances=40)],
        "minor_characters": [_char("Minor B", appearances=5)],
        "relationships": [],
        "minor_sourced_relationships": [],
        "dropped_relationships": [],
        "possible_merges": [],
    }
    art.update(over)
    return art


class AlertsErrorsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "tier3.db")
        self.missing_config = str(self.dir / "no-config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def import_it(self, art, config_path=None):
        path = self.dir / "book.json"
        path.write_text(json.dumps(art), encoding="utf-8")
        return store.import_preview(
            self.db, str(path),
            config_path=config_path or self.missing_config)

    def payloads(self, kind, work_id="work-alerts-errors-1"):
        conn = sqlite3.connect(self.db)
        try:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM tier3_candidates "
                "WHERE work_id = ? AND kind = ?",
                (work_id, kind))]
        finally:
            conn.close()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = store.main(argv)
        return code, out.getvalue(), err.getvalue()

    def supabase_config(self):
        cfg = self.dir / "config.json"
        cfg.write_text(json.dumps({
            "supabase_url": "https://example.invalid",
            "supabase_key": "test-key"}), encoding="utf-8")
        return str(cfg)


class StringScoreTests(AlertsErrorsCase):
    """Issue #21: a string-typed p_yes imports, then crashes
    alerts on the str-to-float comparison. The pipeline's own
    contract is numeric-or-None — process-portable's score
    reader validates a score as int/float, rejects anything
    non-numeric, and returns float or None, and the merges it
    writes carry round(score, 4) or None — so a string score is
    outside the artefact contract entirely. The pinned
    treatment is therefore UNSCORED (the digest's treatment of
    a missing score), even for a readable string like "0.72":
    coercing it would present a malformed record as a judge
    score. The same treatment covers a string validation_p,
    which reaches the digest through the claim bands."""

    def test_numeric_string_p_yes_is_unscored_not_a_crash(self):
        self.import_it(_artefact(possible_merges=[
            {"name_a": "Minor B", "name_b": "Minor Bee",
             "p_yes": "0.72", "reason": "below threshold (0.85)"},
            {"name_a": "Principal A", "name_b": "Principal Ay",
             "p_yes": 0.5, "reason": "below threshold (0.85)"},
        ]))
        alerts = store.collect_alerts(self.db)
        self.assertEqual([a["score"] for a in alerts], [0.5])
        text = store.build_alerts(self.db)
        self.assertIn("0.50", text)
        self.assertNotIn("Minor Bee", text)

    def test_unparseable_string_p_yes_is_unscored(self):
        self.import_it(_artefact(possible_merges=[
            {"name_a": "Minor B", "name_b": "Minor Bee",
             "p_yes": "high", "reason": "below threshold (0.85)"},
        ]))
        self.assertEqual(store.collect_alerts(self.db), [])

    def test_string_validation_p_is_unscored_not_a_crash(self):
        claim = {"from": "Minor B", "to": "Principal A",
                 "type": "sibling", "evidence": "A quote.",
                 "chapter": "Chapter 1", "source": "extracted",
                 "validation_state": "rejected",
                 "validation_p": "0.42",
                 "validation_note": "dropped"}
        self.import_it(_artefact(dropped_relationships=[claim]))
        self.assertEqual(store.collect_alerts(self.db), [])


class DroppedConnectionTests(AlertsErrorsCase):
    """Issue #22: a connection that drops mid-request raises
    http.client.RemoteDisconnected — a ConnectionResetError,
    not a URLError — which escaped the stored-names fetch's
    catch list instead of becoming the fail-closed Tier3Error.
    Every transport failure lands in the same contract, and a
    failed import writes nothing."""

    def assert_transport_fails_closed(self, exc):
        cfg = self.supabase_config()

        def boom(req, timeout=None):
            raise exc

        with mock.patch.object(store.urllib.request, "urlopen",
                               boom):
            with self.assertRaises(store.Tier3Error):
                self.import_it(_artefact(), config_path=cfg)
        # Fail closed means fail BEFORE any store rows exist:
        # the database file itself was never created.
        self.assertFalse(Path(self.db).exists())

    def test_remote_disconnected_is_a_tier3_error(self):
        self.assert_transport_fails_closed(
            http.client.RemoteDisconnected(
                "Remote end closed connection without response"))

    def test_connection_reset_is_a_tier3_error(self):
        self.assert_transport_fails_closed(
            ConnectionResetError("connection reset by peer"))


class NonDatabaseFileTests(AlertsErrorsCase):
    """Issue #25: alerts pointed at a file that exists but is
    not a SQLite database raised sqlite3.DatabaseError instead
    of the named Tier3Error. Read paths validate what they
    opened; the message names the path and that the file is
    not a Tier 3 store. A missing path keeps its established
    behaviour: alerts/list raise the no-store Tier3Error, and
    report answers with its NOT IMPORTED verdict."""

    def garbage_db(self):
        path = self.dir / "garbage.db"
        path.write_text("not a database", encoding="utf-8")
        return str(path)

    def test_alerts_on_a_text_file_is_a_named_tier3_error(self):
        bad = self.garbage_db()
        with self.assertRaises(store.Tier3Error) as cm:
            store.collect_alerts(bad)
        self.assertIn("not a Tier 3 store", str(cm.exception))
        self.assertIn(bad, str(cm.exception))

    def test_alerts_cli_on_a_text_file_has_no_traceback(self):
        bad = self.garbage_db()
        code, _out, err = self.run_cli(["alerts", "--db", bad])
        self.assertEqual(code, 2)
        self.assertIn("not a Tier 3 store", err)
        self.assertNotIn("Traceback", err)

    def test_report_on_a_text_file_is_a_named_tier3_error(self):
        with self.assertRaises(store.Tier3Error):
            store.build_report(self.garbage_db())

    def test_missing_path_behaviour_is_unchanged(self):
        missing = str(self.dir / "nope.db")
        with self.assertRaises(store.Tier3Error):
            store.collect_alerts(missing)
        self.assertIn("NOT IMPORTED", store.build_report(missing))


class MissingMergeNameRenderTests(AlertsErrorsCase):
    """Issue #26: a merge candidate missing one side rendered
    as the literal pair 'None <-> Hero' — Python's null leaking
    into a human-facing label. The rendering names the present
    side and marks the missing side explicitly; what is stored
    does not change."""

    def test_missing_name_b_renders_unnamed_not_none(self):
        self.import_it(_artefact(possible_merges=[
            {"name_a": "Hero", "p_yes": 0.72,
             "reason": "left_for_review"},
        ]))
        alerts = store.collect_alerts(self.db)
        self.assertEqual(len(alerts), 1)
        label = alerts[0]["label"]
        self.assertIn("Hero", label)
        self.assertIn("(unnamed)", label)
        self.assertNotIn("None", label)
        self.assertNotIn("None", store.build_alerts(self.db))

    def test_missing_name_a_renders_unnamed_not_none(self):
        self.import_it(_artefact(possible_merges=[
            {"name_b": "Hero", "p_yes": 0.72,
             "reason": "left_for_review"},
        ]))
        alerts = store.collect_alerts(self.db)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Hero", alerts[0]["label"])
        self.assertIn("(unnamed)", alerts[0]["label"])
        self.assertNotIn("None", alerts[0]["label"])


class NonStringMergeNameImportTests(AlertsErrorsCase):
    """Rider #58 (from batch B's gate): the merge dedupe ran
    tuple(sorted((name_a, name_b))) whenever both names were
    truthy, so a truthy non-string name raised TypeError out of
    the import. Dedupe applies only when both names are strings;
    a non-string-named entry is yielded as recorded, mirroring
    the missing-name treatment, so the import completes and the
    row stays inspectable."""

    def test_numeric_name_a_imports_as_recorded(self):
        self.import_it(_artefact(possible_merges=[
            {"name_a": 5, "name_b": "Hero", "p_yes": 0.72,
             "reason": "below threshold (0.85)"},
        ]))
        payloads = self.payloads("merge")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["name_a"], 5)
        self.assertEqual(payloads[0]["name_b"], "Hero")

    def test_list_name_a_imports_as_recorded(self):
        self.import_it(_artefact(possible_merges=[
            {"name_a": ["Hero"], "name_b": "Hero", "p_yes": 0.72,
             "reason": "below threshold (0.85)"},
        ]))
        payloads = self.payloads("merge")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["name_a"], ["Hero"])


if __name__ == "__main__":
    unittest.main()
