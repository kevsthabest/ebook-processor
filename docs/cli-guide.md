# CozyLibram Ebook Processor (`process-portable.py`) — Complete Guide & CLI Reference

This guide covers how `process-portable.py` functions end-to-end, its three-stage Wiki Ingestion architecture (`extract` → `analyze` → `merge`), its local stores, curation safety guarantees, and **every command-line flag**.

---

## 1. Architecture Overview

```
Ebook File (.epub / .mobi / .azw3)
   │
   ├── 1. --stage extract ──▶ Extraction Artefact (Call A & B per chapter)
   │
   ├── 2. --stage analyze ──▶ Analysis Artefact (Full cast, judged edges, tropes, triggers)
   │
   └── 3. --stage merge   ──▶ Merge Plan (plan.json) ──[--apply]──▶ CozyLibram Supabase
```

The processor supports two operating paradigms:

1. **Three-Stage Pipeline (`--stage extract` → `--stage analyze` → `--stage merge`) — Recommended for Wiki Ingestion**
   - Separates slow/expensive chapter-level LLM extraction (`extract`) from fast identity reconciliation and relationship/trigger judging (`analyze`), and separates both from database publishing (`merge`).
   - Guarantees **zero Supabase writes** during `extract` and `analyze` via an internal write guard (`_WRITE_GUARD`).
   - Publishes the **full cast** (`principal` + `minor` characters), reviewable **relationship edges** (`character_relationships`), and **cross-book series links** (`character_links`).
2. **All-in-One Monolith (`--stage all`, the default when `--stage` is omitted)**
   - Runs extraction, roster reconciliation, decision judging, and preview/publish in a single invocation (also saving the reusable extraction artefact automatically before cleaning up checkpoints).

---

## 2. How Each Stage Works

### Stage 1: Extraction (`--stage extract`)
- **Input:** One ebook file (`--file "import\Book.epub"`).
- **What happens:**
  - Parses the ebook into chapters (or reads pre-ingested chapters if `--use-corpus` is set).
  - Runs two LLM calls per chapter (`v2` pipeline):
    - **Call A (`v2_call_a`):** Extracts characters, aliases, roles, vitality (`alive`/`dead`/`missing`/`unknown`), physical appearance, character descriptions, chapter relationships, and POV character, updating a rolling chapter roster.
    - **Call B (`v2_call_b`):** Extracts chapter summary, spice level (`0`–`5`), content warning triggers + verbatim quotes, trope candidates, and notable quotes.
  - Verifies every extracted quote against the chapter prose (`verify_all_evidence`).
- **Output:** Writes a permanent extraction artefact to:
  `~/.ebook-processor/learned/artefacts/extract/<book_fingerprint>/extract-<timestamp>-<pid>/`
  (`manifest.json`, `meta.json`, `roster.json`, and `chapter_0.json` … `chapter_N.json`).

### Stage 2: Analysis (`--stage analyze`)
- **Input:** `--file "import\Book.epub"` + `--extraction "<extract_artefact_dir>"`.
- **What happens:**
  1. **Fingerprint Verification:** Verifies that the ebook file matches the extraction artefact's SHA-256 fingerprint and chapter count.
  2. **Within-Book Identity Merge:** Reconciles character name variants and aliases across the whole book:
     - Deterministic Tier 1 (co-occurrence cannot-link), Tier 2 (titles, nicknames, spelling variants), and Tier 3 (`post_pass_merge`), **or**
     - Jev bake-off judge (`--jev-merge`), which builds multi-chapter evidence packets and asks the decision model (`typesafe/jev-1.13`) to judge each candidate character pair.
  3. **Reduction & Pair Revival (`v2_reduce`):**
     - Selects the best character roles, vitality (with sticky-death rules), descriptions, and chapter frequencies.
     - Resolves relationship endpoints to canonical roster names, enforces the exclusivity lattice (e.g. `parent`/`child`/`sibling`/`spouse`), and mines binding prose sentences for evidence-less claims.
     - Validates family and non-family relationships via the decision model (`--decision-model`), reviving runner-up claims when a primary claim is rejected and typing revived `"other"` claims via closed-set discovery (`ask_discovery`).
     - Splits the cast into `characters` (principals) and `minor_characters` (minors) for presentation while keeping both in the artefact.
     - Maps tropes to the CozyLibram catalog via embeddings + decision judge, and validates content warning triggers.
