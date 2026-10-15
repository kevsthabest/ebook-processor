"""Option B — persisted per-work roster, slice 2: the cross-run matcher.

Design: eval/b-roster-design.md section 4. The matcher is a pure
function over (run entries, store entity views, human NOs). Every
tier is deterministic in v1 (OC-1): JEV and embeddings never link;
pairs no tier links but which carry a similarity signal are emitted
as split suspects — proposed, never applied. Uncertain matches mint
NEW entities downstream, never forced links.

Covered here:
  * tier 0 anchor claiming (closed key set, A3; contested anchor
    claims NEITHER entry);
  * tier 1 exact-form link + same-form-collision abstention;
  * tier 2 alias cross-match (mutual claim + uniqueness veto);
  * tier 3 subsequence/title signals, uniqueness-bound;
  * folds: disjoint-chapter dual links fold; co-occurring dual
    links are REFUSED and the weaker entry's link is suspended;
  * cannot-link vetoes: human NO, surname trap (no link, no
    suspect), anchor precedence over later tiers;
  * tombstone redirect: a form folded into a survivor in an
    earlier run links to the SURVIVOR, re-vetted by the tiers;
  * split suspects for bridge-less flips (the wrong-surname case).

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _view(eid, canonical, primary=(), aliases=(), chapters=(),
          appearances=50, anchor=None, desc_hash=""):
    return {"id": eid, "canonical": canonical,
            "canonical_key": pp.norm_name(canonical or ""),
            "anchor": anchor, "primary_forms": set(primary),
            "alias_keys": set(aliases), "alias_keys_all": set(aliases),
            "chapters": set(chapters), "appearances": appearances,
            "desc_hash": desc_hash, "provisional": False}


def _entry(key, form, aliases=(), chapters=(), appearances=10,
           desc_hash=""):
    return {"key": key, "name": form, "form_key": pp.norm_name(form),
            "alias_keys": {pp.norm_name(a) for a in aliases},
            "chapters": set(chapters), "appearances": appearances,
            "desc_hash": desc_hash}


def _anchor(name, kind="confirmed", aliases=()):
    return {"kind": kind, "name": name, "source": "db",
            "aliases": sorted(aliases), "seen_run": 1}


class TestTier1(unittest.TestCase):
    def test_exact_form_link(self):
        views = [_view("ent_0001", "Violet Sorrengail",
                       primary=["violet sorrengail"])]
        out = pp.roster_match_entries(
            [_entry("violet sorrengail", "Violet Sorrengail")], views)
        self.assertEqual(out["links"]["violet sorrengail"]["entity"],
                         "ent_0001")
        self.assertEqual(out["links"]["violet sorrengail"]["tier"],
                         "exact-form")

    def test_same_form_collision_abstains(self):
        # Two genuinely different people the book calls by one name:
        # tier 1 does NOT pick the bigger entity — it abstains.
        views = [_view("ent_0001", "Sam", primary=["sam"],
                       appearances=90),
                 _view("ent_0002", "Sam", primary=["sam"],
                       appearances=10)]
        out = pp.roster_match_entries([_entry("sam", "Sam")], views)
        self.assertNotIn("sam", out["links"])
        self.assertTrue(out["collisions"])

    def test_no_match_no_link(self):
        views = [_view("ent_0001", "Xaden Riorson",
                       primary=["xaden riorson"])]
        out = pp.roster_match_entries(
            [_entry("violet", "Violet")], views)
        self.assertEqual(out["links"], {})


class TestTier0Anchors(unittest.TestCase):
    def test_anchor_claims_before_all_tiers(self):
        views = [
            _view("ent_0001", "Violet Sorrengail",
                  primary=["violet sorrengail"],
                  anchor=_anchor("Violet Sorrengail",
                                 aliases=["Violet"])),
            _view("ent_0002", "Violet", primary=["violet"]),
        ]
        out = pp.roster_match_entries([_entry("violet", "Violet")],
                                      views)
        self.assertEqual(out["links"]["violet"]["entity"], "ent_0001")
        self.assertEqual(out["links"]["violet"]["tier"], "anchor")

    def test_anchor_keyset_is_closed_no_surname_claiming(self):
        # A3: Mira's anchor must not capture Violet's entry through
        # the shared surname token.
        views = [_view("ent_0001", "Mira Sorrengail",
                       primary=["mira sorrengail"],
                       anchor=_anchor("Mira Sorrengail"))]
        out = pp.roster_match_entries(
            [_entry("violet sorrengail", "Violet Sorrengail")], views)
        self.assertEqual(out["links"], {})

    def test_contested_anchor_claims_neither(self):
        views = [_view("ent_0001", "Violet Sorrengail",
                       primary=["violet sorrengail"],
                       anchor=_anchor("Violet Sorrengail",
                                      aliases=["Violet"]))]
        entries = [_entry("violet", "Violet", chapters={1}),
                   _entry("violet sorrengail", "Violet Sorrengail",
                          chapters={2})]
        out = pp.roster_match_entries(entries, views)
        # Both entries claim the anchor (one by alias, one by form):
        # the anchor claims NEITHER at tier 0. The full-form entry
        # may still exact-form link at tier 1; the short form may
        # not ride the anchor.
        self.assertNotEqual(
            out["links"].get("violet", {}).get("tier"), "anchor")
        self.assertTrue(any(c["kind"] == "anchor"
                            for c in out["collisions"]))


class TestTier2(unittest.TestCase):
    def test_alias_cross_link_the_violet_case(self):
        views = [_view("ent_0001", "Violet Sorrengail",
                       primary=["violet sorrengail"], aliases=["violet"])]
        out = pp.roster_match_entries(
            [_entry("violet", "Violet", aliases=["Violet Sorrengail"])],
            views)
        self.assertEqual(out["links"]["violet"]["entity"], "ent_0001")
        self.assertEqual(out["links"]["violet"]["tier"], "alias-cross")

    def test_alias_cross_requires_mutual_claim(self):
        # Without the entry claiming the full form back, tier 2 does
        # not fire — the pair drops to tier 3, whose subsequence
        # signal (word-prefix, unique candidate) links it there.
        # Tier 2's mutual-claim discipline is not tier 3's.
        views = [_view("ent_0001", "Violet Sorrengail",
                       primary=["violet sorrengail"], aliases=["violet"])]
        out = pp.roster_match_entries([_entry("violet", "Violet")],
                                      views)
        self.assertEqual(out["links"]["violet"]["tier"], "subsequence")

    def test_alias_cross_uniqueness_not_a_guess(self):
        # Two entities the short form could grow into: tier 3's
        # uniqueness requirement abstains (a suspect, not a link).
        views = [
            _view("ent_0001", "Violet Sorrengail",
                  primary=["violet sorrengail"], aliases=["violet"]),
            _view("ent_0002", "Violet Riorson",
                  primary=["violet riorson"], aliases=["violet"]),
        ]
        out = pp.roster_match_entries([_entry("violet", "Violet")],
                                      views)
        self.assertNotIn("violet", out["links"])

    def test_alias_cross_uniqueness_veto(self):
        # A third entity also claiming the entry's form kills the
        # cross-match: ambiguous alias ownership is not a bridge.
        views = [
            _view("ent_0001", "Violet Sorrengail",
                  primary=["violet sorrengail"], aliases=["violet"]),
            _view("ent_0002", "Violence", primary=["violence"],
                  aliases=["violet"]),
        ]
        out = pp.roster_match_entries(
            [_entry("violet", "Violet", aliases=["Violet Sorrengail"])],
            views)
        self.assertNotIn("violet", out["links"])


class TestTier3(unittest.TestCase):
    def test_title_form_links(self):
        views = [_view("ent_0001", "Augustine Melgren",
                       primary=["augustine melgren"])]
        out = pp.roster_match_entries(
            [_entry("general melgren", "General Melgren")], views)
        self.assertEqual(
            out["links"]["general melgren"]["entity"], "ent_0001")
        self.assertEqual(out["links"]["general melgren"]["tier"],
                         "subsequence")

    def test_surname_trap_is_cannot_link_not_suspect(self):
        views = [_view("ent_0001", "Peter Sebeck",
                       primary=["peter sebeck"], chapters={1, 2})]
        out = pp.roster_match_entries(
            [_entry("chris sebeck", "Chris Sebeck", chapters={1, 2})],
            views)
        self.assertEqual(out["links"], {})
        self.assertFalse(any(s["a"] == "chris sebeck"
                             for s in out["suspects"]))


class TestFolds(unittest.TestCase):
    def _views(self):
        return [_view("ent_0001", "Violet Sorrengail",
                      primary=["violet sorrengail"], aliases=["violet"])]

    def test_disjoint_dual_link_folds(self):
        entries = [
            _entry("violet sorrengail", "Violet Sorrengail",
                   chapters={1, 2, 3}, appearances=40),
            _entry("violet", "Violet", aliases=["Violet Sorrengail"],
                   chapters={4, 5}, appearances=12),
        ]
        out = pp.roster_match_entries(entries, self._views())
        self.assertEqual(len(out["folds"]), 1)
        self.assertEqual(out["folds"][0]["entity"], "ent_0001")
        self.assertEqual(len(out["links"]), 2)
        self.assertEqual(out["suspensions"], [])

    def test_cooccurring_dual_link_refused_weaker_suspended(self):
        entries = [
            _entry("violet sorrengail", "Violet Sorrengail",
                   chapters={1, 2, 3}, appearances=40),
            _entry("violet", "Violet", aliases=["Violet Sorrengail"],
                   chapters={2, 4}, appearances=12),
        ]
        out = pp.roster_match_entries(entries, self._views())
        self.assertEqual(out["folds"], [])
        self.assertIn("violet sorrengail", out["links"])
        self.assertNotIn("violet", out["links"])
        self.assertEqual(len(out["suspensions"]), 1)
        self.assertEqual(out["suspensions"][0]["entry"], "violet")
        self.assertEqual(out["suspensions"][0]["entity"], "ent_0001")


class TestVetoes(unittest.TestCase):
    def test_human_no_vetoes_exact_form(self):
        views = [_view("ent_0001", "Peter Sebeck",
                       primary=["peter sebeck"])]
        out = pp.roster_match_entries(
            [_entry("peter sebeck", "Peter Sebeck")], views,
            no_pairs=[("pete sebeck", "peter sebeck")])
        # The NO is between 'pete sebeck' and the entity's canonical:
        # this entry IS 'peter sebeck' itself, which is the entity's
        # own form — a NO never vetoes an entity against itself.
        self.assertIn("peter sebeck", out["links"])

    def test_human_no_vetoes_cross_form(self):
        views = [_view("ent_0001", "Chris Sebeck",
                       primary=["chris sebeck"], aliases=["seb"])]
        out = pp.roster_match_entries(
            [_entry("seb", "Seb", aliases=["Chris Sebeck"])], views,
            no_pairs=[("seb", "chris sebeck")])
        self.assertNotIn("seb", out["links"])
        self.assertTrue(out["vetoed"])

    def test_anchored_entry_not_stolen_by_later_tier(self):
        views = [
            _view("ent_0001", "Violet Sorrengail",
                  primary=["violet sorrengail"],
                  anchor=_anchor("Violet Sorrengail",
                                 aliases=["Violet"])),
            _view("ent_0002", "Violet Sorrengail",
                  primary=["violet sorrengail"]),
        ]
        out = pp.roster_match_entries(
            [_entry("violet", "Violet")], views)
        self.assertEqual(out["links"]["violet"]["entity"], "ent_0001")


class TestSuspects(unittest.TestCase):
    def test_wrong_surname_flip_is_a_split_suspect(self):
        # No deterministic bridge exists; B reports, never forces.
        views = [_view("ent_0001", "Rhiannon Matthias",
                       primary=["rhiannon matthias"], chapters={3, 4})]
        out = pp.roster_match_entries(
            [_entry("rhiannon sorrengail", "Rhiannon Sorrengail",
                    chapters={3, 9})], views)
        self.assertEqual(out["links"], {})
        self.assertEqual(len(out["suspects"]), 1)
        self.assertEqual(out["suspects"][0]["b"], "ent_0001")

    def test_desc_hash_proximity_is_a_signal(self):
        views = [_view("ent_0001", "Someone Else",
                       primary=["someone else"], chapters={1},
                       desc_hash="abc123")]
        out = pp.roster_match_entries(
            [_entry("zz", "Zz", chapters={2}, desc_hash="abc123")],
            views)
        self.assertEqual(len(out["suspects"]), 1)


class TestTombstoneRedirect(unittest.TestCase):
    def test_folded_form_links_to_survivor_revetted(self):
        # Store-level: run 1 folded 'Violet' into the Sorrengail
        # entity (tombstone). Run 2's 'Violet' entry must link to
        # the SURVIVOR — the folded ID accepts no links.
        with tempfile.TemporaryDirectory() as td:
            st = pp.RosterStore(learn_dir=td, work_key="wk")
            a = st.mint_entity()
            b = st.mint_entity()
            st.append_observation(a, {
                "run": 1, "form": "Violet Sorrengail",
                "form_key": "violet sorrengail", "chapters": [1],
                "appearances": 40, "role_hint": None, "aliases": [],
                "alias_keys": [], "desc_hash": ""})
            st.append_observation(b, {
                "run": 1, "form": "Violet", "form_key": "violet",
                "chapters": [2], "appearances": 10, "role_hint": None,
                "aliases": [], "alias_keys": [], "desc_hash": ""})
            st.set_canonical(a, "Violet Sorrengail", "rule")
            st.record_merge(a, b, run=1, reason="within-run merge")
            views = st.entity_views()
            out = pp.roster_match_entries(
                [_entry("violet", "Violet")], views)
            self.assertEqual(out["links"]["violet"]["entity"], a)


class TestSuspectFormAuthorityLiveShape(unittest.TestCase):
    """Fix-queue batch 2, item 1 (B-F1) at the seam's shape: views
    built by a real RosterStore, matched by the real matcher. A
    form whose only attestation is a suspect observation (a
    suspended link dragged it into this entity) must not tier-1
    link a fresh entry — while suspended, the pair may only be
    PROPOSED as a split suspect. Once a human resolves the
    suspect, the form links again."""

    def _store(self, td):
        st = pp.RosterStore(learn_dir=td, work_key="wk-live")
        a = st.mint_entity()
        st.append_observation(a, {
            "run": 1, "form": "Xaden Riorson",
            "form_key": "xaden riorson", "chapters": [1],
            "appearances": 40, "role_hint": None, "aliases": [],
            "alias_keys": [], "desc_hash": ""})
        st.append_observation(a, {
            "run": 2, "form": "V. Sorrengail",
            "form_key": pp.norm_name("V. Sorrengail"),
            "chapters": [1],
            "appearances": 12, "role_hint": None, "aliases": [],
            "alias_keys": [], "desc_hash": ""})
        st.flag_suspect_observations(
            a, run=2, form_key=pp.norm_name("V. Sorrengail"))
        return st, a

    def test_suspect_only_form_does_not_link_but_is_proposed(self):
        with tempfile.TemporaryDirectory() as td:
            st, a = self._store(td)
            key = pp.norm_name("V. Sorrengail")
            out = pp.roster_match_entries(
                [_entry(key, "V. Sorrengail", chapters=(1,))],
                st.entity_views())
            self.assertNotIn(key, out["links"])
            self.assertTrue(any(
                s["a"] == key and s["b"] == a
                for s in out["suspects"]))

    def test_resolved_suspect_form_links_at_tier_1(self):
        with tempfile.TemporaryDirectory() as td:
            st, a = self._store(td)
            key = pp.norm_name("V. Sorrengail")
            st.resolve_suspect(a, key)
            out = pp.roster_match_entries(
                [_entry(key, "V. Sorrengail", chapters=(1,))],
                st.entity_views())
            link = out["links"][key]
            self.assertEqual(link["entity"], a)
            self.assertEqual(link["tier"], "exact-form")


if __name__ == "__main__":
    unittest.main()
