"""Referent resolution as provenance (spec §2, 2.13.22).

Extraction anchors a bare surname to the character a chapter
follows; validation re-judges from one sentence and loses the
anchoring. This layer restores it as provenance, never as the
conclusion: a deterministic antecedent pass resolves a surname-
or first-name-only mention only when exactly one roster
candidate is supported (nearest preceding full-form mention,
and/or the chapter's focal character), claims gain an endpoint-
match record with the resolving mention stored verbatim, and the
validator's context gains one line per unique resolution.
Ambiguity stays silent, pronouns never resolve, the miner's
grading is untouched, and with no unique resolution validation
is byte-identical to before.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MOD_PATH = str(_ROOT / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _roster(*names):
    roster = {}
    for name in names:
        pp._roster_update(
            roster,
            [{"name": name, "aliases": [], "role": "supporting",
              "description": "", "evidence": ""}],
            1)
    return roster


_SEBECKS = _roster("Peter Sebeck", "Laura Sebeck", "Chris Sebeck")
_CH22 = ("Peter Sebeck arrived at the farmhouse before dawn and "
         "checked the locks twice. The road beyond the fence was "
         "empty. Sebeck's son, Chris, stood next to him.")
_QUOTE = "Sebeck's son, Chris, stood next to him."


def _prov(frm, to, quote, text, focal=None, roster=None, chapter=22):
    return pp._claim_endpoint_provenance(
        {"from": frm, "to": to}, quote, text, chapter,
        roster if roster is not None else _SEBECKS, focal)


def _surname_mention(mentions, sentence_index=None):
    for m in mentions:
        if m["form"] == pp.MATCH_FORM_SURNAME:
            if sentence_index is None \
                    or m["sentence_index"] == sentence_index:
                return m
    return None


class TestAntecedentPass(unittest.TestCase):
    def test_unique_resolution_by_nearest_preceding_full(self):
        mentions = pp._resolve_chapter_mentions(_CH22, 22, _SEBECKS)
        m = _surname_mention(mentions, sentence_index=2)
        self.assertIsNotNone(m)
        self.assertEqual(m["resolved_to"], "Peter Sebeck")
        self.assertEqual(m["basis"]["kinds"],
                         [pp.BASIS_NEAREST_PRECEDING_FULL])

    def test_no_antecedent_stays_unresolved(self):
        text = "The road was empty. Sebeck's son, Chris, stood " \
               "next to him."
        mentions = pp._resolve_chapter_mentions(text, 22, _SEBECKS)
        m = _surname_mention(mentions, sentence_index=1)
        self.assertIsNone(m["resolved_to"])
        self.assertIsNone(m["basis"])

    def test_competing_sources_stay_silent(self):
        # Nearest preceding full mention says Laura; the focal
        # character is Peter. Two sources, two candidates: the
        # pass asserts nothing.
        text = ("Laura Sebeck set the table in the quiet kitchen. "
                "Sebeck's son, Chris, stood next to him.")
        mentions = pp._resolve_chapter_mentions(
            text, 22, _SEBECKS, focal_name="Peter Sebeck")
        m = _surname_mention(mentions, sentence_index=1)
        self.assertIsNone(m["resolved_to"])

    def test_focal_character_path(self):
        # The surname mention precedes every full-form mention in
        # the chapter, so only the focal character can support it.
        text = ("Sebeck checked the locks twice. The house was "
                "quiet. Peter Sebeck made tea and waited.")
        mentions = pp._resolve_chapter_mentions(
            text, 22, _SEBECKS, focal_name="Peter Sebeck")
        m = _surname_mention(mentions, sentence_index=0)
        self.assertEqual(m["resolved_to"], "Peter Sebeck")
        self.assertEqual(m["basis"]["kinds"],
                         [pp.BASIS_FOCAL_CHARACTER])
        self.assertEqual(m["basis"]["mention"],
                         "Peter Sebeck made tea and waited.")

    def test_agreeing_sources_carry_both_kinds(self):
        mentions = pp._resolve_chapter_mentions(
            _CH22, 22, _SEBECKS, focal_name="Peter Sebeck")
        m = _surname_mention(mentions, sentence_index=2)
        self.assertEqual(m["resolved_to"], "Peter Sebeck")
        self.assertEqual(m["basis"]["kinds"],
                         [pp.BASIS_NEAREST_PRECEDING_FULL,
                          pp.BASIS_FOCAL_CHARACTER])

    def test_pronouns_are_never_mentions(self):
        text = ("He arrived before dawn and he checked the locks. "
                "Sebeck's son, Chris, stood next to him.")
        mentions = pp._resolve_chapter_mentions(text, 22, _SEBECKS)
        self.assertTrue(all(m["text"] not in ("He", "he", "him")
                            for m in mentions))
        m = _surname_mention(mentions, sentence_index=1)
        self.assertIsNone(m["resolved_to"])

    def test_same_chapter_only(self):
        # A full mention in a PREVIOUS chapter is not an
        # antecedent: the pass sees one chapter at a time, and a
        # chapter opening on the bare surname stays unresolved.
        text = "Sebeck's son, Chris, stood next to him."
        mentions = pp._resolve_chapter_mentions(text, 23, _SEBECKS)
        m = _surname_mention(mentions, sentence_index=0)
        self.assertIsNone(m["resolved_to"])

    def test_shared_first_name_resolves_by_antecedent(self):
        roster = _roster("Peter Sebeck", "Peter Ross",
                          "Laura Sebeck")
        text = ("Peter Ross left early. Peter Sebeck stayed late. "
                "Peter waved at the door.")
        mentions = pp._resolve_chapter_mentions(text, 3, roster)
        firsts = [m for m in mentions
                  if m["form"] == pp.MATCH_FORM_FIRST
                  and m["sentence_index"] == 2]
        self.assertEqual(len(firsts), 1)
        self.assertEqual(firsts[0]["resolved_to"], "Peter Sebeck")


class TestProvenanceRecord(unittest.TestCase):
    def test_resolved_surname_record_shape_and_verbatim_basis(self):
        prov = _prov("Chris Sebeck", "Peter Sebeck", _QUOTE, _CH22,
                     focal="Peter Sebeck")
        self.assertEqual(set(prov), {"from", "to"})
        for ep in prov.values():
            self.assertEqual(set(ep), {"endpoint", "matched_form",
                                       "mention_text", "basis"})
            self.assertIn(ep["matched_form"],
                          pp.ENDPOINT_MATCH_FORMS)
        to = prov["to"]
        self.assertEqual(to["matched_form"],
                         pp.MATCH_FORM_RESOLVED_SURNAME)
        self.assertEqual(to["mention_text"], "Sebeck")
        basis = to["basis"]
        self.assertEqual(set(basis), {"kinds", "mention",
                                      "mention_text", "chapter",
                                      "position", "sentence_index"})
        self.assertTrue(set(basis["kinds"])
                        <= pp.RESOLUTION_BASIS_KINDS)
        # Verbatim: the stored sentence is the chapter's own text,
        # and the stored position really points at the mention.
        self.assertEqual(
            basis["mention"],
            "Peter Sebeck arrived at the farmhouse before dawn "
            "and checked the locks twice.")
        self.assertIn(basis["mention"], _CH22)
        self.assertEqual(basis["chapter"], 22)
        self.assertTrue(_CH22[basis["position"]:].startswith(
            basis["mention_text"]))
        # The other endpoint is placed directly, by first name.
        self.assertEqual(prov["from"]["matched_form"],
                         pp.MATCH_FORM_FIRST)
        self.assertIsNone(prov["from"]["basis"])

    def test_full_and_unique_surname_forms_need_no_basis(self):
        roster = _roster("Jon Ross", "Peter Sebeck")
        text = "Peter Sebeck met Jon Ross at the office at dawn."
        prov = _prov("Peter Sebeck", "Jon Ross",
                     "Peter Sebeck met Jon Ross at the office at "
                     "dawn.", text, roster=roster, chapter=1)
        self.assertEqual(prov["from"]["matched_form"],
                         pp.MATCH_FORM_FULL)
        self.assertEqual(prov["to"]["matched_form"],
                         pp.MATCH_FORM_FULL)
        text2 = "Sebeck arrived at the office at dawn."
        prov2 = _prov("Jon Ross", "Peter Sebeck", text2, text2,
                      roster=roster, chapter=1)
        self.assertEqual(prov2["to"]["matched_form"],
                         pp.MATCH_FORM_SURNAME)
        self.assertIsNone(prov2["to"]["basis"])

    def test_resolution_to_someone_else_places_nobody(self):
        # The chapter's antecedent is Peter: a Laura claim on the
        # same quote gains no Laura antecedent (spec §2.4's second
        # half) — her endpoint stays unresolved.
        prov = _prov("Chris Sebeck", "Laura Sebeck", _QUOTE, _CH22,
                     focal="Peter Sebeck")
        self.assertEqual(prov["to"]["matched_form"],
                         pp.MATCH_FORM_UNRESOLVED)
        self.assertIsNone(prov["to"]["basis"])

    def test_pronoun_only_endpoint_stays_unresolved(self):
        text = ("Chris Sebeck met Peter Sebeck at dawn. "
                "He stood next to him.")
        prov = _prov("Chris Sebeck", "Peter Sebeck",
                     "He stood next to him.", text, chapter=1)
        self.assertEqual(prov["from"]["matched_form"],
                         pp.MATCH_FORM_UNRESOLVED)
        self.assertEqual(prov["to"]["matched_form"],
                         pp.MATCH_FORM_UNRESOLVED)

    def test_unlocated_quote_records_textual_form_only(self):
        prov = _prov("Chris Sebeck", "Peter Sebeck", _QUOTE,
                     "A chapter where the quote never appears.")
        self.assertEqual(prov["to"]["matched_form"],
                         pp.MATCH_FORM_SURNAME)
        self.assertIsNone(prov["to"]["basis"])
        self.assertEqual(pp._resolution_context_lines(prov), [])


class TestValidatorContextLines(unittest.TestCase):
    def test_line_only_on_unique_resolution(self):
        prov = _prov("Chris Sebeck", "Peter Sebeck", _QUOTE, _CH22,
                     focal="Peter Sebeck")
        lines = pp._resolution_context_lines(prov)
        self.assertEqual(
            lines,
            ["In this chapter, 'Sebeck' refers to Peter Sebeck "
             "(the scene follows them)."])

    def test_ambiguity_stays_silent(self):
        prov = _prov("Chris Sebeck", "Laura Sebeck", _QUOTE, _CH22,
                     focal="Peter Sebeck")
        self.assertEqual(pp._resolution_context_lines(prov), [])
        prov_none = _prov("Chris Sebeck", "Peter Sebeck", _QUOTE,
                          "The road was empty. " + _QUOTE)
        self.assertEqual(pp._resolution_context_lines(prov_none),
                         [])


class _CapturingValidator:
    model = "stub"

    def __init__(self, p=0.95):
        self.p = p
        self.calls = []

    def validate_relationship(self, frm, to, rtype, quote,
                              context=""):
        self.calls.append({"from": frm, "to": to, "type": rtype,
                           "quote": quote, "context": context})
        return True, self.p


class TestReduceIntegration(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _CapturingValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def _drive(self, text, focal=None):
        chs = [{"index": 1, "label": "Ch1", "text": text}]
        chapter_a = {"characters": [], "task_status": {"identity": "ok"},
                     "relationships": [{
                         "from": "Chris Sebeck",
                         "to": "Peter Sebeck", "type": "child",
                         "evidence": _QUOTE}]}
        if focal:
            chapter_a["pov_character"] = focal
        red = pp.v2_reduce({1: chapter_a}, {}, _SEBECKS, chs,
                           preview=False)
        return red

    def test_context_line_and_provenance_reach_the_validator(self):
        red = self._drive(_CH22, focal="Peter Sebeck")
        self.assertEqual(len(self.validator.calls), 1)
        ctx = self.validator.calls[0]["context"]
        self.assertIn("In this chapter, 'Sebeck' refers to Peter "
                      "Sebeck", ctx)
        self.assertIn("Sentences around the quote", ctx)
        rel = red["relationships"][0]
        prov = rel["endpoint_provenance"]
        self.assertEqual(prov["to"]["matched_form"],
                         pp.MATCH_FORM_RESOLVED_SURNAME)
        self.assertEqual(prov["to"]["basis"]["mention"],
                         "Peter Sebeck arrived at the farmhouse "
                         "before dawn and checked the locks twice.")
        self.assertEqual(prov["from"]["matched_form"],
                         pp.MATCH_FORM_FIRST)

    def test_no_resolution_validation_is_byte_identical(self):
        # No antecedent and no focal character: the context the
        # validator receives is exactly the pre-§2 framing, and
        # the claim carries provenance that asserts nothing.
        text = "The road beyond the fence was empty. " + _QUOTE
        red = self._drive(text)
        self.assertEqual(len(self.validator.calls), 1)
        ctx = self.validator.calls[0]["context"]
        expected = ("Sentences around the quote, from the same "
                    "passage: "
                    + pp._evidence_window(_QUOTE, text))
        self.assertEqual(ctx, expected)
        self.assertNotIn("refers to", ctx)
        prov = red["relationships"][0]["endpoint_provenance"]
        self.assertEqual(prov["to"]["matched_form"],
                         pp.MATCH_FORM_UNRESOLVED)


class TestHistoryMirror(unittest.TestCase):
    def _result(self):
        prov = _prov("Chris Sebeck", "Peter Sebeck", _QUOTE, _CH22,
                     focal="Peter Sebeck")
        return {
            "tropes": [], "trope_confidence": {},
            "trope_catalog_map": {}, "trope_retrieval": {},
            "evidence_retrieval": {}, "trope_persistence": {},
            "triggers": [], "trigger_retrieval": {},
            "trigger_persistence": {},
            "relationships": [{
                "from": "Chris Sebeck", "to": "Peter Sebeck",
                "type": "child", "validation_state": "validated",
                "validation_p": 0.9,
                "endpoint_provenance": prov}],
            "dropped_relationships": [],
            "_history_book_key": "bookkey123",
        }

    def test_relationship_row_mirrors_structured_provenance(self):
        claims, rows = pp._history_book_claims(self._result())
        rel_rows = [r for r in rows if r["axis"] == "relationships"]
        self.assertEqual(len(rel_rows), 1)
        row = rel_rows[0]
        self.assertEqual(row["verdict"], "validated")
        self.assertEqual(row["confidence"], 0.9)
        mirror = row["endpoint_provenance"]
        self.assertEqual(mirror["to"]["matched_form"],
                         pp.MATCH_FORM_RESOLVED_SURNAME)
        self.assertEqual(mirror["to"]["basis_kinds"],
                         [pp.BASIS_NEAREST_PRECEDING_FULL,
                          pp.BASIS_FOCAL_CHARACTER])
        self.assertEqual(mirror["to"]["basis_chapter"], 22)
        self.assertEqual(mirror["to"]["basis_position"], 0)
        # Metadata-only: no verbatim book text crosses the mirror.
        blob = json.dumps(row)
        self.assertNotIn("farmhouse", blob)
        self.assertNotIn("Sebeck", blob)

    def test_claim_without_provenance_earns_no_row(self):
        result = self._result()
        del result["relationships"][0]["endpoint_provenance"]
        claims, rows = pp._history_book_claims(result)
        self.assertEqual(
            [r for r in rows if r["axis"] == "relationships"], [])


def _fixture_cases():
    cases = json.loads(
        (_ROOT / "eval" / "relationship_cases.json").read_text(
            encoding="utf-8"))
    return [c for c in cases if c.get("resolution")]


class TestEvalFixtures(unittest.TestCase):
    """The §2.4 test pair and the §2.5 adversarial cases, run
    against production's provenance builder. Every fixture is
    report-only in the eval (expect "any"); these assertions are
    about the resolution layer, not the judge's score."""

    def test_pair_and_adversarial_expectations(self):
        fixtures = _fixture_cases()
        self.assertGreaterEqual(len(fixtures), 5)
        for case in fixtures:
            fx = case["resolution"]
            roster = _roster(*fx["roster"])
            prov = pp._claim_endpoint_provenance(
                {"from": case["from"], "to": case["to"]},
                case["quote"], fx["chapter_text"],
                fx["chapter_index"], roster, fx.get("focal"))
            key = "from" if fx["endpoint"] == case["from"] else "to"
            ep = prov[key]
            if fx["expect_resolution"] == "resolved":
                self.assertEqual(
                    ep["matched_form"],
                    pp.MATCH_FORM_RESOLVED_SURNAME, case["note"])
                self.assertIsNotNone(ep["basis"])
            else:
                self.assertNotEqual(
                    ep["matched_form"],
                    pp.MATCH_FORM_RESOLVED_SURNAME, case["note"])
                self.assertEqual(
                    pp._resolution_context_lines(prov), [],
                    case["note"])

    def test_anti_circularity_evidence_removal_changes_nothing(self):
        # Spec §2.5: for every resolution fixture, resolution
        # output is unchanged when the evidence sentence is
        # removed from the chapter text, and no basis ever rests
        # on the evidence sentence or on a kinship/binding
        # sentence.
        for case in _fixture_cases():
            fx = case["resolution"]
            roster = _roster(*fx["roster"])
            text = fx["chapter_text"]
            span = pp._locate_quote_span(case["quote"], text)
            self.assertIsNotNone(span, case["note"])
            ev_idx = set(range(span[0], span[1] + 1))

            def _summary(txt, exclude):
                ms = pp._resolve_chapter_mentions(
                    txt, fx["chapter_index"], roster,
                    fx.get("focal"), exclude)
                return [(m["text"], m["resolved_to"],
                         (m["basis"] or {}).get("mention_text"))
                        for m in ms
                        if m["sentence_index"] not in exclude]

            before = _summary(text, ev_idx)
            removed = text.replace(case["quote"], "", 1)
            after = _summary(removed, frozenset())
            self.assertEqual(before, after, case["note"])
            # The endpoint's own basis, when one exists, is
            # neither the evidence sentence nor a binding one.
            prov = pp._claim_endpoint_provenance(
                {"from": case["from"], "to": case["to"]},
                case["quote"], text, fx["chapter_index"], roster,
                fx.get("focal"))
            for ep in prov.values():
                basis = ep.get("basis")
                if basis and basis.get("mention"):
                    self.assertNotEqual(basis["mention"],
                                        case["quote"])
                    self.assertIsNone(
                        pp._PAIR_MARKER_RE.search(basis["mention"]),
                        case["note"])

    def test_binding_sentence_cannot_serve_as_antecedent(self):
        # The only full-form mention sits inside a kinship
        # sentence ("Peter Sebeck's wife, Laura"): letting it
        # anchor the surname would smuggle a bond statement into
        # the provenance, so the surname stays unresolved.
        text = ("Peter Sebeck's wife, Laura, made tea for them. "
                "Sebeck's son, Chris, stood next to him.")
        prov = _prov("Chris Sebeck", "Peter Sebeck", _QUOTE, text,
                     chapter=1)
        self.assertEqual(prov["to"]["matched_form"],
                         pp.MATCH_FORM_UNRESOLVED)


if __name__ == "__main__":
    unittest.main()