- **Output:** Writes an analysis artefact to:
  `~/.ebook-processor/learned/artefacts/analyze/<book_fingerprint>/analyze-<timestamp>-<pid>/`
  (`manifest.json`, `analysis.json`, and `rel-types.json` if unmapped relationship types were encountered).

### Stage 3: Cross-Book Merge & Publish (`--stage merge`)
- **Input:** One or more `--analysis "<analyze_artefact_dir>"` flags.
- **What happens:**
  1. **Full Cast Assembly:** Collects both `characters` (`tier="principal"`) and `minor_characters` (`tier="minor"`) with deterministic `pipeline_key`s (`sha256(work_ref | entity_key)[:32]`).
  2. **Reviewable Edges Assembly:** Collects `relationships` and `minor_sourced_relationships` with `direction` (`mutual` vs `a_to_b`) and numeric `confidence`. Evidence-less survivors are preserved as `type="other"`, `claimed_type="<original>"`, `validation_state="needs_evidence"`.
  3. **Cross-Book Series Linking:** Groups characters across 2+ books sharing `(author_key, series_key, name_norm)` and emits `auto-series` links (`status="suggested"` by default, or `status="confirmed"` with `--auto-confirm-links`).
  4. **Plan Output:** Always writes `plan.json` under `~/.ebook-processor/learned/artefacts/merge/<run_key>/plan.json`.
  5. **Database Publish (`--apply`):**
     - Runs `_wiki_merge_schema_preflight()` to verify the migration columns exist in Supabase before touching any table.
     - Updates `works` (`spice_detected`, `series_key`, `series_position`).
     - Idempotently updates/inserts `book_characters`, `character_relationships`, `characters` + `character_links`, `book_trope_claims`, `trope_proposals`, `book_trigger_claims`, `book_quotes`, and `book_meta`.
     - **Curation Safety:** Never deletes, overwrites, or resurrects rows marked `rejected`, and never demotes rows marked `confirmed` or authored by humans (`source_type != 'ai'`).
     - Writes `receipt.json` beside `plan.json`.

---

## 3. Complete CLI Flags Reference

### 3.1 Input & Metadata Flags (`Input` / `ISBN`)

| Flag | Arguments | Default | Description |
|---|---|---|---|
| `--file` | `EBOOK` | `None` | Process a single `.epub`, `.mobi`, or `.azw3` file instead of scanning the `import/` folder. Required for `--stage extract`, `--stage analyze`, and `--replay`. |
| `--watch` | *(none)* | `False` | Continuously rescan the `import/` folder every 60 seconds. |
| `--watch-ingest` | *(none)* | `False` | With `--watch`, deterministically ingest new books in `import/` into `corpus.db` before each scan cycle. |
| `--max-books` | `N` | `None` | Process at most `N` books from the `import/` folder in one scan. |
| `--corpus-only` | *(none)* | `False` | During folder scans, only process books already present in `corpus.db`; skip others. |
| `--isbn` | `ISBN` | `""` | Override the book's ISBN for work resolution (takes priority over EPUB metadata and online lookup). |
| `--series` | `NAME` | `""` | Override the book's series name (e.g. `--series "Daemon"`). |
| `--series-position` | `N` | `None` | Override the book's position in the series (e.g. `--series-position 1` or `2.5`). |
| `--hardcover` | *(none)* | `False` | Resolve the book against the Hardcover API at scan time for canonical title, author, and series metadata (requires `hardcover_token` in `config.json` or `HARDCOVER_TOKEN` env var). |
| `--hardcover-refresh` | *(none)* | `False` | With `--hardcover`, bypass the local Hardcover cache and fetch fresh metadata. |
| `--series-priors` | *(none)* | `False` | Feed known characters from `series_registry.db` into the chapter roster as hints during `v2` extraction (tie-breaks canonical names, parks known aliases as unresolved mentions, nominates merge pairs). |
| `--allow-no-isbn` | *(none)* | `False` | Process books that have no ISBN instead of skipping them. |
| `--rename-no-isbn` | *(none)* | `False` | Rename skipped no-ISBN files to `NO-ISBN-{filename}` in `import/` for visibility. |

