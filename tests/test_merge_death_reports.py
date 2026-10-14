"""Regression: _merge_roster_entries crashed on death-reported keepers.

Live failure (first --roster-reconcile run, Fourth Wing): post_pass_merge
approved "Brennan" -> "Brennan Sorrengail"; Brennan is reported dead
pre-book, so the keeper's entry carried death_reports. The merge died:

    File "process-portable.py", line 3785, in _merge_roster_entries
        ke.setdefault(attr, set()).update(de[attr])
    AttributeError: 'list' object has no attribute 'update'

Root cause: _roster_update creates death_reports as a LIST (chapter
indices appended per report; the v2_reduce consumer at the sticky-death
check treats it as a list and takes min()), while alive_reports and
unresolved_mentions are SETS. _merge_roster_entries lumped all three
into one set-semantics loop, so a keeper that already had death_reports
(a list) made setdefault return the list and .update() raise.

The fix keeps death_reports a list (order-preserving dedupe on merge)
and leaves the two set-typed attrs on set semantics. List is canonical
for death_reports: the producer makes a list and the consumer reads one.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from collections import Counter
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _entry(name, appearances=1, chapters=(), **over):
    """Roster entry in the exact shape _roster_update produces."""
    nk = pp.norm_name(name)
    e = {"name": name, "aliases": set(), "alias_keys": {nk},
         "primary_keys": {nk}, "_generic_keys": set(),
         "appearances": appearances, "last_seen": max(chapters, default=0),
         "chapters": set(chapters), "roles": Counter(),
         "descriptions": [], "evidence": ""}
    e.update(over)
    return nk, e


class TestMergeDeathReports(unittest.TestCase):
    """The unit regression: keeper already has death_reports (a list)."""

    def test_merge_unions_list_and_set_attrs(self):
        roster = {}
        k, keep = _entry(
            "Brennan Sorrengail", appearances=10, chapters={2, 5, 7},
            death_reports=[2, 5], alive_reports={7},
            unresolved_mentions={"Brennan S."})
        roster[k] = keep
        d, drop = _entry(
            "Brennan", appearances=3, chapters={9, 11},
            death_reports=[5, 9], alive_reports={11},
            unresolved_mentions={"Bren"})
        roster[d] = drop

        pp._merge_roster_entries(roster, "brennan sorrengail", "brennan")

        self.assertNotIn("brennan", roster)
        e = roster["brennan sorrengail"]
        # death_reports stays a list: keeper's chapters in encounter
        # order, drop's new ones appended, 5 not duplicated.
        self.assertIsInstance(e["death_reports"], list)
        self.assertEqual(e["death_reports"], [2, 5, 9])
        # Set-typed attrs union as sets.
        self.assertEqual(e["alive_reports"], {7, 11})
        self.assertEqual(e["unresolved_mentions"], {"Brennan S.", "Bren"})

    def test_merge_creates_death_reports_as_list_when_keeper_lacks_it(self):
        roster = {}
        k, keep = _entry("Violet Sorrengail", appearances=8, chapters={1})
        roster[k] = keep
        d, drop = _entry("Violet", appearances=2, chapters={4},
                         death_reports=[4])
        roster[d] = drop

        pp._merge_roster_entries(roster, "violet sorrengail", "violet")

        e = roster["violet sorrengail"]
        self.assertIsInstance(e["death_reports"], list)
        self.assertEqual(e["death_reports"], [4])


class TestPostPassMergeDeathReports(unittest.TestCase):
    """Seam regression: the live crash arrived via post_pass_merge."""

    def test_postpass_tier2_merge_with_death_reports(self):
        roster = {}
        k, keep = _entry("Matthew Sobol", appearances=5, chapters={1, 2},
                         death_reports=[1])
        roster[k] = keep
        d, drop = _entry("Matthew A. Sobol", appearances=1, chapters={5},
                         death_reports=[3], alive_reports={6},
                         unresolved_mentions={"Matt"})
        roster[d] = drop

        n, possible = pp.post_pass_merge(roster, validator=None)

        self.assertEqual(n, 1)
        self.assertEqual(len(roster), 1)
        e = roster["matthew sobol"]
        self.assertIsInstance(e["death_reports"], list)
        self.assertEqual(e["death_reports"], [1, 3])
        self.assertEqual(e["alive_reports"], {6})
        self.assertEqual(e["unresolved_mentions"], {"Matt"})


if __name__ == "__main__":
    unittest.main()
