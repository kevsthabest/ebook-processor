# The corpus store — retained book text, passages, vectors, stage outputs: design

Status: DESIGN ONLY (no code). User-approved direction
2026-10-16 ("retain the books so data is first-class and models
can process them several times"; stated motivation: stronger
analytics in the end). This design supersedes the earlier
data-package spec hold: the corpus store is the package's
foundation, and the explain/forensics command, the one-command
backup, and the calibration report are folded in as §7
deliverables riding on it. Rollout follows the house pattern:
named flags, default OFF, flag-off = no code path, no behaviour
change — except ingest itself, which is a named command that
writes only the new store.

Doctrine, stated once and binding on everything below: **the
corpus is retained source and recorded stage output; it decides
nothing about any claim.** Retention makes analysis re-runnable
and measurable; it mints no claims, promotes nothing, and
suppresses nothing. Every consumer of the corpus inherits the
doctrine of the feature it serves (F's staging rules, G's
evidence rules, the spine's measurement rules) — the corpus is
plumbing under all of them, authority over none. One scoping
rule, explicit: **decision D1 (learned-state governance) is not
reopened here.** Stage outputs (§4) record what stages produced;
whether a human correction may retract a stage output is D1's
question, and §4's shape leaves the seam without pre-deciding it.

The program in one paragraph: acquisition and analysis are
separated. An **importer** mode ingests a book once —
deterministically, through the same parse path a run uses —
storing chapter text, passage spans, a full-text index, and
passage vectors in `corpus.db` beside `state.db`. The
**processor** then consumes corpus slices: chapters without
re-parsing, retrieval without re-embedding, iteration on
downstream stages without re-scanning. Stage outputs are
stamped `(input_hash, stage, model, stage_version)` and a
re-run of a stage replaces its own outputs, so "process the
same book several times" is a safe, ordinary operation instead
of a fresh draw every time.

### Red-team amendments (Security Advisor, 2026-10-16 — PROCEED-WITH-CHANGES)

Folded into the sections named; where an amendment and the
original draft disagreed, the amendment won and the
contradiction was removed, not annotated.

- **A1 — Drift canary** for vector generations: §2.4,
  §1.2 (`meta`), §7.4.
- **A2 — Embed from the stored span only; re-slice check
  at 100%:** §3.3, §3.2.
- **A3 — `verify_quote` scoped by kind + asserted
  chapter:** §10.1, §10.7.
- **A4 — Stage replacement scope = the full
  `(fingerprint, stage)` key space:** §4.1, §4.5.
- **A5 — Completeness is generation-relative:** §3.3,
  §6.4, §7.4.
- **A6 — `stage_version` named in the retrieve input
  hash:** §4.2.
- **A7 — FTS maintained by triggers; `integrity-check`
  gates ingest:** §1.3, §3.2, §3.3.
- **A8 — Cross-generation honesty in reports:** §6.1.
- **A9 — Ingest bookkeeping commits before phase 1;
  `busy_timeout` + contention directions ruled:** §3.3,
  §5.2.
- **A10 — `--corpus-remove` is one full-scope
  transaction:** §3.4.
- **A11 — F8 preservation for corpus.db is
  rename-in-place, never a copy:** §5.3.

Closed as written, no text changed: the backup posture
(§7.2), the Q3 token-equality core (§10.1), the
input-hash-over-stored-hashes rule (§4.1), the §3.2 proof
shape, and the §2.3 adoption-trigger shape.

### Verification amendments (team verification round, 2026-10-16 — seven seats, all AGREE-WITH-CHANGES, zero dissents)

Folded the same way: in place, in the sections named.

- **CozyLibram Builder (4, data-shape):** source edition
  identity (fingerprint + ISBN-13) stated on the quote
  payload: §10.6. The envelope taxonomy is a registry,
  with value shapes fixed before the app builds
  consumers: §6.4. `kind='opening'` verified quotes ship
  as their own payload kind: §10.6. Series-scope facts
  get a named home — **resolved by user ruling**: the
  bibliographic skeleton is pulled from a Cozy-persisted
  Hardcover table via the verdict read-back pattern, and
  processor series judgments are computed against it:
  §6.4.
- **Codebase Reviewer (6, build-level):** shared
  substrate base, parameterized, with the divergences
  enumerated: §1.1 (and §0). The snapshot corpus block
  records the capture run's own chapter hashes, never
  corpus read-backs: §4.3. Replay corpus access is
  read-only, pinned: §4.3, §4.4, §4.5. Envelope refresh
  discipline: corpus-sourced sections refresh in the
  writing transaction; state.db-sourced sections are
  composed live, never materialized: §6.4. The
  `ingesting` commit ordering converged with A9 and was
  verified already folded: §3.3. The FTS mechanism: the
  A7 triggers stand; the rowid-instability pin added:
  §1.3, §3.5. Build notes folded: write guards in handle
  construction from slice 1 (§5.2, §11); per-adopter
  hashed-input enumeration (§4.2); slice 5 reproduces
  the acquisition block's full output surface (§3.1,
  §11, §13); the numpy parity test forces the pure path
  (§2.5); §1.2's duplicated primary key written once;
  slice 4 gains the scoped dump-hash helper (§11).
- **Product Strategist (2):** library coverage rollup
  added as §6.1 query 7. The first post-gate field wave
  is named — the book-facts stage (ending guarantee +
  trigger depth), sequenced after the gate, which it
  does not touch: §11.
- **UI Designer (2):** `spoiler_tier` display contract —
  the screens' pass is the display fact; the tier is a
  field-class policy marker, never a per-line gate:
  §10.6. The raw-confidence payload boundary — payloads
  carry state + evidence tier + provenance; confidence
  stays processor-side: §6.4.
- **UX Tester (6):** sentence-boundary rule for
  signature spans: §10.1, §10.7, §13. The rendering
  contract (unknown = silence; assessed-none = no
  positive claim below full coverage; a `coverage` field
  on safety-relevant sections): §6.4. Provenance
  vocabulary fixed at design time ("From the text" /
  "From readers" / "From the publisher"; "Checked
  against the text"): §6.4. Chapter label from the
  chapter's own heading, with the source-edition caveat:
  §10.6, §10.7, §13. Retraction as a consumer
  requirement (drop from rendered surfaces and local
  caches on next sync): §10.6. Poll-shed books render
  nothing: §10.5, §6.4.
- **Data & Sync Engineer (5):** retraction as a named
  operation — explicit record, supersession, envelope
  trace, one transaction; authorization stays D1's:
  §6.4. Currency enforced at the payload boundary, with
  its pin: §6.4, §6.6. The envelope retraction trace
  (`retracted_at` + `reason`): §6.4. Envelope refresh on
  ingest events, in the generation swap's transaction:
  §6.4, §3.4. The multi-edition payload rule
  (edition-scoped sections ship per edition, keyed by
  edition identity): §6.4.
- **Trope Review seat (4):** VERIFY match multiplicity
  + occurrence preference (earliest occurrence; support
  set diagnostic): §10.1, §10.3, §10.7. The SELECT audit
  surface — selected quotes listed in `--explain`, counts
  in `--corpus-status`: §7.1, §7.4. The calibration
  decision rule printed beside the limit (threshold
  moves require outcome-joined calibration): §6.3. The
  churn query names emission volume beside Jaccard:
  §6.1.

---

## 0. What the code actually looks like today (evidence base)

All citations are process-portable.py on master @ 6e1476a
(v2.11.1) unless noted.

- **Every run re-parses the book and discards the text.**
  `process_file_v2` acquires chapters by calling
  `extract_epub_units` (:715) + `split_chapters` (:920)
  directly (:15121); the legacy path and `_presented_fingerprint`
  (:14993) do the same. Chapter dicts are
  `{"index", "label", "text"}`; nothing persists them. The only
  retained chapter-shaped artefacts are checkpoint payloads —
  which the spine design (§3.9) ruled **do not move** into the
  substrate: ephemeral, code-SHA-bound, lifetime of one session.
- **G re-embeds the book's windows on every run, by rule.**
  Amendment G5 made the window-embedding cache per-run and
  in-memory only. The machinery it caches is real and reusable:
  `_sentence_spans` (:5699) produces verbatim sentence offsets;
  `_evidence_windows` (:5713) builds sentence-anchored windows
  of `_EVIDENCE_WINDOW_CHARS` = 600 chars (:5630) with a
  one-sentence overlap rule; `retrieve_claim_candidates`
  (:5795) ranks them. The cache key, however, is **chapter
  label + window offset** (per the :5795 docstring) — a key
  that is only safe *because* the cache dies with the run.
  Across runs it would serve a stale vector for edited text or
  a foreign vector under a changed model without noticing.
  G5's fear is the key, not the persistence; §4.4 re-rules it.
- **The embedding path and its hygiene precedent exist.**
  `embed_vectors` (:14151) is a plain OpenAI-shaped POST to
  `CONFIG["embed_url"]` with `CONFIG["embed_model"]` (both
  config-only, no default model). `trope_catalog_vectors`
  (:11879) already runs the discipline a persistent vector
  store needs, for the catalog cache: namespaced by
  (model, `EMBED_TEXT_FORMAT = "name-desc-v1"`, :11866),
  per-entry content hashes, wholesale invalidation on
  model/format change, pruning, atomic write. §2 generalizes
  that discipline from a file cache to a table.
- **The substrate pattern is built and proven.** `StateDB`
  wraps one connection per learn dir (WAL,
  `synchronous=FULL`, :2757), schema in `_SUBSTRATE_DDL`
  (:2638) registered in `_SUBSTRATE_TABLES` (:2723),
  `PRAGMA user_version` as schema version, quick-check on
  open, the F8 corruption discipline (loud, `.corrupt`
  preservation incl. sidecars, empty start, no auto-restore),
  rolling backup via SQLite's backup API (`backup_to`,
  :2909; `_maybe_backup`, :2872), a canonical logical
  `dump_hash`, and the A1 migration idiom (build into
  `.migrating`, verify, rename). §1 shares this machinery
  through a common base rather than inventing a parallel
  one (§1.1).
- **Book identity is the fingerprint, everywhere.**
  `_checkpoint_fingerprint` (:14553) — ISBN/ASIN +
  normalized title/author + hash of the first substantial
  chapter's sampled text — keys checkpoints, the snapshot
  store (spine A5: snapshots live under
  `<learn_dir>/snapshots/<fingerprint>/<snapshot_id>/`,
  captured by `capture_snapshot`, :14918), and resume
  refusal. Corpus identity uses the same fingerprint; no
  second identity system is introduced.
- **Snapshots freeze draws; nothing freezes source.**
  A snapshot is per-chapter Call A/B payloads + manifest
  (extraction provenance, payload hash). Replay
  (`run_replay`, :15014) seeds a throwaway substrate via the
  backup API from an immutable open (:15055) and runs the
  current pipeline over the frozen draw. But retrieval
  inside a replay still re-embeds windows from chapter
  text re-derived out of the payloads — the *source* layer
  has no frozen form. §4.3 composes the two.
- **Threshold samples are collected and never analyzed.**
  `record_threshold` (:3638) stores decision-model P(yes)
  samples per trigger (kept/dropped, capped 500/bucket) in
  `learned_threshold_stats`. The only aggregate read in the
  codebase *counts* them (`_learn_status_text`, :8662, for
  `cmd_learn_status`, :8673). The samples carry **no work,
  run, or outcome keys** — a fact §6.3 designs around
  honestly instead of pretending the join exists.
- **Quote verification exists — against in-memory text, per
  chunk.** `verify_evidence(quote, source_text)` (:11613)
  checks a quote on normalized strings (`_norm_quote`) with
  a light fuzzy fallback (`_verify_against_norm`, :11579);
  `verify_all_evidence` (:11619) runs it during extraction
  so fabricated quotes die before aggregation. It answers
  "is this quote in *this chunk*" for a run that happens to
  hold the text. It cannot answer "is this quote in *the
  book*" after the run ends, cannot snap a paraphrase to
  its true span, and has no memory: the same fabricated
  quote is re-fought every run. §10 builds the corpus-side
  stage on this machinery.
- **Verified environment facts (this VM, 2026-10-16):**
  Python's `sqlite3` is SQLite 3.45.1; **FTS5 is
  available**; `enable_load_extension` exists and is
  callable. The user's Windows Python is *not* assumed to
  match: §1.5 makes FTS5 and extension support **probed at
  open**, never presumed — the standard Python.org builds
  ship FTS5, but the design's failure posture does not
  depend on that being true on any given machine.

---

## 1. Schema

### 1.1 The handle and the file

