"""Roster intake filter: role nouns and unnamed descriptors are not
characters.

The Daemon run proved the prompt-level "REAL PEOPLE ONLY" rule alone
does not hold: the roster filled with role nouns ("Mother" at 0.74
frequency), unnamed descriptors ("Second Victim", "Asian Woman",
"Unknown Female Voice"), tickers ("HSTM") and non-name punctuation
("Go Mets!"). _is_person_like is the deterministic backstop at intake
(_roster_update), and it must reject those while keeping every real
named character — including "The Major", which Daemon uses as a name.
"""

import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


REJECTED = [
    "Mother",
    "Second Victim",
    "Asian Woman",
    "Deputy",
    "Go-Go Girls",
    "Unknown Female Voice",
    "Go Mets!",
    "HSTM",
    "Young Asian man",
]

KEPT = [
    "Peter Sebeck",
    "Jon Ross",
    "Brian Gragg",
    "Steven Trear",
    'Roy "Tripwire" Merritt',
    "Anji Anderson",
    "Natalie Philips",
    "Matthew A. Sobol",
    "The Major",
    "The Daemon",
]


class PersonFilterTest(unittest.TestCase):
    def test_role_nouns_and_descriptors_rejected(self):
        for name in REJECTED:
            with self.subTest(name=name):
                self.assertFalse(pp._is_person_like(name))

    def test_named_individuals_kept(self):
        for name in KEPT:
            with self.subTest(name=name):
                self.assertTrue(pp._is_person_like(name))

    def test_empty_name_rejected(self):
        self.assertFalse(pp._is_person_like(""))
        self.assertFalse(pp._is_person_like("   "))

    def test_article_role_form_is_the_only_allowance(self):
        # "The Major" ships; the bare role noun does not.
        self.assertTrue(pp._is_person_like("The Major"))
        self.assertFalse(pp._is_person_like("Major"))

    def test_group_checks_survive_the_rewrite(self):
        self.assertFalse(pp._is_person_like("eva masters"))
        self.assertFalse(pp._is_person_like("The Guards"))
        self.assertFalse(pp._is_person_like("soldiers"))


if __name__ == "__main__":
    unittest.main()
