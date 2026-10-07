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

PORTABLE_VERSION = "2.2.1"

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
    "pipeline": "legacy",  # or "v2": chapter-level map/reduce (--pipeline v2)
    "v2_max_chapter_chars": 16000,  # ~4k tokens; keep well under your
    # server's context window minus prompt (~1k) minus output (~3k)
}


# Config keys that must be int (env overrides arrive as strings).
_INT_CONFIG_KEYS = {"sample_rate", "sample_edges", "batch_size", "llm_max_tokens"}
# Config keys that are bools (EBOOK_DEDUPE=false must not be truthy).
_BOOL_CONFIG_KEYS = {"debug", "dedupe"}


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
    manifest, spine = {}, []
    for e in root.iter():
        name = _local(e.tag)
        if name == "title" and title is None and (e.text or "").strip():
            title = e.text.strip()
        elif name == "creator" and author is None and (e.text or "").strip():
            author = e.text.strip()
        elif name == "identifier" and (e.text or "").strip():
            identifiers.append(e.text.strip())
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
    return (names or None), title, author, {"raw": identifiers}


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


def _pick_identifiers(raw_list):
    """Return {'isbn': <13-digit or None>, 'asin': <str or None>, 'raw': [...]}."""
    out = {"isbn": None, "asin": None, "raw": list(raw_list or [])}
    for r in out["raw"]:
        isbn = _clean_isbn(r)
        if isbn and not out["isbn"]:
            out["isbn"] = isbn
            continue
        m = re.match(r"(?i)^(B[0-9A-Z]{9})$", (r or "").strip())
        if m and not out["asin"]:
            out["asin"] = m.group(1).upper()
    return out


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
    return units, title, author, _pick_identifiers(opf_ids["raw"])


def extract_epub_text(epub_path):
    units, title, author, identifiers = extract_epub_units(epub_path)
    return "\n\n".join(u["text"] for u in units), title, author, identifiers


# --- Chapter splitter (v2 pipeline; legacy chunking untouched) ---
# Front/back-matter heuristic, matched against the spine filename.
_SKIP_UNIT_RE = re.compile(
    r"copyright|toc|table.?of.?contents|dedication|acknowledg|also.?by|"
    r"about.?(the.?)?author|title.?page|\bcover\b|\bnav\b", re.IGNORECASE)

MIN_CHAPTER_CHARS = 1500   # smaller units merge into the next one
MAX_CHAPTER_CHARS = 16000  # ~4k tokens; must leave room in the context window
# for the prompt + the model's thinking + JSON output. Override with
# v2_max_chapter_chars in config.json if your server has a larger context.


def _chapter_part(u, text, part):
    return {"spine": u["spine"],
            "label": u["label"] if part == 1 else f"{u['label']} (part {part})",
            "text": text}


def split_chapters(units):
    """Turn raw spine units into chapter-sized units for the v2 pipeline.

    - Drops front/back matter by filename heuristic.
    - Merges units under MIN_CHAPTER_CHARS into the next unit.
    - Splits units over the configured max (v2_max_chapter_chars, default
      MAX_CHAPTER_CHARS) on paragraph boundaries.
    Returns [{"index", "label", "text"}] in reading order.
    """
    max_chars = CONFIG.get("v2_max_chapter_chars", MAX_CHAPTER_CHARS)
    kept = [u for u in units if not _SKIP_UNIT_RE.search(u["spine"])]
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

