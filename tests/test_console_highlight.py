"""Console name highlighting: per-character colours in the reader.

The pipeline publishes each chapter's full text and, once its
extraction returns, the names identified in it. The store assigns
each character a colour ordinal in first-appearance order and
marks first appearances; the reader lights names in their colours
across the whole chapter, pages through an in-flight chapter on a
timing estimate, and lights a selected relationship's endpoints in
its pinned evidence quote.

Stdlib unittest only. Pilot tests are skip-guarded on the optional
textual package, as in Phases 2 and 3.
Run: python -m unittest discover -s tests -v
"""
import asyncio
import importlib.util
import time
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

import console_events as ce  # noqa: E402
import console_app as capp  # noqa: E402

try:
    import textual  # noqa: F401
    _HAVE_TEXTUAL = True
except Exception:
    _HAVE_TEXTUAL = False


def _ev(kind, book="b1", **payload):
    return {"v": ce.EVENT_VERSION, "kind": kind, "book_id": book,
            "stage": payload.pop("stage", None), "payload": payload}


def _char(store, name, book="b1"):
    store.apply(_ev("candidate_upsert", book, table="characters",
                    key=name, record={"name": name, "chapters": 1,
                                      "status": "candidate"}))


def _chapter(store, idx, excerpt=None, names=None, book="b1"):
    payload = {"pass_id": "identity", "index": idx,
               "label": f"Chapter {idx + 1}", "state": "started"}
    if excerpt is not None:
        payload["excerpt"] = excerpt
    store.apply(_ev("chapter_state", book, **payload))
    if names is not None:
        store.apply(_ev("chapter_state", book, pass_id="identity",
                        index=idx, state="identified", names=names))


class TestStoreColours(unittest.TestCase):
    def test_colours_assigned_in_first_appearance_order(self):
        store = ce.ConsoleStore()
        _char(store, "Ada")
        _char(store, "Bob")
        _char(store, "Ada")  # repeat sighting: no new ordinal
        store.apply(_ev("candidate_upsert", table="relationships",
                        key="pair:x", record={"from": "Ada"}))
        _char(store, "Cid")
        book = store.books["b1"]
        self.assertEqual(book.char_colors,
                         {"Ada": 0, "Bob": 1, "Cid": 2})

    def test_chapter_events_merge_and_mark_first_appearances(self):
        store = ce.ConsoleStore()
        _chapter(store, 0, excerpt="Ada met Bob.", names=["Ada", "Bob"])
        _chapter(store, 1, excerpt="Bob met Cid.", names=["Bob", "Cid"])
        book = store.books["b1"]
        ch0 = book.chapters[("identity", 0)]
        # The identified event must not erase the started event's
        # text, nor the other way round.
        self.assertEqual(ch0["excerpt"], "Ada met Bob.")
        self.assertEqual(ch0["names"], ["Ada", "Bob"])
        self.assertEqual(ch0["new_names"], ["Ada", "Bob"])
        ch1 = book.chapters[("identity", 1)]
        self.assertEqual(ch1["new_names"], ["Cid"])
        self.assertEqual(book.char_first_chapter,
                         {"Ada": 0, "Bob": 0, "Cid": 1})


class TestNameSpans(unittest.TestCase):
    def test_longest_name_wins_and_case_is_ignored(self):
        styles = {"Mira": "bold red", "Mira Sorrengail": "bold blue",
                  "Tairn": "bold gold"}
        text = "mira sorrengail waved at MIRA. Tairn landed."
        spans = capp._name_spans(text, styles)
        got = [(text[s:e], st) for s, e, st in spans]
        self.assertEqual(got, [("mira sorrengail", "bold blue"),
                               ("MIRA", "bold red"),
                               ("Tairn", "bold gold")])

    def test_word_boundaries(self):
        # Glued to other letters, a name does not match; set off
        # by punctuation it does (the visualizer's rule — a hyphen
        # is a boundary, so "tairn-like" would match too).
        spans = capp._name_spans("A Tairnish shape, Tairn.",
                                 {"Tairn": "bold gold"})
        self.assertEqual(len(spans), 1)


