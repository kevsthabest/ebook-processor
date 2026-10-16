"""Operator portal, slice S4 (PORTAL-SPEC.md §10): the Matrices
panel.

Pins: the artifact->grid reduction (pair/verdict/impulse events
become per-book grid state; a settled cell is FINAL — later
events for the same pair, even conflicting ones, change
nothing), the live panel feed on the run page (the job's S0
tailer is the one source; the panel's counts agree with the
snapshot ticker aggregates by construction), the reopening
from a Home book row (the newest artifact that scored the
work's book, reduced by the same function, yields the identical
grid), and the panel's absolute rule: it renders no control
that could change an outcome.
"""
import importlib.util
import json
import sqlite3
import sys
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
pp = _load("process_portable_for_portal_s4_tests", "process-portable.py")

_BOOK = "daemon.epub"
_FP1 = "a" * 64
_FP2 = "b" * 64


def _ev(seq, **kw):
    return {"seq": seq, "ts": 1760000000.0 + seq, **kw}


def fixture_events():
    """One book's resolver pass, in stream order, covering the
    acceptance fixture's cases: a Signal-1 merge (score null), a
    sub-0.50 scored split, an over-bar split (family, not
    identity), a guard block (score null, reason carried, no
    pair event), a clean Signal-2 merge, decision claims, and
    mapped + unmapped impulses."""
    return [
        _ev(1, event="run_start", v=1, kind="run-events",
            run_id="run-20990101-000000-1", preview=True,
            dry_run=False),
        _ev(2, event="book_start", book=_BOOK, decision_cap=50),
        _ev(3, event="verdict", book=_BOOK, a="Daemon", b="daemon",
            verdict="merged", via="signal1", score=None,
            reason=None),
        _ev(4, event="pair", book=_BOOK, a="Chris Sebeck",
            b="Sebeck", score=0.44),
        _ev(5, event="verdict", book=_BOOK, a="Chris Sebeck",
            b="Sebeck", verdict="split", via="signal2",
            score=0.44, reason=None),
        _ev(6, event="pair", book=_BOOK, a="Peter Sebeck",
            b="Sebeck", score=0.88),
        _ev(7, event="verdict", book=_BOOK, a="Peter Sebeck",
            b="Sebeck", verdict="split", via="signal2",
            score=0.88, reason=None),
        _ev(8, event="verdict", book=_BOOK, a="Chris Sebeck",
            b="Peter Sebeck", verdict="blocked", via="guard",
            score=None,
            reason="surname trap (family members sharing "
                   "'sebeck')"),
        _ev(9, event="pair", book=_BOOK, a="the voice",
            b="Daemon", score=0.93),
        _ev(10, event="verdict", book=_BOOK, a="the voice",
            b="Daemon", verdict="merged", via="signal2",
            score=0.93, reason=None),
        _ev(11, event="decision", n=1, used=1, cap=50,
            granted=True),
        _ev(12, event="decision", n=1, used=2, cap=50,
            granted=True),
        _ev(13, event="impulse", book=_BOOK, label="tech thriller",
            neighbours=[{"id": "t1", "name": "Cyberpunk",
                         "score": 0.52},
                        {"id": "t2", "name": "Conspiracy Thriller",
                         "score": 0.47},
                        {"id": "t3", "name": "Near-Future SF",
                         "score": 0.36}],
            fate="unmapped", catalog_id=None, score=0.52),
        _ev(14, event="impulse", book=_BOOK, label="found family",
            neighbours=[{"id": "t9", "name": "Found Family",
                         "score": 0.94},
                        {"id": "t8", "name": "Ensemble Cast",
                         "score": 0.61},
                        {"id": "t7", "name": "Mentor Figure",
                         "score": 0.40}],
            fate="mapped", catalog_id="t9", score=0.94),
        _ev(15, event="book_end", book=_BOOK, decision_calls=2),
        _ev(16, event="run_end", books=1, failures=0,
            interrupted=False),
    ]


