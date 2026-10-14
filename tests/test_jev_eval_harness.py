"""JEV merge-prompt evaluation harness (evaluate-merges.py upgrade).

The harness must judge pairs the way production judges them: the same
state assembly (names + descriptions + evidence, same truncation) and,
for the "legacy" variant, the production instruction text verbatim.
These tests pin that mirror against the real DecisionValidator, pin the
two prompt variants apart (instruction text is the ONLY difference),
and pin the scoring maths — false merges and false splits are separate
lists because they are not equally costly.

Hermetic: urlopen is the network boundary double; the production
validator's _ask_noul is captured, never called. Stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
import urllib.request

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


em = _load("evaluate_merges_harness", "evaluate-merges.py")
pp = _load("process_portable_for_jev_eval", "process-portable.py")


# ----------------------------------------------------------------------
# Production mirror: state assembly + legacy instruction text
# ----------------------------------------------------------------------

def _capture_production(name_a, desc_a, ev_a, name_b, desc_b, ev_b):
    """Run the real DecisionValidator.ask_same_person with _ask_noul
    captured. Returns (instructions, state)."""
    seen = {}

    def fake_ask(self, question_key, instructions, state, label="decision"):
        seen.update(key=question_key, instructions=instructions,
                    state=state, label=label)
        return 0.5

    validator = pp.DecisionValidator("http://127.0.0.1:9/v1")
    with mock.patch.object(pp.DecisionValidator, "_ask_noul", fake_ask):
        validator.ask_same_person(name_a, desc_a, ev_a,
                                  name_b, desc_b, ev_b)
    return seen["instructions"], seen["state"]


class TestProductionMirror(unittest.TestCase):
    def test_legacy_instructions_are_production_verbatim(self):
        instructions, _ = _capture_production("A", "da", "ea",
                                              "B", "db", "eb")
        self.assertEqual(instructions, em.LEGACY_INSTRUCTIONS)
        self.assertEqual(instructions, em.PROMPT_VARIANTS["legacy"])

    def test_state_mirror_full_fields(self):
        _, state = _capture_production("A", "desc a", "ev a",
                                       "B", "desc b", "ev b")
        self.assertEqual(state, em.build_same_person_state(
            "A", "desc a", "ev a", "B", "desc b", "ev b"))

    def test_state_mirror_name_only(self):
        _, state = _capture_production("A", "", "", "B", "", "")
        self.assertEqual(state, "Character A: A\nCharacter B: B")
        self.assertEqual(state, em.build_same_person_state(
            "A", "", "", "B", "", ""))

    def test_state_mirror_partial_fields(self):
        _, state = _capture_production("A", "desc a", "",
                                       "B", "", "ev b")
        self.assertEqual(state, em.build_same_person_state(
            "A", "desc a", "", "B", "", "ev b"))

    def test_state_mirror_truncation(self):
        long_desc = "d" * 700
        long_ev = "e" * 400
        _, state = _capture_production("A", long_desc, long_ev,
                                       "B", "", "")
        self.assertEqual(state, em.build_same_person_state(
            "A", long_desc, long_ev, "B", "", ""))
        self.assertIn("d" * 600, state)
        self.assertNotIn("d" * 601, state)
        self.assertIn("e" * 300, state)
        self.assertNotIn("e" * 301, state)


class TestBuildState(unittest.TestCase):
    def test_part_order_and_omission(self):
        state = em.build_same_person_state(
            "A", "da", "", "B", "", "eb")
        self.assertEqual(
            state,
            "Character A: A\nDescription A: da\n"
            "Character B: B\nEvidence B: eb")

    def test_none_fields_treated_as_empty(self):
        state = em.build_same_person_state(
            "A", None, None, "B", None, None)
        self.assertEqual(state, "Character A: A\nCharacter B: B")


# ----------------------------------------------------------------------
# Prompt variants: only the instruction text differs
# ----------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ask_payloads(instructions_a, instructions_b):
    """Call em.ask_same_person twice with urlopen captured; return the
    two request payloads as dicts."""
    payloads = []

    def fake_urlopen(req, timeout=None):
        payloads.append(json.loads(req.data))
        return _FakeResp({"answers": {"same_person": {"noul": 0.5}}})

    kwargs = dict(desc_a="desc a", ev_a="ev a",
                  desc_b="desc b", ev_b="ev b")
    with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
        em.ask_same_person("http://x/v1", "m", "", "A", "B",
                           instructions=instructions_a, **kwargs)
        em.ask_same_person("http://x/v1", "m", "", "A", "B",
                           instructions=instructions_b, **kwargs)
    return payloads


class TestPromptVariants(unittest.TestCase):
    def test_default_instructions_are_legacy_production_text(self):
        payloads = _ask_payloads(em.LEGACY_INSTRUCTIONS,
                                 em.LEGACY_INSTRUCTIONS)
        instr = payloads[0]["questions"]["same_person"]["instructions"]
        self.assertEqual(instr, em.LEGACY_INSTRUCTIONS)
        # The production literal, not the old drifted paraphrase.
        self.assertIn("character entries", instr)
        self.assertIn("AND the descriptions are compatible", instr)

    def test_variants_differ_only_in_instructions(self):
        legacy, identity = _ask_payloads(
            em.PROMPT_VARIANTS["legacy"], em.PROMPT_VARIANTS["identity"])
        self.assertEqual(legacy["state"], identity["state"])
        self.assertEqual(legacy["model"], identity["model"])
        q_l = legacy["questions"]["same_person"]
        q_i = identity["questions"]["same_person"]
        self.assertEqual(q_l["type"], q_i["type"])
        self.assertNotEqual(q_l["instructions"], q_i["instructions"])
        # State is the production-shaped assembly in both.
        self.assertEqual(legacy["state"], em.build_same_person_state(
            "A", "desc a", "ev a", "B", "desc b", "ev b"))

    def test_identity_has_no_first_names_contradiction(self):
        ident = em.PROMPT_VARIANTS["identity"]
        self.assertNotIn("different first names", ident)

    def test_identity_requires_positive_evidence(self):
        ident = em.PROMPT_VARIANTS["identity"].lower()
        self.assertIn("positive evidence", ident)
        self.assertIn("nickname", ident)
        # Similar descriptions alone never establish identity.
        self.assertIn("alone", ident)
        self.assertIn("never establish", ident)

    def test_identity_names_the_intermediate_score(self):
        ident = em.PROMPT_VARIANTS["identity"].lower()
        self.assertIn("insufficient", ident)
        self.assertIn("middle", ident)


# ----------------------------------------------------------------------
# Pair loading: new list format + legacy dict format
# ----------------------------------------------------------------------

class TestLoadPairs(unittest.TestCase):
    def _tmp_json(self, data):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "pairs.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_new_format_full_entry(self):
        path = self._tmp_json([{
            "name_a": "Pete Sebeck", "name_b": "Peter Sebeck",
            "label": "same", "book": "Daemon",
            "desc_a": "da", "desc_b": "db",
            "evidence_a": "ea", "evidence_b": "eb",
            "source": "hand labels"}])
        pairs = em.load_pairs_file(path)
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertEqual((p["name_a"], p["name_b"]), ("Pete Sebeck",
                                                      "Peter Sebeck"))
        self.assertTrue(p["expected"])
        self.assertEqual(p["book"], "Daemon")
        self.assertEqual((p["desc_a"], p["desc_b"]), ("da", "db"))
        self.assertEqual((p["evidence_a"], p["evidence_b"]), ("ea", "eb"))
        self.assertEqual(p["source"], "hand labels")

    def test_new_format_minimal_entry_defaults(self):
        path = self._tmp_json([{
            "name_a": "A", "name_b": "B", "label": "different",
            "book": "Daemon"}])
        p = em.load_pairs_file(path)[0]
        self.assertFalse(p["expected"])
        self.assertEqual(p["desc_a"], "")
        self.assertEqual(p["evidence_b"], "")

    def test_new_format_bad_label_rejected(self):
        path = self._tmp_json([{
            "name_a": "A", "name_b": "B", "label": "maybe",
            "book": "Daemon"}])
        with self.assertRaises(ValueError):
            em.load_pairs_file(path)

    def test_legacy_dict_format_and_uncertain_skip(self):
        path = self._tmp_json({
            "k1": {"a": "A", "b": "B", "same_person": True},
            "k2": {"a": "C", "b": "D", "same_person": False,
                   "uncertain": True},
            "k3": {"a": "E", "b": "F", "same_person": False},
        })
        pairs = em.load_pairs_file(path)
        self.assertEqual([(p["name_a"], p["expected"]) for p in pairs],
                         [("A", True), ("E", False)])
        # Legacy entries carry no descriptions: name-only mode.
        self.assertTrue(all(em.pair_mode(p) == "name-only" for p in pairs))

    def test_pair_mode_full_when_any_field_present(self):
        self.assertEqual(em.pair_mode({"desc_a": "", "desc_b": "",
                                       "evidence_a": "x",
                                       "evidence_b": ""}), "full")
        self.assertEqual(em.pair_mode({"desc_a": "", "desc_b": "",
                                       "evidence_a": "",
                                       "evidence_b": ""}), "name-only")


# ----------------------------------------------------------------------
# Scoring maths
# ----------------------------------------------------------------------

def _scored(name_a, name_b, expected, p_yes, book="Daemon"):
    return {"name_a": name_a, "name_b": name_b, "expected": expected,
            "book": book, "p_yes": p_yes}


class TestScoring(unittest.TestCase):
    SCORED = [
        _scored("Same High", "Same High Alt", True, 0.9),
        _scored("Same Low", "Same Low Alt", True, 0.4),
        _scored("Diff High", "Diff High Alt", False, 0.95),
        _scored("Diff Low", "Diff Low Alt", False, 0.2),
        _scored("Same Silent", "Same Silent Alt", True, None),
        _scored("Diff Edge", "Diff Edge Alt", False, 0.85),
    ]

    def test_counts_and_rates_at_0_5(self):
        s = em.score_summary(self.SCORED, 0.5)
        self.assertEqual((s["tp"], s["tn"], s["fp"], s["fn"]), (1, 1, 2, 1))
        self.assertEqual(s["answered"], 5)
        self.assertEqual(s["unanswered"], 1)
        self.assertAlmostEqual(s["accuracy"], 0.4)
        self.assertAlmostEqual(s["precision"], 1 / 3)
        self.assertAlmostEqual(s["recall"], 0.5)

    def test_threshold_boundary_is_positive(self):
        # >= semantics: exactly at threshold predicts a merge.
        s = em.score_summary(self.SCORED, 0.85)
        self.assertEqual((s["tp"], s["fp"]), (1, 2))
        self.assertEqual(s["fn"], 1)

    def test_false_merges_and_splits_named_separately(self):
        s = em.score_summary(self.SCORED, 0.5)
        fm = [(p["name_a"], p["name_b"]) for p in s["false_merges"]]
        fs = [(p["name_a"], p["name_b"]) for p in s["false_splits"]]
        self.assertEqual(fm, [("Diff High", "Diff High Alt"),
                              ("Diff Edge", "Diff Edge Alt")])
        self.assertEqual(fs, [("Same Low", "Same Low Alt")])

    def test_unanswered_excluded_from_denominators(self):
        s = em.score_summary(
            [_scored("A", "B", True, None)], 0.5)
        self.assertEqual(s["answered"], 0)
        self.assertEqual(s["unanswered"], 1)
        self.assertEqual(s["accuracy"], 0)


# ----------------------------------------------------------------------
# Dry run: stubbed endpoint, end to end
# ----------------------------------------------------------------------

class TestDryRun(unittest.TestCase):
    def _fixture(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "pairs.json"
        pairs = [
            {"name_a": "True Pair", "name_b": "True Pair Alt",
             "label": "same", "book": "Daemon",
             "desc_a": "a detective", "stub_score": 0.9},
            {"name_a": "Trap Pair", "name_b": "Trap Pair Alt",
             "label": "different", "book": "Daemon",
             "desc_a": "a detective", "stub_score": 0.95},
            {"name_a": "Missed Pair", "name_b": "Missed Pair Alt",
             "label": "same", "book": "Daemon",
             "desc_a": "a detective", "stub_score": 0.2},
            {"name_a": "Silent Pair", "name_b": "Silent Pair Alt",
             "label": "different", "book": "Daemon",
             "desc_a": "a detective", "stub_score": None},
            {"name_a": "Bare Pair", "name_b": "Bare Pair Alt",
             "label": "same", "book": "Daemon", "stub_score": 0.88},
        ]
        path.write_text(json.dumps(pairs), encoding="utf-8")
        return str(path)

    def _run(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = em.main(["--labels", self._fixture(),
                          "--dry-run", *extra])
        return rc, out.getvalue()

    def test_dry_run_end_to_end(self):
        rc, out = self._run("--prompt", "identity")
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("identity", out)
        self.assertIn("Unanswered: 1", out)
        # Both required thresholds are reported.
        self.assertIn("0.50", out)
        self.assertIn("0.85", out)
        # False merges / false splits are named, separately.
        self.assertIn("Trap Pair", out)
        self.assertIn("Missed Pair", out)
        # Name-only pairs are reported as such (1 of 5 here).
        self.assertIn("name-only", out)

    def test_dry_run_legacy_variant(self):
        rc, out = self._run("--prompt", "legacy")
        self.assertEqual(rc, 0)
        self.assertIn("legacy", out)


# ----------------------------------------------------------------------
# The shipped labelled pair set (eval/merge_pairs.json)
# ----------------------------------------------------------------------

class TestShippedPairSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pairs = em.load_pairs_file(str(_ROOT / "eval" /
                                           "merge_pairs.json"))

    def _find(self, name_a, name_b):
        for p in self.pairs:
            if {p["name_a"], p["name_b"]} == {name_a, name_b}:
                return p
        return None

    def test_size_and_class_balance(self):
        self.assertGreaterEqual(len(self.pairs), 100)
        same = sum(1 for p in self.pairs if p["expected"])
        diff = len(self.pairs) - same
        self.assertGreaterEqual(same, 45)
        self.assertGreaterEqual(diff, 45)

    def test_grouped_by_book(self):
        books = {}
        for p in self.pairs:
            self.assertTrue(p["book"])
            books[p["book"]] = books.get(p["book"], 0) + 1
        self.assertGreaterEqual(len(books), 2)
        self.assertTrue(all(n >= 5 for n in books.values()))

    def test_no_duplicate_pairs_per_book(self):
        seen = set()
        for p in self.pairs:
            key = (p["book"],
                   frozenset([p["name_a"].lower(), p["name_b"].lower()]))
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_surname_trap_and_nickname_pairs_present(self):
        pete = self._find("Pete Sebeck", "Peter Sebeck")
        self.assertIsNotNone(pete)
        self.assertTrue(pete["expected"])
        chris = self._find("Chris Sebeck", "Peter Sebeck")
        self.assertIsNotNone(chris)
        self.assertFalse(chris["expected"])

    def test_fourth_wing_pairs_present(self):
        violet = self._find("Violet", "Violet Sorrengail")
        self.assertIsNotNone(violet)
        self.assertTrue(violet["expected"])
        mira = self._find("Mira Sorrengail", "Violet Sorrengail")
        self.assertIsNotNone(mira)
        self.assertFalse(mira["expected"])


if __name__ == "__main__":
    unittest.main()
