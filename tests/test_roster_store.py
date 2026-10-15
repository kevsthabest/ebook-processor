"""Option B — persisted per-work roster, slice 1: RosterStore.

Design: eval/b-roster-design.md (user-approved open choices as amended
by the Security Advisor red-team, A1-A9). Identity is a hypothesis
with provenance, never a fact the store asserts: every union the
store records is attributed to a tier and a run, reversible, and
refused outright when it contradicts a recorded human NO.

Covered here (slice 1):
  * per-work file persistence, F8 pattern (atomic save, loud
    corruption, .corrupt preservation), RLock discipline;
  * entity minting (opaque ent_<seq>, never reused, never derived
    from a name);
  * observation append + run-fingerprint idempotency (a repeated
    extraction refreshes in place, never double-counts);
  * merge tombstones (recorded ONCE in retired, with redirect;
    observations preserved under the survivor);
  * split into per-form successors (original accounts as split);
  * suspension as a LINK state with its own ledger (outside the
    ID partition);
  * the store-level human-NO invariant: matcher links, folds,
    merge tombstones and adoptions that would re-form a vetoed
    union are refused by the store itself;
  * accounting that closes: minted = active + merged + split,
    UNACCOUNTED: 0, incl. randomized operation sequences.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import random
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

WK = "title:test book|some author"


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*args, **kwargs)
    return out, buf.getvalue()


def _obs(run, form, chapters=(1,), appearances=5, aliases=(),
         desc_hash=""):
    return {"run": run, "form": form, "form_key": pp.norm_name(form),
            "chapters": sorted(chapters), "appearances": appearances,
            "role_hint": None,
            "aliases": sorted(aliases),
            "alias_keys": sorted(pp.norm_name(a) for a in aliases),
            "desc_hash": desc_hash}


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _store(self, work_key=WK):
        return pp.RosterStore(learn_dir=self.dir, work_key=work_key)


class TestMintAndPersist(_StoreCase):
    def test_ids_are_opaque_sequential_and_never_reused(self):
        st = self._store()
        a = st.mint_entity()
        b = st.mint_entity()
        self.assertEqual((a, b), ("ent_0001", "ent_0002"))
        st.append_observation(a, _obs(1, "Violet Sorrengail"))
        st.append_observation(b, _obs(1, "Xaden Riorson"))
        st.record_merge(a, b, run=1, reason="within-run merge")
        c = st.mint_entity()
        self.assertEqual(c, "ent_0003")  # ent_0002 is never recycled

    def test_round_trip_through_disk(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet", aliases=["Violet Sorrengail"]))
        st.set_canonical(a, "Violet Sorrengail", "rule")
        self.assertTrue(st.save())
        st2 = self._store()
        self.assertFalse(st2.corrupt_at_load)
        e = st2.get_entity(a)
        self.assertEqual(e["canonical"], "Violet Sorrengail")
        self.assertEqual(len(e["observations"]), 1)
        self.assertEqual(e["observations"][0]["alias_keys"],
                         ["violet sorrengail"])

    def test_one_file_per_work(self):
        st1 = self._store("wk-one")
        st1.mint_entity()
        st1.save()
        st2 = self._store("wk-two")
        st2.mint_entity()
        st2.save()
        files = sorted(p.name for p in (self.dir / "rosters").glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertEqual(self._store("wk-one").data["work_key"], "wk-one")

    def test_missing_store_is_empty_not_corrupt(self):
        st = self._store()
        self.assertFalse(st.corrupt_at_load)
        self.assertEqual(st.accounting()["minted"], 0)


class TestCorruption(_StoreCase):
    def _write_garbage(self):
        st = self._store()
        path = st.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        return path

    def test_corrupt_store_degrades_to_stateless_and_warns(self):
        self._write_garbage()
        st, out = _capture(self._store)
        self.assertTrue(st.corrupt_at_load)
        self.assertIn("WARNING", out)
        self.assertEqual(st.accounting()["minted"], 0)

    def test_corrupt_bytes_preserved_before_overwrite(self):
        path = self._write_garbage()
        st, _ = _capture(self._store)
        st.mint_entity()
        st.save()
        backups = list(path.parent.glob(path.name + ".corrupt*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")
        # And the new file is valid.
        json.loads(path.read_text(encoding="utf-8"))

    def test_wrong_shape_is_corrupt(self):
        st = self._store()
        st.path.parent.mkdir(parents=True, exist_ok=True)
        st.path.write_text(json.dumps({"entities": []}), encoding="utf-8")
        st2, _ = _capture(self._store)
        self.assertTrue(st2.corrupt_at_load)


class TestRunFingerprint(_StoreCase):
    def test_same_fingerprint_refreshes_in_place(self):
        st = self._store()
        ordinal, repeat = st.claim_run("fp-aaa")
        self.assertEqual((ordinal, repeat), (1, False))
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet"))
        st.record_link(a, {"run": 1, "tier": "exact-form",
                           "state": "active", "evidence": "x",
                           "form_key": "violet"},
                       entry_forms={"violet"})
        # The same extraction completing again (resume at the seam
        # boundary): same fingerprint -> same ordinal, no advance,
        # and the first completion's records STAND (the seam's
        # repeat path refreshes observations via replace_observation
        # instead of re-matching — no double count, no re-mint).
        ordinal2, repeat2 = st.claim_run("fp-aaa")
        self.assertEqual((ordinal2, repeat2), (1, True))
        self.assertEqual(st.runs_completed, 1)
        self.assertEqual(st.run_assignments(1), {"violet": a})
        st.replace_observation(a, _obs(1, "Violet", appearances=7))
        obs = st.get_entity(a)["observations"]
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["appearances"], 7)
        self.assertEqual(len(st.get_entity(a)["links"]), 1)

    def test_new_fingerprint_mints_new_ordinal(self):
        st = self._store()
        self.assertEqual(st.claim_run("fp-1"), (1, False))
        self.assertEqual(st.claim_run("fp-2"), (2, False))
        self.assertEqual(st.runs_completed, 2)


class TestMergeTombstone(_StoreCase):
    def test_merge_records_once_in_retired_and_preserves_observations(self):
        st = self._store()
        a = st.mint_entity()
        b = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet Sorrengail"))
        st.append_observation(b, _obs(1, "Violet"))
        st.record_merge(a, b, run=1, reason="within-run merge")
        self.assertIsNone(st.get_entity(b))
        self.assertEqual(st.data["retired"][b]["merged_into"], a)
        self.assertEqual(st.data["retired"][b]["reason"],
                         "within-run merge")
        forms = [o["form"] for o in st.get_entity(a)["observations"]]
        self.assertEqual(forms, ["Violet Sorrengail", "Violet"])
        self.assertEqual(st.resolve_id(b), a)

    def test_resolve_id_follows_chains_and_unknown(self):
        st = self._store()
        a, b, c = st.mint_entity(), st.mint_entity(), st.mint_entity()
        st.record_merge(b, a, run=1, reason="within-run merge")
        st.record_merge(c, b, run=1, reason="within-run merge")
        self.assertEqual(st.resolve_id(a), c)
        self.assertIsNone(st.resolve_id("ent_9999"))


class TestSuspension(_StoreCase):
    def test_suspension_is_a_link_state_with_its_own_ledger(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet"))
        st.record_link(a, {"run": 1, "tier": "alias-cross",
                           "state": "active", "evidence": "mutual claim",
                           "form_key": "violet"},
                       entry_forms={"violet"})
        n = st.suspend_link(a, run=1, form_key="violet",
                            evidence="co-occurs with Violet Sorrengail")
        self.assertEqual(n, 1)
        ledger = st.suspension_ledger()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["entity"], a)
        self.assertEqual(ledger[0]["link"]["state"], "suspended")
        # Suspension is NOT an entity status: the partition is
        # untouched and the accounting still closes.
        acc = st.accounting()
        self.assertEqual(acc["active"], 1)
        self.assertEqual(acc["unaccounted"], 0)
        # The observation the suspended link carried is suspect (A1).
        self.assertTrue(st.get_entity(a)["observations"][0]["suspect"])


class TestSplit(_StoreCase):
    def test_split_partitions_observations_by_form(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Dad", appearances=9))
        st.append_observation(a, _obs(1, "Brennan Sorrengail",
                                      appearances=30))
        st.append_observation(a, _obs(2, "Dad", appearances=11))
        out = st.record_split(a, ["dad", "brennan sorrengail"], run=2)
        self.assertEqual(set(out), {"dad", "brennan sorrengail"})
        orig = st.get_entity(a)
        self.assertEqual(orig["status"], "split")
        self.assertEqual(sorted(orig["split_into"]),
                         sorted(out.values()))
        dad = st.get_entity(out["dad"])
        self.assertEqual([o["appearances"] for o in dad["observations"]],
                         [9, 11])
        self.assertEqual(dad["split_from"], a)
        acc = st.accounting()
        self.assertEqual(acc["split"], 1)
        self.assertEqual(acc["active"], 2)
        self.assertEqual(acc["unaccounted"], 0)

    def test_partial_split_keeps_original_active(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Dad"))
        st.append_observation(a, _obs(1, "Brennan Sorrengail"))
        out = st.record_split(a, ["dad"], run=1)
        orig = st.get_entity(a)
        self.assertEqual(orig["status"], "active")
        self.assertEqual([o["form"] for o in orig["observations"]],
                         ["Brennan Sorrengail"])
        self.assertEqual(st.get_entity(out["dad"])["split_from"], a)
        self.assertEqual(st.accounting()["unaccounted"], 0)


class TestSplitAnchor(_StoreCase):
    """Fix-queue batch 2, item 4: an anchor follows its sightings
    across a split. record_split used to leave the anchor on the
    emptied original — invisible to entity_views (tier 0 starved)
    yet still listed by anchored_entities, so A8 kept
    disengaging on a phantom, and seed_anchor's existing-anchor
    branch shadowed re-attachment to the twin."""

    def test_anchor_follows_its_name_form_to_the_successor(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet Sorrengail",
                                      appearances=30))
        st.append_observation(a, _obs(1, "Violet", appearances=10))
        self.assertEqual(
            st.seed_anchor("Violet Sorrengail", "confirmed", "db",
                           run=1), a)
        out = st.record_split(a, ["violet sorrengail"], run=2)
        twin = out["violet sorrengail"]
        self.assertEqual(st.get_entity(twin)["anchor"]["name"],
                         "Violet Sorrengail")
        self.assertEqual(st.get_entity(twin)["canonical"],
                         "Violet Sorrengail")
        self.assertEqual(st.get_entity(twin)["canonical_source"],
                         "anchor")
        self.assertIsNone(st.get_entity(a)["anchor"])
        self.assertEqual(set(st.anchored_entities()), {twin})
        # Tier 0 can claim the twin: its view carries the anchor.
        views = {v["id"]: v for v in st.entity_views()}
        self.assertIsNotNone(views[twin]["anchor"])
        # The original keeps the remainder under a rule canonical.
        self.assertEqual(st.get_entity(a)["status"], "active")
        self.assertEqual(st.get_entity(a)["canonical"], "Violet")
        self.assertEqual(st.accounting()["unaccounted"], 0)

    def test_anchor_moves_when_split_empties_the_original(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet Sorrengail"))
        st.append_observation(a, _obs(1, "Violet"))
        st.seed_anchor("Violet Sorrengail", "confirmed", "db", run=1)
        out = st.record_split(a, ["violet", "violet sorrengail"],
                              run=2)
        self.assertEqual(st.get_entity(a)["status"], "split")
        self.assertIsNone(st.get_entity(a)["anchor"])
        twin = out["violet sorrengail"]
        self.assertEqual(st.get_entity(twin)["anchor"]["name"],
                         "Violet Sorrengail")
        self.assertEqual(set(st.anchored_entities()), {twin})

    def test_anchor_fallback_when_its_form_was_never_observed(self):
        # A branch-3 seed (anchor minted its entity) whose form
        # never appears among the observations: when a split
        # empties the original, the anchor follows the largest
        # share of the sightings rather than stranding.
        st = self._store()
        a = st.seed_anchor("Violet Sorrengail", "confirmed", "db", 1)
        st.append_observation(a, _obs(1, "Dad", appearances=9))
        st.append_observation(a, _obs(1, "Brennan Sorrengail",
                                      appearances=30))
        out = st.record_split(a, ["dad", "brennan sorrengail"],
                              run=2)
        self.assertEqual(st.get_entity(a)["status"], "split")
        self.assertIsNone(st.get_entity(a)["anchor"])
        self.assertEqual(
            st.get_entity(out["brennan sorrengail"])["anchor"]["name"],
            "Violet Sorrengail")

    def test_anchored_entities_never_lists_split_entities(self):
        # Legacy stores can hold an anchor stranded on a split
        # entity (the pre-fix record_split shape); the A8
        # disengagement set must not contain the phantom.
        st = self._store()
        a = st.seed_anchor("Violet Sorrengail", "confirmed", "db", 1)
        st.data["entities"][a]["status"] = "split"
        self.assertEqual(st.anchored_entities(), {})

    def test_seed_anchor_reattaches_to_the_active_twin(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet Sorrengail"))
        st.seed_anchor("Violet Sorrengail", "confirmed", "db", run=1)
        # Legacy stranding: the sightings moved to a successor
        # but the anchor stayed behind on the emptied original.
        twin = st.mint_entity()
        st.get_entity(twin)["observations"] = \
            st.get_entity(a)["observations"]
        st.get_entity(a)["observations"] = []
        st.get_entity(a)["status"] = "split"
        got = st.seed_anchor("Violet Sorrengail", "confirmed", "db",
                             run=2)
        self.assertEqual(got, twin)
        self.assertEqual(st.get_entity(twin)["anchor"]["name"],
                         "Violet Sorrengail")


class TestHumanNoInvariant(_StoreCase):
    def _two_entities(self, st):
        a = st.mint_entity()
        b = st.mint_entity()
        st.append_observation(a, _obs(1, "Pete Sebeck"))
        st.append_observation(b, _obs(1, "Chris Sebeck"))
        return a, b

    def test_recorded_no_refuses_merge(self):
        st = self._store()
        a, b = self._two_entities(st)
        st.record_no("pete sebeck", "chris sebeck", source="test")
        with self.assertRaises(pp.RosterVetoError):
            st.record_merge(a, b, run=1, reason="within-run merge")
        # Nothing was recorded: both entities survive.
        self.assertIsNotNone(st.get_entity(b))
        self.assertEqual(st.data["retired"], {})

    def test_recorded_no_refuses_link(self):
        st = self._store()
        a, b = self._two_entities(st)
        st.record_no("pete sebeck", "chris sebeck", source="test")
        with self.assertRaises(pp.RosterVetoError):
            st.record_link(b, {"run": 2, "tier": "exact-form",
                               "state": "active", "evidence": "x",
                               "form_key": "pete sebeck"},
                           entry_forms={"pete sebeck"})

    def test_union_veto_accessor(self):
        st = self._store()
        st.record_no("a form", "b form", source="test")
        self.assertIsNotNone(st.union_veto({"a form"}, {"b form"}))
        self.assertIsNotNone(st.union_veto({"b form"}, {"a form"}))
        self.assertIsNone(st.union_veto({"a form"}, {"c form"}))
        self.assertIsNone(st.union_veto({"a form"}, {"a form"}))

    def test_no_pairs_survive_round_trip(self):
        st = self._store()
        st.record_no("a form", "b form", source="--roster-unlink")
        st.save()
        st2 = self._store()
        self.assertTrue(st2.has_no("a form", "b form"))


class TestAdoption(_StoreCase):
    def test_adoption_remints_and_rewrites_redirects(self):
        src = self._store("title:book|author")
        x = src.mint_entity()
        y = src.mint_entity()
        src.append_observation(x, _obs(1, "Violet"))
        src.append_observation(y, _obs(1, "Violet Sorrengail"))
        src.record_merge(x, y, run=1, reason="within-run merge")
        src.record_no("violet", "someone else", source="test")
        dst = self._store("work-id-123")
        z = dst.mint_entity()
        dst.append_observation(z, _obs(1, "Xaden Riorson"))
        moved = dst.adopt_store(src)
        self.assertEqual(moved, 2)
        acc = dst.accounting()
        self.assertEqual(acc["minted"], 3)
        self.assertEqual(acc["merged"], 1)
        self.assertEqual(acc["unaccounted"], 0)
        # Redirects point at reminted ids that exist in dst.
        retired = dst.data["retired"]
        self.assertEqual(len(retired), 1)
        (old_id, rec), = retired.items()
        self.assertIn(rec["merged_into"], dst.data["entities"])
        self.assertTrue(dst.has_no("violet", "someone else"))
        self.assertEqual(len(dst.data["adoptions"]), 1)

    def test_adoption_carries_source_fingerprint(self):
        # Fix-queue batch 2, item 3: the rename path moves the
        # whole record (fingerprint included); the merge path must
        # not drop last_run_fingerprint, or re-completing the
        # source extraction against the adopted store fails the
        # repeat guard and double-counts its sightings.
        src = self._store("title:book|author")
        self.assertEqual(src.claim_run("fp-src"), (1, False))
        x = src.mint_entity()
        src.append_observation(x, _obs(1, "Violet"))
        dst = self._store("work-id-123")
        dst.adopt_store(src)
        self.assertEqual(dst.data["last_run_fingerprint"], "fp-src")
        self.assertEqual(dst.claim_run("fp-src"), (1, True))

    def test_adoption_keeps_fingerprint_of_store_with_more_runs(self):
        # The combined store's runs_completed is the max of the
        # two; the fingerprint must name the SAME store's last
        # completion, or the repeat path's run_assignments(ordinal)
        # would look under the wrong extraction's ordinal.
        dst = self._store("work-id-9")
        dst.claim_run("fp-dst-1")
        dst.claim_run("fp-dst-2")
        src = self._store("title:book|author")
        src.claim_run("fp-src")
        dst.adopt_store(src)
        self.assertEqual(dst.data["last_run_fingerprint"], "fp-dst-2")
        self.assertEqual(dst.runs_completed, 2)
        self.assertEqual(dst.claim_run("fp-dst-2"), (2, True))

    def test_adoption_tie_prefers_source_fingerprint(self):
        # Equal run counts: the source's last extraction is the
        # one that may complete again against the combined store
        # (adoption happens because a run resolved the work
        # mid-flight), so its fingerprint wins the tie.
        dst = self._store("work-id-9")
        dst.claim_run("fp-dst")
        src = self._store("title:book|author")
        src.claim_run("fp-src")
        dst.adopt_store(src)
        self.assertEqual(dst.data["last_run_fingerprint"], "fp-src")
        self.assertEqual(dst.claim_run("fp-src"), (1, True))

    def test_adoption_then_recompletion_does_not_double_count(self):
        # Combined adoption x fingerprint: the source extraction
        # completes again against the adopted store and is
        # recognised as a repeat — the seam's refresh path then
        # replaces its observation in place under the REMINTED id.
        src = self._store("title:book|author")
        self.assertEqual(src.claim_run("fp-src"), (1, False))
        x = src.mint_entity()
        src.append_observation(x, _obs(1, "Violet", appearances=5))
        dst = self._store("work-id-123")
        z = dst.mint_entity()  # dst's own ent_0001: sequences offset
        dst.append_observation(z, _obs(1, "Xaden Riorson"))
        dst.adopt_store(src)
        ordinal, repeat = dst.claim_run("fp-src")
        self.assertEqual((ordinal, repeat), (1, True))
        assignments = dst.run_assignments(1)
        new_id = assignments["violet"]
        self.assertNotEqual(new_id, x)  # reminted under dst
        total = sum(len(e["observations"])
                    for e in dst.data["entities"].values())
        self.assertEqual(total, 2)
        dst.replace_observation(new_id, _obs(1, "Violet",
                                             appearances=9))
        total = sum(len(e["observations"])
                    for e in dst.data["entities"].values())
        self.assertEqual(total, 2)  # refreshed in place, not appended
        obs = dst.get_entity(new_id)["observations"]
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["appearances"], 9)
        self.assertEqual(dst.claim_run("fp-other"), (2, False))
        self.assertEqual(dst.accounting()["unaccounted"], 0)

    def test_adoption_refused_when_a_vetoed_union_is_embodied(self):
        src = self._store("title:book|author")
        x = src.mint_entity()
        src.append_observation(x, _obs(1, "Pete Sebeck"))
        src.append_observation(x, _obs(1, "Chris Sebeck"))
        dst = self._store("work-id-9")
        dst.record_no("pete sebeck", "chris sebeck", source="test")
        with self.assertRaises(pp.RosterVetoError):
            dst.adopt_store(src)
        self.assertEqual(dst.accounting()["minted"], 0)


class TestViews(_StoreCase):
    def test_views_split_trusted_aliases_from_suspect(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet Sorrengail",
                                      aliases=["Violet"]))
        st.append_observation(a, _obs(2, "Violet", aliases=["Violence"],
                                      desc_hash="h1"))
        st.flag_suspect_observations(a, run=2, form_key="violet")
        (view,) = st.entity_views()
        self.assertEqual(view["id"], a)
        self.assertIn("violet sorrengail", view["primary_forms"])
        self.assertIn("violet", view["alias_keys"])
        # 'violence' is attested ONLY by the suspect observation.
        self.assertNotIn("violence", view["alias_keys"])
        self.assertIn("violence", view["alias_keys_all"])
        self.assertEqual(view["desc_hash"], "")  # latest TRUSTED obs

    def test_detached_observations_leave_the_view(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet Sorrengail"))
        st.append_observation(a, _obs(1, "Dad"))
        st.detach_form(a, "dad", run=1)
        (view,) = st.entity_views()
        self.assertNotIn("dad", view["primary_forms"])
        # And the detach recorded NOs against the entity's other forms.
        self.assertTrue(st.has_no("dad", "violet sorrengail"))


class TestSuspectPrimaryForms(_StoreCase):
    """Fix-queue batch 2, item 1 (B-F1): primary-form authority
    follows the A1 exclusion — the same one the canonical vote
    (compute_canonical_name) and tier-2 alias evidence already
    apply. A form attested ONLY by suspect observations must not
    claim tier 1 / tier 3 for its entity: that is a suspended link
    silently resolving itself. The form stays visible in
    primary_forms_all, the reporting / split-suspect surface."""

    def _store_with_suspect_long_form(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet"))
        st.append_observation(a, _obs(2, "Violet Sorrengail"))
        st.flag_suspect_observations(a, run=2,
                                     form_key="violet sorrengail")
        return st, a

    def test_suspect_only_form_carries_no_primary_authority(self):
        st, a = self._store_with_suspect_long_form()
        (view,) = st.entity_views()
        self.assertIn("violet", view["primary_forms"])
        self.assertNotIn("violet sorrengail", view["primary_forms"])
        self.assertIn("violet sorrengail", view["primary_forms_all"])

    def test_form_with_a_trusted_attestation_stays_primary(self):
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet Sorrengail"))
        st.append_observation(a, _obs(2, "Violet Sorrengail"))
        st.flag_suspect_observations(a, run=2,
                                     form_key="violet sorrengail")
        (view,) = st.entity_views()
        self.assertIn("violet sorrengail", view["primary_forms"])

    def test_resolved_suspect_form_votes_again(self):
        st, a = self._store_with_suspect_long_form()
        st.resolve_suspect(a, "violet sorrengail")
        (view,) = st.entity_views()
        self.assertIn("violet sorrengail", view["primary_forms"])


class TestAnchorLifecycle(_StoreCase):
    """Fix-queue batch 2, item 2: the anchor lifecycle. An anchor
    is a fetched curation fact, and the fetch changes: rows flip
    kind, gain aliases, and get deleted. Re-seeding must reconcile
    the stored anchor with the fresh row; a row the re-read no
    longer claims must retire its anchor (tombstoned), or a
    revocation can never take effect; and a merge of two anchored
    entities must not silently drop the folded side's anchor."""

    def test_reseed_reconciles_kind_source_and_aliases(self):
        st = self._store()
        eid = st.seed_anchor("Violet Sorrengail", "confirmed", "db",
                             run=1, aliases=["Violet"])
        again = st.seed_anchor("Violet Sorrengail", "rejected", "db",
                               run=2, aliases=["Violet", "Violence"])
        self.assertEqual(again, eid)
        anchor = st.get_entity(eid)["anchor"]
        # The row flipped confirmed -> rejected between runs: the
        # stored anchor must follow, or suppression (which reads
        # kind off the anchor) can never start.
        self.assertEqual(anchor["kind"], "rejected")
        self.assertEqual(anchor["aliases"], ["Violence", "Violet"])
        self.assertEqual(anchor["seen_run"], 2)
        marker = {"suppressed": [], "suppressed_entities": []}
        pp._note_roster_suppression(st, marker, eid, "anchor",
                                    "Violet Sorrengail")
        self.assertEqual(len(marker["suppressed"]), 1)

    def test_unclaimed_anchor_is_retired_with_a_tombstone(self):
        st = self._store()
        gone = st.seed_anchor("Violet Sorrengail", "rejected", "db",
                              run=1)
        keep = st.seed_anchor("Xaden Riorson", "confirmed", "db",
                              run=1)
        # Run 2's re-read succeeds and no longer carries Violet's
        # row (deleted): only Xaden's anchor is claimed.
        claimed = {st.seed_anchor("Xaden Riorson", "confirmed", "db",
                                  run=2)}
        retired = st.retire_unclaimed_anchors(claimed, run=2)
        self.assertEqual([e for e, _a in retired], [gone])
        self.assertIsNone(st.get_entity(gone)["anchor"])
        self.assertEqual(set(st.anchored_entities()), {keep})
        (tomb,) = st.data["anchor_retirements"]
        self.assertEqual(tomb["entity"], gone)
        self.assertEqual(tomb["run"], 2)
        self.assertEqual(tomb["anchor"]["kind"], "rejected")
        # A deleted rejected row stops suppressing.
        marker = {"suppressed": [], "suppressed_entities": []}
        pp._note_roster_suppression(st, marker, gone, "anchor",
                                    "Violet Sorrengail")
        self.assertEqual(marker["suppressed"], [])
        # Retirement is durable across a save/load round trip.
        st.save()
        st2 = self._store()
        self.assertIsNone(st2.get_entity(gone)["anchor"])
        self.assertEqual(len(st2.data["anchor_retirements"]), 1)

    def test_merge_of_two_anchored_entities_preserves_folded_anchor(self):
        st = self._store()
        a = st.seed_anchor("Violet Sorrengail", "confirmed", "db", 1)
        b = st.seed_anchor("Xaden Riorson", "rejected", "db", 1)
        st.record_merge(a, b, run=2, reason="within-run merge")
        # The survivor keeps its OWN anchor; the folded side's is
        # preserved on the tombstone, not silently dropped.
        self.assertEqual(st.get_entity(a)["anchor"]["name"],
                         "Violet Sorrengail")
        tomb = st.data["retired"][b]
        self.assertEqual(tomb["anchor"]["name"], "Xaden Riorson")
        self.assertEqual(tomb["anchor"]["kind"], "rejected")

    def test_merge_transfers_single_anchor_to_survivor(self):
        # Pre-existing behaviour, pinned: only the folded side
        # anchored -> the survivor inherits anchor + canonical,
        # and no anchor lands on the tombstone.
        st = self._store()
        a = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet"))
        b = st.seed_anchor("Violet Sorrengail", "confirmed", "db", 1)
        st.record_merge(a, b, run=1, reason="within-run merge")
        surv = st.get_entity(a)
        self.assertEqual(surv["anchor"]["name"], "Violet Sorrengail")
        self.assertEqual(surv["canonical"], "Violet Sorrengail")
        self.assertNotIn("anchor", st.data["retired"][b])


