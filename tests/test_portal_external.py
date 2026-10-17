"""Operator portal: standalone (external) runs observed live.

Pins: a processor run started in a terminal with
--emit-run-events is adopted from its run-events artifact and
shown in the same Home strip / jobs list / Run page as a
portal-launched job — labelled external, read-only — while an
artifact a portal job owns is never adopted (the job's claim
wins, even retroactively), a finished run resolves out of the
strip, a stale artifact (no finish event, writer gone) settles
as interrupted instead of being tailed as running forever, and
old silent artifacts are never adopted at all.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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


def _wait_for(predicate, timeout=10.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeProc:
    """A Popen stand-in (the S2 harness's shape): stdout is a
    fixed set of lines; poll() reports running until release()."""

    def __init__(self, argv, lines=(), rc=0):
        self.argv = argv
        self.stdout = io.StringIO("".join(l + "\n" for l in lines))
        self._rc = rc
        self._released = threading.Event()
        self.signals = []

    def poll(self):
        return self._rc if self._released.is_set() else None

    def release(self, rc=None):
        if rc is not None:
            self._rc = rc
        self._released.set()

    def send_signal(self, sig):
        self.signals.append(sig)
        self.release(-int(sig) if isinstance(sig, int) else 3)

    def terminate(self):
        self.release(3)

    def kill(self):
        self.release(9)


def _factory(procs, lines=()):
    def make(argv, **kwargs):
        proc = FakeProc(argv, lines=lines)
        procs.append(proc)
        return proc
    return make


def _dead_pid():
    """A pid that provably does not exist: a child we spawned
    and reaped ourselves (modulo instantaneous reuse, which no
    test timescale makes plausible)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _ev(seq, **kw):
    return {"seq": seq, "ts": time.time(), **kw}


def _run_start(seq=1, preview=True, dry_run=False):
    return _ev(seq, v=1, kind="run-events", event="run_start",
               run_id="run-x", preview=preview, dry_run=dry_run)


def _write_artifact(folder, name, events, mtime=None):
    path = Path(folder) / name
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _append_artifact(path, events):
    with open(path, "a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _fixture_ctx(root, **kw):
    """script_dir (real processor source, for the launch
    builders and Home's source constants) + an empty learn dir."""
    root = Path(root)
    script = root / "repo"
    script.mkdir(parents=True)
    shutil.copy(_ROOT / "process-portable.py",
                script / "process-portable.py")
    (script / "preview").mkdir()
    learn = root / "learned"
    learn.mkdir()
    return portal.PortalContext(learn_dir=learn, script_dir=script,
                                backup_dir=None, **kw)


class _ExternalCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ctx = _fixture_ctx(self._td.name)
        self.events_dir = self.ctx.learn_dir / "run-events"
        self.events_dir.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def _externals(self):
        return [j for j in self.ctx.registry.jobs()
                if j.action == "external"]


class TestPidAlive(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(portal._pid_alive(os.getpid()))

    def test_reaped_child_is_dead(self):
        self.assertFalse(portal._pid_alive(_dead_pid()))

    def test_no_pid_is_unknown_not_dead(self):
        self.assertIsNone(portal._pid_alive(None))
        self.assertIsNone(portal._pid_alive(0))

    def test_artifact_pid_parsing(self):
        self.assertEqual(
            portal._artifact_pid(
                Path("run-20261017-120000-4321.jsonl")), 4321)
        # The collision suffix rides after the pid.
        self.assertEqual(
            portal._artifact_pid(
                Path("run-20261017-120000-4321-2.jsonl")), 4321)
        self.assertIsNone(portal._artifact_pid(Path("notes.txt")))
        # A Unicode digit is not a pid: isdigit() accepts a
        # superscript two that int() rejects, and the parse
        # must say None rather than raise into the watcher.
        self.assertIsNone(
            portal._artifact_pid(
                Path("run-20261017-120000-².jsonl")))


class TestExternalDiscovery(_ExternalCase):
    def test_live_run_is_adopted_and_briefed(self):
        name = f"run-20261017-120000-{os.getpid()}.jsonl"
        _write_artifact(self.events_dir, name, [
            _run_start(),
            _ev(2, event="book_start", book="daemon.epub",
                decision_cap=50),
            _ev(3, event="pair", book="daemon.epub", a="Ann",
                b="Bob", score=0.91),
            _ev(4, event="decision", n=1, used=1, cap=50,
                granted=True),
        ])
        self.ctx.poll_external()
        runs = self._externals()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.state, "running")
        briefs = self.ctx.registry.briefs()
        self.assertEqual([b["id"] for b in briefs], [run.id])
        self.assertEqual(briefs[0]["label"],
                         "External run — daemon.epub (preview)")
        self.assertEqual(briefs[0]["decision"],
                         {"used": 1, "cap": 50})
        snap = run.snapshot()
        self.assertEqual(snap["action"], "external")
        self.assertFalse(snap["can_stop"])
        self.assertFalse(snap["can_resume"])
        self.assertIsNone(snap["exit_code"])
        stage_names = [(s["name"], s["state"])
                       for s in snap["stages"]]
        self.assertEqual(stage_names,
                         [("Book — daemon.epub", "active")])
        feed_texts = [f["text"] for f in snap["feed"]]
        self.assertIn("book start: daemon.epub", feed_texts)
        self.assertIn("pair: Ann × Bob scored 0.91", feed_texts)
        self.assertTrue(all(f["source"] == "events"
                            for f in snap["feed"]))
        self.assertIn("standalone run", snap["log"][0])

    def test_finish_resolves_and_leaves_the_strip(self):
        name = f"run-20261017-120100-{os.getpid()}.jsonl"
        path = _write_artifact(self.events_dir, name, [
            _run_start(),
            _ev(2, event="book_start", book="daemon.epub",
                decision_cap=50),
        ])
        self.ctx.poll_external()
        run = self._externals()[0]
        _append_artifact(path, [
            _ev(3, event="book_end", book="daemon.epub",
                decision_calls=7),
            _ev(4, event="run_end", books=1, failures=0,
                interrupted=False),
        ])
        self.ctx.poll_external()
        self.assertEqual(run.state, "done")
        # The strip is for live runs; the finished preview is
        # Recent runs' story, so the run leaves the strip.
        self.assertEqual(self.ctx.registry.briefs(), [])
        snap = run.snapshot()
        self.assertEqual(snap["books_done"], 1)
        self.assertEqual(snap["books_total"], 1)
        self.assertEqual(snap["stages"][0]["state"], "done")
        self.assertIsNotNone(snap["finished"])
        # ...but its Run page survives the finish, like a
        # launched job's does.
        self.assertIs(self.ctx.registry.get(run.id), run)

    def test_failed_and_interrupted_endings(self):
        pid = os.getpid()
        _write_artifact(self.events_dir,
                        f"run-20261017-120200-{pid}.jsonl", [
                            _run_start(),
                            _ev(2, event="run_end", books=2,
                                failures=2, interrupted=False),
                        ])
        _write_artifact(self.events_dir,
                        f"run-20261017-120300-{pid}.jsonl", [
                            _run_start(),
                            _ev(2, event="run_end", books=1,
                                failures=0, interrupted=True),
                        ])
        self.ctx.poll_external()
        states = sorted(r.state for r in self._externals())
        self.assertEqual(states, ["failed", "interrupted"])
        self.assertEqual(self.ctx.registry.briefs(), [])

    def test_torn_line_is_tolerated_then_completes(self):
        name = f"run-20261017-120400-{os.getpid()}.jsonl"
        path = _write_artifact(self.events_dir, name, [
            _run_start(),
            _ev(2, event="book_start", book="daemon.epub",
                decision_cap=50),
            _ev(3, event="pair", book="daemon.epub", a="Ann",
                b="Bob", score=0.5),
        ])
        # The writer is caught mid-append: a torn final line.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"seq":4,"event":"book_end","book":"dae')
        self.ctx.poll_external()
        run = self._externals()[0]
        self.assertEqual(run.state, "running")
        self.assertEqual(run.snapshot()["events"]["pairs"], 1)
        self.assertEqual(run.snapshot()["books_done"], 0)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('mon.epub","decision_calls":1}\n')
            fh.write(json.dumps(_ev(
                5, event="run_end", books=1, failures=0,
                interrupted=False)) + "\n")
        self.ctx.poll_external()
        self.assertEqual(run.state, "done")
        self.assertEqual(run.snapshot()["books_done"], 1)

    def test_stale_run_settles_interrupted(self):
        dead = _dead_pid()
        name = f"run-20261017-120500-{dead}.jsonl"
        path = _write_artifact(self.events_dir, name, [
            _run_start(),
            _ev(2, event="book_start", book="daemon.epub",
                decision_cap=50),
        ])
        # Fresh file, dead writer: adopted (it may have just
        # crashed), still shown running on first sight...
        self.ctx.poll_external()
        run = self._externals()[0]
        self.assertEqual(run.state, "running")
        # ...but silence past the window with no finish event
        # settles it as interrupted; it is not tailed as
        # running forever.
        old = time.time() - 7200
        os.utime(path, (old, old))
        self.ctx.poll_external()
        self.assertEqual(run.state, "interrupted")
        self.assertEqual(self.ctx.registry.briefs(), [])
        snap = run.snapshot()
        self.assertEqual(snap["stages"][0]["state"], "stopped")
        self.assertIsNotNone(snap["finished"])

    def test_old_silent_artifacts_are_never_adopted(self):
        dead = _dead_pid()
        old = time.time() - 7200
        _write_artifact(self.events_dir,
                        f"run-20261017-110000-{dead}.jsonl", [
                            _run_start(),
                            _ev(2, event="book_start",
                                book="daemon.epub", decision_cap=50),
                        ], mtime=old)
        _write_artifact(self.events_dir,
                        f"run-20261017-110100-{dead}.jsonl", [
                            _run_start(),
                            _ev(2, event="run_end", books=1,
                                failures=0, interrupted=False),
                        ], mtime=old)
        self.ctx.poll_external()
        self.assertEqual(self._externals(), [])

    def test_non_object_jsonl_line_does_not_wedge_adoption(self):
        pid = os.getpid()
        # A hand-mangled artifact, sorting FIRST in the folder,
        # carrying a line that is valid JSON but not an object.
        bad = _write_artifact(
            self.events_dir, f"run-20261017-115900-{pid}.jsonl",
            [_run_start(),
             _ev(2, event="book_start", book="daemon.epub",
                 decision_cap=50)])
        with open(bad, "a", encoding="utf-8") as fh:
            fh.write("42\n")
        _write_artifact(
            self.events_dir, f"run-20261017-120800-{pid}.jsonl",
            [_run_start(),
             _ev(2, event="book_start", book="other.epub",
                 decision_cap=50)])
        # Before the tailer learned to skip non-object lines,
        # the first file's "42" raised inside the poll and no
        # artifact after it was ever adopted.
        self.ctx.poll_external()
        runs = self._externals()
        self.assertEqual(len(runs), 2)
        by_book = {r.snapshot()["stages"][0]["name"]: r
                   for r in runs}
        self.assertIn("Book — daemon.epub", by_book)
        self.assertIn("Book — other.epub", by_book)

    def test_unicode_digit_filename_does_not_wedge_adoption(self):
        pid = os.getpid()
        _write_artifact(
            self.events_dir, "run-20261017-120000-².jsonl",
            [_run_start(),
             _ev(2, event="book_start", book="daemon.epub",
                 decision_cap=50)])
        _write_artifact(
            self.events_dir, f"run-20261017-120900-{pid}.jsonl",
            [_run_start()])
        self.ctx.poll_external()
        self.assertEqual(len(self._externals()), 2)

    def test_simultaneous_external_runs_get_distinct_ids(self):
        pid = os.getpid()
        for hhmmss in ("120600", "120700"):
            _write_artifact(self.events_dir,
                            f"run-20261017-{hhmmss}-{pid}.jsonl",
                            [_run_start()])
        self.ctx.poll_external()
        runs = self._externals()
        self.assertEqual(len(runs), 2)
        self.assertNotEqual(runs[0].id, runs[1].id)
        self.assertEqual(len(self.ctx.registry.briefs()), 2)


class TestOwnJobPrecedence(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.procs = []
        self.events_dir = (Path(self._td.name) / "learned"
                           / "run-events")

    def tearDown(self):
        for proc in self.procs:
            proc.release()
        self._td.cleanup()

    def _ctx(self, lines=()):
        ctx = _fixture_ctx(
            self._td.name,
            popen_factory=_factory(self.procs, lines))
        self.events_dir.mkdir(exist_ok=True)
        return ctx

    def test_announced_artifact_is_never_external(self):
        pid = os.getpid()
        own = self.events_dir / f"run-20261017-130000-{pid}.jsonl"
        lines = (f"  Run events: {own}",)
        ctx = self._ctx(lines)
        _write_artifact(self.events_dir, own.name, [
            _run_start(),
            _ev(2, event="book_start", book="daemon.epub",
                decision_cap=50),
        ])
        _write_artifact(self.events_dir,
                        f"run-20261017-130100-{pid}.jsonl", [
                            _run_start(),
                            _ev(2, event="book_start",
                                book="other.epub", decision_cap=50),
                        ])
        job = ctx.launch({"action": "preview"})
        self.assertTrue(_wait_for(
            lambda: job.tailer.path is not None))
        ctx.poll_external()
        externals = [j for j in ctx.registry.jobs()
                     if j.action == "external"]
        # The job's own artifact is the job's; only the truly
        # standalone run was adopted — both live at once, each
        # in exactly one place.
        self.assertEqual(len(externals), 1)
        self.assertEqual(externals[0].path.name,
                         f"run-20261017-130100-{pid}.jsonl")
        self.assertNotEqual(externals[0].id, job.id)
        labels = [b["label"] for b in ctx.registry.briefs()]
        self.assertEqual(len(labels), 2)

    def test_job_claim_wins_retroactively(self):
        pid = os.getpid()
        own = self.events_dir / f"run-20261017-130200-{pid}.jsonl"
        lines = (f"  Run events: {own}",)
        ctx = self._ctx(lines)
        _write_artifact(self.events_dir, own.name, [
            _run_start(),
            _ev(2, event="book_start", book="daemon.epub",
                decision_cap=50),
        ])
        # The watcher adopts it before any job exists...
        ctx.poll_external()
        externals = [j for j in ctx.registry.jobs()
                     if j.action == "external"]
        self.assertEqual(len(externals), 1)
        adopted_id = externals[0].id
        # ...then a job attaches to the same artifact (its
        # announcement was consumed late). The job's claim
        # wins: the adoption is handed over, never duplicated.
        job = ctx.launch({"action": "preview"})
        self.assertTrue(_wait_for(
            lambda: job.tailer.path is not None))
        ctx.poll_external()
        self.assertEqual(
            [j for j in ctx.registry.jobs()
             if j.action == "external"], [])
        self.assertIsNone(ctx.registry.get(adopted_id))
        self.assertIs(ctx.registry.get(job.id), job)


class TestExternalHTTP(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.ctx = _fixture_ctx(root)
        events_dir = self.ctx.learn_dir / "run-events"
        events_dir.mkdir()
        _write_artifact(
            events_dir,
            f"run-20261017-140000-{os.getpid()}.jsonl", [
                _run_start(),
                _ev(2, event="book_start", book="daemon.epub",
                    decision_cap=50),
                _ev(3, event="decision", n=1, used=1, cap=50,
                    granted=True),
            ])
        self.server = portal.make_server("127.0.0.1", 0, self.ctx)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        self._td.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}",
                timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def _post(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_external_run_surfaces_end_to_end(self):
        status, body = self._get("/api/home")
        self.assertEqual(status, 200)
        strip_jobs = json.loads(body)["strip"]["jobs"]
        self.assertEqual(len(strip_jobs), 1)
        ext = strip_jobs[0]
        self.assertIn("External run", ext["label"])
        self.assertEqual(ext["state"], "running")
        run_id = ext["id"]

        status, _html = self._get(f"/run/{run_id}")
        self.assertEqual(status, 200)
        status, body = self._get(f"/api/jobs/{run_id}")
        self.assertEqual(status, 200)
        snap = json.loads(body)
        self.assertEqual(snap["action"], "external")
        self.assertEqual(snap["state"], "running")
        self.assertFalse(snap["can_stop"])

        status, body = self._get("/api/jobs")
        self.assertEqual(status, 200)
        ids = [j["id"] for j in json.loads(body)["jobs"]]
        self.assertIn(run_id, ids)

        # Stop is a truthful no-op: the portal cannot signal a
        # process it did not launch, and the run's state does
        # not pretend otherwise.
        status, body = self._post(f"/api/jobs/{run_id}/stop")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "running")


if __name__ == "__main__":
    unittest.main()
