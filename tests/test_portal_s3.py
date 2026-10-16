"""Operator portal, slice S3 (PORTAL-SPEC.md §10): the Desk.

Pins: the §5.2 mapping as ruled in Q4 (batch = staged claims
ordered by sightings, then chapter support, inside one round's
window; parked = uncertainty-queue items + staged claims beyond
the window), the claim evidence view (the full sightings history
from the store, plus the shelled --explain claim report, A7),
impulse proposals from the LOCAL trope-map review files only
(Q5), the GAP-4 surface (staged claims state plainly that no
ruling verb exists — no dead controls), and the A6 round trip:
a ruling from the Desk writes the CLI's labels file, the real
CLI consumes it through the one-writer queue, and the item
leaves the queue.
"""
import hashlib
import importlib.util
import io
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, _ROOT / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


portal = _load("portal", "portal.py")
pp = _load("process_portable_for_portal_s3_tests", "process-portable.py")

_WK = "daemon-wk"
_WK2 = "other-wk"


def _ro_dump_hash(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return hashlib.sha256(
            "\n".join(conn.iterdump()).encode("utf-8")).hexdigest()
    finally:
        conn.close()


def _claims_rows():
    """26 staged claims on the main work (sightings 26..1, so the
    batch order is fully determined), 1 staged claim on a second
    work, 2 promoted claims that must never appear on the Desk."""
    rows = []
    for i in range(26):
        n = 26 - i
        rows.append((
            _WK, f"trope:c{i:02d}", "trope", f"Claim {i:02d}",
            "staged", None, None, n, 1, "2026-10-01", "2026-10-15",
            json.dumps(list(range(1, n + 1))),
            json.dumps([f"Claim {i:02d}"]),
            json.dumps({"key": f"trope:c{i:02d}"})))
    rows.append((
        _WK2, "trope:other", "trope", "Other Work Claim", "staged",
        None, None, 3, 0, "2026-10-02", "2026-10-12",
        json.dumps([1, 2]), json.dumps(["Other Work Claim"]),
        json.dumps({"key": "trope:other"})))
    for j in range(2):
        rows.append((
            _WK, f"trope:p{j}", "trope", f"Promoted {j}", "promoted",
            4, "recurrence", 9, 1, "2026-09-01", "2026-10-01",
            json.dumps([1, 2, 3, 4]), json.dumps([f"Promoted {j}"]),
            json.dumps({"key": f"trope:p{j}"})))
    return rows


_PACKETS = {
    "merge": {
        "kind": "merge_pair", "name_a": "Peter Sebeck",
        "name_b": "Detective Peter Sebeck", "p_yes": 0.81,
        "reason": "same surname, same beat",
        "chapters_both": [3, 9],
        "desc_a": "A detective.", "desc_b": "The detective.",
        "evidence_a": "", "evidence_b": "", "book": "Daemon",
        "provenance": {"book": "Daemon", "work_key": _WK}},
    "family": {
        "kind": "family_claim", "from": "Ava Sebeck",
        "to": "Peter Sebeck", "type": "sibling_of",
        "judge_p": 0.42, "note": "judge unsure",
        "evidence": "Ava called Peter her brother.",
        "evidence_grade": "mined",
        "provenance": {"book": "Daemon", "work_key": _WK}},
    "claim": {
        "kind": "claim", "key": "trope:c00",
        "display": "Claim 00", "via": "recurrence",
        "provenance": {"book": "Daemon", "work_key": _WK}},
    "split": {
        "kind": "split_suspect", "work_key": _WK,
        "a": "Loki", "b": "The Voice", "reason": "same chapters",
        "first_run": 1, "runs_completed": 3},
}


def _fixture_learn_dir(root):
    """A learned dir whose state.db carries the Desk's two
    stores, built from the processor's own DDL. No corpus.db:
    the Desk is a state.db surface."""
    d = Path(root) / "learned"
    d.mkdir(parents=True)
    sdb = sqlite3.connect(d / "state.db")
    for ddl in pp._SUBSTRATE_DDL:
        sdb.execute(ddl)
    for wk, wid, title in ((_WK, "wid-a", "Daemon"),
                           (_WK2, "wid-b", "Other Book")):
        sdb.execute(
            "INSERT INTO staging_works (work_key, work_id, title,"
            " author, runs_completed, staged_ever, payload) VALUES"
            " (?,?,?, 'Daniel Suarez', 3, 27, ?)",
            (wk, wid, title, json.dumps({"work_key": wk})))
    sdb.executemany(
        "INSERT INTO staging_claims (work_key, claim_key, class,"
        " display, state, promoted_run, promoted_via,"
        " best_chapters, evidence_ever, first_seen, last_seen,"
        " sightings, name_forms, payload) VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _claims_rows())
    queue = [
        ("near-threshold", "merge: Peter Sebeck ~ Detective "
         "Peter Sebeck", _PACKETS["merge"], "pending"),
        ("tier-judge-disagreement", "family: Ava Sebeck — Peter "
         "Sebeck (sibling_of)", _PACKETS["family"], "pending"),
        ("form-changed-promotion", "claim: Claim 00",
         _PACKETS["claim"], "pending"),
        ("split-suspect", "split suspect: Loki ~ The Voice ("
         + _WK + ")", _PACKETS["split"], "pending"),
        ("near-threshold", "merge: Old ~ Done",
         _PACKETS["merge"], "labelled"),
        ("control", "family: Decided — Pair (parent_of)",
         _PACKETS["family"], "control"),
    ]
    ids = {}
    for cls, subject, packet, status in queue:
        cur = sdb.execute(
            "INSERT INTO uncertainty_queue (created, run_ref,"
            " work_key, class, subject, packet, status) VALUES"
            " ('2026-10-16T09:00:00', 'run-1', ?, ?, ?, ?, ?)",
            (_WK, cls, subject, json.dumps(packet), status))
        ids[status if status != "pending" else packet["kind"]] = \
            cur.lastrowid
    sdb.commit()
    sdb.close()
    return d, ids


def _fixture_script_dir(root):
    """The repo shape the portal assumes: the real CLI beside it
    (copied, so launches and explains run the shipped code), a
    preview/ with trope-map review files, and an eval/ dir for
    the labels intake's merge-pairs append."""
    d = Path(root) / "repo"
    (d / "preview").mkdir(parents=True)
    (d / "eval").mkdir()
    shutil.copy(_ROOT / "process-portable.py",
                d / "process-portable.py")
    (d / "preview" / "Daemon - Daniel Suarez-trope-map.json"
     ).write_text(json.dumps({
         "preview": "Daemon - Daniel Suarez.json", "threshold": 0.78,
         "mappings": [{"pipeline_trope": "hacker culture",
                       "catalog_id": "t-9", "catalog_name":
                       "Hacker Culture", "similarity": 0.91}],
         "proposals": [
             {"pipeline_trope": "daemon process",
              "chapter_count": 12, "nearest_catalog_id": "t-123",
              "nearest_similarity": 0.61, "book": "Daemon",
              "isbn": "9781101007518"},
             {"pipeline_trope": "darknet uprising",
              "chapter_count": 4, "nearest_catalog_id": None,
              "nearest_similarity": 0.22, "book": "Daemon",
              "isbn": "9781101007518"}]}), encoding="utf-8")
    (d / "preview" / "Broken-trope-map.json").write_text(
        "{not json", encoding="utf-8")
    (d / "preview" / "Daemon - Daniel Suarez.json").write_text(
        json.dumps({"title": "Daemon"}), encoding="utf-8")
    return d


class _DeskCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.learn, self.ids = _fixture_learn_dir(self._td.name)
        self.script = _fixture_script_dir(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def desk(self):
        return portal.desk_data(self.learn, self.script)


class TestDeskData(_DeskCase):
    def test_batch_is_the_window_ordered_by_sightings_then_chapters(self):
        desk = self.desk()
        self.assertTrue(desk["present"])
        self.assertEqual(len(desk["batch"]), portal._DESK_BATCH_WINDOW)
        counts = [c["sightings_count"] for c in desk["batch"]]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertEqual(desk["batch"][0]["display"], "Claim 00")
        self.assertEqual(desk["batch"][0]["sightings_count"], 26)
        self.assertEqual(desk["batch"][0]["work_title"], "Daemon")

    def test_parked_claims_are_staged_beyond_the_window(self):
        desk = self.desk()
        parked = desk["parked_claims"]
        self.assertEqual(len(parked), 7)
        counts = [c["sightings_count"] for c in parked]
        self.assertTrue(all(n <= 6 for n in counts))
        keys = {c["key"] for c in parked}
        self.assertIn("trope:other", keys)

    def test_promoted_claims_never_appear(self):
        desk = self.desk()
        shown = {c["key"] for c in desk["batch"]}
        shown |= {c["key"] for c in desk["parked_claims"]}
        self.assertNotIn("trope:p0", shown)
        self.assertNotIn("trope:p1", shown)

    def test_parked_items_are_pending_only_with_labels_by_kind(self):
        desk = self.desk()
        items = desk["parked_items"]
        self.assertEqual(len(items), 4)
        by_kind = {i["kind"]: i for i in items}
        self.assertEqual(by_kind["merge_pair"]["labels"],
                         ["same", "different"])
        self.assertEqual(by_kind["family_claim"]["labels"],
                         ["family", "not-family"])
        self.assertEqual(by_kind["claim"]["labels"],
                         ["correct", "incorrect"])
        self.assertEqual(by_kind["split_suspect"]["labels"],
                         ["same", "different"])

    def test_proposals_come_from_local_trope_map_files_only(self):
        desk = self.desk()
        self.assertEqual(desk["proposals_files"], 1)
        self.assertEqual(desk["proposals_broken"], 1)
        props = desk["proposals"]
        self.assertEqual(len(props), 2)
        first = props[0]
        self.assertEqual(first["label"], "daemon process")
        self.assertEqual(first["nearest_catalog_id"], "t-123")
        self.assertEqual(first["nearest_similarity"], 0.61)
        self.assertEqual(first["file"],
                         "Daemon - Daniel Suarez-trope-map.json")
        # Mappings are rulings already made elsewhere; the Desk
        # lists proposals only.
        self.assertNotIn("hacker culture",
                         [p["label"] for p in props])

    def test_missing_state_db_degrades_to_stated_absence(self):
        empty = Path(self._td.name) / "nowhere"
        empty.mkdir()
        desk = portal.desk_data(empty, self.script)
        self.assertFalse(desk["present"])
        self.assertEqual(desk["batch"], [])
        self.assertEqual(desk["parked_items"], [])


class TestUncertaintyItem(_DeskCase):
    def test_item_carries_the_full_packet(self):
        item = portal.uncertainty_item(self.learn,
                                       self.ids["merge_pair"])
        self.assertEqual(item["packet"]["name_a"], "Peter Sebeck")
        self.assertEqual(item["class"], "near-threshold")
        self.assertEqual(item["labels"], ["same", "different"])

    def test_labelled_and_control_items_are_not_items(self):
        self.assertIsNone(portal.uncertainty_item(
            self.learn, self.ids["labelled"]))
        self.assertIsNone(portal.uncertainty_item(
            self.learn, self.ids["control"]))
        self.assertIsNone(portal.uncertainty_item(self.learn, 99999))
        self.assertIsNone(portal.uncertainty_item(self.learn, None))


class TestDeskClaim(_DeskCase):
    def test_claim_carries_its_full_sightings_history(self):
        claim = portal.desk_claim(self.learn, _WK, "trope:c00")
        self.assertEqual(claim["sightings"], list(range(1, 27)))
        self.assertEqual(claim["sightings_count"], 26)
        self.assertEqual(claim["best_chapters"], 26)

    def test_unknown_or_promoted_claim_is_none(self):
        self.assertIsNone(portal.desk_claim(self.learn, _WK,
                                            "trope:nope"))
        self.assertIsNone(portal.desk_claim(self.learn, _WK,
                                            "trope:p0"))
        self.assertIsNone(portal.desk_claim(self.learn, "no-wk",
                                            "trope:c00"))


class _ServerCase(_DeskCase):
    def setUp(self):
        super().setUp()
        self.ctx = portal.PortalContext(self.learn, self.script)
        self.server = portal.make_server("127.0.0.1", 0, self.ctx)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        super().tearDown()

    def _req(self, method, path, payload=None, headers=None):
        data = None
        hdrs = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode(
                    "utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def _job_state(self, job_id):
        status, body = self._req("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200, body)
        return json.loads(body)["state"]

    def _wait_job(self, job_id, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._job_state(job_id)
            if state in ("done", "failed", "stopped"):
                return state
            time.sleep(0.1)
        self.fail(f"job {job_id} did not settle")


class TestDeskPage(_ServerCase):
    def test_page_renders_all_three_sections_and_gap4(self):
        status, body = self._req("GET", "/desk")
        self.assertEqual(status, 200)
        self.assertIn("Today's trope batch", body)
        self.assertIn("Parked escalations", body)
        self.assertIn("Impulse proposals", body)
        # GAP-4 is stated on the surface, not hidden (§10 S3).
        self.assertIn("GAP-4", body)
        self.assertIn("no verb to keep or drop a staged claim",
                      body)
        # Q5: the proposals are labelled per-preview, local.
        self.assertIn("per-preview", body)
        self.assertIn("daemon process", body)
        self.assertIn("Claim 00", body)
        self.assertIn("merge: Peter Sebeck", body)

    def test_batch_rows_carry_no_ruling_control(self):
        # The ruling affordance exists only inside the evidence
        # view's script (for uncertainty items). The static page
        # must not draw a rule button against a claim row.
        status, body = self._req("GET", "/desk")
        self.assertEqual(status, 200)
        self.assertNotIn("data-rule-claim", body)
        desk = self.desk()
        for claim in desk["batch"] + desk["parked_claims"]:
            self.assertNotIn("labels", claim)

    def test_reads_leave_state_db_untouched(self):
        before = _ro_dump_hash(self.learn / "state.db")
        for path in ("/desk", "/api/desk",
                     f"/api/desk/item?id={self.ids['merge_pair']}",
                     "/api/desk/claim?work_key=" + _WK
                     + "&claim_key=trope:c00"):
            status, _body = self._req("GET", path)
            self.assertEqual(status, 200, path)
        self.assertEqual(_ro_dump_hash(self.learn / "state.db"),
                         before)
        # The claim evidence shells --explain report-mode; no
        # corpus.db may appear in a learn dir that lacks one.
        self.assertFalse((self.learn / "corpus.db").exists())


class TestClaimEvidence(_ServerCase):
    def test_evidence_opens_with_full_sightings_and_explain_text(self):
        status, body = self._req(
            "GET", "/api/desk/claim?work_key=" + _WK
            + "&claim_key=trope:c00")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["claim"]["sightings"],
                         list(range(1, 27)))
        # The shelled A7 report names the claim and its count.
        self.assertIn("Claim 00", data["text"])
        self.assertIn("26 sightings", data["text"])

    def test_unknown_claim_is_404(self):
        status, _body = self._req(
            "GET", "/api/desk/claim?work_key=" + _WK
            + "&claim_key=trope:nope")
        self.assertEqual(status, 404)


class TestItemEvidence(_ServerCase):
    def test_item_endpoint_returns_packet_and_labels(self):
        status, body = self._req(
            "GET", f"/api/desk/item?id={self.ids['merge_pair']}")
        self.assertEqual(status, 200)
        item = json.loads(body)["item"]
        self.assertEqual(item["packet"]["name_b"],
                         "Detective Peter Sebeck")
        self.assertEqual(item["labels"], ["same", "different"])

    def test_non_pending_or_garbage_id_is_404(self):
        for raw in (str(self.ids["labelled"]),
                    str(self.ids["control"]), "99999", "abc", ""):
            status, _body = self._req(
                "GET", f"/api/desk/item?id={raw}")
            self.assertEqual(status, 404, raw)


class TestRulingRoundTrip(_ServerCase):
    def test_rule_from_the_desk_is_consumed_by_the_cli(self):
        qid = self.ids["merge_pair"]
        status, body = self._req(
            "POST", "/api/desk/rule",
            {"queue_id": qid, "label": "same"})
        self.assertEqual(status, 200, body)
        job_id = json.loads(body)["job_id"]
        self.assertEqual(self._wait_job(job_id), "done")
        # The CLI consumed the labels file: the row is labelled,
        # by the portal's labeller, with the recorded verdict.
        conn = sqlite3.connect(self.learn / "state.db")
        try:
            row = conn.execute(
                "SELECT status, label, labelled_by FROM "
                "uncertainty_queue WHERE id=?", (qid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("labelled", "same", "user"))
        # ...and the item has left the Desk's queue.
        self.assertIsNone(portal.uncertainty_item(self.learn, qid))
        desk = self.desk()
        self.assertNotIn(qid, [i["id"] for i in desk["parked_items"]])
        # The labels file the portal wrote is the CLI's contract,
        # verbatim, in the portal's own scratch dir.
        files = list((self.script / "portal-cache" / "labels"
                      ).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(
            json.loads(files[0].read_text(encoding="utf-8")),
            [{"queue_id": qid, "label": "same", "labeller": "user"}])
        # The merge pair flowed into the harness fixture, the
        # CLI's own downstream for same/different labels.
        pairs = json.loads((self.script / "eval" / "merge_pairs.json"
                            ).read_text(encoding="utf-8"))
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["name_a"], "Peter Sebeck")
        self.assertEqual(pairs[0]["label"], "same")

    def test_family_ruling_records_the_label(self):
        qid = self.ids["family_claim"]
        status, body = self._req(
            "POST", "/api/desk/rule",
            {"queue_id": qid, "label": "not-family"})
        self.assertEqual(status, 200, body)
        job_id = json.loads(body)["job_id"]
        self.assertEqual(self._wait_job(job_id), "done")
        conn = sqlite3.connect(self.learn / "state.db")
        try:
            row = conn.execute(
                "SELECT status, label FROM uncertainty_queue "
                "WHERE id=?", (qid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("labelled", "not-family"))


class TestRulingValidation(_ServerCase):
    def _assert_still_pending(self, qid):
        self.assertIsNotNone(portal.uncertainty_item(self.learn, qid))
        status, body = self._req("GET", "/api/jobs")
        self.assertEqual(json.loads(body)["jobs"], [])

    def test_label_outside_the_kind_vocabulary_is_400(self):
        qid = self.ids["family_claim"]
        status, _body = self._req(
            "POST", "/api/desk/rule",
            {"queue_id": qid, "label": "same"})
        self.assertEqual(status, 400)
        self._assert_still_pending(qid)

    def test_unknown_and_non_pending_ids_are_400(self):
        for qid in (99999, self.ids["labelled"], self.ids["control"]):
            status, _body = self._req(
                "POST", "/api/desk/rule",
                {"queue_id": qid, "label": "same"})
            self.assertEqual(status, 400, qid)

    def test_malformed_bodies_are_400(self):
        for payload in ({"queue_id": "x", "label": "same"},
                        {"queue_id": self.ids["merge_pair"]},
                        {"label": "same"}, {}):
            status, _body = self._req("POST", "/api/desk/rule",
                                      payload)
            self.assertEqual(status, 400, payload)
        self._assert_still_pending(self.ids["merge_pair"])

    def test_coerced_queue_ids_are_400_not_truncated(self):
        # A float or boolean id must be refused outright: int()
        # would silently truncate it into a neighbour item's id
        # and rule THAT item (Advisor F3).
        for payload in ({"queue_id": 1.9, "label": "same"},
                        {"queue_id": True, "label": "same"}):
            status, _body = self._req("POST", "/api/desk/rule",
                                      payload)
            self.assertEqual(status, 400, payload)
        self._assert_still_pending(self.ids["merge_pair"])

    def test_foreign_origin_rule_is_403_and_launches_nothing(self):
        qid = self.ids["merge_pair"]
        status, _body = self._req(
            "POST", "/api/desk/rule",
            {"queue_id": qid, "label": "same"},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self._assert_still_pending(qid)

    def test_form_encoded_rule_is_400(self):
        # The S2 CSRF companion control (JSON-only POSTs) covers
        # the Desk's ruling route too.
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/desk/rule",
            data=b"queue_id=1&label=same",
            headers={"Content-Type":
                     "application/x-www-form-urlencoded"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 400)
        self._assert_still_pending(self.ids["merge_pair"])


class _BlockingProc:
    """A Popen stand-in whose wait() blocks until release() —
    the in-flight tests need a ruling job that provably has not
    settled while a second ruling is attempted."""

    def __init__(self, argv, lines=(), rc=0):
        self.argv = argv
        self.stdout = io.StringIO(
            "".join(l + "\n" for l in lines))
        self._rc = rc
        self._released = threading.Event()
        self.signals = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._rc if self._released.is_set() else None

    def wait(self, timeout=None):
        self._released.wait(timeout)
        return self._rc

    def release(self, rc=None):
        if rc is not None:
            self._rc = rc
        self._released.set()

    def send_signal(self, sig):
        self.signals.append(sig)
        self.release(-int(sig) if isinstance(sig, int) else 3)

    def terminate(self):
        self.terminated = True
        self.release(3)

    def kill(self):
        self.killed = True
        self.release(9)


class TestRulingInFlight(_DeskCase):
    """Advisor F1: the queue row stays 'pending' until its apply
    job runs, and the CLI intake applies whatever it is fed — so
    the Desk itself must refuse a second ruling for an item
    whose first ruling is still queued or running."""

    def setUp(self):
        super().setUp()
        self.procs = []

        def factory(argv, **kwargs):
            proc = _BlockingProc(argv)
            self.procs.append(proc)
            return proc

        self.ctx = portal.PortalContext(
            self.learn, self.script, popen_factory=factory)

    def _settle_all(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for proc in self.procs:
                proc.release()
            states = [j.state for j in self.ctx.registry.jobs()]
            if all(s in ("done", "failed", "stopped")
                   for s in states):
                return
            time.sleep(0.05)
        self.fail("jobs did not settle")

    def test_second_ruling_refused_while_first_in_flight(self):
        qid = self.ids["merge_pair"]
        job = self.ctx.rule(qid, "same")
        self.assertEqual(job.state, "running")
        with self.assertRaises(portal.PortalError):
            self.ctx.rule(qid, "different")
        # A ruling for a DIFFERENT item is not blocked — it
        # queues behind the running writer like any launch.
        other = self.ctx.rule(self.ids["family_claim"], "family")
        self.assertEqual(other.state, "queued")
        with self.assertRaises(portal.PortalError):
            self.ctx.rule(self.ids["family_claim"], "not-family")
        self._settle_all()
        # Once settled, the slot frees: the stub child changed
        # nothing in the store, so the item is still pending and
        # a fresh ruling is legitimate (a failed job must not
        # lock the item forever).
        again = self.ctx.rule(qid, "different")
        self.assertIn(again.state, ("queued", "running"))
        self._settle_all()


class TestDeskEscapesUntrustedFields(_ServerCase):
    """Advisor F2: the trope-map files are untrusted data, and
    staging columns are written by book-driven runs — the
    numeric-looking cells are escaped like every other cell."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.learn / "state.db")
        conn.execute(
            "INSERT INTO staging_claims (work_key, claim_key,"
            " class, display, state, best_chapters, sightings,"
            " name_forms, payload) VALUES (?, 'trope:evil',"
            " 'trope', 'Evil Claim', 'staged', '<b>9</b>', '[]',"
            " '[]', '{}')", (_WK,))
        conn.commit()
        conn.close()
        (self.script / "preview" / "Evil-trope-map.json"
         ).write_text(json.dumps({
             "preview": "Evil.json", "threshold": 0.78,
             "mappings": [],
             "proposals": [{
                 "pipeline_trope": "evil trope",
                 "chapter_count": "<i>3</i>",
                 "nearest_catalog_id": "t-x",
                 "nearest_similarity":
                     "<script>alert(1)</script>",
                 "book": "Daemon", "isbn": "x"}]}),
            encoding="utf-8")

    def test_hostile_numeric_fields_render_escaped(self):
        status, body = self._req("GET", "/desk")
        self.assertEqual(status, 200)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)", body)
        self.assertNotIn("<b>9</b>", body)
        self.assertIn("&lt;b&gt;9&lt;/b&gt;", body)
        self.assertNotIn("<i>3</i>", body)
        self.assertIn("&lt;i&gt;3&lt;/i&gt;", body)


if __name__ == "__main__":
    unittest.main()
