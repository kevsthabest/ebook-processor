"""--jev-merge packet v2: the judge gets an evidence brief, not a
name card, and the deterministic janitor keeps the roster clean.

Bake-off run 1 (v2.13.79) judged 176 pairs and merged ZERO: the v1
packet carried only name forms, chapter counts and own-form snippets,
so the scores floated free of identity truth (a pair differing only
in quote marks scored 0.74; 'Heinrich Boerner' / 'Special Agent
Boerner' scored 0.04; cross-person Gragg/Sebeck scored 0.69). The
owner ruled: no eval-first phase — ship packet v2 and the janitor
clause direct; the next run is the test (packet spec §2 and §6; the
threshold stays 0.85 and no three-band outcome is built).

Packet v2 sections, in order: identity cards; co-occurrence facts
(including the explicit never-co-listed line); bridging passages;
definitional passages; scene-partner contrast; claim provenance
(labelled as CLAIMS). No matcher scores or similarity numbers appear
anywhere in the brief. The judge prompt gains a short "reading this
brief" block in the packet itself; Tier 3's _SAME_PERSON_INSTRUCTIONS
stay byte-identical.

Janitor clause: under --jev-merge, _roster_reconcile runs prune-only
(shadow pruning + cannot-link pruning, v2.13.78 semantics) before the
judge pass; its merges do NOT run — merging is the judge's job.

Hermetic: the judge is a transport double; stdlib unittest only.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import re
import unittest
from collections import Counter
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _entry(name, aliases=(), chapters=(0,), appearances=3, evidence="",
           descriptions=()):
    """A roster entry in the shape _roster_update produces."""
    return {
        "name": name,
        "aliases": set(aliases),
        "alias_keys": {pp.norm_name(a) for a in aliases},
        "primary_keys": {pp.norm_name(name)},
        "appearances": appearances,
        "chapters": set(chapters),
        "roles": Counter(),
        "descriptions": list(descriptions),
        "evidence": evidence,
        "statuses": Counter(),
        "death_reports": [],
        "alive_reports": set(),
        "unresolved_mentions": set(),
        "appearances_desc": [],
        "_generic_keys": set(),
    }


def _roster(*entries):
    return {pp.norm_name(e["name"]): e for e in entries}


_CHAPTERS = [
    {"index": 0, "label": "Ch 1",
     "text": "Heinrich Boerner waited by the gate. Boerner checked his "
             "watch twice. Heinrich Boerner, the BKA liaison, had flown "
             "in from Berlin."},
    {"index": 1, "label": "Ch 2",
     "text": "Major Pain in the Ass logged into the game at midnight. "
             "Karl Devon briefed the Pentagon staff. Devon frowned at "
             "the report. Major Karl Devon, the Pentagon liaison, took "
             "his seat."},
    {"index": 2, "label": "Ch 3",
     "text": "Peter Sebeck interviewed the witness. Detective Sebeck, "
             "the lead investigator, closed the folder."},
    {"index": 3, "label": "Ch 4",
     "text": "Brian Gragg sold laptops out of the trunk. Gragg counted "
             "the money twice."},
    {"index": 4, "label": "Ch 5",
     "text": "Karl Devon returned to the Pentagon. The Major outlined "
             "the operation in silence. The Major, Devon himself, had "
             "everyone's attention."},
    {"index": 5, "label": "Ch 6",
     "text": "The Major reviewed the deployment maps. The Major gave "
             "the order at dawn."},
    {"index": 6, "label": "Ch 7",
     "text": "Boerner filed the report in triplicate."},
    {"index": 7, "label": "Ch 8",
     "text": "Boerner took the night train to Frankfurt."},
    {"index": 8, "label": "Ch 9",
     "text": "Special Agent Boerner arrived at the field office."},
    {"index": 9, "label": "Ch 10",
     "text": "Special Agent Boerner debriefed the team."},
    {"index": 10, "label": "Ch 11",
     "text": "Special Agent Boerner signed the warrant."},
    {"index": 11, "label": "Ch 12",
     "text": "Sebeck questioned Gragg at the station. Gragg denied "
             "everything."},
]
_BY_INDEX = {c["index"]: c["text"] for c in _CHAPTERS}


class _FakeValidator:
    disabled = False

    def __init__(self, answers=None, default=(False, 0.10)):
        self.answers = answers or {}
        self.default = default
        self.states = []

    def ask_same_person_packet(self, state):
        self.states.append(state)
        a = re.search(r"^Character A: (.+)$", state, re.M).group(1)
        b = re.search(r"^Character B: (.+)$", state, re.M).group(1)
        ans = self.answers.get(frozenset((a, b)), self.default)
        if ans == "raise":
            raise RuntimeError("transport down")
        return ans


def _packet(roster, ka, kb):
    text, _facts = pp._jev_merge_packet(
        ka, roster[ka], kb, roster[kb], _BY_INDEX, {}, roster=roster)
    return text


class _Base(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()


class TestCooccurrenceFacts(_Base):
    def test_never_co_listed_line_for_disjoint_chapter_rich_pair(self):
        """Boerner shape: two forms of (plausibly) one person, each
        in 3+ chapters, never sharing one — the packet states the
        never-co-listed fact explicitly."""
        roster = _roster(
            _entry("Boerner", chapters=(0, 6, 7), appearances=9),
            _entry("Special Agent Boerner", chapters=(8, 9, 10),
                   appearances=8))
        text = _packet(roster, "boerner", "special agent boerner")
        self.assertIn("Never co-listed", text)
        self.assertIn("Shared chapters: 0", text)

    def test_never_line_omitted_when_co_listed(self):
        """Co-listed entries DO appear together — the line must not
        appear (Sebeck and Gragg share the interrogation chapter)."""
        roster = _roster(
            _entry("Peter Sebeck", chapters=(2, 11), appearances=6),
            _entry("Brian Gragg", chapters=(3, 11), appearances=4))
        text = _packet(roster, "peter sebeck", "brian gragg")
        self.assertNotIn("Never co-listed", text)
        self.assertIn("Shared chapters: 1", text)

    def test_never_line_omitted_when_a_side_is_chapter_poor(self):
        """Disjoint but thin: a form seen in only 2 chapters proves
        nothing by its absence, so the line stays out."""
        roster = _roster(
            _entry("Boerner", chapters=(0, 6), appearances=4),
            _entry("Special Agent Boerner", chapters=(8, 9, 10),
                   appearances=8))
        text = _packet(roster, "boerner", "special agent boerner")
        self.assertNotIn("Never co-listed", text)


class TestBridgingPassages(_Base):
    def test_both_forms_sentence_selected_as_bridging(self):
        """A sentence carrying forms of BOTH entries is the strongest
        link evidence there is; it must reach the packet."""
        roster = _roster(
            _entry("Karl Devon", aliases=("Devon",), chapters=(1, 4)),
            _entry("The Major", chapters=(4, 5)))
        text = _packet(roster, "karl devon", "major")
        self.assertIn("Bridging passages", text)
        self.assertIn("The Major, Devon himself", text)

    def test_window_passage_selected_when_forms_are_neighbours(self):
        """Forms in adjacent sentences (within ±2) form a window
        passage even when no single sentence carries both."""
        roster = _roster(
            _entry("Karl Devon", aliases=("Devon",), chapters=(1, 4)),
            _entry("The Major", chapters=(4, 5)))
        text, facts = pp._jev_merge_packet(
            "karl devon", roster["karl devon"], "major",
            roster["major"], _BY_INDEX, {}, roster=roster)
        self.assertIn("The Major outlined the operation", text)
        self.assertGreaterEqual(facts["bridging_found"], 1)


class TestDefinitionalPassages(_Base):
    def test_appositive_sentence_harvested(self):
        roster = _roster(
            _entry("Heinrich Boerner", chapters=(0,), appearances=5),
            _entry("Boerner", chapters=(0, 6, 7), appearances=9))
        text, facts = pp._jev_merge_packet(
            "heinrich boerner", roster["heinrich boerner"], "boerner",
            roster["boerner"], _BY_INDEX, {}, roster=roster)
        self.assertIn("Definitional passages", text)
        self.assertIn("Heinrich Boerner, the BKA liaison", text)
        self.assertGreaterEqual(facts["definitional_found"], 1)

    def test_first_mention_fallback_when_no_pattern_matches(self):
        """Gragg has no appositive/copula introduction anywhere; the
        brief falls back to his first-mention sentence, labelled as
        a first mention rather than a definition."""
        roster = _roster(
            _entry("Brian Gragg", chapters=(3,), appearances=4),
            _entry("Peter Sebeck", chapters=(2, 11), appearances=6))
        text = _packet(roster, "brian gragg", "peter sebeck")
        self.assertIn("Brian Gragg sold laptops out of the trunk",
                      text)
        self.assertIn("first mention", text.lower())


class TestPartnerContrast(_Base):
    def test_partner_lists_the_right_names(self):
        """Sebeck's scene partner is Gragg (shared interrogation
        chapter); the contrast section names him on Sebeck's side."""
        roster = _roster(
            _entry("Peter Sebeck", chapters=(2, 11), appearances=6),
            _entry("Brian Gragg", chapters=(3, 11), appearances=4),
            _entry("Karl Devon", aliases=("Devon",), chapters=(1, 4)))
        text = _packet(roster, "peter sebeck", "karl devon")
        self.assertIn("Frequent companions A", text)
        partners_a = text.split("Frequent companions A:")[1] \
            .split("\n")[0]
        self.assertIn("Brian Gragg", partners_a)
        partners_b = text.split("Frequent companions B:")[1] \
            .split("\n")[0]
        self.assertNotIn("Brian Gragg", partners_b)


