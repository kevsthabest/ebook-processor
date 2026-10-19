"""Textual inspection console application (Phase 2).

Renders the console_events store while a run proceeds on a worker
thread. Read-only by design: the only keys that affect anything
outside the view are the quit flow's choices (spec §7) — detach and
keep running, or request cancellation through the pipeline's own
KeyboardInterrupt path.

Textual is imported lazily inside run_console / _build_app_class so
that importing this module, and therefore the processor, never
requires the package. The pipeline publishes events through its
observer; this module never calls pipeline functions except the
runner callable it is handed.
"""

import ctypes
import threading

from console_events import STAGES

_POLL_SECONDS = 0.25  # store refresh cap: 4 Hz (spec §11.5)

_STAGE_MARKS = {
    "complete": "✓",
    "active": "●",
    "failed": "✗",
    "skipped": "–",
    "not_applicable": "–",
}


def _deliver_interrupt(thread):
    """Raise KeyboardInterrupt inside the worker thread.

    The pipeline's cancellation contract is KeyboardInterrupt:
    run_once catches it, stops current chapters, and cancels pending
    books. There is no cross-thread cancel flag to set instead, so
    the console's Cancel choice delivers the same signal Ctrl+C
    would. Returns True when the interrupt was scheduled.
    """
    if thread is None or not thread.is_alive():
        return False
    tid = ctypes.c_long(thread.ident)
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        tid, ctypes.py_object(KeyboardInterrupt))
    if res > 1:  # more than one thread state hit: undo, do not guess
        ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, None)
        return False
    return res == 1


class _LogWriter:
    """Line-buffered stdout/stderr stand-in for the worker thread.

    The pipeline prints freely; under a Textual application those
    prints would corrupt the terminal, so while the console runs
    they are routed into log events instead (spec §4.4). After a
    detach ("continue without the console") writes pass through to
    the real stream, because the terminal belongs to the run again.
    """

    def __init__(self, observer, real):
        self._observer = observer
        self._real = real
        self._buf = ""
        self.detached = False

    def write(self, text):
        if self.detached:
            return self._real.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._observer.publish("log", payload={"text": line})
        return len(text)

    def flush(self):
        if self.detached:
            return self._real.flush()
        if self._buf.strip():
            self._observer.publish("log", payload={"text": self._buf})
        self._buf = ""