---

### 3.2 Models & Concurrency Flags (`Models`)

| Flag | Arguments | Default | Description |
|---|---|---|---|
| `--llm` | `ollama\|openrouter\|openai\|cohere` | *(from `config.json`)* | Override the primary extraction LLM backend. |
| `--decision-model` | *(none)* | `False` | Enable the fast classification judge (`_DECISION_VALIDATOR`) for relationship validation, pair discovery, trigger gating, trope boundary validation, and quote polling. |
| `--decision-model-provider` | `local\|openrouter` | `local` | Backend for the decision model: `local` (Unsloth `/v1/systemone` server) or `openrouter` (hosted `typesafe/jev-1.13`). |
| `--decision-model-url` | `URL` | `""` | Base URL for the local decision model server (e.g. `http://127.0.0.1:8888/v1`). |
| `--batch` | `N` | *(from `config.json`)* | Number of parallel LLM requests per book (used for Call B chapters). |

---

### 3.3 Pipeline & Stage Control Flags (`Pipeline`)

| Flag | Arguments | Default | Description |
|---|---|---|---|
| `--pipeline` | `legacy\|v2` | `v2` | Choose `v2` (chapter-level map/reduce with rolling roster) or `legacy` (fixed-size text chunks). |
| `--stage` | `all\|extract\|analyze\|merge\|decide` | `all` | Run a single stage: `extract` (Stage E), `analyze` (Stage D over `--extraction`), `merge` (cross-book plan/publish over `--analysis`), or `all` (full run). (`decide` is deprecated and prints a redirect message to `analyze`.) |
| `--extraction` | `DIR` | `None` | Path to the extraction artefact directory consumed by `--stage analyze`. |
| `--chunks` | `SPEC` | `None` | Process only specific chapter/chunk indices (e.g. `--chunks 1-5` or `--chunks 2,5,8`) for fast testing. |
| `--jobs` | `N` | `None` | Process `N` books in parallel during folder scans (refused when `--decision-model` is active to protect the per-book decision budget). |
| `--sample` | `N` | `None` | Process every `N`th chunk (legacy/sampling mode). |
| `--full` | *(none)* | `False` | Disable sampling and process every chunk/chapter. |
| `--dedupe` | *(none)* | `False` | Run embedding-based character deduplication after extraction (requires `embed_url`/`embed_model` in `config.json`). |

---

### 3.4 Merging, Identity & Discovery Flags (`Merging`)

