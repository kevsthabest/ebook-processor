#!/usr/bin/env python3
"""Training-data harvester for the ebook processor programme.

This is a standalone lab tool, not part of the shipped pipeline.
Nothing in process-portable.py or portal.py imports it, it carries
no version of its own, and it never writes anywhere near production
data: it reads preview artefacts, run-events files, the corpus and
the state store, and writes only inside its own store directory.

It exists because of a decision the User made on 2026-10-17: collect
fine-tuning data now, while the pipeline produces it, and train
later on a provisioned GPU. The design is specified in
docs-adjacent notes (training-data-collection-spec-2026-10-18.md,
grounded in training-data-inventory-2026-10-17.md); this module is
Build 1 of that spec. Three datasets:

  judge-pairs        one record per claim that carries a validation
                     record in a preview artefact: the claim, the
                     quote, the evidence window as judged
                     (reconstructed deterministically from the
                     persisted parts plus the corpus text), the
                     mined provenance, the judge's probability, and
                     the final disposition. Negatives included.
  extraction-pairs   one record per chapter: chapter text plus the
                     roster state the chapter was extracted against,
                     reconstructed by replaying the roster update
                     over the stored per-chapter outputs, and the
                     stored parsed delta as the output. Every record
                     is stamped fidelity "reconstructed" — silver,
                     never to be confused with captured pairs.
  gold               the User's own decisions, from the sources that
                     exist today: labelled uncertainty-queue packets
                     in state.db and human confirmed/rejected claim
                     rows in Supabase. Where a source is unreachable,
                     the export is empty and the gap is recorded —
                     never filled with reconstructed records.

The store layout (the store never lives inside the git repo):

    <store>/raw-previews/<book>/<date>-<hash>.json   (immutable)
    <store>/raw-run-events/<file>.jsonl              (immutable)
    <store>/datasets/<dataset>/<date>-<dataset>.jsonl
    <store>/manifest.json        (what, when, from which run)
    <store>/split-map.json       (book -> train | holdout)

Preview artefacts are one per book and overwritten by every re-run,
so every dataset command archives first: the store is append-only
and a run's output survives its successor. Archiving is idempotent
(content-hash skip) and never modifies its sources.

Holdout discipline: records are tagged split BY BOOK from the
split map. Daemon and Fourth Wing are the standing holdout (they
are the eval books). Chapter-level splitting is forbidden — a
model that has seen a book's chapters has seen the book.

Exclusions enforced here, because the inventory showed these
values are formulas or heuristics rather than model judgments:
auto-approved trope_confidence (0.55 + 0.1 x chapters) is never
emitted as a label; a trigger claim's stored confidence is a
heuristic and is never used as a judge probability (only per-quote
decision_p values from gate "decision" evidence are); trigger
evidence whose gate is "fallback" was never judged and is excluded
from judge pairs. `reason` is null on every judge-pair and gold
record: no judge reason exists anywhere in the system today (the
decisions call as consumed returns only the score), and this tool
records honest nulls, never reconstructed ones.

Usage:
    python3 training_harvest.py archive
    python3 training_harvest.py judge-pairs
    python3 training_harvest.py extraction-pairs
    python3 training_harvest.py gold
    python3 training_harvest.py report

Stdlib only, Python 3.8+, Windows and Linux: the User runs this
from the repo root on his own machine, where his preview
artefacts, his corpus and his config live.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Locations. Every default mirrors where the shipped processor keeps
# the same thing on the User's machine: config.json beside the
# processor script, the learn dir (corpus.db, state.db, run-events)
# under ~/.ebook-processor/learned, previews in the repo's preview/
# directory. The store sits beside the learned state and is
# overridable with --store; it must never live inside the git repo,
# because its records carry full chapter text.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STORE = Path.home() / ".ebook-processor" / "training-data"
DEFAULT_LEARN_DIR = Path.home() / ".ebook-processor" / "learned"
DEFAULT_PREVIEW_DIR = _SCRIPT_DIR / "preview"
DEFAULT_CONFIG = _SCRIPT_DIR / "config.json"

# The standing holdout books: the eval books. Seeded into the
# store's split map on first use; after that the file in the store
# is the only authority, edited deliberately, never by this tool.
DEFAULT_HOLDOUT_BOOKS = ("Daemon", "Fourth Wing")

DATASET_JUDGE = "judge-pairs"
DATASET_EXTRACTION = "extraction-pairs"
DATASET_GOLD = "gold"


class HarvestError(Exception):
    """A user-facing failure: bad input, missing store, unreadable
    artefact. Printed as one line; no traceback for expected cases."""


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _slug(text):
    """Filesystem-safe book key: 'Daemon — Daniel Suarez' ->
    'daemon-daniel-suarez'. Used for store directory names only;
    records keep the verbatim title."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").casefold()).strip("-")
    return s or "unknown-book"


