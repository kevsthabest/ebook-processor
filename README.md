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
   - Writes to Supabase: `book_trope_claims` (status `candidate`, source
     `ai`), `trope_proposals` (unmapped names with book provenance),
     `book_trigger_claims` (source `ai`), `book_characters`,
     `works.spice_detected`, `book_meta` (POVs, series, up to 20 notable
     quotes), and `book_quotes` (3–5 memorable quotes per book, source
     `pipeline`). Relationships are no longer written — the
     `character_relationships` table was archived (`..._archive_20261009`).
4. Processed files move to `import/done/`, failures to `import/failed/`

## Setup

1. Python 3.8+ (stdlib only — no pip packages required for EPUB)
2. For MOBI/AZW3: `pip install mobi`
3. For the rich terminal UI (progress bars, summary tables): `pip install rich`
   (optional — falls back to plain output if not installed)
4. Copy `config.example.json` → `config.json` and fill in:
   - `supabase_url` + `supabase_key` (service role)
   - LLM backend (see below)
   - Optional: `embed_url`/`embed_model`/`dedupe_threshold` for `--dedupe`

## LLM backends

| Flag | Backend | Notes |
|---|---|---|
| `--llm ollama` | Ollama `/api/chat` | default model `qwen2.5-coder:7b`, override with `--ollama-model` |
| `--llm openrouter` | OpenRouter | set `openrouter_model` + `openrouter_key` |
| `--llm openai` | Any OpenAI-compatible endpoint | `openai_base_url` + `openai_model` (+ `openai_key`); used for Unsloth |

Current stack: local Qwen3.5-4B Q4 via Unsloth (`--llm openai`,
`openai_base_url=http://127.0.0.1:8888/v1`) for Phase A extraction — it
beat hosted models on extraction reliability. Decision-model work (merge
judgments, relationship validation) uses `typesafe/jev-1.13` on OpenRouter
(`--decision-model-provider openrouter`) or the local Unsloth server
(default). Turbo Brilliance was evaluated and rejected 2026-10-03.

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
`--chunks 2` or `--chunks 2,5,8` (process only those chunk indices —
numbering matches the sampled list and debug files; handy for re-testing
one failing chunk), `--debug` (save raw failed-chunk output),
`--dedupe` (embedding merge), `--trope-map PREVIEW_JSON` (map a preview's
tropes to the catalog via embeddings; writes a `<preview>-trope-map.json`
review file, nothing to Supabase).

The abort threshold is the `MAX_FAILED_CHUNK_RATIO = 0.5` constant
(`:169`), not a flag; aborted runs still save a preview marked
`"aborted": true`.

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

## v2 chapter pipeline (`--pipeline v2`)

The legacy path chunks the book into fixed 8000-char pieces. The v2 path
works at chapter level:

1. **Chapter splitting** — EPUB: per-spine units via `extract_epub_units()`,
   then `split_chapters()` (front/back-matter skip by filename heuristic,
   sub-1500-char units merged forward, oversized units split on paragraph
   boundaries, labels from the first heading). MOBI/AZW3: `_prose_chapters()`
   splits on chapter/part headings, falling back to fixed-size paragraph
   splits. Legacy chunking is untouched.
2. **Call A (characters)** — per chapter, in order, with a running
   **character roster** in the prompt (alias-aware merging, capped by token
   budget, recency+frequency priority). Carries the simplified evidence-free
   fallback prompt; fallback rates are reported.
3. **Call B (content)** — per chapter, parallelizable: summary, per-category
   trigger severities (24-category closed taxonomy, every category explicit),
   spice 0-5, trope candidates, quotes.
4. **Deterministic reduce** (pure code, no LLM): max trigger severity per
   category with chapter counts and verified evidence; POVs named in 2+
   chapters; relationship dedup with roster remap; evidence-based confidence
   (documented formulas in code).
   - **Spice:** peak-chapter intensity × fraction-at-that-intensity via
     `_SPICE_MATRIX` (0–5; `_spice_level_from_chapters` at `:821`, matrix at
     `:783`). Triggers warn that content *exists*; spice measures
     *pervasiveness*. The legacy pipeline still uses 75th percentile
     (`:6755`).
   - **Trigger prominence:** low/medium/high via `_PROMINENCE_MATRIX`
     (`:855`) for the app UI.
