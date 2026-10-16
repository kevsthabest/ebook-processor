"""Operator portal, slice S1 (PORTAL-SPEC.md §10): skeleton + Home.

Pins: strict loopback bind validation, the Q11 port fallback,
the read-model mirrors of the CLI's own status queries (totals,
coverage, generations, byte breakdown), the coverage-card counts
(Q8: counts by status, no denominator), the queue pointers, the
recent-runs composite, the config whitelist (secrets never reach
the browser), and the S1 acceptance shape — a browsing session
issues no writes (both stores' logical dump hashes unchanged).
"""
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, _ROOT / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


portal = _load("portal", "portal.py")
pp = _load("process_portable_for_portal_tests", "process-portable.py")

_MODEL = "fake-embed-v1"
_FMT = "raw-f32-v1"
_FP1 = "a" * 64
_FP2 = "b" * 64
_FP3 = "c" * 64


def _ro_dump_hash(path):
    """Logical content hash of a SQLite file, read-only — the
    same notion as the substrate's dump_hash (content, not file
    bytes), so WAL checkpoint noise cannot fake a change."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return hashlib.sha256(
            "\n".join(conn.iterdump()).encode("utf-8")).hexdigest()
    finally:
        conn.close()


def _fixture_learn_dir(root):
    """A learned dir with both stores, built from the processor's
    own DDL so the fixture can never drift from the real schema."""
    d = Path(root) / "learned"
    d.mkdir(parents=True)
    cdb = sqlite3.connect(d / "corpus.db")
    for ddl in pp._CORPUS_DDL:
        cdb.execute(ddl)
    works = [
        (_FP1, "Daemon", "Daniel Suarez", "Daemon", 1.0, "complete", "1", 73),
        (_FP2, "Fourth Wing", "Rebecca Yarros", None, None,
         "vectors_partial", "1", 115),
        (_FP3, "Freedom", "Daniel Suarez", "Daemon", 2.0,
         "text_complete", "0", 61),
    ]
    for fp, title, author, series, sidx, status, pv, nch in works:
        cdb.execute(
            "INSERT INTO corpus_works (fingerprint, title, author, "
            "series, series_index, status, parser_version, "
            "chapter_count, ingested_at, updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            (fp, title, author, series, sidx, status, pv, nch,
             "2026-10-16T09:00:00", "2026-10-16T09:00:00"))
    pid = 0
    vec_counts = {_FP1: 4, _FP2: 2, _FP3: 0}
    pass_counts = {_FP1: 4, _FP2: 4, _FP3: 2}
    for fp, n in pass_counts.items():
        for i in range(n):
            pid += 1
            cdb.execute(
                "INSERT INTO corpus_passages (passage_id, fingerprint,"
                " chapter_index, start_offset, end_offset, text_hash)"
                " VALUES (?,?,?,?,?,?)",
                (f"p{pid}", fp, i, 0, 100, f"h{pid}"))
            if i < vec_counts[fp]:
                cdb.execute(
                    "INSERT INTO passage_vectors (passage_id, "
                    "fingerprint, model, format, dim, vector) VALUES "
                    "(?,?,?,?,?,?)",
                    (f"p{pid}", fp, _MODEL, _FMT, 8, b"\x00" * 32))
    cdb.execute(
        "INSERT INTO meta (k, v) VALUES ('generation_current', ?)",
        (json.dumps({"model": _MODEL, "format": _FMT}),))
    cdb.execute(
        "INSERT INTO meta (k, v) VALUES (?, ?)",
        (f"generation:{_MODEL}|{_FMT}",
         json.dumps({"model": _MODEL, "format": _FMT, "dim": 8,
                     "canary": "ok", "born": "2026-10-16T08:00:00",
                     "checked": "2026-10-16T08:00:00"})))
    for qid, fp, status in [("q1", _FP1, "verified"),
                            ("q2", _FP1, "verified"),
                            ("q3", _FP1, "selected"),
                            ("q4", _FP2, "candidate")]:
        cdb.execute(
            "INSERT INTO quotes (quote_id, fingerprint, chapter_index,"
            " start_offset, end_offset, text, kind, status) VALUES "
            "(?,?,0,0,10,'x','opening',?)", (qid, fp, status))
    envelopes = {
        _FP1: {"sections": {
            "text": {"state": "present", "stage": "ingest"},
            "vectors": {"state": "present", "stage": "embed"},
            "quotes": {"state": "present", "stage": "quotes"},
            "ending": {"state": "present", "stage": "ending"},
            "triggers": {"state": "present", "stage": "triggers"}}},
        _FP2: {"sections": {
            "text": {"state": "present", "stage": "ingest"},
            "vectors": {"state": "present", "stage": "embed"}}},
        _FP3: {"sections": {
            "text": {"state": "present", "stage": "ingest"}}},
    }
    for fp, env in envelopes.items():
        cdb.execute(
            "INSERT INTO work_envelope (fingerprint, corpus_v, "
            "envelope, computed_at) VALUES (?, '1', ?, "
            "'2026-10-16T09:05:00')", (fp, json.dumps(env)))
    cdb.execute(
        "INSERT INTO ingest_runs (fingerprint, started, finished, "
        "parser_version, embed_model, outcome, detail) VALUES "
        "(?, '2026-10-16T08:59:00', '2026-10-16T09:00:00', '1', ?, "
        "'complete', ?)",
        (_FP1, _MODEL, json.dumps({"source_name": "daemon.epub"})))
    cdb.execute(
        "INSERT INTO ingest_runs (fingerprint, started, finished, "
        "parser_version, embed_model, outcome, detail) VALUES "
        "(?, '2026-10-16T09:10:00', '2026-10-16T09:11:00', '1', ?, "
        "'partial', ?)",
        (_FP2, _MODEL, json.dumps({"kind": "reembed"})))
    cdb.execute(
        "INSERT INTO stage_runs (run_id, fingerprint, stage, "
        "stage_version, model, started, finished, status) VALUES "
        "('r1', ?, 'retrieve', '1', 'judge', '2026-10-16T09:20:00', "
        "'2026-10-16T09:21:00', 'settled')", (_FP1,))
    cdb.commit()
    cdb.close()

    sdb = sqlite3.connect(d / "state.db")
    for ddl in pp._SUBSTRATE_DDL:
        sdb.execute(ddl)
    for i, state in enumerate(["staged"] * 3 + ["promoted"] * 2):
        sdb.execute(
            "INSERT INTO staging_claims (work_key, claim_key, class, "
            "display, state) VALUES (?, ?, 'trope', 'x', ?)",
            (f"w{i}", f"c{i}", state))
    for i, status in enumerate(["pending"] * 4 + ["labelled", "control"]):
        sdb.execute(
            "INSERT INTO uncertainty_queue (created, work_key, class,"
            " subject, status) VALUES ('2026-10-16T09:00:00', 'w', "
            "'near-threshold', 's', ?)", (status,))
    sdb.commit()
    sdb.close()
    return d


def _fixture_script_dir(root):
    d = Path(root) / "repo"
    (d / "preview").mkdir(parents=True)
    # The portal's standing assumption is that it sits beside
    # process-portable.py (versions and the parser constant are
    # read from its source); the fixture honours that shape with
    # a copy rather than teaching home_data two script dirs.
    import shutil
    shutil.copy(_ROOT / "process-portable.py", d / "process-portable.py")
    (d / "preview" / "demo.json").write_text(json.dumps({
        "title": "Demo Book", "pipeline": "v2",
        "tropes": [{}, {}], "triggers": [{}],
        "characters": [{}, {}, {}], "chunks_failed": 0}),
        encoding="utf-8")
    (d / "preview" / "broken.json").write_text("{not json",
                                              encoding="utf-8")
    return d


class TestBindValidation(unittest.TestCase):
    def test_loopback_spellings_accepted(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertEqual(portal.validate_bind_host(host), host)

    def test_anything_else_refused(self):
        for host in ("0.0.0.0", "192.168.1.5", "", "example.com", "::"):
            with self.assertRaises(portal.PortalError):
                portal.validate_bind_host(host)


class TestHostAllowed(unittest.TestCase):
    def test_own_names_with_the_bound_port(self):
        self.assertTrue(portal._host_allowed("127.0.0.1:8765", 8765))
        self.assertTrue(portal._host_allowed("localhost:8765", 8765))
        self.assertTrue(portal._host_allowed("[::1]:8765", 8765))
        self.assertTrue(portal._host_allowed("127.0.0.1", 8765))

    def test_foreign_or_wrong_port_refused(self):
        self.assertFalse(portal._host_allowed("evil.example.com", 8765))
        self.assertFalse(portal._host_allowed("127.0.0.1:9999", 8765))
        self.assertFalse(portal._host_allowed("192.168.1.5:8765", 8765))
        self.assertFalse(portal._host_allowed("", 8765))
        self.assertFalse(portal._host_allowed(None, 8765))


class TestPickPort(unittest.TestCase):
    def test_first_free_port_wins(self):
        tried = []

        def binder(host, port):
            tried.append(port)
            if port < 8767:
                raise OSError("address in use")

        self.assertEqual(portal.pick_port("127.0.0.1", 8765,
                                          binder=binder), 8767)
        self.assertEqual(tried, [8765, 8766, 8767])

    def test_exhaustion_raises(self):
        def binder(host, port):
            raise OSError("address in use")

        with self.assertRaises(portal.PortalError):
            portal.pick_port("127.0.0.1", 8765, binder=binder,
                             attempts=3)


class TestSourceConstants(unittest.TestCase):
    def test_constants_match_the_processor(self):
        script = _ROOT / "process-portable.py"
        self.assertEqual(portal.source_constant(script, "PORTABLE_VERSION"),
                         pp.PORTABLE_VERSION)
        self.assertEqual(
            portal.source_constant(script, "CORPUS_PARSER_VERSION"),
            pp.CORPUS_PARSER_VERSION)
        self.assertEqual(portal.source_constant(script, "_MERGE_TIER3_CAP"),
                         pp._MERGE_TIER3_CAP)

    def test_missing_constant_is_none(self):
        self.assertIsNone(
            portal.source_constant(_ROOT / "process-portable.py",
                                   "NO_SUCH_CONSTANT"))


class TestConfigDisplay(unittest.TestCase):
    def test_secrets_never_reach_the_display_dict(self):
        with tempfile.TemporaryDirectory() as td:
            cfgp = Path(td) / "config.json"
            cfgp.write_text(json.dumps({
                "llm": "openai", "embed_model": _MODEL,
                "embed_url": "http://127.0.0.1:8888/v1",
                "pipeline": "v2", "trope_map_threshold": 0.78,
                "dedupe_threshold": 0.85, "import_dir": "./import",
                "openrouter_key": "SECRET-KEY-VALUE",
                "supabase_key": "SECRET-SUPABASE",
                "proposer_user_id": "SECRET-USER-ID",
            }), encoding="utf-8")
            shown = portal.config_display(cfgp)
            blob = json.dumps(shown)
            self.assertNotIn("SECRET", blob)
            self.assertEqual(shown["llm"], "openai")
            self.assertEqual(shown["embed_model"], _MODEL)
            self.assertEqual(shown["trope_map_threshold"], 0.78)

    def test_missing_config_is_empty_display(self):
        self.assertEqual(portal.config_display(Path("/nope/config.json")),
                         {})


class TestBackupInfo(unittest.TestCase):
    def test_unconfigured(self):
        info = portal.backup_info(None)
        self.assertFalse(info["configured"])
        self.assertIsNone(info["last_backup"])

    def test_manifest_read(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "backups"
            dest.mkdir()
            (dest / "manifest.json").write_text(json.dumps({
                "created": "2026-10-15T22:30:00",
                "portable_version": "2.13.1",
                "stores": {"state": {"present": True},
                           "corpus": {"present": True}}}),
                encoding="utf-8")
            info = portal.backup_info(dest)
            self.assertTrue(info["configured"])
            self.assertEqual(info["last_backup"], "2026-10-15T22:30:00")
            self.assertEqual(sorted(info["stores"]), ["corpus", "state"])


class TestHomeData(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.learn = _fixture_learn_dir(root)
        self.script = _fixture_script_dir(root)

    def tearDown(self):
        self._td.cleanup()

    def _home(self):
        return portal.home_data(self.learn, self.script)

    def test_totals_and_cards(self):
        home = self._home()
        self.assertTrue(home["present"])
        cards = home["cards"]
        self.assertEqual(cards["works_total"], 3)
        self.assertEqual(cards["works_by_status"],
                         {"complete": 1, "vectors_partial": 1,
                          "text_complete": 1})
        self.assertEqual(cards["works_complete"], 1)
        # verified + selected count as verified-on-file; the
        # candidate does not.
        self.assertEqual(cards["quotes_verified"], 3)
        self.assertEqual(cards["quotes_by_status"],
                         {"verified": 2, "selected": 1, "candidate": 1})
        self.assertEqual(cards["endings_assessed"], 1)
        self.assertEqual(cards["trigger_depth"], 1)

    def test_works_rows(self):
        rows = {w["fingerprint"]: w for w in self._home()["works"]}
        self.assertEqual(rows[_FP1]["vector_coverage"], 1.0)
        self.assertEqual(rows[_FP2]["vector_coverage"], 0.5)
        # A born generation + passages + no vectors reads 0.0 in
        # the CLI's own status model (its text form prints
        # "vectors 0%"); None is reserved for no generation or
        # no passages. The mirror keeps the CLI's semantics.
        self.assertEqual(rows[_FP3]["vector_coverage"], 0.0)
        self.assertFalse(rows[_FP1]["stale_parser"])
        self.assertTrue(rows[_FP3]["stale_parser"])
        self.assertEqual(rows[_FP1]["series_label"], "Daemon #1")
        self.assertEqual(rows[_FP3]["series_label"], "Daemon #2")
        self.assertIsNone(rows[_FP2]["series_label"])
        self.assertIn("retrieve", rows[_FP1]["last_activity"]["label"])

    def test_health(self):
        health = self._home()["health"]
        self.assertEqual(health["canary"], "ok")
        self.assertEqual(health["generation"]["model"], _MODEL)
        self.assertEqual(health["generation"]["dim"], 8)
        self.assertEqual(health["parser"]["stale_works"], 1)
        b = health["stores"]["corpus"]
        self.assertEqual(set(b), {"text", "fts", "vectors", "other",
                                  "total", "via"})
        self.assertGreater(b["total"], 0)
        self.assertGreater(health["stores"]["state_bytes"], 0)

    def test_queue_pointers(self):
        self.assertEqual(self._home()["queues"],
                         {"batch": 3, "parked": 4})

    def test_recent_runs_composite(self):
        runs = self._home()["recent_runs"]
        kinds = [r["kind"] for r in runs]
        self.assertIn("reembed", kinds)
        self.assertIn("ingest", kinds)
        self.assertIn("stage: retrieve", kinds)
        self.assertIn("preview", kinds)
        whens = [r["when"] for r in runs if r["when"]]
        self.assertEqual(whens, sorted(whens, reverse=True))
        preview = [r for r in runs if r["kind"] == "preview"
                   and r["work"] == "Demo Book"]
        self.assertEqual(len(preview), 1)
        self.assertIn("2 tropes", preview[0]["outcome"])

    def test_strip_is_honest_and_empty(self):
        strip = self._home()["strip"]
        self.assertEqual(strip["jobs"], [])
        self.assertEqual(strip["decision_cap"], pp._MERGE_TIER3_CAP)
        self.assertIn("terminal", strip["note"])

    def test_absent_stores_render_empty_not_broken(self):
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "learned"
            empty.mkdir()
            home = portal.home_data(empty, self.script)
            self.assertFalse(home["present"])
            self.assertEqual(home["works"], [])
            self.assertIsNone(home["queues"])
            self.assertEqual(home["cards"]["works_total"], 0)

    def test_resolve_work(self):
        self.assertEqual(portal.resolve_work(self.learn, _FP1[:8]), _FP1)
        self.assertIsNone(portal.resolve_work(self.learn, "deadbeef"))
        self.assertIsNone(portal.resolve_work(self.learn, "ab"))

    def test_malformed_series_index_degrades_not_500s(self):
        # A hand-edited or foreign-written store can hold text in
        # the REAL series_index column; Home must render the row,
        # not die on the float conversion (Advisor W2).
        conn = sqlite3.connect(self.learn / "corpus.db")
        conn.execute(
            "UPDATE corpus_works SET series_index='not-a-number' "
            "WHERE fingerprint=?", (_FP3,))
        conn.commit()
        conn.close()
        home = self._home()
        rows = {w["fingerprint"]: w for w in home["works"]}
        self.assertEqual(rows[_FP3]["series_label"],
                         "Daemon #not-a-number")


class TestHTTPServer(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.learn = _fixture_learn_dir(root)
        self.script = _fixture_script_dir(root)
        cfg = portal.PortalContext(learn_dir=self.learn,
                                   script_dir=self.script,
                                   backup_dir=None)
        self.server = portal.make_server("127.0.0.1", 0, cfg)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self._thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        self._td.cleanup()

    def _get(self, path, timeout=10):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}",
                timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_home_page_and_api_agree_and_write_nothing(self):
        corpus_hash = _ro_dump_hash(self.learn / "corpus.db")
        state_hash = _ro_dump_hash(self.learn / "state.db")
        status, html = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Control Room", html)
        self.assertIn("Daemon", html)
        self.assertIn("Fourth Wing", html)
        self.assertNotIn('src="http', html)
        self.assertNotIn('href="http', html)
        status, body = self._get("/api/home")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["cards"]["works_total"], 3)
        self.assertEqual(data["cards"]["quotes_verified"], 3)
        status, desk = self._get("/desk")
        self.assertEqual(status, 200)
        # S1 pinned the placeholder ("arrives in S3"); S3 has
        # landed, so the pin now names the real Desk surface.
        self.assertIn("Today's trope batch", desk)
        self.assertIn("Parked escalations", desk)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/api/explain?work=deadbeef00")
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(_ro_dump_hash(self.learn / "corpus.db"),
                         corpus_hash)
        self.assertEqual(_ro_dump_hash(self.learn / "state.db"),
                         state_hash)

    def test_forged_host_header_is_refused(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=10)
        conn.putrequest("GET", "/", skip_host=True)
        conn.putheader("Host", "evil.example.com")
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)

    def test_ipv6_bind_is_a_clear_refusal_not_a_gaierror(self):
        ctx = portal.PortalContext(learn_dir=self.learn,
                                   script_dir=self.script,
                                   backup_dir=None)
        with self.assertRaises(portal.PortalError):
            portal.make_server("::1", 0, ctx)

    def test_successful_explain_and_its_hash_caveat(self):
        # Advisor W3: the CLI's report-mode corpus open performs
        # its own meta bookkeeping on a store the substrate has
        # never opened, so a FIRST --explain against such a store
        # can move corpus.db's dump hash. That is the CLI's open
        # behaviour — identical when run by hand in a terminal —
        # not a portal write. What S1 pins: a successful explain
        # answers, and once that bookkeeping has settled, portal
        # traffic (explains included) moves nothing.
        status, body = self._get(f"/api/explain?work={_FP1[:8]}",
                                 timeout=60)
        self.assertEqual(status, 200)
        self.assertIn("Daemon", json.loads(body)["text"])
        settled = _ro_dump_hash(self.learn / "corpus.db")
        self._get(f"/api/explain?work={_FP1[:8]}", timeout=60)
        self._get("/")
        self._get("/api/home")
        self.assertEqual(_ro_dump_hash(self.learn / "corpus.db"),
                         settled)


if __name__ == "__main__":
    unittest.main()
