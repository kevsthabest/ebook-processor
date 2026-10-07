# Ebook Processor for Cozy Libram

Extracts canonical metadata — tropes, trigger warnings, characters,
relationships, spice level, POVs, quotes — from EPUB/MOBI/AZW3 files and
writes it to Cozy Libram's Supabase tables. Source-agnostic: it processes
whatever ebook files you hand it. **Preview first, write later** — every run
can save a full Markdown/JSON report for human review before anything
touches the database.

## How it works

1. Drop ebook files into `import/`
2. Run `python process-portable.py --preview` (or `--watch` for continuous)
3. For each book:
   - Extracts text and metadata (EPUB OPF, MOBI/AZW3 EXTH incl. ISBN)
   - Chunks into ~2000-token pieces (configurable sampling for quick tests)
   - Sends each chunk to the LLM **twice** with focused prompts:
     - *discipline task*: tropes, triggers, spice, POVs, quotes, anthology
     - *identity task*: characters + relationships, with evidence quotes
   - Aggregates across chunks, optionally dedupes characters via embeddings
   - Resolves the work (ISBN → edition → work, else title/author match)
   - Writes trope claims as `candidate`, characters/relationships to their tables
4. Processed files move to `import/done/`, failures to `import/failed/`

## Setup

1. Python 3.8+ (stdlib only — no pip packages required for EPUB)
2. For MOBI/AZW3: `pip install mobi`
3. Copy `config.example.json` → `config.json` and fill in:
   - `supabase_url` + `supabase_key` (service role)
   - LLM backend (see below)
   - Optional: `embed_url`/`embed_model`/`dedupe_threshold` for `--dedupe`

## LLM backends

| Flag | Backend | Notes |
|---|---|---|
| `--llm ollama` | Ollama `/api/chat` | default model `qwen2.5-coder:7b`, override with `--ollama-model` |
| `--llm openrouter` | OpenRouter | set `openrouter_model` + `openrouter_key` |
| `--llm openai` | Any OpenAI-compatible endpoint | `openai_base_url` + `openai_model` (+ `openai_key`); used for Unsloth |

Best results so far: **Turbo Brilliance via Unsloth** (`--llm openai`,
`openai_base_url=http://127.0.0.1:8888/v1`).

## Usage

```bash
# Quick quality test: 17 sampled chunks, preview only, nothing written
python process-portable.py --batch 4 --sample 3 --preview --llm openai

# Full run with character dedup (needs embedding endpoint in config)
python process-portable.py --preview --dedupe --llm openai

# Debug failed chunks (raw model output → preview/debug/)
python process-portable.py --preview --debug --llm openai

# Real run: writes to Supabase, moves files to import/done/
python process-portable.py --llm openai

# Watch import/ continuously (checks every 60s)
python process-portable.py --watch --llm openai
```

Key flags: `--preview` (save report, no DB writes), `--dry-run` (extract,
skip writes, no preview files), `--sample N` (1 of every N chunks),
`--batch N` (parallel requests), `--full` (ignore sampling),
`--debug` (save raw failed-chunk output), `--dedupe` (embedding merge),
`--max-fail-ratio` (abort threshold, default 0.5).

## Two-task extraction (v1.19+)

One mega-prompt made small models conflate everything. Each chunk now gets
two focused calls:

- **discipline** (`PROMPT_DISCIPLINE`): tropes, triggers, spice level, POVs,
  quotes, anthology detection — the things small models do well.
- **identity** (`PROMPT_IDENTITY`): characters + relationships only, with
  entity-merging / pronoun-resolution / no-invented-names rules. Each claim
  should carry an `evidence` quote copied exactly from the excerpt (soft
  requirement since v1.20 — empty string allowed, never invented).

A failed task never invalidates the other. If the rich identity prompt
fails twice on a chunk, one attempt is made with a simplified evidence-free
fallback prompt (`PROMPT_IDENTITY_SIMPLE`) — names without quotes beat no
characters at all. Per-task ok counts (and fallback recoveries) print in
the console and land in the preview.

## Work identity (ISBN)

The ISBN is read from the file itself — EPUB `<dc:identifier>`, MOBI/AZW3
EXTH 104 (ASIN from EXTH 113 is reported). ISBN-10 converts to ISBN-13,
stored digits-only. Resolution order:

1. ISBN → `editions.isbn` → `work_id`
2. Exact normalized title + author match on `works`
3. No match → preview reports "NEW WORK would be created" + up to 5
   similar-title candidates

New works store the ISBN in `provider_ids`. Preview/dry-run resolution is
read-only.

## Character dedup (--dedupe)

Small models fragment identities ("Narrator" vs "Shirley Jackson" vs "I").
With an embedding model serving OpenAI-compatible `/v1/embeddings`, the
script merges duplicates after extraction, before writing. Two signals:
deterministic name normalization + embedding cosine similarity (default
threshold 0.85). Guards: never merge across detectable genders, distinct
proper names, non-person nouns, or multi-person names. Relationships and
POVs remap to canonical names; merges are listed in the preview. A failed
dedup aborts the file rather than writing partial results.

Dedup is janitorial, not a cure — it can't fix model-level coreference
failures. Review the preview before any real `--dedupe` write.

## Aborted runs still save a preview

If the chunk-failure ratio is exceeded, whatever succeeded is still
aggregated and saved as a preview marked `"aborted": true`. Partial
results are never written to Supabase.

## Notes

- Trope claims are written as `candidate` status for review in Trope Lab.
- Anthologies are detected per chunk; stories link as separate works via
  the `edition_works` junction table.
- Each full novel costs roughly 100k tokens through the LLM.
