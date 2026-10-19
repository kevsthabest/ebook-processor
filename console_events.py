"""Console event contract, observer, and display-state store.

This module is the seam between the pipeline and any live inspection
console (spec: textual-inspection-console-spec-2026-10-19, Phase 1).
The pipeline publishes facts; the store reduces them into per-book
display state; a future Textual application renders the store.

Three rules govern everything here:

- Publishing never raises into pipeline code. A broken observer must
  not cost a book its run, so every public entry point swallows its
  own failures and degrades to a warning count.
- The store is a projection, never a source of truth. The result
  dict and the preview artefacts stay authoritative; if the store
  disagrees with them, the store has the bug.
- Nothing in this module imports the pipeline or any UI toolkit, so
  it can be imported and unit-tested in isolation.
"""

import threading
import time
from collections import deque

EVENT_VERSION = 1

# The stage registry is the single place that names pipeline stages
# for display. IDs are stable; labels are presentation. A stage's
# state is one of: not_started, active, complete, failed, skipped,
# not_applicable.
STAGES = (
    ("acquisition", "Acquisition"),
    ("identity", "Identity extraction"),
    ("content", "Content extraction"),
    ("merge_review", "Merge review"),
    ("relationship_validation", "Relationship validation"),
    ("content_gates", "Trigger & trope gates"),
    ("evidence_retrieval", "Evidence retrieval"),
    ("final_gates", "Final gates"),
)
STAGE_IDS = frozenset(sid for sid, _label in STAGES)
STAGE_LABELS = dict(STAGES)

# The two phase strings the pipeline already passes to
# PipelineUI.set_phase, mapped onto registry stages. Later phases
# arrive as the Phase 3 hooks are added; unknown strings are ignored
# rather than guessed at.
PHASE_TO_STAGE = {
    "characters": "identity",
    "content": "content",
}

EVENT_KINDS = frozenset({
    "run_state", "book_state", "stage_state", "chapter_state",
    "candidate_upsert", "verdict_upsert", "counts_snapshot",
    "log", "book_final",
})

# Final relationship states (standing §1.5 names). A verdict event
# may carry an interim judgment long before one of these exists.
FINAL_REL_STATES = frozenset({
    "validated", "rejected", "review_required",
    "budget_exhausted", "validator_error",
})

# Display retention caps per book. Counts in the store stay exact
# when a cap is hit; only retained rows are bounded, because the
# final artefact — not the console — holds the complete set.
ROW_CAPS = {
    "characters": 1000,
    "relationships": 2000,
    "tropes": 500,
    "triggers": 100,
    "merges": 1000,
}


class ConsoleObserver:
    """Thread-safe, bounded, no-throw event publisher.

    The pipeline calls publish(); attached consumers drain(). When
    the queue saturates, a verdict or final event may displace the
    oldest counts snapshot; anything else that cannot be queued is
    dropped with the affected book marked stale — the consumer
    learns its projection is stale instead of silently drifting,
    and the pipeline never blocks waiting for the UI.
    """

    def __init__(self, max_queue=4096):
        self._lock = threading.Lock()
        self._seq = 0
        self._queue = deque(maxlen=max_queue)
        self._max_queue = max_queue
        self.warnings = 0
        self.stale_books = set()

    def publish(self, kind, book_id=None, stage=None, payload=None,
                run_id=None):
        """Append one event; returns its sequence number or None.

        Never raises: observability is an observer, not a
        participant, so a failure here degrades to a warning count.
        """
        try:
            if kind not in EVENT_KINDS:
                self.warnings += 1
                return None
            with self._lock:
                self._seq += 1
                event = {
                    "v": EVENT_VERSION,
                    "seq": self._seq,
                    "ts": time.time(),
                    "run_id": run_id,
                    "book_id": book_id,
                    "stage": stage,
                    "kind": kind,
                    "payload": payload or {},
                }
                if len(self._queue) >= self._max_queue:
                    # Saturation: verdicts and finals are the record
                    # the user is watching, so they displace the
                    # oldest counts snapshot when one exists;
                    # otherwise the event is dropped and the book is
                    # marked stale rather than silently wrong.
                    if kind in ("verdict_upsert", "book_final"):
                        for old in self._queue:
                            if old["kind"] == "counts_snapshot":
                                self._queue.remove(old)
                                break
                        else:
                            self.stale_books.add(book_id)
                            return None
                    else:
                        self.stale_books.add(book_id)
                        return None
                self._queue.append(event)
                return event["seq"]
        except Exception:
            self.warnings += 1
            return None

    def drain(self):
        """Remove and return all queued events, in sequence order."""
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
            return events

    def __len__(self):
        with self._lock:
            return len(self._queue)


class _BookState:
    def __init__(self, book_id):
        self.book_id = book_id
        self.state = "pending"  # pending | active | complete | failed | skipped
        self.title = None
        self.stages = {sid: "not_started" for sid, _ in STAGES}
        self.chapters = {}  # (pass_id, index) -> chapter record
        # Display colours: each character name takes the next
        # ordinal the first time a row for it appears, and keeps it
        # for the whole book — the app maps ordinals onto its
        # palette, so a name's colour is stable without the store
        # knowing anything about rendering.
        self.char_colors = {}  # character row key -> colour ordinal
        # First identity chapter each name was identified in; the
        # reader uses it to mark first appearances.
        self.char_first_chapter = {}
        self.rows = {kind: {} for kind in ROW_CAPS}
        self.row_counts = {kind: 0 for kind in ROW_CAPS}
        self.capped = set()
        self.counts = {}
        self.log_tail = deque(maxlen=200)
        self.final = None

    def upsert_row(self, table, key, record):
        rows = self.rows[table]
        if key not in rows:
            self.row_counts[table] += 1
            if len(rows) >= ROW_CAPS[table]:
                self.capped.add(table)
                return
            if table == "characters" and key not in self.char_colors:
                self.char_colors[key] = len(self.char_colors)
        rows[key] = record


