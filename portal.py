"""Operator portal for the ebook processor (PORTAL-SPEC.md,
slices S1 + S2 + S3).

Run it beside process-portable.py:

    py portal.py [--port 8765] [--learn-dir DIR]

Slice S1 is the skeleton + Home (the Control Room): the health
strip, coverage cards, book table, recent runs, queue pointers,
and the running-now strip. The portal READS the processor's
stores directly (read-only SQLite) and writes nothing to them.

Slice S2 adds launching: the job registry and its one-writer
queue (spec §7), the §6 action inventory (ingest from the cover
picker, the standing preview/write commands, re-embed, Backup
now), and the run page — stage pills, results feed, the raw log
behind its toggle, Stop (SIGINT -> checkpoints) and Resume,
with the S0 run-events artifact tailed live for the pair /
verdict / decision data stdout cannot carry. Every write still
happens inside a CLI subprocess; the portal itself never opens
a store writable.

Slice S3 adds the Desk: the ruling queue over state.db's two
stores (§5.2's mapping, confirmed as drafted in Q4) — today's
trope batch and parked escalations with their evidence views,
impulse proposals from the local trope-map review files (Q5),
and uncertainty rulings through the CLI's labels intake (A6).
Staged claims carry evidence only: the CLI has no ruling verb
for them (GAP-4), and the Desk says so on its surface rather
than drawing controls that cannot act. The Matrices panel is
the remaining slice (S4) and is deliberately absent here.

Stack: Python standard library only. The portal's whole job is
reading two SQLite files and serving a dozen routes on the
operator's own machine; the stdlib does both without help, and
a dependency-free file arrives via the same `git pull` as the
processor and cannot rot against its release cadence. There is
nothing to install: if `py process-portable.py` runs, this runs.

Posture (spec §9 rulings Q11/Q12): binds 127.0.0.1 only — the
server refuses any other bind — with no auth, because the
machine is the boundary. If the default port is taken, the
next free port is used and the actual URL printed (Q11
amendment). Config values that reach the browser pass through
a whitelist: secrets in config.json are never serialized.

Optional portal-local settings live in portal-config.json
beside this file: {"port": 8765, "backup_dir": "...",
"learn_dir": "..."}. Command-line flags override it.
"""

import argparse
import ast
import hashlib
import html
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

DEFAULT_PORT = 8765
LOOPBACK = "127.0.0.1"
_LOOPBACK_SPELLINGS = {"127.0.0.1", "::1", "localhost"}
_PORT_SCAN_ATTEMPTS = 25

# The only config.json keys the browser may ever see. config.json
# also holds API keys and a Supabase user id; an allowlist (not a
# denylist) is what keeps a future secret key from leaking by
# default when someone adds it to the config file.
_CONFIG_DISPLAY_KEYS = [
    "llm", "pipeline", "embed_url", "embed_model",
    "decision_model", "decision_model_provider",
    "openrouter_model", "openai_model", "ollama_model",
    "trope_map_threshold", "dedupe_threshold", "import_dir",
]


class PortalError(Exception):
    """A portal configuration problem the operator must fix
    (bad bind host, no free port). Raised before serving starts,
    never mid-request."""


# ---------------------------------------------------------------------------
# Launch: bind validation + the Q11 port fallback
# ---------------------------------------------------------------------------

def validate_bind_host(host):
    """Return host if it names the loopback interface, else raise.

    Q12's posture is enforced here rather than by convention:
    make_server() runs every bind through this check, so no
    future flag or config value can widen the portal's reach
    past the operator's own machine without removing a guard
    that a reviewer can see.
    """
    if host in _LOOPBACK_SPELLINGS:
        return host
    raise PortalError(
        f"refusing to bind {host!r}: the portal is loopback-only "
        f"(127.0.0.1) by design (PORTAL-SPEC Q12)")


def _bind_test(host, port):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    finally:
        sock.close()


def pick_port(host, start, binder=None, attempts=_PORT_SCAN_ATTEMPTS):
    """First bindable port at or after `start` (Q11 amendment:
    when the default port is taken, move to the next free one
    instead of failing — the operator has one machine and the
    portal should just come up). The binder is injectable so the
    scan is testable without occupying real ports."""
    binder = binder or _bind_test
    for port in range(start, start + attempts):
        try:
            binder(host, port)
        except OSError:
            continue
        return port
    raise PortalError(
        f"no free port in {start}..{start + attempts - 1} — "
        f"set a different port in portal-config.json")


# ---------------------------------------------------------------------------
# Small mirrors of processor facts the portal must not re-derive wrongly
# ---------------------------------------------------------------------------

def source_constant(script_path, name):
    """Read a module-level literal constant out of
    process-portable.py's source (PORTABLE_VERSION,
    CORPUS_PARSER_VERSION, _MERGE_TIER3_CAP,
    _DECISION_POOL_CEILING).

    The portal mirrors the CLI's read models rather than
    importing the processor (a script, not a library); these
    constants are the only facts it needs that live
    nowhere in the stores. Parsing the assignment keeps the
    portal honest when a version bumps — a hardcoded copy
    would silently go stale. Returns None when unreadable.
    """
    try:
        text = Path(script_path).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf"^{re.escape(name)}\s*=\s*(.+)$", text,
                      re.MULTILINE)
    if not match:
        return None
    rhs = match.group(1).strip()
    for candidate in (rhs, rhs.split("#", 1)[0].strip()):
        try:
            return ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            continue
    return None