def _write_artifact(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return path


def _cell(book_state, a, b):
    for cell in book_state["cells"]:
        if {cell["a"], cell["b"]} == {a, b}:
            return cell
    return None


class TestReduceMatrix(unittest.TestCase):
    def test_full_fixture_reduction(self):
        state = portal.reduce_matrix(fixture_events())
        self.assertTrue(state["finished"])
        self.assertFalse(state["interrupted"])
        self.assertEqual(state["decision"],
                         {"used": 2, "cap": 50})
        self.assertEqual(len(state["books"]), 1)
        book = state["books"][0]
        self.assertEqual(book["book"], _BOOK)
        self.assertEqual(book["decision_calls"], 2)
        self.assertEqual(
            book["names"],
            ["Daemon", "daemon", "Chris Sebeck", "Sebeck",
             "Peter Sebeck", "the voice"])
        self.assertEqual(book["candidates"], 5)
        self.assertEqual(book["resolved"], 5)

        s1 = _cell(book, "Daemon", "daemon")
        self.assertEqual(s1["state"], "settled")
        self.assertEqual(s1["verdict"], "merged")
        self.assertEqual(s1["via"], "signal1")
        self.assertIsNone(s1["score"])

        low = _cell(book, "Chris Sebeck", "Sebeck")
        self.assertEqual(low["state"], "settled")
        self.assertEqual(low["verdict"], "split")
        # The state keeps the true score; shade-only below the
        # floor is a rendering rule, not a data loss.
        self.assertEqual(low["score"], 0.44)

        over_bar = _cell(book, "Peter Sebeck", "Sebeck")
        self.assertEqual(over_bar["verdict"], "split")
        self.assertEqual(over_bar["score"], 0.88)

        blocked = _cell(book, "Chris Sebeck", "Peter Sebeck")
        self.assertEqual(blocked["verdict"], "blocked")
        self.assertEqual(blocked["via"], "guard")
        self.assertIsNone(blocked["score"])
        self.assertIn("surname trap", blocked["reason"])

        merged = _cell(book, "the voice", "Daemon")
        self.assertEqual(merged["verdict"], "merged")
        self.assertEqual(merged["score"], 0.93)

        # The verdict rail is settlement order, not axis order.
        rail = [(v["a"], v["b"], v["verdict"])
                for v in book["verdicts"]]
        self.assertEqual(rail, [
            ("Daemon", "daemon", "merged"),
            ("Chris Sebeck", "Sebeck", "split"),
            ("Peter Sebeck", "Sebeck", "split"),
            ("Chris Sebeck", "Peter Sebeck", "blocked"),
            ("Daemon", "the voice", "merged"),
        ])

        impulses = book["impulses"]
        self.assertEqual(len(impulses), 2)
        self.assertEqual(impulses[0]["label"], "tech thriller")
        self.assertEqual(impulses[0]["fate"], "unmapped")
        self.assertIsNone(impulses[0]["catalog_id"])
        self.assertEqual(
            [n["name"] for n in impulses[0]["neighbours"]],
            ["Cyberpunk", "Conspiracy Thriller",
             "Near-Future SF"])
        self.assertEqual(impulses[1]["fate"], "mapped")
        self.assertEqual(impulses[1]["catalog_id"], "t9")

    def test_scored_cell_is_not_settled_until_its_verdict(self):
        events = fixture_events()
        # Prefix ending right after the 0.44 pair event (seq 4).
        state = portal.reduce_matrix(events[:4])
        book = state["books"][0]
        cell = _cell(book, "Chris Sebeck", "Sebeck")
        self.assertEqual(cell["state"], "scored")
        self.assertEqual(cell["score"], 0.44)
        self.assertIsNone(cell["verdict"])
        self.assertEqual(book["resolved"], 1)  # the Signal-1 pair
        self.assertEqual(book["candidates"], 2)
        self.assertFalse(state["finished"])

    def test_settled_cell_never_changes(self):
        """Freeze-on-settle, at the reduction: events appended
        after a pair settles — a re-score and even a conflicting
        verdict — are consumed but change nothing about the
        cell. The grid the operator saw settle is the grid that
        stays."""
        events = fixture_events() + [
            _ev(17, event="pair", book=_BOOK, a="Chris Sebeck",
                b="Sebeck", score=0.99),
            _ev(18, event="verdict", book=_BOOK, a="Chris Sebeck",
                b="Sebeck", verdict="merged", via="signal2",
                score=0.99, reason=None),
        ]
        book = portal.reduce_matrix(events)["books"][0]
        cell = _cell(book, "Chris Sebeck", "Sebeck")
        self.assertEqual(cell["verdict"], "split")
        self.assertEqual(cell["score"], 0.44)
        self.assertEqual(book["resolved"], 5)
        self.assertEqual(len(book["verdicts"]), 5)

    def test_signal1_self_name_pair_lands_on_the_diagonal(self):
        # Two roster entries with one name: Signal 1 merges
        # them, and the pair's cell is (name, name) — the
        # grid's diagonal is that cell's address.
        events = [
            _ev(1, event="book_start", book=_BOOK,
                decision_cap=50),
            _ev(2, event="verdict", book=_BOOK, a="Jon Snow",
                b="Jon Snow", verdict="merged", via="signal1",
                score=None, reason=None),
        ]
        book = portal.reduce_matrix(events)["books"][0]
        self.assertEqual(book["names"], ["Jon Snow"])
        self.assertEqual(book["candidates"], 1)
        cell = _cell(book, "Jon Snow", "Jon Snow")
        self.assertEqual(cell["verdict"], "merged")
        self.assertIsNone(cell["score"])

    def test_verdict_without_pair_event_carries_its_score(self):
        events = [
            _ev(1, event="book_start", book=_BOOK,
                decision_cap=50),
            _ev(2, event="verdict", book=_BOOK, a="X", b="Y",
                verdict="merged", via="signal2", score=0.77,
                reason=None),
        ]
        book = portal.reduce_matrix(events)["books"][0]
        cell = _cell(book, "X", "Y")
        self.assertEqual(cell["state"], "settled")
        self.assertEqual(cell["score"], 0.77)

    def test_unattributed_events_join_the_current_book(self):
        # The sink attributes by thread; an event whose book
        # field is missing belongs to the book bracket it
        # arrived inside, not to the void.
        events = [
            _ev(1, event="book_start", book=_BOOK,
                decision_cap=50),
            _ev(2, event="pair", book=None, a="A", b="B",
                score=0.9),
            _ev(3, event="verdict", book=None, a="A", b="B",
                verdict="merged", via="signal2", score=0.9,
                reason=None),
        ]
        state = portal.reduce_matrix(events)
        self.assertEqual(len(state["books"]), 1)
        self.assertEqual(state["books"][0]["book"], _BOOK)
        self.assertEqual(state["books"][0]["resolved"], 1)

    def test_two_books_stay_separate(self):
        events = fixture_events()
        other = [
            _ev(17, event="book_start", book="other.epub",
                decision_cap=50),
            _ev(18, event="pair", book="other.epub", a="Solo",
                b="Duo", score=0.51),
            _ev(19, event="verdict", book="other.epub", a="Solo",
                b="Duo", verdict="split", via="signal2",
                score=0.51, reason=None),
            _ev(20, event="book_end", book="other.epub",
                decision_calls=0),
        ]
        state = portal.reduce_matrix(events + other)
        self.assertEqual([b["book"] for b in state["books"]],
                         [_BOOK, "other.epub"])
        self.assertEqual(state["books"][1]["candidates"], 1)
        self.assertEqual(state["books"][0]["candidates"], 5)

    def test_empty_stream(self):
        state = portal.reduce_matrix([])
        self.assertEqual(state["books"], [])
        self.assertIsNone(state["decision"])
        self.assertFalse(state["finished"])


class TestTailerEventsAccessor(unittest.TestCase):
    def test_events_returns_every_parsed_event_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_artifact(
                Path(td) / "run-x.jsonl", fixture_events())
            tailer = portal.EventsTailer()
            tailer.attach(path)
            tailer.poll()
            events = tailer.events()
            self.assertEqual([e["seq"] for e in events],
                             list(range(1, 17)))
            # The returned list is a copy: mutating it must not
            # reach into the tailer's retained stream.
            events.clear()
            self.assertEqual(len(tailer.events()), 16)


class TestLatestMatrices(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.learn = Path(self._td.name) / "learned"
        self.folder = self.learn / "run-events"
        self.folder.mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def test_newest_artifact_with_data_wins_per_book(self):
        older = fixture_events()
        _write_artifact(
            self.folder / "run-20261001-090000-111.jsonl", older)
        newer = fixture_events()
        newer[0] = _ev(1, event="run_start", v=1,
                       kind="run-events",
                       run_id="run-20261016-090000-222",
                       preview=True, dry_run=False)
        _write_artifact(
            self.folder / "run-20261016-090000-222.jsonl", newer)
        found = portal.latest_matrices(self.learn, {_BOOK})
        self.assertEqual(found[_BOOK]["run_id"],
                         "run-20261016-090000-222")

    def test_book_with_no_scored_pairs_is_not_mapped(self):
        events = [
            _ev(1, event="run_start", v=1, kind="run-events",
                run_id="run-20261016-090000-222", preview=True,
                dry_run=False),
            _ev(2, event="book_start", book="quiet.epub",
                decision_cap=50),
            _ev(3, event="book_end", book="quiet.epub",
                decision_calls=0),
            _ev(4, event="run_end", books=1, failures=0,
                interrupted=False),
        ]
        _write_artifact(
            self.folder / "run-20261016-090000-222.jsonl", events)
        found = portal.latest_matrices(
            self.learn, {"quiet.epub", _BOOK})
        self.assertEqual(found, {})

    def test_no_folder_is_empty_not_an_error(self):
        self.assertEqual(
            portal.latest_matrices(self.learn, {_BOOK}), {})
        missing = Path(self._td.name) / "nope"
        self.assertEqual(
            portal.latest_matrices(missing, {_BOOK}), {})


def _fixture_learn(root, with_artifacts=True):
    """A learned dir: a corpus holding two works (FP1's payload
    names daemon.epub as its source file; FP2's names a book no
    run has scored), plus — optionally — the fixture run's
    artifact under run-events/."""
    d = Path(root) / "learned"
    d.mkdir(parents=True)
    cdb = sqlite3.connect(d / "corpus.db")
    for ddl in pp._CORPUS_DDL:
        cdb.execute(ddl)
    for fp, title, source in (
            (_FP1, "Daemon", _BOOK),
            (_FP2, "Other Book", "other.epub")):
        cdb.execute(
            "INSERT INTO corpus_works (fingerprint, title, "
            "author, status, parser_version, chapter_count, "
            "payload, ingested_at, updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?)",
            (fp, title, "An Author", "complete", "1", 10,
             json.dumps({"source_name": source,
                         "word_count": 1000}),
             "2026-10-16T09:00:00", "2026-10-16T09:00:00"))
    cdb.commit()
    cdb.close()
    run_id = None
    if with_artifacts:
        path = _write_artifact(
            d / "run-events" / "run-20261016-090000-222.jsonl",
            fixture_events())
        run_id = path.stem
    return d, run_id


_STUB4 = r'''
import json
import sys
import time
from pathlib import Path

argv = sys.argv[1:]

def argval(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None

EVENTS_PART1 = __PART1__
EVENTS_PART2 = __PART2__

fname = argval("--file") or "daemon.epub"
learn = Path(argval("--learn-dir") or ".")
print("Portable vSTUB", flush=True)
print("Preview mode: results saved to preview/", flush=True)
print(f"Processing (v2): {Path(fname).name}", flush=True)
path = None
if "--emit-run-events" in argv:
    folder = learn / "run-events"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "run-20990101-000000-1.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for ev in EVENTS_PART1:
            fh.write(json.dumps(ev) + "\n")
    print(f"  Run events: {path}", flush=True)
# Hold the run open until the test releases it, so the panel's
# mid-run state can be observed before the tail of the stream
# (with its conflicting re-score) lands.
release = learn / "release-matrix"
deadline = time.time() + 60
while not release.exists() and time.time() < deadline:
    time.sleep(0.05)
if path is not None:
    with open(path, "a", encoding="utf-8") as fh:
        for ev in EVENTS_PART2:
            fh.write(json.dumps(ev) + "\n")
print("  Preview saved (pipeline=v2)", flush=True)
print("  Run complete: Stub Book", flush=True)
sys.exit(0)
'''


def _part2_events():
    return [
        _ev(17, event="pair", book=_BOOK, a="Chris Sebeck",
            b="Sebeck", score=0.99),
        _ev(18, event="verdict", book=_BOOK, a="Chris Sebeck",
            b="Sebeck", verdict="merged", via="signal2",
            score=0.99, reason=None),
        _ev(19, event="run_end", books=1, failures=0,
            interrupted=False),
    ]


def _stub_ctx(root):
    """A portal context whose script dir holds the S4 stub
    processor, and whose learned dir ALSO holds the corpus
    fixture — one learn dir, so the run's artifact lands beside
    the corpus the reopening path reads."""
    root = Path(root)
    script = root / "repo"
    (script / "preview").mkdir(parents=True)
    part1 = fixture_events()[:-1]  # everything but run_end
    stub = _STUB4.replace(
        "__PART1__", repr(part1)).replace(
        "__PART2__", repr(_part2_events()))
    (script / "process-portable.py").write_text(stub,
                                                encoding="utf-8")
    learn, run_id = _fixture_learn(root, with_artifacts=False)
    imp = root / "import"
    imp.mkdir()
    (imp / _BOOK).write_bytes(b"fake")
    (imp / "other.epub").write_bytes(b"fake")
    (script / "config.json").write_text(
        json.dumps({"import_dir": str(imp)}), encoding="utf-8")
    ctx = portal.PortalContext(learn_dir=learn, script_dir=script,
                               backup_dir=root / "backups")
    return ctx, script, learn, imp


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ctx, self.script, self.learn, self.imp = _stub_ctx(
            self._td.name)
        self.server = portal.make_server("127.0.0.1", 0, self.ctx)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        (self.learn / "release-matrix").touch()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        self._td.cleanup()

    def _req(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode(
                    "utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def _wait_for(self, predicate, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def _launch_preview(self):
        status, body = self._req(
            "POST", "/api/launch",
            {"action": "preview", "file_name": _BOOK})
        self.assertEqual(status, 200, body)
        return json.loads(body)["job_id"]

    def _job(self, job_id):
        status, body = self._req("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def _matrix(self, job_id):
        status, body = self._req(
            "GET", f"/api/jobs/{job_id}/matrix")
        self.assertEqual(status, 200, body)
        return json.loads(body)


class TestLivePanel(_ServerCase):
    def test_grid_fills_and_a_settled_cell_never_redraws(self):
        job_id = self._launch_preview()
        # Mid-run: the tailer's aggregates and the panel agree,
        # because both are computed from the same retained
        # stream (one source of truth for the ticker).
        self.assertTrue(self._wait_for(
            lambda: self._matrix(job_id)["aggregates"]["pairs"]
            == 3))
        payload = self._matrix(job_id)
        snap = self._job(job_id)
        self.assertEqual(payload["aggregates"],
                         snap["events"])
        self.assertEqual(payload["aggregates"]["decision"],
                         {"used": 2, "cap": 50})
        book = payload["matrix"]["books"][0]
        cell = _cell(book, "Chris Sebeck", "Sebeck")
        self.assertEqual(cell["verdict"], "split")
        self.assertEqual(cell["score"], 0.44)
        self.assertFalse(payload["matrix"]["finished"])
        self.assertEqual(payload["bars"]["merge_bar"], 0.85)
        self.assertEqual(payload["bars"]["map_bar"], 0.78)
        self.assertEqual(payload["bars"]["shade_floor"], 0.50)

        # Release the run: the stream's tail re-scores the
        # settled pair at 0.99 with a conflicting verdict. The
        # tailer consumes both events (aggregates grow) and the
        # settled cell does not move.
        (self.learn / "release-matrix").touch()
        self.assertTrue(self._wait_for(
            lambda: self._job(job_id)["state"] == "done"))
        payload = self._matrix(job_id)
        self.assertEqual(payload["aggregates"]["pairs"], 4)
        self.assertEqual(payload["aggregates"]["verdicts"], 6)
        book = payload["matrix"]["books"][0]
        cell = _cell(book, "Chris Sebeck", "Sebeck")
        self.assertEqual(cell["verdict"], "split")
        self.assertEqual(cell["score"], 0.44)
        self.assertEqual(book["resolved"], 5)
        self.assertTrue(payload["matrix"]["finished"])

    def test_run_page_carries_the_panel(self):
        job_id = self._launch_preview()
        status, body = self._req("GET", f"/run/{job_id}")
        self.assertEqual(status, 200)
        self.assertIn("id='mx-panel'", body)
        self.assertIn("/matrix", body)  # the panel's endpoint


class TestReopening(_ServerCase):
    def test_home_row_links_and_reopening_is_identical(self):
        job_id = self._launch_preview()
        (self.learn / "release-matrix").touch()
        self.assertTrue(self._wait_for(
            lambda: self._job(job_id)["state"] == "done"))
        live = self._matrix(job_id)["matrix"]["books"][0]

        status, home = self._req("GET", "/")
        self.assertEqual(status, 200)
        # Exactly one row carries the link: FP1's book was
        # scored by a retained run; FP2's never was.
        self.assertEqual(home.count("href='/matrix/"), 1)
        self.assertIn(f"href='/matrix/{_FP1}'", home)
        self.assertNotIn(f"href='/matrix/{_FP2}'", home)

        status, page = self._req("GET", f"/matrix/{_FP1}")
        self.assertEqual(status, 200)
        self.assertIn("id='mx-panel'", page)
        self.assertIn("Daemon", page)
        self.assertIn("run-20990101-000000-1", page)

        status, body = self._req(
            "GET", f"/api/matrix?work={_FP1}")
        self.assertEqual(status, 200)
        reopened = json.loads(body)
        self.assertEqual(reopened["run_id"],
                         "run-20990101-000000-1")
        # The identical grid: the reopened book section equals
        # the live panel's, key for key — same artifact, same
        # reduction.
        self.assertEqual(reopened["matrix"]["books"], [live])
        self.assertEqual(reopened["work"]["fingerprint"], _FP1)

    def test_work_without_a_run_gets_a_plain_refusal(self):
        status, body = self._req(
            "GET", f"/api/matrix?work={_FP2}")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))
        status, body = self._req(
            "GET", "/api/matrix?work=" + "f" * 64)
        self.assertEqual(status, 404)


_PANEL_FORBIDDEN = ("<button", "<input", "<select", "<textarea",
                    "<a ", "onclick", "contenteditable",
                    "data-reembed", "<form")


def _panel_segment(html):
    start = html.index("id='mx-panel'")
    end = html.index("</section>", start)
    return html[start:end]


class TestPanelHasNoControls(_ServerCase):
    """The panel displays the resolver's work; it is not a
    second resolver. These pins hold the line structurally:
    the panel's markup contains nothing clickable toward an
    outcome, and its script builds no control elements and
    never repaints a settled cell."""

    def test_run_page_panel_segment_is_control_free(self):
        job_id = self._launch_preview()
        status, body = self._req("GET", f"/run/{job_id}")
        self.assertEqual(status, 200)
        segment = _panel_segment(body)
        for token in _PANEL_FORBIDDEN:
            self.assertNotIn(token, segment)

    def test_matrix_page_panel_segment_is_control_free(self):
        status, body = self._req("GET", f"/matrix/{_FP1}")
        self.assertEqual(status, 200)
        segment = _panel_segment(body)
        for token in _PANEL_FORBIDDEN:
            self.assertNotIn(token, segment)

    def test_panel_script_builds_no_controls_and_freezes(self):
        js = portal._MATRIX_JS
        for token in ("createElement('button')",
                      'createElement("button")',
                      "createElement('a')",
                      "createElement('input')",
                      "createElement('select')",
                      "fetch('/api/launch'",
                      'method: "POST"', "method: 'POST'"):
            self.assertNotIn(token, js)
        # The freeze guard and the shade rule are named
        # mechanisms in the shipped script, not intentions.
        self.assertIn("mxSettled", js)
        self.assertIn("shade_floor", js)
        # The shell-swap recovery (the page shell re-renders
        # <main> under the panel) is likewise a named mechanism.
        self.assertIn("mxWrap", js)


if __name__ == "__main__":
    unittest.main()
