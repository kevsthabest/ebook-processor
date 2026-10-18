"""Regression tests for the tier3_store import-robustness batch
(issues #16, #20, #23, #24).

One family: the candidate generator trusted the artefact's
nested shapes and left duplicate or lossy rows behind, so the
store's counts disagreed with its own surfaces or a malformed
artefact escaped as a traceback instead of the tool's named
one-line error. Each test names its issue and pins the
behaviour the issue's reproduction violated.

Hermetic: synthetic artefacts and SQLite stores in temp dirs,
no network. Stdlib unittest only.
"""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store = _load("tier3_store_import_robustness", "tier3_store.py")


def _char(name, appearances=3, mentions=None):
    return {
        "name": name, "aliases": [],
        "unresolved_mentions": [] if mentions is None else mentions,
        "role": "minor", "description": f"About {name}.",
        "appearances": appearances, "chapters_present": 2,
        "grounded": True, "evidence": f"{name} appeared.",
    }


def _claim(frm, to, ctype="boss"):
    return {"from": frm, "to": to, "type": ctype,
            "evidence": "A quote.", "chapter": "Chapter 1",
            "source": "extracted"}


def _artefact(**over):
    art = {
        "title": "Robustness Book", "author": "An Author",
        "work_id": "work-robust-1",
        "characters": [_char("Principal A", appearances=40)],
        "minor_characters": [_char("Minor B", appearances=5)],
        "relationships": [],
        "minor_sourced_relationships": [],
        "dropped_relationships": [],
        "possible_merges": [],
    }
    art.update(over)
    return art


def _boss_chapters(*chnums):
    return {"chapters": {
        ch: {"a": {"relationships": [
            _claim("Minor B", "Principal A", "boss")]}}
        for ch in chnums}}


class RobustnessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "tier3.db")
        self.missing_config = str(self.dir / "no-config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def import_it(self, art):
        path = self.dir / "book.json"
        path.write_text(json.dumps(art), encoding="utf-8")
        return store.import_preview(
            self.db, str(path), config_path=self.missing_config)

    def payloads(self, kind, work_id="work-robust-1"):
        conn = sqlite3.connect(self.db)
        try:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM tier3_candidates "
                "WHERE work_id = ? AND kind = ?",
                (work_id, kind))]
        finally:
            conn.close()

    def table_count(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()


class BareStringMentionTests(RobustnessCase):
    """Issue #16: the pipeline serializes unresolved_mentions
    as a list of free-text strings, but a hand-edited or older
    artefact can carry a bare string. Iterating that string
    yields one candidate per character — nonsense rows that
    look like real mentions."""

    def test_bare_string_is_one_mention(self):
        art = _artefact()
        art["characters"] = [
            _char("Principal A", appearances=40,
                  mentions="the stranger")]
        result = self.import_it(art)
        payloads = self.payloads("mention")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["mention"], "the stranger")
        self.assertEqual(payloads[0]["character"], "Principal A")
        self.assertEqual(result["candidates"].get("mention"), 1)

    def test_non_list_non_string_mentions_fail_named(self):
        art = _artefact()
        art["characters"] = [
            _char("Principal A", appearances=40,
                  mentions={"text": "the stranger"})]
        with self.assertRaises(store.Tier3Error) as cm:
            self.import_it(art)
        self.assertIn("unresolved_mentions", str(cm.exception))
        self.assertIn("Principal A", str(cm.exception))


class MalformedChapterDataTests(RobustnessCase):
    """Issue #20: chapter_data (or a level inside it) arriving
    as a list crashed the import with an AttributeError
    traceback, because main catches only Tier3Error. Malformed
    nested shapes are a named one-line error, and the failed
    import leaves the store untouched."""

    def assert_clean_failure(self, art):
        with self.assertRaises(store.Tier3Error) as cm:
            self.import_it(art)
        self.assertIn("chapter_data", str(cm.exception))
        self.assertEqual(self.table_count("tier3_books"), 0)
        self.assertEqual(self.table_count("tier3_candidates"), 0)

    def test_chapter_data_as_list(self):
        self.assert_clean_failure(
            _artefact(chapter_data=["not", "an", "object"]))

    def test_chapters_as_list(self):
        self.assert_clean_failure(
            _artefact(chapter_data={"chapters": ["1", "2"]}))

    def test_chapter_entry_as_list(self):
        self.assert_clean_failure(_artefact(chapter_data={
            "chapters": {"1": ["not", "an", "object"]}}))


