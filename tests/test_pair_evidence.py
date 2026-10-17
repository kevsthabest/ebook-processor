"""Pair-evidence mining: deterministic extraction of establishing
sentences for relationship claims.

Motivation (Fourth Wing -13 live run, 2026-10): family claims reached
the decision-model validator carrying ONE topical quote, and true
claims died on non-establishing quotes (Fen->Xaden parent P=0.04,
Dad->Violet parent 0.14, General->Violet parent 0.16) while the one
kept claim carried an establishing quote ("This is my sister, Violet",
P=0.96). Separately, 18/23 principal relationships were skipped at
write as evidence-less.

_mine_pair_evidence finds sentences naming BOTH endpoints and grades
them categorically:
  binding      - a kinship marker syntactically bound to an endpoint,
                 with the other endpoint equated with the relative
                 (apposition / copula / pronoun-apposition patterns);
  co-occurrence- both endpoints + a kinship marker, not bound;
  plain        - both endpoints, no kinship marker.
Only binding may replace the quote a family claim is validated on;
write evidence for evidence-less claims is binding-only for family
claims, plain-or-better for non-family claims.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _roster(*entries):
    """entries: (name, [aliases]) -> roster in _roster_update shape."""
    roster = {}
    for name, aliases in entries:
        pp._roster_update(
            roster,
            [{"name": name, "aliases": aliases, "role": "supporting",
              "description": "", "evidence": ""}],
            1)
    return roster


def _chs(*texts):
    return [{"index": i + 1, "label": f"Ch{i + 1}", "text": t}
            for i, t in enumerate(texts)]


_FW_ROSTER = _roster(
    ("Violet Sorrengail", []),
    ("Brennan Sorrengail", []),
    ("Xaden Riorson", []),
    ("Fen Riorson", []),
    ("General Sorrengail", []),
    ("Mira Sorrengail", []),
)


class TestMinerGrades(unittest.TestCase):
    def test_binding_possessive_apposition(self):
        chs = _chs("Xaden's father, Fen Riorson, had led the rebellion "
                   "years before.")
        hit = pp._mine_pair_evidence("Fen Riorson", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")
        self.assertEqual(
            hit["sentence"],
            "Xaden's father, Fen Riorson, had led the rebellion "
            "years before.")
        self.assertEqual(hit["chapter"], 1)

    def test_binding_of_construction(self):
        chs = _chs("Xaden Riorson, son of Fen Riorson, inherited the "
                   "rebellion.")
        hit = pp._mine_pair_evidence("Fen Riorson", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")

    def test_binding_copula(self):
        chs = _chs("Fen Riorson was Xaden's father, and the rebellion "
                   "died with him.")
        hit = pp._mine_pair_evidence("Fen Riorson", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")

    def test_binding_pronoun_apposition(self):
        # "her brother Brennan" class (the brief's "her son Xaden" shape):
        # a pronoun possessive + marker equating the following endpoint.
        chs = _chs("Violet hugged her brother Brennan before the "
                   "ceremony.")
        hit = pp._mine_pair_evidence("Violet Sorrengail",
                                     "Brennan Sorrengail",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")

    def test_binding_pronoun_apposition_after_name(self):
        chs = _chs("Mira turned to the cadets flanking Violet. "
                   "\"My sister, Violet Sorrengail, will be a "
                   "first-year,\" Mira said.")
        hit = pp._mine_pair_evidence("Mira Sorrengail",
                                     "Violet Sorrengail",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")

    def test_third_party_marker_not_binding_for_pair(self):
        # Advisor case: the marker binds Brennan (a third party in the
        # sentence), NOT either member of the (Violet, Xaden) pair.
        text = ("Violet thought of Brennan, her brother, and of Xaden "
                "waiting below.")
        chs = _chs(text)
        hit = pp._mine_pair_evidence("Violet Sorrengail", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertNotEqual(hit["grade"], "binding")
        self.assertEqual(hit["grade"], "co-occurrence")
        # The SAME sentence IS binding for the pair it actually
        # establishes (Violet, Brennan) — the discrimination is the point.
        hit2 = pp._mine_pair_evidence("Violet Sorrengail",
                                      "Brennan Sorrengail",
                                      chs, _FW_ROSTER)
        self.assertIsNotNone(hit2)
        self.assertEqual(hit2["grade"], "binding")

    def test_marker_bound_to_one_endpoint_only_not_binding(self):
        # Advisor case: "Xaden's father" binds the marker to Xaden and
        # defines a THIRD person (the father). Violet merely co-occurs;
        # nothing equates her with the relative, so the (Violet, Xaden)
        # claim must not grade binding.
        chs = _chs("Violet cursed Xaden's father for the rebellion that "
                   "had orphaned her.")
        hit = pp._mine_pair_evidence("Violet Sorrengail", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertNotEqual(hit["grade"], "binding")
        self.assertEqual(hit["grade"], "co-occurrence")

    def test_cooccurrence_grade(self):
        chs = _chs("Violet Sorrengail thought about her mother while "
                   "Xaden Riorson waited below.")
        hit = pp._mine_pair_evidence("Violet Sorrengail", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "co-occurrence")

    def test_plain_grade(self):
        chs = _chs("Violet Sorrengail met Xaden Riorson at the gate.")
        hit = pp._mine_pair_evidence("Violet Sorrengail", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "plain")

    def test_binding_outranks_earlier_plain(self):
        chs = _chs(
            "Fen Riorson met Xaden Riorson at the border fort.",
            "Xaden's father, Fen Riorson, had led the rebellion.")
        hit = pp._mine_pair_evidence("Fen Riorson", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertEqual(hit["grade"], "binding")
        self.assertEqual(hit["chapter"], 2)

    def test_earlier_chapter_wins_within_grade(self):
        chs = _chs(
            "Fen Riorson was Xaden's father, and all Navarre knew it "
            "before the rebellion even began to turn.",
            "Xaden's father, Fen Riorson, fell.")
        hit = pp._mine_pair_evidence("Fen Riorson", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertEqual(hit["grade"], "binding")
        self.assertEqual(hit["chapter"], 1)


class TestMinerNameForms(unittest.TestCase):
    def test_shared_first_name_disabled(self):
        # Two roster entries share the first name "John": the bare first
        # name may not be used as an endpoint form for either.
        roster = _roster(("John Alpha", []), ("John Beta", []),
                         ("Mary Gamma", []))
        chs = _chs("John walked with Mary through the market.")
        self.assertIsNone(pp._mine_pair_evidence(
            "John Alpha", "Mary Gamma", chs, roster))
        # Full names still mine.
        chs2 = _chs("John Alpha walked with Mary Gamma through the "
                    "market.")
        hit = pp._mine_pair_evidence("John Alpha", "Mary Gamma",
                                     chs2, roster)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "plain")

    def test_unique_first_name_usable(self):
        chs = _chs("Fen watched from the ridge as Xaden crossed the "
                   "yard.")
        hit = pp._mine_pair_evidence("Fen Riorson", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "plain")

    def test_alias_form_can_anchor(self):
        roster = _roster(("William Afton", ["Bill"]), ("Henry Emily", []))
        chs = _chs("Henry Emily was Bill's father in every way that "
                   "mattered.")
        hit = pp._mine_pair_evidence("Henry Emily", "William Afton",
                                     chs, roster)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")

    def test_no_roster_full_names_only(self):
        chs = _chs("Fen Riorson was the father of Xaden Riorson.")
        hit = pp._mine_pair_evidence("Fen Riorson", "Xaden Riorson",
                                     chs, None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")
        # Without a roster, first names are NOT trusted.
        chs2 = _chs("Fen watched Xaden leave.")
        self.assertIsNone(pp._mine_pair_evidence(
            "Fen Riorson", "Xaden Riorson", chs2, None))


class TestMinerVerbatim(unittest.TestCase):
    def test_returned_sentence_is_verbatim_substring(self):
        text = ("The hall was loud. Fen Riorson was Xaden's father, "
                "and the rebellion died with him. Rain followed.")
        chs = _chs(text)
        hit = pp._mine_pair_evidence("Fen Riorson", "Xaden Riorson",
                                     chs, _FW_ROSTER)
        self.assertIsNotNone(hit)
        self.assertIn(hit["sentence"], text)

    def test_split_statement_never_joined(self):
        # The fact is split across two sentences; the miner must return
        # nothing rather than fabricate a joined/paraphrased sentence.
        chs = _chs("Fen Riorson led the rebellion. Xaden never knew "
                   "his father.")
        self.assertIsNone(pp._mine_pair_evidence(
            "Fen Riorson", "Xaden Riorson", chs, _FW_ROSTER))

    def test_no_candidate_returns_none(self):
        chs = _chs("Nothing relevant happened in this chapter at all.")
        self.assertIsNone(pp._mine_pair_evidence(
            "Fen Riorson", "Xaden Riorson", chs, _FW_ROSTER))


class _CapturingValidator:
    model = "stub"

    def __init__(self, p=0.95):
        self.p = p
        self.calls = []  # (from, to, type, quote)

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls.append((frm, to, rtype, quote))
        return True, self.p


class TestValidationIntegration(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _CapturingValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_binding_sentence_replaces_validator_quote(self):
        # Mandatory case: junk original quote + binding sentence in the
        # text -> the validator judges the MINED sentence; the original
        # quote stays on the claim for audit.
        text = ("The rebellion shaped them all. Fen Riorson was "
                "Xaden's father, and the rebellion died with him.")
        chs = _chs(text)
        chapter_as = {1: {"relationships": [{
            "from": "Fen Riorson", "to": "Xaden Riorson",
            "type": "parent", "evidence": "they spoke at the gate"}],
            "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, _FW_ROSTER, chs,
                           preview=False)
        self.assertEqual(len(self.validator.calls), 1)
        frm, to, rtype, quote = self.validator.calls[0]
        self.assertEqual((frm, to, rtype),
                         ("Fen Riorson", "Xaden Riorson", "parent"))
        self.assertEqual(
            quote,
            "Fen Riorson was Xaden's father, and the rebellion died "
            "with him.")
        rel = red["relationships"][0]
        self.assertEqual(rel["evidence"], "they spoke at the gate")
        self.assertEqual(rel["evidence_mined"], quote)
        self.assertEqual(rel["evidence_grade"], "binding")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["type"], "parent")

    def test_cooccurrence_does_not_upgrade_validator_quote(self):
        # Mandatory case: only co-occurrence mining available -> the
        # validator receives the claim's ORIGINAL quote.
        text = ("General Sorrengail reviewed the cadets while Violet "
                "Sorrengail thought of her mother.")
        chs = _chs(text)
        chapter_as = {1: {"relationships": [{
            "from": "General Sorrengail", "to": "Violet Sorrengail",
            "type": "parent", "evidence": "the original quote"}],
            "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, _FW_ROSTER, chs,
                           preview=False)
        self.assertEqual(len(self.validator.calls), 1)
        self.assertEqual(self.validator.calls[0][3], "the original quote")
        rel = red["relationships"][0]
        self.assertNotIn("evidence_mined", rel)
        self.assertEqual(rel["evidence_grade"], "co-occurrence")


class TestWriteEvidenceIntegration(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._DECISION_VALIDATOR = None
        pp._dm_budget_reset()

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_evidenceless_nonfamily_written_with_mined(self):
        # Mandatory case: evidence-less non-family pair with a
        # both-names sentence -> written with mined evidence +
        # evidence_source in preview entry AND write payload.
        chs = _chs("Alice met Bob at the market square.")
        chapter_as = {1: {"relationships": [{
            "from": "Alice", "to": "Bob", "type": "friend",
            "evidence": ""}], "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        rel = red["relationships"][0]
        self.assertEqual(rel["evidence"], "Alice met Bob at the market "
                                          "square.")
        self.assertEqual(rel["evidence_source"], "mined")
        rel_map, evidenceless = pp._build_rel_map(red)
        self.assertEqual(evidenceless, 0)
        self.assertEqual(rel_map["alice"], [{
            "to": "Bob", "type": "friend",
            "evidence": "Alice met Bob at the market square.",
            "evidence_source": "mined"}])

    def test_evidenceless_family_cooccurrence_drops(self):
        # BAR-MOVE (family fail-closed, 2026-10-14): this test used to
        # pin the claim surviving reduce (auto-downgraded) and being
        # skipped at WRITE. Fail-closed moves the exclusion to family
        # validation: an evidence-less family pair with only a
        # co-occurrence sentence DROPS in reduce, so nothing reaches
        # the write pass at all. The write outcome is unchanged in
        # effect — the pair is not written.
        text = ("General Sorrengail reviewed the cadets while Violet "
                "Sorrengail thought of her mother.")
        chs = _chs(text)
        chapter_as = {1: {"relationships": [{
            "from": "General Sorrengail", "to": "Violet Sorrengail",
            "type": "parent", "evidence": ""}], "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, _FW_ROSTER, chs,
                           preview=False)
        self.assertEqual(red["relationships"], [])
        dropped = red["dropped_relationships"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["type"], "parent")
        self.assertEqual(dropped[0]["validation_note"], "no_evidence")
        rel_map, evidenceless = pp._build_rel_map(red)
        self.assertEqual(evidenceless, 0)
        self.assertEqual(rel_map, {})

    def test_evidenceless_family_binding_written(self):
        # The rescue path: evidence-less family pair + binding sentence
        # -> the claim survives with the mined sentence as its evidence.
        text = ("Fen Riorson was Xaden's father, and the rebellion "
                "died with him.")
        chs = _chs(text)
        chapter_as = {1: {"relationships": [{
            "from": "Fen Riorson", "to": "Xaden Riorson",
            "type": "parent", "evidence": ""}], "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, _FW_ROSTER, chs,
                           preview=False)
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "parent")
        self.assertEqual(rel["evidence"], text)
        self.assertEqual(rel["evidence_source"], "mined")
        rel_map, evidenceless = pp._build_rel_map(red)
        self.assertEqual(evidenceless, 0)
        self.assertEqual(rel_map["fen riorson"][0]["type"], "parent")

    def test_original_evidence_untouched_by_write_mining(self):
        # A claim that HAS evidence keeps it verbatim; no mined fields
        # are attached by the write pass.
        chs = _chs("Alice met Bob at the market square.")
        chapter_as = {1: {"relationships": [{
            "from": "Alice", "to": "Bob", "type": "friend",
            "evidence": "an original quote"}], "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=False)
        rel = red["relationships"][0]
        self.assertEqual(rel["evidence"], "an original quote")
        self.assertNotIn("evidence_source", rel)


class TestWritePayloadOrientation(unittest.TestCase):
    """Fix B: orientation pins at the WRITE PAYLOAD seam.

    _canonical_pair itself and the reduce-level relationship list are
    already pinned in tests/test_relationship_integrity.py. What was
    not pinned: the payload the app actually reads — relationships
    embedded per character row via _build_rel_map/_build_character_rows.
    """

    def _rows(self, red):
        chars = [{"name": n, "role": "supporting", "description": "",
                  "appearance": "", "status": "alive", "aliases": [],
                  "first_appearance_chapter": 1, "confidence": 0.9}
                 for n in ("Alice", "Bob")]
        rel_map, _ = pp._build_rel_map(red)
        return pp._build_character_rows(
            "work-x", {"characters": chars}, rel_map, set())

    def test_parent_stored_once_type_describes_from(self):
        # The same fact claimed in both directions across chapters:
        # "Bob is Alice's parent" and "Alice is Bob's child" must land
        # as ONE payload entry, on the canonical from character's row,
        # the type describing `from`.
        chapter_as = {
            1: {"relationships": [{"from": "Bob", "to": "Alice",
                                   "type": "parent", "evidence": "ev1"}],
                "pov_character": None},
            2: {"relationships": [{"from": "Alice", "to": "Bob",
                                   "type": "child", "evidence": "ev2"}],
                "pov_character": None},
        }
        chs = _chs("x " * 200, "y " * 200)
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=True)
        rows = self._rows(red)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["Alice"]["relationships"],
                         [{"to": "Bob", "type": "child",
                           "evidence": "ev1"}])
        self.assertEqual(by_name["Bob"]["relationships"], [])
        total = sum(len(r["relationships"]) for r in rows)
        self.assertEqual(total, 1)

    def test_mentor_payload_keeps_claim_direction(self):
        # "mentor" has no inverse: even though Alice sorts before Bob,
        # the payload entry stays on the MENTOR's row (claim direction).
        chapter_as = {
            1: {"relationships": [{"from": "Bob", "to": "Alice",
                                   "type": "mentor", "evidence": "ev"}],
                "pov_character": None},
        }
        chs = _chs("x " * 200)
        red = pp.v2_reduce(chapter_as, {}, {}, chs, preview=True)
        rows = self._rows(red)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["Bob"]["relationships"],
                         [{"to": "Alice", "type": "mentor",
                           "evidence": "ev"}])
        self.assertEqual(by_name["Alice"]["relationships"], [])


_SEBECK_ROSTER = _roster(
    ("Peter Sebeck", []),
    ("Laura Sebeck", []),
    ("Chris Sebeck", []),
    ("Jon Ross", []),
)


class TestMinerSurnameForms(unittest.TestCase):
    """Surname name forms: kinship stated through the bare surname.

    Daemon run 16 regression set: the validator dropped a spouse
    claim and a child claim whose evidence the grader could only
    call co-occurrence, because the possessive in "Sebeck's son,
    Chris" anchors on a surname the miner did not treat as a name
    form."""

    def test_surname_possessive_apposition_binds(self):
        chs = _chs("Sebeck's son, Chris, stood next to him in the "
                   "hospital corridor.")
        hit = pp._mine_pair_evidence("Chris Sebeck", "Peter Sebeck",
                                     chs, _SEBECK_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")

    def test_surname_of_construction_binds(self):
        chs = _chs("The son of Sebeck, Chris, had his father's hands.")
        hit = pp._mine_pair_evidence("Chris Sebeck", "Peter Sebeck",
                                     chs, _SEBECK_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")

    def test_surname_anchor_does_not_bind_unrelated_pair(self):
        # The same sentence mined for (Chris Sebeck, Jon Ross): the
        # construction belongs to the Sebeck pair; Ross is never
        # equated with the relative.
        chs = _chs("Sebeck's son, Chris, stood next to him while Jon "
                   "Ross waited outside.")
        hit = pp._mine_pair_evidence("Chris Sebeck", "Jon Ross",
                                     chs, _SEBECK_ROSTER)
        self.assertIsNotNone(hit)
        self.assertNotEqual(hit["grade"], "binding")
        self.assertEqual(hit["grade"], "co-occurrence")

    def test_shared_family_surname_still_binds(self):
        # Design trade-off, pinned deliberately: for the pair
        # (Laura Sebeck, Chris Sebeck) the same sentence grades
        # binding — it equates Chris with a Sebeck child, and Laura
        # is the pair's other endpoint. Which Sebeck the sentence
        # means is the validator's question, not the grader's; the
        # grader's job is that the sentence is establishing-shaped.
        chs = _chs("Sebeck's son, Chris, stood next to him in the "
                   "hospital corridor.")
        hit = pp._mine_pair_evidence("Laura Sebeck", "Chris Sebeck",
                                     chs, _SEBECK_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "binding")

    def test_surname_disabled_when_it_is_a_first_name_elsewhere(self):
        # A roster character whose FIRST name is "Sebeck" makes the
        # bare word more likely that person than the family name, so
        # the surname form is withheld — the sentence then cannot
        # even place Peter, and mining finds no candidate.
        roster = _roster(("Peter Sebeck", []), ("Chris Sebeck", []),
                         ("Sebeck Jones", []))
        chs = _chs("Sebeck's son, Chris, stood next to him in the "
                   "hospital corridor.")
        self.assertIsNone(pp._mine_pair_evidence(
            "Chris Sebeck", "Peter Sebeck", chs, roster))

    def test_surname_presence_alone_is_plain(self):
        chs = _chs("Sebeck met Chris at the precinct house.")
        hit = pp._mine_pair_evidence("Chris Sebeck", "Peter Sebeck",
                                     chs, _SEBECK_ROSTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["grade"], "plain")


class _ScriptedValidator:
    """Validator stub whose P is scripted per (from, to) pair."""

    model = "stub"

    def __init__(self, p_by_pair=None, default=0.95):
        self.p_by_pair = p_by_pair or {}
        self.default = default
        self.calls = []

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls.append((frm, to, rtype, quote))
        return True, self.p_by_pair.get((frm, to), self.default)


class TestNonFamilyValidation(unittest.TestCase):
    """Non-family validation: friend/enemy/colleague claims are
    judged too, on their extracted quote or a mined sentence.

    Motivation (relationships audit, 2026-10-17): non-family claims
    were never validated, and the stored Fourth Wing data carries
    pairs typed on quotes that do not involve the pair at all.
    Two-tier outcomes: at or above the drop threshold the type
    stands; below it the claim drops. There is no downgrade tier —
    a doubtful friend claim has no meaningful "other" to become."""

    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _ScriptedValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def _reduce(self, rels, chs=None, roster=None, preview=False):
        chapter_as = {1: {"relationships": rels,
                          "pov_character": None}}
        return pp.v2_reduce(chapter_as, {}, roster or {},
                            chs or _chs("x " * 200), preview=preview)

    def test_junk_nonfamily_drops_in_write_mode(self):
        self.validator.p_by_pair[("Alice", "Bob")] = 0.10
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "friend",
            "evidence": "a quote about somebody else entirely"}])
        self.assertEqual(red["relationships"], [])
        dropped = red["dropped_relationships"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["validation_note"], "dropped")
        self.assertEqual(dropped[0]["validation_p"], 0.10)

    def test_supported_nonfamily_kept(self):
        self.validator.p_by_pair[("Alice", "Bob")] = 0.90
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "friend",
            "evidence": "they were inseparable since childhood"}])
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "friend")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["validation_p"], 0.90)

    def test_middle_band_nonfamily_keeps_its_type(self):
        # 0.30 <= P < 0.70 would downgrade a FAMILY claim to
        # "other"; a non-family claim keeps its type instead.
        self.validator.p_by_pair[("Alice", "Bob")] = 0.50
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "enemy",
            "evidence": "they glared at each other across the hall"}])
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "enemy")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(red["dropped_relationships"], [])

    def test_nonfamily_preview_records_would_drop(self):
        self.validator.p_by_pair[("Alice", "Bob")] = 0.10
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "friend",
            "evidence": "a quote about somebody else entirely"}],
            preview=True)
        rel = red["relationships"][0]
        self.assertEqual(rel["validation_note"], "would_drop")
        self.assertEqual(red["dropped_relationships"], [])

    def test_evidenceless_nonfamily_judged_on_mined_sentence(self):
        # No extracted quote: the judge weighs the mined both-names
        # sentence — the interaction test a bare co-occurrence
        # cannot pass on its own.
        self.validator.p_by_pair[("Alice", "Bob")] = 0.10
        chs = _chs("Alice met Bob at the market square.")
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "friend",
            "evidence": ""}], chs=chs)
        self.assertEqual(len(self.validator.calls), 1)
        self.assertEqual(self.validator.calls[0][3],
                         "Alice met Bob at the market square.")
        self.assertEqual(red["relationships"], [])

    def test_validated_evidenceless_nonfamily_still_written(self):
        # A kept evidence-less claim must still receive its mined
        # write evidence. Its "kept" note must not impose the
        # family bar on the write pass (that bar is traced by the
        # "downgraded" note, not by having been judged at all).
        self.validator.p_by_pair[("Alice", "Bob")] = 0.95
        chs = _chs("Alice met Bob at the market square.")
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "friend",
            "evidence": ""}], chs=chs)
        rel = red["relationships"][0]
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["evidence"],
                         "Alice met Bob at the market square.")
        self.assertEqual(rel["evidence_source"], "mined")

    def test_family_claims_claim_budget_first(self):
        # One budget unit left, a friend claim listed BEFORE a
        # parent claim: the family pass runs first, so the parent
        # claim is judged and the friend claim is the one that
        # reports budget_exhausted.
        for _ in range(pp._MERGE_TIER3_CAP - 1):
            self.assertTrue(pp._dm_budget_claim(1))
        red = self._reduce([
            {"from": "Alice", "to": "Bob", "type": "friend",
             "evidence": "an evidenced friend claim"},
            {"from": "Fen Riorson", "to": "Xaden Riorson",
             "type": "parent", "evidence": "an evidenced parent claim"}],
            roster=_FW_ROSTER)
        self.assertEqual(len(self.validator.calls), 1)
        self.assertEqual(self.validator.calls[0][2], "parent")
        by_type = {r["type"]: r for r in red["relationships"]}
        self.assertEqual(by_type["parent"]["validation_note"], "kept")
        self.assertEqual(by_type["friend"]["validation_note"],
                         "budget_exhausted")

    def test_other_type_is_not_judged(self):
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "other",
            "evidence": "some quote"}])
        self.assertEqual(self.validator.calls, [])
        self.assertNotIn("validation_note", red["relationships"][0])


class TestValidatorPromptConstructions(unittest.TestCase):
    """The validator prompt must teach the surname constructions.

    Motivation: the pre-tune prompt scored "Sebeck's son, Chris"
    at 0.20 in production while scoring "my sister" constructions
    at 0.96 — the judge had never been told a surname alone names
    a person, or that a possessive appositive states a relation.
    These tests pin the guidance so a future prompt edit cannot
    silently drop it (the live scores are gated by
    evaluate-relationships.py against eval/relationship_cases.json).
    """

    def _instructions_for(self, frm, to, rtype, quote):
        captured = {}
        validator = pp.DecisionValidator.__new__(pp.DecisionValidator)

        def _ask(kind, instructions, state, label=""):
            captured["instructions"] = instructions
            return 0.9

        validator._ask_noul = _ask
        validator.validate_relationship(frm, to, rtype, quote)
        return captured["instructions"]

    def test_prompt_teaches_surname_constructions(self):
        text = self._instructions_for(
            "Chris Sebeck", "Peter Sebeck", "child",
            "Sebeck's son, Chris, stood next to him.")
        self.assertIn("surname alone", text)
        self.assertIn("Sebeck's son, Chris", text)
        self.assertIn("judge the construction", text)

    def test_prompt_rejects_third_person_binding(self):
        text = self._instructions_for(
            "Xaden Riorson", "Violet Sorrengail", "sibling",
            "Violet thought of Brennan, her brother.")
        self.assertIn("third person", text)


class TestSurnameValidationIntegration(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _CapturingValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_surname_binding_sentence_replaces_validator_quote(self):
        # The Daemon run-16 child claim end to end: a topical
        # original quote, a surname-anchored establishing sentence
        # in the text — the validator must judge the mined sentence.
        text = ("The hospital corridor was quiet. Sebeck's son, "
                "Chris, stood next to him.")
        chs = _chs(text)
        chapter_as = {1: {"relationships": [{
            "from": "Chris Sebeck", "to": "Peter Sebeck",
            "type": "child", "evidence": "they were at the hospital"}],
            "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, _SEBECK_ROSTER, chs,
                           preview=False)
        self.assertEqual(len(self.validator.calls), 1)
        self.assertEqual(self.validator.calls[0][3],
                         "Sebeck's son, Chris, stood next to him.")
        rel = red["relationships"][0]
        self.assertEqual(rel["evidence_grade"], "binding")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["type"], "child")


if __name__ == "__main__":
    unittest.main()