`corpus.db` lives in the learn dir beside `state.db`
(OC-1), opened through a `CorpusDB` handle that **shares
`StateDB`'s substrate machinery rather than copying it**:
the generic handle code (connection management, schema
creation, corruption preservation, backup, checkpoint,
`dump_hash`, write transactions — roughly 200 of
StateDB's ~250 lines) is factored into a shared base
parameterized by (filename, DDL list, table list, schema
version, corruption-consequence), and each handle
supplies only its policy. One connection per process per learn dir
(a registry beside `_STATE_DBS`), `check_same_thread=False`,
writes serialized by an RLock and executed as single
transactions (`write_txn`), WAL journal, `synchronous=FULL`,
quick-check on open, F8 discipline on corruption
(§5.3 states what corpus corruption *means* — it is not the
same as state corruption, and the difference is designed,
not discovered). Schema version is `PRAGMA user_version`
(`CORPUS_SCHEMA_VERSION`, starting at 1) plus a `meta` table
carrying `corpus_v` as a string for the envelope (§6.4) —
the D&S seat's rule: a corpus schema version on the record
from day one, additive-only within a version.

The sharing is not identity. The enumerated divergences,
complete in v1: (a) **no rolling backup on corpus
writes** — StateDB's per-write backup hook stays
state-only (§7.2's ordering: the substrate is precious,
the corpus is rebuildable); (b) **corruption consequence
differs** — corpus corruption falls back to file parsing
(§5.3), where state corruption starts empty under the
F8 warning, because corpus content is re-derivable and
state content is not; (c) **creation posture differs** —
corpus.db is create-on-ingest, where StateDB's running
stores are open-only (creation there is migration/tests).
Any future divergence joins this list in the build, by
name; an unlisted behavioural difference between the
handles is a bug, not a style. The shared base is also
the codebase's natural future extraction seam — the two
handles + registries as a "stores" unit — should the
single file ever split (§5.1).

`PRAGMA user_version` mismatch on open follows the substrate
precedent: additive DDL is re-applied (`CREATE TABLE IF NOT
EXISTS` throughout), the version is re-stamped, and any
future non-additive change is a named migration in the A1
idiom (build into `corpus.db.migrating`, verify, rename).
There is no JSON-era migration to design: the corpus has no
predecessor store. Its "migration" is ingest itself (§3).

### 1.2 Tables

```sql
meta(k TEXT PRIMARY KEY, v TEXT)      -- corpus_v, created-by,
                                      -- generation stamps, canary
                                      -- sentinels (§2.4), probes (§1.5)

corpus_works(
  fingerprint TEXT PRIMARY KEY,       -- _checkpoint_fingerprint
  book_key TEXT,                      -- staging_work_key currency
  work_id TEXT,                       -- Supabase work id, when known
  title TEXT, author TEXT,
  identifiers TEXT,                   -- JSON {isbn, asin, …}
  series TEXT, series_index REAL,     -- as resolved at ingest
  parser_version TEXT,                -- §1.4
  chapter_count INTEGER,
  text_root_hash TEXT,                -- §3.2 root over chapter hashes
  source_hash TEXT,                   -- hash of the presented file bytes
  status TEXT,                        -- §3.3 state machine
  ingested_at TEXT, updated_at TEXT,
  payload TEXT)                       -- JSON: residual ingest record

corpus_chapters(
  fingerprint TEXT, chapter_index INTEGER,
  label TEXT,
  text TEXT,                          -- PLAIN text (§1.3)
  text_hash TEXT,                     -- sha256 of text (hex)
  char_count INTEGER, word_count INTEGER,
  is_frontmatter INTEGER,
  PRIMARY KEY (fingerprint, chapter_index))

corpus_passages(
  passage_id TEXT PRIMARY KEY,        -- §1.4 content-derived id
  fingerprint TEXT, chapter_index INTEGER,
  start_offset INTEGER, end_offset INTEGER,
  text_hash TEXT)                     -- sha256 of the sliced span;
                                      -- (fingerprint, chapter_index)
                                      -- indexed for range reads

passage_vectors(
  passage_id TEXT PRIMARY KEY,
  fingerprint TEXT,                   -- denormalized for per-book scans
  model TEXT, format TEXT,            -- embed stamp (§1.4)
  dim INTEGER,
  vector BLOB)                        -- float32 little-endian (§2)

corpus_fts …                          -- FTS5, §1.3

ingest_runs(
  id INTEGER PRIMARY KEY, fingerprint TEXT, started TEXT,
  finished TEXT, parser_version TEXT, embed_model TEXT,
  outcome TEXT,                       -- complete|partial|failed|noop
  detail TEXT)                        -- JSON: counts, timings, errors

stage_runs(                           -- §4
  run_id TEXT PRIMARY KEY, fingerprint TEXT,
  stage TEXT, stage_version TEXT, model TEXT,
  input_hash TEXT, started TEXT, finished TEXT,
  status TEXT,                        -- ok|failed|skipped
  payload TEXT)                       -- JSON: stage accounting

stage_outputs(                        -- §4
  fingerprint TEXT, stage TEXT, output_key TEXT,
  input_hash TEXT, run_id TEXT,
  payload TEXT,                       -- JSON stage output record
  PRIMARY KEY (fingerprint, stage, output_key))

quotes(                               -- §10
  quote_id TEXT PRIMARY KEY,
  fingerprint TEXT, chapter_index INTEGER,
  start_offset INTEGER, end_offset INTEGER,
  text TEXT,                          -- the corpus span, verbatim
  kind TEXT,                          -- evidence|signature|opening|…
  status TEXT,                        -- candidate|verified|selected|rejected
  payload TEXT)                       -- verify + poll provenance (§10.3)

work_envelope(                        -- §6.4, materialized
  fingerprint TEXT PRIMARY KEY, corpus_v TEXT,
  envelope TEXT,                      -- JSON per-section records
  computed_at TEXT)
```

Two shape decisions, stated now because everything else
leans on them:

- **Passages store spans, not text.** A passage is
  (chapter, start, end) plus the hash of the slice. Its text
  is `chapter.text[start:end]` — exact by construction,
  because the window builder's spans are literal slices
  (`_evidence_windows`, :5713). Storing the text again would
  double the text footprint to save a slice operation that
  costs microseconds, and would create a second copy of the
  book's words to keep honest. The slice identity is a
  pinned test (§1.6; gate §13): every stored span, re-sliced,
  hashes to
  its stored `text_hash`.
- **Stage outputs and quotes are payload-shaped on
  purpose.** Following spine OC-2's seam discipline: keys,
  stamps, and counters are columns (the things queries
  filter on); stage-specific records ride as JSON payload.
  A field a named query needs gets promoted to a column by
  a schema version, not by redesign.

### 1.3 Text storage: plain text + in-file FTS5 (the trade-off, resolved)

FTS5 needs plain text; compression and FTS5 indexing of the
same words are mutually exclusive in one copy. The three
honest options:

| Option | Per-book cost (FW-class, ~1.01M chars) | Consequences |
|---|---|---|
| **A. Plain text + FTS5 external-content index in corpus.db** | text ~1.01 MB + FTS index ~0.5–1.0 MB | One file, self-contained, copyable whole (§5.2). FTS source *is* the canonical text — no second copy to diverge. |
| B. zlib BLOB canonical + FTS5 content table | ~0.4 MB compressed + ~1.0 MB plain text inside FTS + its index | Pays *more* total bytes than A (the FTS copy is uncompressed) and adds a decompress step to every non-FTS read. Compression only wins if FTS does not exist. |
| C. zlib BLOB canonical + FTS in a sidecar `corpus-index.db` | corpus.db ~0.4 MB; sidecar ~1.5–2.0 MB, rebuildable | corpus.db stays lean, but the file that must travel (§5.2) either grows a sibling or arrives without its index and rebuilds on first use (seconds per book, minutes per library). Two-file discipline for a saving of ~0.6 MB/book. |

**Recommendation: A** (OC-2). At the library scales §8 sizes
(hundreds of books), A's total is single-digit gigabytes
dominated by vectors, not text; the 0.6 MB/book C saves does
not repay a second file in every copy, backup, sandbox, and
restore path. **C is the documented pressure valve**: if a
future `--corpus-status` shows text+FTS (not vectors)
driving corpus.db past a size the user minds, the FTS index
— and only the index — moves to a rebuildable sidecar,
because an index is the one component that is *derived*
and can be dropped and rebuilt from canonical text without
loss. Canonical text never moves into a derived container;
that direction is not reversible and is not offered.

FTS5 form: an external-content table over `corpus_chapters`
(`content=corpus_chapters, content_rowid=rowid` keyed
reads join back to (fingerprint, chapter_index)), unicode61
tokenizer, prefix indexes off in v1. FTS rows are
chapter-granular in v1 — passage-level FTS is named as
not-built (§8.3): every v1 FTS consumer (explain search,
alias evidence, duplicate detection) is served by
chapter hits + span slicing.

**Maintenance mechanism: triggers (A7).** The index is
maintained by SQLite triggers on `corpus_chapters` —
insert, update, and delete fire the matching FTS row
operations inside the same transaction as the text
write. There is no application-level "remember to sync
the index" step to forget, so a generation replacement
(§3.4: old chapters deleted, new ones inserted, one
transaction) cannot desync text from index: they commit
or roll back together. The mechanism is load-bearing
precisely because `corpus_chapters` rowids are **not
stable** across that replacement (rows are deleted and
rewritten) and an external-content FTS5 index does not
track base-table changes by itself — triggers are what
keep the index honest through the rewrite. §3.2's
self-check closes the loop with FTS5's own
`integrity-check`, and §3.5 pins the behaviour: after a
parser-bump re-ingest, FTS finds the new generation's
text and no longer finds removed text.

### 1.4 Identity, versions, and stamps

- **Work identity** is the batch-1 fingerprint
  (`_checkpoint_fingerprint`, :14553), computed at ingest
  from the same parse the pipeline would run (§3.1). A
  different file (different edition) is a different
  fingerprint and a different corpus work — the batch-1
  doctrine, unchanged: a refused match costs a re-ingest,
  a consumed foreign text costs a corrupted book.
- **Chapter identity** is (fingerprint, chapter_index)
  *within a parser generation*, anchored by `text_hash`:
  indices are positional and a future parser may segment
  differently, so anything durable (snapshots, §4.3)
  validates by hash, never by index alone.
- **`parser_version`** is a named constant
  (`CORPUS_PARSER_VERSION`) bumped whenever
  `extract_epub_units` / `split_chapters` / `_prose_chapters`
  or the window builder changes output for the same input.
  It is stamped per work at ingest; a work whose stamp ≠
  current is `stale` (§3.4) — visible, listed, never
  silently mixed.
- **Passage identity** is content-derived:
  `passage_id = sha256(fingerprint ‖ chapter_index ‖
  start ‖ end ‖ window_format)` truncated to 32 hex chars,
  where `window_format` versions `_evidence_windows`'
  contract (600-char sentence-anchored windows today:
  `"win600-sent-v1"`). Two ingests of the same book under
  the same versions produce the same passage ids; a
  window-format change produces a new passage population,
  which is the truth, stated in the ids.
- **Embedding stamps.** Every vector row carries
  `(model, format, dim)`: the model string from
  `CONFIG["embed_model"]` at embed time, and a format
  string (`"raw-f32-v1"` in v1) that versions the vector
  encoding itself — the slot a future quantized format
  (`"int8-v1"`) occupies without a schema change. This is
  `trope_catalog_vectors`' (model, format, content-hash)
  namespace discipline (:11879) applied per row. A run
  whose configured (model, format) differs from a row's
  stamp treats the row as absent (§4.4) — never as a
  foreign vector to compare against, because cosine across
  spaces is meaningless (the catalog cache's own words).

### 1.5 Capability probes (never assumptions)

At `CorpusDB` open, the handle probes once and records in
`meta`: FTS5 (attempt a throwaway virtual table on a temp
connection / read `PRAGMA compile_options` for
`ENABLE_FTS5`) and extension loading
(`enable_load_extension` callable). Absent FTS5 ⇒ corpus
opens **degraded**: text, passages, and vectors work; FTS
consumers (explain's text search, §6.2's alias evidence)
print "FTS unavailable on this Python's SQLite" once per
process and skip. Absent extension support ⇒ §2's fallback
path, which needs no extension at all. Degradation is a
printed property of the open, never a silent mode.

### 1.6 Invariants (tests-to-be)

- `test_corpus_roundtrip_chapter_text` — ingest a fixture
  EPUB; every chapter read back hashes to its stored
  `text_hash`; the root hash equals a fresh parse's.
- `test_passage_slice_identity` — every passage span,
  re-sliced from its chapter, hashes to the passage's
  `text_hash` (the §1.2 span decision, pinned).
- `test_corpus_ids_stable_across_reingest` — same file,
  same versions: passage id set identical after a forced
  re-ingest.
- `test_fts_matches_chapter_text` — a phrase planted in one
  chapter is found by FTS in exactly that chapter.
- `test_degraded_open_without_fts` — probe forced off:
  corpus opens, text/vector paths work, FTS consumers
  report unavailability, nothing crashes.

---

## 2. Vectors: storage and search are separate decisions

The brief's question — sqlite-vec vs BLOB + pure-Python —
conflates two decisions that have different right answers.
**Where the numbers live** (storage) determines whether the
corpus survives copies, backups, sandboxes, and the absence
of any particular binary. **How neighbours are found**
(search) determines latency. Storage must be unconditional;
search may be opportunistic. Decided separately:

### 2.1 Storage: BLOB-canonical, unconditionally

Vectors are stored as float32 little-endian BLOBs in
`passage_vectors` (§1.2), beside their (model, format, dim)
stamp. A corpus.db copied to a machine with no extensions,
opened by a replay sandbox, a report tool, or a future
DuckDB attach (§6.5), contains its vectors *as data* —
readable by anything that can read a BLOB and unpack
floats. The alternative — vec0 virtual tables as the
canonical store — makes the data itself depend on a
loadable binary: every reader, on every machine, forever,
must successfully load the same extension before the corpus
can be read at all. That is an unacceptable property for
the artefact whose entire purpose is to outlive individual
runs and tools. **Storage verdict: BLOB, no exceptions in
v1.** (This is also why the failure drill "missing
extension" in §13 degrades speed and never readability.)

### 2.2 Search: in-process scan is the v1 engine

Every v1 vector consumer is per-book or per-claim scoped:

- **G retrieval** ranks windows *within a claim's support
  chapters* — tens to a few hundred passages per claim
  (`retrieve_claim_candidates`, :5795). The whole-book set
  (~2,000 passages for FW; design point 5,000) is the
  largest working set any v1 query touches.
- **Emission adjudication** (emission design §3.2) compares
  a book's candidates against the *catalog* vectors — a
  small set, already served by the catalog cache.
- **Impulse clustering** compares one run's unresolved
  labels — dozens of vectors.

Latency, stated as estimates to be measured at the gate
(§13), for a 768-dim query against one book's 5,000
passages (3.84M multiply-adds):

| Engine | Per query, one book (5k passages) | Corpus-wide (1M passages) |
|---|---|---|
| Pure Python (list-comp dot) | ~0.15–0.5 s | ~30–90 s — not interactive |
| numpy (when importable) | ~1–5 ms | ~0.3–1 s |
| sqlite-vec vec0 (C scan) | ~1–10 ms | ~0.5–2 s (bandwidth-bound: reads ~3.1 GB of float32) |

The v1 engine is therefore: **load the book's vector block
once per run into memory (the corpus-backed tier of G's
cache, §4.4), scan in-process — numpy when importable
(deps are allowed by the user's 2026-10-16 ruling), a
pure-Python fallback otherwise, both shipped, top-k parity
pinned by test.** Corpus-wide brute force in pure Python
is the one genuinely bad cell in the table, and no v1
consumer asks for it: cross-book questions in v1 are
answered by FTS and joins (§6), not by corpus-wide ANN.

### 2.3 sqlite-vec: the documented acceleration path

sqlite-vec is adopted **as an optional index, when and if
a named consumer needs it** — not in v1's critical path.
The shape, pre-designed so adoption is a slice, not a
redesign:

- A `passage_vec0` vec0 **shadow index** built from
  `passage_vectors` (same passage ids), created by an
  explicit `--corpus-build-vec-index` maintenance step,
  stamped with the vector generation it was built from;
  a stale shadow (generation mismatch) is ignored, never
  silently served.
- Load mechanism: `conn.enable_load_extension(True)` then
  `load_extension`, via the **`sqlite-vec` pip package's**
  platform wheel (pinned in requirements; the package
  ships Windows/Linux/macOS binaries and a loader helper).
  Verified environment fact (§0): extension loading is
  callable on this VM's Python. On the user's machine the
  §1.5 probe decides; a failed load is a printed line and
  the §2.2 engine serves instead.
- **Adoption triggers** (any one suffices to schedule the
  slice): a corpus-wide similarity consumer is designed
  (cross-book "passages like this one" as a feature, not a
  query of the moment); per-book passage counts grow past
  ~50k (audiobook-length or anthology works); or the gate's
  measured pure-Python latency on the user's machine —
  without numpy — makes G's per-book overhead visible
  against G §10's ≤10% wall-clock bar.

### 2.4 Index maintenance on re-ingest

Vectors are content-keyed (§4.4), so maintenance is
surgical: re-ingest of a work deletes and rewrites only
that work's vector rows, in the ingest transaction's
second phase (§3.3). A model/format change does not touch
text at all — it starts a new vector *generation*:
`meta` records the current (model, format); rows stamped
otherwise are treated as absent and re-embedded lazily by
the next ingest or `--corpus-reembed` pass. There is no
bulk invalidation event and no mixed-space comparison is
ever possible, because the stamp check is on the read
path, per row (the catalog cache's rule, §0, at row
granularity).

**The drift canary (A1).** The stamp check proves a
row's *claimed* model; it cannot prove the endpoint
still serves that model. Embeddings come from a
configured endpoint under a model *string*, and an
endpoint re-pointed to different weights under the same
string would — now that vectors persist — poison the
store permanently and silently: every stamp still
matches, every cosine still computes, and the numbers
are all in the wrong space. The per-run cache never
faced this; persistence creates it, so persistence
carries its detector. Each vector generation is born
with a **sentinel set**: a small set of pinned probe
texts (code constants, lexically varied, no book
content), embedded at generation creation, with their
reference vectors and pairwise cosines recorded in
`meta` (§1.2) inside a tolerance band. The canary is
re-checked at every generation write, on every ingest,
on `--corpus-reembed`, and on `--corpus-status` (§7.4).
Drift outside the band ⇒ the generation is marked
**suspect** in `meta`; status prints the suspect state
as a standing warning; and every vector read treats the
generation's rows as misses (§4.4's rule) until the
generation is re-embedded and the canary passes again.
The §4.5 mutation test proves the *key* discipline in
the lab; the canary is the only staleness detector that
exists in production, and it is not optional machinery —
it is the price of the G5 re-rule (§4.4) being safe
outside a test.

### 2.5 Invariants (tests-to-be)

- `test_vector_blob_roundtrip` — a vector written and read
  back is bit-identical (float32 LE pack/unpack).
- `test_search_parity_numpy_vs_pure` — top-k over a fixture
  book identical (same ids, order within float tolerance)
  under both engines. The pure path is **forced** in the
  test (engine selection stubbed to pure), never
  discovered from ambient numpy presence: the user's
  Windows Python may lack numpy, and a parity test that
  silently runs numpy-vs-numpy on the build machine
  proves nothing about the fallback the user may
  actually run.
- `test_foreign_model_vector_never_served` — rows stamped
  with another model are invisible to search under the
  configured model (the §4.4 staleness rule at the vector
  layer).
- `test_drift_canary_marks_generation_suspect` — probe
  vectors perturbed beyond the tolerance band at a canary
  check ⇒ generation marked suspect in `meta`, vector
  reads miss its rows, and a re-embed with a passing
  canary restores service (A1).
- `test_corpus_readable_without_extension` — with extension
  loading disabled, every corpus table incl. vectors reads
  normally; only the (absent) shadow index is unavailable.

---

## 3. Ingest — the importer mode

### 3.1 Command shape and the one-parse-path guarantee

```
--ingest <file|dir> [--ingest-reason TEXT]   importer mode, then exit
--corpus-status [--json]                     report mode, then exit
--corpus-reembed [--work <fingerprint>]     vector generation fill
```

`--ingest` rides the learn/report command family in
`main()`'s argparse (the group at :16992ff beside
`--learn-dir`, `--replay`, `--capture-snapshot`). A
directory argument ingests every supported book file in
it (sorted, per-book outcome lines, failures isolated per
book — one bad EPUB never aborts a library backfill).

**The guarantee the whole design rests on: ingest parses
through the pipeline's own functions** —
`extract_epub_units` (:715) + `split_chapters` (:920) for
EPUB, `read_ebook` + `_prose_chapters` for other formats,
in the same order `process_file_v2` calls them (:15121).
There is no second parser to drift. "Corpus text is what a
run would have parsed" is therefore true *by construction*,
and §3.2's round-trip only has to prove the *storage* is
faithful — a much smaller claim, and the right one.
Frontmatter tagging, fallback heading splits, and chapter
detection diagnostics (`_DIAG.chapter_detection_fallback`)
behave identically, because it is the same code executing.
The fallback flag itself is part of the ingest record —
stored in `corpus_works.payload` at ingest — so a
corpus-sourced run (§11 slice 5) reproduces the
acquisition block's **full** output surface (chapters,
word_count, and the `_DIAG` / `_chap_fallback`
diagnostics downstream code reads) without re-deriving
any of it.

Ingest deliberately does **not** run any model: no Call A,
no Call B, no judge. Acquisition is deterministic — the
same file under the same parser version ingests to the
same corpus, bit for bit, on any machine. (Embeddings are
the one model-adjacent step, and they are quarantined into
their own phase with their own stamps, §3.3.)

### 3.2 Losslessness: the round-trip proof

At the end of every ingest, before the work may be marked
complete (§3.3), the importer verifies its own storage:

1. Re-read every chapter from `corpus_chapters`; hash each
   text; compare against the parse-time hashes.
2. Compute the **root hash** (sha256 over the ordered
   chapter hashes) and compare against the root computed
   from the in-memory parse.
3. Re-slice **every** passage span from its stored chapter
   and compare against the passage's stored `text_hash` —
   at 100%, at every book size, in v1 (A2). A sampled
   check was considered and rejected: sampling re-opens
   exactly the offset-corruption class the check exists
   to close, to save seconds inside an ingest whose
   embedding phase is measured in minutes.
4. Run FTS5's `integrity-check` over `corpus_fts` (A7).
   A mismatch fails the ingest exactly as a hash mismatch
   does: the index is derived and rebuildable, but a
   corrupt index at ingest time is evidence the write
   path is broken, not a detail to repair quietly.

Any mismatch ⇒ the ingest is `failed`, the work's rows are
rolled back (the transaction never commits a partial
book), and the failure is printed with the chapter index.
The §13 gate re-runs this verification *independently* —
fresh parse of the EPUB vs corpus contents, by a checker
that shares no code with the ingest writer beyond the
parse functions themselves — on the three gate books.

### 3.3 The completeness state machine

A work's `status` is the contract every corpus consumer
reads. States:

```
absent ──ingest──▶ ingesting ──▶ text_complete ──▶ complete
                      │                │  (vectors pending/partial)
                      └──▶ failed      └──▶ vectors_partial ──▶ complete
any state ──parser_version ≠ current──▶ stale
```

- **`ingesting`** is written by the ingest bookkeeping —
  the `corpus_works` row at status `ingesting` plus its
  `ingest_runs` row — and that bookkeeping **commits in
  its own small transaction before phase 1 begins** (A9).
  The ordering is load-bearing: bookkeeping riding inside
  phase 1's transaction would roll back with it, and a
  crash would leave no trace the book was ever mid-ingest.
  Committed first, a crash leaves the `ingesting` row
  behind as observable evidence, and the next ingest of
  that fingerprint treats it as absent-and-redo
  (idempotent re-entry, §3.4).
- **Phase 1 (one transaction):** chapters + passages,
  written against the pre-committed bookkeeping row; the
  FTS rows follow the chapter writes via triggers inside
  the same transaction (§1.3, A7). The transaction flips
  the work row to `text_complete` after the §3.2
  self-check. A book in `text_complete` is fully
  usable for text consumers (chapter sourcing, FTS,
  explain) — vectors are the only absence.
- **Phase 2 (second transaction):** embeddings for every
  passage lacking a current-stamp vector, batched via
  `embed_vectors` (:14151). The text sent for each
  passage is **re-sliced from the stored chapter at write
  time** (A2) — never carried in memory from the parse —
  so what the embedder receives is the stored span by
  construction, and no vector is ever computed over text
  the corpus does not hold. Failure mid-phase leaves
  `vectors_partial` with the per-passage rows that *did*
  land committed — resume is gap-filling by content key,
  never restart-from-zero. Completion flips status to
  `complete` when passage count == current-stamp vector
  count.
- **Consumers gate on status, by family:** chapter
  sourcing needs ≥ `text_complete`; retrieval needs
  `complete` (or falls back to per-run embedding for the
  gap, §4.4); anything reading a work in `ingesting` or
  `failed` treats it as **absent**. A half-ingested book
  can therefore never read as whole — the failure the
  brief names is excluded by the read path, not by
  ingest's good behaviour.
- **Completeness is generation-relative (A5).**
  `complete` records that an ingest finished under the
  embedder then current; the word does not move when the
  (model, format) generation does (§2.4). Every report
  and consumer therefore *composes* the stored status
  with current-generation vector coverage — the envelope
  (§6.4) is the normative composition — so a work
  completed under a superseded embedder presents its
  true current coverage (0% until re-embedded), and
  retrieval treats its vectors exactly as §4.4 treats
  any stamped-otherwise row: absent. No surface prints
  a bare `complete` as a coverage claim.

### 3.4 Idempotency and re-ingest triggers

- **Same file, same versions** ⇒ **no-op**: fingerprint +
  `parser_version` + `source_hash` match a `complete`
  work ⇒ printed line ("already ingested, parser vN,
  <date>"), no writes. (`--ingest --force` re-runs phase 1
  anyway — the deliberate-refresh analogue of snapshot
  refresh: named, reasoned, recorded in `ingest_runs`.)
- **Parser version change** ⇒ works stamped older are
  `stale` on next open/status. Re-ingest is a *versioned
  replacement*: phase 1 rebuilds chapters/passages in one
  transaction (old generation's passages and their vectors
  are deleted in the same transaction — superseded
  generations do not accumulate, because passage ids are
  generation-scoped by construction, §1.4); stage outputs
  keyed to the old generation's input hashes become
  non-current automatically (§4.2) and are garbage-
  collected by the same maintenance pass that reports
  them. An `ingest_runs` tombstone row preserves the fact
  and date of the superseded generation for forensics.
  The generation swap also refreshes the work's envelope
  **in the same transaction** (§6.4): a payload must
  never pair a new text generation with sections computed
  against the old one.
- **Embedding model/format change** ⇒ text untouched;
  only the vector generation turns over (§2.4). This is
  the cheap re-ingest: minutes of embedding, zero
  re-parsing.
- **A changed file** (new edition) is a new fingerprint —
  a new work, ingested independently. The old edition's
  corpus remains until explicitly removed
  (`--corpus-remove <fingerprint>`, named for symmetry
  with `--roster-*` command precedents; removal is a
  human act, printed, and never cascades into state.db.
  Its scope is **one transaction across every corpus
  table keyed by the fingerprint** (A10) — works,
  chapters, passages, vectors, stage runs and outputs,
  quotes, and the envelope — with per-table counts
  printed. Partial removal is not offered: an orphaned
  stage output or quote record would resurrect on
  re-ingest, including a selected signature quote the
  removal existed to forget.)

### 3.5 Invariants (tests-to-be)

- `test_ingest_lossless_fixture` — §3.2's three checks on
  a fixture EPUB with frontmatter, a fallback-split
  chapter, and a >16k-char chapter.
- `test_ingest_idempotent_noop` — second ingest writes
  nothing (corpus dump-hash identical) and reports noop.
- `test_half_ingested_never_read_as_complete` — a work
  forced to `vectors_partial` serves text consumers and
  is refused by retrieval-as-complete; a work forced to
  `ingesting` is invisible to all consumers.
- `test_ingest_resume_fills_gaps` — phase 2 killed at k
  vectors resumes to exactly the full set, no duplicates
  (content-key uniqueness).
- `test_parser_bump_stales_and_replaces` — version bump ⇒
  stale listing; re-ingest ⇒ new passage population, old
  vectors gone, tombstone present.
- `test_ingest_bookkeeping_survives_crash` — ingest
  killed between the bookkeeping commit and phase 1 ⇒
  the `ingesting` row is observable in status, and the
  next ingest re-enters cleanly (A9).
- `test_fts_integrity_check_gates_ingest` — a corrupted
  FTS index at self-check ⇒ ingest `failed`, no
  `text_complete` flip (A7).
- `test_fts_reingest_finds_new_not_removed` — after a
  parser-bump re-ingest, FTS finds the new generation's
  text and no longer finds text the replacement removed
  (the §1.3 trigger mechanism's pin: rowids are not
  stable across generation replacement, and the index
  must stay honest through the rewrite).

---

## 4. The stage contract

### 4.1 The stamp and the input hash

Every stage that writes to the corpus records a
`stage_runs` row and keys its `stage_outputs` by:

```
(fingerprint, stage, output_key)  +  input_hash, run_id
```

where **input_hash** is a sha256 over a canonical
serialization of *everything the output depended on*:
the text hashes of the chapters/passages consumed, the
stage's config digest (the spine's `_config_digest`
discipline), and the input hashes of any upstream stage
outputs consumed. The stamp tuple the brief names —
`(input_hash, stage, model, stage_version)` — is the
row's identity of provenance: `model` is the model that
produced it (extraction model, embed model, judge model,
or `"deterministic"` for pure stages), `stage_version`
versions the stage's own contract (prompt + code path),
bumped the way `parser_version` is.