class TestAccountingRandomized(_StoreCase):
    def test_accounting_closes_on_randomized_sequences(self):
        rng = random.Random(20261014)
        st = self._store()
        forms = ["Alpha One", "Beta Two", "Gamma Three", "Delta Four",
                 "Epsilon Five", "Zeta Six"]
        live = []
        for step in range(300):
            op = rng.choice(["mint", "observe", "merge", "split",
                             "link", "suspend"])
            if op == "mint" or not live:
                live.append(st.mint_entity())
            elif op == "observe":
                st.append_observation(
                    rng.choice(live),
                    _obs(rng.randint(1, 3), rng.choice(forms),
                         appearances=rng.randint(1, 40)))
            elif op == "merge" and len(live) >= 2:
                a, b = rng.sample(live, 2)
                st.record_merge(a, b, run=1, reason="within-run merge")
                live.remove(b)
            elif op == "split":
                e = rng.choice(live)
                ent = st.get_entity(e)
                fks = sorted({o["form_key"]
                              for o in ent["observations"]})
                if fks:
                    out = st.record_split(e, fks[:1], run=1)
                    live.extend(out.values())
            elif op == "link":
                e = rng.choice(live)
                st.record_link(
                    e, {"run": 1, "tier": "exact-form", "state": "active",
                        "evidence": "x", "form_key": "alpha one"},
                    entry_forms={"alpha one"})
            elif op == "suspend":
                e = rng.choice(live)
                st.suspend_link(e, run=1, form_key="alpha one",
                                evidence="contradiction")
            acc = st.accounting()
            self.assertEqual(acc["unaccounted"], 0,
                             f"step {step} ({op}): {acc}")
            self.assertEqual(
                acc["minted"],
                acc["active"] + acc["merged"] + acc["split"])


if __name__ == "__main__":
    unittest.main()