class DuplicateCandidateTests(RobustnessCase):
    """Issue #23: the merge and mention passes never deduped,
    unlike the claim passes. The same pair in both orders (or
    the same mention twice) inserted two rows, so list/report
    counts disagreed with the alerts digest, which dedupes
    merges by sorted pair."""

    def test_duplicate_mention_and_reversed_merge_collapse(self):
        art = _artefact()
        art["characters"] = [
            _char("Principal A", appearances=40,
                  mentions=["the stranger", "the stranger"])]
        art["possible_merges"] = [
            {"name_a": "Hero", "name_b": "Hero H.", "p_yes": 0.72,
             "reason": "below threshold (0.85)"},
            {"name_a": "Hero H.", "name_b": "Hero", "p_yes": 0.72,
             "reason": "below threshold (0.85)"},
        ]
        result = self.import_it(art)
        self.assertEqual(len(self.payloads("mention")), 1)
        self.assertEqual(len(self.payloads("merge")), 1)
        self.assertEqual(result["candidates"].get("mention"), 1)
        self.assertEqual(result["candidates"].get("merge"), 1)

    def test_merge_without_both_names_keeps_current_behaviour(self):
        # An entry missing a name cannot form the sorted-pair
        # key; the fix dedupes only when both names are present
        # and does not invent a rejection for it.
        art = _artefact()
        art["possible_merges"] = [
            {"name_a": "Hero", "p_yes": 0.5,
             "reason": "below threshold (0.85)"},
            {"name_a": "Hero", "p_yes": 0.5,
             "reason": "below threshold (0.85)"},
        ]
        self.import_it(art)
        self.assertEqual(len(self.payloads("merge")), 2)


class RepeatTypeProposalTests(RobustnessCase):
    """Issue #24: a type proposed for the same pair in several
    chapters collapsed to one row recording only the first
    chapter, throwing away the recurrence — and recurrence is
    signal: the rejected-types handling at insert already
    treats a re-proposed string's occurrence as evidence."""

    def test_repeat_proposal_records_every_occurrence(self):
        art = _artefact(chapter_data=_boss_chapters("3", "7"))
        self.import_it(art)
        payloads = self.payloads("type_proposal")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["type"], "boss")
        # chapter_number keeps its first-occurrence meaning.
        self.assertEqual(payloads[0]["chapter_number"], "3")
        self.assertEqual(payloads[0]["occurrences"], 2)
        self.assertEqual(payloads[0]["chapters"], ["3", "7"])

    def test_single_proposal_records_one_occurrence(self):
        art = _artefact(chapter_data=_boss_chapters("3"))
        self.import_it(art)
        payloads = self.payloads("type_proposal")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["occurrences"], 1)
        self.assertEqual(payloads[0]["chapters"], ["3"])

    def test_rejected_repeat_is_one_annotated_row(self):
        # Seed the rejected-types record as the subcommand
        # would, then propose the same string in two chapters.
        conn = store._connect(self.db)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO rejected_types (type, reason, "
                    "decided_at) VALUES (?,?,?)",
                    ("boss", "not a relationship", "2026-10-18"))
        finally:
            conn.close()
        art = _artefact(chapter_data=_boss_chapters("3", "7"))
        result = self.import_it(art)
        payloads = self.payloads("type_proposal")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["occurrences"], 2)
        self.assertEqual(payloads[0]["chapters"], ["3", "7"])
        hit = payloads[0]["previously_rejected"]
        self.assertEqual(hit["reason"], "not a relationship")
        self.assertEqual(result["previously_rejected_types"],
                         ["boss"])
        # The list surface still counts previously rejected
        # proposals apart from fresh ones (the count adjustment
        # in _book_lines subtracts exactly this one row).
        self.assertEqual(result["candidates"].get("type_proposal"),
                         1)


if __name__ == "__main__":
    unittest.main()