The rule that makes re-processing safe, stated once:
**an output is current iff its input_hash equals the
input hash recomputed from the corpus's present state.**
A stage re-run recomputes its input hashes; outputs whose
hash matches are *reused* (the re-run is a no-op for
them); the rest are settled **inside one transaction**
whose delete scope is the stage's **whole
`(fingerprint, stage)` key space** (A4): the re-run
enumerates the output keys it produces now; rows for keys
whose hash changed are replaced, and every stored key
for that `(fingerprint, stage)` *not* in the current set
is **deleted in the same transaction**. Deleting the
no-longer-produced is half of what replacement means:
without it, a stage that stops producing an output — a
claim that no longer retrieves, a quote no longer
proposed — leaves a ghost row indistinguishable from
current output, and the envelope (§6.4) reads the latest
stage output, which is exactly where a ghost surfaces
(the sweep's F19 precedent, designed out here). The new
`stage_runs` row stamps the same transaction — commit or
nothing. A stage never touches another stage's rows, and
a stage's replacement never reaches outside its own
`(fingerprint, stage)` space. This is replace-wholesale
(the corpus-write doctrine the program already proved on
Supabase rows) turned inward, with content addressing
doing the targeting that wholesale replacement does by
scope.

### 4.2 First adopters, and what stays out

**Wave 1 (this design's build):**

1. **`embed`** — passage vectors themselves (§2). The
   stage whose re-rule §4.4 spells out; its outputs are
   the `passage_vectors` rows (kept in their own table for
   scan ergonomics, stamped and keyed exactly as the
   contract requires).
2. **`retrieve`** — G's results. Today
   `evidence_retrieve_result` (:6140) returns
   `trope_evidence` + per-claim records that live in the
   run's result and die with it; the abstention accounting
   (Trope amendment B: a negative retrieval is an explicit
   recorded result, searched-chapters named) is re-derived
   or lost per run. As stage outputs keyed by claim key,
   with input hash over (support-chapter text hashes,
   embed stamp, judge model, floor, query-shape version,
   and the stage_version itself — A6: a judge-prompt edit
   that left the hash unmoved would keep yesterday's
   verdicts "current" under a judge that no longer
   exists),
   retrieval becomes *reusable*: a re-run of the pipeline
   whose text and stamps are unchanged re-reads yesterday's
   verdicts instead of re-judging — and a text or model
   change re-derives exactly the affected claims. This is
   the single largest repeated-processing win in the
   design, because G's judge calls are the expensive,
   variance-carrying part of evidence work.
3. **`quote-verify` / `quote-select`** — §10's two layers,
   stage-shaped from birth.

**The enumeration rule (every adopter, no exceptions):**
a stage joins the contract only with its hashed inputs
enumerated in its design entry, the way `retrieve`'s
entry above enumerates its own. `input_hash` completeness
is the contract's hard invariant: reuse over inputs
nobody hashed serves outputs derived from unexamined
state, silently — the exact staleness class §4.4's
content key exists to exclude. An adopter whose inputs
cannot be enumerated does not adopt; it stays a per-run
computation until they can.

**Wave 2 (named, not built here):** extraction payload
*references* (a stage row pointing at a snapshot's frozen
draw — §4.3 — not a copy of it); the emission design's
adjudication outputs (its §3.4 chain is a stage in
everything but storage); chapter summaries as derived
stage outputs (today they are draw data inside snapshots,
built at :10540).

**Out of v1, deliberately:** decision-call *recording*
(spine OC-5 stands: decision calls stay live in replay;
the stage contract records stage *outputs*, and a JEV
verdict consumed by a stage is an input whose hash covers
the request, not a stored response); trigger/relationship
machinery beyond G's existing records; any stage whose
inputs include live network state (verdict read-back is
state.db's cache concern, not a corpus stage).

### 4.3 Composition with replay and snapshots (reconcile, don't duplicate)

Replay and the corpus freeze **different layers**, and
the design keeps both rather than merging them:

| | Snapshot (spine) | Corpus (this design) |
|---|---|---|
| Freezes | the **draw** — Call A/B payloads | the **source** — text, passages, vectors |
| Answers | "same extraction, changed pipeline: what moved?" | "same text, any stage: re-derive without re-acquiring" |
| Lifetime | curated baselines, refreshed deliberately | the library's working memory |
| Store | files under `snapshots/<fingerprint>/` + registry row | `corpus.db` |

**Snapshots reference the corpus; they do not merge into
it.** `capture_snapshot` (:14918) gains one manifest
block:

```json
"corpus": {"fingerprint": "…", "parser_version": "…",
           "text_root_hash": "…",
           "chapter_hashes": ["…", …]}
```

— written at **every** capture, from the capture run's
own parse: `chapter_hashes` and `text_root_hash` are the
hashes of the text the run itself extracted, recorded
independently — **never read back from the corpus row**.
That independence is the property the block exists for:
hashes copied from the corpus would make replay
validation degenerate to "corpus unchanged since
capture," attesting nothing about the draw. Run-recorded
hashes attest the wanted thing — corpus text == the text
the snapshot's draw was extracted from — including the
case the fingerprint cannot see: it samples only the
first substantial chapter (:14553), so a same-fingerprint
file with different later chapters is exactly what
per-chapter hash validation exists to catch.
`parser_version` rides along as the run's own stamp. The
block does not depend on a corpus ingest existing at
capture time; it is absent only for captures that
predate this design. The snapshot's
own payload files stay exactly as they are
(self-contained: a snapshot must remain valid if
corpus.db is deleted, rebuilt, or never existed — its
payloads are the baseline, and a baseline that depends on
a second store's survival is not one). What the reference
buys: a **replay with `--use-corpus` can serve its
retrieval layer from the corpus** — G-in-replay reads
passages and vectors by id instead of re-embedding
windows out of payload text — *after validating* that the
snapshot's recorded chapter hashes match the corpus's
(per-chapter hash comparison; a mismatch — different
parser generation, edited corpus — refuses the corpus
path for that replay and falls back to today's behaviour,
printed). **Replay corpus access is read-only, pinned.**
Under replay the corpus tier serves reads only: §4.4's
gap-fill write-back does not apply, and a retrieval miss
under replay embeds in-memory (G5's original behaviour)
rather than writing. A measurement instrument mutating
the library's working memory would be a new hole in
replay's isolation doctrine — replays write only inside
the sandbox substrate, and the live substrate's dump
hash must not move — so the corpus inherits the doctrine
whole. The pin sits in §4.5. Passage ids (§1.4) are the join key, and they
are safe to reference precisely because they are
content-derived: a snapshot naming a passage id names a
span of text, and either the corpus holds that exact span
(hash-verified) or it holds nothing under that id.

The reverse direction is refused, on the record: corpus
stage outputs never *require* a snapshot. Ordinary runs
source from the corpus without any snapshot existing;
snapshots remain the gate books' curated instrument, not
a runtime dependency the library must carry.

### 4.4 The G5 re-rule (what exactly changes)

G5's text (the `retrieve_claim_candidates` docstring,
:5795): the window-embedding cache is "per-run and
in-memory only, keyed by chapter label + window offset,
shared across claims, discarded when the run ends — never
persisted and never shared across runs."

**Re-ruled:** the prohibition was never on persistence;
it was on a key that cannot detect staleness. The cache
key becomes the **content key** — (passage text hash,
embedding model, embed format) — the catalog cache's
namespace discipline (:11879) at passage granularity.
Under that key, persistence is sound, and G5 is amended
to:

> Window embeddings are keyed by content — passage text
> hash + embedding model + format version. A vector is
> served only when all three match the current passage
> and the run's configured embedder; any mismatch is a
> miss and a re-embed, never a serve. The corpus is the
> persistent tier of the cache; the per-run in-memory
> cache remains in front of it, unchanged in shape.

Mechanically: `_evidence_run`'s cache (today a bare dict
threaded through `retrieve_claim_candidates`) becomes a
two-tier lookup — memory, then corpus `passage_vectors`
(read when the run is corpus-sourced or the work is
`complete` in the corpus), then `embed_vectors` with the
result written back to the corpus *when the run holds a
complete ingest to write against* (and never under
replay — §4.3's read-only rule; a run on an uningested
book does not sprout a partial corpus entry as a side
effect — ingest is a named act, §3; the run's embeddings
for such a book stay in-memory, G5's original behaviour,
which remains correct for unretained books).

**The staleness proof** (the property that makes the
re-rule safe, pinned by test): mutate one chapter's text
in a fixture corpus ⇒ that chapter's passage text hashes
change ⇒ every vector keyed to the old hashes misses ⇒
re-embedding occurs; the untouched chapters' vectors
still hit. Change the configured embed model ⇒ every
vector misses (stamp mismatch), wholesale, exactly as the
catalog cache invalidates. There is no path by which a
vector computed over different words, or in a different
space, is served — the failure G5 existed to prevent is
excluded by the key, and the §13 gate demonstrates it
rather than asserting it.

### 4.5 Invariants (tests-to-be)

- `test_stage_rerun_replaces_own_outputs_only` — stage A
  re-run with changed inputs replaces A's rows
  transactionally; stage B's rows and hashes are untouched
  (dump-hash comparison over B's key space).
- `test_stage_rerun_same_inputs_is_noop` — identical
  input hashes ⇒ zero writes, outputs reused, the
  `stage_runs` row records `status: ok` with a reuse
  count.
- `test_stage_rerun_deletes_unproduced_outputs` — a
  re-run producing a strict subset of its previous keys
  deletes the missing keys in the same transaction; the
  envelope afterwards reflects the subset, not the ghost
  (A4).
- `test_input_hash_covers_upstream` — changing an
  upstream stage's output changes the downstream stage's
  input hash (retrieval re-derives when embeddings turn
  over, even though the text did not change).
- `test_g5_content_key_staleness` — §4.4's proof, both
  directions (text mutation, model change).
- `test_replay_corpus_ref_validates_by_hash` — a snapshot
  whose recorded chapter hashes match the corpus replays
  retrieval from corpus passages; one hash mutated ⇒
  corpus path refused for that replay, fallback printed,
  replay itself unaffected.
- `test_replay_corpus_access_read_only` — a `--use-corpus`
  replay leaves the corpus dump-hash unchanged: no
  gap-fill write-back, misses embed in-memory, reads only
  (the §4.3 rule, pinned beside
  `test_writer_family_discipline`).

---

## 5. Split shape: one repo, two modes, one file contract

### 5.1 Modes, not apps (v1)

Per the user's conversation: the importer and the
processor are **two entry-point modes in this repo** —
`--ingest` (and its maintenance siblings) on one side;
the existing run/report commands on the other — becoming
separate programs only if they diverge (different
machines' schedules, different owners). The boundary
between them is the corpus schema + `corpus_v`, never
shared in-memory state: the importer's contract with the
processor is exactly "a `complete` work in corpus.db,"
and the processor's contract back is "stage outputs
stamped per §4." Either side can be rewritten behind its
mode without the other's knowledge, which is the property
a two-app split would exist to buy — obtained here at
one-repo cost.

### 5.2 One writer per table family; WAL expectations

| Table family | Writer | Readers |
|---|---|---|
| `corpus_works/chapters/passages`, FTS | importer mode only | processor, reports, explain |
| `passage_vectors` | importer phase 2; `--corpus-reembed`; a corpus-sourced run filling gaps in a `complete` work (§4.4; never under replay, §4.3) | retrieval, analytics |
| `stage_runs/stage_outputs`, `quotes` | processor stages | reports, explain, payload builder |
| `work_envelope` | the envelope refresh (§6.4), run at stage-write and ingest-event time | explain, payload path |
| `meta` | CorpusDB open (probes, generation stamps) | all |

The mode-scoped write guards this table names are a
property of **handle construction**, not convention: a
handle is built *in* a mode and refuses other families'
writes from its first day (§5.4's
`test_writer_family_discipline` enforces them). Slice 1
builds the guard into the handle (§11), because
retrofitting it after slices 2–4 land their writers is
where the mechanism would hurt.

SQLite serializes writers anyway (WAL: one writer, many
readers); the rule's purpose is not lock avoidance but
*accountability* — a table whose contents surprise you
has exactly one suspect. WAL expectations mirror the
substrate's: readers (report commands, `--explain`,
dashboards) may open corpus.db read-only while an ingest
or a run holds it; the rolling-backup rule (backup API,
never raw copy — spine A2) applies to every corpus copy
operation, including §7.2's backup and §5.3's mobility
copies.

**Contention directions, ruled (A9).** The corpus handle
sets `busy_timeout` on open; beyond that, the failure
directions are asymmetric on purpose. **Processor-side
corpus writes degrade**: stage outputs, vector gap-fills,
and envelope refreshes that meet a locked database print
and proceed *unrecorded* — a corpus write never fails a
run (§5.3's direction: slower or poorer, never wrong).
**Ingest refuses**: an ingest that cannot take the write
lock exits cleanly with a printed line rather than
interleaving with another writer — two importers never
share a book, and phase transactions stay whole-book
regardless of contention. Readers are unaffected
throughout (WAL).

### 5.3 Mobility: what travels, what does not

corpus.db is designed to be copied between the user's
machines (ingest where the books are; process where the
GPU is — the user's stated shape). The copy discipline:

- **Travels:** corpus.db as a single artefact, copied via
  the backup API (or after `PRAGMA wal_checkpoint(TRUNCATE)`
  through the handle) so the copy is one self-contained
  file with no `-wal`/`-shm` sidecars to lose. It is
  self-contained by §1.3's option A: text, index, vectors,
  stamps, provenance — everything a processor on the far
  side needs, nothing it must rebuild.
- **Does not travel inside it:** `config.json` and
  anything credential-shaped (spine §3.9's rule, inherited
  verbatim — a file that gets copied, backed up, and
  attached to bug reports never contains keys); machine-
  local endpoint configuration (embed/LLM URLs are
  *configuration*, resolved on the destination machine);
  absolute source paths (the work row records the
  fingerprint and identifiers — identity — and the
  source filename as provenance text, never as a path
  another machine must honour).
- **Independence:** corpus.db and state.db travel
  separately and neither requires the other to open.
  `--explain` (§7.1) is richest with both present and
  degrades to the stores that exist, saying so.

**Corpus loss is a designed non-event.** corpus.db
corrupt ⇒ F8 discipline (loud, `.corrupt` preserved, no
auto-restore), with one corpus-specific mechanics rule
(A11): preservation is a **rename-in-place** — the file
and its `-wal`/`-shm` sidecars are renamed to their
`.corrupt` siblings, never copied. At §8's sizes (≈2.4 GB
at 300 books) a preservation *copy* can fail on exactly
the nearly-full disk that plausibly caused the
corruption, and a copy that dies midway leaves the
preservation itself in doubt; a rename is atomic on the
same filesystem and cannot half-happen. Processing then
*falls back to file parsing* — today's behaviour — for
every affected book.
The corpus's failure direction is **slower, never wrong**:
it is retained work, and retained work can always be
re-derived from the books themselves, which remain the
ultimate source. (Contrast state.db, whose NO pairs and
sighting histories exist nowhere else: the substrate is
precious, the corpus is *rebuildable*, and §7.2's backup
priority reflects exactly that ordering.)

### 5.4 Invariants (tests-to-be)

- `test_corpus_copy_is_self_contained` — backup-API copy
  of a fixture corpus, opened alone in a fresh directory:
  status report and a retrieval read succeed with no
  other file present.
- `test_writer_family_discipline` — importer-mode handle
  refuses stage-output writes; processor-mode handle
  refuses chapter writes (mode-scoped write guards, the
  §5.2 table enforced in code, not convention).
- `test_corpus_absent_run_byte_identical` — with no
  corpus.db present and no corpus flags, a flag-off
  preview is byte-identical to today's (the corpus is
  opt-in machinery; its absence is the status quo).

---

## 6. Analytics — the headline motivation, made concrete

The user's stated reason for the whole program: *stronger
analytics in the end*. This section names the queries that
justify the store, each mapped to the tables that answer
it. A query nobody can name does not get a table (§1.2's
promotion rule runs both ways).

### 6.1 The query contract

1. **Longitudinal quality per work/author/series.** For
   each run of a work: ship/stage/drop sets, promotion
   events and their `via`, G abstention and adjacency
   counts, regenerated-rejection events — joined across
   runs, then rolled up by author and series. Sources:
   `stage_outputs` (retrieve), `staging_claims` +
   `rejection_events` (state.db), `fixture_scores`. *Was
   impractical:* staging history lived per-file with no
   cross-work query; retrieval results died with the run.
2. **Churn on bit-identical inputs.** Trope/claim Jaccard
   across two runs *of the same corpus generation*: text
   hash equality proves the input did not move, so the
   residual variance is draw/prompt/model — attribution
   the dashboard's two numbers (spine §1.4) approximate
   today and the corpus makes exact for every stage that
   adopts §4. The query names **emission volume beside the
   Jaccard** — each run's set sizes printed next to the
   similarity — because the churn history this program
   measured was dominated by yield collapse, and a Jaccard
   printed alone invites reading a collapse as
   instability. Sources: `corpus_works.text_root_hash`,
   `stage_runs.input_hash`, preview artefacts.
3. **Series identity drift.** A character's canonical
   forms, aliases, and roster outcomes across every work
   of a series, joined on series metadata: where book 3's
   roster calls her X and book 5's calls her Y, which
   passages support each form. Sources: `roster_entities`
   (state.db) + `series_state` + FTS over `corpus_chapters`
   (every occurrence of each name form, with chapter
   citations — the *alias evidence* query: a proposed
   alias arrives with its corpus-wide occurrence counts
   and sample spans attached, instead of a reviewer's
   memory).
4. **Duplicate and overlap detection.** Identical
   `text_hash` chapters (or passage hashes) across
   different fingerprints: shared chapters between an
   anthology and its constituent novels, duplicate
   editions under different ISBNs, a sample chapter
   ingested twice. This is D5's missing evidence (the
   anthology guard currently refuses on a hardcoded
   `is_anthology: False`): overlap is now a query, and
   D5's eventual ruling gets numbers instead of a guess.
5. **Threshold calibration.** §6.3.
6. **The envelope.** §6.4.
7. **Library coverage rollup.** Per-section presence and
   state counts across every ingested work — one
   `GROUP BY` over `work_envelope` — answering "are we
   there yet" for the feature gates that wait on corpus
   facts (the Previously On trigger, Finale's honesty
   rule, the mood gate from the new-data consult).
   Feature readiness stops being an impression: a gate
   names the section and the count it needs, and the
   rollup says whether the library has it.

**Generation honesty (A8).** A work's text is anchored
to its parser generation (§1.4), and generations can
segment the same book differently. Cross-work queries
(1, 3, 4, 7, and every rollup built on them) therefore
restrict to the **current parser generation** by
default; a report that deliberately spans generations
prints the generation mix on its face — §6.3's rule (a
number without its provenance is not a measurement)
applied to parser provenance. Silently joining across
generations would manufacture apparent drift that is
the parser's, not the books'.

### 6.2 What changes about answering

Each query above is a SQL statement over files that
already exist locally — no run, no model call, no
network. That is the qualitative change the user is
buying: questions about the library stop costing
*processing* and start costing *a query*. The analytics
surface in v1 is deliberately unglamorous: the §7.3
calibration report and `--corpus-status` ship as commands;
a dashboard "Corpus & trends" section (additions-only,
the preview_variance precedent) is the named second step,
fed by the same queries, and is **not** in v1's build
(OC-7).

### 6.3 Threshold calibration — first deliverable, honestly scoped

`learned_threshold_stats` holds, per trigger, two samples
of decision-model P(yes): kept and dropped, 500/bucket
(`record_threshold`, :3638). Today the corpus–state
system can say nothing about them beyond a count. The
**calibration report** (command in §7.3) computes, per
trigger: the two distributions (deciles, n per bucket),
the separation between them, and the P(yes) value at
which the observed kept-rate crosses named levels — the
input a human needs to move a validation threshold from
*vibes* to *the score distribution the judge actually
produces*, per trigger instead of globally.

The honest limit, designed-in rather than discovered:
the samples carry **no work, run, or downstream-outcome
keys**, so v1's report is *distributional* — it cannot
yet say "samples above 0.8 were confirmed by humans 94%
of the time," because nothing joins a sample to its fate.
The design closes that prospectively, not retroactively:
the relationship-validation stage adopts the §4 contract
(wave 2, §4.2), and its stage outputs record samples
*with* work/run/outcome refs, so outcome-joined
calibration becomes possible from adoption forward. The
v1 report prints this limit on its face (the G3 rule: a
number without its provenance is not a measurement).
Beside the limit, the report prints its **decision
rule**: distributional shape may inform a threshold
conversation; it may not move one. **A threshold move
requires the wave-2 outcome-joined calibration** — only
samples joined to their fates can say what a score was
worth, and v1's samples cannot be joined (§0).

### 6.4 The completeness/confidence envelope (Data & Sync spec)

Per the Data & Sync seat's consult additions (provenance,
`corpus_v`, retraction, scope — filed at
`~/workspace/ebook-processor-sweep/new-data-consult/
data-sync.md`), every work carries an **envelope**: a
per-section record of what the system knows about the
work, materialized in `work_envelope`. Refresh
discipline splits by source family: **corpus-sourced
sections** (text, vectors, retrieval, quotes) are
materialized and refreshed in the same transaction as
the write that changes them — a stage write, or an
ingest event including a generation swap (a payload must
never pair a new text generation with sections computed
against the old one, §3.4). **State.db-sourced sections**
(roster, staging, verdicts, claims shipped) change
through non-corpus paths — ordinary runs, `--roster-*`
commands, verdict fetches — so they are **composed live
at read / payload-build time** from state.db's current
contents and never materialized at all: there is no
refresh trigger to forget, and they cannot publish stale
facts between corpus stage writes. Sections and
their sources:

| Section | Presence source | Scope |
|---|---|---|
| text (chapters, passages) | corpus status (§3.3) | edition |
| vectors | vector generation + coverage against the CURRENT (model, format) (A5) | edition |
| retrieval (G) | latest `retrieve` stage output | edition |
| characters / roster | `roster_works` (state.db) | work |
| staging | `staging_works` (state.db) | work |
| claims shipped | last run's result markers | work |
| quotes | `quotes` counts by status (§10) | edition |
| verdicts | `verdicts_cache` freshness (state.db) | work |
| series | Cozy skeleton pull (read-back) + series-judgment stage outputs | series |

The vectors section is the **normative coverage
statement** for a work (A5): composed against the
current (model, format) generation, so a work completed
under a superseded embedder reports its true current
coverage — 0% until re-embedded — and never the bare
status word `complete` (§3.3).

**The series section's home (user ruling, 2026-10-16).**
Series-scoped facts have no single source, and the
design splits them by kind. The **bibliographic
skeleton** — membership, order, published totals,
completion — is **pulled from Cozy Libram, not derived
by the processor**: Cozy persists the series skeleton it
already fetches from Hardcover (series, author, ordered
books with positions and ISBNs) into a shared corpus-level
table, and the processor reads it through the **verdict
read-back pattern** — GET-only, cached locally, stamped
with its fetched-at time, degrading gracefully when
unavailable. Provenance on the section reads "via
Hardcover, via Cozy, fetched <time>"; no Hardcover
credential and no new network dependency enters the
processor. The **text-derived series judgments** —
standalone-safety, reading-order necessity, recurring
cast — stay processor-side, computed as stage outputs
**against that skeleton**: the pull is what tells the
processor two books share a series and in what order,
which is the precondition for computing them at all. An
unavailable pull leaves the section `unknown`
(first-class, per the state rule below) — never guessed
from text. **Named dependency:** the app-side
persistence (Cozy writing the skeleton at fetch time) is
a companion item owned by the app side (Builder / Data &
Sync seats); until it exists, the pull has no source and
the series section is `unknown` for every work — a
designed absence, not a defect. Finale's honesty rule
consumes series totals from this section, which is why
totals come from the curated skeleton and not from
counting ingested books.

Each section record is `{state, confidence?, evidence_tier?,
coverage?, stage, model, stage_version, updated_at, scope,
retracted_at?, reason?}` where
**`state` ∈ {present, unknown}** — the seat's first-class
rule, adopted verbatim: absence renders as `unknown`,
never as `none`; "assessed, none found" is a *present*
section whose payload says so (G's abstention records are
exactly this shape). **`coverage`** rides sections whose
"present, none found" could render as a safety claim
(triggers at minimum): the fraction of the work the
assessment actually covered, so a partial assessment can
never present as a whole-book clearance (the rendering
contract below). **The retraction trace** — `retracted_at`
+ `reason` on a withdrawn section's record — keeps "never
assessed" and "assessed, then withdrawn" distinguishable
downstream forever; safety-class facts need exactly that
distinction, and an envelope that collapses them has
forgotten the one fact a later reader most needs.
`corpus_v` rides the envelope from
day one; additive-only within a version. The envelope is
what the publication payload (§10.5, §6.5) is built from
— with a **boundary**: the payload carries each section's
**state, evidence tier, and provenance — never the raw
confidence number**. Confidence stays processor-side (the
envelope record keeps it; `--explain` and the calibration
report consume it), extending §10.2's doctrine — votes
never ship as a number — to envelope confidence: a
decimal in a payload reads as a measurement and invites
the app to re-gate on it, which is the processor's job,
done once. What Cozy Libram receives is per-section facts
with their state, tier, and provenance attached — Advisor
addition A from the same consult (provenance + confidence
*recorded* as data, per field, from birth) — rather than
bare values whose certainty the app must guess.

**The rendering contract (fixed at design time).**
Payload strings have a way of becoming UI strings
verbatim, so the state→rendering map is part of this
design, not an app afterthought: **`unknown` renders as
silence** — no badge, no "unknown" text on user surfaces
(curator/admin views may show "not yet assessed");
**assessed-none renders as no positive claim except at
full coverage** — a triggers section below full
`coverage` never renders as "No content warnings" or
anything a reader can take as certified clean; and a
section **present with zero selected** (the poll-shed
quotes state, §10.5) renders as silence too — no surface
shows a "nothing available" placeholder for a garnish
that was never promised. **Provenance vocabulary is fixed
here**: source renders as **"From the text" / "From
readers" / "From the publisher"**; evidence strength
renders at most as **"Checked against the text"** vs
unmarked. Tier enums and confidence decimals are
machine-side only and never cross into user-visible
strings.

**Retraction is a named operation** (the seat's third
addition), designed in at this layer — a verb, not an
emergent behaviour. A retraction: (1) writes an **explicit
retraction record** in the stage layer (the `ingest_runs`
tombstone precedent: what was withdrawn, when, and the
reason class, preserved for forensics); (2) supersedes
the underlying stage output through the ordinary stage
replacement (§4.1); and (3) refreshes the envelope
entry, stamping its `retracted_at` + `reason` trace —
**all in one transaction**, so no reader ever sees the
record without the withdrawal or the withdrawal without
the record. The payload builder reads only current
outputs, so a retraction propagates to the next payload
by construction. What *authorizes* a retraction of
human-corrected data is D1's question (§0's scoping
rule) — this is mechanism only, and it waits for D1
without blocking.

