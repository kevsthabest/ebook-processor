"""The kinship-evidence probe (find-kinship-evidence.py).

Born from the Daemon surname case: before asking whether
another book confirms a pair, scan its text for kinship
sentences naming both endpoints and report HOW each endpoint
is named — an explicit full-name statement is a different
class of evidence from a surname-anchored one.

Stdlib unittest only. Fake chapter dicts; no corpus.db.
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "find-kinship-evidence.py")
_spec = importlib.util.spec_from_file_location("find_kinship", _MOD_PATH)
fk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fk)
pp = fk.pp


def _chs(text):
    return [{"index": 1, "label": "Ch1", "text": text}]


class TestScanPair(unittest.TestCase):
    def test_surname_anchored_sentence_found_and_labelled(self):
        # Daemon ch22, verbatim shape: the only Peter-form is the
        # surname; Chris is placed by first name.
        hits = fk.scan_pair(pp, _chs(
            "Sebeck’s son, Chris, stood next to him."),
            "Peter Sebeck", "Chris Sebeck")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["a_form"], "surname")
        self.assertEqual(hits[0]["b_form"], "first")

    def test_explicit_full_name_statement_classified_full(self):
        hits = fk.scan_pair(pp, _chs(
            "Peter Sebeck's son, Chris, waved at them."),
            "Peter Sebeck", "Chris Sebeck")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["a_form"], "full")

    def test_single_shared_surname_cannot_place_both(self):
        # One "Sebeck" occurrence must not count for both
        # endpoints (the Sebeck inside a sentence about neither
        # Chris nor a pair bond).
        hits = fk.scan_pair(pp, _chs(
            "Sebeck thought about his son's homework."),
            "Peter Sebeck", "Chris Sebeck")
        self.assertEqual(hits, [])

    def test_no_marker_no_hit(self):
        hits = fk.scan_pair(pp, _chs(
            "Peter Sebeck met Chris at the office."),
            "Peter Sebeck", "Chris Sebeck")
        self.assertEqual(hits, [])

    def test_nickname_form_matches(self):
        # Freedom calls Peter "Pete": the nickname form is part
        # of the scan's accepted forms.
        hits = fk.scan_pair(pp, _chs(
            "Pete Sebeck held his son, Chris, close."),
            "Peter Sebeck", "Chris Sebeck")
        self.assertEqual(len(hits), 1)
        self.assertIn(hits[0]["a_form"], ("full", "nickname", "first"))


if __name__ == "__main__":
    unittest.main()