PROMPT_V2_CHARACTERS = """Analyze this book chapter and return ONLY valid JSON. No commentary, no markdown, just the JSON object.
Keep any internal reasoning extremely brief — a complete, valid JSON object is the priority; do not let thinking crowd out the answer.

Chapter: {chapter_label}

Known characters so far (reuse these EXACT names when the same person appears; add new aliases instead of creating duplicates):
{roster}

{
  "characters": [{"name": "...", "aliases": ["other names used in this chapter"], "role": "protagonist|antagonist|supporting|minor", "description": "...", "evidence": "exact sentence from the chapter"}],
  "relationships": [{"from": "...", "to": "...", "type": "spouse|parent|child|sibling|friend|enemy|mentor|colleague|neighbor|other", "evidence": "exact sentence from the chapter"}],
  "pov_character": "name of the character narrating this chapter, or null"
}

RULES:
1. ENTITY MERGING: "Mother", "the narrator", "I" and the author's name are one person — list ONCE under the most specific name, put the rest in aliases.
2. NEVER INVENT NAMES. If gender is ambiguous from the name alone, leave it out rather than guessing.
3. REAL PEOPLE ONLY who appear or are directly involved in this chapter.
4. EVIDENCE: best supporting sentence copied exactly from the chapter; empty string if none — never invent, never repeat.
5. ROLE must be exactly one of: protagonist, antagonist, supporting, minor. When in doubt, supporting.
6. pov_character is ONLY someone who narrates this chapter. Most chapters have one; some have none (null)."""

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
  "quotes": [{"text": "exact notable line from the chapter", "spoiler": false}]
}

RULES:
1. TRIGGERS: emit a severity for EVERY category below — none, mentioned (referenced but not shown), on_page (depicted), graphic (depicted in disturbing detail). Be conservative: ordinary life stress is not a trigger.
   NON-EXAMPLES (do NOT flag these): children playing or play-fighting; a child getting a minor injury during normal activity; characters discussing or joking about a topic; a passing mention of someone's death; childbirth or pregnancy described without distress; pretend-play scenarios (e.g. "playing Indians", toy weapons).
   When in doubt between two severities, choose the LOWER one.
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
                    "the narrator", "narrator",
                    "my husband", "the husband", "husband",
                    "my wife", "the wife", "wife",
                    "my mother", "my father", "mom", "dad",
                    "mother", "father"}