**Currency is enforced at the payload boundary.** The
payload builder ships a section only if its stage output
is **current** — its `input_hash` recomputes equal
against the corpus's present state (§4.1's rule, checked
at build time, not assumed). A section that fails the
check is omitted (it renders `unknown`), never shipped
stale; and because an output's hash covers its upstream
inputs, retracting or replacing an upstream output
cascade-invalidates its downstream sections at the same
boundary — the pin (§6.6): retract upstream ⇒ the next
payload omits the downstream fact.

**Multi-edition rule.** A work with several ingested
editions has one envelope per fingerprint; the payload
ships work-scoped sections once and **edition-scoped
sections per edition**, each block keyed by its source
edition's identity (fingerprint + ISBN-13 — the same
identity §10.6 attaches to quotes). Two editions'
edition-scoped facts are never silently merged into one
section: the consumer picks the block matching the
reader's edition, and any surface showing another
edition's facts can say which edition they came from,
because the block says so.

**The taxonomy is a registry.** The section table above
is the one place new-data sections join; a future section
is registered here — name, source, scope, and **value
shape** (the enum or band set its payload may carry) —
*before* the app builds a consumer for it, so value
shapes are fixed in one place and never reverse-engineered
from a payload. Registered now, built later: **ending**
(work scope; enumerated outcome classes, fixed by the
book-facts stage, §11), **heat shape** (work; bands only,
never counts or locations), **POV** (work; enumerated
person + tense), **mood** (work; derived coarse bands
only — never extracted claims), **setting facets** (work;
facet enumerations), **series** (series scope; per the
series paragraph above). Each inherits the section record
wholesale — state, evidence tier, provenance, coverage
where safety-relevant — from birth.

