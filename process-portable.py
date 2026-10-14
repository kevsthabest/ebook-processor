#!/usr/bin/env python3
"""
Ebook processor for Cozy Libram - portable version for Windows/Linux/Mac.

Extracts tropes, trigger warnings, and characters from EPUB/MOBI/AZW3 files
and writes them to Supabase.

Setup:
  1. Install Python 3.8+
  2. pip install mobi            (only needed for .mobi / .azw3)
  3. Copy config.example.json to config.json and fill in your credentials
  4. Drop ebook files into the import/ folder
  5. Run: python process-portable.py

Usage:
  python process-portable.py [--llm ollama|openrouter|openai] [--dry-run] [--preview] [--watch]

  --dry-run   Run extraction, print a summary, write nothing, move no files.
  --preview   Run extraction, save preview/<book>.json + .md, write nothing to
              Supabase (credentials not required), move no files.
  --debug     With --preview/--dry-run, save raw LLM output of failed chunks
              to preview/debug/ for diagnosis.
  --dedupe    Merge duplicate characters (name normalization + embeddings)
              before writing. Needs embed_url/embed_model in config.json.
  --watch     Re-scan the import folder every 60 seconds.
  --llm       Override the "llm" setting from config.json.

Config (config.json):
{
  "supabase_url": "https://<ref>.supabase.co",
  "supabase_key": "<key>",
  "openrouter_key": "<key>",                 // for --llm openrouter
  "openrouter_model": "qwen/qwen3-27b",      // verify this model ID exists
  "ollama_host": "http://localhost:11434",
  "ollama_model": "qwen2.5:7b-instruct",
  "openai_base_url": "http://127.0.0.1:8888/v1",  // for --llm openai (Unsloth etc.)
  "openai_model": "qwen2.5-2k:latest",
  "embed_url": "http://127.0.0.1:8888/v1",      // for --dedupe (OpenAI-compatible /v1/embeddings)
  "embed_model": "",
  "dedupe_threshold": 0.85,
  "import_dir": "./import",
  "llm": "openai"
}
Any key can also be set via an env var named EBOOK_<KEY>, e.g. EBOOK_SUPABASE_KEY.
Keep config.json out of version control.
"""

PORTABLE_VERSION = "2.9.8"

import argparse
import difflib
import json
import math
import os
import posixpath
import re
import shutil
import struct
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote

# Windows consoles default to cp1252 and choke on many titles/emoji.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- Config ---
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

DEFAULT_CONFIG = {
    "supabase_url": "",
    "supabase_key": "",
    "openrouter_key": "",
    "openrouter_api_key": "",  # decision-model key for provider 'openrouter' (or OPENROUTER_API_KEY env)
    "ollama_host": "http://localhost:11434",
    "ollama_model": "qwen2.5:7b-instruct",
    "import_dir": str(SCRIPT_DIR / "import"),
    "llm": "ollama",
    "openrouter_model": "qwen/qwen3-27b",
    "openai_base_url": "http://127.0.0.1:8888/v1",
    "openai_model": "",
    "sample_rate": 1,
    "sample_edges": 2,
    "batch_size": 1,
    "debug": False,
    "dedupe": False,
    "embed_url": "",
    "embed_model": "",
    "dedupe_threshold": 0.85,
    "llm_max_tokens": 8000,
    "llm_system_prefix": "",
    "llm_response_format": "json_object",  # or "none"
    "llm_json_schema": False,  # try response_format json_schema first, fall back on 400
    # (default false: json_schema silently truncates on some servers;
    #  json_object is the proven mode)
    "llm_think_suffix": "",  # appended to system prompt; e.g. "/no_think" for Qwen3
    # (note: /no_think does NOT work on Qwen3.5; use llm_enable_thinking)
    "llm_enable_thinking": None,  # None=auto, True/False -> extra_body
    # chat_template_kwargs.enable_thinking (Qwen3.5 mechanism; servers that
    # don't support it ignore extra_body harmlessly)
    "v2_think_suffix_a": None,  # Call A (identity) override; None = use llm_think_suffix
    "v2_think_suffix_b": None,  # Call B (content) override; None = use llm_think_suffix
    "v2_enable_thinking_a": None,  # Call A override; None = use llm_enable_thinking
    "v2_enable_thinking_b": None,  # Call B override; None = use llm_enable_thinking
    "trope_map_threshold": 0.78,  # cosine threshold for catalog mapping
    "trope_map_cache": "trope-vectors.json",  # cached catalog embeddings
    "v2_prompt_cache": False,  # put chapter first for llama.cpp KV cache reuse
    "proposer_user_id": "",  # Supabase users.id for trope_proposals.proposed_by
    "pipeline": "legacy",  # or "v2": chapter-level map/reduce (--pipeline v2)
    "v2_max_chapter_chars": 16000,  # ~4k tokens; keep well under your
    # server's context window minus prompt (~1k) minus output (~3k)
}


# Config keys that must be int (env overrides arrive as strings).
_INT_CONFIG_KEYS = {"sample_rate", "sample_edges", "batch_size", "llm_max_tokens"}
# Config keys that are bools (EBOOK_DEDUPE=false must not be truthy).
_BOOL_CONFIG_KEYS = {"debug", "dedupe", "llm_enable_thinking", "llm_json_schema",
                     "v2_prompt_cache"}


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        # utf-8-sig tolerates the BOM that Windows Notepad adds
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    for k in cfg:
        env_k = "EBOOK_" + k.upper()
        if env_k in os.environ:
            v = os.environ[env_k]
            if k in _INT_CONFIG_KEYS:
                try:
                    v = int(v)
                except ValueError:
                    print(f"  Warning: {env_k}={v!r} is not an int; "
                          f"using default {cfg[k]}")
                    continue
            elif k in _BOOL_CONFIG_KEYS:
                v = v.strip().lower() in ("1", "true", "yes", "on")
            cfg[k] = v
    return cfg


CONFIG = load_config()
IMPORT_DIR = Path(CONFIG["import_dir"])
DONE_DIR = IMPORT_DIR / "done"
FAILED_DIR = IMPORT_DIR / "failed"
CHUNK_CHARS = 8000
NUM_CTX = 8192            # must comfortably exceed CHUNK_CHARS/3 + prompt + output


# --- Auto-update helpers (--auto-update) -------------------------------------
# All use subprocess with timeouts and degrade gracefully when git is missing
# or the directory is not a git repo. PowerShell-compatible (no ANSI codes).

_GIT_REMOTE = "origin"
_GIT_BRANCH = "master"


def _git_commit_sha():
    """Return the current HEAD commit SHA, or None if unavailable."""
    import subprocess as _sp
    try:
        r = _sp.run(["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=10,
                    cwd=str(SCRIPT_DIR))
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def _check_for_updates():
    """Check origin/master for new commits.

    Returns (behind_count, error_str). error_str is None on success.
    """
    import subprocess as _sp
    try:
        r = _sp.run(["git", "fetch", _GIT_REMOTE],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(SCRIPT_DIR))
        if r.returncode != 0:
            return 0, "fetch failed: %s" % r.stderr.strip()[:100]
        r = _sp.run(["git", "rev-list", "--count",
                     "HEAD..%s/%s" % (_GIT_REMOTE, _GIT_BRANCH)],
                    capture_output=True, text=True, timeout=10,
                    cwd=str(SCRIPT_DIR))
        if r.returncode != 0:
            return 0, "rev-list failed: %s" % r.stderr.strip()[:100]
        return int(r.stdout.strip()), None
    except FileNotFoundError:
        return 0, "git not installed"
    except _sp.TimeoutExpired:
        return 0, "git operation timed out"
    except Exception as e:
        return 0, str(e)[:100]


def _do_auto_update(preserve_flag=False):
    """Check for updates and pull if behind.

    On successful pull, verify the new code compiles, then re-exec the
    script and never returns. With preserve_flag=False (one-shot startup)
    the --auto-update flag is stripped to avoid a loop; with
    preserve_flag=True (--watch mode) it is kept so later cycles keep
    checking (the 60s sleep already prevents tight loops).
    Returns False to continue with the current version.
    """
    import subprocess as _sp
    behind, err = _check_for_updates()
    if err:
        print(f"  Auto-update check failed: {err}. "
              f"Continuing with current version.")
        return False
    if behind <= 0:
        print("  Already up to date.")
        return False
    print(f"  {behind} new commit(s) available. Pulling...")
    try:
        r = _sp.run(["git", "pull", "--ff-only", _GIT_REMOTE, _GIT_BRANCH],
                    capture_output=True, text=True, timeout=60,
                    cwd=str(SCRIPT_DIR))
    except FileNotFoundError:
        print("  Auto-update failed: git not installed. "
              "Continuing with current version.")
        return False
    except _sp.TimeoutExpired:
        print("  Auto-update failed: pull timed out. "
              "Continuing with current version.")
        return False
    except Exception as e:
        print(f"  Auto-update failed: {e}. Continuing with current version.")
        return False
    if r.returncode != 0:
        print(f"  Auto-update failed: {r.stderr.strip()[:200]}. "
              f"Continuing with current version.")
        return False
    # Never exec into code that does not even compile: stay on the
    # currently-loaded (working) version instead.
    try:
        c = _sp.run([sys.executable, "-m", "py_compile",
                     str(Path(__file__).resolve())],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(SCRIPT_DIR))
    except Exception as e:
        print(f"  Auto-update: compile check could not run ({e}). "
              f"Continuing with current version.")
        return False
    if c.returncode != 0:
        print("  Auto-update: pulled code failed py_compile. "
              "Continuing with current version; run 'git log' and "
              "'git reset --hard %s/%s' to recover." %
              (_GIT_REMOTE, _GIT_BRANCH))
        return False
    print(f"  Updated to latest ({behind} new commits). Restarting...")
    sys.stdout.flush()
    new_args = [a for a in sys.argv[1:]
                if preserve_flag or a != "--auto-update"]
    os.execv(sys.executable, [sys.executable, sys.argv[0]] + new_args)
SAMPLE_RATE = 1           # 1 = every chunk, 2 = every other, 3 = every third, etc.
SAMPLE_EDGES = 2          # always process this many chunks from start and end
BATCH_SIZE = 1            # parallel LLM requests (4 for Unsloth)
MAX_FAILED_CHUNK_RATIO = 0.5
VALID_ROLES = ("protagonist", "antagonist", "supporting", "minor")
_DEBUG_TAG = None  # per-file label for --debug dumps, set in process_file

for d in (IMPORT_DIR, DONE_DIR, FAILED_DIR):
    d.mkdir(parents=True, exist_ok=True)


# --- Text extraction ---
class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.text = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in self.BREAK:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.text.append(data)

    def get_text(self):
        return "".join(self.text)


def html_to_text(html):
    p = TextExtractor()
    p.feed(html)
    text = re.sub(r"[ \t]+", " ", p.get_text())
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def read_opf(z):
    """Return (ordered_content_names or None, title, author) using the OPF spine."""
    try:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(
            (e.attrib["full-path"] for e in container.iter()
             if _local(e.tag) == "rootfile" and "full-path" in e.attrib), None)
        if not opf_path:
            return None, None, None, {"raw": []}
        root = ET.fromstring(z.read(opf_path))
    except Exception as e:
        print(f"  Warning: could not parse OPF ({e}); falling back to filename order")
        return None, None, None, {"raw": []}

    title = author = None
    identifiers = []
    series = series_index = None
    manifest, spine = {}, []
    for e in root.iter():
        name = _local(e.tag)
        if name == "title" and title is None and (e.text or "").strip():
            title = e.text.strip()
        elif name == "creator" and author is None and (e.text or "").strip():
            author = e.text.strip()
        elif name == "identifier" and (e.text or "").strip():
            identifiers.append(e.text.strip())
        elif name == "meta":
            # Calibre series metadata: <meta name="calibre:series"
            # content="Daemon"/> and calibre:series_index.
            _mname = (e.attrib.get("name") or "").strip()
            _mcontent = (e.attrib.get("content") or "").strip()
            if _mname == "calibre:series" and _mcontent and series is None:
                series = _mcontent
            elif _mname == "calibre:series_index" and _mcontent \
                    and series_index is None:
                try:
                    series_index = float(_mcontent)
                except ValueError:
                    pass
        elif name == "item" and "id" in e.attrib and "href" in e.attrib:
            manifest[e.attrib["id"]] = (e.attrib["href"], e.attrib.get("properties", ""))
        elif name == "itemref" and "idref" in e.attrib:
            spine.append(e.attrib["idref"])

    opf_dir = posixpath.dirname(opf_path)
    names = []
    for idref in spine:
        if idref not in manifest:
            continue
        href, props = manifest[idref]
        if "nav" in props.split():
            continue
        names.append(posixpath.normpath(posixpath.join(opf_dir, unquote(href.split("#")[0]))))
    return (names or None), title, author, {"raw": identifiers,
                                           "series": series,
                                           "series_index": series_index}


def _validate_isbn(isbn):
    """Validate an ISBN string. Returns normalized digits-only ISBN or None.

    Strips hyphens, spaces, and urn:isbn: prefixes. Requires 10 or 13
    digits and a valid check digit (mod-11 for ISBN-10, mod-10 for
    ISBN-13). ISBN-10 may end in 'X' (check digit 10). The returned value
    keeps its original length (10 stays 10, 13 stays 13); callers that
    need ISBN-13 should convert via _clean_isbn.
    """
    s = re.sub(r"(?i)^urn:isbn:", "", (isbn or "").strip())
    s = re.sub(r"[\s\-]", "", s).upper()
    if len(s) == 13 and s.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(s))
        return s if total % 10 == 0 else None
    if len(s) == 10 and s[:9].isdigit() and s[9] in "0123456789X":
        total = sum(int(s[i]) * (10 - i) for i in range(9))
        check_val = 10 if s[9] == "X" else int(s[9])
        return s if (total + check_val) % 11 == 0 else None
    return None


def _clean_isbn(s):
    """Normalize an identifier string to digits-only ISBN-13, or None.
    Handles urn:isbn: prefixes, hyphens/spaces, and ISBN-10 -> ISBN-13.
    Check digits are validated; invalid ISBNs return None."""
    valid = _validate_isbn(s)
    if not valid:
        return None
    if len(valid) == 13:
        return valid
    # ISBN-10 -> ISBN-13 (check digit already validated above).
    core = "978" + valid[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def _pick_identifiers(raw_list, series=None, series_index=None):
    """Return {'isbn': <13-digit or None>, 'asin': <str or None>, 'raw': [...],
    'series': <str or None>, 'series_index': <float or None>}."""
    out = {"isbn": None, "asin": None, "raw": list(raw_list or []),
           "series": series, "series_index": series_index}
    for r in out["raw"]:
        isbn = _clean_isbn(r)
        if isbn and not out["isbn"]:
            out["isbn"] = isbn
            continue
        m = re.match(r"(?i)^(B[0-9A-Z]{9})$", (r or "").strip())
        if m and not out["asin"]:
            out["asin"] = m.group(1).upper()
    return out


def _ol_request(url, timeout=10):
    """GET a URL from Open Library and parse the JSON response.

    Sets the ebook-processor User-Agent header and applies the timeout.
    Returns the parsed dict, or None on any failure (network error,
    HTTP error, invalid JSON). Callers must URL-quote query parameters
    with urllib.parse.quote before building the URL.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ebook-processor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        # Notice, not silence: an outage must be distinguishable
        # from "no match" (callers route books on the difference).
        print(f"  Open Library request failed ({e}); treating as no match")
        return None


def _lookup_isbn_openlibrary(title, author):
    """Look up an ISBN via Open Library search API. Returns cleaned ISBN-13 or None.

    Only returns a result on a confident match: the first result's title must
    match (normalized) the given title, tolerating series prefixes like
    "[Series 02] - Title". Network call wrapped in try/except —
    any failure returns None (caller falls through to skip behavior).
    """
    if not title:
        return None
    q = (f"https://openlibrary.org/search.json?title={quote(title or '', safe='')}"
         f"&author={quote(author or '', safe='')}"
         f"&fields=title,isbn&limit=1")
    data = _ol_request(q)
    if not data:
        return None
    try:
        docs = data.get("docs") or []
        if not docs:
            return None
        doc = docs[0]
        # Confident match: normalized titles agree, tolerating a series
        # prefix like "[Daemon 02] - " on the local title.
        ol_title = _tnorm(doc.get("title") or "")
        local_title = _tnorm(title)
        local_bare = re.sub(r"^\[.*?\]\s*[-–:]\s*", "", local_title).strip()
        if not ol_title or ol_title not in (local_title, local_bare):
            # Also accept when the OL title appears as a whole word-phrase
            # within the local title (handles "Freedom" vs "[Daemon 02] - FreedomTM").
            if not (len(ol_title) > 3 and ol_title in local_title):
                return None
        for raw in doc.get("isbn") or []:
            cleaned = _clean_isbn(raw)
            if cleaned:
                return cleaned
        return None
    except Exception:
        return None


def _resolve_book_isbn(identifiers, title, author):
    """Resolve the book's ISBN by priority. Mutates identifiers in place.

    Priority:
      1. --isbn flag (CONFIG["isbn_override"]) -> source "flag"
      2. EPUB metadata (identifiers["isbn"])   -> source "epub"
      3. Open Library auto-lookup              -> source "openlibrary"
      4. none                                  -> source "none"

    Returns (isbn_or_None, source). When found via flag or Open Library,
    identifiers["isbn"] is set so downstream work resolution uses it.
    """
    override = CONFIG.get("isbn_override", "")
    if override:
        valid = _validate_isbn(override)
        if not valid:
            print(f"  ERROR: --isbn value {override!r} is not a valid ISBN "
                  f"(bad length or check digit); ignoring override.")
        else:
            # Normalize through _clean_isbn so the work key is always ISBN-13.
            normalized = _clean_isbn(override)
            identifiers["isbn"] = normalized
            return normalized, "flag"
    if identifiers.get("isbn"):
        # EPUB-sourced; _clean_isbn already validated the check digit, but
        # re-validate defensively since identifiers may be populated directly.
        valid = _validate_isbn(identifiers["isbn"])
        if valid:
            return _clean_isbn(identifiers["isbn"]), "epub"
        print(f"  WARNING: EPUB ISBN {identifiers['isbn']!r} failed check-digit "
              f"validation; ignoring.")
        identifiers["isbn"] = None
    # Auto-lookup before giving up.
    auto = _lookup_isbn_openlibrary(title, author)
    if auto:
        identifiers["isbn"] = auto
        print(f"  Auto-resolved ISBN {auto} via Open Library")
        return auto, "openlibrary"
    return None, "none"


def _handle_missing_isbn(fpath, preview=False, dry_run=False):
    """Print skip/warning for a book with no ISBN. Returns True if processing
    may continue (i.e. --allow-no-isbn), False if the book should be skipped.

    The --rename-no-isbn rename is a filesystem write: it must never
    fire in preview or dry-run mode (both promise files are not
    moved). In those modes it prints what WOULD happen instead; the
    skip decision itself is unchanged."""
    if CONFIG.get("allow_no_isbn"):
        print("")
        print("  \u26a0 WARNING: No ISBN found (metadata, --isbn, and Open Library lookup all failed).")
        print("    Work will be matched by title/author only, which may create duplicates.")
        print("")
        return True
    print(f"  \u23ed Skipping '{fpath.name}': no ISBN found. "
          f"Use --isbn to override or --allow-no-isbn to process anyway.")
    if CONFIG.get("rename_no_isbn"):
        new_name = fpath.parent / f"NO-ISBN-{fpath.name}"
        if preview or dry_run:
            mode = "preview" if preview else "dry-run"
            print(f"  Rename skipped in {mode} mode: would rename to "
                  f"'{new_name.name}'")
        else:
            try:
                if not new_name.exists():
                    fpath.rename(new_name)
                    print(f"  Renamed to '{new_name.name}'")
                else:
                    print(f"  Rename skipped: '{new_name.name}' already exists")
            except Exception as e:
                print(f"  Rename failed: {e}")
    return False


# --- Series detection ---
# Priority: 1. --series/--series-position flags
#            2. EPUB calibre:series / calibre:series_index metadata
#            3. Filename pattern: [Series 02] - Title  or  (Series #2)
#            4. Open Library work record (best-effort)
_SERIES_FILENAME_RES = (
    re.compile(r"^\[(.+?)\s+(\d+(?:\.\d+)?)\]\s*[-–:]\s*"),
    re.compile(r"^\((.+?)\s+#(\d+(?:\.\d+)?)\)"),
)


def _parse_series_from_filename(fname):
    """Extract (series_name, position) from a filename stem.

    Handles '[Daemon 02] - Freedom' and '(Daemon #2) - Freedom'.
    Returns (None, None) when no pattern matches.
    """
    stem = fname.stem if hasattr(fname, "stem") else str(fname).rsplit(".", 1)[0]
    for rx in _SERIES_FILENAME_RES:
        m = rx.match(stem.strip())
        if m:
            name = m.group(1).strip()
            try:
                pos = float(m.group(2))
            except ValueError:
                pos = None
            if name:
                return name, pos
    return None, None


def _lookup_series_openlibrary(title, author):
    """Best-effort series lookup via Open Library. Returns
    (series_name_or_None, position_or_None). Any failure returns (None, None);
    series data in Open Library is sparse, so this is a last resort."""
    if not title:
        return None, None
    q = (f"https://openlibrary.org/search.json?title={quote(title or '', safe='')}"
         f"&author={quote(author or '', safe='')}"
         f"&fields=title,series&limit=1")
    data = _ol_request(q)
    if not data:
        return None, None
    try:
        docs = data.get("docs") or []
        if not docs:
            return None, None
        series = docs[0].get("series") or []
        if not series:
            return None, None
        # series entries look like "Daemon, 2" or just "Daemon".
        first = str(series[0]).strip()
        m = re.match(r"^(.*?)[,:\s]+(\d+(?:\.\d+)?)$", first)
        if m:
            return m.group(1).strip() or None, float(m.group(2))
        return first or None, None
    except Exception:
        return None, None


def _detect_series(fpath, identifiers, title, author):
    """Detect (series_name, position, source) by priority.

    Sources: 'flag' > 'epub' > 'filename' > 'openlibrary' > 'none'.
    identifiers may carry 'series'/'series_index' from EPUB calibre metadata.
    """
    # 1. CLI flags.
    flag_name = (CONFIG.get("series_override") or "").strip()
    if flag_name:
        flag_pos = CONFIG.get("series_position_override")
        return flag_name, flag_pos, "flag"
    # 2. EPUB calibre metadata.
    epub_series = (identifiers.get("series") or "").strip() if identifiers else ""
    if epub_series:
        epub_pos = identifiers.get("series_index")
        return epub_series, epub_pos, "epub"
    # 3. Filename pattern.
    fn_series, fn_pos = _parse_series_from_filename(fpath.name if hasattr(fpath, "name") else fpath)
    if fn_series:
        return fn_series, fn_pos, "filename"
    # 4. Open Library (best-effort, may be slow — shares the 10s timeout).
    ol_series, ol_pos = _lookup_series_openlibrary(title, author)
    if ol_series:
        return ol_series, ol_pos, "openlibrary"
    return None, None, "none"

def extract_mobi_identifiers(path):
    """Read ISBN (EXTH 104) / ASIN (EXTH 113) from a MOBI/AZW3 header. Stdlib."""
    ids = {"isbn": None, "asin": None, "raw": []}
    try:
        data = Path(path).read_bytes()
        if len(data) < 200:
            return ids
        rec0 = struct.unpack(">I", data[78:82])[0]
        if rec0 + 132 > len(data) or data[rec0 + 16:rec0 + 20] != b"MOBI":
            return ids
        mobi_len = struct.unpack(">I", data[rec0 + 20:rec0 + 24])[0]
        if not (struct.unpack(">I", data[rec0 + 128:rec0 + 132])[0] & 0x40):
            return ids
        exth = rec0 + mobi_len
        if data[exth:exth + 4] != b"EXTH":
            return ids
        nrec = struct.unpack(">I", data[exth + 8:exth + 12])[0]
        pos = exth + 12
        for _ in range(min(nrec, 200)):
            if pos + 8 > len(data):
                break
            rtype, rlen = struct.unpack(">II", data[pos:pos + 8])
            if rlen < 8 or pos + rlen > len(data):
                break
            val = data[pos + 8:pos + rlen].decode("utf-8", errors="ignore").strip()
            if val:
                ids["raw"].append(val)
                if rtype == 104 and not ids["isbn"]:
                    ids["isbn"] = _clean_isbn(val)
                elif rtype == 113 and not ids["asin"]:
                    ids["asin"] = val
            pos += rlen
    except Exception as e:
        print(f"  MOBI identifier extraction failed for "
              f"{Path(path).name} ({e}); no ISBN/ASIN read")
    return ids


def extract_headings(html, limit=1):
    """Return up to `limit` heading texts (h1-h3) from raw HTML, for chapter labels."""
    heads = []
    for m in re.finditer(r"<h[1-3][^>]*>(.*?)</h[1-3]>", html, re.DOTALL | re.IGNORECASE):
        t = re.sub(r"<[^>]+>", "", m.group(1))
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            heads.append(t)
        if len(heads) >= limit:
            break
    return heads


def extract_epub_units(epub_path):
    """Return ([{"spine", "label", "text"}], title, author, identifiers).

    Per-spine units in reading order, front matter included. Use
    split_chapters() for clean chapter-sized units; extract_epub_text()
    keeps the legacy joined-string behavior.
    """
    units = []
    with zipfile.ZipFile(epub_path, "r") as z:
        available = set(z.namelist())
        names, title, author, opf_ids = read_opf(z)
        if names is None:
            names = sorted((n for n in available
                            if n.lower().endswith((".html", ".xhtml", ".htm"))),
                           key=_natural_key)
        for fname in names:
            if fname not in available:
                continue
            try:
                content = z.read(fname).decode("utf-8", errors="ignore")
                text = html_to_text(content)
                if len(text) > 100:
                    heads = extract_headings(content)
                    label = heads[0][:80] if heads else posixpath.splitext(
                        posixpath.basename(fname))[0]
                    units.append({"spine": fname, "label": label, "text": text})
            except Exception as e:
                print(f"  Warning: {fname}: {e}")
    return units, title, author, _pick_identifiers(opf_ids["raw"],
                                                 series=opf_ids.get("series"),
                                                 series_index=opf_ids.get("series_index"))


def extract_epub_text(epub_path):
    units, title, author, identifiers = extract_epub_units(epub_path)
    return "\n\n".join(u["text"] for u in units), title, author, identifiers


# --- Chapter splitter (v2 pipeline; legacy chunking untouched) ---
# Front/back-matter heuristic, matched against the spine filename.
_SKIP_UNIT_RE = re.compile(
    r"copyright|(?<![a-z])toc(?![a-z])|table.?of.?contents|dedication|acknowledg|also.?by|"
    r"about.?(the.?)?author|title.?page|(?<![a-z])cover(?![a-z])|\bnav\b", re.IGNORECASE)

# Content-based front/back matter: skip units whose opening text matches.
# Catches what filename heuristics miss (e.g. "part0001.html" that's actually a TOC).
_FRONTMATTER_CONTENT_RE = re.compile(
    r"copyright|all rights reserved|isbn|also\s+by\s|dedication|"
    r"acknowledg(e?)ments|about\s+the\s+author|table\s+of\s+contents|"
    r"^contents\s*$",
    re.IGNORECASE)

def _is_frontmatter_content(text):
    """True if the opening looks like front/back matter.

    Requires 2+ distinct signals to avoid false-positiving on prose
    (e.g. "also by the door" in a real chapter). Checks first 2000 chars.
    """
    head = text[:2000].lower()
    signals = 0
    # "also by the author" as a full phrase is a strong signal (counts double);
    # bare "also by" needs a second signal (avoids "also by the door" in prose)
    if re.search(r"also\s+by\s+the\s+author", head):
        signals += 2
    for pat in (r"copyright", r"all rights reserved", r"isbn[\s:]*[0-9]",
                r"\bdedication\b", r"acknowledg(e?)ments",
                r"about\s+the\s+author", r"table\s+of\s+contents"):
        if re.search(pat, head, re.IGNORECASE):
            signals += 1
    return signals >= 2


def _is_toc_chapter(text):
    """True if a chapter-sized text block is a table of contents, not prose.

    A ToC is a title list: many short lines, almost no sentence
    punctuation. Its "content" is other chapters' titles, so feeding it to
    trope/trigger extraction invents claims from titles (e.g. a chapter
    titled "Mind Mapping" becoming the Mind Game trope).
    Conservative: dialogue-heavy prose has short lines but ends them in
    punctuation/quotes, so it is not flagged.
    """
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    if len(lines) < 12:
        return False
    short = sum(1 for l in lines if len(l) < 60)
    if short / len(lines) < 0.7:
        return False
    punct = sum(1 for l in lines
                if l[-1] in ".!?\"'" "\u201d\u2019" or "," in l or ";" in l)
    return punct / len(lines) < 0.3


# Front/back matter chapter tagging. The unit-drop heuristics above remove
# whole spine units before indexing, but front/back matter that survives
# them (generic spine names, single-signal content, fallback splits) used
# to be processed as story chapters — a dedication page produced a
# character named "Author", and About-the-Author bios became trope
# evidence. Tagging (not dropping) keeps positional slots: chapter
# indices feed chapter_spice, first_appearance, and trigger
# chapter_indices downstream, so tagged chapters are skipped for
# extraction/evidence but never removed from the sequence.
_FRONTMATTER_HEADING_RE = re.compile(
    r"^(?:dedications?|dedicated\s+to\b.*|acknowledg(?:e)?ments?|"
    r"about\s+the\s+author|"
    r"(?:also\s+by|books\s+by|other\s+books\s+by|more\s+by|"
    r"by\s+the\s+same\s+author)\b.*|"
    r"copyright(?:\s+page)?|colophon)$", re.IGNORECASE)
_FRONTMATTER_MAX_CHARS = 8000  # fm pages are short; real chapters are not
_FRONTMATTER_EDGE = 3          # only the first/last N chapters can be fm


def _fm_heading(line):
    """Normalize a candidate heading line, or None if it can't be one.

    A heading is short and carries no sentence punctuation; a prose line
    that merely starts with a front-matter word ("Dedicated to her, he
    raised the sword.") is not a heading.
    """
    h = re.sub(r"[-_]+", " ", (line or "").strip().lower()).strip(" .:! ")
    if not h or len(h) > 60 or "," in h or ";" in h:
        return None
    return h


def _is_frontmatter_chapter(label, text, index, n_chapters):
    """True if a chapter is front/back matter, not story content.

    Conservative — precision over recall, because a false tag loses a
    real chapter while a missed tag only wastes some extraction. ALL
    must hold: position (first/last _FRONTMATTER_EDGE chapters),
    heading (the label or the first text line is a standalone
    front/back-matter heading), and length (<= _FRONTMATTER_MAX_CHARS —
    back matter merged into a full-length final chapter must not take
    the real chapter down with it).
    """
    if not (index <= _FRONTMATTER_EDGE
            or index > n_chapters - _FRONTMATTER_EDGE):
        return False
    text = text or ""
    if len(text) > _FRONTMATTER_MAX_CHARS:
        return False
    lines = text.split("\n")
    first_line = next((l for l in lines if l.strip()), "")
    heads = [h for h in (_fm_heading(label), _fm_heading(first_line)) if h]
    if not any(_FRONTMATTER_HEADING_RE.match(h) for h in heads):
        return False
    # Merge-forward guard: a chapter heading later in the text means
    # front matter was merged into a real chapter — it stays content.
    if len(lines) > 1 and _CHAPTER_HEADING_RE.search("\n".join(lines[1:])):
        return False
    return True

# Chapter-detection fallback flag lives on _DIAG (threading.local) because
# --jobs runs books in a ThreadPoolExecutor. See _DIAG definition above.

MIN_CHAPTER_CHARS = 1500   # smaller units merge into the next one
MAX_CHAPTER_CHARS = 16000  # ~4k tokens; must leave room in the context window
# for the prompt + the model's thinking + JSON output. Override with
# v2_max_chapter_chars in config.json if your server has a larger context.


def _chapter_part(u, text, part):
    return {"spine": u["spine"],
            "label": u["label"] if part == 1 else f"{u['label']} (part {part})",
            "text": text}


def _fallback_heading_split(units):
    """Split units on _CHAPTER_HEADING_RE when spine structure is unusable.

    Used when an EPUB yields <5 units or one unit holds >60% of the text.
    Returns (new_units, found_headings). Keeps all chunks (including short
    ones and pre-heading text); the merge-forward logic in split_chapters
    handles small units. Only skips TOC-like chunks (many short lines).
    """
    out = []
    found = False
    for u in units:
        text = u["text"]
        matches = list(_CHAPTER_HEADING_RE.finditer(text))
        if len(matches) < 2:
            out.append(u)
            continue
        found = True
        bounds = [m.start() for m in matches] + [len(text)]
        # Keep text before first heading (don't silently delete it)
        pre = text[:bounds[0]].strip()
        if pre and len(pre) > 200:
            out.append({"spine": f"{u['spine']}#fb0",
                        "label": "Pre-heading", "text": pre})
        for i in range(len(matches)):
            chunk = text[bounds[i]:bounds[i + 1]].strip()
            if not chunk:
                continue
            # Skip TOC-like chunks: many short lines, little prose
            lines = chunk.split("\n")
            if len(lines) > 10 and sum(len(l) for l in lines) / len(lines) < 40:
                continue
            label = chunk.split("\n", 1)[0].strip()[:80]
            out.append({"spine": f"{u['spine']}#fb{i + 1}",
                        "label": label, "text": chunk})
    return out, found


def split_chapters(units):
    """Turn raw spine units into chapter-sized units for the v2 pipeline.

    - Drops front/back matter by filename heuristic; front/back matter
      that survives is tagged is_frontmatter (slots kept, see
      _is_frontmatter_chapter).
    - Merges units under MIN_CHAPTER_CHARS into the next unit.
    - Splits units over the configured max (v2_max_chapter_chars, default
      MAX_CHAPTER_CHARS) on paragraph boundaries.
    Returns [{"index", "label", "text"}] in reading order.
    """
    max_chars = CONFIG.get("v2_max_chapter_chars", MAX_CHAPTER_CHARS)
    kept = [u for u in units
            if not _SKIP_UNIT_RE.search(u["spine"])
            and not _is_frontmatter_content(u.get("text", ""))]
    # Fallback: if too few units survived, or one unit dominates the text
    # (common when a converter dumps the whole book in one HTML file),
    # split on chapter headings.
    if kept:
        total = sum(len(u["text"]) for u in kept)
        biggest = max(kept, key=lambda u: len(u["text"]))
        if len(kept) < 5 or (total and len(biggest["text"]) / total > 0.6):
            # Only flag as fallback if heading split actually found structure.
            # Short books with clean spines shouldn't be penalized.
            new_kept, found = _fallback_heading_split(kept)
            if found:
                _DIAG.chapter_detection_fallback = True
                print("!!! CHAPTER DETECTION FALLBACK - heading-based split, "
                      "results may be unreliable")
                kept = new_kept
    # Merge small units forward (label of the following, larger unit wins).
    merged = []
    pending = None
    for u in kept:
        if pending is not None:
            u = {"spine": u["spine"], "label": u["label"],
                 "text": pending["text"] + "\n\n" + u["text"]}
            pending = None
        if len(u["text"]) < MIN_CHAPTER_CHARS:
            pending = u
        else:
            merged.append(u)
    if pending is not None:
        if merged:
            merged[-1]["text"] += "\n\n" + pending["text"]
        else:
            merged.append(pending)
    # Split oversized units on paragraph boundaries.
    out = []
    for u in merged:
        if len(u["text"]) <= max_chars:
            out.append(u)
            continue
        cur, clen, part = [], 0, 1
        for p in u["text"].split("\n\n"):
            while len(p) > max_chars:  # hard-split giant paragraph
                if cur:
                    out.append(_chapter_part(u, "\n\n".join(cur), part))
                    part += 1
                    cur, clen = [], 0
                out.append(_chapter_part(u, p[:max_chars], part))
                part += 1
                p = p[max_chars:]
            if clen + len(p) > max_chars and cur:
                out.append(_chapter_part(u, "\n\n".join(cur), part))
                part += 1
                cur, clen = [], 0
            cur.append(p)
            clen += len(p)
        if cur:
            out.append(_chapter_part(u, "\n\n".join(cur), part))
    chapters = [{"index": i, "label": u["label"], "text": u["text"],
                 "is_toc": _is_toc_chapter(u["text"])}
                for i, u in enumerate(out, 1)]
    # Tag front/back matter that survived the unit-drop heuristics.
    # Tagged chapters keep their slots (indices are positional and
    # consumed downstream); the v2 pipeline skips them for extraction
    # and evidence instead of renumbering.
    for c in chapters:
        c["is_frontmatter"] = _is_frontmatter_chapter(
            c["label"], c["text"], c["index"], len(chapters))
    return chapters


# --- v2 pipeline: chapter-level map/reduce ---
# Per-chapter extraction (Call A: characters, Call B: content) with a running
# character roster, then a deterministic reduce in code. The legacy
# fixed-chunk pipeline is untouched; select v2 with --pipeline v2.

# Closed trigger taxonomy (editable constant). Call B must emit a severity
# for EVERY category so absence is recorded, not assumed.
TRIGGER_CATEGORIES = [
    "sexual_violence", "non_consent", "domestic_abuse", "child_abuse",
    "self_harm", "suicide", "death_of_loved_one", "graphic_violence",
    "murder", "torture", "kidnapping_captivity", "stalking",
    "substance_abuse", "addiction", "eating_disorder",
    "miscarriage_pregnancy_loss", "infidelity", "animal_harm", "war",
    "medical_trauma", "bullying", "racism", "homophobia", "cheating",
    "explicit_sex",
]
_SEVERITY_ORDER = {"none": 0, "mentioned": 1, "on_page": 2, "graphic": 3}

# Spice level: peak-chapter intensity x fraction-of-chapters-at-that-intensity.
# Principle: triggers warn that content EXISTS; spice level measures how
# PERVASIVE it is *at the peak intensity*. A single explicit scene in a
# 500-page book is not a 5, even if many chapters have mild content.
# Keep as module-level data so future tuning is a data edit, not code.
def _spice_band_peak(peak):
    """Peak chapter spice (0-5) -> intensity band."""
    if peak <= 0:
        return "none"
    if peak == 1:
        return "mild"
    if peak <= 3:
        return "moderate"
    return "explicit"


def _spice_band_freq(frac):
    """Fraction of chapters with spice > 0 -> frequency band.

    NOTE: cutoffs (0.06/0.15/0.35) differ from trigger prominence bands
    (0.05/0.15) intentionally — calibrated so Daemon (2-4 spicy of 73 ch)
    lands at spice level 2. Do not "unify" without re-running acceptance.
    """
    if frac <= 0:
        return "none"
    if frac < 0.06:
        return "rare"
    if frac < 0.15:
        return "occasional"
    if frac < 0.35:
        return "frequent"
    return "pervasive"


_SPICE_MATRIX = {
    ("mild", "rare"): 1, ("mild", "occasional"): 1,
    ("mild", "frequent"): 2, ("mild", "pervasive"): 2,
    ("moderate", "rare"): 1, ("moderate", "occasional"): 2,
    ("moderate", "frequent"): 3, ("moderate", "pervasive"): 4,
    ("explicit", "rare"): 2, ("explicit", "occasional"): 3,
    ("explicit", "frequent"): 4, ("explicit", "pervasive"): 5,
}
# ("none", "none") -> 0.


# Minimum chapter spice to count toward frequency for each peak band.
# Frequency measures chapters AT the peak intensity, not just any spice.
# ("mild" peak -> count spice>=1; "moderate" -> spice>=2; "explicit" -> spice>=4)
_PEAK_BAND_MIN_SPICE = {"mild": 1, "moderate": 2, "explicit": 4}


def _spice_band_detail(spice_levels, successful_chapters):
    """Pure function: per-chapter spice levels -> (peak_band, freq_band, level).

    Same computation as _spice_level_from_chapters but also returns the
    matrix coordinates, for visualization (--show-matrices).
    """
    # Clamp out-of-range values (defensive: bad model output shouldn't skew).
    spice_levels = [max(0, min(5, s)) for s in spice_levels]
    peak = max(spice_levels) if spice_levels else 0
    peak_band = _spice_band_peak(peak)
    if peak_band == "none":
        return ("none", "none", 0)
    min_spice = _PEAK_BAND_MIN_SPICE[peak_band]
    n_at_peak = sum(1 for s in spice_levels if s >= min_spice)
    frac = (n_at_peak / successful_chapters) if successful_chapters else 0
    freq_band = _spice_band_freq(frac)
    if freq_band == "none":
        return (peak_band, "none", 0)
    return (peak_band, freq_band, _SPICE_MATRIX.get((peak_band, freq_band), 0))


def _spice_level_from_chapters(spice_levels, successful_chapters):
    """Pure function: list of per-chapter spice levels (0-5) + count of
    successfully processed chapters -> book spice level 0-5 via the
    intensity x frequency matrix.

    Book-level spice answers "how intense AND how prevalent is the spicy
    content?" — NOT simply "how spicy is the content?" A book with one
    explicit scene gets a low rating even though explicit content exists;
    the `explicit_sex` trigger (independent of this rating) records that
    explicit content occurs at all.

    Frequency counts chapters at or above the peak intensity band:
    - peak "mild"     -> chapters with spice >= 1
    - peak "moderate" -> chapters with spice >= 2
    - peak "explicit" -> chapters with spice >= 4

    `successful_chapters` is the denominator (not total chapters), so
    failed extractions don't silently dilute the frequency. Out-of-range
    spice values are clamped to 0-5.
    """
    return _spice_band_detail(spice_levels, successful_chapters)[2]


# Trigger prominence: severity x frequency per trigger. Additive — severity
# is unchanged. Gives the app UI a low/medium/high signal for display.
def _prominence_band_freq(frac):
    """Fraction of chapters containing the trigger -> frequency band."""
    if frac < 0.05:
        return "rare"
    if frac <= 0.15:
        return "occasional"
    return "frequent"


_PROMINENCE_MATRIX = {
    ("mentioned", "rare"): "low",
    ("mentioned", "occasional"): "low",
    ("mentioned", "frequent"): "medium",
    ("on_page", "rare"): "low",
    ("on_page", "occasional"): "medium",
    ("on_page", "frequent"): "high",
    ("graphic", "rare"): "medium",
    ("graphic", "occasional"): "high",
    ("graphic", "frequent"): "high",
}


def _trigger_prominence(severity, chapter_count, successful_chapters):
    """Pure function: severity name + chapter counts -> low/medium/high.

    `successful_chapters` is the denominator (not total chapters), so
    failed extractions don't silently dilute the frequency.
    """
    if successful_chapters <= 0 or chapter_count <= 0:
        return "low"
    freq_band = _prominence_band_freq(chapter_count / successful_chapters)
    return _PROMINENCE_MATRIX.get((severity, freq_band), "low")


# Character importance: frequency x mentions -> deterministic role.
# Replaces LLM-assigned roles which are unreliable (minor characters get
# promoted, leads get demoted). The role answers "how central is this
# character to the book?" based on observed data, not model opinion.
def _char_freq_band(frac):
    """Fraction of chapters the character appears in -> frequency band."""
    if frac <= 0:
        return "none"
    if frac < 0.10:
        return "rare"
    if frac < 0.30:
        return "occasional"
    return "frequent"


def _char_deterministic_role(chapter_count, successful_chapters, is_pov,
                             role_llm=None):
    """Pure function: observed data -> protagonist/supporting/minor.

    Rules:
    - POV characters are always protagonist (they narrate the book).
    - frequent (30%+ of chapters) -> protagonist
    - occasional (10-30%) -> supporting
    - rare (<10%) -> minor

    If the LLM said "antagonist" and the deterministic role is protagonist
    or supporting, preserve "antagonist" (it's a useful subtype, not a
    different importance level).
    """
    if is_pov:
        return "protagonist"
    frac = (chapter_count / successful_chapters) if successful_chapters else 0
    band = _char_freq_band(frac)
    if band == "frequent":
        det = "protagonist"
    elif band == "occasional":
        det = "supporting"
    else:
        det = "minor"
    # Preserve antagonist subtype from LLM when importance is high enough.
    if role_llm == "antagonist" and det in ("protagonist", "supporting"):
        return "antagonist"
    return det


# Relationship confidence: evidence x type specificity -> high/medium/low.
# Every relationship gets a confidence score so the app can distinguish
# well-evidenced relationships from LLM guesses.
def _rel_evidence_band(n_quotes, n_cooccur):
    """Number of supporting quotes + co-occurrence chapters -> band."""
    if n_quotes >= 2:
        return "strong"
    if n_quotes == 1 or n_cooccur >= 3:
        return "moderate"
    return "weak"


_REL_SPECIFIC_TYPES = {"spouse", "parent", "child", "sibling"}
_REL_VAGUE_TYPES = {"other", "colleague", "neighbor"}


def _rel_type_band(rtype):
    """Relationship type -> specificity band."""
    if rtype in _REL_SPECIFIC_TYPES:
        return "specific"
    if rtype in _REL_VAGUE_TYPES:
        return "vague"
    return "moderate"  # friend, enemy, mentor, romantic, rival, ally


_REL_CONFIDENCE = {
    ("strong", "specific"): "high",
    ("strong", "moderate"): "high",
    ("strong", "vague"): "medium",
    ("moderate", "specific"): "high",
    ("moderate", "moderate"): "medium",
    ("moderate", "vague"): "low",
    ("weak", "specific"): "medium",
    ("weak", "moderate"): "low",
    ("weak", "vague"): "low",
}


def _rel_confidence(n_quotes, n_cooccur, rtype):
    """Pure function: evidence + type -> high/medium/low confidence."""
    ev_band = _rel_evidence_band(n_quotes, n_cooccur)
    type_band = _rel_type_band(rtype)
    return _REL_CONFIDENCE.get((ev_band, type_band), "low")


# ASCII matrix visualizations for --show-matrices (terminal display during
# processing). All pure functions returning strings — no I/O, no ANSI codes
# (Windows PowerShell compatible). Active cells are marked with >> <<.
_SPICE_VIZ_ROWS = ["mild", "moderate", "explicit"]
_SPICE_VIZ_COLS = ["rare", "occasional", "frequent", "pervasive"]


def _spice_matrix_ascii(peak_band, freq_band, level):
    """ASCII spice matrix with the book's position highlighted.

    peak_band/freq_band come from _spice_band_detail(). Handles "none"
    bands (no spicy content) gracefully — no highlight, just the grid.
    """
    lines = ["Spice matrix (peak intensity x frequency):"]
    lines.append(" " * 14 + "".join(f"{c:^12}" for c in _SPICE_VIZ_COLS))
    for r in _SPICE_VIZ_ROWS:
        cells = []
        for c in _SPICE_VIZ_COLS:
            v = _SPICE_MATRIX.get((r, c), 0)
            if r == peak_band and c == freq_band:
                cells.append(f">>{v}<<".center(12))
            else:
                cells.append(str(v).center(12))
        label = f">>{r}<<" if r == peak_band else f"  {r}"
        lines.append(f"{label:<14}" + "".join(cells))
    if level is None:
        # Unknown level (no chapter reported one): not a measured 0.
        lines.append("  -> Book spice: unknown (no chapter reported "
                     "a level)")
    elif peak_band in ("none",) or freq_band in ("none",):
        lines.append("  -> Book spice: 0 (no spicy content detected)")
    else:
        lines.append(f"  -> Book spice: {level} "
                     f"(peak={peak_band}, freq={freq_band})")
    return "\n".join(lines)


def _char_importance_ascii(principals, minors, max_rows=20):
    """ASCII table of principal characters sorted by chapter frequency."""
    principals = principals or []
    minors = minors or []
    lines = [f"Character importance "
             f"({len(principals)} principals, {len(minors)} minor):"]
    lines.append(f"  {'Name':<24}{'Freq':>7}  {'Role':<12}  POV")
    _sorted = sorted(principals, key=lambda c: -(c.get("frequency") or 0))
    for c in _sorted[:max_rows]:
        _freq = c.get("frequency") or 0
        _pov = "*" if c.get("is_pov") else ""
        _name = str(c.get("name", "?"))[:24]
        _role = str(c.get("role", "?"))[:12]
        lines.append(f"  {_name:<24}{_freq:>6.0%}  {_role:<12}  {_pov}")
    if len(_sorted) > max_rows:
        lines.append(f"  ... and {len(_sorted) - max_rows} more")
    return "\n".join(lines)


def _rel_confidence_ascii(relationships):
    """ASCII distribution of relationship confidence levels."""
    counts = Counter()
    for r in relationships or []:
        counts[r.get("confidence", "low")] += 1
    return ("Relationship confidence:\n"
            f"  high: {counts.get('high', 0)}, "
            f"medium: {counts.get('medium', 0)}, "
            f"low: {counts.get('low', 0)}")


def _matrix_snapshot_data(chapter_as, chapter_bs):
    """Running aggregates for the live matrix viz panel. Pure function.

    Returns (char_freq, spice_peak, spice_counts, rel_count):
    - char_freq: {display_name: chapters_seen_in} (Call A characters)
    - spice_peak: max spice_level seen so far (Call B), 0 if none
    - spice_counts: {1: chapters>=1, 2: chapters>=2, 4: chapters>=4}
    - rel_count: total relationships across Call A results so far
    """
    seen_names = {}  # norm_name -> display name (first-seen wins)
    char_freq = Counter()
    rel_count = 0
    for idx in sorted(chapter_as or {}):
        a = chapter_as[idx]
        if not isinstance(a, dict):
            continue
        in_chapter = set()
        for c in a.get("characters", []) or []:
            nm = (c.get("name") or "").strip() if isinstance(c, dict) else ""
            if not nm:
                continue
            nk = norm_name(nm)
            if nk and nk not in seen_names:
                seen_names[nk] = nm
            if nk:
                in_chapter.add(nk)
        for nk in in_chapter:
            char_freq[seen_names[nk]] += 1
        rels = a.get("relationships", []) or []
        rel_count += sum(1 for r in rels if isinstance(r, dict))
    # Display aggregate only: an unknown level (None) counts as
    # no-known-spice here; the authoritative book level lives in
    # v2_reduce, which excludes unknowns instead of zeroing them.
    spice_levels = [(b.get("spice_level") or 0)
                    for b in (chapter_bs or {}).values()
                    if isinstance(b, dict)]
    spice_peak = max(spice_levels) if spice_levels else 0
    spice_counts = {1: sum(1 for s in spice_levels if s >= 1),
                    2: sum(1 for s in spice_levels if s >= 2),
                    4: sum(1 for s in spice_levels if s >= 4)}
    return dict(char_freq), spice_peak, spice_counts, rel_count


def _matrix_panel_lines(mx):
    """Compact Rich-markup matrix snapshot for the viz panel. Pure.

    3 lines, no Rich dependency (only uses markup strings; escaping of
    '[' in names is done inline so this works without Rich installed).
    """
    mx = mx or {}
    char_freq = mx.get("char_freq") or {}
    top = sorted(char_freq.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    if top:
        chars = ", ".join("%s (%d)" % (str(n).replace("[", "\\["), c)
                          for n, c in top)
    else:
        chars = "\u2014"
    sc = mx.get("spice_counts") or {}
    return "\n".join([
        "[dim]Top chars:[/] %s" % chars,
        "[dim]Spice:[/] peak=%d | ch\u22651: %d, \u22652: %d, \u22654: %d"
        % (mx.get("spice_peak") or 0, sc.get(1, 0), sc.get(2, 0),
           sc.get(4, 0)),
        "[dim]Rels:[/] %d" % (mx.get("rel_count") or 0),
    ])


def _matrix_panel_block(mx, show):
    """Full matrix block for the viz panel, or '' when not shown. Pure."""
    if not (mx and show):
        return ""
    return "\n[dim]\u2500[/]\n" + _matrix_panel_lines(mx)


def _push_matrix_viz_ui(chapter_as, chapter_bs):
    """Push running matrix aggregates to the Rich viz panel (--show-matrices).

    Module-level so the hook sites in process_file_v2 stay one-liners.
    No-op unless --show-matrices is set.
    """
    if not CONFIG.get("show_matrices"):
        return
    cf, sp, sc, rc = _matrix_snapshot_data(chapter_as, chapter_bs)
    UI.update_matrix_data(cf, sp, sc, rc)


# ---------------------------------------------------------------------------
# P4/P5: preview summary + preview diff (pure string builders, no I/O).
# PowerShell-safe: ASCII arrows, no emoji, no ANSI codes.
# ---------------------------------------------------------------------------

_SUMMARY_MAX_PRINCIPALS = 12
_SUMMARY_MAX_RELS = 10
_SUMMARY_MAX_DROPPED = 6
_DIFF_MAX_ITEMS = 8


def _preview_summary(result):
    """Pure: result dict -> compact human-readable digest string.

    Same data that goes into the preview JSON, rendered scannable.
    Never raises on missing/odd keys.
    """
    r = result or {}
    lines = []
    title = r.get("title") or "?"
    author = r.get("author") or "Unknown author"
    lines.append(f"Preview summary: {title} - {author}")

    def _freq(c):
        try:
            return float((c or {}).get("frequency") or 0)
        except (TypeError, ValueError):
            return 0

    chars = r.get("characters") or []
    minors = r.get("minor_characters") or []
    ordered = sorted(chars,
                     key=lambda c: (-_freq(c), str((c or {}).get("name") or "")))
    lines.append(f"  Principals ({len(chars)}):")
    for c in ordered[:_SUMMARY_MAX_PRINCIPALS]:
        c = c or {}
        name = c.get("name") or "?"
        pct = int(round(_freq(c) * 100))
        role = c.get("role") or "unknown"
        pov = " *" if c.get("is_pov") else ""
        lines.append(f"    {name} ({pct}%, {role}){pov}")
    if len(ordered) > _SUMMARY_MAX_PRINCIPALS:
        lines.append(f"    ... and {len(ordered) - _SUMMARY_MAX_PRINCIPALS} more")

    rels = r.get("relationships") or []
    lines.append(f"  Relationships ({len(rels)}):")
    for rel in rels[:_SUMMARY_MAX_RELS]:
        rel = rel or {}
        conf = rel.get("confidence")
        conf_s = f" ({conf})" if conf else ""
        lines.append(f"    {rel.get('from') or '?'} -> {rel.get('to') or '?'}: "
                     f"{rel.get('type') or '?'}{conf_s}")
    if len(rels) > _SUMMARY_MAX_RELS:
        lines.append(f"    ... and {len(rels) - _SUMMARY_MAX_RELS} more")

    dropped = r.get("dropped_relationships") or []
    if dropped:
        lines.append(f"  Dropped relationships ({len(dropped)}):")
        for rel in dropped[:_SUMMARY_MAX_DROPPED]:
            rel = rel or {}
            vp = rel.get("validation_p")
            note = rel.get("validation_note") or "dropped"
            p_s = f"P={vp:.2f}, " if isinstance(vp, (int, float)) else ""
            lines.append(f"    {rel.get('from') or '?'} -> {rel.get('to') or '?'}: "
                         f"{rel.get('type') or '?'} ({p_s}{note})")
        if len(dropped) > _SUMMARY_MAX_DROPPED:
            lines.append(f"    ... and {len(dropped) - _SUMMARY_MAX_DROPPED} more")

    lvl = r.get("spice_level")
    peak_band = r.get("spice_peak_band") or _spice_band_peak(r.get("spice_peak") or 0)
    freq_band = r.get("spice_freq_band")
    spice_s = f"{lvl}/5" if lvl is not None else "?/5"
    if peak_band and peak_band != "none":
        detail = f"peak={peak_band}"
        if freq_band and freq_band != "none":
            detail += f", freq={freq_band}"
        lines.append(f"  Spice: {spice_s} ({detail})")
    elif lvl:
        # The level says there IS spice but no peak data survived
        # (an older/hand-built result): "(no spicy content)" would
        # be a false claim — print the level without a parenthetical.
        lines.append(f"  Spice: {spice_s}")
    elif lvl is None:
        # Unknown level (no chapter reported one): "(no spicy
        # content)" would be a false claim in the other direction.
        lines.append(f"  Spice: {spice_s} (unknown)")
    else:
        lines.append(f"  Spice: {spice_s} (no spicy content)")

    trigs = r.get("triggers") or []
    n_high = sum(1 for t in trigs
                 if isinstance(t, dict) and t.get("prominence") == "high")
    lines.append(f"  Triggers: {len(trigs)} ({n_high} high prominence)")

    tropes = r.get("tropes") or []
    lines.append(f"  Tropes: {len(tropes)} confirmed")

    _stg = r.get("staging")
    if _stg:
        lines.append(f"  Staging: {len(_stg.get('staged') or [])} "
                     f"held back (staging gate)")

    if minors:
        lines.append(f"  Minor characters: {len(minors)} (see preview JSON)")

    return "\n".join(lines)


def _rel_pair_key(rel):
    """(from, to) identity key, case-insensitive. Pure."""
    rel = rel or {}
    return (str(rel.get("from") or "").strip().lower(),
            str(rel.get("to") or "").strip().lower())


def _dialogue_ratio(text):
    """Pure: fraction of characters inside quotes (dialogue) vs total.

    Handles straight quotes (") and curly quotes (\u201c \u201d).
    Returns 0.0 for empty text, 1.0 for all-dialogue.
    """
    if not text:
        return 0.0
    # Match quoted spans: "..." or "..." (non-greedy, no nesting)
    # Straight quotes: "..."  Curly: "\u201c...\u201d"
    import re as _re
    _QUOTE_RE = _re.compile(r'"[^"]*"|\u201c[^\u201d]*\u201d')
    dialogue_chars = sum(len(m.group(0)) for m in _QUOTE_RE.finditer(text))
    total = len(text)
    if total == 0:
        return 0.0
    return round(min(1.0, dialogue_chars / total), 4)


def _chapter_spice_array(chapter_bs, n_chapters):
    """Pure: build per-chapter spice array indexed by chapter number.

    chapter_bs: {chapter_index: result or None}. Returns list where
    index i holds the spice level for chapter i (0 if missing/failed).
    """
    arr = [0] * (n_chapters + 1)  # 1-based indexing; [0] unused
    for idx, b in (chapter_bs or {}).items():
        if b and 1 <= idx <= n_chapters:
            try:
                arr[idx] = max(0, min(5, int(b.get("spice_level", 0))))
            except (TypeError, ValueError):
                arr[idx] = 0
    return arr


def _trigger_chapters_map(triggers):
    """Pure: build {trigger_name: [chapter_indices]} from trigger list.

    Each trigger dict may have 'chapter_indices' (preferred) or 'chapters'
    (label strings, skipped). Returns sorted index lists.
    """
    result = {}
    for t in triggers or []:
        name = t.get("warning") or t.get("name")
        if not name:
            continue
        indices = t.get("chapter_indices")
        if indices:
            result[name] = sorted(indices)
    return result


def _preview_diff(old, new):
    """Pure: two preview-JSON dicts -> compact diff string. Never raises."""
    o, n = old or {}, new or {}
    lines = []
    o_title = o.get("title") or "old"
    n_title = n.get("title") or "new"
    lines.append(f"Preview diff: {o_title} (old) -> {n_title} (new)")

    # --- Characters ---
    o_chars = o.get("characters") or []
    o_minors = o.get("minor_characters") or []
    n_chars = n.get("characters") or []
    n_minors = n.get("minor_characters") or []
    o_total = len(o_chars) + len(o_minors)
    if n_minors or o_minors:
        lines.append(f"  Characters: {o_total} -> {len(n_chars)} principals "
                     f"({len(n_minors)} minor)")
    else:
        lines.append(f"  Characters: {o_total} -> {len(n_chars)}")

    # --- Relationships ---
    o_rels = o.get("relationships") or []
    n_rels = n.get("relationships") or []
    n_dropped = n.get("dropped_relationships") or []
    o_by_pair = {_rel_pair_key(r): r for r in o_rels}
    n_by_pair = {_rel_pair_key(r): r for r in n_rels}
    drop_by_pair = {_rel_pair_key(r): r for r in n_dropped}

    added, removed, changed = [], [], []
    for key, nr in n_by_pair.items():
        orr = o_by_pair.get(key)
        if orr is None:
            added.append(nr)
        elif (str(orr.get("type") or "").strip().lower()
              != str(nr.get("type") or "").strip().lower()):
            changed.append((orr, nr))
    for key, orr in o_by_pair.items():
        if key not in n_by_pair:
            removed.append(orr)

    _chg_s = f", ~{len(changed)} changed" if changed else ""
    lines.append(f"  Relationships: {len(o_rels)} -> {len(n_rels)} "
                 f"(+{len(added)} new, -{len(removed)} removed{_chg_s})")

    def _fmt_rel(r):
        r = r or {}
        conf = r.get("confidence")
        return (f"{r.get('from') or '?'} -> {r.get('to') or '?'}: "
                f"{r.get('type') or '?'}" + (f" ({conf})" if conf else ""))

    for r in added[:_DIFF_MAX_ITEMS]:
        lines.append(f"    + {_fmt_rel(r)}")
    for orr, nr in changed[:_DIFF_MAX_ITEMS]:
        note = (nr or {}).get("validation_note")
        reason = f" ({note})" if note else ""
        lines.append(f"    ~ {(orr or {}).get('from')} -> {(orr or {}).get('to')}: "
                     f"{(orr or {}).get('type')} -> {(nr or {}).get('type')}{reason}")
    for r in removed[:_DIFF_MAX_ITEMS]:
        dr = drop_by_pair.get(_rel_pair_key(r))
        if dr is not None:
            vp = dr.get("validation_p")
            note = dr.get("validation_note") or "dropped"
            p_s = f"P={vp:.2f}, " if isinstance(vp, (int, float)) else ""
            reason = f" ({p_s}{note})"
        else:
            reason = " (not in new run)"
        lines.append(f"    - {_fmt_rel(r)}{reason}")
    _shown = (min(len(added), _DIFF_MAX_ITEMS)
              + min(len(removed), _DIFF_MAX_ITEMS)
              + min(len(changed), _DIFF_MAX_ITEMS))
    if len(added) + len(removed) + len(changed) > _shown:
        lines.append(f"    ... and {len(added) + len(removed) + len(changed) - _shown} more")

    # --- Spice ---
    o_sp, n_sp = o.get("spice_level"), n.get("spice_level")
    if o_sp == n_sp:
        lines.append(f"  Spice: {o_sp} -> {n_sp} (unchanged)")
    else:
        lines.append(f"  Spice: {o_sp} -> {n_sp}")

    # --- Triggers (by warning name) ---
    def _trig_names(d):
        names = []
        for t in (d or {}).get("triggers") or []:
            names.append(str(t.get("warning") or "?") if isinstance(t, dict)
                         else str(t))
        return names
    o_tn, n_tn = _trig_names(o), _trig_names(n)
    t_added = [w for w in n_tn if w not in o_tn]
    t_removed = [w for w in o_tn if w not in n_tn]
    t_line = f"  Triggers: {len(o_tn)} -> {len(n_tn)}"
    _tbits = [f"+{w}" for w in t_added[:3]] + [f"-{w}" for w in t_removed[:3]]
    if _tbits:
        t_line += f" ({', '.join(_tbits)})"
    elif len(o_tn) != len(n_tn):
        t_line += f" ({len(n_tn) - len(o_tn):+d})"
    lines.append(t_line)

    # --- Tropes ---
    o_tr = [str(t) for t in (o.get("tropes") or [])]
    n_tr = [str(t) for t in (n.get("tropes") or [])]
    tr_added = [t for t in n_tr if t not in o_tr]
    tr_removed = [t for t in o_tr if t not in n_tr]
    tr_line = f"  Tropes: {len(o_tr)} -> {len(n_tr)}"
    _rbits = [f"+{t}" for t in tr_added[:3]] + [f"-{t}" for t in tr_removed[:3]]
    if _rbits:
        tr_line += f" ({', '.join(_rbits)})"
    elif len(o_tr) != len(n_tr):
        tr_line += f" ({len(n_tr) - len(o_tr):+d})"
    lines.append(tr_line)

    return "\n".join(lines)


def _cmd_preview_diff(old_path, new_path):
    """Load two preview JSONs and return the diff string (or an error)."""
    try:
        with open(old_path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError) as e:
        return f"ERROR: cannot read {old_path}: {e}"
    try:
        with open(new_path, encoding="utf-8") as f:
            new = json.load(f)
    except (OSError, ValueError) as e:
        return f"ERROR: cannot read {new_path}: {e}"
    return _preview_diff(old, new)


def _recalibrate_spice(preview_data):
    """Pure: check if a preview JSON has per-chapter spice data and recompute.

    Returns (status, old_level, new_level, detail) where status is one of:
    - "ok": recomputed successfully
    - "no_data": no per-chapter spice data, needs reprocessing
    - "no_change": recomputed level matches the stored level
    """
    d = preview_data or {}
    old_level = d.get("spice_level")
    chapter_spice = d.get("chapter_spice")
    if not chapter_spice:
        return ("no_data", old_level, None,
                "no per-chapter spice data — needs reprocessing")
    # chapter_spice is 1-based (index 0 unused); filter to successful chapters
    n_successful = d.get("chapters_successful") or d.get("chapters_total") or 0
    # Use non-zero entries as the spice levels (0 = no spice or failed chapter)
    spices = [s for s in chapter_spice[1:] if s]
    if not spices and not n_successful:
        return ("no_data", old_level, None,
                "chapter_spice present but empty — needs reprocessing")
    new_level = _spice_level_from_chapters(spices, n_successful or len(spices))
    if new_level == old_level:
        return ("no_change", old_level, new_level,
                f"spice {old_level} confirmed by current matrix")
    return ("ok", old_level, new_level,
            f"spice {old_level} -> {new_level} under current matrix")


def _cmd_recalibrate_spice(path):
    """Load a preview JSON and report spice recalibration status."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return f"ERROR: cannot read {path}: {e}"
    title = data.get("title", path)
    status, old, new, detail = _recalibrate_spice(data)
    lines = [f"Recalibrate spice: {title}"]
    if status == "no_data":
        lines.append(f"  Old spice: {old}")
        lines.append(f"  Status: NEEDS REPROCESSING — {detail}")
    elif status == "no_change":
        lines.append(f"  Spice: {old} (unchanged)")
        lines.append(f"  Status: {detail}")
    else:
        lines.append(f"  Old spice: {old}")
        lines.append(f"  New spice: {new}")
        lines.append(f"  Status: {detail}")
    return "\n".join(lines)


def _filter_principals(characters, relationships, min_frequency=0.20):
    """Filter to principal characters. Returns
    (principals, minors, filtered_relationships).

    A character is a principal if ANY of:
    - is_pov is True, OR
    - frequency >= min_frequency (default 0.20 = 20% of chapters), OR
    - has 3+ relationships with other principals (2 iterative passes:
      pass 1 finds frequency/POV principals, pass 2-3 promote well-connected
      characters, so a connector between principals isn't dropped).

    Relationships are kept only when BOTH endpoints are principals.
    Pure function: no I/O, no LLM.
    """
    char_by_norm = {}
    for c in characters or []:
        nk = norm_name(c.get("name", ""))
        if nk and nk not in char_by_norm:
            char_by_norm[nk] = c

    # Pass 1: POV or frequency.
    principals = set()
    for nk, c in char_by_norm.items():
        if c.get("is_pov"):
            principals.add(nk)
        elif (c.get("frequency") or 0) >= min_frequency:
            principals.add(nk)

    # Passes 2-3: 3+ relationships with principals (iterative promotion).
    adj = {}
    for r in relationships or []:
        a = norm_name(r.get("from", ""))
        b = norm_name(r.get("to", ""))
        if a and b and a != b:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
    for _ in range(2):
        newly = {nk for nk in char_by_norm
                 if nk not in principals
                 and sum(1 for nb in adj.get(nk, ()) if nb in principals) >= 3}
        if not newly:
            break
        principals.update(newly)

    principal_chars = [c for c in (characters or [])
                       if norm_name(c.get("name", "")) in principals]
    minor_chars = [c for c in (characters or [])
                   if norm_name(c.get("name", "")) not in principals]
    filtered_rels = [r for r in (relationships or [])
                     if norm_name(r.get("from", "")) in principals
                     and norm_name(r.get("to", "")) in principals]
    return principal_chars, minor_chars, filtered_rels


def _drop_ghost_relationships(result):
    """Move ghost-endpoint relationships out of result["relationships"]
    into result["dropped_relationships"]; returns the count moved.

    A ghost endpoint is a name with no character in the result's FINAL
    character set (mentioned in prose or in a claim, but never
    rostered — or filtered away upstream). Left in place, the two
    directions fail differently and silently: a from-ghost entry is
    keyed into the write path's rel_map under a name with no character
    row and never consumed, while a to-ghost entry IS embedded in the
    from-character's DB row, pointing at a character that has no row.
    The preview shows both; the DB keeps at most one — a silent
    preview/DB divergence. Accounting for them here (validation_note
    "ghost_endpoint") makes the drop visible and keeps preview == DB.
    With the principals filter on this is a no-op: surviving
    relationships already have both endpoints in the set."""
    names = {norm_name(c.get("name", ""))
             for c in result.get("characters") or []
             if isinstance(c, dict)}
    names.discard("")
    kept, ghosts = [], []
    for r in result.get("relationships") or []:
        if (norm_name(r.get("from", "")) in names
                and norm_name(r.get("to", "")) in names):
            kept.append(r)
        else:
            ghosts.append(r)
    if not ghosts:
        return 0
    result["relationships"] = kept
    dropped = result.setdefault("dropped_relationships", [])
    for r in ghosts:
        entry = dict(r)  # never annotate the caller's dict in place
        entry["validation_note"] = "ghost_endpoint"
        dropped.append(entry)
    return len(ghosts)

# Relationship dedupe: exclusive types resolve by precedence (lower wins).
# Non-exclusive types are kept as extras only with evidence.
_REL_PRECEDENCE = {"spouse": 0, "parent": 1, "child": 1, "sibling": 2,
                   "romantic": 3, "friend": 4, "colleague": 4, "enemy": 5}
_REL_NONEXCLUSIVE = {"mentor", "rival", "ally", "neighbor", "other"}

# ---------------------------------------------------------------------------
# Relationship direction convention (AUTHORITATIVE — the app renders to this;
# also documented in README.md, "Relationship direction").
#
# A relationship entry {"from": A, "to": B, "type": T} means "A is the T of
# B": the type ALWAYS describes `from` in relation to `to`. Example:
# {"from": "Lilith Sorrengail", "to": "Violet Sorrengail", "type": "parent"}
# means Lilith is Violet's parent. The decision-model validator uses the
# same reading ("is {from} the {type} of {to}?"), and write_claims stores
# the entry on `from`'s character row as {"to": B, "type": T}.
#
# Canonical form: a pair is stored ONCE, with endpoints in canonical
# (case-insensitive alphabetical) order. When a claim's direction is
# reversed to fit canonical order, asymmetric types are flipped via
# _REL_INVERSE (parent <-> child). Symmetric types (spouse, sibling,
# friend, enemy, romantic, colleague, neighbor, rival, ally, other) are
# unaffected by reversal. "mentor" is asymmetric but has NO inverse in the
# type vocabulary, so mentor pairs are stored in claim direction (mentor
# first) instead of canonical order — the one documented exception.
# ---------------------------------------------------------------------------
_REL_INVERSE = {"parent": "child", "child": "parent"}
_REL_NO_INVERSE = {"mentor"}


def _canonical_pair(frm, to, rtype):
    """Normalize a relationship claim to canonical form (see convention
    above). Returns (from, to, type) with the type lowercased, or None for
    a self-pair (endpoints normalize to the same character)."""
    if norm_name(frm) == norm_name(to):
        return None
    t = (rtype or "").lower()
    if t in _REL_NO_INVERSE:
        return frm, to, t
    a, b = sorted([frm, to], key=lambda s: s.lower())
    if frm == a:
        return frm, to, t
    return a, b, _REL_INVERSE.get(t, t)


def _alias_shadowed(roster, self_key, alias_key):
    """True if alias_key is the canonical/primary key of a DIFFERENT roster
    entry. Such an alias is pollution (another character's name recorded as
    this entry's alias) and must never resolve endpoints to this entry:
    the real character wins over the alias claim."""
    for k, e in roster.items():
        if k == self_key:
            continue
        if alias_key == k or alias_key in e.get("primary_keys", set()):
            return True
    return False


# --- Pair-evidence mining --------------------------------------------------
# Deterministic search for a sentence naming BOTH endpoints of a
# relationship pair. Two uses in v2_reduce: (a) family validation — an
# establishing sentence may replace the topical quote the validator
# judges; (b) write evidence for claims extracted without any. Grades
# are categorical and never blended:
#   binding       a kinship marker syntactically bound to an endpoint,
#                 with the pair's other endpoint equated with the
#                 relative the construction defines;
#   co-occurrence both endpoints + a kinship marker, not bound;
#   plain         both endpoints, no kinship marker.
_PAIR_MARKERS = (
    "father", "fathers", "mother", "mothers", "dad", "dads", "mom",
    "moms", "mum", "mums", "son", "sons", "daughter", "daughters",
    "brother", "brothers", "sister", "sisters", "sibling", "siblings",
    "husband", "husbands", "wife", "wives", "spouse", "spouses",
    "parent", "parents", "child", "children",
)
_PAIR_MARKER_ALT = r"(?i:\b(?:" + "|".join(_PAIR_MARKERS) + r"))"
_PAIR_MARKER_RE = re.compile(_PAIR_MARKER_ALT + r"\b", re.IGNORECASE)
_PAIR_PRONOUN_ALT = r"(?i:my|his|her|their)"
# "Sentences" longer than this are splitting failures (unsplittable
# text blocks), not evidence quotes.
_PAIR_MAX_SENTENCE = 1000


def _pair_name_forms(name, roster):
    """Name forms an endpoint may appear under in chapter text: the
    roster entry's canonical name and aliases, plus the first name —
    but only when that first name is unique across the roster (a
    shared first name could denote the other character and is never
    used). Without a roster entry, only the full name is trusted."""
    forms = {name} if name else set()
    if not roster or not name:
        return forms
    entry = None
    for e in roster.values():
        if norm_name(e.get("name", "")) == norm_name(name):
            entry = e
            break
    if entry is None:
        return forms
    forms.add(entry["name"])
    forms.update(a for a in entry.get("aliases", []) if a)
    parts = entry["name"].split()
    first = parts[0] if parts else ""
    if first and first != entry["name"]:
        shared = False
        for e in roster.values():
            if e is entry:
                continue
            ename = e.get("name", "")
            eparts = ename.split()
            if eparts and eparts[0] == first:
                shared = True
                break
            for a in e.get("aliases", []):
                aparts = a.split()
                if a == first or (aparts and aparts[0] == first):
                    shared = True
                    break
            if shared:
                break
        if not shared:
            forms.add(first)
    return forms


def _forms_re(forms):
    """Case-sensitive word-boundary regex matching any of the forms
    (longest first, so a full name wins over its own first name).
    Names are matched case-sensitively: a lowercase common word that
    happens to spell a first name is not a mention."""
    ordered = sorted((f for f in forms if f), key=len, reverse=True)
    if not ordered:
        return None
    return re.compile(r"(?<!\w)(?:"
                      + "|".join(re.escape(f) for f in ordered)
                      + r")(?!\w)")


def _split_sentences(text):
    """Deterministic sentence split (after terminal punctuation).
    Every returned sentence is a verbatim substring of `text`: pieces
    are contiguous slices and only surrounding whitespace is trimmed.
    Sentences are never joined or rewritten."""
    out = []
    for piece in re.split(r"(?<=[.!?])\s+", text):
        s = piece.strip()
        if s:
            out.append(s)
    return out


def _pair_binding(sentence, re_anchor, re_other):
    """True when a kinship-marker construction anchored at one endpoint
    equates the pair's OTHER endpoint with the relative it defines:
      possessive ("Xaden's father") or of-construction ("son of Fen
      Riorson"), equated by apposition ("Xaden's father, Fen Riorson,
      ...", "Fen Riorson, Xaden's father, ...") or copula ("Fen was
      Xaden's father", "Xaden's father is Fen"); or a pronoun
      possessive equating an adjacent endpoint ("her brother Brennan",
      "Brennan, her brother") — the pronoun's referent is taken to be
      the pair's other endpoint, which the caller has established is
      also present in the sentence.
    A marker bound to a third party, or defining a relative who is
    not the other endpoint, does NOT count: mere presence of both
    names near a marker is co-occurrence, not binding."""
    poss = re_anchor.pattern + r"['’]s\s+" + _PAIR_MARKER_ALT + r"\b"
    ofph = (_PAIR_MARKER_ALT + r"\s+of\s+(?:the\s+)?"
            + re_anchor.pattern)
    for phrase in (poss, ofph):
        for m in re.finditer(phrase, sentence):
            before, after = sentence[:m.start()], sentence[m.end():]
            if re.match(r"[\s,]+" + re_other.pattern, after):
                return True  # apposition after the phrase
            if re.search(re_other.pattern + r"[\s,]+$", before):
                return True  # apposition before the phrase
            if re.match(r"\s+(?i:is|was|became)\s+(?:the\s+)?"
                        + re_other.pattern, after):
                return True  # "Xaden's father is Fen"
            if re.search(re_other.pattern
                         + r"\s+(?i:is|was|became)\s+(?:the\s+)?$",
                         before):
                return True  # "Fen was Xaden's father"
    pron = _PAIR_PRONOUN_ALT + r"\s+" + _PAIR_MARKER_ALT + r"\b"
    either = r"(?:" + re_anchor.pattern + r"|" + re_other.pattern + r")"
    for m in re.finditer(pron, sentence):
        before, after = sentence[:m.start()], sentence[m.end():]
        if re.match(r"\s*,?\s*" + either, after):
            return True  # "her brother Brennan" / "my sister, Violet"
        if re.search(either + r"[\s,]+$", before):
            return True  # "Brennan, her brother"
    return False


def _pair_sentence_grade(sentence, re_a, re_b):
    """Grade one candidate sentence (both endpoints already known to
    appear): binding > co-occurrence > plain."""
    if not _PAIR_MARKER_RE.search(sentence):
        return "plain"
    if (_pair_binding(sentence, re_a, re_b)
            or _pair_binding(sentence, re_b, re_a)):
        return "binding"
    return "co-occurrence"


def _mine_pair_evidence(from_name, to_name, chapters, roster=None):
    """Mine chapter texts for the best sentence naming BOTH endpoints
    of a pair. chapters: dicts with "index" and "text" (the caller
    excludes ToC/front matter). Returns {"sentence", "grade",
    "chapter"} or None. The sentence is always a verbatim substring
    of its chapter text.

    Deterministic ranking: grade first (binding > co-occurrence >
    plain), then earliest chapter, then shortest sentence, then
    earliest position in the chapter."""
    re_a = _forms_re(_pair_name_forms(from_name, roster))
    re_b = _forms_re(_pair_name_forms(to_name, roster))
    if re_a is None or re_b is None:
        return None
    _rank = {"binding": 0, "co-occurrence": 1, "plain": 2}
    best = None  # (key, hit)
    for c in chapters or []:
        text = c.get("text", "") or ""
        if not text:
            continue
        # Cheap pre-filter: both endpoints must appear in the chapter.
        if not (re_a.search(text) and re_b.search(text)):
            continue
        idx = c.get("index", 0)
        for pos, sentence in enumerate(_split_sentences(text)):
            if len(sentence) > _PAIR_MAX_SENTENCE:
                continue
            if not (re_a.search(sentence) and re_b.search(sentence)):
                continue
            grade = _pair_sentence_grade(sentence, re_a, re_b)
            key = (_rank[grade], idx, len(sentence), pos)
            if best is None or key < best[0]:
                best = (key, {"sentence": sentence, "grade": grade,
                              "chapter": idx})
    return best[1] if best else None

# Regex evidence gates for high-severity trigger claims. The evidence quote
# must contain supporting language, not just mention the topic. Catches
# false positives like "digital suicide" or a consensual dance tagged as
# sexual violence. Applied at sev>=2 in v2_reduce.
_TRIGGER_EVIDENCE_RE = {
    "suicide": re.compile(
        r"suicid|kill(ed)? (him|her|them)self|took (his|her|their) own life|"
        r"hanged (him|her|them)self", re.IGNORECASE),
    "sexual_violence": re.compile(
        r"rap(e|ed|ing)|forced|assault|coerc|non-consensual|"
        r"against (his|her|their) will", re.IGNORECASE),
    "child_abuse": re.compile(
        r"child|kid|boy|girl|minor", re.IGNORECASE),
    "self_harm": re.compile(
        r"cut (him|her|them)self|self-harm|self-injur", re.IGNORECASE),
}

# Negative patterns: hypothetical/hedged/metaphorical language meaning the
# evidence does NOT depict a real event, even if a positive pattern matched.
# Checked before the positive gate; a match downgrades to severity 1.
_TRIGGER_NEGATIVE_RE = {
    # Specific false-positive phrases only. A broad hypothetical-word list was
    # tried and removed 2026-10-08: it matched "would"/"from" in unrelated
    # clauses and dropped legitimate triggers (false negatives are worse).
    "suicide": re.compile(
        r"hitting (him|her|them)self"          # hyperbole, not self-harm
        r"|would be suicide"                   # metaphorical ("that job would be suicide")
        r"|committing digital",                 # "digital suicide" metaphor
        re.IGNORECASE),
}

# Decision-model trigger validation via Unsloth /v1/systemone.
# Opt-in via --decision-model-url; replaces the regex gates above with a
# dedicated decision model (e.g. Laya) that returns P(yes) for "does this
# passage depict [trigger]?". Prototype validated 7/7 on metaphorical vs
# literal cases that regex struggles with.
_DECISION_TRIGGER_DEFS = {
    "suicide": (
        "Does this passage depict suicide?",
        "A character attempts or completes suicide; expresses clear intent to kill themselves",
        "Metaphorical use ('career suicide', 'digital suicide'); hypothetical; someone else's death",
    ),
    "sexual_violence": (
        "Does this passage depict sexual violence?",
        "Non-consensual sexual acts, assault, or coercion described or clearly implied",
        "Consensual intimacy; romantic tension without coercion",
    ),
    "child_abuse": (
        "Does this passage depict child abuse?",
        "A child is physically harmed, sexually abused, or severely neglected",
        "Discipline without injury; adults arguing around children",
    ),
    "self_harm": (
        "Does this passage depict self-harm?",
        "A character deliberately injures themselves (cutting, burning, etc.)",
        "Metaphorical ('beating himself up over it'); accidental injury",
    ),
    "addiction": (
        "Does this passage depict addiction?",
        "A character shows compulsive drug, alcohol, gambling, or other addictive behavior; withdrawal or cravings described",
        "Casual or social drinking/drug use without compulsion; a single use with no pattern",
    ),
    "bullying": (
        "Does this passage depict bullying?",
        "A character is harassed, intimidated, or humiliated by someone with a power advantage over them",
        "Mutual arguments; workplace criticism; a one-off insult without a power imbalance",
    ),
    "cheating": (
        "Does this passage depict cheating?",
        "A character cheats at a game, exam, contest, or system (hacking, rigging, using forbidden aids)",
        "Relationship infidelity (that's infidelity); fair play; winning by skill",
    ),
    "death_of_loved_one": (
        "Does this passage depict the death of a loved one?",
        "A character the POV cares about dies on the page or the death is a present emotional event",
        "A stranger's death; a historical death mentioned in passing; someone else's grief the character doesn't share",
    ),
    "domestic_abuse": (
        "Does this passage depict domestic abuse?",
        "A partner or family member physically harms, threatens, or coercively controls another household member",
        "An unhappy or cold marriage without abuse; arguments without threats or violence",
    ),
    "explicit_sex": (
        "Does this passage depict explicit sexual content?",
        "Sexual acts described in explicit anatomical or pornographic detail",
        "Romantic intimacy without explicit detail; innuendo; fade-to-black; kissing",
    ),
    "graphic_violence": (
        "Does this passage depict graphic violence?",
        "Violence described in visceral, bloody, or disturbing detail (wounds, gore, suffering)",
        "Mild action violence; violence mentioned but not described; cartoonish violence",
    ),
    "infidelity": (
        "Does this passage depict infidelity?",
        "A character in a committed relationship has a romantic or sexual affair with someone else",
        "Cheating at games or systems (that's cheating); flirting without acting; a breakup before the new relationship",
    ),
    "kidnapping_captivity": (
        "Does this passage depict kidnapping or captivity?",
        "A character is abducted, held against their will, or imprisoned",
        "Voluntary confinement; being grounded; a character choosing to stay somewhere",
    ),
    "medical_trauma": (
        "Does this passage depict medical trauma?",
        "Graphic medical procedures, severe illness, surgery, or hospital suffering described in disturbing detail",
        "A routine doctor visit; illness mentioned without detail; recovery described calmly",
    ),
    "miscarriage_pregnancy_loss": (
        "Does this passage depict miscarriage or pregnancy loss?",
        "A character loses a pregnancy through miscarriage, stillbirth, or abortion depicted on the page",
        "A successful birth; pregnancy mentioned without loss; fear of loss that doesn't happen",
    ),
    "murder": (
        "Does this passage depict murder?",
        "A character kills another character on the page, or a killing is described in present detail",
        "Threats without action; deaths by accident or natural causes",
    ),
    "non_consent": (
        "Does this passage depict non-consensual acts (non-sexual)?",
        "A character is drugged, coerced, or forced into a non-sexual act against their will (forced ingestion, coercion, manipulation)",
        "Sexual non-consent (that's sexual_violence); persuasion without coercion; voluntary acts",
    ),
    "stalking": (
        "Does this passage depict stalking?",
        "A character obsessively follows, watches, or harasses another person who has not consented to the attention",
        "A chance encounter; surveillance as part of a job (detective, security); mutual interest",
    ),
    "substance_abuse": (
        "Does this passage depict substance abuse?",
        "A character abuses drugs or alcohol to a harmful degree; intoxication driving behavior; overdose",
        "Social drinking; a single drink; prescribed medication used correctly (that's medical, not abuse)",
    ),
    "torture": (
        "Does this passage depict torture?",
        "A character deliberately inflicts severe pain or suffering on a captive victim, physically or psychologically",
        "A fair fight; punishment without prolonged suffering; threats of torture not carried out",
    ),
    "war": (
        "Does this passage depict war?",
        "Armed conflict between organized armed groups, battles, or war zones described as present events",
        "A single fight; a historical war mentioned in passing; military characters in peacetime",
    ),
    "animal_harm": (
        "Does this passage depict harm to animals?",
        "An animal is injured, killed, or abused on the page",
        "Hunting mentioned in passing; a pet's natural death described gently",
    ),
    "eating_disorder": (
        "Does this passage depict disordered eating?",
        "A character engages in restrictive eating, purging, or obsessive food behaviors described in detail",
        "Dieting mentioned casually; a character skipping a meal",
    ),
    "homophobia": (
        "Does this passage depict homophobia?",
        "A character is targeted with slurs, discrimination, or violence because of their sexuality",
        "A character's sexuality mentioned neutrally; bigotry discussed abstractly without a depicted incident",
    ),
    "racism": (
        "Does this passage depict racism?",
        "A character faces racial slurs, discrimination, or racial violence",
        "A diverse cast without depicted bigotry; historical setting without a depicted incident",
    ),
}
# Per-trigger P(yes) thresholds, tuned from prototype runs (2026-10-09).
# Laya rank-orders correctly but isn't calibrated to 0.5.
_DECISION_TRIGGER_THRESHOLDS = {
    "suicide": 0.82,
    "sexual_violence": 0.20,
    "child_abuse": 0.50,
    "self_harm": 0.35,
    "addiction": 0.50,
    "bullying": 0.50,
    "cheating": 0.35,
    "death_of_loved_one": 0.50,
    "domestic_abuse": 0.50,
    "explicit_sex": 0.50,
    "graphic_violence": 0.50,
    "infidelity": 0.50,
    "kidnapping_captivity": 0.35,
    "medical_trauma": 0.50,
    "miscarriage_pregnancy_loss": 0.50,
    "murder": 0.35,
    "non_consent": 0.50,
    "stalking": 0.35,
    "substance_abuse": 0.50,
    "torture": 0.50,
    "war": 0.50,
    "animal_harm": 0.50,
    "eating_disorder": 0.50,
    "homophobia": 0.50,
    "racism": 0.50,
}

# Two-tier thresholds for per-quote judging.
#   hi: P(yes) >= hi -> keep quote at full claimed severity
#   lo: lo <= P(yes) < hi -> keep quote but downgrade trigger to "mentioned"
#   P(yes) < lo -> discard the quote; drop the category only if no quote survives
# Derived from the base thresholds (lo = hi - 0.15, floored at 0.10) so that
# retuning the base threshold retunes both tiers together.
_DECISION_TRIGGER_THRESHOLDS_HI = dict(_DECISION_TRIGGER_THRESHOLDS)
_DECISION_TRIGGER_THRESHOLDS_LO = {
    k: max(0.10, round(v - 0.15, 2))
    for k, v in _DECISION_TRIGGER_THRESHOLDS.items()
}


def _extract_quote_context(chapter_text, quote, window=300):
    """Return ~window chars of context on each side of quote in chapter text.

    Used to give the decision model surrounding context for a bare evidence
    quote. Returns "" if the quote can't be located.
    """
    if not chapter_text or not quote:
        return ""
    idx = chapter_text.find(quote)
    if idx >= 0:
        start = max(0, idx - window)
        end = min(len(chapter_text), idx + len(quote) + window)
        return chapter_text[start:end]
    # Fallback: normalize whitespace and retry (LLM quotes sometimes
    # collapse newlines).
    norm_text = re.sub(r"\s+", " ", chapter_text)
    norm_quote = re.sub(r"\s+", " ", quote).strip()
    if not norm_quote:
        return ""
    idx = norm_text.find(norm_quote)
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(norm_text), idx + len(norm_quote) + window)
    return norm_text[start:end]


# ---------------------------------------------------------------------------
# Decision-model providers: local Unsloth server vs hosted OpenRouter.
# ---------------------------------------------------------------------------
DECISION_MODEL_PROVIDERS = ("local", "openrouter")
OPENROUTER_SYSTEMONE_URL = "https://openrouter.ai/api/v1/systemone"
# Pinned Jev release; never the "latest" alias (silent upstream changes
# would shift scores and invalidate tuned thresholds).
OPENROUTER_DECISION_MODEL = "typesafe/jev-1.13"
OPENROUTER_ATTRIBUTION_TITLE = "ebook-processor"


def resolve_decision_model(provider, url_override="", config=None, env=None):
    """Resolve decision-model connection details for the given provider.

    Returns (endpoint, model, api_key, extra_headers). Raises ValueError
    with a human-readable message when the provider cannot be configured
    (e.g. missing API key). The key is never logged or printed.
    """
    cfg = config or {}
    environ = env if env is not None else os.environ
    if provider == "openrouter":
        api_key = (cfg.get("openrouter_api_key", "") or
                   cfg.get("openrouter_key", "") or
                   environ.get("OPENROUTER_API_KEY", ""))
        if not api_key:
            raise ValueError(
                "decision-model provider 'openrouter' needs an API key: "
                "set \"openrouter_api_key\" in config.json or the "
                "OPENROUTER_API_KEY environment variable")
        return (OPENROUTER_SYSTEMONE_URL, OPENROUTER_DECISION_MODEL,
                api_key, {"X-Title": OPENROUTER_ATTRIBUTION_TITLE})
    # local (default): Unsloth /v1/systemone, e.g. Laya
    base_url = url_override or cfg.get("openai_base_url", "") or ""
    if not base_url:
        raise ValueError(
            "decision-model provider 'local' needs a base URL: pass "
            "--decision-model-url or set \"openai_base_url\" in config.json")
    return (base_url, "default", "", {})


class DecisionValidator:
    """Validates trigger evidence via a /v1/systemone decision model.

    Stateless per call (each validate() is an independent HTTP request),
    so a single instance is safe to share across --jobs threads.
    On any error, validate() returns (None, None) and the caller falls
    back to the regex gates.
    """

    def __init__(self, base_url, model="default", api_key="", timeout=30,
                 extra_headers=None):
        base = base_url.rstrip("/")
        if base.endswith("/v1/systemone"):
            base = base[:-len("/v1/systemone")]
        elif base.endswith("/v1"):
            base = base[:-3]
        self.endpoint = base + "/v1/systemone"
        self.model = model or "default"
        self.api_key = api_key
        self.extra_headers = dict(extra_headers or {})
        self.timeout = timeout
        self._lock = threading.Lock()
        self._consec_errors = 0
        self._disabled = False
        self._disable_warned = False

    def _record_success(self):
        with self._lock:
            self._consec_errors = 0

    def _record_error(self):
        """Record an error. Returns True if this tripped the circuit breaker."""
        with self._lock:
            self._consec_errors += 1
            if self._consec_errors >= 3 and not self._disabled:
                self._disabled = True
                return True
            return False

    def _note_disabled(self):
        """Print the circuit-breaker warning once (thread-safe)."""
        with self._lock:
            if self._disable_warned:
                return
            self._disable_warned = True
        print("  Decision model disabled after 3 consecutive errors; "
              "falling back to regex for remainder of run")

    @property
    def disabled(self):
        with self._lock:
            return self._disabled

    def _ask_noul(self, question_key, instructions, state, label="decision"):
        """Ask one noul question. Returns p_yes (float) or None on any error.

        Applies strict score validation and the circuit breaker. Never raises;
        a missing/malformed score is an error (caller falls back), never a
        silent negative.
        """
        with self._lock:
            if self._disabled:
                return None
        payload = json.dumps({
            "model": self.model,
            "state": state,
            "questions": {
                question_key: {
                    "type": "noul",
                    "instructions": instructions,
                }
            },
        }).encode()
        headers = {"Content-Type": "application/json"}
        headers.update(self.extra_headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            req = urllib.request.Request(
                self.endpoint, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read())
            ans = result["answers"][question_key]
            p_yes = ans.get("noul")
            if p_yes is None:
                p_yes = ans.get("probabilities", {}).get("yes")
            # Strict score validation: a missing/malformed score is an
            # error (falls back to regex), never a silent negative.
            if p_yes is None:
                raise ValueError("response missing 'noul' score")
            if isinstance(p_yes, bool) or not isinstance(p_yes, (int, float)):
                raise ValueError(f"non-numeric noul score: {p_yes!r}")
            if math.isnan(p_yes) or math.isinf(p_yes):
                raise ValueError(f"non-finite noul score: {p_yes}")
            if not 0 <= p_yes <= 1:
                raise ValueError(f"noul score out of range [0,1]: {p_yes}")
            self._record_success()
            return float(p_yes)
        except Exception as e:
            _tripped = self._record_error()
            if _tripped:
                self._note_disabled()
            print(f"  Decision model error ({label}): {e}; "
                  f"falling back to regex")
            return None

    def validate(self, trigger, quote, context=""):
        """Judge one evidence quote. Returns (verdict_bool, p_yes).

        verdict_bool uses the hi threshold (True = keep at full severity);
        the caller applies the hi/lo tiers per quote. (None, None) on any
        error, in which case the caller falls back to the regex gates.
        """
        q, yes_when, no_when = _DECISION_TRIGGER_DEFS.get(
            trigger, (f"Does this depict {trigger}?", "", ""))
        hi = _DECISION_TRIGGER_THRESHOLDS_HI.get(trigger, 0.5)
        if context:
            _state = f"Context:\n{context}\n\nQuote to judge:\n{quote}"
        else:
            _state = quote
        _instructions = (f"{q} Answer YES when: {yes_when}. "
                         f"Answer NO when: {no_when}.")
        p_yes = self._ask_noul("trigger_check", _instructions, _state,
                               label=trigger)
        if p_yes is None:
            return None, None
        verdict = p_yes >= hi
        print(f"  Decision: {trigger}: P(yes)={p_yes:.2f} "
              f"{'≥' if verdict else '<'} {hi:.2f} → "
              f"{'keep' if verdict else 'drop/downgrade'}")
        return verdict, p_yes

    def ask_same_person(self, name_a, desc_a, ev_a, name_b, desc_b, ev_b):
        """Tier 3 merge judgment: do these two roster entries refer to the
        same person? Returns (merged_bool, p_yes). (None, None) on any
        error, in which case the caller keeps them separate.

        merged_bool uses _MERGE_P_YES_THRESHOLD (deliberately high: a false
        merge corrupts the roster permanently).
        """
        _parts = [f"Character A: {name_a}"]
        if desc_a:
            _parts.append(f"Description A: {desc_a[:600]}")
        if ev_a:
            _parts.append(f"Evidence A: {ev_a[:300]}")
        _parts.append(f"Character B: {name_b}")
        if desc_b:
            _parts.append(f"Description B: {desc_b[:600]}")
        if ev_b:
            _parts.append(f"Evidence B: {ev_b[:300]}")
        _instructions = (
            "Are these two character entries referring to the same person? "
            "Answer YES when: the names are variants of each other (nickname, "
            "shortened form, title + name matching a fuller name, minor "
            "spelling differences) AND the descriptions are compatible. "
            "Answer NO when: different first names, different people sharing "
            "a surname (siblings, parent/child), incompatible descriptions "
            "or roles, or insufficient evidence they are the same individual."
        )
        p_yes = self._ask_noul("same_person", _instructions,
                               "\n".join(_parts), label="merge")
        if p_yes is None:
            return None, None
        merged = p_yes >= _MERGE_P_YES_THRESHOLD
        print(f"  Merge decision: {name_a[:40]} vs {name_b[:40]}: "
              f"P(yes)={p_yes:.2f} {'≥' if merged else '<'} "
              f"{_MERGE_P_YES_THRESHOLD:.2f} → "
              f"{'merge' if merged else 'keep separate'}")
        return merged, p_yes

    def validate_relationship(self, from_name, to_name, rel_type, quote,
                              context=""):
        """Judge whether a quote supports a family relationship claim.

        Used for high-stakes types (spouse/parent/child/sibling) where a
        wrong label is embarrassing. Returns (verdict_bool, p_yes).
        (None, None) on any error, in which case the caller keeps the
        original type (fail open: a validator outage shouldn't rewrite data).
        """
        _instructions = (
            f"Given the quote, is {from_name} the {rel_type} of {to_name}? "
            f"Answer YES when: the quote directly states or clearly implies "
            f"this {rel_type} relationship (e.g. 'my wife', 'his mother', "
            f"'her brother'). "
            f"Answer NO when: the quote is ambiguous, describes a different "
            f"relationship (friend, lover, mistress, colleague, enemy), or "
            f"does not support the claimed {rel_type} relationship."
        )
        _state = f"Quote:\n{quote}"
        if context:
            _state = f"Context:\n{context}\n\nQuote:\n{quote}"
        p_yes = self._ask_noul("relationship_check", _instructions, _state,
                               label=f"rel_{rel_type}")
        if p_yes is None:
            return None, None
        if p_yes >= _REL_VALIDATION_THRESHOLD:
            _action = "keep"
        elif p_yes >= _REL_DROP_THRESHOLD:
            _action = "downgrade to other"
        else:
            _action = "drop"
        print(f"  Relationship validation: {from_name[:30]} -> "
              f"{to_name[:30]} ({rel_type}): P(yes)={p_yes:.2f} → {_action}")
        return p_yes >= _REL_VALIDATION_THRESHOLD, p_yes


# ---------------------------------------------------------------------------
# Cross-run learning: the pipeline gets smarter with every book processed.
#
# LearnedState persists four kinds of knowledge under <learn_dir>/:
#   nicknames.json       short name -> canonical name (e.g. "pete" -> "peter")
#   titles.json          observed title prefixes ("dr", "herr oberstleutnant")
#   threshold_stats.json P(yes) samples per trigger, kept vs dropped
#   series/<key>.json    character rosters keyed by author, for alias hints
#
# All mutations are lock-protected (safe for --jobs threads). Saves are
# atomic (temp file + os.replace). A single shared instance is created in
# main(); _learn() returns a disabled no-op stub when learning is off so
# call sites never need None checks.
# ---------------------------------------------------------------------------

_LEARN_TITLE_TRUST_COUNT = 2  # distinct name-remainders before a title is trusted


# Trope rejection learning: human rejections (from Trope Lab / the app) feed
# back into the pipeline. 3+ global rejections -> never propose again;
# 1-2 rejections -> raise the acceptance confidence threshold by 0.10 each.
_REJECT_DENYLIST_COUNT = 3
_REJECT_CONF_STEP = 0.10
_TROPE_REJECT_BASE_CONF = 0.50


def _trej_key(trope_id):
    """Rejection lookup key: _tnorm with hyphens -> spaces, so
    'enemies-to-lovers' matches 'enemies to lovers'."""
    return _tnorm(trope_id).replace("-", " ")


class LearnedState:
    """Persistent cross-run learning state. Thread-safe. Degrades to no-op
    when disabled or when the learn directory isn't writable."""

    def __init__(self, learn_dir=None, enabled=True):
        # RLock: load/save hold the lock while their helpers
        # (_note_corrupt, _preserve_corrupt) record state under it.
        self._lock = threading.RLock()
        self.enabled = enabled
        if learn_dir:
            self.dir = Path(learn_dir)
        else:
            self.dir = Path(os.path.expanduser("~")) / ".ebook-processor" / "learned"
        self._writable = True
        self._warned = False
        # Files whose load failed (corrupt/unreadable): their original
        # bytes are preserved as .corrupt backups before any overwrite,
        # and pop_summary flags that prior state was not carried over.
        self._corrupt_paths = set()
        self._corrupt_seen = set()
        # In-memory state (mirrors the JSON files).
        self.nicknames = {}       # nkey -> {"full": nkey, "count": int, "books": [isbn]}
        self.titles = {}          # title -> {"count": int, "rests": [str]}
        self.threshold_stats = {}  # trigger -> {"kept": [p], "dropped": [p]}
        self.rejected_tropes = {}  # trej_key -> {"count": int, "books": [book_key], "reasons": [str], "human": True}
        self._titles_cache = None  # cached frozenset of trusted titles
        self._new = Counter()     # fresh learnings this book (for the summary)
        if self.enabled:
            self._ensure_dir()
            self._load_all()

    # -- setup ----------------------------------------------------------
    def _ensure_dir(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "series").mkdir(parents=True, exist_ok=True)
        except Exception:
            self._writable = False
            self._warn_once(
                f"  Learn dir not writable ({self.dir}); learning disabled "
                "for this run")

    def _warn_once(self, msg):
        with self._lock:
            if self._warned:
                return
            self._warned = True
        print(msg)

    # -- persistence ----------------------------------------------------
    def _path(self, name):
        return self.dir / name

    def _note_corrupt(self, path, err):
        """Record a state file that failed to load and say so LOUDLY.

        The store starts empty for that file — silently resetting
        accumulated learning to a plausible fresh state (and then
        overwriting the evidence on save) is how corruption used to
        erase it without a trace. The original bytes are preserved
        as a .corrupt backup before any overwrite (see
        _preserve_corrupt)."""
        path = Path(path)
        with self._lock:
            self._corrupt_paths.add(path)
            first = path.name not in self._corrupt_seen
            self._corrupt_seen.add(path.name)
        if first:
            print(f"  !! WARNING: learned-state file {path} could not "
                  f"be loaded ({err}); starting that store EMPTY. The "
                  f"original file will be preserved as a .corrupt "
                  f"backup before it is overwritten.")

    def _preserve_corrupt(self, path):
        """Back up a corrupt state file's original bytes before it is
        overwritten. Never clobbers an existing backup. No-op for
        files that loaded fine."""
        path = Path(path)
        with self._lock:
            if path not in self._corrupt_paths:
                return
        try:
            if path.exists():
                dest = path.with_name(path.name + ".corrupt")
                n = 1
                while dest.exists():
                    dest = path.with_name(f"{path.name}.corrupt-{n}")
                    n += 1
                shutil.copyfile(path, dest)
                print(f"  !! Preserved corrupt learned-state file "
                      f"as {dest}")
            with self._lock:
                self._corrupt_paths.discard(path)
        except Exception as e:
            print(f"  !! WARNING: could not preserve corrupt file "
                  f"{path} ({e}); it will still be overwritten")

    def _load_json(self, name, default):
        p = self._path(name)
        if not p.exists():
            return default
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self._note_corrupt(p, e)
            return default

    def _save_json(self, name, data):
        """Atomic write: temp file + os.replace. No-op when not writable."""
        if not self._writable:
            return False
        try:
            p = self._path(name)
            self._preserve_corrupt(p)
            tmp = p.with_suffix(p.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
            return True
        except Exception as e:
            self._writable = False
            self._warn_once(f"  Learn dir write failed ({e}); learning "
                            "disabled for this run")
            return False

    def _load_all(self):
        with self._lock:
            self.nicknames = self._load_json("nicknames.json", {})
            self.titles = self._load_json("titles.json", {})
            self.threshold_stats = self._load_json("threshold_stats.json", {})
            self.rejected_tropes = self._load_json("rejected_tropes.json", {})
            self._titles_cache = None

    def save(self):
        """Persist all state. Called at end of each book."""
        if not self.enabled:
            return
        with self._lock:
            self._save_json("nicknames.json", self.nicknames)
            self._save_json("titles.json", self.titles)
            self._save_json("threshold_stats.json", self.threshold_stats)
            self._save_json("rejected_tropes.json", self.rejected_tropes)

    # -- nicknames ------------------------------------------------------
    def record_alias(self, alias_name, canonical_name):
        """Record a human-validated alias (from --alias flag). Cross-book:
        applies to all future books. Human aliases outrank everything."""
        if not self.enabled or not alias_name or not canonical_name:
            return
        short = norm_name(alias_name)
        full = norm_name(canonical_name)
        if not short or not full or short == full:
            return
        with self._lock:
            ent = self.nicknames.get(short)
            if ent is None:
                ent = {"full": full, "count": 0, "books": [],
                       "human": True, "manual": True}
                self.nicknames[short] = ent
                self._new["nicknames"] += 1
            else:
                # Manual alias always wins (it's a fact about the universe).
                ent["full"] = full
                ent["human"] = True
                ent["manual"] = True
            ent["count"] += 1

    def record_merge(self, short_key, full_key, isbn=None):
        """Record that short_key merged into full_key. Called from
        _roster_update on successful merges."""
        if not self.enabled or not short_key or not full_key:
            return
        if short_key == full_key:
            return
        with self._lock:
            ent = self.nicknames.get(short_key)
            if ent is None:
                ent = {"full": full_key, "count": 0, "books": []}
                self.nicknames[short_key] = ent
                self._new["nicknames"] += 1
            elif ent["full"] != full_key:
                # Conflicting mapping: human labels outrank automatic ones.
                if ent.get("human"):
                    return
                # Keep the more-observed one.
                if ent["count"] > 1:
                    return
                ent["full"] = full_key
            ent["count"] += 1
            if isbn and isbn not in ent["books"]:
                ent["books"].append(isbn)

    def record_token_merge(self, short_tok, full_tok, isbn=None):
        """Record a single-token nickname, e.g. 'pete' -> 'peter'."""
        if not self.enabled or not short_tok or not full_tok:
            return
        if short_tok == full_tok or len(short_tok) < 2:
            return
        self.record_merge("@" + short_tok, "@" + full_tok, isbn)

    def nick_lookup(self, nkey):
        """Full-key lookup: returns the learned canonical key or None."""
        if not self.enabled or not nkey:
            return None
        with self._lock:
            ent = self.nicknames.get(nkey)
            return ent["full"] if ent else None

    def token_lookup(self, tok):
        """Single-token lookup: returns the learned full token or None."""
        if not self.enabled or not tok:
            return None
        with self._lock:
            ent = self.nicknames.get("@" + tok)
            if ent:
                full = ent["full"]
                return full[1:] if full.startswith("@") else full
            return None

    def import_labels(self, path):
        """Import hand-labeled merge pairs (from label-merges.py) as
        human-validated nickname mappings. Returns count imported."""
        if not self.enabled:
            return 0
        try:
            with open(path, encoding="utf-8") as f:
                labels = json.load(f)
        except Exception as e:
            print(f"  Could not import labels from {path}: {e}")
            return 0
        n = 0
        for _key, lbl in labels.items():
            if lbl.get("uncertain") or not lbl.get("same_person"):
                continue
            a = norm_name(lbl.get("a", ""))
            b = norm_name(lbl.get("b", ""))
            if not a or not b or a == b:
                continue
            # Direction: shorter -> longer (variant -> canonical).
            short, full = (a, b) if len(a) <= len(b) else (b, a)
            with self._lock:
                ent = self.nicknames.get(short)
                if ent is None:
                    ent = {"full": full, "count": 0, "books": [],
                           "human": True}
                    self.nicknames[short] = ent
                    self._new["nicknames"] += 1
                ent["full"] = full
                ent["count"] += 1
                ent["human"] = True
            n += 1
        return n

    # -- titles ---------------------------------------------------------
    def observe_title(self, candidate, rest):
        """Observe a potential title prefix. Trusted after
        _LEARN_TITLE_TRUST_COUNT distinct name-remainders."""
        if not self.enabled or not candidate or not rest:
            return
        candidate = candidate.lower().strip()
        if (not candidate or candidate in _TITLES
                or candidate in {"the", "a", "an"} or len(candidate) < 2):
            return
        with self._lock:
            ent = self.titles.get(candidate)
            if ent is None:
                ent = {"count": 0, "rests": []}
                self.titles[candidate] = ent
            rest_n = rest.lower().strip()
            if rest_n and rest_n not in ent["rests"]:
                ent["rests"].append(rest_n)
                ent["count"] += 1
                if len(ent["rests"]) == _LEARN_TITLE_TRUST_COUNT:
                    self._new["titles"] += 1
                self._titles_cache = None  # invalidate

    def trusted_titles(self):
        """Frozenset of learned titles trusted for stripping (cached)."""
        if not self.enabled:
            return frozenset()
        with self._lock:
            if self._titles_cache is None:
                self._titles_cache = frozenset(
                    t for t, e in self.titles.items()
                    if len(e.get("rests", [])) >= _LEARN_TITLE_TRUST_COUNT)
            return self._titles_cache

    # -- threshold stats ------------------------------------------------
    def record_threshold(self, trigger, p_yes, kept):
        """Record a decision-model score for future threshold tuning."""
        if not self.enabled:
            return
        try:
            p = float(p_yes)
        except (TypeError, ValueError):
            return
        with self._lock:
            ent = self.threshold_stats.setdefault(
                trigger, {"kept": [], "dropped": []})
            ent["kept" if kept else "dropped"].append(round(p, 4))
            # Cap memory: keep the most recent 500 samples per bucket.
            for k in ("kept", "dropped"):
                if len(ent[k]) > 500:
                    ent[k] = ent[k][-500:]
            self._new["threshold_samples"] += 1

    # -- trope rejections ------------------------------------------------
    def record_trope_rejection(self, trope_id, book_key, reason):
        """Record a human rejection of a trope claim (from Trope Lab or the
        app). Human rejections outrank everything (same pattern as
        --import-labels). Idempotent on (trope, book): re-importing the same
        file does not double-count. Returns True if newly counted."""
        if not self.enabled or not trope_id:
            return False
        key = _trej_key(trope_id)
        if not key:
            return False
        with self._lock:
            ent = self.rejected_tropes.get(key)
            if ent is None:
                ent = {"count": 0, "books": [], "reasons": [],
                       "human": True}
                self.rejected_tropes[key] = ent
                self._new["rejected_tropes"] += 1
            # Idempotency: the same (trope, book) rejection only counts once.
            # A new reason for an already-counted book is still kept.
            dup = bool(book_key) and book_key in ent["books"]
            if not dup:
                ent["count"] += 1
            if book_key and book_key not in ent["books"]:
                ent["books"].append(book_key)
            if reason:
                r = reason[:200]
                if r not in ent["reasons"]:
                    ent["reasons"].append(r)
            return not dup

    def get_trope_rejection_count(self, trope_id):
        """Global rejection count for a trope (0 when disabled/unknown)."""
        if not self.enabled or not trope_id:
            return 0
        with self._lock:
            ent = self.rejected_tropes.get(_trej_key(trope_id))
            return ent["count"] if ent else 0

    def is_trope_denylisted(self, trope_id):
        """True when a trope has been rejected enough times to never propose."""
        return self.get_trope_rejection_count(trope_id) >= _REJECT_DENYLIST_COUNT

    def trope_rejection_threshold(self, trope_id, base=_TROPE_REJECT_BASE_CONF):
        """Acceptance confidence threshold for a trope, raised by
        _REJECT_CONF_STEP per rejection (capped below the denylist)."""
        n = min(self.get_trope_rejection_count(trope_id),
                _REJECT_DENYLIST_COUNT - 1)
        return round(base + _REJECT_CONF_STEP * n, 2)

    def import_rejections(self, path):
        """Import trope rejections from a JSON file (see --import-rejections).
        Accepts a list of {trope_id, book_key, reason} or
        {"rejections": [...]}. Returns count newly recorded."""
        if not self.enabled:
            return 0
        try:
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
        except Exception as e:
            print(f"  Could not import rejections from {path}: {e}")
            return 0
        if isinstance(items, dict):
            items = items.get("rejections", [])
        if not isinstance(items, list):
            print(f"  Could not import rejections from {path}: "
                  "expected a list")
            return 0
        n = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            if self.record_trope_rejection(it.get("trope_id"),
                                           it.get("book_key"),
                                           it.get("reason")):
                n += 1
        return n

    def import_rejections_from_supabase(self):
        """Import rejected trope claims from Supabase (book_trope_claims
        where status='rejected'). Catalog trope_ids are reverse-mapped to
        names via the tropes table. Returns count imported, or -1 when
        Supabase isn't configured/reachable."""
        if not self.enabled:
            return 0
        if not CONFIG.get("supabase_url") or not CONFIG.get("supabase_key"):
            print("  Supabase not configured; cannot import rejections "
                  "from Supabase")
            return -1
        cat_rows = sb("tropes", params="?select=id,name&limit=500")
        if cat_rows is None:
            print("  Could not load trope catalog from Supabase")
            return -1
        id_to_name = {str(r.get("id")): r.get("name", "") for r in cat_rows}
        rows = sb("book_trope_claims",
                  params="?status=eq.rejected&select=trope_id,work_id&limit=2000")
        if rows is None:
            return -1
        n = 0
        for r in rows:
            tid = str(r.get("trope_id") or "")
            name = id_to_name.get(tid, tid)  # fall back to the raw id
            if self.record_trope_rejection(
                    name, r.get("work_id"),
                    "rejected in Trope Lab/app"):
                n += 1
        return n

    # -- series rosters -------------------------------------------------
    @staticmethod
    def series_key(author):
        """Series bucket key. Currently author-based (no series metadata
        in EPUB parsing); per-author buckets with per-book rosters."""
        a = (author or "").strip().casefold()
        if not a:
            return None
        return "author_" + re.sub(r"\W+", "_", a)[:48].strip("_")

    def _series_path(self, series_key):
        safe = re.sub(r"\W+", "_", series_key)[:64].strip("_") or "unknown"
        return self.dir / "series" / (safe + ".json")

    def _load_series_data(self, p):
        """Load a series-roster file for a save-side merge. A corrupt
        file starts empty LOUDLY and its bytes are preserved as a
        .corrupt backup before the overwrite — the same rule as the
        learned files (a silent reset here used to erase the roster
        the merge was supposed to extend)."""
        if not p.exists():
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self._note_corrupt(p, e)
            return {}

    def load_series_hints(self, series_key):
        """Return {nkey: canonical_nkey} alias hints from prior books in
        the series. Empty dict when disabled/unknown."""
        if not self.enabled or not series_key:
            return {}
        p = self._series_path(series_key)
        if not p.exists():
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self._note_corrupt(p, e)
            return {}
        hints = {}
        for ch in data.get("characters", []):
            canon = norm_name(ch.get("name", ""))
            if not canon:
                continue
            for al in ch.get("aliases", []):
                ak = norm_name(al)
                if ak and ak != canon:
                    hints.setdefault(ak, canon)
        return hints

    def save_series_roster(self, series_key, title, author, isbn, characters):
        """Persist this book's character roster into the series bucket."""
        if not self.enabled or not series_key:
            return
        p = self._series_path(series_key)
        data = self._load_series_data(p)
        data.setdefault("characters", [])
        data.setdefault("books", [])
        seen = {norm_name(c.get("name", "")) for c in data["characters"]}
        added = 0
        for c in characters or []:
            nk = norm_name(c.get("name", ""))
            if not nk or nk in seen:
                continue
            seen.add(nk)
            aliases = sorted({a for a in (c.get("aliases") or [])
                              if a and norm_name(a) != nk})
            data["characters"].append({"name": c.get("name", ""),
                                       "aliases": aliases})
            added += 1
        if isbn and not any(b.get("isbn") == isbn for b in data["books"]):
            data["books"].append({"isbn": isbn, "title": title})
        # Atomic write.
        try:
            self._preserve_corrupt(p)
            tmp = p.with_suffix(p.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except Exception as e:
            self._warn_once(f"  Could not save series roster ({e})")
            return
        if added:
            with self._lock:
                self._new["series_chars"] += added

    @staticmethod
    def series_name_key(series_name):
        """Bucket key for a named series (e.g. 'Daemon'). Distinct from the
        author-based series_key(); used for cross-book character continuity
        within a detected series."""
        s = (series_name or "").strip().casefold()
        if not s:
            return None
        return re.sub(r"\W+", "_", s)[:48].strip("_") or None

    def _named_series_path(self, series_key):
        """Path: <learn_dir>/series/{normalized}/roster.json"""
        safe = re.sub(r"\W+", "_", series_key)[:64].strip("_") or "unknown"
        return self.dir / "series" / safe / "roster.json"

    def load_series_name_hints(self, series_key):
        """Return {nkey: canonical_nkey} alias hints from prior books in the
        named series. Empty dict when disabled/unknown."""
        if not self.enabled or not series_key:
            return {}
        p = self._named_series_path(series_key)
        if not p.exists():
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self._note_corrupt(p, e)
            return {}
        hints = {}
        for ch in data.get("characters", []):
            canon = norm_name(ch.get("name", ""))
            if not canon:
                continue
            for al in ch.get("aliases", []):
                ak = norm_name(al)
                if ak and ak != canon:
                    hints.setdefault(ak, canon)
        return hints

    def save_series_name_roster(self, series_key, title, author, isbn,
                                series_position, characters):
        """Persist this book's roster into the named-series bucket."""
        if not self.enabled or not series_key:
            return
        p = self._named_series_path(series_key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass  # the write below surfaces the failure via _warn_once
        data = self._load_series_data(p)
        data.setdefault("characters", [])
        data.setdefault("books", [])
        data["series"] = series_key
        seen = {norm_name(c.get("name", "")) for c in data["characters"]}
        added = 0
        for c in characters or []:
            nk = norm_name(c.get("name", ""))
            if not nk or nk in seen:
                continue
            seen.add(nk)
            aliases = sorted({a for a in (c.get("aliases") or [])
                              if a and norm_name(a) != nk})
            data["characters"].append({"name": c.get("name", ""),
                                       "aliases": aliases})
            added += 1
        if isbn and not any(b.get("isbn") == isbn for b in data["books"]):
            data["books"].append({"isbn": isbn, "title": title,
                                  "position": series_position})
        # Atomic write.
        try:
            self._preserve_corrupt(p)
            tmp = p.with_suffix(p.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except Exception as e:
            self._warn_once(f"  Could not save named-series roster ({e})")
            return
        if added:
            with self._lock:
                self._new["series_chars"] += added

    # -- summary --------------------------------------------------------
    def pop_summary(self):
        """Return 'Learned: ...' summary of this book's fresh learnings and
        reset the counters. Empty string when nothing was learned."""
        with self._lock:
            parts = []
            n = self._new.get("nicknames", 0)
            if n:
                parts.append(f"{n} nickname{'s' if n != 1 else ''}")
            t = self._new.get("titles", 0)
            if t:
                parts.append(f"{t} title{'s' if t != 1 else ''}")
            s = self._new.get("threshold_samples", 0)
            if s:
                parts.append(f"{s} threshold sample{'s' if s != 1 else ''}")
            sc = self._new.get("series_chars", 0)
            if sc:
                parts.append(f"{sc} series character{'s' if sc != 1 else ''}")
            rj = self._new.get("rejected_tropes", 0)
            if rj:
                parts.append(f"{rj} trope rejection{'s' if rj != 1 else ''}")
            self._new.clear()
            corrupt = sorted(self._corrupt_seen)
        if not parts:
            return ""
        summary = "Learned: " + ", ".join(parts)
        if corrupt:
            # This run's counts are real, but the store they joined
            # started empty for these files — the summary must not
            # read as if the prior state had carried over.
            summary += (f" (warning: {len(corrupt)} learned file(s) "
                        f"failed to load this run — prior state not "
                        f"carried over; originals preserved as .corrupt "
                        f"backups)")
        return summary


# ---------------------------------------------------------------------------
# Option F: staging store (empty-evidence staging gate + persistence gate).
#
# Design: eval/f-staging-design.md. Doctrine, binding on everything
# below: persistence is a stability filter, not truth. A promoted claim
# is a claim that stopped flickering, not a claim that was verified.
# Promotion never raises a claim's confidence, never applies to family
# relationships (the helper REFUSES those classes — there is no code
# path from staging to a relationship row), and never overrides a
# denylist, a rejection, or a human curation state.
#
# StagingState is per-work RUN BOOKKEEPING, not learning: entries
# expire, learning accumulates. It shares LearnedState's directory and
# its F8 persistence discipline (atomic save, loud corruption notice,
# .corrupt preservation) but is a separate class and file
# (staging.json), deliberately unentangled from the learning
# governance questions. Every failure direction is delay-only:
# corruption, splits and lost state can postpone a claim, never ship
# one early — promotion requires state the failure destroyed.
# ---------------------------------------------------------------------------

_STAGEABLE_CLASSES = ("trope", "trigger")
_STAGING_EXPIRE_ABSENCES = 3    # completed runs unsighted -> expired
_STAGING_STALE_DAYS = 180       # report flag; no wall-clock expiry
_STAGING_EXPIRED_CAP = 200      # retained expired records per work
_STAGING_SIZE_WARN = 5 * 1024 * 1024


def staging_claim_key(cls, name=None, catalog_id=None, warning=None):
    """Identity key for a stageable claim (design section 3).

    Tropes anchor on the catalog id when the claim resolves to one
    (paraphrases collapse to one id, surviving display-form flips);
    otherwise the normalized name. Triggers key on the normalized
    warning name. Any other claim class — relationships above all —
    is refused: staging has no identity for them and must never grow
    one in v1 (endpoint identity is the unsolved roster problem)."""
    if cls == "trope":
        if catalog_id:
            return f"trope:id:{catalog_id}"
        return f"trope:name:{_tnorm(name)}"
    if cls == "trigger":
        return f"trigger:{(warning or '').lower().strip()}"
    raise ValueError(
        f"claim class {cls!r} is not stageable: only trope and "
        "trigger claims stage in v1; relationships/family claims are "
        "never staged or promoted (fail-closed has no bypass)")


def staging_work_key(work_id=None, isbn=None, title="", author=""):
    """Store key for a work (design section 3): the catalog work_id
    when resolved, else the ISBN, else normalized title|author. The
    weaker keys are a fallback for previewing un-ingested books, not
    a second identity system: entries under them are adopted into
    the work_id entry when a run resolves the id (OC-8)."""
    if work_id:
        return str(work_id)
    if isbn:
        return f"isbn:{isbn}"
    return f"title:{_tnorm(title)}|{_tnorm(author)}"


def _staging_day(s):
    """ISO date string -> ordinal day number (None when unparsable)."""
    if not s:
        return None
    try:
        from datetime import date
        y, m, d = (int(p) for p in str(s).split("-"))
        return date(y, m, d).toordinal()
    except (ValueError, TypeError):
        return None


def _staging_today():
    from datetime import date
    return date.today().isoformat()


class StagingState:
    """Per-work staging bookkeeping. Thread-safe: observe+save are
    serialized under an RLock (books run concurrently under --jobs;
    an unsynchronized load-modify-save would lose sightings — delay
    only, but promotion timing would go nondeterministic). Degrades
    to no-claims-ship-differently when the directory isn't writable:
    the seam checks .enabled/.writable and skips staging entirely."""

    FILE_NAME = "staging.json"

    def __init__(self, learn_dir=None, enabled=True):
        # RLock: observe holds the lock while its helpers record
        # state under it (same lesson as LearnedState, sweep F8).
        self._lock = threading.RLock()
        self.enabled = enabled
        if learn_dir:
            self.dir = Path(learn_dir)
        else:
            self.dir = Path(os.path.expanduser("~")) / ".ebook-processor" / "learned"
        self._writable = True
        self._warned = False
        self._corrupt_paths = set()
        self._corrupt_seen = set()
        self.data = {"version": 1, "works": {}}
        if self.enabled:
            self._ensure_dir()
            self._load()

    # -- setup / persistence (LearnedState's F8 pattern) --------------
    def _ensure_dir(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self._writable = False
            self._warn_once(
                f"  Staging dir not writable ({self.dir}); staging "
                "disabled for this run")

    def _warn_once(self, msg):
        with self._lock:
            if self._warned:
                return
            self._warned = True
        print(msg)

    @property
    def writable(self):
        return self._writable

    @property
    def corrupt_at_load(self):
        return bool(self._corrupt_seen)

    def _path(self):
        return self.dir / self.FILE_NAME

    def _note_corrupt(self, path, err):
        """Record a staging file that failed to load and say so
        LOUDLY. The store starts empty — the failure direction is
        safe by construction: lost state means sightings reset and
        promotions are DELAYED, because promotion requires state the
        corruption destroyed. The original bytes are preserved as a
        .corrupt backup before any overwrite."""
        path = Path(path)
        with self._lock:
            self._corrupt_paths.add(path)
            first = path.name not in self._corrupt_seen
            self._corrupt_seen.add(path.name)
        if first:
            print(f"  !! WARNING: staging file {path} could not be "
                  f"loaded ({err}); starting the staging store EMPTY "
                  f"(claims will re-stage; nothing can promote "
                  f"early). The original file will be preserved as a "
                  f".corrupt backup before it is overwritten.")

    def _preserve_corrupt(self, path):
        """Back up a corrupt staging file's original bytes before it
        is overwritten. Never clobbers an existing backup."""
        path = Path(path)
        with self._lock:
            if path not in self._corrupt_paths:
                return
        try:
            if path.exists():
                dest = path.with_name(path.name + ".corrupt")
                n = 1
                while dest.exists():
                    dest = path.with_name(f"{path.name}.corrupt-{n}")
                    n += 1
                shutil.copyfile(path, dest)
                print(f"  !! Preserved corrupt staging file as {dest}")
            with self._lock:
                self._corrupt_paths.discard(path)
        except Exception as e:
            print(f"  !! WARNING: could not preserve corrupt file "
                  f"{path} ({e}); it will still be overwritten")

    def _load(self):
        p = self._path()
        if not p.exists():
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) \
                    or not isinstance(data.get("works"), dict):
                raise ValueError("staging store has no 'works' map")
            self.data = {"version": 1, "works": data["works"]}
        except Exception as e:
            self._note_corrupt(p, e)
            self.data = {"version": 1, "works": {}}

    def save(self):
        """Atomic write: temp file + os.replace. No-op when not
        writable. A size guard warns past 5 MB (the realistic growth
        driver is works never reprocessed; the report shows them)."""
        if not self._writable or not self.enabled:
            return False
        with self._lock:
            try:
                p = self._path()
                self._preserve_corrupt(p)
                tmp = p.with_suffix(p.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, p)
                try:
                    if p.stat().st_size > _STAGING_SIZE_WARN:
                        self._warn_once(
                            f"  Staging store is large "
                            f"({p.stat().st_size} bytes); run "
                            "--staging-report to see what is trapped")
                except OSError:
                    pass
                return True
            except Exception as e:
                self._writable = False
                self._warn_once(
                    f"  Staging dir write failed ({e}); staging "
                    "disabled for this run")
                return False

    # -- work entries ---------------------------------------------------
    @staticmethod
    def _new_work(work_id, title, author):
        return {"work_id": work_id, "title": title or "",
                "author": author or "", "runs_completed": 0,
                "staged_ever": 0, "claims": {}, "expired_recent": [],
                "expired_dropped": 0, "adoptions": []}

    @staticmethod
    def _new_claim(cls, display, today):
        return {"class": cls, "display": display or "",
                "name_forms": [display] if display else [],
                "sightings": [], "first_seen": today,
                "last_seen": today, "best_chapters": 0,
                "evidence_ever": False, "state": "staged",
                "promoted_run": None, "promoted_via": None,
                "promoted_form_changed": False}

    def work_entry(self, work_key, work_id=None, title="", author=""):
        """Fetch-or-create a work entry (call under the lock)."""
        w = self.data["works"].get(work_key)
        if w is None:
            w = self._new_work(work_id, title, author)
            self.data["works"][work_key] = w
        else:
            if work_id and not w.get("work_id"):
                w["work_id"] = work_id
            if title and not w.get("title"):
                w["title"] = title
            if author and not w.get("author"):
                w["author"] = author
        return w

    # -- adoption (OC-8) --------------------------------------------------
    def _adopt_fallbacks(self, work, work_id, title, author, isbn):
        """Adopt fallback-key entries into the work_id entry when a
        work first resolves its id. Exact title+author match only
        (normalized); adoptions are recorded on the surviving entry
        so the next trapped report lists them — a wrong adoption
        must be visible to be correctable."""
        if not work_id:
            return
        candidates = []
        if isbn:
            candidates.append(f"isbn:{isbn}")
        candidates.append(
            f"title:{_tnorm(title)}|{_tnorm(author)}")
        for cand in candidates:
            src = self.data["works"].get(cand)
            if src is None or src is work:
                continue
            if _tnorm(src.get("title")) != _tnorm(title) \
                    or _tnorm(src.get("author")) != _tnorm(author):
                continue
            moved = self._merge_work_into(work, src)
            del self.data["works"][cand]
            if moved:
                work["adoptions"].append(
                    {"from_key": cand, "claims_moved": moved})

    def _merge_work_into(self, dst, src):
        """Fold src's claims into dst (adoption). The two entries
        partition the work's runs (each run observes under exactly
        one key), so src's sighting ordinals SHIFT by dst's completed
        run count before unioning — a fallback entry recreated fresh
        each run numbers its run 1 every time, and a plain union
        would silently eat every later sighting (delay-only, but it
        would break promotion for exactly the books adoption serves).
        staged_ever stays exact: a claim key live in BOTH entries was
        one claim counted on both sides, so each such overlap
        subtracts one duplicate; a key expired on one side and live
        on the other is two staging episodes (expired, then
        re-staged) and counts twice, exactly as observe counts a
        re-stage. Promotion is sticky. Returns claims moved/merged."""
        moved = 0
        dup = 0
        shift = dst["runs_completed"]
        for key, entry in list(src["claims"].items()):
            moved += 1
            if shift:
                entry["sightings"] = [s + shift
                                      for s in entry["sightings"]]
                if entry.get("promoted_run"):
                    entry["promoted_run"] += shift
            cur = dst["claims"].get(key)
            if cur is None:
                dst["claims"][key] = entry
            else:
                self._fold_entry_into(cur, entry)
                dup += 1
        for rec in src["expired_recent"]:
            if shift and rec.get("expired_run"):
                rec["expired_run"] += shift
            if all(r["key"] != rec["key"]
                   for r in dst["expired_recent"]):
                dst["expired_recent"].append(rec)
            else:
                dup += 1
        dst["expired_dropped"] = dst.get("expired_dropped", 0) \
            + src.get("expired_dropped", 0)
        dst["staged_ever"] += src["staged_ever"] - dup
        while len(dst["expired_recent"]) > _STAGING_EXPIRED_CAP:
            dst["expired_recent"].pop(0)
            dst["expired_dropped"] += 1
        dst["runs_completed"] = shift + src["runs_completed"]
        return moved

    def adopt_for(self, work_id, title="", author="", isbn=None):
        """Post-resolution adoption (OC-8) for callers that learn the
        work_id only AFTER observing under a fallback key — the
        pipeline resolves works after extraction, so the observe seam
        never has the id. Creates no entry when nothing waits to be
        adopted. Returns True when an adoption happened."""
        if not work_id or not self.enabled:
            return False
        with self._lock:
            dst_key = str(work_id)
            candidates = []
            if isbn:
                candidates.append(f"isbn:{isbn}")
            candidates.append(
                f"title:{_tnorm(title)}|{_tnorm(author)}")
            if not any(c in self.data["works"] for c in candidates
                       if c != dst_key):
                return False
            dst = self.work_entry(dst_key, work_id, title, author)
            before = len(dst["adoptions"])
            self._adopt_fallbacks(dst, work_id, title, author, isbn)
            if len(dst["adoptions"]) == before:
                return False
            self.save()
            return True

    @staticmethod
    def _fold_entry_into(dst, src):
        """Merge src claim entry into dst (same claim, two keyings).
        Sightings union (ordinals are per-work; a run contributes at
        most one ordinal per claim, so union is exact)."""
        dst["sightings"] = sorted(set(dst["sightings"])
                                   | set(src["sightings"]))
        for form in src["name_forms"]:
            if _tnorm(form) not in {_tnorm(f)
                                    for f in dst["name_forms"]}:
                dst["name_forms"].append(form)
        dst["best_chapters"] = max(dst["best_chapters"],
                                   src["best_chapters"])
        dst["evidence_ever"] = dst["evidence_ever"] \
            or src["evidence_ever"]
        for f in ("first_seen",):
            a, b = _staging_day(dst.get(f)), _staging_day(src.get(f))
            if b is not None and (a is None or b < a):
                dst[f] = src[f]
        a, b = _staging_day(dst.get("last_seen")), \
            _staging_day(src.get("last_seen"))
        if b is not None and (a is None or b > a):
            dst["last_seen"] = src["last_seen"]
        if src["state"] == "promoted" and dst["state"] != "promoted":
            dst["state"] = "promoted"
            dst["promoted_run"] = src["promoted_run"]
            dst["promoted_via"] = src["promoted_via"]
            dst["promoted_form_changed"] = \
                src["promoted_form_changed"]

    # -- observe ----------------------------------------------------------
    def observe_work(self, *, work_key, work_id=None, title="",
                     author="", isbn=None, tropes=(), triggers=(),
                     auto_min=3, independent=True, today=None):
        """Record one completed run's stageable claims for a work and
        decide this run's ship/hold split.

        tropes:   [{"name", "n_ch", "catalog_id"}]
        triggers: [{"warning", "has_evidence"}]

        A sighting counts only on an INDEPENDENT run (fresh
        extraction — no checkpoint payloads reused). A resumed or
        retried run re-derives the same extraction; counting it would
        manufacture persistence out of one observation. Non-independent
        runs still get the split from current store state (the run is
        a continuation of its original) and may move last_seen, but
        they create no entries, record no sightings, promote nothing
        and expire nothing.

        Returns {"trope_actions", "trigger_actions", "staged",
        "promoted", "expired_this_run", "sightings_recorded",
        "trapped_total"}. Actions align with the input lists:
        {"action": "ship"|"hold", "key", "stamp", "form_changed"}.
        Serialized under the lock, save included."""
        today = today or _staging_today()
        with self._lock:
            if not independent \
                    and work_key not in self.data["works"]:
                # A continuation of a run this store never saw: no
                # entries to continue, nothing may be created.
                ship = {"action": "ship", "key": None, "stamp": None,
                        "form_changed": False}
                return {"trope_actions": [dict(ship) for _ in tropes],
                        "trigger_actions": [dict(ship)
                                            for _ in triggers],
                        "staged": [], "promoted": [],
                        "expired_this_run": 0, "sightings_recorded": 0,
                        "trapped_total": 0}
            work = self.work_entry(work_key, work_id, title, author)
            self._adopt_fallbacks(work, work_id, title, author, isbn)
            out = {"trope_actions": [], "trigger_actions": [],
                   "staged": [], "promoted": [], "expired_this_run": 0,
                   "sightings_recorded": 0, "trapped_total": 0}
            ordinal = None
            if independent:
                work["runs_completed"] += 1
                ordinal = work["runs_completed"]
            for obs in tropes:
                act = self._observe_trope(work, obs, auto_min,
                                          independent, ordinal, today,
                                          out)
                out["trope_actions"].append(act)
            for obs in triggers:
                act = self._observe_trigger(work, obs, independent,
                                            ordinal, today, out)
                out["trigger_actions"].append(act)
            if independent:
                out["expired_this_run"] = self._expire_absent(
                    work, ordinal)
            out["trapped_total"] = sum(
                1 for e in work["claims"].values()
                if e["state"] == "staged")
            self.save()
            return out

    def _resolve_trope_entry(self, work, obs):
        """Find (entry, key, form_changed) for a trope observation,
        folding a name-keyed entry into its catalog-id entry when the
        claim now resolves (union of ordinals — exact, never summed).
        Creates nothing; returns (None, key, False) when unsighted."""
        name = obs.get("name") or ""
        cat = obs.get("catalog_id")
        key_name = staging_claim_key("trope", name=name)
        if not cat:
            return work["claims"].get(key_name), key_name, False
        key_id = staging_claim_key("trope", name=name, catalog_id=cat)
        claims = work["claims"]
        entry = claims.get(key_id)
        name_entry = claims.get(key_name)
        if name_entry is not None:
            if entry is None:
                # The entry MOVES to the id key; it was always one
                # claim, so staged_ever does not change.
                entry = name_entry
                claims[key_id] = entry
                del claims[key_name]
            else:
                # Two entries were one claim sighted under two keys.
                self._fold_entry_into(entry, name_entry)
                del claims[key_name]
                work["staged_ever"] -= 1
        key = key_id or key_name
        entry = claims.get(key)
        form_changed = False
        if entry is not None and key_id:
            forms = {_tnorm(f) for f in entry["name_forms"]}
            form_changed = _tnorm(name) not in forms
        return entry, key, form_changed

    def _record_sighting(self, work, entry, ordinal, today, obs,
                         name):
        if ordinal not in entry["sightings"]:
            entry["sightings"].append(ordinal)
            entry["sightings"].sort()
            recorded = True
        else:
            recorded = False
        entry["last_seen"] = today
        if name and _tnorm(name) not in {
                _tnorm(f) for f in entry["name_forms"]}:
            entry["name_forms"].append(name)
        return recorded

    def _promote(self, work, entry, ordinal, via, form_changed, out,
                 display):
        entry["state"] = "promoted"
        entry["promoted_run"] = ordinal
        entry["promoted_via"] = via
        entry["promoted_form_changed"] = bool(form_changed)
        item = {"key": None, "display": display or entry["display"],
                "via": via, "sightings": len(entry["sightings"])}
        if form_changed:
            item["form_changed"] = True
        out["promoted"].append(item)
        return item

    def _observe_trope(self, work, obs, auto_min, independent, ordinal,
                       today, out):
        name = obs.get("name") or ""
        n_ch = obs.get("n_ch") or 0
        entry, key, form_changed = self._resolve_trope_entry(work, obs)
        if entry is None:
            if not independent or n_ch >= auto_min:
                # Non-independent runs create no entries (the run is
                # a continuation); auto-band claims never stage.
                return {"action": "ship", "key": None, "stamp": None,
                        "form_changed": False}
            entry = self._new_claim("trope", name, today)
            entry["best_chapters"] = n_ch
            work["claims"][key] = entry
            work["staged_ever"] += 1
            if self._record_sighting(work, entry, ordinal, today,
                                     obs, name):
                out["sightings_recorded"] += 1
            out["staged"].append(
                {"key": key, "display": name, "class": "trope",
                 "sightings": len(entry["sightings"])})
            return {"action": "hold", "key": key, "stamp": None,
                    "form_changed": False}
        # Existing entry.
        if independent:
            if self._record_sighting(work, entry, ordinal, today,
                                     obs, name):
                out["sightings_recorded"] += 1
            entry["best_chapters"] = max(entry["best_chapters"], n_ch)
        else:
            entry["last_seen"] = today
        if entry["state"] == "promoted":
            stamp = None
            if n_ch < auto_min:
                # Gate-band ship explained by earned standing alone:
                # the stamp is how a reviewer sees that (OC-7).
                stamp = {"sightings": len(entry["sightings"]),
                         "via": entry["promoted_via"]}
            return {"action": "ship", "key": key, "stamp": stamp,
                    "form_changed": False}
        # Staged entry: promotion rules (independent sightings only;
        # a non-independent run adds no sighting, so nothing new can
        # promote — the checks below simply don't fire for it because
        # len(sightings) did not change... except support: a resumed
        # run re-derives the same n_ch its original run saw, which
        # already had its chance; gate on independent explicitly).
        if independent and n_ch >= auto_min:
            item = self._promote(work, entry, ordinal, "support",
                                 form_changed, out, name)
            item["key"] = key
            return {"action": "ship", "key": key, "stamp": None,
                    "form_changed": form_changed}
        if independent and len(entry["sightings"]) >= 2:
            item = self._promote(work, entry, ordinal, "persistence",
                                 form_changed, out, name)
            item["key"] = key
            stamp = {"sightings": len(entry["sightings"]),
                     "via": "persistence"}
            return {"action": "ship", "key": key, "stamp": stamp,
                    "form_changed": form_changed}
        out["staged"].append(
            {"key": key, "display": name, "class": "trope",
             "sightings": len(entry["sightings"])})
        return {"action": "hold", "key": key, "stamp": None,
                "form_changed": False}

    def _observe_trigger(self, work, obs, independent, ordinal, today,
                         out):
        warning = obs.get("warning") or ""
        has_ev = bool(obs.get("has_evidence"))
        key = staging_claim_key("trigger", warning=warning)
        entry = work["claims"].get(key)
        if entry is None:
            if not independent or has_ev:
                # Evidenced triggers never stage; non-independent
                # runs create no entries.
                return {"action": "ship", "key": None, "stamp": None,
                        "form_changed": False}
            entry = self._new_claim("trigger", warning, today)
            entry["evidence_ever"] = False
            work["claims"][key] = entry
            work["staged_ever"] += 1
            if self._record_sighting(work, entry, ordinal, today,
                                     obs, warning):
                out["sightings_recorded"] += 1
            out["staged"].append(
                {"key": key, "display": warning, "class": "trigger",
                 "sightings": len(entry["sightings"])})
            return {"action": "hold", "key": key, "stamp": None,
                    "form_changed": False}
        if independent:
            if self._record_sighting(work, entry, ordinal, today,
                                     obs, warning):
                out["sightings_recorded"] += 1
        else:
            entry["last_seen"] = today
        entry["evidence_ever"] = entry["evidence_ever"] or has_ev
        if entry["state"] == "promoted":
            stamp = None
            if not has_ev:
                stamp = {"sightings": len(entry["sightings"]),
                         "via": entry["promoted_via"]}
            return {"action": "ship", "key": key, "stamp": stamp,
                    "form_changed": False}
        if independent and has_ev:
            item = self._promote(work, entry, ordinal, "evidence",
                                 False, out, warning)
            item["key"] = key
            return {"action": "ship", "key": key, "stamp": None,
                    "form_changed": False}
        if independent and len(entry["sightings"]) >= 2:
            item = self._promote(work, entry, ordinal, "persistence",
                                 False, out, warning)
            item["key"] = key
            stamp = {"sightings": len(entry["sightings"]),
                     "via": "persistence"}
            return {"action": "ship", "key": key, "stamp": stamp,
                    "form_changed": False}
        out["staged"].append(
            {"key": key, "display": warning, "class": "trigger",
             "sightings": len(entry["sightings"])})
        return {"action": "hold", "key": key, "stamp": None,
                "form_changed": False}

    def _expire_absent(self, work, ordinal):
        """Expire staged entries unseen for 3 completed runs of this
        work. Absence evidence, not the clock: a never-reprocessed
        book accrues no absences and its claims wait, visibly."""
        n_expired = 0
        for key, entry in list(work["claims"].items()):
            if entry["state"] != "staged":
                continue
            last = max(entry["sightings"]) if entry["sightings"] else 0
            if ordinal - last >= _STAGING_EXPIRE_ABSENCES:
                work["expired_recent"].append(
                    {"key": key, "display": entry["display"],
                     "class": entry["class"], "expired_run": ordinal,
                     "sightings": len(entry["sightings"])})
                del work["claims"][key]
                n_expired += 1
        while len(work["expired_recent"]) > _STAGING_EXPIRED_CAP:
            work["expired_recent"].pop(0)
            work["expired_dropped"] = work.get("expired_dropped", 0) + 1
        return n_expired

    def consume_adoptions(self):
        """Return all pending adoption records and clear them (the
        trapped report lists each adoption once)."""
        with self._lock:
            out = []
            for key, w in self.data["works"].items():
                for rec in w.get("adoptions", []):
                    out.append({"work_key": key, **rec})
                w["adoptions"] = []
            if out:
                self.save()
            return out


def staging_report_data(state, today=None):
    """Pure read model over a StagingState for --staging-report and
    the dashboard: per-work buckets that must satisfy
    staged_ever = promoted + still_staged + expired, with the
    difference surfaced as `unaccounted` (printed; must be 0)."""
    today = today or _staging_today()
    today_ord = _staging_day(today)
    works_out = []
    totals = {"works": 0, "staged_ever": 0, "promoted": 0, "staged": 0,
              "expired": 0, "form_changed_promotions": 0}
    unaccounted = 0
    for key in sorted(state.data.get("works", {})):
        w = state.data["works"][key]
        claims_out = []
        n_promoted = n_staged = 0
        fc_promotions = 0
        for ckey in sorted(w["claims"]):
            e = w["claims"][ckey]
            if e["state"] == "promoted":
                n_promoted += 1
                if e.get("promoted_form_changed"):
                    fc_promotions += 1
            else:
                n_staged += 1
            first = _staging_day(e.get("first_seen"))
            last = _staging_day(e.get("last_seen"))
            forms = {_tnorm(f) for f in e.get("name_forms", [])}
            claims_out.append({
                "key": ckey, "display": e["display"],
                "class": e["class"], "state": e["state"],
                "sightings": list(e["sightings"]),
                "sightings_count": len(e["sightings"]),
                "best_chapters": e["best_chapters"],
                "first_seen": e.get("first_seen"),
                "last_seen": e.get("last_seen"),
                "age_days": (today_ord - first)
                            if (today_ord and first) else 0,
                "stale": bool(
                    e["state"] == "staged" and today_ord and last
                    and today_ord - last >= _STAGING_STALE_DAYS),
                "forms_differ": len(forms) > 1,
                "promoted_via": e.get("promoted_via"),
                "promoted_form_changed": bool(
                    e.get("promoted_form_changed")),
            })
        n_expired = len(w["expired_recent"]) \
            + w.get("expired_dropped", 0)
        w_unacc = w["staged_ever"] - (n_promoted + n_staged
                                      + n_expired)
        unaccounted += w_unacc
        works_out.append({
            "key": key, "work_id": w.get("work_id"),
            "title": w.get("title", ""), "author": w.get("author", ""),
            "runs_completed": w["runs_completed"],
            "staged_ever": w["staged_ever"], "promoted": n_promoted,
            "staged": n_staged, "expired": n_expired,
            "expired_retained": len(w["expired_recent"]),
            "unaccounted": w_unacc,
            "form_changed_promotions": fc_promotions,
            "claims": claims_out,
            "expired_recent": list(w["expired_recent"]),
            "adoptions": list(w.get("adoptions", [])),
        })
        totals["works"] += 1
        totals["staged_ever"] += w["staged_ever"]
        totals["promoted"] += n_promoted
        totals["staged"] += n_staged
        totals["expired"] += n_expired
        totals["form_changed_promotions"] += fc_promotions
    return {"works": works_out, "totals": totals,
            "unaccounted": unaccounted,
            "corrupt_at_load": state.corrupt_at_load}


def staging_observe_result(parts, state, *, independent):
    """Option F seam helper (design section 5): observe one completed
    run's stageable claims and compute the ship set. Called ONCE per
    run from process_file_v2; result["tropes"]/["triggers"] become
    the returned ship set and every downstream consumer inherits the
    split.

    The pre-staging hard filters run here for the gated candidates,
    exactly as they do for any claim: rejection learning is applied
    with the pipeline's own formula confidence (the gate block applied
    it to the ship candidates), so a claim the filter drops is never
    observed and never staged. Denylisted candidates never reach this
    helper at all (the gate block's split already excluded them).

    parts keys: work_key, work_id, title, author, isbn, auto_min,
    trope_auto, trope_gated, trope_counts, trope_ship, trope_conf,
    catalog_map, triggers.
    Returns {ship_tropes, trope_conf, ship_triggers, marker,
    trope_persistence, trigger_persistence}."""
    counts = parts.get("trope_counts") or {}
    cat_map = parts.get("catalog_map") or {}
    gated = list(parts.get("trope_gated") or [])
    if gated:
        _gconf = {_tnorm(c): round(
            min(0.9, 0.55 + 0.1 * counts.get(_tnorm(c), 1)), 2)
            for c in gated}
        gated, _gconf, _nf = _apply_trope_rejections(
            gated, _gconf, _learn())
    obs_tropes = [{"name": c,
                   "n_ch": counts.get(_tnorm(c), 1),
                   "catalog_id": cat_map.get(_tnorm(c))}
                  for c in list(parts.get("trope_auto") or []) + gated]
    triggers_in = list(parts.get("triggers") or [])
    obs_triggers = []
    for t in triggers_in:
        if isinstance(t, dict):
            obs_triggers.append({
                "warning": t.get("warning") or "",
                "has_evidence": bool(t.get("evidence"))})
        else:
            obs_triggers.append({"warning": str(t),
                                 "has_evidence": False})
    out = state.observe_work(
        work_key=parts["work_key"], work_id=parts.get("work_id"),
        title=parts.get("title", ""), author=parts.get("author", ""),
        isbn=parts.get("isbn"), tropes=obs_tropes,
        triggers=obs_triggers, auto_min=parts.get("auto_min", 3),
        independent=independent)
    acts = {}
    for obs, act in zip(obs_tropes, out["trope_actions"]):
        acts[_tnorm(obs["name"])] = act
    ship_src = list(parts.get("trope_ship") or [])
    ship_tropes = [t for t in ship_src
                   if (acts.get(_tnorm(t)) or {}).get("action")
                   == "ship"]
    in_ship = {_tnorm(t) for t in ship_tropes}
    for c in list(parts.get("trope_auto") or []) + gated:
        if _tnorm(c) not in in_ship \
                and (acts.get(_tnorm(c)) or {}).get("action") == "ship":
            ship_tropes.append(c)
            in_ship.add(_tnorm(c))
    conf = {k: v for k, v in (parts.get("trope_conf") or {}).items()
            if k in in_ship}
    for t in ship_tropes:
        k = _tnorm(t)
        if k not in conf:
            # A claim shipping via promotion carries the pipeline's
            # own formula confidence for its support — never boosted,
            # and identical to what a gate "yes" would have given it.
            conf[k] = round(min(0.9, 0.55 + 0.1
                                * counts.get(k, 1)), 2)
    trope_persistence = {}
    for obs, act in zip(obs_tropes, out["trope_actions"]):
        if act.get("stamp"):
            trope_persistence[_tnorm(obs["name"])] = act["stamp"]
    ship_triggers = []
    trigger_persistence = {}
    for t, act in zip(triggers_in, out["trigger_actions"]):
        if act.get("action") == "ship":
            ship_triggers.append(t)
            if act.get("stamp"):
                w = t.get("warning") if isinstance(t, dict) else str(t)
                trigger_persistence[str(w).lower()] = act["stamp"]
    marker = {"staged": out["staged"], "promoted": out["promoted"],
              "trapped_total": out["trapped_total"],
              "expired_this_run": out["expired_this_run"],
              "sightings_recorded": out["sightings_recorded"],
              "trapped_store_total":
                  staging_report_data(state)["totals"]["staged"]}
    return {"ship_tropes": ship_tropes, "trope_conf": conf,
            "ship_triggers": ship_triggers, "marker": marker,
            "trope_persistence": trope_persistence,
            "trigger_persistence": trigger_persistence}


def _staging_report_text(state, today=None, data=None):
    """The trapped report (design section 6 of the spec's reporting
    surface): every staged claim named with its sightings and best
    support, promotions with their via, form-changed promotions on
    their own line (OC-6), expiries and adoptions listed, and the
    accounting identity closed in the open. Pure over the read
    model."""
    if data is None:
        data = staging_report_data(state, today=today)
    lines = ["Staging report (local staging store):"]
    if data["corrupt_at_load"]:
        lines.append(
            "  WARNING: staging.json failed to load and the store "
            "started EMPTY (original preserved as a .corrupt backup). "
            "Sightings were reset — store loss delays promotion, it "
            "never causes one.")
    t = data["totals"]
    if not data["works"]:
        lines.append("  Nothing trapped: the staging store is empty.")
    lines.append(
        f"  Totals: {t['works']} work(s), {t['staged_ever']} claim(s) "
        f"ever staged, {t['promoted']} promoted, {t['staged']} still "
        f"staged, {t['expired']} expired")
    for w in data["works"]:
        lines.append(
            f"  {w['title'] or w['key']} by {w['author'] or '?'} "
            f"[{w['key']}] — {w['runs_completed']} completed run(s)")
        for c in w["claims"]:
            if c["state"] == "staged":
                stale = " STALE" if c["stale"] else ""
                lines.append(
                    f"    STAGED {c['class']}: {c['display']} — "
                    f"sightings {c['sightings_count']}, best support "
                    f"{c['best_chapters']} ch, last seen "
                    f"{c['last_seen']}{stale}")
        for c in w["claims"]:
            if c["state"] == "promoted":
                fc = (" [name form changed between sightings]"
                      if c["promoted_form_changed"] else "")
                lines.append(
                    f"    PROMOTED {c['class']}: {c['display']} "
                    f"(via {c['promoted_via']}){fc}")
        if w["form_changed_promotions"]:
            lines.append(
                f"    Form-changed promotions: "
                f"{w['form_changed_promotions']}")
        for rec in w["expired_recent"]:
            lines.append(
                f"    EXPIRED {rec['class']}: {rec['display']} "
                f"(sightings {rec['sightings']})")
        for ad in w["adoptions"]:
            lines.append(
                f"    ADOPTED: {ad['claims_moved']} claim(s) from "
                f"{ad['from_key']}")
    lines.append(
        f"  UNACCOUNTED: {data['unaccounted']} (must be 0: "
        f"staged_ever = promoted + still staged + expired)")
    return "\n".join(lines)


def cmd_staging_report(as_json=False, learn_dir=None, state=None):
    """Print the trapped report (for --staging-report). Adoptions are
    listed once: they are consumed after being reported, so the next
    report does not repeat them."""
    if state is None:
        state = StagingState(learn_dir=learn_dir, enabled=True)
    data = staging_report_data(state)
    text = None if as_json else _staging_report_text(state, data=data)
    adoptions = state.consume_adoptions()
    if as_json:
        data["adoptions"] = adoptions
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(text)


# ---------------------------------------------------------------------------
# Option B — persisted per-work roster (design: eval/b-roster-design.md).
#
# Identity is a hypothesis with provenance, never a fact the store
# asserts. A persisted link says "run N's entry X was judged the same
# person as entity E, by tier T, on this evidence" — recorded,
# reversible, and re-vetted against every later run's evidence. Names
# are observations; identity is the thing observed. Unlike F's staging
# store, this store holds authority (it can change WHO a row is
# about), so it does not claim F's delay-only failure property: a
# missing/corrupt store degrades to the stateless path (safe), and a
# loadable-but-wrong store is countered by provenance, suspension,
# anchors and the roster report — every exercise of the store's
# authority is attributed and reversible.
# ---------------------------------------------------------------------------

_ROSTER_SIZE_WARN = 5 * 1024 * 1024


class RosterVetoError(ValueError):
    """A recorded union would contradict a human NO. Raised by
    RosterStore itself (the invariant lives in the store, not in
    callers, so no future code path can re-form a vetoed identity
    by forgetting to check)."""


def roster_desc_hash(text):
    """Short hash of a run's best description for an entry/entity, so
    description continuity is checkable across runs without storing
    prose (design section 3)."""
    import hashlib as _hl
    if not text:
        return ""
    return _hl.sha1(text.encode("utf-8")).hexdigest()[:12]


def _roster_store_filename(work_key):
    """Filesystem name for a work's roster store file. The work_key
    itself is stored inside the file and is authoritative; the name
    is a deterministic sanitization (plus a hash suffix whenever
    sanitization changed anything, so distinct keys can't collide)."""
    import hashlib as _hl
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(work_key)).strip("_")
    if not safe:
        safe = "work"
    if safe != str(work_key):
        safe = f"{safe}-{_hl.sha1(str(work_key).encode()).hexdigest()[:8]}"
    return safe + ".json"


class RosterStore:
    """One work's persisted roster identity: minted entity IDs,
    name observations with provenance, first-class link records,
    curation anchors, merge tombstones and a suspension ledger.

    One JSON file per work under <learn_dir>/rosters/, persistence
    discipline copied from StagingState (F8: atomic save, loud
    corruption with .corrupt preservation, RLock). NOT a
    LearnedState file and NOT the staging file: cross-run roster
    links live and die inside this per-work store and never become
    cross-book learning (design section 7, D1 containment).
    """

    def __init__(self, learn_dir=None, work_key=None, enabled=True):
        self._lock = threading.RLock()
        self.enabled = enabled
        if learn_dir:
            base = Path(learn_dir)
        else:
            base = Path(os.path.expanduser("~")) / ".ebook-processor" \
                / "learned"
        self.dir = base / "rosters"
        self.work_key = work_key
        self._writable = True
        self._warned = False
        self._corrupt_paths = set()
        self._corrupt_seen = set()
        self.data = self._empty_data(work_key)
        if self.enabled:
            self._ensure_dir()
            if work_key is not None:
                self._load()

    @staticmethod
    def _empty_data(work_key):
        return {"version": 1, "work_key": work_key, "work_id": None,
                "title": "", "author": "", "runs_completed": 0,
                "last_run_fingerprint": None, "next_entity_seq": 1,
                "minted_total": 0, "entities": {}, "retired": {},
                "no_pairs": [], "adoptions": []}

    # -- setup / persistence (StagingState's F8 pattern) --------------
    def _ensure_dir(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self._writable = False
            self._warn_once(
                f"  Roster dir not writable ({self.dir}); roster "
                "persistence disabled for this run")

    def _warn_once(self, msg):
        with self._lock:
            if self._warned:
                return
            self._warned = True
        print(msg)

    @property
    def writable(self):
        return self._writable

    @property
    def corrupt_at_load(self):
        return bool(self._corrupt_seen)

    @property
    def path(self):
        return self.dir / _roster_store_filename(self.work_key or "")

    @property
    def runs_completed(self):
        return self.data["runs_completed"]

    def _note_corrupt(self, path, err):
        """Record a roster file that failed to load and say so LOUDLY.
        The store starts EMPTY: the run degrades to the stateless path
        (today's behaviour) and says so on its face. Identity history
        is lost; correctness is not. Original bytes are preserved as a
        .corrupt backup before any overwrite."""
        path = Path(path)
        with self._lock:
            self._corrupt_paths.add(path)
            first = path.name not in self._corrupt_seen
            self._corrupt_seen.add(path.name)
        if first:
            print(f"  !! WARNING: roster store {path} could not be "
                  f"loaded ({err}); starting this work's roster store "
                  f"EMPTY (the run is stateless for this work). The "
                  f"original file will be preserved as a .corrupt "
                  f"backup before it is overwritten.")

    def _preserve_corrupt(self, path):
        path = Path(path)
        with self._lock:
            if path not in self._corrupt_paths:
                return
        try:
            if path.exists():
                dest = path.with_name(path.name + ".corrupt")
                n = 1
                while dest.exists():
                    dest = path.with_name(f"{path.name}.corrupt-{n}")
                    n += 1
                shutil.copyfile(path, dest)
                print(f"  !! Preserved corrupt roster store as {dest}")
            with self._lock:
                self._corrupt_paths.discard(path)
        except Exception as e:
            print(f"  !! WARNING: could not preserve corrupt file "
                  f"{path} ({e}); it will still be overwritten")

    def _load(self):
        p = self.path
        if not p.exists():
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) \
                    or not isinstance(data.get("entities"), dict) \
                    or not isinstance(data.get("retired"), dict):
                raise ValueError("roster store has no entity maps")
            empty = self._empty_data(self.work_key)
            empty.update(data)
            empty["work_key"] = self.work_key
            self.data = empty
        except Exception as e:
            self._note_corrupt(p, e)
            self.data = self._empty_data(self.work_key)

    def save(self):
        """Atomic write: temp file + os.replace. No-op when not
        writable/enabled."""
        if not self._writable or not self.enabled \
                or self.work_key is None:
            return False
        with self._lock:
            try:
                p = self.path
                self._preserve_corrupt(p)
                tmp = p.with_suffix(p.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, p)
                try:
                    if p.stat().st_size > _ROSTER_SIZE_WARN:
                        self._warn_once(
                            f"  Roster store is large "
                            f"({p.stat().st_size} bytes); run "
                            "--roster-report to inspect it")
                except OSError:
                    pass
                return True
            except Exception as e:
                self._writable = False
                self._warn_once(
                    f"  Roster store write failed ({e}); roster "
                    "persistence disabled for this run")
                return False

    # -- entities -------------------------------------------------------
    def mint_entity(self):
        """Mint a fresh opaque entity ID. IDs are never derived from
        a name, never reused, and never deleted (a merged-away ID
        tombstones in retired with a redirect)."""
        with self._lock:
            eid = f"ent_{self.data['next_entity_seq']:04d}"
            self.data["next_entity_seq"] += 1
            self.data["minted_total"] += 1
            self.data["entities"][eid] = {
                "canonical": None, "canonical_source": None,
                "status": "active", "split_from": None, "split_into": [],
                "anchor": None, "observations": [], "links": []}
            return eid

    def get_entity(self, eid):
        return self.data["entities"].get(eid)

    def resolve_id(self, eid):
        """Follow merged_into redirects to the surviving entity ID
        (None when the ID is unknown). Split entities do not
        redirect: their successors carry the observations, so
        matching finds them directly."""
        seen = set()
        cur = eid
        while cur in self.data["retired"]:
            if cur in seen:
                return None
            seen.add(cur)
            cur = self.data["retired"][cur].get("merged_into")
        return cur if cur in self.data["entities"] else None

    def set_canonical(self, eid, name, source):
        with self._lock:
            ent = self.data["entities"][eid]
            ent["canonical"] = name
            ent["canonical_source"] = source

    # -- observations ---------------------------------------------------
    def append_observation(self, eid, obs):
        """Append one run's observation of an entity (at most one per
        entity per run; the caller mints the run ordinal via
        claim_run). Stored verbatim apart from the suspect/detached
        flags, which default to False."""
        with self._lock:
            o = dict(obs)
            o.setdefault("suspect", False)
            o.setdefault("detached", False)
            o["chapters"] = sorted(o.get("chapters") or [])
            o["aliases"] = sorted(o.get("aliases") or [])
            o["alias_keys"] = sorted(o.get("alias_keys") or [])
            self.data["entities"][eid]["observations"].append(o)
            return o

    def claim_run(self, fingerprint):
        """Claim the run ordinal for a completing run. Returns
        (ordinal, repeat): when the roster fingerprint equals the
        last completed run's, this is the SAME extraction completing
        again (resume-after-crash at the seam boundary) — the ordinal
        is reused, runs_completed does not advance, and the run's
        prior records are purged so re-recording refreshes in place
        instead of double-counting one extraction (design section 7).
        """
        with self._lock:
            if fingerprint is not None \
                    and fingerprint == self.data["last_run_fingerprint"] \
                    and self.data["runs_completed"] > 0:
                ordinal = self.data["runs_completed"]
                self._purge_run(ordinal)
                return ordinal, True
            self.data["runs_completed"] += 1
            self.data["last_run_fingerprint"] = fingerprint
            return self.data["runs_completed"], False

    def _purge_run(self, ordinal):
        for ent in self.data["entities"].values():
            ent["observations"] = [
                o for o in ent["observations"] if o.get("run") != ordinal]
            ent["links"] = [
                l for l in ent["links"] if l.get("run") != ordinal]

    # -- human NO (store-level invariant) --------------------------------
    def record_no(self, form_a, form_b, source=""):
        """Record a human NO: these two name forms are NOT the same
        person. The store refuses any later union that would re-form
        this pair's identity (links, folds, merge tombstones,
        adoptions) — see union_veto."""
        a, b = norm_name(form_a), norm_name(form_b)
        if not a or not b or a == b:
            return False
        with self._lock:
            if self.has_no(a, b):
                return False
            self.data["no_pairs"].append(
                {"a": a, "b": b, "source": source or ""})
            return True

    def has_no(self, form_a, form_b):
        a, b = norm_name(form_a), norm_name(form_b)
        return any({p["a"], p["b"]} == {a, b}
                   for p in self.data["no_pairs"])

    def union_veto(self, forms_a, forms_b):
        """The recorded NO a union of these two form sets would
        contradict, or None. A union is contradicted when one side
        of a NO pair sits in each set."""
        sa = {norm_name(f) for f in forms_a if f}
        sb = {norm_name(f) for f in forms_b if f}
        for p in self.data["no_pairs"]:
            if (p["a"] in sa and p["b"] in sb) \
                    or (p["a"] in sb and p["b"] in sa):
                return p
        return None

    def _entity_form_keys(self, eid, include_detached=False):
        """Every form key an entity claims: observed primary forms,
        observed alias keys, and its canonical key. Detached
        observations (a human removed that form from this entity)
        no longer speak for it."""
        ent = self.data["entities"].get(eid)
        if ent is None:
            return set()
        keys = set()
        for o in ent["observations"]:
            if o.get("detached") and not include_detached:
                continue
            keys.add(o["form_key"])
            keys.update(o.get("alias_keys") or [])
        if ent.get("canonical"):
            keys.add(norm_name(ent["canonical"]))
        anchor = ent.get("anchor")
        if anchor:
            keys.add(norm_name(anchor["name"]))
            keys.update(norm_name(a) for a in anchor.get("aliases") or [])
        return keys

    def _check_union(self, forms_a, forms_b, what):
        veto = self.union_veto(forms_a, forms_b)
        if veto is not None:
            raise RosterVetoError(
                f"{what} refused: it would re-form an identity a human "
                f"vetoed ({veto['a']!r} is not {veto['b']!r}"
                f"{'; ' + veto['source'] if veto.get('source') else ''})")

    # -- links ------------------------------------------------------------
    def record_link(self, eid, link, entry_forms):
        """Record a cross-run link between a run entry (entry_forms:
        its form key + claimed alias keys) and an entity. Refused by
        the store itself when the union contradicts a recorded NO.
        The link record carries run, tier, state and a human-readable
        evidence string — a link with no recorded basis does not
        exist."""
        with self._lock:
            self._check_union(set(entry_forms),
                              self._entity_form_keys(eid), "link")
            rec = {"run": link.get("run"), "tier": link.get("tier"),
                   "state": link.get("state", "active"),
                   "evidence": link.get("evidence", ""),
                   "form_key": link.get("form_key")}
            self.data["entities"][eid]["links"].append(rec)
            return rec

    def suspend_link(self, eid, run, form_key, evidence):
        """Suspend the active link(s) this run's entry (form_key)
        made into the entity, and flag the observations that link
        carried as suspect (A1: a suspended link's observations stop
        voting — excluded from canonical weighting and tier-2 alias
        evidence until a human resolves them). Returns how many
        links were suspended."""
        with self._lock:
            ent = self.data["entities"].get(eid)
            if ent is None:
                return 0
            n = 0
            for link in ent["links"]:
                if link.get("run") == run \
                        and link.get("form_key") == form_key \
                        and link.get("state") == "active":
                    link["state"] = "suspended"
                    link["evidence"] = (
                        f"{link.get('evidence', '')} | suspended: "
                        f"{evidence}").strip(" |")
                    n += 1
            if n:
                self.flag_suspect_observations(eid, run, form_key)
            return n

    def suspension_ledger(self):
        """Suspended links plus the entities they touch — reported
        as its own line, OUTSIDE the ID partition (suspension is a
        link state, not an entity status; design A4)."""
        out = []
        for eid, ent in self.data["entities"].items():
            for link in ent["links"]:
                if link.get("state") == "suspended":
                    out.append({"entity": eid, "link": dict(link)})
        return out

    def flag_suspect_observations(self, eid, run, form_key):
        with self._lock:
            ent = self.data["entities"].get(eid)
            if ent is None:
                return
            for o in ent["observations"]:
                if o.get("run") == run \
                        and o.get("form_key") == form_key:
                    o["suspect"] = True

    def resolve_suspect(self, eid, form_key):
        """A human affirmed this form belongs to this entity: its
        observations vote again."""
        with self._lock:
            ent = self.data["entities"].get(eid)
            if ent is None:
                return
            for o in ent["observations"]:
                if o.get("form_key") == form_key:
                    o["suspect"] = False

    # -- detach / split / merge -------------------------------------------
    def detach_form(self, eid, form_key, run=None):
        """A human removed this form from this entity (--roster-
        unlink): the form's observations are flagged detached (they
        stop speaking for the entity in every view, permanently
        until a human re-links), and NOs are recorded between the
        form and every other form the entity claims, so the union
        cannot silently re-form. Returns the NO records added."""
        form_key = norm_name(form_key)
        added = []
        with self._lock:
            ent = self.data["entities"].get(eid)
            if ent is None:
                return added
            others = self._entity_form_keys(eid) - {form_key}
            for o in ent["observations"]:
                if o.get("form_key") == form_key:
                    o["detached"] = True
                    o["suspect"] = False
            for other in sorted(others):
                if self.record_no(form_key, other,
                                   source="--roster-unlink"):
                    added.append({"a": form_key, "b": other})
            return added

    def record_merge(self, survivor_id, folded_id, run, reason):
        """Tombstone folded_id into survivor_id: the folded entity
        leaves `entities` and is recorded ONCE in `retired` with a
        merged_into redirect; its observations and links are
        preserved under the survivor. Refused by the store itself
        when the union contradicts a recorded human NO."""
        with self._lock:
            if survivor_id == folded_id:
                return False
            surv = self.data["entities"].get(survivor_id)
            fold = self.data["entities"].get(folded_id)
            if surv is None or fold is None:
                return False
            self._check_union(self._entity_form_keys(survivor_id),
                              self._entity_form_keys(folded_id),
                              "merge")
            surv["observations"].extend(fold["observations"])
            surv["links"].extend(fold["links"])
            if surv.get("anchor") is None and fold.get("anchor"):
                surv["anchor"] = fold["anchor"]
                surv["canonical"] = fold["canonical"]
                surv["canonical_source"] = fold["canonical_source"]
            self.data["retired"][folded_id] = {
                "merged_into": survivor_id, "run": run,
                "reason": reason}
            del self.data["entities"][folded_id]
            return True

    def record_split(self, eid, form_keys, run=None):
        """Partition an entity's observations by form: each listed
        form's observations move to a fresh successor entity
        (split_from = eid). When no observations remain, the
        original tombstones as status `split` with split_into
        naming the successors; otherwise it stays active with the
        remainder. Returns {form_key: successor_id}."""
        wanted = [norm_name(f) for f in form_keys]
        out = {}
        with self._lock:
            ent = self.data["entities"].get(eid)
            if ent is None:
                return out
            for fk in wanted:
                mine = [o for o in ent["observations"]
                        if o.get("form_key") == fk]
                if not mine:
                    continue
                ent["observations"] = [
                    o for o in ent["observations"]
                    if o.get("form_key") != fk]
                succ = self.mint_entity()
                sent = self.data["entities"][succ]
                sent["split_from"] = eid
                sent["observations"] = mine
                sent["canonical"] = mine[-1].get("form")
                sent["canonical_source"] = "rule"
                ent["split_into"].append(succ)
                out[fk] = succ
            if out and not ent["observations"]:
                ent["status"] = "split"
            return out

    # -- anchors ------------------------------------------------------------
    def seed_anchor(self, name, kind, source, run, aliases=()):
        """Fetch-or-create the anchored entity for a human decision
        (a preserved DB row, or a manual --alias). The anchor's key
        set is closed and exact (A3): the anchor name's form_key
        plus the form keys of the aliases recorded for it — no token
        decomposition. Re-seeding refreshes seen_run. Returns the
        entity ID."""
        form_key = norm_name(name)
        keyset = {form_key} | {norm_name(a) for a in aliases if a}
        with self._lock:
            for eid, ent in self.data["entities"].items():
                anchor = ent.get("anchor")
                if anchor:
                    aset = {norm_name(anchor["name"])} | {
                        norm_name(a) for a in anchor.get("aliases") or []}
                    if aset & keyset:
                        anchor["seen_run"] = run
                        return eid
            for eid, ent in self.data["entities"].items():
                if ent.get("status") != "active":
                    continue
                primaries = {o["form_key"] for o in ent["observations"]
                             if not o.get("detached")}
                if primaries & keyset:
                    ent["anchor"] = {
                        "kind": kind, "name": name, "source": source,
                        "aliases": sorted(aliases), "seen_run": run}
                    ent["canonical"] = name
                    ent["canonical_source"] = "anchor"
                    return eid
            eid = self.mint_entity()
            ent = self.data["entities"][eid]
            ent["anchor"] = {"kind": kind, "name": name,
                             "source": source,
                             "aliases": sorted(aliases),
                             "seen_run": run}
            ent["canonical"] = name
            ent["canonical_source"] = "anchor"
            return eid

    def anchored_entities(self):
        return {eid: ent["anchor"]
                for eid, ent in self.data["entities"].items()
                if ent.get("anchor")}

    # -- views (matcher input) ----------------------------------------------
    def entity_views(self):
        """Per-entity matching views over ACTIVE entities. Primary
        forms come from every non-detached observation; tier-2 alias
        evidence (`alias_keys`) counts only attestations NOT carried
        solely by suspect observations (A1), while `alias_keys_all`
        keeps the full set for reporting. desc_hash is the latest
        trusted (non-suspect) observation's."""
        views = []
        with self._lock:
            for eid, ent in self.data["entities"].items():
                if ent.get("status") != "active":
                    continue
                primary, alias_trusted, alias_all = set(), set(), set()
                chapters = set()
                appearances = 0
                desc_hash = ""
                for o in ent["observations"]:
                    if o.get("detached"):
                        continue
                    primary.add(o["form_key"])
                    alias_all.update(o.get("alias_keys") or [])
                    chapters.update(o.get("chapters") or [])
                    appearances += o.get("appearances") or 0
                    if not o.get("suspect"):
                        alias_trusted.update(o.get("alias_keys") or [])
                        if o.get("desc_hash"):
                            desc_hash = o["desc_hash"]
                views.append({
                    "id": eid,
                    "canonical": ent.get("canonical"),
                    "canonical_key": norm_name(ent["canonical"] or ""),
                    "anchor": ent.get("anchor"),
                    "primary_forms": primary,
                    "alias_keys": alias_trusted,
                    "alias_keys_all": alias_all,
                    "chapters": chapters,
                    "appearances": appearances,
                    "desc_hash": desc_hash,
                    "provisional": self._is_provisional(ent),
                })
            return views

    @staticmethod
    def _is_provisional(ent):
        return sum(1 for o in ent["observations"]
                   if not o.get("detached")) < 2

    def is_provisional(self, eid):
        ent = self.data["entities"].get(eid)
        return bool(ent) and self._is_provisional(ent)

    # -- adoption -------------------------------------------------------------
    def adopt_store(self, src):
        """Adopt another store's contents into this one (work-key
        adoption on work_id resolution, design section 8): entities
        are reminted under this store's sequence, redirects and
        split_from are rewritten, NO records union in, and the
        adoption is recorded on this store. Refused wholesale when
        any entity on either side embodies a union the combined NO
        set vetoes — adoption must not smuggle a vetoed identity
        past the store-level invariant. Returns entities moved."""
        with self._lock:
            nos = list(self.data["no_pairs"]) + list(src.data["no_pairs"])
            for store in (self, src):
                for ent in store.data["entities"].values():
                    forms = set()
                    for o in ent["observations"]:
                        forms.add(o["form_key"])
                        forms.update(o.get("alias_keys") or [])
                    for p in nos:
                        if p["a"] in forms and p["b"] in forms:
                            raise RosterVetoError(
                                "adoption refused: an entity embodies "
                                f"a vetoed union ({p['a']!r} is not "
                                f"{p['b']!r})")
            id_map = {}
            for old_id in src.data["entities"]:
                id_map[old_id] = self.mint_entity()
                # mint_entity counted a fresh mint; adoption MOVES an
                # identity, it does not mint one — the combined
                # store's minted_total is the two stores' sum, set
                # explicitly below.
                self.data["minted_total"] -= 1
            for old_id, ent in src.data["entities"].items():
                new_ent = self.data["entities"][id_map[old_id]]
                moved = json.loads(json.dumps(ent))
                moved["split_from"] = id_map.get(ent.get("split_from"),
                                                 ent.get("split_from"))
                moved["split_into"] = [
                    id_map.get(s, s) for s in ent.get("split_into") or []]
                new_ent.update(moved)
            n_retired = 0
            for old_id, rec in src.data["retired"].items():
                new_id = f"ent_{self.data['next_entity_seq']:04d}"
                self.data["next_entity_seq"] += 1
                n_retired += 1
                self.data["retired"][new_id] = {
                    "merged_into": id_map.get(rec.get("merged_into"),
                                              rec.get("merged_into")),
                    "run": rec.get("run"), "reason": rec.get("reason")}
            for p in src.data["no_pairs"]:
                self.record_no(p["a"], p["b"], source=p.get("source", ""))
            self.data["minted_total"] += src.data["minted_total"]
            self.data["runs_completed"] = max(
                self.data["runs_completed"], src.data["runs_completed"])
            if src.work_key:
                self.data["adoptions"].append(
                    {"from_key": src.work_key,
                     "entities_moved": len(id_map) + n_retired})
            return len(id_map) + n_retired

    # -- accounting -----------------------------------------------------------
    def accounting(self):
        """The ID partition, closed in the open (F's accounting
        discipline applied to IDs): minted = active + merged + split
        with UNACCOUNTED: 0. Suspension is a link state and is NOT
        part of this partition; it has its own ledger."""
        with self._lock:
            active = sum(1 for e in self.data["entities"].values()
                         if e.get("status") == "active")
            split = sum(1 for e in self.data["entities"].values()
                        if e.get("status") == "split")
            merged = len(self.data["retired"])
            minted = self.data["minted_total"]
            return {"minted": minted, "active": active,
                    "merged": merged, "split": split,
                    "unaccounted": minted - (active + merged + split)}


def roster_store_paths(learn_dir=None):
    """All roster store files under the learned dir's rosters/
    (for --roster-report and the correction commands, which find a
    work's store by scanning: the file's stored work_key is
    authoritative, not its name)."""
    if learn_dir:
        base = Path(learn_dir)
    else:
        base = Path(os.path.expanduser("~")) / ".ebook-processor" \
            / "learned"
    rdir = base / "rosters"
    if not rdir.is_dir():
        return []
    return sorted(rdir.glob("*.json"))


def load_roster_store(learn_dir, work_key):
    return RosterStore(learn_dir=learn_dir, work_key=work_key)


def find_roster_store(learn_dir, work):
    """Find the store for a <work> argument (exact work_key, else
    work_id, else title — first unambiguous match). Returns
    (RosterStore, None) or (None, error_message)."""
    matches = []
    for path in roster_store_paths(learn_dir):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        wk = data.get("work_key")
        store = RosterStore(learn_dir=learn_dir, work_key=wk)
        if work in (wk, data.get("work_id")):
            return store, None
        if work and _tnorm(work) == _tnorm(data.get("title") or ""):
            matches.append(store)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, (f"work {work!r} matches {len(matches)} roster "
                      "stores by title; use the work_id or work key")
    return None, f"no roster store found for work {work!r}"


def adopt_roster_store(learn_dir, work_id, title="", author="",
                       isbn=None):
    """Work-key adoption on work_id resolution (the F adopt_for
    pattern, per-work files): a store recorded under a fallback key
    (isbn / title|author) is adopted into the work_id store once a
    run resolves the id — merged in when both files exist, renamed
    when only the fallback does. Exact title+author match only.
    Adoptions are recorded on the surviving store so the next
    roster report lists them. Returns True when an adoption
    happened."""
    if not work_id:
        return False
    candidates = []
    if isbn:
        candidates.append(f"isbn:{isbn}")
    candidates.append(f"title:{_tnorm(title)}|{_tnorm(author)}")
    dst_key = str(work_id)
    adopted = False
    for cand in candidates:
        if cand == dst_key:
            continue
        src = RosterStore(learn_dir=learn_dir, work_key=cand)
        if not src.path.exists() or src.corrupt_at_load:
            continue
        if _tnorm(src.data.get("title")) != _tnorm(title) \
                or _tnorm(src.data.get("author")) != _tnorm(author):
            continue
        dst = RosterStore(learn_dir=learn_dir, work_key=dst_key)
        if not dst.path.exists():
            # Rename: same store, new key — no reminting needed.
            src.data["work_key"] = dst_key
            src.data["work_id"] = str(work_id)
            src.data["adoptions"].append(
                {"from_key": cand, "entities_moved":
                 len(src.data["entities"]) + len(src.data["retired"])})
            old_path = src.path
            src.work_key = dst_key  # .path now resolves to the new key
            src.save()              # writes the store under the new key
            try:
                if old_path != src.path and old_path.exists():
                    old_path.unlink()
            except OSError:
                pass
            adopted = True
            continue
        try:
            dst.adopt_store(src)
        except RosterVetoError as e:
            print(f"  Roster adoption refused for {cand}: {e}")
            continue
        if not dst.data.get("work_id"):
            dst.data["work_id"] = str(work_id)
        dst.save()
        try:
            src.path.unlink()
        except OSError:
            pass
        adopted = True
    return adopted


# Module-level staging state, mirroring _learn(): _staging() always
# returns a usable object (disabled stub when --staging-gate is off),
# rooted at the learned-state directory so staging.json sits beside
# the learning files. Tests may assign _STAGING_STATE directly.
_STAGING_STATE = None


def _staging():
    global _STAGING_STATE
    if _STAGING_STATE is None:
        if CONFIG.get("staging_gate", False):
            _STAGING_STATE = StagingState(_learn().dir)
        else:
            _STAGING_STATE = StagingState(enabled=False)
    return _STAGING_STATE


# Module-level learning state, created in main() when --learn is on.
# _learn() always returns a usable object (disabled stub when off), so
# call sites never need None checks.
_LEARNED = None


def _learn():
    global _LEARNED
    if _LEARNED is None:
        _LEARNED = LearnedState(enabled=False)
    return _LEARNED


def _count_series_rosters(state):
    """(n_series, n_characters) from the <learn_dir>/series/ tree.

    Counts each series bucket once: author-based flat files
    (series/*.json) and named-series rosters (series/*/roster.json).
    Pure (no I/O beyond reading); returns (0, 0) when the dir is absent.
    """
    series_dir = state.dir / "series"
    if not series_dir.is_dir():
        return 0, 0
    n_series = 0
    n_chars = 0
    seen = set()
    for p in sorted(series_dir.rglob("*.json")):
        if p.name.endswith(".tmp"):
            continue
        # Bucket key: flat file -> stem; roster.json -> parent dir name.
        # Prevents double-counting if both layouts ever overlap.
        if p.name == "roster.json":
            key = p.parent.name
        else:
            key = p.stem
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        n_series += 1
        chars = data.get("characters", [])
        if isinstance(chars, list):
            n_chars += len(chars)
    return n_series, n_chars


def _learn_status_text(state):
    """Human-readable summary of a LearnedState. Pure (no I/O)."""
    lines = [f"Learning system status ({state.dir}/):"]
    # Nicknames (human-validated subset) and manual aliases.
    nicks = state.nicknames or {}
    n_total = len(nicks)
    n_human = sum(1 for e in nicks.values()
                  if isinstance(e, dict) and e.get("human"))
    lines.append(f"  Nicknames: {n_total} ({n_human} human-validated)")
    n_manual = sum(1 for e in nicks.values()
                   if isinstance(e, dict) and e.get("manual"))
    lines.append(f"  Manual aliases: {n_manual}")
    # Trusted titles.
    try:
        n_titles = len(state.trusted_titles())
    except Exception:
        n_titles = 0
    lines.append(f"  Trusted titles: {n_titles}")
    # Rejected tropes (denylisted subset, with names).
    rej = state.rejected_tropes or {}
    n_rej = len(rej)
    deny_names = sorted(
        k for k, e in rej.items()
        if isinstance(e, dict)
        and e.get("count", 0) >= _REJECT_DENYLIST_COUNT)
    lines.append(f"  Rejected tropes: {n_rej} ({len(deny_names)} denylisted)")
    if deny_names:
        lines.append(f"    Denylisted: {', '.join(deny_names)}")
    # Threshold samples.
    n_samples = 0
    for e in (state.threshold_stats or {}).values():
        if isinstance(e, dict):
            n_samples += len(e.get("kept", []) or [])
            n_samples += len(e.get("dropped", []) or [])
    lines.append(f"  Threshold samples: {n_samples}")
    # Series rosters.
    n_series, n_chars = _count_series_rosters(state)
    lines.append(f"  Series rosters: {n_series} series, {n_chars} characters")
    return "\n".join(lines)


def cmd_learn_status(learn_dir=None):
    """Print learning system status and exit (for --learn-status)."""
    state = LearnedState(learn_dir=learn_dir, enabled=True)
    print(_learn_status_text(state))


def _effective_titles():
    """Hardcoded titles plus learned trusted titles."""
    return _TITLES | _learn().trusted_titles()


# Module-level validator, set from --decision-model-url in main().
# None = use regex gates (default).
_DECISION_VALIDATOR = None


PROMPT_V2_CHARACTERS = """Analyze this book chapter and return ONLY valid JSON. No commentary, no markdown, just the JSON object.
Keep any internal reasoning extremely brief — a complete, valid JSON object is the priority; do not let thinking crowd out the answer.

Chapter: {chapter_label}

Known characters so far (reuse these EXACT names when the same person appears; add new aliases instead of creating duplicates):
{roster}

{
  "characters": [{"name": "...", "aliases": ["other names used in this chapter"], "role": "protagonist|antagonist|supporting|minor", "description": "...", "appearance": "brief physical description (hair, eyes, build, distinctive features), or empty string if not described", "status": "alive|dead|unknown|missing", "evidence": "exact sentence from the chapter"}],
  "relationships": [{"from": "...", "to": "...", "type": "spouse|parent|child|sibling|friend|enemy|mentor|colleague|neighbor|other", "evidence": "exact sentence from the chapter"}],
  "pov_character": "name of the character narrating this chapter, or null"
}

RULES:
1. ENTITY MERGING: "Mother", "the narrator", "I" and the author's name are one person — list ONCE under the most specific name, put the rest in aliases.
2. NEVER INVENT NAMES. If gender is ambiguous from the name alone, leave it out rather than guessing.
3. REAL PEOPLE ONLY who appear or are directly involved in this chapter.
   - Appearance and description must describe the character themselves, not someone they observe.
     If the point-of-view character sees another person, do not attribute that person's looks
     to the POV character.
   - A character is an individual person. NOT organizations, companies, ships, places, or groups.
   - "O Palácio" (a casino), "EVA masters" (a job title), "the committee" are NOT characters — skip them.
   - If a name could be a place or thing rather than a person, skip it unless the chapter clearly treats it as a person.
4. EVIDENCE: best supporting sentence copied exactly from the chapter; empty string if none — never invent, never repeat.
5. ROLE must be exactly one of: protagonist, antagonist, supporting, minor. When in doubt, supporting.
6. pov_character is ONLY someone who narrates this chapter. Most chapters have one; some have none (null).
8. STATUS must be exactly one of: alive, dead, unknown, missing. Use "unknown" unless the chapter makes it clear. "missing" is for characters who vanished or whose fate is unresolved.
7. RELATIONSHIPS: only list relationships EXPLICITLY shown or stated in this chapter. Do not infer.
   - Direction matters: "type" describes "from" — if A is B's parent, write {"from": "A", "to": "B", "type": "parent"} (or equivalently {"from": "B", "to": "A", "type": "child"}). Pick ONE direction; never list both.
   - "spouse" means married or explicitly romantic partners. A character has at most ONE spouse. Dragons, mentors, and friends are NOT spouses.
   - "sibling" means they share parents. Cousins, friends, and squadmates are NOT siblings.
   - Every relationship needs a "to" that is a character IN THIS BOOK. Never use real-world names, author names, or names from other books.
   - When in doubt, use "other" or omit entirely. Fewer, correct relationships beat exhaustive wrong ones."""

PROMPT_V2_CHARACTERS_SIMPLE = """Extract character information from this book chapter. Return ONLY valid JSON, no other text.
{
  "characters": [{"name": "...", "aliases": [], "role": "protagonist|antagonist|supporting|minor", "description": "..."}],
  "relationships": [{"from": "...", "to": "...", "type": "spouse|parent|child|sibling|friend|enemy|mentor|colleague|neighbor|other"}],
  "pov_character": null
}
List every person named or clearly present. Merge obvious aliases. Do not invent names.
Start your response with {."""

PROMPT_V2_CONTENT = """Analyze this book chapter and return ONLY valid JSON. No commentary, no markdown, just the JSON object.
Keep any internal reasoning extremely brief — a complete, valid JSON object is the priority; do not let thinking crowd out the answer.

{
  "summary": "2-3 sentence summary of what happens in this chapter",
  "spice_level": 0,
  "triggers": {"TRIGGER_CATEGORIES_PLACEHOLDER": "none|mentioned|on_page|graphic"},
  "trigger_evidence": {"category": "exact quote from the chapter supporting on_page/graphic"},
  "trope_candidates": ["trope names you notice in this chapter"],
  "quotes": [{"text": "exact notable line from the chapter", "speaker": "character name who said it, or null if narration", "spoiler": false}]
}

RULES:
1. TRIGGERS: emit a severity for EVERY category below — none, mentioned, on_page, graphic. DEFAULT TO "none" unless the chapter clearly contains the trigger.
   - "mentioned": the trigger is a real plot element or substantive discussion (a character's backstory, a central theme, a threat that drives action). NOT a single word in passing, a joke, a metaphor, or world-building flavor.
   - "on_page": the trigger is depicted happening in the scene.
   - "graphic": depicted in disturbing detail.
   Be conservative: ordinary life stress is not a trigger.
   NON-EXAMPLES (do NOT flag these): children playing or play-fighting; a child getting a minor injury during normal activity; characters discussing or joking about a topic; a passing mention of someone's death; childbirth or pregnancy described without distress; pretend-play scenarios (e.g. "playing Indians", toy weapons); a single metaphorical use of a word (e.g. "killer deal", "torturous wait", "murder mystery" as a genre reference).
   When in doubt between two severities, choose the LOWER one. When in doubt whether it counts at all, choose "none".
2. Every category at on_page or graphic REQUIRES an evidence quote in trigger_evidence, copied EXACTLY character-for-character from the chapter.
3. SPICE LEVEL rubric: 0 = none at all. 1 = chaste romance. 2 = kissing/mild innuendo. 3 = explicit references, fade-to-black. 4 = on-page sex, moderate detail. 5 = explicit/graphic.
4. QUOTES must be exact text copied from the chapter, character-for-character. Do not paraphrase, clean up punctuation, or normalize wording. If you cannot reproduce a line exactly, omit it. Empty array if none stand out.
5. TROPE_CANDIDATES: list 0-2 narrative patterns STRONGLY present in this chapter, and only then. A trope is a reusable storytelling pattern (e.g. "enemies to lovers", "found family"), NOT a plot summary ("learning to drive", "car trouble"). "Enemies to lovers" needs an actual romantic arc. Empty array is the default; most chapters have no tropes worth naming.

Trigger categories (severity for each, exactly these names):
TRIGGER_CATEGORIES_PLACEHOLDER2"""

PROMPT_V2_CONTENT = PROMPT_V2_CONTENT.replace(
    "TRIGGER_CATEGORIES_PLACEHOLDER",
    "\n".join(f'"{c}": "..."' for c in TRIGGER_CATEGORIES))
PROMPT_V2_CONTENT = PROMPT_V2_CONTENT.replace(
    "TRIGGER_CATEGORIES_PLACEHOLDER2", ", ".join(TRIGGER_CATEGORIES))


# --- v2 character roster ---
# Carried between chapters in order. Alias-aware: a new name matching any
# known name/alias (normalized) merges instead of creating a duplicate.

# Generic references safe for aggressive roster merging. Proper-name aliases
# are recorded for display but never trigger a merge — the model lists
# distinct people as "aliases" too often.
_GENERIC_ALIASES = {"i", "me", "my", "mine", "myself",
                    "the narrator", "narrator"}
# Note: "wife", "husband", "mother", "father" etc. were removed 2026-10-08.
# Merging on relationship words across characters caused false merges
# (e.g. two characters both listing "wife" as an alias).


# Words that are relationships/descriptors, not names. Never valid as aliases.
_ALIAS_BAN_WORDS = frozenset({
    "wife", "husband", "son", "daughter", "mother", "father", "brother", "sister",
    "parent", "child", "spouse", "partner", "friend", "enemy", "lover",
    "boyfriend", "girlfriend", "fiance", "fiancee",
})

def _is_person_like(name):
    """Heuristic: is this name likely an individual person, not a group/place/thing?

    Catches what the prompt misses: plural job titles ("EVA masters"),
    generic groups ("the guards"). Conservative — only rejects clear non-persons.
    """
    if not name:
        return False
    nl = name.strip().lower()
    words = nl.split()
    # Plural group nouns: "eva masters", "the guards", "soldiers"
    _group_words = {"masters", "guards", "soldiers", "troops", "workers", "crew",
                    "staff", "team", "committee", "council", "board", "agents"}
    # Use the ORIGINAL name for case check (nl is lowercased)
    orig_words = name.strip().split()
    if orig_words:
        last = orig_words[-1]
        # Lowercase plural = common noun (group), not a surname: "EVA masters" vs "John Masters"
        if last.islower() or (last[0].islower() if last else False):
            base = last.lower().rstrip("s")
            _group_bases = {w.rstrip("s") for w in _group_words}
            if base in _group_bases:
                return False
        # Single-word "the X" already handled; also catch "The Guards" (capitalized article + plural)
        if len(orig_words) == 2 and orig_words[0].lower() in ("the", "a", "an"):
            if orig_words[-1].lower().rstrip("s") in {w.rstrip("s") for w in _group_words}:
                return False
    return True


def _is_valid_alias(alias):
    """Reject aliases that are generic descriptors, not identity claims.

    Rejects: possessives ("Sebeck's wife"), relationship words ("wife", "son"),
    generic descriptors ("the major", "narrator"). These contaminate the alias
    list and can cause false merges downstream.
    """
    if not alias or not isinstance(alias, str):
        return False
    a = alias.strip()
    if not a:
        return False
    al = a.lower()
    words = [w.strip(".,") for w in al.split()]
    # Possessives: "Sebeck's wife", "Peter's son" — describes a relationship, not a name
    if "'s " in al or al.endswith("'s"):
        return False
    # Honorific + surname without a first name: "Mrs. Sebeck", "Mr. Ross"
    # These are relationship descriptors, not identity aliases
    _honorifics = {"mr", "mrs", "ms", "miss", "dr", "prof"}
    if len(words) == 2 and words[0].rstrip(".") in _honorifics:
        return False
    # Possessive pronoun + ban-word: "his wife", "her son", "my mother"
    _poss = {"his", "her", "my", "their", "our", "your", "its"}
    if len(words) == 2 and words[0] in _poss and words[1] in _ALIAS_BAN_WORDS:
        return False
    # Relationship/descriptor words (exact match or as the only meaningful word)
    if len(words) == 1 and words[0] in _ALIAS_BAN_WORDS:
        return False
    # Generic descriptors with articles
    if al.startswith("the ") and len(words) <= 3:
        # "the major", "the narrator" — but allow "Theokoles" etc. (single word, no space)
        return False
    return True


def _roster_update(roster, characters, chapter_idx):
    """Fold one chapter's characters into the roster. Mutates roster.

    Merge rules, in order:
    1. Incoming primary name matches a known PRIMARY -> merge.
    2. Learned nickname / series hint / token lookup -> merge.
    3. Tiered deterministic merge (_merge_tier): Tier 1 hard NO (same-chapter
       co-occurrence), Tier 2 YES (titles, spelling, nicknames, unambiguous
       subsequence). Ambiguous pairs are left for post_pass_merge (Tier 3).
    4. Incoming name or alias is a GENERIC reference ("i", "the narrator",
       ...) matching an entry's generic alias -> merge.
    Proper-name aliases never trigger a merge (they're recorded, not trusted).
    The book's author is never a character (author filter).
    """
    primaries = {}
    for k, e in roster.items():
        for pk in e.get("primary_keys", {k}):
            primaries[pk] = k
    # Tier 1 bookkeeping: nkeys listed as separate characters in THIS chapter.
    _chapter_keys = {norm_name(c.get("name", "")) for c in characters}
    _chapter_keys.discard("")
    _book_author = getattr(_DIAG, "book_author", None)

    for c in characters:
        name = c.get("name", "")
        nkey = norm_name(name)
        if not nkey:
            continue
        if _book_author and _is_author_name(name, _book_author):
            # The author is not a character ("About the Author" pages etc.).
            UI.viz_event(getattr(_DIAG, "viz_book_key", None),
                         '✗ Filtered: "%s" (book author)' % name[:40])
            continue
        if not _is_person_like(name):
            # Skip groups/organizations/places misclassified as characters
            UI.viz_event(getattr(_DIAG, "viz_book_key", None),
                         '✗ Filtered: "%s" (not a person)' % name[:40])
            continue
        # Learn title prefixes: observe leading 1-2 tokens of multi-word
        # names. A candidate becomes trusted after 2+ distinct remainders.
        _ln_obs = _learn()
        if _ln_obs.enabled:
            _ntoks = name.split()
            if len(_ntoks) >= 3:
                _ln_obs.observe_title(_ntoks[0], " ".join(_ntoks[1:]))
                _ln_obs.observe_title(" ".join(_ntoks[:2]),
                                      " ".join(_ntoks[2:]))
        alias_keys = {norm_name(a) for a in c.get("aliases", [])
                      if _is_valid_alias(a)}
        alias_keys.discard("")
        # Generic-merge uses raw aliases: "the narrator" is rejected as a display
        # alias but must still trigger first-person merging.
        _generic_keys = {norm_name(a) for a in c.get("aliases", [])} & _GENERIC_ALIASES
        _generic_keys.add(nkey)  # primary name may itself be generic ("I", "Narrator")
        _generic_keys &= _GENERIC_ALIASES

        if nkey in primaries:
            found = primaries[nkey]
        else:
            # Learned nickname lookup (cross-book learning): an exact
            # previously-observed variant maps straight to its canonical key.
            found = None
            _ln = _learn()
            if _ln.enabled:
                _lk = _ln.nick_lookup(nkey)
                if _lk and _lk in primaries:
                    found = primaries[_lk]
                if found is None:
                    # Series hints from prior books by the same author.
                    _sh = getattr(_DIAG, "learn_series_hints", None)
                    if _sh:
                        _sk = _sh.get(nkey)
                        if _sk and _sk in primaries:
                            found = primaries[_sk]
                if found is None and " " not in nkey.strip():
                    # Single-token nickname ("pete"): resolve via the learned
                    # token map, but only when the roster has exactly one
                    # candidate with that first token (unambiguous).
                    _tok_full = _ln.token_lookup(nkey)
                    if _tok_full:
                        _cands = [rk for pk, rk in primaries.items()
                                  if pk.split(" ")[0] == _tok_full]
                        if len(set(_cands)) == 1:
                            found = _cands[0]
            if found is None:
                # Tiered deterministic merge (replaces the old _names_overlap
                # substring heuristic): Tier 1 hard NO for same-chapter
                # co-occurrence, Tier 2 YES for titles/spelling/nicknames/
                # unambiguous subsequence. "ambiguous" pairs are left as
                # separate entries for post_pass_merge (Tier 3).
                _pkeys = set(primaries.keys())
                for pk, rk in primaries.items():
                    _tier = _merge_tier(nkey, pk, chapter_keys=_chapter_keys,
                                        roster_keys=_pkeys)
                    if _tier == "yes":
                        found = rk
                        break
                else:
                    found = None
            if _generic_keys:
                for k, e in roster.items():
                    _e_generic = ({k} | e["alias_keys"]) & _GENERIC_ALIASES
                    # Also check stored generic keys from when entry was created
                    _e_generic |= e.get("_generic_keys", set())
                    if _generic_keys & _e_generic:
                        found = k
                        break

        if found is None:
            roster[nkey] = {"name": name, "aliases": set(), "alias_keys": {nkey},
                            "primary_keys": {nkey}, "_generic_keys": _generic_keys,
                            "appearances": 0, "last_seen": chapter_idx,
                            "chapters": set(),
                            "roles": Counter(), "descriptions": []}
            found = nkey
            primaries[nkey] = nkey
            UI.viz_event(getattr(_DIAG, "viz_book_key", None),
                         "+ New: %s" % name[:40])
            _is_new_variant = False
        else:
            # Log only genuinely new name variants (not routine re-appearances).
            _is_new_variant = nkey not in roster[found].get("primary_keys", set())
        e = roster[found]
        if _is_new_variant:
            UI.viz_event(getattr(_DIAG, "viz_book_key", None),
                         "→ Merged: %s → %s" % (name[:30], e["name"][:30]))
            # Learn the nickname mapping for future books: incoming variant
            # -> canonical roster key. Also learn single-token nicknames
            # ("pete" -> "peter") when the variant is a lone token.
            _ln_rec = _learn()
            if _ln_rec.enabled:
                _isbn = getattr(_DIAG, "learn_isbn", None)
                _canon_key = found  # roster key of the merged-into entry
                _ln_rec.record_merge(nkey, _canon_key, _isbn)
                _nw = nkey.split()
                _cw = _canon_key.split()
                if len(_nw) == 1 and _cw and _nw[0] != _cw[0]:
                    _ln_rec.record_token_merge(_nw[0], _cw[0], _isbn)
        e["appearances"] += 1
        e["last_seen"] = chapter_idx
        e["chapters"].add(chapter_idx)
        e["alias_keys"].add(nkey)
        # Alias pollution guard: never adopt an alias that is another roster
        # character's own name — it would make _roster_canonical resolve
        # that character's relationship endpoints to THIS entry.
        e["alias_keys"] |= {ak for ak in alias_keys
                            if not _alias_shadowed(roster, found, ak)}
        e["primary_keys"].add(nkey)
        for alias in {name} | set(c.get("aliases", [])):
            if not alias or alias == e["name"]:
                continue
            if _is_valid_alias(alias) and not _alias_shadowed(
                    roster, found, norm_name(alias)):
                e["aliases"].add(alias)
            else:
                # Preserve rejected aliases as unresolved mentions (GPT audit:
                # they may be useful clues, don't throw them away)
                e.setdefault("unresolved_mentions", set()).add(alias)
        if c.get("role"):
            e["roles"][c["role"]] += 1
        if c.get("description"):
            # Track (chapter, text, has_evidence) for description selection in v2_reduce.
            # "First speaks or acts" ~= earliest chapter with evidence.
            e["descriptions"].append(
                (chapter_idx, c["description"], bool(c.get("evidence"))))
        if c.get("appearance"):
            e.setdefault("appearances_desc", []).append(c["appearance"])
        if c.get("status"):
            e["statuses"] = e.get("statuses", Counter())
            e["statuses"][c["status"]] += 1
            # Track death reports with chapter for sticky-death logic (A3).
            # Only counts if the chapter provided evidence (not just a mention).
            if c["status"] == "dead" and c.get("evidence"):
                e.setdefault("death_reports", []).append(chapter_idx)
            # Track "alive" reports with evidence: a dead character merely mentioned
            # doesn't count as acting. Only status="alive" + evidence clears death.
            if c["status"] == "alive" and c.get("evidence"):
                e.setdefault("alive_reports", set()).add(chapter_idx)
        if c.get("evidence") and not e.get("evidence"):
            e["evidence"] = c["evidence"]  # first verified evidence wins


def _roster_prompt(roster, budget=1500):
    """Render the roster for a Call A prompt. Capped by char budget (~tokens);
    priority is recency then frequency. Returns (text, dropped_names)."""
    ranked = sorted(roster.values(),
                    key=lambda e: (e["last_seen"], e["appearances"]),
                    reverse=True)
    lines, dropped, total = [], [], 0
    for e in ranked:
        alias_str = f" (also: {', '.join(sorted(e['aliases']))})" if e["aliases"] else ""
        line = f"- {e['name']}{alias_str}"
        if total + len(line) > budget:
            dropped.append(e["name"])
            continue
        lines.append(line)
        total += len(line)
    text = "\n".join(lines) if lines else "(no characters known yet)"
    return text, dropped


def _sanitize_v2a(r):
    """Sanitize Call A output (characters/relationships/POV). None if unusable."""
    if not isinstance(r, dict):
        return None
    characters = []
    for c in _list(r.get("characters")):
        if not isinstance(c, dict):
            continue
        name = _s(c.get("name"), 120)
        if not name:
            continue
        role = _s(c.get("role"), 30).lower()
        _status = _s(c.get("status"), 20).lower()
        _status = {"deceased": "dead", "killed": "dead", "died": "dead",
                   "vanished": "missing", "disappeared": "missing",
                   "gone": "missing"}.get(_status, _status)
        if _status not in ("alive", "dead", "unknown", "missing"):
            _status = "unknown"
        characters.append({
            "name": name,
            "aliases": [a for a in (_s(x, 120) for x in _list(c.get("aliases"))) if a],
            "role": role if role in VALID_ROLES else None,
            "description": _s(c.get("description"), 1000),
            "appearance": _s(c.get("appearance"), 500),
            "status": _status,
            "evidence": _s(c.get("evidence"), 500),
        })
    relationships = []
    for rel in _list(r.get("relationships")):
        if not isinstance(rel, dict):
            continue
        a, b = _s(rel.get("from"), 120), _s(rel.get("to"), 120)
        ty = _s(rel.get("type"), 60).lower()
        if a and b and ty:
            relationships.append({"from": a, "to": b, "type": ty,
                                  "evidence": _s(rel.get("evidence"), 500)})
    pov = _s(r.get("pov_character"), 120)
    return {"characters": characters, "relationships": relationships,
            "pov_character": pov or None}


def _sanitize_v2b(r):
    """Sanitize Call B output (summary/triggers/spice/tropes/quotes)."""
    if not isinstance(r, dict):
        return None
    raw_trig = r.get("triggers") if isinstance(r.get("triggers"), dict) else {}
    triggers = {}
    for cat in TRIGGER_CATEGORIES:
        sev = _s(raw_trig.get(cat), 20).lower()
        triggers[cat] = sev if sev in _SEVERITY_ORDER else "none"
    trigger_evidence = {}
    raw_tev = r.get("trigger_evidence")
    if isinstance(raw_tev, dict):
        for cat in TRIGGER_CATEGORIES:
            q = _s(raw_tev.get(cat), 500)
            if q:
                trigger_evidence[cat] = q
    try:
        spice = max(0, min(5, int(float(r.get("spice_level")))))
    except (TypeError, ValueError):
        # Absent or unparseable: the level is UNKNOWN (None), not 0.
        # 0 is a measurement — a genuinely clean chapter — and folding
        # unknowns in as 0 drags the book level to a false low that
        # then overwrites works.spice_detected. Consumers that need a
        # number (the per-chapter array) map None to 0 themselves;
        # the book-level computation excludes unknowns, and a book
        # with no known chapter keeps an unknown level.
        spice = None
    tropes = [_s(t, 80) for t in _list(r.get("trope_candidates")) if _s(t, 80)]
    quotes = []
    for q in _list(r.get("quotes")):
        if isinstance(q, dict):
            text = _s(q.get("text"), 600)
            spoiler = q.get("spoiler") in (True, "true", "True")
            speaker = _s(q.get("speaker"), 100) or None
        else:
            text, spoiler, speaker = _s(q, 600), False, None
        if text:
            quotes.append({"text": text, "speaker": speaker, "spoiler": spoiler})
    return {"summary": _s(r.get("summary"), 1000),
            "spice_level": spice,
            "triggers": triggers,
            "trigger_evidence": trigger_evidence,
            "trope_candidates": tropes,
            "quotes": quotes}


def _verify_trigger_evidence(trigger_evidence, text):
    """Verify Call B trigger quotes against the chapter text. Drops failures."""
    verified = {}
    for cat, q in trigger_evidence.items():
        if verify_evidence(q, text):
            verified[cat] = q
    return verified


def _v2_think_suffix(which):
    """Resolve the think suffix for Call A/B: per-call override, else global."""
    key = "v2_think_suffix_a" if which == "a" else "v2_think_suffix_b"
    val = CONFIG.get(key)
    return val if val is not None else CONFIG.get("llm_think_suffix", "")


def _v2_enable_thinking(which):
    """Resolve enable_thinking for Call A/B: per-call override, else global."""
    key = "v2_enable_thinking_a" if which == "a" else "v2_enable_thinking_b"
    val = CONFIG.get(key)
    return val if val is not None else CONFIG.get("llm_enable_thinking")


def v2_call_a(call, roster, chapter, n_chapters):
    """Call A: characters/relationships/POV for one chapter.

    Must run in chapter order (roster dependency). Returns (result, fell_back).
    """
    idx = chapter["index"]
    _DIAG.think_suffix = _v2_think_suffix("a")
    _DIAG.enable_thinking = _v2_enable_thinking("a")
    try:
        return _v2_call_a_inner(call, roster, chapter, n_chapters)
    finally:
        _DIAG.think_suffix = None
        _DIAG.enable_thinking = None


V2_SHARED_SYSTEM = "You are a book analysis assistant. Return only valid JSON. No commentary, no markdown."



def _v2_call_a_inner(call, roster, chapter, n_chapters):
    idx = chapter["index"]
    roster_text, dropped = _roster_prompt(roster)
    if dropped and CONFIG.get("debug"):
        print(f"(roster capped, dropped: {', '.join(dropped[:5])})",
              end=" ", flush=True)
    # POV pre-check: if the chapter heading names a roster character, use it
    # directly instead of asking the LLM (dual-POV books often label chapters).
    pov_hint = None
    label_low = chapter["label"].lower()
    for e in roster.values():
        if e["name"].lower() in label_low and len(e["name"]) > 2:
            pov_hint = e["name"]
            break
    prompt = (PROMPT_V2_CHARACTERS
              .replace("{chapter_label}", chapter["label"])
              .replace("{roster}", roster_text))
    _use_shared = CONFIG.get("v2_shared_system")  # isolation flag only; hurts quality
    _task_last = (CONFIG.get("v2_prompt_cache") or CONFIG.get("v2_task_last"))
    _system_a = V2_SHARED_SYSTEM if _use_shared else prompt
    if _task_last:
        _user_a = f"CHAPTER TEXT:\n{chapter['text']}\n\nTASK:\n{prompt}"
    else:
        _user_a = chapter["text"]
    a = _run_task(call, _system_a, _user_a, idx, n_chapters,
                  f"v2-ch{idx}-identity", sanitize_fn=_sanitize_v2a)
    # Apply POV hint if the LLM didn't determine one (or as override).
    if a and pov_hint and not a.get("pov_character"):
        a["pov_character"] = pov_hint
    fell_back = False
    if a is None:
        a = _run_task(call, PROMPT_V2_CHARACTERS_SIMPLE, chapter["text"], idx,
                      n_chapters, f"v2-ch{idx}-identity-fallback",
                      sanitize_fn=_sanitize_v2a)
        fell_back = a is not None
    if a is not None:
        # Verify evidence against this chapter's text before roster merge.
        vc, vr, _, vstats = verify_all_evidence(
            a["characters"], a["relationships"], [], chapter["text"])
        a["characters"], a["relationships"] = vc, vr
        a["verification"] = vstats
        _roster_update(roster, vc, idx)
    return a, fell_back


def v2_call_b(call, chapter, n_chapters):
    """Call B: summary/triggers/spice/tropes/quotes. Independent per chapter."""
    _DIAG.think_suffix = _v2_think_suffix("b")
    _DIAG.enable_thinking = _v2_enable_thinking("b")
    try:
        return _v2_call_b_inner(call, chapter, n_chapters)
    finally:
        _DIAG.think_suffix = None
        _DIAG.enable_thinking = None


def _v2_call_b_inner(call, chapter, n_chapters):
    idx = chapter["index"]
    _use_shared_b = CONFIG.get("v2_shared_system")  # isolation flag only; hurts quality
    _task_last_b = (CONFIG.get("v2_prompt_cache") or CONFIG.get("v2_task_last"))
    _system_b = V2_SHARED_SYSTEM if _use_shared_b else PROMPT_V2_CONTENT
    if _task_last_b:
        _user_b = f"CHAPTER TEXT:\n{chapter['text']}\n\nTASK:\n{PROMPT_V2_CONTENT}"
    else:
        _user_b = chapter["text"]
    b = _run_task(call, _system_b, _user_b, idx, n_chapters,
                  f"v2-ch{idx}-content", sanitize_fn=_sanitize_v2b)
    if b is not None:
        b["trigger_evidence"] = _verify_trigger_evidence(
            b["trigger_evidence"], chapter["text"])
        _, _, vq, vstats = verify_all_evidence([], [], b["quotes"], chapter["text"])
        b["quotes"] = vq
        b["verification"] = vstats
    return b


def _roster_canonical(roster, name):
    """Map any name/alias to the roster's canonical name (or the name itself).

    Two passes, in strict order:
    1. Primary: an exact roster key or primary-key match. A real character's
       own name ALWAYS wins — an entry's alias list can be polluted with
       other people's names (LLM alias errors folded in by _roster_update /
       _merge_roster_entries), and resolving "Lilith Sorrengail" via
       Violet's polluted alias_keys collapsed Violet<->Lilith pairs into
       Violet->Violet self-pairs.
    2. Alias: alias_keys, but only when exactly ONE entry claims the key.
       An alias claimed by two entries is ambiguous; leaving the name
       unresolved is safer than guessing (false merges are worse than
       unresolved duplicates)."""
    nkey = norm_name(name)
    for k, e in roster.items():
        if nkey == k or nkey in e.get("primary_keys", set()):
            return e["name"]
    hits = [e["name"] for e in roster.values()
            if nkey in e.get("alias_keys", set())]
    if len(hits) == 1:
        return hits[0]
    return name


def _merge_roster_entries(roster, keep_key, drop_key):
    """Fold drop_key's roster entry into keep_key's. Mutates roster.

    Combines aliases, appearances, chapters, descriptions, and evidence.
    The display name prefers the cleaner (fewer noise tokens) variant.
    """
    if keep_key == drop_key or drop_key not in roster or keep_key not in roster:
        return
    ke, de = roster[keep_key], roster[drop_key]
    ke["alias_keys"] |= de.get("alias_keys", set())
    ke["primary_keys"] |= de.get("primary_keys", set())
    ke["aliases"] |= de.get("aliases", set())
    ke["appearances"] = ke.get("appearances", 0) + de.get("appearances", 0)
    ke["chapters"] |= de.get("chapters", set())
    ke["roles"] += de.get("roles", Counter())
    for d in de.get("descriptions", []):
        if d not in ke["descriptions"]:
            ke["descriptions"].append(d)
    if len(de.get("evidence", "")) > len(ke.get("evidence", "")):
        ke["evidence"] = de["evidence"]
    for attr in ("statuses",):
        if de.get(attr):
            ke.setdefault(attr, Counter()).update(de[attr])
    # death_reports is a LIST (chapter indices appended by _roster_update;
    # the sticky-death consumer takes min() over it) — merge it with
    # order-preserving dedupe. alive_reports / unresolved_mentions are
    # sets and keep set semantics. (Lumping all three into one set loop
    # crashed with AttributeError whenever the keeper already carried
    # death reports and a merge folded another entry into it.)
    if de.get("death_reports"):
        _kd = ke.setdefault("death_reports", [])
        for _idx in de["death_reports"]:
            if _idx not in _kd:
                _kd.append(_idx)
    for attr in ("alive_reports", "unresolved_mentions"):
        if de.get(attr):
            ke.setdefault(attr, set()).update(de[attr])
    for attr in ("appearances_desc",):
        if de.get(attr):
            ke.setdefault(attr, []).extend(
                x for x in de[attr] if x not in ke.get(attr, []))
    # Display name: prefer the cleaner variant (fewer noise tokens), then
    # the one with more appearances.
    _keep_toks = _strip_merge_noise(keep_key)
    _drop_toks = _strip_merge_noise(drop_key)
    if (len(_drop_toks) < len(_keep_toks) and _drop_toks
            and de.get("name")):
        ke["name"] = de["name"]
    # Merge generic-key bookkeeping so first-person resolution still works.
    ke["_generic_keys"] = ke.get("_generic_keys", set()) | de.get(
        "_generic_keys", set())
    del roster[drop_key]
    # Alias pollution guard: the union above can import alias keys that are
    # a THIRD character's own name (the dropped entry's LLM alias errors).
    # Drop those from the keeper; the real character's entry wins.
    ke["alias_keys"] = {ak for ak in ke["alias_keys"]
                        if not _alias_shadowed(roster, keep_key, ak)}


def _roster_claim_keys(entry):
    """Name keys an entry CLAIMS denote itself: adopted alias keys plus
    the preserved unresolved mentions. Mentions were rejected at fold
    time because their target was already an entry (the GPT-audit
    "useful clues" set) — with whole-roster hindsight they carry the
    other half of the mutual-claim signal."""
    keys = set(entry.get("alias_keys", set()))
    for m in entry.get("unresolved_mentions", set()):
        mk = norm_name(m)
        if mk:
            keys.add(mk)
    return keys


def _roster_reconcile(roster):
    """Deterministic whole-roster reconciliation (Experiment 1,
    --roster-reconcile). Runs ONCE after Call A completes, before
    post_pass_merge / v2_reduce consume the roster. Mutates roster.

    Call A builds the roster chapter by chapter, so alias claims are
    judged against a partial roster: a claim adopted in ch. 2 may be
    pollution by ch. 40 (its target became a real entry), and a full
    name revealed in ch. 40 never reunites with its early short form
    (the surname trap hard-NOs that pair in every later pass). With
    the whole book sighted, three deterministic repairs apply — all
    from existing machinery, no LLM/Jev calls:

    1. Mutual-claim merge: two entries that each claim the other's
       primary (via alias_keys or unresolved_mentions), never
       co-listed in a chapter (Tier 1 cannot-link: their chapter
       sets are disjoint), and claimed by no third entry, are one
       person sighted under two name forms. A LONE proper-name
       claim never merges (the _roster_update doctrine: aliases
       are recorded, not trusted) — one-directional claims fall
       through to pruning instead.
    2. Learned/manual link merge: nick_lookup maps one entry's
       primary to another entry's primary. Update-time lookups
       miss when both entries already exist; the same vetoes apply.
    3. Stale-shadow pruning: alias keys / display aliases that are
       another entry's primary in the FINAL roster are pollution
       (_alias_shadowed with hindsight) and are pruned, so a stale
       claim can never become the unique claimant that resolves a
       name to the wrong entry.

    Membership invariant: merges only fold sighted entries into each
    other (_merge_roster_entries unions existing forms and keeps a
    sighted display name). Nothing is created.

    Returns {"entries_before", "entries_after", "variants_merged",
    "aliases_pruned"}; aliases_pruned counts alias keys + display
    aliases removed by pass 3 (merges prune their own keepers).
    """
    stats = {"entries_before": len(roster),
             "entries_after": len(roster),
             "variants_merged": 0, "aliases_pruned": 0}
    _ln = _learn()
    _isbn = getattr(_DIAG, "learn_isbn", None)

    def _record(keep_key, drop_key):
        if _ln.enabled:
            _ln.record_merge(drop_key, keep_key, _isbn)
            _dw = drop_key.split()
            _cw = keep_key.split()
            if len(_dw) == 1 and _cw and _dw[0] != _cw[0]:
                _ln.record_token_merge(_dw[0], _cw[0], _isbn)

    # Passes 1+2, converging: a merge can complete a mutual claim
    # for a third entry, so loop until stable. Pair enumeration is
    # sorted for determinism; each merge shrinks the roster.
    _changed = True
    while _changed:
        _changed = False
        prim = {}
        for k, e in roster.items():
            for pk in e.get("primary_keys", {k}):
                prim[pk] = k
        claims = {k: _roster_claim_keys(e) for k, e in roster.items()}
        # target primary key -> claimant entry keys (an entry's
        # claims on its OWN forms are not claims on another entry).
        claimants = {}
        for k, cks in claims.items():
            own = roster[k].get("primary_keys", {k})
            for c in cks:
                if c in own:
                    continue
                tgt = prim.get(c)
                if tgt is not None and tgt != k:
                    claimants.setdefault(c, set()).add(k)

        def _unique(pks, ka, kb):
            # Canonical pass 2 lifted to entries: a primary claimed
            # by a third entry is ambiguous and links nothing.
            return all(claimants.get(pk, set()) <= {ka, kb}
                       for pk in pks)

        for ka in sorted(roster):
            if ka not in roster:
                continue
            for kb in sorted(roster):
                if kb <= ka or kb not in roster:
                    continue
                ea, eb = roster[ka], roster[kb]
                # Tier 1 cannot-link (the signal post_pass_merge
                # uses): co-listed in a chapter -> two people.
                if ea.get("chapters", set()) & eb.get("chapters", set()):
                    continue
                pa = ea.get("primary_keys", {ka})
                pb = eb.get("primary_keys", {kb})
                mutual = (any(c in pb for c in claims[ka])
                          and any(c in pa for c in claims[kb]))
                learned = False
                if _ln.enabled:
                    for pk in pa:
                        if prim.get(_ln.nick_lookup(pk) or "") == kb:
                            learned = True
                    for pk in pb:
                        if prim.get(_ln.nick_lookup(pk) or "") == ka:
                            learned = True
                if not (learned or (mutual and _unique(pa, ka, kb)
                                    and _unique(pb, ka, kb))):
                    continue
                keep, drop = (ka, kb) if ea.get("appearances", 0) >= \
                    eb.get("appearances", 0) else (kb, ka)
                _merge_roster_entries(roster, keep, drop)
                _record(keep, drop)
                stats["variants_merged"] += 1
                _changed = True
                break
            if _changed:
                break

    # Pass 3: prune stale shadowed aliases with final-roster
    # hindsight (the keeper pruning _merge_roster_entries does,
    # applied to every entry).
    for k, e in roster.items():
        kept_keys = {ak for ak in e.get("alias_keys", set())
                     if not _alias_shadowed(roster, k, ak)}
        stats["aliases_pruned"] += \
            len(e.get("alias_keys", set())) - len(kept_keys)
        e["alias_keys"] = kept_keys
        kept_aliases = {a for a in e.get("aliases", set())
                        if not _alias_shadowed(roster, k, norm_name(a))}
        stats["aliases_pruned"] += \
            len(e.get("aliases", set())) - len(kept_aliases)
        e["aliases"] = kept_aliases

    stats["entries_after"] = len(roster)
    return stats


def _roster_best_desc(entry):
    """Best description text for a roster entry (same policy as v2_reduce:
    earliest evidence-backed, then longest)."""
    _descs = entry.get("descriptions", [])
    _descs = [d if isinstance(d, tuple) else (0, d, False) for d in _descs]
    if not _descs:
        return ""
    _with_ev = [d for d in _descs if len(d) > 2 and d[2]]
    if _with_ev:
        return sorted(_with_ev, key=lambda x: (x[0], -len(x[1])))[0][1]
    return max(_descs, key=lambda x: len(x[1]))[1]


def _merge_pair_signal(ka, kb):
    """Similarity signal strength for a Tier 3 candidate pair (0 = none).
    Counts shared non-title tokens; substring matches count too."""
    sa = set(_strip_merge_noise(ka)) - _NAME_STOPWORDS
    sb = set(_strip_merge_noise(kb)) - _NAME_STOPWORDS
    if not sa or not sb:
        return 0
    _shared = len(sa & sb)
    if _shared:
        return _shared
    # Substring signal on the joined stripped names.
    ja, jb = " ".join(sorted(sa)), " ".join(sorted(sb))
    if ja and jb and (ja in jb or jb in ja):
        return 1
    return 0


def post_pass_merge(roster, validator=None):
    """Post-chapter merge pass over the roster. Tier 1/2 deterministic first
    (converging loop), then Tier 3 via the decision model for ambiguous pairs
    with a similarity signal.

    Returns (n_merged, possible_merges) where possible_merges is a list of
    {"name_a", "name_b", "p_yes", "reason"} for manual review. Pairs are left
    separate (never force-merged) when the model is unavailable or unsure.
    """
    possible_merges = []
    n_merged = 0
    _ln = _learn()
    _isbn = getattr(_DIAG, "learn_isbn", None)

    def _record(keep_key, drop_key):
        if _ln.enabled:
            _ln.record_merge(drop_key, keep_key, _isbn)
            _dw = drop_key.split()
            _cw = keep_key.split()
            if len(_dw) == 1 and _cw and _dw[0] != _cw[0]:
                _ln.record_token_merge(_dw[0], _cw[0], _isbn)

    # Tier 1/2: converge deterministic merges.
    _changed = True
    while _changed:
        _changed = False
        _keys = list(roster.keys())
        _rkeys = set(_keys)
        for i, ka in enumerate(_keys):
            if ka not in roster:
                continue
            for kb in _keys[i + 1:]:
                if kb not in roster:
                    continue
                ea, eb = roster[ka], roster[kb]
                _tier = _merge_tier(
                    ka, kb, a_chapters=ea.get("chapters"),
                    b_chapters=eb.get("chapters"), roster_keys=_rkeys)
                if _tier == "yes":
                    # Keep the entry with more appearances (more evidence).
                    keep, drop = (ka, kb) if ea.get("appearances", 0) >= \
                        eb.get("appearances", 0) else (kb, ka)
                    _merge_roster_entries(roster, keep, drop)
                    _record(keep, drop)
                    n_merged += 1
                    _changed = True
                    break
                elif _tier == "no" and CONFIG.get("explain_blocks"):
                    # Explain Tier 1 hard-NO blocks (surname trap).
                    _st_blocked, _st_reason = _surname_trap_blocked(ka, kb)
                    if _st_blocked:
                        print(f"  Merge blocked: '{ka}' vs '{kb}' \u2014 "
                              f"{_st_reason}")
            if _changed:
                break

    # Tier 3: ambiguous pairs with a similarity signal.
    _keys = list(roster.keys())
    _cands = []  # (signal, ka, kb)
    for i, ka in enumerate(_keys):
        for kb in _keys[i + 1:]:
            ea, eb = roster[ka], roster[kb]
            _tier = _merge_tier(
                ka, kb, a_chapters=ea.get("chapters"),
                b_chapters=eb.get("chapters"),
                roster_keys=set(_keys))
            if _tier != "ambiguous":
                continue
            _sig = _merge_pair_signal(ka, kb)
            if _sig:
                _cands.append((_sig, ka, kb))
    # Strongest signals first. The shared per-book budget (relationship
    # validation later draws from the same pool) is claimed per ACTUAL
    # call, immediately before it — never up front for the whole
    # candidate list. Pairs the validator never sees (decision model
    # off, or an endpoint merged away by an earlier Tier 3 decision)
    # must not consume budget they never spend; the old up-front
    # partition drained the pool on validator-off runs and starved
    # relationship validation on merged-away pairs.
    _cands.sort(key=lambda x: -x[0])
    _dm_off = validator is None or getattr(validator, "disabled", False)
    for _sig, ka, kb in _cands:
        if ka not in roster or kb not in roster:
            continue  # merged by an earlier Tier 3 decision
        ea, eb = roster[ka], roster[kb]
        _na, _nb = ea.get("name", ka), eb.get("name", kb)
        if _dm_off:
            possible_merges.append({
                "name_a": _na, "name_b": _nb, "p_yes": None,
                "reason": "unresolved (decision model disabled)"})
            continue
        if not _dm_budget_claim(1):
            # Pairs beyond the cap are listed for manual review,
            # not silently dropped.
            possible_merges.append({
                "name_a": _na, "name_b": _nb, "p_yes": None,
                "reason": "unresolved (Tier 3 cap reached)"})
            continue
        _merged, _p = validator.ask_same_person(
            _na, _roster_best_desc(ea), ea.get("evidence", ""),
            _nb, _roster_best_desc(eb), eb.get("evidence", ""))
        if _merged is None:
            possible_merges.append({
                "name_a": _na, "name_b": _nb, "p_yes": None,
                "reason": "decision model error"})
            if getattr(validator, "disabled", False):
                _dm_off = True  # circuit breaker tripped; rest are unresolved
            continue
        if _merged:
            keep, drop = (ka, kb) if ea.get("appearances", 0) >= \
                eb.get("appearances", 0) else (kb, ka)
            _merge_roster_entries(roster, keep, drop)
            _record(keep, drop)
            n_merged += 1
        else:
            possible_merges.append({
                "name_a": _na, "name_b": _nb,
                "p_yes": round(_p, 4) if _p is not None else None,
                "reason": f"below threshold ({_MERGE_P_YES_THRESHOLD})"})
    return n_merged, possible_merges


def _v2_char_confidence(appearances, has_verified_evidence):
    """Evidence-based character confidence (replaces legacy hit-count formula).

    0.5 base + 0.1 per chapter appearance (capped at +0.3) + 0.15 for
    verified evidence. Capped at 0.95.
    """
    return round(min(0.95, 0.5 + min(0.3, 0.1 * appearances) +
                     (0.15 if has_verified_evidence else 0)), 2)


def _v2_trigger_confidence(severity_level, n_chapters, n_evidence):
    """Evidence-based trigger confidence.

    0.4 base + 0.15 per severity level + 0.05 per chapter (capped at +0.15)
    + 0.1 per verified quote (capped at +0.2). Capped at 0.95.
    """
    return round(min(0.95, 0.4 + 0.15 * severity_level +
                     min(0.15, 0.05 * n_chapters) +
                     min(0.2, 0.1 * n_evidence)), 2)


def _ground_entities(characters, relationships, chapters):
    """Verify extracted names against the raw chapter text. Pure function.

    Characters: grounded if name or any alias appears as a whole word
    (case-insensitive, word boundaries) anywhere in the book text.
    Catches hallucinated names. Word boundaries (not substring) because
    false-grounded is the dangerous direction — a short hallucinated name
    like "Bo" must not match inside "book".
    Relationships: grounded if both parties co-occur as whole words in the
    claimed chapter (entity co-occurrence, not relationship validation).
    Mutates the dicts in place, adding grounded/variants_matched. Returns
    a summary dict.
    """
    import re as _re
    full_text = "\n".join(c.get("text", "") for c in chapters)
    grounded = 0
    ungrounded_names = []
    for ch in characters:
        variants = {(ch.get("name") or "")}
        variants |= {(a or "") for a in ch.get("aliases", [])}
        variants.discard("")
        matched = 0
        for v in variants:
            if not v:
                continue
            pat = _re.compile(r"(?<!\w)" + _re.escape(v) + r"(?!\w)", _re.IGNORECASE)
            if pat.search(full_text):
                matched += 1
        ch["grounded"] = matched > 0
        ch["variants_matched"] = matched
        if matched > 0:
            grounded += 1
        else:
            _nm = ch.get("name") or "?"
            ungrounded_names.append(_nm)
    # Chapter text lookup by label for relationship co-occurrence
    label_to_text = {c.get("label"): c.get("text", "") for c in chapters}
    rel_grounded = 0
    for rel in relationships:
        ch_text = label_to_text.get(rel.get("chapter", ""), "")
        a = (rel.get("from") or "")
        b = (rel.get("to") or "")
        ok = False
        if a and b:
            pa = _re.compile(r"(?<!\w)" + _re.escape(a) + r"(?!\w)", _re.IGNORECASE)
            pb = _re.compile(r"(?<!\w)" + _re.escape(b) + r"(?!\w)", _re.IGNORECASE)
            ok = bool(pa.search(ch_text) and pb.search(ch_text))
        rel["grounded"] = ok
        if ok:
            rel_grounded += 1
    return {"characters_grounded": grounded,
            "characters_total": len(characters),
            "ungrounded_names": ungrounded_names,
            "relationships_grounded": rel_grounded,
            "relationships_total": len(relationships)}


# Safety: minor indicators and sexualized terms for appearance filtering.
# Conservative: when in doubt, blank the appearance field.
_MINOR_RE = re.compile(
    r"\b(child|children|teen(?:ager)?|boy|girl|kid|youth|juvenile|adolescent|"
    r"toddler|baby|infant|minor|youngster|preteen)\b|"
    r"\b(?:[1-9]|1[0-7])[- ]year[- ]old\b|"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen)[- ]year[- ]old\b",
    re.IGNORECASE)
_SEXUALIZED_TERMS = frozenset({
    "sexy", "voluptuous", "curvy", "busty", "seductive", "sensual", "erotic",
    "arousing", "lustful", "provocative", "sultry", "titillating", "shapely",
    "cleavage", "thong", "lingerie",
})
_SEXUALIZED_RE = re.compile(
    r"\b(" + "|".join(sorted(_SEXUALIZED_TERMS)) + r")\b", re.IGNORECASE)

def _is_minor(role, description):
    """True if role/description indicates a child/teen. Word-boundary match."""
    text = f"{role or ''} {description or ''}"
    return bool(_MINOR_RE.search(text))


def v2_reduce(chapter_as, chapter_bs, roster, chapters, preview=False,
              show_matrices=False, n_chapters_total=None):
    """Deterministic reduce over per-chapter v2 results. Pure code, no LLM.

    chapter_as/bs: {chapter_index: result or None}. Returns a result dict
    shaped for write_claims()/save_preview()/resolve_work()/dedupe_characters().

    preview: when True, decision-model validation scores are recorded but
    low-confidence family relationships are NOT downgraded and
    very-low-confidence ones are NOT dropped (audit only).

    show_matrices: when True, print ASCII matrix visualizations (spice,
    relationship confidence) to the terminal during processing.

    n_chapters_total: the book's true chapter count when `chapters`
    is a --chunks subset. chapter_bs keys are ORIGINAL chapter
    indices, so the per-chapter spice array must be bounded by the
    total, not by len(chapters) — bounding by the subset size
    silently zeroed every subset run's chapter_spice. Defaults to
    len(chapters) (full runs).
    """
    n = len(chapters)
    idx_to_label = {c["index"]: c["label"] for c in chapters}
    # Table-of-contents chapters are title lists, not story content. Their
    # candidates would be inferred from other chapters' titles, so they are
    # excluded from trope/trigger evidence collection below.
    _toc_idx = {c["index"] for c in chapters if c.get("is_toc")}
    # Front/back matter chapters (dedication, about-the-author, ...) are
    # not story content either: excluded from the same evidence
    # collection as ToC chapters (trope/trigger evidence, quotes, POVs).
    _fm_idx = {c["index"] for c in chapters if c.get("is_frontmatter")}
    _skip_evidence_idx = _toc_idx | _fm_idx
    _label_to_text = {c["label"]: c.get("text", "") for c in chapters}
    # Extraction coverage: chapters with successful Call B results. Failed
    # chapters are NOT treated as "no content" — frequency denominators use
    # n_successful, and low coverage sets a warning flag on the result.
    n_successful_b = sum(1 for b in chapter_bs.values() if b)
    coverage_warning = (n_successful_b / n < 0.5) if n else True

    # --- POVs: named in >=2 chapters (or >=1 if fewer than 4 chapters) ---
    # (Computed here because character importance needs is_pov.)
    pov_counts = Counter()
    for idx in sorted(chapter_as):
        if idx in _fm_idx:
            continue  # front/back matter: not a story POV source
        a = chapter_as[idx]
        if a and a.get("pov_character"):
            pov_counts[_roster_canonical(roster, a["pov_character"])] += 1
    # Require POV in at least max(2, 10%) of chapters (filters one-off misfires).
    _pov_threshold = max(2, n // 10) if n >= 4 else 1
    povs = sorted(p for p, c in pov_counts.items() if c >= _pov_threshold)
    _pov_set = {p.lower() for p in povs}

    # --- Characters: from the roster (aliases already merged) ---
    # Keep only characters with a proper name OR 3+ chapter appearances.
    # (Filters the long tail of generic one-off mentions.)
    characters = []
    for e in sorted(roster.values(), key=lambda x: -x["appearances"]):
        n_ch = len(e.get("chapters", set()))
        # Keep proper names (2+ chapters in long books) or 3+ chapter appearances.
        min_ch = 2 if n > 30 else 1
        if not ((_is_proper(e["name"]) and n_ch >= min_ch) or n_ch >= 3):
            continue
        role = e["roles"].most_common(1)[0][0] if e["roles"] else None
        # Description: prefer earliest evidence-backed (character speaks/acts),
        # then longest. Falls back to longest overall if none have evidence.
        _descs = e.get("descriptions", [])  # [(chapter_idx, text, has_evidence)]
        # Handle legacy plain-string format (shouldn't occur, but be safe)
        _descs = [d if isinstance(d, tuple) else (0, d, False) for d in _descs]
        if _descs:
            _with_ev = [d for d in _descs if d[2]]
            if _with_ev:
                # Earliest evidence-backed (first speaks/acts), then longest
                _pool_sorted = sorted(_with_ev, key=lambda x: (x[0], -len(x[1])))
                desc = _pool_sorted[0][1]
            else:
                # No evidence: longest overall
                desc = max(_descs, key=lambda x: len(x[1]))[1]
        else:
            desc = ""
        ev = e.get("evidence", "")
        _statuses = e.get("statuses") or Counter()
        _status = _statuses.most_common(1)[0][0] if _statuses else "unknown"
        # Sticky death: once reported dead with evidence, stays dead unless
        # the character acts on the page in a later chapter.
        _death_reports = e.get("death_reports", [])
        if _death_reports:
            _first_death = min(_death_reports)
            _acted_later = any(
                idx > _first_death for idx in e.get("alive_reports", set()))
            if not _acted_later:
                _status = "dead"
        _appearances = e.get("appearances_desc") or []
        _appearance = max(_appearances, key=len) if _appearances else ""
        # Minor protection: blank appearance for minors; drop sexualized text.
        if _is_minor(role, desc):
            _appearance = ""
        elif _appearance and _SEXUALIZED_RE.search(_appearance):
            _appearance = ""
        # First appearance: lowest chapter index (1-based, matches --chunks numbering)
        _chapters_sorted = sorted(e.get("chapters", set()))
        # Deterministic role from observed data (replaces unreliable LLM roles).
        _role_llm = role  # original LLM assignment, kept for comparison
        _is_pov = e["name"].lower() in _pov_set
        _det_role = _char_deterministic_role(n_ch, n_successful_b, _is_pov,
                                             _role_llm)
        _freq = round(n_ch / n_successful_b, 4) if n_successful_b else 0
        characters.append({
            "name": e["name"],
            "aliases": sorted(e["aliases"]),
            "unresolved_mentions": sorted(e.get("unresolved_mentions", set())),
            "role": _det_role,
            "role_llm": _role_llm,
            "description": desc,
            "appearance": _appearance,
            "status": _status,
            "first_appearance_chapter": _chapters_sorted[0] if _chapters_sorted else None,
            "first_appearance": _chapters_sorted[0] if _chapters_sorted else None,
            "evidence": ev,
            "evidence_verified": bool(ev),
            "evidence_offered": bool(ev),
            "appearances": e["appearances"],
            "chapters_present": n_ch,
            "chapter_count": n_ch,
            "mention_count": e["appearances"],
            "frequency": _freq,
            "is_pov": _is_pov,
            "confidence": _v2_char_confidence(e["appearances"], bool(ev)),
        })

    # --- Relationships: canonical-remap, self-guard, unordered-pair dedupe ---
    # Exclusive types resolve by precedence; non-exclusive kept as extras w/ evidence.
    _pair_cands = {}    # unordered (a, b) -> list of candidate dicts
    _pair_chapters = {}  # unordered (a, b) -> set of chapter indices
    _self_pairs_extracted = 0  # LLM emitted from == to verbatim
    _self_pairs_remapped = 0   # canonical remap collapsed a real pair
    # Author filter: the book's author is never a character, so relationships
    # naming the author are dropped here (the roster filter only covers the
    # character list; relationship endpoints bypass it via _roster_canonical).
    _book_author = getattr(_DIAG, "book_author", None)
    for idx in sorted(chapter_as):
        a = chapter_as[idx]
        if not a:
            continue
        if idx in _fm_idx:
            continue  # front/back matter: not relationship evidence
        for r in a["relationships"]:
            frm = _roster_canonical(roster, r["from"])
            to = _roster_canonical(roster, r["to"])
            if _rel_involves_author({"from": frm, "to": to}, _book_author):
                continue
            # Self-relationship guard (after canonicalisation). The origin
            # is counted: "extracted" pairs were emitted from == to by the
            # LLM, but "remapped" pairs named a real second person whose
            # name/alias canonicalised onto the first character — a real
            # relationship is being eaten, which is a roster-alias problem,
            # not an extraction one (see the field-leakage audit).
            if norm_name(frm) == norm_name(to):
                if norm_name(r["from"]) == norm_name(r["to"]):
                    _self_pairs_extracted += 1
                else:
                    _self_pairs_remapped += 1
                continue
            rtype = r["type"]
            # Canonical form (direction convention, see _canonical_pair):
            # endpoints in canonical order, asymmetric types flipped on
            # reversal, so claims from different chapters are scored as the
            # same fact. Never None here: self-pairs were dropped above.
            _cp = _canonical_pair(frm, to, rtype)
            if _cp is None:
                continue
            frm, to, rtype = _cp
            _pair_key = tuple(sorted([frm.lower(), to.lower()]))
            _pair_chapters.setdefault(_pair_key, set()).add(idx)
            _pair_cands.setdefault(_pair_key, []).append({
                "from": frm, "to": to, "type": rtype,
                "evidence": r.get("evidence", ""),
                "evidence_verified": bool(r.get("evidence")),
                "evidence_offered": bool(r.get("evidence")),
                "chapter": idx_to_label.get(idx, ""),
                "_idx": idx,
            })

    if _self_pairs_extracted or _self_pairs_remapped:
        print(f"  Self-relationship guard: dropped "
              f"{_self_pairs_extracted + _self_pairs_remapped} self-pair(s) "
              f"({_self_pairs_extracted} extracted, "
              f"{_self_pairs_remapped} created by canonical remap)")

    relationships = []
    _guard_suppressed = []  # non-family claims suppressed by the family guard
    _CONF_RANK = {"high": 0, "medium": 1, "low": 2}
    _TYPE_SPEC_RANK = {"specific": 0, "moderate": 1, "vague": 2}
    for _pair_key, cands in _pair_cands.items():
        # Score each candidate by confidence; keep only the best per pair.
        _scored = []
        for c in cands:
            t = c["type"]
            if t not in _REL_PRECEDENCE and t not in _REL_NONEXCLUSIVE:
                print(f"  Dropping unknown relationship type: {t!r}")
                continue
            _n_quotes = sum(1 for cc in cands
                            if cc["type"] == t and cc.get("evidence"))
            _co = len(_pair_chapters.get(_pair_key, set()))
            _conf = _rel_confidence(_n_quotes, _co, t)
            _scored.append((_CONF_RANK[_conf],
                            _TYPE_SPEC_RANK[_rel_type_band(t)],
                            0 if c.get("evidence") else 1,
                            c["_idx"], _conf, _n_quotes, _co, c))
        if not _scored:
            continue
        # Best: highest confidence, then most specific type, then evidence,
        # then earliest chapter.
        _scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        # Family guard: a family claim (spouse/parent/child/sibling) is the
        # canonical winner for its pair. Family types are all "specific"
        # band, so a family claim's matrix confidence is never below medium
        # — but a strong-evidence non-family claim (repeated "enemy") can
        # still outrank a medium family claim on confidence alone. Family
        # facts are high-stakes, and decision-model validation below
        # arbitrates the family claim itself (keep / downgrade to "other" /
        # drop), so when any family candidate exists the winner is chosen
        # from the family pool. The suppressed non-family winner is kept
        # for audit in dropped_relationships.
        _fam = [s for s in _scored if s[7]["type"] in _REL_FAMILY_TYPES]
        if _fam and _scored[0][7]["type"] not in _REL_FAMILY_TYPES:
            _sup = _scored[0]
            _sup_rel = {k: v for k, v in _sup[7].items()
                        if not k.startswith("_")}
            _sup_rel["confidence"] = _sup[4]
            _sup_rel["evidence_count"] = _sup[5]
            _sup_rel["cooccurrence_count"] = _sup[6]
            _sup_rel["family_guard"] = "suppressed_by_family"
            _sup_rel["suppressed_by_type"] = _fam[0][7]["type"]
            _guard_suppressed.append(_sup_rel)
            print(f"  Family guard: {_sup_rel['from']} / {_sup_rel['to']} "
                  f"'{_sup_rel['type']}' suppressed by family claim "
                  f"'{_fam[0][7]['type']}'")
            _scored = _fam
        _, _, _, _, _conf, _nq, _co, _w = _scored[0]
        _rel = {k: v for k, v in _w.items() if not k.startswith("_")}
        _rel["confidence"] = _conf
        _rel["evidence_count"] = _nq
        _rel["cooccurrence_count"] = _co
        relationships.append(_rel)

    # --- Relationship importance: 1-5 from chapter co-occurrence ---
    # More chapters together = more important. Scaled to book length.
    _n_chapters = max(1, len(chapters))
    for rel in relationships:
        _pk = tuple(sorted([rel["from"].lower(), rel["to"].lower()]))
        _co = len(_pair_chapters.get(_pk, set()))
        # 1 chapter = 1, ~20% of book = 5
        rel["importance"] = max(1, min(5, round(1 + 4 * _co / max(1, _n_chapters * 0.2))))

    # --- Family relationship validation (decision model) ---
    # High-stakes types (spouse/parent/child/sibling): the claim is judged
    # against its evidence quote. Three tiers:
    #   P >= 0.70: keep the type.
    #   0.30 <= P < 0.70: downgrade to "other" (relationship exists;
    #     we're just unsure of the type).
    #   P < 0.30: drop entirely (likely hallucinated, not just mistyped).
    # No evidence (no quote, no binding-grade mined sentence) -> drop,
    # fail closed, without spending a decision-model call.
    # In preview mode scores/notes are recorded but nothing is changed.
    # Draws from the shared per-book decision-model budget (merges first).
    _dropped_relationships = []
    # Pair-evidence mining: a family claim's single extracted quote is
    # often topical rather than establishing, and the validator can
    # only judge what it is shown (FW -13: Fen->Xaden parent dropped
    # at P=0.04 on a non-establishing quote, while Mira->Violet
    # sibling was kept at P=0.96 on an establishing one). Before
    # judging, mine the chapter texts for a sentence naming both
    # endpoints; only a binding-grade sentence may replace the quote
    # the validator sees. Co-occurrence/plain hits never upgrade
    # validation evidence. The claim's original quote is never
    # altered; what the validator judged is recorded on the claim as
    # evidence_mined / evidence_grade. ToC and front/back matter are
    # excluded, as for all other evidence. Results are cached: the
    # write-evidence pass below reuses them.
    _mine_chapters = [c for c in chapters
                      if c["index"] not in _skip_evidence_idx]
    _mine_cache = {}

    def _mine(frm, to):
        _k = (frm.lower(), to.lower())
        if _k not in _mine_cache:
            _mine_cache[_k] = _mine_pair_evidence(
                frm, to, _mine_chapters, roster)
        return _mine_cache[_k]

    for rel in relationships:
        _rt = rel.get("type", "")
        if _rt not in _REL_FAMILY_TYPES:
            continue
        _ev = (rel.get("evidence") or "").strip()
        _mined = _mine(rel["from"], rel["to"])
        if _mined:
            rel["evidence_grade"] = _mined["grade"]
            if _mined["grade"] == "binding":
                rel["evidence_mined"] = _mined["sentence"]
                _ev = _mined["sentence"]
        if not _ev:
            # No evidence at all — no extracted quote and no
            # binding-grade mined sentence: fail closed. The claim
            # DROPS rather than surviving as an unjudged downgrade
            # (FW -14 control set: false family claims that carried
            # junk quotes in -13 were validated and dropped; the same
            # claims survived in -14 purely because no quote was
            # attached — survival must not depend on that accident).
            # No decision-model call is spent. In write mode the
            # claim moves to dropped_relationships with its ORIGINAL
            # family type; the note records the reason (location +
            # note carry the meaning). In preview mode the claim
            # stays in relationships with the note, as with every
            # other validation outcome — preview changes nothing.
            # The budget boundary is separate: a claim WITH a quote
            # whose validation is skipped on budget keeps its type
            # with note "budget_exhausted" below; "the validator
            # wasn't consulted" is never conflated with "no evidence".
            rel["validation_p"] = None
            rel["validation_note"] = "no_evidence"
            if not preview:
                _dropped_relationships.append(rel)
            continue
        if _DECISION_VALIDATOR is None:
            continue  # no validator configured; leave as-is
        if not _dm_budget_claim(1):
            rel["validation_note"] = "budget_exhausted"
            continue
        _v, _p = _DECISION_VALIDATOR.validate_relationship(
            rel["from"], rel["to"], _rt, _ev)
        if _p is None:
            # Validator error: fail open, keep the original type.
            rel["validation_note"] = "validator_error"
            continue
        rel["validation_p"] = round(_p, 4)
        rel["validation_model"] = _DECISION_VALIDATOR.model
        if _p >= _REL_VALIDATION_THRESHOLD:
            rel["validation_note"] = "kept"
        elif _p >= _REL_DROP_THRESHOLD:
            rel["validation_note"] = "downgraded"
            if not preview:
                rel["type"] = "other"
        else:
            # Very low P: likely hallucinated. Drop (audit in preview).
            rel["validation_note"] = "dropped" if not preview else "would_drop"
            if not preview:
                _dropped_relationships.append(rel)
    if _dropped_relationships:
        _drop_ids = {id(r) for r in _dropped_relationships}
        relationships = [r for r in relationships if id(r) not in _drop_ids]
        print(f"  Relationship validation: dropped "
              f"{len(_dropped_relationships)} hallucinated relationship(s)")
    if _guard_suppressed:
        # Audit trail only: these claims lost to a family claim for the
        # same pair (family guard above); they were never emitted.
        _dropped_relationships.extend(_guard_suppressed)

    # --- Mined write evidence for evidence-less survivors ---
    # A surviving claim with no extracted quote would be skipped at
    # write (kept in the preview only). When the miner found a
    # sentence naming both endpoints, it can serve as the write
    # evidence instead: family claims require a binding-grade
    # sentence (the same bar as validation); non-family claims accept
    # any both-names sentence (it evidences the interaction itself).
    # Claims that went through family validation keep the family bar
    # even after a downgrade changed their type to "other" (their
    # validation_note is the trace). Mined evidence is marked
    # evidence_source="mined" so provenance survives into the preview
    # and the write payload; claims with original evidence are
    # untouched.
    for rel in relationships:
        if (rel.get("evidence") or "").strip():
            continue
        _mined = _mine(rel["from"], rel["to"])
        if not _mined:
            continue
        _family_bar = (rel.get("type") in _REL_FAMILY_TYPES
                       or "validation_note" in rel)
        if _family_bar and _mined["grade"] != "binding":
            continue
        rel["evidence"] = _mined["sentence"]
        rel["evidence_source"] = "mined"
        rel["evidence_grade"] = _mined["grade"]
        if _mined["grade"] == "binding":
            rel.setdefault("evidence_mined", _mined["sentence"])

    if show_matrices:
        print(_rel_confidence_ascii(relationships))

    # --- Triggers: max severity per category, chapter counts, top evidence ---
    trig_acc = {c: {"sev": 0, "chapters": [], "chapter_indices": [], "evidence": []}
                for c in TRIGGER_CATEGORIES}
    for idx in sorted(chapter_bs):
        b = chapter_bs[idx]
        if not b:
            continue
        if idx in _skip_evidence_idx:
            continue  # ToC/front matter: titles/bios, not trigger evidence
        for cat in TRIGGER_CATEGORIES:
            sev = _SEVERITY_ORDER[b["triggers"][cat]]
            if sev > 0:
                acc = trig_acc[cat]
                acc["sev"] = max(acc["sev"], sev)
                acc["chapters"].append(idx_to_label.get(idx, ""))
                acc["chapter_indices"].append(idx)
                q = b["trigger_evidence"].get(cat)
                if q and len(acc["evidence"]) < 3:
                    acc["evidence"].append({"quote": q,
                                            "chapter": idx_to_label.get(idx, "")})
    sev_names = ["none", "mentioned", "on_page", "graphic"]
    triggers = []
    _gated_dropped = 0
    for cat in TRIGGER_CATEGORIES:
        acc = trig_acc[cat]
        if acc["sev"] == 0:
            continue
        # on_page/graphic without a verified quote is downgraded first: a severity
        # claim needs textual evidence, not just the model's assertion.
        sev = acc["sev"]
        if sev >= 2 and not acc["evidence"]:
            sev = 1
        # Regex evidence gates (before chapter-count, so downgrades filter).
        # Conservative: false negatives are worse than false positives for
        # content warnings. Negative patterns (hypothetical/hedged/metaphorical)
        # mean a clear false positive -> drop. Missing positive patterns mean
        # ambiguous evidence -> downgrade to 1, keep the claim.
        if sev >= 2:
            _ev_text = " ".join(e.get("quote", "") for e in acc["evidence"])
            _dv_used = False
            # Decision-model gate (opt-in via --decision-model).
            # Per-quote judging with hi/lo tiers:
            #   P >= hi -> keep quote at claimed severity
            #   lo <= P < hi -> keep quote, downgrade trigger to "mentioned"
            #   P < lo -> discard the quote
            # The category is dropped only if no quote survives. Top 3
            # surviving quotes by P(yes) are kept as evidence.
            if _DECISION_VALIDATOR is not None and _ev_text.strip():
                _hi = _DECISION_TRIGGER_THRESHOLDS_HI.get(cat, 0.5)
                _lo = _DECISION_TRIGGER_THRESHOLDS_LO.get(cat, 0.35)
                _judged = []  # (p_yes, evidence_entry), scored, ungated
                _dv_error = False
                for _ev in acc["evidence"]:
                    _q = _ev.get("quote", "")
                    _ctx = _extract_quote_context(
                        _label_to_text.get(_ev.get("chapter", ""), ""), _q)
                    if not _dm_budget_claim(1):
                        # Budget exhausted mid-gate: this category is
                        # NOT judged — take the regex-gate fallback,
                        # exactly as on a validator error. The claim is
                        # per actual call (this gate never claimed at
                        # all before, so merge/validation accounting
                        # understated real spend by every trigger call).
                        _dv_error = True
                        break
                    _v, _p = _DECISION_VALIDATOR.validate(cat, _q, _ctx)
                    if _v is None:
                        _dv_error = True
                        break
                    _judged.append((_p, _ev))
                if not _dv_error:
                    _dv_used = True
                    _kept = []  # (p_yes, evidence_entry)
                    _downgrade = False
                    for _p, _ev in _judged:
                        _ev["decision_p"] = round(_p, 4)
                        _ev["decision_gate"] = "decision"
                        _kept_now = _p >= _hi
                        # Record the score for cross-run threshold tuning.
                        _learn().record_threshold(cat, _p, _kept_now)
                        if _kept_now:
                            _kept.append((_p, _ev))
                        elif _p >= _lo:
                            _kept.append((_p, _ev))
                            _downgrade = True
                        # else: discard this quote's evidence
                    if not _kept:
                        _gated_dropped += 1
                        continue
                    if _downgrade:
                        sev = 1
                    _kept.sort(key=lambda x: -x[0])
                    acc["evidence"] = [_ev for _, _ev in _kept[:3]]
            # Regex gates: default path, or fallback if the decision model errored.
            if not _dv_used:
                _was_dv = (_DECISION_VALIDATOR is not None
                           and _ev_text.strip())
                for _ev in acc["evidence"]:
                    _ev["decision_gate"] = "fallback" if _was_dv else "regex"
                _neg_re = _TRIGGER_NEGATIVE_RE.get(cat)
                _gate_re = _TRIGGER_EVIDENCE_RE.get(cat)
                if _neg_re and _neg_re.search(_ev_text):
                    _gated_dropped += 1
                    continue
                elif _gate_re and not _gate_re.search(_ev_text):
                    sev = 1
        # Only drop single-chapter "mentioned" items as noise. A graphic or
        # on_page scene that happens once is exactly what a trigger warning
        # is for — never drop those on chapter count alone.
        if len(acc["chapters"]) < 2 and sev < 2:
            continue
        # Stamp any evidence that never went through a gate (sev was already
        # "mentioned", so no gate ran).
        for _ev in acc["evidence"]:
            _ev.setdefault("decision_gate", "none")
        triggers.append({
            "warning": cat,
            "severity": sev_names[sev],
            "severity_claimed": sev_names[acc["sev"]],
            "chapters": acc["chapters"],
            "chapter_indices": sorted(acc["chapter_indices"]),
            "chapter_count": len(acc["chapters"]),
            "frequency": round(len(acc["chapters"]) / n_successful_b, 4) if n_successful_b else 0,
            "prominence": _trigger_prominence(
                sev_names[sev], len(acc["chapters"]), n_successful_b),
            "evidence": acc["evidence"],
            "evidence_verified": bool(acc["evidence"]),
            "evidence_offered": bool(acc["evidence"]),
            # No textual evidence -> no real confidence. A "mentioned"
            # claim with zero quotes must not carry a 0.65-style score.
            "confidence": (0.0 if not acc["evidence"]
                           else _v2_trigger_confidence(sev, len(acc["chapters"]),
                                                      len(acc["evidence"]))),
        })
    if _gated_dropped:
        print(f"  Trigger gate: dropped {_gated_dropped} unsupported claim(s)")

    # --- Spice: peak-chapter intensity x fraction-at-that-intensity (matrix) ---
    # Triggers warn that content EXISTS; spice level measures PERVASIVENESS
    # at the peak intensity. A single explicit scene in a long book is not
    # a 5, even if many chapters have mild content.
    # Known chapters only: a chapter whose spice is unknown (None —
    # the model omitted or garbled it) is excluded from both the
    # levels and the frequency denominator, never folded in as a
    # measured 0. No known chapter at all -> the book level is itself
    # unknown (None) and the works.spice_detected write is skipped
    # (write_claims already guards on None).
    spices = [b["spice_level"] for b in chapter_bs.values()
              if b and b.get("spice_level") is not None]
    spice_peak = max(spices) if spices else 0
    _sb_peak, _sb_freq, _spice_lvl = _spice_band_detail(spices, len(spices))
    spice_level = _spice_lvl if spices else None
    if show_matrices:
        print(_spice_matrix_ascii(_sb_peak, _sb_freq, spice_level))
    # Per-chapter spice array (1-based; index 0 unused) for the Spice Forecast
    # app feature and --recalibrate-spice. Bounded by the book's true
    # chapter count: chapter_bs keys are original indices, which a
    # --chunks subset's len(chapters) would clip to zeroes.
    chapter_spice = _chapter_spice_array(
        chapter_bs, n_chapters_total if n_chapters_total else n)

    # --- Dialogue/narration ratio: regex-based, no LLM calls ---
    # Per-chapter fraction of characters inside quotes. Book-level average
    # for the app's "dialogue-heavy" badge.
    _chapter_dialogue = {}
    for c in chapters:
        _ci = c.get("index")
        _ct = c.get("text", "")
        if _ci is not None and _ct:
            _chapter_dialogue[_ci] = _dialogue_ratio(_ct)
    avg_dialogue_ratio = (round(sum(_chapter_dialogue.values()) / len(_chapter_dialogue), 4)
                          if _chapter_dialogue else 0.0)

    # --- Trope candidates: union across chapters (gate runs separately) ---
    # (POVs computed earlier, before the characters section.)
    # Map to catalog IDs via embeddings when available, so paraphrases
    # ("enemies to lovers" vs "enemies-to-lovers dynamic") collapse before
    # tiering. Falls back to text normalization if embeddings unavailable.
    trope_candidates = []
    trope_counts = Counter()
    seen_t = set()
    _pov_tropes_dropped = set()  # _tnorm keys of POV labels filtered out
    red_trope_chapters = {}  # display-key -> [chapter labels]; seeded here,
    # rebuilt with catalog-collapsed keys by the embedding branch below.
    for idx in sorted(chapter_bs):
        b = chapter_bs[idx]
        if not b:
            continue
        if idx in _skip_evidence_idx:
            continue  # ToC/front matter: titles/bios, not trope evidence
        seen_this_chapter = set()
        for t in b["trope_candidates"]:
            k = _tnorm(t)
            if not k or k in seen_this_chapter:
                continue
            if _is_pov_trope_label(t):
                # POV metadata, not a trope: never counted, never a
                # candidate, so it cannot reach the gate or proposals.
                _pov_tropes_dropped.add(k)
                continue
            seen_this_chapter.add(k)
            trope_counts[k] += 1
            # Chapter-level evidence fallback: the embedding branch below
            # rebuilds this with catalog-collapsed keys, but without
            # embeddings this mapping is the only chapter evidence tropes
            # get. One chapter lists each trope at most once.
            red_trope_chapters.setdefault(k, []).append(
                idx_to_label.get(idx, ""))
            if k not in seen_t:
                seen_t.add(k)
                trope_candidates.append(_clean_trope(t))
    # Catalog mapping: collapse paraphrases to catalog IDs before tiering.
    # (red_trope_chapters was seeded by the union loop above; the embedding
    # branch below rebuilds it with catalog-collapsed keys.)
    catalog_map = {}  # _tnorm(candidate) -> catalog_id (or None if unmapped)
    if trope_candidates and CONFIG.get("embed_url"):
        try:
            catalog = trope_catalog_vectors()
            if catalog:
                threshold = CONFIG.get("trope_map_threshold", 0.78)
                vecs = embed_vectors(CONFIG["embed_url"],
                                     CONFIG.get("embed_model", ""),
                                     trope_candidates)
                vecs = [_normalize(v) for v in vecs]  # dot == cosine now
                for name, vec in zip(trope_candidates, vecs):
                    best_id, best_sim = None, -1
                    for cid, cname, cvec in catalog:
                        sim = _dot(vec, cvec)
                        if sim > best_sim:
                            best_id, best_sim = cid, sim
                    catalog_map[_tnorm(name)] = best_id if best_sim >= threshold else None
                # Re-count chapters per displayed name (catalog name for mapped).
                # One chapter can list two paraphrases of the same trope —
                # count each key at most once per chapter.
                names = {ci: cn for ci, cn, _ in catalog}  # catalog_id -> name
                new_counts, new_candidates, new_seen = Counter(), [], set()
                new_catalog_map = {}
                # Rebuild chapter evidence with catalog-collapsed keys,
                # replacing the candidate-keyed fallback seeded above.
                red_trope_chapters = {}
                for idx in sorted(chapter_bs):
                    b = chapter_bs[idx]
                    if not b:
                        continue
                    if idx in _skip_evidence_idx:
                        continue  # ToC/front matter: not trope evidence
                    seen_this_chapter = set()
                    for t in b["trope_candidates"]:
                        k = _tnorm(t)
                        if not k:
                            continue
                        if _is_pov_trope_label(t):
                            continue  # filtered in the union loop above
                        cid = catalog_map.get(k)
                        shown = names.get(cid) if cid else None
                        shown = shown or _clean_trope(t)
                        key = _tnorm(shown)
                        if key in seen_this_chapter:
                            continue
                        seen_this_chapter.add(key)
                        new_counts[key] += 1
                        red_trope_chapters.setdefault(key, []).append(
                            idx_to_label.get(idx, ""))
                        if cid:
                            new_catalog_map[key] = cid
                        if key not in new_seen:
                            new_seen.add(key)
                            new_candidates.append(shown)
                # Swap in the catalog-collapsed counts (keyed by display name).
                trope_candidates = new_candidates
                trope_counts = new_counts
                catalog_map = new_catalog_map
                n_mapped = len(new_catalog_map)
                n_unmapped = len(new_candidates) - n_mapped
                print(f"  Trope catalog mapping: {n_mapped} mapped, {n_unmapped} unmapped")
        except Exception as e:
            print(f"  (catalog mapping skipped: {e})")
    if _pov_tropes_dropped:
        print(f"  Trope filter: dropped {len(_pov_tropes_dropped)} "
              f"POV label(s) from trope candidates")
    # Stash the mapping for write_claims.
    red_catalog_map = {k: v for k, v in catalog_map.items() if v}
    # red_trope_chapters: catalog-collapsed keys when embeddings ran, else
    # the candidate-keyed fallback seeded by the union loop (never empty
    # when candidates exist, so trope claims always carry chapter evidence).

    # --- Quotes: filtered, ranked by length * distinctiveness, top 5-8 ---
    def _qtokens(s):
        return set((s or "").lower().split())

    _cand_quotes = []
    for idx in sorted(chapter_bs):
        b = chapter_bs[idx]
        if not b:
            continue
        if idx in _fm_idx:
            continue  # quotes are never drawn from front/back matter
        _ch_label = idx_to_label.get(idx, "")
        _sum_tokens = _qtokens(b.get("summary", ""))
        for q in b["quotes"]:
            _text = q.get("text", "")
            # Drop short quotes
            if len(_text) < 40:
                continue
            # Drop quotes without speaker attribution
            if not q.get("speaker"):
                continue
            _qt = _qtokens(_text)
            # Drop quotes that mostly restate the chapter summary
            if _qt and _sum_tokens:
                if len(_qt & _sum_tokens) / len(_qt) > 0.6:
                    continue
            _cand_quotes.append({**q, "chapter": _ch_label, "_tokens": _qt})

    # Rank by (length * distinctiveness); distinctiveness = 1 - max token
    # overlap with any other candidate quote.
    _scored = []
    for i, q in enumerate(_cand_quotes):
        _qt = q["_tokens"]
        _max_ov = 0.0
        for j, o in enumerate(_cand_quotes):
            if i == j:
                continue
            _ot = o["_tokens"]
            if _qt and _ot:
                _max_ov = max(_max_ov, len(_qt & _ot) / len(_qt))
        _distinct = 1.0 - _max_ov
        _scored.append((len(q.get("text", "")) * _distinct, q))
    _scored.sort(key=lambda x: -x[0])
    _qlimit = 8 if len(_scored) > 20 else 5
    quotes = [{k: v for k, v in q.items() if not k.startswith("_")}
              for _, q in _scored[:_qlimit]]

    # --- Chapter summaries ---
    chapter_summaries = [{"index": c["index"], "label": c["label"],
                          "summary": (chapter_bs.get(c["index"]) or {}).get("summary", "")}
                         for c in chapters]

    # --- Verification totals ---
    v_checked = v_verified = 0
    for src in list(chapter_as.values()) + list(chapter_bs.values()):
        if src and src.get("verification"):
            v_checked += src["verification"].get("evidence_checked", 0)
            v_verified += src["verification"].get("evidence_verified", 0)

    # --- Grounding: verify names against raw text (catches hallucinations) ---
    _grounding = _ground_entities(characters, relationships, chapters)

    return {
        "characters": characters,
        "relationships": relationships,
        "dropped_relationships": _dropped_relationships,
        "self_pairs_dropped": {"extracted": _self_pairs_extracted,
                               "remapped": _self_pairs_remapped},
        "triggers": triggers,
        "trigger_chapters": _trigger_chapters_map(triggers),
        "spice_level": spice_level,
        "spice_peak": spice_peak,
        "spice_peak_band": _sb_peak,
        "spice_freq_band": _sb_freq,
        "chapter_spice": chapter_spice,
        "avg_dialogue_ratio": avg_dialogue_ratio,
        "chapter_dialogue": _chapter_dialogue,
        "coverage_warning": coverage_warning,
        "chapters_successful": n_successful_b,
        "chapters_total": n,
        "povs": povs,
        "trope_candidates": trope_candidates,
        "trope_candidate_counts": dict(trope_counts),
        "trope_catalog_map": red_catalog_map,
        "trope_chapters": red_trope_chapters,
        "tropes": [],  # filled by the trope gate
        "trope_confidence": {},
        "quotes": quotes,
        "chapter_summaries": chapter_summaries,
        "verification": {"evidence_checked": v_checked,
                         "evidence_verified": v_verified},
        "grounding": _grounding,
    }


PROMPT_V2_TROPE_GATE = """You are confirming trope candidates for a book. For each candidate trope, judge whether it is GENUINELY present based on the chapter summaries. Return ONLY valid JSON. No commentary, no markdown.

Chapter summaries:
{summaries}

Candidate tropes:
{candidates}

{
  "verdicts": [{"trope": "...", "verdict": "yes|no|unsure", "justification": "one line citing a chapter number"}]
}

RULES:
1. "yes" only if a summary clearly supports the trope. "unsure" if plausible but not clearly supported. "no" otherwise.
2. Every verdict needs a justification citing the chapter number (e.g. "Ch 3").
3. Do not add tropes that are not in the candidate list.
4. Judge ONLY from the summary content, never from a chapter title or heading alone. A trope requires the story to actually contain it — a suggestive title (e.g. "Mind Mapping") is not evidence of the trope."""


def _sanitize_v2_gate(r):
    if not isinstance(r, dict):
        return None
    verdicts = []
    for v in _list(r.get("verdicts")):
        if not isinstance(v, dict):
            continue
        trope = _s(v.get("trope"), 80)
        verdict = _s(v.get("verdict"), 10).lower()
        if trope and verdict in ("yes", "no", "unsure"):
            verdicts.append({"trope": trope, "verdict": verdict,
                             "justification": _s(v.get("justification"), 300)})
    return {"verdicts": verdicts}


# Genre/setting labels that are not tropes. Dropped via _tnorm match.
_TROPE_DENYLIST = frozenset({
    "techno-thriller", "technothriller", "techno thriller", "dystopia", "dystopian",
    "conspiracy", "thriller", "sci-fi", "scifi", "sci fi", "science fiction",
    "science-fiction", "fantasy", "romance",
    "mystery", "horror", "comedy", "drama", "adventure", "action",
    "cyberpunk", "steampunk", "space opera", "urban fantasy", "dark fantasy",
    "paranormal", "historical fiction", "literary fiction", "crime",
    "detective", "noir", "western", "war story",
})

def _is_denylisted_trope(name):
    # Normalize hyphens to spaces: "sci-fi" -> "sci fi" matches "science fiction" variants
    key = _tnorm(name).replace("-", " ")
    key = " ".join(key.split())  # collapse double spaces from hyphen replacement
    return key in _TROPE_DENYLIST or _tnorm(name) in _TROPE_DENYLIST


def v2_trope_gate(call, chapter_summaries, candidates, candidate_counts):
    """Confirm/deny trope candidates against chapter summaries (one LLM call).

    Returns (confirmed_names, confidence_dict). Evidence-based trope
    confidence: 0.55 base for a gate "yes" + 0.1 per candidating chapter,
    capped at 0.9.
    """
    confirmed, conf = [], {}
    if not candidates:
        return confirmed, conf
    summaries = "\n".join(
        f"Ch {s['index']} ({s['label']}): {s['summary']}"
        for s in chapter_summaries if s.get("summary"))
    # Batch candidates (large books can produce 200+; a single gate call
    # can't judge them all carefully). Summaries are repeated per batch.
    BATCH = 30
    cand_keys = {_tnorm(c): c for c in candidates}
    for bi in range(0, len(candidates), BATCH):
        batch = candidates[bi:bi + BATCH]
        prompt = (PROMPT_V2_TROPE_GATE
                  .replace("{summaries}", summaries)
                  .replace("{candidates}", "\n".join(f"- {c}" for c in batch)))
        out = _run_task(call, prompt, "", 1, 1, f"v2-trope-gate-b{bi // BATCH + 1}",
                        sanitize_fn=_sanitize_v2_gate)
        if not out:
            continue
        for v in out["verdicts"]:
            key = _tnorm(v["trope"])
            if v["verdict"] == "yes" and key in cand_keys:
                name = cand_keys[key]
                if name not in confirmed:
                    confirmed.append(name)
                    n_ch = candidate_counts.get(key, 1)
                    conf[_tnorm(name)] = round(min(0.9, 0.55 + 0.1 * n_ch), 2)
    return confirmed, conf


def _apply_trope_rejections(tropes, trope_conf, learned):
    """Pure helper: filter tropes by human rejection learning.

    - 3+ global rejections -> dropped (denylist).
    - 1-2 rejections -> dropped unless confidence meets the raised
      threshold (base 0.50 + 0.10 per rejection).
    Returns (kept_tropes, kept_conf, n_filtered)."""
    kept, kept_conf, n_filtered = [], {}, 0
    for t in tropes:
        n_rej = learned.get_trope_rejection_count(_trej_key(t))
        if n_rej >= _REJECT_DENYLIST_COUNT:
            n_filtered += 1
            continue
        thresh = _TROPE_REJECT_BASE_CONF + _REJECT_CONF_STEP * n_rej
        if trope_conf.get(_tnorm(t), 0.0) < thresh:
            n_filtered += 1
            continue
        kept.append(t)
        kept_conf[_tnorm(t)] = trope_conf.get(_tnorm(t), 0.0)
    return kept, kept_conf, n_filtered


# --- v2 chapter extraction for non-EPUB (single-text) books ---
_CHAPTER_HEADING_RE = re.compile(
    r"(?m)^[ \t]*((?:chapter|part|book|section)\s+(?:\d+|[ivxlc]+|one|two|"
    r"three|four|five|six|seven|eight|nine|ten)\b[^\n]{0,60}|prologue|"
    r"epilogue|interlude)[ \t]*$", re.IGNORECASE)


def _prose_chapters(text):
    """Split non-EPUB text into chapter-ish units, then run split_chapters().

    Splits on chapter/part headings when detectable; otherwise falls back to
    fixed-size paragraph splits. Returns indexed chapter units.
    """
    matches = list(_CHAPTER_HEADING_RE.finditer(text))
    units = []
    if len(matches) >= 2:
        bounds = [m.start() for m in matches] + [len(text)]
        for i in range(len(matches)):
            chunk = text[bounds[i]:bounds[i + 1]].strip()
            if len(chunk) > 500:
                label = chunk.split("\n", 1)[0].strip()[:80]
                units.append({"spine": f"part{i + 1}", "label": label,
                              "text": chunk})
    else:
        # Fixed-size split: no chapter structure found. Flag as fallback.
        _DIAG.chapter_detection_fallback = True
        print("!!! CHAPTER DETECTION FALLBACK - fixed-size split, "
              "results may be unreliable")
        target = 30000
        cur, clen = [], 0
        for p in text.split("\n\n"):
            if clen + len(p) > target and cur:
                units.append({"spine": f"part{len(units) + 1}",
                              "label": f"Part {len(units) + 1}",
                              "text": "\n\n".join(cur)})
                cur, clen = [], 0
            cur.append(p)
            clen += len(p)
        if cur:
            units.append({"spine": f"part{len(units) + 1}",
                          "label": f"Part {len(units) + 1}",
                          "text": "\n\n".join(cur)})
    return split_chapters(units)


def chunk_text(text, chunk_size=CHUNK_CHARS):
    # Hard-split any single paragraph larger than a chunk
    paras = []
    for para in text.split("\n\n"):
        while len(para) > chunk_size:
            paras.append(para[:chunk_size])
            para = para[chunk_size:]
        paras.append(para)
    chunks, current, clen = [], [], 0
    for para in paras:
        if clen + len(para) > chunk_size and current:
            chunks.append("\n\n".join(current))
            current, clen = [para], len(para)
        else:
            current.append(para)
            clen += len(para)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


# --- LLM ---
# Fallback identity prompt: no evidence field. Used once per chunk when the rich
# identity prompt returns empty/unparseable output twice. The old mega-prompt's
# evidence-free character section worked reliably on small models, so this is the
# graceful-degradation path: names and descriptions beat no characters at all.
PROMPT_IDENTITY_SIMPLE = """You are extracting character information from a book excerpt.

List every person named or clearly present in this excerpt. Merge obvious aliases: "I"/"my husband"/"the narrator" refer to the narrator; use the most complete name available. Do not invent names. If gender is ambiguous from the name alone, leave it out of the description rather than guessing.

For each pair with a clear relationship, note it.

Respond with ONLY this JSON object, no other text:
{
  "characters": [{"name": "...", "role": "protagonist|antagonist|supporting|minor", "description": "..."}],
  "relationships": [{"from": "...", "to": "...", "type": "spouse|parent|child|sibling|friend|enemy|mentor|colleague|neighbor|other"}]
}

Start your response with {."""

PROMPT_DISCIPLINE = """Analyze this book excerpt and return ONLY valid JSON. No commentary, no markdown, just the JSON object.
Keep any internal reasoning extremely brief — a complete, valid JSON object is the priority; do not let thinking crowd out the answer.

{
  "tropes": ["trope names"],
  "triggers": [{"warning": "...", "detail": "...", "spoiler": false}],
  "spice_level": 0,
  "povs": ["character names who narrate"],
  "quotes": [{"text": "...", "spoiler": false}],
  "is_anthology": false,
  "stories": []
}

RULES — follow these strictly:

1. TROPES: Identify narrative patterns genuinely present in the text. Include clear genre markers (e.g. "memoir", "domestic comedy", "slice of life" for a family memoir). "Enemies to lovers" requires an actual romantic arc between rivals — do not apply it loosely. Prefer accurate tropes over many questionable ones, but do not return an empty list if obvious patterns exist.

2. SPICE LEVEL rubric: 0 = no romantic/sexual content at all. 1 = chaste romance, hand-holding. 2 = kissing, mild innuendo. 3 = explicit sexual references, fade-to-black. 4 = on-page sex, moderate detail. 5 = explicit/graphic. A family memoir defaults to 0.

3. POVS are only characters who actually narrate sections. A character mentioned in an anecdote is not a POV character. In first-person memoir, there is typically ONE pov: the narrator.

4. QUOTES must be exact text copied from the excerpt, not paraphrases or descriptions of the text. If you cannot find a notable verbatim line, return an empty array rather than describing the passage.

5. TRIGGER WARNINGS should reflect genuinely concerning content, not ordinary life stress. An overwhelmed parent is not "child neglect". Children playing is not "violence". Be conservative.

6. ANTHOLOGY: If the excerpt contains multiple distinct stories (not chapters of one story), set is_anthology true and list the story titles you can identify. Otherwise false and []."""


PROMPT_IDENTITY = """Analyze this book excerpt and return ONLY valid JSON. No commentary, no markdown, just the JSON object.
Keep any internal reasoning extremely brief — a complete, valid JSON object is the priority; do not let thinking crowd out the answer.

{
  "characters": [{"name": "...", "role": "protagonist|antagonist|supporting|minor", "description": "...", "evidence": "exact sentence from the excerpt"}],
  "relationships": [{"from": "...", "to": "...", "type": "...", "evidence": "exact sentence from the excerpt"}]
}

RULES — follow these strictly:

1. ENTITY MERGING: If "Mother", "the narrator", "I", and "Shirley Jackson" all refer to the same person, list them ONCE under the most specific name. Never create separate entries for different names/titles of the same individual.

1b. GENDER FROM PRONOUNS: Determine each character's gender from the pronouns used for them in the text (he/him = male, she/her = female). Do not guess gender from the name alone — "Laurie" can be male (Laurence) or female (Laura). Track pronouns carefully.

2. NEVER INVENT NAMES: If someone is only called "the husband" or "S.E.H.", use exactly that. Do not expand initials into full names. Do not guess last names. Do not combine a title with surrounding context into a name (e.g. "Mrs." + "P.T.A." is not a person). If a name is unclear, use the clearest reference available.

3. REAL PEOPLE ONLY: List only people (or animals) who actually appear or are directly involved in the excerpt. Do not list historical figures, celebrities, or fictional characters that are merely mentioned, referenced in play, or used as comparisons.

4. IGNORE PRETEND PLAY: If children are playing games (cowboys, pretend shooting, imaginary scenarios), do NOT treat game events as real plot events. Do not create "killer" relationships from play.

5. RELATIONSHIP TYPES must be accurate: parent-child, sibling, spouse, friend, enemy, mentor, etc. A parent is not a "spouse" of their child. A classmate is not a family member. Verify each relationship against the text — do not assume family connections for every character mentioned near the narrator.

6. EVIDENCE: For each character, include "evidence": the best supporting sentence copied exactly from the excerpt. For each relationship, include "evidence": the best supporting sentence. If no single sentence captures it, leave evidence as an empty string — never invent quotes, and never repeat the same phrase twice.

7. ROLE must be exactly one of: protagonist, antagonist, supporting, minor. When in doubt, use supporting. RELATIONSHIP TYPE must be one of: spouse, parent, child, sibling, friend, enemy, mentor, colleague, neighbor, other. When in doubt, use other."""


def _post_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def llm_ollama(prompt, chunk, i, n):
    payload = {"model": CONFIG["ollama_model"],
               "messages": [{"role": "system", "content": prompt},
                            {"role": "user", "content": f"Excerpt {i}/{n}:\n\n{chunk}"}],
               "stream": False, "format": "json",
               "options": {"num_ctx": NUM_CTX, "temperature": 0.2}}
    # Try /api/chat first (newer/proxied setups), fall back to /api/generate
    for endpoint, key in (("/api/chat", "message"), ("/api/generate", "response")):
        try:
            if endpoint == "/api/generate":
                payload = {"model": payload["model"],
                           "prompt": f"{prompt}\n\nExcerpt {i}/{n}:\n\n{chunk}",
                           "stream": False, "format": "json",
                           "options": payload["options"]}
            resp = _post_json(f"{CONFIG['ollama_host']}{endpoint}", payload,
                              {"Content-Type": "application/json"}, 300)
            if key == "message":
                return resp.get("message", {}).get("content", "")
            return resp.get("response", "")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue  # try next endpoint
            print(f"\n  Ollama error: {e}")
            return None
        except Exception as e:
            print(f"\n  Ollama error: {e}")
            return None
    print("\n  Ollama error: neither /api/chat nor /api/generate available")
    return None


def llm_openrouter(prompt, chunk, i, n):
    try:
        resp = _post_json(
            "https://openrouter.ai/api/v1/chat/completions",
            {"model": CONFIG["openrouter_model"],
             "messages": [{"role": "system", "content": prompt},
                          {"role": "user", "content": f"Excerpt {i}/{n}:\n\n{chunk}"}],
             "max_tokens": 4000},
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {CONFIG['openrouter_key']}",
             "HTTP-Referer": "https://cozylibram.pages.dev"}, 120)
        return resp["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        print(f"\n  OpenRouter {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")
    except Exception as e:
        print(f"\n  OpenRouter error: {e}")
    return None


# Thread-local stash for per-call LLM diagnostics (empty-response forensics).
# Set by llm_openai_compat, read by _run_task. Thread-local because chunks run
# in a ThreadPoolExecutor when --batch > 1.
# --- Rich TUI (optional; falls back to plain prints) ---
try:
    from rich.console import Console as _RichConsole
    from rich.progress import (Progress as _RichProgress, BarColumn as _RichBar,
                               TextColumn as _RichText, TaskProgressColumn as _RichPct,
                               TimeRemainingColumn as _RichETA)
    from rich.table import Table as _RichTable
    from rich import box as _rich_box
    from rich.markup import escape as _rich_escape
    from rich.panel import Panel as _RichPanel
    from rich.layout import Layout as _RichLayout
    from rich.live import Live as _RichLive
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


_VIZ_TYPE_CPS = 30  # typing-effect reveal speed, chars/sec
_VIZ_SPOTLIGHT_SECS = 3  # how long a new-character spotlight stays up
_VIZ_TICKER_MAX = 12  # names kept in the scrolling ticker
_VIZ_MILESTONE_EVERY = 25  # celebrate every N new characters


def _viz_event_kind(msg):
    """Classify a visualizer event for color-coding."""
    if msg.startswith("+ New:"):
        return "new"
    if msg.startswith("→ Merged:"):
        return "merged"
    if msg.startswith("✗ Filtered:"):
        return "filtered"
    if msg.startswith("🎉"):
        return "milestone"
    return "other"


_VIZ_EVENT_STYLES = {
    "new": "green",
    "merged": "yellow",
    "filtered": "red",
    "milestone": "bold magenta",
    "other": "",
}


def _viz_revealed_text(text, t0, now, cps=_VIZ_TYPE_CPS):
    """Typing effect: reveal text progressively, at least 1 char."""
    n = int((now - t0) * cps)
    return text[:max(1, n)]


def _viz_stats_line(st, now):
    """Plain-text stats line for the visualizer panel (no Rich markup)."""
    elapsed_min = max((now - st.get("run_t0", now)) / 60.0, 1e-6)
    speed = int(st.get("chars_total", 0) / elapsed_min)
    avg_toks = speed / 60.0 / 4.0  # ~4 chars/token for English
    # Recent speed: current chapter only.
    chap_elapsed = max(now - st.get("chap_t0", now), 1e-6)
    chap_speed = int(st.get("chap_chars", 0) / chap_elapsed * 60.0)
    cur_toks = chap_speed / 60.0 / 4.0
    ci = st.get("chap_idx") or "?"
    ct = st.get("chap_total") or "?"
    line = "Characters: %d | Chapter %s/%s | %.1f tok/s (avg %.1f tok/s)" % (
        st.get("new_count", 0), ci, ct, cur_toks, avg_toks)
    # P6: ETA from rolling per-chapter average (last 10 chapters).
    times = st.get("chapter_times") or []
    if times and isinstance(ci, int) and isinstance(ct, int) and ct > ci:
        avg = sum(times) / len(times)
        line += " | ETA: %s" % _format_eta(avg, ct - ci)
    return line


def _format_eta(avg_sec, remaining):
    """Pure: format ETA from avg seconds/chapter and chapters remaining."""
    if not avg_sec or avg_sec <= 0 or remaining <= 0:
        return "calculating..."
    total = avg_sec * remaining
    if total < 60:
        return "~%d sec remaining" % int(total)
    mins = total / 60.0
    if mins < 60:
        return "~%d min remaining" % int(round(mins))
    return "~%.1f hr remaining" % (mins / 60.0)


def _format_duration(total_sec):
    """Pure: format seconds as '42m 15s', '3h 5m', or '45s'."""
    total_sec = max(int(total_sec or 0), 0)
    if total_sec < 60:
        return "%ds" % total_sec
    mins, secs = divmod(total_sec, 60)
    if mins < 60:
        return "%dm %ds" % (mins, secs)
    hrs, mins = divmod(mins, 60)
    return "%dh %dm" % (hrs, mins)


def _detect_bottleneck(samples):
    """Pure: infer bottleneck from [(input_chars, duration_sec), ...].

    Prefill-bound if long chapters take proportionally longer than short
    ones; otherwise decode-bound or API-latency-bound. Returns
    (label, suggestion).
    """
    if len(samples) < 3:
        return ("collecting data...", "")
    by_len = sorted(samples, key=lambda s: s[0])
    half = len(by_len) // 2
    short, long = by_len[:half], by_len[half:]
    if not short or not long:
        return ("collecting data...", "")
    short_avg = sum(s[1] for s in short) / len(short)
    long_avg = sum(s[1] for s in long) / len(long)
    if short_avg <= 0:
        return ("collecting data...", "")
    if long_avg / short_avg > 1.5:
        return ("Prefill-bound (long chapters)", "batch 8 helps")
    return ("Decode-bound or API latency", "try smaller batches")


def _viz_new_state(book_key):
    """Fresh visualizer state dict (run-level keys included)."""
    now = time.time()
    return {"book": book_key, "label": "", "text": "", "names": [],
            "events": [], "t0": now, "spotlight": None,
            "run_t0": now, "chars_total": 0, "new_count": 0,
            "milestones": set(), "ticker": [],
            "chap_idx": None, "chap_total": None}


class _VizRenderable:
    """Rich renderable for the chapter visualizer panel.

    Reads live state from the owning PipelineUI on each refresh. The scan
    marker position is derived from wall-clock time, so it animates with the
    Live's regular refresh cycle (no timer thread needed).
    """
    def __init__(self, ui):
        self._ui = ui

    def __rich_console__(self, console, options):
        yield self._ui._render_viz_panel()


class PipelineUI:
    """Terminal UI for the pipeline.

    Rich mode: live progress bars (one per book, thread-safe for --jobs),
    a chapter visualizer panel below the bars, status messages above,
    summary tables on completion.
    Fallback: plain prints, same information, no dependencies.
    """
    def __init__(self):
        self.rich = _HAS_RICH and sys.stdout.isatty()
        self.console = _RichConsole() if self.rich else None
        self.progress = None
        self.tasks = {}  # book_key -> task_id
        self._labels = {}  # book_key -> base label (unescaped)
        self._lock = threading.Lock()
        # Chapter visualizer: single panel, most recently active book.
        self._viz_enabled = True  # flipped by --no-viz
        self._viz = None  # {book, label, text, names, events, t0}
        self._live = None
        self._layout = None

    def set_viz_enabled(self, enabled):
        """Enable/disable the chapter visualizer panel (--no-viz)."""
        self._viz_enabled = bool(enabled)

    def _viz_usable(self):
        return self.rich and self._viz_enabled

    def start(self):
        if self.rich and self._live is None:
            self.progress = _RichProgress(
                _RichText("[bold cyan]{task.description}"),
                _RichBar(bar_width=30),
                _RichPct(),
                _RichETA(),
                console=self.console,
                transient=False,
            )
            # Single Live drives both the progress bars and the visualizer
            # panel (Progress is rendered as a layout child; its internal
            # Live is never started).
            if self._viz_enabled:
                self._layout = _RichLayout()
                # Panel rows: base 24 + 2 for bottleneck line + 2 more when
                # matrix viz is on (--show-matrices).
                viz_size = 28 if getattr(self, '_show_matrices', False) else 26
                self._layout.split_column(
                    _RichLayout(self.progress, name="bars"),
                    _RichLayout(_VizRenderable(self), name="viz", size=viz_size),
                )
                renderable = self._layout
            else:
                renderable = self.progress
            self._live = _RichLive(renderable, console=self.console,
                                   refresh_per_second=4, transient=False)
            self._live.start()

    def stop(self):
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None
            self._layout = None
        self.progress = None
        self.tasks = {}
        self._labels = {}
        with self._lock:
            self._viz = None

    def book_start(self, book_key, label, total):
        """Register a book; returns nothing. total = work units."""
        if self.progress:
            with self._lock:
                self._labels[book_key] = label
                disp = _rich_escape(label) if self.rich else label
                tid = self.progress.add_task(disp, total=total)
                self.tasks[book_key] = tid
        else:
            print(f"\n{label} ({total} steps)")

    def advance(self, book_key, n=1):
        if self.progress:
            tid = self.tasks.get(book_key)
            if tid is not None:
                try:
                    self.progress.update(tid, advance=n)
                except KeyError:
                    pass

    def set_phase(self, book_key, phase):
        """Update the task description to show current phase."""
        if self.progress:
            tid = self.tasks.get(book_key)
            if tid is not None:
                base = self._labels.get(book_key, "")
                disp = _rich_escape(f"{base} — {phase}") if self.rich else f"{base} — {phase}"
                try:
                    self.progress.update(tid, description=disp)
                except KeyError:
                    pass

    def status(self, msg):
        """Print a status line (above progress bars in rich mode)."""
        if self.progress:
            self.progress.console.print(_rich_escape(msg))
        else:
            print(msg)

    def book_done(self, book_key):
        if self.progress:
            with self._lock:
                tid = self.tasks.pop(book_key, None)
                self._labels.pop(book_key, None)
                if tid is not None:
                    try:
                        self.progress.remove_task(tid)
                    except KeyError:
                        pass

    def viz_chapter(self, book_key, chapter_label, text, chap_idx=None,
                    chap_total=None):
        """Start visualizing a chapter (call at the start of Call A)."""
        if not self._viz_usable():
            return
        with self._lock:
            prev = self._viz or {}
            now = time.time()
            # P6/P11: rolling per-chapter timing for ETA + bottleneck.
            # Previous chapter's duration = now - its chap_t0.
            times = list(prev.get("chapter_times") or [])
            lens = list(prev.get("chapter_lens") or [])
            _pt0, _pchars = prev.get("chap_t0"), prev.get("chap_chars")
            if _pt0 and _pchars:
                _dur = max(now - _pt0, 0.01)
                times.append(_dur)
                times = times[-10:]
                lens.append((_pchars, _dur))
                lens = lens[-20:]
            txt = (text or "")[:800]
            self._viz = {
                "book": book_key,
                "label": chapter_label or "",
                "text": txt,
                "names": [],
                "events": prev.get("events", [])[-8:],
                "t0": now,
                "spotlight": None,
                # Run-level state, carried across chapters.
                "run_t0": prev.get("run_t0") or now,
                "chars_total": prev.get("chars_total", 0) + len(txt),
                "new_count": prev.get("new_count", 0),
                "milestones": prev.get("milestones") or set(),
                "ticker": prev.get("ticker") or [],
                "chap_idx": chap_idx,
                "chap_total": chap_total,
                "chap_t0": now,
                "chap_chars": len(txt),
                "chapter_times": times,
                "chapter_lens": lens,
                # Live matrix snapshot (--show-matrices), carried over so the
                # panel doesn't flicker to empty between chapters.
                "matrix": prev.get("matrix"),
            }

    def viz_characters(self, book_key, characters):
        """Highlight identified names after Call A returns."""
        if not self._viz_usable():
            return
        names = []
        for c in (characters or []):
            n = (c.get("name") or "").strip()
            if n:
                names.append(n)
        with self._lock:
            if self._viz is None:
                self._viz = _viz_new_state(book_key)
            self._viz["book"] = book_key
            self._viz["names"] = names

    def viz_event(self, book_key, msg):
        """Append a line to the roster event feed."""
        if not self._viz_usable():
            return
        with self._lock:
            if self._viz is None:
                self._viz = _viz_new_state(book_key)
            self._viz["book"] = book_key
            ev = self._viz["events"]
            ev.append(msg)
            if _viz_event_kind(msg) == "new":
                name = msg[len("+ New:"):].strip()
                self._viz["new_count"] = self._viz.get("new_count", 0) + 1
                self._viz["spotlight"] = {"name": name, "t": time.time()}
                ticker = self._viz.setdefault("ticker", [])
                ticker.append(name)
                del ticker[:-_VIZ_TICKER_MAX]
                n = self._viz["new_count"]
                milestones = self._viz.setdefault("milestones", set())
                if n % _VIZ_MILESTONE_EVERY == 0 and n not in milestones:
                    milestones.add(n)
                    ev.append("🎉 %d characters discovered!" % n)
            del ev[:-8]

    def update_matrix_data(self, char_freq, spice_peak, spice_counts,
                           rel_count):
        """Store a live matrix snapshot for the viz panel (--show-matrices).

        Called per chapter from process_file_v2; the Rich Live refresh
        renders it at 4fps. Thread-safe; no-op when the viz is unusable.
        """
        if not self._viz_usable():
            return
        with self._lock:
            if self._viz is None:
                self._viz = _viz_new_state(None)
            self._viz["matrix"] = {
                "char_freq": dict(char_freq or {}),
                "spice_peak": spice_peak or 0,
                "spice_counts": dict(spice_counts or {}),
                "rel_count": rel_count or 0,
            }

    def _render_viz_panel(self):
        """Build the visualizer Panel. Called on each Live refresh."""
        with self._lock:
            st = None
            if self._viz is not None:
                st = dict(self._viz)
                st["events"] = list(st.get("events", []))
                st["ticker"] = list(st.get("ticker", []))
                spot = st.get("spotlight")
                st["spotlight"] = dict(spot) if spot else None
        if not st or not st.get("text"):
            return _RichPanel("[dim]— visualizer idle —[/]",
                              title="🔍 Chapter visualizer",
                              border_style="dim blue")
        now = time.time()
        # Typing effect: reveal text progressively (~30 chars/sec).
        shown = _viz_revealed_text(st["text"], st["t0"], now)
        lines = shown.split("\n")
        view = lines[:8]
        names = sorted(set(st.get("names", ())), key=len, reverse=True)
        pat = "|".join(re.escape(nm) for nm in names) if names else None
        out = []
        for idx, ln in enumerate(view):
            ln = _rich_escape(ln[:110])
            if pat:
                ln = re.sub(r"\b(%s)\b" % pat, r"[bold yellow]\1[/]", ln,
                            flags=re.IGNORECASE)
            # Scan marker rides the last revealed line.
            marker = "[dim]▸ [/]" if idx == len(view) - 1 else "  "
            out.append(marker + ln)
        body = "[bold cyan]%s[/]\n[dim]%s[/]\n" % (
            _rich_escape(_viz_stats_line(st, now)), "─" * 40)
        spot = st.get("spotlight")
        if spot and (now - spot["t"]) < _VIZ_SPOTLIGHT_SECS:
            body += "[bold bright_green]✨ New: %s[/]\n" % _rich_escape(
                spot["name"][:50])
        body += "\n".join(out)
        events = st.get("events", [])
        if events:
            colored = []
            for e in events[-5:]:
                style = _VIZ_EVENT_STYLES.get(_viz_event_kind(e), "")
                esc = _rich_escape(e)
                colored.append("[%s]%s[/]" % (style, esc) if style else esc)
            body += "\n[dim]─[/]\n" + "\n".join(colored)
        ticker = st.get("ticker", [])
        if ticker:
            tick = " • ".join(ticker[-_VIZ_TICKER_MAX:])
            body += "\n[dim]─[/]\n[dim]%s[/]" % _rich_escape(tick[:100])
        # Live matrix snapshot (--show-matrices): top characters, spice,
        # relationship count. Rendered at the panel's 4fps refresh.
        body += _matrix_panel_block(st.get("matrix"),
                                    CONFIG.get("show_matrices", False))
        # P11: bottleneck diagnosis from chapter length vs duration.
        _lens = st.get("chapter_lens") or []
        if len(_lens) >= 3:
            _blabel, _bsugg = _detect_bottleneck(_lens)
            _bline = "[dim]Bottleneck: %s" % _rich_escape(_blabel)
            if _bsugg:
                _bline += " — [italic]%s[/]" % _rich_escape(_bsugg)
            _bline += "[/]"
            body += "\n[dim]─[/]\n" + _bline
        ci = st.get("chap_idx")
        ct = st.get("chap_total")
        if ci and ct:
            title = "🔍 Ch.%d/%d — %s" % (ci, ct, st.get("label") or "chapter")
        else:
            title = "🔍 %s" % (st.get("label") or "chapter")
        return _RichPanel(body, title=_rich_escape(title[:58]),
                          border_style="dim blue")

    def summary(self, title, rows):
        """Print a summary table. rows = [(metric, value), ...]."""
        if self.rich:
            tbl = _RichTable(title=_rich_escape(title), box=_rich_box.ROUNDED,
                             show_header=False, padding=(0, 2))
            tbl.add_column("metric", style="dim cyan")
            tbl.add_column("value", style="bold white")
            for k, v in rows:
                tbl.add_row(_rich_escape(k), _rich_escape(str(v)))
            self.console.print(tbl)
        else:
            print(f"  {title}")
            for k, v in rows:
                print(f"    {k}: {v}")

    def run_summary(self, title, stats):
        """P7: end-of-run summary panel. stats dict keys:
        principals, minors, rels_total, rels_high, rels_medium, rels_low,
        val_kept, val_downgraded, val_dropped, spice,
        wrote (dict or None), elapsed_sec, preview (bool).
        """
        s = stats or {}
        lines = []
        lines.append("Principals: %d (%d minor)" % (
            s.get("principals", 0), s.get("minors", 0)))
        lines.append("Relationships: %d (%d high, %d medium, %d low)" % (
            s.get("rels_total", 0), s.get("rels_high", 0),
            s.get("rels_medium", 0), s.get("rels_low", 0)))
        lines.append("Validation: %d kept, %d downgraded, %d dropped" % (
            s.get("val_kept", 0), s.get("val_downgraded", 0),
            s.get("val_dropped", 0)))
        lines.append("Spice: %d/5" % s.get("spice", 0))
        wrote = s.get("wrote")
        if s.get("preview"):
            lines.append("Preview mode — nothing written to DB")
        elif wrote:
            lines.append("Written to DB: %d chars, %d tropes, "
                         "%d triggers, %d quotes" % (
                             wrote.get("characters", 0),
                             wrote.get("tropes", 0),
                             wrote.get("triggers", 0),
                             wrote.get("quotes", 0)))
        else:
            lines.append("Written to DB: n/a")
        lines.append("Time: %s" % _format_duration(s.get("elapsed_sec", 0)))
        body = "\n".join(lines)
        if self.rich and self.console:
            self.console.print(_RichPanel(
                body, title=_rich_escape("Run complete: %s" % title),
                border_style="green", box=_rich_box.ROUNDED))
        else:
            print(f"  Run complete: {title}")
            for ln in lines:
                print(f"    {ln}")


UI = PipelineUI()

_DIAG = threading.local()
_PRINT_LOCK = threading.Lock()
# Set on Ctrl+C; chapter/chunk loops check between iterations and bail early.
# (Python can't forcibly kill threads, so this is cooperative.)
_CANCEL = threading.Event()


def effective_batch_size():
    """Per-book batch size: thread-local override when --jobs splits slots."""
    override = getattr(_DIAG, "batch_size_override", None)
    if override is not None:
        return max(1, override)
    return max(1, CONFIG.get("batch_size", BATCH_SIZE))


def _diag_reset():
    _DIAG.info = {"finish_reason": None, "completion_tokens": None,
                  "reasoning_content": False}


# Per-session cache for the json_schema fallback chain: None = untested,
# False = server rejected json_schema with 400 (skip it from then on).
_JSON_SCHEMA_OK = None


def _response_format_candidates():
    """Ordered response_format payloads to try; first accepted by the server wins."""
    fmt = CONFIG.get("llm_response_format", "json_object")
    if fmt == "none":
        return [None]  # explicit: no constraint at all
    cands = []
    if CONFIG.get("llm_json_schema", True) and _JSON_SCHEMA_OK is not False:
        cands.append({"type": "json_schema",
                      "json_schema": {"name": "extraction",
                                      "schema": {"type": "object"},
                                      "strict": False}})
    if fmt == "json_object":
        cands.append({"type": "json_object"})
    else:
        print(f"\n  Warning: llm_response_format={fmt!r} unknown; using json_object")
        cands.append({"type": "json_object"})
    cands.append(None)  # last resort: unconstrained
    return cands


def llm_openai_compat(prompt, chunk, i, n):
    """Generic OpenAI-compatible API (Unsloth, vLLM, llama.cpp, etc.)

    Empty-response forensics: finish_reason, completion_tokens and whether
    the server split thinking into reasoning_content are stashed in _DIAG so
    _run_task can report WHY a response came back empty. A model that spends
    its whole token budget thinking (or a json constraint fighting <think>)
    shows up here as finish_reason=length + reasoning_content=yes.
    """
    _diag_reset()
    base = CONFIG.get("openai_base_url", "http://127.0.0.1:8888/v1").rstrip("/")
    model = CONFIG.get("openai_model", "")
    max_tokens = CONFIG.get("llm_max_tokens", 8000)
    if getattr(_DIAG, "token_boost", False):
        max_tokens *= 2  # truncation retry: doubled budget, thread-local
    system_prefix = CONFIG.get("llm_system_prefix", "")
    system_prompt = f"{system_prefix}\n{prompt}" if system_prefix else prompt
    # Per-call override (v2 sets this thread-locally); falls back to global.
    think_suffix = getattr(_DIAG, "think_suffix", None)
    if think_suffix is None:
        think_suffix = CONFIG.get("llm_think_suffix", "")
    if think_suffix:
        system_prompt = f"{system_prompt}\n{think_suffix}"
    # Qwen3.5 thinking control via chat_template_kwargs (ignored if unsupported).
    enable_thinking = getattr(_DIAG, "enable_thinking", None)
    if enable_thinking is None:
        enable_thinking = CONFIG.get("llm_enable_thinking")
    base_payload = {"model": model,
                    "messages": [{"role": "system", "content": system_prompt},
                                 {"role": "user", "content": f"Excerpt {i}/{n}:\n\n{chunk}"}],
                    "temperature": 0.2,
                    "max_tokens": max_tokens}
    if enable_thinking is not None:
        base_payload["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}
    global _JSON_SCHEMA_OK
    last_err = None
    for rf in _response_format_candidates():
        payload = dict(base_payload)
        if rf is not None:
            payload["response_format"] = rf
        try:
            resp = _post_json(f"{base}/chat/completions", payload,
                              {"Content-Type": "application/json"}, 300)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:200]
            last_err = f"{e.code}: {body}"
            if e.code == 400 and rf is not None:
                if rf.get("type") == "json_schema":
                    _JSON_SCHEMA_OK = False
                print(f"\n  response_format {rf.get('type')} rejected (400); "
                      f"trying fallback", end=" ", flush=True)
                continue
            print(f"\n  OpenAI-compat {e.code}: {body}")
            return None
        except Exception as e:
            print(f"\n  OpenAI-compat error: {e}")
            return None
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = resp.get("usage") or {}
        timings = resp.get("timings") or {}
        _DIAG.info = {
            "finish_reason": choice.get("finish_reason"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_content": bool(msg.get("reasoning_content")),
            "prompt_n": timings.get("prompt_n"),
            "cache_n": timings.get("cache_n"),
        }
        if CONFIG.get("debug") and timings.get("prompt_n"):
            _p = timings.get("prompt_n") or 0
            _c = timings.get("cache_n") or 0
            print(f" (cache {_c}/{_p})", end="", flush=True)
        return msg.get("content")
    print(f"\n  OpenAI-compat: all response_format options rejected ({last_err})")
    return None


def parse_json_obj(text):
    """Pull the first JSON object out of model output (tolerates <think> blocks and fences)."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"```(?:json)?", "", text)
    # Try direct parse first (for response_format=json_object output)
    text_stripped = text.strip()
    if text_stripped.startswith("{"):
        try:
            obj = json.loads(text_stripped)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    # Fall back to scanning for JSON objects
    dec = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = dec.raw_decode(text[pos:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        pos = text.find("{", pos + 1)
    return None


def _s(x, limit=500):
    """Coerce to a clean short string, or ''."""
    if isinstance(x, (str, int, float)) and not isinstance(x, bool):
        return str(x).strip()[:limit]
    return ""


def _list(x):
    return x if isinstance(x, list) else []


# --- Evidence verification (Phase 1) ---
# Checks that model-quoted evidence/quotes actually appear in the source text.
# Small models invent plausible-sounding quotes; verification keeps the claim
# but drops the fabricated quote and flags it.
_QUOTE_NORM_TABLE = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2026": "...", "\u00a0": " ",
})

def _norm_quote(s):
    """Normalize for evidence matching: unify quotes/dashes, casefold,
    collapse whitespace, strip trailing punctuation."""
    if not isinstance(s, str):
        return ""
    s = s.translate(_QUOTE_NORM_TABLE).casefold()
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(".,;:!?\"'").strip()


def _verify_against_norm(q_norm, src_norm):
    """Core check on pre-normalized strings. Exact substring first, then a
    light fuzzy fallback: anchor on the quote's opening tokens and compare a
    local window (keeps it cheap on book-length text)."""
    if not q_norm:
        return False
    if len(q_norm) < 12:
        # Short quotes: exact normalized substring only, no fuzzy.
        return q_norm in src_norm
    if q_norm in src_norm:
        return True
    qtokens = q_norm.split()
    if len(qtokens) < 6:
        return False
    anchor = " ".join(qtokens[:4])
    start, checked = 0, 0
    while checked < 5:
        pos = src_norm.find(anchor, start)
        if pos == -1:
            break
        # Compare the quote against source windows of the same length near
        # the anchor hit; a minor transcription slip still scores >= 0.9.
        for off in range(-8, 9):
            wstart = max(0, pos + off)
            window = src_norm[wstart:wstart + len(q_norm)]
            if len(window) < len(q_norm) * 0.8:
                continue
            if difflib.SequenceMatcher(None, q_norm, window, autojunk=False).ratio() >= 0.9:
                return True
        start = pos + 1
        checked += 1
    return False


def verify_evidence(quote, source_text):
    """True if the quote appears in the source text (normalized comparison
    with a light fuzzy fallback for minor OCR/whitespace differences)."""
    return _verify_against_norm(_norm_quote(quote), _norm_quote(source_text))


def verify_all_evidence(characters, relationships, quotes, source_text):
    """Verify every evidence/quote string against the source text.

    Unverified quotes are dropped (a fabricated notable quote is worse than
    none). Character/relationship claims are kept but their evidence is
    cleared and flagged evidence_verified=false.
    Returns (characters, relationships, quotes, stats)."""
    src_norm = _norm_quote(source_text)
    stats = {"evidence_checked": 0, "evidence_verified": 0, "quotes_dropped": 0}

    def check(ev):
        # Returns (kept_text, verified, failed, offered).
        # Trivially short evidence (< 3 words, e.g. "Mother") is not
        # meaningful support: excluded from the stats so it can't inflate
        # the verified ratio, but offered=True distinguishes it from the
        # model offering nothing at all.
        offered = bool(ev and ev.strip())
        if not offered:
            return "", False, False, False
        if len(_norm_quote(ev).split()) < 3:
            return "", False, False, True
        stats["evidence_checked"] += 1
        ok = _verify_against_norm(_norm_quote(ev), src_norm)
        if ok:
            stats["evidence_verified"] += 1
            return ev, True, False, True
        return "", False, True, True  # checked and failed

    for c in characters:
        ev, ok, failed, offered = check(c.get("evidence"))
        c["evidence"] = ev
        c["evidence_verified"] = ok
        c["evidence_offered"] = offered
    for r in relationships:
        ev, ok, failed, offered = check(r.get("evidence"))
        r["evidence"] = ev
        r["evidence_verified"] = ok
        r["evidence_offered"] = offered
    kept_quotes = []
    for q in quotes:
        qtext = q.get("text", "") if isinstance(q, dict) else ""
        ev, ok, failed, offered = check(qtext)
        if failed:
            stats["quotes_dropped"] += 1
            continue
        if isinstance(q, dict):
            q["evidence_verified"] = ok
        kept_quotes.append(q)
    return characters, relationships, kept_quotes, stats


def _spread_quotes(quotes, limit=10):
    """Pick `limit` quotes evenly spaced across the book instead of the
    first N in chunk order (which biases toward the front)."""
    if len(quotes) <= limit:
        return quotes
    step = len(quotes) / limit
    return [quotes[int(i * step)] for i in range(limit)]


def sanitize(r):
    """Coerce raw model JSON into the exact shape the rest of the code expects."""
    if not isinstance(r, dict):
        return None

    tropes = []
    for t in _list(r.get("tropes")):
        name = _s(t.get("name") if isinstance(t, dict) else t, 80)
        if name:
            tropes.append(name)

    triggers = []
    for t in _list(r.get("triggers")):
        if isinstance(t, dict):
            w = _s(t.get("warning"), 120)
            if w:
                triggers.append({"warning": w, "detail": _s(t.get("detail")),
                                 "spoiler": t.get("spoiler") in (True, "true", "True")})
        else:
            w = _s(t, 120)
            if w:
                triggers.append({"warning": w, "detail": "", "spoiler": False})

    characters = []
    for c in _list(r.get("characters")):
        if isinstance(c, dict):
            name = _s(c.get("name"), 120)
            role = _s(c.get("role"), 30).lower()
            desc = _s(c.get("description"), 1000)
            ev = _s(c.get("evidence"), 500)
        else:
            name, role, desc, ev = _s(c, 120), "", "", ""
        if name:
            characters.append({"name": name,
                               "role": role if role in VALID_ROLES else None,
                               "description": desc, "evidence": ev})

    relationships = []
    for rel in _list(r.get("relationships")):
        if isinstance(rel, dict):
            a, b, ty = _s(rel.get("from"), 120), _s(rel.get("to"), 120), _s(rel.get("type"), 60)
            ev = _s(rel.get("evidence"), 500)
            if a and b and ty:
                relationships.append({"from": a, "to": b, "type": ty, "evidence": ev})

    try:
        spice = max(0, min(5, int(float(r.get("spice_level")))))
    except (TypeError, ValueError):
        spice = None  # unknown, not a measured 0 (see _sanitize_v2b)

    povs = [p for p in (_s(x, 120) for x in _list(r.get("povs"))) if p]

    quotes = []
    for q in _list(r.get("quotes")):
        if isinstance(q, dict):
            text = _s(q.get("text"), 600)
            spoiler = q.get("spoiler") in (True, "true", "True")
            speaker = _s(q.get("speaker"), 100) or None
        else:
            text, spoiler, speaker = _s(q, 600), False, None
        if text:
            quotes.append({"text": text, "speaker": speaker, "spoiler": spoiler})

    stories = [s for s in (_s(x, 200) for x in _list(r.get("stories"))) if s]

    return {"tropes": tropes, "triggers": triggers, "characters": characters,
            "relationships": relationships, "spice_level": spice, "povs": povs,
            "quotes": quotes,
            "is_anthology": r.get("is_anthology") in (True, "true", "True"),
            "stories": stories}


def _run_task(call, prompt, chunk, i, n, task_name, sanitize_fn=None):
    """Run one prompt task with retries. Returns sanitized dict or None.

    sanitize_fn defaults to the legacy sanitize(); v2 passes its own.
    If a response comes back empty with finish_reason=length, attempt 2
    retries with a doubled token budget (thread-local boost, --batch safe).
    """
    san = sanitize_fn or sanitize
    raws = []
    boosted = False
    for attempt in (1, 2):
        _DIAG.token_boost = boosted
        try:
            out = call(prompt, chunk, i, n)
        finally:
            _DIAG.token_boost = False
        result = san(parse_json_obj(out)) if out is not None else None
        if result is not None:
            return result
        if out is None:
            continue  # backend error already reported; spend next attempt
        raws.append(out)
        d = getattr(_DIAG, "info", None) or {}
        if not out and d.get("finish_reason") == "length" and not boosted and attempt == 1:
            boosted = True
            print("(truncated, retrying with 2x budget)", end=" ", flush=True)
            continue  # attempt 2 runs boosted
        if not out:
            # Empty response: report the forensics stashed by the backend.
            print(f"(empty: finish_reason={d.get('finish_reason')}, "
                  f"completion_tokens={d.get('completion_tokens')}, "
                  f"reasoning_content={'yes' if d.get('reasoning_content') else 'no'})",
                  end=" ", flush=True)
        else:
            print("(unparseable, retrying)", end=" ", flush=True)
    if raws and CONFIG.get("debug") and _DEBUG_TAG:
        dbg = SCRIPT_DIR / "preview" / "debug"
        dbg.mkdir(parents=True, exist_ok=True)
        with open(dbg / f"{_DEBUG_TAG}-chunk{i}-{task_name}.txt", "w", encoding="utf-8") as f:
            d = getattr(_DIAG, "info", None) or {}
            f.write(f"--- diag: finish_reason={d.get('finish_reason')} "
                    f"completion_tokens={d.get('completion_tokens')} "
                    f"reasoning_content={d.get('reasoning_content')} ---\n\n")
            for a, raw in enumerate(raws, 1):
                f.write(f"--- attempt {a} ({len(raw)} chars) ---\n{raw}\n\n")
        print(f"(debug saved: chunk{i}-{task_name})", end=" ", flush=True)
    return None


def extract_llm(chunk, i, n):
    """Run the discipline task (tropes/triggers/spice/quotes) and the identity
    task (characters/relationships) separately. A failed task does not
    invalidate the other. If the rich identity prompt fails twice, one attempt
    is made with the simplified no-evidence fallback prompt. Returns merged
    dict, or None if both failed."""
    backends = {"ollama": llm_ollama, "openrouter": llm_openrouter, "openai": llm_openai_compat}
    call = backends.get(CONFIG["llm"], llm_ollama)
    disc = _run_task(call, PROMPT_DISCIPLINE, chunk, i, n, "discipline")
    ident = _run_task(call, PROMPT_IDENTITY, chunk, i, n, "identity")
    ident_simple = False
    if ident is None:
        # Graceful degradation: evidence-free fallback prompt, routed through
        # _run_task for retries, debug dumps and empty-response diagnostics.
        ident = _run_task(call, PROMPT_IDENTITY_SIMPLE, chunk, i, n,
                          "identity-fallback")
        if ident is not None:
            ident_simple = True
            if CONFIG.get("debug"):
                print("(identity fallback ok)", end=" ", flush=True)
    if disc is None and ident is None:
        return None
    result = disc if disc is not None else ident
    if ident is not None:
        result["characters"] = ident["characters"]
        result["relationships"] = ident["relationships"]
    result["task_status"] = {"discipline": "ok" if disc is not None else "failed",
                             "identity": "ok" if ident is not None else "failed",
                             "identity_simple": ident_simple}
    # Phase 1b: verify evidence against THIS chunk's text, before aggregation.
    # Fabricated quotes are dropped here so the [:3]/[:10] cuts below only
    # ever see verified quotes.
    v_chars, v_rels, v_quotes, vstats = verify_all_evidence(
        result["characters"], result["relationships"], result["quotes"], chunk)
    result["characters"], result["relationships"], result["quotes"] = \
        v_chars, v_rels, v_quotes
    result["verification"] = vstats
    return result


# --- Supabase REST ---
def _cosine(a, b):
    """Cosine similarity. For repeated comparisons, normalize once with
    _normalize() and use _dot() instead — this recomputes norms every call."""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _normalize(v):
    import math
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


# The text shape embedded for trope catalog entries ("name: description").
# Part of the trope-vector cache namespace: bump this whenever the
# embedded text format changes — vectors produced from a different text
# shape (or a different model) live in a different space and must never
# be mixed with current ones.
EMBED_TEXT_FORMAT = "name-desc-v1"


def trope_catalog_vectors():
    """Load trope catalog with embeddings. Fetches from Supabase `tropes`,
    embeds name+description via the dedupe embedding endpoint, caches to
    CONFIG['trope_map_cache']. Returns [(id, name, vector)].

    The cache file is namespaced by (embed_model, EMBED_TEXT_FORMAT) —
    {"model": ..., "format": ..., "vectors": {id: vector}}. A cache
    written under any other model or text format, including the legacy
    bare {id: vector} shape, is invalidated wholesale and re-embedded:
    cosine comparisons across different vector spaces are meaningless.
    """
    import os
    cache_path = CONFIG.get("trope_map_cache", "trope-vectors.json")
    model = CONFIG.get("embed_model", "")
    cached = {}
    if os.path.exists(cache_path):
        try:
            raw_cache = json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            raw_cache = None
        if (isinstance(raw_cache, dict)
                and raw_cache.get("model") == model
                and raw_cache.get("format") == EMBED_TEXT_FORMAT
                and isinstance(raw_cache.get("vectors"), dict)):
            cached = raw_cache["vectors"]
        else:
            print("  trope vector cache invalidated (model/format "
                  "changed); re-embedding catalog")
    rows = []
    if CONFIG.get("supabase_url"):
        rows = sb("tropes", params="?select=id,name,description&order=id")
    if not rows:
        if not CONFIG.get("supabase_url"):
            print("  (trope catalog mapping skipped: no Supabase credentials)")
        else:
            print("  WARNING: could not load tropes from Supabase")
        return []
    # Embed any catalog entries missing from cache.
    missing = [r for r in rows if r["id"] not in cached]
    if missing:
        texts = [f"{r['name']}: {r.get('description') or ''}" for r in missing]
        vecs = embed_vectors(CONFIG["embed_url"], model, texts)
        for r, v in zip(missing, vecs):
            cached[r["id"]] = v
        try:
            json.dump({"model": model, "format": EMBED_TEXT_FORMAT,
                       "vectors": cached},
                      open(cache_path, "w", encoding="utf-8"))
        except Exception as e:
            print(f"  warning: could not write vector cache: {e}")
    # Return normalized vectors so callers can use _dot() for fast comparison.
    return [(r["id"], r["name"], _normalize(cached[r["id"]]))
            for r in rows if r["id"] in cached]


# Names that are known noise (hallucinations, family generics). "jason" is a
# Fourth Wing-specific model hallucination. Keep this short — if it grows
# into a per-book list, it should become a config file instead.
VERIFY_NOISE_NAMES = {"dad", "mom", "father", "mother", "jason"}


def _verify_flags(preview_chars, db_chars, db_tropes, db_triggers, preview_tropes, preview_triggers):
    """Pure comparison logic for --verify. All args are plain dicts/lists.
    Returns [(flag_name, description, [items...]), ...]. No I/O, no DB."""
    import re
    flags = []

    pv_names = {(c.get("name") or "").strip().lower(): c for c in preview_chars}
    db_names = {(r.get("name") or "").strip().lower(): r for r in db_chars}
    # Drop empty keys from NULL/blank names
    pv_names.pop("", None)
    db_names.pop("", None)

    new_chars = sorted(pv_names[n]["name"] for n in set(pv_names) - set(db_names))
    missing = sorted(db_names[n]["name"] for n in set(db_names) - set(pv_names))
    if new_chars:
        flags.append(("NEW CHARACTERS", f"{len(new_chars)} in preview, not in DB",
                      new_chars, len(new_chars) > 15))
    if missing:
        real_missing = [m for m in missing if m.lower() not in VERIFY_NOISE_NAMES]
        gone_noise = [m for m in missing if m.lower() in VERIFY_NOISE_NAMES]
        if real_missing:
            flags.append(("MISSING CHARACTERS",
                          f"{len(real_missing)} in DB, not in preview (possible misses)",
                          real_missing, len(real_missing) > 15))
        if gone_noise:
            flags.append(("DROPPED NOISE",
                          f"{len(gone_noise)} noisy names correctly absent from preview",
                          gone_noise, False))

    null_vit = [r["name"] for n, r in db_names.items() if not r.get("vitality")]
    if null_vit:
        flags.append(("VITALITY BACKFILL",
                      f"{len(null_vit)} DB characters have no vitality value",
                      sorted(null_vit)[:10], len(null_vit) > 10))

    _creature_re = re.compile(r"\b(rider|wyvern|daggertail|dragon)\b", re.IGNORECASE)
    _suspicious = []
    for n, c in pv_names.items():
        cn = c.get("name", "")
        if _creature_re.search(cn) and len(cn.split()) <= 3:
            _suspicious.append(f"{cn} (creature type?)")
        elif "'s " in cn or " husband" in cn.lower() or " wife" in cn.lower():
            _suspicious.append(f"{cn} (awkward name)")
    if _suspicious:
        flags.append(("SUSPICIOUS NAMES", f"{len(_suspicious)} preview names look noisy",
                      _suspicious[:10], len(_suspicious) > 10))

    # Duplicates keyed on trope_id alone (matches write-path dedup invariant)
    _seen_tid = set()
    dupes = []
    for r in db_tropes:
        tid = r.get("trope_id")
        if tid in _seen_tid:
            dupes.append(f"{str(tid)[:8]} (status={r.get('status')})")
        _seen_tid.add(tid)
    if dupes:
        flags.append(("DUPLICATE TROPE CLAIMS",
                      f"{len(dupes)} duplicate trope_ids in DB",
                      dupes[:10], len(dupes) > 10))

    if preview_tropes:
        flags.append(("PREVIEW TROPES", f"{len(preview_tropes)} tropes in preview",
                      sorted(preview_tropes)[:15], len(preview_tropes) > 15))

    rejected = {w for w, s in db_triggers.items() if s == "rejected"}
    overlap = sorted(set(preview_triggers) & rejected)
    if overlap:
        flags.append(("TRIGGERS ALREADY REJECTED",
                      f"{len(overlap)} preview triggers are rejected in DB (won't re-add)",
                      overlap[:10], len(overlap) > 10))
    return flags


def cmd_verify(preview_path):
    """Compare a preview JSON against current DB state and flag discrepancies.
    Read-only: no writes, no LLM calls. Use before --push-preview to review
    what would change.
    """
    if not os.path.isfile(preview_path):
        print(f"  Not found: {preview_path}")
        return
    result = json.load(open(preview_path, encoding="utf-8"))
    title = result.get("title") or os.path.basename(preview_path)
    identifiers = {}
    if result.get("isbn"):
        identifiers["isbn"] = result["isbn"]
    if result.get("asin"):
        identifiers["asin"] = result["asin"]
    wid, how = resolve_work(title, result.get("author"), identifiers, False,
                            create=False)
    if how == "db_error":
        print(f"  DB lookup failed — cannot verify '{title}'")
        return
    if not wid:
        print(f"  No existing work found for '{title}' ({how}) — nothing to compare")
        return
    print(f"  Verifying '{title}' against work {wid[:8]} ({how})")

    _raw_chars = sb("book_characters",
                    params=f"?work_id=eq.{wid}&select=name,role,vitality,status&limit=500")
    _raw_tropes = sb("book_trope_claims",
                     params=f"?work_id=eq.{wid}&select=trope_id,status&limit=200")
    _raw_trig = sb("book_trigger_claims",
                   params=f"?work_id=eq.{wid}&select=warning,status&limit=100")
    if _raw_chars is None or _raw_tropes is None or _raw_trig is None:
        # sb() returns None on error: a failed read is a DB failure,
        # not an empty DB — reporting "everything differs" (or
        # "nothing to compare") off a failed read would be fiction.
        _failed = [t for t, r in (("book_characters", _raw_chars),
                                  ("book_trope_claims", _raw_tropes),
                                  ("book_trigger_claims", _raw_trig))
                   if r is None]
        print(f"  DB read failed for {', '.join(_failed)} — cannot "
              f"verify '{title}' (a failed read is not an empty DB)")
        return
    db_chars = _raw_chars
    db_tropes = _raw_tropes
    db_trig = {r["warning"]: r["status"] for r in _raw_trig}
    pv_trope_names = {t.get("name", "").lower() if isinstance(t, dict) else str(t).lower()
                      for t in result.get("tropes", [])}
    pv_trig = {t.get("warning") if isinstance(t, dict) else t
               for t in result.get("triggers", [])}

    flags = _verify_flags(result.get("characters", []), db_chars, db_tropes,
                          db_trig, pv_trope_names, pv_trig)

    if not flags:
        print("  No flags — preview and DB are consistent")
        return
    rejected_n = sum(1 for s in db_trig.values() if s == "rejected")
    UI.summary(f"Verification: {title}", [
        ("Flags", len(flags)),
        ("DB characters", len(db_chars)),
        ("Preview characters", len(result.get("characters", []))),
        ("DB trope claims", len(db_tropes)),
        ("DB triggers", f"{len(db_trig)} ({rejected_n} rejected)"),
    ])
    for fname, fdesc, items, has_more in flags:
        print(f"\n  ⚠ {fname}: {fdesc}")
        for it in items:
            print(f"      - {it}")
        if has_more:
            print(f"      ... and more")


def _preview_shape_problems(result):
    """Structural check of a preview dict against what the write path
    direct-indexes. Returns a list of human-readable problems
    ([] = pushable).

    write_claims' builders index result["tropes"/"triggers"/
    "relationships"/"characters"] and per-item c["name"]/c["role"],
    r["from"]/r["to"]/r["type"], t["warning"] (dict triggers) without
    guards, and sections replace sequentially — so a shape failure
    discovered mid-push lands AFTER earlier sections' delete+insert:
    a partial replace with a traceback. Validating up front turns
    that into a clean refusal with zero writes. Only the fields the
    builders actually require are demanded; everything else stays
    optional (quotes included — the write path reads them via .get).
    """
    problems = []
    for sec in ("tropes", "triggers", "relationships", "characters"):
        v = result.get(sec)
        if v is None:
            problems.append(f"missing '{sec}' section")
        elif not isinstance(v, list):
            problems.append(f"'{sec}' section is not a list")

    def _items(sec):
        v = result.get(sec)
        return v if isinstance(v, list) else []

    for i, t in enumerate(_items("tropes")):
        if not isinstance(t, str):
            problems.append(f"tropes[{i}] is not a string")
    for i, t in enumerate(_items("triggers")):
        if isinstance(t, dict):
            if "warning" not in t:
                problems.append(f"triggers[{i}] missing 'warning'")
        elif not isinstance(t, str):
            problems.append(f"triggers[{i}] is not an object or string")
    for i, r in enumerate(_items("relationships")):
        if not isinstance(r, dict):
            problems.append(f"relationships[{i}] is not an object")
            continue
        for k in ("from", "to", "type"):
            if k not in r:
                problems.append(f"relationships[{i}] missing '{k}'")
            elif not isinstance(r[k], str):
                problems.append(f"relationships[{i}] '{k}' is not a string")
    for i, c in enumerate(_items("characters")):
        if not isinstance(c, dict):
            problems.append(f"characters[{i}] is not an object")
            continue
        for k in ("name", "role"):
            if k not in c:
                problems.append(f"characters[{i}] missing '{k}'")
        if "name" in c and not isinstance(c["name"], str):
            problems.append(f"characters[{i}] 'name' is not a string")
    for i, q in enumerate(_items("quotes")):
        if not isinstance(q, dict):
            problems.append(f"quotes[{i}] is not an object")
    return problems


def cmd_push_preview(preview_path):
    """Push a preview JSON's data to Supabase without re-running the LLM.
    Resolves the work, then runs write_claims. Use for retrying failed writes.
    Accepts a single file or a directory (all .json files except trope-maps)."""
    import os
    import glob as _glob
    if not (CONFIG.get("supabase_url") and CONFIG.get("supabase_key")):
        print("  ERROR: supabase_url and supabase_key must be set in config.json")
        return
    if os.path.isdir(preview_path):
        files = sorted(_glob.glob(os.path.join(preview_path, "*.json")))
        files = [f for f in files if "trope-map" not in os.path.basename(f)]
        print(f"  Pushing {len(files)} preview files...")
        for f in files:
            try:
                cmd_push_preview(f)
            except Exception as e:
                print(f"  FAILED {os.path.basename(f)}: {type(e).__name__}: {e}")
        return
    if not os.path.isfile(preview_path):
        print(f"  Not found: {preview_path}")
        return
    result = json.load(open(preview_path, encoding="utf-8"))
    if not isinstance(result, dict):
        print(f"  Refusing to push {preview_path}: preview is not a JSON "
              f"object — no writes made")
        return
    title = result.get("title") or os.path.basename(preview_path)
    if result.get("aborted"):
        print(f"  Skipping {title}: preview is marked ABORTED (partial results)")
        return
    if result.get("chapter_detection") == "fallback":
        print(f"  Skipping {title}: chapter detection used FALLBACK splitting "
              f"(unreliable chapters) — re-run from a clean EPUB")
        return
    # Shape gate: the write builders direct-index sections and item
    # keys, and sections replace sequentially — a malformed preview
    # must be refused HERE, before any work resolution or write, not
    # discovered mid-push after earlier sections already replaced.
    problems = _preview_shape_problems(result)
    if problems:
        print(f"  Refusing to push {title}: preview is missing data the "
              f"write path requires (older or partial preview) — "
              f"no writes made:")
        for p in problems[:10]:
            print(f"    - {p}")
        if len(problems) > 10:
            print(f"    - ... and {len(problems) - 10} more")
        print("  Re-run the book to regenerate a complete preview.")
        return
    print(f"  Pushing: {title}")
    # Resolve work (create if needed).
    identifiers = {}
    if result.get("isbn"):
        identifiers["isbn"] = result["isbn"]
    if result.get("asin"):
        identifiers["asin"] = result["asin"]
    title, _author = correct_metadata(title, result.get("author"), identifiers)[:2]
    result["author"] = _author
    # Check first without creating: confirm before creating a new work row.
    wid, how = resolve_work(title, result.get("author"), identifiers, False,
                            create=False)
    if how == "new":
        try:
            ans = input(f"  No existing work for '{title}'. Create new work? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans != "y":
            print("  Skipped (new work not confirmed)")
            return
        wid, how = resolve_work(title, result.get("author"), identifiers, False,
                                create=True)
    if not wid:
        print(f"  Could not resolve work ({how})")
        return
    print(f"  Work ID: {wid} ({how})")
    # The only backfills: both are semantically safe when absent —
    # an empty catalog map just means per-trope DB lookups, an empty
    # confidence map means the builders' documented defaults. Every
    # other required field is validated, never fabricated (see
    # _preview_shape_problems above).
    result.setdefault("trope_catalog_map", {})
    result.setdefault("trope_confidence", {})
    errors = write_claims(wid, result)
    if errors:
        print(f"  {errors} database operation(s) failed")
    else:
        print(f"  Push complete, no errors")


def cmd_retry_failed(preview_path):
    """P9: re-run only the failed chapters from a preview JSON.

    Reads chapter_data from the preview, identifies chapters where Call A
    (identity) or Call B (content) failed, seeds a checkpoint dir with the
    successful chapters, then runs process_file_v2 in resume mode. The merged
    result is saved as {book}_retried.json; the original is untouched.
    Always runs in preview mode (no Supabase writes).
    """
    import os
    if not os.path.isfile(preview_path):
        print(f"  Not found: {preview_path}")
        return
    result = json.load(open(preview_path, encoding="utf-8"))
    cdata = result.get("chapter_data") or {}
    chapters = cdata.get("chapters") or {}
    if not chapters:
        print(f"  No chapter_data in {preview_path} "
              f"(needs a preview from the checkpoint-enabled pipeline)")
        return
    failed = sorted(int(k) for k, v in chapters.items()
                    if v.get("a") is None or v.get("b") is None)
    if not failed:
        print(f"  No failed chapters to retry in {preview_path}")
        return
    print(f"  Retrying {len(failed)} failed chapters: {failed}")
    # Locate the book file.
    fname = result.get("file", "")
    fpath = None
    for cand in (Path(fname), IMPORT_DIR / fname):
        if cand.is_file():
            fpath = cand
            break
    if fpath is None:
        print(f"  Could not find book file '{fname}' "
              f"(checked cwd and {IMPORT_DIR})")
        return
    # Seed checkpoints from the successful chapters so --resume skips them.
    book_key = _checkpoint_book_key(fpath)
    _checkpoint_clear(book_key)  # start clean
    n_total = len(chapters)
    _checkpoint_save_meta(book_key, n_total)
    roster = cdata.get("roster") or {}
    # The preview may predate core roster fields (see
    # _ROSTER_CORE_DEFAULTS): complete the seed with the same census
    # the checkpoint load applies, so the seeded roster.json is
    # well-formed before resume ever reads it.
    if isinstance(roster, dict):
        for _entry in roster.values():
            _roster_entry_complete(_entry)
    _checkpoint_save_roster(book_key, roster)
    for k, v in chapters.items():
        idx = int(k)
        if idx not in failed:
            _checkpoint_save_chapter(book_key, idx, v.get("a"), v.get("b"))
        elif v.get("a") is not None:
            # Call A succeeded but Call B failed. The roster seeded
            # above is the FINAL roster — it already contains this
            # chapter's Call A fold, and _roster_update is explicitly
            # non-idempotent. Seed the saved A (with b=None) so the
            # resume guard in process_file_v2 reuses it instead of
            # re-running Call A and double-counting the chapter's
            # appearances in the retried preview.
            _checkpoint_save_chapter(book_key, idx, v.get("a"), None)
    # Resume mode + retried output name. Force preview (no DB writes).
    CONFIG["resume"] = True
    CONFIG["preview_name_suffix"] = "_retried"
    # Bypass the interactive flags-hash prompt: the checkpoint was just
    # seeded from this exact preview, so flags necessarily match.
    CONFIG["resume_force"] = True
    try:
        process_file_v2(fpath, dry_run=False, preview=True)
    finally:
        CONFIG.pop("resume_force", None)
        CONFIG.pop("preview_name_suffix", None)
    print(f"  Retried preview saved (original untouched)")


def cmd_validate(preview_path):
    """Run automated quality checks on a preview JSON or a directory of them.
    Prints a report per file, plus a summary table for directories."""
    import os
    import glob as _glob
    if os.path.isdir(preview_path):
        files = sorted(_glob.glob(os.path.join(preview_path, "*.json")))
        # Skip trope-map review files.
        files = [f for f in files if "trope-map" not in os.path.basename(f)]
        if not files:
            print(f"  No preview JSON files in {preview_path}")
            return
        print(f"  Validating {len(files)} preview files...\n")
        summary = []
        for f in files:
            issues, notes = _validate_one(f, quiet=True)
            status = f"{len(issues)} issues" if issues else "clean"
            summary.append((os.path.basename(f)[:50], status, len(issues)))
            print(f"  {os.path.basename(f)[:60]:62} {status}")
        print(f"\n  {sum(1 for _, _, n in summary if n == 0)}/{len(summary)} clean")
        return
    issues, notes = _validate_one(preview_path, quiet=False)


def _validate_one(preview_path, quiet=False):
    """Run checks on one file. Returns (issues, notes). Prints unless quiet."""
    import os
    from collections import Counter
    d = json.load(open(preview_path, encoding="utf-8"))
    issues, notes = [], []
    chars = d.get("characters", [])
    tropes = d.get("tropes", [])
    triggers = d.get("triggers", [])
    ver = d.get("verification", {})

    # --- Bad merge detection: A's aliases contain B's canonical name ---
    names = {c["name"].lower(): c["name"] for c in chars}
    for c in chars:
        for alias in c.get("aliases", []):
            al = alias.lower()
            if al in names and names[al] != c["name"]:
                issues.append(f"BAD MERGE: '{c['name']}' claims alias '{alias}' "
                              f"which is also a standalone character")

    # --- Suspicious shared surnames (hallucinated last names) ---
    surnames = Counter()
    for c in chars:
        parts = c["name"].split()
        if len(parts) >= 2:
            surnames[parts[-1].lower()] += 1
    for surname, count in surnames.most_common():
        if count >= 3 and len(surname) > 3:
            # Check if it's a known family name (shared legitimately)
            issues.append(f"SUSPICIOUS SURNAME: '{surname}' on {count} characters "
                          f"(possible hallucinated last name)")

    # --- Generic/noise characters ---
    generic = [c["name"] for c in chars
               if not _is_proper(c["name"]) or c.get("appearances", 0) <= 1]
    if generic:
        notes.append(f"{len(generic)} generic/low-appearance characters: "
                     f"{', '.join(generic[:8])}{'...' if len(generic) > 8 else ''}")

    # --- POVs ---
    povs = d.get("povs", [])
    notes.append(f"POVs ({len(povs)}): {', '.join(povs) if povs else 'none'}")

    # --- Tropes ---
    notes.append(f"Tropes ({len(tropes)}): {', '.join(tropes[:10])}"
                 f"{'...' if len(tropes) > 10 else ''}")
    long_tropes = [t for t in tropes if len(t.split()) > 4]
    if long_tropes:
        issues.append(f"PLOT-SUMMARY TROPES (not reusable patterns): {long_tropes[:5]}")

    # --- Verification rates ---
    checked = ver.get("evidence_checked", 0)
    verified = ver.get("evidence_verified", 0)
    rate = f"{100*verified//checked}%" if checked else "n/a"
    notes.append(f"Evidence: {verified}/{checked} verified ({rate})")
    if checked and verified / checked < 0.4:
        issues.append(f"LOW VERIFICATION RATE ({rate}) — model may be paraphrasing")

    # --- Triggers ---
    trig_list = triggers if isinstance(triggers, list) else []
    no_evidence = [t.get("warning") for t in trig_list
                   if isinstance(t, dict) and t.get("severity") in ("on_page", "graphic")
                   and not t.get("evidence")]
    if no_evidence:
        issues.append(f"TRIGGERS WITHOUT EVIDENCE: {no_evidence[:5]}")

    # --- Spice ---
    notes.append(f"Spice: {d.get('spice_level', 'n/a')}/5")

    # --- Report ---
    if not quiet:
        print(f"\n=== Validation: {os.path.basename(preview_path)} ===")
        print(f"Characters: {len(chars)}, Tropes: {len(tropes)}, "
              f"Triggers: {len(trig_list)}")
        if issues:
            print(f"\nISSUES ({len(issues)}):")
            for i in issues:
                print(f"  ! {i}")
        else:
            print("\nNo issues detected.")
        if notes:
            print(f"\nNOTES:")
            for n_ in notes:
                print(f"  - {n_}")
        print()
    return issues, notes


def cmd_trope_map(preview_path):
    """Map a preview JSON's tropes to the catalog. Writes a review JSON;
    nothing is written to Supabase.

    Uses saved trope_candidates when present (includes unmapped ones dropped
    during tiering), falling back to final tropes for older previews."""
    import os
    d = json.load(open(preview_path, encoding="utf-8"))
    # Prefer full candidate list (enables remapping previously-unmapped).
    candidates = d.get("trope_candidates") or []
    counts = d.get("trope_candidate_counts") or {}
    if candidates:
        tropes = candidates
        print(f"  Using {len(tropes)} saved candidates (incl. unmapped)...")
    else:
        tropes = d.get("tropes") or []
        if not tropes:
            print("  No tropes in preview file.")
            return
    print(f"  Loading catalog vectors...")
    catalog = trope_catalog_vectors()
    if not catalog:
        return
    print(f"  {len(catalog)} catalog tropes, {len(tropes)} to map...")
    # Embed the pipeline tropes.
    trope_vecs = embed_vectors(CONFIG["embed_url"], CONFIG.get("embed_model", ""),
                               tropes)
    trope_vecs = [_normalize(v) for v in trope_vecs]
    threshold = CONFIG.get("trope_map_threshold", 0.78)
    mappings, proposals = [], []
    for name, vec in zip(tropes, trope_vecs):
        best, best_sim = None, -1
        for cid, cname, cvec in catalog:
            sim = _dot(vec, cvec)
            if sim > best_sim:
                best, best_sim = (cid, cname), sim
        entry = {"pipeline_trope": name,
                 "chapter_count": counts.get(_tnorm(name), 1)}
        if best_sim >= threshold:
            entry.update({"catalog_id": best[0], "catalog_name": best[1],
                          "similarity": round(best_sim, 3)})
            mappings.append(entry)
        else:
            entry.update({"nearest_catalog_id": best[0] if best else None,
                          "nearest_similarity": round(best_sim, 3) if best else None,
                          "book": d.get("title"), "isbn": d.get("isbn")})
            proposals.append(entry)
    out = {"preview": os.path.basename(preview_path),
           "threshold": threshold,
           "mappings": mappings, "proposals": proposals}
    out_path = os.path.splitext(preview_path)[0] + "-trope-map.json"
    json.dump(out, open(out_path, "w", encoding="utf-8"), indent=2)
    print(f"  {len(mappings)} mapped, {len(proposals)} proposals -> {out_path}")


def sb(table, method="GET", data=None, params="", prefer=None):
    """Returns a list on success (possibly empty) or None on any error."""
    url = f"{CONFIG['supabase_url']}/rest/v1/{table}{params}"
    _prefer = "return=representation"
    if prefer:
        _prefer = f"{_prefer},{prefer}"
    headers = {
        "apikey": CONFIG["supabase_key"],
        "Authorization": f"Bearer {CONFIG['supabase_key']}",
        "Content-Type": "application/json",
        "Prefer": _prefer,
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8") or "[]")
    except urllib.error.HTTPError as e:
        print(f"  Supabase {table} {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")
    except Exception as e:
        print(f"  Supabase {table} error: {e}")
    return None


def norm(s):
    """Lowercase, strip accents and punctuation, collapse whitespace to single
    spaces. Matches the app's tropeNormIdent: "Fourth Wing" -> "fourth wing"."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\W_]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def norm_name(n):
    """Deterministic character-name key: casefold, drop leading article, tidy spaces."""
    n = (n or "").strip().casefold()
    n = re.sub(r"^(the|a|an)\s+", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# Common words that are never distinctive surnames.
_NAME_STOPWORDS = frozenset({
    "name", "names", "man", "woman", "boy", "girl", "child", "kid",
    "person", "people", "guy", "lady", "lord", "sir", "mr", "mrs",
    "ms", "dr", "professor", "general", "colonel", "major", "captain",
    "commander", "king", "queen", "prince", "princess", "number",
})

_TITLES = {"mr", "mrs", "ms", "miss", "dr", "professor", "general", "colonel",
          "major", "captain", "commander", "lieutenant", "sergeant", "lord",
          "lady", "sir", "king", "queen", "prince", "princess", "master"}


def _names_overlap(a, b):
    """True if two normalized names likely refer to the same person.
    Matches: exact, one is a word-prefix of the other ("bodhi" vs "bodhi durran"),
    or a bare surname matches a titled/full name's last word ("melgren" in
    "general melgren" vs "augustine melgren"). Two full names sharing only a
    surname ("brennan sorrengail" vs "violet sorrengail") do NOT match —
    those are different people (siblings)."""
    if not a or not b or a == b:
        return a == b
    aw, bw = a.split(), b.split()
    # One is a word-prefix of the other (first name vs full name)
    if aw == bw[:len(aw)] or bw == aw[:len(bw)]:
        return True
    # Bare surname vs titled/full name: strip titles, then the single remaining
    # word must equal the longer name's last word. Two multi-word names sharing
    # only a surname are siblings, not the same person.
    _et = _effective_titles()
    sa = [w for w in aw if w.rstrip(".") not in _et]
    sb_ = [w for w in bw if w.rstrip(".") not in _et]
    short, long_ = sorted((sa, sb_), key=len)
    return (len(short) == 1 and len(long_) > 1 and short[0] == long_[-1]
            and len(short[0]) > 3 and short[0] not in _NAME_STOPWORDS)


# ---------------------------------------------------------------------------
# Hybrid character merge: deterministic tiers + decision-model fallback.
#
# Tier 1 (hard NO): the two names were listed as separate characters in the
#   same chapter -> different people, never merge.
# Tier 2 (deterministic YES): title/contextual-prefix stripping, exact match,
#   spelling variants, known nicknames, unambiguous multi-token subsequence.
#   Single-token-to-multi-token is NEVER automatic (the surname-family trap).
# Tier 3 (ambiguous): deferred to post_pass_merge(), which asks the decision
#   model with both characters' descriptions as evidence.
# ---------------------------------------------------------------------------

# Contextual prefixes that don't change identity ("the late Matthew Sobol"
# is still Matthew Sobol).
_CONTEXTUAL_PREFIXES = frozenset({"the late", "late", "official"})

# Professional titles/ranks beyond _TITLES, stripped before merge comparison.
# Only leading tokens are stripped, and only when the remainder still matches.
_PROFESSIONAL_TITLES = frozenset({
    "agent", "special agent", "detective", "officer", "deputy", "sheriff",
    "chief", "director", "commissioner", "inspector", "constable", "marshal",
    "president", "vice president", "senator", "congressman", "mayor",
    "governor", "judge", "justice", "attorney", "prosecutor", "ambassador",
    "secretary", "minister",
    "private", "corporal", "staff sergeant", "master sergeant",
    "warrant officer", "ensign", "admiral",
    "doctor", "nurse", "surgeon", "dean",
    "herr", "frau", "oberstleutnant", "oberst", "hauptmann", "leutnant",
    "father", "reverend", "pastor", "rabbi", "bishop", "priest",
})

# Well-established nickname -> full first name. Used only when surnames match
# exactly, so a wrong entry can't merge strangers.
_COMMON_NICKNAMES = {
    "pete": "peter", "mike": "michael", "jim": "james", "jimmy": "james",
    "bob": "robert", "bobby": "robert", "rob": "robert",
    "bill": "william", "billy": "william", "will": "william", "liam": "william",
    "dave": "david", "steve": "steven", "dan": "daniel", "danny": "daniel",
    "matt": "matthew", "chris": "christopher",
    "nick": "nicholas", "alex": "alexander",
    "ben": "benjamin", "charlie": "charles", "chuck": "charles",
    "chaz": "charles", "tom": "thomas", "tommy": "thomas",
    "rick": "richard", "ricky": "richard", "joe": "joseph", "joey": "joseph",
    "sam": "samuel", "tony": "anthony", "jack": "john", "johnny": "john",
    "josh": "joshua", "greg": "gregory", "jeff": "jeffrey",
    "ken": "kenneth", "larry": "lawrence", "ron": "ronald",
    "phil": "philip", "tim": "timothy", "ed": "edward", "eddie": "edward",
    "ted": "theodore", "liz": "elizabeth", "beth": "elizabeth",
    "betty": "elizabeth", "kate": "katherine", "katie": "katherine",
    "jen": "jennifer", "jenny": "jennifer", "jess": "jessica",
    "becky": "rebecca", "sue": "susan", "nat": "natalie",
}

# Decision-model merge threshold (Tier 3). Deliberately high: a false merge
# corrupts the roster permanently, a missed merge just leaves a duplicate.
_MERGE_P_YES_THRESHOLD = 0.85
# Safety cap on Tier 3 decision-model calls per book.
_MERGE_TIER3_CAP = 50
# Decision-model relationship validation threshold (family types only).
# Lower than the merge threshold: a wrong relationship type is embarrassing
# but doesn't corrupt the roster permanently.
_REL_VALIDATION_THRESHOLD = 0.70
# Decision-model relationship DROP threshold (family types only).
# Three-tier logic: P >= 0.70 keeps the type; 0.30 <= P < 0.70 downgrades
# to "other" (relationship exists, type uncertain); P < 0.30 drops the
# relationship entirely (likely hallucinated, not just mistyped).
_REL_DROP_THRESHOLD = 0.30
# Family relationship types validated by the decision model. High-stakes:
# being wrong about spouse/parent/child/sibling is worse than being wrong
# about friend/enemy/colleague.
_REL_FAMILY_TYPES = frozenset({"spouse", "parent", "child", "sibling"})

# Shared decision-model call budget per book (merges + relationship
# validation draw from the same pool; merges run first so they get priority).
_DM_BUDGET_LOCK = threading.Lock()
_DM_BUDGET_USED = 0


def _dm_budget_reset():
    """Reset the per-book decision-model call budget. Call at book start."""
    global _DM_BUDGET_USED
    with _DM_BUDGET_LOCK:
        _DM_BUDGET_USED = 0


def _dm_budget_claim(n=1):
    """Claim n decision-model calls from the shared per-book budget.
    Returns True if claimed, False if the budget is exhausted."""
    global _DM_BUDGET_USED
    with _DM_BUDGET_LOCK:
        if _DM_BUDGET_USED + n > _MERGE_TIER3_CAP:
            return False
        _DM_BUDGET_USED += n
        return True


def _strip_merge_noise(nkey):
    """Strip titles, contextual prefixes, and middle initials from a
    normalized name key. Returns a token list for merge comparison."""
    toks = (nkey or "").split()
    # Leading contextual prefixes ("the late", "official").
    for pref in ("the late", "late", "official"):
        pp = pref.split()
        if toks[:len(pp)] == pp:
            toks = toks[len(pp):]
            break
    # Leading titles (longest match first for multi-word titles like
    # "special agent"). Repeats: "special agent" then "agent" can't stack.
    titles = _effective_titles() | _PROFESSIONAL_TITLES
    changed = True
    while changed and toks:
        changed = False
        for t in sorted(titles, key=len, reverse=True):
            tp = t.split()
            if toks[:len(tp)] == tp:
                toks = toks[len(tp):]
                changed = True
                break
    # Middle initials ("matthew a. sobol" -> ["matthew", "sobol"]). Never
    # strip the only token.
    if len(toks) > 1:
        toks = [t for t in toks
                if not (len(t.rstrip(".")) == 1 and t.rstrip(".").isalpha())]
    return toks


def _levenshtein(a, b):
    """Edit distance (iterative, O(min(m,n)) space)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_spelling_variant(ta, tb):
    """Conservative spelling-variant check for two tokens (e.g. 'joseph' vs
    'josef', 'mosely' vs 'mosley'). Requires a shared 3+ char prefix and
    edit distance <= 2. Never a hard-NO signal on its own."""
    if ta == tb:
        return True
    if abs(len(ta) - len(tb)) > 1:
        return False
    if min(len(ta), len(tb)) < 4:
        return False
    _pre = 0
    for ca, cb in zip(ta, tb):
        if ca != cb:
            break
        _pre += 1
    if _pre < 3:
        return False
    return _levenshtein(ta, tb) <= 2


def _is_contig_subseq(short, long_):
    """True if short is a contiguous subsequence of long_ (token lists)."""
    n = len(short)
    if n > len(long_):
        return False
    return any(long_[i:i + n] == short for i in range(len(long_) - n + 1))


def _merge_subseq_unambiguous(short, a_key, b_key, roster_keys):
    """True if no OTHER roster key contains the token subsequence short.
    Returns False when roster_keys is unavailable (can't verify)."""
    if not roster_keys or len(short) < 2:
        return False
    for rk in roster_keys:
        if rk == a_key or rk == b_key:
            continue
        if _is_contig_subseq(short, _strip_merge_noise(rk)):
            return False
    return True


def _nick_pair(fa, fb):
    """True if fa/fb are a known nickname pair (either direction)."""
    return fa != fb and (_COMMON_NICKNAMES.get(fa) == fb or
                         _COMMON_NICKNAMES.get(fb) == fa)


def _merge_tier(a_key, b_key, chapter_keys=None, a_chapters=None,
                b_chapters=None, roster_keys=None):
    """Tiered merge decision between two normalized name keys.

    Returns 'yes' (Tier 2 deterministic), 'no' (Tier 1 hard cannot-link),
    or 'ambiguous' (defer to Tier 3 decision model).

    Tier 2 is checked BEFORE Tier 1: when stripping makes the names
    identical (title/spelling/nickname), that's Call A inconsistency, not
    two people -- even in the same chapter.

    chapter_keys: nkeys listed as separate characters in the current chapter
        (Tier 1 during _roster_update).
    a_chapters/b_chapters: chapter-index sets of the two roster entries
        (Tier 1 during post_pass_merge).
    roster_keys: all roster primary keys (for the unambiguous-subsequence
        check).
    """
    if not a_key or not b_key:
        return "ambiguous"
    if a_key == b_key:
        return "yes"
    # Tier 2: deterministic YES rules (beat Tier 1).
    sa = _strip_merge_noise(a_key)
    sb = _strip_merge_noise(b_key)
    if sa and sa == sb:
        return "yes"
    if len(sa) == len(sb) and len(sa) >= 2:
        # Spelling variant: one differing token pair ("joseph"/"josef").
        _diffs = [(x, y) for x, y in zip(sa, sb) if x != y]
        if len(_diffs) == 1 and _is_spelling_variant(*_diffs[0]):
            return "yes"
        # Nickname: known pair on the first token, rest identical.
        if sa[1:] == sb[1:] and _nick_pair(sa[0], sb[0]):
            return "yes"
        # Unambiguous multi-token subsequence.
        short, long_ = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
        if len(short) >= 2 and _is_contig_subseq(short, long_):
            if _merge_subseq_unambiguous(short, a_key, b_key, roster_keys):
                return "yes"
    # Tier 1: listed as separate characters in the same chapter -> hard NO.
    if chapter_keys is not None:
        if b_key in chapter_keys and b_key != a_key:
            return "no"
    elif a_chapters is not None and b_chapters is not None:
        if a_chapters & b_chapters:
            return "no"
    # Surname-family trap: same surname, incompatible first names -> hard NO.
    # (The prototype's #1 false-merge source; the decision model still gets
    # these wrong sometimes, so they never reach Tier 3.)
    _blocked, _ = _surname_trap_blocked(a_key, b_key)
    if _blocked:
        return "no"
    return "ambiguous"


def _rel_involves_author(rel, author):
    """True if either relationship endpoint matches the book's author."""
    if not author:
        return False
    return (_is_author_name(rel.get("from", ""), author) or
            _is_author_name(rel.get("to", ""), author))


def _is_author_name(name, author):
    """True if a character name matches the book's author (any common form).
    Prevents 'Daniel Suarez' the author from becoming a character."""
    if not name or not author:
        return False
    nk = norm_name(name)
    ak = norm_name(author)
    if not nk or not ak:
        return False
    if nk == ak:
        return True
    # "Last, First" form -> "first last" (check the character name).
    if "," in name:
        _parts = [p.strip() for p in name.split(",")]
        if len(_parts) == 2:
            _rev = norm_name(f"{_parts[1]} {_parts[0]}")
            if _rev == ak:
                return True
    _ntok = nk.split()
    _atok = ak.split()
    # Author surname alone as a character name ("suarez" for Daniel Suarez).
    if len(_atok) >= 2 and nk == _atok[-1] and len(nk) > 3:
        return True
    # Middle-name variants: "Daniel James Suarez" vs "Daniel Suarez".
    # The shorter token list must be a subsequence of the longer, sharing
    # first and last tokens. ("Daniel Smith" vs "Daniel Suarez" fails on
    # the last token, so similar-but-different names are safe.)
    if len(_ntok) >= 2 and len(_atok) >= 2:
        _short, _long = (_ntok, _atok) if len(_ntok) <= len(_atok) \
            else (_atok, _ntok)
        if _short[0] == _long[0] and _short[-1] == _long[-1]:
            _li, _matched = 0, 0
            for _st in _short:
                while _li < len(_long) and _long[_li] != _st:
                    _li += 1
                if _li < len(_long):
                    _matched += 1
                    _li += 1
            if _matched == len(_short):
                return True
    return False


def _tnorm(t):
    """Trope dedup key: lowercase, underscores -> spaces, tidy spaces."""
    return re.sub(r"\s+", " ", (t or "").lower().replace("_", " ")).strip()


def _clean_trope(t):
    """Display form of a trope name: underscores -> spaces, tidy spaces."""
    return re.sub(r"\s+", " ", (t or "").replace("_", " ")).strip()


def _is_pov_trope_label(label):
    """True when a trope label is really POV metadata ("Xaden's POV",
    "POV: Xaden", "Villain POV", "Dual POV"). POV belongs in the povs
    list, never in tropes — these labels leak out of chapter extraction
    and must not survive into final tropes or trope proposals.

    Token check on the punctuation-stripped form (norm): only a
    standalone "pov"/"povs" token counts. Deliberate: a catalogued
    "Point of View" label has no such token and survives — it names
    a narrative technique, not a per-character POV tag. Substrings
    do not count either ("Poverty" is not a POV label).
    """
    return any(tok in ("pov", "povs") for tok in norm(label).split())


_MALE_TOKENS = {"mr", "master", "sir", "lord", "king", "father", "dad",
                "daddy", "papa", "husband", "son", "boy", "brother",
                "uncle", "nephew", "grandfather", "grandpa", "groom",
                "widower", "fiance"}
_FEMALE_TOKENS = {"mrs", "ms", "miss", "madam", "dame", "lady", "queen",
                  "mother", "mom", "mommy", "mama", "wife", "daughter",
                  "girl", "sister", "aunt", "niece", "grandmother",
                  "grandma", "bride", "widow", "fiancee"}
_GENERIC_NOUNS = {
    "mother", "father", "mama", "papa", "daddy", "mommy", "mom", "dad",
    "husband", "wife", "spouse", "narrator", "landlord", "grocer",
    "teacher", "doctor", "officer", "neighbor", "neighbour", "friend",
    "child", "children", "baby", "son", "daughter", "boy", "girl",
    "man", "woman", "person", "people", "family", "host", "hostess",
    "driver", "milkman", "i", "me", "we", "they", "you", "it",
}


def _guess_gender(name):
    """Crude gender from titles/kinship words; None when undetectable."""
    nn = norm_name(name)
    tok = nn.split(" ")[0].rstrip(".") if nn else ""
    if tok in _MALE_TOKENS:
        return "m"
    if tok in _FEMALE_TOKENS:
        return "f"
    return None


def _is_proper(name):
    """True for proper names ('Mrs. Black', 'the Cortlands'); False for
    generics ('the father', 'Daddy', 'the narrator', 'I')."""
    n = norm_name(name)
    if not n or n.split(" ")[0] in _GENERIC_NOUNS:
        return False
    for tok in (name or "").strip().split():
        if tok.lower() in ("the", "a", "an"):
            continue
        if tok and tok[0].isupper():
            return True
    return False


def _norm_desc(d):
    return re.sub(r"\s+", " ", (d or "").strip().casefold())


_NONPERSON_NOUNS = {
    "house", "car", "truck", "town", "city", "village", "school",
    "store", "church", "hospital", "office", "building", "room",
    "garden", "farm", "hotel", "station", "bridge", "road",
    "street", "park", "kingdom", "empire", "province", "region",
    "territory", "realm", "nation", "country", "continent", "island",
    "mountain", "river", "forest", "desert", "valley", "castle",
    "fortress", "tower", "keep", "palace", "temple", "shrine",
    # Generic family references (not named characters)
    "dad", "mom", "father", "mother", "daddy", "mommy", "papa",
    "grandfather", "grandmother", "grandpa", "grandma",
}


def _has_nonperson_noun(name):
    toks = set(norm_name(name).split(" "))
    return bool(toks & _NONPERSON_NOUNS)


def _is_multi(name):
    # "the host and hostess" denotes more than one person.
    return " and " in f" {norm_name(name)} "


_EDITION_MARKER_RE = re.compile(
    r"\s*[\(\[][^()\[\]]*(?:unabridged|abridged|dramatized|adaptation|"
    r"audiobook|audio\s+edition|special\s+edition|deluxe\s+edition|"
    r"illustrated\s+edition|collector'?s\s+edition|movie\s+tie-?in|"
    r"film\s+tie-?in|tv\s+tie-?in|large\s+print|\ba\s+novel\b|"
    r"part\s+\d+(?:\s+of\s+\d+)?|\bbook\s+\d+\b|\bvol(?:ume)?\.?\s*\d+|#\s*\d+)"
    r"[^()\[\]]*[\)\]]\s*$",
    re.IGNORECASE)

_GENERIC_SUBTITLE_RE = re.compile(
    r"\s*:\s*(?:a novel|a thriller|a mystery|a memoir|an autobiography|"
    r"a biography|a romance|a novella)\.?\s*$",
    re.IGNORECASE)


def _invisible_to_space(s):
    """Map invisible format chars (zero-width spaces, BOM, word joiners, soft
    hyphens) to plain spaces and collapse whitespace. Word boundaries stay
    intact so 'Silver\\u200bFlames' becomes 'Silver Flames', not 'SilverFlames'."""
    return re.sub(r"\s+", " ", "".join(
        " " if unicodedata.category(ch) == "Cf" else ch
        for ch in (s or ""))).strip()


def _strip_edition_suffix(title):
    """Reduce an edition/variant title to its canonical work title.

    Strips trailing bracketed/paren edition markers ("(Part 1 of 2)",
    "[Dramatized Adaptation]", "(Unabridged)", "(Women of the Otherworld, #1)")
    and generic ": A Novel"-style subtitles. Real subtitles ("Dune: House
    Atreides") are untouched. Applied iteratively so stacked suffixes collapse
    fully.
    """
    t = _invisible_to_space(title)
    while True:
        stepped = _EDITION_MARKER_RE.sub("", t)
        stepped = _GENERIC_SUBTITLE_RE.sub("", stepped).strip()
        if stepped == t:
            return t
        t = stepped


def _canonical_author(author):
    """'Last, First' (Calibre-style) -> 'First Last'; otherwise tidy whitespace."""
    a = _invisible_to_space(author)
    parts = [p.strip() for p in a.split(",")]
    if len(parts) == 2 and parts[0] and parts[1]:
        a = f"{parts[1]} {parts[0]}"
    return re.sub(r"\s+", " ", a).strip()


def _display_title(title):
    """Consistent stored-title casing. A title with no uppercase letters at all
    (e.g. 'radicalized') gets word-initial caps, apostrophe-aware ("o'brien"
    -> "O'Brien"); anything already cased is left untouched so acronyms like
    'IT' survive."""
    t = _invisible_to_space(title)
    if not t or any(ch.isupper() for ch in t):
        return t

    def _cap_word(w):
        # "o'brien's" -> "O'Brien's", "don't" -> "Don't", "d'artagnan" -> "D'Artagnan":
        # capitalize the first segment and any post-apostrophe segment longer
        # than one char (possessives like 's stay lowercase).
        segs = w.split("'")
        out = [segs[0][:1].upper() + segs[0][1:]]
        out += ["'" + (s[:1].upper() + s[1:] if len(s) > 1 else s)
                for s in segs[1:]]
        return "".join(out)

    return " ".join(_cap_word(w) for w in t.split())


def _work_key(title, author):
    """Match key for work resolution: edition suffixes stripped, invisible
    chars neutralized, author order canonicalized, then norm(). The same book
    resolves to the same key regardless of which edition was processed."""
    return norm(_strip_edition_suffix(title)), norm(_canonical_author(author))


def _correct_metadata_raw(title, author, identifiers):
    """Cross-check parsed title/author against OpenLibrary via ISBN.

    Returns (title, author, corrected: bool). Only overrides when the API
    clearly disagrees: empty parsed title, parsed title == author name
    (the Change Agent bug), or zero shared significant words.
    One HTTP call per book; failures are silent (keeps parsed values).
    """
    isbn = (identifiers or {}).get("isbn")
    if not isbn:
        return title, author, False
    url = (f"https://openlibrary.org/search.json?q={quote(isbn, safe='')}"
           f"&fields=title,author_name&limit=1")
    data = _ol_request(url)
    if not data:
        return title, author, False
    try:
        docs = data.get("docs") or []
        if not docs:
            return title, author, False
        api_title = (docs[0].get("title") or "").strip()
        api_authors = docs[0].get("author_name") or []
        api_author = api_authors[0] if api_authors else ""
        if not api_title:
            return title, author, False

        tn, atn = norm(title), norm(api_title)
        an = norm(author)
        # Significant words (len >= 4) shared between parsed and API titles.
        tw = {w for w in tn.split() if len(w) >= 4}
        aw = {w for w in atn.split() if len(w) >= 4}
        shared = tw & aw

        needs_fix = (
            not tn                      # empty parsed title
            or (tn and tn == an)         # title == author (Change Agent bug)
            or (tw and aw and not shared)  # completely different titles
        )
        if not needs_fix:
            return title, author, False

        new_title = api_title
        new_author = author or api_author
        print(f"  Metadata corrected via OpenLibrary: "
              f"'{title}' -> '{new_title}'" + (f", author '{new_author}'" if not author and api_author else ""))
        return new_title, new_author, True
    except Exception:
        return title, author, False


def correct_metadata(title, author, identifiers):
    """Cross-check parsed title/author against OpenLibrary via ISBN, then
    canonicalize the display forms (consistent title casing, 'Last, First' ->
    'First Last'). Wrapper around _correct_metadata_raw so every return path
    yields canonical display metadata."""
    t, a, fixed = _correct_metadata_raw(title, author, identifiers)
    return _display_title(t), _canonical_author(a), fixed


def resolve_work(title, author, identifiers, is_anthology=False, create=True):
    """Return (work_id, how). Resolution order: ISBN -> edition -> work,
    then provider_ids ISBN -> work (catches re-runs whose title form changed),
    then exact canonical title/author match. With create=False (preview) never
    POSTs: how is 'new' when nothing matched.

    Author-name-only titles are refused outright ('author_title'): never
    matched, never created. Edition suffixes and invisible characters are
    stripped for both matching and the stored title, so one book maps to one
    work row.
    """
    isbn = (identifiers or {}).get("isbn")
    if isbn:
        rows = sb("editions", params=f"?isbn=eq.{quote(isbn, safe='')}&select=work_id&limit=1")
        if rows is None:
            return None, "db_error"
        if rows and rows[0].get("work_id"):
            return rows[0]["work_id"], f"isbn:{isbn}"
        rows = sb("works",
                  params=f"?provider_ids->>isbn=eq.{quote(isbn, safe='')}&select=id&limit=1")
        if rows is None:
            return None, "db_error"
        if rows:
            return rows[0]["id"], f"work_isbn:{isbn}"
    tn, an = _work_key(title, author)
    if not tn:
        print("  Cannot build a match key from the title; refusing to create a work")
        return None, "no_title"
    if an and tn == an:
        print(f"  Refusing work resolution: title is the author name ('{title}')")
        return None, "author_title"
    rows = sb("works", params=f"?title_norm=eq.{quote(tn, safe='')}"
                              f"&author_norm=eq.{quote(an, safe='')}&select=id&limit=1")
    if rows is None:
        return None, "db_error"
    if rows:
        return rows[0]["id"], "title_author"
    if not create:
        return None, "new"
    disp_title, disp_author = _display_title(_strip_edition_suffix(title)), _canonical_author(author)
    rows = sb("works", method="POST", data={
        "title": disp_title, "title_norm": tn,
        "authors": [disp_author] if disp_author else [], "author_norm": an,
        "provider_ids": {"isbn": isbn} if isbn else {},
        "is_anthology": is_anthology,
    })
    if not rows:
        return None, "db_error"
    return rows[0]["id"], "created"


def _title_candidates(title):
    """Up to 5 existing works with a similar title, for preview review."""
    words = [w for w in re.findall(r"[A-Za-z0-9']+", title or "") if len(w) >= 4]
    if not words:
        return []
    key = re.sub(r"[*%_\\\\]", "", max(words, key=len))
    if not key:
        return []
    rows = sb("works", params=f"?title=ilike.*{quote(key, safe='')}*"
                              f"&select=id,title,authors&limit=5")
    return rows or []


def _existing(table, col, work_id):
    rows = sb(table, params=f"?work_id=eq.{quote(str(work_id), safe='')}&select={col}")
    if rows is None:
        return None
    return {str(r.get(col, "")).lower() for r in rows}


class TropeLookupError(RuntimeError):
    """The trope catalog lookup itself failed (sb() returned None —
    a DB error), as opposed to the trope genuinely not being in the
    catalog (which returns None). Diverting a trope to proposals on
    a lookup failure would file a catalogued trope as uncatalogued."""


def _trope_lookup(name):
    # Exact, case-insensitive match. Strip LIKE/PostgREST wildcard characters first.
    clean = re.sub(r"[*%_\\]", "", name).strip()
    if not clean:
        return None
    rows = sb("tropes", params=f"?name=ilike.{quote(clean, safe='')}&select=id&limit=1")
    if rows is None:
        raise TropeLookupError(f"trope catalog lookup failed for {name!r}")
    return rows[0]["id"] if rows else None


def _suggest_character_links(inserted_rows):
    """For newly inserted book_characters rows, look up the canonical
    characters table by name_norm and set suggested_character_id on matches.
    Never auto-links; the UI shows accept/reject."""
    if not inserted_rows:
        return
    # Batch lookup: collect unique normalized names.
    name_to_id = {}
    for r in inserted_rows:
        nid = r.get("id")
        nname = r.get("name", "")
        nkey = norm_name(nname)
        if nid and nkey and nkey not in name_to_id:
            name_to_id[nkey] = None
    if not name_to_id:
        return
    # Query characters table for matches (batched via in= operator).
    from urllib.parse import quote as _quote
    keys = list(name_to_id.keys())
    in_list = ",".join(_quote(k, safe='') for k in keys)
    matches = sb("characters", params=f"?name_norm=in.({in_list})&select=id,name_norm")
    if not matches:
        return
    for m in matches:
        nk = (m.get("name_norm") or "").strip()
        if nk in name_to_id:
            name_to_id[nk] = m["id"]
    # PATCH each matched row with the suggestion.
    for r in inserted_rows:
        nid = r.get("id")
        nkey = norm_name(r.get("name", ""))
        cid = name_to_id.get(nkey)
        if nid and cid:
            sb("book_characters", method="PATCH",
               params=f"?id=eq.{nid}",
               data={"suggested_character_id": cid})


# --- Replace-wholesale claim writes --------------------------------------
# Reprocessing a work REPLACES the claims the processor previously wrote for
# it (approved 2026-10-14). Processor rows are identified by the per-table
# source_type marker below; rows with any other source_type are contributor/
# human rows and are NEVER deleted. Rows with status='rejected' are human
# rejection state: excluded from every DELETE, and the new run is deduped
# against them so a rejected claim is never resurrected as a candidate.
_CLAIM_TABLES = {
    "book_trope_claims": {"id_col": "trope_id", "source": "ai"},
    "book_trigger_claims": {"id_col": "warning", "source": "ai"},
    "book_characters": {"id_col": "name", "source": "ai"},
    "book_quotes": {"id_col": "quote", "source": "pipeline"},
}

_PROP_MIN_CONF = 0.7  # proposals: only propose at/above this confidence


def _claim_rows(table, work_id):
    """All claim rows for a work: id, identity column, status, source_type.
    Returns None on DB error. (book_quotes has no status column.)"""
    spec = _CLAIM_TABLES[table]
    cols = f"id,{spec['id_col']},source_type"
    if table != "book_quotes":
        cols += ",status"
    return sb(table, params=f"?work_id=eq.{quote(str(work_id), safe='')}"
                            f"&select={cols}&limit=1000")


def _claim_key(table, row):
    """Dedupe identity for an existing claim row, matching the key the
    insert builders use for new-run items."""
    v = row.get(_CLAIM_TABLES[table]["id_col"], "")
    if table == "book_quotes":
        return (v or "")[:100]
    return str(v if v is not None else "").lower()


def _partition_claims(table, rows):
    """Split existing rows into (preserved_keys, rejected_keys, deletable_n).

    Preserved = contributor rows (source_type != processor marker) plus
    human-curated rows: rejected rows and confirmed rows (decision A,
    2026-10-14 — confirmation is curation state like rejection, with the
    opposite sign). New-run claims are deduped against preserved keys, so
    a curated claim is never re-inserted as a fresh candidate.
    Deletable = processor rows with a non-empty status that is neither
    rejected nor confirmed.
    A NULL/empty-status processor row is NOT deletable: the DELETE's
    status=neq.* filters never match NULL in PostgREST, so the
    row would survive the delete while its key left the preserved set
    and a re-proposed claim would insert a duplicate beside it.
    (book_quotes has no status column; partition == delete there.)
    """
    src = _CLAIM_TABLES[table]["source"]
    preserved, rejected, ndel = set(), set(), 0
    for r in rows:
        k = _claim_key(table, r)
        status = r.get("status") or ""
        if status == "rejected":
            rejected.add(k)
            preserved.add(k)
        elif status == "confirmed":
            preserved.add(k)
        elif (r.get("source_type") or "") == src:
            if table != "book_quotes" and not status:
                preserved.add(k)
            else:
                ndel += 1
        else:
            preserved.add(k)
    return preserved, rejected, ndel


def _delete_processor_claims(table, work_id):
    """Delete ONLY this work's processor-sourced, non-curated rows.
    The source_type condition is on every DELETE; claims tables also
    carry status=neq.rejected AND status=neq.confirmed (PostgREST ANDs
    repeated column filters) so human curation state — rejections and
    confirmations alike — is never removed. A third exclusion,
    status=neq. (empty value), keeps the DELETE consistent with
    _partition_claims: the partition preserves ''-status processor rows,
    but a plain neq.rejected/neq.confirmed pair MATCHES '' in PostgREST
    (only NULL is unmatched by neq), so without it a preserved ''-status
    row was deleted while its key stayed in the skip set — deleted and
    never re-inserted. NULL-status rows are unaffected either way (no
    neq filter matches NULL).
    Returns the number of rows deleted, or None on error."""
    src = _CLAIM_TABLES[table]["source"]
    params = (f"?work_id=eq.{quote(str(work_id), safe='')}"
              f"&source_type=eq.{src}")
    if table != "book_quotes":
        params += "&status=neq.rejected&status=neq.confirmed&status=neq."
    gone = sb(table, method="DELETE", params=params)
    return None if gone is None else len(gone)


def _build_trope_rows(work_id, result, cat_map, trope_mappings, model, skip_ids):
    """Build book_trope_claims rows for the new run. skip_ids holds the
    lowercased trope_ids that survive in the DB (contributor/rejected) —
    a new-run trope resolving to one is not re-inserted.
    Returns (rows, unmatched_names, lookup_failed_names). A trope whose
    catalog lookup FAILED (DB error) is in neither rows nor unmatched:
    it is an error for the book, never a proposal."""
    conf_t = result.get("trope_confidence", {})
    rows, unmatched, lookup_failed, used = [], [], [], set(skip_ids)
    for t in result["tropes"]:
        tid = cat_map.get(_tnorm(t))
        if tid is None and trope_mappings and t in trope_mappings:
            tid = trope_mappings[t]
        if tid is None:
            try:
                tid = _trope_lookup(t)
            except TropeLookupError:
                lookup_failed.append(t)
                continue
        if tid is None:
            unmatched.append(t)
            continue
        if str(tid).lower() in used:
            continue
        used.add(str(tid).lower())
        _chaps = (result.get("trope_chapters") or {}).get(_tnorm(t), [])
        _tconf = conf_t.get(_tnorm(t), 0.7)
        if not _chaps:
            # No supporting chapters recorded: the claim has no
            # evidence, so it must not carry real confidence.
            _tconf = 0.0
        _ev = {"source": "ebook-extraction",
               "chapters": _chaps,
               "chapter_count": len(_chaps)}
        # Option F (OC-7, a requirement): a claim shipping via the
        # persistence gate carries its stamp in the row, so a reviewer
        # can tell it apart from a natural candidate. The confidence
        # above is the pipeline's own value — never boosted.
        _stamp = (result.get("trope_persistence") or {}).get(_tnorm(t))
        if _stamp:
            _ev["persistence"] = dict(_stamp)
        rows.append({"work_id": work_id, "trope_id": tid, "status": "candidate",
                     "confidence": _tconf,
                     "source_type": "ai",
                     "model": model,
                     "evidence": _ev})
    return rows, unmatched, lookup_failed


def _build_trigger_rows(work_id, result, model, skip_warnings):
    """Build book_trigger_claims rows, skipping warnings that survive in
    the DB (contributor/rejected rows)."""
    rows, seen = [], set(skip_warnings)
    for t in result["triggers"]:
        warn = t["warning"] if isinstance(t, dict) else t
        if str(warn).lower() in seen:
            continue
        seen.add(str(warn).lower())
        td = t if isinstance(t, dict) else {}
        _tev = td.get("evidence") or []
        _tconf = td.get("confidence", 0.7)
        if not _tev:
            # No textual evidence -> no real confidence.
            _tconf = 0.0
        _ev = {"source": "ebook-extraction",
               "severity": td.get("severity", ""),
               "severity_claimed": td.get("severity_claimed", ""),
               "chapters": td.get("chapters", []),
               "chapter_count": td.get("chapter_count", 0),
               "frequency": td.get("frequency", 0),
               "prominence": td.get("prominence", ""),
               "quotes": td.get("evidence", [])}
        # Option F (OC-7): persistence stamp, as in _build_trope_rows.
        _stamp = (result.get("trigger_persistence") or {}).get(
            str(warn).lower())
        if _stamp:
            _ev["persistence"] = dict(_stamp)
        rows.append({"work_id": work_id, "warning": warn, "status": "candidate",
                     "confidence": _tconf,
                     "source_type": "ai",
                     "model": model,
                     "evidence": _ev})
    return rows


def _build_rel_map(result):
    """Map character name (lower) -> relationship list for DB promotion.
    Evidence-less relationships stay in the preview JSON only.
    Returns (rel_map, n_evidenceless)."""
    rel_map, evidenceless = {}, 0
    for r in result["relationships"]:
        if not r.get("evidence"):
            evidenceless += 1
            continue
        _entry = {"to": r["to"], "type": r["type"],
                  "evidence": r.get("evidence", "")}
        if r.get("evidence_source"):
            _entry["evidence_source"] = r["evidence_source"]
        rel_map.setdefault(r["from"].lower(), []).append(_entry)
    return rel_map, evidenceless


def _build_character_rows(work_id, result, rel_map, skip_names):
    """Build book_characters rows (relationships embedded per character),
    skipping names that survive in the DB (contributor/rejected rows)."""
    rows, seen = [], set(skip_names)
    for c in result["characters"]:
        if c["name"].lower() in seen:
            continue
        seen.add(c["name"].lower())
        rows.append({"work_id": work_id, "name": c["name"], "role": c["role"],
                     "description": c.get("description", ""),
                     "appearance": c.get("appearance", ""),
                     "vitality": c.get("status", "unknown"),
                     "status": "candidate",
                     "aliases": c.get("aliases", []),
                     "first_appearance_chapter": c.get("first_appearance_chapter"),
                     "relationships": rel_map.get(c["name"].lower(), []),
                     "source_type": "ai",
                     "confidence": c.get("confidence", 0.7)})
    return rows


def _build_quote_rows(work_id, result, skip_prefixes):
    """Build book_quotes rows (cap 5), skipping quote prefixes that survive
    in the DB (contributor rows)."""
    qrows, seen_q = [], set(skip_prefixes)
    for q in result.get("quotes", [])[:5]:  # cap at 5
        qtext = q.get("text", "") if isinstance(q, dict) else str(q)
        if not qtext or qtext[:100] in seen_q:
            continue
        seen_q.add(qtext[:100])
        _spk = q.get("speaker") if isinstance(q, dict) else None
        qrows.append({"work_id": work_id,
                      "quote": qtext,
                      "speaker_name": _spk,
                      "source_type": "pipeline",
                      "confidence": 0.7})
    return qrows


def _build_proposal_rows(result, unmatched, skip_keys):
    """Build trope_proposals rows for unmapped tropes. skip_keys holds
    name_keys that will still exist after the replace (other works'
    proposals + this work's human-reviewed ones). Denylisted tropes
    (static list + learned 3+-rejection denylist) and sub-threshold
    tropes are never proposed."""
    conf_t = result.get("trope_confidence", {})
    rows, seen_keys = [], set(skip_keys)
    for t in unmatched:
        key = _tnorm(t)
        if key in seen_keys:
            continue
        # Skip denylisted and low-confidence
        if _is_denylisted_trope(t):
            continue
        if _learn().is_trope_denylisted(_trej_key(t)):
            continue
        if conf_t.get(key, 0.7) < _PROP_MIN_CONF:
            continue
        seen_keys.add(key)
        row = {"name": t, "name_key": key,
               "description": f"Detected by ebook processor in '{result.get('title', '')}'.",
               "genres": [],
               "book_key": result.get("isbn") or result.get("title", ""),
               "status": "pending"}
        uid = CONFIG.get("proposer_user_id")
        if uid:
            row["proposed_by"] = uid
        rows.append(row)
    return rows


def _proposal_scope():
    """PostgREST condition limiting trope_proposals to THIS processor's
    own rows, structurally: proposed_by NULL (no proposer configured /
    legacy rows) or the configured proposer_user_id — the only values
    the processor ever writes (see _build_proposal_rows). App-written
    suggestions carry a real user uuid and must never fall inside a
    processor fetch-for-replace or DELETE, whatever key format either
    side uses."""
    uid = CONFIG.get("proposer_user_id")
    if uid:
        return ("or=(proposed_by.is.null,proposed_by.eq."
                f"{quote(str(uid), safe='')})")
    return "proposed_by=is.null"


def _proposal_state(result):
    """Proposal bookkeeping for one work. trope_proposals has no work_id or
    source_type: a work's processor proposals are the rows carrying its
    book_key with status='pending' AND the processor's proposer scope
    (_proposal_scope) — app suggestions (foreign proposed_by) are never
    'own' rows, even if key formats align. Human-reviewed rows carry
    other statuses and are preserved. Returns (all_name_keys, own_rows)
    or (None, None) on DB error."""
    all_rows = sb("trope_proposals", params="?select=name_key&limit=1000")
    if all_rows is None:
        return None, None
    book_key = result.get("isbn") or result.get("title", "")
    if not book_key:
        return {r["name_key"] for r in all_rows}, []
    own = sb("trope_proposals",
             params=f"?book_key=eq.{quote(str(book_key), safe='')}"
                    f"&{_proposal_scope()}"
                    f"&select=id,name_key,status,proposed_by&limit=1000")
    if own is None:
        return None, None
    return {r["name_key"] for r in all_rows}, own


def write_claims(work_id, result, trope_mappings=None):
    """Replace this work's processor-sourced claims with the new run's claims.

    Replace-wholesale: existing processor claims for the work are deleted
    and the new run's claims inserted, so run-to-run variance and bad
    claims don't accumulate. Hard limits:
      * Only processor rows are deleted — the per-table source_type marker
        in _CLAIM_TABLES is a condition on every DELETE. Contributor/human
        rows are untouched, and new claims dedupe against them.
      * Human curation survives: status='rejected' and status='confirmed'
        rows are excluded from every DELETE (the curation record itself
        is kept) and the new run dedupes against them, so a rejected claim
        is never resurrected and a confirmed claim never silently
        reverts to a fresh candidate.
    trope_proposals is scoped by book_key + status='pending' +
    _proposal_scope() (it has no source_type, so proposed_by is the
    structural own-rows boundary). works.spice_detected is already an overwrite; book_meta
    is a shared merge store with no source marker and is NOT replaced.
    Returns the number of failed operations.
    """
    errors = 0
    _models = {"ollama": CONFIG["ollama_model"], "openrouter": CONFIG["openrouter_model"],
               "openai": CONFIG.get("openai_model", "")}
    model = _models.get(CONFIG["llm"], "")
    wrote = {"tropes": 0, "triggers": 0, "characters": 0, "proposals": 0, "quotes": 0}
    deleted = {"tropes": 0, "triggers": 0, "characters": 0, "proposals": 0, "quotes": 0}
    preserved_rej = 0
    # Merge explicit mappings with the in-pipeline catalog map.
    _cat = dict(result.get("trope_catalog_map", {}) or {})
    if trope_mappings:
        _cat.update({_tnorm(k): v for k, v in trope_mappings.items()})
    unmatched = []

    # Tropes (embedding-mapped when available, else exact lookup)
    _tstate = _claim_rows("book_trope_claims", work_id)
    if _tstate is None:
        errors += 1
    else:
        _preserved, _rejected, _ndel = _partition_claims("book_trope_claims", _tstate)
        preserved_rej += len(_rejected)
        rows, unmatched, _lookup_failed = _build_trope_rows(
            work_id, result, _cat, trope_mappings, model, _preserved)
        if _lookup_failed:
            # A failed catalog read is an error for this book — those
            # tropes are neither claimed nor filed as proposals.
            errors += len(_lookup_failed)
            print(f"  Trope catalog lookup failed for "
                  f"{len(_lookup_failed)} trope(s) "
                  f"({', '.join(_lookup_failed[:3])}); counted as "
                  f"errors, not filed as proposals")
        _ok = True
        if _ndel:
            _gone = _delete_processor_claims("book_trope_claims", work_id)
            if _gone is None:
                errors += 1
                _ok = False  # old rows remain; inserting would duplicate them
            else:
                deleted["tropes"] = _gone
        if _ok and rows:
            if sb("book_trope_claims", method="POST", data=rows) is None:
                errors += 1
            else:
                wrote["tropes"] = len(rows)

    # Unmapped tropes -> trope_proposals (with book provenance). Runs
    # unconditionally, like the other sections: a reprocess where every
    # trope now maps (unmatched empty) must still replace away this
    # work's own pending proposals from earlier runs — zero new rows is
    # a valid insert set. Human-reviewed and foreign-proposer rows are
    # outside the delete scope and survive exactly.
    _all_keys, _own = _proposal_state(result)
    if _all_keys is None:
        errors += 1
    else:
        _own_pending = {r["name_key"] for r in _own
                        if (r.get("status") or "pending") == "pending"}
        preserved_rej += sum(1 for r in _own
                             if (r.get("status") or "") == "rejected")
        prop_rows = _build_proposal_rows(
            result, unmatched, _all_keys - _own_pending)
        _ok = True
        if _own_pending:
            _gone_rows = sb(
                "trope_proposals", method="DELETE",
                params=f"?book_key=eq.{quote(str(result.get('isbn') or result.get('title', '')), safe='')}"
                       f"&status=eq.pending&{_proposal_scope()}")
            if _gone_rows is None:
                errors += 1
                _ok = False
            else:
                deleted["proposals"] = len(_gone_rows)
        if _ok and prop_rows:
            if sb("trope_proposals", method="POST", data=prop_rows) is None:
                errors += 1
            else:
                wrote["proposals"] = len(prop_rows)

    # Triggers
    _gstate = _claim_rows("book_trigger_claims", work_id)
    if _gstate is None:
        errors += 1
    else:
        _preserved, _rejected, _ndel = _partition_claims("book_trigger_claims", _gstate)
        preserved_rej += len(_rejected)
        rows = _build_trigger_rows(work_id, result, model, _preserved)
        _ok = True
        if _ndel:
            _gone = _delete_processor_claims("book_trigger_claims", work_id)
            if _gone is None:
                errors += 1
                _ok = False
            else:
                deleted["triggers"] = _gone
        if _ok and rows:
            if sb("book_trigger_claims", method="POST", data=rows) is None:
                errors += 1
            else:
                wrote["triggers"] = len(rows)

    # Characters (relationships are stored per character)
    rel_map, _evidenceless_rels = _build_rel_map(result)
    if _evidenceless_rels:
        print(f"  Skipped {_evidenceless_rels} evidence-less relationships (kept in preview)")
    _cstate = _claim_rows("book_characters", work_id)
    if _cstate is None:
        errors += 1
    else:
        _preserved, _rejected, _ndel = _partition_claims("book_characters", _cstate)
        preserved_rej += len(_rejected)
        pre_existing = set(_preserved)  # surviving rows: backfill targets
        rows = _build_character_rows(work_id, result, rel_map, _preserved)
        _ok = True
        if _ndel:
            _gone = _delete_processor_claims("book_characters", work_id)
            if _gone is None:
                errors += 1
                _ok = False
            else:
                deleted["characters"] = _gone
        if _ok and rows:
            inserted = sb("book_characters", method="POST", data=rows)
            if inserted is None:
                errors += 1
            else:
                wrote["characters"] = len(rows)
                # Suggest canonical character links via name_norm match.
                try:
                    _suggest_character_links(inserted)
                except Exception as e:
                    print(f"  (character link suggestions skipped: {e})")
        # Backfill: surviving (contributor/rejected) rows keep insert-only
        # semantics — PATCH NULL fields from the current extraction, never
        # overwrite. (Processor rows were replaced above with full data.)
        if _ok:
            _backfilled = 0
            for c in result["characters"]:
                if c["name"].lower() not in pre_existing:
                    continue  # only backfill rows that survived the replace
                patch = {}
                if c.get("appearance"):
                    patch["appearance"] = c["appearance"]
                if c.get("status"):
                    patch["vitality"] = c["status"]
                if c.get("aliases"):
                    patch["aliases"] = c["aliases"]
                if c.get("first_appearance_chapter") is not None:
                    patch["first_appearance_chapter"] = c["first_appearance_chapter"]
                if not patch:
                    continue
                # Only fill NULLs: fetch current values first
                existing = sb("book_characters",
                              params=f"?work_id=eq.{work_id}&name=ilike.{quote(c['name'], safe='')}"
                                     f"&select=id,appearance,vitality,aliases,first_appearance_chapter"
                                     f"&limit=1")
                if not existing:
                    continue
                row = existing[0]
                null_patch = {k: v for k, v in patch.items() if row.get(k) is None}
                # Empty aliases array counts as unset
                if row.get("aliases") == [] and "aliases" in patch:
                    null_patch["aliases"] = patch["aliases"]
                # Empty appearance string counts as unset (inserts write "", not NULL)
                if row.get("appearance") == "" and "appearance" in patch:
                    null_patch["appearance"] = patch["appearance"]
                if null_patch:
                    r = sb("book_characters", method="PATCH",
                           params=f"?id=eq.{row['id']}", data=null_patch)
                    if r is None:
                        errors += 1
                    else:
                        _backfilled += 1
            if _backfilled:
                print(f"  Backfilled {_backfilled} existing characters (NULL fields only)")

    # Spice -> works.spice_detected
    spice = result.get("spice_level")
    if spice is not None:
        r = sb("works", method="PATCH", params=f"?id=eq.{work_id}",
               data={"spice_detected": spice})
        if r is None:
            errors += 1

    # POVs, quotes, series -> book_meta.data (merge with existing)
    meta_updates = {}
    if result.get("povs"):
        meta_updates["povs"] = result["povs"]
    if result.get("series_name"):
        meta_updates["series_name"] = result["series_name"]
    if result.get("series_position") is not None:
        meta_updates["series_position"] = result["series_position"]
    if result.get("series_source"):
        meta_updates["series_source"] = result["series_source"]
    quotes = [{"text": q["text"], "chapter": q.get("chapter", "")}
              for q in result.get("quotes", []) if q.get("text")]
    if quotes:
        meta_updates["notable_quotes"] = quotes[:20]  # cap at 20
    if meta_updates:
        isbn = result.get("isbn")
        if isbn:
            existing = sb("book_meta", params=f"?isbn=eq.{isbn}&select=data")
            if existing is None:
                # sb() returns None on ANY error — falsy exactly like an
                # empty result ([]), but the two are not interchangeable
                # here: book_meta.data is a shared per-ISBN merge store,
                # and merging this run's updates into an empty dict and
                # upserting would REPLACE the stored blob, discarding
                # every other contributor's keys. Abort this book's
                # book_meta update instead and count the failure.
                errors += 1
            else:
                merged = dict(existing[0]["data"]) if existing and existing[0].get("data") else {}
                merged.update(meta_updates)
                # POST with upsert (merge-duplicates required for on_conflict)
                r = sb("book_meta", method="POST",
                       params="?on_conflict=isbn",
                       data={"isbn": isbn, "data": merged},
                       prefer="resolution=merge-duplicates")
                if r is None:
                    errors += 1

    # Quotes -> book_quotes (3-5 memorable per book, no speaker
    # attribution yet). Runs unconditionally, like the other sections:
    # a reprocess that now yields zero quotes must still delete this
    # work's stale pipeline quotes — zero rows is a valid insert set.
    _qstate = _claim_rows("book_quotes", work_id)
    if _qstate is None:
        errors += 1
    else:
        _preserved, _rejected, _ndel = _partition_claims("book_quotes", _qstate)
        qrows = _build_quote_rows(work_id, result, _preserved)
        _ok = True
        if _ndel:
            _gone = _delete_processor_claims("book_quotes", work_id)
            if _gone is None:
                errors += 1
                _ok = False
            else:
                deleted["quotes"] = _gone
        if _ok and qrows:
            if sb("book_quotes", method="POST", data=qrows) is None:
                errors += 1
            else:
                wrote["quotes"] = len(qrows)

    _tot_del = sum(deleted.values())
    _tot_new = sum(wrote.values())
    UI.status(f"  Replaced {_tot_del} processor claims with {_tot_new} new rows; "
              f"preserved {preserved_rej} rejections")
    UI.status(f"  Wrote {wrote['tropes']} tropes, {wrote['triggers']} triggers, "
              f"{wrote['characters']} characters, {wrote['proposals']} proposals, "
              f"{wrote.get('quotes', 0)} quotes (new rows only)")
    # P7: stash for the end-of-run summary panel.
    _DIAG.wrote_stats = dict(wrote)
    return errors


def plan_replace(work_id, result, trope_mappings=None):
    """Read-only replace plan for preview mode: what write_claims WOULD do
    for an already-in-DB work. GETs only — never POST/PATCH/DELETE.
    Returns None when Supabase isn't configured (caller stays silent),
    {} when a DB lookup fails, else {would_delete, would_insert,
    rejections_preserved, existing}."""
    if not work_id or not (CONFIG.get("supabase_url") and CONFIG.get("supabase_key")):
        return None
    _models = {"ollama": CONFIG["ollama_model"], "openrouter": CONFIG["openrouter_model"],
               "openai": CONFIG.get("openai_model", "")}
    model = _models.get(CONFIG["llm"], "")
    _cat = dict(result.get("trope_catalog_map", {}) or {})
    if trope_mappings:
        _cat.update({_tnorm(k): v for k, v in trope_mappings.items()})
    plan = {"would_delete": {}, "would_insert": {},
            "rejections_preserved": 0, "existing": 0}
    unmatched = []

    _tstate = _claim_rows("book_trope_claims", work_id)
    if _tstate is None:
        return {}
    _preserved, _rejected, _ndel = _partition_claims("book_trope_claims", _tstate)
    plan["would_delete"]["book_trope_claims"] = _ndel
    plan["existing"] += len(_tstate)
    plan["rejections_preserved"] += len(_rejected)
    _rows, unmatched, _lookup_failed = _build_trope_rows(
        work_id, result, _cat, trope_mappings, model, _preserved)
    if _lookup_failed:
        print(f"  Trope catalog lookup failed for "
              f"{len(_lookup_failed)} trope(s) "
              f"({', '.join(_lookup_failed[:3])}); excluded from the "
              f"plan (a failed read is not 'uncatalogued')")
    plan["would_insert"]["tropes"] = len(_rows)

    # Mirrors write_claims: the proposals replace is unconditional,
    # so the plan counts own pending proposals even when this run's
    # unmatched list is empty.
    _all_keys, _own = _proposal_state(result)
    if _all_keys is None:
        return {}
    _own_pending = {r["name_key"] for r in _own
                    if (r.get("status") or "pending") == "pending"}
    plan["would_delete"]["trope_proposals"] = len(_own_pending)
    plan["existing"] += len(_own)
    plan["rejections_preserved"] += sum(
        1 for r in _own if (r.get("status") or "") == "rejected")
    plan["would_insert"]["proposals"] = len(_build_proposal_rows(
        result, unmatched, _all_keys - _own_pending))

    _gstate = _claim_rows("book_trigger_claims", work_id)
    if _gstate is None:
        return {}
    _preserved, _rejected, _ndel = _partition_claims("book_trigger_claims", _gstate)
    plan["would_delete"]["book_trigger_claims"] = _ndel
    plan["existing"] += len(_gstate)
    plan["rejections_preserved"] += len(_rejected)
    plan["would_insert"]["triggers"] = len(
        _build_trigger_rows(work_id, result, model, _preserved))

    _cstate = _claim_rows("book_characters", work_id)
    if _cstate is None:
        return {}
    _preserved, _rejected, _ndel = _partition_claims("book_characters", _cstate)
    plan["would_delete"]["book_characters"] = _ndel
    plan["existing"] += len(_cstate)
    plan["rejections_preserved"] += len(_rejected)
    _rel_map, _nev = _build_rel_map(result)
    plan["would_insert"]["characters"] = len(
        _build_character_rows(work_id, result, _rel_map, _preserved))

    # Mirrors write_claims: the quotes replace is unconditional, so
    # the plan counts stale pipeline quotes even for a zero-quote run.
    _qstate = _claim_rows("book_quotes", work_id)
    if _qstate is None:
        return {}
    _preserved, _rejected, _ndel = _partition_claims("book_quotes", _qstate)
    plan["would_delete"]["book_quotes"] = _ndel
    plan["existing"] += len(_qstate)
    plan["would_insert"]["quotes"] = len(
        _build_quote_rows(work_id, result, _preserved))

    # Option F: held-back counts come from the run's staging marker —
    # the plan reads the seam's decision, it does not re-derive it
    # (a second gated copy is how plans learn to understate).
    _stg = result.get("staging")
    if _stg:
        _staged = _stg.get("staged") or []
        plan["held_back"] = {
            "tropes": sum(1 for s in _staged
                          if s.get("class") == "trope"),
            "triggers": sum(1 for s in _staged
                            if s.get("class") == "trigger")}
    return plan


def print_replace_plan(work_id, result, trope_mappings=None):
    """Preview-mode audit line: log the replace plan for an in-DB work.
    Read-only — preview mode never deletes or writes."""
    plan = plan_replace(work_id, result, trope_mappings)
    if plan is None:
        return
    if not plan:
        print("  Replace plan: unavailable (DB lookup failed); preview made no writes")
        return
    _wd = sum(plan["would_delete"].values())
    _wi = sum(plan["would_insert"].values())
    _hb = plan.get("held_back") or {}
    if _hb.get("tropes") or _hb.get("triggers"):
        print(f"  Staging: {_hb.get('tropes', 0)} trope(s), "
              f"{_hb.get('triggers', 0)} trigger(s) held back by the "
              f"staging gate (not in the counts below)")
    if not plan["existing"]:
        print(f"  Replace plan: no existing claims in DB — "
              f"a write run would insert {_wi} fresh rows")
        return
    print(f"  Replace plan: would delete {_wd} processor claims, "
          f"insert {_wi} new rows, preserve {plan['rejections_preserved']} "
          f"rejections (preview — nothing written)")


# --- Dedup (post-extraction character canonicalization) ---
def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def embed_vectors(base_url, model, texts):
    """POST texts to <base_url>/embeddings (OpenAI shape); raise on error."""
    url = base_url.rstrip("/") + "/embeddings"
    payload = {"model": model, "input": texts}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} -> HTTP {e.code}: "
                           f"{e.read().decode('utf-8', 'ignore')[:200]}")
    except Exception as e:
        raise RuntimeError(f"could not reach {url}: {e}")
    vecs = [None] * len(texts)
    for item in data.get("data", []):
        vecs[item["index"]] = item["embedding"]
    if any(v is None for v in vecs):
        raise RuntimeError("embeddings endpoint did not return a vector per input")
    return vecs


# Words that mark a name as a descriptive label rather than a real name
# ("the voice", "unknown voice", "the stranger").
_GENERIC_LABEL_WORDS = {
    "voice", "unknown", "unidentified", "stranger", "figure", "individual",
    "intruder", "caller", "speaker", "visitor", "newcomer", "bystander",
    "witness", "suspect", "victim", "officer", "guard", "clerk", "driver",
}


def _is_generic_label(name):
    """True for descriptive labels like 'the voice' or 'unknown voice'.

    These are not real names; merging them needs strong evidence.
    A name is generic if it is not a proper name, or if every content
    word is a generic descriptor (articles excluded).
    """
    if not name:
        return True
    if not _is_proper(name):
        return True
    toks = set(norm_name(name).split()) - {"the", "a", "an"}
    return bool(toks) and toks <= _GENERIC_LABEL_WORDS


def _dedupe_guard(name_a, name_b, desc_a="", desc_b=""):
    """Check dedupe Signal-2 guards for a candidate pair.

    Returns (allowed: bool, reason: str). Guards are evaluated in order;
    the first block wins. An empty reason means allowed. The guard
    conditions are identical to the inline checks they replace in
    dedupe_characters(); only the reason strings are new.
    """
    ga, gb = _guess_gender(name_a), _guess_gender(name_b)
    if ga and gb and ga != gb:
        return False, "gender mismatch"
    if _has_nonperson_noun(name_a) or _has_nonperson_noun(name_b):
        return False, "non-person noun"
    if _is_multi(name_a) != _is_multi(name_b):
        return False, "group vs individual"
    if norm_name(name_a) != norm_name(name_b):
        if _is_generic_label(name_a) or _is_generic_label(name_b):
            return False, "generic label, insufficient evidence"
        if _is_proper(name_a) and _is_proper(name_b):
            if _norm_desc(desc_a) != _norm_desc(desc_b):
                return False, "distinct proper names"
    return True, ""


def _surname_trap_blocked(a_key, b_key):
    """(blocked: bool, reason: str) for the surname-family trap.

    Same surname with incompatible first names (e.g. 'chris sebeck' vs
    'peter sebeck') are family members, not the same person. Extracted
    from _merge_tier(); the condition is unchanged.
    """
    sa = _strip_merge_noise(a_key)
    sb = _strip_merge_noise(b_key)
    if len(sa) >= 2 and len(sb) >= 2 and sa[-1] == sb[-1]:
        fa, fb = sa[0], sb[0]
        if (fa != fb and len(fa) >= 3 and len(fb) >= 3
                and not _nick_pair(fa, fb)
                and not _is_spelling_variant(fa, fb)
                and len(fa) > 1 and len(fb) > 1):
            return True, f"surname trap (family members sharing '{sa[-1]}')"
    return False, ""


def dedupe_characters(characters, relationships, povs):
    """Merge duplicate characters.

    Signal 1 (deterministic): norm_name() exact matches.
    Signal 2 (semantic): embedding cosine >= CONFIG['dedupe_threshold'].
    Relationships and POVs are remapped to canonical names and deduped.
    Returns (characters, relationships, povs, report).
    """
    if not characters:
        return characters, relationships, povs, []
    names = [c.get("name") or "" for c in characters]
    parent = list(range(len(characters)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Signal 1: deterministic name normalization
    buckets = {}
    for i, n in enumerate(names):
        buckets.setdefault(norm_name(n), []).append(i)
    for key, idxs in buckets.items():
        if key and len(idxs) > 1:
            for a, b in zip(idxs, idxs[1:]):
                union(a, b)
    s1_root = [find(i) for i in range(len(characters))]

    # Signal 2: embeddings
    thr = float(CONFIG.get("dedupe_threshold", 0.85))
    texts = [f"{names[i]}: {characters[i].get('description') or ''}"
             for i in range(len(characters))]
    try:
        vecs = embed_vectors(CONFIG["embed_url"], CONFIG.get("embed_model", ""), texts)
    except Exception as e:
        # Embedding is an enhancement, not a gate (the trope-catalog
        # path degrades the same way): an unconfigured or unreachable
        # endpoint must not discard a completed extraction. But the
        # Signal 1 unions computed above are exact normalized-name
        # matches — the safest merge class in the system — and a
        # transient embedding failure is no reason to throw them away.
        # Degrade to DETERMINISTIC merges only: fall through to
        # clustering with vecs = None, skipping the Signal 2 loop
        # entirely. Only the embedding step degrades like this;
        # genuine dedupe errors below still raise to the caller.
        print(f"  WARNING: dedupe embeddings unavailable ({e}); "
              f"applying DETERMINISTIC merges only — no embedding-based merges")
        vecs = None
    if vecs is not None:
        for i in range(len(characters)):
            for j in range(i + 1, len(characters)):
                if s1_root[i] == s1_root[j]:
                    continue  # already merged by signal 1
                _allowed, _reason = _dedupe_guard(
                    names[i], names[j],
                    characters[i].get("description"),
                    characters[j].get("description"))
                if not _allowed:
                    if CONFIG.get("explain_blocks"):
                        print(f"  Merge blocked: '{names[i]}' vs "
                              f"'{names[j]}' \u2014 {_reason}")
                    continue
                if _cos(vecs[i], vecs[j]) >= thr:
                    union(i, j)

    clusters = {}
    for i in range(len(characters)):
        clusters.setdefault(find(i), []).append(i)

    canon_of, name_to_canon = {}, {}
    new_chars, report = [], []
    for members in clusters.values():
        if len(members) == 1:
            i = members[0]
            canon_of[i] = names[i]
            new_chars.append(characters[i])
            continue
        canon_idx = max(members, key=lambda i: (characters[i].get("confidence") or 0,
                                                len(names[i])))
        canon_name = names[canon_idx]
        merged = dict(characters[canon_idx])
        merged["name"] = canon_name
        merged["description"] = max(
            (characters[i].get("description") or "" for i in members), key=len)
        merged["confidence"] = max(characters[i].get("confidence") or 0
                                   for i in members)
        # Union the dropped members' fields into the canonical character
        # instead of silently discarding them (previously the merged
        # character kept only the canonical member's aliases, appearance
        # counts, first appearance, status and POV flag, so the write
        # path and the principals filter saw a partial character).
        # Mirrors _merge_roster_entries where the shapes allow; each
        # field is only written when at least one member carries it, so
        # leaner character shapes (e.g. legacy sanitize output) gain no
        # invented keys.
        def _union_list(field):
            out = []
            for i in members:
                for v in characters[i].get(field) or []:
                    if v not in out:
                        out.append(v)
            return out

        if any("aliases" in characters[i] for i in members):
            _aliases = _union_list("aliases")
            # A merged member's name is another name for the canonical
            # character (the roster merge preserves dropped primaries
            # the same way, in primary_keys/alias_keys).
            for i in members:
                if i != canon_idx and names[i] and names[i] not in _aliases:
                    _aliases.append(names[i])
            merged["aliases"] = _aliases
        if any("unresolved_mentions" in characters[i] for i in members):
            merged["unresolved_mentions"] = _union_list("unresolved_mentions")
        # Appearances are mention counts — additive, summed like the
        # roster merge sums them.
        for field in ("appearances", "mention_count"):
            if any(field in characters[i] for i in members):
                merged[field] = sum(characters[i].get(field) or 0
                                    for i in members)
        # Chapter counts cannot be summed (members may share chapters
        # and the dicts carry no chapter set to union — the shape
        # differs from roster entries): take the max, the union's
        # lower bound, rather than overstating presence.
        for field in ("chapters_present", "chapter_count"):
            if any(field in characters[i] for i in members):
                merged[field] = max(characters[i].get(field) or 0
                                    for i in members)
        for field in ("first_appearance_chapter", "first_appearance"):
            _vals = [characters[i].get(field) for i in members
                     if characters[i].get(field) is not None]
            if _vals:
                merged[field] = min(_vals)
        if any("is_pov" in characters[i] for i in members):
            merged["is_pov"] = any(characters[i].get("is_pov")
                                    for i in members)
        # Sticky death (the reduce's rule): a member reported dead
        # keeps the merged character dead.
        if any(characters[i].get("status") == "dead" for i in members):
            merged["status"] = "dead"
        if any("appearance" in characters[i] for i in members):
            merged["appearance"] = max(
                (characters[i].get("appearance") or "" for i in members),
                key=len)
        if any("evidence" in characters[i] for i in members):
            merged["evidence"] = max(
                (characters[i].get("evidence") or "" for i in members),
                key=len)
        for i in members:
            canon_of[i] = canon_name
        new_chars.append(merged)
        report.append({"canonical": canon_name,
                       "merged": sorted(names[i] for i in members if i != canon_idx)})

    for i, n in enumerate(names):
        c = canon_of[i]
        name_to_canon[n] = c
        name_to_canon[n.lower()] = c
        name_to_canon[norm_name(n)] = c

    def remap(nm):
        return name_to_canon.get(nm,
               name_to_canon.get((nm or "").lower(),
               name_to_canon.get(norm_name(nm), nm)))

    new_rels, seen = [], set()
    _n_self_dropped = 0
    for r in relationships:
        a, b, t = remap(r["from"]), remap(r["to"]), r["type"]
        # Remapping can collapse both endpoints onto the same canonical
        # character (a merge the pair straddled). That self-pair is a
        # canonicalization artifact, not data — drop it (the field-
        # leakage remap guard's intent, kept here with the drop counted
        # and printed below). Canonical form also makes flipped
        # duplicates of the same pair dedupe together.
        _cp = _canonical_pair(a, b, t)
        if _cp is None:
            _n_self_dropped += 1
            continue
        a, b, t = _cp
        key = (a.lower(), b.lower(), t.lower())
        if key not in seen:
            seen.add(key)
            # Remap the endpoints on the FULL relationship dict: every
            # other key (evidence, evidence_source, confidence, ...)
            # is preserved verbatim. Reducing the dict to just
            # {from, to, type} here made every relationship look
            # evidence-less to the write path, which then wrote none
            # of them — a dedupe run silently shipped zero embedded
            # relationships even when zero clusters merged.
            _nr = dict(r)
            _nr["from"], _nr["to"], _nr["type"] = a, b, t
            new_rels.append(_nr)
    if _n_self_dropped:
        print(f"  Dedup: dropped {_n_self_dropped} self-relationship(s) "
              f"created by remapping")

    new_povs, seen_p = [], set()
    for p in povs:
        c = remap(p)
        if c.lower() not in seen_p:
            seen_p.add(c.lower())
            new_povs.append(c)

    return new_chars, new_rels, sorted(new_povs), report


# --- Processing ---
def _confidence(hits, ok):
    return round(min(0.95, 0.5 + 0.45 * hits / max(ok, 1)), 2)


def read_ebook(fpath):
    """Return (text, title, author, identifiers), or None if unsupported/failed."""
    suffix = fpath.suffix.lower()
    if suffix not in (".mobi", ".azw3"):
        return extract_epub_text(fpath)

    print(f"  Converting {suffix}...")
    identifiers = extract_mobi_identifiers(fpath)
    tmpdir = None
    try:
        from mobi import extract as mextract
        res = mextract(str(fpath))
        # mobi.extract returns a (tempdir, filepath) tuple
        if isinstance(res, (tuple, list)) and len(res) == 2:
            tmpdir, outpath = str(res[0]), Path(str(res[1]))
        else:
            outpath = Path(str(res))
        if outpath.suffix.lower() == ".epub":
            text, title, author, epub_ids = extract_epub_text(outpath)
            if not identifiers["isbn"] and epub_ids["isbn"]:
                identifiers["isbn"] = epub_ids["isbn"]
            identifiers["raw"].extend(epub_ids["raw"])
            return text, title, author, identifiers
        if outpath.suffix.lower() in (".html", ".htm"):
            html = outpath.read_text(encoding="utf-8", errors="ignore")
            return html_to_text(html), fpath.stem, None, identifiers
        print(f"  Unsupported extracted format: {outpath.suffix}")
        return None
    except Exception as e:
        print(f"  Conversion failed: {e}")
        return None
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _select_chunks(chunks, only):
    """Filter chunks by --chunks spec (e.g. '2' or '2,5,8'). Indices are
    1-based into the sampled chunk list; original numbering is kept so debug
    output stays comparable across runs."""
    indexed = list(enumerate(chunks, 1))
    if not only:
        return indexed
    wanted = {int(x) for x in str(only).split(",") if x.strip().isdigit()}
    return [(i, ch) for i, ch in indexed if i in wanted]


# ---------------------------------------------------------------------------
# P8: Crash resume — per-chapter checkpoints under preview/.checkpoints/
# ---------------------------------------------------------------------------
# After each chapter's Call A/B completes, its result is saved to
# preview/.checkpoints/{book_key}/chapter_{idx}.json. A --resume run loads
# completed chapters and skips their LLM calls. The roster dict is saved
# alongside (sets are converted to sorted lists for JSON).

_CHECKPOINT_FLAGS = (
    # CONFIG keys that affect output; a mismatch on --resume warns.
    "llm", "batch_size", "pipeline", "principals_only", "min_frequency",
    "dedupe", "v2_prompt_cache", "v2_shared_system", "v2_task_last",
    "decision_model", "decision_model_provider", "only_chunks",
)


def _checkpoint_book_key(fpath):
    """Safe directory name for a book's checkpoints."""
    import re as _re
    stem = Path(fpath).stem if not isinstance(fpath, str) else Path(fpath).stem
    safe = _re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_")
    return safe or "book"


def _checkpoint_dir(book_key):
    return SCRIPT_DIR / "preview" / ".checkpoints" / book_key


def _checkpoint_flags_hash():
    """Hash of the CONFIG values that affect processing output.

    Includes the current git commit SHA so that resuming after a code
    update (e.g. via --auto-update) invalidates stale checkpoints.
    """
    import hashlib as _hl
    import json as _js
    vals = {k: CONFIG.get(k) for k in _CHECKPOINT_FLAGS}
    vals["_code_sha"] = _git_commit_sha() or "unknown"
    return _hl.sha256(_js.dumps(vals, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _ckpt_json_safe(obj):
    """Convert sets to sorted lists so the object is JSON-serializable."""
    if isinstance(obj, set):
        return sorted(obj, key=str)
    if isinstance(obj, dict):
        return {k: _ckpt_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_ckpt_json_safe(v) for v in obj]
    return obj


def _ckpt_json_restore(obj):
    """Best-effort inverse of _ckpt_json_safe for known roster fields.

    Roster entries store several fields as sets (aliases, primary_keys,
    chapter appearances). On load we restore the known set-valued fields;
    anything else stays as-is (lists are harmless for the reduce).
    """
    _SET_FIELDS = {"aliases", "primary_keys", "chapters", "evidence_chapters"}
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _SET_FIELDS and isinstance(v, list):
                out[k] = set(v)
            else:
                out[k] = _ckpt_json_restore(v)
        return out
    if isinstance(obj, list):
        return [_ckpt_json_restore(v) for v in obj]
    return obj


# Roster-entry fields _roster_update produces with non-JSON-native types
# beyond the four _ckpt_json_restore restores generically. The generic
# restore cannot cover these: it also walks chapter a/b payloads, which
# may legitimately carry same-named keys (roles, statuses, alias_keys,
# descriptions) that must stay plain JSON values.
_ROSTER_SET_FIELDS = {"alias_keys", "alive_reports", "unresolved_mentions",
                      "_generic_keys"}
_ROSTER_COUNTER_FIELDS = {"roles", "statuses"}

# Core roster-entry fields: every entry _roster_update creates at birth
# carries exactly these, with these canonical empty defaults. This is
# the ONE census shared by every seam that ingests an external roster
# (checkpoint load; the cmd_retry_failed seed — which then reaches the
# same load on resume). Consumers (_merge_roster_entries, v2_reduce,
# _roster_prompt) direct-index these fields, so an entry from an older
# roster.json that predates one of them must be COMPLETED at the seam,
# not discovered by a KeyError mid-run. Completion is schema, not data:
# an empty set/Counter/list/str/0 asserts nothing about the book, and
# present values are never altered.
_ROSTER_CORE_DEFAULTS = (
    ("name", str), ("aliases", set), ("alias_keys", set),
    ("primary_keys", set), ("_generic_keys", set),
    ("appearances", int), ("last_seen", int),
    ("chapters", set), ("roles", Counter), ("descriptions", list),
)


def _roster_entry_complete(entry):
    """Fill any ABSENT core roster fields with their canonical empty
    defaults (see _ROSTER_CORE_DEFAULTS). Mutates and returns the
    entry; present values are never altered."""
    if isinstance(entry, dict):
        for _f, _mk in _ROSTER_CORE_DEFAULTS:
            if _f not in entry:
                entry[_f] = _mk()
    return entry


def _ckpt_restore_roster_entry(entry):
    """Normalize one externally-loaded roster entry back to the
    producer's canonical shape. Mutates and returns the entry.

    Two halves. (1) Type coercion: _ckpt_json_safe turns sets into
    sorted lists, Counters into dicts, and description tuples into
    lists; _ckpt_json_restore only undoes the four generic set fields.
    Left as JSON values, a resumed roster crashed
    _merge_roster_entries (roles: dict += Counter -> TypeError) and
    would crash v2_reduce instead on statuses.most_common(1) in a
    merge-free resumed run; statuses merged via Counter.update(dict)
    also silently replaced counts instead of summing. Only fields that
    are present AND still of the JSON type are coerced.
    death_reports / appearances_desc are lists by design and stay
    lists; descriptions stays a list, its 3-element entries
    (chapter, text, has_evidence) become tuples again so the
    description selectors see the shape _roster_update appended.
    (2) Schema completion (_roster_entry_complete): core fields ABSENT
    entirely — a roster.json written before the field existed — get
    their canonical empty defaults, so merge/reduce never KeyError on
    a field the producer guarantees.
    """
    if not isinstance(entry, dict):
        return entry
    for k in _ROSTER_SET_FIELDS:
        if isinstance(entry.get(k), list):
            entry[k] = set(entry[k])
    for k in _ROSTER_COUNTER_FIELDS:
        if isinstance(entry.get(k), dict) \
                and not isinstance(entry[k], Counter):
            entry[k] = Counter(entry[k])
    descs = entry.get("descriptions")
    if isinstance(descs, list):
        entry["descriptions"] = [
            tuple(d) if isinstance(d, list) and len(d) == 3 else d
            for d in descs]
    return _roster_entry_complete(entry)


def _checkpoint_save_meta(book_key, total_chapters):
    d = _checkpoint_dir(book_key)
    d.mkdir(parents=True, exist_ok=True)
    meta = {"book_key": book_key, "total_chapters": total_chapters,
            "flags_hash": _checkpoint_flags_hash(),
            "version": PORTABLE_VERSION}
    with open(d / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _checkpoint_save_chapter(book_key, idx, a, b):
    d = _checkpoint_dir(book_key)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"chapter_{idx}.json", "w", encoding="utf-8") as f:
        json.dump({"index": idx, "a": _ckpt_json_safe(a),
                   "b": _ckpt_json_safe(b)}, f, ensure_ascii=False)


def _checkpoint_save_roster(book_key, roster):
    d = _checkpoint_dir(book_key)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "roster.json", "w", encoding="utf-8") as f:
        json.dump(_ckpt_json_safe(roster), f, ensure_ascii=False)


def _checkpoint_load(book_key):
    """Returns (meta, roster, {idx: (a, b)}) or None if no checkpoints."""
    d = _checkpoint_dir(book_key)
    meta_path = d / "meta.json"
    if not meta_path.is_file():
        return None
    meta = json.load(open(meta_path, encoding="utf-8"))
    roster = {}
    rp = d / "roster.json"
    if rp.is_file():
        roster = _ckpt_json_restore(json.load(open(rp, encoding="utf-8")))
        if isinstance(roster, dict):
            # Roster-scoped: chapter a/b payloads below share
            # _ckpt_json_restore but must NOT get Counter/tuple coercion.
            for _entry in roster.values():
                _ckpt_restore_roster_entry(_entry)
    chapters = {}
    for cp in sorted(d.glob("chapter_*.json")):
        try:
            idx = int(cp.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        data = json.load(open(cp, encoding="utf-8"))
        a = _ckpt_json_restore(data.get("a"))
        b = _ckpt_json_restore(data.get("b"))
        chapters[idx] = (a, b)
    return meta, roster, chapters


def _checkpoint_clear(book_key):
    import shutil as _sh
    d = _checkpoint_dir(book_key)
    if d.is_dir():
        _sh.rmtree(d)


def _checkpoint_resume_check(book_key, total_chapters):
    """Validate checkpoints for a --resume run.

    Returns (chapters_dict, roster) or (None, None) if resume is not possible.
    Warns on flags-hash or chapter-count mismatch; prompts to confirm when
    interactive, otherwise refuses to resume.
    """
    loaded = _checkpoint_load(book_key)
    if not loaded:
        print(f"  --resume: no checkpoints found for '{book_key}', starting fresh")
        return None, None
    meta, roster, chapters = loaded
    problems = []
    if meta.get("flags_hash") != _checkpoint_flags_hash():
        problems.append("processing flags differ from the checkpointed run")
    if meta.get("total_chapters") != total_chapters:
        problems.append(
            f"chapter count differs (checkpoint: {meta.get('total_chapters')}, "
            f"now: {total_chapters})")
    if problems:
        print(f"  --resume WARNING: {'; '.join(problems)}")
        if CONFIG.get("resume_force"):
            print("  Resuming despite mismatch (--retry-failed seeded these)")
        else:
            try:
                import sys as _sys
                if _sys.stdin.isatty():
                    ans = input("  Resume anyway? [y/N] ").strip().lower()
                else:
                    ans = "n"
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans != "y":
                print("  Starting fresh (clearing checkpoints)")
                _checkpoint_clear(book_key)
                return None, None
            print("  Resuming despite mismatch")
    n_done = sum(1 for a, b in chapters.values() if a is not None and b is not None)
    print(f"  --resume: {n_done}/{total_chapters} chapters already complete, "
          f"skipping their LLM calls")
    return chapters, roster


def process_file_v2(fpath, dry_run=False, preview=False):
    """v2 pipeline: chapter-level map/reduce. Chapters run Call A (characters,
    sequential for the roster) then Call B (content, parallelizable), followed
    by a deterministic reduce and a trope confirmation gate."""
    import time as _time
    _t0 = _time.time()
    print(f"\nProcessing (v2): {fpath.name}")
    global _DEBUG_TAG
    _DEBUG_TAG = fpath.stem if (preview or dry_run) and CONFIG.get("debug") else None
    suffix = fpath.suffix.lower()

    if suffix == ".epub":
        _DIAG.chapter_detection_fallback = False  # reset per book
        units, title, author, identifiers = extract_epub_units(fpath)
        chapters = split_chapters(units)
        _chap_fallback = getattr(_DIAG, "chapter_detection_fallback", False)
        word_count = sum(len(u["text"].split()) for u in units)
    else:
        extracted = read_ebook(fpath)
        if extracted is None:
            return False
        text, title, author, identifiers = extracted
        _DIAG.chapter_detection_fallback = False  # reset per book (_prose_chapters may set it)
        chapters = _prose_chapters(text)
        _chap_fallback = getattr(_DIAG, "chapter_detection_fallback", False)
        word_count = len(text.split())

    _book_key = str(fpath)
    # ISBN resolution: --isbn flag > EPUB metadata > Open Library lookup.
    # Default is to skip books with no ISBN (see --allow-no-isbn).
    _isbn, _isbn_source = _resolve_book_isbn(identifiers, title, author)
    if _isbn_source == "none" and not _handle_missing_isbn(
            fpath, preview=preview, dry_run=dry_run):
        return False
    # Reset the per-book decision-model call budget (merges + relationship
    # validation share it; merges run first).
    _dm_budget_reset()
    # Series detection: --series flag > EPUB calibre metadata > filename
    # pattern > Open Library. Used for cross-book character continuity.
    _series_name, _series_pos, _series_source = _detect_series(
        fpath, identifiers, title, author)
    if _series_name:
        _pos_str = f" #{_series_pos:g}" if _series_pos is not None else ""
        UI.status(f"  Series: {_series_name}{_pos_str} (from {_series_source})")
    # Cross-run learning: stash per-book context for _roster_update hooks.
    _ln_book = _learn()
    _learn_isbn = identifiers.get("isbn") or identifiers.get("asin")
    _DIAG.learn_isbn = _learn_isbn
    # Author filter: the book's author is never a character.
    _DIAG.book_author = author
    _learn_skey = _ln_book.series_key(author) if _ln_book.enabled else None
    _DIAG.learn_series_key = _learn_skey
    _learn_hints = (_ln_book.load_series_hints(_learn_skey)
                    if _learn_skey else {})
    # Series-specific roster (in addition to author roster): when the series
    # is known, prior books in the same series contribute alias hints.
    _learn_series_name_key = (_ln_book.series_name_key(_series_name)
                              if (_ln_book.enabled and _series_name) else None)
    _DIAG.learn_series_name_key = _learn_series_name_key
    if _learn_series_name_key:
        _series_hints = _ln_book.load_series_name_hints(_learn_series_name_key)
        # Series hints win on conflict (more specific than author-level).
        _learn_hints = {**_learn_hints, **_series_hints}
    _DIAG.learn_series_hints = _learn_hints
    UI.status(f"  Title: {title or fpath.stem}, "
              f"Chars: {sum(len(c['text']) for c in chapters):,}")
    if identifiers.get("isbn"):
        UI.status(f"  ISBN: {identifiers['isbn']}")
    elif identifiers.get("asin"):
        UI.status(f"  ASIN: {identifiers['asin']} (no ISBN found)")
    if not chapters:
        UI.status("  No chapters extracted")
        return False

    # --chunks: restrict to specific chapter indices (1-based). Applied to the
    # chapter list; original numbering is kept for debug comparability.
    only = CONFIG.get("only_chunks")
    indexed = _select_chunks(chapters, only)
    if only:
        if not indexed:
            print(f"  --chunks {only}: no chapters match (1-{len(chapters)})")
            return False
        UI.status(f"  {len(chapters)} chapters, selecting {len(indexed)} "
                  f"(--chunks {only}), LLM: {CONFIG['llm']}")
    else:
        UI.status(f"  {len(chapters)} chapters, LLM: {CONFIG['llm']}")
    n = len(indexed)
    # Front/back matter chapters keep their slots but are never story
    # content: no Call A/B is spent on them and they contribute no
    # roster entries or evidence (v2_reduce also excludes them).
    _fm_idx_v2 = {i for i, ch in indexed if ch.get("is_frontmatter")}
    if _fm_idx_v2:
        UI.status(f"  Skipping {len(_fm_idx_v2)} front/back-matter "
                  f"chapter(s) (no extraction)")
    # 2 work units per chapter (Call A identity + Call B content)
    UI.book_start(_book_key, f"📖 {title or fpath.stem}", total=2 * n)

    backends = {"ollama": llm_ollama, "openrouter": llm_openrouter,
                "openai": llm_openai_compat}
    call = backends.get(CONFIG["llm"], llm_ollama)
    batch_size = effective_batch_size()

    # P8: crash resume. Checkpoint key is stable per book file.
    _ckpt_key = _checkpoint_book_key(fpath)
    _resumed_chapters = {}  # idx -> (a, b) loaded from checkpoints
    _resumed_roster = None
    if CONFIG.get("resume"):
        _rc, _rr = _checkpoint_resume_check(_ckpt_key, n)
        if _rc is not None:
            _resumed_chapters = _rc
            _resumed_roster = _rr
    else:
        # Fresh run: write meta.json so a later --resume can validate flags.
        _checkpoint_save_meta(_ckpt_key, n)

    def _ckpt_complete(idx):
        """True if chapter idx has both Call A and Call B checkpointed."""
        pair = _resumed_chapters.get(idx)
        return pair is not None and pair[0] is not None and pair[1] is not None

    # Pass 1: Call A sequential (roster must see chapters in order).
    # When v2_prompt_cache is on, submit B(i) right after A(i) so the
    # chapter's KV cache is still hot (interleaved, B on worker threads).
    roster, chapter_as, chapter_bs = {}, {}, {}
    a_ok = a_failed = a_fallback = 0
    b_ok = b_failed = 0
    # On resume, restore the roster built by the checkpointed chapters and
    # pre-fill results for fully completed chapters (their LLM calls are skipped).
    if _resumed_roster:
        roster.update(_resumed_roster)
    for _ri, _rpair in sorted(_resumed_chapters.items()):
        if _ri in _fm_idx_v2:
            continue  # front/back matter: stale checkpoint results dropped
        _ra, _rb = _rpair
        if _ra is not None and _rb is not None:
            chapter_as[_ri] = _ra
            chapter_bs[_ri] = _rb
            a_ok += 1
            b_ok += 1
            UI.advance(_book_key, 2)  # both work units already done
    # Front/back matter slots: present in chapter_data (indices must not
    # shift) but permanently empty — no calls, no results, in every mode.
    for _fi in _fm_idx_v2:
        chapter_as[_fi] = None
        chapter_bs[_fi] = None
    interleave = CONFIG.get("v2_prompt_cache") and batch_size > 1
    # Single-slot prompt-cache: run B inline right after A (cache is hottest).
    inline_b = CONFIG.get("v2_prompt_cache") and batch_size <= 1
    b_futs = {}
    if interleave:
        b_pool = ThreadPoolExecutor(max_workers=batch_size)
    UI.set_phase(_book_key, "characters")
    _DIAG.viz_book_key = _book_key  # for viz_event in _roster_update
    for i, ch in indexed:
        if _CANCEL.is_set():
            UI.status(f"  Cancelled during chapter {i}/{n}")
            if interleave:
                b_pool.shutdown(wait=False, cancel_futures=True)
            UI.book_done(_book_key)
            print("  Checkpoint saved. Run with --resume to continue.")
            return False
        if i in _fm_idx_v2:
            # No Call A/B for front/back matter. Advance the work units
            # this chapter would have consumed in the current mode (in
            # sequential mode Pass 2 advances the B unit via _skipped_b,
            # since chapter_bs is pre-seeded above).
            UI.advance(_book_key, 2 if (interleave or inline_b) else 1)
            continue
        if _ckpt_complete(i):
            continue  # resumed: A+B already done, roster restored
        _saved_a, _saved_b = _resumed_chapters.get(i, (None, None))
        if _saved_a is not None:
            # A already ran (roster already includes it via the restored
            # roster) — reuse the saved result, do NOT re-run _roster_update.
            a, fell_back = _saved_a, False
            chapter_as[i] = a
            a_ok += 1
            UI.advance(_book_key)  # A unit already done
        else:
            UI.viz_chapter(_book_key, ch["label"], ch["text"],
                           chap_idx=i, chap_total=n)
            a, fell_back = v2_call_a(call, roster, ch, n)
            chapter_as[i] = a
            if a is None:
                a_failed += 1
            else:
                a_ok += 1
                if fell_back:
                    a_fallback += 1
            UI.viz_characters(_book_key, a.get("characters", []) if a else [])
            UI.advance(_book_key)
            _checkpoint_save_chapter(_ckpt_key, i, a, _saved_b)
        _b_already = _saved_b is not None or i in chapter_bs
        if interleave:
            if not _b_already:
                b_futs[b_pool.submit(v2_call_b, call, ch, n)] = i
            else:
                if _saved_b is not None and i not in chapter_bs:
                    chapter_bs[i] = _saved_b
                    b_ok += 1
                    UI.advance(_book_key)
        elif inline_b:
            if _b_already:
                if _saved_b is not None and i not in chapter_bs:
                    chapter_bs[i] = _saved_b
                    b_ok += 1
                    UI.advance(_book_key)
            else:
                b = v2_call_b(call, ch, n)
                chapter_bs[i] = b
                if b is None:
                    b_failed += 1
                else:
                    b_ok += 1
                UI.advance(_book_key)  # B unit (A already advanced above)
                _checkpoint_save_chapter(_ckpt_key, i, a, b)
        _push_matrix_viz_ui(chapter_as, chapter_bs)
    # Roster is complete after Pass 1 (all Call A done) — checkpoint it so a
    # resume doesn't need to replay _roster_update (which isn't idempotent).
    _checkpoint_save_roster(_ckpt_key, roster)
    if interleave:
        done_count = 0
        for fut in as_completed(b_futs):
            if _CANCEL.is_set():
                for f in b_futs:
                    f.cancel()
                b_pool.shutdown(wait=False, cancel_futures=True)
                UI.book_done(_book_key)
                print("  Checkpoint saved. Run with --resume to continue.")
                return False
            i = b_futs[fut]
            b = fut.result()
            chapter_bs[i] = b
            done_count += 1
            if b is None:
                b_failed += 1
            else:
                b_ok += 1
            UI.advance(_book_key)
            _checkpoint_save_chapter(_ckpt_key, i, chapter_as.get(i), b)
            _push_matrix_viz_ui(chapter_as, chapter_bs)
        b_pool.shutdown()

    # Pass 2: Call B parallel (independent per chapter; skipped if interleaved).
    # Resumed chapters with a saved B result skip their LLM call.
    if not interleave and not inline_b:
        UI.set_phase(_book_key, "content")
        _pass2_indexed = [(i, ch) for i, ch in indexed if i not in chapter_bs]
        _skipped_b = len(indexed) - len(_pass2_indexed)
        if _skipped_b:
            UI.advance(_book_key, _skipped_b)  # B units already done (resumed)
        if batch_size > 1:
            with ThreadPoolExecutor(max_workers=batch_size) as ex:
                futs = {ex.submit(v2_call_b, call, ch, n): i
                        for i, ch in _pass2_indexed}
                done_count = 0
                for fut in as_completed(futs):
                    if _CANCEL.is_set():
                        # Cancel remaining futures; partial results discarded
                        for f in futs:
                            f.cancel()
                        UI.book_done(_book_key)
                        print("  Checkpoint saved. Run with --resume to continue.")
                        return False
                    i = futs[fut]
                    b = fut.result()
                    chapter_bs[i] = b
                    done_count += 1
                    if b is None:
                        b_failed += 1
                    else:
                        b_ok += 1
                    UI.advance(_book_key)
                    _checkpoint_save_chapter(_ckpt_key, i, chapter_as.get(i), b)
                    _push_matrix_viz_ui(chapter_as, chapter_bs)
        else:
            UI.set_phase(_book_key, "content")
            for i, ch in _pass2_indexed:
                if _CANCEL.is_set():
                    UI.book_done(_book_key)
                    print("  Checkpoint saved. Run with --resume to continue.")
                    return False
                b = v2_call_b(call, ch, n)
                chapter_bs[i] = b
                if b is None:
                    b_failed += 1
                else:
                    b_ok += 1
                UI.advance(_book_key)
                _checkpoint_save_chapter(_ckpt_key, i, chapter_as.get(i), b)
                _push_matrix_viz_ui(chapter_as, chapter_bs)

    failed = a_failed + b_failed
    aborted = (a_ok == 0 and b_ok == 0) or failed / max(2 * n, 1) > MAX_FAILED_CHUNK_RATIO
    if aborted:
        print(f"  {failed}/{2 * n} calls failed; not writing partial results")

    # Roster reconciliation (Experiment 1, --roster-reconcile): Call A
    # built the roster chapter by chapter against partial information;
    # canonicalize it once with the whole book sighted, before the
    # post-pass merge and the reduce consume it. Deterministic.
    _recon_stats = None
    if CONFIG.get("roster_reconcile"):
        _recon_stats = _roster_reconcile(roster)
        print(f"  Roster reconcile: {_recon_stats['entries_before']} -> "
              f"{_recon_stats['entries_after']} entries "
              f"({_recon_stats['variants_merged']} variants merged, "
              f"{_recon_stats['aliases_pruned']} polluted aliases pruned)")

    # Post-pass character merge (Tier 1/2 deterministic + Tier 3 decision
    # model). Runs after all chapters, before the reduce, so v2_reduce sees
    # the final merged roster and relationships canonical-remap correctly.
    _pp_merged, _pp_possible = post_pass_merge(
        roster, validator=_DECISION_VALIDATOR)
    if _pp_merged:
        print(f"  Post-pass merge: {_pp_merged} roster entries merged")
    if _pp_possible:
        print(f"  Post-pass merge: {len(_pp_possible)} ambiguous pairs "
              f"left for review")

    # Deterministic reduce (no LLM). n_chapters_total is the FULL
    # chapter list's length: `indexed` may be a --chunks subset, but
    # chapter_bs keys (and the spice array) live in original indices.
    red = v2_reduce(chapter_as, chapter_bs, roster,
                    [ch for _, ch in indexed], preview=preview,
                    show_matrices=CONFIG.get("show_matrices", False),
                    n_chapters_total=len(chapters))

    # Trope confirmation: frequency tiers, LLM gate only for the borderline.
    # Thresholds scale with book length (5% of chapters, min 3):
    # - >= auto_min chapters: auto-confirm (a recurring pattern is a trope).
    # - 2 to auto_min-1 chapters: LLM gate judges against summaries.
    # - 1 chapter: drop in long books (not a book-level trope).
    tropes, trope_conf = [], {}
    _sb_auto, _sb_gated = [], []  # staging observe inputs (Option F)
    if not dry_run and red["trope_candidates"]:
        counts = red["trope_candidate_counts"]
        # Scale thresholds with book length: 5% of chapters, min 3.
        # (e.g. 73-chapter book -> 3; 100-chapter book -> 5.)
        auto_min = max(3, n // 20)  # 5% of chapters, min 3
        drop_single = n >= 20  # only drop single-chapter tropes in long books
        auto, gated, dropped = [], [], 0
        # Build chapter lists per trope for evidence.
        # Prefer the display-keyed map from v2_reduce (handles paraphrases);
        # fall back to raw-text keys only if the reduce didn't build one
        # (e.g. no embed_url, so no catalog mapping ran).
        if red.get("trope_chapters"):
            trope_chapters = red["trope_chapters"]
        else:
            trope_chapters = {}
            for idx in sorted(chapter_bs):
                b = chapter_bs[idx]
                if not b:
                    continue
                seen_ch = set()
                for tc in b.get("trope_candidates", []):
                    key = _tnorm(tc)
                    if key not in seen_ch:
                        seen_ch.add(key)
                        trope_chapters.setdefault(key, []).append(idx)
            red["trope_chapters"] = trope_chapters
        _denied = 0
        _rej_denied = 0
        for c in red["trope_candidates"]:
            # Drop genre/setting labels (not tropes)
            if _is_denylisted_trope(c):
                _denied += 1
                continue
            # Drop tropes humans have rejected 3+ times (rejection denylist)
            if _learn().is_trope_denylisted(_trej_key(c)):
                _denied += 1
                _rej_denied += 1
                continue
            n_ch = counts.get(_tnorm(c), 1)
            if n_ch >= auto_min:
                auto.append(c)
            elif n_ch >= 2 or not drop_single:
                gated.append(c)
            else:
                dropped += 1
        print(f"  Tropes: {len(auto)} auto ({auto_min}+ ch), {len(gated)} to gate, "
              f"{dropped} single-ch dropped, {_denied} denylisted...", end=" ", flush=True)
        _sb_auto, _sb_gated = list(auto), list(gated)
        for c in auto:
            tropes.append(c)
            trope_conf[_tnorm(c)] = round(
                min(0.9, 0.55 + 0.1 * counts.get(_tnorm(c), auto_min)), 2)
        if gated:
            g_tropes, g_conf = v2_trope_gate(
                call, red["chapter_summaries"], gated, counts)
            tropes.extend(g_tropes)
            trope_conf.update(g_conf)
        # Rejection learning: human rejections raise the acceptance bar for
        # this trope (0.10 per rejection); 3+ rejections already denylisted.
        _rej_filtered = 0
        if tropes:
            tropes, trope_conf, _rej_filtered = _apply_trope_rejections(
                tropes, trope_conf, _learn())
        _rej_total = _rej_denied + _rej_filtered
        rej_note = f", {_rej_total} rejected" if _rej_total else ""
        print(f"{len(tropes)} confirmed{rej_note}")
    red["tropes"] = tropes
    red["trope_confidence"] = trope_conf

    # Option F staging seam (design section 5): ONE split point,
    # after the trope gate + rejection learning and the trigger
    # finalization in v2_reduce, before result assembly. Flag off
    # (or dry-run, or an aborted run): skipped entirely — no store
    # access, no marker, byte-identical behaviour. Flag on: observe
    # the run's stageable claims and rewrite tropes/triggers to the
    # ship set here; write_claims, plan_replace, save_preview and
    # push-preview all inherit the split from the result.
    _staging_marker = None
    _trope_persistence = {}
    _trigger_persistence = {}
    if CONFIG.get("staging_gate") and not dry_run and not aborted:
        _st = _staging()
        if _st.enabled and _st.writable:
            _sg = staging_observe_result({
                "work_key": staging_work_key(
                    None, identifiers.get("isbn"),
                    title or fpath.stem, author),
                "work_id": None, "title": title or fpath.stem,
                "author": author, "isbn": identifiers.get("isbn"),
                "auto_min": max(3, n // 20),
                "trope_auto": _sb_auto, "trope_gated": _sb_gated,
                "trope_counts": red.get("trope_candidate_counts")
                                or {},
                "trope_ship": tropes, "trope_conf": trope_conf,
                "catalog_map": red.get("trope_catalog_map") or {},
                "triggers": red["triggers"],
            }, _st, independent=not _resumed_chapters)
            tropes = _sg["ship_tropes"]
            trope_conf = _sg["trope_conf"]
            red["tropes"] = tropes
            red["trope_confidence"] = trope_conf
            red["triggers"] = _sg["ship_triggers"]
            _staging_marker = _sg["marker"]
            _trope_persistence = _sg["trope_persistence"]
            _trigger_persistence = _sg["trigger_persistence"]
            print(f"  Staging: {len(_staging_marker['staged'])} held "
                  f"back, {len(_staging_marker['promoted'])} promoted, "
                  f"{_staging_marker['trapped_total']} trapped for "
                  f"this work")

    reading_mins = word_count // 250
    result = {
        "file": fpath.name, "title": title or fpath.stem, "author": author,
        "isbn": identifiers.get("isbn"), "asin": identifiers.get("asin"),
        "isbn_source": _isbn_source,  # "flag" | "epub" | "openlibrary" | "none"
        "series_name": _series_name, "series_position": _series_pos,
        "series_source": _series_source,  # "flag"|"epub"|"filename"|"openlibrary"|"none"
        "word_count": word_count, "reading_time_mins": reading_mins,
        # Same A+B definition as the abort decision above: a persisted
        # A-only count showed zero failures for books whose content
        # calls mostly failed.
        "chunks": n, "chunks_failed": a_failed + b_failed,
        "pipeline": "v2",
        "tropes": tropes, "trope_confidence": trope_conf,
        "triggers": red["triggers"], "characters": red["characters"],
        "relationships": red["relationships"],
        "dropped_relationships": red.get("dropped_relationships", []),
        "self_pairs_dropped": red.get("self_pairs_dropped",
                                      {"extracted": 0, "remapped": 0}),
        "spice_level": red["spice_level"],
        # Reduce-computed fields the consumers read from the RESULT
        # (not from red): --recalibrate-spice needs chapter_spice +
        # chapters_successful, _preview_summary needs the peak/freq
        # bands, save_preview needs avg_dialogue_ratio. Forwarding
        # none of them left recalibrate permanently at no_data and
        # the summary claiming "(no spicy content)" for spicy books.
        "spice_peak": red["spice_peak"],
        "spice_peak_band": red["spice_peak_band"],
        "spice_freq_band": red["spice_freq_band"],
        "chapter_spice": red["chapter_spice"],
        "chapters_successful": red["chapters_successful"],
        "trigger_chapters": red["trigger_chapters"],
        "avg_dialogue_ratio": red["avg_dialogue_ratio"],
        "povs": red["povs"], "quotes": red["quotes"],
        "chapter_summaries": red["chapter_summaries"],
        "is_anthology": False, "stories": [],
        "aborted": aborted, "chunks_ok": a_ok,
        "tasks": {"discipline": b_ok, "identity": a_ok,
                  "identity_simple": a_fallback},
        "verification": red["verification"],
        "roster_size": len(roster),
        # Saved for future remapping (--trope-map can retry these later).
        "trope_candidates": red.get("trope_candidates", []),
        "trope_candidate_counts": red.get("trope_candidate_counts", {}),
        "trope_catalog_map": red.get("trope_catalog_map", {}),
        "trope_chapters": red.get("trope_chapters", {}),
        "possible_merges": _pp_possible,
        "post_pass_merged": _pp_merged,
        "chapter_detection": "fallback" if _chap_fallback else "spine",
        # P8/P9: per-chapter Call A/B results + roster, for --resume and
        # --retry-failed. Keys are str(idx) for JSON stability.
        "chapter_data": {
            "chapters": {str(i): {"a": _ckpt_json_safe(chapter_as.get(i)),
                                  "b": _ckpt_json_safe(chapter_bs.get(i))}
                         for i, _ch in indexed},
            "roster": _ckpt_json_safe(roster),
        },
    }
    if _recon_stats is not None:
        result["roster_reconcile"] = _recon_stats
    if _staging_marker is not None:
        result["staging"] = _staging_marker
    if _trope_persistence:
        result["trope_persistence"] = _trope_persistence
    if _trigger_persistence:
        result["trigger_persistence"] = _trigger_persistence
    v = red["verification"]
    if v["evidence_checked"]:
        print(f"  Evidence: {v['evidence_verified']}/{v['evidence_checked']} verified")

    # Metadata correction via OpenLibrary (one call per book; skipped in preview/dry-run).
    if not (preview or dry_run):
        title, author, _corrected = correct_metadata(title or fpath.stem, author, identifiers)
        result["title"] = title
        result["author"] = author
    # Work identity resolution (read-only in preview/dry-run).
    wid, how = resolve_work(title or fpath.stem, author, identifiers, False,
                            create=not (preview or dry_run))
    result["work_id"] = wid
    result["work_resolution"] = how
    if _staging_marker is not None and wid:
        # OC-8: the observe seam ran before the work_id existed, so
        # this run's entries sit under a fallback key; adopt them
        # into the resolved work's entry now that the id is known.
        _staging().adopt_for(wid, result.get("title") or "",
                             result.get("author") or "",
                             identifiers.get("isbn"))
    if how == "new":
        result["work_candidates"] = _title_candidates(title or fpath.stem)
        print("  Work: no match — NEW WORK would be created")
    elif wid:
        print(f"  Work: matched via {how} -> {wid[:8]}")
    else:
        print(f"  Work: resolution failed ({how})")

    smsg = f" ({a_fallback} via simple fallback)" if a_fallback else ""
    UI.book_done(_book_key)
    _gr = red.get("grounding", {})
    _g_chars = f"{_gr.get('characters_grounded', '?')}/{_gr.get('characters_total', '?')}"
    _g_rels = f"{_gr.get('relationships_grounded', '?')}/{_gr.get('relationships_total', '?')}"
    UI.summary(f"📖 {title or fpath.stem}", [
        ("Tropes", f"{len(tropes)} confirmed"),
        ("Triggers", len(red['triggers'])),
        ("Characters", f"{len(red['characters'])} ({len(roster)} roster)"),
        ("Grounded", f"chars {_g_chars}, rels {_g_rels}"),
        ("Tasks", f"content {b_ok}/{n} ok, identity {a_ok}/{n} ok{smsg}"),
        ("Spice", f"{red['spice_level']}/5"),
        ("POVs", ', '.join(red['povs']) or 'none'),
        ("Reading time", f"~{reading_mins} min"),
    ])
    _ung = _gr.get("ungrounded_names", [])
    if _ung:
        UI.status(f"  ⚠ Ungrounded characters (name never appears in text): {', '.join(_ung[:10])}"
                  + (f" (+{len(_ung)-10} more)" if len(_ung) > 10 else ""))

    if CONFIG.get("dedupe") and result["characters"]:
        n_before = len(result["characters"])
        try:
            d_chars, d_rels, d_povs, dreport = dedupe_characters(
                result["characters"], result["relationships"], result["povs"])
        except Exception as e:
            print(f"  Dedup failed ({e}); not writing partial results")
            return False
        result["characters"], result["relationships"], result["povs"] = \
            d_chars, d_rels, d_povs
        result["dedup_merges"] = dreport
        print(f"  Dedup: {n_before} -> {len(d_chars)} characters "
              f"({len(dreport)} clusters merged)")

    # Principal filter (default on): keep only significant characters.
    # Non-principals are preserved in result["minor_characters"] for
    # reference; only principals go to Supabase. The series roster below
    # still receives the FULL list (a minor here may recur later).
    _full_characters = result["characters"]
    _full_relationships = result["relationships"]
    result["minor_characters"] = []
    if CONFIG.get("principals_only", True):
        _min_freq = CONFIG.get("min_frequency", 0.20)
        _princs, _minors, _prels = _filter_principals(
            _full_characters, _full_relationships, _min_freq)
        result["characters"] = _princs
        result["relationships"] = _prels
        result["minor_characters"] = _minors
        print(f"  Principals: {len(_princs)}/{len(_full_characters)} characters, "
              f"{len(_prels)}/{len(_full_relationships)} relationships "
              f"(min_frequency={_min_freq})")
        if CONFIG.get("show_matrices"):
            print(_char_importance_ascii(_princs, _minors))

    # Ghost-endpoint relationships: a claim can name a person who has
    # no character row in the final set (with the principals filter
    # off, nothing else removes those). Account for them in
    # dropped_relationships instead of letting preview and DB diverge
    # silently (see _drop_ghost_relationships).
    _n_ghost = _drop_ghost_relationships(result)
    if _n_ghost:
        print(f"  Relationships: dropped {_n_ghost} with an endpoint "
              f"outside the character set (ghost_endpoint)")

    # Cross-run learning: persist series roster + learned state. Runs in
    # preview/dry-run too (learning from a preview is the point).
    _ln_end = _learn()
    if _ln_end.enabled:
        _skey = getattr(_DIAG, "learn_series_key", None)
        if _skey:
            _ln_end.save_series_roster(
                _skey, title, author,
                getattr(_DIAG, "learn_isbn", None),
                _full_characters)
        # Named-series roster (in addition to author roster): cross-book
        # character continuity within a detected series.
        _snkey = getattr(_DIAG, "learn_series_name_key", None)
        if _snkey:
            _ln_end.save_series_name_roster(
                _snkey, title, author,
                getattr(_DIAG, "learn_isbn", None),
                _series_pos, _full_characters)
        _ln_end.save()
        _lsummary = _ln_end.pop_summary()
        if _lsummary:
            print(f"  {_lsummary}")

    if dry_run:
        print("  DRY RUN — nothing written")
        UI.run_summary(title or fpath.stem,
                       _run_summary_stats(result, True, time.time() - _t0))
        return not aborted
    if preview:
        save_preview(fpath, result)
        print(f"  Preview saved (pipeline=v2)")
        if wid:
            # Replace-wholesale validation gate: log what a write run
            # would replace for this in-DB work (read-only).
            print_replace_plan(wid, result)
        _checkpoint_clear(_ckpt_key)  # P8: run complete, checkpoints unneeded
        UI.run_summary(title or fpath.stem,
                       _run_summary_stats(result, True, time.time() - _t0))
        return not aborted
    # Write mode: still save the preview JSON as an audit trail.
    save_preview(fpath, result)
    print(f"  Preview saved (audit trail, pipeline=v2)")
    if aborted:
        print("  ABORTED — not writing partial results")
        return False
    if not wid:
        print(f"  Could not find or create work ({how})")
        return False
    print(f"  Work ID: {wid}")
    errors = write_claims(wid, result)
    if errors:
        print(f"  {errors} database operation(s) failed")
        return False
    _checkpoint_clear(_ckpt_key)  # P8: run complete, checkpoints unneeded
    UI.run_summary(title or fpath.stem,
                   _run_summary_stats(result, False, time.time() - _t0))
    return True


def _run_summary_stats(result, preview, elapsed_sec):
    """P7: build stats dict for UI.run_summary from a v2 result dict."""
    rels = result.get("relationships", []) or []
    dropped = result.get("dropped_relationships", []) or []
    conf = {"high": 0, "medium": 0, "low": 0}
    val = {"kept": 0, "downgraded": 0, "dropped": 0}
    for r in rels:
        c = (r.get("confidence") or "").lower()
        if c in conf:
            conf[c] += 1
        vn = (r.get("validation_note") or "").lower()
        if vn in ("kept",):
            val["kept"] += 1
        elif vn in ("downgraded", "would_downgrade"):
            val["downgraded"] += 1
    for r in dropped:
        val["dropped"] += 1
    return {
        "principals": len(result.get("characters", []) or []),
        "minors": len(result.get("minor_characters", []) or []),
        "rels_total": len(rels),
        "rels_high": conf["high"],
        "rels_medium": conf["medium"],
        "rels_low": conf["low"],
        "val_kept": val["kept"],
        "val_downgraded": val["downgraded"],
        "val_dropped": val["dropped"],
        "spice": result.get("spice_level", 0) or 0,
        "wrote": getattr(_DIAG, "wrote_stats", None),
        "elapsed_sec": elapsed_sec,
        "preview": bool(preview),
    }


def process_file(fpath, dry_run=False, preview=False):
    if CONFIG.get("pipeline") == "v2":
        return process_file_v2(fpath, dry_run, preview)
    _t0 = time.time()
    _book_key = str(fpath)
    UI.status(f"\nProcessing: {fpath.name}")
    global _DEBUG_TAG
    _DEBUG_TAG = fpath.stem if (preview or dry_run) and CONFIG.get("debug") else None
    extracted = read_ebook(fpath)
    if extracted is None:
        return False
    text, title, author, identifiers = extracted

    UI.status(f"  Title: {title or fpath.stem}, Chars: {len(text):,}")
    # Author filter: the book's author is never a character. Stashed on
    # _DIAG for the v1 chunk loop below (same pattern as process_file_v2).
    _DIAG.book_author = author
    # ISBN resolution: --isbn flag > EPUB metadata > Open Library lookup.
    # Default is to skip books with no ISBN (see --allow-no-isbn).
    _isbn, _isbn_source = _resolve_book_isbn(identifiers, title, author)
    if _isbn_source == "none" and not _handle_missing_isbn(
            fpath, preview=preview, dry_run=dry_run):
        return False
    _series_name, _series_pos, _series_source = _detect_series(
        fpath, identifiers, title, author)
    if identifiers.get("isbn"):
        print(f"  ISBN: {identifiers['isbn']}")
    elif identifiers.get("asin"):
        print(f"  ASIN: {identifiers['asin']} (no ISBN found)")
    if len(text) < 1000:
        print("  Too short")
        return False

    all_chunks = chunk_text(text)
    n_total = len(all_chunks)
    word_count = len(text.split())

    # Smart sampling: always take edges, sample the middle
    sample_rate = CONFIG.get("sample_rate", SAMPLE_RATE)
    edge_n = CONFIG.get("sample_edges", SAMPLE_EDGES)
    if sample_rate > 1 and n_total > edge_n * 2 + 4:
        indices = list(range(edge_n))  # first N
        indices += list(range(n_total - edge_n, n_total))  # last N
        # Sample middle
        mid_start, mid_end = edge_n, n_total - edge_n
        indices += list(range(mid_start, mid_end, sample_rate))
        indices = sorted(set(indices))
        chunks = [all_chunks[i] for i in indices]
        print(f"  {n_total} chunks total, sampling {len(chunks)} (rate=1/{sample_rate}, edges={edge_n})")
    else:
        chunks = all_chunks
        indices = list(range(n_total))
    n = len(chunks)
    # --chunks: restrict to specific chunk indices (1-based, as numbered in
    # debug files). Applied after sampling; original numbering is kept so
    # debug output stays comparable across runs.
    only = CONFIG.get("only_chunks")
    indexed = _select_chunks(chunks, only)
    if only:
        if not indexed:
            print(f"  --chunks {only}: no chunks match (1-{n})")
            return False
        print(f"  {n} chunks, selecting {len(indexed)} (--chunks {only}), "
              f"LLM: {CONFIG['llm']}")
    else:
        UI.status(f"  {n} chunks, LLM: {CONFIG['llm']}")
    n = len(indexed)
    UI.book_start(_book_key, f"\U0001f4d6 {title or fpath.stem}", total=n)

    trope_hits, trope_name = Counter(), {}
    trig_hits, trig_data = Counter(), {}
    char_hits, char_data = Counter(), {}
    rel_seen, rel_idx, relationships = set(), {}, []
    povs, quotes, spice_votes = set(), [], []
    is_anth, stories = False, []
    ok = failed = 0
    v_checked = v_verified = v_dropped = 0
    task_ok = {"discipline": 0, "identity": 0, "identity_simple": 0}

    batch_size = effective_batch_size()

    def _run_chunk(args):
        idx, ch = args
        return (idx, extract_llm(ch, idx, n))

    # Collect results (parallel when batch_size > 1)
    results = {}
    if batch_size > 1:
        with ThreadPoolExecutor(max_workers=batch_size) as ex:
            futs = {ex.submit(_run_chunk, (i, ch)): i for i, ch in indexed}
            done_count = 0
            for fut in as_completed(futs):
                idx, r = fut.result()
                results[idx] = r
                done_count += 1
                UI.advance(_book_key)
    else:
        for i, ch in indexed:
            if _CANCEL.is_set():
                UI.status(f"  Cancelled during chunk {i}/{n}")
                UI.book_done(_book_key)
                return False
            _, r = _run_chunk((i, ch))
            results[i] = r
            UI.advance(_book_key)

    for i, _ch in indexed:
        r = results.get(i)
        if r is None:
            failed += 1
            continue
        ok += 1
        ts = r.get("task_status", {})
        if ts.get("discipline") == "ok":
            task_ok["discipline"] += 1
        if ts.get("identity") == "ok":
            task_ok["identity"] += 1
            if ts.get("identity_simple"):
                task_ok["identity_simple"] += 1
        for t in {_tnorm(x): x for x in r["tropes"]}.items():
            trope_hits[t[0]] += 1
            trope_name.setdefault(t[0], t[1])
        for t in r["triggers"]:
            k = t["warning"].lower()
            trig_hits[k] += 1
            trig_data.setdefault(k, t)
        for c in r["characters"]:
            k = c["name"].lower()
            char_hits[k] += 1
            if k not in char_data:
                char_data[k] = c
            else:
                # Keep the first VERIFIED evidence: upgrade when the stored
                # entry's evidence failed verification but this one passed.
                ex = char_data[k]
                if not ex.get("evidence_verified") and c.get("evidence_verified"):
                    ex["evidence"] = c["evidence"]
                    ex["evidence_verified"] = True
        for rel in r["relationships"]:
            # Author filter: the book's author is never a character, so
            # relationships naming the author are dropped.
            if _rel_involves_author(rel, author):
                continue
            # Canonical form (direction convention, see _canonical_pair):
            # flipped duplicates of the same pair dedupe together and
            # self-pairs are dropped — _canonical_pair returns None for a
            # self-pair, which is the field-leakage self-relationship
            # guard's drop (same backstop as v2_reduce and the dedupe
            # remap), preserved here.
            _cp = _canonical_pair(rel["from"], rel["to"], rel["type"])
            if _cp is None:
                continue
            rel = dict(rel, **{"from": _cp[0], "to": _cp[1], "type": _cp[2]})
            k = (rel["from"].lower(), rel["to"].lower(), rel["type"].lower())
            if k not in rel_seen:
                rel_seen.add(k)
                rel_idx[k] = len(relationships)
                relationships.append(rel)
            else:
                ex = relationships[rel_idx[k]]
                if not ex.get("evidence_verified") and rel.get("evidence_verified"):
                    ex["evidence"] = rel["evidence"]
                    ex["evidence_verified"] = True
        v = r.get("verification", {})
        v_checked += v.get("evidence_checked", 0)
        v_verified += v.get("evidence_verified", 0)
        v_dropped += v.get("quotes_dropped", 0)
        povs.update(r["povs"])
        quotes.extend(r["quotes"][:3])
        if r["spice_level"] is not None:
            spice_votes.append(r["spice_level"])
        if r["is_anthology"]:
            is_anth = True
            stories.extend(s for s in r["stories"] if s not in stories)
        print("done")

    aborted = ok == 0 or failed / n > MAX_FAILED_CHUNK_RATIO
    if aborted:
        print(f"  {failed}/{n} chunks failed; not writing partial results")

    # Aggregate. Require 2+ chunk hits for tropes/triggers on longer books to cut one-off noise.
    min_hits = 2 if ok >= 4 else 1
    tropes = sorted(_clean_trope(trope_name[k]) for k, h in trope_hits.items() if h >= min_hits)
    trope_conf = {k: _confidence(h, ok) for k, h in trope_hits.items() if h >= min_hits}
    # POV labels are not tropes (same filter as the v2 candidate path).
    _pov_dropped = [t for t in tropes if _is_pov_trope_label(t)]
    if _pov_dropped:
        tropes = [t for t in tropes if not _is_pov_trope_label(t)]
        trope_conf = {k: v for k, v in trope_conf.items()
                      if not _is_pov_trope_label(k)}
        print(f"  Trope filter: dropped {len(_pov_dropped)} POV label(s)")
    # Rejection learning (same as v2): denylist + raised thresholds.
    if tropes:
        tropes, trope_conf, _v1_rej = _apply_trope_rejections(
            tropes, trope_conf, _learn())
        if _v1_rej:
            print(f"  Tropes: {_v1_rej} filtered by rejection learning")
    triggers = [dict(trig_data[k], confidence=_confidence(h, ok))
                for k, h in trig_hits.items() if h >= min_hits]
    characters = [dict(char_data[k], confidence=_confidence(h, ok))
                  for k, h in char_hits.most_common()]
    votes = sorted(spice_votes)
    # No known chapter levels -> the book level is unknown (None),
    # not a measured 0; write_claims skips the spice overwrite on None.
    spice_level = votes[int(0.75 * (len(votes) - 1))] if votes else None  # 75th percentile
    reading_mins = word_count // 250

    result = {
        "file": fpath.name, "title": title or fpath.stem, "author": author,
        "isbn": identifiers.get("isbn"), "asin": identifiers.get("asin"),
        "isbn_source": _isbn_source,  # "flag" | "epub" | "openlibrary" | "none"
        "series_name": _series_name, "series_position": _series_pos,
        "series_source": _series_source,
        "word_count": word_count, "reading_time_mins": reading_mins,
        "chunks": n, "chunks_failed": failed,
        "tropes": tropes, "trope_confidence": trope_conf,
        "triggers": triggers, "characters": characters,
        "relationships": relationships, "dropped_relationships": [],
        "spice_level": spice_level,
        "povs": sorted(povs), "quotes": _spread_quotes(quotes, 10),
        "is_anthology": is_anth, "stories": stories,
        "aborted": aborted, "chunks_ok": ok,
        "tasks": task_ok,
    }

    # Phase 1b: evidence was verified per-chunk in extract_llm; aggregate stats.
    vstats = {"evidence_checked": v_checked, "evidence_verified": v_verified,
              "quotes_dropped": v_dropped}
    result["verification"] = vstats
    if v_checked:
        print(f"  Evidence: {v_verified}/{v_checked} verified"
              + (f", {v_dropped} quotes dropped" if v_dropped else ""))

    # Metadata correction via OpenLibrary (one call per book; skipped in preview/dry-run).
    if not (preview or dry_run):
        title, author, _corrected = correct_metadata(title or fpath.stem, author, identifiers)
        result["title"] = title
        result["author"] = author
    # Work identity resolution (read-only in preview/dry-run).
    wid, how = resolve_work(title or fpath.stem, author, identifiers, is_anth,
                            create=not (preview or dry_run))
    result["work_id"] = wid
    result["work_resolution"] = how
    if how == "new":
        result["work_candidates"] = _title_candidates(title or fpath.stem)
        print("  Work: no match — NEW WORK would be created")
        for c in result["work_candidates"]:
            print(f"    ? possible duplicate: {c['title']} "
                  f"({', '.join(c.get('authors') or [])}) [{c['id'][:8]}]")
    elif wid:
        print(f"  Work: matched via {how} -> {wid[:8]}")
    else:
        print(f"  Work: resolution failed ({how})")

    _elapsed = time.time() - _t0
    smsg = f" ({task_ok['identity_simple']} via simple fallback)" if task_ok["identity_simple"] else ""
    UI.book_done(_book_key)
    UI.summary(f"\U0001f4d6 {title or fpath.stem}", [
        ("Tropes", f"{len(tropes)} confirmed"),
        ("Triggers", len(triggers)),
        ("Characters", len(characters)),
        ("Tasks", f"discipline {task_ok['discipline']}/{n} ok, identity {task_ok['identity']}/{n} ok{smsg}"),
        ("Spice", f"{spice_level}/5"),
        ("POVs", ', '.join(sorted(povs)) or 'none'),
        ("Reading time", f"~{reading_mins} min"),
        ("Elapsed", f"{_elapsed/60:.1f} min ({_elapsed/n:.1f}s/chunk)"),
    ])

    if CONFIG.get("dedupe") and result["characters"]:
        n_before = len(result["characters"])
        try:
            d_chars, d_rels, d_povs, dreport = dedupe_characters(
                result["characters"], result["relationships"], result["povs"])
        except Exception as e:
            print(f"  Dedup failed ({e}); not writing partial results")
            return False
        result["characters"], result["relationships"], result["povs"] = \
            d_chars, d_rels, d_povs
        result["dedup_merges"] = dreport
        print(f"  Dedup: {n_before} -> {len(d_chars)} characters "
              f"({len(dreport)} clusters merged)")
        for m in dreport:
            print(f"    = {m['canonical']} <- {', '.join(m['merged'])}")

    # Principal filter (default on): keep only significant characters.
    # Non-principals are preserved in result["minor_characters"] for reference.
    result["minor_characters"] = []
    if CONFIG.get("principals_only", True):
        _min_freq = CONFIG.get("min_frequency", 0.20)
        _princs, _minors, _prels = _filter_principals(
            result["characters"], result["relationships"], _min_freq)
        print(f"  Principals: {len(_princs)}/{len(result['characters'])} characters, "
              f"{len(_prels)}/{len(result['relationships'])} relationships "
              f"(min_frequency={_min_freq})")
        if CONFIG.get("show_matrices"):
            print(_char_importance_ascii(_princs, _minors))
        result["characters"] = _princs
        result["relationships"] = _prels
        result["minor_characters"] = _minors
    if is_anth:
        print(f"  Anthology with {len(stories)} stories")
    if failed:
        print(f"  Note: {failed}/{n} chunks failed and were skipped")

    if preview:
        save_preview(fpath, result)
        if wid:
            # Replace-wholesale validation gate (read-only; see v2 path).
            print_replace_plan(wid, result)
        return not aborted
    if dry_run:
        print("  [DRY] nothing written")
        return not aborted

    if aborted:
        # Never write partial results to the database.
        return False
    # wid/how were resolved above with create=True.
    if not wid:
        print(f"  Could not find or create work ({how})")
        return False
    print(f"  Work ID: {wid}")
    errors = write_claims(wid, result)
    if errors:
        print(f"  {errors} database operation(s) failed")
        return False
    UI.run_summary(title or fpath.stem,
                   _run_summary_stats(result, False, time.time() - _t0))
    return True


def save_preview(fpath, result):
    """Save human-readable preview to preview/ folder."""
    prev_dir = SCRIPT_DIR / "preview"
    prev_dir.mkdir(exist_ok=True)
    base = fpath.stem + CONFIG.get("preview_name_suffix", "")

    with open(prev_dir / f"{base}.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    md = [f"# {result['title']}", f"*{result['author'] or 'Unknown author'}*", ""]
    if result.get("aborted"):
        md.append(f"## ⚠️ ABORTED — partial results "
                  f"({result.get('chunks_failed', '?')}/{result.get('chunks', '?')} chunks failed)")
        md.append("")
    md.append(f"**Words:** {result['word_count']:,} (~{result['reading_time_mins']} min)")
    t = result.get("tasks", {})
    if t:
        md.append(f"**Tasks:** discipline {t.get('discipline', '?')}/{result.get('chunks', '?')} ok, "
                  f"identity {t.get('identity', '?')}/{result.get('chunks', '?')} ok")
    _spice_lvl = result.get("spice_level")
    md.append(f"**Spice:** {'🌶️' * _spice_lvl if _spice_lvl else 'None'} "
              f"({_spice_lvl if _spice_lvl is not None else '?'}/5)")
    _adr = result.get("avg_dialogue_ratio")
    if _adr is not None:
        md.append(f"**Dialogue:** {int(_adr * 100)}% of text is dialogue")
    md.append(f"**POVs:** {', '.join(result['povs']) or 'Unknown'}")
    if result.get("isbn"):
        _src = result.get("isbn_source", "")
        _tag = {"flag": " (from --isbn override)",
                "openlibrary": " (auto-resolved via Open Library)"}.get(_src, "")
        md.append(f"**ISBN:** {result['isbn']}{_tag}")
    elif result.get("asin"):
        md.append(f"**ASIN:** {result['asin']} (no ISBN in file)")
    else:
        md.append("**ISBN:** not found — work matched by title/author only")
    if result.get("series_name"):
        _spos = result.get("series_position")
        _spos_str = f" #{_spos:g}" if _spos is not None else ""
        _ssrc = result.get("series_source", "")
        _ssrc_tag = {"flag": " (--series override)", "epub": " (EPUB metadata)",
                     "filename": " (filename)", "openlibrary": " (Open Library)"}.get(_ssrc, "")
        md.append(f"**Series:** {result['series_name']}{_spos_str}{_ssrc_tag}")
    how = result.get("work_resolution", "?")
    if how == "new":
        md.append("**Work:** NO MATCH — new work would be created")
        for c in result.get("work_candidates", []):
            md.append(f"  - possible duplicate: {c['title']} "
                      f"({', '.join(c.get('authors') or [])})")
    elif result.get("work_id"):
        md.append(f"**Work:** matched via {how}")
    else:
        md.append(f"**Work:** resolution failed ({how})")
    if result.get("is_anthology"):
        md.append(f"**Anthology:** {len(result['stories'])} stories")
    v = result.get("verification") or {}
    if v.get("evidence_checked"):
        md.append(f"**Evidence:** {v['evidence_verified']}/{v['evidence_checked']} quotes verified"
                  + (f", {v['quotes_dropped']} fabricated quotes dropped"
                     if v.get("quotes_dropped") else ""))
    _stg = result.get("staging")
    if _stg:
        # Option F: the staging outcome is declared on the preview's
        # face — held-back claims, promotions WITH their mechanism,
        # and the sightings this preview recorded (OC-4). "Promoted"
        # always pairs with its via; a sighting is an observation,
        # never a verification.
        _prom = _stg.get("promoted") or []
        _vias = {}
        for _p in _prom:
            _v = _p.get("via") or "?"
            _vias[_v] = _vias.get(_v, 0) + 1
        if _prom:
            _prom_s = (f"{len(_prom)} promoted (" + ", ".join(
                f"via {v}" + (f" ×{c}" if c > 1 else "")
                for v, c in sorted(_vias.items())) + ")")
        else:
            _prom_s = "0 promoted"
        md.append(f"**Staging:** {len(_stg.get('staged') or [])} held "
                  f"back, {_prom_s}, "
                  f"{_stg.get('trapped_store_total', 0)} trapped across "
                  f"all works")
        _ns = _stg.get("sightings_recorded", 0)
        md.append(f"**Staging:** this preview recorded {_ns} "
                  f"sighting{'s' if _ns != 1 else ''} in the local "
                  f"staging store")
    md.append("")
    md.append(f"## Tropes ({len(result['tropes'])})")
    for t in result["tropes"]:
        md.append(f"- {t} ({result['trope_confidence'].get(_tnorm(t), '?')})")
    md.append("")
    md.append(f"## Trigger Warnings ({len(result['triggers'])})")
    for t in result["triggers"]:
        line = f"- **{t['warning']}**"
        if t.get("severity"):
            line += f" ({t['severity']}"
            if t.get("prominence"):
                line += f", {t['prominence']} prominence"
            line += ")"
        _tci = t.get("chapter_indices")
        if _tci:
            line += f" [chapters: {', '.join(str(i) for i in _tci)}]"
        if t.get("detail"):
            line += f": {t['detail']}"
        if t.get("spoiler"):
            line += " ⚠️ SPOILER"
        md.append(line)
    md.append("")
    if result.get("dedup_merges"):
        md.append(f"## Dedup merges ({len(result['dedup_merges'])})")
        for m in result["dedup_merges"]:
            md.append(f"- {m['canonical']} <- {', '.join(m['merged'])}")
        md.append("")
    if result.get("possible_merges"):
        md.append(f"## Possible merges for review ({len(result['possible_merges'])})")
        for m in result["possible_merges"]:
            _p = f" P(yes)={m['p_yes']:.2f}" if m.get("p_yes") is not None else ""
            md.append(f"- {m['name_a']} vs {m['name_b']}{_p} ({m['reason']})")
        md.append("")
    md.append(f"## Characters ({len(result['characters'])})")
    for c in result["characters"]:
        md.append(f"### {c['name']} ({c.get('role') or 'unknown'})")
        _fa = c.get("first_appearance")
        if _fa is not None:
            md.append(f"*First appears: chapter {_fa}*")
        if c.get("description"):
            md.append(c["description"])
        if c.get("evidence"):
            md.append(f"> {c['evidence'][:300]} ✓")
        elif c.get("evidence_offered"):
            md.append("> *offered evidence failed verification — dropped*")
        md.append("")
    _minors = result.get("minor_characters") or []
    if _minors:
        md.append(f"## Minor characters ({len(_minors)}) — below principal threshold")
        for c in _minors:
            md.append(f"- {c['name']} ({c.get('role') or 'unknown'})")
        md.append("")
    if result["relationships"]:
        md.append(f"## Relationships ({len(result['relationships'])})")
        for r in result["relationships"]:
            _conf = r.get("confidence", "")
            _conf_str = f" ({_conf} confidence)" if _conf else ""
            _vp = r.get("validation_p")
            _vn = r.get("validation_note", "")
            if _vp is not None:
                _conf_str += f" [validated P={_vp:.2f}]"
            if _vn == "downgraded":
                _conf_str += " [downgraded: low validation]"
            elif _vn == "would_drop":
                _conf_str += " [would drop: very low validation]"
            elif _vn == "no_evidence":
                _conf_str += " [would drop: no evidence]"
            md.append(f"- {r['from']} → {r['to']}: {r['type']}{_conf_str}")
        md.append("")
    _dropped = result.get("dropped_relationships") or []
    if _dropped:
        md.append(f"## Dropped relationships ({len(_dropped)}) — likely hallucinated")
        for r in _dropped:
            _vp = r.get("validation_p")
            _vp_str = f" [P={_vp:.2f}]" if _vp is not None else ""
            md.append(f"- {r['from']} → {r['to']}: {r['type']}{_vp_str}")
        md.append("")
    if result["quotes"]:
        md.append(f"## Quotes ({len(result['quotes'])})")
        for q in result["quotes"]:
            spoiler = " ⚠️ SPOILER" if q.get("spoiler") else ""
            md.append(f"> {q['text']}{spoiler}")
            md.append("")
    with open(prev_dir / f"{base}.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"  Preview saved to preview/{base}.md")
    if CONFIG.get("preview_summary"):
        _summary = _preview_summary(result)
        print(_summary)
        with open(prev_dir / f"{base}_summary.md", "w", encoding="utf-8") as f:
            f.write(_summary + "\n")
        print(f"  Preview summary saved to preview/{base}_summary.md")


def safe_move(src, dst_dir):
    dst = dst_dir / src.name
    if dst.exists():  # shutil.move onto an existing file fails on Windows
        dst = dst_dir / f"{src.stem}-{int(time.time())}{src.suffix}"
    shutil.move(str(src), str(dst))


def _process_one(args):
    """Wrapper for parallel book processing: sets per-book batch split."""
    f, dry_run, preview, per_book_batch = args
    _DIAG.batch_size_override = per_book_batch
    try:
        ok = process_file(f, dry_run, preview)
    except Exception as e:
        with _PRINT_LOCK:
            print(f"  Error processing {f.name}: {e}")
            traceback.print_exc()
        ok = False
    if not dry_run and not preview and not _is_single_file_run(f):
        try:
            safe_move(f, DONE_DIR if ok else FAILED_DIR)
        except Exception as e:
            with _PRINT_LOCK:
                print(f"  Could not move {f.name}: {e}")
    return ok


def _is_single_file_run(f):
    """True if this file was passed via --file (don't move it after processing)."""
    return getattr(_DIAG, "single_file", None) is not None and Path(f) == Path(_DIAG.single_file)


def run_once(dry_run, preview, max_books=None, file=None):
    if file:
        p = Path(file)
        if not p.is_file():
            print(f"File not found: {file}")
            return 1
        if p.suffix.lower() not in (".epub", ".mobi", ".azw3"):
            print(f"Unsupported format: {p.suffix} (need .epub, .mobi, or .azw3)")
            return 1
        files = [p]
        _DIAG.single_file = str(p)
    else:
        _DIAG.single_file = None
        files = sorted(p for p in IMPORT_DIR.iterdir()
                       if p.is_file() and p.suffix.lower() in (".epub", ".mobi", ".azw3"))
    if max_books and len(files) > max_books:
        print(f"Limiting to {max_books} of {len(files)} books (--max-books)")
        files = files[:max_books]
    if not files:
        print("No ebooks found.")
        return 0
    _CANCEL.clear()
    UI.start()
    try:
        return _run_once_inner(dry_run, preview, files)
    except KeyboardInterrupt:
        _CANCEL.set()
        UI.status("\nCancelled — stopping after current chapter...")
        print("  Checkpoint saved. Run with --resume to continue.")
        raise
    finally:
        UI.stop()


def _run_once_inner(dry_run, preview, files):
    jobs = max(1, CONFIG.get("jobs", 1))
    batch_size = max(1, CONFIG.get("batch_size", BATCH_SIZE))
    if jobs > 1 and len(files) > 1:
        per_book = max(1, batch_size // jobs)
        total_slots = per_book * min(jobs, len(files))
        print(f"Parallel mode: {min(jobs, len(files))} books x {per_book} slots "
              f"({total_slots}/{batch_size} LLM slots used)")
        from concurrent.futures import ThreadPoolExecutor
        failures = 0
        ex = ThreadPoolExecutor(max_workers=min(jobs, len(files)))
        try:
            results = list(ex.map(_process_one,
                                  [(f, dry_run, preview, per_book) for f in files]))
        except KeyboardInterrupt:
            _CANCEL.set()
            UI.status("\nInterrupted — stopping current chapters, cancelling pending books...")
            print("  Checkpoint saved. Run with --resume to continue.")
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        ex.shutdown(wait=True)
        failures = sum(1 for ok in results if not ok)
        return failures
    failures = 0
    for f in files:
        try:
            ok = process_file(f, dry_run, preview)
        except Exception as e:
            print(f"  Error: {e}")
            traceback.print_exc()
            ok = False
        if not ok:
            failures += 1
        if not dry_run and not preview:
            try:
                safe_move(f, DONE_DIR if ok else FAILED_DIR)
            except Exception as e:
                print(f"  Could not move {f.name}: {e}")
    return failures


# ---------------------------------------------------------------------------
# CLI profiles (P1) and guided mode (P3)
# ---------------------------------------------------------------------------

PROFILES_DIR = Path(os.path.expanduser("~")) / ".ebook-processor" / "profiles"

# Built-in profiles ship in code so they work on any machine with no setup.
BUILTIN_PROFILES = {
    "mass": {
        "decision_model": True,
        "decision_model_provider": "openrouter",
        "llm": "openai",
        "pipeline": "v2",
        "batch": 8,
        "rename_no_isbn": True,
        "show_matrices": True,
    },
    "quick": {
        "preview": True,
        "pipeline": "v2",
        "llm": "openai",
    },
}

# Dests that --save-profile never persists (per-run values or meta commands).
_PROFILE_EXCLUDE = frozenset({
    "file", "isbn", "series", "series_position",
    "import_labels", "import_rejections", "from_supabase",
    "trope_map", "validate", "push_preview", "verify",
    "profile", "save_profile", "list_profiles", "guided",
})


def _profile_path(name):
    return PROFILES_DIR / f"{name}.json"


def _available_profiles():
    """name -> 'builtin' | 'user'."""
    names = {n: "builtin" for n in BUILTIN_PROFILES}
    try:
        if PROFILES_DIR.is_dir():
            for p in sorted(PROFILES_DIR.glob("*.json")):
                names[p.stem] = "user"
    except OSError:
        pass
    return names


def _load_profile(name):
    """Return the profile dict, or None if not found / unreadable."""
    if name in BUILTIN_PROFILES:
        return dict(BUILTIN_PROFILES[name])
    p = _profile_path(name)
    try:
        if p.is_file():
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return None


def _save_profile(name, args, defaults):
    """Persist non-default, non-excluded flag values as JSON. Returns path."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    for dest, val in vars(args).items():
        if dest in _PROFILE_EXCLUDE:
            continue
        if val != getattr(defaults, dest, None):
            data[dest] = val
    path = _profile_path(name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


def _apply_profile(args, defaults, profile):
    """Apply profile values for flags the user did not explicitly set.

    Explicit CLI flags win: a flag counts as explicit when its parsed value
    differs from the argparse default.
    """
    applied = []
    for dest, val in profile.items():
        if not hasattr(args, dest):
            print(f"  WARNING: profile key '{dest}' is not a known flag, ignored")
            continue
        if getattr(args, dest) != getattr(defaults, dest, None):
            continue  # explicit CLI flag overrides the profile
        setattr(args, dest, val)
        applied.append(dest)
    if applied:
        print(f"  Profile: {', '.join(sorted(applied))}")


def _guided_setup(args):
    """Interactive prompts for the key options. Mutates args.

    Returns True to continue, False when cancelled / no file given.
    """
    def ask(prompt, default=""):
        suffix = f" [{default}]" if default else ""
        val = input(f"{prompt}{suffix}: ").strip() or default
        # Strip surrounding quotes (common when pasting Windows paths).
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        return val

    try:
        fpath = ask("EPUB file path")
        if not fpath:
            print("No file given, aborting.")
            return False
        args.file = fpath

        mode = ask("Preview or write?", "preview").lower()
        args.preview = mode != "write"

        dm = ask("Use decision model?", "y").lower()
        args.decision_model = dm in ("y", "yes")

        princ = ask("Principals-only?", "y").lower()
        args.principals_only = princ in ("y", "yes")

        mat = ask("Show matrices?", "n").lower()
        args.show_matrices = mat in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return False
    return True


def _build_parser():
    ap = argparse.ArgumentParser(description="Extract tropes/triggers/characters from ebooks.")

    g_in = ap.add_argument_group("Input")
    g_in.add_argument("--file", metavar="EBOOK", default=None,
                    help="process a single ebook file instead of the import folder")
    g_in.add_argument("--watch", action="store_true", help="rescan the import folder every 60s")
    g_in.add_argument("--max-books", type=int, default=None,
                        help="process at most N books from the import folder (e.g. --max-books 3)")
    g_in.add_argument("--isbn", metavar="ISBN", default="",
                    help="override ISBN for work resolution (e.g. when the EPUB "
                         "has no ISBN in its metadata); takes priority over EPUB "
                         "metadata and auto-lookup")
    g_in.add_argument("--series", metavar="NAME", default="",
                    help="override series name for this book (e.g. 'Daemon'); "
                         "takes priority over EPUB metadata, filename, and lookup")
    g_in.add_argument("--series-position", metavar="N", type=float, default=None,
                    help="override series position (e.g. 2 or 2.5)")

    g_models = ap.add_argument_group("Models")
    g_models.add_argument("--llm", choices=["ollama", "openrouter", "openai"], help="override config llm")
    g_models.add_argument("--decision-model", action="store_true",
                    help="enable decision-model trigger validation using the "
                         "configured openai_base_url (same Unsloth server)")
    g_models.add_argument("--decision-model-provider",
                    choices=["local", "openrouter"], default="local",
                    help="decision-model backend: 'local' Unsloth server "
                         "(default) or 'openrouter' hosted Jev (needs "
                         "openrouter_api_key in config.json or "
                         "OPENROUTER_API_KEY env)")
    g_models.add_argument("--decision-model-url", metavar="URL", default="",
                    help="Unsloth /v1/systemone base URL for trigger validation "
                         "(e.g. http://127.0.0.1:8888/v1); omit for regex gates")
    g_models.add_argument("--batch", type=int, default=None, help="parallel LLM requests (e.g. --batch 4)")

    g_pipe = ap.add_argument_group("Pipeline")
    g_pipe.add_argument("--pipeline", default=None, choices=["legacy", "v2"],
                    help="extraction pipeline: legacy (fixed chunks) or v2 "
                         "(chapter-level map/reduce with character roster)")
    g_pipe.add_argument("--chunks", default=None,
                    help="process only these chunk indices (e.g. --chunks 2 or --chunks 2,5,8); "
                         "numbering matches the sampled chunk list and debug files")
    g_pipe.add_argument("--jobs", type=int, default=None,
                    help="process N books in parallel, splitting --batch slots across them (e.g. --jobs 2)")
    g_pipe.add_argument("--sample", type=int, default=None, help="process every Nth chunk (e.g. --sample 3)")
    g_pipe.add_argument("--full", action="store_true", help="disable sampling, process every chunk")
    g_pipe.add_argument("--dedupe", action="store_true",
                    help="merge duplicate characters (name normalization + "
                         "embeddings) before writing; needs embed_url/embed_model")

    g_merge = ap.add_argument_group("Merging")
    g_merge.add_argument("--alias", metavar="A=B", action="append", default=[],
                    help="manual character alias: 'Loki=Brian Gragg' merges them. "
                         "Repeatable. Stored as human-validated, applies cross-book.")
    g_merge.add_argument("--principals-only", dest="principals_only",
                    action="store_true", default=True,
                    help="write only principal characters to Supabase "
                         "(default on; minors kept in preview JSON)")
    g_merge.add_argument("--no-principals-only", dest="principals_only",
                    action="store_false",
                    help="disable the principal filter; write all characters")
    g_merge.add_argument("--min-frequency", metavar="F", type=float, default=0.20,
                    help="minimum chapter frequency for principal status "
                         "(default 0.20)")
    g_merge.add_argument("--roster-reconcile", action="store_true",
                    help="experiment (v2): deterministic whole-roster "
                         "reconciliation after Call A — mutual alias "
                         "claims and learned links merge, stale alias "
                         "pollution is pruned (default off)")
    g_merge.add_argument("--staging-gate", action="store_true",
                    help="option F (v2): gate-band trope claims and "
                         "evidence-less trigger claims stage locally "
                         "instead of shipping; a claim ships when it "
                         "recurs across independent runs, grows into "
                         "the auto band, or gains evidence (default "
                         "off; see eval/f-staging-design.md)")

    g_learn = ap.add_argument_group("Learning")
    g_learn.add_argument("--learn", dest="learn", action="store_true", default=True,
                    help="learn nicknames/titles/thresholds across runs (default on)")
    g_learn.add_argument("--no-learn", dest="learn", action="store_false",
                    help="disable cross-run learning")
    g_learn.add_argument("--learn-dir", metavar="DIR", default="",
                    help="learning state directory "
                         "(default ~/.ebook-processor/learned/)")
    g_learn.add_argument("--learn-status", action="store_true",
                    help="print a summary of the learning system state "
                         "and exit (no books processed)")
    g_learn.add_argument("--staging-report", action="store_true",
                    help="print the staging gate's trapped report "
                         "(staged/promoted/expired claims per work) "
                         "and exit (no books processed); combine with "
                         "--json for machine-readable output")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON where a command supports it "
                         "(currently --staging-report)")
    g_learn.add_argument("--import-labels", metavar="PATH", default="",
                    help="import hand-labeled merge pairs (label-merges.py output) "
                         "into the nickname dictionary, then continue")
    g_learn.add_argument("--import-rejections", nargs="?", const="", default=None,
                    metavar="PATH",
                    help="import trope rejections JSON (from Trope Lab / the app) "
                         "into the learning system, then continue; "
                         "combine with --from-supabase to pull from Supabase "
                         "instead of a file")
    g_learn.add_argument("--from-supabase", action="store_true", default=False,
                    help="with --import-rejections: import rejected trope claims "
                         "from Supabase book_trope_claims (status='rejected') "
                         "instead of a JSON file")

    g_out = ap.add_argument_group("Output")
    g_out.add_argument("--preview", action="store_true", help="save preview files; write and move nothing")
    g_out.add_argument("--dry-run", action="store_true", help="extract only; write and move nothing")
    g_out.add_argument("--show-matrices", action="store_true",
                    help="print ASCII matrix visualizations (spice, character "
                         "importance, relationship confidence) during processing")
    g_out.add_argument("--preview-summary", action="store_true",
                    help="after a run, print a compact human-readable digest "
                         "and save it as preview/{book}_summary.md")
    g_out.add_argument("--preview-diff", nargs=2,
                    metavar=("OLD_JSON", "NEW_JSON"),
                    help="compare two preview JSONs and print what changed; "
                         "exits without processing books")
    g_out.add_argument("--recalibrate-spice", metavar="PREVIEW_JSON",
                    help="recompute book spice from per-chapter data using the "
                         "current matrix; reports old vs new or flags for "
                         "reprocessing; exits without processing books")
    g_out.add_argument("--no-viz", action="store_true",
                    help="disable the chapter visualizer panel in the Rich TUI")
    g_out.add_argument("--push-preview", metavar="PREVIEW_JSON",
                    help="push a preview JSON to Supabase without re-running the LLM")
    g_out.add_argument("--verify", metavar="PREVIEW_JSON",
                    help="compare a preview JSON against the DB and flag discrepancies (read-only)")
    g_out.add_argument("--resume", action="store_true",
                    help="resume an interrupted v2 run from per-chapter checkpoints "
                         "(preview/.checkpoints/); skips chapters that already completed")
    g_out.add_argument("--retry-failed", metavar="PREVIEW_JSON",
                    help="re-run only the failed chapters from a preview JSON "
                         "(chapters with content_ok=False or identity_ok=False); "
                         "saves to {book}_retried.json, original untouched")
    g_out.add_argument("--validate", metavar="PREVIEW_JSON",
                    help="run automated quality checks on a preview file")
    g_out.add_argument("--trope-map", metavar="PREVIEW_JSON",
                    help="map a preview file's tropes to the Cozy Libram catalog "
                         "via embeddings; writes a review JSON, nothing to Supabase")

    g_isbn = ap.add_argument_group("ISBN")
    g_isbn.add_argument("--allow-no-isbn", action="store_true",
                    help="process books without ISBN instead of skipping them "
                         "(default is to skip with a message)")
    g_isbn.add_argument("--rename-no-isbn", action="store_true",
                    help="rename skipped no-ISBN EPUBs to NO-ISBN-{original}.epub "
                         "so they are visible in the folder (never deletes)")

    g_dbg = ap.add_argument_group("Debug")
    g_dbg.add_argument("--debug", action="store_true",
                    help="save raw LLM output of failed chunks to preview/debug/")
    g_dbg.add_argument("--explain-blocks", dest="explain_blocks",
                    action="store_true", default=None,
                    help="explain why dedupe/merge guards block merges "
                         "(default: on with --debug, off otherwise)")
    g_dbg.add_argument("--no-explain-blocks", dest="explain_blocks",
                    action="store_false",
                    help="disable merge-block explanations")
    g_dbg.add_argument("--prompt-cache", action="store_true",
                    help="v2: task-last prompt layout for llama.cpp KV cache "
                         "reuse + interleaved A/B calls (experimental)")
    g_dbg.add_argument("--shared-system", action="store_true",
                    help="v2: use shared system prompt for A/B calls (isolation test)")
    g_dbg.add_argument("--task-last", action="store_true",
                    help="v2: put chapter text before task instructions (isolation test)")

    g_prof = ap.add_argument_group("Profiles & modes")
    g_prof.add_argument("--profile", metavar="NAME", default=None,
                    help="load flag values from a saved profile "
                         "(~/.ebook-processor/profiles/NAME.json); "
                         "explicit CLI flags override the profile")
    g_prof.add_argument("--save-profile", metavar="NAME", default=None,
                    help="save the current non-default flags as a profile, then exit")
    g_prof.add_argument("--list-profiles", action="store_true",
                    help="list available profiles (builtin + saved), then exit")
    g_prof.add_argument("--guided", action="store_true",
                    help="interactive setup: prompts for file, mode, and key options")
    g_prof.add_argument("--auto-update", action="store_true",
                    help="check origin/master for updates at startup; pull and "
                         "restart if behind (also checked each --watch cycle)")

    return ap


def main():
    ap = _build_parser()
    args = ap.parse_args()
    defaults = ap.parse_args([])

    if args.list_profiles:
        for name, src in sorted(_available_profiles().items()):
            print(f"  {name} ({src})")
        return

    if args.save_profile:
        path = _save_profile(args.save_profile, args, defaults)
        print(f"  Profile saved to {path}")
        return

    if args.guided:
        if not _guided_setup(args):
            return

    if args.profile:
        profile = _load_profile(args.profile)
        if profile is None:
            sys.exit(f"ERROR: profile '{args.profile}' not found "
                     "(--list-profiles to see available)")
        _apply_profile(args, defaults, profile)

    if args.trope_map:
        load_config()
        cmd_trope_map(args.trope_map)
        return

    if args.preview_diff:
        print(_cmd_preview_diff(args.preview_diff[0], args.preview_diff[1]))
        return

    if args.recalibrate_spice:
        print(_cmd_recalibrate_spice(args.recalibrate_spice))
        return

    if args.validate:
        load_config()
        cmd_validate(args.validate)
        return

    if args.push_preview:
        load_config()
        cmd_push_preview(args.push_preview)
        return

    if args.retry_failed:
        load_config()
        cmd_retry_failed(args.retry_failed)
        return

    if args.verify:
        load_config()
        cmd_verify(args.verify)
        return

    if args.learn_status:
        load_config()
        cmd_learn_status(args.learn_dir or None)
        return
    if args.staging_report:
        load_config()
        cmd_staging_report(as_json=args.json,
                           learn_dir=args.learn_dir or None)
        return

    # Auto-update: check origin/master before doing any real work. On a
    # successful pull the process re-execs with fresh code (never returns).
    if args.auto_update and not args.watch:
        _do_auto_update()

    if args.prompt_cache:
        CONFIG["v2_prompt_cache"] = True
    if args.shared_system:
        CONFIG["v2_shared_system"] = True
    if args.task_last:
        CONFIG["v2_task_last"] = True
    if args.no_viz:
        UI.set_viz_enabled(False)
    _dm_enabled = bool(args.decision_model or args.decision_model_url
                       or args.decision_model_provider == "openrouter")
    if _dm_enabled and args.decision_model_provider == "openrouter" \
            and args.decision_model_url:
        print("  Warning: --decision-model-url is ignored with provider "
              "'openrouter' (endpoint is fixed)")
    if _dm_enabled:
        try:
            _dm_endpoint, _dm_model, _dm_key, _dm_headers = \
                resolve_decision_model(
                    args.decision_model_provider,
                    args.decision_model_url, CONFIG)
        except ValueError as e:
            print(f"  Error: {e}")
            return
        global _DECISION_VALIDATOR
        _DECISION_VALIDATOR = DecisionValidator(
            _dm_endpoint, model=_dm_model, api_key=_dm_key,
            extra_headers=_dm_headers)
        # Never print the API key.
        print(f"  Decision model trigger validation: "
              f"provider={args.decision_model_provider} "
              f"endpoint={_dm_endpoint} model={_dm_model}")

    # Cross-run learning state (shared across --jobs threads).
    global _LEARNED
    _LEARNED = LearnedState(
        learn_dir=args.learn_dir or None, enabled=args.learn)
    if args.learn:
        print(f"  Learning state: {_LEARNED.dir} "
              f"({len(_LEARNED.nicknames)} nicknames, "
              f"{len(_LEARNED.trusted_titles())} trusted titles, "
              f"{len(_LEARNED.rejected_tropes)} rejected tropes)")
    if args.import_labels:
        n_imp = _LEARNED.import_labels(args.import_labels)
        print(f"  Imported {n_imp} labeled merge pairs from "
              f"{args.import_labels}")
        _LEARNED.save()
    if args.import_rejections is not None:
        if args.from_supabase:
            n_imp = _LEARNED.import_rejections_from_supabase()
            if n_imp >= 0:
                print(f"  Imported {n_imp} trope rejections from Supabase")
        elif args.import_rejections:
            n_imp = _LEARNED.import_rejections(args.import_rejections)
            print(f"  Imported {n_imp} trope rejections from "
                  f"{args.import_rejections}")
        else:
            print("  --import-rejections needs a PATH or --from-supabase")
        _LEARNED.save()
    if args.alias:
        n_alias = 0
        for spec in args.alias:
            if "=" not in spec:
                print(f"  WARNING: --alias '{spec}' ignored (use A=B format)")
                continue
            a, b = spec.split("=", 1)
            a, b = a.strip(), b.strip()
            if not a or not b:
                print(f"  WARNING: --alias '{spec}' ignored (empty name)")
                continue
            _LEARNED.record_alias(a, b)
            n_alias += 1
            print(f"  Alias: {a} = {b}")
        if n_alias:
            _LEARNED.save()

    if args.isbn:
        _cleaned = _clean_isbn(args.isbn)
        if not _cleaned:
            sys.exit(f"ERROR: --isbn '{args.isbn}' is not a valid ISBN-10/13")
        CONFIG["isbn_override"] = _cleaned
        print(f"ISBN override: {_cleaned}")
    if args.allow_no_isbn:
        CONFIG["allow_no_isbn"] = True
    if args.rename_no_isbn:
        CONFIG["rename_no_isbn"] = True
    if args.series:
        CONFIG["series_override"] = args.series.strip()
        print(f"Series override: {CONFIG['series_override']}")
    if args.series_position is not None:
        CONFIG["series_position_override"] = args.series_position
        print(f"Series position override: {args.series_position:g}")
    if not args.principals_only:
        CONFIG["principals_only"] = False
    if args.min_frequency != 0.20:
        CONFIG["min_frequency"] = args.min_frequency
    if args.show_matrices:
        CONFIG["show_matrices"] = True
        UI._show_matrices = True
    if args.roster_reconcile:
        CONFIG["roster_reconcile"] = True
    if args.staging_gate:
        CONFIG["staging_gate"] = True
    if args.preview_summary:
        CONFIG["preview_summary"] = True
    if args.resume:
        CONFIG["resume"] = True
    # --explain-blocks defaults ON with --debug, OFF otherwise; explicit
    # --explain-blocks/--no-explain-blocks wins.
    if args.explain_blocks is not None:
        CONFIG["explain_blocks"] = bool(args.explain_blocks)
    elif args.debug:
        CONFIG["explain_blocks"] = True
    if args.llm:
        CONFIG["llm"] = args.llm
    if args.full:
        CONFIG["sample_rate"] = 1
    elif args.sample:
        CONFIG["sample_rate"] = args.sample
    if args.batch:
        CONFIG["batch_size"] = args.batch
    if args.jobs:
        CONFIG["jobs"] = max(1, args.jobs)
    if args.chunks:
        CONFIG["only_chunks"] = args.chunks
    if args.pipeline:
        CONFIG["pipeline"] = args.pipeline
    if args.debug:
        CONFIG["debug"] = True
    if args.dedupe:
        CONFIG["dedupe"] = True
    if CONFIG.get("dedupe") and not CONFIG.get("embed_url"):
        sys.exit("ERROR: --dedupe needs embed_url (and embed_model) set in config.json")
    if CONFIG["llm"] not in ("ollama", "openrouter", "openai"):
        sys.exit(f"ERROR: unknown llm '{CONFIG['llm']}' (use ollama, openrouter, or openai)")
    if CONFIG["llm"] == "openrouter" and not CONFIG["openrouter_key"]:
        sys.exit("ERROR: openrouter_key is not set")
    if not (args.dry_run or args.preview) and not (CONFIG["supabase_url"] and CONFIG["supabase_key"]):
        sys.exit("ERROR: Set supabase_url and supabase_key in config.json")

    print(f"Portable v{PORTABLE_VERSION}")
    print(f"LLM: {CONFIG['llm']}")
    if args.preview:
        print("Preview mode: results saved to preview/, nothing written to Supabase, files not moved")
    elif args.dry_run:
        print("Dry run: nothing written, files not moved")

    if args.watch:
        print(f"Watching {IMPORT_DIR}... (Ctrl+C to stop)")
        try:
            while True:
                # In watch mode, --auto-update checks at the start of each
                # cycle. A successful pull re-execs with fresh code; the flag
                # is preserved across the exec so later cycles keep checking.
                if args.auto_update:
                    _do_auto_update(preserve_flag=True)
                run_once(args.dry_run, args.preview, args.max_books, args.file)
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        sys.exit(1 if run_once(args.dry_run, args.preview, args.max_books, args.file) else 0)


if __name__ == "__main__":
    main()
