"""Regression: checkpoint resume rehydrated roster field types incompletely.

Live failure (resumed Fourth Wing run, Portable v2.9.2): the run skipped
all 115 chapters from checkpoints, then post_pass_merge approved
"Violet Sorrengail" vs "Violet" and the merge died:

    File "process-portable.py", line 3774, in _merge_roster_entries
        ke["roles"] += de.get("roles", Counter())
    TypeError: unsupported operand type(s) for +=: 'dict' and 'dict'

Root cause: _ckpt_json_safe serializes every roster field to plain JSON
(sets -> sorted lists, Counters -> dicts, description tuples -> lists),
but _ckpt_json_restore only restored FOUR set fields (aliases,
primary_keys, chapters, evidence_chapters). A resumed roster therefore
carried:

- roles / statuses as plain dicts: `roles += Counter()` raises the
  TypeError above; `statuses` merged via setdefault(Counter()).update()
  silently REPLACED counts instead of summing; and v2_reduce calls
  statuses.most_common(1), which a dict does not have — a resumed run
  with no merges would have crashed there instead.
- alias_keys / alive_reports / unresolved_mentions / _generic_keys as
  lists: `alias_keys |= ...` on a list raises TypeError (in the live
  run this was masked only because --roster-reconcile's pruning pass
  rewrites alias_keys as a set before post_pass_merge — roles was
  simply the first unmasked field); alive_reports /
  unresolved_mentions hit setdefault(set()).update() ->
  AttributeError waiting downstream; _generic_keys feeds a
  `set | list` union -> TypeError.
- descriptions entries as lists: _roster_best_desc / v2_reduce wrap any
  non-tuple as (0, entry, False), mangling a rehydrated
  [chapter, text, has_evidence] entry into a description whose "text"
  is the list itself.

The fix is roster-scoped normalization at the checkpoint load seam:
_checkpoint_load coerces each roster entry's fields back to the
producer's canonical types after _ckpt_json_restore. Chapter a/b
payloads share _ckpt_json_restore and may legitimately contain
same-named keys, so they are NOT coerced — pinned by a test below.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_SET_FIELDS = ("aliases", "primary_keys", "chapters", "evidence_chapters",
               "alias_keys", "alive_reports", "unresolved_mentions",
               "_generic_keys")


def _producer_roster():
    """Two roster entries in the exact shape _roster_update produces,
    with EVERY typed field populated."""
    violet = {
        "name": "Violet Sorrengail",
        "aliases": {"Violence"},
        "alias_keys": {"violet sorrengail", "violet", "violence"},
        "primary_keys": {"violet sorrengail"},
        "_generic_keys": {"i"},
        "appearances": 40, "last_seen": 90,
        "chapters": {1, 2, 90},
        "evidence_chapters": {1, 2},
        "roles": Counter({"protagonist": 30, "cadet": 5}),
        "statuses": Counter({"alive": 35, "injured": 2}),
        "descriptions": [(1, "Small and fragile.", True),
                         (2, "Shared description.", True)],
        "appearances_desc": ["Silver hair in a braid."],
        "death_reports": [],
        "alive_reports": {5, 90},
        "unresolved_mentions": {"Vi"},
        "evidence": "I am Violet.",
    }
    violet_short = {
        "name": "Violet",
        "aliases": set(),
        "alias_keys": {"violet"},
        "primary_keys": {"violet"},
        "_generic_keys": set(),
        "appearances": 12, "last_seen": 40,
        "chapters": {3, 40},
        "evidence_chapters": {3},
        "roles": Counter({"protagonist": 10}),
        "statuses": Counter({"alive": 10, "dead": 1}),
        "descriptions": [(2, "Shared description.", True),
                         (3, "Short-form only.", False)],
        "appearances_desc": ["Silver hair."],
        "death_reports": [40],
        "alive_reports": {3},
        "unresolved_mentions": {"Violet S."},
        "evidence": "",
    }
    return {"violet sorrengail": violet, "violet": violet_short}


class _CheckpointCase(unittest.TestCase):
    """Redirect the checkpoint dir to a temp location (same pattern as
    tests/test_phase4.py TestCheckpoints)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_dir = pp._checkpoint_dir
        tmp = self._tmp

        def _tmp_dir(book_key, _t=tmp):
            return Path(_t) / book_key
        pp._checkpoint_dir = _tmp_dir
        _orig = self._orig_dir
        self.addCleanup(lambda: setattr(pp, "_checkpoint_dir", _orig))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

    def _roundtrip(self, roster):
        pp._checkpoint_save_meta("bk", 115)
        pp._checkpoint_save_roster("bk", roster)
        _meta, loaded, _chapters = pp._checkpoint_load("bk")
        return loaded