def _build_app_class():
    """Define the Textual App. Imported here so the module itself
    stays importable on machines without Textual."""
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import (Button, DataTable, Footer, Log,
                                 Static, TabbedContent, TabPane)

    class QuitScreen(ModalScreen):
        def compose(self):
            yield Static("The run is still active. q never kills "
                         "a run silently:")
            yield Button("Back", id="back", variant="primary")
            yield Button("Continue without the console", id="plain")
            yield Button("Cancel run", id="cancel", variant="error")

        def on_button_pressed(self, event):
            self.dismiss(event.button.id)

    class LogScreen(ModalScreen):
        BINDINGS = [("escape", "close", "Close")]

        def __init__(self, store):
            super().__init__()
            self._store = store
            self._seen = 0

        def compose(self):
            yield Log(id="runlog")

        def on_mount(self):
            self._pump()
            self.set_interval(_POLL_SECONDS, self._pump)

        def _pump(self):
            log = self.query_one("#runlog", Log)
            lines = list(self._store.run_log)
            for line in lines[self._seen:]:
                log.write_line(line)
            self._seen = len(lines)

        def action_close(self):
            self.dismiss()

    class ConsoleApp(App):
        CSS = """
        #hdr {height: 3; border-bottom: solid $accent;}
        #rail {height: 2; color: $text-muted;}
        #main {height: 1fr;}
        #reader {width: 58%; border-right: solid $accent; padding: 0 1;}
        #right {width: 42%;}
        #tabs {height: 1fr;}
        #detail {height: 9; border-top: solid $accent; padding: 0 1;}
        #op {height: 1; color: $text-muted;}
        .pane-hdr {height: 1; color: $text-muted;}
        #tropes {height: 55%;}
        """
        BINDINGS = [
            ("[", "prev_book", ""), ("]", "next_book", ""),
            ("l", "follow_live", "Follow live"),
            ("o", "open_log", "Log"),
            ("q", "quit_flow", "Quit"),
            ("ctrl+c", "cancel_run", ""),
        ]

        def __init__(self, observer, store, control):
            super().__init__()
            self._observer = observer
            self._store = store
            self._control = control  # dict: detach/cancel callbacks
            self.viewed = None
            self.follow = True
            self._seq_seen = -1
            self._selected = None  # (table_id, row_key) in the store
            self._merges_shown = False

        def compose(self) -> ComposeResult:
            yield Static(id="hdr")
            yield Static(id="rail")
            with Horizontal(id="main"):
                yield Static(id="reader")
                with Vertical(id="right"):
                    with TabbedContent(id="tabs"):
                        with TabPane("Characters", id="t-chars"):
                            yield DataTable(id="chars")
                        with TabPane("Relationships", id="t-rels"):
                            yield Static("", id="rels-hdr",
                                         classes="pane-hdr")
                            yield DataTable(id="rels")
                        with TabPane("Tropes & triggers", id="t-tt"):
                            yield Static("", id="tt-hdr",
                                         classes="pane-hdr")
                            yield DataTable(id="tropes")
                            yield DataTable(id="triggers")
                        with TabPane("Merges", id="t-merges"):
                            yield Static("", id="merges-hdr",
                                         classes="pane-hdr")
                            yield DataTable(id="merges")
                    yield Static(id="detail")
            yield Static(id="op")
            yield Footer()

        def on_mount(self):
            # Row cursor, not cell: the console's one gesture is
            # "select a result, see its evidence" — a row is the
            # unit of selection in every table.
            for wid, cols in (
                    ("#chars", ("Name", "Chapters", "Status")),
                    ("#rels", ("Pair", "Type", "Judgment", "Final",
                               "P")),
                    ("#tropes", ("Trope", "Ch", "State", "Conf")),
                    ("#triggers", ("Warning", "Severity", "Ch",
                                   "Gate")),
                    ("#merges", ("Pair", "Tier", "Outcome",
                                 "P / reason"))):
                table = self.query_one(wid, DataTable)
                table.cursor_type = "row"
                table.add_columns(*cols)
            # The Merges tab does not exist until merge review
            # starts (spec §6.5): an empty permanent tab is noise.
            self.query_one("#tabs", TabbedContent).hide_tab("t-merges")
            self.set_interval(_POLL_SECONDS, self._refresh)
            self._refresh()
            # The worker starts here, not before the app: Textual
            # replaces sys.stdout/sys.stderr with its own capture
            # when the app starts, so a redirect installed earlier
            # is overwritten for the whole run. Started from
            # on_mount, the worker's redirect is installed after
            # the capture and wins.
            start = self._control.get("start")
            if start is not None:
                start()

        # -- state refresh -------------------------------------------
        def _refresh(self):
            for event in self._observer.drain():
                self._store.apply(event)
            if self._store.last_seq == self._seq_seen:
                return
            self._seq_seen = self._store.last_seq
            live = self._store.live_book_id()
            if self.viewed is None or (self.follow and live is not None):
                self.viewed = live
            self._render()

        def _render(self):
            store = self._store
            hdr = self.query_one("#hdr", Static)
            rail = self.query_one("#rail", Static)
            op = self.query_one("#op", Static)
            book = store.books.get(self.viewed) if self.viewed else None
            if book is None:
                hdr.update("Waiting for the first book…")
                rail.update("")
                self.query_one("#reader", Static).update("")
                self.query_one("#detail", Static).update("")
                op.update("")
                return
            live_id = store.live_book_id()
            is_live = self.viewed == live_id and book.state == "active"
            title = book.title or self.viewed
            hdr.update(Text(
                f"{title}  ·  {'LIVE' if is_live else book.state.upper()}"
                f"  ·  run {store.run_state}"))
            rail.update(" › ".join(
                f"{_STAGE_MARKS.get(book.stages[sid], '○')} {label}"
                for sid, label in STAGES))
            self._render_chars(book)
            self._render_rels(book)
            self._render_tt(book)
            self._render_merges(book)
            self._render_reader(book, is_live)
            self._render_detail(book)
            idx = store.book_order.index(self.viewed) + 1
            counts = book.counts
            extra = ""
            if counts.get("decision_pool"):
                extra += (f" · pool {counts.get('decision_spent', 0)}"
                          f"/{counts['decision_pool']}")
            if counts.get("quotes_checked"):
                extra += (f" · quotes {counts.get('quotes_verified', 0)}"
                          f"/{counts['quotes_checked']}")
            if counts.get("retrieval_attempted"):
                extra += (f" · retrieval "
                          f"{counts.get('retrieval_supported', 0)}"
                          f"/{counts['retrieval_attempted']}")
            op.update(
                f"Viewing book {idx} of {len(store.book_order)} · "
                f"Follow {'on' if self.follow else 'off'} · "
                f"[ ] books · L follow live{extra}")

        # -- table fills ----------------------------------------------
        def _render_chars(self, book):
            rows = sorted(book.rows["characters"].items(),
                          key=lambda kv: (-(kv[1].get("chapters") or 0),
                                          kv[1].get("name") or ""))
            table = self.query_one("#chars", DataTable)
            table.clear()
            for key, r in rows:
                table.add_row(r.get("name") or "",
                              str(r.get("chapters") or ""),
                              r.get("status") or "candidate", key=key)

        def _render_rels(self, book):
            items = list(book.rows["relationships"].items())

            def group(kv):
                key = kv[0]
                if key.startswith("pair:") and "origin:loser" in key:
                    return 1
                if key.startswith("pair:"):
                    return 0
                return 2

            items.sort(key=lambda kv: (group(kv),
                                       kv[1].get("from") or "",
                                       kv[1].get("to") or "",
                                       kv[1].get("type") or ""))
            table = self.query_one("#rels", DataTable)
            table.clear()
            for key, r in items:
                p = r.get("p")
                table.add_row(
                    f"{r.get('from') or ''} → {r.get('to') or ''}",
                    r.get("claimed_type") or r.get("type") or "",
                    r.get("decision") or r.get("status") or "",
                    r.get("final_state") or "—",
                    f"{p:.2f}" if isinstance(p, (int, float)) else "—",
                    key=key)
            counts = book.counts
            parts = []
            if counts.get("rel_candidates"):
                parts.append(f"{counts['rel_candidates']} candidates")
            if counts.get("reduced_claims"):
                parts.append(f"{counts['reduced_claims']} reduced claims")
            for label, ck in (("validated", "rel_validated"),
                              ("review", "rel_review_required"),
                              ("rejected", "rel_rejected")):
                if counts.get(ck):
                    parts.append(f"{counts[ck]} {label}")
            if "relationships" in book.capped:
                parts.append("display capped — final artefact has "
                             "the full set")
            self.query_one("#rels-hdr", Static).update(Text(
                " · ".join(parts) if parts else
                "Candidates become claims at the reduce, after "
                "merge review."))

        def _render_tt(self, book):
            tropes = sorted(book.rows["tropes"].items(),
                            key=lambda kv: (-(kv[1].get("chapters")
                                              or 0),
                                            kv[1].get("name") or ""))
            table = self.query_one("#tropes", DataTable)
            table.clear()
            for key, r in tropes:
                conf = r.get("confidence")
                table.add_row(
                    r.get("name") or key, str(r.get("chapters") or ""),
                    r.get("state") or r.get("decision") or "candidate",
                    f"{conf:.2f}" if isinstance(conf, (int, float))
                    else "—", key=key)
            trigs = sorted(book.rows["triggers"].items(),
                           key=lambda kv: kv[1].get("warning") or "")
            table = self.query_one("#triggers", DataTable)
            table.clear()
            for key, r in trigs:
                table.add_row(
                    r.get("warning") or "", r.get("severity")
                    or r.get("severity_claimed") or "",
                    str(r.get("chapters") or ""),
                    r.get("gate") or "", key=key)
            n_t, n_g = book.row_counts["tropes"], book.row_counts["triggers"]
            self.query_one("#tt-hdr", Static).update(Text(
                f"{n_t} tropes · {n_g} triggers" if (n_t or n_g) else
                "Content candidates appear here as Call B settles "
                "chapters."))

        def _render_merges(self, book):
            if not self._merges_shown and (
                    book.rows["merges"]
                    or book.stages.get("merge_review") not in
                    (None, "not_started")):
                self._merges_shown = True
                self.query_one("#tabs", TabbedContent).show_tab(
                    "t-merges")
            merges = sorted(book.rows["merges"].items(),
                            key=lambda kv: kv[1].get("name_a") or "")
            table = self.query_one("#merges", DataTable)
            table.clear()
            for key, r in merges:
                p = r.get("p")
                table.add_row(
                    f"{r.get('name_a') or ''} ↔ {r.get('name_b') or ''}",
                    r.get("tier") or "", r.get("outcome") or "",
                    f"{p:.2f}" if isinstance(p, (int, float))
                    else (r.get("reason") or ""), key=key)
            counts = book.counts
            self.query_one("#merges-hdr", Static).update(Text(
                f"{counts.get('merges_merged', 0)} merged · "
                f"{counts.get('merges_unresolved', 0)} unresolved"
                if book.rows["merges"] else ""))

        # -- reader + detail -------------------------------------------
        def _selected_row(self, book):
            if not self._selected or book is None:
                return None, None
            table_id, key = self._selected
            table = {"chars": "characters", "rels": "relationships",
                     "tropes": "tropes", "triggers": "triggers",
                     "merges": "merges"}.get(table_id)
            if table is None:
                return None, None
            return table_id, book.rows[table].get(key)

        def _render_reader(self, book, is_live=False):
            reader = self.query_one("#reader", Static)
            _tid, row = self._selected_row(book)
            if row is not None:
                ev = row.get("evidence")
                if isinstance(ev, dict) and ev.get("quote"):
                    reader.update(Text(
                        f"Evidence — {row.get('from', '')} → "
                        f"{row.get('to', '')}\n\n“{ev['quote']}”\n\n"
                        f"{ev.get('chapter') or ''} · source "
                        f"{ev.get('source') or ''} · grade "
                        f"{ev.get('grade') or '—'}"))
                    return
                if isinstance(ev, list) and ev and ev[0].get("quote"):
                    reader.update(Text(
                        f"Evidence — {row.get('warning', '')}\n\n"
                        f"“{ev[0]['quote']}”"))
                    return
            chapters = [c for (p, _i), c in sorted(
                book.chapters.items(), key=lambda kv: kv[0][1] or 0)]
            if chapters:
                last = chapters[-1]
                body = (f"{last.get('label') or 'Chapter'} — Identity "
                        f"extraction\n\n{last.get('excerpt') or ''}")
                if is_live and book.stages.get("identity") == "active":
                    body += ("\n\nEstimated input position · timing "
                             "based — not a reading cursor")
                # Text, not str: the label and excerpt are book text,
                # and Static parses plain strings as markup — a
                # bracketed passage would restyle or raise instead
                # of rendering literally.
                reader.update(Text(body))
            else:
                reader.update("No chapter has started yet.")

        def _render_detail(self, book):
            detail = self.query_one("#detail", Static)
            table_id, row = self._selected_row(book)
            if row is None:
                counts = book.counts
                pool = (f"{counts.get('decision_spent', 0)}/"
                        f"{counts['decision_pool']}"
                        if counts.get("decision_pool") else "— "
                        "(opens at the reduce)")
                detail.update(Text(
                    f"{book.row_counts['characters']} characters · "
                    f"{book.row_counts['relationships']} relationship "
                    f"rows · decision pool {pool}"))
                return
            lines = []
            if table_id == "chars":
                lines.append(row.get("name") or "")
                lines.append(f"Status {row.get('status') or 'candidate'}"
                             f" · {row.get('chapters') or 0} chapters")
                if row.get("aliases"):
                    lines.append("Aliases: "
                                 + ", ".join(row["aliases"]))
                if row.get("is_pov"):
                    lines.append("POV character")
                if row.get("merged_into"):
                    lines.append(f"Merged into {row['merged_into']}")
            elif table_id == "rels":
                lines.append(f"{row.get('from') or ''} → "
                             f"{row.get('to') or ''}")
                claimed = row.get("claimed_type") or row.get("type")
                shipped = row.get("shipped_type")
                tline = f"Claimed type {claimed or ''}"
                if shipped and shipped != claimed:
                    tline += f" · shipped as {shipped}"
                lines.append(tline)
                lines.append(
                    f"Judgment {row.get('decision') or '—'} · "
                    f"Final state {row.get('final_state') or '—'}")
                p = row.get("p")
                if isinstance(p, (int, float)):
                    lines.append(f"P {p:.2f}"
                                 + (f" · {row['model']}"
                                    if row.get("model") else ""))
                if row.get("note"):
                    lines.append(f"Note: {row['note']}")
                if row.get("revived"):
                    lines.append("Revived runner-up over "
                                 f"{row.get('revived_over') or ''}")
                if row.get("direction_contradiction"):
                    lines.append("Direction contradiction on this "
                                 "pair — a human decides which "
                                 "generation is right")
                if row.get("not_selected"):
                    lines.append("Pair-competition loser — audit "
                                 "row, never judged")
            elif table_id == "tropes":
                lines.append(row.get("name") or "")
                lines.append(f"State {row.get('state') or 'candidate'}"
                             f" · {row.get('chapters') or 0} chapters")
                if row.get("decision"):
                    lines.append(f"Gate: {row['decision']}")
                if row.get("justification"):
                    lines.append(str(row["justification"]))
                if row.get("evidence_outcome"):
                    lines.append(
                        f"Retrieval: {row['evidence_outcome']} · "
                        f"support {row.get('evidence_support') or '—'}"
                        f" · {row.get('evidence_passages', 0)} passages")
            elif table_id == "triggers":
                lines.append(row.get("warning") or "")
                sev = row.get("severity") or ""
                claimed = row.get("severity_claimed") or ""
                lines.append(f"Severity {sev}" + (
                    f" (claimed {claimed})" if claimed and
                    claimed != sev else "")
                             + f" · gate {row.get('gate') or '—'}"
                             f" · {row.get('chapters') or 0} chapters")
                if row.get("status"):
                    lines.append(f"Disposition: {row['status']}")
            elif table_id == "merges":
                lines.append(f"{row.get('name_a') or ''} ↔ "
                             f"{row.get('name_b') or ''}")
                lines.append(f"{row.get('tier') or ''} · "
                             f"{row.get('outcome') or ''}")
                p = row.get("p")
                if isinstance(p, (int, float)):
                    lines.append(f"P {p:.2f}")
                if row.get("reason"):
                    lines.append(f"Reason: {row['reason']}")
            detail.update(Text("\n".join(lines)))

        def _select(self, table_id, row_key):
            if row_key is None:
                return
            self._selected = (table_id, row_key.value)
            book = self._store.books.get(self.viewed) \
                if self.viewed else None
            if book is not None:
                self._render_reader(book)
                self._render_detail(book)

        def on_data_table_row_highlighted(self, event):
            self._select(event.data_table.id, event.row_key)

        def on_data_table_row_selected(self, event):
            self._select(event.data_table.id, event.row_key)

        # -- actions ---------------------------------------------------
        def _step_book(self, delta):
            order = self._store.book_order
            if not order or self.viewed is None:
                return
            i = order.index(self.viewed)
            j = min(max(i + delta, 0), len(order) - 1)
            self.viewed = order[j]
            self.follow = self.viewed == self._store.live_book_id()
            self._render()

        def action_prev_book(self):
            self._step_book(-1)

        def action_next_book(self):
            self._step_book(1)

        def action_follow_live(self):
            self.viewed = self._store.live_book_id()
            self.follow = True
            self._render()

        def action_open_log(self):
            self.push_screen(LogScreen(self._store))

        def action_quit_flow(self):
            if self._store.run_state != "running":
                self.exit(self._control.get("result"))
                return

            def done(choice):
                if choice == "plain":
                    self._control["detach"]()
                    self.exit("detached")
                elif choice == "cancel":
                    self.action_cancel_run()

            self.push_screen(QuitScreen(), done)

        def action_cancel_run(self):
            self._control["cancel"]()
            self.exit("cancelling")

    return ConsoleApp


