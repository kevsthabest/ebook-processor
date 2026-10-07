#!/usr/bin/env python3
"""
Ebook processor for Cozy Libram — extracts tropes, trigger warnings, and
characters from EPUB files and writes them to Supabase.

Watches ~/workspace/ebook-import/ for .epub, .mobi, and .azw3 files, processes them through
OpenRouter, and stores structured claims in the works/claims tables.

Usage:
  python3 process.py              # process all .epub in the import folder once
  python3 process.py --watch      # watch continuously (polls every 60s)
  python3 process.py --dry-run    # extract but don't write to Supabase

The script is source-agnostic: it processes whatever EPUB files appear in
the import folder. How those files get there is the operator's responsibility.
"""

import os
import sys
import json
import time
import zipfile
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

# Config
IMPORT_DIR = Path.home() / "workspace" / "ebook-import"
DONE_DIR = IMPORT_DIR / "done"
FAILED_DIR = IMPORT_DIR / "failed"
CHUNK_CHARS = 8000  # ~2000 tokens
OPENROUTER_MODEL = "qwen/qwen3-27b"  # free tier, vision-capable per v293
OLLAMA_HOST = "http://localhost:11434"  # override with --ollama-host
OLLAMA_MODEL = "qwen2.5-coder:7b"  # override with --ollama-model
LLM_BACKEND = "openrouter"  # or "ollama", override with --llm
CHAT_PY = Path.home() / "workspace" / "skills" / "openrouter" / "bin" / "chat.py"
SB_QUERY = Path.home() / "workspace" / "skills" / "supabase" / "bin" / "sb-query"
SUPABASE_REF = "dvhimjkrroxuatthiizc"

for d in [IMPORT_DIR, DONE_DIR, FAILED_DIR]:
    d.mkdir(parents=True, exist_ok=True)


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = False
        elif tag in ('p', 'div', 'br', 'h1', 'h2', 'h3', 'h4'):
            self.text.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)

    def get_text(self):
        return ''.join(self.text)


def extract_epub_text(epub_path):
    """Extract clean text from an EPUB file."""
    texts = []
    # Also try to get title/author from OPF
    title, author = None, None
    with zipfile.ZipFile(epub_path, 'r') as z:
        # Try to find OPF for metadata
        try:
            container = z.read('META-INF/container.xml').decode('utf-8')
            opf_path = re.search(r'full-path="([^"]+)"', container)
            if opf_path:
                opf = z.read(opf_path.group(1)).decode('utf-8', errors='ignore')
                tm = re.search(r'<dc:title[^>]*>([^<]+)</dc:title>', opf)
                am = re.search(r'<dc:creator[^>]*>([^<]+)</dc:creator>', opf)
                if tm: title = tm.group(1).strip()
                if am: author = am.group(1).strip()
        except:
            pass

        html_files = [n for n in z.namelist()
                      if n.lower().endswith(('.html', '.xhtml', '.htm'))]
        html_files.sort()
        for fname in html_files:
            try:
                content = z.read(fname).decode('utf-8', errors='ignore')
                parser = TextExtractor()
                parser.feed(content)
                text = parser.get_text()
                text = re.sub(r'\n\s*\n', '\n\n', text)
                text = re.sub(r'[ \t]+', ' ', text)
                if len(text.strip()) > 100:
                    texts.append(text.strip())
            except Exception as e:
                print(f"  Warning: {fname}: {e}")
    return '\n\n'.join(texts), title, author


def chunk_text(text, chunk_size=CHUNK_CHARS):
    chunks = []
    paras = text.split('\n\n')
    current, current_len = [], 0
    for para in paras:
        if current_len + len(para) > chunk_size and current:
            chunks.append('\n\n'.join(current))
            current, current_len = [para], len(para)
        else:
            current.append(para)
            current_len += len(para)
    if current:
        chunks.append('\n\n'.join(current))
    return chunks


EXTRACTION_PROMPT = """You are analyzing an excerpt from a book. Extract the following as JSON:

{
  "tropes": ["trope name", ...],
  "triggers": ["trigger warning", ...],
  "characters": [{"name": "...", "role": "protagonist/antagonist/supporting", "description": "..."}],
  "is_anthology": false,
  "stories": []
}

Tropes are recurring narrative patterns (e.g., "enemies to lovers", "morally grey protagonist", "found family").
Trigger warnings are content that might distress readers (e.g., "graphic violence", "sexual assault", "self-harm").
Characters are named individuals with significant roles.

If this is an ANTHOLOGY or COLLECTION (multiple independent stories, e.g., Stephen King short story collections):
- Set "is_anthology" to true
- List each story in "stories": [{"title": "...", "author": "..."}]

Be specific but concise. Only include what you're confident about from THIS excerpt.
Return ONLY valid JSON, no other text."""


