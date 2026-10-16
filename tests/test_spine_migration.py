"""Measurement spine, slice 2: StagingState + RosterStore on the
substrate, plus the JSON->SQLite migration (spine-design §3.7 + A1).

Pins: fully-populated round-trips (the deep review's spot gap),
claim_run parity on the substrate (batch-2's adoption/re-completion
semantics), concurrent observes losing no sightings, and the
migration protocol — build into state.db.migrating, verify, rename
into place, originals renamed (never deleted), rollback restores
byte-identical JSON authority, failure is delay-only, and a
simulated mid-migration crash (leftover .migrating) is discarded
and retried, never run on.
"""
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

WK = "title:test book|some author"


def _obs(run, form, chapters=(1,), appearances=5, aliases=()):
    return {"run": run, "form": form, "form_key": pp.norm_name(form),
            "chapters": sorted(chapters), "appearances": appearances,
            "role_hint": None, "aliases": sorted(aliases),
            "alias_keys": sorted(pp.norm_name(a) for a in aliases),
            "desc_hash": ""}


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        pp._close_state_dbs()
        self._td.cleanup()


class TestStagingOnSubstrate(_Base):
    def test_fully_populated_roundtrip(self):
        pp.StateDB.create(self.dir)
        st = pp.StagingState(learn_dir=self.dir, enabled=True)
        w = st.work_entry("wk-1", work_id="wid-1", title="T", author="A")
        w["runs_completed"] = 3
        w["staged_ever"] = 3
        w["adoptions"] = [{"from_key": "isbn:x", "sightings_moved": 1,
                           "claims_moved": 2, "run": 2}]
        w["expired_dropped"] = 1
        claim = st._new_claim("trope", "Enemies to Lovers", "2026-10-01")
        claim.update({
            "name_forms": ["Enemies to Lovers", "enemies-to-lovers"],
            "sightings": [1, 3], "first_seen": "2026-10-01",
            "last_seen": "2026-10-03", "best_chapters": 7,
            "evidence_ever": True, "state": "promoted",
            "promoted_run": 3, "promoted_via": "persistence",
            "promoted_form_changed": True})
        w["claims"]["trope:id:abc"] = claim
        held = st._new_claim("trigger", "graphic violence",
                             "2026-10-02")
        held.update({"sightings": [2], "best_chapters": 2})
        w["claims"]["trigger:graphic violence"] = held
        w["expired_recent"] = [{
            "key": "trope:name:old", "class": "trope", "display": "Old",
            "sightings": [1], "expired_run": 3, "last_seen": "2026-10-01",
            "best_chapters": 1, "evidence_ever": False}]
        self.assertTrue(st.save())
        self.assertFalse((self.dir / "staging.json").exists())
        fresh = pp.StagingState(learn_dir=self.dir, enabled=True)
        self.assertEqual(fresh.data, st.data)

    def test_observe_flow_on_substrate(self):
        pp.StateDB.create(self.dir)
        st = pp.StagingState(learn_dir=self.dir, enabled=True)
        tropes = [{"name": "Found Family", "n_ch": 1, "catalog_id": "t-1"}]
        out1 = st.observe_work(work_key="wk", title="T", author="A",
                               tropes=tropes, auto_min=3, today="2026-10-01")
        self.assertEqual(out1["trope_actions"][0]["action"], "hold")
        st2 = pp.StagingState(learn_dir=self.dir, enabled=True)
        out2 = st2.observe_work(work_key="wk", title="T", author="A",
                                tropes=tropes, auto_min=3,
                                today="2026-10-02")
        self.assertEqual(out2["trope_actions"][0]["action"], "ship")
        self.assertEqual(out2["trope_actions"][0]["stamp"],
                         {"sightings": 2, "via": "persistence",
                          "sighting_runs": [1, 2]})

    def test_concurrent_observe_no_lost_sightings(self):
        pp.StateDB.create(self.dir)
        st = pp.StagingState(learn_dir=self.dir, enabled=True)
        errs = []

        def worker(k):
            try:
                st.observe_work(
                    work_key=f"wk-{k}", title="T", author="A",
                    tropes=[{"name": "Found Family", "n_ch": 1,
                             "catalog_id": "t-1"}],
                    auto_min=3, today="2026-10-01")
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])
        fresh = pp.StagingState(learn_dir=self.dir, enabled=True)
        self.assertEqual(len(fresh.data["works"]), 8)
        for w in fresh.data["works"].values():
            claims = list(w["claims"].values())
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0]["sightings"], [1])