### 6.5 DuckDB: the read-only escape hatch

Named now so nobody builds it wrong later. If a future
analytic outgrows SQLite-in-process — corpus-wide vector
math over ~1M passages, Fellegi–Sunter fitting over the
uncertainty queue's labels, churn dashboards across
hundreds of works — **DuckDB attaches corpus.db read-only**
(its SQLite extension) or reads a Parquet export produced
by a corpus maintenance command, and does its work there.
Two rules, absolute: **DuckDB never writes corpus.db**
(two engines owning one SQLite file is a WAL-ownership
and corruption hazard, and §5.2's writer table has no row
for it), and **exports are derived artefacts** — deletable,
rebuildable, never the source of truth. v1 builds no
DuckDB code; it designs the door and labels it.

### 6.6 Invariants (tests-to-be)

- `test_envelope_unknown_never_none` — a work with text
  but no retrieval renders retrieval as `unknown`; a work
  whose retrieval ran and abstained everywhere renders a
  present section recording exactly that.
- `test_envelope_refreshes_on_stage_write` — a stage
  write for a work updates its envelope sections and
  `computed_at` in the same transaction.
- `test_envelope_state_sections_never_stale` — a roster
  change made through a non-corpus path (no corpus stage
  write) is reflected in the next envelope read, because
  state.db-sourced sections are composed live, never
  materialized (§6.4).
