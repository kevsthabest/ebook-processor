"""Option B — persisted per-work roster, slices 3+4: seam, threading,
canonical rule, anchor seeding.

Design: eval/b-roster-design.md sections 5, 7, 8. One seam in
process_file_v2 (after post_pass_merge, before v2_reduce) assigns
identity; every downstream consumer inherits it — there is no
second matching pass for plan/write to disagree with.

Covered here:
  * roster_assign_entities: first run mints + annotates; second run
    links across a form flip and ships the stable canonical;
  * folds at the seam (disjoint dual links merge the roster
    entries; co-occurring dual links suspend the weaker, whose
    observation lands suspect and which ships unannotated);
  * resume idempotency: the same extraction completing twice
    records ONE observation set (fingerprint repeat path);
  * A8 anchor-fetch failure: disengage loudly when the store holds
    anchors, warn-and-proceed when it holds none;
  * anchor seeding (slice 4): preserved DB rows become anchored
    entities BEFORE matching; the anchor name is rule-proof;
    a rejected anchor suppresses its forms at row build (tier 0/1
    links only);
  * the canonical rule (slice 4): appearance-weighted, fullest
    form on near-ties, suspect observations do not vote;
  * threading: entity_id on characters, from_id/to_id on
    relationships, builders resolving canonical names; flag-off
    shapes gain NO new keys;
  * dedupe folds tombstone the folded entity ID in the store.

Stdlib unittest only. No network, no Supabase (fake sb), no real
ebook files. Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from collections import Counter
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _entry(name, chapters=(1,), appearances=10, aliases=(),
           alias_keys=(), descriptions=(), evidence=""):
    nkey = pp.norm_name(name)
    return {"name": name, "aliases": set(aliases),
            "alias_keys": {nkey} | {pp.norm_name(a) for a in alias_keys},
            "primary_keys": {nkey}, "_generic_keys": set(),
            "appearances": appearances, "last_seen": max(chapters),
            "chapters": set(chapters), "roles": Counter(),
            "descriptions": list(descriptions), "evidence": evidence}


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*args, **kwargs)
    return out, buf.getvalue()


class _SeamCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _store(self):
        return pp.RosterStore(learn_dir=self.dir, work_key="wk-test")


class TestFirstAndSecondRun(_SeamCase):
    def test_first_run_mints_annotates_and_saves(self):
        st = self._store()
        roster = {"violet sorrengail": _entry("Violet Sorrengail"),
                  "xaden riorson": _entry("Xaden Riorson")}
        marker = pp.roster_assign_entities(roster, st, anchors=[])
        self.assertIsNotNone(marker)
        self.assertEqual(marker["minted"], 2)
        self.assertEqual(marker["linked"], 0)
        for e in roster.values():
            self.assertTrue(e.get("entity_id"))
        self.assertTrue(st.path.exists())
        e = st.get_entity(roster["violet sorrengail"]["entity_id"])
        self.assertEqual(e["canonical"], "Violet Sorrengail")
        self.assertEqual(len(e["observations"]), 1)

    def test_second_run_links_across_form_flip(self):
        st = self._store()
        # Run 1's extraction claimed the short form as an alias of
        # the full form — one half of the mutual claim tier 2 needs.
        roster1 = {"violet sorrengail": _entry(
            "Violet Sorrengail", alias_keys=["Violet"])}
        pp.roster_assign_entities(roster1, st, anchors=[])
        eid1 = roster1["violet sorrengail"]["entity_id"]
        st2 = self._store()
        roster2 = {"violet": _entry("Violet",
                                    alias_keys=["Violet Sorrengail"])}
        marker = pp.roster_assign_entities(roster2, st2, anchors=[])
        self.assertEqual(marker["linked"], 1)
        self.assertEqual(marker["minted"], 0)
        self.assertEqual(roster2["violet"]["entity_id"], eid1)
        # Canonical names map: the run ships the stable name.
        self.assertEqual(marker["canonical_names"][eid1],
                         "Violet Sorrengail")
        ent = st2.get_entity(eid1)
        self.assertEqual(len(ent["observations"]), 2)
        self.assertEqual(ent["links"][0]["tier"], "alias-cross")

    def test_fingerprint_repeat_records_one_observation_set(self):
        st = self._store()
        roster = {"violet sorrengail": _entry("Violet Sorrengail")}
        pp.roster_assign_entities(roster, st, anchors=[])
        eid = roster["violet sorrengail"]["entity_id"]
        # The same extraction completing again (resume at the seam
        # boundary): fresh roster dict, identical content.
        roster2 = {"violet sorrengail": _entry("Violet Sorrengail")}
        marker = pp.roster_assign_entities(roster2, st, anchors=[])
        self.assertTrue(marker["repeat"])
        self.assertEqual(roster2["violet sorrengail"]["entity_id"],
                         eid)
        self.assertEqual(
            len(st.get_entity(eid)["observations"]), 1)
        self.assertEqual(st.runs_completed, 1)


class TestSeamFolds(_SeamCase):
    def _run1(self, st):
        roster1 = {"violet sorrengail": _entry(
            "Violet Sorrengail", chapters=(1, 2, 3, 4, 5),
            appearances=40, alias_keys=["Violet"])}
        # Run 1's extraction claimed 'Violet' as an alias.
        roster1["violet sorrengail"]["alias_keys"].add("violet")
        pp.roster_assign_entities(roster1, st, anchors=[])
        return roster1["violet sorrengail"]["entity_id"]

    def test_disjoint_dual_link_folds_roster(self):
        st = self._store()
        eid = self._run1(st)
        roster2 = {
            "violet sorrengail": _entry("Violet Sorrengail",
                                        chapters=(1, 2, 3),
                                        appearances=30),
            "violet": _entry("Violet", chapters=(4, 5),
                             appearances=12,
                             alias_keys=["Violet Sorrengail"]),
        }
        marker = pp.roster_assign_entities(roster2, st, anchors=[])
        self.assertEqual(marker["folded"], 1)
        self.assertNotIn("violet", roster2)  # folded into the keeper
        keeper = roster2["violet sorrengail"]
        self.assertEqual(keeper["entity_id"], eid)
        self.assertEqual(keeper["appearances"], 42)
        # The entity keeps BOTH observations (one per form).
        forms = [o["form"] for o in st.get_entity(eid)["observations"]
                 if o["run"] == 2]
        self.assertEqual(sorted(forms), ["Violet", "Violet Sorrengail"])

    def test_cooccurring_dual_link_suspends_weaker(self):
        st = self._store()
        eid = self._run1(st)
        roster2 = {
            "violet sorrengail": _entry("Violet Sorrengail",
                                        chapters=(1, 2, 3),
                                        appearances=30),
            "violet": _entry("Violet", chapters=(2, 4),
                             appearances=12,
                             alias_keys=["Violet Sorrengail"]),
        }
        marker = pp.roster_assign_entities(roster2, st, anchors=[])
        self.assertEqual(marker["folded"], 0)
        self.assertIn("violet", roster2)  # still ships, unannotated
        self.assertNotIn("entity_id", roster2["violet"])
        self.assertEqual(len(marker["suspended"]), 1)
        self.assertEqual(len(st.suspension_ledger()), 1)
        obs = [o for o in st.get_entity(eid)["observations"]
               if o["run"] == 2 and o["form_key"] == "violet"]
        self.assertEqual(len(obs), 1)
        self.assertTrue(obs[0]["suspect"])


class TestA8(_SeamCase):
    def test_fetch_failure_with_anchors_disengages(self):
        st = self._store()
        st.seed_anchor("Violet Sorrengail", "confirmed", "db", run=1)
        st.save()
        before = st.path.read_bytes()
        roster = {"violet sorrengail": _entry("Violet Sorrengail")}
        marker, out = _capture(pp.roster_assign_entities,
                               roster, st, anchors=None)
        self.assertIsNone(marker)
        self.assertNotIn("entity_id", roster["violet sorrengail"])
        self.assertEqual(st.path.read_bytes(), before)  # untouched

    def test_fetch_failure_without_anchors_warns_and_proceeds(self):
        st = self._store()
        roster = {"violet sorrengail": _entry("Violet Sorrengail")}
        marker, out = _capture(pp.roster_assign_entities,
                               roster, st, anchors=None)
        self.assertIsNotNone(marker)
        self.assertIn("entity_id", roster["violet sorrengail"])


class TestThreading(unittest.TestCase):
    def _roster(self, annotated):
        e = _entry("Violet Sorrengail", chapters=(1, 2), appearances=20)
        if annotated:
            e["entity_id"] = "ent_0001"
        return {"violet sorrengail": e}

    def _reduce(self, roster):
        chapter_as = {0: {"relationships": [
            {"from": "Violet Sorrengail", "to": "Xaden Riorson",
             "type": "romantic", "evidence": "q"}],
            "pov_character": None}}
        chapters = [{"index": 0, "label": "Ch 1", "text": "x"}]
        roster["xaden riorson"] = _entry("Xaden Riorson",
                                         chapters=(1,), appearances=8)
        return pp.v2_reduce(chapter_as, {}, roster, chapters)

    def test_flag_off_shapes_gain_no_keys(self):
        red = self._reduce(self._roster(False))
        for c in red["characters"]:
            self.assertNotIn("entity_id", c)
        for r in red["relationships"]:
            self.assertNotIn("from_id", r)
            self.assertNotIn("to_id", r)

    def test_flag_on_threads_ids(self):
        red = self._reduce(self._roster(True))
        chars = {c["name"]: c for c in red["characters"]}
        self.assertEqual(chars["Violet Sorrengail"]["entity_id"],
                         "ent_0001")
        (rel,) = red["relationships"]
        self.assertEqual(rel["from_id"], "ent_0001")
        self.assertNotIn("to_id", rel)  # Xaden unannotated in fixture

    def test_builders_resolve_canonical_names(self):
        result = {
            "characters": [{"name": "Violet", "role": "protagonist",
                            "description": "", "appearance": "",
                            "status": "alive", "aliases": [],
                            "first_appearance_chapter": 1,
                            "confidence": 0.9,
                            "entity_id": "ent_0001"}],
            "relationships": [
                {"from": "Xaden Riorson", "to": "Violet",
                 "type": "romantic", "evidence": "q",
                 "to_id": "ent_0001"}],
            "roster_persistence": {
                "canonical_names": {"ent_0001": "Violet Sorrengail"}},
        }
        rel_map, _ = pp._build_rel_map(result)
        self.assertEqual(rel_map["xaden riorson"][0]["to"],
                         "Violet Sorrengail")
        rows = pp._build_character_rows("wid", result, rel_map, set())
        self.assertEqual(rows[0]["name"], "Violet Sorrengail")

    def test_builders_flag_off_unchanged(self):
        result = {
            "characters": [{"name": "Violet", "role": "protagonist",
                            "description": "", "appearance": "",
                            "status": "alive", "aliases": [],
                            "first_appearance_chapter": 1,
                            "confidence": 0.9}],
            "relationships": [
                {"from": "Xaden Riorson", "to": "Violet",
                 "type": "romantic", "evidence": "q"}],
        }
        rel_map, _ = pp._build_rel_map(result)
        self.assertEqual(rel_map["xaden riorson"][0]["to"], "Violet")
        rows = pp._build_character_rows("wid", result, rel_map, set())
        self.assertEqual(rows[0]["name"], "Violet")


class TestDedupeFold(unittest.TestCase):
    def test_dedupe_fold_tombstones_folded_id(self):
        with tempfile.TemporaryDirectory() as td:
            st = pp.RosterStore(learn_dir=td, work_key="wk")
            e1 = st.mint_entity()
            e2 = st.mint_entity()
            pre = [{"name": "Violet", "entity_id": e1},
                   {"name": "Violet Sorrengail", "entity_id": e2}]
            report = [{"canonical": "Violet Sorrengail",
                       "merged": ["Violet"]}]
            n = pp._roster_record_dedupe_folds(st, pre, report, 3)
            self.assertEqual(n, 1)
            self.assertEqual(st.data["retired"][e1]["merged_into"], e2)
            self.assertEqual(
                st.data["retired"][e1]["reason"], "within-run merge")


def _obs(form, run, appearances, suspect=False, detached=False):
    return {"run": run, "form": form, "form_key": pp.norm_name(form),
            "chapters": [1], "appearances": appearances,
            "role_hint": "", "aliases": [], "alias_keys": [],
            "desc_hash": "", "suspect": suspect, "detached": detached}


def _entity(observations, anchor=None, canonical_source="rule"):
    return {"canonical": None, "canonical_source": canonical_source,
            "status": "active", "split_from": None, "split_into": [],
            "anchor": anchor, "observations": observations,
            "links": []}


class TestCanonicalRule(unittest.TestCase):
    def test_appearance_weighting_clear_winner(self):
        ent = _entity([_obs("Violet", 1, 40),
                       _obs("Violet Sorrengail", 1, 10)])
        name, src = pp.compute_canonical_name(ent)
        self.assertEqual(name, "Violet")  # 10 < 80% of 40: no near-tie
        self.assertEqual(src, "rule")

    def test_near_tie_breaks_to_fullest_form(self):
        ent = _entity([_obs("Violet", 1, 40),
                       _obs("Violet Sorrengail", 1, 34)])
        name, _ = pp.compute_canonical_name(ent)
        self.assertEqual(name, "Violet Sorrengail")  # 34 >= 32: fullest

    def test_exact_tie_breaks_to_most_recent(self):
        ent = _entity([_obs("Geralt", 1, 20),
                       _obs("Geralt of Rivia", 1, 20),
                       _obs("Geralt", 2, 0)])
        name, _ = pp.compute_canonical_name(ent)
        # "Geralt of Rivia" is fullest AND tied — fullness wins first.
        self.assertEqual(name, "Geralt of Rivia")
        ent2 = _entity([_obs("Alys Rivers", 1, 20),
                        _obs("Alys Rivers", 2, 20)])
        self.assertEqual(pp.compute_canonical_name(ent2)[0],
                         "Alys Rivers")

    def test_suspect_observations_do_not_vote(self):
        ent = _entity([_obs("Violet", 1, 5),
                       _obs("Violetta Sorrengail", 2, 99, suspect=True)])
        name, _ = pp.compute_canonical_name(ent)
        self.assertEqual(name, "Violet")

    def test_detached_observations_do_not_vote(self):
        ent = _entity([_obs("Violet", 1, 5),
                       _obs("Violetta", 2, 99, detached=True)])
        self.assertEqual(pp.compute_canonical_name(ent)[0], "Violet")

    def test_anchor_name_is_rule_proof(self):
        anchor = {"kind": "confirmed", "name": "Violet Sorrengail",
                  "source": "db", "aliases": [], "seen_run": 1}
        ent = _entity([_obs("Violet", 1, 99)], anchor=anchor,
                      canonical_source="anchor")
        name, src = pp.compute_canonical_name(ent)
        self.assertEqual((name, src), ("Violet Sorrengail", "anchor"))

    def test_manual_alias_anchor_does_not_lock(self):
        # lock_canonical=False seeding leaves canonical_source unset,
        # so the rule still runs (the anchor name here would be a
        # normalized key string, never a display form).
        anchor = {"kind": "manual-alias", "name": "violet sorrengail",
                  "source": "--alias", "aliases": ["violet"],
                  "seen_run": 1}
        ent = _entity([_obs("Violet", 1, 30),
                       _obs("Violet Sorrengail", 1, 10)],
                      anchor=anchor, canonical_source="")
        name, src = pp.compute_canonical_name(ent)
        self.assertEqual((name, src), ("Violet", "rule"))

    def test_alias_override_picks_observed_target_form(self):
        ent = _entity([_obs("Violet", 1, 90),
                       _obs("Violet Sorrengail", 1, 5)])
        name, src = pp.compute_canonical_name(
            ent, alias_targets={"violet": "violet sorrengail"})
        self.assertEqual((name, src), ("Violet Sorrengail", "human"))

    def test_alias_override_unobserved_target_falls_through(self):
        ent = _entity([_obs("Violet", 1, 90)])
        name, src = pp.compute_canonical_name(
            ent, alias_targets={"violet": "violet sorrengail"})
        self.assertEqual((name, src), ("Violet", "rule"))


class TestCanonicalChangeEvents(_SeamCase):
    def test_canonical_change_is_an_event_not_a_silent_rename(self):
        st = self._store()
        roster1 = {"violet": _entry("Violet", appearances=90)}
        pp.roster_assign_entities(roster1, st, anchors=[])
        eid = roster1["violet"]["entity_id"]
        st2 = self._store()
        roster2 = {"violet sorrengail": _entry(
            "Violet Sorrengail", appearances=95)}
        marker = pp.roster_assign_entities(roster2, st2, anchors=[])
        changes = [c for c in marker["canonical_changes"]
                   if c["entity"] == eid]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["from"], "Violet")
        self.assertEqual(changes[0]["to"], "Violet Sorrengail")
        self.assertIn("reason", changes[0])


class TestRejectedAnchorSuppression(_SeamCase):
    _REJECTED = [{"name": "Violet Sorrengail", "kind": "rejected",
                  "source": "db", "aliases": [],
                  "lock_canonical": True}]

    def test_tier0_link_suppresses_at_row_build(self):
        st = self._store()
        roster = {"violet sorrengail": _entry("Violet Sorrengail")}
        marker = pp.roster_assign_entities(
            roster, st, anchors=list(self._REJECTED))
        eid = roster["violet sorrengail"]["entity_id"]
        self.assertEqual(marker["suppressed_entities"], [eid])
        self.assertEqual(len(marker["suppressed"]), 1)
        self.assertEqual(marker["suppressed"][0]["tier"], "anchor")
        result = {"characters": [{"name": "Violet Sorrengail",
                                  "entity_id": eid, "role": "major",
                                  "description": "", "appearance": "",
                                  "status": "alive", "aliases": [],
                                  "first_appearance_chapter": 1,
                                  "confidence": 0.9}],
                  "relationships": [],
                  "roster_persistence": marker}
        rows = pp._build_character_rows("wid", result, {}, set())
        self.assertEqual(rows, [])

    def test_weaker_tiers_never_suppress(self):
        # OC-3's tier condition, tested at the rule unit: only tier
        # 0 (anchor) and tier 1 (exact-form) links suppress. (A
        # tier-2 link to a DB-anchored entity is in fact unreachable
        # through the seam — tier 2 must claim the entity's
        # canonical, which for an anchored entity is the anchor
        # name, and claiming that name is itself a tier-0 claim —
        # so the carve-out is pinned here where the rule lives.)
        st = self._store()
        eid = st.seed_anchor("Violet Sorrengail", "rejected", "db", 1)
        for tier in ("alias-cross", "subsequence", "fold", "human"):
            marker = {"suppressed": [], "suppressed_entities": []}
            pp._note_roster_suppression(st, marker, eid, tier,
                                         "Violet")
            self.assertEqual(marker["suppressed"], [], tier)
            self.assertEqual(marker["suppressed_entities"], [], tier)
        for tier in ("anchor", "exact-form"):
            marker = {"suppressed": [], "suppressed_entities": []}
            pp._note_roster_suppression(st, marker, eid, tier,
                                         "Violet")
            self.assertEqual(len(marker["suppressed"]), 1, tier)
            self.assertEqual(marker["suppressed_entities"], [eid], tier)
        # A non-rejected anchor never suppresses at any tier.
        st2 = self._store()
        eid2 = st2.seed_anchor("Violet Sorrengail", "confirmed", "db", 1)
        marker = {"suppressed": [], "suppressed_entities": []}
        pp._note_roster_suppression(st2, marker, eid2, "anchor",
                                     "Violet Sorrengail")
        self.assertEqual(marker["suppressed"], [])


class TestAnchorFetch(_SeamCase):
    def setUp(self):
        super().setUp()
        self._old_sb = pp.sb
        self._old_learned = pp._LEARNED
        pp._LEARNED = pp.LearnedState(learn_dir=self.dir / "learn",
                                       enabled=True)
        self.addCleanup(self._restore)

    def _restore(self):
        pp.sb = self._old_sb
        pp._LEARNED = self._old_learned

    def _rows(self):
        return [
            {"id": "1", "name": "Confirmed Person", "status": "confirmed",
             "source_type": "ai", "aliases": ["CP"]},
            {"id": "2", "name": "Rejected Person", "status": "rejected",
             "source_type": "ai", "aliases": []},
            {"id": "3", "name": "Human Added", "status": "candidate",
             "source_type": "human", "aliases": []},
            {"id": "4", "name": "Processor Candidate",
             "status": "candidate", "source_type": "ai", "aliases": []},
        ]

    def test_preserved_rows_become_anchors_candidates_do_not(self):
        pp.sb = lambda table, **kw: self._rows()
        anchors = pp._roster_fetch_anchors("wid-1")
        by_name = {a["name"]: a for a in anchors}
        self.assertEqual(by_name["Confirmed Person"]["kind"],
                         "confirmed")
        self.assertEqual(by_name["Rejected Person"]["kind"], "rejected")
        self.assertEqual(by_name["Human Added"]["kind"], "contributor")
        self.assertNotIn("Processor Candidate", by_name)
        self.assertEqual(by_name["Confirmed Person"]["aliases"], ["CP"])

    def test_fetch_failure_returns_none(self):
        pp.sb = lambda table, **kw: None
        self.assertIsNone(pp._roster_fetch_anchors("wid-1"))

    def test_manual_alias_pairs_become_unlocked_anchors(self):
        pp.sb = lambda table, **kw: []
        pp._LEARNED.record_alias("Violet", "Violet Sorrengail")
        anchors = pp._roster_fetch_anchors("wid-1")
        manual = [a for a in anchors if a["kind"] == "manual-alias"]
        self.assertEqual(len(manual), 1)
        self.assertFalse(manual[0]["lock_canonical"])
        self.assertEqual(manual[0]["aliases"], ["violet"])


class TestNoOpSecondRunRows(_SeamCase):
    """OC-6: a no-op second run ships rows byte-identical to the
    flag-off rows — identity resolution changes names only when it
    legitimately resolves a DIFFERENT form to the stable name."""

    def _roster(self):
        return {"geralt of rivia": _entry(
                    "Geralt of Rivia", appearances=40,
                    descriptions=["A witcher."],
                    evidence="Geralt of Rivia drew his sword."),
                "yennefer": _entry(
                    "Yennefer", appearances=25,
                    descriptions=["A sorceress."],
                    evidence="Yennefer cast a spell.")}

    def _rows(self, roster, marker):
        red = pp.v2_reduce({}, {}, roster, [], preview=True,
                           show_matrices=False, n_chapters_total=3)
        result = {"characters": red["characters"],
                  "relationships": red["relationships"]}
        if marker is not None:
            result["roster_persistence"] = marker
        rel_map, _ = pp._build_rel_map(result)
        return pp._build_character_rows("wid", result, rel_map, set())

    def test_noop_second_run_rows_match_flag_off(self):
        st = self._store()
        pp.roster_assign_entities(self._roster(), st, anchors=[])
        st2 = self._store()
        roster2 = self._roster()
        marker2 = pp.roster_assign_entities(roster2, st2, anchors=[])
        rows_on = self._rows(roster2, marker2)
        rows_off = self._rows(self._roster(), None)
        self.assertEqual(rows_on, rows_off)


if __name__ == "__main__":
    unittest.main()
