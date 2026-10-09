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

PORTABLE_VERSION = "2.8.0"

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


def _clean_isbn(s):
    """Normalize an identifier string to digits-only ISBN-13, or None.
    Handles urn:isbn: prefixes, hyphens/spaces, and ISBN-10 -> ISBN-13."""
    s = re.sub(r"(?i)^urn:isbn:", "", (s or "").strip())
    s = re.sub(r"[^0-9Xx]", "", s).upper()
    if len(s) == 13 and s.isdigit():
        return s
    if len(s) == 10 and s[:9].isdigit() and s[9] in "0123456789X":
        core = "978" + s[:9]
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
        return core + str((10 - total % 10) % 10)
    return None


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


def _lookup_isbn_openlibrary(title, author):
    """Look up an ISBN via Open Library search API. Returns cleaned ISBN-13 or None.

    Only returns a result on a confident match: the first result's title must
    match (normalized) the given title, tolerating series prefixes like
    "[Series 02] - Title". Network call wrapped in try/except —
    any failure returns None (caller falls through to skip behavior).
    """
    if not title:
        return None
    try:
        q = (f"https://openlibrary.org/search.json?title={quote(title or '', safe='')}"
             f"&author={quote(author or '', safe='')}"
             f"&fields=title,isbn&limit=1")
        req = urllib.request.Request(q, headers={"User-Agent": "ebook-processor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
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
        identifiers["isbn"] = override
        return override, "flag"
    if identifiers.get("isbn"):
        return identifiers["isbn"], "epub"
    # Auto-lookup before giving up.
    auto = _lookup_isbn_openlibrary(title, author)
    if auto:
        identifiers["isbn"] = auto
        print(f"  Auto-resolved ISBN {auto} via Open Library")
        return auto, "openlibrary"
    return None, "none"


def _handle_missing_isbn(fpath):
    """Print skip/warning for a book with no ISBN. Returns True if processing
    may continue (i.e. --allow-no-isbn), False if the book should be skipped."""
    if CONFIG.get("allow_no_isbn"):
        print("")
        print("  \u26a0 WARNING: No ISBN found (metadata, --isbn, and Open Library lookup all failed).")
        print("    Work will be matched by title/author only, which may create duplicates.")
        print("")
        return True
    print(f"  \u23ed Skipping '{fpath.name}': no ISBN found. "
          f"Use --isbn to override or --allow-no-isbn to process anyway.")
    if CONFIG.get("rename_no_isbn"):
        try:
            new_name = fpath.parent / f"NO-ISBN-{fpath.name}"
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
    try:
        q = (f"https://openlibrary.org/search.json?title={quote(title or '', safe='')}"
             f"&author={quote(author or '', safe='')}"
             f"&fields=title,series&limit=1")
        req = urllib.request.Request(q, headers={"User-Agent": "ebook-processor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
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
    except Exception:
        pass
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

    - Drops front/back matter by filename heuristic.
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
    return [{"index": i, "label": u["label"], "text": u["text"]}
            for i, u in enumerate(out, 1)]


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
    # Clamp out-of-range values (defensive: bad model output shouldn't skew).
    spice_levels = [max(0, min(5, s)) for s in spice_levels]
    peak = max(spice_levels) if spice_levels else 0
    peak_band = _spice_band_peak(peak)
    if peak_band == "none":
        return 0
    min_spice = _PEAK_BAND_MIN_SPICE[peak_band]
    n_at_peak = sum(1 for s in spice_levels if s >= min_spice)
    frac = (n_at_peak / successful_chapters) if successful_chapters else 0
    freq_band = _spice_band_freq(frac)
    if freq_band == "none":
        return 0
    return _SPICE_MATRIX.get((peak_band, freq_band), 0)


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

# Relationship dedupe: exclusive types resolve by precedence (lower wins).
# Non-exclusive types are kept as extras only with evidence.
_REL_PRECEDENCE = {"spouse": 0, "parent": 1, "child": 1, "sibling": 2,
                   "romantic": 3, "friend": 4, "colleague": 4, "enemy": 5}
_REL_NONEXCLUSIVE = {"mentor", "rival", "ally", "neighbor", "other"}

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
        verdict = p_yes >= _REL_VALIDATION_THRESHOLD
        print(f"  Relationship validation: {from_name[:30]} -> "
              f"{to_name[:30]} ({rel_type}): P(yes)={p_yes:.2f} "
              f"{'≥' if verdict else '<'} {_REL_VALIDATION_THRESHOLD:.2f} → "
              f"{'keep' if verdict else 'downgrade to other'}")
        return verdict, p_yes


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


class LearnedState:
    """Persistent cross-run learning state. Thread-safe. Degrades to no-op
    when disabled or when the learn directory isn't writable."""

    def __init__(self, learn_dir=None, enabled=True):
        self._lock = threading.Lock()
        self.enabled = enabled
        if learn_dir:
            self.dir = Path(learn_dir)
        else:
            self.dir = Path(os.path.expanduser("~")) / ".ebook-processor" / "learned"
        self._writable = True
        self._warned = False
        # In-memory state (mirrors the JSON files).
        self.nicknames = {}       # nkey -> {"full": nkey, "count": int, "books": [isbn]}
        self.titles = {}          # title -> {"count": int, "rests": [str]}
        self.threshold_stats = {}  # trigger -> {"kept": [p], "dropped": [p]}
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

    def _load_json(self, name, default):
        try:
            p = self._path(name)
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return default

    def _save_json(self, name, data):
        """Atomic write: temp file + os.replace. No-op when not writable."""
        if not self._writable:
            return False
        try:
            p = self._path(name)
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
            self._titles_cache = None

    def save(self):
        """Persist all state. Called at end of each book."""
        if not self.enabled:
            return
        with self._lock:
            self._save_json("nicknames.json", self.nicknames)
            self._save_json("titles.json", self.titles)
            self._save_json("threshold_stats.json", self.threshold_stats)

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

    def load_series_hints(self, series_key):
        """Return {nkey: canonical_nkey} alias hints from prior books in
        the series. Empty dict when disabled/unknown."""
        if not self.enabled or not series_key:
            return {}
        try:
            p = self._series_path(series_key)
            if not p.exists():
                return {}
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
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
        try:
            p = self._series_path(series_key)
            data = {}
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:
            data = {}
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
        try:
            p = self._named_series_path(series_key)
            if not p.exists():
                return {}
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
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
        try:
            p = self._named_series_path(series_key)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:
            data = {}
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
            self._new.clear()
        if not parts:
            return ""
        return "Learned: " + ", ".join(parts)


# Module-level learning state, created in main() when --learn is on.
# _learn() always returns a usable object (disabled stub when off), so
# call sites never need None checks.
_LEARNED = None


def _learn():
    global _LEARNED
    if _LEARNED is None:
        _LEARNED = LearnedState(enabled=False)
    return _LEARNED


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
   - Direction matters: if A is B's parent, do NOT also list B as A's parent. Pick ONE direction.
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
        e["alias_keys"] |= alias_keys
        e["primary_keys"].add(nkey)
        for alias in {name} | set(c.get("aliases", [])):
            if not alias or alias == e["name"]:
                continue
            if _is_valid_alias(alias):
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
        spice = max(0, min(5, int(float(r.get("spice_level", 0)))))
    except (TypeError, ValueError):
        spice = 0
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
    """Map any name/alias to the roster's canonical name (or the name itself)."""
    nkey = norm_name(name)
    for k, e in roster.items():
        if nkey == k or nkey in e["alias_keys"]:
            return e["name"]
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
    for attr in ("death_reports", "alive_reports", "unresolved_mentions"):
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
    # Strongest signals first; cap total decision-model calls via the shared
    # per-book budget (relationship validation later draws from the same pool).
    _cands.sort(key=lambda x: -x[0])
    _in_budget, _over_cap = [], []
    for _c in _cands:
        (_in_budget if _dm_budget_claim(1) else _over_cap).append(_c)
    _cands = _in_budget
    # Pairs beyond the cap are listed for manual review, not silently dropped.
    for _sig, ka, kb in _over_cap:
        if ka not in roster or kb not in roster:
            continue
        ea, eb = roster[ka], roster[kb]
        possible_merges.append({
            "name_a": ea.get("name", ka), "name_b": eb.get("name", kb),
            "p_yes": None, "reason": "unresolved (Tier 3 cap reached)"})

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


def v2_reduce(chapter_as, chapter_bs, roster, chapters, preview=False):
    """Deterministic reduce over per-chapter v2 results. Pure code, no LLM.

    chapter_as/bs: {chapter_index: result or None}. Returns a result dict
    shaped for write_claims()/save_preview()/resolve_work()/dedupe_characters().

    preview: when True, decision-model validation scores are recorded but
    low-confidence family relationships are NOT downgraded (audit only).
    """
    n = len(chapters)
    idx_to_label = {c["index"]: c["label"] for c in chapters}
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
    for idx in sorted(chapter_as):
        a = chapter_as[idx]
        if not a:
            continue
        for r in a["relationships"]:
            frm = _roster_canonical(roster, r["from"])
            to = _roster_canonical(roster, r["to"])
            # Self-relationship guard (after canonicalisation)
            if norm_name(frm) == norm_name(to):
                continue
            rtype = r["type"]
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

    relationships = []
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
    # against its evidence quote. Low P or no evidence -> downgrade to
    # "other" (the relationship exists; we're just unsure of the type).
    # In preview mode the score is recorded but the type is NOT changed.
    # Draws from the shared per-book decision-model budget (merges first).
    for rel in relationships:
        _rt = rel.get("type", "")
        if _rt not in _REL_FAMILY_TYPES:
            continue
        _ev = (rel.get("evidence") or "").strip()
        if not _ev:
            # No evidence: downgrade without spending a decision-model call.
            rel["validation_p"] = None
            rel["validation_note"] = "no_evidence"
            if not preview:
                rel["type"] = "other"
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
        if not _v:
            rel["validation_note"] = "downgraded"
            if not preview:
                rel["type"] = "other"

    # --- Triggers: max severity per category, chapter counts, top evidence ---
    trig_acc = {c: {"sev": 0, "chapters": [], "evidence": []}
                for c in TRIGGER_CATEGORIES}
    for idx in sorted(chapter_bs):
        b = chapter_bs[idx]
        if not b:
            continue
        for cat in TRIGGER_CATEGORIES:
            sev = _SEVERITY_ORDER[b["triggers"][cat]]
            if sev > 0:
                acc = trig_acc[cat]
                acc["sev"] = max(acc["sev"], sev)
                acc["chapters"].append(idx_to_label.get(idx, ""))
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
            "chapter_count": len(acc["chapters"]),
            "frequency": round(len(acc["chapters"]) / n_successful_b, 4) if n_successful_b else 0,
            "prominence": _trigger_prominence(
                sev_names[sev], len(acc["chapters"]), n_successful_b),
            "evidence": acc["evidence"],
            "evidence_verified": bool(acc["evidence"]),
            "evidence_offered": bool(acc["evidence"]),
            "confidence": _v2_trigger_confidence(sev, len(acc["chapters"]),
                                                 len(acc["evidence"])),
        })
    if _gated_dropped:
        print(f"  Trigger gate: dropped {_gated_dropped} unsupported claim(s)")

    # --- Spice: peak-chapter intensity x fraction-at-that-intensity (matrix) ---
    # Triggers warn that content EXISTS; spice level measures PERVASIVENESS
    # at the peak intensity. A single explicit scene in a long book is not
    # a 5, even if many chapters have mild content.
    spices = [b["spice_level"] for b in chapter_bs.values() if b]
    spice_peak = max(spices) if spices else 0
    spice_level = _spice_level_from_chapters(spices, n_successful_b)

    # --- Trope candidates: union across chapters (gate runs separately) ---
    # (POVs computed earlier, before the characters section.)
    # Map to catalog IDs via embeddings when available, so paraphrases
    # ("enemies to lovers" vs "enemies-to-lovers dynamic") collapse before
    # tiering. Falls back to text normalization if embeddings unavailable.
    trope_candidates = []
    trope_counts = Counter()
    seen_t = set()
    for idx in sorted(chapter_bs):
        b = chapter_bs[idx]
        if not b:
            continue
        for t in b["trope_candidates"]:
            k = _tnorm(t)
            if not k:
                continue
            trope_counts[k] += 1
            if k not in seen_t:
                seen_t.add(k)
                trope_candidates.append(_clean_trope(t))
    # Catalog mapping: collapse paraphrases to catalog IDs before tiering.
    catalog_map = {}  # _tnorm(candidate) -> catalog_id (or None if unmapped)
    red_trope_chapters = {}  # display-key -> [chapter labels], built below
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
                for idx in sorted(chapter_bs):
                    b = chapter_bs[idx]
                    if not b:
                        continue
                    seen_this_chapter = set()
                    for t in b["trope_candidates"]:
                        k = _tnorm(t)
                        if not k:
                            continue
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
    # Stash the mapping for write_claims.
    red_catalog_map = {k: v for k, v in catalog_map.items() if v}
    # red_trope_chapters was built in the recount loop above (empty if skipped).

    # --- Quotes: filtered, ranked by length * distinctiveness, top 5-8 ---
    def _qtokens(s):
        return set((s or "").lower().split())

    _cand_quotes = []
    for idx in sorted(chapter_bs):
        b = chapter_bs[idx]
        if not b:
            continue
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
        "triggers": triggers,
        "spice_level": spice_level,
        "spice_peak": spice_peak,
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
3. Do not add tropes that are not in the candidate list."""


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
    return "Characters: %d | Chapter %s/%s | %.1f tok/s (avg %.1f tok/s)" % (
        st.get("new_count", 0), ci, ct, cur_toks, avg_toks)


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
                self._layout.split_column(
                    _RichLayout(self.progress, name="bars"),
                    _RichLayout(_VizRenderable(self), name="viz", size=24),
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
            txt = (text or "")[:800]
            self._viz = {
                "book": book_key,
                "label": chapter_label or "",
                "text": txt,
                "names": [],
                "events": prev.get("events", [])[-8:],
                "t0": time.time(),
                "spotlight": None,
                # Run-level state, carried across chapters.
                "run_t0": prev.get("run_t0") or time.time(),
                "chars_total": prev.get("chars_total", 0) + len(txt),
                "new_count": prev.get("new_count", 0),
                "milestones": prev.get("milestones") or set(),
                "ticker": prev.get("ticker") or [],
                "chap_idx": chap_idx,
                "chap_total": chap_total,
                "chap_t0": time.time(),
                "chap_chars": len(txt),
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
        spice = max(0, min(5, int(float(r.get("spice_level", 0)))))
    except (TypeError, ValueError):
        spice = 0

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


def trope_catalog_vectors():
    """Load trope catalog with embeddings. Fetches from Supabase `tropes`,
    embeds name+description via the dedupe embedding endpoint, caches to
    CONFIG['trope_map_cache']. Returns [(id, name, vector)]."""
    import os
    cache_path = CONFIG.get("trope_map_cache", "trope-vectors.json")
    cached = {}
    if os.path.exists(cache_path):
        try:
            cached = json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            pass
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
        vecs = embed_vectors(CONFIG["embed_url"], CONFIG.get("embed_model", ""),
                             texts)
        for r, v in zip(missing, vecs):
            cached[r["id"]] = v
        try:
            json.dump(cached, open(cache_path, "w", encoding="utf-8"))
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

    db_chars = (sb("book_characters",
                   params=f"?work_id=eq.{wid}&select=name,role,vitality,status&limit=500") or [])
    db_tropes = (sb("book_trope_claims",
                    params=f"?work_id=eq.{wid}&select=trope_id,status&limit=200") or [])
    db_trig = {r["warning"]: r["status"] for r in (
        sb("book_trigger_claims",
           params=f"?work_id=eq.{wid}&select=warning,status&limit=100") or [])}
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
    title = result.get("title") or os.path.basename(preview_path)
    if result.get("aborted"):
        print(f"  Skipping {title}: preview is marked ABORTED (partial results)")
        return
    if result.get("chapter_detection") == "fallback":
        print(f"  Skipping {title}: chapter detection used FALLBACK splitting "
              f"(unreliable chapters) — re-run from a clean EPUB")
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
    # Ensure the fields write_claims needs are present.
    result.setdefault("trope_catalog_map", {})
    result.setdefault("trope_confidence", {})
    errors = write_claims(wid, result)
    if errors:
        print(f"  {errors} database operation(s) failed")
    else:
        print(f"  Push complete, no errors")


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
    if len(sa) >= 2 and len(sb) >= 2 and sa[-1] == sb[-1]:
        fa, fb = sa[0], sb[0]
        if (fa != fb and len(fa) >= 3 and len(fb) >= 3
                and not _nick_pair(fa, fb)
                and not _is_spelling_variant(fa, fb)
                and len(fa) > 1 and len(fb) > 1):
            return "no"
    return "ambiguous"


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
    # Author surname alone as a character name ("suarez" for Daniel Suarez).
    _atok = ak.split()
    if len(_atok) >= 2 and nk == _atok[-1] and len(nk) > 3:
        return True
    return False


def _tnorm(t):
    """Trope dedup key: lowercase, underscores -> spaces, tidy spaces."""
    return re.sub(r"\s+", " ", (t or "").lower().replace("_", " ")).strip()


def _clean_trope(t):
    """Display form of a trope name: underscores -> spaces, tidy spaces."""
    return re.sub(r"\s+", " ", (t or "").replace("_", " ")).strip()


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


def correct_metadata(title, author, identifiers):
    """Cross-check parsed title/author against OpenLibrary via ISBN.

    Returns (title, author, corrected: bool). Only overrides when the API
    clearly disagrees: empty parsed title, parsed title == author name
    (the Change Agent bug), or zero shared significant words.
    One HTTP call per book; failures are silent (keeps parsed values).
    """
    isbn = (identifiers or {}).get("isbn")
    if not isbn:
        return title, author, False
    try:
        url = f"https://openlibrary.org/search.json?q={isbn}&fields=title,author_name&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "ebook-processor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
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


def resolve_work(title, author, identifiers, is_anthology=False, create=True):
    """Return (work_id, how). Resolution order: ISBN -> edition -> work,
    then exact title/author match. With create=False (preview) never POSTs:
    how is 'new' when nothing matched."""
    isbn = (identifiers or {}).get("isbn")
    if isbn:
        rows = sb("editions", params=f"?isbn=eq.{isbn}&select=work_id&limit=1")
        if rows is None:
            return None, "db_error"
        if rows and rows[0].get("work_id"):
            return rows[0]["work_id"], f"isbn:{isbn}"
    tn, an = norm(title), norm(author)
    if not tn:
        print("  Cannot build a match key from the title; refusing to create a work")
        return None, "no_title"
    rows = sb("works", params=f"?title_norm=eq.{quote(tn, safe='')}"
                              f"&author_norm=eq.{quote(an, safe='')}&select=id&limit=1")
    if rows is None:
        return None, "db_error"
    if rows:
        return rows[0]["id"], "title_author"
    if not create:
        return None, "new"
    rows = sb("works", method="POST", data={
        "title": title, "title_norm": tn,
        "authors": [author] if author else [], "author_norm": an,
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


def _trope_lookup(name):
    # Exact, case-insensitive match. Strip LIKE/PostgREST wildcard characters first.
    clean = re.sub(r"[*%_\\]", "", name).strip()
    if not clean:
        return None
    rows = sb("tropes", params=f"?name=ilike.{quote(clean, safe='')}&select=id&limit=1")
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


def write_claims(work_id, result, trope_mappings=None):
    """Insert only what is not already there. Returns the number of failed operations.

    trope_mappings: optional dict {pipeline_trope_name: catalog_id} from
    embedding-based mapping. Falls back to result["trope_catalog_map"], then
    _trope_lookup (exact match). Unmapped tropes go to trope_proposals.
    """
    errors = 0
    _models = {"ollama": CONFIG["ollama_model"], "openrouter": CONFIG["openrouter_model"],
               "openai": CONFIG.get("openai_model", "")}
    model = _models.get(CONFIG["llm"], "")
    conf_t = result.get("trope_confidence", {})
    wrote = {"tropes": 0, "triggers": 0, "characters": 0, "proposals": 0, "quotes": 0}
    # Merge explicit mappings with the in-pipeline catalog map.
    _cat = dict(result.get("trope_catalog_map", {}) or {})
    if trope_mappings:
        _cat.update({_tnorm(k): v for k, v in trope_mappings.items()})
    unmatched = []

    # Tropes (embedding-mapped when available, else exact lookup)
    seen = _existing("book_trope_claims", "trope_id", work_id)
    if seen is None:
        errors += 1
    else:
        rows, used = [], set(seen)
        for t in result["tropes"]:
            tid = _cat.get(_tnorm(t))
            if tid is None and trope_mappings and t in trope_mappings:
                tid = trope_mappings[t]
            if tid is None:
                tid = _trope_lookup(t)
            if tid is None:
                unmatched.append(t)
                continue
            if str(tid).lower() in used:
                continue
            used.add(str(tid).lower())
            _chaps = (result.get("trope_chapters") or {}).get(_tnorm(t), [])
            rows.append({"work_id": work_id, "trope_id": tid, "status": "candidate",
                         "confidence": conf_t.get(_tnorm(t), 0.7),
                         "source_type": "ai",
                         "model": model,
                         "evidence": {"source": "ebook-extraction",
                                      "chapters": _chaps,
                                      "chapter_count": len(_chaps)}})
        if rows:
            if sb("book_trope_claims", method="POST", data=rows) is None:
                errors += 1
            else:
                wrote["tropes"] = len(rows)

    # Unmapped tropes -> trope_proposals (with book provenance).
    # Drop sub-threshold: only propose if confidence meets the bar.
    _PROP_MIN_CONF = 0.7
    if unmatched:
        seen_prop = sb("trope_proposals", params="?select=name_key&limit=1000") or []
        seen_keys = {r["name_key"] for r in seen_prop}
        prop_rows = []
        for t in unmatched:
            key = _tnorm(t)
            if key in seen_keys:
                continue
            # Skip denylisted and low-confidence
            if _is_denylisted_trope(t):
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
            prop_rows.append(row)
        if prop_rows:
            if sb("trope_proposals", method="POST", data=prop_rows) is None:
                errors += 1
            else:
                wrote["proposals"] = len(prop_rows)

    # Triggers
    seen = _existing("book_trigger_claims", "warning", work_id)
    if seen is None:
        errors += 1
    else:
        rows = []
        for t in result["triggers"]:
            warn = t["warning"] if isinstance(t, dict) else t
            if str(warn).lower() in seen:
                continue
            seen.add(str(warn).lower())
            td = t if isinstance(t, dict) else {}
            rows.append({"work_id": work_id, "warning": warn, "status": "candidate",
                         "confidence": td.get("confidence", 0.7),
                         "source_type": "ai",
                         "model": model,
                         "evidence": {"source": "ebook-extraction",
                                      "severity": td.get("severity", ""),
                                      "severity_claimed": td.get("severity_claimed", ""),
                                      "chapters": td.get("chapters", []),
                                      "chapter_count": td.get("chapter_count", 0),
                                      "frequency": td.get("frequency", 0),
                                      "prominence": td.get("prominence", ""),
                                      "quotes": td.get("evidence", [])}})
        if rows:
            if sb("book_trigger_claims", method="POST", data=rows) is None:
                errors += 1
            else:
                wrote["triggers"] = len(rows)

    # Characters (relationships are stored per character)
    rel_map = {}
    _evidenceless_rels = 0
    for r in result["relationships"]:
        # GPT audit: require evidence for DB promotion. Evidence-less relationships
        # stay in the preview JSON but don't get written to the database.
        if not r.get("evidence"):
            _evidenceless_rels += 1
            continue
        rel_map.setdefault(r["from"].lower(), []).append(
            {"to": r["to"], "type": r["type"], "evidence": r.get("evidence", "")})
    if _evidenceless_rels:
        print(f"  Skipped {_evidenceless_rels} evidence-less relationships (kept in preview)")
    seen = _existing("book_characters", "name", work_id)
    if seen is None:
        errors += 1
    else:
        pre_existing = set(seen)  # for backfill: names already in DB before this run
        rows = []
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
        if rows:
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
        # Backfill: existing rows never got the new columns (insert-only).
        # PATCH NULL fields from the current extraction — never overwrite.
        _backfilled = 0
        for c in result["characters"]:
            if c["name"].lower() not in pre_existing:
                continue  # was just inserted above; only backfill pre-existing rows
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
            merged = dict(existing[0]["data"]) if existing and existing[0].get("data") else {}
            merged.update(meta_updates)
            # POST with upsert (merge-duplicates required for on_conflict)
            r = sb("book_meta", method="POST",
                   params="?on_conflict=isbn",
                   data={"isbn": isbn, "data": merged},
                   prefer="resolution=merge-duplicates")
            if r is None:
                errors += 1

    # Quotes -> book_quotes (3-5 memorable per book, no speaker attribution yet)
    quotes = result.get("quotes", [])
    if quotes:
        # Check existing to avoid duplicates
        existing_q = sb("book_quotes", params=f"?work_id=eq.{work_id}&select=quote&limit=100")
        seen_q = {r.get("quote", "")[:100] for r in (existing_q or [])}
        qrows = []
        for q in quotes[:5]:  # cap at 5
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
        if qrows:
            if sb("book_quotes", method="POST", data=qrows) is None:
                errors += 1
            else:
                wrote["quotes"] = len(qrows)

    UI.status(f"  Wrote {wrote['tropes']} tropes, {wrote['triggers']} triggers, "
                f"{wrote['characters']} characters, {wrote['proposals']} proposals, "
                f"{wrote.get('quotes', 0)} quotes (new rows only)")
    return errors


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
    vecs = embed_vectors(CONFIG["embed_url"], CONFIG.get("embed_model", ""), texts)
    for i in range(len(characters)):
        for j in range(i + 1, len(characters)):
            if s1_root[i] == s1_root[j]:
                continue  # already merged by signal 1
            ga, gb = _guess_gender(names[i]), _guess_gender(names[j])
            if ga and gb and ga != gb:
                continue  # never merge across genders
            if _has_nonperson_noun(names[i]) or _has_nonperson_noun(names[j]):
                continue  # places/things never merge into people
            if _is_multi(names[i]) != _is_multi(names[j]):
                continue  # "the host and hostess" != "the husband"
            if (_is_proper(names[i]) and _is_proper(names[j])
                    and norm_name(names[i]) != norm_name(names[j])):
                # Distinct proper names: only merge on (near-)identical
                # descriptions, e.g. the same mentioned-only plumber.
                if (_norm_desc(characters[i].get("description")) !=
                        _norm_desc(characters[j].get("description"))):
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
    for r in relationships:
        a, b, t = remap(r["from"]), remap(r["to"]), r["type"]
        key = (a.lower(), b.lower(), t.lower())
        if key not in seen:
            seen.add(key)
            new_rels.append({"from": a, "to": b, "type": t})

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
    if _isbn_source == "none" and not _handle_missing_isbn(fpath):
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
    # 2 work units per chapter (Call A identity + Call B content)
    UI.book_start(_book_key, f"📖 {title or fpath.stem}", total=2 * n)

    backends = {"ollama": llm_ollama, "openrouter": llm_openrouter,
                "openai": llm_openai_compat}
    call = backends.get(CONFIG["llm"], llm_ollama)
    batch_size = effective_batch_size()

    # Pass 1: Call A sequential (roster must see chapters in order).
    # When v2_prompt_cache is on, submit B(i) right after A(i) so the
    # chapter's KV cache is still hot (interleaved, B on worker threads).
    roster, chapter_as, chapter_bs = {}, {}, {}
    a_ok = a_failed = a_fallback = 0
    b_ok = b_failed = 0
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
            return False
        UI.viz_chapter(_book_key, ch["label"], ch["text"],
                       chap_idx=i + 1, chap_total=n)
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
        if interleave:
            b_futs[b_pool.submit(v2_call_b, call, ch, n)] = i
        elif inline_b:
            b = v2_call_b(call, ch, n)
            chapter_bs[i] = b
            if b is None:
                b_failed += 1
            else:
                b_ok += 1
            UI.advance(_book_key)  # B unit (A already advanced above)
    if interleave:
        done_count = 0
        for fut in as_completed(b_futs):
            if _CANCEL.is_set():
                for f in b_futs:
                    f.cancel()
                b_pool.shutdown(wait=False, cancel_futures=True)
                UI.book_done(_book_key)
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
        b_pool.shutdown()

    # Pass 2: Call B parallel (independent per chapter; skipped if interleaved).
    if not interleave and not inline_b:
        UI.set_phase(_book_key, "content")
        if batch_size > 1:
            with ThreadPoolExecutor(max_workers=batch_size) as ex:
                futs = {ex.submit(v2_call_b, call, ch, n): i for i, ch in indexed}
                done_count = 0
                for fut in as_completed(futs):
                    if _CANCEL.is_set():
                        # Cancel remaining futures; partial results discarded
                        for f in futs:
                            f.cancel()
                        UI.book_done(_book_key)
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
        else:
            UI.set_phase(_book_key, "content")
            for i, ch in indexed:
                b = v2_call_b(call, ch, n)
                chapter_bs[i] = b
                if b is None:
                    b_failed += 1
                else:
                    b_ok += 1
                UI.advance(_book_key)

    failed = a_failed + b_failed
    aborted = (a_ok == 0 and b_ok == 0) or failed / max(2 * n, 1) > MAX_FAILED_CHUNK_RATIO
    if aborted:
        print(f"  {failed}/{2 * n} calls failed; not writing partial results")

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

    # Deterministic reduce (no LLM).
    red = v2_reduce(chapter_as, chapter_bs, roster,
                    [ch for _, ch in indexed], preview=preview)

    # Trope confirmation: frequency tiers, LLM gate only for the borderline.
    # Thresholds scale with book length (5% of chapters, min 3):
    # - >= auto_min chapters: auto-confirm (a recurring pattern is a trope).
    # - 2 to auto_min-1 chapters: LLM gate judges against summaries.
    # - 1 chapter: drop in long books (not a book-level trope).
    tropes, trope_conf = [], {}
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
        for c in red["trope_candidates"]:
            # Drop genre/setting labels (not tropes)
            if _is_denylisted_trope(c):
                _denied += 1
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
        for c in auto:
            tropes.append(c)
            trope_conf[_tnorm(c)] = round(
                min(0.9, 0.55 + 0.1 * counts.get(_tnorm(c), auto_min)), 2)
        if gated:
            g_tropes, g_conf = v2_trope_gate(
                call, red["chapter_summaries"], gated, counts)
            tropes.extend(g_tropes)
            trope_conf.update(g_conf)
        print(f"{len(tropes)} confirmed")
    red["tropes"] = tropes
    red["trope_confidence"] = trope_conf

    reading_mins = word_count // 250
    result = {
        "file": fpath.name, "title": title or fpath.stem, "author": author,
        "isbn": identifiers.get("isbn"), "asin": identifiers.get("asin"),
        "isbn_source": _isbn_source,  # "flag" | "epub" | "openlibrary" | "none"
        "series_name": _series_name, "series_position": _series_pos,
        "series_source": _series_source,  # "flag"|"epub"|"filename"|"openlibrary"|"none"
        "word_count": word_count, "reading_time_mins": reading_mins,
        "chunks": n, "chunks_failed": a_failed,
        "pipeline": "v2",
        "tropes": tropes, "trope_confidence": trope_conf,
        "triggers": red["triggers"], "characters": red["characters"],
        "relationships": red["relationships"],
        "spice_level": red["spice_level"],
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
    }
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
        return not aborted
    if preview:
        save_preview(fpath, result)
        print(f"  Preview saved (pipeline=v2)")
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
    return True


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
    # ISBN resolution: --isbn flag > EPUB metadata > Open Library lookup.
    # Default is to skip books with no ISBN (see --allow-no-isbn).
    _isbn, _isbn_source = _resolve_book_isbn(identifiers, title, author)
    if _isbn_source == "none" and not _handle_missing_isbn(fpath):
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
    triggers = [dict(trig_data[k], confidence=_confidence(h, ok))
                for k, h in trig_hits.items() if h >= min_hits]
    characters = [dict(char_data[k], confidence=_confidence(h, ok))
                  for k, h in char_hits.most_common()]
    votes = sorted(spice_votes)
    spice_level = votes[int(0.75 * (len(votes) - 1))] if votes else 0  # 75th percentile
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
        "relationships": relationships, "spice_level": spice_level,
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
        result["characters"] = _princs
        result["relationships"] = _prels
        result["minor_characters"] = _minors
    if is_anth:
        print(f"  Anthology with {len(stories)} stories")
    if failed:
        print(f"  Note: {failed}/{n} chunks failed and were skipped")

    if preview:
        save_preview(fpath, result)
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
    return True


def save_preview(fpath, result):
    """Save human-readable preview to preview/ folder."""
    prev_dir = SCRIPT_DIR / "preview"
    prev_dir.mkdir(exist_ok=True)
    base = fpath.stem

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
    md.append(f"**Spice:** {'🌶️' * result['spice_level'] or 'None'} ({result['spice_level']}/5)")
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
            if _vp is not None:
                _conf_str += f" [validated P={_vp:.2f}]"
            elif r.get("validation_note") == "downgraded":
                _conf_str += " [downgraded: low validation]"
            elif r.get("validation_note") == "no_evidence":
                _conf_str += " [downgraded: no evidence]"
            md.append(f"- {r['from']} → {r['to']}: {r['type']}{_conf_str}")
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


def main():
    ap = argparse.ArgumentParser(description="Extract tropes/triggers/characters from ebooks.")
    ap.add_argument("--dry-run", action="store_true", help="extract only; write and move nothing")
    ap.add_argument("--preview", action="store_true", help="save preview files; write and move nothing")
    ap.add_argument("--watch", action="store_true", help="rescan the import folder every 60s")
    ap.add_argument("--llm", choices=["ollama", "openrouter", "openai"], help="override config llm")
    ap.add_argument("--sample", type=int, default=None, help="process every Nth chunk (e.g. --sample 3)")
    ap.add_argument("--full", action="store_true", help="disable sampling, process every chunk")
    ap.add_argument("--batch", type=int, default=None, help="parallel LLM requests (e.g. --batch 4)")
    ap.add_argument("--max-books", type=int, default=None,
                        help="process at most N books from the import folder (e.g. --max-books 3)")
    ap.add_argument("--file", metavar="EBOOK", default=None,
                    help="process a single ebook file instead of the import folder")
    ap.add_argument("--isbn", metavar="ISBN", default="",
                    help="override ISBN for work resolution (e.g. when the EPUB "
                         "has no ISBN in its metadata); takes priority over EPUB "
                         "metadata and auto-lookup")
    ap.add_argument("--alias", metavar="A=B", action="append", default=[],
                    help="manual character alias: 'Loki=Brian Gragg' merges them. "
                         "Repeatable. Stored as human-validated, applies cross-book.")
    ap.add_argument("--series", metavar="NAME", default="",
                    help="override series name for this book (e.g. 'Daemon'); "
                         "takes priority over EPUB metadata, filename, and lookup")
    ap.add_argument("--series-position", metavar="N", type=float, default=None,
                    help="override series position (e.g. 2 or 2.5)")
    ap.add_argument("--principals-only", dest="principals_only",
                    action="store_true", default=True,
                    help="write only principal characters to Supabase "
                         "(default on; minors kept in preview JSON)")
    ap.add_argument("--no-principals-only", dest="principals_only",
                    action="store_false",
                    help="disable the principal filter; write all characters")
    ap.add_argument("--min-frequency", metavar="F", type=float, default=0.20,
                    help="minimum chapter frequency for principal status "
                         "(default 0.20)")
    ap.add_argument("--allow-no-isbn", action="store_true",
                    help="process books without ISBN instead of skipping them "
                         "(default is to skip with a message)")
    ap.add_argument("--rename-no-isbn", action="store_true",
                    help="rename skipped no-ISBN EPUBs to NO-ISBN-{original}.epub "
                         "so they are visible in the folder (never deletes)")
    ap.add_argument("--jobs", type=int, default=None,
                    help="process N books in parallel, splitting --batch slots across them (e.g. --jobs 2)")
    ap.add_argument("--chunks", default=None,
                    help="process only these chunk indices (e.g. --chunks 2 or --chunks 2,5,8); "
                         "numbering matches the sampled chunk list and debug files")
    ap.add_argument("--pipeline", default=None, choices=["legacy", "v2"],
                    help="extraction pipeline: legacy (fixed chunks) or v2 "
                         "(chapter-level map/reduce with character roster)")
    ap.add_argument("--debug", action="store_true",
                    help="save raw LLM output of failed chunks to preview/debug/")
    ap.add_argument("--no-viz", action="store_true",
                    help="disable the chapter visualizer panel in the Rich TUI")
    ap.add_argument("--decision-model-url", metavar="URL", default="",
                    help="Unsloth /v1/systemone base URL for trigger validation "
                         "(e.g. http://127.0.0.1:8888/v1); omit for regex gates")
    ap.add_argument("--decision-model", action="store_true",
                    help="enable decision-model trigger validation using the "
                         "configured openai_base_url (same Unsloth server)")
    ap.add_argument("--decision-model-provider",
                    choices=["local", "openrouter"], default="local",
                    help="decision-model backend: 'local' Unsloth server "
                         "(default) or 'openrouter' hosted Jev (needs "
                         "openrouter_api_key in config.json or "
                         "OPENROUTER_API_KEY env)")
    ap.add_argument("--dedupe", action="store_true",
                    help="merge duplicate characters (name normalization + "
                         "embeddings) before writing; needs embed_url/embed_model")
    ap.add_argument("--trope-map", metavar="PREVIEW_JSON",
                    help="map a preview file's tropes to the Cozy Libram catalog "
                         "via embeddings; writes a review JSON, nothing to Supabase")
    ap.add_argument("--validate", metavar="PREVIEW_JSON",
                    help="run automated quality checks on a preview file")
    ap.add_argument("--push-preview", metavar="PREVIEW_JSON",
                    help="push a preview JSON to Supabase without re-running the LLM")
    ap.add_argument("--verify", metavar="PREVIEW_JSON",
                    help="compare a preview JSON against the DB and flag discrepancies (read-only)")
    ap.add_argument("--prompt-cache", action="store_true",
                    help="v2: task-last prompt layout for llama.cpp KV cache "
                         "reuse + interleaved A/B calls (experimental)")
    ap.add_argument("--shared-system", action="store_true",
                    help="v2: use shared system prompt for A/B calls (isolation test)")
    ap.add_argument("--task-last", action="store_true",
                    help="v2: put chapter text before task instructions (isolation test)")
    ap.add_argument("--learn", dest="learn", action="store_true", default=True,
                    help="learn nicknames/titles/thresholds across runs (default on)")
    ap.add_argument("--no-learn", dest="learn", action="store_false",
                    help="disable cross-run learning")
    ap.add_argument("--learn-dir", metavar="DIR", default="",
                    help="learning state directory "
                         "(default ~/.ebook-processor/learned/)")
    ap.add_argument("--import-labels", metavar="PATH", default="",
                    help="import hand-labeled merge pairs (label-merges.py output) "
                         "into the nickname dictionary, then continue")
    args = ap.parse_args()

    if args.trope_map:
        load_config()
        cmd_trope_map(args.trope_map)
        return

    if args.validate:
        load_config()
        cmd_validate(args.validate)
        return

    if args.push_preview:
        load_config()
        cmd_push_preview(args.push_preview)
        return

    if args.verify:
        load_config()
        cmd_verify(args.verify)
        return

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
              f"{len(_LEARNED.trusted_titles())} trusted titles)")
    if args.import_labels:
        n_imp = _LEARNED.import_labels(args.import_labels)
        print(f"  Imported {n_imp} labeled merge pairs from "
              f"{args.import_labels}")
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
                run_once(args.dry_run, args.preview, args.max_books, args.file)
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        sys.exit(1 if run_once(args.dry_run, args.preview, args.max_books, args.file) else 0)


if __name__ == "__main__":
    main()