class TestPublisherHooks(unittest.TestCase):
    def _ui_with_observer(self):
        ui = pp.PipelineUI()
        obs = ce.ConsoleObserver()
        ui.attach_console(obs)
        return ui, obs

    def _events(self, obs):
        return [(e["kind"], e["payload"]) for e in obs.drain()]

    def test_chapter_names_published_against_their_chapter(self):
        ui, obs = self._ui_with_observer()
        ui.viz_chapter("bk", "Chapter 1", "Ada met Bob.", chap_idx=0)
        ui.viz_characters("bk", [{"name": "Ada"}, {"name": "Bob"},
                                 {"name": "Ada"}])
        chapters = [p for k, p in self._events(obs)
                    if k == "chapter_state"]
        self.assertEqual(chapters[0]["excerpt"], "Ada met Bob.")
        identified = [p for p in chapters if p.get("names")]
        self.assertEqual(len(identified), 1)
        self.assertEqual(identified[0]["index"], 0)
        self.assertEqual(identified[0]["names"], ["Ada", "Bob"])

    def test_full_chapter_text_with_a_pathology_cap(self):
        ui, obs = self._ui_with_observer()
        ui.viz_chapter("bk", "Chapter 1", "x" * 130_000, chap_idx=0)
        chapters = [p for k, p in self._events(obs)
                    if k == "chapter_state"]
        self.assertEqual(len(chapters[0]["excerpt"]),
                         pp._CONSOLE_CHAPTER_TEXT_CAP)

    def test_names_without_a_started_chapter_publish_nothing(self):
        ui, obs = self._ui_with_observer()
        ui.viz_characters("bk", [{"name": "Ada"}])
        kinds = [k for k, _p in self._events(obs)]
        self.assertNotIn("chapter_state", kinds)
        self.assertIn("candidate_upsert", kinds)

    def test_no_observer_no_tracking_no_noise(self):
        ui = pp.PipelineUI()
        ui.viz_chapter("bk", "Chapter 1", "Ada.", chap_idx=0)
        ui.viz_characters("bk", [{"name": "Ada"}])
        self.assertEqual(ui._console_chapter_idx, {})

    def test_work_units_stream_from_book_start_and_advance(self):
        ui, obs = self._ui_with_observer()
        ui.book_start("bk", "Daemon", total=144)
        ui.advance("bk")
        ui.advance("bk", 5)
        counts = [p["counts"] for k, p in self._events(obs)
                  if k == "counts_snapshot"]
        self.assertEqual(counts[0], {"work_done": 0,
                                     "work_total": 144})
        self.assertEqual(counts[-1], {"work_done": 6})
        # After book_done the counter is gone: stray advances for
        # the finished book publish nothing more.
        ui.book_done("bk")
        ui.advance("bk")
        counts = [p for k, p in self._events(obs)
                  if k == "counts_snapshot"]
        self.assertEqual(counts, [])

    def test_work_units_inert_without_observer(self):
        ui = pp.PipelineUI()
        ui.book_start("bk", "Daemon", total=10)
        ui.advance("bk", 3)
        ui.book_done("bk")
        self.assertEqual(ui._console_work, {})