def norm_name(n):
    """Deterministic character-name key, mirrored from
    process-portable.py's norm_name: casefold, drop a leading
    article, tidy spaces. The roster replay keys entries by this,
    the same key the processor's roster uses."""
    n = (n or "").strip().casefold()
    n = re.sub(r"^(the|a|an)\s+", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# ---------------------------------------------------------------------------
# Evidence-window mirrors. The validator's judged context is a
# deterministic function of the claim's quote and its chapter text
# (process-portable.py: _split_sentences, _collapse_ws,
# _locate_quote_span, _evidence_window, _resolution_context_lines,
# and the composition in _validation_context). These are mirrored —
# not imported — so the lab stays standalone; the tests pin the
# mirror on synthetic text, and any drift in the original shows up
# as windows that fail to locate, never as silently wrong windows.
# ---------------------------------------------------------------------------

def _split_sentences(text):
    out = []
    for piece in re.split(r"(?<=[.!?])\s+", text or ""):
        s = piece.strip()
        if s:
            out.append(s)
    return out


def _collapse_ws(s):
    s = s or ""
    for curly, plain in (("‘", "'"), ("’", "'"),
                         ("“", '"'), ("”", '"'),
                         ("—", "-"), ("–", "-"),
                         ("…", "...")):
        s = s.replace(curly, plain)
    return re.sub(r"\s+", " ", s).strip()


def _locate_quote_span(quote, text):
    pieces = _split_sentences(text)
    if not pieces or not quote:
        return None
    collapsed = [_collapse_ws(p) for p in pieces]
    joined = " ".join(collapsed)
    needle = _collapse_ws(quote)
    if not needle:
        return None
    start = joined.find(needle)
    if start < 0:
        return None
    end = start + len(needle)
    first = last = None
    pos = 0
    for i, piece in enumerate(collapsed):
        nxt = pos + len(piece)
        if first is None and nxt >= start:
            first = i
        if nxt >= end - 1:
            last = i
            break
        pos = nxt + 1
    if first is None or last is None:
        return None
    return first, last


def _evidence_window(quote, chapter_text):
    """The verbatim sentences immediately around a quote: two each
    side (the width production has used since 2.13.13), the quote's
    own sentences excluded, joined and capped at 1200 chars. ""
    when the quote cannot be located — production validates with
    no window in exactly that case, so the reconstruction matches
    by returning the same emptiness."""
    if not quote or not chapter_text:
        return ""
    span = _locate_quote_span(quote, chapter_text)
    if span is None:
        return ""
    pieces = _split_sentences(chapter_text)
    first, last = span
    around = (pieces[max(0, first - 2):first]
              + pieces[last + 1:last + 3])
    return " ".join(around)[:1200]


# The provenance enums, mirrored from process-portable.py
# (MATCH_FORM_RESOLVED_SURNAME / BASIS_FOCAL_CHARACTER).
_MATCH_FORM_RESOLVED_SURNAME = "resolved-surname"
_BASIS_FOCAL_CHARACTER = "focal_character"


def _resolution_context_lines(provenance):
    """One context line per endpoint whose surname the chapter
    uniquely resolved, and nothing otherwise — ambiguity stays
    silent, exactly as the production validator leaves it."""
    lines = []
    for key in ("from", "to"):
        ep = (provenance or {}).get(key) or {}
        if ep.get("matched_form") != _MATCH_FORM_RESOLVED_SURNAME:
            continue
        basis = ep.get("basis") or {}
        kinds = basis.get("kinds") or []
        if _BASIS_FOCAL_CHARACTER in kinds:
            note = "the scene follows them"
        else:
            note = "named in full earlier in the chapter"
        lines.append(
            f"In this chapter, '{ep.get('mention_text')}' refers "
            f"to {ep.get('endpoint')} ({note}).")
    return lines


def _judged_context(quote, chapter_text, provenance):
    """Compose the validator context exactly as production does:
    the window line first, then any resolution lines. Returns
    (window, context); both "" when nothing could be composed."""
    window = _evidence_window(quote, chapter_text)
    ctx = (f"Sentences around the quote, from the same passage: {window}"
           if window else "")
    lines = _resolution_context_lines(provenance)
    if lines:
        ctx = (ctx + "\n" if ctx else "") + "\n".join(lines)
    return window, ctx


# ---------------------------------------------------------------------------
# Store: layout, manifest, split map.
# ---------------------------------------------------------------------------

def _store_paths(store):
    store = Path(store)
    return {
        "root": store,
        "raw_previews": store / "raw-previews",
        "raw_run_events": store / "raw-run-events",
        "datasets": store / "datasets",
        "manifest": store / "manifest.json",
        "split_map": store / "split-map.json",
    }


def _ensure_store(store):
    paths = _store_paths(store)
    for key in ("root", "raw_previews", "raw_run_events", "datasets"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def _load_manifest(paths):
    if paths["manifest"].exists():
        try:
            m = json.loads(paths["manifest"].read_text(encoding="utf-8"))
            if isinstance(m, dict):
                m.setdefault("archives", [])
                m.setdefault("exports", [])
                return m
        except ValueError:
            pass
    return {"version": 1, "created_at": _now_iso(),
            "archives": [], "exports": []}


def _save_manifest(paths, manifest):
    manifest["updated_at"] = _now_iso()
    paths["manifest"].write_text(
        json.dumps(manifest, indent=1, ensure_ascii=False),
        encoding="utf-8")


def _load_split_map(paths):
    """The split map is an explicit file in the store. On first use
    it is seeded with the standing holdout books; afterwards this
    tool only ever reads it — moving a book across the line is a
    deliberate human edit, never a side effect of a harvest."""
    if paths["split_map"].exists():
        try:
            m = json.loads(paths["split_map"].read_text(encoding="utf-8"))
            if isinstance(m, dict):
                return {str(k): str(v) for k, v in m.items()}
        except ValueError:
            pass
    seeded = {title: "holdout" for title in DEFAULT_HOLDOUT_BOOKS}
    paths["split_map"].write_text(
        json.dumps(seeded, indent=1, ensure_ascii=False),
        encoding="utf-8")
    return seeded


def _split_for(split_map, title):
    """Split for one book title. The map is keyed by title as the
    artefacts carry it; matching is case-insensitive so 'Daemon'
    and 'daemon' cannot drift apart. Unmapped books default to
    train — and the report lists them, so a default is never
    silent."""
    wanted = (title or "").strip().casefold()
    for key, value in split_map.items():
        if key.strip().casefold() == wanted:
            return value
    return "train"


# ---------------------------------------------------------------------------
# Archive: copy preview artefacts and run-events files into the
# store, append-only, skipping anything whose content hash is
# already archived. Sources are opened read-only and never written.
# ---------------------------------------------------------------------------

def _looks_like_preview(data):
    """A preview artefact is a book's run result: a JSON object
    carrying a title and the run's substance (chapter_data or the
    relationship lists). Other JSON that accumulates near the
    preview dir (stage aborts, checkpoints) is not an artefact and
    is reported as skipped, not archived."""
    if not isinstance(data, dict):
        return False
    if not data.get("title"):
        return False
    return ("chapter_data" in data or "relationships" in data
            or "dropped_relationships" in data)


def archive_sources(store, preview_dir, run_events_dir):
    """Archive everything new from both source dirs. Returns a
    counters dict; also records each new file in the manifest."""
    paths = _ensure_store(store)
    manifest = _load_manifest(paths)
    known_hashes = {a["sha256"] for a in manifest["archives"]}
    counts = {"previews_new": 0, "previews_skipped_known": 0,
              "previews_skipped_nonartefact": 0,
              "run_events_new": 0, "run_events_skipped_known": 0}

    preview_dir = Path(preview_dir)
    if preview_dir.is_dir():
        for src in sorted(preview_dir.glob("*.json")):
            raw = src.read_bytes()
            digest = _sha256_bytes(raw)
            if digest in known_hashes:
                counts["previews_skipped_known"] += 1
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                counts["previews_skipped_nonartefact"] += 1
                continue
            if not _looks_like_preview(data):
                counts["previews_skipped_nonartefact"] += 1
                continue
            book = _slug(f"{data.get('title', '')} "
                         f"{data.get('author', '')}".strip()
                         or src.stem)
            book_dir = paths["raw_previews"] / book
            book_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.fromtimestamp(
                src.stat().st_mtime).strftime("%Y-%m-%d")
            dest = book_dir / f"{stamp}-{digest[:10]}.json"
            if not dest.exists():
                dest.write_bytes(raw)
            manifest["archives"].append({
                "kind": "preview", "book": book,
                "book_title": data.get("title"),
                "source_name": src.name,
                "store_path": str(dest.relative_to(paths["root"])),
                "sha256": digest, "bytes": len(raw),
                "archived_at": _now_iso(),
            })
            known_hashes.add(digest)
            counts["previews_new"] += 1

    run_events_dir = Path(run_events_dir)
    if run_events_dir.is_dir():
        for src in sorted(run_events_dir.glob("*.jsonl")):
            raw = src.read_bytes()
            digest = _sha256_bytes(raw)
            if digest in known_hashes:
                counts["run_events_skipped_known"] += 1
                continue
            dest = paths["raw_run_events"] / src.name
            if dest.exists():
                # Same name, different content: a recycled run id.
                # Keep both — the store is append-only — by letting
                # the hash disambiguate the later arrival.
                dest = paths["raw_run_events"] / (
                    f"{src.stem}-{digest[:8]}{src.suffix}")
            if not dest.exists():
                dest.write_bytes(raw)
            manifest["archives"].append({
                "kind": "run-events", "book": None, "book_title": None,
                "source_name": src.name,
                "store_path": str(dest.relative_to(paths["root"])),
                "sha256": digest, "bytes": len(raw),
                "archived_at": _now_iso(),
            })
            known_hashes.add(digest)
            counts["run_events_new"] += 1

    _save_manifest(paths, manifest)
    return counts


def _archived_previews(paths, manifest):
    """Yield (archive_entry, parsed_json) for every archived
    preview, in manifest order. Unreadable entries are skipped —
    the store is append-only, so a corrupt file must not take the
    whole harvest down with it; the report counts what it read."""
    out = []
    for entry in manifest["archives"]:
        if entry.get("kind") != "preview":
            continue
        dest = paths["root"] / entry["store_path"]
        try:
            data = json.loads(dest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append((entry, data))
    return out


# ---------------------------------------------------------------------------
# Corpus access: chapter text, read-only, joined by work identity.
# The preview artefact carries work_id but not the corpus
# fingerprint, so the work is resolved work_id first, then
# title+author — the same identities corpus_works itself carries.
# A missing corpus is not an error: windows and chapter text come
# back empty and the records say so, which is the difference
# between a harvest that degrades honestly and one that guesses.
# ---------------------------------------------------------------------------

class Corpus:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.conn = None
        if self.path.exists():
            self.conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row

    @property
    def available(self):
        return self.conn is not None

    def fingerprint_for(self, work_id, title, author):
        if not self.available:
            return None
        try:
            if work_id:
                row = self.conn.execute(
                    "SELECT fingerprint FROM corpus_works "
                    "WHERE work_id = ?", (work_id,)).fetchone()
                if row:
                    return row["fingerprint"]
            if title:
                row = self.conn.execute(
                    "SELECT fingerprint FROM corpus_works "
                    "WHERE lower(title) = lower(?) "
                    "AND lower(coalesce(author, '')) = lower(?)",
                    (title, author or "")).fetchone()
                if row:
                    return row["fingerprint"]
        except sqlite3.Error:
            return None
        return None

    def chapter_text(self, fingerprint, chapter_index):
        if not self.available or not fingerprint:
            return None
        try:
            row = self.conn.execute(
                "SELECT text FROM corpus_chapters WHERE fingerprint = ? "
                "AND chapter_index = ?", (fingerprint, chapter_index)
            ).fetchone()
        except sqlite3.Error:
            return None
        return row["text"] if row else None


# ---------------------------------------------------------------------------
# Shared record plumbing.
# ---------------------------------------------------------------------------

def _meta(dataset, entry, data, split, chapter=None, judge_model=None,
          fidelity=None):
    """The canonical meta block every dataset record carries.
    processor_version is None unless the artefact itself states
    one: preview artefacts do not record the processor version,
    and a harvest that invents it would poison every later
    comparison that trusts the field."""
    return {
        "dataset": dataset,
        "book": data.get("title"),
        "book_slug": entry.get("book"),
        "work_id": data.get("work_id"),
        "chapter": chapter,
        "source_archive": entry.get("store_path"),
        "source_file": entry.get("source_name"),
        "processor_version": data.get("processor_version"),
        "judge_model": judge_model,
        "fidelity": fidelity,
        "split": split,
    }


def _write_dataset(paths, manifest, dataset, records, counters):
    out_dir = paths["datasets"] / dataset
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{_today()}-{dataset}.jsonl"
    with dest.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    manifest["exports"].append({
        "dataset": dataset,
        "store_path": str(dest.relative_to(paths["root"])),
        "records": len(records),
        "built_at": _now_iso(),
        "counters": counters,
    })
    _save_manifest(paths, manifest)
    return dest


# ---------------------------------------------------------------------------
# Dataset A: judge pairs.
#
# One record per claim carrying a validation record in the artefact
# (the relationships list and the dropped_relationships list are
# where validation outcomes live), plus one per trigger evidence
# entry the decision gate actually judged. Every relationship
# validation record is accounted for — shipped, downgraded and
# dropped — including the ones that never received a draw
# (budget-exhausted, no-evidence): they carry judged=False and a
# null probability, because a harvest that quietly omitted them
# could never reconcile against its source, and their abstention
# shape is itself training signal for a future judge.
# ---------------------------------------------------------------------------

def _disposition(record, shipped):
    """Final disposition of a validated relationship claim. A
    downgrade is recognised by the signals the pipeline stamps:
    an explicit downgrade note, or an original_type that differs
    from the stored type. Anything else in the shipped list
    shipped as extracted; anything in the dropped list dropped."""
    note = (record.get("validation_note") or "").strip().lower()
    original = record.get("original_type")
    if shipped and (note in ("downgraded", "downgrade")
                    or (original and original != record.get("type"))):
        return "downgraded"
    return "shipped" if shipped else "dropped"


def build_judge_pairs(archived, corpus, split_map):
    """archived: [(entry, data)] from _archived_previews. Returns
    (records, counters). The counters include every exclusion the
    spec names, so the report can show the values that were
    refused as labels, not just the ones that were kept."""
    records = []
    counters = {
        "relationship_validation_records": 0,
        "relationship_judge_draws": 0,
        "by_disposition": {"shipped": 0, "downgraded": 0, "dropped": 0},
        "trigger_decision_pairs": 0,
        "excluded_trigger_fallback_evidence": 0,
        "excluded_trigger_unjudged_evidence": {},
        "excluded_trope_formula_confidences": 0,
        "excluded_trigger_heuristic_confidences": 0,
        "merge_judge_records_out_of_scope": 0,
        "windows_reconstructed": 0,
        "windows_empty": 0,
    }
    for entry, data in archived:
        split = _split_for(split_map, data.get("title"))
        fp = corpus.fingerprint_for(data.get("work_id"),
                                    data.get("title"),
                                    data.get("author"))
        # Chapter label -> index, from the artefact's own summary
        # table, so a claim's chapter text can be found in the
        # corpus without guessing at index arithmetic.
        label_to_index = {}
        for s in data.get("chapter_summaries") or []:
            if isinstance(s, dict) and s.get("label"):
                label_to_index[s["label"]] = s.get("index")

        def chapter_text_for(label):
            idx = label_to_index.get(label)
            if idx is None:
                return None
            return corpus.chapter_text(fp, idx)

        for shipped, key in ((True, "relationships"),
                             (False, "dropped_relationships")):
            for rel in data.get(key) or []:
                if not isinstance(rel, dict):
                    continue
                counters["relationship_validation_records"] += 1
                p = rel.get("validation_p")
                judged = p is not None
                if judged:
                    counters["relationship_judge_draws"] += 1
                disposition = _disposition(rel, shipped)
                counters["by_disposition"][disposition] += 1
                quote = rel.get("evidence") or ""
                provenance = rel.get("endpoint_provenance")
                window, context = _judged_context(
                    quote, chapter_text_for(rel.get("chapter")),
                    provenance)
                if window:
                    counters["windows_reconstructed"] += 1
                else:
                    counters["windows_empty"] += 1
                records.append({
                    "meta": _meta(DATASET_JUDGE, entry, data, split,
                                  chapter=rel.get("chapter"),
                                  judge_model=rel.get("validation_model"),
                                  fidelity="reconstructed"),
                    "claim": {"axis": "relationships",
                              "from": rel.get("from"),
                              "to": rel.get("to"),
                              "type": rel.get("type")},
                    "evidence": {
                        "quote": quote,
                        "window": window,
                        "context": context,
                        "grade": rel.get("evidence_grade"),
                        "source": rel.get("evidence_source"),
                        "verified": rel.get("evidence_verified"),
                    },
                    "provenance": provenance,
                    "validation": {"p": p,
                                   "model": rel.get("validation_model"),
                                   "note": rel.get("validation_note"),
                                   "state": rel.get("validation_state")},
                    "disposition": disposition,
                    "judged": judged,
                    "reason": None,
                })

        # Trigger judge pairs: only evidence the decision gate
        # itself judged. The claim-level confidence on a trigger is
        # a heuristic formula and is counted as excluded, never
        # used; fallback evidence was never judged; retrieval and
        # none gates are not decision-model draws either.
        for trig in data.get("triggers") or []:
            if not isinstance(trig, dict):
                continue
            if trig.get("confidence") is not None:
                counters["excluded_trigger_heuristic_confidences"] += 1
            for ev in trig.get("evidence") or []:
                if not isinstance(ev, dict):
                    continue
                gate = ev.get("decision_gate")
                if gate == "decision" and ev.get("decision_p") is not None:
                    counters["trigger_decision_pairs"] += 1
                    records.append({
                        "meta": _meta(DATASET_JUDGE, entry, data, split,
                                      chapter=ev.get("chapter"),
                                      fidelity="reconstructed"),
                        "claim": {"axis": "triggers",
                                  "category": trig.get("warning")},
                        "evidence": {"quote": ev.get("quote") or "",
                                     "window": "",
                                     "context": "",
                                     "grade": None,
                                     "source": "decision_gate",
                                     "verified": trig.get(
                                         "evidence_verified")},
                        "provenance": None,
                        "validation": {"p": ev.get("decision_p"),
                                       "model": None,
                                       "note": None,
                                       "state": None},
                        "disposition": "shipped",
                        "judged": True,
                        "gate": "decision",
                        "reason": None,
                    })
                elif gate == "fallback":
                    counters["excluded_trigger_fallback_evidence"] += 1
                else:
                    bucket = counters["excluded_trigger_unjudged_evidence"]
                    bucket[str(gate)] = bucket.get(str(gate), 0) + 1

        # Trope confidences in the artefact are the auto-approval
        # formula, not model output: counted, never emitted.
        counters["excluded_trope_formula_confidences"] += len(
            data.get("trope_confidence") or {})
        # Merge-tier judge records exist in the artefact
        # (possible_merges with p_yes) but are outside dataset A's
        # claim shape per the spec; counted so the report accounts
        # for their presence instead of hiding them.
        counters["merge_judge_records_out_of_scope"] += len(
            data.get("possible_merges") or [])
    return records, counters


# ---------------------------------------------------------------------------
# Dataset B: extraction pairs (silver).
#
# The roster state at chapter k is recorded nowhere; only the final
# roster survives. The replay below folds each chapter's stored
# Call A output into a fresh roster in chapter order, mirroring
# _roster_update's accumulation (appearances, chapters, roles,
# statuses, descriptions, first-evidence-wins) and its rule 1
# (an incoming primary name that matches a known primary folds
# into that entry). It deliberately does NOT reproduce the tiered
# merge machinery, the learned-nickname lookups or the roster
# reconcile pass — those are exactly why the fidelity stamp is
# "reconstructed", and why the report prints the replayed final
# roster size beside the stored one: the divergence is measured,
# never hidden.
# ---------------------------------------------------------------------------

def _new_roster_entry(name, nkey):
    return {"name": name, "aliases": [], "alias_keys": [nkey],
            "primary_keys": [nkey], "appearances": 0,
            "last_seen": None, "chapters": [], "roles": {},
            "descriptions": [], "statuses": {}, "evidence": None,
            "death_reports": [], "alive_reports": [],
            "unresolved_mentions": []}


def _roster_fold(roster, characters, chapter_idx):
    """Fold one chapter's stored characters into the replay roster
    (dict keyed by norm_name, insertion-ordered). Mutates roster."""
    primaries = {}
    for key, e in roster.items():
        for pk in e["primary_keys"]:
            primaries[pk] = key
    for c in characters or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        nkey = norm_name(name)
        if not nkey:
            continue
        found = primaries.get(nkey)
        if found is None:
            roster[nkey] = _new_roster_entry(name, nkey)
            found = nkey
            primaries[nkey] = nkey
        e = roster[found]
        e["appearances"] += 1
        e["last_seen"] = chapter_idx
        if chapter_idx not in e["chapters"]:
            e["chapters"].append(chapter_idx)
        if nkey not in e["alias_keys"]:
            e["alias_keys"].append(nkey)
        if nkey not in e["primary_keys"]:
            e["primary_keys"].append(nkey)
        for alias in c.get("aliases") or []:
            ak = norm_name(alias)
            if not alias or alias == e["name"] or not ak:
                continue
            # Alias pollution guard, mirrored in spirit: never
            # adopt an alias that is another entry's primary name.
            owner = primaries.get(ak)
            if owner is not None and owner != found:
                if alias not in e["unresolved_mentions"]:
                    e["unresolved_mentions"].append(alias)
                continue
            if ak not in e["alias_keys"]:
                e["alias_keys"].append(ak)
            if alias not in e["aliases"]:
                e["aliases"].append(alias)
        if c.get("role"):
            e["roles"][c["role"]] = e["roles"].get(c["role"], 0) + 1
        if c.get("description"):
            e["descriptions"].append(
                [chapter_idx, c["description"], bool(c.get("evidence"))])
        if c.get("status"):
            e["statuses"][c["status"]] = \
                e["statuses"].get(c["status"], 0) + 1
            if c["status"] == "dead" and c.get("evidence"):
                e["death_reports"].append(chapter_idx)
            if c["status"] == "alive" and c.get("evidence"):
                if chapter_idx not in e["alive_reports"]:
                    e["alive_reports"].append(chapter_idx)
        if c.get("evidence") and not e.get("evidence"):
            e["evidence"] = c["evidence"]


def _roster_snapshot(roster):
    """A deep, JSON-shaped copy of the replay roster as a list, in
    first-appearance order — the shape the stored final roster
    uses, so a trainer can compare like with like."""
    return json.loads(json.dumps(list(roster.values())))


def build_extraction_pairs(archived, corpus, split_map):
    """Returns (records, counters). Validation outcomes are joined
    onto each chapter delta's relationships by (from, to, type)
    name matching — the only lineage the artefacts support — so a
    trainer can filter or weight by outcome instead of learning
    from claims the judge killed as if they were truth."""
    records = []
    counters = {"chapters": 0, "chapters_with_text": 0,
                "validation_joined": 0, "validation_unjoined": 0,
                "per_book": {}}
    for entry, data in archived:
        split = _split_for(split_map, data.get("title"))
        fp = corpus.fingerprint_for(data.get("work_id"),
                                    data.get("title"),
                                    data.get("author"))
        chapter_data = data.get("chapter_data") or {}
        chapters = chapter_data.get("chapters") or {}
        stored_roster = chapter_data.get("roster") or {}

        # Final validation outcomes by claim identity, for the join.
        outcomes = {}
        for shipped, key in ((True, "relationships"),
                             (False, "dropped_relationships")):
            for rel in data.get(key) or []:
                if not isinstance(rel, dict):
                    continue
                ck = (rel.get("from"), rel.get("to"), rel.get("type"))
                outcomes[ck] = {
                    "validation_state": rel.get("validation_state"),
                    "validation_p": rel.get("validation_p"),
                    "validation_note": rel.get("validation_note"),
                    "disposition": _disposition(rel, shipped),
                }

        label_by_index = {}
        for s in data.get("chapter_summaries") or []:
            if isinstance(s, dict) and s.get("index") is not None:
                label_by_index[s["index"]] = s.get("label")

        def _chapter_sort(kv):
            try:
                return (0, int(kv[0]), "")
            except (TypeError, ValueError):
                return (1, 0, str(kv[0]))

        roster = {}
        book_chapters = 0
        for idx_key, chapter in sorted(chapters.items(),
                                       key=_chapter_sort):
            if not isinstance(chapter, dict):
                continue
            try:
                idx = int(idx_key)
            except (TypeError, ValueError):
                idx = idx_key
            a = chapter.get("a") or {}
            b = chapter.get("b") or {}
            text = corpus.chapter_text(fp, idx) \
                if isinstance(idx, int) else None
            joined = []
            n_joined = n_unjoined = 0
            for rel in a.get("relationships") or []:
                if not isinstance(rel, dict):
                    continue
                outcome = outcomes.get(
                    (rel.get("from"), rel.get("to"), rel.get("type")))
                if outcome is not None:
                    n_joined += 1
                else:
                    n_unjoined += 1
                joined.append({"from": rel.get("from"),
                               "to": rel.get("to"),
                               "type": rel.get("type"),
                               "outcome": outcome})
            counters["validation_joined"] += n_joined
            counters["validation_unjoined"] += n_unjoined
            if text:
                counters["chapters_with_text"] += 1
            records.append({
                "meta": _meta(DATASET_EXTRACTION, entry, data, split,
                              chapter=label_by_index.get(idx),
                              fidelity="reconstructed"),
                "input": {
                    "chapter_index": idx,
                    "chapter_label": label_by_index.get(idx),
                    "chapter_text": text,
                    "chapter_text_source": "corpus" if text
                                           else "unavailable",
                    "roster_state": _roster_snapshot(roster),
                    "roster_size": len(roster),
                },
                "output": {"a": a, "b": b},
                "validation_join": {
                    "relationships": joined,
                    "joined": n_joined,
                    "unjoined": n_unjoined,
                },
            })
            _roster_fold(roster, a.get("characters"), idx)
            book_chapters += 1
        counters["chapters"] += book_chapters
        counters["per_book"][entry.get("book")] = {
            "chapters": book_chapters,
            "replay_final_roster_size": len(roster),
            "stored_roster_size": len(stored_roster),
        }
    return records, counters


# ---------------------------------------------------------------------------
# Dataset C: gold human decisions.
#
# Harvest what exists, mark the gaps, invent nothing. Two sources
# are reachable from the processor's side of the fence:
#
#   - the uncertainty queue in state.db: self-contained labelling
#     packets that already store label, labeller and timestamp —
#     the one event-grade capture surface that exists today;
#   - Supabase claim rows carrying a human confirmed/rejected
#     status, read with the processor's own config credentials.
#     Those rows are current state, not events: they carry no
#     decider and no decision time that this tool may claim, so
#     labeller and decided_at stay null unless the row itself
#     carries a decision timestamp field.
#
# Where neither source is reachable, the export is empty and the
# gaps are recorded in the manifest and printed — an empty gold
# export with an honest gap list is a valid harvest; a padded one
# is not.
# ---------------------------------------------------------------------------

def _gold_from_uncertainty_queue(learn_dir, gaps):
    records = []
    db_path = Path(learn_dir) / "state.db"
    if not db_path.exists():
        gaps.append(f"uncertainty_queue: no state.db under {learn_dir}")
        return records
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, created, run_ref, work_key, class, subject, "
            "packet, status, label, labelled_by, labelled_at "
            "FROM uncertainty_queue WHERE label IS NOT NULL").fetchall()
    except sqlite3.Error as e:
        gaps.append(f"uncertainty_queue: unreadable ({type(e).__name__})")
        return records
    for row in rows:
        packet = None
        try:
            packet = json.loads(row["packet"]) if row["packet"] else None
        except ValueError:
            packet = None
        evidence = None
        if isinstance(packet, dict):
            evidence = packet.get("evidence")
        records.append({
            "item": {"source": "uncertainty_queue",
                     "queue_id": row["id"],
                     "class": row["class"],
                     "subject": row["subject"],
                     "work_key": row["work_key"],
                     "run_ref": row["run_ref"],
                     "packet": packet},
            "verdict": row["label"],
            "labeller": row["labelled_by"],
            "decided_at": row["labelled_at"],
            "evidence": evidence,
            "reason": None,
        })
    if not records:
        gaps.append("uncertainty_queue: no labelled packets yet")
    return records


def _supabase_get(url, key, table, params):
    req = urllib.request.Request(
        f"{url}/rest/v1/{table}?{params}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gold_from_supabase(config_path, gaps):
    records = []
    cfg = {}
    config_path = Path(config_path)
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except ValueError:
            cfg = {}
    url = (cfg.get("supabase_url") or "").rstrip("/")
    key = cfg.get("supabase_key") or ""
    if not url or not key:
        gaps.append("supabase: no supabase_url/supabase_key in config — "
                    "human claim rows not harvested")
        return records
    for table, identity_field in (("book_trope_claims", "trope_id"),
                                  ("book_trigger_claims", "warning")):
        try:
            rows = _supabase_get(
                url, key, table,
                "select=*&status=in.(confirmed,rejected)")
        except urllib.error.HTTPError as e:
            gaps.append(f"supabase {table}: HTTP {e.code} — not harvested")
            continue
        except (urllib.error.URLError, ValueError, OSError) as e:
            gaps.append(f"supabase {table}: unreachable "
                        f"({type(e).__name__}) — not harvested")
            continue
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            decided = None
            for field in ("decided_at", "reviewed_at", "labelled_at"):
                if row.get(field):
                    decided = row[field]
                    break
            records.append({
                "item": {"source": f"supabase:{table}",
                         "id": row.get("id"),
                         "work_id": row.get("work_id"),
                         "identity": row.get(identity_field),
                         "source_type": row.get("source_type")},
                "verdict": row.get("status"),
                "labeller": None,
                "decided_at": decided,
                "evidence": None,
                "reason": None,
            })
    return records


def build_gold(config_path, learn_dir):
    gaps = []
    records = []
    records += _gold_from_uncertainty_queue(learn_dir, gaps)
    records += _gold_from_supabase(config_path, gaps)
    # Gold records carry no single book identity (a decision is
    # about a work, not a preview run), so their meta is thinner
    # than the other datasets' — the spec asks for item, verdict,
    # labeller, decided-at, evidence, reason, and nothing invented
    # to fill the rest.
    out = []
    for rec in records:
        rec = dict(rec)
        rec["meta"] = {"dataset": DATASET_GOLD, "fidelity": "captured",
                       "split": None}
        out.append(rec)
    counters = {"records": len(out), "gaps": gaps,
                "by_source": {}}
    for rec in out:
        src = rec["item"].get("source")
        counters["by_source"][src] = counters["by_source"].get(src, 0) + 1
    return out, counters


# ---------------------------------------------------------------------------
# Report: counts only. The report reads meta blocks and counters —
# never quotes, never chapter text — so it is safe to print
# anywhere, including into a chat. Reconciliation compares the
# harvested counts against the source artefacts' own counts, per
# archived preview, and prints the verdict with the numbers.
# ---------------------------------------------------------------------------

def _read_jsonl(path):
    out = []
    if not path or not Path(path).exists():
        return out
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _latest_exports(manifest):
    latest = {}
    for entry in manifest["exports"]:
        latest[entry["dataset"]] = entry
    return latest


def build_report(store):
    paths = _ensure_store(store)
    manifest = _load_manifest(paths)
    split_map = _load_split_map(paths)
    exports = _latest_exports(manifest)
    archived = _archived_previews(paths, manifest)

    lines = []
    lines.append(f"Training-data store: {paths['root']}")
    n_prev = sum(1 for a in manifest["archives"]
                 if a.get("kind") == "preview")
    n_events = sum(1 for a in manifest["archives"]
                   if a.get("kind") == "run-events")
    lines.append(f"Archived: {n_prev} preview artefact(s), "
                 f"{n_events} run-events file(s)")
    lines.append(f"Split map (by book): {json.dumps(split_map)}")
    unmapped = sorted({(data.get('title') or '?') for _, data in archived
                       if _split_for(split_map, data.get('title'))
                       and not any(k.strip().casefold()
                                   == (data.get('title') or '').strip()
                                   .casefold() for k in split_map)})
    if unmapped:
        lines.append(f"Books not in the split map (defaulted to "
                     f"train): {', '.join(unmapped)}")

    dataset_records = {}
    for dataset, entry in sorted(exports.items()):
        recs = _read_jsonl(paths["root"] / entry["store_path"])
        dataset_records[dataset] = recs
        by_book, by_split, by_fidelity = {}, {}, {}
        for rec in recs:
            meta = rec.get("meta") or {}
            by_book[meta.get("book")] = by_book.get(meta.get("book"), 0) + 1
            by_split[meta.get("split")] = \
                by_split.get(meta.get("split"), 0) + 1
            by_fidelity[meta.get("fidelity")] = \
                by_fidelity.get(meta.get("fidelity"), 0) + 1
        lines.append(f"\n{dataset}: {len(recs)} record(s) "
                     f"({entry['store_path']}, built {entry['built_at']})")
        lines.append(f"  by book: {json.dumps(by_book)}")
        lines.append(f"  by split: {json.dumps(by_split)}")
        lines.append(f"  by fidelity: {json.dumps(by_fidelity)}")
        if dataset == DATASET_JUDGE:
            disp, judged_n = {}, 0
            for rec in recs:
                disp[rec.get("disposition")] = \
                    disp.get(rec.get("disposition"), 0) + 1
                if rec.get("judged"):
                    judged_n += 1
            lines.append(f"  by disposition: {json.dumps(disp)}; "
                         f"with a judge draw: {judged_n}")
            c = entry.get("counters") or {}
            lines.append(
                "  exclusions: "
                f"trope formula confidences {c.get('excluded_trope_formula_confidences', 0)}, "
                f"trigger heuristic confidences {c.get('excluded_trigger_heuristic_confidences', 0)}, "
                f"trigger fallback evidence {c.get('excluded_trigger_fallback_evidence', 0)}, "
                f"trigger unjudged evidence {json.dumps(c.get('excluded_trigger_unjudged_evidence', {}))}, "
                f"merge judge records out of scope {c.get('merge_judge_records_out_of_scope', 0)}")
        if dataset == DATASET_GOLD:
            gaps = (entry.get("counters") or {}).get("gaps") or []
            for gap in gaps:
                lines.append(f"  gap: {gap}")

    # Reconciliation: every archived preview's own counts against
    # the harvested records that cite it as their source.
    lines.append("\nReconciliation (per archived preview):")
    judge_recs = dataset_records.get(DATASET_JUDGE, [])
    extr_recs = dataset_records.get(DATASET_EXTRACTION, [])
    extr_counters = (exports.get(DATASET_EXTRACTION) or {}).get(
        "counters") or {}
    for entry, data in archived:
        src = entry.get("store_path")
        artefact_rels = len(data.get("relationships") or []) \
            + len(data.get("dropped_relationships") or [])
        harvested_rels = sum(
            1 for r in judge_recs
            if (r.get("meta") or {}).get("source_archive") == src
            and (r.get("claim") or {}).get("axis") == "relationships")
        artefact_chapters = len(
            (data.get("chapter_data") or {}).get("chapters") or {})
        harvested_chapters = sum(
            1 for r in extr_recs
            if (r.get("meta") or {}).get("source_archive") == src)
        ok_rel = artefact_rels == harvested_rels
        ok_ch = artefact_chapters == harvested_chapters
        verdict = "RECONCILED" if (ok_rel and ok_ch) else "MISMATCH"
        lines.append(
            f"  {entry.get('book_title')}: {verdict} — relationship "
            f"validation records artefact {artefact_rels} / harvested "
            f"{harvested_rels}; chapters artefact {artefact_chapters} "
            f"/ extraction pairs {harvested_chapters}")
        per_book = (extr_counters.get("per_book") or {}).get(
            entry.get("book"))
        if per_book:
            lines.append(
                f"    roster replay: final size "
                f"{per_book.get('replay_final_roster_size')} vs stored "
                f"{per_book.get('stored_roster_size')} (replay omits "
                f"the tiered merges and reconcile pass by design)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

def _corpus_for(args):
    corpus_path = args.corpus
    if corpus_path is None:
        corpus_path = str(Path(args.learn_dir) / "corpus.db")
    return Corpus(corpus_path)


def cmd_archive(args):
    counts = archive_sources(args.store, args.preview_dir,
                             args.run_events_dir)
    print(f"archive: {counts['previews_new']} preview(s) newly archived, "
          f"{counts['previews_skipped_known']} already archived, "
          f"{counts['previews_skipped_nonartefact']} non-artefact "
          f"skipped; {counts['run_events_new']} run-events file(s) "
          f"newly archived, {counts['run_events_skipped_known']} "
          f"already archived")
    return 0


def _prepare(args):
    """Every dataset command archives first — the spec's standing
    rule, because a preview that is not archived before the next
    run overwrites it is data already lost."""
    archive_sources(args.store, args.preview_dir, args.run_events_dir)
    paths = _ensure_store(args.store)
    manifest = _load_manifest(paths)
    split_map = _load_split_map(paths)
    return paths, manifest, split_map, _archived_previews(paths, manifest)


def cmd_judge_pairs(args):
    paths, manifest, split_map, archived = _prepare(args)
    corpus = _corpus_for(args)
    records, counters = build_judge_pairs(archived, corpus, split_map)
    dest = _write_dataset(paths, manifest, DATASET_JUDGE,
                          records, counters)
    print(f"judge-pairs: {len(records)} record(s) -> {dest}")
    print(f"  relationship validation records "
          f"{counters['relationship_validation_records']} "
          f"({counters['relationship_judge_draws']} with a judge draw); "
          f"dispositions {json.dumps(counters['by_disposition'])}; "
          f"trigger decision pairs {counters['trigger_decision_pairs']}")
    print(f"  exclusions: trope formula "
          f"{counters['excluded_trope_formula_confidences']}, trigger "
          f"fallback {counters['excluded_trigger_fallback_evidence']}, "
          f"trigger heuristic "
          f"{counters['excluded_trigger_heuristic_confidences']}")
    if not corpus.available:
        print("  note: no corpus.db found — evidence windows are "
              "empty in this export (quotes and verdicts unaffected)")
    return 0


def cmd_extraction_pairs(args):
    paths, manifest, split_map, archived = _prepare(args)
    corpus = _corpus_for(args)
    records, counters = build_extraction_pairs(archived, corpus,
                                               split_map)
    dest = _write_dataset(paths, manifest, DATASET_EXTRACTION,
                          records, counters)
    print(f"extraction-pairs: {len(records)} record(s) -> {dest}")
    print(f"  chapters {counters['chapters']} "
          f"({counters['chapters_with_text']} with corpus text); "
          f"validation joined {counters['validation_joined']}, "
          f"unjoined {counters['validation_unjoined']}")
    for book, pb in sorted(counters["per_book"].items()):
        print(f"  {book}: roster replay final "
              f"{pb['replay_final_roster_size']} vs stored "
              f"{pb['stored_roster_size']}")
    if not corpus.available:
        print("  note: no corpus.db found — chapter_text is null in "
              "this export (roster replay and deltas unaffected)")
    return 0


def cmd_gold(args):
    archive_sources(args.store, args.preview_dir, args.run_events_dir)
    paths = _ensure_store(args.store)
    manifest = _load_manifest(paths)
    records, counters = build_gold(args.config, args.learn_dir)
    dest = _write_dataset(paths, manifest, DATASET_GOLD,
                          records, counters)
    print(f"gold: {len(records)} record(s) -> {dest}")
    for gap in counters["gaps"]:
        print(f"  gap: {gap}")
    return 0


def cmd_report(args):
    print(build_report(args.store))
    return 0


def _add_store(ap):
    ap.add_argument("--store", default=str(DEFAULT_STORE),
                    help="training-data store directory "
                         f"(default {DEFAULT_STORE})")


def _add_archive_inputs(ap):
    ap.add_argument("--preview-dir", default=str(DEFAULT_PREVIEW_DIR),
                    help="processor preview directory to archive "
                         f"from (default {DEFAULT_PREVIEW_DIR})")
    ap.add_argument("--run-events-dir",
                    default=str(DEFAULT_LEARN_DIR / "run-events"),
                    help="run-events directory to archive from "
                         "(default the learn dir's run-events)")


def _add_corpus_input(ap):
    ap.add_argument("--learn-dir", default=str(DEFAULT_LEARN_DIR),
                    help=f"processor learn dir (default "
                         f"{DEFAULT_LEARN_DIR})")
    ap.add_argument("--corpus", default=None,
                    help="path to corpus.db (default: corpus.db in "
                         "the learn dir)")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="training_harvest.py",
        description="Harvest fine-tuning datasets (judge pairs, "
                    "extraction pairs, gold decisions) from ebook "
                    "processor artefacts into an append-only store.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_archive = sub.add_parser(
        "archive", help="copy previews + run-events into the store")
    _add_store(p_archive)
    _add_archive_inputs(p_archive)
    p_archive.set_defaults(fn=cmd_archive)

    p_judge = sub.add_parser(
        "judge-pairs", help="build dataset A (judge pairs)")
    _add_store(p_judge)
    _add_archive_inputs(p_judge)
    _add_corpus_input(p_judge)
    p_judge.set_defaults(fn=cmd_judge_pairs)

    p_extract = sub.add_parser(
        "extraction-pairs", help="build dataset B (extraction pairs, "
                                 "silver)")
    _add_store(p_extract)
    _add_archive_inputs(p_extract)
    _add_corpus_input(p_extract)
    p_extract.set_defaults(fn=cmd_extraction_pairs)

    p_gold = sub.add_parser(
        "gold", help="build dataset C (gold human decisions)")
    _add_store(p_gold)
    _add_archive_inputs(p_gold)
    ap_gold_learn = p_gold.add_argument(
        "--learn-dir", default=str(DEFAULT_LEARN_DIR),
        help=f"processor learn dir (default {DEFAULT_LEARN_DIR})")
    p_gold.add_argument(
        "--config", default=str(DEFAULT_CONFIG),
        help="processor config.json holding the Supabase credentials "
             f"(default {DEFAULT_CONFIG})")
    p_gold.set_defaults(fn=cmd_gold)

    p_report = sub.add_parser(
        "report", help="counts, exclusions, split map, "
                       "reconciliation (no text)")
    _add_store(p_report)
    p_report.set_defaults(fn=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except HarvestError as e:
        print(f"training_harvest: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
