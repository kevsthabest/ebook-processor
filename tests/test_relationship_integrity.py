"""Relationship integrity: family guard, direction convention, alias
pollution in endpoint canonicalization.

Regression context (Fourth Wing baseline validation, 2026-10):
- A run claimed "Mira Sorrengail & Brennan Sorrengail — enemies" although a
  sibling claim existed for the same pair: a strong-evidence non-family
  claim outranked the family claim in v2_reduce winner selection.
- Violet->Violet self-pairs ("child"/"sibling"/"friend") carried verified
  evidence about third parties: Violet's roster entry was polluted with
  other people's names as aliases ('Lilith Sorrengail', 'Mira Sorrengail',
  ...), and _roster_canonical resolved those endpoints to Violet.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import json
import unittest
from pathlib import Path
import importlib.util
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _ch(idx, label, text="x " * 200):
    return {"index": idx, "label": label, "text": text}


def _char(name, aliases=None):
    return {"name": name, "aliases": aliases or [], "role": "supporting",
            "description": "", "evidence": ""}


def _reduce(rels_by_chapter, roster=None, preview=False):
    """rels_by_chapter: {idx: [(from, to, type, evidence)]}"""
    chapter_as = {}
    for idx, rels in rels_by_chapter.items():
        chapter_as[idx] = {"relationships": [
            {"from": f, "to": t, "type": ty, "evidence": ev}
            for f, t, ty, ev in rels], "pov_character": None}
    chs = [_ch(i, f"Ch{i}") for i in sorted(rels_by_chapter)]
    return pp.v2_reduce(chapter_as, {}, roster or {}, chs, preview=preview)


class TestCanonicalPair(unittest.TestCase):
    def test_parent_no_flip_when_already_canonical(self):
        self.assertEqual(pp._canonical_pair("Alice", "Bob", "parent"),
                         ("Alice", "Bob", "parent"))

    def test_parent_flips_to_child_on_reversal(self):
        # "Bob is Alice's parent" stored canonically (Alice first) must
        # become "Alice is Bob's child" — the same fact, flipped type.
        self.assertEqual(pp._canonical_pair("Bob", "Alice", "parent"),
                         ("Alice", "Bob", "child"))

    def test_child_flips_to_parent_on_reversal(self):
        self.assertEqual(pp._canonical_pair("Bob", "Alice", "child"),
                         ("Alice", "Bob", "parent"))

    def test_equivalent_claims_normalize_identically(self):
        a = pp._canonical_pair("Alice", "Bob", "parent")
        b = pp._canonical_pair("Bob", "Alice", "child")
        self.assertEqual(a, b)

    def test_symmetric_types_unaffected_by_reversal(self):
        for t in ("spouse", "sibling", "friend", "enemy", "colleague"):
            self.assertEqual(pp._canonical_pair("Bob", "Alice", t),
                             ("Alice", "Bob", t))

    def test_mentor_keeps_claim_direction(self):
        # "mentor" has no inverse in the type vocabulary: stored
        # mentor-first (claim direction), the documented exception.
        self.assertEqual(pp._canonical_pair("Bob", "Alice", "mentor"),
                         ("Bob", "Alice", "mentor"))

    def test_self_pair_is_none(self):
        self.assertIsNone(pp._canonical_pair("Alice", "alice", "friend"))
        self.assertIsNone(pp._canonical_pair("Alice", "Alice", "parent"))


class TestDirectionConventionReduce(unittest.TestCase):
    def test_parent_claim_stored_canonically(self):
        red = _reduce({1: [("Alice", "Bob", "parent", "ev")]})
        rel = red["relationships"][0]
        self.assertEqual((rel["from"], rel["to"], rel["type"]),
                         ("Alice", "Bob", "parent"))

    def test_reversed_parent_claim_flips_type(self):
        red = _reduce({1: [("Bob", "Alice", "parent", "ev")]})
        rel = red["relationships"][0]
        self.assertEqual((rel["from"], rel["to"], rel["type"]),
                         ("Alice", "Bob", "child"))

    def test_equivalent_claims_produce_identical_output(self):
        r1 = _reduce({1: [("Alice", "Bob", "parent", "ev")]})["relationships"]
        r2 = _reduce({1: [("Bob", "Alice", "child", "ev")]})["relationships"]
        self.assertEqual(
            [(r["from"], r["to"], r["type"]) for r in r1],
            [(r["from"], r["to"], r["type"]) for r in r2])

    def test_symmetric_reversal_keeps_type(self):
        red = _reduce({1: [("Bob", "Alice", "spouse", "ev")]})
        rel = red["relationships"][0]
        self.assertEqual((rel["from"], rel["to"], rel["type"]),
                         ("Alice", "Bob", "spouse"))

    def test_contradictory_directions_do_not_inflate_evidence(self):
        # (Alice->Bob, parent) and (Bob->Alice, parent) are CONTRADICTORY
        # claims. After normalization the second is (Alice, Bob, child):
        # they compete as different types, one winner, evidence_count 1 —
        # previously both counted toward "parent".
        red = _reduce({
            1: [("Alice", "Bob", "parent", "ev1")],
            2: [("Bob", "Alice", "parent", "ev2")],
        })
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "parent")
        self.assertEqual(rel["evidence_count"], 1)


class TestFamilyGuard(unittest.TestCase):
    # The Fourth Wing shape: the sibling claim's evidence did not survive
    # verbatim verification (empty at reduce), the pair co-occurs in only
    # 2 chapters (family confidence = medium), while "enemy" is claimed
    # with evidence twice (confidence = high). Before the guard, "enemy"
    # won the pair.
    _FW = {
        1: [("Mira Sorrengail", "Brennan Sorrengail", "sibling", ""),
            ("Mira Sorrengail", "Brennan Sorrengail", "enemy", "they fought")],
        2: [("Brennan Sorrengail", "Mira Sorrengail", "enemy", "he glared")],
    }

    def test_family_wins_over_enemy_preview(self):
        red = _reduce(self._FW, preview=True)
        self.assertEqual(len(red["relationships"]), 1)
        rel = red["relationships"][0]
        self.assertEqual(rel["type"], "sibling")
        self.assertEqual({rel["from"], rel["to"]},
                         {"Mira Sorrengail", "Brennan Sorrengail"})

    def test_suppressed_claim_is_auditable(self):
        red = _reduce(self._FW, preview=True)
        dropped = red["dropped_relationships"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["type"], "enemy")
        self.assertEqual(dropped[0]["family_guard"], "suppressed_by_family")
        self.assertEqual(dropped[0]["suppressed_by_type"], "sibling")

    def test_family_winner_no_evidence_drops_in_write_mode(self):
        # BAR-MOVE (family fail-closed, 2026-10-14): this test used to
        # pin the guard winner surviving write mode as "other" with
        # note "no_evidence" (the auto-downgrade). Fail-closed: the
        # evidence-less family winner DROPS, so the pair ends with no
        # surviving relationship, and dropped_relationships carries
        # BOTH audit entries — the suppressed enemy (family_guard at
        # selection) and the dropped sibling (no_evidence at
        # validation).
        red = _reduce(self._FW, preview=False)
        self.assertEqual(red["relationships"], [])
        dropped = red["dropped_relationships"]
        self.assertTrue(any(d.get("family_guard") == "suppressed_by_family"
                            for d in dropped))
        sibling_drops = [d for d in dropped
                         if d.get("validation_note") == "no_evidence"]
        self.assertEqual(len(sibling_drops), 1)
        self.assertEqual(sibling_drops[0]["type"], "sibling")

    def test_no_family_claim_no_guard(self):
        red = _reduce({
            1: [("Alice", "Bob", "friend", "ev1")],
            2: [("Alice", "Bob", "enemy", "ev2")],
        }, preview=True)
        self.assertEqual(len(red["relationships"]), 1)
        self.assertFalse(any("family_guard" in d
                             for d in red["dropped_relationships"]))

    def test_family_vs_family_uses_normal_scoring(self):
        red = _reduce({
            1: [("Alice", "Bob", "spouse", "they married")],
            2: [("Alice", "Bob", "sibling", "her brother")],
        }, preview=True)
        self.assertEqual(len(red["relationships"]), 1)
        self.assertIn(red["relationships"][0]["type"],
                      pp._REL_FAMILY_TYPES)
        # The losing family claim lost by scoring, not by the guard:
        # nothing is recorded as guard-suppressed.
        self.assertFalse(any("family_guard" in d
                             for d in red["dropped_relationships"]))

    def test_guard_output_json_round_trip(self):
        red = _reduce(self._FW, preview=True)
        payload = {"relationships": red["relationships"],
                   "dropped_relationships": red["dropped_relationships"]}
        back = json.loads(json.dumps(payload))
        self.assertEqual(back, payload)
        self.assertEqual(back["dropped_relationships"][0]["family_guard"],
                         "suppressed_by_family")


class TestAliasPollution(unittest.TestCase):
    """The FW -12 smoking gun: Violet's roster entry carried other
    characters' names as aliases, and _roster_canonical resolved those
    names to Violet, collapsing pairs into Violet->Violet self-pairs."""

    def _polluted_roster(self):
        roster = {}
        # Violet's chapter entry lists OTHER people's names as aliases
        # (LLM error). Lilith/Mira do not exist as entries yet, so the
        # fold-time prune cannot catch this — resolution must.
        pp._roster_update(roster, [_char(
            "Violet Sorrengail",
            ["Lilith Sorrengail", "Mira Sorrengail", "Mom", "Mother"])], 1)
        pp._roster_update(roster, [_char("Lilith Sorrengail")], 2)
        pp._roster_update(roster, [_char("Mira Sorrengail")], 2)
        pp._roster_update(roster, [_char("Brennan Sorrengail")], 2)
        return roster

    def test_real_character_wins_over_polluted_alias(self):
        roster = self._polluted_roster()
        self.assertEqual(pp._roster_canonical(roster, "Lilith Sorrengail"),
                         "Lilith Sorrengail")
        self.assertEqual(pp._roster_canonical(roster, "Mira Sorrengail"),
                         "Mira Sorrengail")
        self.assertEqual(pp._roster_canonical(roster, "Brennan Sorrengail"),
                         "Brennan Sorrengail")
        self.assertEqual(pp._roster_canonical(roster, "Violet Sorrengail"),
                         "Violet Sorrengail")

    def test_polluted_pair_does_not_collapse(self):
        roster = self._polluted_roster()
        red = _reduce({
            1: [("Violet Sorrengail", "Lilith Sorrengail", "child",
                 "her mother Lilith"),
                ("Violet Sorrengail", "Mira Sorrengail", "sibling",
                 "her sister Mira")],
        }, roster=roster)
        self.assertEqual(len(red["relationships"]), 2)
        for rel in red["relationships"]:
            self.assertNotEqual(rel["from"], rel["to"])
        by_pair = {tuple(sorted([r["from"], r["to"]])): r
                   for r in red["relationships"]}
        # "Violet is Lilith's child" in canonical form: Lilith first,
        # type flipped to "parent" (Lilith is Violet's parent).
        vl = by_pair[("Lilith Sorrengail", "Violet Sorrengail")]
        self.assertEqual((vl["from"], vl["to"], vl["type"]),
                         ("Lilith Sorrengail", "Violet Sorrengail", "parent"))
        mv = by_pair[("Mira Sorrengail", "Violet Sorrengail")]
        self.assertEqual(mv["type"], "sibling")

    def test_fold_time_prune_blocks_known_names(self):
        roster = {}
        pp._roster_update(roster, [_char("Lilith Sorrengail")], 1)
        pp._roster_update(roster, [_char("Violet Sorrengail")], 1)
        # A later chapter re-asserts the polluted alias for Violet; Lilith
        # is a known character by now, so the alias must not be adopted.
        pp._roster_update(roster, [_char(
            "Violet Sorrengail", ["Lilith Sorrengail", "Vi"])], 2)
        violet = roster[pp.norm_name("Violet Sorrengail")]
        self.assertNotIn(pp.norm_name("Lilith Sorrengail"),
                         violet["alias_keys"])
        self.assertIn(pp.norm_name("Vi"), violet["alias_keys"])
        self.assertNotIn("Lilith Sorrengail", violet["aliases"])
        self.assertIn("Vi", violet["aliases"])

    def test_merge_fold_prune_blocks_third_party_names(self):
        # _merge_roster_entries unions the dropped entry's alias keys into
        # the keeper; a polluted key naming a THIRD character must be
        # pruned, or the keeper starts resolving that character's
        # endpoints to itself.
        def entry(name, key, alias_keys):
            return {"name": name, "aliases": set(), "alias_keys": alias_keys,
                    "primary_keys": {key}, "appearances": 1,
                    "chapters": set(), "roles": pp.Counter(),
                    "descriptions": [], "evidence": ""}
        roster = {
            "violet sorrengail": entry("Violet Sorrengail", "violet sorrengail",
                                       {"violet sorrengail"}),
            "violet s": entry("Violet S.", "violet s",
                              {"violet s", "lilith sorrengail"}),
            "lilith sorrengail": entry("Lilith Sorrengail", "lilith sorrengail",
                                       {"lilith sorrengail"}),
        }
        pp._merge_roster_entries(roster, "violet sorrengail", "violet s")
        keeper = roster["violet sorrengail"]
        self.assertIn("violet s", keeper["alias_keys"])  # variant survives
        self.assertNotIn("lilith sorrengail", keeper["alias_keys"])
        self.assertEqual(pp._roster_canonical(roster, "Lilith Sorrengail"),
                         "Lilith Sorrengail")

    def test_ambiguous_alias_left_unresolved(self):
        roster = {}
        pp._roster_update(roster, [_char("John Alpha", ["Jack"])], 1)
        pp._roster_update(roster, [_char("John Beta", ["Jack"])], 1)
        # Two entries claim "Jack": resolving to either would be a guess.
        self.assertEqual(pp._roster_canonical(roster, "Jack"), "Jack")

    def test_unambiguous_alias_still_resolves(self):
        roster = {}
        pp._roster_update(roster, [_char("Robert", ["Bob"])], 1)
        self.assertEqual(pp._roster_canonical(roster, "Bob"), "Robert")


class TestDedupeRemapSelfPair(unittest.TestCase):
    def test_remap_created_self_pair_dropped(self):
        chars = [
            {"name": "Lilith Sorrengail", "description": "general",
             "confidence": 0.9},
            {"name": "Lilith  Sorrengail", "description": "general",
             "confidence": 0.5},
            {"name": "Violet Sorrengail", "description": "rider",
             "confidence": 0.9},
        ]
        rels = [
            # Between the two Lilith variants: a merge artifact once
            # remapped (both endpoints -> the canonical Lilith).
            {"from": "Lilith Sorrengail", "to": "Lilith  Sorrengail",
             "type": "friend"},
            {"from": "Violet Sorrengail", "to": "Lilith Sorrengail",
             "type": "child"},
        ]
        vecs = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        with mock.patch.object(pp, "embed_vectors",
                               lambda url, model, texts: vecs[:len(texts)]):
            _chars, new_rels, _povs, _report = pp.dedupe_characters(
                chars, rels, [])
        # Self-pair dropped; the real pair survives in canonical form:
        # "Violet is Lilith's child" -> Lilith first, type "parent".
        self.assertEqual(new_rels, [{"from": "Lilith Sorrengail",
                                     "to": "Violet Sorrengail",
                                     "type": "parent"}])


if __name__ == "__main__":
    unittest.main()