class TestRosterRoundTripTypes(_CheckpointCase):
    """Save a producer-shaped roster, load it, assert exact types AND
    values for every typed field."""

    def test_all_typed_fields_restored(self):
        loaded = self._roundtrip(_producer_roster())
        for key in ("violet sorrengail", "violet"):
            e = loaded[key]
            for f in _SET_FIELDS:
                self.assertIsInstance(e[f], set, f"{key}.{f}")
            self.assertIsInstance(e["roles"], Counter, f"{key}.roles")
            self.assertIsInstance(e["statuses"], Counter, f"{key}.statuses")
            self.assertIsInstance(e["death_reports"], list,
                                  f"{key}.death_reports")
            self.assertIsInstance(e["descriptions"], list,
                                  f"{key}.descriptions")
            for d in e["descriptions"]:
                self.assertIsInstance(d, tuple, f"{key}.descriptions entry")
                self.assertEqual(len(d), 3)
            self.assertIsInstance(e["appearances_desc"], list,
                                  f"{key}.appearances_desc")

    def test_values_survive_roundtrip(self):
        loaded = self._roundtrip(_producer_roster())
        e = loaded["violet sorrengail"]
        self.assertEqual(e["aliases"], {"Violence"})
        self.assertEqual(e["alias_keys"],
                         {"violet sorrengail", "violet", "violence"})
        self.assertEqual(e["primary_keys"], {"violet sorrengail"})
        self.assertEqual(e["_generic_keys"], {"i"})
        self.assertEqual(e["chapters"], {1, 2, 90})
        self.assertEqual(e["evidence_chapters"], {1, 2})
        self.assertEqual(e["roles"], Counter({"protagonist": 30, "cadet": 5}))
        self.assertEqual(e["statuses"], Counter({"alive": 35, "injured": 2}))
        self.assertEqual(e["descriptions"],
                         [(1, "Small and fragile.", True),
                          (2, "Shared description.", True)])
        self.assertEqual(e["appearances_desc"], ["Silver hair in a braid."])
        self.assertEqual(e["death_reports"], [])
        self.assertEqual(e["alive_reports"], {5, 90})
        self.assertEqual(e["unresolved_mentions"], {"Vi"})
        self.assertEqual(e["name"], "Violet Sorrengail")
        self.assertEqual(e["appearances"], 40)
        self.assertEqual(e["last_seen"], 90)
        self.assertEqual(e["evidence"], "I am Violet.")
        d = loaded["violet"]
        self.assertEqual(d["death_reports"], [40])
        self.assertEqual(d["statuses"], Counter({"alive": 10, "dead": 1}))

    def test_statuses_most_common_works_after_resume(self):
        """v2_reduce calls statuses.most_common(1) on every entry — the
        crash a merge-free resumed run would have hit instead."""
        loaded = self._roundtrip(_producer_roster())
        self.assertEqual(
            loaded["violet sorrengail"]["statuses"].most_common(1),
            [("alive", 35)])
        self.assertEqual(
            loaded["violet sorrengail"]["roles"].most_common(1),
            [("protagonist", 30)])

    def test_best_desc_reads_rehydrated_descriptions(self):
        """_roster_best_desc must see (chapter, text, has_evidence), not
        a mangled (0, [chapter, text, has_evidence], False) wrapper."""
        loaded = self._roundtrip(_producer_roster())
        self.assertEqual(pp._roster_best_desc(loaded["violet sorrengail"]),
                         "Small and fragile.")


class TestMergeAfterResume(_CheckpointCase):
    """The live crash seam: merge two REHYDRATED entries."""

    def test_merge_rehydrated_entries(self):
        roster = self._roundtrip(_producer_roster())

        pp._merge_roster_entries(roster, "violet sorrengail", "violet")

        self.assertNotIn("violet", roster)
        e = roster["violet sorrengail"]
        # Counters SUM (pre-fix: roles raised TypeError; statuses would
        # have silently replaced).
        self.assertIsInstance(e["roles"], Counter)
        self.assertEqual(e["roles"],
                         Counter({"protagonist": 40, "cadet": 5}))
        self.assertIsInstance(e["statuses"], Counter)
        self.assertEqual(e["statuses"],
                         Counter({"alive": 45, "injured": 2, "dead": 1}))
        # Set fields union WITHOUT duplicates: keep and drop both claim
        # "violet" in alias_keys — a list |= would have duplicated it.
        self.assertIsInstance(e["alias_keys"], set)
        self.assertEqual(e["alias_keys"],
                         {"violet sorrengail", "violet", "violence"})
        self.assertEqual(e["primary_keys"], {"violet sorrengail", "violet"})
        self.assertEqual(e["chapters"], {1, 2, 3, 40, 90})
        self.assertEqual(e["_generic_keys"], {"i"})
        self.assertEqual(e["alive_reports"], {3, 5, 90})
        self.assertEqual(e["unresolved_mentions"], {"Vi", "Violet S."})
        # death_reports stays a list, order-preserving dedupe.
        self.assertIsInstance(e["death_reports"], list)
        self.assertEqual(e["death_reports"], [40])
        # Descriptions: shared tuple not duplicated, drop-only appended,
        # entries still tuples after the merge.
        self.assertEqual(e["descriptions"],
                         [(1, "Small and fragile.", True),
                          (2, "Shared description.", True),
                          (3, "Short-form only.", False)])
        self.assertEqual(e["appearances"], 52)


class TestChapterPayloadsUntouched(_CheckpointCase):
    """Normalization is roster-scoped: chapter a/b payloads share
    _ckpt_json_restore and may legitimately carry same-named keys —
    they must come back exactly as JSON left them."""

    def test_chapter_payload_same_named_keys_not_coerced(self):
        pp._checkpoint_save_meta("bk", 1)
        a = {"characters": [{"name": "Violet"}],
             "roles": {"protagonist": 3},
             "statuses": {"alive": 2},
             "alias_keys": ["violet"],
             "descriptions": [[1, "text", True]]}
        pp._checkpoint_save_chapter("bk", 0, a, {"summary": "s"})
        _meta, _roster, chapters = pp._checkpoint_load("bk")
        la, _lb = chapters[0]
        self.assertIsInstance(la["roles"], dict)
        self.assertNotIsInstance(la["roles"], Counter)
        self.assertIsInstance(la["statuses"], dict)
        self.assertNotIsInstance(la["statuses"], Counter)
        self.assertIsInstance(la["alias_keys"], list)
        self.assertIsInstance(la["descriptions"][0], list)


if __name__ == "__main__":
    unittest.main()
