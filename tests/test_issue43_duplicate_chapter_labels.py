"""Duplicate chapter labels must not cross validation context (issue #43).

Chapter labels are heading-derived, so duplicates are a natural
book shape: numbering restarts between the volumes of an omnibus,
and headings like "Prologue" repeat. The validation-context
resolution in v2_reduce keyed chapters by label alone, and a dict
built that way keeps only the LAST chapter carrying a label — so a
claim extracted from the FIRST "Chapter 5" was judged against the
second "Chapter 5"'s text: its evidence window came back empty
(the quote lives in the other chapter) or, worse, windowed a
different passage entirely, and its endpoint provenance was
stamped from the wrong chapter too.

The claim's own chapter identity is known where the claim is
produced: candidates are built per chapter index, and the index
rides the candidate as its internal chapter key. Validation must
resolve through that identity when it is present and fall back to
the label only for claims that carry none, so books with unique
labels validate exactly as before.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


# Two volumes, one printed label: both chapters are "Chapter 5".
# Their vocabularies are disjoint on purpose — any sentence in a
# validator context can be attributed to exactly one of them.
_FIRST_TEXT = ("The harbour bell rang over the quiet town. "
               "Alice Marlow promised Bob she would come home "
               "before winter. "
               "The ferry left at dawn without a sound.")
_FIRST_QUOTE = ("Alice Marlow promised Bob she would come home "
                "before winter.")
_SECOND_TEXT = ("The desert wind scoured the empty camp. "
                "Rosa Delgado promised Tomas she would cross the "
                "dunes before winter. "
                "The camels rested beside the dry well.")
_SECOND_QUOTE = ("Rosa Delgado promised Tomas she would cross the "
                 "dunes before winter.")


def _dup_chapters():
    return [{"index": 5, "label": "Chapter 5", "text": _FIRST_TEXT},
            {"index": 14, "label": "Chapter 5", "text": _SECOND_TEXT}]


def _roster(*names):
    roster = {}
    for name in names:
        pp._roster_update(
            roster,
            [{"name": name, "aliases": [], "role": "supporting",
              "description": "", "evidence": ""}],
            1)
    return roster


class _ContextValidator:
    """Validator stub that records the context it was shown."""

    model = "stub"

    def __init__(self, p=0.95):
        self.p = p
        self.calls = []

    def validate_relationship(self, frm, to, rtype, quote, context=""):
        self.calls.append({"from": frm, "to": to, "type": rtype,
                           "quote": quote, "context": context})
        return True, self.p


class DuplicateLabelContextTests(unittest.TestCase):
    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _ContextValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def _reduce(self, idx, rel, chapters, roster):
        chapter_as = {idx: {"relationships": [rel],
                            "pov_character": None}}
        return pp.v2_reduce(chapter_as, {}, roster, chapters,
                            preview=False)

    def test_first_occurrence_claim_windows_against_first_chapter(self):
        roster = _roster("Alice Marlow", "Bob Marlow",
                         "Rosa Delgado", "Tomas Delgado")
        red = self._reduce(5, {
            "from": "Alice Marlow", "to": "Bob Marlow",
            "type": "spouse", "evidence": _FIRST_QUOTE},
            _dup_chapters(), roster)
        self.assertEqual(len(self.validator.calls), 1)
        call = self.validator.calls[0]
        self.assertEqual(call["quote"], _FIRST_QUOTE)
        # The window is the first Chapter 5's neighbours of the
        # quote — pre-fix the label lookup landed on the second
        # Chapter 5, where this quote does not exist, and the
        # context came back empty.
        self.assertIn("harbour bell", call["context"])
        self.assertIn("ferry left at dawn", call["context"])
        self.assertNotIn("desert wind", call["context"])
        self.assertNotIn("camels", call["context"])
        rel = red["relationships"][0]
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["chapter"], "Chapter 5")
        # The chapter identity is internal plumbing: it must not
        # leak into the shipped claim shape.
        self.assertNotIn("_idx", rel)

    def test_second_occurrence_claim_windows_against_second_chapter(self):
        roster = _roster("Alice Marlow", "Bob Marlow",
                         "Rosa Delgado", "Tomas Delgado")
        red = self._reduce(14, {
            "from": "Rosa Delgado", "to": "Tomas Delgado",
            "type": "spouse", "evidence": _SECOND_QUOTE},
            _dup_chapters(), roster)
        self.assertEqual(len(self.validator.calls), 1)
        call = self.validator.calls[0]
        self.assertEqual(call["quote"], _SECOND_QUOTE)
        self.assertIn("desert wind", call["context"])
        self.assertIn("camels rested", call["context"])
        self.assertNotIn("harbour bell", call["context"])
        self.assertNotIn("ferry", call["context"])
        rel = red["relationships"][0]
        self.assertEqual(rel["validation_note"], "kept")
        self.assertEqual(rel["chapter"], "Chapter 5")
        self.assertNotIn("_idx", rel)

    def test_unique_labels_validate_byte_identically(self):
        # Same texts, distinct labels: the context the validator
        # receives is exactly the pre-fix framing — the window
        # over the claim's own chapter text, nothing added.
        chapters = [{"index": 5, "label": "Chapter 5",
                     "text": _FIRST_TEXT},
                    {"index": 14, "label": "Chapter 6",
                     "text": _SECOND_TEXT}]
        roster = _roster("Alice Marlow", "Bob Marlow",
                         "Rosa Delgado", "Tomas Delgado")
        red = self._reduce(5, {
            "from": "Alice Marlow", "to": "Bob Marlow",
            "type": "spouse", "evidence": _FIRST_QUOTE},
            chapters, roster)
        self.assertEqual(len(self.validator.calls), 1)
        expected = ("Sentences around the quote, from the same "
                    "passage: "
                    + pp._evidence_window(_FIRST_QUOTE, _FIRST_TEXT))
        self.assertEqual(self.validator.calls[0]["context"], expected)
        rel = red["relationships"][0]
        self.assertEqual(rel["chapter"], "Chapter 5")
        self.assertNotIn("_idx", rel)


class DuplicateLabelProvenanceTests(unittest.TestCase):
    """The provenance stamp is the second consumer of the same
    resolution: it must describe the claim's own chapter too. The
    surname in the quote resolves to a different person in each
    volume, so the stamp can only come out right if it was taken
    from the claim's own chapter."""

    _QUOTE = "Sebeck's son, Chris, stood next to him."
    _TEXT_A = ("Peter Sebeck arrived at the harbour office before "
               "dawn and checked the locks twice. "
               "The gulls circled over the quiet water. "
               "Sebeck's son, Chris, stood next to him.")
    _TEXT_B = ("Laura Sebeck set the table in the desert kitchen. "
               "The camels rested beside the dry well. "
               "Sebeck's son, Chris, stood next to him.")

    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.validator = _ContextValidator()
        pp._DECISION_VALIDATOR = self.validator

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    def test_provenance_stamps_from_the_claims_own_chapter(self):
        chapters = [{"index": 5, "label": "Chapter 5",
                     "text": self._TEXT_A},
                    {"index": 14, "label": "Chapter 5",
                     "text": self._TEXT_B}]
        roster = _roster("Peter Sebeck", "Laura Sebeck",
                         "Chris Sebeck")
        chapter_as = {5: {"relationships": [{
            "from": "Chris Sebeck", "to": "Peter Sebeck",
            "type": "friend", "evidence": self._QUOTE}],
            "pov_character": None}}
        red = pp.v2_reduce(chapter_as, {}, roster, chapters,
                           preview=False)
        rel = red["relationships"][0]
        prov = rel["endpoint_provenance"]["to"]
        # In the first Chapter 5 the nearest preceding full form
        # is Peter, so the surname resolves to him, with the basis
        # recorded in chapter 5. Stamped from the second Chapter 5
        # (the pre-fix label lookup) the same surname resolves to
        # Laura and the endpoint stays unresolved.
        self.assertEqual(prov["matched_form"],
                         pp.MATCH_FORM_RESOLVED_SURNAME)
        self.assertEqual(prov["basis"]["chapter"], 5)
        self.assertNotIn("_idx", rel)


if __name__ == "__main__":
    unittest.main()