class TestRosterOnSubstrate(_Base):
    def test_fully_populated_roundtrip(self):
        pp.StateDB.create(self.dir)
        st = pp.RosterStore(learn_dir=self.dir, work_key=WK)
        a = st.mint_entity()
        b = st.mint_entity()
        st.append_observation(a, _obs(1, "Violet Sorrengail",
                                      aliases=["Violet"]))
        st.append_observation(b, _obs(1, "Xaden Riorson"))
        st.set_canonical(a, "Violet Sorrengail", "rule")
        st.seed_anchor("Xaden Riorson", "confirmed", "db", run=1,
                       aliases=["Xaden"])
        st.record_no("pete sebeck", "jonas", source="test")
        st.record_merge(a, b, run=1, reason="within-run merge")
        st.data["suspects"] = [{"a": "x", "b": "y", "run": 1,
                                "reason": "form overlap"}]
        st.data["canonical_changes"] = [
            {"entity": a, "from": None, "to": "Violet Sorrengail",
             "run": 1}]
        st.data["collisions"] = [{"run": 1, "note": "c"}]
        st.data["anchor_retirements"] = [
            {"entity_id": "ent_0099", "run": 1, "name": "Gone"}]
        st.data["adoptions"] = [{"from_key": "isbn:z", "run": 1}]
        self.assertTrue(st.save())
        self.assertFalse(st.path.exists())
        fresh = pp.RosterStore(learn_dir=self.dir, work_key=WK)
        self.assertFalse(fresh.corrupt_at_load)
        self.assertEqual(fresh.data, st.data)
        self.assertEqual(fresh.accounting()["unaccounted"], 0)

    def test_claim_run_parity_on_substrate(self):
        pp.StateDB.create(self.dir)
        st = pp.RosterStore(learn_dir=self.dir, work_key=WK)
        fp = pp.roster_fingerprint(
            {"violet": {"name": "Violet", "appearances": 3,
                        "chapters": [1]}})
        o1, rep1 = st.claim_run(fp)
        self.assertEqual((o1, rep1), (1, False))
        e = st.mint_entity()
        st.append_observation(e, _obs(1, "Violet"))
        st.save()
        # Re-completion of the SAME extraction: ordinal reused,
        # runs_completed does not advance, no re-mint.
        fresh = pp.RosterStore(learn_dir=self.dir, work_key=WK)
        o2, rep2 = fresh.claim_run(fp)
        self.assertEqual((o2, rep2), (1, True))
        self.assertEqual(fresh.data["runs_completed"], 1)
        o3, rep3 = fresh.claim_run("a-different-fingerprint")
        self.assertEqual((o3, rep3), (2, False))


# -- migration fixtures: real-SHAPE stores (the §3.7 discipline) ----

def _seed_json_stores(d):
    (d / "nicknames.json").write_text(json.dumps(
        {"pete": {"full": "peter", "count": 3, "books": ["i1"],
                  "human": True, "manual": True}}), encoding="utf-8")
    (d / "titles.json").write_text(json.dumps(
        {"captain": {"count": 2, "rests": ["a", "b"]}}), encoding="utf-8")
    (d / "threshold_stats.json").write_text(json.dumps(
        {"gate": {"kept": [0.9], "dropped": [0.2]}}), encoding="utf-8")
    (d / "rejected_tropes.json").write_text(json.dumps(
        {"enemies to lovers": {"count": 1, "books": ["Daemon"],
                                "reasons": ["no"], "human": True}}),
        encoding="utf-8")
    (d / "series").mkdir(exist_ok=True)
    (d / "series" / "author_daniel_suarez.json").write_text(json.dumps(
        {"characters": [{"name": "Peter Sebeck", "aliases": ["Pete"]}],
         "books": []}), encoding="utf-8")
    (d / "series" / "daemon").mkdir(exist_ok=True)
    (d / "series" / "daemon" / "roster.json").write_text(json.dumps(
        {"characters": [{"name": "Peter Sebeck", "aliases": ["Pete"]}],
         "books": [], "series": "daemon"}), encoding="utf-8")
    works = {}
    for wk, n_claims in (("wk-a", 2), ("wk-b", 1)):
        claims = {}
        for i in range(n_claims):
            claims[f"trope:id:t{i}"] = {
                "class": "trope", "display": f"Trope {i}",
                "name_forms": [f"Trope {i}"], "sightings": [1],
                "first_seen": "2026-10-01", "last_seen": "2026-10-01",
                "best_chapters": 2, "evidence_ever": False,
                "state": "staged", "promoted_run": None,
                "promoted_via": None, "promoted_form_changed": False}
        works[wk] = {"work_id": None, "title": wk, "author": "A",
                     "runs_completed": 1, "staged_ever": n_claims,
                     "claims": claims, "expired_recent": [],
                     "expired_dropped": 0, "adoptions": []}
    (d / "staging.json").write_text(json.dumps(
        {"version": 1, "works": works}), encoding="utf-8")
    (d / "rosters").mkdir(exist_ok=True)
    roster = pp.RosterStore._empty_data("wk-a")
    roster["title"] = "wk-a"
    roster["entities"]["ent_0001"] = {
        "canonical": "Violet", "canonical_source": "rule",
        "status": "active", "split_from": None, "split_into": [],
        "anchor": None, "observations": [_obs(1, "Violet")], "links": []}
    roster["next_entity_seq"] = 2
    roster["minted_total"] = 1
    roster["runs_completed"] = 1
    (d / "rosters" / pp._roster_store_filename("wk-a")).write_text(
        json.dumps(roster), encoding="utf-8")


