"""Option F — empty-evidence staging gate + persistence gate.

Design: eval/f-staging-design.md (user-approved open choices as amended
by the Security Advisor red-team). Persistence is a stability filter,
not truth: a promoted claim is a claim that stopped flickering, never
a claim that was verified.

Covered here, by build slice:
  1. StagingState store: F8 persistence pattern (atomic save, loud
     corruption, .corrupt preservation), RLock-serialized observe,
     catalog-id keying + name->id folding by union of run ordinals,
     absence-based expiry, accounting that closes
     (staged_ever = promoted + still_staged + expired), family refusal,
     store loss => nothing promotes.
  2. Trope observe/split wiring behind --staging-gate.
  3. Trigger observe/split wiring.
  4. Write-path honoring (stamps, proposals, plan, push-preview).
  5. --staging-report.

Stdlib unittest only. No network, no Supabase (fake sb where needed).
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

D1 = "2026-10-14"
D2 = "2026-10-21"
TITLE_KEY = "title:test book|some author"


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*args, **kwargs)
    return out, buf.getvalue()


def _buckets(state, work_key):
    """(staged_ever, promoted, still_staged, expired) for one work.
    Expired counts retained records plus cap-evicted ones (the work's
    expired_dropped counter) so the identity can close exactly."""
    w = state.data["works"][work_key]
    promoted = sum(1 for e in w["claims"].values()
                   if e["state"] == "promoted")
    staged = sum(1 for e in w["claims"].values()
                 if e["state"] == "staged")
    expired = len(w["expired_recent"]) + w.get("expired_dropped", 0)
    return w["staged_ever"], promoted, staged, expired


def _assert_accounting_closes(case, state, work_key):
    ever, promoted, staged, expired = _buckets(state, work_key)
    case.assertEqual(ever, promoted + staged + expired,
                     f"accounting open for {work_key}: ever={ever} "
                     f"promoted={promoted} staged={staged} "
                     f"expired={expired}")


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _state(self):
        return pp.StagingState(learn_dir=self.dir, enabled=True)

    def _observe(self, state, **kw):
        kw.setdefault("work_key", "w-1")
        kw.setdefault("work_id", "w-1")
        kw.setdefault("title", "Test Book")
        kw.setdefault("author", "Some Author")
        kw.setdefault("auto_min", 5)
        kw.setdefault("independent", True)
        kw.setdefault("today", D1)
        return state.observe_work(**kw)


# ----------------------------------------------------------------------
# Slice 1 — StagingState store
# ----------------------------------------------------------------------

class TestStagingClaimKey(unittest.TestCase):
    def test_trope_id_key_and_name_key(self):
        self.assertEqual(
            pp.staging_claim_key("trope", name="X", catalog_id="abc"),
            "trope:id:abc")
        self.assertEqual(
            pp.staging_claim_key("trope", name="Enemies_to Lovers"),
            "trope:name:enemies to lovers")

    def test_trigger_key_normalizes(self):
        self.assertEqual(
            pp.staging_claim_key("trigger", warning="  Non_Consent "),
            "trigger:non_consent")

    def test_family_and_relationship_claims_refused(self):
        # The head-on doctrine case (design section 7): there is no
        # code path from staging to a relationship row; the helper
        # refuses the class outright instead of staging it.
        for cls in ("relationship", "family", "character"):
            with self.assertRaises(ValueError):
                pp.staging_claim_key(cls, name="A -> B (parent)")


class TestStagingStoreBasics(_StoreCase):
    def test_first_gate_band_sighting_stages_and_holds(self):
        st = self._state()
        out = self._observe(st, tropes=[
            {"name": "Found Family", "n_ch": 2, "catalog_id": None}])
        self.assertEqual(out["trope_actions"][0]["action"], "hold")
        self.assertEqual(out["sightings_recorded"], 1)
        self.assertEqual(out["trapped_total"], 1)
        self.assertEqual(len(out["staged"]), 1)
        item = out["staged"][0]
        self.assertEqual(item["key"], "trope:name:found family")
        self.assertEqual(item["display"], "Found Family")
        self.assertEqual(item["class"], "trope")
        self.assertEqual(item["sightings"], 1)
        _assert_accounting_closes(self, st, "w-1")

    def test_auto_band_first_sighting_ships_without_entry(self):
        st = self._state()
        out = self._observe(st, tropes=[
            {"name": "Quest", "n_ch": 5, "catalog_id": None}])
        self.assertEqual(out["trope_actions"][0]["action"], "ship")
        self.assertEqual(out["trapped_total"], 0)
        self.assertEqual(st.data["works"]["w-1"]["claims"], {})
        _assert_accounting_closes(self, st, "w-1")

    def test_second_sighting_promotes_via_persistence_with_stamp(self):
        st = self._state()
        obs = [{"name": "Found Family", "n_ch": 2, "catalog_id": None}]
        self._observe(st, tropes=obs)
        out = self._observe(st, tropes=obs, today=D2)
        act = out["trope_actions"][0]
        self.assertEqual(act["action"], "ship")
        self.assertEqual(act["stamp"], {"sightings": 2,
                                        "via": "persistence"})
        self.assertEqual(len(out["promoted"]), 1)
        self.assertEqual(out["promoted"][0]["via"], "persistence")
        self.assertEqual(out["promoted"][0]["sightings"], 2)
        self.assertEqual(out["trapped_total"], 0)
        entry = st.data["works"]["w-1"]["claims"]["trope:name:found family"]
        self.assertEqual(entry["state"], "promoted")
        self.assertEqual(entry["sightings"], [1, 2])
        _assert_accounting_closes(self, st, "w-1")

    def test_support_growth_promotes_and_sticks(self):
        st = self._state()
        self._observe(st, tropes=[
            {"name": "Quest", "n_ch": 2, "catalog_id": None}])
        out = self._observe(st, tropes=[
            {"name": "Quest", "n_ch": 7, "catalog_id": None}], today=D2)
        self.assertEqual(out["trope_actions"][0]["action"], "ship")
        self.assertEqual(len(out["promoted"]), 1)
        self.assertEqual(out["promoted"][0]["via"], "support")
        # Support promotion ships on this run's own band: no stamp yet.
        self.assertIsNone(out["trope_actions"][0]["stamp"])
        # Flicker back down to the gate band: sticky promotion ships
        # it, and now the stamp explains its presence in the ship set.
        out3 = self._observe(st, tropes=[
            {"name": "Quest", "n_ch": 2, "catalog_id": None}], today=D2)
        self.assertEqual(out3["trope_actions"][0]["action"], "ship")
        self.assertEqual(out3["trope_actions"][0]["stamp"],
                         {"sightings": 3, "via": "support"})
        self.assertEqual(out3["promoted"], [])
        _assert_accounting_closes(self, st, "w-1")

    def test_flicker_down_stages_fresh(self):
        # First sighted AUTO band (ships, no entry); a later gate-band
        # sighting stages it as a first sighting and holds it (design
        # section 4, flicker-down).
        st = self._state()
        out1 = self._observe(st, tropes=[
            {"name": "Quest", "n_ch": 9, "catalog_id": None}])
        self.assertEqual(out1["trope_actions"][0]["action"], "ship")
        out2 = self._observe(st, tropes=[
            {"name": "Quest", "n_ch": 2, "catalog_id": None}], today=D2)
        self.assertEqual(out2["trope_actions"][0]["action"], "hold")
        self.assertEqual(out2["sightings_recorded"], 1)
        _assert_accounting_closes(self, st, "w-1")

    def test_same_run_duplicate_forms_count_one_sighting(self):
        st = self._state()
        out = self._observe(st, tropes=[
            {"name": "Enemies to Lovers", "n_ch": 2, "catalog_id": "t-1"},
            {"name": "Enemies To Lovers!", "n_ch": 2,
             "catalog_id": "t-1"}])
        entry = st.data["works"]["w-1"]["claims"]["trope:id:t-1"]
        self.assertEqual(entry["sightings"], [1])
        self.assertEqual(out["sightings_recorded"], 1)
        self.assertEqual(entry["state"], "staged")
        _assert_accounting_closes(self, st, "w-1")

    def test_persistence_roundtrip_through_disk(self):
        st = self._state()
        self._observe(st, tropes=[
            {"name": "Found Family", "n_ch": 2, "catalog_id": None}])
        st2 = self._state()
        self.assertEqual(
            st2.data["works"]["w-1"]["claims"]
            ["trope:name:found family"]["sightings"], [1])
        out = self._observe(st2, tropes=[
            {"name": "Found Family", "n_ch": 2, "catalog_id": None}],
            today=D2)
        self.assertEqual(out["trope_actions"][0]["action"], "ship")


class TestStagingFolding(_StoreCase):
    def test_name_entry_folds_into_id_entry_unioning_ordinals(self):
        st = self._state()
        self._observe(st, tropes=[
            {"name": "Enemies to Lovers", "n_ch": 2, "catalog_id": None}])
        out = self._observe(st, tropes=[
            {"name": "Enemies to Lovers", "n_ch": 2,
             "catalog_id": "t-9"}], today=D2)
        claims = st.data["works"]["w-1"]["claims"]
        self.assertEqual(list(claims), ["trope:id:t-9"])
        entry = claims["trope:id:t-9"]
        # Union of ordinals, never a sum: [1] U [2] == [1, 2].
        self.assertEqual(entry["sightings"], [1, 2])
        self.assertEqual(entry["state"], "promoted")
        self.assertEqual(out["trope_actions"][0]["action"], "ship")
        # Two entries were never two claims: the counter says 1.
        _assert_accounting_closes(self, st, "w-1")
        self.assertEqual(st.data["works"]["w-1"]["staged_ever"], 1)

    def test_form_change_on_promotion_is_flagged(self):
        st = self._state()
        self._observe(st, tropes=[
            {"name": "Enemies to Lovers", "n_ch": 2,
             "catalog_id": "t-1"}])
        out = self._observe(st, tropes=[
            {"name": "Enemies To Lovers!", "n_ch": 2,
             "catalog_id": "t-1"}], today=D2)
        self.assertEqual(len(out["promoted"]), 1)
        self.assertTrue(out["promoted"][0].get("form_changed"))
        entry = st.data["works"]["w-1"]["claims"]["trope:id:t-1"]
        self.assertTrue(entry["promoted_form_changed"])
        self.assertEqual(len(entry["name_forms"]), 2)


class TestStagingTriggers(_StoreCase):
    def test_evidenceless_trigger_stages_then_promotes_by_persistence(
            self):
        st = self._state()
        obs = [{"warning": "non_consent", "has_evidence": False}]
        out1 = self._observe(st, triggers=obs)
        self.assertEqual(out1["trigger_actions"][0]["action"], "hold")
        out2 = self._observe(st, triggers=obs, today=D2)
        act = out2["trigger_actions"][0]
        self.assertEqual(act["action"], "ship")
        self.assertEqual(act["stamp"], {"sightings": 2,
                                        "via": "persistence"})
        _assert_accounting_closes(self, st, "w-1")

    def test_evidenced_trigger_never_stages(self):
        st = self._state()
        out = self._observe(st, triggers=[
            {"warning": "murder", "has_evidence": True}])
        self.assertEqual(out["trigger_actions"][0]["action"], "ship")
        self.assertEqual(st.data["works"]["w-1"]["claims"], {})

    def test_evidence_arrival_promotes(self):
        st = self._state()
        self._observe(st, triggers=[
            {"warning": "murder", "has_evidence": False}])
        out = self._observe(st, triggers=[
            {"warning": "murder", "has_evidence": True}], today=D2)
        self.assertEqual(out["trigger_actions"][0]["action"], "ship")
        self.assertEqual(len(out["promoted"]), 1)
        self.assertEqual(out["promoted"][0]["via"], "evidence")
        # Ships on this sighting's own evidence: no stamp this run.
        self.assertIsNone(out["trigger_actions"][0]["stamp"])
        entry = st.data["works"]["w-1"]["claims"]["trigger:murder"]
        self.assertTrue(entry["evidence_ever"])


class TestStagingNonIndependent(_StoreCase):
    def test_resumed_run_never_increments_sightings(self):
        # The -15 shape: a full resume re-derives the same extraction.
        # Counting it would manufacture persistence from one
        # observation.
        st = self._state()
        obs = [{"name": "Found Family", "n_ch": 2, "catalog_id": None}]
        self._observe(st, tropes=obs)
        out = self._observe(st, tropes=obs, independent=False, today=D2)
        self.assertEqual(out["sightings_recorded"], 0)
        entry = st.data["works"]["w-1"]["claims"]["trope:name:found family"]
        self.assertEqual(entry["sightings"], [1])
        self.assertEqual(entry["state"], "staged")
        self.assertEqual(entry["last_seen"], D2)  # metadata may move
        self.assertEqual(st.data["works"]["w-1"]["runs_completed"], 1)
        # The split still applies: a staged claim stays held.
        self.assertEqual(out["trope_actions"][0]["action"], "hold")
        # A later independent run is the genuine second sighting.
        out3 = self._observe(st, tropes=obs, today=D2)
        self.assertEqual(out3["trope_actions"][0]["action"], "ship")
        self.assertEqual(out3["trope_actions"][0]["stamp"],
                         {"sightings": 2, "via": "persistence"})

    def test_nonindependent_run_creates_no_entries(self):
        st = self._state()
        out = self._observe(st, tropes=[
            {"name": "Found Family", "n_ch": 2, "catalog_id": None}],
            independent=False)
        self.assertEqual(out["trope_actions"][0]["action"], "ship")
        self.assertEqual(st.data["works"], {})
        self.assertEqual(out["trapped_total"], 0)


class TestStagingExpiry(_StoreCase):
    def test_expires_at_exactly_three_absences(self):
        st = self._state()
        self._observe(st, tropes=[
            {"name": "Found Family", "n_ch": 2, "catalog_id": None}])
        for _ in range(2):  # runs 2 and 3: 1 and 2 absences
            out = self._observe(st, tropes=[])
            self.assertEqual(out["expired_this_run"], 0)
            self.assertEqual(out["trapped_total"], 1)
        out = self._observe(st, tropes=[])  # run 4: third absence
        self.assertEqual(out["expired_this_run"], 1)
        self.assertEqual(out["trapped_total"], 0)
        w = st.data["works"]["w-1"]
        self.assertEqual(len(w["expired_recent"]), 1)
        self.assertEqual(w["expired_recent"][0]["key"],
                         "trope:name:found family")
        self.assertEqual(w["expired_recent"][0]["sightings"], 1)
        _assert_accounting_closes(self, st, "w-1")

    def test_promoted_entries_never_expire(self):
        st = self._state()
        obs = [{"name": "Found Family", "n_ch": 2, "catalog_id": None}]
        self._observe(st, tropes=obs)
        self._observe(st, tropes=obs, today=D2)
        for _ in range(4):
            out = self._observe(st, tropes=[])
        self.assertEqual(out["expired_this_run"], 0)
        entry = st.data["works"]["w-1"]["claims"]["trope:name:found family"]
        self.assertEqual(entry["state"], "promoted")
        _assert_accounting_closes(self, st, "w-1")

    def test_expired_recent_is_capped(self):
        st = self._state()
        many = [{"name": f"Trope {i:03d}", "n_ch": 2, "catalog_id": None}
                for i in range(210)]
        self._observe(st, tropes=many)
        for _ in range(3):
            self._observe(st, tropes=[])
        w = st.data["works"]["w-1"]
        self.assertEqual(len(w["expired_recent"]),
                         pp._STAGING_EXPIRED_CAP)
        _assert_accounting_closes(self, st, "w-1")


class TestStagingCorruption(_StoreCase):
    def test_corrupt_store_warns_preserves_and_never_promotes_early(
            self):
        (self.dir / "staging.json").write_text("{ not json !!",
                                               encoding="utf-8")
        st, out_text = _capture(self._state)
        self.assertIn("staging.json", out_text)
        self.assertIn("WARNING", out_text)
        self.assertEqual(st.data["works"], {})
        self.assertTrue(st.corrupt_at_load)
        # A claim that would have had history stages fresh: the
        # failure direction is delay-only, never an early ship.
        out = self._observe(st, tropes=[
            {"name": "Found Family", "n_ch": 2, "catalog_id": None}])
        self.assertEqual(out["trope_actions"][0]["action"], "hold")
        # The original bytes survive as a .corrupt backup.
        backups = sorted(p.name for p in self.dir.glob("staging.json.corrupt*"))
        self.assertTrue(backups)
        saved = json.loads((self.dir / "staging.json").read_text(
            encoding="utf-8"))
        self.assertIn("works", saved)

    def test_store_loss_resets_sightings(self):
        st = self._state()
        obs = [{"name": "Found Family", "n_ch": 2, "catalog_id": None}]
        self._observe(st, tropes=obs)
        (self.dir / "staging.json").unlink()
        st2 = self._state()
        out = self._observe(st2, tropes=obs, today=D2)
        # No memory of run 1: this is a first sighting again.
        self.assertEqual(out["trope_actions"][0]["action"], "hold")
        self.assertEqual(out["sightings_recorded"], 1)


class TestStagingAdoption(_StoreCase):
    TITLE_KEY = "title:test book|some author"

    def test_fallback_entries_adopted_on_work_id_resolution(self):
        st = self._state()
        self._observe(st, work_key=self.TITLE_KEY, work_id=None,
                      tropes=[{"name": "Found Family", "n_ch": 2,
                               "catalog_id": None}])
        out = self._observe(st, tropes=[
            {"name": "Found Family", "n_ch": 2, "catalog_id": None}],
            today=D2)
        self.assertNotIn(self.TITLE_KEY, st.data["works"])
        w = st.data["works"]["w-1"]
        self.assertEqual(len(w["adoptions"]), 1)
        self.assertEqual(w["adoptions"][0]["from_key"], self.TITLE_KEY)
        self.assertEqual(w["adoptions"][0]["claims_moved"], 1)
        # The adopted sighting counts: this run is the second one.
        self.assertEqual(out["trope_actions"][0]["action"], "ship")
        self.assertEqual(out["trope_actions"][0]["stamp"],
                         {"sightings": 2, "via": "persistence"})
        _assert_accounting_closes(self, st, "w-1")

    def test_adoption_requires_exact_title_author_match(self):
        st = self._state()
        self._observe(st, work_key=self.TITLE_KEY, work_id=None,
                      tropes=[{"name": "Found Family", "n_ch": 2,
                               "catalog_id": None}])
        # Same title key string, but the stored author differs from
        # the resolving run's author: no adoption.
        st.data["works"][self.TITLE_KEY]["author"] = "Different Author"
        self._observe(st, today=D2)
        self.assertIn(self.TITLE_KEY, st.data["works"])
        self.assertEqual(st.data["works"]["w-1"]["adoptions"], [])


class TestStagingContention(_StoreCase):
    def test_concurrent_observes_lose_no_sightings(self):
        st = self._state()
        errors = []

        def worker(tag):
            claims = [{"name": f"Trope {tag} {i}", "n_ch": 2,
                       "catalog_id": None} for i in range(15)]
            try:
                for _ in range(15):
                    st.observe_work(
                        work_key=f"w-{tag}", work_id=f"w-{tag}",
                        title="T", author="A", auto_min=5,
                        independent=True, today=D1,
                        tropes=claims, triggers=[])
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for tag in range(4):
            w = st.data["works"][f"w-{tag}"]
            self.assertEqual(w["runs_completed"], 15)
            self.assertEqual(len(w["claims"]), 15)
            _assert_accounting_closes(self, st, f"w-{tag}")
        # The file on disk is valid, complete JSON.
        saved = json.loads((self.dir / "staging.json").read_text(
            encoding="utf-8"))
        self.assertEqual(len(saved["works"]), 4)


class TestStagingReportData(_StoreCase):
    def test_report_data_accounts_for_every_entry(self):
        st = self._state()
        self._observe(st, tropes=[
            {"name": "Alpha", "n_ch": 2, "catalog_id": None},
            {"name": "Beta", "n_ch": 2, "catalog_id": None},
            {"name": "Gamma", "n_ch": 2, "catalog_id": None}])
        self._observe(st, tropes=[
            {"name": "Alpha", "n_ch": 2, "catalog_id": None}], today=D2)
        for _ in range(3):
            self._observe(st, tropes=[])
        data = pp.staging_report_data(st, today=D2)
        self.assertEqual(data["unaccounted"], 0)
        w = data["works"][0]
        self.assertEqual(w["staged_ever"], 3)
        self.assertEqual(w["promoted"], 1)   # Alpha
        self.assertEqual(w["expired"], 2)    # Beta, Gamma
        self.assertEqual(w["staged"], 0)
        self.assertEqual(data["totals"]["staged_ever"], 3)

    def test_report_counts_form_changed_promotions(self):
        st = self._state()
        self._observe(st, tropes=[
            {"name": "Enemies to Lovers", "n_ch": 2,
             "catalog_id": "t-1"}], today="2026-01-01")
        self._observe(st, tropes=[
            {"name": "Enemies To Lovers!", "n_ch": 2,
             "catalog_id": "t-1"}], today="2026-01-02")
        data = pp.staging_report_data(st, today="2026-12-31")
        w = data["works"][0]
        self.assertEqual(w["form_changed_promotions"], 1)
        claim = w["claims"][0]
        self.assertTrue(claim["forms_differ"])
        self.assertEqual(data["totals"]["form_changed_promotions"], 1)

    def test_report_flags_stale_staged_claims(self):
        st = self._state()
        self._observe(st, tropes=[
            {"name": "Found Family", "n_ch": 2, "catalog_id": None}],
            today="2026-01-01")
        data = pp.staging_report_data(st, today="2026-12-31")
        claim = data["works"][0]["claims"][0]
        self.assertTrue(claim["stale"])
        self.assertGreaterEqual(claim["age_days"], 180)
        # A recently-seen claim is not stale.
        data2 = pp.staging_report_data(st, today="2026-01-20")
        self.assertFalse(data2["works"][0]["claims"][0]["stale"])


# ---------------------------------------------------------------------------
# Slice 2: adoption ordinal shift, adopt_for, seam helper, pipeline wiring
# ---------------------------------------------------------------------------

class TestAdoptionShift(_StoreCase):
    """A fallback entry recreated fresh numbers its run 1 every time;
    adoption must shift its ordinals by the surviving entry's run
    count, or every post-adoption sighting is silently eaten."""

    def test_shifted_union_across_repeated_adoptions(self):
        st = self._state()
        a = {"name": "Alpha Trope", "n_ch": 2, "catalog_id": None}
        b = {"name": "Beta Trope", "n_ch": 2, "catalog_id": None}
        # Two runs under the work_id key: Alpha promotes.
        self._observe(st, work_key="wid", work_id="wid", tropes=[a])
        self._observe(st, work_key="wid", work_id="wid", tropes=[a],
                      today=D2)
        # One run under the fallback key (fresh entry: Alpha+B at [1]).
        self._observe(st, work_key=TITLE_KEY, work_id=None,
                      tropes=[a, b], today="2026-10-28")
        self.assertTrue(st.adopt_for("wid", "Test Book", "Some Author"))
        w = st.data["works"]["wid"]
        self.assertNotIn(TITLE_KEY, st.data["works"])
        self.assertEqual(w["runs_completed"], 3)
        alpha = w["claims"]["trope:name:alpha trope"]
        beta = w["claims"]["trope:name:beta trope"]
        self.assertEqual(alpha["sightings"], [1, 2, 3])
        self.assertEqual(beta["sightings"], [3])
        self.assertEqual(beta["state"], "staged")
        _assert_accounting_closes(self, st, "wid")

    def test_adopt_for_noop_creates_nothing(self):
        st = self._state()
        self.assertFalse(st.adopt_for("wid", "Test Book", "Some Author"))
        self.assertEqual(st.data["works"], {})


def _parts(**kw):
    base = {"work_key": "w-1", "work_id": None, "title": "Test Book",
            "author": "Some Author", "isbn": None, "auto_min": 5,
            "trope_auto": [], "trope_gated": ["Found Family"],
            "trope_counts": {"found family": 2},
            "trope_ship": ["Found Family"],
            "trope_conf": {"found family": 0.75},
            "catalog_map": {}, "triggers": []}
    base.update(kw)
    return base


class _StubLearned:
    def __init__(self, counts):
        self._counts = counts

    def get_trope_rejection_count(self, key):
        return self._counts.get(key, 0)


class TestStagingObserveResult(_StoreCase):
    def test_gate_yes_held_run1_promoted_run2(self):
        st = self._state()
        out1 = pp.staging_observe_result(_parts(), st, independent=True)
        self.assertEqual(out1["ship_tropes"], [])
        self.assertEqual(len(out1["marker"]["staged"]), 1)
        self.assertEqual(out1["marker"]["sightings_recorded"], 1)
        self.assertEqual(out1["trope_persistence"], {})
        out2 = pp.staging_observe_result(_parts(), st, independent=True)
        self.assertEqual(out2["ship_tropes"], ["Found Family"])
        self.assertEqual(out2["trope_persistence"],
                         {"found family": {"sightings": 2,
                                           "via": "persistence"}})
        self.assertEqual(out2["marker"]["promoted"][0]["via"],
                         "persistence")

    def test_gate_no_claim_promotes_into_ship_set_with_formula_conf(self):
        st = self._state()
        p = _parts(trope_ship=[], trope_conf={})
        pp.staging_observe_result(p, st, independent=True)
        out = pp.staging_observe_result(p, st, independent=True)
        # Never gate-approved in either run; persistence alone ships
        # it, at the pipeline's own formula confidence (0.55+0.1*2).
        self.assertEqual(out["ship_tropes"], ["Found Family"])
        self.assertEqual(out["trope_conf"]["found family"], 0.75)

    def test_auto_band_ships_and_leaves_no_entry(self):
        st = self._state()
        p = _parts(trope_auto=["Big Trope"], trope_gated=[],
                   trope_counts={"big trope": 9},
                   trope_ship=["Big Trope"],
                   trope_conf={"big trope": 0.9})
        out = pp.staging_observe_result(p, st, independent=True)
        self.assertEqual(out["ship_tropes"], ["Big Trope"])
        self.assertEqual(st.data["works"]["w-1"]["claims"], {})

    def test_rejection_filtered_gate_claim_is_never_observed(self):
        st = self._state()
        stub = _StubLearned({pp._trej_key("Shaky Trope"): 2})
        p = _parts(trope_gated=["Shaky Trope"],
                   trope_counts={"shaky trope": 1},
                   trope_ship=[], trope_conf={})
        with mock.patch.object(pp, "_LEARNED", stub):
            out = pp.staging_observe_result(p, st, independent=True)
        # 2 rejections raise the bar to 0.70; formula conf at 1
        # chapter is 0.65 — the hard filter runs BEFORE staging, so
        # the claim is never observed (the run itself still books a
        # completed run for the work, with zero claims).
        self.assertEqual(out["marker"]["staged"], [])
        self.assertEqual(st.data["works"]["w-1"]["claims"], {})

    def test_nonindependent_helper_records_no_sighting(self):
        st = self._state()
        pp.staging_observe_result(_parts(), st, independent=True)
        out = pp.staging_observe_result(_parts(), st, independent=False)
        self.assertEqual(out["ship_tropes"], [])  # still held
        self.assertEqual(out["marker"]["sightings_recorded"], 0)
        entry = st.data["works"]["w-1"]["claims"][
            "trope:name:found family"]
        self.assertEqual(entry["sightings"], [1])


# --- end-to-end through process_file_v2 (fake backend = LLM boundary) ---

_FILLER = ("<p>The valley was quiet and the river ran cold and bright "
           "under a wide northern sky full of slow clouds.</p>")


def _xhtml(body):
    return ("<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.idpf.org/2007/xhtml'><head>"
            "<title>t</title></head>"
            f"<body>{body}</body></html>")


def _make_epub(path, files, title, author, isbn):
    manifest = "\n".join(
        f'    <item id="i{n}" href="{name.split("/", 1)[1]}" '
        'media-type="application/xhtml+xml"/>'
        for n, name in enumerate(files)
        if name != "META-INF/container.xml")
    spine = "\n".join(
        f'    <itemref idref="i{n}"/>' for n, name in enumerate(files)
        if name != "META-INF/container.xml")
    opf = ("""<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='bid'>
  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
    <dc:title>""" + title + """</dc:title>
    <dc:creator>""" + author + """</dc:creator>
    <dc:identifier>""" + isbn + """</dc:identifier>
  </metadata>
  <manifest>
