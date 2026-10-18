"""Provenance stamps (2.13.31): run artefacts describe themselves.

Two records a run produces were losing information a later reader
needs:

1. A family claim the validator scores in the middle band is
   downgraded: its type is rewritten to "other" and the type it was
   actually judged under survives nowhere on the shipped claim —
   an alert or a curator sees "other" and cannot tell a downgraded
   spouse from a claim that was always vague. The downgrade step
   now stamps the pre-rewrite type on the claim as `claimed_type`,
   only when the rewrite actually changes the type: never-downgraded
   claims gain no key, so the artefact grows no noise.

2. A preview artefact recorded the vocabulary's SOURCE (registry
   vs built-in, and a type count) but not the vocabulary itself, so
   a reader could not reconstruct which types a run judged against
   without the registry that happened to be live that day. The
   artefact now carries `relationship_type_vocabulary` — the
   effective type list, read from the same single vocabulary object
   the run's prompts and judges read.

The run-history mirror carries claimed_type inside the provenance
jsonb when a claim has it: it is a registry vocabulary token, not
evidence or free text, so it crosses on the mirror's existing
terms (metadata only, no verbatim text).

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _chs(*texts):
    return [{"index": i + 1, "label": f"Ch{i + 1}", "text": t}
            for i, t in enumerate(texts)]


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


class DowngradeStampTests(unittest.TestCase):
    """claimed_type is stamped at the downgrade rewrite, and
    nowhere else: the key's ABSENCE on every other claim shape is
    the pin that the artefact grows no noise."""

    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _ScriptedValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def _reduce(self, rels, preview=False):
        chapter_as = {1: {"relationships": rels,
                          "pov_character": None}}
        return pp.v2_reduce(chapter_as, {}, {},
                            _chs("x " * 200), preview=preview)

    def test_downgrade_stamps_the_claimed_type(self):
        # Middle band (0.30 <= P < 0.70): the claim ships, typed
        # "other" — and now says what it was claimed as.
        self.validator.p_by_pair[("Alice", "Bob")] = 0.50
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "spouse",
            "evidence": "his wife Alice poured the coffee"}])
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "other")
        self.assertEqual(rel["validation_note"], "downgraded")
        self.assertEqual(rel["claimed_type"], "spouse")

    def test_kept_claim_gains_no_stamp(self):
        self.validator.p_by_pair[("Alice", "Bob")] = 0.90
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "spouse",
            "evidence": "his wife Alice poured the coffee"}])
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "spouse")
        self.assertEqual(rel["validation_note"], "kept")
        self.assertNotIn("claimed_type", rel)

    def test_dropped_claim_keeps_its_type_and_gains_no_stamp(self):
        # Below the drop threshold the claim leaves WITH its
        # original family type (fail-closed convention): the type
        # is on the face of the record, so a stamp would be a
        # second copy of a fact that was never erased.
        self.validator.p_by_pair[("Alice", "Bob")] = 0.10
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "spouse",
            "evidence": "a sentence about two strangers"}])
        self.assertEqual(red["relationships"], [])
        dropped = red["dropped_relationships"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["type"], "spouse")
        self.assertNotIn("claimed_type", dropped[0])

    def test_preview_downgrade_rewrites_nothing_and_stamps_nothing(self):
        # Preview mode records the "downgraded" note but does NOT
        # rewrite the type — the claimed type is still on the face
        # of the claim, so there is no erasure to repair.
        self.validator.p_by_pair[("Alice", "Bob")] = 0.50
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "spouse",
            "evidence": "his wife Alice poured the coffee"}],
            preview=True)
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "spouse")
        self.assertEqual(rel["validation_note"], "downgraded")
        self.assertNotIn("claimed_type", rel)

    def test_nonfamily_middle_band_never_downgrades(self):
        # The non-family pass has no downgrade tier: a middle-band
        # enemy claim keeps its type and gains no stamp.
        self.validator.p_by_pair[("Alice", "Bob")] = 0.50
        red = self._reduce([{
            "from": "Alice", "to": "Bob", "type": "enemy",
            "evidence": "they glared at each other across the hall"}])
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "enemy")
        self.assertNotIn("claimed_type", rel)


class VocabularyStampTests(unittest.TestCase):
    """The artefact's vocabulary is the run's ACTUAL resolution:
    read from the single vocabulary object, so installing a
    registry vocabulary changes the stamp with no second list to
    keep in sync."""

    def setUp(self):
        self._vocab = pp._rel_vocab()

    def tearDown(self):
        pp._apply_rel_vocab(self._vocab)

    def test_helper_reads_the_active_vocabulary(self):
        self.assertEqual(
            pp._rel_vocabulary_ids(),
            [e["id"] for e in pp._rel_vocab()["types"]])
        self.assertEqual(
            pp._rel_vocabulary_ids(),
            [r["id"] for r in pp._BUILTIN_REL_TYPES])

    def test_helper_reflects_an_installed_registry_vocabulary(self):
        rows = [dict(r) for r in pp._BUILTIN_REL_TYPES]
        rows.append({"id": "blood_oath", "label": "Blood oath",
                     "gloss": "Bound by a blood oath.",
                     "direction": "either", "is_family": False,
                     "exclusivity": "exclusive", "precedence": 3,
                     "graph_group": "friend", "suggestible": True})
        pp._apply_rel_vocab(pp._derive_rel_vocab(rows))
        ids = pp._rel_vocabulary_ids()
        self.assertIn("blood_oath", ids)
        self.assertEqual(len(ids), len(pp._BUILTIN_REL_TYPES) + 1)

    def test_reduce_output_carries_the_vocabulary_in_force(self):
        chapter_as = {1: {"relationships": [], "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, {}, _chs("x " * 200),
                           preview=True)
        self.assertEqual(red["relationship_type_vocabulary"],
                         pp._rel_vocabulary_ids())

    def test_reduce_vocabulary_follows_an_installed_registry(self):
        rows = [dict(r) for r in pp._BUILTIN_REL_TYPES]
        rows.append({"id": "blood_oath", "label": "Blood oath",
                     "gloss": "Bound by a blood oath.",
                     "direction": "either", "is_family": False,
                     "exclusivity": "exclusive", "precedence": 3,
                     "graph_group": "friend", "suggestible": True})
        pp._apply_rel_vocab(pp._derive_rel_vocab(rows))
        chapter_as = {1: {"relationships": [], "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, {}, _chs("x " * 200),
                           preview=True)
        self.assertIn("blood_oath",
                      red["relationship_type_vocabulary"])
        self.assertEqual(red["relationship_type_vocabulary"],
                         pp._rel_vocabulary_ids())


def _provenance(endpoint, form):
    return {"endpoint": endpoint, "matched_form": form,
            "basis": {"kinds": ["antecedent"], "chapter": 22,
                      "position": 3,
                      "text": "Sebeck's son, Chris, stood next "
                              "to him."}
            if form == "resolved_surname" else None}


class HistoryMirrorStampTests(unittest.TestCase):
    """claimed_type crosses into run history inside the
    provenance mirror jsonb when a claim carries it — and the
    mirror's shape for claims without it is byte-identical to
    before (the 2.13.27 uniform-shape discipline lives at the
    row level; inside the jsonb, an absent stamp stays absent)."""

    def _result(self):
        return {
            "_history_book_key": "book-key",
            "relationships": [
                {"from": "Laura Sebeck", "to": "Peter Sebeck",
                 "type": "other", "claimed_type": "spouse",
                 "validation_state": "validated",
                 "validation_p": 0.58,
                 "endpoint_provenance": {
                     "from": _provenance("Laura Sebeck",
                                         "resolved_surname"),
                     "to": _provenance("Peter Sebeck", "full")}},
                {"from": "Chris Sebeck", "to": "Peter Sebeck",
                 "type": "child",
                 "validation_state": "validated",
                 "validation_p": 0.88,
                 "endpoint_provenance": {
                     "from": _provenance("Chris Sebeck",
                                         "resolved_surname"),
                     "to": _provenance("Peter Sebeck", "full")}},
            ],
            "dropped_relationships": [],
        }

    def test_mirror_carries_claimed_type_when_the_claim_does(self):
        _claims, rows = pp._history_book_claims(self._result())
        rel_rows = [r for r in rows if r["axis"] == "relationships"]
        self.assertEqual(len(rel_rows), 2)
        by_conf = {r["confidence"]: r for r in rel_rows}
        mirror = by_conf[0.58]["endpoint_provenance"]
        self.assertEqual(mirror["claimed_type"], "spouse")
        # The mirror stays metadata-only: the verbatim basis
        # text never crosses, stamp or no stamp.
        self.assertNotIn("Sebeck's son", json.dumps(mirror))

    def test_mirror_shape_unchanged_without_a_stamp(self):
        _claims, rows = pp._history_book_claims(self._result())
        rel_rows = [r for r in rows if r["axis"] == "relationships"]
        by_conf = {r["confidence"]: r for r in rel_rows}
        mirror = by_conf[0.88]["endpoint_provenance"]
        self.assertEqual(set(mirror), {"from", "to"})


if __name__ == "__main__":
    unittest.main()