def _worker_body(observer, runner, outcome, control, app_holder):
    """The runner's thread: run the work with its printed output
    routed into log events.

    A crash's traceback is published as log events directly rather
    than printed — the ambient stream cannot be trusted to be the
    writer at that moment (see run_console), and a vanished
    traceback makes a failed run undiagnosable.
    """
    import contextlib
    import sys
    import traceback
    writer = _LogWriter(observer, sys.__stdout__)
    control["writer"] = writer
    with contextlib.redirect_stdout(writer), \
            contextlib.redirect_stderr(writer):
        try:
            outcome["ok"] = bool(runner())
        except KeyboardInterrupt:
            outcome["ok"] = False
            outcome["interrupted"] = True
        except BaseException:
            for line in traceback.format_exc().splitlines():
                if line.strip():
                    observer.publish("log", payload={"text": line})
            outcome["ok"] = False
            outcome["error"] = True
        finally:
            writer.flush()
    if not control.get("detached"):
        app = app_holder.get("app")
        if app is not None:
            try:
                app.call_from_thread(app.exit, outcome.get("ok"))
            except Exception:
                pass


def run_console(observer, store, runner):
    """Run `runner` on a worker thread beneath the console.

    Returns {"ran": True, "ok": bool} once the run has been handed
    to the worker — including when the UI itself fails, because a
    broken console must not cost a book its run — or None when the
    console cannot start at all, in which case the caller falls back
    to the standard console and runs the work itself.
    """
    try:
        app_class = _build_app_class()
    except Exception:
        return None

    outcome = {}
    control = {}
    app_holder = {}
    thread = threading.Thread(
        target=_worker_body,
        args=(observer, runner, outcome, control, app_holder),
        daemon=True, name="console-runner")

    def detach():
        control["detached"] = True
        writer = control.get("writer")
        if writer is not None:
            writer.detached = True

    def cancel():
        _deliver_interrupt(thread)

    control["detach"] = detach
    control["cancel"] = cancel
    control["result"] = None
    # The app starts the worker from on_mount (see ConsoleApp): only
    # then is Textual's own stream capture already installed, so the
    # worker's redirect — installed after it — is the one that wins.
    control["start"] = thread.start

    try:
        app = app_class(observer, store, control)
    except Exception:
        return None
    app_holder["app"] = app
    try:
        app.run()
    except Exception:
        # The UI failed; if the worker had already started it keeps
        # the run (spec §8.3) and we join it below. If it never
        # started, the caller falls back and runs the work itself.
        pass
    if not thread.is_alive() and "ok" not in outcome:
        return None
    thread.join()
    return {"ran": True, "ok": bool(outcome.get("ok"))}
