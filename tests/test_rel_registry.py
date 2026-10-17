"""Relationship type registry (spec addendum, ruled 2026-10-17).

The vocabulary has one source of truth — the Supabase
`relationship_types` table — read at run start through sb(). Every
consumer (extraction prompts, validator prompt, reduce lattice,
family routing, validity check) derives from the single vocabulary
object _derive_rel_vocab() installs, so a registry-only type must
work end to end with no code change, and every failure of the load
must leave the built-in mirror in force rather than shrinking or
breaking the run.

Stdlib unittest only. No network, no Supabase: sb() is mocked.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import re
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_MIGRATION = (Path(__file__).resolve().parent.parent.parent / "booktok"
              / "supabase" / "migrations"
              / "v614_relationship_types.sql")

_BLOOD_OATH = {"id": "blood_oath", "label": "Blood oath",
               "gloss": "Bound by a blood oath sworn together in "
                        "ritual: the oath was sealed in blood, not "
                        "merely spoken.",
               "direction": "either", "is_family": False,
               "exclusivity": "exclusive", "precedence": 3,
               "graph_group": "friend", "suggestible": True}


def _fixture_rows():
    """Registry-shaped rows: the built-in set plus one type that
    exists only in this payload."""
    rows = [dict(r) for r in pp._BUILTIN_REL_TYPES]
    rows.append(dict(_BLOOD_OATH))
    return rows


class _RegistryTestCase(unittest.TestCase):
    """Snapshot and restore the active vocabulary around each test:
    the vocabulary is module state, and a test that installs a
    fixture must not leak it into the rest of the suite."""

    def setUp(self):
        self._config = dict(pp.CONFIG)
        self._status = dict(pp._REL_REGISTRY_STATUS)
        self._attempted = pp._REL_REGISTRY_LOAD_ATTEMPTED

    def tearDown(self):
        pp.CONFIG.clear()
        pp.CONFIG.update(self._config)
        pp._apply_rel_vocab(pp._derive_rel_vocab(pp._BUILTIN_REL_TYPES))
        pp._REL_REGISTRY_STATUS = self._status
        pp._REL_REGISTRY_LOAD_ATTEMPTED = self._attempted

    def _configure_sb(self):
        pp.CONFIG["supabase_url"] = "https://example.invalid"
        pp.CONFIG["supabase_key"] = "test-key"


class TestDeriveVocab(_RegistryTestCase):
    def test_builtin_derives_lattice(self):
        v = pp._derive_rel_vocab(pp._BUILTIN_REL_TYPES)
        self.assertIsNotNone(v)
        self.assertEqual(v["precedence"]["spouse"], 0)
        self.assertEqual(v["precedence"]["bonded"], 3)
        self.assertEqual(v["precedence"]["sire"], 1)
        self.assertEqual(v["precedence"]["handler"], 4)
        self.assertIn("packmate", v["nonexclusive"])
        self.assertIn("crewmate", v["nonexclusive"])
        self.assertNotIn("bonded", v["nonexclusive"])

    def test_builtin_family_routing(self):
        v = pp._derive_rel_vocab(pp._BUILTIN_REL_TYPES)
        for t in ("spouse", "parent", "child", "sibling",
                  "step_parent", "step_child"):
            self.assertIn(t, v["family"])
        for t in ("sire", "bonded", "mate", "handler"):
            self.assertNotIn(t, v["family"])

    def test_builtin_inverse_pairs(self):
        v = pp._derive_rel_vocab(pp._BUILTIN_REL_TYPES)
        self.assertEqual(v["inverse"]["parent"], "child")
        self.assertEqual(v["inverse"]["step_parent"], "step_child")
        self.assertEqual(v["inverse"]["step_child"], "step_parent")
        # Directed types with no named inverse keep claim direction,
        # the mentor treatment generalized.
        for t in ("mentor", "sire", "handler"):
            self.assertIn(t, v["no_inverse"])

    def test_malformed_rows_rejected(self):
        self.assertIsNone(pp._derive_rel_vocab(None))
        self.assertIsNone(pp._derive_rel_vocab([]))
        self.assertIsNone(pp._derive_rel_vocab([{"id": "x"}]))
        dup = [dict(pp._BUILTIN_REL_TYPES[0]),
               dict(pp._BUILTIN_REL_TYPES[0])]
        self.assertIsNone(pp._derive_rel_vocab(dup))
        bad = [dict(r) for r in pp._BUILTIN_REL_TYPES]
        bad[0]["precedence"] = None  # exclusive without a rank
        self.assertIsNone(pp._derive_rel_vocab(bad))
        bad2 = [dict(r) for r in pp._BUILTIN_REL_TYPES]
        bad2[0]["direction"] = "sideways"
        self.assertIsNone(pp._derive_rel_vocab(bad2))

    def test_canonical_pair_step_and_no_inverse(self):
        self.assertEqual(
            pp._canonical_pair("Bob", "Alice", "step_child"),
            ("Alice", "Bob", "step_parent"))
        self.assertEqual(
            pp._canonical_pair("Bob", "Alice", "sire"),
            ("Bob", "Alice", "sire"))


class TestLoadAndFallback(_RegistryTestCase):
    def test_load_fixture_installs_registry_vocab(self):
        self._configure_sb()
        with mock.patch.object(pp, "sb", return_value=_fixture_rows()):
            status = pp._load_rel_registry(force=True)
        self.assertEqual(status["source"], "registry")
        self.assertIn("blood_oath", pp._REL_PRECEDENCE)
        self.assertEqual(pp._rel_gloss("blood_oath"),
                         _BLOOD_OATH["gloss"])

    def test_no_config_falls_back(self):
        pp.CONFIG["supabase_url"] = ""
        pp.CONFIG["supabase_key"] = ""
        status = pp._load_rel_registry(force=True)
        self.assertEqual(status["source"], "builtin")
        self.assertEqual(status["reason"], "no-supabase-config")
        self.assertIn("bonded", pp._REL_PRECEDENCE)

    def test_unreachable_falls_back(self):
        self._configure_sb()
        with mock.patch.object(pp, "sb", return_value=None):
            status = pp._load_rel_registry(force=True)
        self.assertEqual(status["source"], "builtin")
        self.assertEqual(status["reason"],
                         "unreachable-or-missing-table")
        self.assertIn("spouse", pp._REL_PRECEDENCE)

    def test_malformed_falls_back(self):
        self._configure_sb()
        with mock.patch.object(pp, "sb",
                               return_value=[{"id": "broken"}]):
            status = pp._load_rel_registry(force=True)
        self.assertEqual(status["source"], "builtin")
        self.assertEqual(status["reason"], "malformed-rows")
        self.assertIn("step_parent", pp._REL_FAMILY_TYPES)

    def test_load_error_never_raises(self):
        self._configure_sb()
        with mock.patch.object(pp, "sb",
                               side_effect=RuntimeError("boom")):
            status = pp._load_rel_registry(force=True)
        self.assertEqual(status["source"], "builtin")
        self.assertEqual(status["reason"], "load-error")

    def test_status_rides_run_history_thresholds(self):
        self._configure_sb()
        with mock.patch.object(pp, "sb", return_value=None):
            pp._load_rel_registry(force=True)
        stamp = pp._history_thresholds()["rel_registry"]
        self.assertEqual(stamp["source"], "builtin")
        self.assertEqual(stamp["reason"],
                         "unreachable-or-missing-table")


class TestSyntheticNewType(_RegistryTestCase):
    """A type present only in the registry payload must appear in
    the extraction vocabulary, the validator prompt (with its gloss),
    and the reduce lattice — with no code change."""

    def setUp(self):
        super().setUp()
        self._configure_sb()
        with mock.patch.object(pp, "sb",
                               return_value=_fixture_rows()):
            pp._load_rel_registry(force=True)

    def test_extraction_prompt_offers_new_type_with_gloss(self):
        for template in (pp.PROMPT_V2_CHARACTERS, pp.PROMPT_IDENTITY):
            text = pp._rel_prompt(template)
            self.assertIn("blood_oath", text)
            self.assertIn(_BLOOD_OATH["gloss"], text)

    def test_validator_prompt_carries_new_type_gloss(self):
        captured = {}
        validator = pp.DecisionValidator.__new__(pp.DecisionValidator)

        def _ask(kind, instructions, state, label=""):
            captured["instructions"] = instructions
            return 0.9

        validator._ask_noul = _ask
        validator.validate_relationship(
            "Alice", "Bob", "blood_oath", "They swore it in blood.")
        self.assertIn(_BLOOD_OATH["gloss"], captured["instructions"])

    def test_lattice_and_validity_accept_new_type(self):
        self.assertIn("blood_oath", pp._REL_PRECEDENCE)
        self.assertEqual(pp._REL_PRECEDENCE["blood_oath"], 3)
        self.assertNotIn("blood_oath", pp._REL_FAMILY_TYPES)
        # The reduce validity check is membership in precedence or
        # the extras set; the canonical-pair step must also keep an
        # "either" type unchanged under reversal.
        self.assertEqual(
            pp._canonical_pair("Bob", "Alice", "blood_oath"),
            ("Alice", "Bob", "blood_oath"))


class TestBuiltinSeedParity(_RegistryTestCase):
    """The built-in fallback must mirror the app migration's seed
    exactly: the two are written in different repos, and a drift
    would make offline runs disagree with the seeded database."""

    def test_fallback_equals_migration_seed(self):
        if not _MIGRATION.exists():
            self.skipTest("booktok sibling checkout not present")
        sql = _MIGRATION.read_text(encoding="utf-8")
        row_re = re.compile(
            r"\('([a-z_]+)', '((?:[^']|'')*)', '((?:[^']|'')*)', "
            r"'(symmetric|directed|either)', (true|false), "
            r"'(exclusive|extra)', (null|\d+), '([a-z]+)', "
            r"(true|false)\)")
        seeded = {}
        for m in row_re.finditer(sql):
            (tid, label, gloss, direction, fam, excl, prec,
             group, sugg) = m.groups()
            seeded[tid] = {
                "id": tid,
                "label": label.replace("''", "'"),
                "gloss": gloss.replace("''", "'"),
                "direction": direction,
                "is_family": fam == "true",
                "exclusivity": excl,
                "precedence": None if prec == "null" else int(prec),
                "graph_group": group,
                "suggestible": sugg == "true",
            }
        builtin = {r["id"]: r for r in pp._BUILTIN_REL_TYPES}
        self.assertEqual(set(seeded), set(builtin))
        for tid, row in builtin.items():
            self.assertEqual(seeded[tid], row,
                             f"seed mismatch for {tid}")


if __name__ == "__main__":
    unittest.main()