""" + manifest + """
  </manifest>
  <spine>
""" + spine + """
  </spine>
</package>""")
    container = ("""<?xml version='1.0' encoding='utf-8'?>
<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' version='1.0'>
  <rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles>
</container>""")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, content in files.items():
            z.writestr(name, content)


def _staging_book(epub_path):
    def chapter(heading, marker):
        body = f"<h1>{heading}</h1>"
        if marker:
            body += f"<p>{marker}</p>"
        body += ("<p>Mira Solace drew her sword and faced the dawn.</p>"
                 + _FILLER * 22)
        return _xhtml(body)
    files = {
        "OEBPS/ch01.xhtml": chapter("Chapter 1", "FFMARK"),
        "OEBPS/ch02.xhtml": chapter("Chapter 2", "FFMARK"),
        "OEBPS/ch03.xhtml": chapter("Chapter 3", ""),
        "OEBPS/ch04.xhtml": chapter("Chapter 4", ""),
        "OEBPS/ch05.xhtml": chapter("Chapter 5", ""),
    }
    _make_epub(epub_path, files, "Staging Test Book", "Test Author",
               "9780306406157")


class _StagingBackend:
    """Call A candidates Found Family (+ denylisted Science Fiction)
    in chapters 1-2 only: 2 chapters of 5 -> gate band (auto_min 3).
    The trope gate approves Found Family."""

    def __call__(self, prompt, chunk, i, n):
        if prompt == pp.PROMPT_V2_CONTENT:
            cands = (["Found Family", "Science Fiction"]
                     if "FFMARK" in chunk else [])
            return json.dumps({
                "summary": "A chapter summary.", "spice_level": 0,
                "triggers": {}, "trigger_evidence": {},
                "trope_candidates": cands, "quotes": []})
        if "confirming trope candidates" in prompt:
            return json.dumps({"verdicts": [
                {"trope": "Found Family", "verdict": "yes"}]})
        return json.dumps({
            "characters": [{
                "name": "Mira Solace", "role": "protagonist",
                "description": "A swordswoman of the valley.",
                "evidence": "Mira Solace drew her sword."}],
            "relationships": [], "pov_character": "Mira Solace"})


class TestStagingE2E(unittest.TestCase):
    def _epub_for(self, stem):
        # Stable path per stem: the checkpoint key derives from the
        # file path, so a second run of the same stem must reuse the
        # same epub for --resume to find run 1's checkpoints.
        cache = getattr(self, "_epub_cache", None)
        if cache is None:
            cache = self._epub_cache = {}
        if stem not in cache:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            epub = str(Path(tmp.name) / f"{stem}.epub")
            _staging_book(epub)
            cache[stem] = epub
        return cache[stem]

    def _run(self, stem, extra_config, state, resolve=None):
        epub = self._epub_for(stem)
        preview_json = pp.SCRIPT_DIR / "preview" / f"{stem}.json"
        preview_md = pp.SCRIPT_DIR / "preview" / f"{stem}.md"
        self.addCleanup(lambda: preview_json.unlink(missing_ok=True))
        self.addCleanup(lambda: preview_md.unlink(missing_ok=True))
        self.addCleanup(lambda: shutil.rmtree(
            pp._checkpoint_dir(pp._checkpoint_book_key(Path(epub))),
            ignore_errors=True))
        config = {"llm": "ollama"}
        config.update(extra_config)
        resolve = resolve or (lambda *a, **k: (None, "test"))
        old_state = pp._STAGING_STATE
        pp._STAGING_STATE = state
        self.addCleanup(lambda: setattr(pp, "_STAGING_STATE", old_state))
        with mock.patch.object(pp, "llm_ollama", _StagingBackend()), \
                mock.patch.object(pp, "_lookup_series_openlibrary",
                                  lambda *a, **k: (None, None)), \
                mock.patch.object(pp, "resolve_work", resolve), \
                mock.patch.dict(pp.CONFIG, config):
            ok = pp.process_file_v2(Path(epub), preview=True)
        self.assertTrue(ok)
        result = json.loads(preview_json.read_text(encoding="utf-8"))
        md = preview_md.read_text(encoding="utf-8")
        return result, md

    def _tmp_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pp.StagingState(Path(tmp.name))

    def test_flag_off_no_marker_no_store_no_md_line(self):
        st = self._tmp_state()
        result, md = self._run("stageoff", {}, st)
        self.assertIn("Found Family", result["tropes"])
        self.assertNotIn("staging", result)
        self.assertNotIn("Staging:", md)
        self.assertFalse((st.dir / "staging.json").exists())

    def test_flag_on_stages_then_promotes(self):
        st = self._tmp_state()
        cfg = {"staging_gate": True}
        r1, md1 = self._run("stageon", cfg, st)
        self.assertNotIn("Found Family", r1["tropes"])
        self.assertNotIn("Science Fiction", r1["tropes"])
        self.assertEqual(len(r1["staging"]["staged"]), 1)
        self.assertIn("**Staging:** 1 held back", md1)
        self.assertIn("recorded 1 sighting", md1)
        # The denylisted genre label never reaches the store.
        claims = st.data["works"]["isbn:9780306406157"]["claims"]
        self.assertEqual(list(claims), ["trope:name:found family"])
        r2, md2 = self._run("stageon", cfg, st)
        self.assertIn("Found Family", r2["tropes"])
        self.assertEqual(r2["trope_persistence"],
                         {"found family": {"sightings": 2,
                                           "via": "persistence"}})
        self.assertIn("promoted (via persistence)", md2)
        self.assertEqual(r2["staging"]["promoted"][0]["via"],
                         "persistence")

    def test_resumed_run_does_not_increment_sightings(self):
        st = self._tmp_state()
        cfg = {"staging_gate": True}
        r1, _ = self._run("stageres", cfg, st)
        # A completed run clears its checkpoints (P8), so seed them
        # from run 1's preview chapter_data — exactly what
        # --retry-failed does. Run 2's extraction is then entirely
        # checkpoint payloads: NOT an independent sighting (the -15
        # shape: persistence must not be manufactured from one
        # observation replayed).
        epub = self._epub_for("stageres")
        key = pp._checkpoint_book_key(Path(epub))
        cdata = r1["chapter_data"]
        with mock.patch.dict(pp.CONFIG, cfg):
            pp._checkpoint_save_meta(key, len(cdata["chapters"]))
            pp._checkpoint_save_roster(key, cdata["roster"])
            for k, v in cdata["chapters"].items():
                pp._checkpoint_save_chapter(key, int(k), v.get("a"),
                                            v.get("b"))
        r2, _ = self._run(
            "stageres", {**cfg, "resume": True, "resume_force": True},
            st)
        entry = st.data["works"]["isbn:9780306406157"]["claims"][
            "trope:name:found family"]
        self.assertEqual(entry["sightings"], [1])
        self.assertEqual(r2["staging"]["sightings_recorded"], 0)
        self.assertNotIn("Found Family", r2["tropes"])

    def test_adoption_into_resolved_work_id(self):
        st = self._tmp_state()
        cfg = {"staging_gate": True}
        self._run("stageadopt", cfg, st)  # unresolved: isbn key
        r2, _ = self._run(
            "stageadopt", cfg, st,
            resolve=lambda *a, **k: ("work-xyz", "test"))
        self.assertIn("work-xyz", st.data["works"])
        self.assertNotIn("isbn:9780306406157", st.data["works"])
        # The adopted sighting shifted, so run 2's sighting is the
        # second: the claim promoted and shipped in run 2.
        self.assertIn("Found Family", r2["tropes"])
        w = st.data["works"]["work-xyz"]
        self.assertEqual(len(w["adoptions"]), 1)
        _assert_accounting_closes(self, st, "work-xyz")


if __name__ == "__main__":
    unittest.main()
