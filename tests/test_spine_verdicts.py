"""Measurement spine, slice 5: per-work verdict read-back + two-net
suppression (spine-design §3.5, amendment A3, OC-3).

Pins: read-back is verdict-exact (curated statuses only), read-only
(zero writes to Supabase), and fail-closed in the cmd_verify_preview
posture — a FAILED read is not an empty set: it falls back to the
cache and says so with the cache's age, or suppresses nothing and
says that. Suppression is TWO nets: net A filters at emission, net
B drops a promoted re-ship inside the staging seam (the A3 pin:
staged in run N, rejected before run N+1, does NOT ship in N+1).
The counter counts the union of both nets, one row per claim.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

WID = "a42e27eb-f5c4-4082-be90-31ef9c6183e7"
TROPE_ID = "11111111-2222-3333-4444-555555555555"
KEY = f"trope:id:{TROPE_ID}"


class _SbStub:
    def __init__(self, test):
        self.test = test
        self.calls = []

    def __call__(self, table, method="GET", data=None, params="",
                 prefer=None):
        self.calls.append(method)
        if self.test.fail:
            return None
        if table == "book_trope_claims":
            return [{"trope_id": TROPE_ID, "status": "rejected"},
                    {"trope_id": "t-conf", "status": "confirmed"}]
        if table == "book_trigger_claims":
            return [{"warning": "Graphic Violence",
                     "status": "rejected"}]
        return []


class _Base(unittest.TestCase):
    fail = False

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self._saved_sb = pp.sb
        self.stub = _SbStub(self)
        pp.sb = self.stub
        self._saved_cfg = dict(pp.CONFIG)
        pp.CONFIG["supabase_url"] = "https://example.invalid"
        pp.CONFIG["supabase_key"] = "k"

    def tearDown(self):
        pp.sb = self._saved_sb
        pp.CONFIG.clear()
        pp.CONFIG.update(self._saved_cfg)
        pp._close_state_dbs()
        self._td.cleanup()


class TestFetch(_Base):
    def test_fetch_builds_staging_currency_keys(self):
        vs = pp.fetch_work_verdicts(WID)
        self.assertIsNotNone(vs)
        self.assertEqual(vs.rejected_keys(),
                         {KEY, "trigger:graphic violence"})
        self.assertEqual(vs.confirmed_keys(), {"trope:id:t-conf"})

    def test_fetch_is_read_only(self):
        pp.fetch_work_verdicts(WID)
        self.assertTrue(self.stub.calls)
        self.assertEqual(set(self.stub.calls), {"GET"})

    def test_failed_fetch_is_not_an_empty_set(self):
        self.fail = True
        self.assertIsNone(pp.fetch_work_verdicts(WID))


class TestCacheFallback(_Base):
    def test_fetch_caches_and_logs(self):
        pp.StateDB.create(self.dir)
        vs, note = pp.work_verdicts(WID, self.dir)
        self.assertIsNotNone(vs)
        db = pp._state_db_for(self.dir)
        rows = db.query("SELECT claim_key, verdict FROM "
                        "verdicts_cache WHERE work_id=?", (WID,))
        self.assertEqual(len(rows), 3)
        logs = db.query("SELECT ok, claims FROM verdict_fetches")
        self.assertEqual(logs, [(1, 3)])

    def test_failed_fetch_falls_back_to_cache_and_says_so(self):
        pp.StateDB.create(self.dir)
        pp.work_verdicts(WID, self.dir)  # populate the cache
        self.fail = True
        vs, note = pp.work_verdicts(WID, self.dir)
        self.assertIsNotNone(vs)
        self.assertTrue(vs.from_cache)
        self.assertEqual(vs.rejected_keys(),
                         {KEY, "trigger:graphic violence"})
        self.assertIn("cached verdicts from", note)

    def test_failed_fetch_no_cache_suppresses_nothing(self):
        pp.StateDB.create(self.dir)
        self.fail = True
        vs, note = pp.work_verdicts(WID, self.dir)
        self.assertIsNone(vs)
        self.assertIn("nothing suppressed", note)


class TestNetA(unittest.TestCase):
    def test_emission_filter_drops_rejected_everywhere(self):
        tropes = ["Found Family", "Enemies to Lovers"]
        conf = {pp._tnorm("Found Family"): 0.85,
                pp._tnorm("Enemies to Lovers"): 0.9}
        triggers = [{"warning": "Graphic Violence"},
                    {"warning": "Death"}]
        cat = {pp._tnorm("Enemies to Lovers"): TROPE_ID}
        out_t, out_c, out_tr, keys, names = \
            pp.apply_verdict_suppression(
                tropes, conf, triggers,
                {KEY, "trigger:graphic violence"}, cat)
        self.assertEqual(out_t, ["Found Family"])
        self.assertEqual(list(out_c), [pp._tnorm("Found Family")])
        self.assertEqual([t["warning"] for t in out_tr],
                         ["Death"])
        self.assertEqual(keys, {KEY, "trigger:graphic violence"})
        self.assertEqual(set(names),
                         {"Enemies to Lovers", "Graphic Violence"})


def _parts(ship):
    return {"work_key": "wk", "work_id": WID, "title": "T",
            "author": "A", "isbn": None, "auto_min": 3,
            "trope_auto": [], "trope_gated": ["Found Family"],
            "trope_counts": {pp._tnorm("Found Family"): 1},
            "trope_ship": ship,
            "trope_conf": {pp._tnorm("Found Family"): 0.75},
            "catalog_map": {pp._tnorm("Found Family"): TROPE_ID},
            "triggers": []}


class TestNetB(unittest.TestCase):
    """The A3 pin: staged -> rejected -> no ship."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        pp._close_state_dbs()
        self._td.cleanup()

    def test_promoted_reship_is_suppressed(self):
        st = pp.StagingState(learn_dir=self.dir, enabled=True)
        out1 = pp.staging_observe_result(
            _parts(["Found Family"]), st, independent=True)
        self.assertEqual(out1["ship_tropes"], [])  # staged, held
        # Sanity: without a rejection the second sighting promotes
        # and ships.
        st2 = pp.StagingState(learn_dir=self.dir, enabled=True)
        out2 = pp.staging_observe_result(
            _parts(["Found Family"]), st2, independent=True)
        self.assertEqual(out2["ship_tropes"], ["Found Family"])
        # Fresh store, same two runs, rejection lands before run 2.
        td2 = tempfile.TemporaryDirectory()
        self.addCleanup(td2.cleanup)
        st3 = pp.StagingState(learn_dir=Path(td2.name), enabled=True)
        pp.staging_observe_result(_parts(["Found Family"]), st3,
                                  independent=True)
        parts = _parts(["Found Family"])
        parts["verdict_rejected"] = {KEY}
        out3 = pp.staging_observe_result(parts, st3,
                                         independent=True)
        self.assertEqual(out3["ship_tropes"], [])
        self.assertEqual(out3["suppressed_keys"], {KEY})
        self.assertEqual(out3["suppressed_names"], ["Found Family"])


class TestCounter(_Base):
    def test_rejection_events_one_row_per_key(self):
        pp.StateDB.create(self.dir)
        pp.record_rejection_events(self.dir, "run-1", "wk",
                                   {KEY, "trigger:graphic violence"})
        db = pp._state_db_for(self.dir)
        rows = db.query("SELECT claim_key FROM rejection_events "
                        "ORDER BY claim_key")
        self.assertEqual([r[0] for r in rows],
                         ["trigger:graphic violence", KEY])


if __name__ == "__main__":
    unittest.main()
