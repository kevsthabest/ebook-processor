"""Shadow relationship discovery (spec §3, v2.13.23).

What is pinned here:
- The §3.2 marker-coverage audit: the production miner's kinship
  markers grade kinship constructions binding and every
  non-kinship category at zero; discovery's extended vocabulary
  grades all of them binding WITHOUT weakening precision —
  co-presence and third-party bindings still never grade binding
  (the K6 line). The measured numbers are recorded in
  docs/discovery-marker-audit.md.
- §3.1: the one-pass sentence index, candidate pairs (buckets
  with no surviving claim), T1/T2 tiers, the per-book cap by
  bucket strength.
- §3.2: the miner pre-filter advances only pairs with a
  binding-grade sentence, at zero judge calls.
- §3.4: one closed-set discovery call per advancing pair (full
  registry vocabulary + explicit "none"), then the standard
  validator against the best sentence, as a shadow record only.
- §3.5/§3.7: shadow ONLY — inputs unmutated, nothing written,
  flag off means the gate returns None and touches nothing.
- §3.6: the separate experimental allowance is spent and
  reported; the shared pool is never claimed.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import copy
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _roster(*names):
    roster = {}
    for n in names:
        pp._roster_update(
            roster,
            [{"name": n, "aliases": [], "role": "supporting",
              "description": "", "evidence": ""}],
            1)
    return roster


def _chs(*texts):
    return [{"index": i + 1, "label": f"Ch{i + 1}", "text": t}
            for i, t in enumerate(texts)]


_FW = ("Violet Sorrengail", "Xaden Riorson", "Fen Riorson",
       "Mira Sorrengail", "Brennan Sorrengail", "Liam Mairi",
       "Dain Aetos", "Jack Barlowe", "Imogen Cardulo",
       "Professor Kaori")


class TestMarkerCoverageAudit(unittest.TestCase):
    """The labelled set from docs/discovery-marker-audit.md: every
    sentence states its bond in a binding construction."""

    CASES = [
        ("kinship", "Xaden's father, Fen Riorson, had led the "
         "rebellion years before.", "Fen Riorson", "Xaden Riorson"),
        ("kinship", "Fen Riorson was Xaden's father, and the "
         "rebellion died with him.", "Fen Riorson", "Xaden Riorson"),
        ("kinship", "Violet hugged her brother Brennan before the "
         "ceremony.", "Violet Sorrengail", "Brennan Sorrengail"),
        ("kinship", "Mira Sorrengail, sister of Violet Sorrengail, "
         "addressed the cadets.", "Mira Sorrengail",
         "Violet Sorrengail"),
        ("ally", "Violet's ally, Xaden Riorson, held the western "
         "pass.", "Violet Sorrengail", "Xaden Riorson"),
        ("ally", "Xaden Riorson was Violet's ally in the council "
         "vote.", "Violet Sorrengail", "Xaden Riorson"),
        ("ally", "She turned to her ally, Xaden Riorson, for "
         "support.", "Violet Sorrengail", "Xaden Riorson"),
        ("rival", "Xaden's rival, Dain Aetos, watched from the "
         "parapet.", "Xaden Riorson", "Dain Aetos"),
        ("rival", "Dain Aetos, her rival, had challenged Violet at "
         "dawn.", "Violet Sorrengail", "Dain Aetos"),
        ("rival", "Violet's rival is Dain Aetos, by every cadet's "
         "reckoning.", "Violet Sorrengail", "Dain Aetos"),
        ("mentor", "Violet's mentor, Professor Kaori, corrected "
         "her stance.", "Violet Sorrengail", "Professor Kaori"),
        ("mentor", "Professor Kaori was Violet's mentor through "
         "her first year.", "Violet Sorrengail", "Professor Kaori"),
        ("mentor", "She trusted her mentor, Professor Kaori, "
         "completely.", "Violet Sorrengail", "Professor Kaori"),
        ("friend", "Violet's friend, Imogen Cardulo, shared her "
         "rations.", "Violet Sorrengail", "Imogen Cardulo"),
        ("friend", "Imogen Cardulo was Violet's friend long before "
         "the quadrant.", "Violet Sorrengail", "Imogen Cardulo"),
        ("friend", "Liam Mairi, his friend, stood watch with "
         "Xaden.", "Xaden Riorson", "Liam Mairi"),
        ("romantic", "Violet's lover, Xaden Riorson, waited by "
         "the gate.", "Violet Sorrengail", "Xaden Riorson"),
        ("romantic", "Xaden Riorson was Violet's beloved, though "
         "neither said it aloud.", "Violet Sorrengail",
         "Xaden Riorson"),
        ("romantic", "She kissed her boyfriend, Xaden Riorson, "
         "goodbye.", "Violet Sorrengail", "Xaden Riorson"),
        ("enemy", "Violet's enemy, Jack Barlowe, blocked the "
         "bridge.", "Violet Sorrengail", "Jack Barlowe"),
        ("enemy", "Jack Barlowe was Violet's enemy from the first "
         "day.", "Violet Sorrengail", "Jack Barlowe"),
        ("enemy", "His nemesis, Jack Barlowe, had sworn to see "
         "Violet fall.", "Violet Sorrengail", "Jack Barlowe"),
    ]
    NEGATIVES = [
        ("Violet and Xaden stood in the courtyard.",
         "Violet Sorrengail", "Xaden Riorson", "plain"),
        ("Violet watched Xaden's father from the wall.",
         "Violet Sorrengail", "Xaden Riorson", "co-occurrence"),
        ("Her brother Brennan waved at Violet and Xaden.",
         "Violet Sorrengail", "Xaden Riorson", "co-occurrence"),
    ]

    @classmethod
    def setUpClass(cls):
        cls.roster = _roster(*_FW)

    def _grades(self, sentence, a, b):
        _, aa = pp._pair_aug_res(a, self.roster)
        _, ab = pp._pair_aug_res(b, self.roster)
        return (pp._pair_sentence_grade(sentence, aa, ab),
                pp._discovery_sentence_grade(sentence, aa, ab))

    def test_kinship_coverage_is_full_under_both_vocabularies(self):
        for cat, s, a, b in self.CASES:
            if cat != "kinship":
                continue
            kin, ext = self._grades(s, a, b)
            self.assertEqual(kin, "binding", s)
            self.assertEqual(ext, "binding", s)
            self.assertTrue(pp._PAIR_MARKER_RE.search(s), s)

    def test_nonkinship_gap_under_kinship_markers(self):
        # The audit's headline: 0/18. A kinship-only pre-filter
        # would discover family bonds and nothing else.
        for cat, s, a, b in self.CASES:
            if cat == "kinship":
                continue
            kin, _ = self._grades(s, a, b)
            self.assertNotEqual(kin, "binding", (cat, s))
            self.assertFalse(pp._PAIR_MARKER_RE.search(s), (cat, s))

    def test_extended_vocabulary_covers_every_category(self):
        for cat, s, a, b in self.CASES:
            _, ext = self._grades(s, a, b)
            self.assertEqual(ext, "binding", (cat, s))

    def test_copresence_never_grades_binding(self):
        # K6 precision: extending the marker vocabulary must not
        # turn co-presence, or a marker bound to a third party,
        # into binding evidence — under either vocabulary.
        for s, a, b, want in self.NEGATIVES:
            kin, ext = self._grades(s, a, b)
            self.assertEqual(kin, want, s)
            self.assertEqual(ext, want, s)


class StubDM:
    """Decision-model stub recording every call it receives."""

    model = "stub"

    def __init__(self, dtype="ally", dp=0.9, vp=0.95, direction="sym"):
        self.dtype, self.dp, self.vp = dtype, dp, vp
        self.direction = direction
        self.discovery_calls = []
        self.validate_calls = []

    def ask_discovery(self, a, b, sentences):
        self.discovery_calls.append((a, b, tuple(sentences)))
        if self.dtype is None:
            return None, {"p": 0.1, "direction": "sym", "top": []}
        return self.dtype, {"p": self.dp, "direction": self.direction,
                            "top": []}

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.validate_calls.append((frm, to, rtype, quote))
        return self.vp >= pp._REL_VALIDATION_THRESHOLD, self.vp


def _book():
    """Two discoverable pairs: Fen/Xaden (kinship binding) and
    Violet/Xaden (extended ally binding); no surviving claims."""
    roster = _roster("Violet Sorrengail", "Xaden Riorson",
                     "Fen Riorson", "Mira Sorrengail")
    chs = _chs(
        "Xaden's father, Fen Riorson, had led the rebellion years "
        "before. Violet Sorrengail watched the courtyard.",
        "Violet's ally, Xaden Riorson, held the western pass. "
        "Mira Sorrengail smiled at the cadets.")
    return roster, chs


class TestIndexAndCandidates(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()

    def tearDown(self):
        pp._dm_budget_reset()

    def test_one_pass_index_buckets_pairs(self):
        roster, chs = _book()
        buckets, display = pp._discovery_sentence_index(roster, chs)
        key = tuple(sorted((pp.norm_name("Fen Riorson"),
                            pp.norm_name("Xaden Riorson"))))
        self.assertIn(key, buckets)
        self.assertEqual(len(buckets[key]), 1)
        self.assertEqual(display[key[0]],
                         "Fen Riorson" if key[0].startswith("fen")
                         else "Xaden Riorson")

    def test_three_names_in_one_sentence_make_three_buckets(self):
        roster = _roster("Alice A", "Bob B", "Carol C")
        chs = _chs("Alice A met Bob B and Carol C at the gate.")
        buckets, _ = pp._discovery_sentence_index(roster, chs)
        self.assertEqual(len(buckets), 3)

    def test_surviving_claim_pairs_are_not_candidates(self):
        roster, chs = _book()
        cands = pp._discovery_candidates(
            roster, chs,
            {tuple(sorted((pp.norm_name("Fen Riorson"),
                            pp.norm_name("Xaden Riorson"))))},
            set())
        pairs = {(c["norm_a"], c["norm_b"]) for c in cands}
        self.assertNotIn(tuple(sorted(
            (pp.norm_name("Fen Riorson"),
             pp.norm_name("Xaden Riorson")))), pairs)
        self.assertTrue(pairs)  # the other pair remains

    def test_tiers(self):
        roster, chs = _book()
        cands = pp._discovery_candidates(
            roster, chs, set(), {pp.norm_name("Violet Sorrengail")})
        by_pair = {(c["name_a"], c["name_b"]): c["tier"]
                   for c in cands}
        vx = tuple(sorted(("Violet Sorrengail", "Xaden Riorson")))
        fx = tuple(sorted(("Fen Riorson", "Xaden Riorson")))
        self.assertEqual(by_pair[vx], "T1")
        self.assertEqual(by_pair[fx], "T2")

    def test_prefilter_advances_binding_only_at_zero_judge_calls(self):
        # The pre-filter is deterministic text work: an exploding
        # validator standing in the module global must not matter.
        old = pp._DECISION_VALIDATOR

        class Boom:
            def __getattr__(self, name):
                raise AssertionError("judge called during pre-filter")
        pp._DECISION_VALIDATOR = Boom()
        try:
            roster, chs = _book()
            cands = pp._discovery_candidates(roster, chs, set(), set())
            for c in cands:
                graded = pp._discovery_grade_bucket(
                    c["name_a"], c["name_b"], c["bucket"], roster)
                self.assertTrue(
                    any(g["grade"] == "binding" for g in graded),
                    c["name_a"])
        finally:
            pp._DECISION_VALIDATOR = old


class TestShadowRun(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()
        self._old_validator = pp._DECISION_VALIDATOR

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_shadow_keep_flow_and_report(self):
        roster, chs = _book()
        stub = StubDM()
        report = pp._discovery_shadow_run(roster, chs, [],
                                          validator=stub)
        self.assertEqual(report["mode"], "shadow")
        self.assertEqual(report["writes"], 0)
        self.assertEqual(report["pairs_considered"], 2)
        self.assertEqual(report["pairs_with_binding"], 2)
        self.assertEqual(report["pairs_judged"], 2)
        keeps = [r for r in report["records"]
                 if r["verdict"] == "shadow_keep"]
        self.assertEqual(len(keeps), 2)
        self.assertTrue(all(r["discovery_type"] == "ally"
                            for r in keeps))
        self.assertTrue(all(r["validator_p"] == 0.95 for r in keeps))
        # One discovery call + one validator call per pair.
        self.assertEqual(len(stub.discovery_calls), 2)
        self.assertEqual(len(stub.validate_calls), 2)
        self.assertEqual(report["budget"]["spent"], 4)
        self.assertEqual(report["budget"]["mode"],
                         "experimental_allowance")
        # The shared pool was never claimed.
        snap = pp._dm_budget_snapshot()
        self.assertEqual(
            snap.get("discovery", {}).get("spent", 0), 0)

    def test_inputs_are_not_mutated(self):
        roster, chs = _book()
        rels = [{"from": "Mira Sorrengail", "to": "Violet Sorrengail",
                 "type": "sibling", "evidence": "ev"}]
        roster_before = copy.deepcopy(
            {k: (v.get("name"), set(v.get("aliases", [])))
             for k, v in roster.items()})
        rels_before = copy.deepcopy(rels)
        pp._discovery_shadow_run(roster, chs, rels,
                                 validator=StubDM())
        self.assertEqual(
            {k: (v.get("name"), set(v.get("aliases", [])))
             for k, v in roster.items()}, roster_before)
        self.assertEqual(rels, rels_before)

    def test_copresence_only_pair_never_reaches_the_judge(self):
        # The K6 recorded unreachable: heavy co-presence, no stated
        # bond — considered, never advanced, zero judge calls.
        roster = _roster("Jon Ross", "Peter Sebeck")
        chs = _chs("Jon Ross and Peter Sebeck stood together in "
                   "the yard. Jon Ross nodded at Peter Sebeck and "
                   "walked on.")
        stub = StubDM()
        report = pp._discovery_shadow_run(roster, chs, [],
                                          validator=stub)
        self.assertEqual(report["pairs_considered"], 1)
        self.assertEqual(report["pairs_with_binding"], 0)
        self.assertEqual(report["pairs_judged"], 0)
        self.assertEqual(stub.discovery_calls, [])
        self.assertEqual(report["budget"]["spent"], 0)

    def test_none_verdict_spends_no_validator_call(self):
        roster, chs = _book()
        stub = StubDM(dtype=None)
        report = pp._discovery_shadow_run(roster, chs, [],
                                          validator=stub)
        self.assertTrue(all(r["verdict"] == "none"
                            for r in report["records"]))
        self.assertEqual(stub.validate_calls, [])
        self.assertEqual(report["budget"]["spent"], 2)

    def test_validator_drop_is_a_shadow_drop(self):
        roster, chs = _book()
        stub = StubDM(vp=0.1)  # ally is non-family: < 0.30 drops
        report = pp._discovery_shadow_run(roster, chs, [],
                                          validator=stub)
        self.assertTrue(all(r["verdict"] == "shadow_drop"
                            for r in report["records"]))
        self.assertTrue(all(r["validator_outcome"] == "drop"
                            for r in report["records"]))

    def test_directed_winner_direction_is_carried(self):
        # A "ba" winner is validated in the proposed direction:
        # the record's from/to are the swapped pair, and the
        # validator was called with exactly the recorded pairs.
        roster, chs = _book()
        stub = StubDM(dtype="parent", direction="ba")
        report = pp._discovery_shadow_run(roster, chs, [],
                                          validator=stub)
        called = {(c[0], c[1]) for c in stub.validate_calls}
        rec_pairs = {(r["from"], r["to"]) for r in report["records"]}
        self.assertEqual(called, rec_pairs)
        # Bucket order is (Fen, Xaden) / (Violet, Xaden); "ba"
        # must have flipped at least the Fen pair's direction.
        self.assertIn(("Xaden Riorson", "Fen Riorson"), rec_pairs)

    def test_no_validator_records_without_spending(self):
        roster, chs = _book()
        pp._DECISION_VALIDATOR = None
        report = pp._discovery_shadow_run(roster, chs, [],
                                          validator=None)
        self.assertTrue(all(r["verdict"] == "no_validator"
                            for r in report["records"]))
        self.assertEqual(report["budget"]["spent"], 0)

    def test_cap_by_bucket_strength(self):
        roster, chs = _book()
        # Give Fen/Xaden a second binding sentence (stronger bucket).
        chs.append({"index": 3, "label": "Ch3",
                    "text": "Fen Riorson was Xaden's father, and "
                            "the rebellion died with him."})
        report = pp._discovery_shadow_run(
            roster, chs, [], validator=StubDM(), max_pairs=1)
        self.assertEqual(report["pairs_judged"], 1)
        self.assertEqual(report["pairs_capped_out"], 1)
        rec = report["records"][0]
        self.assertEqual({rec["from"], rec["to"]},
                         {"Fen Riorson", "Xaden Riorson"})
        self.assertEqual(rec["binding_count"], 2)

    def test_allowance_exhaustion_is_reported_not_hidden(self):
        roster, chs = _book()
        report = pp._discovery_shadow_run(
            roster, chs, [], validator=StubDM(), allowance_limit=1)
        self.assertEqual(report["budget"]["spent"], 1)
        self.assertGreaterEqual(report["budget"]["denied"], 1)
        self.assertTrue(all(r["verdict"] == "budget_exhausted"
                            for r in report["records"]))

    def test_evidence_prefers_two_chapters(self):
        roster = _roster("Violet Sorrengail", "Xaden Riorson")
        chs = _chs(
            "Violet's ally, Xaden Riorson, held the pass.",
            "Violet turned to her ally, Xaden Riorson, for "
            "support.")
        report = pp._discovery_shadow_run(roster, chs, [],
                                          validator=StubDM())
        rec = report["records"][0]
        self.assertEqual(len(rec["evidence"]), 2)
        self.assertEqual({e["chapter"] for e in rec["evidence"]},
                         {1, 2})


class TestClosedSetCall(unittest.TestCase):
    """DecisionValidator.ask_discovery: one batched request
    carrying the whole vocabulary, or it is not one call."""

    def _validator_with_scores(self, scores):
        v = pp.DecisionValidator("http://127.0.0.1:1/v1")
        calls = []

        def fake_batch(validator, questions, state, label=""):
            calls.append((dict(questions), state))
            return {k: scores.get(k, 0.05) for k in questions}
        return v, calls, fake_batch

    def test_one_call_full_vocabulary_and_none(self):
        v, calls, fake = self._validator_with_scores({})
        old = pp._ask_noul_batch
        pp._ask_noul_batch = fake
        try:
            dtype, detail = v.ask_discovery(
                "Alice A", "Bob B", ["Alice A met Bob B."])
        finally:
            pp._ask_noul_batch = old
        self.assertEqual(len(calls), 1)  # ONE request
        questions = calls[0][0]
        ids = {e["id"] for e in pp._rel_vocab()["types"]}
        asked = {k.split("::")[0] for k in questions}
        self.assertEqual(asked, ids)  # full vocabulary offered
        for e in pp._rel_vocab()["types"]:
            if e["direction"] == "directed":
                self.assertIn(f"{e['id']}::ab", questions)
                self.assertIn(f"{e['id']}::ba", questions)
            else:
                self.assertIn(f"{e['id']}::sym", questions)
        # Every score below the threshold: explicit "none".
        self.assertIsNone(dtype)
        self.assertIsNotNone(detail)

    def test_winner_and_direction(self):
        v, calls, fake = self._validator_with_scores(
            {"parent::ba": 0.9})
        old = pp._ask_noul_batch
        pp._ask_noul_batch = fake
        try:
            dtype, detail = v.ask_discovery(
                "Alice A", "Bob B", ["Some binding sentence."])
        finally:
            pp._ask_noul_batch = old
        self.assertEqual(dtype, "parent")
        self.assertEqual(detail["direction"], "ba")
        self.assertEqual(detail["p"], 0.9)

    def test_error_is_not_a_none_verdict(self):
        v = pp.DecisionValidator("http://127.0.0.1:1/v1")
        old = pp._ask_noul_batch
        pp._ask_noul_batch = lambda *a, **k: None
        try:
            dtype, detail = v.ask_discovery("A A", "B B", ["s"])
        finally:
            pp._ask_noul_batch = old
        self.assertIsNone(dtype)
        self.assertIsNone(detail)


class TestEmbeddingArm(unittest.TestCase):
    def _embedder(self, texts):
        out = []
        for t in texts:
            if "allies" in t or "ally," in t or "her ally" in t:
                out.append([1.0, 0.0, 0.0])
            elif "rivals" in t:
                out.append([0.0, 1.0, 0.0])
            elif "rival" in t:
                out.append([0.0, 0.3, 0.95])
            else:
                out.append([1.0, 1.0, 0.0])
        return out

    def test_embedding_arm_can_reorder_evidence(self):
        roster = _roster("Violet Sorrengail", "Xaden Riorson")
        chs = _chs(
            "Violet's rival, Xaden Riorson, sneered at her.",
            "Violet met Xaden Riorson by the gate at noon.",
            "Violet turned to her ally, Xaden Riorson, for "
            "support.")
        miner = pp._discovery_shadow_run(
            roster, chs, [], validator=StubDM(), use_embedding=False)
        emb = pp._discovery_shadow_run(
            roster, chs, [], validator=StubDM(), use_embedding=True,
            embedder=self._embedder)
        self.assertEqual(miner["ranking_arm"], "miner")
        self.assertEqual(emb["ranking_arm"], "embedding")
        # Miner order takes the earliest chapter's sentence;
        # the embedding arm promotes the ally sentence.
        self.assertIn("rival", miner["records"][0]["evidence"][0]
                      ["sentence"])
        self.assertIn("ally", emb["records"][0]["evidence"][0]
                      ["sentence"])

    def test_failed_embedder_falls_back_to_miner_order(self):
        roster, chs = _book()
        report = pp._discovery_shadow_run(
            roster, chs, [], validator=StubDM(), use_embedding=True,
            embedder=lambda texts: None)
        self.assertTrue(report["ranking_arm"].startswith("miner"))


class TestFlagGate(unittest.TestCase):
    def setUp(self):
        self._old_cfg = pp.CONFIG.get("discovery_shadow")
        self._old_validator = pp._DECISION_VALIDATOR

    def tearDown(self):
        pp.CONFIG["discovery_shadow"] = self._old_cfg
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_default_off_everywhere(self):
        self.assertFalse(pp.DEFAULT_CONFIG["discovery_shadow"])
        self.assertFalse(pp.DEFAULT_CONFIG["discovery_embedding_rank"])
        args = pp._build_parser().parse_args([])
        self.assertFalse(args.discovery_shadow)
        self.assertFalse(args.discovery_embedding_rank)

    def test_flag_off_returns_none_and_touches_nothing(self):
        pp.CONFIG["discovery_shadow"] = False

        class Boom:
            def __getattr__(self, name):
                raise AssertionError("validator touched while off")
        pp._DECISION_VALIDATOR = Boom()
        pp._dm_budget_reset()
        roster, chs = _book()
        self.assertIsNone(pp._maybe_run_discovery(roster, chs, [], []))
        self.assertEqual(pp._dm_budget_snapshot(), {})

    def test_markdown_declares_shadow(self):
        roster, chs = _book()
        report = pp._discovery_shadow_run(roster, chs, [],
                                          validator=StubDM())
        lines = pp._discovery_markdown(report)
        text = "\n".join(lines)
        self.assertIn("SHADOW", text)
        self.assertIn("nothing written", text)
        self.assertIn("experimental allowance", text)
        self.assertEqual(pp._discovery_markdown(None), [])


if __name__ == "__main__":
    unittest.main()