- `test_payload_omits_noncurrent_section` — an upstream
  output retracted or replaced ⇒ the next payload omits
  the downstream section whose input hash no longer
  recomputes (currency at the payload boundary, §6.4).
- `test_calibration_report_prints_provenance_limit` —
  the v1 report carries its distributional-only notice
  and per-trigger n beside every decile table.

---

## 7. The folded-in data package

The three items the earlier (held) data package promised,
now riding on the corpus instead of standing alone.

### 7.1 `--explain` — per-claim / per-character forensics

```
--explain <work> [--claim <claim_key>] [--character <name>]
```

One read-only report joining both stores for one work:

- **For a claim:** every staging sighting (run, chapters,
  name forms), the promotion event and its `via` + stamp,
  suppression events (`rejection_events`) and the verdict
  cache state behind them, and — the corpus's contribution
  — the `retrieve` stage record: outcome, support grade,
  chapters searched, and the attached passages *quoted
  from the corpus spans* with chapter + offset citations.
  "Why is this trope on this book" becomes one command
  whose answer ends at literal text.
- **For a character:** the roster lineage — mint, folds
  (adoptions), splits, anchor state, NO pairs — plus the
  corpus side: first/last passage occurrences by FTS +
  span citation, and every name form's occurrence counts
  across the work (and, with a series flag, across the
  series' ingested works — §6.1 query 3 in command form).
- **For quotes:** the work's selected quotes with their
  verbatim text, screen results, and spoiler tier — the
  SELECT layer's audit surface. Sampling audit,
  deliberately not a queue: VERIFY is deterministic
  truth and needs no approval flow, but a screen miss
  would otherwise publish silently; the listing makes
  selection *inspectable* per work, which is the
  proportionate check.
- Read-only in the strong sense: opens both DBs read-only
  where they exist, works with either store missing
  (prints which half is absent), writes nothing.

### 7.2 `--backup-state` / `--restore-state` — one command, both stores

```
--backup-state <dir>     snapshot state.db + corpus.db + manifest
--restore-state <dir>    verify, then restore both
```

- **Backup:** each DB copied via the backup API (A2) into
  `<dir>/` as `state.db` + `corpus.db`, plus a
  `manifest.json`: per-file logical dump hash (the
  substrate's `dump_hash` discipline, corpus analogue),
  schema versions (`user_version`, `corpus_v`), portable
  version, timestamp. The pair + manifest is the unit;
  a backup dir missing any element is not a backup.
- **Restore:** verifies manifest hashes against the
  backup files *before* touching the live ones; refuses
  major schema-version mismatches (minor/additive:
  proceeds, the open path re-applies DDL per §1.1);
  replaces live files only after verification, keeping
  the replaced pair as `<name>.pre-restore-<date>`
  beside them — restore is itself reversible, because a
  restore discovered to be wrong must not be a second
  disaster. Restore is a human-invoked command only:
  nothing in any run path ever restores automatically
  (the substrate's no-auto-restore rule, extended).
- The rolling per-run backup stays state.db-only
  (§5.3's ordering: the substrate is precious, the corpus
  is rebuildable); corpus.db joins the *commanded*
  backups, which is where multi-gigabyte artefacts belong.

### 7.3 `--calibration-report` — the first analytics deliverable

§6.3's report as a command (console + `--json`): per
trigger, kept/dropped P(yes) deciles with n, separation,
crossing points, and the provenance-limit notice. It is
first for a reason: it consumes only existing data
(`learned_threshold_stats` — no ingest required), so the
analytics promise pays out in the first slice that ships
a report, before the corpus holds a single book.

### 7.4 `--corpus-status`

The learn-status analogue (`cmd_learn_status`, :8673):
per-work lines (title, fingerprint prefix, status, parser
version vs current, chapters, passages, vector coverage
% — always against the **current** (model, format)
generation, §3.3/A5 — stage summary, envelope freshness),
totals (works by status, quotes by status, corpus.db bytes broken into
text / FTS / vectors / other via page counts per table),
the vector generations with their canary state — a
generation marked **suspect** by the drift canary (§2.4,
A1) prints as a standing warning line, because status is
a canary check point, not just a report — and the standing
sentence the substrate report taught us: **corpus.db IS
retained book text, not a cache** — deleting it costs
re-ingest, never correctness (§5.3) — printed so no
cleanup script or future operator has to guess which
kind of file it is.

---

## 8. Sizing, and what v1 does not build

### 8.1 Storage per book (FW-class: 115 chapters, ~1.01M chars)

Estimates from the G design's measured reference book;
**measured on the real gate books at the §13 gate** and
the scorecard replaces these with observations.

| Component | Bytes/book (768-dim) | Bytes/book (384-dim) |
|---|---|---|
| Chapter text (plain) | ~1.01 MB | ~1.01 MB |
| FTS5 index | ~0.5–1.0 MB | ~0.5–1.0 MB |
| Passage spans (~2,000) | ~0.15 MB | ~0.15 MB |
| Vectors (float32) | ~6.1 MB | ~3.1 MB |
| Stage outputs + quotes + envelope | ~0.05 MB | ~0.05 MB |
| **Total** | **~7.8–8.3 MB** | **~4.8–5.3 MB** |

Vectors dominate — by design honesty: full-book float32
coverage is the expensive choice, and OC-5 prices it.
Library scale at 768-dim: 100 books ≈ 0.8 GB; 300 books
≈ 2.4 GB. The quantization lever (`"int8-v1"` format,
§1.4) quarters the vector line if size ever binds; it is
a format version, not a redesign, and is not built in v1.

### 8.2 Ingest wall-clock per book (estimates, gate-measured)

Parse + split + store + FTS: seconds (stdlib zip/HTML
over ~1 MB of text). Embeddings: ~2,000 windows in
batched POSTs to the user's local endpoint — the dominant
cost, estimated 1–4 minutes on the user's stack (G's
per-run experience embedded comparable volumes inside a
~1-hour run's overhead). Total one-time ingest:
**~1–5 minutes per book**, against every subsequent
re-analysis dropping its parse + embed costs to reads.
A 300-book backfill is an overnight job, stated plainly;
it is also exactly once.

### 8.3 What v1 deliberately does NOT build

- Chunk-level (Call A/B input) retention — draws remain
  checkpoint/snapshot property (spine §3.9).
- Passage-level FTS (chapter-granular only, §1.3).
- Corpus-wide ANN / the sqlite-vec shadow index (§2.3 —
  designed, trigger-gated, not built).
- DuckDB integration code (§6.5 — door designed, not
  opened).
- Auto-ingest on run start (OC-4 — ingest is a named act).
- Corpus sync between machines (file copy per §5.3 is
  the mechanism; no sync protocol).
- Snapshot payload migration into the corpus (§4.3 —
  references, not absorption).
- Multi-writer corpus access, network corpus access, any
  corpus content in Supabase (the publication path carries
  claims, envelope facts, and §10's selected quotes —
  never book text; §9's posture note).

---

## 9. Failure modes + abuse cases (cross-cutting)

- **The corpus as single point of failure.** One file now
  holds every book's text. Compensations: §7.2's commanded
  backups; §5.3's ordering (corpus loss = re-ingest,
  never wrong output — the fallback is today's file
  parsing, exercised by `test_corpus_absent_run_byte_identical`);
  F8 discipline so corruption is loud and bytes are
  preserved. Residual risk, named: an operator deleting
  corpus.db as "just a cache" costs an overnight
  re-backfill (embeddings dominate). §7.4's status report
  names the file as retained text to prevent exactly
  that misreading.
- **Stale-vector laundering.** A persisted vector served
  across a text edit or model change would silently
  corrupt every downstream cosine — G5's founding fear,
  now with a larger blast radius. Excluded by the content
  key (§4.4) and demonstrated at the gate (§11 signal 5),
  not merely asserted.
- **Parser split-brain.** If ingest and runs ever parsed
  differently, corpus-sourced and file-sourced results
  would diverge invisibly. Excluded by construction (one
  parse path, §3.1) and by the §11 byte-identity signal,
  which compares the two sourcing paths on the same book
  through the replay instrument.
- **Generation mixing.** A work half-re-ingested under a
  new parser serving old-generation passages beside new
  chapters. Excluded by §3.3's state machine (consumers
  gate on status) + §3.4's single-transaction generation
  replacement.
- **The corpus quietly becoming doctrine.** Retained text
  and pretty analytics create pressure to let corpus
  *statistics* decide claims ("this passage pattern
  usually means X"). The header doctrine forbids it: the
  corpus is source and record; decisions stay in the
  stages, under their own designs and gates. Analytics
  (§6) report; they do not vote.
- **Posture note — full text at rest.** corpus.db holds
  the user's books' full text on the user's own machine,
  the same posture as the EPUB files it was built from.
  It is never published, never synced to Supabase, and the
  publication payload carries claims and short verified
  quotes (§10.5) — not text. Backups (§7.2) multiply the
  text's on-disk footprint by the number of backup
  generations kept; that is the user's disk and the
  user's call, named here so it is a decision rather
  than a discovery.

---

## 10. Quotes: verification + decision-model selection

The corpus turns quotes from a per-run fight into a
two-layer stage. The layers are kept apart on purpose:
**verification is deterministic truth about text;
selection is judgment about taste and risk.** The Trope
Review seat's consult verdict set the shape — a signature
line is adoptable *only* with verbatim verification,
because as model recall it is a fabrication engine (the
seat's rejection checklist already carries a wrong-book-
quote pattern from ordinary claims) — and the UX seat
named the gap this closes: verbatim-vs-paraphrase is
currently *unclassified* in the pipeline. This stage
classifies it, permanently, for every quote the system
ships.

### 10.1 Layer 1 — VERIFY (deterministic, fail-closed)

A pure corpus function:

```
verify_quote(fingerprint, candidate_text)
    -> {chapter_index, start, end, snapped, drift, occurrences} | None
```

- **Scope is the work's own rows.** The match runs against
  the fingerprint's chapters only (FTS narrows to chapters
  containing the candidate's rare tokens; a token-sequence
  scan confirms). A quote from another book *cannot*
  verify against this one — wrong-book quotes are
  impossible by construction, not by vigilance.
- **Scope is also kind- and chapter-scoped (A3).** An
  **evidence-kind** candidate carries an asserted chapter
  (its claim's), and verification searches **that chapter
  only**. A match in a different chapter does not verify
  it: the outcome records drift class `chapter` (the
  found location rides the record as a diagnostic, §10.3)
  and the verification fails — it never extends the
  evidence claim's chapter set, because G grades support
  by *distinct* chapters and a repeated line found
  elsewhere would inflate that grade with a chapter the
  claim never asserted. **Book-wide** search is reserved
  for the signature and opening kinds, and for candidates
  carrying no chapter assertion; there, the chapter the
  line is found in *becomes* its assertion.
- **Occurrence discipline.** A line can occur more than
  once, and the wrong occurrence is a wrong chapter
  attribution. The verify record therefore carries
  **match multiplicity** (`occurrences`: how many spans
  in scope matched), and the choice rule is fixed:
  signature and opening kinds take the **earliest**
  occurrence in the book; an evidence-kind match is
  already confined to its asserted chapter (A3), and
  within that chapter the earliest occurrence ships.
  When a claim's support-chapter set is known and a
  book-wide-verified span lands outside it, the record
  says so (`outside_support_chapters`) as a diagnostic —
  G's grading reads the claim's chapters, and the record
  must not let a found-elsewhere line masquerade as
  support.
- **Matching discipline:** normalize both sides with the
  existing `_norm_quote` discipline (the `verify_evidence`
  machinery, :11613, generalized from a chunk to the
  book). Tolerant of **whitespace and punctuation drift**
  — curly vs straight quotes, em-dash spacing, ellipsis
  forms, capitalization at sentence starts. **Strict on
  words:** the candidate's word-token sequence must equal
  the span's, in order. A word substituted, inserted, or
  omitted (including a "…" elision of middle words) is a
  **no-match** — that is a paraphrase wearing quotation
  marks, and it fails closed: no match, no ship.
- **Snap-to-span:** when the words match modulo drift,
  the quote *becomes* the corpus span — the shipped text
  is the span's verbatim bytes, never the candidate's
  rendering — and provenance records the candidate text,
  `snapped: true`, and the drift class. A candidate that
  was already verbatim records `snapped: false`. Either
  way, every shipped quote carries **chapter + offset +
  verification status** (§10.3), so its truth is checkable
  from its own record forever.
- **Sentence boundaries for signature spans.**
  Verification proves the words exist contiguously; it
  does not prove the span is a complete thought, and a
  fragment reads on a share card exactly like a
  misquote. A **signature-kind** span must therefore
  begin and end at sentence boundaries in its chapter —
  checkable at verify time against `_sentence_spans`
  (:5699) — and a candidate that verifies only as a
  fragment is ineligible for signature selection, full
  stop. Fragments remain fine as *evidence* quotes, where
  the claim carries the meaning; the opening line is
  sentence-bounded by construction. Snap fixes drift,
  not truncation; this rule is the truncation half.
- **Where it lives:** a standalone stage over the corpus
  (`stage = "quote-verify"`, §4 contract), with three
  call sites — (a) **ingest-time**, for the deterministic
  candidate set only (the opening line, §10.4 — it exists
  before any model runs); (b) **run-time**, for
  model-proposed quotes: extraction evidence quotes are
  verified against the corpus when the work is ingested,
  upgrading `verify_all_evidence`'s per-chunk check
  (:11619) to a whole-book, permanent one (an uningested
  book keeps today's per-chunk behaviour — the corpus
  improves the check where it exists, and is never a
  precondition); (c) **on demand**, for candidates from
  any future surface, via the same function. Verification
  is never a batch step that "processes the corpus" in
  the abstract: it answers for a candidate, at the moment
  a candidate exists.

### 10.2 Layer 2 — SELECT (decision-model poll, metadata only)

The decision model **polls only verified candidates** —
an unverified candidate is not shown to it, because a
quotability judgment about a fabricated line is wasted
budget on a lie. For each verified candidate the poll
returns:

- **quotability** and **representativeness** votes (does
  this line stand for the book?), and
- two **screens**: *spoiler* (does the line reveal an
  outcome, identity, or ending?) and *heat* (explicit
  content — the consult's rule for anything share-shaped:
  UX + Advisor both condition signature lines on a heat
  screen before mixed-age surfaces ever see them).

**Votes are metadata.** They *rank* the candidates that
pass; they never constitute a claim, never surface as a
confidence, and never ship in any payload as a number
(the house judge doctrine, G §4's precedent: verdicts
ride as metadata, the grade is computed deterministically
or not at all). The **ship gate is conjunctive and
mostly deterministic**: verified ∧ spoiler-screen pass ∧
heat-screen pass ∧ caps (§10.4). The poll orders the
passing set; it cannot promote a screen-failed or
unverified candidate into it, and a tie or a low vote
total changes *which* passing line ships, never *whether
the rules were met*.

### 10.3 The quote record

In the `quotes` table (§1.2): the span (chapter, offsets),
the **verbatim span text**, `kind`
(`evidence|signature|opening|…`), `status`
(`candidate|verified|selected|rejected`), and a payload:

```json
{"verify": {"candidate_text": "…", "snapped": true,
            "drift": "punctuation", "occurrences": 1,
            "stage_run": "…"},
 "source": "extraction|deterministic|manual",
 "poll":   {"model": "…", "quotability": 0, "representative": 0,
            "spoiler": "pass", "heat": "pass", "rank": 1},
 "spoiler_tier": "mild"}
```

Status semantics by kind: for `evidence` quotes,
`verified` is the terminal good state (whether the claim
ships is the claim pipeline's decision, not this stage's);
for `signature`/`opening`, `selected` is the publication
state (§10.5). `rejected` carries its reason in the
payload (`no_match` | `spoiler_screen` | `heat_screen` |
`cap`), so the desk can see *which* layer killed a line —
the classification the UX seat asked for, kept as data.

### 10.4 Caps and sourcing (the consult's conditions, as rules)

For the `signature` kind, the consult converged and the
design adopts the convergence as generation rules, not
guidelines:

- **One selected signature line per book. One sentence.
  Hard length ceiling** — the D&S seat's ≈140-char cap is
  adopted as the schema-level limit for signature
  selection (Advisor: caps are the discipline; Designer:
  one line, never a rotation, never on tiles).
- **Early/iconic sourcing, enforced at generation:**
  eligible candidates are (a) the **opening line** — the
  literal first sentence of the first non-frontmatter
  chapter, generated deterministically at ingest (the
  Strategist's freebie: zero inference, always a
  candidate); (b) verified evidence quotes from the
  book's first half; (c) extraction-proposed signature
  candidates that verify. **Candidates from the final
  quarter of the book are ineligible for signature
  selection in v1** regardless of poll outcome — a climax
  line is a spoiler class, and excluding the region at
  generation removes the class instead of asking a screen
  to catch every instance of it. (The screens still run:
  generation rules bound the territory, screens judge
  the individual line; belt and braces, deliberately.)
- Signature quotes carry the field-level **spoiler tier**
  `mild` (Advisor addition B: classify fields once) in
  their record and payload, so display policy consumes a
  fact, not a vibe.

### 10.5 Budget posture (recommendation)

Quote polling draws the **shared decision budget**
(`_dm_budget_claim`'s pool of 50/book) — at the **lowest
priority** (after merges, then family validation, then
the trigger decision gate — the pool's existing order),
**batched to one call per book** (all verified candidates
in a single poll), and **first shed**: if the pool is
exhausted by higher classes, or the call fails, polling
is *skipped*, the stage run records `poll: "skipped"`,
and **no signature line ships for that book** —
selection without screens is not a degraded mode this
design offers; the envelope (§6.4) records the quotes
section as present with zero selected, which is the
honest rendering. Under §6.4's rendering contract that
state renders as **silence** in the UI: a missing
garnish is not a gap state, and no surface ever shows a
"no signature line available" placeholder. A separate allowance was considered
and rejected: a second ledger re-opens the contention
accounting the shared budget exists to close, and
enrichment that can starve family validation — the worst
claim class — has its priorities inverted (G §9.3's
lesson, applied before it can bite). Polling is a
garnish; its absence costs a garnish.

### 10.6 Flow to publication

Selected quotes ride the **work-level corpus payload** —
the server-authoritative channel the D&S seat specified
(§6.4's envelope is its carrier). The quote section is
**edition-scoped** and states its source edition's
identity — **fingerprint + ISBN-13 when known** — at
section level: a verified quote is a fact about one
edition's text, and the payload says which. Per selected
quote it carries `{text, chapter_label, kind,
spoiler_tier, screens: {spoiler, heat}}`. **Verified
`opening` quotes ship as their own payload kind**, in
their own right — not only when the opening line is also
the selected signature (the app's dormant first-line
clue consumes them directly); they pass the same
screens, and the §10.4 caps hold app-side as fixed text
zones the payload does not relax. **`chapter_label` is
the chapter's own heading** where the ingested text
provides one (the stored `label`), falling back to
`Chapter N`: it is a pointer into the *source* edition
named by the section, and surfaces must not present it
as a pointer into the reader's own edition — prologue
and frontmatter counting diverges across editions, so a
bare index can be wrong for the copy in the reader's
hands even when it is right for ours. Candidate debris
and poll internals stay
processor-side (reachable via `--explain`); the payload
carries the attestation (verified, screened, tiered),
not the paperwork. Unselecting a quote — a stage
replacement under §4.1 — removes it from the next payload
by construction, which is the seat's retraction
requirement met by the ordinary mechanism; the consumer
half is a stated requirement, not a hope: **the app
drops an unselected quote from rendered surfaces on
next sync, including any locally cached copy** — a
retraction the app keeps rendering from cache is only
half real.

**Display contract for `spoiler_tier`.** The screens'
pass is the display fact: a selected quote ships because
it passed. `spoiler_tier` is a **field-class policy
marker** — it classifies the *field* (signature quotes
as a class are `mild`, §10.4), informing which surfaces
may carry the field at all; it is never a per-line
grade, gate, or label, and no surface re-gates, badges,
or warns on a cleared line using its tier. Double-gating
a line the screens cleared would make the payload
contradict itself; the tier's consumer is placement
policy, not the reader. What the app
does with the section (About zone, italic, attributed —
the Designer's placement) is app doctrine consuming
payload facts; this design's boundary is the payload.

### 10.7 Invariants (tests-to-be)

- `test_quote_never_ships_unverified` — no path from
  candidate to selected that skips `verify_quote`
  (stubbed-poll assertion: the poll receives only
  verified candidates).
- `test_snap_records_provenance_and_ships_span` — a
  punctuation-drifted candidate ships the corpus span's
  bytes; the record holds candidate text + drift class.
- `test_word_substitution_fails_closed` — one word
  changed ⇒ no match, status rejected/`no_match`,
  nothing ships.
- `test_cross_book_quote_never_verifies` — a genuine
  quote from book A, proposed for book B (both ingested),
  does not verify against B.
- `test_evidence_quote_wrong_chapter_fails` — a genuine
  line asserted against the wrong chapter fails
  evidence-kind verification with drift class `chapter`
  recorded and the claim's chapter set unchanged; the
  same line as a signature candidate verifies book-wide
  and adopts the found chapter (A3).
- `test_poll_is_metadata_only` — poll votes alter rank
  within the passing set and nothing else: a screen-fail
  with top votes is not selected; poll output appears in
  no confidence field anywhere.
- `test_poll_shed_is_fail_closed` — budget exhausted ⇒
  poll skipped, zero signature selected, run completes
  normally, stage run records the skip.
- `test_final_quarter_ineligible` — a verified,
  screen-passing candidate from the final quarter is not
  signature-selected in v1.
- `test_verify_records_occurrence_preference` — a line
  occurring three times in scope: signature selection
  ships the earliest occurrence, the record carries
  `occurrences: 3`, and a span landing outside a known
  support-chapter set records the
  `outside_support_chapters` diagnostic.
- `test_signature_span_sentence_bounded` — a candidate
  that verifies only as a mid-sentence fragment is
  ineligible for signature selection; the same fragment
  verifies as an evidence quote.
- `test_payload_chapter_label_from_heading` — the
  payload's `chapter_label` is the chapter's stored
  heading where one exists, `Chapter N` only as the
  fallback.

---

## 11. Build plan (slices in order; each independently testable)

Order rationale: the store before its writers; ingest
before sourcing; the stage contract before its second
adopter; quotes after the contract they ride; the data
package last because explain/backup are worth most when
there is something to explain and two files to protect.
The calibration report is the exception — it needs no
corpus at all and ships in slice 1's train as the
analytics down payment (§7.3).

1. **CorpusDB core + reports.** Handle (on the shared
   substrate base, §1.1), schema, probes
   (§1.5), F8 discipline, `--corpus-status` (correct on an
   absent/empty corpus), `--calibration-report` (§7.3).
   The **mode-scoped write guards are built into handle
   construction in this slice** (§5.2) — slices 2–4 land
   writers against guards that already exist, never the
   reverse. Tests: §1.6 probe/degraded pins.
2. **Ingest phase 1.** `--ingest` (file + dir), one parse
   path, text/passages/FTS, completeness machine,
   round-trip self-check. Tests: §3.5.
3. **Vectors.** Phase 2 embedding, `--corpus-reembed`,
   content-keyed reads, in-process search (numpy + pure
   parity). Tests: §2.5, §4.5 staleness.
4. **Stage contract + retrieve adoption.** `stage_runs` /
   `stage_outputs`; G's records become stage outputs;
   two-tier cache (§4.4) behind corpus-sourced reads.
   Tests: §4.5. Note for this slice: stage accounting
   needs a **scoped dump hash** (per fingerprint + stage
   key space) for §4.5's pins and §13 signal 8 — the
   substrate's `_dump_hash_conn` is whole-DB over a fixed
   table list, so this slice adds the small variant
   helper here rather than discovering the need mid-gate.
5. **Corpus-sourced runs + replay composition.**
   `--use-corpus` (default OFF) chapter acquisition in
   `process_file_v2`; snapshot manifest corpus block;
   replay hash validation + corpus retrieval path.
   Corpus-sourced acquisition must reproduce the :15121
   block's **full output surface** — chapters,
   `word_count`, and the `_DIAG` / `_chap_fallback`
   diagnostics (the fallback flag read from
   `corpus_works.payload`, §3.1) — not chapter text
   alone; §13 signal 2a is read as covering the whole
   surface. Tests: §5.4, §4.5 replay pin.
6. **Quote verification.** `verify_quote` + stage
   adoption; deterministic opening-line candidates at
   ingest; run-time evidence-quote verification for
   ingested works. Tests: §10.7 (verify half).
7. **Quote selection + payload.** The poll (shared
   budget, §10.5), caps, payload quote section, envelope
   quotes section. Tests: §10.7 (poll half).
8. **Explain + backup/restore.** §7.1, §7.2. Tests:
   explain on fixture stores (both, either, neither);
   backup/restore hash verification + version refusal.
9. **Validation gate (not code).** §13's protocol, run
   and scored, scorecard written to `eval/` beside this
   doc.

**Post-gate, named now so substrate-first has a
terminus:** the first new-field build after the §13 gate
passes is the **book-facts stage** — ending guarantee +
trigger depth — riding the §4 contract and registered in
§6.4's taxonomy. It is sequenced *after* the gate, not
inside it: the gate's signals are untouched by its
existence, and no field ships on a substrate that has
not passed.

---

## 12. Open choices (recommendation in bold)

- **OC-1 — corpus.db location.** **The learn dir, beside
  state.db.** The learn dir is already "the processor's
  state lives here" on every machine and in every report;
  a second location invents a second thing to back up,
  point sandboxes at, and explain. The books-directory
  alternative (corpus travels with the library) couples
  state lifetime to media management — books get moved
  by tools that know nothing of corpora.
- **OC-2 — Text storage form.** **Plain text + in-file
  FTS5 (§1.3 option A)**, with option C (compressed
  canonical + rebuildable sidecar index) as the named
  pressure valve if text+FTS — not vectors — ever drives
  size. Option B (paying for both copies in one file) is
  rejected outright.
- **OC-3 — Vector engine.** **BLOB-canonical storage +
  in-process scan (numpy when available, pure-Python
  fallback); sqlite-vec as the documented, trigger-gated
  acceleration index (§2.3).** vec0-as-canonical is
  rejected: the corpus's readability must never depend on
  a loadable binary (§2.1).
- **OC-4 — Ingest trigger + run sourcing.** **Explicit
  `--ingest`; `--use-corpus` default OFF for ordinary
  runs in v1.** Auto-ingest on first sight would make a
  routine run silently pay minutes of embedding and
  gigabytes of commitment — the migration-surprise class
  the spine's OC-7 debate bounded with verification and
  printed rollback; ingest gets the same respect by being
  a named act. Auto-sourcing when a `complete` ingest
  exists is the named default-flip candidate *after* the
  §13 identity signal passes.
- **OC-5 — Embedding scope at ingest.** **All passages
  embedded at ingest** (costed in §8: it is most of the
  ingest wall-clock and most of the file). Text-only
  ingest with vectors on first need re-creates G5's
  per-run embedding cost at the first G run of every book
  and makes "the corpus is ready" a lie of omission;
  coarser chapter-mean vectors would delete passage
  retrieval — G's whole mechanism — to save the money.
  The honest price is paid once, at the named act.
- **OC-6 — Snapshot composition.** **Manifest corpus
  references; payloads stay self-contained (§4.3).**
  Migrating snapshot texts into the corpus would make
  baselines depend on the corpus's survival — the one
  dependency direction §4.3 refuses.
- **OC-7 — Analytics v1 scope.** **Calibration report +
  corpus-status + explain; the dashboard trends section
  deferred.** The queries (§6.1) are designed now so the
  section is a rendering slice later; v1's build budget
  goes to the store and the stages, not the chart.
- **OC-8 — Quote poll budget.** **Shared pool, lowest
  priority, one batched call per book, first shed,
  shed ⇒ no signature (§10.5).** A separate allowance is
  the alternative; it is rejected for reopening the
  two-ledger contention the shared budget exists to
  close, and because enrichment must never be able to
  starve family validation even by accident of ordering.
- **OC-9 — Publication path.** **Cozy Libram consumes the
  corpus only via the server-authoritative payload**
  (envelope + quotes sections), never by reading
  corpus.db directly — the file is a local processing
  artefact on one user's machine; the payload is the
  contract. Direct reads by app-side tooling would couple
  the app to a schema version it does not own.

---

## 13. Validation gate (pre-registered)

Fixed set, signals named in advance, gate books Fourth
Wing / Daemon / Freedom™ (the F/G/spine set). Every rate
reports its n (G3). Ship/no-ship rests on these signals
as written; in particular signals 2 and 3 are absolute in
the spine §8 sense: a retention layer that changes what
the pipeline computes is not a retention layer.

1. **Losslessness.** Independent checker (shares no code
   with the ingest writer beyond the parse functions):
   fresh parse of each gate EPUB vs corpus — every
   chapter hash equal, root hashes equal, passage slice
   identity 100%. Any mismatch fails the build.
2. **Sourcing identity.** (a) Acquisition-surface
   identity: corpus-sourced acquisition returns chapter
   hashes identical to file-sourced acquisition, per
   book — and the rest of the :15121 block's output
   surface with them (`word_count`, the chapter-detection
   fallback diagnostics; §11 slice 5) — deterministic and
   absolute. (b) Replay on one snapshot
   per book, G flag on, retrieval corpus-sourced vs
   payload-sourced: retrieval *candidates* identical
   (pre-judge, deterministic); deterministic sections of
   the two previews identical; judge-carrying sections
   reported with their variance named, not pinned (spine
   §1.5's honesty about live decision calls).
3. **Corpus-absent identity.** No corpus.db, no corpus
   flags: flag-off previews on the gate books
   byte-identical to the pre-corpus code's, and the full
   suite passes with no weakened or deleted tests.
4. **Performance, measured not estimated.** Ingest
   wall-clock per gate book (parse/store vs embed
   phases split); corpus.db bytes per book broken out
   per §8.1's table; per-claim retrieval latency
   corpus-backed vs the per-run-embedding baseline on
   the same draws. All written into the scorecard beside
   §8's estimates, whatever they say.
5. **Staleness demonstration.** §4.4's two mutations on a
   fixture corpus (one chapter's text edited; embed model
   changed): vector misses occur exactly as specified —
   the edited chapter re-embeds, untouched chapters hit;
   model change misses wholesale.
6. **Failure drills.** Corrupt corpus.db ⇒ F8 discipline
   + a run completes file-sourced. Extension loading
   disabled ⇒ §2 fallback serves, notice printed,
   nothing else changes. A work forced to
   `vectors_partial` and one to `ingesting` ⇒ consumers
   behave per §3.3's family gates; `--corpus-status`
   shows the true states. Restore from a backup with a
   mismatched major schema version ⇒ refused before any
   live file is touched.
7. **Quote drills.** Planted candidates on the gate
   books: a punctuation-drifted genuine line snaps to
   its span (record shows candidate + snapped + drift);
   a one-word-substituted line fails closed; a genuine
   Daemon line proposed for Freedom™ never verifies;
   budget exhaustion during a poll ⇒ no signature ships
   and the run is otherwise unaffected. A
   verified-but-fragment candidate ⇒ ineligible for
   signature selection (§10.1's sentence-boundary rule);
   a human-legitimate elided quote ⇒ fails closed — the
   accepted trade, confirmed on the record; a quote from
   a chapter with its own heading ⇒ the payload label is
   the heading, not the index (§10.6).
8. **Stage accounting.** Re-running a stage over
   unchanged inputs writes nothing (dump-hash over the
   stage's key space identical) and reports reuse;
   changing one chapter's text re-derives exactly the
   outputs whose input hashes cover that chapter.

---

*Sources: process-portable.py on master @ 6e1476a
(v2.11.1), read firsthand for every citation;
eval/spine-design.md (substrate pattern, A1/A2/A5,
§3.9 non-moves, OC-5); eval/g-evidence-design.md (G5 as
written, the seam and budget doctrine, G3/G4 measurement
discipline); eval/emission-design.md (§3.4 resolution
chain as a stage consumer, substrate extension pattern);
the Data & Sync seat's new-data consult (envelope,
corpus_v, retraction, scope) and the Advisor / Designer /
UX consult replies on signature lines (caps, screens,
verbatim-or-nothing), all filed at
~/workspace/ebook-processor-sweep/new-data-consult/;
environment probes (SQLite 3.45.1, FTS5, extension
loading) run firsthand on this VM, 2026-10-16.*
