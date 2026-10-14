"""Coverage for the merge-evaluation helper scripts (QA-scout
remediation, 2026-10-14).

evaluate-merges-batched.py underpins the batched-vs-single Jev verdict,
and label-merges.py's load_pairs produces the labelled pairs every
future eval rests on. Both were at zero coverage.

Hermetic: _post is the network boundary double; label fixtures are
synthetic preview JSONs in temp dirs. Stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


emb = _load("evaluate_merges_batched", "evaluate-merges-batched.py")
lm = _load("label_merges", "label-merges.py")


# ----------------------------------------------------------------------
# evaluate-merges-batched: _extract_pyes
# ----------------------------------------------------------------------

class TestExtractPyes(unittest.TestCase):
    def test_noul_score(self):
        self.assertEqual(emb._extract_pyes({"noul": 0.9}), 0.9)

    def test_noul_zero_is_a_score_not_missing(self):
        self.assertEqual(emb._extract_pyes({"noul": 0}), 0.0)

    def test_noul_one_boundary(self):
        self.assertEqual(emb._extract_pyes({"noul": 1}), 1.0)

    def test_probabilities_fallback(self):
        self.assertEqual(
            emb._extract_pyes({"probabilities": {"yes": 0.3}}), 0.3)

    def test_none_noul_falls_back(self):
        self.assertEqual(
            emb._extract_pyes({"noul": None,
                               "probabilities": {"yes": 0.4}}), 0.4)

    def test_missing_score_raises(self):
        with self.assertRaises(ValueError):
            emb._extract_pyes({})
        with self.assertRaises(ValueError):
            emb._extract_pyes({"probabilities": {}})

    def test_bool_rejected(self):
        with self.assertRaises(ValueError):
            emb._extract_pyes({"noul": True})

    def test_non_numeric_rejected(self):
        with self.assertRaises(ValueError):
            emb._extract_pyes({"noul": "0.9"})

    def test_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            emb._extract_pyes({"noul": 1.5})
        with self.assertRaises(ValueError):
            emb._extract_pyes({"noul": -0.1})

    def test_non_finite_rejected(self):
        with self.assertRaises(ValueError):
            emb._extract_pyes({"noul": float("nan")})
        with self.assertRaises(ValueError):
            emb._extract_pyes({"noul": float("inf")})


# ----------------------------------------------------------------------
# evaluate-merges-batched: metrics
# ----------------------------------------------------------------------

class TestBatchedMetrics(unittest.TestCase):
    def test_confusion_counts_and_rates(self):
        results = [(True, 0.9), (True, 0.4), (False, 0.8), (False, 0.2)]
        acc, prec, rec, tp, tn, fp, fn, total = emb.metrics(results, 0.85)
        self.assertEqual((tp, tn, fp, fn, total), (1, 2, 0, 1, 4))
        self.assertAlmostEqual(acc, 0.75)
        self.assertAlmostEqual(prec, 1.0)
        self.assertAlmostEqual(rec, 0.5)

    def test_score_at_threshold_counts_positive(self):
        # >= semantics: exactly at threshold is a positive call.
        _a, _p, _r, tp, _tn, _fp, fn, _t = emb.metrics([(True, 0.85)], 0.85)
        self.assertEqual((tp, fn), (1, 0))

    def test_none_scores_excluded(self):
        acc, prec, rec, tp, tn, fp, fn, total = emb.metrics(
            [(True, None), (False, None)], 0.85)
        self.assertEqual((tp, tn, fp, fn, total), (0, 0, 0, 0, 0))
        self.assertEqual((acc, prec, rec), (0, 0, 0))

    def test_empty(self):
        self.assertEqual(emb.metrics([], 0.85), (0, 0, 0, 0, 0, 0, 0, 0))


# ----------------------------------------------------------------------
# evaluate-merges-batched: ask_batched / ask_single / resolve_provider
# ----------------------------------------------------------------------

class TestAskBatched(unittest.TestCase):
    PAIRS = [("k1", {"a": "Alice", "b": "Alicia"}),
             ("k2", {"a": "Bob", "b": "Rob"})]

    def _run(self, fake_post, pairs=None):
        with mock.patch.object(emb, "_post", fake_post):
            return emb.ask_batched("http://x/v1", "m1", "key",
                                   pairs or self.PAIRS, {})

    def test_request_shape_and_parsing(self):
        seen = {}

        def fake_post(base_url, model, api_key, payload, headers, timeout):
            seen.update(payload=payload, timeout=timeout, model=model)
            return {"answers": {"pair_0": {"noul": 0.93},
                                "pair_1": {"noul": 0.1}}}

        out = self._run(fake_post)
        self.assertEqual(out, {"k1": 0.93, "k2": 0.1})
        payload = seen["payload"]
        self.assertEqual(seen["timeout"], 60)
        self.assertEqual(payload["model"], "m1")
        self.assertEqual(sorted(payload["questions"]), ["pair_0", "pair_1"])
        for q in payload["questions"].values():
            self.assertEqual(q["type"], "noul")
        self.assertIn("Pair 1:", payload["state"])
        self.assertIn("Pair 2:", payload["state"])
        self.assertIn("Alice", payload["state"])
        self.assertIn("Rob", payload["state"])

    def test_bad_answer_yields_none_for_that_pair_only(self):
        def fake_post(*a):
            return {"answers": {"pair_0": {"noul": 0.93},
                                "pair_1": {"noul": "bogus"}}}

        out = self._run(fake_post)
        self.assertEqual(out, {"k1": 0.93, "k2": None})

    def test_missing_answer_yields_none(self):
        def fake_post(*a):
            return {"answers": {"pair_0": {"noul": 0.5}}}

        out = self._run(fake_post)
        self.assertEqual(out, {"k1": 0.5, "k2": None})

    def test_transport_failure_yields_all_none(self):
        def fake_post(*a):
            raise RuntimeError("boom")

        self.assertEqual(self._run(fake_post), {"k1": None, "k2": None})


class TestAskSingle(unittest.TestCase):
    def test_baseline_path(self):
        seen = {}

        def fake_post(base_url, model, api_key, payload, headers, timeout):
            seen.update(payload=payload, timeout=timeout)
            return {"answers": {"same_person": {"noul": 0.7}}}

        with mock.patch.object(emb, "_post", fake_post):
            out = emb.ask_single("http://x/v1", "m1", "key",
                                 "Alice", "Alicia", {})
        self.assertEqual(out, 0.7)
        self.assertEqual(seen["timeout"], 30)
        self.assertEqual(list(seen["payload"]["questions"]),
                         ["same_person"])
        self.assertIn("Alice", seen["payload"]["state"])


class TestResolveProvider(unittest.TestCase):
    def test_openrouter_needs_env_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                emb.resolve_provider("openrouter", "", "")

    def test_openrouter_from_env(self):
        with mock.patch.dict(os.environ,
                             {"OPENROUTER_API_KEY": "sk-test"}, clear=True):
            base, model, key, headers = emb.resolve_provider(
                "openrouter", "", "")
        self.assertEqual(base, emb.OPENROUTER_SYSTEMONE_BASE)
        self.assertEqual(model, emb.OPENROUTER_DECISION_MODEL)
        self.assertEqual(key, "sk-test")
        self.assertEqual(headers, {"X-Title": emb.OPENROUTER_ATTRIBUTION_TITLE})

    def test_local_passthrough(self):
        self.assertEqual(
            emb.resolve_provider("local", "http://127.0.0.1:8888/v1", "m"),
            ("http://127.0.0.1:8888/v1", "m", "", {}))


# ----------------------------------------------------------------------
# label-merges: load_pairs
# ----------------------------------------------------------------------

class TestLoadPairs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, characters):
        p = self.td / "preview.json"
        p.write_text(json.dumps({"characters": characters}),
                     encoding="utf-8")
        return str(p)

    def test_alias_and_similar_pairs(self):
        path = self._write([
            {"name": "Peter Sebeck", "description": "detective",
             "aliases": ["Pete Sebeck", {"name": "Petey"}]},
            {"name": "Peter Parker", "description": "photographer",
             "aliases": []},
            {"name": "Mary Sebeck", "description": "widow", "aliases": []},
        ])
        pairs = lm.load_pairs(path)
        got = [(p["a"]["name"], p["b"]["name"], p["source"]) for p in pairs]
        self.assertEqual(got, [
            ("Peter Sebeck", "Pete Sebeck", "alias"),
            ("Peter Sebeck", "Petey", "alias"),
            ("Peter Sebeck", "Peter Parker", "similar"),   # shared "peter"
            ("Peter Sebeck", "Mary Sebeck", "similar"),    # shared "sebeck"
        ])

    def test_max_pairs_honoured(self):
        path = self._write([
            {"name": "Peter Sebeck", "description": "d",
             "aliases": ["Pete Sebeck", {"name": "Petey"}]},
            {"name": "Peter Parker", "description": "p", "aliases": []},
        ])
        self.assertEqual(len(lm.load_pairs(path, max_pairs=2)), 2)
        self.assertEqual(len(lm.load_pairs(path, max_pairs=1)), 1)

    def test_description_truncated_to_200(self):
        path = self._write([
            {"name": "Mary Sebeck", "description": "x" * 300,
             "aliases": ["Mary S"]},
        ])
        pairs = lm.load_pairs(path)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(len(pairs[0]["a"]["desc"]), 200)

    def test_self_pair_and_duplicate_pair_skipped(self):
        path = self._write([
            {"name": "Peter Sebeck", "description": "d",
             "aliases": ["Peter Sebeck", "Pete Sebeck"]},
            # Standalone Pete: the similar-name pass would re-propose
            # (Peter Sebeck, Pete Sebeck) — dedupe keeps the alias one.
            {"name": "Pete Sebeck", "description": "d2", "aliases": []},
        ])
        pairs = lm.load_pairs(path)
        got = [(p["a"]["name"], p["b"]["name"], p["source"]) for p in pairs]
        self.assertEqual(got, [("Peter Sebeck", "Pete Sebeck", "alias")])

    def test_malformed_entries_handled(self):
        path = self._write([
            {"name": "Lone Wolf", "aliases": [{"bogus": 1}, "", "Wolf"]},
            {"name": "No Alias Key"},
            {"name": "No Desc", "aliases": ["ND"]},
        ])
        pairs = lm.load_pairs(path)  # must not raise
        got = [(p["a"]["name"], p["b"]["name"]) for p in pairs]
        self.assertIn(("Lone Wolf", "Wolf"), got)
        self.assertIn(("No Desc", "ND"), got)
        nd = [p for p in pairs if p["a"]["name"] == "No Desc"][0]
        self.assertEqual(nd["a"]["desc"], "")

    def test_no_characters_no_pairs(self):
        self.assertEqual(lm.load_pairs(self._write([])), [])


if __name__ == "__main__":
    unittest.main()