| Flag | Arguments | Default | Description |
|---|---|---|---|
| `--analysis` | `DIR` *(repeatable)* | `None` | Path to an analysis artefact directory for `--stage merge`. Pass multiple times to merge a multi-book series. |
| `--apply` | *(none)* | `False` | With `--stage merge`: execute the merge plan against Supabase and write `receipt.json`. Without `--apply`, `--stage merge` only writes `plan.json`. |
| `--source-run` | `KEY` | `None` | With `--stage merge`: explicit `source_run` identifier stamped onto all written Supabase rows (defaults to the generated `merge-<timestamp>-<pid>` key). |
| `--auto-confirm-links` | *(none)* | `None` | Allow exact-name same-author same-series character links in `--stage merge` to land with `status='confirmed'`. |
| `--no-auto-confirm-links` | *(none)* | `None` | Force all proposed cross-book character links to land with `status='suggested'` (the default). |
| `--alias` | `"A=B"` *(repeatable)* | `[]` | Record a human-verified character alias (e.g. `--alias "Loki=Brian Gragg"`), persisted across runs. |
| `--principals-only` | *(none)* | `True` | Split characters into principals (`characters`) and minors (`minor_characters`) using `--min-frequency`, POV status, and confirmed connections. (Note: `--stage merge` still publishes the full cast with `tier='principal'` / `tier='minor'`.) |
| `--no-principals-only` | *(none)* | `False` | Disable the principal/minor split so every rostered character is placed in `characters`. |
| `--min-frequency` | `F` | `0.20` | Minimum chapter appearance frequency (`0.0`–`1.0`) for automatic principal status. |
| `--jev-merge` | *(none)* | `False` | Use the Jev decision model to judge candidate character merges from multi-chapter evidence packets instead of heuristic post-pass merging. |
| `--roster-reconcile` | *(none)* | `False` | Run deterministic whole-roster reconciliation after Call A (merges mutual alias claims and learned links, prunes shadowed aliases). |
| `--persist-roster` | *(none)* | `False` | Persist per-work character entity IDs in `state.db` across runs so canonical names and curation anchors remain stable across re-extractions. |
| `--staging-gate` | *(none)* | `False` | Stage borderline tropes and evidence-less triggers locally in `state.db`, promoting them only when corroborated across runs or chapters. |
| `--evidence-retrieval` | *(none)* | `False` | Retrieve literal supporting passages from a trope claim's chapters and attach them as quote evidence with a retrieval grade stamp. |
| `--corpus-quotes` | *(none)* | `False` | Verify quotes against `corpus.db` and run decision-model quote polling for ingested works. |
| `--use-corpus` | *(none)* | `False` | Source chapter text and chapter-detection metadata from `corpus.db` when the book is already ingested. |
| `--discovery-shadow` | *(none)* | `False` | Run co-occurrence sentence mining + closed-set relationship discovery for un-emitted character pairs and record results in `discovery` (shadow mode). |
| `--discovery-embedding-rank` | *(none)* | `False` | With `--discovery-shadow`, rank candidate binding sentences by embedding similarity before judging. |
| `--emit-uncertainty` | *(none)* | `False` | Emit uncertain merge pairs and borderline judgments to the local uncertainty queue in `state.db` for human labelling. |
| `--emit-run-events` | *(none)* | `False` | Write a per-run JSONL telemetry stream to `<learn-dir>/run-events/` for the operator portal. |
| `--run-history` | *(none)* | `False` | Mirror metadata-only run summaries and claim outcome counts to the `processor` schema in Supabase. |
| `--run-history-backfill` | `JSONL [...]` | `None` | Backfill Supabase run history from one or more local `run-events/*.jsonl` files (or directory) and exit. |

---

### 3.5 Output, Preview & Maintenance Flags (`Output`)

| Flag | Arguments | Default | Description |
|---|---|---|---|
| `--preview` | *(none)* | `False` | Save output to `preview/<Book>.json`; never write to Supabase or move files out of `import/`. |
| `--dry-run` | *(none)* | `False` | Run extraction/analysis in memory without writing to Supabase or moving files. |
| `--show-matrices` | *(none)* | `False` | Print ASCII matrices for chapter spice, character importance, and relationship confidence in the console. |
| `--preview-summary` | *(none)* | `False` | Generate a human-readable markdown digest at `preview/<Book>_summary.md`. |
| `--preview-diff` | `OLD_JSON NEW_JSON` | `None` | Compare two preview/analysis JSON files, print character/relationship/trope/trigger diffs, and exit. |
| `--recalibrate-spice` | `PREVIEW_JSON` | `None` | Recompute a preview file's book-level spice score from its chapter spice array and exit. |
| `--no-viz` | *(none)* | `False` | Disable the live chapter visualizer panel in the Rich terminal UI. |
| `--tui` | *(none)* | `False` | Launch the interactive Textual inspection console for the run. |
| `--push-preview` | `PREVIEW_JSON` | `None` | Push a saved preview JSON file (or directory of previews) to Supabase using legacy `write_claims` and exit. |
| `--verify` | `PREVIEW_JSON` | `None` | Compare a preview JSON against current Supabase rows (read-only) and exit. |
| `--resume` | *(none)* | `False` | Resume an interrupted `v2` extraction from `preview/.checkpoints/`, skipping completed chapters. |
| `--retry-failed` | `PREVIEW_JSON` | `None` | Re-run only chapters that failed Call A or Call B in a previous preview, saving `<Book>_retried.json`. |
| `--validate` | `PREVIEW_JSON` | `None` | Run automated quality checks (bad merges, plot-summary tropes, unverified triggers) on a preview JSON or directory and exit. |
| `--trope-map` | `PREVIEW_JSON` | `None` | Map a preview file's tropes to the CozyLibram catalog via embeddings and write `<Book>-trope-map.json`. |
| `--clear-checkpoints` | *(none)* | `False` | Delete all per-book checkpoint directories under `preview/.checkpoints/` and exit. |
| `--apply-rel-types` | `REVIEW_JSON` | `None` | Add operator-approved relationship types from `<Book>--rel-types.json` into the Supabase `relationship_types` registry and exit. |
| `--json` | *(none)* | `False` | Emit machine-readable JSON for reporting commands (`--staging-report`, `--corpus-status`, `--calibration-report`). |