class TestProvenance(_Base):
    def test_claims_labelled_as_claims(self):
        """An extractor alias claim between the pair is reported as
        a CLAIM (the extractor's guess), never as a fact."""
        a = _entry("Zelda Voss", chapters=(0,))
        b = _entry("Miriam Okafor", chapters=(0,))
        b["alias_keys"].add(pp.norm_name("Zelda Voss"))
        b["aliases"].add("Zelda Voss")
        roster = _roster(a, b)
        text = _packet(roster, "miriam okafor", "zelda voss")
        self.assertIn("Claims (unverified", text)
        self.assertIn("CLAIM:", text)
        self.assertIn("Zelda Voss", text)

    def test_no_scores_anywhere_in_the_packet(self):
        """The brief carries facts and passages only: no nomination
        signal, no similarity score, nothing for the judge to
        anchor on."""
        roster = _roster(
            _entry("Karl Devon", aliases=("Devon",), chapters=(1, 4)),
            _entry("The Major", chapters=(4, 5)))
        text = _packet(roster, "karl devon", "major")
        low = text.lower()
        self.assertNotIn("signal", low)
        self.assertNotIn("score", low)
        self.assertNotIn("similarity", low)


class TestReadingBriefBlock(_Base):
    def test_packet_carries_the_brief_block(self):
        roster = _roster(
            _entry("Karl Devon", aliases=("Devon",), chapters=(1, 4)),
            _entry("The Major", chapters=(4, 5)))
        text = _packet(roster, "karl devon", "major")
        self.assertIn("HOW TO READ THIS BRIEF", text)
        self.assertIn("one person sighted under two names", text)

    def test_tier3_instructions_byte_identical(self):
        """The block lives in the packet, not in the shared
        instructions: _SAME_PERSON_INSTRUCTIONS is untouched and
        carries no brief language."""
        self.assertNotIn("HOW TO READ THIS BRIEF",
                         pp._SAME_PERSON_INSTRUCTIONS)
        self.assertNotIn("brief", pp._SAME_PERSON_INSTRUCTIONS.lower())


