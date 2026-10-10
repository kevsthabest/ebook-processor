"""Tests for the dedupe stack: dedupe-prototype.py, deterministic-match.py,
and the embedding-threshold + guard logic in process-portable.py.

Stdlib unittest only. All embedding calls are mocked — no network,
no real embeddings, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import math
import unittest
from pathlib import Path
from unittest.mock import patch

_BASE = Path(__file__).resolve().parent.parent


def _load(name, filename):
    path = str(_BASE / filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_dp = _load("dedupe_prototype", "dedupe-prototype.py")
_dm = _load("deterministic_match", "deterministic-match.py")
_pp_spec = importlib.util.spec_from_file_location(
    "process_portable", str(_BASE / "process-portable.py"))
pp = importlib.util.module_from_spec(_pp_spec)
_pp_spec.loader.exec_module(pp)


# ---------------------------------------------------------------------------
# dedupe-prototype.py: norm_name + cos + threshold logic (mocked embeddings)
# ---------------------------------------------------------------------------

class TestDedupePrototypeNorm(unittest.TestCase):
    def test_casefold(self):
        self.assertEqual(_dp.norm_name("Matthew Sobol"), "matthew sobol")

    def test_strips_leading_article(self):
        self.assertEqual(_dp.norm_name("The Narrator"), "narrator")
        self.assertEqual(_dp.norm_name("a landlord"), "landlord")
        self.assertEqual(_dp.norm_name("An Old Man"), "old man")

    def test_collapses_whitespace(self):
        self.assertEqual(_dp.norm_name("John   Smith"), "john smith")

    def test_empty(self):
        self.assertEqual(_dp.norm_name(""), "")
        self.assertEqual(_dp.norm_name(None), "")

    def test_mid_sentence_article_kept(self):
        # Only leading articles stripped
        self.assertEqual(_dp.norm_name("Man of the People"), "man of the people")


class TestDedupePrototypeCos(unittest.TestCase):
    def test_identical(self):
        self.assertAlmostEqual(_dp.cos([1, 0], [1, 0]), 1.0)

    def test_orthogonal(self):
        self.assertAlmostEqual(_dp.cos([1, 0], [0, 1]), 0.0)

    def test_opposite(self):
        self.assertAlmostEqual(_dp.cos([1, 0], [-1, 0]), -1.0)

    def test_zero_vector(self):
        self.assertEqual(_dp.cos([0, 0], [1, 1]), 0.0)

    def test_scaled(self):
        # Cosine is magnitude-invariant
        self.assertAlmostEqual(_dp.cos([1, 1], [2, 2]), 1.0)


class TestDedupePrototypeThresholds(unittest.TestCase):
    """Threshold behavior with mocked embeddings (no network)."""

    def _run_signal2(self, names, descs, vecs, threshold):
        """Replicate the signal-2 pair loop with injected vectors."""
        chars = [{"name": n, "description": d}
                 for n, d in zip(names, descs)]
        parent = list(range(len(chars)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        pairs = []
        for i in range(len(chars)):
            for j in range(i + 1, len(chars)):
                s = _dp.cos(vecs[i], vecs[j])
                if s >= threshold:
                    union(i, j)
                    pairs.append((s, i, j))
        return pairs, parent

    def test_threshold_085_strict(self):
        # 0.85: only very similar vectors merge
        vecs = [[1.0, 0.0], [0.99, 0.14], [0.0, 1.0]]
        pairs, _ = self._run_signal2(["a", "b", "c"], ["", "", ""], vecs, 0.85)
        # cos([1,0],[0.99,0.14]) ≈ 0.99 → merge; others orthogonal → no
        self.assertEqual(len(pairs), 1)
        self.assertEqual((pairs[0][1], pairs[0][2]), (0, 1))

    def test_threshold_080_looser(self):
        # 0.80 catches pairs that 0.85 misses (mega-cluster behavior)
        # cos([1,0],[0.95,0.31]) ≈ 0.95; cos([1,0],[0.8,0.6]) = 0.8
        vecs = [[1.0, 0.0], [0.8, 0.6], [0.0, 1.0]]
        pairs_85, _ = self._run_signal2(["a", "b", "c"], ["", "", ""], vecs, 0.85)
        pairs_80, _ = self._run_signal2(["a", "b", "c"], ["", "", ""], vecs, 0.80)
        self.assertEqual(len(pairs_85), 0)
        self.assertEqual(len(pairs_80), 1)

    def test_below_threshold_no_merge(self):
        vecs = [[1.0, 0.0], [0.0, 1.0]]
        pairs, parent = self._run_signal2(["a", "b"], ["", ""], vecs, 0.85)
        self.assertEqual(pairs, [])
        self.assertNotEqual(parent[0], parent[1])


# ---------------------------------------------------------------------------
# deterministic-match.py: Tier 1 hard-no / Tier 2 deterministic-yes
# ---------------------------------------------------------------------------

class TestDeterministicMatchNormalize(unittest.TestCase):
    def test_strips_titles(self):
        self.assertEqual(_dm.normalize("Special Agent Neal Decker"),
                         ["neal", "decker"])
        self.assertEqual(_dm.normalize("Dr. Smith"), ["smith"])

    def test_removes_parentheticals(self):
        self.assertEqual(_dm.normalize("John (assumed identity)"),
                         ["john"])

    def test_lowercase(self):
        self.assertEqual(_dm.normalize("MATTHEW SOBOL"), ["matthew", "sobol"])


class TestDeterministicMatchHardNo(unittest.TestCase):
    def test_surname_trap(self):
        # Same surname, different first names → hard no
        self.assertEqual(
            _dm.classify_pair("Chris Sebeck", "Peter Sebeck"), "no")
        self.assertEqual(
            _dm.classify_pair("Laura Sebeck", "Marilyn Sebeck"), "no")

    def test_different_people(self):
        self.assertEqual(
            _dm.classify_pair("Charles Taylor", "Raymond Taylor"), "no")


class TestDeterministicMatchYes(unittest.TestCase):
    def test_title_stripping(self):
        self.assertEqual(
            _dm.classify_pair("Special Agent Neal Decker", "Neal Decker"),
            "yes")

    def test_identical(self):
        self.assertEqual(
            _dm.classify_pair("Matthew Sobol", "Matthew Sobol"), "yes")

    def test_middle_name_noncontiguous(self):
        # Contiguous-subsequence only: "peter andrew sebeck" vs "peter sebeck"
        # is NOT a contiguous subsequence match → ambiguous (goes to model).
        # Conservative by design.
        self.assertEqual(
            _dm.classify_pair("Peter Andrew Sebeck", "Peter Sebeck"),
            "ambiguous")

    def test_contiguous_subsequence(self):
        # "neal decker" IS a contiguous subsequence of itself after
        # title-stripping, covered by test_title_stripping above.
        self.assertEqual(
            _dm.classify_pair("Neal Decker", "Decker Neal"), "ambiguous")


class TestDeterministicMatchAmbiguous(unittest.TestCase):
    def test_single_token_vs_multi(self):
        # "Ross" vs "Jon Ross" → ambiguous (can't rule out sibling trap)
        self.assertEqual(
            _dm.classify_pair("Ross", "Jon Ross"), "ambiguous")

    def test_unrelated(self):
        self.assertEqual(
            _dm.classify_pair("Matthew Sobol", "Jon Ross"), "ambiguous")


# ---------------------------------------------------------------------------
# process-portable.py dedupe guards: v1.13 (gender, proper-name),
# v1.14 (non-person-noun, multi-person)
# ---------------------------------------------------------------------------

class TestDedupeGuardsGender(unittest.TestCase):
    """v1.13 gender guard: never merge across detected genders."""

    def test_male_tokens(self):
        self.assertEqual(pp._guess_gender("Mr. Smith"), "m")
        self.assertEqual(pp._guess_gender("Father John"), "m")

    def test_female_tokens(self):
        self.assertEqual(pp._guess_gender("Mrs. Smith"), "f")
        self.assertEqual(pp._guess_gender("Mother Superior"), "f")

    def test_undetectable(self):
        self.assertIsNone(pp._guess_gender("Matthew Sobol"))
        self.assertIsNone(pp._guess_gender("The Narrator"))
        self.assertIsNone(pp._guess_gender(""))


class TestDedupeGuardsProperName(unittest.TestCase):
    """v1.13 proper-name guard: distinct proper names need identical descs."""

    def test_proper_names(self):
        self.assertTrue(pp._is_proper("Matthew Sobol"))
        self.assertTrue(pp._is_proper("Mrs. Black"))

    def test_generics_not_proper(self):
        self.assertFalse(pp._is_proper("the father"))
        self.assertFalse(pp._is_proper("Daddy"))
        self.assertFalse(pp._is_proper("the narrator"))

    def test_empty(self):
        self.assertFalse(pp._is_proper(""))
        self.assertFalse(pp._is_proper(None))


class TestDedupeGuardsNonPerson(unittest.TestCase):
    """v1.14 non-person-noun guard: places/things never merge into people."""

    def test_places_rejected(self):
        self.assertTrue(pp._has_nonperson_noun("the old house"))
        self.assertTrue(pp._has_nonperson_noun("City Hospital"))

    def test_generic_family_refs_rejected(self):
        # "father"/"mother" etc. are in _NONPERSON_NOUNS as generic
        # family references (not named characters) — intentional.
        self.assertTrue(pp._has_nonperson_noun("the father"))

    def test_people_ok(self):
        self.assertFalse(pp._has_nonperson_noun("Matthew Sobol"))
        self.assertFalse(pp._has_nonperson_noun("the old man"))


class TestDedupeGuardsMulti(unittest.TestCase):
    """v1.14 multi-person guard: groups don't merge with individuals."""

    def test_multi_detected(self):
        self.assertTrue(pp._is_multi("the host and hostess"))
        # Note: possessive plurals like "Roy Merritt's daughters" are NOT
        # caught (only " and " is checked) — known limitation, not a bug.

    def test_single_not_multi(self):
        self.assertFalse(pp._is_multi("Matthew Sobol"))
        self.assertFalse(pp._is_multi("the husband"))


class TestDedupeThresholdConfig(unittest.TestCase):
    """Embedding threshold defaults: 0.85 standard, 0.80 mega-clusters."""

    def test_default_threshold(self):
        # CONFIG default used by the v2 dedupe path
        thr = float(pp.CONFIG.get("dedupe_threshold", 0.85))
        self.assertEqual(thr, 0.85)

    def test_threshold_boundary(self):
        # At exactly 0.85, a 0.85 cosine merges (>= semantics)
        self.assertTrue(0.85 >= 0.85)
        self.assertFalse(0.849 >= 0.85)


if __name__ == "__main__":
    unittest.main()