---

### 3.6 Learning, Corpus & Forensics Flags (`Learning`)

| Flag | Arguments | Default | Description |
|---|---|---|---|
| `--learn` / `--no-learn` | *(none)* | `--learn` | Enable or disable cross-run learning of nicknames, title prefixes, and series rosters. |
| `--learn-dir` | `DIR` | `~/.ebook-processor/learned` | Override the directory where `state.db`, `corpus.db`, `series_registry.db`, and `artefacts/` are stored. |
| `--learn-status` | *(none)* | `False` | Print a summary of learned nicknames, titles, series rosters, and denylisted tropes, then exit. |
| `--import-labels` | `PATH` | `""` | Import hand-labeled character merge pairs from `label-merges.py` into the learned dictionary. |
| `--import-rejections` | `[PATH]` | `None` | Import rejected tropes from a JSON file (or from Supabase when combined with `--from-supabase`). |
| `--from-supabase` | *(none)* | `False` | With `--import-rejections`, pull rejected trope claims directly from `book_trope_claims` in Supabase. |
| `--apply-uncertainty-labels` | `FILE` | `None` | Apply human labels (`[{queue_id, label, labeller}]`) to the uncertainty queue in `state.db` and exit. |
| `--capture-snapshot` | `ID` | `None` | During a `--preview` run, save extracted chapter payloads as a named replay snapshot `ID`. |
| `--snapshot-reason` | `TEXT` | `""` | Attach a note to a `--capture-snapshot` manifest. |
| `--replay` | `ID` | `None` | Re-run downstream reduction/judging from snapshot `ID` in an isolated sandbox (requires `--file`). |
| `--staging-report` | *(none)* | `False` | Print staged, promoted, and expired claims from the `--staging-gate` store and exit. |
| `--roster-report` | *(none)* | `False` | Print per-work entity lineage and suspect observations from the `--persist-roster` store and exit. |
| `--roster-link` | `WORK ENTITY FORM` | `None` | Assert that `FORM` belongs to `ENTITY` in `WORK`'s roster store and exit. |
| `--roster-unlink` | `WORK ENTITY FORM` | `None` | Detach `FORM` from `ENTITY` in `WORK`'s roster store (recording cannot-link NOs) and exit. |
| `--roster-split` | `WORK ENTITY FORM [...]` | `None` | Split one or more `FORM`s off `ENTITY` into a new character entity in `WORK`'s roster store and exit. |
| `--roster-no` | `WORK FORM_A FORM_B` | `None` | Record a permanent human cannot-link `NO` between `FORM_A` and `FORM_B` for `WORK` and exit. |
| `--ingest` | `PATH` | `None` | Deterministically parse and ingest an ebook file or directory into `corpus.db` (no LLM calls) and exit. |
| `--ingest-reason` | `TEXT` | `""` | Record a reason string on an `--ingest` operation. |
| `--ingest-force` | *(none)* | `False` | Force `--ingest` to re-parse even if the book is already in `corpus.db` at the current parser version. |
| `--corpus-status` | *(none)* | `False` | Print `corpus.db` statistics (ingested works, chapter counts, vector coverage, DB size) and exit. |
| `--corpus-snapshot` | *(none)* | `False` | Upload a metadata-only corpus coverage snapshot to the `processor` schema in Supabase and exit. |
| `--corpus-remove` | `FINGERPRINT` | `None` | Delete a work and all its chapters/vectors from `corpus.db` by fingerprint and exit. |
| `--corpus-reembed` | *(none)* | `False` | Compute current-generation passage embeddings for ingested works missing vectors and exit (restrict to one work with `--work FINGERPRINT`). |
| `--corpus-verify-quote` | `FINGERPRINT` | `None` | Verify a single quote (`--quote "TEXT"`) against an ingested work in `corpus.db` and exit. |
| `--explain` | `WORK` | `None` | Print a forensics report for `WORK` joining `state.db` and `corpus.db` (narrow to one claim with `--explain-claim` or one character with `--explain-character`). |
| `--backup-state` | `DIR` | `None` | Atomically back up `state.db` and `corpus.db` with SHA-256 verification manifests into `DIR` and exit. |
| `--restore-state` | `DIR` | `None` | Verify and restore `state.db` and `corpus.db` from a `--backup-state` directory and exit. |
| `--calibration-report` | *(none)* | `False` | Print score distributions for kept vs. dropped trigger judgments and exit. |