class TestDiagnosticsV2(_Base):
    def test_packet_version_and_fact_counts_recorded(self):
        roster = _roster(
            _entry("Karl Devon", aliases=("Devon", "Major Devon"),
                   chapters=(1, 4)),
            _entry("The Major", chapters=(4, 5)))
        judge = _FakeValidator(default=(False, 0.2))
        diag = pp.jev_merge_pass(roster, _CHAPTERS, validator=judge)
        self.assertEqual(diag["packet_version"], 2)
        verdict = [v for v in diag["verdicts"]
                   if {v["a"], v["b"]} == {"Karl Devon", "The Major"}]
        self.assertEqual(len(verdict), 1)
        v = verdict[0]
        self.assertEqual(v["shared_chapters"], 1)
        self.assertGreaterEqual(v["bridging_found"], 1)
        self.assertGreaterEqual(v["definitional_found"], 1)


class TestJanitor(_Base):
    def test_prune_only_prunes_shadow_and_no_but_never_merges(self):
        """Janitor end state under --jev-merge: a shadowed alias and
        a NO-banned alias are pruned, while a mutual-claim pair the
        full reconcile would merge stays two entries."""
        devon = _entry("Karl Devon", chapters=(1,), appearances=6)
        pain = _entry("Major Pain in the Ass", aliases=("MPITA",),
                      chapters=(2,), appearances=4)
        # Polluted aliases: Devon's key rides on the Pain entry
        # (shadowed by Devon's own entry AND banned by the NO), and
        # the two entries claim each other (a mutual claim the merge
        # passes would fold, with no chapter overlap).
        pain["alias_keys"].add("karl devon")
        pain["aliases"].add("Karl Devon")
        devon["alias_keys"].add("major pain in the ass")
        devon["aliases"].add("Major Pain in the Ass")
        roster = _roster(devon, pain)
        stats = pp._roster_reconcile(
            roster, no_pairs=[("karl devon", "major pain in the ass")],
            prune_only=True)
        self.assertEqual(stats["variants_merged"], 0)
        self.assertEqual(sorted(roster),
                         ["karl devon", "major pain in the ass"])
        self.assertNotIn("karl devon",
                         roster["major pain in the ass"]["alias_keys"])
        self.assertNotIn("Karl Devon",
                         roster["major pain in the ass"]["aliases"])
        self.assertNotIn("major pain in the ass",
                         roster["karl devon"]["alias_keys"])
        self.assertGreaterEqual(stats["aliases_pruned"], 2)

    def test_prune_only_control_full_reconcile_would_merge(self):
        """Control: the same mutual-claim shape WITHOUT prune_only
        does merge — the flag is what holds the merger back."""
        devon = _entry("Karl Devon", chapters=(1,), appearances=6)
        pain = _entry("Major Pain in the Ass", aliases=("MPITA",),
                      chapters=(2,), appearances=4)
        pain["alias_keys"].add("karl devon")
        pain["aliases"].add("Karl Devon")
        devon["alias_keys"].add("major pain in the ass")
        devon["aliases"].add("Major Pain in the Ass")
        roster = _roster(devon, pain)
        stats = pp._roster_reconcile(roster)
        self.assertEqual(stats["variants_merged"], 1)
        self.assertEqual(len(roster), 1)


class TestJanitorWiring(unittest.TestCase):
    def test_jev_branch_runs_janitor_before_the_pass(self):
        src = Path(_MOD_PATH).read_text(encoding="utf-8")
        janitor_at = src.index("prune_only=True")
        pass_at = src.index("_merge_judge_stats = jev_merge_pass(")
        self.assertLess(janitor_at, pass_at)


if __name__ == "__main__":
    unittest.main()
