"""Phase 2 tests for the Textual inspection console: --tui
selection and fallback, log routing, cancel delivery, and the
Textual application driven by Textual's Pilot.

The Pilot tests are skip-guarded on the textual package: the
processor treats Textual as an optional dependency, so the suite
must pass on machines without it. Run with textual installed to
exercise them.
Run: python -m unittest discover -s tests -v
"""
import asyncio
import io
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import importlib.util

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import console_events as ce  # noqa: E402
import console_app as capp  # noqa: E402

_MOD_PATH = str(_ROOT / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_HAS_TEXTUAL = importlib.util.find_spec("textual") is not None


class TestFlagAndSelection(unittest.TestCase):
    def test_tui_flag_exists_and_is_classified(self):
        args = pp._build_parser().parse_args(["--tui"])
        self.assertTrue(args.tui)
        self.assertIn("tui", pp._PROFILE_PERSISTABLE)
        self.assertNotIn("tui", pp._PROFILE_EXCLUDE)

    def test_unavailable_without_tty(self):
        # The test runner is never a TTY, so selection must refuse
        # with the terminal reason rather than starting a console.
        self.assertEqual(pp._tui_unavailable_reason(),
                         "not an interactive terminal")

    def test_run_with_tui_falls_back_without_calling_run_once(self):
        called = []
        orig = pp.run_once
        pp.run_once = lambda *a: called.append(a) or True
        try:
            args = pp._build_parser().parse_args(["--tui"])
            out = io.StringIO()
            with redirect_stdout(out):
                result = pp._run_with_tui(args)
        finally:
            pp.run_once = orig
        self.assertIsNone(result)
        self.assertEqual(called, [])
        self.assertIn("--tui:", out.getvalue())


class TestStoreRunLog(unittest.TestCase):
    def test_bookless_log_lands_in_run_log(self):
        store = ce.ConsoleStore()
        store.apply({"v": ce.EVENT_VERSION, "seq": 1, "ts": 0.0,
                     "run_id": None, "book_id": None, "stage": None,
                     "kind": "log", "payload": {"text": "hello"}})
        self.assertEqual(list(store.run_log), ["hello"])

    def test_book_log_lands_in_both(self):
        store = ce.ConsoleStore()
        store.apply({"v": ce.EVENT_VERSION, "seq": 1, "ts": 0.0,
                     "run_id": None, "book_id": "b1", "stage": None,
                     "kind": "log", "payload": {"text": "hi"}})
        self.assertEqual(list(store.run_log), ["hi"])
        self.assertEqual(list(store.books["b1"].log_tail), ["hi"])


class TestLogWriter(unittest.TestCase):
    def test_lines_become_events_and_detach_passes_through(self):
        obs = ce.ConsoleObserver()
        real = io.StringIO()
        writer = capp._LogWriter(obs, real)
        writer.write("first line\npartial")
        writer.write(" rest\n")
        writer.flush()
        texts = [e["payload"]["text"] for e in obs.drain()
                 if e["kind"] == "log"]
        self.assertEqual(texts, ["first line", "partial rest"])
        writer.detached = True
        writer.write("plain again\n")
        self.assertEqual(real.getvalue(), "plain again\n")


class TestWorkerBody(unittest.TestCase):
    def test_crash_traceback_is_published_as_log_events(self):
        obs = ce.ConsoleObserver()
        outcome, control = {}, {}

        def runner():
            raise RuntimeError("boom-marker")

        capp._worker_body(obs, runner, outcome, control, {})
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["error"])
        texts = [e["payload"]["text"] for e in obs.drain()
                 if e["kind"] == "log"]
        self.assertTrue(any("RuntimeError" in t and "boom-marker" in t
                            for t in texts), texts)

    def test_prints_route_to_log_events(self):
        obs = ce.ConsoleObserver()
        outcome, control = {}, {}

        def runner():
            print("hello from the run")
            return True

        capp._worker_body(obs, runner, outcome, control, {})
        self.assertTrue(outcome["ok"])
        texts = [e["payload"]["text"] for e in obs.drain()
                 if e["kind"] == "log"]
        self.assertIn("hello from the run", texts)


class TestDeliverInterrupt(unittest.TestCase):
    def test_keyboard_interrupt_reaches_worker(self):
        seen = []

        def worker():
            try:
                while True:
                    time.sleep(0.01)
            except KeyboardInterrupt:
                seen.append(True)

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.05)
        self.assertTrue(capp._deliver_interrupt(t))
        t.join(timeout=5)
        self.assertEqual(seen, [True])
        self.assertFalse(capp._deliver_interrupt(t))


def _seeded_console():
    obs = ce.ConsoleObserver()
    store = ce.ConsoleStore()
    obs.publish("run_state", payload={"state": "running"})
    obs.publish("book_state", book_id="b1",
                payload={"state": "active", "title": "Daemon"})
    obs.publish("candidate_upsert", book_id="b1", payload={
        "table": "characters", "key": "Peter Sebeck",
        "record": {"name": "Peter Sebeck", "chapters": 9,
                   "status": "candidate"}})
    obs.publish("candidate_upsert", book_id="b1", payload={
        "table": "characters", "key": "Jon Ross",
        "record": {"name": "Jon Ross", "chapters": 7,
                   "status": "candidate"}})
    obs.publish("chapter_state", book_id="b1", stage="identity",
                payload={"pass_id": "identity", "index": 3,
                         "label": "Chapter 3", "state": "started",
                         "excerpt": "Sebeck stared at the daemon."})
    control = {"detach": lambda: control.update(detached=True),
               "cancel": lambda: control.update(cancelled=True),
               "result": None}
    return obs, store, control


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class TestConsoleAppPilot(unittest.TestCase):
    def test_renders_store_and_browses(self):
        async def main():
            obs, store, control = _seeded_console()
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                table = app.query_one("#chars")
                self.assertEqual(table.row_count, 2)
                hdr = str(app.query_one("#hdr").render())
                self.assertIn("Daemon", hdr)
                self.assertIn("LIVE", hdr)
                await pilot.press("[")   # only one book: stays put
                await pilot.press("l")   # follow live
                await pilot.press("o")   # log screen
                await pilot.pause()
                self.assertEqual(len(app.screen_stack), 2)
                await pilot.press("escape")
                self.assertEqual(len(app.screen_stack), 1)
        asyncio.run(main())

    def test_markup_shaped_book_text_renders_literally(self):
        async def main():
            obs, store, control = _seeded_console()
            obs.publish("chapter_state", book_id="b1", stage="identity",
                        payload={"pass_id": "identity", "index": 4,
                                 "label": "Chapter [red]4[/red]",
                                 "state": "started",
                                 "excerpt": "He clicked "
                                            "[link=http://x]the link[/link] "
                                            "and [b]ran[/b]."})
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                rendered = str(app.query_one("#reader").render())
                self.assertIn("[link=http://x]the link[/link]", rendered)
                self.assertIn("[red]4[/red]", rendered)
        asyncio.run(main())

    def test_quit_screen_cancel_invokes_control(self):
        async def main():
            obs, store, control = _seeded_console()
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("q")
                await pilot.pause()
                self.assertEqual(len(app.screen_stack), 2)
                await pilot.click("#cancel")
                await pilot.pause()
            self.assertTrue(control.get("cancelled"))
        asyncio.run(main())

    def test_quit_screen_detach_invokes_control(self):
        async def main():
            obs, store, control = _seeded_console()
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("q")
                await pilot.pause()
                await pilot.click("#plain")
                await pilot.pause()
            self.assertTrue(control.get("detached"))
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