---

### 3.7 Debug & Profiles Flags (`Debug` / `Profiles & modes`)

| Flag | Arguments | Default | Description |
|---|---|---|---|
| `--debug` | *(none)* | `False` | Save raw LLM responses for failed chunks to `preview/debug/` and enable verbose diagnostics. |
| `--explain-blocks` / `--no-explain-blocks` | *(none)* | *(on with `--debug`)* | Print detailed explanations whenever merge guards block a candidate character merge. |
| `--prompt-cache` | *(none)* | `False` | Place chapter text before task instructions in Call A/B prompts for KV-cache reuse on local `llama.cpp` servers. |
| `--shared-system` | *(none)* | `False` | Isolation test flag: use a minimal shared system prompt for Call A/B. |
| `--task-last` | *(none)* | `False` | Isolation test flag: place chapter text before task instructions. |
| `--profile` | `NAME` | `None` | Load flag presets from `~/.ebook-processor/profiles/<NAME>.json` (explicit CLI flags override profile values). |
| `--save-profile` | `NAME` | `None` | Save the current command line's non-default flags to `~/.ebook-processor/profiles/<NAME>.json` and exit. |
| `--list-profiles` | *(none)* | `False` | List built-in and saved profiles and exit. |
| `--guided` | *(none)* | `False` | Interactive wizard that prompts for the book file, mode, and options. |
| `--auto-update` | *(none)* | `False` | Check `origin/master` on startup (and each `--watch` cycle) and `git pull` + restart if behind. |

---

## 4. Common Recipes

### Recipe A: Process a Single Book into CozyLibram (3-Stage Workflow)
```powershell
# 1. Extract chapters once (slow, LLM-intensive)
py process-portable.py --stage extract --file "import\Daemon - Daniel Suarez.epub"

# 2. Analyze roster, relationships, tropes, and triggers (fast, repeatable)
py process-portable.py --stage analyze `
  --file "import\Daemon - Daniel Suarez.epub" `
  --extraction "C:\Users\kevst\.ebook-processor\learned\artefacts\extract\<fp>\<extract_key>" `
  --jev-merge --decision-model

# 3. Preview merge plan, then apply to Supabase
py process-portable.py --stage merge `
  --analysis "C:\Users\kevst\.ebook-processor\learned\artefacts\analyze\<fp>\<analyze_key>"
py process-portable.py --stage merge `
  --analysis "C:\Users\kevst\.ebook-processor\learned\artefacts\analyze\<fp>\<analyze_key>" `
  --apply
```

### Recipe B: Link Characters Across a Multi-Book Series
```powershell
py process-portable.py --stage merge `
  --analysis "C:\Users\kevst\.ebook-processor\learned\artefacts\analyze\<fp_book1>\<analyze_key_1>" `
  --analysis "C:\Users\kevst\.ebook-processor\learned\artefacts\analyze\<fp_book2>\<analyze_key_2>" `
  --auto-confirm-links --apply
```

### Recipe C: Score an Analysis Against a Gold Evaluation Pack
```powershell
py score-gold.py --gold eval\daemon-labels.json `
  --run "C:\Users\kevst\.ebook-processor\learned\artefacts\analyze\<fp>\<analyze_key>\analysis.json"
```