5. **Trope confirmation gate** — candidates are tiered by chapter count
   (thresholds scale with book length). `≥ max(3, n // 20)` chapters (5% of
   chapters) auto-confirms — a recurring pattern is a trope. Borderline
   candidates (2 to `auto_min - 1` chapters) go to the LLM gate: one call per
   batch of 30 candidates judges each against the chapter summaries
   (yes/no/unsure); only "yes" becomes a claim. Single-chapter candidates
   drop in long books (`≥ 20` chapters). Genre/setting labels are denylisted
   first (`_is_denylisted_trope`). Before tiering, catalog-ID mapping via
   embeddings (`trope_map_threshold` 0.78) collapses paraphrases to one
   display name so chapter counts don't split.

**Context window matters:** v2 chapters default to 16000 chars (~4k tokens)
via `v2_max_chapter_chars`, sized to leave room in an 8192-token context for
the prompt plus the model's thinking and JSON output. If your server runs a
larger context (e.g. Unsloth with Context Length raised), raise
`v2_max_chapter_chars` proportionally — roughly
`(context_window - 4000) * 4` chars.

Reasoning-model handling (both pipelines): `llm_json_schema` tries
`response_format: json_schema` first and falls back to `json_object` then
unconstrained on HTTP 400 (cached per session). If a response comes back
empty with `finish_reason=length`, the next attempt retries with a doubled
token budget (thread-local, `--batch` safe).

Compare pipelines on a known book:

```bash
python process-portable.py --preview --llm openai --pipeline legacy book.epub
python process-portable.py --preview --llm openai --pipeline v2 book.epub
```

Default is `legacy` (also settable via `pipeline` in `config.json`).

### Evidence verification (v1.22+, per-chunk since v1.22.1)

Every evidence quote and notable quote is checked against the chunk text it
came from, right after extraction and before aggregation: normalization
(casefold, curly/straight quote and dash unification, whitespace collapsing,
trailing punctuation stripped), exact substring match first, then a light
fuzzy fallback for minor transcription slips (short quotes under 12 chars
use exact match only). Fabricated quotes are dropped before the quote cuts,
so the top-10 only ever contains verified lines; the claim is kept but
flagged `evidence_verified: false`. During aggregation, the first *verified*
evidence wins per character/relationship. Verification stats
(verified/checked, quotes dropped) print in the console and appear in the
preview report.

### Empty-response diagnostics (v1.22+)

If the OpenAI-compatible backend returns an empty response, the console now
reports *why*: `finish_reason`, `completion_tokens`, and whether the server
split thinking into `reasoning_content`. A model that spends its whole
token budget thinking shows up as
`(empty: finish_reason=length, completion_tokens=8000, reasoning_content=yes)`.
With `--debug`, the same forensics land in the chunk's debug file.

**Troubleshooting empty responses** — re-run one failing chunk with each
change in isolation:
1. Double `llm_max_tokens` in `config.json` (thinking ate the budget).
2. Set `llm_response_format` to `"none"` (the JSON constraint is fighting `<think>`).
3. Set `llm_system_prefix` to a verbosity tag such as `{REASON:ilow}`
   (supported by some Unsloth-hosted reasoning models).

New config keys: `llm_max_tokens` (default 8000), `llm_system_prefix`
(default ""), `llm_response_format` (default `"json_object"`, or `"none"`).
Int-typed keys (`llm_max_tokens`, `sample_rate`, `sample_edges`,
`batch_size`) can also be set via environment as `EBOOK_LLM_MAX_TOKENS`
etc.; invalid values warn and keep the default.

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

Flags: `--isbn` overrides the ISBN for work resolution (takes priority over
EPUB metadata and auto-lookup). `--allow-no-isbn` processes books with no
ISBN instead of skipping them; `--rename-no-isbn` renames skipped no-ISBN
EPUBs to `NO-ISBN-{original}.epub` for triage. When the ISBN isn't in the
file, the script tries an Open Library auto-lookup (title + author) before
giving up. `inject_isbn.py` embeds an ISBN into an EPUB's OPF metadata:

```bash
py inject_isbn.py "book.epub" 9781615871100
```

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
failures. Review the preview before any real `--dedupe` write. Embeddings
run on Kevin's PC only: `embed_url` defaults to `127.0.0.1:8888` in
`config.example.json`, so dedup and catalog mapping won't work from
elsewhere.

## Characters

- `--principals-only` (default on) writes only principal characters to
  Supabase; minors stay in the preview JSON. `--no-principals-only` writes
  all characters. `--min-frequency 0.20` sets the chapter-frequency floor
  for principal status.
- Roles are deterministic (`_char_deterministic_role`): POV = protagonist,
  ≥30% of chapters = protagonist, 10–30% = supporting, <10% = minor. An
  LLM "antagonist" label is kept as a subtype on protagonist/supporting.
- `--alias "Loki=Brian Gragg"` records a manual character alias: repeatable,
  stored as human-validated, applies cross-book.

## Decision model & character merge

The decision model handles judgment calls that rules can't:

- `--decision-model` — validate family-type relationships with the decision
  model (uses the same Unsloth server as `openai_base_url`).
- `--decision-model-provider {local,openrouter}` — `local` (default) uses the
  local Unsloth `/v1/systemone` server; `openrouter` uses hosted
  `typesafe/jev-1.13` (needs `openrouter_api_key` in `config.json` — falls
  back to `openrouter_key` — or the `OPENROUTER_API_KEY` env var).
- `--decision-model-url URL` — point the local provider at a specific Unsloth
  server (defaults to the config `openai_base_url`).

Character merging is 3-tiered:

1. **Tier 1 hard-NO** — same-chapter co-occurrence (two names listed in one
   chapter don't merge; catches the surname-family trap).
2. **Tier 2 deterministic YES** — title stripping ("Dr. X" → "X"), middle
   initials, spelling variants, nicknames.
3. **Tier 3 Qwen** — ambiguous pairs judged by the decision model at a
   0.85 threshold, capped at 50 calls per book.

Human labels always outrank automatic merges.

### Relationship validation

Family-type relationships (`spouse`, `parent`, `child`, `sibling`) get a
decision-model score: P ≥ 0.70 keeps the type, 0.30 ≤ P < 0.70 downgrades to
`"other"`, P < 0.30 drops the relationship (likely hallucinated). No
evidence = downgrade without spending a decision-model call. In preview mode
nothing changes; scores and notes land in the preview for audit.
`--show-matrices` renders the spice/prominence matrices as ASCII during the
run.

## Cross-run learning

Learning is on by default (`--learn`); `--no-learn` disables it.

- `--learn-dir DIR` — learning state directory (default
  `~/.ebook-processor/learned/`).
- `--import-labels PATH` — import hand-labeled merge pairs
  (`label-merges.py` output) into the nickname dictionary, then continue.

`LearnedState` persists four kinds of knowledge: `nicknames.json` (short
name → canonical name), `titles.json` (trusted title prefixes),
`threshold_stats.json` (per-trigger keep/drop stats), and
`series/<key>.json` (per-author character rosters used as alias hints).

## Helper scripts

`process.py` (413 lines) is the legacy predecessor watching
`~/workspace/ebook-import/`; `overnight-watch.sh` runs unattended batch
runs. `label-merges.py` hand-labels merge pairs for `--import-labels`,
`evaluate-merges.py` scores the tiered merge against those labels,
`dedupe-prototype.py` prototypes the embedding dedup, and the `prototype-*`
scripts explore experimental paths before they land in
`process-portable.py`.

## Aborted runs still save a preview

If the chunk-failure ratio is exceeded, whatever succeeded is still
aggregated and saved as a preview marked `"aborted": true`. Partial
results are never written to Supabase.

## Notes

- Trope claims are written as `candidate` status for review in Trope Lab.
- Anthologies are detected per chunk; stories link as separate works via
  the `edition_works` junction table.
- Token cost depends on pipeline and book length — re-measure for the v2
  chapter pipeline before quoting a number.
- Legacy `--pipeline legacy` and v2 `--pipeline v2` share the same result
  shape, so previews, work resolution, and DB writes work identically.
