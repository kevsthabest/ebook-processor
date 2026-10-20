"""Issue #75 — surname contamination: the attestation gate.

The extractor sometimes attaches a prominent character's surname to
an unrelated character (Fourth Wing shipped "Garrick Aetos" and
"Imogen Aetos" — Aetos belongs to Dain), and because the roster
adopts the first coined form and echoes it back in every later
chapter prompt, the coined form becomes the character's canonical
name. The gate checks a candidate display name against the whole
book's text before the roster adopts it: a multi-token form must be
attested (printed as a consecutive token sequence, or with a
given-side token printed immediately before the surname). A form
that is unattested while its surname is attested on a DIFFERENT
character — the borrowing signature — falls back to the attested
shorter form or the given name alone.

Backtest (artefacts + method in the sweep folder,
attestation-gate-backtest-2026-10-20.py): on the Fourth Wing
run-20261019-114632 roster the gate catches both filed forms plus
"Bodhi Riorson"; on Daemon's baseline roster its legitimate blocks
are surnames print never places beside the given name (a son's
surname stated only as "his son Chris", a couple's shared surname in
"son of Harold and Ivana [Ross]").

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable_attest",
                                              _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _char(name, role="supporting"):
    return {"name": name, "aliases": [], "role": role}


# A miniature book. Aetos is Dain's surname and print only ever gives
# it to him; Imogen's attested full form is Imogen Cardulo; Decker is
# a surname-only agent; the persona's full screen name is printed
# verbatim once, the way game handles appear in narration.
_TEXT_A = ("Dain Aetos glared at the cadets. Imogen laughed at him. "
           "'Imogen!' Dain shouted. Imogen shook her head.")
_TEXT_B = ("Special Agent Decker arrived by train. Decker nodded. "
           "His screen name was Major Pain in the Ass, and everyone "
           "in the session knew it.")
_TEXT_C = "Imogen Cardulo signed the roster in a careful hand."


class _GateCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_learned = pp._LEARNED
        self.addCleanup(self._restore_learned)
        pp._LEARNED = pp.LearnedState(
            learn_dir=Path(self._tmp.name), enabled=False)
        self._old_attest = getattr(pp._DIAG, "name_attest", None)
        self.addCleanup(self._restore_attest)
        chapters = [{"text": t} for t in (_TEXT_A, _TEXT_B, _TEXT_C)]
        pp._DIAG.name_attest = pp._build_name_attestation(chapters)
        self._viz_patch = mock.patch.object(
            pp.UI, "viz_event", lambda key, msg: None)
        self._viz_patch.start()
        self.addCleanup(self._viz_patch.stop)

    def _restore_learned(self):
        pp._LEARNED = self._old_learned

    def _restore_attest(self):
        pp._DIAG.name_attest = self._old_attest


class TestAttestedDisplayName(_GateCase):

    def test_contaminated_surname_is_refused(self):
        # The filed defect: Aetos is attested on Dain, never on
        # Imogen. The candidate falls back to the given name alone.
        self.assertEqual(pp._attested_display_name("Imogen Aetos"),
                         "Imogen")

    def test_attested_forms_pass_unchanged(self):
        for form in ("Dain Aetos", "Imogen Cardulo", "Imogen",
                     "Major Pain in the Ass"):
            self.assertEqual(pp._attested_display_name(form), form)

    def test_surname_only_person_is_not_mangled(self):
        # "Special Agent Decker" has no given name for a surname to
        # be borrowed onto — stripping leading titles leaves one
        # token, so the gate must leave the form alone.
        self.assertEqual(
            pp._attested_display_name("Special Agent Decker"),
            "Special Agent Decker")

    def test_middle_name_form_attested_by_given_surname_pair(self):
        # The full string is printed once; even where it is not, the
        # (given, surname) pairing print attests is what legitimises
        # a middle-name form.
        pp._DIAG.name_attest = pp._build_name_attestation(
            [{"text": "Peter Sebeck opened the door. Years later the "
                      "file still read Peter Andrew Sebeck, in full."}])
        self.assertEqual(
            pp._attested_display_name("Peter Andrew Sebeck"),
            "Peter Andrew Sebeck")

    def test_gate_is_inert_without_a_book_context(self):
        # Callers outside a v2 book run (other pipelines, tooling)
        # load no attestation index; the gate must not guess there.
        pp._DIAG.name_attest = None
        self.assertEqual(pp._attested_display_name("Imogen Aetos"),
                         "Imogen Aetos")


class TestRosterUpdateGate(_GateCase):

    def test_roster_adopts_the_attested_form(self):
        roster = {}
        pp._roster_update(roster, [_char("Imogen Aetos")], 0)
        self.assertEqual(set(roster), {"imogen"})
        self.assertEqual(roster["imogen"]["name"], "Imogen")
        # Her attested full form, sighted later, is admitted as its
        # own entry at this stage — whether the bare given name and
        # the full form are one person is the post-pass merge's call,
        # as it is for any bare/full pair. What the gate guarantees
        # is that the coined form never becomes an entry at all, so
        # there is no contaminated name for the merge to adopt.
        pp._roster_update(roster, [_char("Imogen Cardulo")], 1)
        self.assertNotIn("imogen aetos", roster)
        self.assertIn("imogen cardulo", roster)
        for e in roster.values():
            self.assertNotEqual(e["name"], "Imogen Aetos")

    def test_revealed_surname_is_adopted(self):
        # The attestation index covers the whole book, so a surname
        # first printed in the last chapter legitimises the form
        # even for a sighting the extractor reports early.
        roster = {}
        pp._roster_update(roster, [_char("Imogen Cardulo")], 0)
        self.assertEqual(roster["imogen cardulo"]["name"],
                         "Imogen Cardulo")

    def test_no_context_no_gate(self):
        pp._DIAG.name_attest = None
        roster = {}
        pp._roster_update(roster, [_char("Imogen Aetos")], 0)
        self.assertEqual(roster["imogen aetos"]["name"], "Imogen Aetos")


if __name__ == "__main__":
    unittest.main()