class TestMigration(_Base):
    def test_migration_happy_path_and_rollback(self):
        _seed_json_stores(self.dir)
        before = pp.StagingState(learn_dir=self.dir, enabled=True).data
        rep = pp.migrate_state_to_substrate(self.dir)
        self.assertEqual(rep["status"], "migrated")
        self.assertTrue((self.dir / "state.db").exists())
        self.assertFalse((self.dir / "state.db.migrating").exists())
        # Originals renamed, never deleted.
        self.assertFalse((self.dir / "staging.json").exists())
        renamed = sorted(p.name for p in self.dir.glob("*.migrated-*"))
        self.assertIn("staging.json", [r.rsplit(".migrated-", 1)[0]
                                       for r in renamed])
        # Stores now read the substrate, byte-identical content.
        after = pp.StagingState(learn_dir=self.dir, enabled=True).data
        self.assertEqual(after, before)
        learned = pp.LearnedState(learn_dir=self.dir, enabled=True)
        self.assertEqual(learned.nicknames["pete"]["full"], "peter")
        hints = learned.load_series_hints(
            learned.series_key("Daniel Suarez"))
        self.assertEqual(hints.get("pete"), "peter sebeck")
        roster = pp.RosterStore(learn_dir=self.dir, work_key="wk-a")
        self.assertEqual(roster.accounting()["unaccounted"], 0)
        # Rollback: delete state.db, strip the suffixes, JSON rules
        # again with identical content.
        pp._close_state_dbs()
        (self.dir / "state.db").unlink()
        for p in list(self.dir.rglob("*.migrated-*")):
            p.rename(Path(str(p).rsplit(".migrated-", 1)[0]))
        restored = pp.StagingState(learn_dir=self.dir, enabled=True).data
        self.assertEqual(restored, before)

    def test_migration_idempotent(self):
        _seed_json_stores(self.dir)
        pp.migrate_state_to_substrate(self.dir)
        rep2 = pp.migrate_state_to_substrate(self.dir)
        self.assertEqual(rep2["status"], "already")

    def test_migration_aborts_on_corrupt_store_delay_only(self):
        _seed_json_stores(self.dir)
        (self.dir / "staging.json").write_text("{not json",
                                              encoding="utf-8")
        rep = pp.migrate_state_to_substrate(self.dir)
        self.assertEqual(rep["status"], "aborted")
        self.assertFalse((self.dir / "state.db").exists())
        self.assertFalse((self.dir / "state.db.migrating").exists())
        # JSON remains authoritative and untouched.
        self.assertTrue((self.dir / "staging.json").exists())
        self.assertFalse(list(self.dir.glob("*.migrated-*")))

    def test_migration_aborts_on_broken_identity(self):
        _seed_json_stores(self.dir)
        data = json.loads((self.dir / "staging.json").read_text())
        data["works"]["wk-a"]["staged_ever"] = 99  # identity broken
        (self.dir / "staging.json").write_text(json.dumps(data))
        rep = pp.migrate_state_to_substrate(self.dir)
        self.assertEqual(rep["status"], "aborted")
        self.assertFalse((self.dir / "state.db").exists())

    def test_leftover_migrating_is_discarded_and_retried(self):
        """A1: a crash mid-migration leaves state.db.migrating; the
        next attempt discards it and completes — the partial DB is
        never run on."""
        _seed_json_stores(self.dir)
        (self.dir / "state.db.migrating").write_bytes(b"partial junk")
        rep = pp.migrate_state_to_substrate(self.dir)
        self.assertEqual(rep["status"], "migrated")
        self.assertTrue((self.dir / "state.db").exists())
        st = pp.StagingState(learn_dir=self.dir, enabled=True)
        self.assertEqual(len(st.data["works"]), 2)


if __name__ == "__main__":
    unittest.main()
