"""Operator portal, slice S2 (PORTAL-SPEC.md §10): launching +
run page.

Pins: the §6 launch argv (standing command verbatim, per-action
flags, write runs behind a server-side confirm), the §7
one-writer queue (a second writer queues visibly and starts only
after the first exits), Stop (signal -> checkpoints -> stopped)
and Resume (original argv + --resume, only when checkpoints
exist), the run-events tailer (live aggregates; a torn final
JSONL line after a crash is skipped, never wedges the reader),
the stage/feed line mapping (§5.3's tested table), EPUB cover
extraction for the ingest picker (Q10), and the HTTP surface
that carries all of it.
"""
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, _ROOT / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


portal = _load("portal", "portal.py")
pp = _load("process_portable_for_portal_s2_tests", "process-portable.py")


def _wait_for(predicate, timeout=10.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeProc:
    """A Popen stand-in with test-controlled lifetime: stdout is
    a fixed set of lines; wait() blocks until release() — the
    queue tests need a first writer that provably has not exited
    while the second is launched."""

    def __init__(self, argv, lines=(), rc=0):
        self.argv = argv
        self.stdout = io.StringIO("".join(l + "\n" for l in lines))
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


def _fake_factory(procs):
    def factory(argv, **kwargs):
        proc = FakeProc(argv)
        procs.append(proc)
        return proc
    return factory


def _fixture_dirs(root, import_names=("daemon.epub",)):
    """script_dir (real processor source copied), learn_dir,
    import_dir with the named (tiny, fake) book files, backup
    dir path. The launch builders only need paths to exist."""
    root = Path(root)
    script = root / "repo"
    script.mkdir(parents=True)
    import shutil
    shutil.copy(_ROOT / "process-portable.py", script / "process-portable.py")
    learn = root / "learned"
    learn.mkdir()
    imp = root / "import"
    imp.mkdir()
    for name in import_names:
        (imp / name).write_bytes(b"fake-book")
    (script / "config.json").write_text(
        json.dumps({"import_dir": str(imp)}), encoding="utf-8")
    backup = root / "backups"
    return script, learn, imp, backup


def _ctx(root, backup=True, **kw):
    script, learn, imp, bdir = _fixture_dirs(root)
    return portal.PortalContext(
        learn_dir=learn, script_dir=script,
        backup_dir=bdir if backup else None, **kw)


class TestRingBuffer(unittest.TestCase):
    def test_seq_and_since(self):
        buf = portal.RingBuffer(10)
        self.assertEqual(buf.append("a"), 1)
        self.assertEqual(buf.append("b"), 2)
        self.assertEqual(buf.since(0), [(1, "a"), (2, "b")])
        self.assertEqual(buf.since(1), [(2, "b")])
        self.assertEqual(buf.lines(), ["a", "b"])

    def test_bound_drops_oldest_but_seq_continues(self):
        buf = portal.RingBuffer(3)
        for i in range(5):
            buf.append(f"line{i}")
        self.assertEqual(buf.lines(), ["line2", "line3", "line4"])
        self.assertEqual(buf.since(0)[0][0], 3)


class TestBuildLaunch(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ctx = _ctx(self._td.name)
        self.script = str(self.ctx.script_dir / "process-portable.py")

    def tearDown(self):
        self._td.cleanup()

    def test_ingest_minimal(self):
        spec = portal.build_launch(self.ctx, {
            "action": "ingest", "file_name": "daemon.epub"})
        argv = spec.argv
        self.assertEqual(argv[1], self.script)
        self.assertIn("--ingest", argv)
        self.assertIn(str(self.ctx.script_dir.parent / "import"
                          / "daemon.epub"), argv)
        self.assertIn("--learn-dir", argv)
        self.assertNotIn("--ingest-force", argv)
        self.assertNotIn("--ingest-reason", argv)

    def test_ingest_reason_and_force_only_when_set(self):
        spec = portal.build_launch(self.ctx, {
            "action": "ingest", "file_name": "daemon.epub",
            "reason": "s13 drill", "force": True})
        self.assertIn("--ingest-force", spec.argv)
        idx = spec.argv.index("--ingest-reason")
        self.assertEqual(spec.argv[idx + 1], "s13 drill")

    def test_ingest_file_must_come_from_the_picker(self):
        # A name the picker never listed — a traversal attempt or
        # a file outside the import dir — is refused, so the
        # browser can never aim the pipeline at an arbitrary path.
        for bad in ("../secret.epub", "missing.epub", "/etc/passwd"):
            with self.assertRaises(portal.PortalError):
                portal.build_launch(self.ctx, {
                    "action": "ingest", "file_name": bad})

    def test_preview_is_the_standing_command_verbatim(self):
        spec = portal.build_launch(self.ctx, {"action": "preview"})
        argv = spec.argv
        for flag in portal.STANDING_FLAGS:
            self.assertIn(flag, argv)
        self.assertIn("--preview", argv)
        self.assertIn("--no-viz", argv)
        self.assertIn("--emit-run-events", argv)
        self.assertNotIn("--file", argv)

    def test_preview_single_file_and_profile(self):
        spec = portal.build_launch(self.ctx, {
            "action": "preview", "file_name": "daemon.epub",
            "profile": "quick"})
        idx = spec.argv.index("--file")
        self.assertTrue(spec.argv[idx + 1].endswith("daemon.epub"))
        idx = spec.argv.index("--profile")
        self.assertEqual(spec.argv[idx + 1], "quick")

    def test_write_run_needs_the_confirm(self):
        with self.assertRaises(portal.PortalError):
            portal.build_launch(self.ctx, {"action": "write"})
        spec = portal.build_launch(self.ctx, {
            "action": "write", "confirm": True})
        self.assertNotIn("--preview", spec.argv)
        self.assertIn("--no-viz", spec.argv)
        self.assertIn("--emit-run-events", spec.argv)
        for flag in portal.STANDING_FLAGS:
            self.assertIn(flag, spec.argv)

    def test_reembed_all_and_one_work(self):
        spec = portal.build_launch(self.ctx, {"action": "reembed"})
        self.assertIn("--corpus-reembed", spec.argv)
        self.assertNotIn("--work", spec.argv)
        fp = "a" * 64
        spec = portal.build_launch(self.ctx, {
            "action": "reembed", "work": fp})
        idx = spec.argv.index("--work")
        self.assertEqual(spec.argv[idx + 1], fp)

    def test_backup_uses_the_configured_dir(self):
        spec = portal.build_launch(self.ctx, {"action": "backup"})
        idx = spec.argv.index("--backup-state")
        self.assertEqual(spec.argv[idx + 1], str(self.ctx.backup_dir))

    def test_backup_without_a_configured_dir_is_refused(self):
        ctx = _ctx(self._td.name + "x", backup=False)
        with self.assertRaises(portal.PortalError):
            portal.build_launch(ctx, {"action": "backup"})

    def test_unknown_action_refused(self):
        with self.assertRaises(portal.PortalError):
            portal.build_launch(self.ctx, {"action": "corpus-remove"})


class TestJobQueue(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.procs = []
        self.ctx = _ctx(self._td.name)
        self.registry = portal.JobRegistry(
            self.ctx, popen_factory=_fake_factory(self.procs),
            stop_grace=0.2)

    def tearDown(self):
        self._td.cleanup()

    def _launch(self, name="daemon.epub"):
        spec = portal.build_launch(self.ctx, {
            "action": "ingest", "file_name": name})
        return self.registry.launch(spec)

    def test_second_writer_queues_until_the_first_exits(self):
        first = self._launch()
        self.assertEqual(first.state, "running")
        second = self._launch()
        self.assertEqual(second.state, "queued")
        # The child for the second job has not even been spawned.
        self.assertEqual(len(self.procs), 1)
        self.procs[0].release(0)
        self.assertTrue(_wait_for(lambda: first.state == "done"))
        self.assertTrue(_wait_for(lambda: second.state == "running"))
        self.assertEqual(len(self.procs), 2)
        self.procs[1].release(0)
        self.assertTrue(_wait_for(lambda: second.state == "done"))

    def test_failed_exit_marks_failed_and_frees_the_slot(self):
        first = self._launch()
        second = self._launch()
        self.procs[0].release(2)
        self.assertTrue(_wait_for(lambda: first.state == "failed"))
        self.assertEqual(first.exit_code, 2)
        self.assertTrue(_wait_for(lambda: second.state == "running"))
        self.procs[1].release(0)
        self.assertTrue(_wait_for(lambda: second.state == "done"))

    def test_stop_a_queued_job_never_starts_it(self):
        first = self._launch()
        second = self._launch()
        second.stop()
        self.assertEqual(second.state, "stopped")
        self.procs[0].release(0)
        self.assertTrue(_wait_for(lambda: first.state == "done"))
        time.sleep(0.1)
        self.assertEqual(len(self.procs), 1)
        self.assertEqual(second.state, "stopped")

    def test_stop_a_running_job_signals_and_marks_stopped(self):
        job = self._launch()
        job.stop()
        self.assertTrue(self.procs[0].signals)
        self.assertTrue(_wait_for(lambda: job.state == "stopped"))

    def test_checkpointed_reflects_files_not_just_the_line(self):
        # A child stopped mid-call never prints "Checkpoint
        # saved", but its per-chapter files are on disk; the
        # snapshot must agree with what Resume would find.
        tmp = Path(tempfile.mkdtemp(prefix="portal-s2-ckpt-"))
        procs = []
        ctx = _ctx(tmp)
        registry = portal.JobRegistry(
            ctx, popen_factory=_fake_factory(procs), stop_grace=0.2)
        book = ctx.script_dir / "preview" / ".checkpoints" \
            / "some-book"
        book.mkdir(parents=True)
        (book / "chapter_0.json").write_text("{}", "utf-8")
        spec = portal.LaunchSpec(
            "preview", ["python", "x.py"], "Preview — some-book.epub",
            file_path=ctx.script_dir / "some-book.epub")
        job = registry.launch(spec)
        job.stop()
        procs[-1].release(-2)
        self.assertTrue(
            _wait_for(lambda: job.state == "stopped"))
        snap = job.snapshot()
        self.assertTrue(snap["checkpointed"])
        self.assertTrue(snap["can_resume"])

    def test_stdout_lines_land_in_the_buffer(self):
        spec = portal.build_launch(self.ctx, {"action": "backup"})
        job = self.registry.launch(spec)
        proc = self.procs[0]
        proc.stdout = io.StringIO("  Backup written to x: corpus\n")
        # The reader thread consumed the initial (empty) stream;
        # feed lines through the job's own line sink instead —
        # the same path the reader thread uses.
        job._note_line("  Backup written to x: corpus")
        self.assertIn("Backup written", job.snapshot()["now"])
        proc.release(0)
        self.assertTrue(_wait_for(lambda: job.state == "done"))


class TestEventsTailer(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name) / "run-events"
        self.dir.mkdir()
        self.path = self.dir / "run-20261016-120000-42.jsonl"

    def tearDown(self):
        self._td.cleanup()

    def _write(self, text, append=True):
        with open(self.path, "a" if append else "w",
                  encoding="utf-8") as fh:
            fh.write(text)

    def test_aggregates_and_torn_line_tolerance(self):
        self._write(
            '{"seq":1,"event":"run_start","run_id":"run-x",'
            '"preview":true}\n'
            '{"seq":2,"event":"book_start","book":"a.epub",'
            '"decision_cap":50}\n'
            '{"seq":3,"event":"pair","book":"a.epub","a":"Ann",'
            '"b":"Bob","score":0.91}\n'
            '{"seq":4,"event":"verdict","book":"a.epub","a":"Ann",'
            '"b":"Bob","verdict":"merged","via":"signal2",'
            '"score":0.91,"reason":null}\n'
            '{"seq":5,"event":"decision","n":1,"used":1,"cap":50,'
            '"granted":true}\n', append=False)
        tailer = portal.EventsTailer()
        tailer.attach(self.path)
        agg = tailer.poll()
        self.assertEqual(agg["pairs"], 1)
        self.assertEqual(agg["verdicts"], 1)
        self.assertEqual(agg["decision"], {"used": 1, "cap": 50})
        self.assertFalse(agg["finished"])
        self.assertEqual(len(tailer.new_events()), 5)
        # A torn final line (crash mid-write): skipped, not fatal,
        # and picked up once the writer completes it.
        self._write('{"seq":6,"event":"book_end","book":"a.epub",'
                    '"decision_calls":3')
        agg = tailer.poll()
        self.assertEqual(agg["book_decisions"], {})
        self._write('}\n{"seq":7,"event":"run_end","books":1,'
                    '"failures":0,"interrupted":false}\n')
        agg = tailer.poll()
        self.assertEqual(agg["book_decisions"], {"a.epub": 3})
        self.assertTrue(agg["finished"])
        self.assertFalse(agg["interrupted"])

    def test_poll_without_attach_is_empty(self):
        tailer = portal.EventsTailer()
        agg = tailer.poll()
        self.assertEqual(agg["pairs"], 0)
        self.assertIsNone(agg["path"])


class TestStageTracker(unittest.TestCase):
    def test_pipeline_lines(self):
        tracker = portal.StageTracker("preview")
        tracker.feed_line("Portable v2.13.1")
        tracker.feed_line("Preview mode: results saved to preview/")
        snap = tracker.snapshot()
        self.assertEqual(snap["stages"][0]["name"], "Startup")
        tracker.feed_line("Processing (v2): daemon.epub")
        tracker.feed_line("  Title: Daemon, Chars: 843,573")
        tracker.feed_line("  Preview saved (pipeline=v2)")
        tracker.feed_line("  Run complete: Daemon")
        snap = tracker.snapshot()
        names = [s["name"] for s in snap["stages"]]
        self.assertIn("Book — daemon.epub", names)
        book = [s for s in snap["stages"]
                if s["name"] == "Book — daemon.epub"][0]
        self.assertEqual(book["state"], "done")
        self.assertEqual(snap["books_done"], 1)
        feed = snap["feed"]
        self.assertTrue(any("Preview saved" in f["text"]
                            for f in feed))
        self.assertTrue(any("Run complete" in f["text"]
                            for f in feed))

    def test_ingest_outcome_line_completes_the_file_stage(self):
        tracker = portal.StageTracker(
            "ingest", files=["daemon.epub"])
        tracker.feed_line(
            "  (no embedder configured — text-only ingest; "
            "vectors stay pending.)")
        tracker.feed_line(
            "  daemon.epub: text_complete [2847ba64] 73 chapters, "
            "1797 passages")
        snap = tracker.snapshot()
        stage = [s for s in snap["stages"]
                 if "daemon.epub" in s["name"]][0]
        self.assertEqual(stage["state"], "done")
        self.assertEqual(snap["books_done"], 1)

    def test_unknown_lines_still_land_in_the_feed(self):
        tracker = portal.StageTracker("preview")
        tracker.feed_line("  some line nobody mapped")
        snap = tracker.snapshot()
        self.assertEqual(snap["feed"][-1]["text"],
                         "  some line nobody mapped")
        self.assertEqual(snap["now"], "some line nobody mapped")


def _make_epub(path, cover=None, cover_props=True, meta_cover=False):
    """A minimal EPUB: container + OPF + one chapter, with an
    optional cover image declared the EPUB3 way (properties) or
    the EPUB2 way (meta name=cover)."""
    manifest = ['<item id="c1" href="c1.xhtml" '
                'media-type="application/xhtml+xml"/>']
    meta = ""
    if cover is not None:
        props = ' properties="cover-image"' if cover_props else ""
        manifest.append(
            f'<item id="cov" href="cover.jpg"{props} '
            f'media-type="image/jpeg"/>')
        if meta_cover:
            meta = '<meta name="cover" content="cov"/>'
    opf = (
        '<?xml version="1.0"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'{meta}<dc:title>Fixture</dc:title></metadata>'
        f'<manifest>{"".join(manifest)}</manifest>'
        '<spine><itemref idref="c1"/></spine></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?>\n'
                   '<container xmlns="urn:oasis:names:tc:opendocument:'
                   'xmlns:container" version="1.0"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c1.xhtml", "<html><body>text</body></html>")
        if cover is not None:
            z.writestr("OEBPS/cover.jpg", cover)


class TestCovers(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.cache = self.root / "portal-cache" / "covers"

    def tearDown(self):
        self._td.cleanup()

    def test_epub3_cover_image_property(self):
        book = self.root / "a.epub"
        _make_epub(book, cover=b"\xff\xd8JPEGDATA")
        got = portal.epub_cover(book, self.cache)
        self.assertIsNotNone(got)
        data, ctype = got
        self.assertEqual(data, b"\xff\xd8JPEGDATA")
        self.assertEqual(ctype, "image/jpeg")

    def test_epub2_meta_cover(self):
        book = self.root / "b.epub"
        _make_epub(book, cover=b"\xff\xd8OLD", cover_props=False,
                   meta_cover=True)
        data, _ctype = portal.epub_cover(book, self.cache)
        self.assertEqual(data, b"\xff\xd8OLD")

    def test_no_cover_is_none_not_an_error(self):
        book = self.root / "c.epub"
        _make_epub(book)
        self.assertIsNone(portal.epub_cover(book, self.cache))

    def test_cache_is_written_in_the_portal_dir(self):
        book = self.root / "d.epub"
        _make_epub(book, cover=b"\xff\xd8CACHED")
        portal.epub_cover(book, self.cache)
        cached = list(self.cache.glob("*"))
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0].read_bytes(), b"\xff\xd8CACHED")

    def test_not_a_zip_is_none(self):
        book = self.root / "e.epub"
        book.write_bytes(b"not a zip")
        self.assertIsNone(portal.epub_cover(book, self.cache))

    def test_svg_cover_is_not_served_as_active_svg(self):
        # The served type comes from a raster allowlist, never
        # verbatim from book metadata: active SVG on the portal
        # origin could script against the portal API.
        book = self.root / "svg.epub"
        opf = (
            '<?xml version="1.0"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>Fixture</dc:title></metadata><manifest>'
            '<item id="c1" href="c1.xhtml" '
            'media-type="application/xhtml+xml"/>'
            '<item id="cov" href="cover.svg" '
            'media-type="image/svg+xml" '
            'properties="cover-image"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>')
        with zipfile.ZipFile(book, "w") as z:
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0"?>\n'
                       '<container xmlns="urn:oasis:names:tc:'
                       'opendocument:xmlns:container" '
                       'version="1.0"><rootfiles>'
                       '<rootfile full-path="OEBPS/content.opf" '
                       'media-type="application/oebps-package+xml"/>'
                       '</rootfiles></container>')
            z.writestr("OEBPS/content.opf", opf)
            z.writestr("OEBPS/c1.xhtml",
                       "<html><body>text</body></html>")
            z.writestr("OEBPS/cover.svg", b"<svg/>")
        self.assertIsNone(portal.epub_cover(book, self.cache))


class TestProfilesMirror(unittest.TestCase):
    def test_builtins_come_from_the_processor_source(self):
        names = portal.available_profiles()
        self.assertEqual(names.get("mass"), "builtin")
        self.assertEqual(names.get("quick"), "builtin")


# ---------------------------------------------------------------------------
# HTTP integration: a stub process-portable.py stands in for the
# processor so spawn/capture/stop/resume run for real, fast.
# ---------------------------------------------------------------------------

_STUB = r'''
import json, os, signal, sys, time
from pathlib import Path

argv = sys.argv[1:]
here = Path(__file__).resolve().parent
(here / "stub-calls.log").open("a", encoding="utf-8").write(
    json.dumps(argv) + "\n")

def argval(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None

if "--backup-state" in argv:
    dest = Path(argval("--backup-state"))
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(json.dumps({
        "created": "2026-10-16T13:00:00", "portable_version": "stub",
        "stores": {"state": {"present": True},
                   "corpus": {"present": True}}}), encoding="utf-8")
    print(f"  Backup written to {dest}: corpus, state", flush=True)
    sys.exit(0)

if "--ingest" in argv:
    target = Path(argval("--ingest"))
    print("  (no embedder configured — text-only ingest)", flush=True)
    print(f"  {target.name}: text_complete [abcdef01] 3 chapters, "
          f"30 passages", flush=True)
    sys.exit(0)

if "--corpus-reembed" in argv:
    print("  [abcdef01] re-embed: complete (model stub)", flush=True)
    sys.exit(0)

fname = argval("--file") or "hang.epub"
print("Portable vSTUB", flush=True)
print("Preview mode: results saved to preview/", flush=True)
print(f"Processing (v2): {Path(fname).name}", flush=True)
print("  Title: Stub Book, Chars: 1,234", flush=True)

if "--emit-run-events" in argv:
    learn = Path(argval("--learn-dir") or ".")
    events = learn / "run-events"
    events.mkdir(parents=True, exist_ok=True)
    path = events / "run-20990101-000000-1.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 1, "event": "run_start",
                             "run_id": path.stem, "preview": True})
                 + "\n")
        fh.write(json.dumps({"seq": 2, "event": "book_start",
                             "book": Path(fname).name,
                             "decision_cap": 50}) + "\n")
        fh.write(json.dumps({"seq": 3, "event": "pair",
                             "book": Path(fname).name, "a": "Ann",
                             "b": "Bob", "score": 0.91}) + "\n")
        fh.write(json.dumps({"seq": 4, "event": "decision", "n": 1,
                             "used": 1, "cap": 50, "granted": True})
                 + "\n")
    print(f"  Run events: {path}", flush=True)

if "--resume" in argv:
    print("  --resume: 2/2 chapters already complete, skipping "
          "their LLM calls", flush=True)
    print("  Preview saved (pipeline=v2)", flush=True)
    print("  Run complete: Stub Book", flush=True)
    sys.exit(0)

if "hang" in Path(fname).name:
    import re as _re
    key = _re.sub(r"[^a-zA-Z0-9_-]+", "_",
                  Path(fname).stem).strip("_") or "book"
    ckpt = here / "preview" / ".checkpoints" / key
    ckpt.mkdir(parents=True, exist_ok=True)
    (ckpt / "chapter_0.json").write_text("{}", encoding="utf-8")
    (ckpt / "meta.json").write_text("{}", encoding="utf-8")

    def _on_sigint(sig, frame):
        print("  Checkpoint saved. Run with --resume to continue.",
              flush=True)
        sys.exit(3)

    signal.signal(signal.SIGINT, _on_sigint)
    while True:
        time.sleep(0.05)

print("  Preview saved (pipeline=v2)", flush=True)
print("  Run complete: Stub Book", flush=True)
sys.exit(0)
'''


def _stub_ctx(root):
    root = Path(root)
    script = root / "repo"
    (script / "preview").mkdir(parents=True)
    (script / "process-portable.py").write_text(_STUB, encoding="utf-8")
    learn = root / "learned"
    learn.mkdir()
    imp = root / "import"
    imp.mkdir()
    (imp / "hang.epub").write_bytes(b"fake")
    (imp / "quick.epub").write_bytes(b"fake")
    _make_epub(imp / "coverbook.epub", cover=b"\xff\xd8COVER")
    (script / "config.json").write_text(
        json.dumps({"import_dir": str(imp)}), encoding="utf-8")
    backup = root / "backups"
    ctx = portal.PortalContext(learn_dir=learn, script_dir=script,
                               backup_dir=backup)
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
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        self._td.cleanup()

    def _req(self, method, path, payload=None):
        status, raw, headers = self._req_raw(method, path, payload)
        return status, raw.decode("utf-8", "replace"), headers

    def _req_raw(self, method, path, payload=None):
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
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def _launch(self, payload):
        status, body, _h = self._req("POST", "/api/launch", payload)
        self.assertEqual(status, 200, body)
        return json.loads(body)["job_id"]


class TestLaunchHTTP(_ServerCase):
    def test_preview_launch_runs_and_reports(self):
        job_id = self._launch({"action": "preview",
                               "file_name": "quick.epub"})
        self.assertTrue(_wait_for(lambda: self._job(job_id)["state"]
                                  == "done"))
        snap = self._job(job_id)
        self.assertIn("Preview saved", snap["now"] + " ".join(
            f["text"] for f in snap["feed"]))
        status, body, _h = self._req("GET", f"/run/{job_id}")
        self.assertEqual(status, 200)
        # The shell carries the job's identity (label + flags);
        # live content arrives via the snapshot the page polls.
        self.assertIn("Preview — quick.epub", body)
        self.assertIn(f"const JOB_ID = {job_id};", body)

    def _job(self, job_id):
        status, body, _h = self._req("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def _raw_post(self, path, body, headers):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body.encode("utf-8"), headers=headers,
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_foreign_origin_post_is_403_and_launches_nothing(self):
        # A web page on another origin must not be able to fire
        # a launch: the Host check validates the destination,
        # which a cross-site request satisfies by construction.
        status, _b = self._raw_post(
            "/api/launch",
            '{"action": "write", "confirm": true}',
            {"Content-Type": "application/json",
             "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, body, _h = self._req("GET", "/api/jobs")
        self.assertEqual(json.loads(body)["jobs"], [])

    def test_text_plain_post_is_400_and_launches_nothing(self):
        # The simple-request vector: no preflight is triggered
        # by text/plain, so the JSON-only rule is what stops it.
        status, _b = self._raw_post(
            "/api/launch",
            '{"action": "write", "confirm": true}',
            {"Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        status, body, _h = self._req("GET", "/api/jobs")
        self.assertEqual(json.loads(body)["jobs"], [])

    def test_same_origin_post_still_launches(self):
        status, _b = self._raw_post(
            "/api/backup", "{}",
            {"Content-Type": "application/json",
             "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_write_run_without_confirm_is_400(self):
        status, body, _h = self._req(
            "POST", "/api/launch", {"action": "write"})
        self.assertEqual(status, 400)
        self.assertIn("confirm", body)

    def test_unknown_action_is_400(self):
        status, _body, _h = self._req(
            "POST", "/api/launch", {"action": "corpus-remove"})
        self.assertEqual(status, 400)

    def test_backup_fires_and_reports_the_manifest(self):
        status, body, _h = self._req("POST", "/api/backup", {})
        self.assertEqual(status, 200, body)
        job_id = json.loads(body)["job_id"]
        self.assertTrue(_wait_for(lambda: self._job(job_id)["state"]
                                  == "done"))
        snap = self._job(job_id)
        self.assertIsNotNone(snap["backup"])
        self.assertEqual(snap["backup"]["last_backup"],
                         "2026-10-16T13:00:00")

    def test_cover_route_serves_only_picker_files(self):
        status, raw, headers = self._req_raw(
            "GET", "/cover?name=coverbook.epub")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        self.assertEqual(raw, b"\xff\xd8COVER")
        status, _b, _h = self._req("GET", "/cover?name=hang.epub")
        self.assertEqual(status, 404)  # in the picker, but no cover
        status, _b, _h = self._req(
            "GET", "/cover?name=../process-portable.py")
        self.assertEqual(status, 404)

    def test_ingest_picker_lists_import_files(self):
        status, body, _h = self._req("GET", "/ingest")
        self.assertEqual(status, 200)
        self.assertIn("coverbook.epub", body)
        self.assertIn("hang.epub", body)


class TestStopResumeHTTP(_ServerCase):
    def _job(self, job_id):
        status, body, _h = self._req("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_queue_stop_resume_end_to_end(self):
        hang_id = self._launch({"action": "preview",
                                "file_name": "hang.epub"})
        self.assertTrue(_wait_for(
            lambda: self._job(hang_id)["state"] == "running"))
        # A second writer queues behind the running one (§7).
        ingest_id = self._launch({"action": "ingest",
                                  "file_name": "quick.epub"})
        self.assertEqual(self._job(ingest_id)["state"], "queued")
        # The Home strip tells the same truth.
        status, body, _h = self._req("GET", "/api/home")
        strip = json.loads(body)["strip"]
        labels = " ".join(j["label"] for j in strip["jobs"])
        self.assertIn("hang.epub", labels)
        # Events from the run artifact reach the job snapshot.
        snap = self._job(hang_id)
        self.assertTrue(_wait_for(
            lambda: self._job(hang_id)["events"]["pairs"] == 1))
        self.assertEqual(self._job(hang_id)["events"]["decision"],
                         {"used": 1, "cap": 50})
        # Stop: SIGINT -> the child checkpoints -> job is stopped
        # and offers Resume (checkpoints exist on disk).
        status, _b, _h = self._req(
            "POST", f"/api/jobs/{hang_id}/stop", {})
        self.assertEqual(status, 200)
        self.assertTrue(_wait_for(
            lambda: self._job(hang_id)["state"] == "stopped"))
        snap = self._job(hang_id)
        self.assertTrue(snap["can_resume"])
        self.assertTrue(any("Checkpoint saved" in f["text"]
                            for f in snap["feed"]))
        # The queued ingest now runs to completion on its own.
        self.assertTrue(_wait_for(
            lambda: self._job(ingest_id)["state"] == "done"))
        # Resume: a new job whose argv is the original + --resume.
        status, body, _h = self._req(
            "POST", f"/api/jobs/{hang_id}/resume", {})
        self.assertEqual(status, 200, body)
        resume_id = json.loads(body)["job_id"]
        self.assertTrue(_wait_for(
            lambda: self._job(resume_id)["state"] == "done"))
        calls = (self.script / "stub-calls.log").read_text(
            encoding="utf-8")
        resume_calls = [json.loads(l) for l in calls.splitlines()
                        if "--resume" in l]
        self.assertEqual(len(resume_calls), 1)
        self.assertIn("--preview", resume_calls[0])
        self.assertIn("--emit-run-events", resume_calls[0])
        # The same job cannot be resumed twice: its claim is
        # spent (a second launch could republish a write run
        # with no fresh confirm).
        status, _b, _h = self._req(
            "POST", f"/api/jobs/{hang_id}/resume", {})
        self.assertEqual(status, 400)

    def test_resume_refused_without_checkpoints(self):
        job_id = self._launch({"action": "preview",
                               "file_name": "quick.epub"})
        self.assertTrue(_wait_for(
            lambda: self._job(job_id)["state"] == "done"))
        self.assertFalse(self._job(job_id)["can_resume"])
        status, _b, _h = self._req(
            "POST", f"/api/jobs/{job_id}/resume", {})
        self.assertEqual(status, 400)

    def test_sse_stream_delivers_a_snapshot(self):
        import http.client
        job_id = self._launch({"action": "preview",
                               "file_name": "hang.epub"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=10)
        conn.request("GET", f"/api/jobs/{job_id}/stream")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream",
                      resp.getheader("Content-Type"))
        buf = b""
        deadline = time.time() + 8
        while b"data:" not in buf and time.time() < deadline:
            chunk = resp.read1(4096)
            if not chunk:
                break
            buf += chunk
        conn.close()
        self.assertIn(b"data:", buf)
        self._req("POST", f"/api/jobs/{job_id}/stop", {})


if __name__ == "__main__":
    unittest.main()
