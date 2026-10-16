"""Cheap wins batch tests.

Item 1 — diminutive table (diminutives.json): candidate generation
ONLY. A diminutive link proposes a pair to the Tier 3 ladder; it
never merges, never scores a deterministic tier, and never bypasses
a veto (Tier 1 cannot-link, surname-family trap).

Item 2 — evidence-floor hygiene at the row builders: a trope /
trigger claim with an empty support-chapter list AND no
chapter-provenanced evidence quotes is held (not written) with a
named report line; quote-backed claims ship. Split-file chapter
labels in evidence payloads are normalized to "Chapter N" via the
result's chapter map.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import unittest
from collections import Counter
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_JSON_PATH = Path(__file__).resolve().parent.parent / "diminutives.json"


def _mk_entry(name, chapters):
    nk = pp.norm_name(name)
    return {nk: {
        "name": name, "aliases": set(), "alias_keys": {nk},
        "primary_keys": {nk}, "appearances": 1,
        "chapters": set(chapters), "roles": Counter(),
        "descriptions": [(min(chapters), "d", False)], "evidence": "",
    }}


def _roster(*specs):
    roster = {}
    for name, chapters in specs:
        roster.update(_mk_entry(name, chapters))
    return roster


class TestDiminutiveTable(unittest.TestCase):
    def test_table_loads_and_round_trips(self):
        data = json.loads(_JSON_PATH.read_text(encoding="utf-8"))
        self.assertIn("pairs", data)
        pairs = data["pairs"]
        self.assertGreaterEqual(len(pairs), 300)
        for full, dims in pairs.items():
            self.assertIsInstance(full, str)
            self.assertTrue(full.isalpha() and full == full.lower())
            self.assertIsInstance(dims, list)
            for d in dims:
                self.assertIsInstance(d, str)
                self.assertTrue(d.isalpha() and d == d.lower())
        # Serializes back out cleanly (round-trip).
        json.dumps(data)

    def test_loader_map_covers_table_and_builtin(self):
        # Table-only witnesses (not in the built-in map).
        self.assertTrue(pp._diminutive_pair("peggy", "margaret"))
        self.assertTrue(pp._diminutive_pair("margaret", "peggy"))
        self.assertTrue(pp._diminutive_pair("meg", "margaret"))
        # The loader's map is a superset of the built-in pairs.
        for nick, full in pp._COMMON_NICKNAMES.items():
            self.assertTrue(pp._diminutive_pair(nick, full),
                            f"built-in pair lost: {nick}/{full}")

    def test_loader_falls_back_to_builtin_when_file_missing(self):
        saved_dir, saved_cache = pp.SCRIPT_DIR, pp._DIMINUTIVES_CACHE
        try:
            pp.SCRIPT_DIR = Path("/nonexistent-cheap-wins-dir")
            pp._DIMINUTIVES_CACHE = None
            # No crash; built-in pairs still resolve.
            self.assertTrue(pp._diminutive_pair("bill", "william"))
            # Table-only pairs are absent without the file.
            self.assertFalse(pp._diminutive_pair("peggy", "margaret"))
        finally:
            pp.SCRIPT_DIR = saved_dir
            pp._DIMINUTIVES_CACHE = saved_cache

    def test_true_negative_peter_petra(self):
        # Petra is a distinct name, not a diminutive of Peter.
        self.assertFalse(pp._diminutive_pair("peter", "petra"))
        self.assertFalse(pp._diminutive_pair("petra", "peter"))
        self.assertEqual(pp._merge_pair_signal("peter", "petra ragnar"), 0)
        roster = _roster(("Peter", [1]), ("Petra Ragnar", [2]))
        n, possible = pp.post_pass_merge(roster, validator=None)
        self.assertEqual(n, 0)
        self.assertEqual(possible, [])


class TestDiminutiveCandidates(unittest.TestCase):
    def test_signal_zero_token_overlap_pairs(self):
        # No shared token, no substring: signal exists ONLY via the
        # table. (Both returned 0 before this batch.)
        self.assertEqual(pp._merge_pair_signal("bill", "william smith"), 1)
        self.assertEqual(pp._merge_pair_signal("peggy", "margaret olson"), 1)

    def test_signal_guard_unrelated_surnames(self):
        # Diminutive-linked first names alone are not enough when
        # both sides carry unrelated extra tokens.
        self.assertEqual(pp._merge_pair_signal("peggy olson",
                                               "margaret smith"), 0)

    def test_table_creates_no_deterministic_tier(self):
        keys = {"bill", "william smith"}
        tier = pp._merge_tier("bill", "william smith",
                              a_chapters={1}, b_chapters={2},
                              roster_keys=keys)
        self.assertEqual(tier, "ambiguous")

    def test_candidate_surfaces_bill_william(self):
        roster = _roster(("Bill", [1]), ("William Smith", [2]))
        n, possible = pp.post_pass_merge(roster, validator=None)
        self.assertEqual(n, 0)  # proposed, never merged by the table
        self.assertEqual(len(possible), 1)
        pair = {pp.norm_name(possible[0]["name_a"]),
                pp.norm_name(possible[0]["name_b"])}
        self.assertEqual(pair, {"bill", "william smith"})
        self.assertEqual(possible[0]["reason"],
                         "unresolved (decision model disabled)")

    def test_candidate_surfaces_peggy_margaret(self):
        roster = _roster(("Peggy", [1]), ("Margaret Olson", [2]))
        n, possible = pp.post_pass_merge(roster, validator=None)
        self.assertEqual(n, 0)
        self.assertEqual(len(possible), 1)
        pair = {pp.norm_name(possible[0]["name_a"]),
                pp.norm_name(possible[0]["name_b"])}
        self.assertEqual(pair, {"peggy", "margaret olson"})

    def test_cannot_link_veto_holds(self):
        # Same-chapter co-listing is a Tier 1 hard NO: the diminutive
        # link must not surface the pair at all.
        roster = _roster(("Bill", [1, 2]), ("William Smith", [2, 3]))
        n, possible = pp.post_pass_merge(roster, validator=None)
        self.assertEqual(n, 0)
        self.assertEqual(possible, [])
        self.assertEqual(len(roster), 2)

    def test_surname_trap_veto_holds(self):
        # Same surname + table-only diminutive first names: the
        # surname-family trap is a hard NO the table never bypasses.
        roster = _roster(("Peggy Olson", [1]), ("Margaret Olson", [2]))
        n, possible = pp.post_pass_merge(roster, validator=None)
        self.assertEqual(n, 0)
        self.assertEqual(possible, [])
        self.assertEqual(len(roster), 2)


def _trope_rows(result):
    cat = {pp._tnorm(t): f"tid-{i}" for i, t in enumerate(result["tropes"])}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rows, unmatched, failed = pp._build_trope_rows(
            "work-1", result, cat, None, "model-x", set())
    return rows, unmatched, failed, buf.getvalue()


def _trigger_rows(result):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rows = pp._build_trigger_rows("work-1", result, "model-x", set())
    return rows, buf.getvalue()


class TestEvidenceFloor(unittest.TestCase):
    def test_trope_empty_support_held(self):
        result = {"tropes": ["Mind Game"], "trope_confidence": {},
                  "trope_chapters": {}, "chapter_summaries": []}
        rows, _, _, out = _trope_rows(result)
        self.assertEqual(rows, [])
        self.assertIn("Evidence floor", out)
        self.assertIn("Mind Game", out)

    def test_trope_unprovenanced_passages_do_not_rescue(self):
        result = {"tropes": ["Mind Game"], "trope_confidence": {},
                  "trope_chapters": {}, "chapter_summaries": [],
                  "trope_evidence": {pp._tnorm("Mind Game"): [
                      {"quote": "q", "chapter": ""},
                      {"quote": "q2"}]}}
        rows, _, _, out = _trope_rows(result)
        self.assertEqual(rows, [])
        self.assertIn("Evidence floor", out)

    def test_trope_quote_backed_ships(self):
        result = {"tropes": ["Mind Game"],
                  "trope_confidence": {pp._tnorm("Mind Game"): 0.9},
                  "trope_chapters": {}, "chapter_summaries": [],
                  "trope_evidence": {pp._tnorm("Mind Game"): [
                      {"quote": "the letter burned", "chapter": "Prologue"}]}}
        rows, _, _, out = _trope_rows(result)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"]["quotes"],
                         [{"quote": "the letter burned",
                           "chapter": "Prologue"}])
        self.assertNotIn("Evidence floor", out)

    def test_trope_with_chapters_ships(self):
        result = {"tropes": ["Mind Game"], "trope_confidence": {},
                  "trope_chapters": {pp._tnorm("Mind Game"): ["Prologue"]},
                  "chapter_summaries": []}
        rows, _, _, _ = _trope_rows(result)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"]["chapters"], ["Prologue"])

    def test_trigger_empty_support_held(self):
        result = {"triggers": [{"warning": "Violence", "chapters": [],
                                 "evidence": [], "confidence": 0.9}]}
        rows, out = _trigger_rows(result)
        self.assertEqual(rows, [])
        self.assertIn("Evidence floor", out)
        self.assertIn("Violence", out)

    def test_trigger_chapterless_quotes_do_not_rescue(self):
        result = {"triggers": [{"warning": "Violence", "chapters": [],
                                 "evidence": [{"quote": "q"}],
                                 "confidence": 0.9}]}
        rows, out = _trigger_rows(result)
        self.assertEqual(rows, [])
        self.assertIn("Evidence floor", out)

    def test_trigger_with_chapters_ships(self):
        result = {"triggers": [{"warning": "Violence",
                                 "chapters": ["Prologue"], "evidence": [],
                                 "confidence": 0.9}]}
        rows, out = _trigger_rows(result)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("Evidence floor", out)

    def test_trigger_quote_backed_ships(self):
        result = {"triggers": [{"warning": "Violence", "chapters": [],
                                 "evidence": [{"quote": "q",
                                               "chapter": "Prologue"}],
                                 "confidence": 0.9}]}
        rows, out = _trigger_rows(result)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("Evidence floor", out)


_SPLIT_LABEL = "part0007_split_000 (part 4)"
_SUMMARIES = [{"index": 1, "label": "Prologue"},
              {"index": 2, "label": "Chapter Two"},
              {"index": 23, "label": _SPLIT_LABEL}]


class TestChapterLabelNormalization(unittest.TestCase):
    def test_trope_split_label_normalized(self):
        result = {"tropes": ["Mind Game"], "trope_confidence": {},
                  "trope_chapters": {pp._tnorm("Mind Game"):
                                     [_SPLIT_LABEL, "Prologue"]},
                  "chapter_summaries": _SUMMARIES,
                  "trope_evidence": {pp._tnorm("Mind Game"): [
                      {"quote": "q", "chapter": _SPLIT_LABEL}]}}
        rows, _, _, _ = _trope_rows(result)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"]["chapters"],
                         ["Chapter 23", "Prologue"])
        self.assertEqual(rows[0]["evidence"]["quotes"][0]["chapter"],
                         "Chapter 23")

    def test_trigger_split_label_normalized(self):
        result = {"triggers": [{"warning": "Violence",
                                 "chapters": [_SPLIT_LABEL],
                                 "evidence": [{"quote": "q",
                                               "chapter": _SPLIT_LABEL}],
                                 "confidence": 0.9}],
                  "chapter_summaries": _SUMMARIES}
        rows, _ = _trigger_rows(result)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"]["chapters"], ["Chapter 23"])
        self.assertEqual(rows[0]["evidence"]["quotes"][0]["chapter"],
                         "Chapter 23")

    def test_titled_labels_unchanged(self):
        result = {"tropes": ["Mind Game"], "trope_confidence": {},
                  "trope_chapters": {pp._tnorm("Mind Game"):
                                     ["Chapter Two", "Prologue"]},
                  "chapter_summaries": _SUMMARIES}
        rows, _, _, _ = _trope_rows(result)
        self.assertEqual(rows[0]["evidence"]["chapters"],
                         ["Chapter Two", "Prologue"])

    def test_no_chapter_map_labels_unchanged(self):
        result = {"tropes": ["Mind Game"], "trope_confidence": {},
                  "trope_chapters": {pp._tnorm("Mind Game"): [_SPLIT_LABEL]}}
        rows, _, _, _ = _trope_rows(result)
        self.assertEqual(rows[0]["evidence"]["chapters"], [_SPLIT_LABEL])

    def test_unknown_label_unchanged(self):
        result = {"tropes": ["Mind Game"], "trope_confidence": {},
                  "trope_chapters": {pp._tnorm("Mind Game"):
                                     ["part0099_split_002"]},
                  "chapter_summaries": _SUMMARIES}
        rows, _, _, _ = _trope_rows(result)
        self.assertEqual(rows[0]["evidence"]["chapters"],
                         ["part0099_split_002"])


if __name__ == "__main__":
    unittest.main()