def _seeded_reader(book_text_ch1=None):
    """A book mid-identity: ch2 identified (Tairn old, Mira new)."""
    obs = ce.ConsoleObserver()
    store = ce.ConsoleStore()
    obs.publish("book_state", book_id="b1",
                payload={"state": "active", "title": "Fourth Wing"})
    obs.publish("stage_state", book_id="b1", stage="identity",
                payload={"state": "active"})
    for name in ("Tairn", "Mira Sorrengail", "Mira"):
        obs.publish("candidate_upsert", book_id="b1", payload={
            "table": "characters", "key": name,
            "record": {"name": name, "chapters": 1,
                       "status": "candidate"}})
    obs.publish("chapter_state", book_id="b1", payload={
        "pass_id": "identity", "index": 0, "label": "Chapter 1",
        "state": "started", "excerpt": "Tairn landed."})
    obs.publish("chapter_state", book_id="b1", payload={
        "pass_id": "identity", "index": 0, "state": "identified",
        "names": ["Tairn"]})
    obs.publish("chapter_state", book_id="b1", payload={
        "pass_id": "identity", "index": 1, "label": "Chapter 2",
        "state": "started",
        "excerpt": book_text_ch1 or
        "Mira Sorrengail watched Mira wave. Tairn landed hard."})
    obs.publish("chapter_state", book_id="b1", payload={
        "pass_id": "identity", "index": 1, "state": "identified",
        "names": ["Mira Sorrengail", "Mira", "Tairn"]})
    return obs, store, {}


