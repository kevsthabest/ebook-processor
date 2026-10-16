"""Operator portal for the ebook processor (PORTAL-SPEC.md, slice S1).

Run it beside process-portable.py:

    py portal.py [--port 8765] [--learn-dir DIR]

Slice S1 is the skeleton + Home (the Control Room): the health
strip, coverage cards, book table, recent runs, queue pointers,
and the running-now strip. The portal READS the processor's
stores directly (read-only SQLite) and writes nothing anywhere:
no store writes, no file writes, no launched processes except
the read-only --explain forensics report behind a book row.
Launching runs, the Desk, and the Matrices panel are later
slices (S2/S3/S4 in the spec) and are deliberately absent here.

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
import html
import json
import re
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
    CORPUS_PARSER_VERSION, _MERGE_TIER3_CAP).

    The portal mirrors the CLI's read models rather than
    importing the processor (a script, not a library); these
    three constants are the only facts it needs that live
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
    for fp, title, author, series, sidx, status, pv, nch in conn.execute(
            "SELECT fingerprint, title, author, series, "
            "series_index, status, parser_version, chapter_count "
            "FROM corpus_works ORDER BY title, fingerprint"):
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
                idx = int(sidx) if float(sidx).is_integer() else sidx
                series_label = f"{series} #{idx}"
        works.append({
            "fingerprint": fp, "title": title, "author": author,
            "series_label": series_label, "status": status,
            "parser_version": pv, "chapters": nch,
            "passages": n_pass, "vector_coverage": coverage,
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
        for path in Path(preview_dir).glob("*.json"):
            if path.name.endswith("-trope-map.json") \
                    or path.name.startswith("."):
                continue
            when = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S")
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
    strip = {
        "jobs": [],
        "decision_cap": cap if isinstance(cap, int) else 50,
        "note": ("Only runs launched from this portal appear here "
                 "— run launching arrives in slice S2. A run "
                 "started in a terminal does not appear; its "
                 "effects show as ordinary state changes on "
                 "refresh."),
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
            out.append(f"<p>{_esc(job)}</p>")
    else:
        out.append("<p class='muted'>Nothing running under the "
                   "portal.</p>")
    out.append(
        f"<p class='muted'>Decision-call budget: "
        f"{strip['decision_cap']} per book — live spend arrives "
        f"with the run-event emission (S0).</p>")
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
                   "<th>Last activity</th></tr></thead><tbody>")
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
            out.append(
                f"<tr data-fp='{_esc(w['fingerprint'])}'>"
                f"<td>{_esc(w['title'])}</td>"
                f"<td>{_esc(w['author'])}</td>"
                f"<td>{_esc(w['series_label'] or '')}</td>"
                f"<td>{_chip(w['status'])}</td>"
                f"<td>{parser_cell}</td>"
                f"<td>{_esc(w['chapters'])}</td>"
                f"<td>{_esc(w['passages'])}</td>"
                f"<td>{cov}</td><td>{activity_cell}</td></tr>")
        out.append("</tbody></table>")
        out.append("<p class='note'>Select a book row for its "
                   "forensics report (--explain).</p>")
        out.append("<pre id='explain' hidden></pre>")
    else:
        out.append("<p class='muted'>No works in the corpus yet. "
                   "Ingest from the terminal with --ingest; "
                   "launching from the portal arrives in S2.</p>")
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
"""

_PAGE_JS = """
// Read-only dashboard refresh: re-render <main> from the server
// every 30 s while the tab is visible. The server is the only
// renderer, so a polled page can never drift from a fresh load.
setInterval(async () => {
  if (document.visibilityState !== 'visible') return;
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
// Book rows open the work's forensics: the one CLI shell-out in
// S1, and it is the read-only --explain report.
function wireRows() {
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
}
wireRows();
"""


def _page(title, main_html, ctx):
    version = source_constant(ctx.script_dir / "process-portable.py",
                              "PORTABLE_VERSION") or "?"
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, "
        f"initial-scale=1'><title>{_esc(title)}</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        "<header class='top'><h1>Processor Control Room</h1>"
        "<nav><a href='/'>Home</a><a href='/desk'>The Desk</a></nav>"
        f"<span class='meta'>ebook-processor v{_esc(version)} · "
        "loopback only</span></header>"
        f"<main>{main_html}</main>"
        f"<script>{_PAGE_JS}</script></body></html>")


_DESK_HTML = (
    "<h2>The Desk</h2><p class='muted'>The ruling queue arrives "
    "in slice S3. Home's queue pointers already count it; the "
    "queue itself — today's trope batch, parked escalations, "
    "impulse proposals — is built on the staging and uncertainty "
    "stores in S3.</p>")


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class PortalContext:
    """Everything a request needs: where the stores live, where
    the processor script lives, and the configured backup dir.
    Held explicitly (not in globals) so tests can point the
    server at fixture stores."""

    def __init__(self, learn_dir, script_dir, backup_dir=None):
        self.learn_dir = Path(learn_dir)
        self.script_dir = Path(script_dir)
        self.backup_dir = Path(backup_dir) if backup_dir else None

    def home(self):
        return home_data(self.learn_dir, self.script_dir,
                         self.backup_dir)

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


def make_server(host, port, ctx):
    """Build (but do not start) the portal server. The bind host
    passes validate_bind_host here as well as in main(), so the
    loopback guarantee holds for any caller, not just the CLI
    entry point."""
    host = validate_bind_host(host)
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

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._send(200, _page(
                        "Control Room", _render_home(ctx.home()), ctx),
                        "text/html; charset=utf-8")
                elif parsed.path == "/desk":
                    self._send(200, _page("The Desk", _DESK_HTML, ctx),
                               "text/html; charset=utf-8")
                elif parsed.path == "/api/home":
                    self._send(200, json.dumps(ctx.home(),
                                               ensure_ascii=False),
                               "application/json")
                elif parsed.path == "/api/explain":
                    query = parse_qs(parsed.query).get("work", [""])[0]
                    fingerprint = resolve_work(ctx.learn_dir, query)
                    if fingerprint is None:
                        self._send(404, json.dumps(
                            {"error": f"no such work: {query}"}),
                            "application/json")
                        return
                    self._send(200, json.dumps(
                        {"work": fingerprint,
                         "text": ctx.explain(fingerprint)},
                        ensure_ascii=False), "application/json")
                else:
                    self._send(404, json.dumps({"error": "not found"}),
                               "application/json")
            except Exception as e:  # a broken surface must say so,
                self._send(500, json.dumps(  # not hang the operator
                    {"error": f"{type(e).__name__}: {e}"}),
                    "application/json")

    server = ThreadingHTTPServer((bind_host, port), Handler)
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
                    "(slice S1: Home / Control Room, read-only).")
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
    print("  slice S1 — Home is read-only; Ctrl+C stops the portal.",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPortal stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