def _roster_update(roster, characters, chapter_idx):
    """Fold one chapter's characters into the roster. Mutates roster.

    Merge rules, in order:
    1. Incoming primary name matches a known PRIMARY -> merge.
    2. Incoming name or alias is a GENERIC reference ("i", "the narrator",
       ...) matching an entry's generic alias -> merge.
    Proper-name aliases never trigger a merge (they're recorded, not trusted).
    """
    primaries = {}
    for k, e in roster.items():
        for pk in e.get("primary_keys", {k}):
            primaries[pk] = k

    for c in characters:
        name = c.get("name", "")
        nkey = norm_name(name)
        if not nkey:
            continue
        alias_keys = {norm_name(a) for a in c.get("aliases", [])}
        alias_keys.discard("")

        if nkey in primaries:
            found = primaries[nkey]
        else:
            found = None
            generic_keys = ({nkey} | alias_keys) & _GENERIC_ALIASES
            if generic_keys:
                for k, e in roster.items():
                    if generic_keys & (e["alias_keys"] & _GENERIC_ALIASES):
                        found = k
                        break

        if found is None:
            roster[nkey] = {"name": name, "aliases": set(), "alias_keys": {nkey},
                            "primary_keys": {nkey},
                            "appearances": 0, "last_seen": chapter_idx,
                            "chapters": set(),
                            "roles": Counter(), "descriptions": []}
            found = nkey
            primaries[nkey] = nkey
        e = roster[found]
        e["appearances"] += 1
        e["last_seen"] = chapter_idx
        e["chapters"].add(chapter_idx)
        e["alias_keys"].add(nkey)
        e["alias_keys"] |= alias_keys
        e["primary_keys"].add(nkey)
        for alias in {name} | set(c.get("aliases", [])):
            if alias and alias != e["name"]:
                e["aliases"].add(alias)
        if c.get("role"):
            e["roles"][c["role"]] += 1
        if c.get("description"):
            e["descriptions"].append(c["description"])
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
        characters.append({
            "name": name,
            "aliases": [a for a in (_s(x, 120) for x in _list(c.get("aliases"))) if a],
            "role": role if role in VALID_ROLES else None,
            "description": _s(c.get("description"), 1000),
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
        else:
            text, spoiler = _s(q, 600), False
        if text:
            quotes.append({"text": text, "spoiler": spoiler})
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
    if CONFIG.get("v2_prompt_cache"):
        # Chapter first (shared prefix for llama.cpp KV cache), task last.
        a = _run_task(call, V2_SHARED_SYSTEM,
                      f"CHAPTER TEXT:\n{chapter['text']}\n\nTASK:\n{prompt}",
                      idx, n_chapters, f"v2-ch{idx}-identity",
                      sanitize_fn=_sanitize_v2a)
    else:
        a = _run_task(call, prompt, chapter["text"], idx, n_chapters,
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
    if CONFIG.get("v2_prompt_cache"):
        # Chapter first (shared prefix for llama.cpp KV cache), task last.
        b = _run_task(call, V2_SHARED_SYSTEM,
                      f"CHAPTER TEXT:\n{chapter['text']}\n\nTASK:\n{PROMPT_V2_CONTENT}",
                      idx, n_chapters, f"v2-ch{idx}-content",
                      sanitize_fn=_sanitize_v2b)
    else:
        b = _run_task(call, PROMPT_V2_CONTENT, chapter["text"], idx, n_chapters,
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


def v2_reduce(chapter_as, chapter_bs, roster, chapters):
    """Deterministic reduce over per-chapter v2 results. Pure code, no LLM.

    chapter_as/bs: {chapter_index: result or None}. Returns a result dict
    shaped for write_claims()/save_preview()/resolve_work()/dedupe_characters().
    """
    n = len(chapters)
    idx_to_label = {c["index"]: c["label"] for c in chapters}

    # --- Characters: from the roster (aliases already merged) ---
    # Keep only characters with a proper name OR 3+ chapter appearances.
    # (Filters the long tail of generic one-off mentions.)
    characters = []
    for e in sorted(roster.values(), key=lambda x: -x["appearances"]):
        n_ch = len(e.get("chapters", set()))
        if not (_is_proper(e["name"]) or n_ch >= 3):
            continue
        role = e["roles"].most_common(1)[0][0] if e["roles"] else None
        desc = max(e["descriptions"], key=len) if e["descriptions"] else ""
        ev = e.get("evidence", "")
        characters.append({
            "name": e["name"],
            "aliases": sorted(e["aliases"]),
            "role": role,
            "description": desc,
            "evidence": ev,
            "evidence_verified": bool(ev),
            "evidence_offered": bool(ev),
            "appearances": e["appearances"],
            "chapters_present": n_ch,
            "confidence": _v2_char_confidence(e["appearances"], bool(ev)),
        })

    # --- Relationships: canonical-remap, dedupe by (from, to, type) ---
    seen_rel, relationships = set(), []
    for idx in sorted(chapter_as):
        a = chapter_as[idx]
        if not a:
            continue
        for r in a["relationships"]:
            frm = _roster_canonical(roster, r["from"])
            to = _roster_canonical(roster, r["to"])
            key = (frm, to, r["type"])
            if key in seen_rel:
                continue
            seen_rel.add(key)
            relationships.append({
                "from": frm, "to": to, "type": r["type"],
                "evidence": r.get("evidence", ""),
                "evidence_verified": bool(r.get("evidence")),
                "evidence_offered": bool(r.get("evidence")),
                "chapter": idx_to_label.get(idx, ""),
            })

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
    for cat in TRIGGER_CATEGORIES:
        acc = trig_acc[cat]
        if acc["sev"] == 0:
            continue
        # on_page/graphic without a verified quote is downgraded: a severity
        # claim needs textual evidence, not just the model's assertion.
        sev = acc["sev"]
        if sev >= 2 and not acc["evidence"]:
            sev = 1
        triggers.append({
            "warning": cat,
            "severity": sev_names[sev],
            "severity_claimed": sev_names[acc["sev"]],
            "chapters": acc["chapters"],
            "evidence": acc["evidence"],
            "evidence_verified": bool(acc["evidence"]),
            "evidence_offered": bool(acc["evidence"]),
            "confidence": _v2_trigger_confidence(sev, len(acc["chapters"]),
                                                 len(acc["evidence"])),
        })

    # --- Spice: 75th percentile across chapters ---
    spices = sorted(b["spice_level"] for b in chapter_bs.values() if b)
    spice_level = spices[max(0, math.ceil(0.75 * len(spices)) - 1)] if spices else 0

    # --- POVs: named in >=2 chapters (or >=1 if fewer than 4 chapters) ---
    pov_counts = Counter()
    for idx in sorted(chapter_as):
        a = chapter_as[idx]
        if a and a.get("pov_character"):
            pov_counts[_roster_canonical(roster, a["pov_character"])] += 1
    # Require POV in at least max(2, 10%) of chapters (filters one-off misfires).
    threshold = max(2, n // 10) if n >= 4 else 1
    povs = sorted(p for p, c in pov_counts.items() if c >= threshold)

    # --- Trope candidates: union across chapters (gate runs separately) ---
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

    # --- Quotes: verified, spread across chapters ---
    all_quotes = []
    for idx in sorted(chapter_bs):
        b = chapter_bs[idx]
        if b:
            for q in b["quotes"]:
                all_quotes.append({**q, "chapter": idx_to_label.get(idx, "")})
    quotes = _spread_quotes(all_quotes)

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

    return {
        "characters": characters,
        "relationships": relationships,
        "triggers": triggers,
        "spice_level": spice_level,
        "povs": povs,
        "trope_candidates": trope_candidates,
        "trope_candidate_counts": dict(trope_counts),
        "tropes": [],  # filled by the trope gate
        "trope_confidence": {},
        "quotes": quotes,
        "chapter_summaries": chapter_summaries,
        "verification": {"evidence_checked": v_checked,
                         "evidence_verified": v_verified},
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
                    conf[name] = round(min(0.9, 0.55 + 0.1 * n_ch), 2)
    return confirmed, conf


# --- v2 chapter extraction for non-EPUB (single-text) books ---
_CHAPTER_HEADING_RE = re.compile(
    r"(?m)^[ \t]*(chapter\s+\d+|chapter\s+[ivxlc]+|part\s+\d+|"
    r"prologue|epilogue)[ \t]*$", re.IGNORECASE)


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
_DIAG = threading.local()


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
        _DIAG.info = {
            "finish_reason": choice.get("finish_reason"),
            "completion_tokens": (resp.get("usage") or {}).get("completion_tokens"),
            "reasoning_content": bool(msg.get("reasoning_content")),
        }
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
        else:
            text, spoiler = _s(q, 600), False
        if text:
            quotes.append({"text": text, "spoiler": spoiler})

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
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


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
    rows = sb("tropes", params="?select=id,name,description&order=id")
    if not rows:
        print("  ERROR: could not load tropes from Supabase")
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
    return [(r["id"], r["name"], cached[r["id"]])
            for r in rows if r["id"] in cached]


def cmd_trope_map(preview_path):
    """Map a preview JSON's tropes to the catalog. Writes a review JSON;
    nothing is written to Supabase."""
    import os
    d = json.load(open(preview_path, encoding="utf-8"))
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
    threshold = CONFIG.get("trope_map_threshold", 0.78)
    mappings, proposals = [], []
    for name, vec in zip(tropes, trope_vecs):
        best, best_sim = None, -1
        for cid, cname, cvec in catalog:
            sim = _cosine(vec, cvec)
            if sim > best_sim:
                best, best_sim = (cid, cname), sim
        if best_sim >= threshold:
            mappings.append({"pipeline_trope": name, "catalog_id": best[0],
                             "catalog_name": best[1],
                             "similarity": round(best_sim, 3)})
        else:
            proposals.append({"pipeline_trope": name,
                              "nearest_catalog_id": best[0] if best else None,
                              "nearest_similarity": round(best_sim, 3) if best else None,
                              "book": d.get("title"), "isbn": d.get("isbn")})
    out = {"preview": os.path.basename(preview_path),
           "threshold": threshold,
           "mappings": mappings, "proposals": proposals}
    out_path = os.path.splitext(preview_path)[0] + "-trope-map.json"
    json.dump(out, open(out_path, "w", encoding="utf-8"), indent=2)
    print(f"  {len(mappings)} mapped, {len(proposals)} proposals -> {out_path}")


def sb(table, method="GET", data=None, params=""):
    """Returns a list on success (possibly empty) or None on any error."""
    url = f"{CONFIG['supabase_url']}/rest/v1/{table}{params}"
    headers = {
        "apikey": CONFIG["supabase_key"],
        "Authorization": f"Bearer {CONFIG['supabase_key']}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
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
    """Lowercase, strip accents and punctuation, keep letters/digits in any script."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[\W_]+", "", s.lower())


def norm_name(n):
    """Deterministic character-name key: casefold, drop leading article, tidy spaces."""
    n = (n or "").strip().casefold()
    n = re.sub(r"^(the|a|an)\s+", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


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
    "street", "park",
}


def _has_nonperson_noun(name):
    toks = set(norm_name(name).split(" "))
    return bool(toks & _NONPERSON_NOUNS)


def _is_multi(name):
    # "the host and hostess" denotes more than one person.
    return " and " in f" {norm_name(name)} "


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


def write_claims(work_id, result, trope_mappings=None):
    """Insert only what is not already there. Returns the number of failed operations.

    trope_mappings: optional dict {pipeline_trope_name: catalog_id} from
    embedding-based mapping. Falls back to _trope_lookup (exact match) when
    not provided. Unmapped tropes go to trope_proposals.
    """
    errors = 0
    _models = {"ollama": CONFIG["ollama_model"], "openrouter": CONFIG["openrouter_model"],
               "openai": CONFIG.get("openai_model", "")}
    model = _models.get(CONFIG["llm"], "")
    conf_t = result.get("trope_confidence", {})
    wrote = {"tropes": 0, "triggers": 0, "characters": 0, "proposals": 0}
    unmatched = []

    # Tropes (embedding-mapped when available, else exact lookup)
    seen = _existing("book_trope_claims", "trope_id", work_id)
    if seen is None:
        errors += 1
    else:
        rows, used = [], set(seen)
        for t in result["tropes"]:
            if trope_mappings and t in trope_mappings:
                tid = trope_mappings[t]
            else:
                tid = _trope_lookup(t)
            if tid is None:
                unmatched.append(t)
                continue
            if str(tid).lower() in used:
                continue
            used.add(str(tid).lower())
            rows.append({"work_id": work_id, "trope_id": tid, "status": "candidate",
                         "confidence": conf_t.get(_tnorm(t), 0.7),
                         "source_type": "ebook-processor",
                         "model": model, "evidence": {"source": "ebook-extraction"}})
        if rows:
            if sb("book_trope_claims", method="POST", data=rows) is None:
                errors += 1
            else:
                wrote["tropes"] = len(rows)

    # Unmapped tropes -> trope_proposals (with book provenance)
    if unmatched:
        seen_prop = sb("trope_proposals", params="?select=name_key&limit=1000") or []
        seen_keys = {r["name_key"] for r in seen_prop}
        prop_rows = []
        for t in unmatched:
            key = _tnorm(t)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            prop_rows.append({"name": t, "name_key": key,
                              "description": f"Detected by ebook processor in '{result.get('title', '')}'.",
                              "book_key": result.get("isbn") or result.get("title", ""),
                              "status": "pending"})
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
                         "source_type": "ebook-processor",
                         "model": model,
                         "evidence": {"source": "ebook-extraction",
                                      "severity": td.get("severity", ""),
                                      "chapters": td.get("chapters", [])}})
        if rows:
            if sb("book_trigger_claims", method="POST", data=rows) is None:
                errors += 1
            else:
                wrote["triggers"] = len(rows)

    # Characters (relationships are stored per character)
    rel_map = {}
    for r in result["relationships"]:
        rel_map.setdefault(r["from"].lower(), []).append({"to": r["to"], "type": r["type"]})
    seen = _existing("book_characters", "name", work_id)
    if seen is None:
        errors += 1
    else:
        rows = []
        for c in result["characters"]:
            if c["name"].lower() in seen:
                continue
            seen.add(c["name"].lower())
            rows.append({"work_id": work_id, "name": c["name"], "role": c["role"],
                         "description": c.get("description", ""),
                         "relationships": rel_map.get(c["name"].lower(), []),
                         "source_type": "ebook-processor",
                         "confidence": c.get("confidence", 0.7)})
        if rows:
            if sb("book_characters", method="POST", data=rows) is None:
                errors += 1
            else:
                wrote["characters"] = len(rows)

    # Spice -> works.spice_detected
    spice = result.get("spice_level")
    if spice is not None:
        r = sb("works", method="PATCH", params=f"?id=eq.{work_id}",
               data={"spice_detected": spice})
        if r is None:
            errors += 1

    # POVs, quotes -> book_meta.data (merge with existing)
    meta_updates = {}
    if result.get("povs"):
        meta_updates["povs"] = result["povs"]
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
            # POST with upsert
            r = sb("book_meta", method="POST",
                   params="?on_conflict=isbn",
                   data={"isbn": isbn, "data": merged})
            if r is None:
                errors += 1

    print(f"  Wrote {wrote['tropes']} tropes, {wrote['triggers']} triggers, "
          f"{wrote['characters']} characters, {wrote['proposals']} proposals (new rows only)")
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
    print(f"\nProcessing (v2): {fpath.name}")
    global _DEBUG_TAG
    _DEBUG_TAG = fpath.stem if (preview or dry_run) and CONFIG.get("debug") else None
    suffix = fpath.suffix.lower()

    if suffix == ".epub":
        units, title, author, identifiers = extract_epub_units(fpath)
        chapters = split_chapters(units)
        word_count = sum(len(u["text"].split()) for u in units)
    else:
        extracted = read_ebook(fpath)
        if extracted is None:
            return False
        text, title, author, identifiers = extracted
        chapters = _prose_chapters(text)
        word_count = len(text.split())

    print(f"  Title: {title or fpath.stem}, Chars: "
          f"{sum(len(c['text']) for c in chapters)}")
    if identifiers.get("isbn"):
        print(f"  ISBN: {identifiers['isbn']}")
    elif identifiers.get("asin"):
        print(f"  ASIN: {identifiers['asin']} (no ISBN found)")
    if not chapters:
        print("  No chapters extracted")
        return False

    # --chunks: restrict to specific chapter indices (1-based). Applied to the
    # chapter list; original numbering is kept for debug comparability.
    only = CONFIG.get("only_chunks")
    indexed = _select_chunks(chapters, only)
    if only:
        if not indexed:
            print(f"  --chunks {only}: no chapters match (1-{len(chapters)})")
            return False
        print(f"  {len(chapters)} chapters, selecting {len(indexed)} "
              f"(--chunks {only}), LLM: {CONFIG['llm']}")
    else:
        print(f"  {len(chapters)} chapters, LLM: {CONFIG['llm']}")
    n = len(indexed)

    backends = {"ollama": llm_ollama, "openrouter": llm_openrouter,
                "openai": llm_openai_compat}
    call = backends.get(CONFIG["llm"], llm_ollama)
    batch_size = max(1, CONFIG.get("batch_size", BATCH_SIZE))

    # Pass 1: Call A sequential (roster must see chapters in order).
    roster, chapter_as = {}, {}
    a_ok = a_failed = a_fallback = 0
    for i, ch in indexed:
        print(f"  Chapter {i}/{n} (A)...", end=" ", flush=True)
        a, fell_back = v2_call_a(call, roster, ch, n)
        chapter_as[i] = a
        if a is None:
            a_failed += 1
            print("FAILED")
        else:
            a_ok += 1
            if fell_back:
                a_fallback += 1
            print("done" + (" (simple)" if fell_back else ""))

    # Pass 2: Call B parallel (independent per chapter).
    chapter_bs = {}
    b_ok = b_failed = 0
    if batch_size > 1:
        print(f"  Batching {batch_size} parallel content calls...")
        with ThreadPoolExecutor(max_workers=batch_size) as ex:
            futs = {ex.submit(v2_call_b, call, ch, n): i for i, ch in indexed}
            done_count = 0
            for fut in as_completed(futs):
                i = futs[fut]
                b = fut.result()
                chapter_bs[i] = b
                done_count += 1
                if b is None:
                    b_failed += 1
                else:
                    b_ok += 1
                print(f"\r  Content: {done_count}/{n} done", end="", flush=True)
        print()
    else:
        for i, ch in indexed:
            print(f"  Chapter {i}/{n} (B)...", end=" ", flush=True)
            b = v2_call_b(call, ch, n)
            chapter_bs[i] = b
            if b is None:
                b_failed += 1
                print("FAILED")
            else:
                b_ok += 1
                print("done")

    failed = a_failed + b_failed
    aborted = (a_ok == 0 and b_ok == 0) or failed / max(n, 1) > MAX_FAILED_CHUNK_RATIO
    if aborted:
        print(f"  {failed}/{2 * n} calls failed; not writing partial results")

    # Deterministic reduce (no LLM).
    red = v2_reduce(chapter_as, chapter_bs, roster,
                    [ch for _, ch in indexed])

    # Trope confirmation: frequency tiers, LLM gate only for the borderline.
    # - 5+ chapters: auto-confirm (a recurring pattern is a trope).
    # - 2-4 chapters: LLM gate judges against summaries.
    # - 1 chapter: drop (not a book-level trope).
    tropes, trope_conf = [], {}
    if not dry_run and red["trope_candidates"]:
        counts = red["trope_candidate_counts"]
        auto, gated, dropped = [], [], 0
        for c in red["trope_candidates"]:
            n = counts.get(_tnorm(c), 1)
            if n >= 5:
                auto.append(c)
            elif n >= 2:
                gated.append(c)
            else:
                dropped += 1
        print(f"  Tropes: {len(auto)} auto (5+ ch), {len(gated)} to gate, "
              f"{dropped} single-ch dropped...", end=" ", flush=True)
        for c in auto:
            tropes.append(c)
            trope_conf[c] = round(min(0.9, 0.55 + 0.1 * counts.get(_tnorm(c), 5)), 2)
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
    }
    v = red["verification"]
    if v["evidence_checked"]:
        print(f"  Evidence: {v['evidence_verified']}/{v['evidence_checked']} verified")

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

    print(f"  Found: {len(tropes)} tropes, {len(red['triggers'])} triggers, "
          f"{len(red['characters'])} characters ({len(roster)} roster)")
    smsg = f" ({a_fallback} via simple fallback)" if a_fallback else ""
    print(f"  Tasks: content {b_ok}/{n} ok, identity {a_ok}/{n} ok{smsg}")
    print(f"  Spice: {red['spice_level']}/5, POVs: {', '.join(red['povs']) or 'none'}, "
          f"~{reading_mins}min read")

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

    if dry_run:
        print("  DRY RUN — nothing written")
        return not aborted
    if preview:
        save_preview(fpath, result)
        print(f"  Preview saved (pipeline=v2)")
        return not aborted
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
    print(f"\nProcessing: {fpath.name}")
    global _DEBUG_TAG
    _DEBUG_TAG = fpath.stem if (preview or dry_run) and CONFIG.get("debug") else None
    extracted = read_ebook(fpath)
    if extracted is None:
        return False
    text, title, author, identifiers = extracted

    print(f"  Title: {title or fpath.stem}, Chars: {len(text)}")
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
        print(f"  {n} chunks, LLM: {CONFIG['llm']}")
    n = len(indexed)

    trope_hits, trope_name = Counter(), {}
    trig_hits, trig_data = Counter(), {}
    char_hits, char_data = Counter(), {}
    rel_seen, rel_idx, relationships = set(), {}, []
    povs, quotes, spice_votes = set(), [], []
    is_anth, stories = False, []
    ok = failed = 0
    v_checked = v_verified = v_dropped = 0
    task_ok = {"discipline": 0, "identity": 0, "identity_simple": 0}

    batch_size = max(1, CONFIG.get("batch_size", BATCH_SIZE))

    def _run_chunk(args):
        idx, ch = args
        return (idx, extract_llm(ch, idx, n))

    # Collect results (parallel when batch_size > 1)
    results = {}
    if batch_size > 1:
        print(f"  Batching {batch_size} parallel requests...")
        with ThreadPoolExecutor(max_workers=batch_size) as ex:
            futs = {ex.submit(_run_chunk, (i, ch)): i for i, ch in indexed}
            done_count = 0
            for fut in as_completed(futs):
                idx, r = fut.result()
                results[idx] = r
                done_count += 1
                print(f"\r  Chunks: {done_count}/{n} done", end="", flush=True)
        print()  # newline after progress
    else:
        for i, ch in indexed:
            print(f"  Chunk {i}/{n}...", end=" ", flush=True)
            _, r = _run_chunk((i, ch))
            results[i] = r
            print("done" if r else "FAILED")

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

    print(f"  Found: {len(tropes)} tropes, {len(triggers)} triggers, {len(characters)} characters")
    smsg = f" ({task_ok['identity_simple']} via simple fallback)" if task_ok["identity_simple"] else ""
    print(f"  Tasks: discipline {task_ok['discipline']}/{n} ok, identity {task_ok['identity']}/{n} ok{smsg}")
    print(f"  Spice: {spice_level}/5, POVs: {', '.join(sorted(povs)) or 'none'}, ~{reading_mins}min read")

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
        md.append(f"**ISBN:** {result['isbn']}")
    elif result.get("asin"):
        md.append(f"**ASIN:** {result['asin']} (no ISBN in file)")
    else:
        md.append("**ISBN:** not found in file")
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
    if result["relationships"]:
        md.append(f"## Relationships ({len(result['relationships'])})")
        for r in result["relationships"]:
            md.append(f"- {r['from']} → {r['to']}: {r['type']}")
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


def run_once(dry_run, preview):
    files = sorted(p for p in IMPORT_DIR.iterdir()
                   if p.is_file() and p.suffix.lower() in (".epub", ".mobi", ".azw3"))
    if not files:
        print("No ebooks found.")
        return 0
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
    ap.add_argument("--chunks", default=None,
                    help="process only these chunk indices (e.g. --chunks 2 or --chunks 2,5,8); "
                         "numbering matches the sampled chunk list and debug files")
    ap.add_argument("--pipeline", default=None, choices=["legacy", "v2"],
                    help="extraction pipeline: legacy (fixed chunks) or v2 "
                         "(chapter-level map/reduce with character roster)")
    ap.add_argument("--debug", action="store_true",
                    help="save raw LLM output of failed chunks to preview/debug/")
    ap.add_argument("--dedupe", action="store_true",
                    help="merge duplicate characters (name normalization + "
                         "embeddings) before writing; needs embed_url/embed_model")
    ap.add_argument("--trope-map", metavar="PREVIEW_JSON",
                    help="map a preview file's tropes to the Cozy Libram catalog "
                         "via embeddings; writes a review JSON, nothing to Supabase")
    args = ap.parse_args()

    if args.trope_map:
        load_config()
        cmd_trope_map(args.trope_map)
        return

    if args.llm:
        CONFIG["llm"] = args.llm
    if args.full:
        CONFIG["sample_rate"] = 1
    elif args.sample:
        CONFIG["sample_rate"] = args.sample
    if args.batch:
        CONFIG["batch_size"] = args.batch
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
                run_once(args.dry_run, args.preview)
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        sys.exit(1 if run_once(args.dry_run, args.preview) else 0)


if __name__ == "__main__":
    main()