class ConsoleStore:
    """Reducer: console events in, per-book display state out.

    Unknown kinds and unknown stages are ignored, never fatal — the
    event contract may grow on the producer side before a consumer
    learns the new shapes. A relationship row's final_state stays
    None until an event explicitly carries one; interim judgments
    never promote themselves, because revival and the direction
    guard run after judging and can still move the claim.
    """

    def __init__(self):
        self.books = {}
        self.book_order = []
        self.run_state = "pending"
        self.run_log = deque(maxlen=1000)
        self.stale_books = set()
        self.last_seq = 0

    def _book(self, book_id):
        book = self.books.get(book_id)
        if book is None:
            book = self.books[book_id] = _BookState(book_id)
            self.book_order.append(book_id)
        return book

    def apply(self, event):
        try:
            self._apply(event)
        except Exception:
            # A malformed event costs the display one update, never
            # the consumer's loop.
            pass

    def _apply(self, event):
        if event.get("v") != EVENT_VERSION:
            return
        seq = event.get("seq") or 0
        if seq > self.last_seq:
            self.last_seq = seq
        kind = event.get("kind")
        payload = event.get("payload") or {}
        book_id = event.get("book_id")

        if kind == "run_state":
            self.run_state = payload.get("state", self.run_state)
            return
        if kind == "log":
            # Run-level log lines (the pipeline's printed output,
            # routed by the console) often carry no book context;
            # they still belong in the run log view.
            self.run_log.append(payload.get("text", ""))
            if book_id is None:
                return
        if book_id is None:
            return
        book = self._book(book_id)

        if kind == "book_state":
            book.state = payload.get("state", book.state)
            if payload.get("title"):
                book.title = payload["title"]
        elif kind == "stage_state":
            stage = event.get("stage") or payload.get("stage")
            if stage in STAGE_IDS and payload.get("state"):
                book.stages[stage] = payload["state"]
        elif kind == "chapter_state":
            key = (payload.get("pass_id"), payload.get("index"))
            # Merge, not replace: a chapter's record is built by
            # more than one event (started with its text, identified
            # with its names), and neither may erase the other.
            merged = dict(book.chapters.get(key) or {})
            merged.update(payload)
            book.chapters[key] = merged
            names = payload.get("names")
            if payload.get("pass_id") == "identity" and names:
                idx = payload.get("index")
                for n in names:
                    first = book.char_first_chapter.get(n)
                    if first is None or (idx is not None
                                         and idx < first):
                        book.char_first_chapter[n] = idx
                merged["new_names"] = [
                    n for n in names
                    if book.char_first_chapter.get(n) == idx]
        elif kind == "candidate_upsert":
            table = payload.get("table")
            if table in ROW_CAPS and payload.get("key") is not None:
                book.upsert_row(table, payload["key"],
                                dict(payload.get("record") or {}))
        elif kind == "verdict_upsert":
            self._apply_verdict(book, payload)
        elif kind == "counts_snapshot":
            book.counts.update(payload.get("counts") or {})
        elif kind == "log":
            book.log_tail.append(payload.get("text", ""))
        elif kind == "book_final":
            book.final = dict(payload)
            book.state = "complete"
            for table, rows in (payload.get("rows") or {}).items():
                if table in ROW_CAPS:
                    for key, record in rows.items():
                        book.upsert_row(table, key, dict(record))

    def _apply_verdict(self, book, payload):
        table = payload.get("table", "relationships")
        key = payload.get("row_key")
        if table not in ROW_CAPS or key is None:
            return
        existing = book.rows[table].get(key, {})
        record = dict(existing)
        # decision and final_state are verdict fields, not record
        # data: they are applied only from the event's top level
        # (final_state through the whitelist below), so a producer
        # nesting them inside "record" cannot bypass the check.
        nested = {k: v for k, v in (payload.get("record") or {}).items()
                  if k not in ("decision", "final_state")}
        record.update(nested)
        if "decision" in payload:
            record["decision"] = payload["decision"]
        if payload.get("p") is not None:
            record["p"] = payload["p"]
        final = payload.get("final_state")
        if final in FINAL_REL_STATES:
            record["final_state"] = final
        # A pair loser is an audit row, not a claim: it can never
        # acquire a verdict by display logic.
        if record.get("not_selected"):
            record.pop("decision", None)
            record.pop("final_state", None)
        book.upsert_row(table, key, record)

    def mark_stale(self, book_id):
        if book_id is not None:
            self.stale_books.add(book_id)

    def live_book_id(self):
        """The most recently ordered book that is still active."""
        for book_id in reversed(self.book_order):
            if self.books[book_id].state == "active":
                return book_id
        return self.book_order[-1] if self.book_order else None