def extract_with_ollama(chunk, chunk_num, total_chunks):
    """Call local Ollama to extract structured data."""
    import urllib.request
    prompt = EXTRACTION_PROMPT + f"\n\nExcerpt {chunk_num} of {total_chunks}:\n\n{chunk}"
    data = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": 2048}
    }).encode('utf-8')
    try:
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            output = result.get('response', '')
            json_match = re.search(r'\{.*\}', output, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
    except Exception as e:
        print(f"  Ollama error: {e}")
    return {'tropes': [], 'triggers': [], 'characters': []}


def extract_with_llm(chunk, chunk_num, total_chunks):
    """Dispatch to configured LLM backend."""
    if LLM_BACKEND == "ollama":
        return extract_with_ollama(chunk, chunk_num, total_chunks)
    # OpenRouter via chat.py
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as sf:
        sf.write(EXTRACTION_PROMPT)
        sys_path = sf.name
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as uf:
        uf.write(f"Excerpt {chunk_num} of {total_chunks}:\n\n{chunk}")
        user_path = uf.name
    try:
        result = subprocess.run(
            [sys.executable, str(CHAT_PY),
             '--model', OPENROUTER_MODEL,
             '--system', sys_path,
             '--user', user_path,
             '--max-tokens', '1000'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            print(f"  LLM error: {result.stderr[:200]}")
            return {'tropes': [], 'triggers': [], 'characters': []}
        # Parse JSON from output
        output = result.stdout.strip()
        # Try to find JSON in the output
        json_match = re.search(r'\{.*\}', output, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
        return {'tropes': [], 'triggers': [], 'characters': []}
    except Exception as e:
        print(f"  LLM exception: {e}")
        return {'tropes': [], 'triggers': [], 'characters': []}
    finally:
        os.unlink(sys_path)
        os.unlink(user_path)


def sb(sql):
    """Run a Supabase query via sb-query."""
    result = subprocess.run(
        [str(SB_QUERY), SUPABASE_REF, sql],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"  Supabase error: {result.stderr[:200]}")
        return None
    try:
        return json.loads(result.stdout)
    except:
        return result.stdout


def find_or_create_work(title, author):
    """Find existing work by title/author, or create it."""
    if not title:
        return None
    # Normalize for matching
    title_norm = re.sub(r'[^a-z0-9]', '', title.lower())
    author_norm = re.sub(r'[^a-z0-9]', '', (author or '').lower())

    # Try to find
    rows = sb(f"SELECT id FROM works WHERE title_norm = '{title_norm}' AND author_norm = '{author_norm}' LIMIT 1")
    if rows and len(rows) > 0:
        return rows[0]['id']

    # Create
    authors_json = json.dumps([author] if author else [])
    result = sb(f"""
        INSERT INTO works (title, title_norm, authors, author_norm, provider_ids)
        VALUES ('{title.replace(chr(39), chr(39)*2)}', '{title_norm}', '{authors_json}', '{author_norm}', '{{}}')
        RETURNING id
    """)
    if result and len(result) > 0:
        return result[0]['id']
    return None


def write_claims(work_id, tropes, triggers, characters, dry_run=False):
    """Write extracted data to Supabase."""
    if dry_run:
        print(f"  [DRY RUN] Would write {len(tropes)} tropes, {len(triggers)} triggers, {len(characters)} characters")
        return

    # Tropes -> book_trope_claims (need trope_id from tropes table)
    for trope_name in tropes:
        # Find trope by name (fuzzy)
        trope_rows = sb(f"SELECT id FROM tropes WHERE LOWER(name) LIKE '%{trope_name.lower().replace(chr(39), chr(39)*2)}%' LIMIT 1")
        if trope_rows and len(trope_rows) > 0:
            trope_id = trope_rows[0]['id']
            sb(f"""
                INSERT INTO book_trope_claims (work_id, trope_id, status, confidence, source_type, model, evidence)
                VALUES ('{work_id}', '{trope_id}', 'candidate', 0.7, 'import', '{OPENROUTER_MODEL}', '{{"source": "ebook-extraction"}}')
                ON CONFLICT DO NOTHING
            """)

    # Trigger warnings -> book_trigger_claims
    for warning in triggers:
        w_esc = warning.replace(chr(39), chr(39)*2)
        sb(f"""
            INSERT INTO book_trigger_claims (work_id, warning, status, confidence, source_type, model, evidence)
            VALUES ('{work_id}', '{w_esc}', 'candidate', 0.7, 'import', '{OPENROUTER_MODEL}', '{{"source": "ebook-extraction"}}')
            ON CONFLICT (work_id, warning) DO NOTHING
        """)
    if triggers:
        print(f"  Wrote {len(triggers)} trigger claims")

    # Characters -> book_characters
    for c in characters:
        name = c.get('name', '').replace(chr(39), chr(39)*2)
        if not name:
            continue
        role = c.get('role', '')
        if role not in ('protagonist', 'antagonist', 'supporting', 'minor'):
            role = None
        desc = (c.get('description') or '').replace(chr(39), chr(39)*2)
        role_sql = f"'{role}'" if role else "NULL"
        sb(f"""
            INSERT INTO book_characters (work_id, name, role, description, source_type, confidence)
            VALUES ('{work_id}', '{name}', {role_sql}, '{desc}', 'import', 0.7)
            ON CONFLICT (work_id, name) DO NOTHING
        """)
    if characters:
        print(f"  Wrote {len(characters)} characters")


def process_ebook(fpath, dry_run=False):
    print(f"\nProcessing: {fpath.name}")
    # Convert MOBI/AZW3 to EPUB first
    tmp_epub = None
    epub_path = fpath
    if fpath.suffix.lower() in ('.mobi', '.azw3'):
        print(f"  Converting {fpath.suffix} to EPUB...")
        try:
            from mobi import extract as mobi_extract
            tmp_epub = mobi_extract(str(fpath))
            epub_path = Path(tmp_epub)
            print(f"  Converted to {epub_path}")
        except Exception as e:
            print(f"  Conversion failed: {e}")
            return False
    try:
        text, title, author = extract_epub_text(epub_path)
    finally:
        # Clean up temp EPUB
        if tmp_epub and Path(tmp_epub).exists():
            try:
                # mobi.extract creates a directory, not a file
                import shutil
                if Path(tmp_epub).is_dir():
                    shutil.rmtree(tmp_epub)
                else:
                    Path(tmp_epub).unlink()
            except:
                pass
    print(f"  Title: {title or 'unknown'}, Author: {author or 'unknown'}")
    print(f"  Extracted {len(text)} characters")
    if len(text) < 1000:
        print("  Too short, skipping")
        return False

    chunks = chunk_text(text)
    print(f"  Split into {len(chunks)} chunks")

    all_tropes, all_triggers = set(), set()
    all_characters = {}
    for i, chunk in enumerate(chunks, 1):
        print(f"  Chunk {i}/{len(chunks)}...", end=' ', flush=True)
        result = extract_with_llm(chunk, i, len(chunks))
        for t in result.get('tropes', []):
            all_tropes.add(t)
        for t in result.get('triggers', []):
            all_triggers.add(t)
        for c in result.get('characters', []):
            name = c.get('name', '')
            if name and name not in all_characters:
                all_characters[name] = c
        print("done")

    print(f"  Found: {len(all_tropes)} tropes, {len(all_triggers)} triggers, {len(all_characters)} characters")

    if not dry_run and title:
        work_id = find_or_create_work(title, author)
        if work_id:
            write_claims(work_id, all_tropes, all_triggers, all_characters, dry_run)
            print(f"  Written to work {work_id}")
        else:
            print("  Could not create/find work")
    elif dry_run:
        print(f"  [DRY RUN] Tropes: {list(all_tropes)[:5]}")
        print(f"  [DRY RUN] Triggers: {list(all_triggers)[:5]}")

    return True


def main():
    global LLM_BACKEND, OLLAMA_HOST, OLLAMA_MODEL
    dry_run = '--dry-run' in sys.argv
    watch = '--watch' in sys.argv
    # Parse --llm, --ollama-host, --ollama-model
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == '--llm' and i + 1 < len(args):
            LLM_BACKEND = args[i + 1]
        elif a == '--ollama-host' and i + 1 < len(args):
            OLLAMA_HOST = args[i + 1]
        elif a == '--ollama-model' and i + 1 < len(args):
            OLLAMA_MODEL = args[i + 1]
    print(f"LLM backend: {LLM_BACKEND}" + (f" ({OLLAMA_MODEL} @ {OLLAMA_HOST})" if LLM_BACKEND == "ollama" else f" ({OPENROUTER_MODEL})"))

    def run_once():
        # Support EPUB, MOBI, and AZW3
        files = list(IMPORT_DIR.glob('*.epub')) + list(IMPORT_DIR.glob('*.mobi')) + list(IMPORT_DIR.glob('*.azw3'))
        if not files:
            print("No ebook files found (.epub, .mobi, .azw3).")
            return
        for fpath in files:
            try:
                if process_ebook(fpath, dry_run):
                    if not dry_run:
                        shutil.move(str(fpath), str(DONE_DIR / fpath.name))
                        print("  Moved to done/")
                else:
                    shutil.move(str(fpath), str(FAILED_DIR / fpath.name))
                    print("  Moved to failed/")
            except Exception as e:
                print(f"  Error: {e}")
                import traceback
                traceback.print_exc()

    if watch:
        print(f"Watching {IMPORT_DIR} (Ctrl+C to stop)...")
        while True:
            run_once()
            time.sleep(60)
    else:
        run_once()


if __name__ == '__main__':
    main()