@unittest.skipUnless(_HAVE_TEXTUAL, "textual not installed")
class TestHighlightPilot(unittest.TestCase):
    def test_reader_colours_and_first_appearance_pop(self):
        async def main():
            obs, store, control = _seeded_reader()
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                plain, spans = app._reader_sig
                self.assertIn("Mira Sorrengail watched", plain)
                by_text = {}
                for s, e, st in spans:
                    by_text.setdefault(plain[s:e], []).append(st)
                # Tairn appeared in ch1: plain colour, no underline.
                tairn = by_text["Tairn"][0]
                self.assertNotIn("underline", tairn)
                # Mira is new in ch2: her colour, underlined. The
                # full name wins over the short form at its span.
                mira = by_text["Mira"][0]
                self.assertIn("underline", mira)
                self.assertNotEqual(tairn, mira)
                self.assertIn("Mira Sorrengail", by_text)
        asyncio.run(main())

    def test_in_flight_chapter_colours_returning_cast(self):
        # Regression: the displayed chapter is normally the one in
        # flight, whose own names have not landed. It must still
        # light every character identified in earlier chapters.
        async def main():
            obs = ce.ConsoleObserver()
            store = ce.ConsoleStore()
            obs.publish("book_state", book_id="b1", payload={
                "state": "active", "title": "Fourth Wing"})
            obs.publish("stage_state", book_id="b1",
                        stage="identity", payload={"state": "active"})
            for name in ("Tairn", "Mira"):
                obs.publish("candidate_upsert", book_id="b1",
                            payload={
                                "table": "characters", "key": name,
                                "record": {"name": name, "chapters": 1,
                                           "status": "candidate"}})
            obs.publish("chapter_state", book_id="b1", payload={
                "pass_id": "identity", "index": 0,
                "label": "Chapter 1", "state": "started",
                "excerpt": "Tairn landed. Mira watched."})
            obs.publish("chapter_state", book_id="b1", payload={
                "pass_id": "identity", "index": 0,
                "state": "identified", "names": ["Tairn", "Mira"]})
            obs.publish("chapter_state", book_id="b1", payload={
                "pass_id": "identity", "index": 1,
                "label": "Chapter 2", "state": "started",
                "excerpt": "Mira ran. Tairn followed Mira."})
            app = capp._build_app_class()(obs, store, {})
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                plain, spans = app._reader_sig
                self.assertIn("Chapter 2", plain)
                by_text = {}
                for s, e, st in spans:
                    by_text.setdefault(plain[s:e], []).append(st)
                # Both returning names are lit in the in-flight
                # chapter; neither pops (neither is new here).
                self.assertIn("Tairn", by_text)
                self.assertIn("Mira", by_text)
                for st in by_text["Tairn"] + by_text["Mira"]:
                    self.assertNotIn("underline", st)
        asyncio.run(main())

    def test_characters_tab_dot_matches_reader_colour(self):
        async def main():
            from textual.coordinate import Coordinate
            from textual.widgets import DataTable
            obs, store, control = _seeded_reader()
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                table = app.query_one("#chars", DataTable)
                cell = table.get_cell_at(Coordinate(0, 0))
                self.assertTrue(cell.plain.startswith("● "))
                name = cell.plain[2:]
                colour = app._colour_for(store.books["b1"], name)
                styles = [str(s.style) for s in cell.spans]
                self.assertIn(colour, styles)
        asyncio.run(main())

    def test_evidence_endpoints_light_up(self):
        async def main():
            from textual.widgets import DataTable, TabbedContent
            obs, store, control = _seeded_reader()
            obs.publish("verdict_upsert", book_id="b1", payload={
                "table": "relationships",
                "row_key": "pair:mira|tairn|type:bonded"
                           "|origin:extracted",
                "decision": "kept", "p": 0.9,
                "final_state": "validated",
                "record": {
                    "from": "Mira", "to": "Tairn",
                    "claimed_type": "bonded",
                    "evidence": {
                        "quote": "Mira laughed. Tairn landed.",
                        "source": "extracted", "grade": "binding",
                        "chapter": "Chapter 2"}}})
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.query_one("#tabs", TabbedContent).active = "t-rels"
                await pilot.pause()
                table = app.query_one("#rels", DataTable)
                table.focus()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                plain, spans = app._reader_sig
                self.assertIn("Mira laughed. Tairn landed.", plain)
                book = store.books["b1"]
                styles = {st for _s, _e, st in spans}
                self.assertIn("bold " + app._colour_for(book, "Mira"),
                              styles)
                self.assertIn("bold " + app._colour_for(book, "Tairn"),
                              styles)
        asyncio.run(main())

    def test_progress_bar_and_header_percent(self):
        async def main():
            from textual.widgets import ProgressBar
            obs, store, control = _seeded_reader()
            obs.publish("counts_snapshot", book_id="b1", payload={
                "counts": {"work_done": 115, "work_total": 230}})
            app = capp._build_app_class()(obs, store, control)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                bar = app.query_one("#prog", ProgressBar)
                self.assertEqual(bar.total, 1000)
                self.assertEqual(bar.progress, 500)
                hdr = str(app.query_one("#hdr").render())
                self.assertIn("50%", hdr)
                rail = str(app.query_one("#rail").render())
                self.assertIn("steps 115/230", rail)
        asyncio.run(main())

    def test_auto_page_advances_and_yields(self):
        async def main():
            from textual.containers import VerticalScroll
            lines = [f"Line {i} of the chapter." for i in range(200)]
            obs, store, control = _seeded_reader(
                book_text_ch1="\n".join(lines))
            # Rewind: the seeded ch2 is identified; make it a fresh
            # in-flight chapter instead by using a new book state.
            store2 = ce.ConsoleStore()
            obs2 = ce.ConsoleObserver()
            obs2.publish("book_state", book_id="b1", payload={
                "state": "active", "title": "Daemon"})
            obs2.publish("stage_state", book_id="b1",
                         stage="identity",
                         payload={"state": "active"})
            obs2.publish("chapter_state", book_id="b1", payload={
                "pass_id": "identity", "index": 0,
                "label": "Chapter 1", "state": "started",
                "excerpt": "\n".join(lines)})
            app = capp._build_app_class()(obs2, store2, {})
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                scroller = app.query_one("#reader-scroll",
                                         VerticalScroll)
                self.assertGreater(scroller.max_scroll_y, 0)
                key = app._pg_chapter
                self.assertIsNotNone(key)
                # The chapter has been "in flight" 30s against a
                # 10s rolling average: the pager is at the end.
                app._pg_open_since[key] = time.monotonic() - 30
                app._pg_durations = [10.0]
                app._auto_page()
                await pilot.pause()
                self.assertGreater(scroller.scroll_y, 0)
                target = scroller.scroll_y
                # The user takes the page back to the top; the
                # pager notices the divergence and stands down.
                scroller.scroll_to(y=0, animate=False)
                await pilot.pause()
                app._auto_page()
                self.assertTrue(app._pg_manual)
                self.assertEqual(scroller.scroll_y, 0)
                self.assertLess(0, target)
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