def config_display(config_path):
    """The whitelisted, display-safe slice of config.json.

    Display + (in later slices) launch-flag sourcing only; the
    portal never edits config. A missing or malformed file is an
    empty display, not an error — the health strip just has less
    to say.
    """
    try:
        raw = json.loads(Path(config_path).read_text(
            encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: raw[k] for k in _CONFIG_DISPLAY_KEYS if k in raw}


def backup_info(backup_dir):
    """Last-backup display from the backup directory's manifest.

    --backup-state writes manifest.json (created time + per-store
    dump hashes) beside the store copies; the manifest's presence
    is the "recorded" state. Verifying the hashes is the restore
    command's job (§9 Q6 keeps restore terminal-only), so the
    strip reports the manifest, not a re-verification.
    """
    info = {"configured": False, "last_backup": None,
            "stores": [], "note": None}
    if not backup_dir:
        info["note"] = "no backup directory configured"
        return info
    info["configured"] = True
    manifest_path = Path(backup_dir) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        info["note"] = "no manifest found in the backup directory"
        return info
    info["last_backup"] = manifest.get("created")
    stores = manifest.get("stores") or {}
    info["stores"] = sorted(
        k for k, v in stores.items()
        if isinstance(v, dict) and v.get("present"))
    return info


# ---------------------------------------------------------------------------
# Read models — direct read-only SQLite, mirroring the CLI's own queries
# ---------------------------------------------------------------------------

def _ro_connect(path):
    """A short-lived read-only connection. mode=ro is the S1
    no-writes guarantee at the SQLite layer: even a buggy query
    in this file cannot mutate a store it cannot open writable.
    A short busy timeout keeps a mid-write store from hanging a
    page load; the caller turns a lock into a 'store busy'
    notice instead of retrying in a hot loop (spec §4)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                           timeout=2.0)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _store_size(path):
    """File bytes including WAL sidecars — the same accounting
    as the substrate's size_info(), so portal and CLI quote the
    same store size."""
    total = 0
    for candidate in (path, Path(str(path) + "-wal"),
                      Path(str(path) + "-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            pass
    return total


def _size_breakdown(conn, db_path):
    """corpus.db bytes by component, mirroring
    _corpus_size_breakdown: dbstat page counts when this SQLite
    has them, content-byte estimates as the fallback."""
    total = _store_size(db_path)
    try:
        pages = {name: size for name, size in conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
        text = pages.get("corpus_chapters", 0)
        fts = pages.get("corpus_fts", 0) + sum(
            v for k, v in pages.items() if k.startswith("corpus_fts_"))
        vectors = pages.get("passage_vectors", 0)
        return {"text": text, "fts": fts, "vectors": vectors,
                "other": max(total - text - fts - vectors, 0),
                "total": total, "via": "dbstat"}
    except sqlite3.Error:
        text = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(text)), 0) "
            "FROM corpus_chapters").fetchone()[0]
        vectors = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(vector)), 0) "
            "FROM passage_vectors").fetchone()[0]
        return {"text": text, "fts": 0, "vectors": vectors,
                "other": max(total - text - vectors, 0),
                "total": total, "via": "content-estimate"}


def _generations(conn):
    """Vector generations recorded in meta, oldest first —
    mirrors _corpus_generations."""
    gens = []
    for _k, v in conn.execute(
            "SELECT k, v FROM meta WHERE k LIKE 'generation:%' "
            "ORDER BY k"):
        try:
            gens.append(json.loads(v))
        except (TypeError, ValueError):
            continue
    return gens


def _last_activity(conn, fingerprint):
    """A work's latest stage_runs / ingest_runs record, composed
    by direct read (spec §5.1's 'last stage'). Returns
    {label, when} or None."""
    candidates = []
    row = conn.execute(
        "SELECT stage, status, COALESCE(finished, started) "
        "FROM stage_runs WHERE fingerprint=? "
        "ORDER BY COALESCE(finished, started) DESC LIMIT 1",
        (fingerprint,)).fetchone()
    if row:
        candidates.append((row[2] or "", f"stage {row[0]} · {row[1]}"))
    row = conn.execute(
        "SELECT outcome, detail, COALESCE(finished, started) "
        "FROM ingest_runs WHERE fingerprint=? "
        "ORDER BY COALESCE(finished, started) DESC LIMIT 1",
        (fingerprint,)).fetchone()
    if row:
        kind = "ingest"
        try:
            if (json.loads(row[1] or "{}") or {}).get("kind") == "reembed":
                kind = "reembed"
        except (TypeError, ValueError):
            pass
        candidates.append((row[2] or "", f"{kind} · {row[0]}"))
    if not candidates:
        return None
    when, label = max(candidates)
    return {"label": label, "when": when}


def _corpus_reads(conn, db_path):
    """The corpus half of Home: the _corpus_status_data queries
    mirrored verbatim, plus the envelope facts and last-activity
    composition §5.1 adds. One connection, one snapshot of the
    store, so a page never mixes two moments."""
    cur = conn.execute(
        "SELECT v FROM meta WHERE k='generation_current'").fetchone()
    gen = None
    if cur:
        try:
            gen = json.loads(cur[0])
        except (TypeError, ValueError):
            gen = None
    fts_available = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='corpus_fts'"
    ).fetchone() is not None
    works = []
    by_status = {}
    for fp, title, author, series, sidx, status, pv, nch, \
            payload_raw in conn.execute(
            "SELECT fingerprint, title, author, series, "
            "series_index, status, parser_version, chapter_count, "
            "payload FROM corpus_works "
            "ORDER BY title, fingerprint"):
        n_pass = conn.execute(
            "SELECT COUNT(*) FROM corpus_passages WHERE fingerprint=?",
            (fp,)).fetchone()[0]
        coverage = None
        if gen and n_pass:
            n_vec = conn.execute(
                "SELECT COUNT(*) FROM passage_vectors pv JOIN "
                "corpus_passages p ON p.passage_id = pv.passage_id "
                "WHERE p.fingerprint=? AND pv.model=? AND pv.format=?",
                (fp, gen.get("model"), gen.get("format"))).fetchone()[0]
            coverage = round(n_vec / n_pass, 4)
        env = conn.execute(
            "SELECT computed_at FROM work_envelope WHERE fingerprint=?",
            (fp,)).fetchone()
        series_label = None
        if series:
            series_label = series
            if sidx is not None:
                # series_index is a REAL column, but a hand-edited
                # or foreign-written store can hold text there;
                # one malformed value must not take Home down
                # (the slice's degrade-never-error contract).
                try:
                    num = float(sidx)
                    idx = int(num) if num.is_integer() else sidx
                except (TypeError, ValueError):
                    idx = sidx
                series_label = f"{series} #{idx}"
        # The ingest-recorded source file name rides the
        # payload; it is the join key to the run-events
        # artifacts (§2.1's "last run's matrix"), whose books
        # are file names and nothing else.
        source_name = None
        try:
            payload = json.loads(payload_raw or "{}") or {}
            if isinstance(payload, dict):
                source_name = payload.get("source_name")
        except (TypeError, ValueError):
            source_name = None
        works.append({
            "fingerprint": fp, "title": title, "author": author,
            "series_label": series_label, "status": status,
            "parser_version": pv, "chapters": nch,
            "passages": n_pass, "vector_coverage": coverage,
            "source_name": source_name,
            "envelope_computed_at": env[0] if env else None,
            "last_activity": _last_activity(conn, fp)})
        by_status[status] = by_status.get(status, 0) + 1
    quotes_by_status = {}
    for status, n in conn.execute(
            "SELECT status, COUNT(*) FROM quotes GROUP BY status"):
        quotes_by_status[status] = n
    # Coverage facts that live only in the envelope: a fact is
    # counted when the work's envelope carries a section for it
    # in state 'present' (§5.1). Sections are matched by their
    # stage name as well as their key, because the section key
    # and the stage that wrote it are the same fact recorded
    # twice; a section the corpus has not grown yet simply
    # counts zero, and the card says so.
    facts = {"ending": 0, "triggers": 0}
    for (blob,) in conn.execute("SELECT envelope FROM work_envelope"):
        try:
            sections = (json.loads(blob or "{}") or {}).get(
                "sections") or {}
        except (TypeError, ValueError):
            continue
        for key, section in sections.items():
            if not isinstance(section, dict):
                continue
            if section.get("state") != "present":
                continue
            names = {key, section.get("stage")}
            for fact in facts:
                if fact in names:
                    facts[fact] += 1
    generations = _generations(conn)
    current_gen = None
    if gen:
        for g in generations:
            if (g.get("model") == gen.get("model")
                    and g.get("format") == gen.get("format")):
                current_gen = g
                break
    return {
        "works": works, "by_status": by_status,
        "quotes_by_status": quotes_by_status,
        "generations": generations, "current_generation": current_gen,
        "generation_pointer": gen, "fts_available": fts_available,
        "bytes": _size_breakdown(conn, db_path), "facts": facts,
    }


def _recent_runs(corpus_conn, works_by_fp, preview_dir):
    """Recent runs, composite (spec §5.1, GAP-5's soft edges
    accepted): ingest + stage records from corpus.db, preview
    artifacts from preview/. A pipeline run that left no record
    and no artifact is invisible here — the composite shows
    what the stores can prove, not what the operator remembers."""
    runs = []
    if corpus_conn is not None:
        for fp, started, finished, outcome, detail in corpus_conn.execute(
                "SELECT fingerprint, started, finished, outcome, "
                "detail FROM ingest_runs "
                "ORDER BY COALESCE(finished, started) DESC LIMIT 12"):
            kind = "ingest"
            source = None
            try:
                parsed = json.loads(detail or "{}") or {}
                if parsed.get("kind") == "reembed":
                    kind = "reembed"
                source = parsed.get("source_name")
            except (TypeError, ValueError):
                pass
            runs.append({
                "kind": kind, "when": finished or started,
                "work": works_by_fp.get(fp, (fp or "")[:8]),
                "outcome": outcome or "",
                "detail": source or ""})
        for fp, stage, status, started, finished in corpus_conn.execute(
                "SELECT fingerprint, stage, status, started, "
                "finished FROM stage_runs "
                "ORDER BY COALESCE(finished, started) DESC LIMIT 12"):
            runs.append({
                "kind": f"stage: {stage}",
                "when": finished or started,
                "work": works_by_fp.get(fp, (fp or "")[:8]),
                "outcome": status or "", "detail": ""})
    if preview_dir is not None and Path(preview_dir).is_dir():
        # Parse only the newest artifacts: the composite shows
        # 12 runs, so re-reading every preview file on every
        # poll would grow without bound as artifacts accumulate.
        # mtime is the sort the composite itself uses, so the
        # newest 25 files always contain any preview that could
        # make the cut.
        artifacts = [p for p in Path(preview_dir).glob("*.json")
                     if not p.name.endswith("-trope-map.json")
                     and not p.name.startswith(".")]
        # A preview file can vanish between the glob and either
        # stat below (operator cleanup, harvest archiving while the
        # Portal polls). A vanished file is simply absent from
        # recent runs — a named, non-fatal outcome for that file —
        # so both stat sites treat OSError as "not there" instead
        # of failing the whole Home assembly.
        def _sort_mtime(p):
            try:
                return p.stat().st_mtime
            except OSError:
                return float("-inf")

        artifacts.sort(key=_sort_mtime, reverse=True)
        for path in artifacts[:25]:
            try:
                when = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S")
            except OSError:
                continue
            title, outcome = path.stem, ""
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                title = data.get("title") or title
                parts = []
                for key, label in (("tropes", "tropes"),
                                   ("triggers", "triggers"),
                                   ("characters", "characters")):
                    value = data.get(key)
                    if isinstance(value, list):
                        parts.append(f"{len(value)} {label}")
                if data.get("chunks_failed"):
                    parts.append(
                        f"{data['chunks_failed']} chunks failed")
                outcome = " · ".join(parts)
            runs.append({"kind": "preview", "when": when,
                         "work": title, "outcome": outcome,
                         "detail": path.name})
    runs.sort(key=lambda r: r["when"] or "", reverse=True)
    return runs[:12]


def _queue_pointers(state_path):
    """The Desk's two counts for Home's queue pointers (§5.2's
    mapping): the batch is staged claims awaiting the staging
    gate's own promotion paths; parked is the uncertainty
    queue's unlabelled items. Counts only — Home carries no
    ruling controls. Returns None when state.db is absent."""
    if not Path(state_path).exists():
        return None
    conn = _ro_connect(state_path)
    try:
        batch = conn.execute(
            "SELECT COUNT(*) FROM staging_claims "
            "WHERE state='staged'").fetchone()[0]
        parked = conn.execute(
            "SELECT COUNT(*) FROM uncertainty_queue "
            "WHERE status='pending'").fetchone()[0]
        return {"batch": batch, "parked": parked}
    finally:
        conn.close()


def home_data(learn_dir, script_dir, backup_dir=None):
    """Assemble everything Home renders, as one dict. Missing
    stores degrade to empty surfaces with the absence stated —
    the Control Room's first job on a fresh machine is saying
    'nothing ingested yet', not erroring."""
    learn_dir = Path(learn_dir)
    script_dir = Path(script_dir)
    corpus_path = learn_dir / "corpus.db"
    state_path = learn_dir / "state.db"
    busy = []
    corpus = None
    corpus_conn = None
    if corpus_path.exists():
        try:
            corpus_conn = _ro_connect(corpus_path)
            corpus = _corpus_reads(corpus_conn, corpus_path)
        except sqlite3.Error:
            busy.append("corpus.db")
            if corpus_conn is not None:
                corpus_conn.close()
                corpus_conn = None
            corpus = None
    works = corpus["works"] if corpus else []
    works_by_fp = {w["fingerprint"]: w["title"] or w["fingerprint"][:8]
                   for w in works}
    recent = _recent_runs(corpus_conn, works_by_fp,
                          script_dir / "preview")
    if corpus_conn is not None:
        corpus_conn.close()
    queues = None
    try:
        queues = _queue_pointers(state_path)
    except sqlite3.Error:
        busy.append("state.db")
    parser_current = source_constant(
        script_dir / "process-portable.py", "CORPUS_PARSER_VERSION")
    stale = sum(1 for w in works
                if parser_current is not None
                and w["parser_version"] != parser_current)
    for w in works:
        w["stale_parser"] = bool(
            parser_current is not None
            and w["parser_version"] != parser_current)
    # "Last run's matrix" (§2.1, S4): which retained artifact,
    # if any, last scored each work's book. The scan memoises
    # on the artifacts' signatures, so the 30 s Home refresh
    # pays a directory listing, not a re-read.
    matrix_map = latest_matrices(
        learn_dir, {w.get("source_name") for w in works})
    for w in works:
        entry = matrix_map.get(w.get("source_name") or "")
        w["matrix_run"] = entry["run_id"] if entry else None
    quotes_by_status = corpus["quotes_by_status"] if corpus else {}
    cards = {
        "works_total": len(works),
        "works_by_status": corpus["by_status"] if corpus else {},
        "works_complete": (corpus["by_status"].get("complete", 0)
                           if corpus else 0),
        "endings_assessed": corpus["facts"]["ending"] if corpus else 0,
        "trigger_depth": corpus["facts"]["triggers"] if corpus else 0,
        "quotes_verified": (quotes_by_status.get("verified", 0)
                             + quotes_by_status.get("selected", 0)),
        "quotes_by_status": quotes_by_status,
    }
    gen = corpus["current_generation"] if corpus else None
    health = {
        "canary": (gen or {}).get("canary"),
        "generation": ({"model": gen.get("model"),
                        "format": gen.get("format"),
                        "dim": gen.get("dim")} if gen else None),
        "generations_known": len(corpus["generations"]) if corpus else 0,
        "parser": {"current": parser_current, "stale_works": stale},
        "stores": {
            "corpus": corpus["bytes"] if corpus else None,
            "state_bytes": (_store_size(state_path)
                            if state_path.exists() else None),
        },
        "fts_available": (corpus["fts_available"] if corpus else None),
        "backup": backup_info(backup_dir),
        "setup": config_display(script_dir / "config.json"),
        "processor_version": source_constant(
            script_dir / "process-portable.py", "PORTABLE_VERSION"),
    }
    cap = source_constant(script_dir / "process-portable.py",
                          "_MERGE_TIER3_CAP")
    ceiling = source_constant(script_dir / "process-portable.py",
                              "_DECISION_POOL_CEILING")
    strip = {
        "jobs": [],
        "decision_cap": cap if isinstance(cap, int) else 50,
        "decision_ceiling": ceiling if isinstance(ceiling, int)
        else 250,
        "note": ("Runs launched from this portal appear here. "
                 "A run started in a terminal also appears — "
                 "labelled external — while it runs, when it "
                 "was launched with --emit-run-events; its "
                 "finished preview appears under Recent runs."),
    }
    return {
        "present": corpus is not None, "dir": str(learn_dir),
        "busy": busy, "health": health, "cards": cards,
        "works": works, "recent_runs": recent, "queues": queues,
        "strip": strip,
    }


def resolve_work(learn_dir, query):
    """Resolve an operator's work reference to a full
    fingerprint, or None. The explain endpoint shells the CLI;
    the fingerprint it passes is always this database-resolved
    value, never the raw request string, so request input can
    never become a CLI argument the operator didn't name."""
    if not query or len(query) < 4:
        return None
    corpus_path = Path(learn_dir) / "corpus.db"
    if not corpus_path.exists():
        return None
    escaped = (query.replace("\\", "\\\\").replace("%", "\\%")
               .replace("_", "\\_"))
    conn = _ro_connect(corpus_path)
    try:
        rows = conn.execute(
            "SELECT fingerprint FROM corpus_works WHERE "
            "fingerprint = ? OR fingerprint LIKE ? ESCAPE '\\' "
            "LIMIT 2", (query, escaped + "%")).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        return None
    return rows[0][0]


# ---------------------------------------------------------------------------
# The Desk (slice S3): the ruling queue over state.db's two stores
# ---------------------------------------------------------------------------

# The batch window: one labelling round's worth of staged claims.
# The spine design caps a round at ~20 (OC-4) because a queue that
# becomes homework dies; the Desk's batch is that round, and every
# staged claim past it is parked — §5.2's mapping, stated there so
# the split is a decision and not an accident.
_DESK_BATCH_WINDOW = 20

# The labeller string the portal records on its rulings. The
# labels intake stores it verbatim in the queue's audit columns
# (label / labelled_by / labelled_at); the CLI's own intake test
# files its labels as "user", and the portal rules as the
# operator's hand, so it uses the same name.
_DESK_LABELLER = "user"

# The verdict strings the Desk offers per packet kind. Only
# merge_pair's pair is machine-consumed — the CLI appends
# same/different merge pairs to eval/merge_pairs.json, so a
# different string would silently lose that flow. The rest are
# the labeller's audit vocabulary, stored verbatim on the queue
# row; each button therefore shows the exact string it records,
# never a friendlier paraphrase of it.
_DESK_LABELS = {
    "merge_pair": ("same", "different"),
    "split_suspect": ("same", "different"),
    "family_claim": ("family", "not-family"),
    "claim": ("correct", "incorrect"),
}
_DESK_LABELS_DEFAULT = ("correct", "incorrect")

# GAP-4, stated on the Desk itself (Q4's ruling: an operator
# surface should say what it cannot do). Staged claims have no
# ruling verb — the staging gate promotes by recurrence, band,
# or evidence — so the batch shows evidence and no controls.
_GAP4_NOTE = (
    "Staged claims cannot be ruled from the Desk: the CLI has "
    "no verb to keep or drop a staged claim by hand (GAP-4). "
    "The staging gate promotes by recurrence, band, or "
    "evidence; this page shows the evidence and leaves the "
    "gate to its work.")

# Placeholder in PortalContext._ruling_jobs between claiming an
# item's in-flight slot and the launched job landing in it.
_RULING_SENTINEL = object()


def _labels_for_kind(kind):
    return list(_DESK_LABELS.get(kind, _DESK_LABELS_DEFAULT))


def _json_list(raw):
    """A stored JSON list column, read defensively: the Desk
    renders operator evidence, and a malformed cell must degrade
    to an empty list, never take the page down."""
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _num(value):
    """A store cell as an ordering number: pipeline-written
    counts are ints, but the Desk treats the store as untrusted
    data — a non-numeric cell orders as 0 and is displayed
    (escaped) as what it is, never crashed on."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _desk_claims(conn):
    """Every staged claim with its work, in staging currency.
    Mirrors the claim shape of the CLI's own staging_report_data
    (the §4 rule: portal SQL mirrors the CLI's read models).
    Ordered most-judgeable first — sightings, then chapter
    support (§5.2) — with title/display tiebreaks so the order
    cannot wobble between polls."""
    works = {}
    for wk, wid, title, author in conn.execute(
            "SELECT work_key, work_id, title, author "
            "FROM staging_works"):
        works[wk] = {"work_id": wid, "title": title or "",
                     "author": author or ""}
    claims = []
    for row in conn.execute(
            "SELECT work_key, claim_key, class, display, "
            "promoted_via, best_chapters, first_seen, last_seen, "
            "sightings, name_forms FROM staging_claims "
            "WHERE state='staged'"):
        work = works.get(row[0], {"work_id": None, "title": "",
                                  "author": ""})
        sightings = _json_list(row[8])
        claims.append({
            "work_key": row[0], "work_id": work["work_id"],
            "work_title": work["title"],
            "work_author": work["author"],
            "key": row[1], "class": row[2], "display": row[3],
            "promoted_via": row[4], "best_chapters": row[5] or 0,
            "first_seen": row[6], "last_seen": row[7],
            "sightings": sightings,
            "sightings_count": len(sightings),
            "name_forms": _json_list(row[9]),
        })
    claims.sort(key=lambda c: (-c["sightings_count"],
                               -_num(c["best_chapters"]),
                               c["work_title"], c["display"]))
    return claims


def _desk_uncertainty(conn):
    """Pending uncertainty items, queue order — the parked half
    of the Desk. Labelled rows have left the queue and control
    rows were never in it (both stay in the table as the audit
    trail; neither is the operator's homework). The packet
    itself is not carried here; the evidence endpoint reads it
    per item, so the list stays light."""
    out = []
    for row in conn.execute(
            "SELECT id, created, run_ref, work_key, class, "
            "subject, packet FROM uncertainty_queue "
            "WHERE status='pending' ORDER BY id"):
        try:
            packet = json.loads(row[6]) if row[6] else {}
        except (TypeError, ValueError):
            packet = {}
        kind = packet.get("kind") if isinstance(packet, dict) \
            else None
        out.append({
            "id": row[0], "created": row[1], "run_ref": row[2],
            "work_key": row[3], "class": row[4], "subject": row[5],
            "kind": kind, "labels": _labels_for_kind(kind),
        })
    return out


def uncertainty_item(learn_dir, queue_id):
    """One pending uncertainty item with its full packet, or
    None. The Desk rules only what it listed: an item that is
    not pending (already labelled, or a control) is not here,
    and a ruling re-reads through this function rather than
    trusting the request's copy of the row."""
    if not isinstance(queue_id, int):
        return None
    state_path = Path(learn_dir) / "state.db"
    if not state_path.exists():
        return None
    conn = _ro_connect(state_path)
    try:
        row = conn.execute(
            "SELECT id, created, run_ref, work_key, class, "
            "subject, packet FROM uncertainty_queue "
            "WHERE id=? AND status='pending'",
            (queue_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        packet = json.loads(row[6]) if row[6] else {}
    except (TypeError, ValueError):
        packet = {}
    if not isinstance(packet, dict):
        packet = {}
    return {
        "id": row[0], "created": row[1], "run_ref": row[2],
        "work_key": row[3], "class": row[4], "subject": row[5],
        "kind": packet.get("kind"), "packet": packet,
        "labels": _labels_for_kind(packet.get("kind")),
    }


def desk_claim(learn_dir, work_key, claim_key):
    """The staged claim the Desk listed, re-read fresh, with its
    complete sightings history. Evidence endpoints verify
    against the store rather than trusting request parameters:
    a pair that is not a staged claim (unknown, or promoted
    since the page loaded) is None."""
    state_path = Path(learn_dir) / "state.db"
    if not state_path.exists():
        return None
    conn = _ro_connect(state_path)
    try:
        for claim in _desk_claims(conn):
            if claim["work_key"] == work_key \
                    and claim["key"] == claim_key:
                return claim
        return None
    finally:
        conn.close()


def _impulse_proposals(preview_dir):
    """Q5: impulse proposals are the LOCAL --trope-map review
    files' proposals[] — per-preview records, not the global
    queue (write-runs record theirs in Supabase, which the
    portal never reads, §4). A broken review file is skipped
    and counted, never fatal: the Desk reports what it could
    read and says that some of it could not be read."""
    out = []
    files = 0
    broken = 0
    preview_dir = Path(preview_dir)
    if preview_dir.is_dir():
        for path in sorted(preview_dir.glob("*-trope-map.json")):
            try:
                data = json.loads(
                    path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                broken += 1
                continue
            if not isinstance(data, dict):
                broken += 1
                continue
            files += 1
            for entry in data.get("proposals") or []:
                if not isinstance(entry, dict):
                    continue
                out.append({
                    "file": path.name,
                    "preview": data.get("preview"),
                    "threshold": data.get("threshold"),
                    "label": entry.get("pipeline_trope"),
                    "chapter_count": entry.get("chapter_count"),
                    "nearest_catalog_id":
                        entry.get("nearest_catalog_id"),
                    "nearest_similarity":
                        entry.get("nearest_similarity"),
                    "book": entry.get("book"),
                    "isbn": entry.get("isbn"),
                })
    return out, files, broken


def desk_data(learn_dir, script_dir):
    """Assemble the Desk (§5.2): today's batch (staged claims,
    sightings then chapter support, one round's window), parked
    (pending uncertainty items + staged claims beyond the
    window), and impulse proposals from the local trope-map
    files. Missing stores degrade to empty sections with the
    absence stated, Home's posture."""
    learn_dir = Path(learn_dir)
    script_dir = Path(script_dir)
    proposals, files, broken = _impulse_proposals(
        script_dir / "preview")
    desk = {
        "present": False, "busy": [],
        "batch": [], "parked_claims": [], "parked_items": [],
        "proposals": proposals, "proposals_files": files,
        "proposals_broken": broken,
        "gap4": _GAP4_NOTE, "batch_window": _DESK_BATCH_WINDOW,
        "staged_total": 0,
    }
    state_path = learn_dir / "state.db"
    if not state_path.exists():
        return desk
    conn = None
    try:
        conn = _ro_connect(state_path)
        claims = _desk_claims(conn)
        desk["batch"] = claims[:_DESK_BATCH_WINDOW]
        desk["parked_claims"] = claims[_DESK_BATCH_WINDOW:]
        desk["staged_total"] = len(claims)
        desk["parked_items"] = _desk_uncertainty(conn)
        desk["present"] = True
    except sqlite3.Error:
        desk["busy"] = ["state.db"]
    finally:
        if conn is not None:
            conn.close()
    return desk


# ---------------------------------------------------------------------------
# Launching (slice S2): the §6 action inventory as argv builders
# ---------------------------------------------------------------------------

# The standing command (spec §6, A2) exactly as the operator
# runs it by hand. Portal launches add only the per-action
# flags §6 names (--preview / --file / --no-viz), the S0
# emission flag (its acceptance names portal-launched previews
# as the artifact's producers), and --learn-dir so the child
# writes to the same stores the portal reads.
STANDING_FLAGS = [
    "--pipeline", "v2",
    "--decision-model",
    "--decision-model-provider", "openrouter",
    "--llm", "openai",
    "--rename-no-isbn",
    "--show-matrices",
    "--roster-reconcile",
]

# Mirrors _CORPUS_BOOK_SUFFIXES in the processor: the formats
# --ingest accepts, and therefore the picker's universe.
_BOOK_SUFFIXES = (".epub", ".mobi", ".azw3")


class LaunchSpec:
    """One validated launch: the argv to run, the label the run
    page shows, and the facts later steps need (the source file
    for checkpoint detection and Resume). Built only by
    build_launch / resume_spec — handlers never assemble argv
    themselves, so every portal launch passes the same checks."""

    def __init__(self, action, argv, label, file_path=None,
                 work=None):
        self.action = action
        self.argv = argv
        self.label = label
        self.file_path = file_path
        self.work = work

    @property
    def is_pipeline(self):
        return self.action in ("preview", "write")


def _raw_config(ctx):
    """The full config.json, for launch sourcing only (import
    dir, profiles). Nothing from here is serialized to the
    browser — display goes through config_display's whitelist."""
    try:
        raw = json.loads((ctx.script_dir / "config.json").read_text(
            encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def import_dir(ctx):
    """The operator's book drop folder, resolved the way the
    CLI resolves it when the portal launches it (cwd = the
    script dir): a relative import_dir in config.json is
    relative to the repo root."""
    raw = _raw_config(ctx).get("import_dir") or "./import"
    path = Path(raw)
    if not path.is_absolute():
        path = ctx.script_dir / path
    return path


def import_files(ctx):
    """The ingest picker's universe: book files in the import
    dir, name-sorted, with sizes. A missing dir is an empty
    picker with the absence stated by the page, not an error."""
    folder = import_dir(ctx)
    out = []
    if folder.is_dir():
        for path in sorted(folder.iterdir(), key=lambda p: p.name):
            if path.is_file() \
                    and path.suffix.lower() in _BOOK_SUFFIXES:
                try:
                    out.append({"name": path.name,
                                "size": path.stat().st_size})
                except OSError:
                    continue
    return out


def _picker_file(ctx, file_name):
    """Resolve a picker file name to its path, or refuse. The
    name must be a bare file name (no separators, no traversal)
    with a book suffix, present in the import dir listing — the
    browser can only ever aim the pipeline at a file the picker
    itself offered."""
    if not file_name or Path(file_name).name != file_name:
        raise PortalError(f"no such picker file: {file_name!r}")
    if Path(file_name).suffix.lower() not in _BOOK_SUFFIXES:
        raise PortalError(f"not a book file: {file_name!r}")
    path = import_dir(ctx) / file_name
    if not path.is_file():
        raise PortalError(f"no such picker file: {file_name!r}")
    return path


def _source_dict_literal(script_path, name):
    """A module-level dict literal out of the processor's
    source, balanced-brace extracted: BUILTIN_PROFILES spans
    lines, which the single-line source_constant cannot hold.
    Returns the dict, or None when unreadable."""
    try:
        text = Path(script_path).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf"^{re.escape(name)}\s*=\s*\{{", text,
                      re.MULTILINE)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = ast.literal_eval(text[start:i + 1])
                except (ValueError, SyntaxError):
                    return None
                return value if isinstance(value, dict) else None
    return None


def available_profiles(script_dir=None):
    """name -> 'builtin' | 'user', mirroring the CLI's
    _available_profiles: builtins are read out of the launched
    script's source (a dict literal, parsed like the other
    source constants) and user profiles are the JSON files in
    ~/.ebook-processor/profiles."""
    if script_dir is None:
        script_dir = Path(__file__).resolve().parent
    names = {}
    builtins = _source_dict_literal(
        Path(script_dir) / "process-portable.py", "BUILTIN_PROFILES")
    if isinstance(builtins, dict):
        names = {name: "builtin" for name in builtins}
    profiles_dir = Path.home() / ".ebook-processor" / "profiles"
    try:
        if profiles_dir.is_dir():
            for path in sorted(profiles_dir.glob("*.json")):
                names[path.stem] = "user"
    except OSError:
        pass
    return names


def build_launch(ctx, spec):
    """Validate a launch request dict and build its LaunchSpec
    (spec §6). Raises PortalError with the operator-facing
    reason; the HTTP layer turns that into a 400, so a refused
    launch never reaches the queue half-built."""
    action = (spec or {}).get("action")
    script = str(ctx.script_dir / "process-portable.py")
    base = [sys.executable, script]
    learn = ["--learn-dir", str(ctx.learn_dir)]

    if action == "ingest":
        path = _picker_file(ctx, spec.get("file_name"))
        argv = base + ["--ingest", str(path)]
        if spec.get("reason"):
            argv += ["--ingest-reason", str(spec["reason"])]
        if spec.get("force"):
            argv += ["--ingest-force"]
        argv += learn
        return LaunchSpec("ingest", argv,
                          f"Ingest — {path.name}", file_path=path)

    if action in ("preview", "write"):
        if action == "write" and spec.get("confirm") is not True:
            # A4 publishes. The confirm is enforced HERE, server
            # side, not by a disabled button: a request that did
            # not carry the operator's explicit confirm never
            # becomes a write run.
            raise PortalError(
                "write runs publish to the corpus database — "
                "confirm is required")
        argv = base + list(STANDING_FLAGS)
        if action == "preview":
            argv += ["--preview"]
        argv += ["--no-viz", "--emit-run-events"]
        path = None
        if spec.get("file_name"):
            path = _picker_file(ctx, spec["file_name"])
            argv += ["--file", str(path)]
        if spec.get("profile"):
            profile = str(spec["profile"])
            if profile not in available_profiles(ctx.script_dir):
                raise PortalError(f"unknown profile: {profile!r}")
            argv += ["--profile", profile]
        argv += learn
        what = path.name if path else "import dir"
        label = ("Preview" if action == "preview" else "Write run") \
            + f" — {what}"
        return LaunchSpec(action, argv, label, file_path=path)

    if action == "reembed":
        argv = base + ["--corpus-reembed"]
        work = spec.get("work")
        if work:
            if not re.fullmatch(r"[0-9a-fA-F]{8,64}", str(work)):
                raise PortalError("work must be a fingerprint")
            argv += ["--work", str(work)]
        argv += learn
        label = "Re-embed — " + (f"work {str(work)[:8]}"
                                  if work else "all works")
        return LaunchSpec("reembed", argv, label, work=work)

    if action == "backup":
        if not ctx.backup_dir:
            raise PortalError(
                "no backup directory configured — set backup_dir "
                "in portal-config.json (Q6: configured, never "
                "prompted per click)")
        argv = base + ["--backup-state", str(ctx.backup_dir)] + learn
        return LaunchSpec("backup", argv, "Backup now")

    if action == "apply_labels":
        # A6 (§6): the portal writes the labels file (the CLI's
        # own intake format) and the CLI consumes it. Only a
        # file the portal itself just wrote may be applied: the
        # path must sit directly in the portal's labels dir, so
        # a request can never aim the intake at an arbitrary
        # file on the operator's disk.
        raw = spec.get("labels_file") or ""
        path = Path(str(raw))
        labels_root = (ctx.script_dir / "portal-cache"
                       / "labels").resolve()
        try:
            inside = path.resolve().parent == labels_root
        except OSError:
            inside = False
        if not inside or not path.is_file():
            raise PortalError(
                "labels file is not a portal labels file")
        argv = base + ["--apply-uncertainty-labels",
                       str(path)] + learn
        return LaunchSpec("apply_labels", argv,
                          "Apply ruling — uncertainty labels")

    raise PortalError(f"unknown action: {action!r}")


def resume_spec(job):
    """A10: the stopped run's original argv + --resume. The
    resume flag is not part of the checkpoint flags hash (the
    CLI's _CHECKPOINT_FLAGS omits it), so the resumed run
    validates against the checkpoints the stopped run left."""
    argv = list(job.spec.argv)
    if "--resume" not in argv:
        argv += ["--resume"]
    return LaunchSpec(job.spec.action, argv,
                      f"Resume — {job.spec.label}",
                      file_path=job.spec.file_path,
                      work=job.spec.work)


# ---------------------------------------------------------------------------
# Run capture: ring buffer, stage tracker, run-events tailer
# ---------------------------------------------------------------------------

class RingBuffer:
    """A bounded line buffer with run-wide sequence numbers.
    The run page polls with lines_since(last seq); the SSE
    stream waits on the same condition. Bounded because a long
    run's log is evidence, not an archive — the child writes
    the durable record (preview artifacts) itself."""

    def __init__(self, maxlen=2000):
        self._maxlen = maxlen
        self._items = []  # [(seq, line)]
        self._seq = 0
        self._cond = threading.Condition()

    def append(self, line):
        with self._cond:
            self._seq += 1
            self._items.append((self._seq, line))
            del self._items[:-self._maxlen]
            self._cond.notify_all()
            return self._seq

    def since(self, seq):
        with self._cond:
            return [(s, l) for s, l in self._items if s > seq]

    def lines(self):
        with self._cond:
            return [l for _s, l in self._items]

    def wait_since(self, seq, timeout):
        with self._cond:
            if any(s > seq for s, _l in self._items):
                return self.since(seq)
            self._cond.wait(timeout)
            return self.since(seq)


# Stage names the runner itself reports, per §5.3: its printed
# anchors, not a portal-invented taxonomy. The table below is
# the whole mapping — a line that matches nothing still lands
# in the feed verbatim (the tracker keeps every line), it just
# moves no pill.
_PROCESSING_RE = re.compile(r"^Processing(?: \(v2\))?: (.+)$")
_INGEST_OUTCOME_RE = re.compile(r"^  (\S.*): (\S+) \[([0-9a-f?]{8})\] ")
_REEMBED_OUTCOME_RE = re.compile(r"^  \[([0-9a-f]{8})\] re-embed: (\S+)")
_BACKUP_DONE_RE = re.compile(r"^  Backup written to ")
_RUN_EVENTS_RE = re.compile(r"^  Run events: (.+)$")


class StageTracker:
    """Derives stage pills, the now-line, the results feed, and
    book progress from a job's stdout lines (§5.3). One tracker
    per job, seeded with the launch's known files where the
    action has them (ingest: one stage per picked file)."""

    def __init__(self, action, files=()):
        self.action = action
        self.stages = [{"name": "Startup", "state": "active"}]
        self.feed = []  # [{seq, text, source}] oldest first
        self.now = ""
        self.books_done = 0
        self.books_total = len(files) or None
        self.checkpointed = False
        self._book_stage = None
        for name in files:
            self.stages.append(
                {"name": f"Ingest — {name}", "state": "pending"})
        if action == "reembed":
            self.stages.append({"name": "Re-embed", "state": "active"})
        if action == "backup":
            self.stages.append({"name": "Backup", "state": "active"})
        self._seq = 0

    def _settle_startup(self):
        if self.stages[0]["state"] == "active":
            self.stages[0]["state"] = "done"

    def _finish_book_stage(self):
        if self._book_stage is not None:
            self._book_stage["state"] = "done"
            self._book_stage = None
            self.books_done += 1

    def feed_line(self, line):
        self._seq += 1
        self.feed.append({"seq": self._seq, "text": line,
                          "source": "stdout"})
        del self.feed[:-400]
        text = line.strip()
        if text:
            self.now = text
        m = _PROCESSING_RE.match(line)
        if m:
            self._settle_startup()
            self._finish_book_stage()
            stage = {"name": f"Book — {m.group(1).strip()}",
                     "state": "active"}
            self.stages.append(stage)
            self._book_stage = stage
            if self.books_total is None:
                seen = sum(1 for s in self.stages
                           if s["name"].startswith("Book — "))
                self.books_total = max(seen, self.books_done + 1)
            return
        if line.startswith("  Run complete:"):
            self._finish_book_stage()
            return
        if "Checkpoint saved" in line:
            self.checkpointed = True
            return
        m = _INGEST_OUTCOME_RE.match(line)
        if m and self.action == "ingest":
            self._settle_startup()
            for stage in self.stages:
                if stage["name"] == f"Ingest — {m.group(1)}":
                    stage["state"] = "done"
                    self.books_done += 1
                    break
            return
        m = _REEMBED_OUTCOME_RE.match(line)
        if m and self.action == "reembed":
            self._settle_startup()
            self.books_done += 1
            return
        if _BACKUP_DONE_RE.match(line) and self.action == "backup":
            self._settle_startup()
            for stage in self.stages:
                if stage["name"] == "Backup":
                    stage["state"] = "done"
            return

    def feed_event(self, text):
        """A feed entry derived from the run-events artifact
        (source='events') — the pair/verdict lines §5.3 lifts
        into the feed once S0 exists."""
        self._seq += 1
        self.feed.append({"seq": self._seq, "text": text,
                          "source": "events"})
        del self.feed[:-400]

    def close(self, state):
        """Settle any still-open stage when the job ends: done
        stays done; an open stage takes the job's end state
        (stopped reads stopped, anything else done/failed by
        the caller's word)."""
        for stage in self.stages:
            if stage["state"] in ("active", "pending"):
                stage["state"] = state
        self._book_stage = None

    def snapshot(self):
        return {
            "stages": [dict(s) for s in self.stages],
            "now": self.now,
            "feed": list(reversed(self.feed)),
            "books_done": self.books_done,
            "books_total": self.books_total,
            "checkpointed": self.checkpointed,
        }


class EventsTailer:
    """Incrementally reads one run's S0 event artifact. Only
    newline-terminated lines are consumed: after a crash the
    final line can be torn mid-write, and a torn line is not an
    error — it is read again once the writer (or nobody, after
    a crash) completes it. A line that still does not parse is
    skipped for the same reason: the tailer serves a web page,
    and a page must never wedge on its evidence file."""

    def __init__(self):
        self.path = None
        self._offset = 0
        self._events = []       # every parsed event, in order
        self._consumed = 0      # events handed out via new_events
        self._lock = threading.Lock()

    def attach(self, path):
        with self._lock:
            path = Path(path)
            if self.path == path:
                return
            self.path = path
            self._offset = 0
            self._events = []
            self._consumed = 0

    def poll(self):
        """Read any new complete lines; return the aggregates."""
        with self._lock:
            if self.path is not None:
                try:
                    size = self.path.stat().st_size
                except OSError:
                    size = 0
                if size > self._offset:
                    try:
                        with open(self.path, "rb") as fh:
                            fh.seek(self._offset)
                            raw = fh.read(size - self._offset)
                    except OSError:
                        raw = b""
                    cut = raw.rfind(b"\n")
                    if cut >= 0:
                        chunk = raw[:cut + 1]
                        self._offset += len(chunk)
                        for line in chunk.decode(
                                "utf-8", "replace").splitlines():
                            try:
                                ev = json.loads(line)
                            except ValueError:
                                continue
                            # Every consumer of this stream
                            # calls .get on its events, so a line
                            # that is valid JSON but not an object
                            # (a stray "42" in a hand-mangled
                            # artifact) is skipped here, at the
                            # one reader, rather than wedging
                            # each consumer downstream.
                            if isinstance(ev, dict):
                                self._events.append(ev)
            return self._aggregates()

    def _aggregates(self):
        agg = {"path": str(self.path) if self.path else None,
               "pairs": 0, "verdicts": 0, "impulses": 0,
               "decision": None, "book_decisions": {},
               "current_book": None, "finished": False,
               "interrupted": False, "books": 0}
        for ev in self._events:
            kind = ev.get("event")
            if kind == "pair":
                agg["pairs"] += 1
            elif kind == "verdict":
                agg["verdicts"] += 1
            elif kind == "impulse":
                agg["impulses"] += 1
            elif kind == "decision":
                agg["decision"] = {"used": ev.get("used"),
                                   "cap": ev.get("cap")}
            elif kind == "book_start":
                agg["current_book"] = ev.get("book")
            elif kind == "book_end":
                agg["book_decisions"][ev.get("book")] = \
                    ev.get("decision_calls")
            elif kind == "run_end":
                agg["finished"] = True
                agg["interrupted"] = bool(ev.get("interrupted"))
                agg["books"] = ev.get("books") or 0
        return agg

    def new_events(self):
        """Events not yet handed out (the job's feed consumer)."""
        with self._lock:
            out = self._events[self._consumed:]
            self._consumed = len(self._events)
            return list(out)

    def events(self):
        """Every parsed event so far, in stream order — the
        Matrices panel's raw material (S4). A copy: the panel
        reduces the stream, it does not share the tailer's
        list."""
        with self._lock:
            return list(self._events)


def _event_feed_text(ev):
    """One feed line for an artifact event (§5.3). Decision
    claims are ticker data, not feed lines — they arrive far too
    often to read; bookends and resolver outcomes are the feed."""
    kind = ev.get("event")
    if kind == "pair":
        return (f"pair: {ev.get('a')} × {ev.get('b')} scored "
                f"{ev.get('score')}")
    if kind == "verdict":
        text = (f"verdict: {ev.get('a')} × {ev.get('b')} → "
                f"{ev.get('verdict')} via {ev.get('via')}")
        if ev.get("score") is not None:
            text += f" ({ev['score']})"
        if ev.get("reason"):
            text += f" — {ev['reason']}"
        return text
    if kind == "impulse":
        if ev.get("fate") == "mapped":
            return (f"impulse: '{ev.get('label')}' → mapped "
                    f"({ev.get('score')})")
        return (f"impulse: '{ev.get('label')}' → unmapped "
                f"(best {ev.get('score')})")
    if kind == "book_start":
        return f"book start: {ev.get('book')}"
    if kind == "book_end":
        return (f"book end: {ev.get('book')} — "
                f"{ev.get('decision_calls')} decision call(s)")
    if kind == "run_end":
        return (f"run end: {ev.get('books')} book(s), failures "
                f"{ev.get('failures')}")
    return None


# ---------------------------------------------------------------------------
# The Matrices panel (spec §2.3, slice S4): the resolver's work,
# reduced from the S0 run-events artifact
# ---------------------------------------------------------------------------

# Below this cosine a cell paints shade only — no printed
# number (§2.3). It is a display rule, not a resolver threshold:
# the reduction keeps every true score, the panel declines to
# print the ones that would read as precision they are not.
_MATRIX_SHADE_FLOOR = 0.50


def _matrix_bars(ctx):
    """The bars the panel prints. The merge bar and the mapping
    bar are the resolver's own configured thresholds — the
    values a run launched now would use — falling back to the
    processor's defaults when config names neither. The shade
    floor is the spec's display rule above."""
    raw = _raw_config(ctx)

    def _bar(key, default):
        try:
            value = float(raw.get(key))
        except (TypeError, ValueError):
            return default
        return value if 0.0 <= value <= 1.0 else default

    return {"merge_bar": _bar("dedupe_threshold", 0.85),
            "map_bar": _bar("trope_map_threshold", 0.78),
            "shade_floor": _MATRIX_SHADE_FLOOR}


def reduce_matrix(events):
    """Reduce one run's event stream to the panel's grid state.

    Pure function of the stream: the live panel (a job's
    tailer) and the post-run reopening (the retained artifact
    read back) feed it the same events and get the identical
    grid — that identity is the reopening's whole claim.

    The freeze rule lives here, not in the renderer: a pair's
    first verdict settles its cell, and no later event — a
    re-score, even a conflicting verdict — changes a settled
    cell. The renderer can therefore repaint freely; what it
    is handed for a settled pair never moves.

    A resolver event with no book field belongs to the book
    bracket it arrived inside (the sink attributes by thread;
    the bracket is the stream's own record of that)."""
    books = {}
    order = []
    current = None
    decision = None
    finished = False
    interrupted = False

    def section(book):
        if book not in books:
            books[book] = {"book": book, "names": [], "cells": {},
                           "verdicts": [], "impulses": [],
                           "decision_calls": None}
            order.append(book)
        return books[book]

    for ev in events:
        kind = ev.get("event")
        if kind == "book_start":
            current = ev.get("book")
            if current is not None:
                section(current)
        elif kind == "book_end":
            book = ev.get("book") or current
            if book is not None:
                section(book)["decision_calls"] = \
                    ev.get("decision_calls")
        elif kind == "decision":
            decision = {"used": ev.get("used"),
                        "cap": ev.get("cap")}
        elif kind == "run_end":
            finished = True
            interrupted = bool(ev.get("interrupted"))
        elif kind in ("pair", "verdict", "impulse"):
            book = ev.get("book") or current
            if book is None:
                continue
            sec = section(book)
            if kind == "impulse":
                sec["impulses"].append({
                    "label": ev.get("label"),
                    "neighbours": ev.get("neighbours") or [],
                    "fate": ev.get("fate"),
                    "catalog_id": ev.get("catalog_id"),
                    "score": ev.get("score"),
                    "seq": ev.get("seq")})
                continue
            a, b = ev.get("a"), ev.get("b")
            if a is None or b is None:
                continue
            for name in (a, b):
                if name not in sec["names"]:
                    sec["names"].append(name)
            key = (a, b) if a <= b else (b, a)
            cell = sec["cells"].get(key)
            if kind == "pair":
                if cell is not None \
                        and cell["state"] == "settled":
                    continue  # frozen — see the docstring
                if cell is None:
                    cell = {"a": key[0], "b": key[1],
                            "score": None, "state": "scored",
                            "verdict": None, "via": None,
                            "reason": None, "seq": ev.get("seq")}
                    sec["cells"][key] = cell
                cell["score"] = ev.get("score")
                cell["seq"] = ev.get("seq")
            else:  # verdict
                if cell is not None \
                        and cell["state"] == "settled":
                    continue  # the first verdict is final
                if cell is None:
                    cell = {"a": key[0], "b": key[1],
                            "score": None, "state": "scored",
                            "verdict": None, "via": None,
                            "reason": None, "seq": ev.get("seq")}
                    sec["cells"][key] = cell
                if ev.get("score") is not None:
                    cell["score"] = ev.get("score")
                cell.update(
                    {"state": "settled",
                     "verdict": ev.get("verdict"),
                     "via": ev.get("via"),
                     "reason": ev.get("reason"),
                     "seq": ev.get("seq")})
                sec["verdicts"].append({
                    "a": cell["a"], "b": cell["b"],
                    "verdict": cell["verdict"],
                    "via": cell["via"], "score": cell["score"],
                    "reason": cell["reason"],
                    "seq": ev.get("seq")})
    out = []
    for book in order:
        sec = books[book]
        idx = {name: i for i, name in enumerate(sec["names"])}
        cells = sorted(
            sec["cells"].values(),
            key=lambda c: (idx[c["a"]], idx[c["b"]]))
        out.append({
            "book": book, "names": sec["names"], "cells": cells,
            "verdicts": sec["verdicts"],
            "impulses": sec["impulses"],
            "candidates": len(cells),
            "resolved": sum(1 for c in cells
                            if c["state"] == "settled"),
            "decision_calls": sec["decision_calls"]})
    return {"books": out, "decision": decision,
            "finished": finished, "interrupted": interrupted}


_ARTIFACT_MEMO = {}
# The memo exists so re-polls of a retained artifact cost a stat,
# not a re-read — but a long-lived portal would otherwise grow it
# by one entry per artifact ever opened. Entries are pure
# functions of immutable files, so a full clear on overflow is
# safe (the worst case is one re-read per artifact still being
# viewed) and simpler than eviction bookkeeping.
_ARTIFACT_MEMO_CAP = 512


def _artifact_events(path):
    """(events, aggregates) for one retained artifact, read
    through a throwaway tailer so a torn final line is tolerated
    exactly as the live path tolerates it. Memoised on the
    file's (size, mtime): a finished artifact is immutable, so
    the reopening page's polls are stat-only once read; a still
    growing one re-reads when its signature moves."""
    path = Path(path)
    try:
        st = path.stat()
        sig = (st.st_size, st.st_mtime_ns)
    except OSError:
        sig = None
    if sig is not None:
        hit = _ARTIFACT_MEMO.get(str(path))
        if hit is not None and hit[0] == sig:
            return hit[1], hit[2]
    tailer = EventsTailer()
    tailer.attach(path)
    agg = tailer.poll()
    events = tailer.events()
    if sig is not None:
        if str(path) not in _ARTIFACT_MEMO \
                and len(_ARTIFACT_MEMO) >= _ARTIFACT_MEMO_CAP:
            _ARTIFACT_MEMO.clear()
        _ARTIFACT_MEMO[str(path)] = (sig, events, agg)
    return events, agg


_MATRIX_SCAN_CACHE = {}


def latest_matrices(learn_dir, wanted):
    """For each wanted book (file) name, the newest retained
    artifact that actually scored it: {book: {run_id, path}}.

    "Actually scored" matters: an artifact that merely opened
    the book (book_start, no pair or verdict) holds no matrix
    to reopen. Artifacts are append-only and immutable once
    their run ends, so the scan memoises on the directory's
    (name, size, mtime) signature — Home polls this, and the
    steady state costs one listing, not a re-read of every
    run the operator has ever kept."""
    wanted = {b for b in wanted if b}
    if not wanted:
        return {}
    folder = Path(learn_dir) / "run-events"
    try:
        files = sorted(folder.glob("run-*.jsonl"))
    except OSError:
        files = []
    sig = []
    for path in files:
        try:
            st = path.stat()
            sig.append((path.name, st.st_size, st.st_mtime_ns))
        except OSError:
            continue
    sig = tuple(sig)
    key = (str(learn_dir), tuple(sorted(wanted)))
    hit = _MATRIX_SCAN_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return dict(hit[1])

    def _newest_first(path):
        try:
            return (path.stat().st_mtime_ns, path.name)
        except OSError:
            return (0, path.name)

    found = {}
    for path in sorted(files, key=_newest_first, reverse=True):
        if len(found) == len(wanted):
            break
        events, _agg = _artifact_events(path)
        scored = {ev.get("book") for ev in events
                  if ev.get("event") in ("pair", "verdict")
                  and ev.get("book")}
        for book in scored & (wanted - set(found)):
            found[book] = {"run_id": path.stem, "path": str(path)}
    _MATRIX_SCAN_CACHE[key] = (sig, dict(found))
    return found


# ---------------------------------------------------------------------------
# Jobs: the registry and the one-writer queue (spec §7)
# ---------------------------------------------------------------------------

def _stop_signal():
    """The graceful stop signal for a child. POSIX: SIGINT —
    the runner treats Ctrl+C as 'checkpoint and stop'. Windows
    has no per-process SIGINT from another process; CTRL_BREAK
    is its equivalent, deliverable because portal children are
    spawned in their own process group (see _popen_kwargs)."""
    if sys.platform == "win32":
        return signal.CTRL_BREAK_EVENT
    return signal.SIGINT


def _popen_kwargs(ctx):
    """How every portal child is spawned: stdout+stderr merged
    into one pipe (the run page's single stream; the CLI's own
    prints interleave the same way in a terminal), stdin closed
    (a child must never block on a prompt the operator cannot
    see), unbuffered UTF-8 output so lines arrive as they are
    printed and decode identically on any console codepage."""
    kwargs = {
        "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL, "text": True,
        "encoding": "utf-8", "errors": "replace", "bufsize": 1,
        "cwd": str(ctx.script_dir),
        "env": {**os.environ, "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8"},
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return kwargs


def _preview_artifacts(ctx):
    """Preview result files currently in preview/ (the run
    page links the ones a job created: snapshot at launch,
    diff at read time)."""
    folder = Path(ctx.script_dir) / "preview"
    try:
        return {p.name for p in folder.glob("*.json")
                if not p.name.endswith("-trope-map.json")}
    except OSError:
        return set()


def _checkpoint_book_key(file_path):
    """Mirror of the CLI's _checkpoint_book_key: the file stem
    made directory-safe. The portal only needs to know whether
    a stopped book's checkpoints exist (Resume's gate), and
    this is the directory they live under."""
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_",
                  Path(file_path).stem).strip("_")
    return safe or "book"


def checkpoints_exist(script_dir, file_path):
    folder = (Path(script_dir) / "preview" / ".checkpoints"
              / _checkpoint_book_key(file_path))
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return False


class Job:
    """One launched child and everything the run page shows
    about it. Lifecycle: queued -> running -> done | failed |
    stopped. A job's stdout is consumed by a reader thread into
    the ring buffer + stage tracker; a waiter thread owns the
    exit (and the stop escalation, §6 A9: signal, then
    terminate, then kill — each only if the child ignores the
    gentler step)."""

    def __init__(self, registry, job_id, spec):
        self.registry = registry
        self.ctx = registry.ctx
        self.id = job_id
        self.spec = spec
        self.action = spec.action
        self.label = spec.label
        self.argv = list(spec.argv)
        self.state = "queued"
        self.exit_code = None
        self.started = None
        self.finished = None
        self.proc = None
        self.buffer = RingBuffer()
        files = []
        if spec.action == "ingest" and spec.file_path is not None:
            files = [spec.file_path.name]
        self.tracker = StageTracker(spec.action, files=files)
        self.tailer = EventsTailer()
        self.stop_requested = False
        self._stop_at = None
        self._terminated = False
        self._killed = False
        self._resume_claimed = False
        self._preview_before = set()
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------

    def _start(self):
        with self._lock:
            self.state = "running"
            self.started = time.time()
        self._preview_before = _preview_artifacts(self.ctx)
        try:
            self.proc = self.registry._popen(
                self.argv, **_popen_kwargs(self.ctx))
        except Exception as e:
            # A child that cannot even spawn is a failed job with
            # the reason in its feed, never a dead portal request.
            self._note_line(
                f"portal: could not launch ({type(e).__name__}: {e})")
            self._finish(None)
            self.registry._job_finished(self)
            return
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._waiter, daemon=True).start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self._note_line(line.rstrip("\r\n"))
        except Exception:
            pass

    def _waiter(self):
        grace = self.registry.stop_grace
        while True:
            rc = self.proc.poll()
            if rc is not None:
                break
            if self.stop_requested and self._stop_at is not None:
                waited = time.time() - self._stop_at
                if waited > grace and not self._terminated:
                    self._terminated = True
                    try:
                        self.proc.terminate()
                    except Exception:
                        pass
                if waited > grace * 4 and not self._killed:
                    self._killed = True
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            self._refresh_events()
            time.sleep(0.05)
        self._refresh_events()
        try:
            if self.proc.stdout is not None:
                self.proc.stdout.close()
        except Exception:
            pass
        self._finish(rc)
        self.registry._job_finished(self)

    def _finish(self, rc):
        with self._lock:
            self.exit_code = rc
            self.finished = time.time()
            if self.stop_requested:
                self.state = "stopped"
            elif rc == 0:
                self.state = "done"
            else:
                self.state = "failed"
            state = self.state
        self.tracker.close(state)

    def stop(self):
        """A9: signal the child (the runner checkpoints on
        Ctrl+C by design). A queued job is simply never started;
        a finished job ignores the request."""
        proc = None
        with self._lock:
            if self.state == "queued":
                self.state = "stopped"
                self.finished = time.time()
                self.tracker.close("stopped")
                return True
            if self.state != "running":
                return False
            self.stop_requested = True
            self._stop_at = time.time()
            proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(_stop_signal())
            except Exception:
                pass
        return True

    # -- evidence -------------------------------------------------

    def _note_line(self, line):
        self.buffer.append(line)
        self.tracker.feed_line(line)
        m = _RUN_EVENTS_RE.match(line)
        if m:
            # The child announces its artifact path (S0) — the
            # authoritative association between job and file.
            self.tailer.attach(Path(m.group(1).strip()))

    def _find_artifact(self):
        """Fallback association when the announcement line was
        missed: the newest artifact in the learn dir's
        run-events/ written since this job started. Sound
        because the one-writer slot means only one flagged run
        can be producing artifacts at a time."""
        folder = Path(self.ctx.learn_dir) / "run-events"
        try:
            candidates = [p for p in folder.glob("run-*.jsonl")
                          if p.stat().st_mtime
                          >= (self.started or 0)]
        except OSError:
            return
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_mtime)
            self.tailer.attach(candidates[-1])

    def _refresh_events(self):
        if self.spec.is_pipeline:
            if self.tailer.path is None and self.started is not None:
                self._find_artifact()
            self.tailer.poll()
            for ev in self.tailer.new_events():
                text = _event_feed_text(ev)
                if text:
                    self.tracker.feed_event(text)
        return self.tailer.poll()

    def _can_resume(self):
        if self.action not in ("preview", "write"):
            return False
        if self.state not in ("stopped", "failed"):
            return False
        if self.spec.file_path is None:
            return False
        return checkpoints_exist(self.ctx.script_dir,
                                 self.spec.file_path)

    def claim_resume(self):
        """Claim this job's single resume, atomically. The
        check and the claim are one lock-held step: two
        concurrent resume requests for the same job must not
        both launch (for a write job the second would
        republish with no fresh confirm). A job whose resume
        was claimed is resumed through the NEW job it created,
        which carries its own claim."""
        with self._lock:
            if self._resume_claimed:
                return False
            if not self._can_resume():
                return False
            self._resume_claimed = True
            return True

    def snapshot(self):
        agg = self._refresh_events()
        with self._lock:
            state, exit_code = self.state, self.exit_code
            started, finished = self.started, self.finished
        track = self.tracker.snapshot()
        snap = {
            "id": self.id, "action": self.action,
            "label": self.label, "argv": list(self.argv),
            "state": state, "exit_code": exit_code,
            "started": started, "finished": finished,
            "elapsed": ((finished or time.time()) - started
                        if started else 0),
            "stages": track["stages"], "now": track["now"],
            "feed": track["feed"],
            "books_done": track["books_done"],
            "books_total": track["books_total"],
            # The tracker's flag comes from the child's own
            # "Checkpoint saved" line — but a child stopped
            # mid-call may never print it even though its
            # per-chapter checkpoints are on disk (they are
            # written as chapters complete, before any shutdown
            # summary). The run page must not claim "no
            # checkpoints" over files that Resume would use, so
            # the filesystem is the second source of truth.
            "checkpointed": track["checkpointed"] or (
                self.spec.file_path is not None
                and checkpoints_exist(self.ctx.script_dir,
                                      self.spec.file_path)),
            "log": self.buffer.lines()[-400:],
            "events": agg,
            "can_stop": state in ("queued", "running"),
            "can_resume": self._can_resume(),
            "artifacts": sorted(
                _preview_artifacts(self.ctx) - self._preview_before),
            "backup": None,
            "queued_behind": None,
        }
        if self.action == "backup" and state in (
                "done", "failed", "stopped"):
            snap["backup"] = backup_info(self.ctx.backup_dir)
        if state == "queued":
            running = self.registry.running_job()
            if running is not None and running is not self:
                snap["queued_behind"] = running.label
        return snap

    def matrix(self):
        """The Matrices panel's payload for this job (S4): the
        tailer's aggregates — the same object the snapshot
        carries, so the panel and the decision ticker read one
        computation and cannot disagree — plus the grid
        reduction over the tailer's retained stream. The
        tailer stays the panel's only consumer of the artifact;
        the reduction is a second view of its events, never a
        second reader of the file."""
        agg = self._refresh_events()
        return {"aggregates": agg,
                "matrix": reduce_matrix(self.tailer.events()),
                "run_id": (self.tailer.path.stem
                           if self.tailer.path is not None
                           else None)}

    def brief(self):
        """The running-now strip's view of this job (§5.1)."""
        agg = self.tailer.poll() if self.spec.is_pipeline else None
        return {
            "id": self.id, "label": self.label,
            "state": self.state,
            "now": self.tracker.now,
            "decision": (agg or {}).get("decision"),
        }


# ---------------------------------------------------------------------------
# External runs: standalone processor runs observed live (§5.1)
# ---------------------------------------------------------------------------

# A standalone run is discoverable while it could still be news:
# its artifact changed within this window, or its writer is
# provably alive. Anything older and silent is history — Recent
# runs and the retained-artifact pages already cover it, and
# adopting every artifact ever written would bury the strip.
_EXTERNAL_DISCOVERY_SECONDS = 900

# Extraction can sit silent inside one book for many minutes —
# the artifact only speaks at book boundaries and during the
# reduce — so a live run is not declared dead on a short quiet
# spell. The writer's pid (below) is the stronger signal; this
# window is the fallback for when the pid cannot be checked.
_EXTERNAL_SILENCE_SECONDS = 1800


def _path_key(path):
    """The comparison key for artifact paths: two spellings of
    one file (the child's announced path, the portal's glob of
    the same folder) must claim once, never twice."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def _artifact_pid(path):
    """The writer's pid, from the artifact's pinned name
    (run-<YYYYMMDD-HHMMSS>-<pid>.jsonl, plus an optional numeric
    collision suffix). None when the name does not carry one —
    the name is the processor's own pin, so a foreign file in
    the folder simply has no writer to check."""
    parts = Path(path).stem.split("-")
    # isdigit() alone would wave through Unicode digits (a
    # superscript two in a crafted filename) that int() then
    # rejects — the pid is an ASCII field or it is not a pid.
    if len(parts) >= 4 and parts[3].isascii() \
            and parts[3].isdigit():
        return int(parts[3])
    return None


def _pid_alive(pid):
    """True/False when the platform can answer, None when it
    cannot — a watcher must never declare a run dead on a
    failed guess, so 'cannot tell' falls back to the file's
    mtime rather than to a verdict."""
    if not pid or pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            # PROCESS_QUERY_LIMITED_INFORMATION: enough to ask
            # whether the process exists, never to touch it.
            handle = ctypes.windll.kernel32.OpenProcess(
                0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            # ERROR_INVALID_PARAMETER is OpenProcess's answer
            # for a pid that does not exist; anything else
            # (notably access denied) means it does.
            err = ctypes.get_last_error()
            if err == 87:
                return False
            if err == 5:
                return True
            return None
        except Exception:
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


class ExternalRun:
    """A processor run the portal did not launch — started in a
    terminal with --emit-run-events — observed through its
    run-events artifact. Duck-typed to the Job surface the
    handlers and the run page consume (snapshot / brief /
    matrix / stop / claim_resume), because the page must not
    care which side of the glass started the run.

    The differences are stated, not hidden: there is no argv
    (the portal never saw the command), no captured terminal
    output (the log says so), and the run cannot be stopped or
    resumed from here — those verbs belong to the terminal
    that owns the process."""

    def __init__(self, ctx, path):
        self.ctx = ctx
        self.id = None  # the registry assigns it on adoption
        self.action = "external"
        self.argv = []
        self.path = Path(path)
        self.run_id = self.path.stem
        self.tailer = EventsTailer()
        self.tailer.attach(self.path)
        self.state = "running"
        self.exit_code = None
        self.started = None
        self.finished = None
        self.label = f"External run — {self.run_id}"
        self._preview = False
        self._dry_run = False
        self._failures = 0
        self._ended = False
        self._stages = []
        self._book_stage = None
        self._feed = []
        self._seq = 0
        self._now = ""
        self._books_done = 0
        self._books_total = None
        self._preview_before = _preview_artifacts(ctx)
        self._lock = threading.Lock()

    # -- evidence -------------------------------------------------

    def _settle_book_stage(self, state="done"):
        if self._book_stage is not None:
            self._book_stage["state"] = state
            self._book_stage = None

    def _close_stages(self, state):
        self._settle_book_stage(state)
        for stage in self._stages:
            if stage["state"] in ("active", "pending"):
                stage["state"] = state

    def _absorb(self, ev):
        """Fold one artifact event into stages, feed, and the
        label — the same reductions the launched-job path makes
        from the same stream (bookends open and close stages;
        every event's feed text comes from _event_feed_text,
        so the two paths cannot drift in wording)."""
        kind = ev.get("event")
        ts = ev.get("ts")
        if isinstance(ts, (int, float)):
            if self.started is None:
                self.started = ts
        text = _event_feed_text(ev)
        if text:
            self._seq += 1
            self._feed.append({"seq": self._seq, "text": text,
                               "source": "events"})
            del self._feed[:-400]
            self._now = text
        if kind == "run_start":
            self._preview = bool(ev.get("preview"))
            self._dry_run = bool(ev.get("dry_run"))
        elif kind == "book_start":
            self._settle_book_stage()
            stage = {"name": f"Book — {ev.get('book')}",
                     "state": "active"}
            self._stages.append(stage)
            self._book_stage = stage
            book = ev.get("book") or self.run_id
            label = f"External run — {book}"
            if self._preview:
                label += " (preview)"
            elif self._dry_run:
                label += " (dry run)"
            self.label = label
        elif kind == "book_end":
            self._settle_book_stage()
            self._books_done += 1
        elif kind == "run_end":
            self._ended = True
            self._failures = ev.get("failures") or 0
            if isinstance(ts, (int, float)):
                self.finished = ts
            total = ev.get("books")
            if isinstance(total, int) and total:
                self._books_total = total

    def _writer_live(self):
        """Whether the run is still producing: its writer's
        process exists, or the artifact moved within the
        silence window. A book mid-extraction is quiet, not
        dead — the pid is consulted first for exactly that
        reason."""
        if _pid_alive(_artifact_pid(self.path)):
            return True
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        return (time.time() - mtime) <= _EXTERNAL_SILENCE_SECONDS

    def refresh(self):
        """Read whatever the writer has appended since the last
        look and settle the run's state from the evidence:
        run_end is terminal (done / failed / interrupted by its
        own flags); no run_end and a live writer is running;
        no run_end and silence is a run the portal can no
        longer vouch for — interrupted, never tailed as running
        forever. The stale call is recomputed each refresh, so
        a run that speaks again after a long quiet book returns
        to running on its own evidence."""
        agg = self.tailer.poll()
        with self._lock:
            for ev in self.tailer.new_events():
                self._absorb(ev)
            if self._ended:
                if agg["interrupted"]:
                    self.state = "interrupted"
                    self._close_stages("stopped")
                elif self._failures:
                    self.state = "failed"
                    self._close_stages("failed")
                else:
                    self.state = "done"
                    self._close_stages("done")
            elif self._writer_live():
                self.state = "running"
            else:
                self.state = "interrupted"
                self._close_stages("stopped")
                if self.finished is None:
                    self.finished = self._last_event_ts()
        return agg

    def _last_event_ts(self):
        events = self.tailer.events()
        for ev in reversed(events):
            ts = ev.get("ts")
            if isinstance(ts, (int, float)):
                return ts
        return self.started

    # -- the Job-shaped surface -----------------------------------

    def snapshot(self):
        agg = self.refresh()
        with self._lock:
            state, started = self.state, self.started
            finished = self.finished
            stages = [dict(s) for s in self._stages]
            feed = list(reversed(self._feed))
            now = self._now
            books_done, books_total = (self._books_done,
                                       self._books_total)
        return {
            "id": self.id, "action": self.action,
            "label": self.label, "argv": list(self.argv),
            "state": state, "exit_code": None,
            "started": started, "finished": finished,
            "elapsed": ((finished or time.time()) - started
                        if started else 0),
            "stages": stages, "now": now, "feed": feed,
            "books_done": books_done, "books_total": books_total,
            "checkpointed": False,
            "log": ["portal: standalone run — its terminal "
                    "output belongs to the terminal that "
                    "launched it; this page is reduced from "
                    "its run-events artifact."],
            "events": agg,
            "can_stop": False, "can_resume": False,
            "artifacts": sorted(
                _preview_artifacts(self.ctx) - self._preview_before),
            "backup": None, "queued_behind": None,
        }

    def matrix(self):
        """The Matrices panel payload, same shape and same
        single-reader discipline as a launched job's: the
        reduction runs over this run's own tailer stream."""
        agg = self.refresh()
        return {"aggregates": agg,
                "matrix": reduce_matrix(self.tailer.events()),
                "run_id": self.path.stem}

    def brief(self):
        agg = self.refresh()
        return {
            "id": self.id, "label": self.label,
            "state": self.state, "now": self._now,
            "decision": agg.get("decision"),
        }

    def stop(self):
        """Not ours to stop: the verb belongs to the terminal
        that owns the process. Returning False leaves the
        caller's state report truthful — nothing happened."""
        return False

    def claim_resume(self):
        """Resume re-runs a launched argv with checkpoints the
        portal can point at; a standalone run offers neither."""
        return False


class ExternalRunWatcher:
    """Adopts standalone runs' artifacts for the portal.

    Association is by claim, in one direction only: an artifact
    a portal job's tailer has attached to belongs to that job
    (its announcement line is authoritative, its fallback scan
    next), and is never adopted — and if a job attaches to an
    artifact already adopted here, the adoption is handed over,
    so a run is never presented twice. Everything unclaimed is
    a candidate: adopted while it is fresh or its writer lives,
    so a portal that starts mid-run still picks the run up."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._runs = {}  # path key -> ExternalRun
        self._lock = threading.Lock()

    def poll(self):
        registry = self.ctx.registry
        claimed = registry.claimed_artifacts()
        folder = Path(self.ctx.learn_dir) / "run-events"
        with self._lock:
            for key, run in list(self._runs.items()):
                if key in claimed:
                    # The job's claim wins, retroactively: its
                    # fallback scan can attach after we adopted,
                    # and the run must live in exactly one place.
                    registry.unregister_external(run)
                    del self._runs[key]
            try:
                paths = sorted(folder.glob("run-*.jsonl"))
            except OSError:
                paths = []
            now = time.time()
            for path in paths:
                key = _path_key(path)
                if key in claimed or key in self._runs:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                fresh = (now - st.st_mtime) \
                    <= _EXTERNAL_DISCOVERY_SECONDS
                if not fresh and not _pid_alive(_artifact_pid(path)):
                    # Old and silent: a finished run's preview
                    # and matrix already present it; an
                    # interrupted one is aged out, not adopted
                    # as a permanent ghost.
                    continue
                run = ExternalRun(self.ctx, path)
                run.refresh()
                registry.register_external(run)
                self._runs[key] = run
            for run in list(self._runs.values()):
                run.refresh()


class JobRegistry:
    """Every job the portal has launched, and the one-writer
    slot (§7): writer-class jobs run one at a time, in launch
    order; launching while one runs queues visibly. Read-only
    work never enters the registry — it is not a job. The
    registry is in-memory only: a portal restart orphans its
    children to the terminal's care, exactly as §7 states."""

    def __init__(self, ctx, popen_factory=None, stop_grace=5.0):
        self.ctx = ctx
        self._popen = popen_factory or subprocess.Popen
        self.stop_grace = stop_grace
        self._lock = threading.Lock()
        self._jobs = {}
        self._external = {}
        self._queue = []
        self._next_id = 1
        self._running_id = None

    def launch(self, spec):
        with self._lock:
            job = Job(self, self._next_id, spec)
            self._next_id += 1
            self._jobs[job.id] = job
            self._queue.append(job.id)
        self._pump()
        return job

    def _pump(self):
        with self._lock:
            if self._running_id is not None:
                return
            job = None
            for job_id in list(self._queue):
                candidate = self._jobs[job_id]
                if candidate.state == "queued":
                    self._queue.remove(job_id)
                    self._running_id = job_id
                    job = candidate
                    break
                self._queue.remove(job_id)
            if job is None:
                return
        job._start()

    def _job_finished(self, job):
        with self._lock:
            if self._running_id == job.id:
                self._running_id = None
        self._pump()

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                job = self._external.get(job_id)
            return job

    def jobs(self):
        with self._lock:
            all_jobs = list(self._jobs.values()) \
                + list(self._external.values())
        return sorted(all_jobs, key=lambda j: j.id)

    def running_job(self):
        with self._lock:
            if self._running_id is None:
                return None
            return self._jobs.get(self._running_id)

    def briefs(self):
        return [job.brief() for job in self.jobs()
                if job.state in ("queued", "running")]

    # -- external runs (standalone, observed — never queued) ----

    def register_external(self, run):
        """Adopt a standalone run into the id space. Ids come
        from the same counter as launched jobs so every surface
        that resolves an id (/run/<id>, /api/jobs/<id>) finds
        exactly one run per id, whichever side started it.
        Adopted runs never enter the launch queue: the
        one-writer slot governs children the portal spawns,
        not processes it merely observes."""
        with self._lock:
            run.id = self._next_id
            self._next_id += 1
            self._external[run.id] = run
        return run

    def unregister_external(self, run):
        with self._lock:
            self._external.pop(run.id, None)

    def claimed_artifacts(self):
        """The artifact paths this portal's own jobs own, as
        comparison keys. A job's tailer path is set by its
        child's announcement (authoritative) or its fallback
        scan; either way the artifact is spoken for, and the
        external watcher must not adopt it."""
        with self._lock:
            jobs = list(self._jobs.values())
        keys = set()
        for job in jobs:
            if job.tailer.path is not None:
                keys.add(_path_key(job.tailer.path))
        return keys


# ---------------------------------------------------------------------------
# Covers for the ingest picker (Q10)
# ---------------------------------------------------------------------------

_COVER_MAX_BYTES = 16 * 1024 * 1024
_COVER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif",
                ".webp": "image/webp"}
_COVER_EXTS = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/gif": ".gif", "image/webp": ".webp"}


def _zip_entry_text(z, name, cap):
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > cap:
        return None
    return z.read(name).decode("utf-8", "replace")


def _extract_cover(path):
    """The EPUB's declared cover image bytes + type, or None.

    Portal-side extraction (Q10): a file read of the operator's
    own book, not pipeline logic — the processor never opens
    the cover, the picker shows it. Both declarations are
    honoured: EPUB3's properties="cover-image" manifest item
    and EPUB2's <meta name="cover"> pointer. Sizes are capped
    before reading so a hostile archive cannot make the picker
    slurp an unbounded entry."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            container = _zip_entry_text(
                z, "META-INF/container.xml", 1 << 20)
            if container is None:
                return None
            opf_path = None
            for el in ET.fromstring(container).iter():
                if el.tag.rsplit("}", 1)[-1] == "rootfile":
                    opf_path = el.get("full-path")
                    break
            if not opf_path or opf_path not in names:
                return None
            opf_text = _zip_entry_text(z, opf_path, 5 << 20)
            if opf_text is None:
                return None
            items = {}
            cover_id = None
            for el in ET.fromstring(opf_text).iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "item":
                    items[el.get("id")] = (
                        el.get("href"), el.get("media-type") or "",
                        el.get("properties") or "")
                elif tag == "meta" and el.get("name") == "cover":
                    cover_id = el.get("content")
            chosen = None
            if cover_id and cover_id in items:
                chosen = items[cover_id]
            else:
                for item in items.values():
                    if "cover-image" in item[2]:
                        chosen = item
                        break
            if chosen is None:
                return None
            href, media_type, _props = chosen
            import posixpath
            entry = posixpath.normpath(posixpath.join(
                posixpath.dirname(opf_path), href))
            if entry not in names:
                return None
            if z.getinfo(entry).file_size > _COVER_MAX_BYTES:
                return None
            data = z.read(entry)
            # The served type comes from a raster allowlist,
            # never verbatim from book metadata: a book that
            # declares its cover image/svg+xml would otherwise
            # get active SVG served from the portal's own
            # origin, where its script could reach the portal
            # API. Anything unlisted falls back to the
            # extension's type, or to no cover at all.
            if media_type in _COVER_EXTS:
                ctype = media_type
            else:
                ctype = _COVER_TYPES.get(Path(entry).suffix.lower())
            if ctype is None:
                return None
            return data, ctype
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError):
        return None


def epub_cover(path, cache_dir):
    """(bytes, content_type) for a book's cover, or None. The
    cache lives in the portal's own directory (Q10) — never in
    the learned dir, never in the stores — keyed by path +
    mtime + size so a replaced book file re-extracts."""
    path = Path(path)
    cache_dir = Path(cache_dir)
    try:
        st = path.stat()
    except OSError:
        return None
    key = hashlib.sha1(
        f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}".encode(
            "utf-8")).hexdigest()
    if cache_dir.is_dir():
        for hit in cache_dir.glob(key + ".*"):
            if hit.suffix.lower() in _COVER_TYPES:
                try:
                    return hit.read_bytes(), \
                        _COVER_TYPES[hit.suffix.lower()]
                except OSError:
                    break
    found = _extract_cover(path)
    if found is None:
        return None
    data, ctype = found
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_dir / (key + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache_dir / (key + _COVER_EXTS.get(ctype, ".jpg")))
    except OSError:
        pass
    return data, ctype


# ---------------------------------------------------------------------------
# Rendering — server-side, one renderer; the page polls and swaps <main>
# ---------------------------------------------------------------------------

def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_bytes(n):
    if n is None:
        return "—"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" \
                else f"{value:.1f} {unit}"
        value /= 1024


def _fmt_when(when):
    if not when:
        return "—"
    return str(when).replace("T", " ")[:16]


def _chip(status):
    cls = re.sub(r"[^a-z_]", "", str(status or "").lower()) or "unknown"
    return f'<span class="chip chip-{cls}">{_esc(status)}</span>'


def _render_home(home):
    h = home["health"]
    cards = home["cards"]
    out = []
    out.append("<header class='strip health'>")
    gen = h["generation"]
    if gen:
        canary = h["canary"] or "unknown"
        canary_cls = "warn" if canary == "suspect" else "ok"
        out.append(
            f"<div><span class='k'>Vector generation</span>"
            f"<span class='v'>{_esc(gen['model'])} / "
            f"{_esc(gen['format'])} · dim {_esc(gen['dim'])}</span></div>")
        out.append(
            f"<div><span class='k'>Drift canary</span>"
            f"<span class='v {canary_cls}'>{_esc(canary)}</span></div>")
    else:
        out.append(
            "<div><span class='k'>Vector generation</span>"
            "<span class='v'>none born yet</span></div>")
    parser = h["parser"]
    stale_note = (f" · {parser['stale_works']} stale"
                  if parser["stale_works"] else "")
    out.append(
        f"<div><span class='k'>Parser</span><span class='v'>"
        f"v{_esc(parser['current'])}{stale_note}</span></div>")
    corpus_bytes = h["stores"]["corpus"]
    if corpus_bytes:
        out.append(
            f"<div><span class='k'>corpus.db</span><span class='v'>"
            f"{_fmt_bytes(corpus_bytes['total'])} "
            f"(text {_fmt_bytes(corpus_bytes['text'])}, "
            f"vectors {_fmt_bytes(corpus_bytes['vectors'])}, "
            f"FTS {_fmt_bytes(corpus_bytes['fts'])})</span></div>")
    else:
        out.append(
            "<div><span class='k'>corpus.db</span>"
            "<span class='v'>absent — no books ingested yet</span></div>")
    if h["stores"]["state_bytes"] is not None:
        out.append(
            f"<div><span class='k'>state.db</span><span class='v'>"
            f"{_fmt_bytes(h['stores']['state_bytes'])}</span></div>")
    backup = h["backup"]
    if backup["last_backup"]:
        out.append(
            f"<div><span class='k'>Last backup</span><span class='v'>"
            f"{_fmt_when(backup['last_backup'])} "
            f"({', '.join(backup['stores'])})</span></div>")
    elif backup["configured"]:
        out.append(
            "<div><span class='k'>Last backup</span>"
            "<span class='v'>none recorded</span></div>")
    else:
        out.append(
            "<div><span class='k'>Last backup</span>"
            "<span class='v'>backup directory not configured</span>"
            "</div>")
    setup = h["setup"]
    if setup:
        bits = []
        if setup.get("llm"):
            bits.append(f"extraction {_esc(setup['llm'])}")
        if setup.get("decision_model_provider"):
            bits.append(
                f"decisions {_esc(setup['decision_model_provider'])}")
        if setup.get("embed_model"):
            bits.append(f"embed {_esc(setup['embed_model'])}")
        if bits:
            out.append(
                f"<div><span class='k'>Setup</span>"
                f"<span class='v'>{' · '.join(bits)}</span></div>")
    if h["fts_available"] is False:
        out.append(
            "<div><span class='k'>FTS</span><span class='v warn'>"
            "unavailable on this Python's SQLite — text search "
            "skipped</span></div>")
    if home["busy"]:
        out.append(
            f"<div><span class='k'>Store</span><span class='v warn'>"
            f"{', '.join(home['busy'])} busy — retrying on the "
            f"next refresh</span></div>")
    out.append("</header>")

    strip = home["strip"]
    out.append("<section class='strip running'>")
    out.append("<h2>Running now</h2>")
    if strip["jobs"]:
        for job in strip["jobs"]:
            now = f" — {_esc(job['now'])}" if job.get("now") else ""
            out.append(
                f"<p><a href='/run/{_esc(job['id'])}'>"
                f"{_esc(job['label'])}</a> "
                f"<span class='chip'>{_esc(job['state'])}</span>"
                f"{now}</p>")
            if job.get("decision"):
                out.append(
                    f"<p class='muted'>Decision calls: "
                    f"{_esc(job['decision'].get('used'))} / "
                    f"{_esc(job['decision'].get('cap'))} — live "
                    f"from the run's event artifact.</p>")
    else:
        out.append("<p class='muted'>Nothing running under the "
                   "portal.</p>")
        out.append(
            f"<p class='muted'>Decision-call budget: "
            f"{strip['decision_cap']}–{strip['decision_ceiling']} "
            f"per book, scaled by "
            f"chapter count — live spend shows here while a "
            f"portal-launched run is active."
            f"</p>")
    out.append(f"<p class='note'>{_esc(strip['note'])}</p>")
    out.append("</section>")

    by_status = cards["works_by_status"]
    status_bits = " · ".join(
        f"{_esc(k)} {v}" for k, v in sorted(by_status.items()))
    quotes_bits = " · ".join(
        f"{_esc(k)} {v}"
        for k, v in sorted(cards["quotes_by_status"].items()))
    out.append("<section class='cards'>")
    out.append(
        f"<div class='card'><span class='n'>"
        f"{cards['works_complete']}</span><span class='t'>works "
        f"fully ingested</span><span class='s'>{status_bits or '—'}"
        f"</span></div>")
    out.append(
        f"<div class='card'><span class='n'>"
        f"{cards['endings_assessed']}</span><span class='t'>"
        f"endings assessed</span><span class='s'>corpus envelope "
        f"sections</span></div>")
    out.append(
        f"<div class='card'><span class='n'>"
        f"{cards['trigger_depth']}</span><span class='t'>trigger "
        f"depth present</span><span class='s'>corpus envelope "
        f"sections</span></div>")
    out.append(
        f"<div class='card'><span class='n'>"
        f"{cards['quotes_verified']}</span><span class='t'>"
        f"verified quotes on file</span><span class='s'>"
        f"{quotes_bits or '—'}</span></div>")
    out.append("</section>")

    out.append("<section><h2>Books</h2>")
    if home["works"]:
        out.append("<table><thead><tr><th>Title</th><th>Author</th>"
                   "<th>Series</th><th>State</th><th>Parser</th>"
                   "<th>Chapters</th><th>Passages</th><th>Vectors</th>"
                   "<th>Last activity</th><th></th></tr></thead>"
                   "<tbody>")
        for w in home["works"]:
            cov = ("n/a" if w["vector_coverage"] is None
                   else f"{w['vector_coverage'] * 100:.0f}%")
            parser_cell = _esc(w["parser_version"]) + (
                " (stale)" if w.get("stale_parser") else "")
            activity = w.get("last_activity") or {}
            activity_cell = (
                f"{_esc(activity.get('label', '—'))} · "
                f"{_fmt_when(activity.get('when'))}"
                if activity else "—")
            # A3's row entry point: offered exactly when the
            # vectors are behind — coverage short of 100%, or a
            # status that says embedding never finished.
            reembed = ""
            if (w["vector_coverage"] is not None
                    and w["vector_coverage"] < 1.0) \
                    or w["status"] in ("vectors_partial",
                                       "text_complete"):
                reembed = (
                    f"<button type='button' class='rowact' "
                    f"data-reembed='{_esc(w['fingerprint'])}'>"
                    f"Re-embed</button>")
            # S4's row entry point: offered exactly when a
            # retained artifact scored this work's book. The
            # link is navigation to a read-only page; wireRows
            # stops its click bubbling into the row's forensics
            # handler.
            matrix_link = ""
            if w.get("matrix_run"):
                matrix_link = (
                    f"<a class='rowact' data-mxlink "
                    f"href='/matrix/{_esc(w['fingerprint'])}'>"
                    f"Last run's matrix</a>")
            actions = " ".join(
                part for part in (reembed, matrix_link) if part)
            out.append(
                f"<tr data-fp='{_esc(w['fingerprint'])}'>"
                f"<td>{_esc(w['title'])}</td>"
                f"<td>{_esc(w['author'])}</td>"
                f"<td>{_esc(w['series_label'] or '')}</td>"
                f"<td>{_chip(w['status'])}</td>"
                f"<td>{parser_cell}</td>"
                f"<td>{_esc(w['chapters'])}</td>"
                f"<td>{_esc(w['passages'])}</td>"
                f"<td>{cov}</td><td>{activity_cell}</td>"
                f"<td>{actions}</td></tr>")
        out.append("</tbody></table>")
        out.append("<p class='note'>Select a book row for its "
                   "forensics report (--explain).</p>")
        out.append("<pre id='explain' hidden></pre>")
    else:
        out.append("<p class='muted'>No works in the corpus yet. "
                   "Ingest from the <a href='/ingest'>picker</a>.</p>")
    out.append("</section>")

    out.append("<section><h2>Recent runs</h2>")
    if home["recent_runs"]:
        out.append("<table><thead><tr><th>When</th><th>Kind</th>"
                   "<th>Work</th><th>Outcome</th></tr></thead>"
                   "<tbody>")
        for r in home["recent_runs"]:
            out.append(
                f"<tr><td>{_fmt_when(r['when'])}</td>"
                f"<td>{_esc(r['kind'])}</td>"
                f"<td>{_esc(r['work'])}</td>"
                f"<td>{_esc(r['outcome'])}"
                f"{(' — ' + _esc(r['detail'])) if r['detail'] else ''}"
                f"</td></tr>")
        out.append("</tbody></table>")
    else:
        out.append("<p class='muted'>No runs recorded yet.</p>")
    out.append("</section>")

    out.append("<section><h2>Queue</h2>")
    if home["queues"] is not None:
        q = home["queues"]
        out.append(
            f"<p><a href='/desk'>Today's trope batch: "
            f"{q['batch']}</a> · "
            f"<a href='/desk'>Parked escalations: {q['parked']}</a>"
            f"</p>")
    else:
        out.append("<p class='muted'>state.db absent — no queue "
                   "to point at.</p>")
    out.append("</section>")
    return "\n".join(out)


_PAGE_CSS = """
:root { color-scheme: light; }
body { font-family: system-ui, sans-serif; margin: 0; background:
  #f6f4ef; color: #1d1a16; }
header.top { display: flex; justify-content: space-between;
  align-items: baseline; padding: 14px 22px 6px; }
header.top h1 { font-size: 19px; margin: 0; }
header.top .meta { color: #6b6459; font-size: 12.5px; }
nav a { color: #4a4238; margin-left: 14px; font-size: 13.5px; }
main { padding: 6px 22px 30px; }
.strip.health { display: flex; flex-wrap: wrap; gap: 8px 26px;
  background: #fff; border: 1px solid #ddd5c7; border-radius: 8px;
  padding: 10px 14px; margin: 10px 0; }
.strip.health .k { display: block; font-size: 11px; text-transform:
  uppercase; letter-spacing: .05em; color: #8a8175; }
.strip.health .v { font-size: 13.5px; }
.v.warn, .warn { color: #a33327; font-weight: 600; }
.v.ok { color: #2e6b34; }
section { margin: 20px 0; }
h2 { font-size: 14.5px; margin: 0 0 8px; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; }
.card { background: #fff; border: 1px solid #ddd5c7; border-radius:
  8px; padding: 12px 16px; min-width: 150px; display: flex;
  flex-direction: column; gap: 2px; }
.card .n { font-size: 26px; font-weight: 650; }
.card .t { font-size: 13px; }
.card .s { font-size: 12px; color: #6b6459; }
table { border-collapse: collapse; width: 100%; background: #fff;
  border: 1px solid #ddd5c7; border-radius: 8px; }
th, td { text-align: left; padding: 7px 10px; font-size: 13px;
  border-bottom: 1px solid #eee7da; }
th { font-size: 11.5px; text-transform: uppercase; letter-spacing:
  .04em; color: #8a8175; }
tbody tr[data-fp] { cursor: pointer; }
tbody tr[data-fp]:hover { background: #faf6ee; }
.chip { font-size: 12px; padding: 1px 8px; border-radius: 9px;
  background: #e8e2d5; }
.chip-complete { background: #d7ecd4; }
.chip-vectors_partial { background: #f3e3bb; }
.chip-text_complete { background: #e6ddf0; }
.chip-failed { background: #f3c4bd; }
.muted { color: #6b6459; }
.note { color: #6b6459; font-size: 12.5px; }
pre#explain { background: #fff; border: 1px solid #ddd5c7;
  border-radius: 8px; padding: 12px; white-space: pre-wrap;
  font-size: 12.5px; max-height: 480px; overflow: auto; }
tbody tr.rowlink { cursor: pointer; }
tbody tr.rowlink:hover { background: #faf6ee; }
section#explain { background: #fff; border: 1px solid #ddd5c7;
  border-radius: 8px; padding: 12px 16px; margin: 20px 0; }
section#explain pre { white-space: pre-wrap; font-size: 12.5px;
  max-height: 480px; overflow: auto; }
dl.packet { display: grid; grid-template-columns: 140px 1fr;
  gap: 3px 12px; font-size: 13px; margin: 8px 0; }
dl.packet dt { color: #8a8175; }
dl.packet dd { margin: 0; white-space: pre-wrap; word-break:
  break-word; }
.rulebar { margin: 12px 0 4px; display: flex; gap: 8px;
  align-items: center; flex-wrap: wrap; }
.rulebar .note { margin-right: 4px; }
button, .btn { font: inherit; font-size: 13px; background: #2e4a34;
  color: #fff; border: 0; border-radius: 6px; padding: 6px 12px;
  cursor: pointer; text-decoration: none; display: inline-block; }
button:hover, .btn:hover { background: #3a5f43; }
button.rowact { background: #e8e2d5; color: #1d1a16; padding:
  3px 9px; font-size: 12px; }
button.danger { background: #a33327; }
header.top .actions { display: flex; gap: 8px; align-items: center; }
.tiles { display: flex; flex-wrap: wrap; gap: 14px; }
.tile { width: 150px; background: #fff; border: 1px solid #ddd5c7;
  border-radius: 8px; padding: 10px; text-align: center; }
.tile img { max-width: 110px; max-height: 150px; border-radius: 4px; }
.tile .nocover { height: 150px; display: flex; align-items: center;
  justify-content: center; color: #8a8175; font-size: 12px;
  border: 1px dashed #ddd5c7; border-radius: 4px; padding: 6px; }
.tile .tname { font-size: 12.5px; margin-top: 8px; word-break:
  break-word; }
.tile label { display: block; margin-top: 6px; font-size: 12.5px; }
.stages { display: flex; flex-wrap: wrap; gap: 6px; list-style:
  none; padding: 0; margin: 8px 0; }
.stages li { font-size: 12.5px; padding: 3px 10px; border-radius:
  11px; background: #e8e2d5; }
.stages li.active { background: #2e4a34; color: #fff; }
.stages li.done { background: #d7ecd4; }
.stages li.stopped { background: #f3e3bb; }
.stages li.failed { background: #f3c4bd; }
.feed { list-style: none; padding: 0; margin: 0; font-size: 13px; }
.feed li { padding: 3px 0; border-bottom: 1px solid #eee7da; }
.feed li.events { color: #4a4238; font-style: italic; }
pre.log { background: #1d1a16; color: #e8e2d5; border-radius: 8px;
  padding: 12px; white-space: pre-wrap; font-size: 12px;
  max-height: 420px; overflow: auto; }
code.argv { font-size: 12px; word-break: break-all; }
fieldset { border: 1px solid #ddd5c7; border-radius: 8px;
  background: #fff; margin: 12px 0; }
legend { font-size: 13px; font-weight: 600; }
a.rowact { background: #e8e2d5; color: #1d1a16; padding: 3px 9px;
  font-size: 12px; border-radius: 6px; text-decoration: none;
  display: inline-block; }
a.rowact:hover { background: #ddd3bf; }
.mxbook { margin: 14px 0 22px; }
.mxwrap { overflow-x: auto; }
table.mx { width: auto; max-width: 100%; }
table.mx th, table.mx td { padding: 3px 5px; font-size: 11.5px;
  text-align: center; }
table.mx th.ch { max-width: 92px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
table.mx th.rh { text-align: left; max-width: 130px; overflow:
  hidden; text-overflow: ellipsis; white-space: nowrap; }
.mxcell { min-width: 36px; height: 26px; }
.mxcell.diag { background: #efe9dc; }
.mxcell.bar { box-shadow: inset 0 0 0 2px #b8860b; }
.mxcell.settled-merged { box-shadow: inset 0 0 0 2px #2e6b34;
  font-weight: 650; }
.mxcell.settled-split { opacity: .55; }
.mxcell.settled-blocked { box-shadow: inset 0 0 0 2px #a33327;
  color: #a33327; font-weight: 650; }
.mxcols { display: grid; grid-template-columns: minmax(0, 1fr)
  300px; gap: 16px; align-items: start; }
@media (max-width: 900px) { .mxcols { grid-template-columns: 1fr; } }
.mxprog { font-size: 13px; margin: 0 0 6px; }
ul.verdicts { list-style: none; padding: 0; margin: 0;
  font-size: 12.5px; }
ul.verdicts li { padding: 3px 0; border-bottom: 1px solid
  #eee7da; }
ul.verdicts li.v-merged { color: #2e6b34; }
ul.verdicts li.v-blocked { color: #a33327; }
.mxnote { color: #6b6459; font-size: 12.5px; max-width: 640px; }
table.tp td.bar { box-shadow: inset 0 0 0 2px #b8860b; }
"""

_PAGE_JS = """
// Read-only dashboard refresh: re-render <main> from the server
// every 30 s while the tab is visible. The server is the only
// renderer, so a polled page can never drift from a fresh load.
setInterval(async () => {
  if (document.visibilityState !== 'visible') return;
  // An open forensics report is mid-read; swapping <main> would
  // destroy it. The poll simply skips until it is closed.
  const pre = document.getElementById('explain');
  if (pre && !pre.hidden) return;
  try {
    const res = await fetch(location.pathname, {cache: 'no-store'});
    if (!res.ok) return;
    const text = await res.text();
    const doc = new DOMParser().parseFromString(text, 'text/html');
    const fresh = doc.querySelector('main');
    const y = window.scrollY;
    document.querySelector('main').replaceWith(fresh);
    wireRows();
    window.scrollTo(0, y);
  } catch (e) { /* a busy store answers on the next poll */ }
}, 30000);
// Book rows open the work's forensics (the read-only --explain
// report); a row's Re-embed button launches action A3 and
// navigates to the new run page.
function wireRows() {
  // Cover tiles: a book with no cover gets the placeholder
  // block. The swap is wired here, with the stem carried in a
  // data attribute and assigned via textContent — an inline
  // onerror string would break (and worse) on stems with
  // quotes in them.
  document.querySelectorAll('img.tileimg').forEach(img => {
    img.addEventListener('error', () => {
      const d = document.createElement('div');
      d.className = 'nocover';
      d.textContent = img.dataset.stem || '';
      img.replaceWith(d);
    });
  });
  document.querySelectorAll('tr[data-fp]').forEach(row => {
    row.addEventListener('click', async () => {
      const pre = document.getElementById('explain');
      pre.hidden = false;
      pre.textContent = 'Loading forensics…';
      try {
        const res = await fetch('/api/explain?work=' +
          encodeURIComponent(row.dataset.fp));
        const data = await res.json();
        pre.textContent = data.text || data.error || 'No report.';
      } catch (e) {
        pre.textContent = 'Forensics unavailable: ' + e;
      }
      pre.scrollIntoView({block: 'nearest'});
    });
  });
  // "Last run's matrix" links navigate; without this, the
  // click would also bubble into the row's forensics handler
  // on its way out.
  document.querySelectorAll('a[data-mxlink]').forEach(a => {
    a.addEventListener('click', (ev) => { ev.stopPropagation(); });
  });
  document.querySelectorAll('[data-reembed]').forEach(btn => {
    btn.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      btn.disabled = true;
      try {
        const res = await fetch('/api/launch', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action: 'reembed',
                                work: btn.dataset.reembed})});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.status);
        location.href = '/run/' + data.job_id;
      } catch (e) {
        alert('Re-embed refused: ' + e.message);
        btn.disabled = false;
      }
    });
  });
  // Page-specific wiring (the Desk's rows) rebinds after every
  // poll swap, through the same single entry point.
  if (window.wireDesk) window.wireDesk();
}
wireRows();
// Backup now (A5): fires the CLI backup into the configured
// directory and navigates to its run page; a refusal (no
// directory configured) is shown, never hidden.
async function fireBackup() {
  try {
    const res = await fetch('/api/backup', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: '{}'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    location.href = '/run/' + data.job_id;
  } catch (e) {
    alert('Backup refused: ' + e.message);
  }
}
"""


def _page(title, main_html, ctx, extra_script=""):
    version = source_constant(ctx.script_dir / "process-portable.py",
                              "PORTABLE_VERSION") or "?"
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, "
        f"initial-scale=1'><title>{_esc(title)}</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        "<header class='top'><h1>Processor Control Room</h1>"
        "<span class='actions'>"
        "<a class='btn' href='/ingest'>Ingest…</a>"
        "<a class='btn' href='/launch'>Run preview</a>"
        "<button type='button' onclick='fireBackup()'>"
        "Backup now</button></span>"
        "<nav><a href='/'>Home</a><a href='/desk'>The Desk</a></nav>"
        f"<span class='meta'>ebook-processor v{_esc(version)} · "
        "loopback only</span></header>"
        f"<main>{main_html}</main>"
        f"<script>{_PAGE_JS}</script>{extra_script}</body></html>")


def _claim_row(claim):
    forms = claim["name_forms"]
    forms_cell = _esc(", ".join(str(f) for f in forms)) \
        if len(forms) > 1 else ""
    return (
        f"<tr class='rowlink' data-claim='1' "
        f"data-work-key='{_esc(claim['work_key'])}' "
        f"data-claim-key='{_esc(claim['key'])}'>"
        f"<td>{_esc(claim['display'])}</td>"
        f"<td>{_esc(claim['work_title'])}</td>"
        f"<td>{_esc(claim['class'])}</td>"
        f"<td>{claim['sightings_count']}</td>"
        f"<td>{_esc(claim['best_chapters'])}</td>"
        f"<td>{_esc(claim['first_seen'] or '')}</td>"
        f"<td>{_esc(claim['last_seen'] or '')}</td>"
        f"<td>{forms_cell}</td></tr>")


_CLAIM_TABLE_HEAD = (
    "<table><thead><tr><th>Claim</th><th>Work</th><th>Class</th>"
    "<th>Sightings</th><th>Chapters</th><th>First seen</th>"
    "<th>Last seen</th><th>Forms differ</th></tr></thead><tbody>")


def _render_desk(desk):
    """The Desk (§2.2): today's batch, parked escalations,
    impulse proposals. Rows open evidence; the only ruling
    controls in the building live inside an uncertainty item's
    evidence view (§2.2's structural cut — no inline Keep/Drop,
    and per GAP-4 no claim controls anywhere)."""
    out = ["<h2>The Desk</h2>"]
    if desk["busy"]:
        out.append(
            "<p class='warn'>state.db is busy — another process "
            "holds it. The Desk retries on refresh; it never "
            "opens a store writable.</p>")
    if not desk["present"] and not desk["busy"]:
        out.append(
            "<p class='muted'>No state.db yet — nothing has "
            "staged or queued. The Desk fills when runs leave "
            "claims and uncertainty behind.</p>")

    out.append("<section><h2>Today's trope batch</h2>")
    if desk["present"]:
        out.append(
            f"<p class='note'>Staged claims, most sightings "
            f"first, then chapter support — the first "
            f"{desk['batch_window']} of {desk['staged_total']} "
            f"staged (one labelling round's window). A row opens "
            f"the claim's evidence.</p>")
        out.append(
            f"<p class='note'><strong>GAP-4:</strong> "
            f"{_esc(desk['gap4'])}</p>")
        if desk["batch"]:
            out.append(_CLAIM_TABLE_HEAD)
            out.extend(_claim_row(c) for c in desk["batch"])
            out.append("</tbody></table>")
        else:
            out.append("<p class='muted'>No staged claims.</p>")
    out.append("</section>")

    out.append("<section><h2>Parked escalations</h2>")
    if desk["present"]:
        items = desk["parked_items"]
        if items:
            out.append(
                "<table><thead><tr><th>#</th><th>Class</th>"
                "<th>Subject</th><th>Packet</th><th>Filed</th>"
                "</tr></thead><tbody>")
            for item in items:
                out.append(
                    f"<tr class='rowlink' data-qid='{item['id']}'>"
                    f"<td>{item['id']}</td>"
                    f"<td>{_esc(item['class'])}</td>"
                    f"<td>{_esc(item['subject'] or '')}</td>"
                    f"<td>{_esc(item['kind'] or '')}</td>"
                    f"<td>{_esc(item['created'] or '')}</td></tr>")
            out.append("</tbody></table>")
        else:
            out.append(
                "<p class='muted'>No uncertainty items awaiting "
                "a ruling.</p>")
        if desk["parked_claims"]:
            out.append(
                f"<h2>Staged beyond the batch window "
                f"({len(desk['parked_claims'])})</h2>")
            out.append(
                "<p class='note'>These wait for a later round; "
                "the window keeps the daily batch a round, not "
                "a backlog.</p>")
            out.append(_CLAIM_TABLE_HEAD)
            out.extend(_claim_row(c)
                       for c in desk["parked_claims"])
            out.append("</tbody></table>")
    out.append("</section>")

    out.append("<section><h2>Impulse proposals</h2>")
    out.append(
        "<p class='note'>Off-catalogue trope labels from the "
        "local <code>--trope-map</code> review files — "
        "per-preview proposals (Q5), not the global queue. "
        "Write-runs record theirs in Supabase, which the portal "
        "never reads (§4).</p>")
    if desk["proposals_broken"]:
        out.append(
            f"<p class='warn'>{desk['proposals_broken']} "
            f"trope-map review file(s) could not be read and "
            f"were skipped.</p>")
    if desk["proposals"]:
        out.append(
            "<table><thead><tr><th>Label</th><th>Nearest "
            "catalogue</th><th>Similarity</th><th>Chapters</th>"
            "<th>Book</th><th>From file</th></tr></thead>"
            "<tbody>")
        for p in desk["proposals"]:
            near = p["nearest_catalog_id"] or "—"
            sim = p["nearest_similarity"]
            # The review files are untrusted data (a broken one
            # is skipped and counted); their numeric-looking
            # fields are escaped like everything else they carry.
            out.append(
                f"<tr><td>{_esc(p['label'] or '')}</td>"
                f"<td>{_esc(near)}</td>"
                f"<td>{'' if sim is None else _esc(sim)}</td>"
                f"<td>{_esc(p['chapter_count'] or '')}</td>"
                f"<td>{_esc(p['book'] or '')}</td>"
                f"<td>{_esc(p['file'])}</td></tr>")
        out.append("</tbody></table>")
    else:
        out.append(
            "<p class='muted'>No proposals in the local review "
            "files.</p>")
    out.append("</section>")

    out.append(
        "<section id='explain' hidden><h2 id='ev-title'>"
        "Evidence</h2><div id='ev-body'></div></section>")
    return "\n".join(out)


_DESK_JS = """
<script>
// The Desk's wiring. Everything fetched is rendered with
// textContent / createElement — evidence and packets are book
// data, and book data never becomes markup.
function _evSection() {
  const sec = document.getElementById('explain');
  sec.hidden = false;
  return sec;
}
function _evSet(title, build) {
  const sec = _evSection();
  document.getElementById('ev-title').textContent = title;
  const body = document.getElementById('ev-body');
  body.replaceChildren();
  build(body);
  sec.scrollIntoView({block: 'nearest'});
}
function _evNote(body, text) {
  const p = document.createElement('p');
  p.className = 'note';
  p.textContent = text;
  body.appendChild(p);
}
async function _openClaim(row) {
  _evSet('Claim evidence', (body) => {
    _evNote(body, 'Loading evidence…');
  });
  try {
    const res = await fetch('/api/desk/claim?work_key=' +
      encodeURIComponent(row.dataset.workKey) + '&claim_key=' +
      encodeURIComponent(row.dataset.claimKey));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    const claim = data.claim;
    _evSet('Claim — ' + claim.display, (body) => {
      // The evidence opens with the full sightings history:
      // every run that has ever sighted this claim, from the
      // store's own record.
      const h = document.createElement('p');
      const runs = claim.sightings.join(', ');
      h.textContent = 'Sightings history — ' +
        claim.sightings_count + ' sighting(s): run(s) ' + runs +
        '. First seen ' + (claim.first_seen || '—') +
        ', last seen ' + (claim.last_seen || '—') +
        ', best chapter support ' + claim.best_chapters + '.';
      body.appendChild(h);
      if (claim.name_forms.length) {
        _evNote(body, 'Name forms: ' +
          claim.name_forms.join(' · '));
      }
      _evNote(body, 'Staged claims have no ruling verb ' +
        '(GAP-4): this evidence is the whole of the Desk\\'s ' +
        'offer for a claim.');
      const pre = document.createElement('pre');
      pre.textContent = data.text || 'No report.';
      body.appendChild(pre);
    });
  } catch (e) {
    _evSet('Claim evidence', (body) => {
      _evNote(body, 'Evidence unavailable: ' + e.message);
    });
  }
}
function _packetDl(packet) {
  const dl = document.createElement('dl');
  dl.className = 'packet';
  for (const [key, value] of Object.entries(packet)) {
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    dd.textContent = (value !== null &&
      typeof value === 'object')
      ? JSON.stringify(value) : String(value ?? '');
    dl.appendChild(dt);
    dl.appendChild(dd);
  }
  return dl;
}
async function _openItem(row) {
  _evSet('Uncertainty item', (body) => {
    _evNote(body, 'Loading evidence…');
  });
  try {
    const res = await fetch('/api/desk/item?id=' +
      encodeURIComponent(row.dataset.qid));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    const item = data.item;
    _evSet(item.subject || ('Item #' + item.id), (body) => {
      _evNote(body, 'Class ' + item.class + ' · filed ' +
        (item.created || '—') + ' · run ' +
        (item.run_ref || '—') + ' · packet kind ' +
        (item.kind || '—') + '.');
      body.appendChild(_packetDl(item.packet || {}));
      // The ruling control lives here, in the evidence view
      // (§2.2) — never on the queue row. Each button records
      // the exact label string it shows.
      const bar = document.createElement('div');
      bar.className = 'rulebar';
      const lead = document.createElement('span');
      lead.className = 'note';
      lead.textContent = 'Rule this item:';
      bar.appendChild(lead);
      for (const label of item.labels) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = label;
        btn.addEventListener('click', async () => {
          bar.querySelectorAll('button').forEach(
            (b) => { b.disabled = true; });
          try {
            const r = await fetch('/api/desk/rule', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({queue_id: item.id,
                                    label: label})});
            const d = await r.json();
            if (!r.ok) throw new Error(d.error || r.status);
            location.href = '/run/' + d.job_id;
          } catch (e) {
            alert('Ruling refused: ' + e.message);
            bar.querySelectorAll('button').forEach(
              (b) => { b.disabled = false; });
          }
        });
        bar.appendChild(btn);
      }
      body.appendChild(bar);
      _evNote(body, 'A ruling writes the CLI\\'s labels file ' +
        'and applies it through the one-writer queue ' +
        '(--apply-uncertainty-labels); the item then leaves ' +
        'this queue.');
    });
  } catch (e) {
    _evSet('Uncertainty item', (body) => {
      _evNote(body, 'Evidence unavailable: ' + e.message);
    });
  }
}
window.wireDesk = function () {
  document.querySelectorAll('tr[data-claim]').forEach((row) => {
    row.addEventListener('click', () => _openClaim(row));
  });
  document.querySelectorAll('tr[data-qid]').forEach((row) => {
    row.addEventListener('click', () => _openItem(row));
  });
};
wireDesk();
</script>
"""


def _render_ingest(ctx):
    """The ingest picker (§2.4): cover tiles exist only here.
    One launch per picked file (A1 is per-file), all sharing
    the reason / refresh choices; the jobs queue under the
    one-writer rule like everything else."""
    files = import_files(ctx)
    out = ["<h2>Ingest — pick from the import folder</h2>"]
    out.append(
        f"<p class='note'>{_esc(import_dir(ctx))} — the same "
        f"folder the CLI reads. Tiles show each book's own "
        f"cover, extracted by the portal for this picker only "
        f"(Q10).</p>")
    if not files:
        out.append("<p class='muted'>No book files in the import "
                   "folder.</p>")
        return "\n".join(out)
    out.append("<form id='ingest-form'><div class='tiles'>")
    for entry in files:
        name = entry["name"]
        stem = Path(name).stem
        out.append(
            f"<div class='tile'>"
            f"<img class='tileimg' "
            f"src='/cover?name={quote(name, safe='')}' alt='' "
            f"data-stem='{_esc(stem)}'>"
            f"<div class='tname'>{_esc(name)}</div>"
            f"<div class='muted'>{_fmt_bytes(entry['size'])}</div>"
            f"<label><input type='checkbox' name='file' "
            f"value='{_esc(name)}'> Ingest</label></div>")
    out.append("</div>")
    out.append(
        "<fieldset><legend>Options</legend>"
        "<label>Reason <input type='text' id='ingest-reason' "
        "size='40'></label> "
        "<label><input type='checkbox' id='ingest-force'> "
        "Refresh (re-ingest even if unchanged)</label>"
        "</fieldset>"
        "<p><button type='submit'>Ingest picked books</button></p>"
        "</form>")
    return "\n".join(out)


_INGEST_JS = """
<script>
document.getElementById('ingest-form').addEventListener(
  'submit', async (ev) => {
    ev.preventDefault();
    const picked = [...document.querySelectorAll(
      'input[name=file]:checked')].map(i => i.value);
    if (!picked.length) { alert('Pick at least one book.'); return; }
    const reason = document.getElementById('ingest-reason').value;
    const force = document.getElementById('ingest-force').checked;
    let first = null;
    for (const name of picked) {
      const res = await fetch('/api/launch', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'ingest', file_name: name,
                              reason: reason, force: force})});
      const data = await res.json();
      if (!res.ok) { alert('Ingest refused for ' + name + ': ' +
        data.error); return; }
      if (first === null) first = data.job_id;
    }
    location.href = '/run/' + first;
  });
</script>
"""


def _render_launch(ctx):
    """The run launcher (A2/A4, Q9): the standing command is
    the only recipe — the variations are a single file, a saved
    profile, and the preview/write choice. No flag builder."""
    files = import_files(ctx)
    profiles = available_profiles(ctx.script_dir)
    out = ["<h2>Run — the standing command</h2>"]
    out.append(
        "<p class='note'>Launches <code class='argv'>"
        + _esc(" ".join(STANDING_FLAGS)) +
        "</code> exactly as the terminal standing command does, "
        "plus --no-viz and --emit-run-events. A saved profile "
        "is the only variation (Q9).</p>")
    out.append("<form id='launch-form'>")
    out.append(
        "<fieldset><legend>Run type</legend>"
        "<label><input type='radio' name='kind' value='preview' "
        "checked> Preview — saves preview files, publishes "
        "nothing</label><br>"
        "<label><input type='radio' name='kind' value='write'> "
        "Write run — publishes to the corpus database</label>"
        "</fieldset>")
    out.append("<fieldset><legend>Scope</legend>")
    out.append("<label>Book <select id='launch-file'>"
               "<option value=''>Whole import folder</option>")
    for entry in files:
        out.append(f"<option value='{_esc(entry['name'])}'>"
                   f"{_esc(entry['name'])}</option>")
    out.append("</select></label> ")
    out.append("<label>Profile <select id='launch-profile'>"
               "<option value=''>None (standing command)</option>")
    for name, src in sorted(profiles.items()):
        out.append(f"<option value='{_esc(name)}'>"
                   f"{_esc(name)} ({_esc(src)})</option>")
    out.append("</select></label></fieldset>")
    out.append(
        "<fieldset><legend>Write-run confirm</legend>"
        "<label><input type='checkbox' id='launch-confirm'> "
        "I understand a write run publishes — it writes claims "
        "to the corpus database</label></fieldset>")
    out.append("<p><button type='submit'>Launch</button></p>"
               "</form>")
    return "\n".join(out)


_LAUNCH_JS = """
<script>
document.getElementById('launch-form').addEventListener(
  'submit', async (ev) => {
    ev.preventDefault();
    const kind = document.querySelector(
      'input[name=kind]:checked').value;
    const payload = {action: kind,
      file_name: document.getElementById('launch-file').value ||
        null,
      profile: document.getElementById('launch-profile').value ||
        null,
      confirm: document.getElementById('launch-confirm').checked};
    const res = await fetch('/api/launch', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)});
    const data = await res.json();
    if (!res.ok) { alert('Launch refused: ' + data.error); return; }
    location.href = '/run/' + data.job_id;
  });
</script>
"""


def _render_run(job):
    """The run page shell (§2.3). Everything dynamic arrives
    from the job snapshot (SSE first, polling fallback); the
    server renders identity — label, flags exactly as launched,
    which store it writes — because those never change."""
    store = ("corpus.db + state.db (via the CLI)"
             if job.action != "backup"
             else "the configured backup directory (via the CLI)")
    main_html = (
        f"<h2>{_esc(job.label)}</h2>"
        f"<p class='note'>Job {job.id} · writes {store}</p>"
        f"<p><code class='argv'>{_esc(' '.join(job.argv))}</code></p>"
        "<p><span class='chip' id='run-state'>…</span> "
        "<span class='muted' id='run-elapsed'></span></p>"
        "<p id='run-queued' class='warn'></p>"
        "<ol class='stages' id='run-stages'></ol>"
        "<p id='run-now'></p>"
        "<p class='muted' id='run-progress'></p>"
        "<p class='muted' id='run-decision'></p>"
        "<p><button type='button' id='run-stop' class='danger' "
        "hidden>Stop</button> "
        "<button type='button' id='run-resume' hidden>"
        "Resume</button></p>"
        "<div id='run-backup'></div>"
        "<div id='run-artifacts'></div>"
        + _MATRIX_PANEL_HTML +
        "<h3>Results</h3><ul class='feed' id='run-feed'></ul>"
        "<details><summary>Run log (raw output)</summary>"
        "<pre class='log' id='run-log'></pre></details>")
    script = (f"<script>const JOB_ID = {job.id}; "
              f"const MX_URL = '/api/jobs/' + JOB_ID + '/matrix'; "
              f"const MX_LOCK_META = false;</script>"
              + _RUN_JS + _MATRIX_JS)
    return main_html, script


_RUN_JS = """
<script>
const feedEl = document.getElementById('run-feed');
const logEl = document.getElementById('run-log');
function esc(s) { const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s); return d.innerHTML; }
function fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ':' + String(s).padStart(2, '0'); }
function render(snap) {
  document.getElementById('run-state').textContent = snap.state;
  document.getElementById('run-elapsed').textContent =
    'elapsed ' + fmtElapsed(snap.elapsed) +
    (snap.exit_code != null ? ' · exit ' + snap.exit_code : '');
  document.getElementById('run-queued').textContent =
    snap.queued_behind ? 'Queued behind ' + snap.queued_behind +
      ' — writer jobs run one at a time.' : '';
  document.getElementById('run-stages').innerHTML =
    snap.stages.map(s => '<li class="' + esc(s.state) + '">' +
      esc(s.name) + '</li>').join('');
  document.getElementById('run-now').textContent = snap.now || '';
  let prog = '';
  if (snap.books_total) prog = 'Books: ' + snap.books_done +
    ' / ' + snap.books_total + ' complete';
  else if (snap.books_done) prog = 'Books complete: ' +
    snap.books_done;
  if (snap.checkpointed) prog += ' · checkpoints saved';
  document.getElementById('run-progress').textContent = prog;
  const dec = snap.events && snap.events.decision;
  document.getElementById('run-decision').textContent = dec ?
    'Decision calls: ' + dec.used + ' / ' + dec.cap +
    ' (run-events artifact)' : '';
  document.getElementById('run-stop').hidden = !snap.can_stop;
  document.getElementById('run-resume').hidden = !snap.can_resume;
  document.getElementById('run-backup').innerHTML =
    snap.backup && snap.backup.last_backup ?
      '<p>Backup recorded: ' + esc(snap.backup.last_backup) +
      ' (' + esc(snap.backup.stores.join(', ')) + ')</p>' : '';
  document.getElementById('run-artifacts').innerHTML =
    snap.artifacts && snap.artifacts.length ?
      '<p>Preview artifacts: ' +
      snap.artifacts.map(esc).join(', ') + '</p>' : '';
  feedEl.innerHTML = snap.feed.map(f =>
    '<li class="' + esc(f.source) + '">' + esc(f.text) +
    '</li>').join('');
  logEl.textContent = snap.log.join('\\n');
}
let lastJson = '';
function apply(text) {
  if (text === lastJson) return;
  lastJson = text;
  try { render(JSON.parse(text)); } catch (e) {}
}
async function poll() {
  try {
    const res = await fetch('/api/jobs/' + JOB_ID,
      {cache: 'no-store'});
    if (res.ok) apply(await res.text());
  } catch (e) {}
}
let polling = setInterval(poll, 1500);
poll();
try {
  const es = new EventSource('/api/jobs/' + JOB_ID + '/stream');
  es.onmessage = (ev) => { apply(ev.data); };
  es.onerror = () => { /* the poll above is the fallback */ };
} catch (e) {}
document.getElementById('run-stop').addEventListener('click',
  async () => {
    await fetch('/api/jobs/' + JOB_ID + '/stop', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: '{}'});
    poll();
  });
document.getElementById('run-resume').addEventListener('click',
  async () => {
    const res = await fetch('/api/jobs/' + JOB_ID + '/resume',
      {method: 'POST', headers: {'Content-Type':
      'application/json'}, body: '{}'});
    const data = await res.json();
    if (!res.ok) { alert('Resume refused: ' + data.error); return; }
    location.href = '/run/' + data.job_id;
  });
</script>
"""


# The panel's server-rendered skeleton — identical on the run
# page and on the reopening page (§2.3: it lives on the run
# page, plus its post-run reopening; nowhere else). Everything
# inside it is display: the skeleton holds no control of any
# kind, and the script below builds none either. The grid
# itself is client-rendered from the reduction payload because
# it grows cell by cell while a run is live.
_MATRIX_PANEL_HTML = (
    "<section id='mx-panel' hidden>"
    "<h3>Matrices — merge resolution</h3>"
    "<p class='note' id='mx-meta'></p>"
    "<div id='mx-books'></div>"
    "</section>")


_MATRIX_JS = """
<script>
// The Matrices panel (S4, spec §2.3). A window, not a lever:
// this script only GETs its payload and paints display nodes —
// it builds no button, link, input, or any other control, and
// nothing in the panel can change an outcome. Two rendering
// rules carry the spec:
//   * freeze-on-settle — a settled cell is painted once and
//     never touched again (mxSettled is consulted before
//     every paint; the reduction already guarantees a settled
//     cell's data never moves, this keeps its DOM node just
//     as still);
//   * shade-only under the floor — a score below the payload's
//     shade_floor paints colour and no number (mxCellText).
const mxBooks = {};   // book name -> rendered section state
const mxSettled = {}; // book + pair key -> true once painted
function mxKey(a, b) {
  return a <= b ? a + '\\u0000' + b : b + '\\u0000' + a; }
function mxShade(score) {
  if (score == null) return '';
  const alpha = 0.08 + 0.55 *
    Math.max(0, Math.min(1, score));
  return 'background:rgba(51,82,63,' + alpha.toFixed(2) + ')'; }
function mxCellText(cell, bars) {
  if (cell.score != null)
    return cell.score >= bars.shade_floor
      ? cell.score.toFixed(2) : '';
  if (cell.state === 'settled')
    return cell.verdict === 'merged' ? '✓'
      : (cell.verdict === 'blocked' ? '⊘' : '');
  return '';
}
function mxCellTitle(cell) {
  let t = cell.a + ' × ' + cell.b;
  if (cell.score != null) t += ' · ' + cell.score.toFixed(3);
  if (cell.state === 'settled') {
    t += ' → ' + cell.verdict + ' via ' + cell.via;
    if (cell.reason) t += ' — ' + cell.reason;
  }
  return t;
}
function mxSection(book) {
  const root = document.createElement('div');
  root.className = 'mxbook';
  const h = document.createElement('h4');
  h.textContent = book + ' — character candidates';
  root.appendChild(h);
  const cols = document.createElement('div');
  cols.className = 'mxcols';
  const left = document.createElement('div');
  const twrap = document.createElement('div');
  twrap.className = 'mxwrap';
  const table = document.createElement('table');
  table.className = 'mx';
  const thead = document.createElement('thead');
  const hrow = document.createElement('tr');
  hrow.appendChild(document.createElement('th'));
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  table.appendChild(tbody);
  twrap.appendChild(table);
  left.appendChild(twrap);
  const legend = document.createElement('p');
  legend.className = 'mxnote';
  legend.textContent = 'Scores are candidacy, not decisions: ' +
    'a pair can clear the bar and still split when the ' +
    'evidence binds the two as family rather than identity.';
  left.appendChild(legend);
  cols.appendChild(left);
  const rail = document.createElement('div');
  const prog = document.createElement('p');
  prog.className = 'mxprog';
  rail.appendChild(prog);
  const ul = document.createElement('ul');
  ul.className = 'verdicts';
  rail.appendChild(ul);
  cols.appendChild(rail);
  root.appendChild(cols);
  const ih = document.createElement('h4');
  ih.textContent = 'Trope impulses — nearest catalogue ' +
    'neighbours';
  ih.hidden = true;
  root.appendChild(ih);
  const tp = document.createElement('table');
  tp.className = 'tp';
  tp.hidden = true;
  const itp = document.createElement('thead');
  const itr = document.createElement('tr');
  ['Label', 'Nearest', '2nd', '3rd', 'Fate'].forEach(t => {
    const th = document.createElement('th');
    th.textContent = t;
    itr.appendChild(th);
  });
  itp.appendChild(itr);
  tp.appendChild(itp);
  const itb = document.createElement('tbody');
  tp.appendChild(itb);
  root.appendChild(tp);
  document.getElementById('mx-books').appendChild(root);
  const sec = {hrow, tbody, prog, ul, ih, tp, itb, names: [],
               rows: {}, cellEls: {}, railCount: 0, impCount: 0};
  mxBooks[book] = sec;
  return sec;
}
function mxPairCell(sec, key) {
  const td = document.createElement('td');
  td.className = 'mxcell';
  (sec.cellEls[key] = sec.cellEls[key] || []).push(td);
  return td;
}
function mxGrowAxes(sec, names) {
  // Names only ever append — the reduction lists them in
  // first-appearance order — so growth adds a header, a row,
  // and one cell per existing row. Existing nodes, settled
  // ones included, are never rebuilt or moved.
  for (let i = sec.names.length; i < names.length; i++) {
    const name = names[i];
    const th = document.createElement('th');
    th.className = 'ch';
    th.textContent = name;
    th.title = name;
    sec.hrow.appendChild(th);
    sec.names.forEach(prev => {
      sec.rows[prev].appendChild(
        mxPairCell(sec, mxKey(prev, name)));
    });
    const tr = document.createElement('tr');
    const rh = document.createElement('th');
    rh.className = 'rh';
    rh.textContent = name;
    rh.title = name;
    tr.appendChild(rh);
    sec.names.forEach(prev => {
      tr.appendChild(mxPairCell(sec, mxKey(prev, name)));
    });
    const diag = document.createElement('td');
    diag.className = 'mxcell diag';
    // The diagonal is a real address, not decoration: a
    // Signal-1 merge joins two roster entries carrying the
    // SAME name, and its cell is (name, name).
    (sec.cellEls[mxKey(name, name)] =
      sec.cellEls[mxKey(name, name)] || []).push(diag);
    tr.appendChild(diag);
    sec.tbody.appendChild(tr);
    sec.rows[name] = tr;
    sec.names.push(name);
  }
}
function mxPaintCells(sec, bookState, bars) {
  bookState.cells.forEach(cell => {
    const key = mxKey(cell.a, cell.b);
    const skey = bookState.book + '\\u0000' + key;
    if (mxSettled[skey]) return;  // frozen: never repainted
    (sec.cellEls[key] || []).forEach(td => {
      td.className = 'mxcell' +
        (cell.score != null && cell.score >= bars.merge_bar
          ? ' bar' : '') +
        (cell.state === 'settled'
          ? ' settled-' + cell.verdict : '');
      td.style.cssText = mxShade(cell.score);
      td.textContent = mxCellText(cell, bars);
      td.title = mxCellTitle(cell);
    });
    if (cell.state === 'settled') mxSettled[skey] = true;
  });
}
function mxAppendRail(sec, bookState) {
  const vs = bookState.verdicts;
  for (let i = sec.railCount; i < vs.length; i++) {
    const v = vs[i];
    const li = document.createElement('li');
    li.className = 'v-' + v.verdict;
    let t = v.a + ' × ' + v.b + ' → ' + v.verdict +
      ' via ' + v.via;
    if (v.score != null) t += ' (' + v.score.toFixed(2) + ')';
    if (v.reason) t += ' — ' + v.reason;
    li.textContent = t;
    sec.ul.appendChild(li);
  }
  sec.railCount = vs.length;
  sec.prog.textContent = bookState.resolved + ' / ' +
    bookState.candidates + ' candidate pairs resolved' +
    (bookState.decision_calls != null
      ? ' · ' + bookState.decision_calls + ' decision calls'
      : '');
}
function mxAppendImpulses(sec, bookState, bars) {
  const imps = bookState.impulses;
  sec.ih.hidden = sec.tp.hidden = imps.length === 0;
  for (let i = sec.impCount; i < imps.length; i++) {
    const im = imps[i];
    const tr = document.createElement('tr');
    const label = document.createElement('td');
    label.textContent = im.label;
    tr.appendChild(label);
    for (let k = 0; k < 3; k++) {
      const td = document.createElement('td');
      const nb = im.neighbours[k];
      if (nb) {
        td.textContent = nb.name + ' (' +
          nb.score.toFixed(2) + ')';
        td.style.cssText = mxShade(nb.score);
        if (nb.score >= bars.map_bar) td.className = 'bar';
      }
      tr.appendChild(td);
    }
    const fate = document.createElement('td');
    if (im.fate === 'mapped') {
      const cat = im.neighbours.find(n => n.id === im.catalog_id);
      fate.textContent = 'mapped → ' +
        (cat ? cat.name : im.catalog_id);
    } else {
      fate.textContent = 'routed to proposals';
    }
    tr.appendChild(fate);
    sec.itb.appendChild(tr);
  }
  sec.impCount = imps.length;
}
function mxApplyBook(bookState, bars) {
  const sec = mxBooks[bookState.book] ||
    mxSection(bookState.book);
  mxGrowAxes(sec, bookState.names);
  mxPaintCells(sec, bookState, bars);
  mxAppendRail(sec, bookState);
  mxAppendImpulses(sec, bookState, bars);
}
let mxMetaDone = false;
let mxWrap = null;
function applyMatrix(data) {
  const panel = document.getElementById('mx-panel');
  // The page shell re-renders <main> from the server on its
  // own refresh; when it does, the skeleton is a fresh element
  // and every node this script painted is detached with the
  // old one. Detect the swap by the container's identity and
  // rebuild into the live skeleton — the same recovery a page
  // reload performs. Without it the panel would keep painting
  // into a detached tree and show nothing.
  const wrap = document.getElementById('mx-books');
  if (mxWrap !== null && wrap !== mxWrap) {
    for (const k in mxBooks) delete mxBooks[k];
    for (const k in mxSettled) delete mxSettled[k];
    mxMetaDone = false;
  }
  mxWrap = wrap;
  const books = (data.matrix && data.matrix.books) || [];
  const hasContent = books.some(b =>
    b.cells.length || b.impulses.length);
  if (hasContent || data.matrix.finished) panel.hidden = false;
  if (!mxMetaDone && !MX_LOCK_META) {
    mxMetaDone = true;
    document.getElementById('mx-meta').textContent =
      'Cosine over name + context vectors · merge bar ' +
      data.bars.merge_bar + ' · mapping bar ' +
      data.bars.map_bar + ' · cells under ' +
      data.bars.shade_floor + ' are shade only — the bar ' +
      'nominates; guards and verdicts decide.' +
      (data.run_id ? ' · run events ' + data.run_id : '');
  }
  books.forEach(b => mxApplyBook(b, data.bars));
  if (!hasContent && data.matrix.finished && !MX_LOCK_META) {
    document.getElementById('mx-meta').textContent =
      'This run scored no candidate pairs.';
  }
}
async function pollMatrix() {
  try {
    const res = await fetch(MX_URL, {cache: 'no-store'});
    if (res.ok) applyMatrix(await res.json());
  } catch (e) {}
}
setInterval(pollMatrix, 1500);
pollMatrix();
</script>
"""


def _render_matrix_page(info, run_id):
    """The reopening page (§2.1): one work's last run's
    matrix, read from the retained artifact. Same panel, same
    script as the run page — the grid is reduced from the
    artifact itself, so what reopens is the identical grid,
    not a re-rendering of a summary."""
    fingerprint, title, source_name = info
    if run_id:
        note = (f"Run events {_esc(run_id)}, read from the "
                f"retained run-events artifact. The panel is a "
                f"window on the resolver's finished work — "
                f"nothing here can change an outcome.")
    else:
        note = ("No retained run-events artifact records a "
                "resolver pass for this book yet.")
    main_html = (
        f"<h2>{_esc(title or source_name)} — last run's "
        f"matrix</h2>"
        f"<p class='note'>{note}</p>"
        + _MATRIX_PANEL_HTML)
    script = (f"<script>const MX_URL = '/api/matrix?work="
              f"{_esc(fingerprint)}'; "
              f"const MX_LOCK_META = true;</script>"
              + _MATRIX_JS)
    return main_html, script


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class PortalContext:
    """Everything a request needs: where the stores live, where
    the processor script lives, and the configured backup dir.
    Held explicitly (not in globals) so tests can point the
    server at fixture stores. Owns the job registry (S2): every
    launch in the portal goes through it, so the one-writer
    slot has exactly one keeper."""

    def __init__(self, learn_dir, script_dir, backup_dir=None,
                 popen_factory=None, stop_grace=5.0):
        self.learn_dir = Path(learn_dir)
        self.script_dir = Path(script_dir)
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self.registry = JobRegistry(
            self, popen_factory=popen_factory,
            stop_grace=stop_grace)
        # Standalone runs the portal observes but did not
        # launch (see ExternalRunWatcher): adopted from their
        # run-events artifacts, never queued, never signalled.
        self.external = ExternalRunWatcher(self)
        # Rulings in flight, queue_id -> job (or the _RULING_SENTINEL
        # while the launch is being built). A queue row stays
        # 'pending' in the store until its apply job actually
        # runs — behind a long writer, that can be a while — and
        # the CLI intake has no idempotence backstop (it would
        # apply a second ruling too, and append a second merge
        # pair). The Desk's guard therefore lives here: one
        # ruling per item at a time (Advisor F1).
        self._ruling_jobs = {}
        self._ruling_lock = threading.Lock()

    @property
    def cover_cache_dir(self):
        # Q10: the cover cache is the portal's own — a folder
        # beside portal.py, never in the learned dir or stores.
        return self.script_dir / "portal-cache" / "covers"

    def poll_external(self):
        """Let the external watcher look at the run-events
        folder once. A watcher fault must never take a page
        down — the surfaces degrade to launched jobs only, and
        the reason goes to the operator's terminal."""
        try:
            self.external.poll()
        except Exception as e:
            print(f"portal: external run scan failed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)

    def home(self):
        self.poll_external()
        data = home_data(self.learn_dir, self.script_dir,
                         self.backup_dir)
        # The strip reports this portal's own jobs plus the
        # standalone runs the watcher is observing live — each
        # labelled external, so the strip never lets a terminal
        # run borrow a launched job's authority (§5.1).
        data["strip"]["jobs"] = self.registry.briefs()
        return data

    def launch(self, spec_dict):
        return self.registry.launch(build_launch(self, spec_dict))

    def resume(self, job):
        return self.registry.launch(resume_spec(job))

    def explain(self, fingerprint):
        """Shell the CLI's --explain (action A7, read-only: the
        command opens both stores report-mode / mode=ro and
        writes nothing) and return its text. The fingerprint is
        the database-resolved value from resolve_work(), passed
        as one argv item — no shell ever sees request input."""
        script = self.script_dir / "process-portable.py"
        result = subprocess.run(
            [sys.executable, str(script), "--explain", fingerprint,
             "--learn-dir", str(self.learn_dir)],
            capture_output=True, text=True, timeout=120)
        text = (result.stdout or "").strip()
        if not text:
            text = (result.stderr or "").strip() \
                or "(--explain produced no output)"
        return text

    def work_info(self, query):
        """(fingerprint, title, source_name) for a work, or
        None. The fingerprint is resolve_work's
        database-resolved value — never the raw request string.
        The source name is the corpus record's own payload
        field: the name the work's book file carried at ingest,
        which is the only name a run-events artifact knows the
        book by."""
        fingerprint = resolve_work(self.learn_dir, query)
        if fingerprint is None:
            return None
        corpus_path = self.learn_dir / "corpus.db"
        if not corpus_path.exists():
            return None
        conn = _ro_connect(corpus_path)
        try:
            row = conn.execute(
                "SELECT title, payload FROM corpus_works "
                "WHERE fingerprint=?", (fingerprint,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if row is None:
            return None
        source_name = None
        try:
            payload = json.loads(row[1] or "{}") or {}
            if isinstance(payload, dict):
                source_name = payload.get("source_name")
        except (TypeError, ValueError):
            source_name = None
        return fingerprint, row[0], source_name

    def work_matrix(self, query):
        """The reopening payload for one work (§2.1's "last
        run's matrix"): the newest retained artifact that
        scored the work's book, reduced by the same function
        the live panel uses — the identical grid by
        construction, not a re-rendering. Returns
        (payload, None) or (None, reason)."""
        info = self.work_info(query)
        if info is None:
            return None, f"no such work: {query}"
        fingerprint, title, source_name = info
        if not source_name:
            return None, ("this work's corpus record names no "
                          "source file, so no run can be "
                          "matched to it")
        entry = latest_matrices(
            self.learn_dir, {source_name}).get(source_name)
        if entry is None:
            return None, ("no retained run-events artifact "
                          "records a resolver pass for this book")
        events, agg = _artifact_events(Path(entry["path"]))
        state = reduce_matrix(events)
        state = dict(state, books=[
            b for b in state["books"]
            if b["book"] == source_name])
        return {"work": {"fingerprint": fingerprint,
                         "title": title, "book": source_name},
                "run_id": entry["run_id"], "aggregates": agg,
                "matrix": state,
                "bars": _matrix_bars(self)}, None

    @property
    def labels_dir(self):
        # A6's scratch: the labels files the portal writes for
        # the CLI to consume live in the portal's own cache dir,
        # beside the cover cache (Q10's pattern) — never in the
        # learned dir, never in a store (§4's posture).
        return self.script_dir / "portal-cache" / "labels"

    def desk(self):
        return desk_data(self.learn_dir, self.script_dir)

    def explain_claim(self, work_key, claim_key):
        """Shell --explain for one staged claim (action A7,
        read-only). The work argument is the claim's own
        work_key: --explain resolves it exactly against
        staging_works, where a title could collide across
        editions; the claim key is the store's stored value,
        passed as one argv item."""
        script = self.script_dir / "process-portable.py"
        result = subprocess.run(
            [sys.executable, str(script), "--explain", work_key,
             "--explain-claim", claim_key,
             "--learn-dir", str(self.learn_dir)],
            capture_output=True, text=True, timeout=120)
        text = (result.stdout or "").strip()
        if not text:
            text = (result.stderr or "").strip() \
                or "(--explain produced no output)"
        return text

    def _claim_ruling(self, queue_id):
        """Claim the in-flight slot for one item, or refuse.
        The claim is recorded under the lock before the launch
        is built, so two handler threads cannot both pass; a
        settled job's claim prunes itself here, so a ruling
        whose job failed (the item is still pending) can be
        made again."""
        with self._ruling_lock:
            existing = self._ruling_jobs.get(queue_id)
            if existing is _RULING_SENTINEL or (
                    existing is not None
                    and existing.state in ("queued", "running")):
                raise PortalError(
                    f"uncertainty item {queue_id} already has a "
                    f"ruling queued or running — wait for it to "
                    f"settle")
            self._ruling_jobs[queue_id] = _RULING_SENTINEL

    def _release_ruling(self, queue_id, job=None):
        with self._ruling_lock:
            if job is None:
                self._ruling_jobs.pop(queue_id, None)
            else:
                self._ruling_jobs[queue_id] = job

    def rule(self, queue_id, label):
        """A6: rule one pending uncertainty item from its
        evidence view. The item is re-read fresh (a ruling never
        trusts the page's copy), the label must be one of the
        verdict strings that item's packet kind offers, and the
        apply runs as a writer job through the registry — the
        Desk's ruling takes the one-writer slot like every
        other state-changing launch (§7). One ruling per item
        at a time: the row stays pending until the job runs, so
        without the in-flight claim a second ruling (a second
        tab, a revisit while queued) would also be accepted and
        the CLI would apply both."""
        item = uncertainty_item(self.learn_dir, queue_id)
        if item is None:
            raise PortalError(
                f"no pending uncertainty item: {queue_id}")
        if label not in item["labels"]:
            raise PortalError(
                f"{label!r} is not a verdict for this item "
                f"(choose: {', '.join(item['labels'])})")
        self._claim_ruling(queue_id)
        try:
            path = self._write_labels_file(item, label)
            job = self.registry.launch(build_launch(
                self, {"action": "apply_labels",
                       "labels_file": str(path)}))
        except Exception:
            self._release_ruling(queue_id)
            raise
        self._release_ruling(queue_id, job)
        return job

    def _write_labels_file(self, item, label):
        """The CLI's labels intake, verbatim: a JSON list of
        {queue_id, label, labeller}. One file per ruling, kept
        in the portal's labels dir — the queue row the CLI
        updates is the audit trail; the file is its receipt."""
        self.labels_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.labels_dir / f"labels-{stamp}-{item['id']}.json"
        n = 1
        while path.exists():
            n += 1
            path = self.labels_dir / (
                f"labels-{stamp}-{item['id']}-{n}.json")
        entries = [{"queue_id": item["id"], "label": label,
                    "labeller": _DESK_LABELLER}]
        path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=1)
            + "\n", encoding="utf-8")
        return path


def _host_allowed(host_header, port):
    """Whether a request's Host header names this portal.

    Binding loopback stops other machines reaching the server,
    but not a malicious web page in the operator's own browser:
    without a Host check, DNS rebinding could make the page's
    origin resolve to 127.0.0.1 and read the console (or fire
    explain runs) cross-origin. Only the portal's own names —
    with the port it actually bound — are served.
    """
    if not host_header:
        return False
    allowed = {LOOPBACK, "localhost", "[::1]", "::1"}
    name = host_header.strip()
    if name.startswith("["):
        bare, _, rest = name[1:].partition("]")
        host_part = f"[{bare}]"
        port_part = rest.lstrip(":")
    elif name.count(":") == 1:
        host_part, _, port_part = name.partition(":")
    else:
        # No colon: a bare host. More than one: a bare IPv6
        # literal (the bracketed form is handled above).
        host_part, port_part = name, ""
    if host_part not in allowed:
        return False
    if not port_part:
        return True
    return port_part.isdigit() and int(port_part) == port


class _PortalHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that stays quiet about dead connections.

    Browsers open speculative connections and abort them before
    sending a request line; the read then raises
    ConnectionAbortedError (WinError 10053) or ConnectionResetError,
    and socketserver's default handle_error dumps a full traceback
    per abort — dozens per page load, burying the real errors in
    the operator's console. A connection the peer abandoned is not
    a server fault, so those are swallowed; every other exception
    keeps the default traceback.
    """

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError,
                            ConnectionResetError)):
            return
        # The same aborts in their raw Windows shape: an OSError
        # whose winerror is 10053 (aborted) or 10054 (reset). The
        # subclasses above usually carry them on Windows, but the
        # bare form is the same peer behaviour and gets the same
        # quiet.
        if isinstance(exc, OSError) \
                and getattr(exc, "winerror", None) in (10053, 10054):
            return
        super().handle_error(request, client_address)


def make_server(host, port, ctx):
    """Build (but do not start) the portal server. The bind host
    passes validate_bind_host here as well as in main(), so the
    loopback guarantee holds for any caller, not just the CLI
    entry point."""
    host = validate_bind_host(host)
    if host == "::1":
        # validate_bind_host accepts the spelling (it IS
        # loopback), but the stdlib server here is IPv4; failing
        # with a clear refusal beats a raw gaierror at bind time.
        raise PortalError(
            "this server binds IPv4 loopback (127.0.0.1); "
            "::1 is refused rather than half-supported")
    bind_host = LOOPBACK if host == "localhost" else host

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the operator's
            pass                        # terminal readable

        def _send(self, code, body, content_type):
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, code, payload):
            self._send(code, json.dumps(payload, ensure_ascii=False),
                       "application/json")

        def _host_ok(self):
            bound_port = self.server.server_address[1]
            if _host_allowed(self.headers.get("Host"), bound_port):
                return True
            self._json(403, {"error": "host not allowed"})
            return False

        def _job_or_404(self, job_id):
            # Adopt/refresh standalone runs first: an id the
            # strip just linked to must resolve here, and a run
            # page's polls are what keep an external run's
            # state honest between Home refreshes.
            ctx.poll_external()
            try:
                job = ctx.registry.get(int(job_id))
            except (TypeError, ValueError):
                job = None
            if job is None:
                self._json(404, {"error": f"no such job: {job_id}"})
            return job

        def _read_json_body(self):
            try:
                length = int(self.headers.get("Content-Length")
                             or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            if length > 65536:
                raise PortalError("request body too large")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise PortalError("request body is not JSON")
            if not isinstance(data, dict):
                raise PortalError("request body must be an object")
            return data

        def _launch_response(self, spec_dict):
            try:
                job = ctx.launch(spec_dict)
            except PortalError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, {"job_id": job.id, "state": job.state})

        def _rule_response(self, body):
            """A6 from the Desk: validate the body's shape here;
            the item, its status, and its label vocabulary are
            validated against the store in ctx.rule (PortalError
            -> the do_POST handler's 400). The id must be a JSON
            integer exactly — a float or boolean is refused, not
            truncated into a neighbour's id."""
            queue_id = body.get("queue_id")
            if isinstance(queue_id, bool) \
                    or not isinstance(queue_id, int):
                self._json(400, {
                    "error": "queue_id must be an integer"})
                return
            label = body.get("label")
            if not isinstance(label, str) or not label:
                self._json(400, {"error": "label is required"})
                return
            job = ctx.rule(queue_id, label)
            self._json(200, {"job_id": job.id, "state": job.state})

        def _origin_ok(self):
            """POSTs are state-changing, and the Host check
            alone cannot vet them: it validates the DESTINATION
            host, which a cross-site request to 127.0.0.1
            satisfies by construction. A browser page on another
            origin must not be able to fire launches (a "simple
            request" needs no preflight, and the write-run
            confirm is just a body field to an attacker). So:
            an Origin header, when present, must name this
            portal itself — loopback host, the bound port. The
            portal's own pages are same-origin, so they always
            pass; non-browser clients send no Origin at all."""
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            parsed = urlparse(origin)
            if (parsed.hostname or "") not in (
                    "127.0.0.1", "localhost", "::1"):
                return False
            try:
                if parsed.port is None:
                    return True
                return parsed.port == self.server.server_address[1]
            except ValueError:
                return False

        def do_POST(self):
            if not self._host_ok():
                return
            if not self._origin_ok():
                self._json(403, {"error": "cross-origin POST "
                                          "refused"})
                return
            # Companion control: the API is JSON-only. A
            # cross-site form post cannot set this content type
            # without a preflight the portal never grants, so
            # requiring it closes the simple-request vector
            # even where an Origin header is absent. The
            # portal's own scripts always send it.
            ctype = (self.headers.get("Content-Type") or "")
            ctype = ctype.split(";")[0].strip().lower()
            if ctype != "application/json":
                self._json(400, {"error": "POSTs must be "
                                          "application/json"})
                return
            parsed = urlparse(self.path)
            try:
                body = self._read_json_body()
                if parsed.path == "/api/launch":
                    self._launch_response(body)
                elif parsed.path == "/api/desk/rule":
                    self._rule_response(body)
                elif parsed.path == "/api/backup":
                    self._launch_response({"action": "backup"})
                elif parsed.path.startswith("/api/jobs/"):
                    parts = parsed.path.strip("/").split("/")
                    if len(parts) != 4:
                        self._json(404, {"error": "not found"})
                        return
                    job = self._job_or_404(parts[2])
                    if job is None:
                        return
                    if parts[3] == "stop":
                        job.stop()
                        self._json(200, {"state": job.state})
                    elif parts[3] == "resume":
                        if not job.claim_resume():
                            self._json(400, {
                                "error": "cannot resume this run "
                                         "— no checkpoints for it, "
                                         "or it was already "
                                         "resumed"})
                            return
                        new_job = ctx.resume(job)
                        self._json(200, {"job_id": new_job.id,
                                         "state": new_job.state})
                    else:
                        self._json(404, {"error": "not found"})
                else:
                    self._json(404, {"error": "not found"})
            except PortalError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                print(f"portal: request failed: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                self._json(500, {
                    "error": f"internal error ({type(e).__name__})"})

        def _stream_job(self, job):
            """SSE for one run page: the job snapshot as an event
            whenever it changes, a heartbeat comment otherwise,
            and a final event when the job settles. The pollable
            /api/jobs/<id> carries the identical snapshot — the
            stream is a latency nicety, never the only channel
            (§3's fallback rule)."""
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            last = ""
            idle = 0.0
            while True:
                text = json.dumps(job.snapshot(),
                                  ensure_ascii=False)
                if text != last:
                    last = text
                    idle = 0.0
                    frame = f"data: {text}\n\n"
                else:
                    idle += 0.5
                    if idle < 15.0 and job.state in (
                            "queued", "running"):
                        time.sleep(0.5)
                        continue
                    frame = ": heartbeat\n\n"
                    idle = 0.0
                try:
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError,
                        OSError):
                    return
                if job.state not in ("queued", "running"):
                    return
                time.sleep(0.25)

        def do_GET(self):
            if not self._host_ok():
                return
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._send(200, _page(
                        "Control Room", _render_home(ctx.home()), ctx),
                        "text/html; charset=utf-8")
                elif parsed.path == "/desk":
                    self._send(200, _page(
                        "The Desk", _render_desk(ctx.desk()), ctx,
                        extra_script=_DESK_JS),
                        "text/html; charset=utf-8")
                elif parsed.path == "/api/desk":
                    self._json(200, ctx.desk())
                elif parsed.path == "/api/desk/claim":
                    qs = parse_qs(parsed.query)
                    claim = desk_claim(
                        ctx.learn_dir, qs.get("work_key", [""])[0],
                        qs.get("claim_key", [""])[0])
                    if claim is None:
                        self._json(404, {
                            "error": "no such staged claim"})
                        return
                    self._json(200, {
                        "claim": claim,
                        "text": ctx.explain_claim(
                            claim["work_key"], claim["key"])})
                elif parsed.path == "/api/desk/item":
                    raw = parse_qs(parsed.query).get("id", [""])[0]
                    try:
                        queue_id = int(raw)
                    except (TypeError, ValueError):
                        queue_id = None
                    item = uncertainty_item(
                        ctx.learn_dir, queue_id) \
                        if queue_id is not None else None
                    if item is None:
                        self._json(404, {
                            "error": f"no pending item: {raw}"})
                        return
                    self._json(200, {"item": item})
                elif parsed.path == "/ingest":
                    self._send(200, _page(
                        "Ingest", _render_ingest(ctx), ctx,
                        extra_script=_INGEST_JS),
                        "text/html; charset=utf-8")
                elif parsed.path == "/launch":
                    self._send(200, _page(
                        "Run", _render_launch(ctx), ctx,
                        extra_script=_LAUNCH_JS),
                        "text/html; charset=utf-8")
                elif parsed.path.startswith("/run/"):
                    job = self._job_or_404(
                        parsed.path[len("/run/"):])
                    if job is None:
                        return
                    main_html, script = _render_run(job)
                    self._send(200, _page(
                        job.label, main_html, ctx,
                        extra_script=script),
                        "text/html; charset=utf-8")
                elif parsed.path == "/cover":
                    name = parse_qs(parsed.query).get(
                        "name", [""])[0]
                    try:
                        path = _picker_file(ctx, name)
                    except PortalError:
                        self._json(404, {"error": "no such file"})
                        return
                    found = epub_cover(path, ctx.cover_cache_dir)
                    if found is None:
                        self._json(404, {"error": "no cover"})
                        return
                    data, ctype = found
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control",
                                     "private, max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                elif parsed.path == "/api/home":
                    self._json(200, ctx.home())
                elif parsed.path == "/api/explain":
                    query = parse_qs(parsed.query).get("work", [""])[0]
                    fingerprint = resolve_work(ctx.learn_dir, query)
                    if fingerprint is None:
                        self._json(404, {
                            "error": f"no such work: {query}"})
                        return
                    self._json(200, {
                        "work": fingerprint,
                        "text": ctx.explain(fingerprint)})
                elif parsed.path == "/api/matrix":
                    query = parse_qs(parsed.query).get(
                        "work", [""])[0]
                    payload, err = ctx.work_matrix(query)
                    if payload is None:
                        self._json(404, {"error": err})
                        return
                    self._json(200, payload)
                elif parsed.path.startswith("/matrix/"):
                    info = ctx.work_info(
                        parsed.path[len("/matrix/"):])
                    if info is None:
                        self._json(404, {
                            "error": "no such work"})
                        return
                    payload, _err = ctx.work_matrix(info[0])
                    main_html, script = _render_matrix_page(
                        info,
                        payload["run_id"] if payload else None)
                    self._send(200, _page(
                        f"{info[1] or info[2]} — last run's "
                        f"matrix", main_html, ctx,
                        extra_script=script),
                        "text/html; charset=utf-8")
                elif parsed.path == "/api/jobs":
                    ctx.poll_external()
                    self._json(200, {"jobs": [
                        job.brief() for job in
                        ctx.registry.jobs()]})
                elif parsed.path.startswith("/api/jobs/"):
                    parts = parsed.path.strip("/").split("/")
                    job = self._job_or_404(parts[2]) \
                        if len(parts) >= 3 else None
                    if job is None:
                        return
                    if len(parts) == 4 and parts[3] == "stream":
                        self._stream_job(job)
                    elif len(parts) == 4 and parts[3] == "matrix":
                        payload = job.matrix()
                        payload["bars"] = _matrix_bars(ctx)
                        self._json(200, payload)
                    elif len(parts) == 3:
                        self._json(200, job.snapshot())
                    else:
                        self._json(404, {"error": "not found"})
                else:
                    self._json(404, {"error": "not found"})
            except Exception as e:  # a broken surface must say so,
                # not hang the operator. The browser gets the
                # exception type only — full detail (which can
                # embed paths and argv) goes to the operator's
                # terminal, the side of the glass that owns it.
                print(f"portal: request failed: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                try:
                    self._json(500, {
                        "error": f"internal error "
                                 f"({type(e).__name__})"})
                except Exception:
                    pass

    server = _PortalHTTPServer((bind_host, port), Handler)
    server.daemon_threads = True
    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_portal_config(script_dir):
    """portal-config.json beside portal.py, when the operator
    has written one. Malformed config is a launch error worth
    naming, not a silent default — a typo'd backup directory
    would otherwise hide behind the strip's 'not configured'."""
    path = Path(script_dir) / "portal-config.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise PortalError(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise PortalError(f"{path} must hold a JSON object")
    return data


def main(argv=None):
    script_dir = Path(__file__).resolve().parent
    config = load_portal_config(script_dir)
    parser = argparse.ArgumentParser(
        description="Operator portal for the ebook processor "
                    "(slices S1 + S2 + S3: Control Room, launching, "
                    "run pages, the Desk).")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port to bind (default {DEFAULT_PORT}, "
                             f"or portal-config.json; the next free "
                             f"port is used when taken)")
    parser.add_argument("--learn-dir", default=None,
                        help="learned directory holding state.db / "
                             "corpus.db (default: the processor's "
                             "own ~/.ebook-processor/learned)")
    args = parser.parse_args(argv)
    port_start = args.port or config.get("port") or DEFAULT_PORT
    learn_dir = (args.learn_dir or config.get("learn_dir")
                 or Path.home() / ".ebook-processor" / "learned")
    ctx = PortalContext(learn_dir=learn_dir, script_dir=script_dir,
                        backup_dir=config.get("backup_dir"))
    # The bind host is not a setting. validate_bind_host is the
    # Q12 guard; LOOPBACK is the only value main will ever pass.
    host = validate_bind_host(LOOPBACK)
    port = pick_port(host, int(port_start))
    server = make_server(host, port, ctx)
    # flush=True: the URL line is the operator's only handle on
    # which port the Q11 fallback landed on; block-buffered
    # stdout (a redirected launch) must not sit on it.
    print(f"Portal: http://{LOOPBACK}:{port}/", flush=True)
    print(f"  learn dir: {ctx.learn_dir}", flush=True)
    print("  slices S1 + S2 + S3 — Home, launching, run pages, "
          "the Desk; Ctrl+C stops the portal.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPortal stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
