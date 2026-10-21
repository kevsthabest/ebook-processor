# Wiki ingestion — characters, relationships, series identity, recaps: spec

Date: 2026-10-21. Status: DRAFT for ruling. Processor target: 2.14.
App-side contract lands later in `cozylibram`; this file is the record
both repos build against.

## 1. Problem (measured)

The processor is a precision instrument; the wiki is a coverage product.
Today's write path drops what it cannot prove, so the app receives a clean
but thin cast.

- `book_characters`: 442 rows, all `status='candidate'`.
- `characters`: 71 canonical rows; `character_links`: 0; `character_relationships`: 0.
- `book_trope_claims`: 859 (420 confirmed / 378 candidate / 61 rejected).
- `book_trigger_claims`: 587 (440 candidate / 147 rejected).
- Fourth Wing fixture baseline: 2/5 true family pairs survive, 0/13 false
  pairs survive, 18 evidence-less relationships skipped per book.

Four gates after extraction cause most of the misses:

1. Only Call A mints relationships, one chapter at a time.
2. Discovery (`_discovery_shadow_run`) finds missed pairs but is shadow-only.
3. `_filter_principals` keeps a relationship only when both ends are principals.
4. `_build_rel_map` skips evidence-less claims; `_drop_ghost_relationships`
   removes endpoints with no character row.

Identity is per work. The series registry is a derived local hint store,
and `--persist-roster` is opt-in, so two books never share character identity
unless a human links them.

## 2. Decisions

D1. **Three runs.** `extract` → `analyze` → `merge`. Each run has one
    artefact, one fingerprint chain, and one job. `merge` is the only stage
    that writes to Supabase; `extract` and `analyze` are pure.

D2. **Within-book identity belongs to `analyze`.** The existing
    `stage_merge_book` (roster reconcile, Tier 1–3 merge, Jev bake-off)
    becomes an internal phase of `analyze`. The name `--stage merge` is
    reserved for cross-book identity and publishing.

D3. **The app's identity tables are the system of record.**
    `characters` + `character_links` (audit trail, `linked_by`, `note`)
    own cross-book identity. The processor proposes links and auto-confirms
    only the safe rule: same author + same series + identical normalized
    name. The local `series_registry.db` stays a hint source, never a
    second truth.

D4. **Write the full cast.** `--principals-only` becomes a presentation
    filter, not a write filter. Every rostered character ships with `role`,
    `tier`, `tier_source`, `vitality`, first/last appearance. The app groups
    by role and may hide minors; the database does not discard them.

D5. **Relationships ship as reviewable edges.** `merge` writes
    `character_relationships` rows with `review_status='pending'`, evidence,
    `evidence_source`, `validation_state`, `claimed_type`, and `confidence`.
    An evidence-less claim is not dropped: it ships as:
      - `type='other'`
      - `claimed_type=<original>`
      - `validation_state='needs_evidence'`
    A family type without binding evidence never ships as that family type.
    This preserves the fail-closed rule for the claim while preserving the
    person for the reviewer.

D6. **Discovery is promoted from shadow to candidate generation.**
    Co-occurrence pairs not emitted by Call A are nominated, graded against
    sentence binding, optionally embedding-ranked, and judged only when
    nominated. Accepted pairs become `source='discovery'` edges with
    `review_status='pending'`. They never auto-confirm.

D7. **Provenance and idempotency on every processor row.**
    Every written row carries `source_run` and a stable `pipeline_key`
    (`work + entity` for characters, `work + pair + type` for edges).
    Re-running `merge` over the same analyses is a no-op.

D8. **Series position is a first-class field.** `works.series_key` and
    `works.series_position` carry the ordering the recap feature needs.
    No new `series` table in this slice.

D9. **Events are the recap substrate.** A later `analyze` phase extracts
    chapter events into `book_events`; recaps compose from events up to the
    reader's position. Not built in the first slice, schema specified now.

D10. **No silent writes.** `merge` defaults to a plan; `--apply` is
     required. `extract` and `analyze` refuse any Supabase write at the
     `sb()` seam.

## 3. The three runs

```
book file ──extract──▶ extraction artefact ──analyze──▶ analysis artefact
                                                              │
                                      (1..N analyses)         ▼
                                                   merge ──▶ Supabase
```

Artefacts live under
`<learn_dir>/artefacts/<stage>/<book_fingerprint>/<run_key>/`.
Text is never duplicated: the extraction manifest carries chapter hashes,
and `analyze` reads the book's text from `corpus.db` (keyed by source hash)
or re-parses the file. Chapter payloads, roster, and manifests are JSON.

### 3.1 `--stage extract`

Input: one book file.

Output: `manifest.json`, `roster.json`, `chapters/<idx>.json` (Call A and
Call B payloads, verification stats, text hash).

Manifest fields: `format_version`, `extract_key`, `book_key`, `fingerprint`,
identifiers, title, author, chapter count, coverage, extractor identity
(pipeline, llm, model, processor version, code SHA), `extraction_flags_hash`,
`source` (`file` or `corpus`, with SHA-256), `created_at`.

Rules:
- No Supabase access, no preview, no merge.
- `--resume` and `--replay` keep their current semantics.
- Aborted extraction writes no completed manifest.
- `--stage extract` replaces the current refusal; it is the missing step 4
  of the stage split.

### 3.2 `--stage analyze`

Input: one extraction artefact (only) + corpus (read-only) + learned state.

Output: `manifest.json`, `analysis.json`, `analysis.md`.

`analysis.json` sections:
- `roster`: entities with stable `entity_id`, canonical name, aliases,
  forms, chapters, merges, unresolved mentions.
- `characters`: full cast, role, tier, tier_source, vitality, frequency,
  first/last appearance, description, appearance, confidence, evidence.
- `relationships`: edges with canonical direction, type, `claimed_type`,
  evidence, `evidence_source` (`extracted|mined|discovery`),
  `validation_state` (`validated|review_required|needs_evidence|budget_exhausted|validator_error`), confidence, chapters.
- `dropped_relationships`: same shape + reason.
- `discovery`: nominated / judged / accepted / rejected / allowance.
- `tropes`, `triggers`, `quotes`, `povs`, spice, chapter summaries.
- `decision_ledger`: every judge call, budget draws, circuit breaker.
- `coverage`: chapters, extraction failures, evidence coverage.

Rules:
- Within-book roster reconcile and merge run here (D2).
- Discovery runs here (D6).
- No DB writes. Re-running is cheap: extraction calls are not repeated;
  decision calls are live unless stage outputs are stamped and reusable.
- Front/back matter contributes nothing, as today.

### 3.3 `--stage merge`

Input: one or more analysis artefacts, plus read-only DB state.

Output: `plan.json` (default) and, with `--apply`, `receipt.json`.

Phases:
1. **Work resolution** — ISBN → edition → work; title/author; report
   duplicate work candidates; never delete.
2. **Within-work safety check** — flag remaining false splits (two rows for
   one person) and alias collisions. These are suggestions; `analyze` should
   already have canonicalized them.
3. **Cross-work identity** —
   - auto-confirm: same author + same series + identical normalized name;
   - suggest: alias/diminutive/priors matches, with evidence;
   - never auto-link across authors or series;
   - canonical duplicate rows produce `merged_into` suggestions only.
4. **Publish** — upsert characters, edges, links, claims, quotes, works
   series fields. Preserve `confirmed`/`rejected` curation exactly as
   `write_claims` does today.

CLI:

```
py process-portable.py --stage extract --file book.epub
py process-portable.py --stage analyze --extraction <dir> --preview
py process-portable.py --stage merge --analysis <dir> [<dir> ...] --apply
py process-portable.py --stage merge --all --series "Fourth Wing" --apply
```

`--dry-run` is the default posture for `merge`; `--apply` is explicit.
`extract` and `analyze` are never write-mode.

Existing entries: `--stage decide` is renamed `--stage analyze`, and the
current per-book `--stage merge` (within-book roster merge) moves inside
`analyze`. Both old forms refuse with a message naming the new entry rather
than silently changing meaning.

### 3.4 Shared rules

- Fingerprint hard stops: analyze requires the extraction stamp and flags
  hash; merge requires every input analysis to name the same extraction it
  was built from. A mismatch refuses by name, never auto-runs upstream.
- Write guard: a stage context flag makes `sb()` raise on any non-GET
  method outside `merge`.
- `--source-run <key>` stamps every row; `pipeline_key` makes upserts
  idempotent.
- A failed section restores what it deleted, per `write_claims`.

## 4. Database changes

All app-side changes are additive, re-runnable, and backfill-free, matching
the house migration posture. The column and index list below is the migration
contract; the SQL file lands with the app-side work in
`cozylibram/supabase/migrations/`.

### 4.1 `book_characters`

Add:
- `name_norm text` — app-computed normalized key for dedupe and display joins.
- `last_appearance_chapter integer`.
- `pipeline_key text` — stable per work + roster entity.
- `source_run text`, `analysis_version text`.
- Unique index on `pipeline_key` where not null.
- Index on `(work_id, name_norm)`.

Existing `role`, `description`, `appearance`, `aliases`, `vitality`,
`first_appearance_chapter`, `status`, `tier`, `tier_source`, `character_id`,
`suggested_character_id`, `duplicate_of`, `merge_snapshot` are unchanged.

Status vocabulary stays `candidate|confirmed|rejected|merged`; the processor
writes `candidate` only. Curation is the app's act.

### 4.2 `character_relationships`

Add:
- `evidence text`, `evidence_source text`.
- `validation_state text`, `claimed_type text`.
- `confidence numeric`.
- `pipeline_key text`, `source_run text`.
- Unique index on `pipeline_key`.
- Index on `review_status`.

The existing unique `(character_a_id, character_b_id, relationship_type,
direction)` is the idempotent edge key. The processor maps:
- symmetric types → `direction='mutual'`;
- directed types → `a_to_b` when `a` is the claimed role of `b`.

Never update a `confirmed` or `rejected` row's status. Pending rows may be
refreshed with new evidence. A human-rejected edge is never resurrected by
a later run: the upsert skips it and records the skip in the receipt.

### 4.3 `character_links` and `characters`

`character_links` add:
- `status text default 'confirmed'` (`suggested|confirmed|rejected`).
- `link_source text` (`human|auto-series|pipeline`).
- `confidence numeric`, `evidence text`, `source_run text`.
- Unique index on `(book_character_id, character_id)`.

`characters` add:
- `merged_into uuid references characters(id)`.
- Index on `(name_norm)`.

The app must filter `character_links.status='confirmed'` for the wiki and
surface `suggested` rows in Character Lab. `merged_into` is set only by the
app's merge action, never by the processor.

### 4.4 `works`

Add:
- `series_key text` — normalized author + series.
- `series_position numeric`.
- Index `(series_key, series_position)`.

`edition_works.position` stays what it is (position inside an omnibus).

### 4.5 Claims and quotes

`book_trope_claims`, `book_trigger_claims`: add `validation_state text`,
`pipeline_key text`, `source_run text`.

`book_quotes`: add `chapter_index integer`, `chapter_label text`,
`spoiler boolean default false`, `verified boolean default false`,
`pipeline_key text`, `source_run text`.

Trigger severity and prominence already ride inside `evidence`; no column is
added for them in this slice.

### 4.6 `book_events` (Phase 4)

```
book_events(
  id uuid pk,
  work_id uuid references works(id) on delete cascade,
  chapter_index int not null,
  event_index int not null,
  kind text not null,          -- plot | relationship | status | reveal
  summary text not null,
  participants text[] not null default '{}',
  evidence text,
  spoiler_through_chapter int,
  source_run text,
  created_at timestamptz default now(),
  unique (work_id, chapter_index, event_index)
)
```

The processor does not write this table until the event phase ships.

### 4.7 Security

- New columns inherit table RLS.
- `book_events` needs the same policies as `book_characters`: authenticated
  read, admin write.
- **App-side hardening:** `character_relationships` currently allows any
  authenticated user to insert/update/delete (`v377`). That is not a wiki
  review policy. The app migration should restrict writes to `is_admin()`;
  the processor uses the service role and is unaffected.

## 5. Quality changes

### 5.1 Relationship recall

- Promote discovery into `analyze` (D6). Nomination is deterministic:
  roster pairs that co-occur in at least one chapter and are not already
  claims; cap per book, reported in `analysis.json`.
- Evidence-less survivors ship (D5). `_build_rel_map` no longer drops them;
  it writes an edge with `type='other'`, `claimed_type`, and
  `validation_state='needs_evidence'`.
- `--principals-only` no longer removes edges whose endpoints are minors.
  The full cast ships; the app filters display.
- Family claims keep the binding-evidence bar for the family type. A
  parent/sibling claim that fails it ships as `other` + `claimed_type`, never
  as a family edge and never silently vanishes.

### 5.2 Identity

- `--persist-roster` becomes the default for series books; roster entity IDs
  are the `pipeline_key` basis.
- Confirmed aliases and links are deterministic Tier 2 evidence for later
  books; ambiguous forms remain poisoned, as series priors already do.
- A false merge remains the worst error. No auto-link across authors or
  series; no auto-merge of canonical `characters` rows.
- Every unresolved mention becomes an entry in the analysis artefact and,
  with `--emit-uncertainty`, a Desk item.

### 5.3 Series and progress

- `analyze` records per-book role, vitality, first/last appearance.
- `merge` writes `works.series_key` and `series_position`.
- The app derives progress by joining a canonical character's linked
  book rows in series order: role changes, vitality changes, appearances.
- Cross-book relationships are not a new table: the app unions confirmed
  per-work edges across a character's linked rows. Series-level claims stay
  traceable to the book that evidenced them.

### 5.4 Recaps (later phase)

- `analyze --events` extracts chapter events with evidence and
  `spoiler_through_chapter`.
- A recap command (processor or app) selects events in series order up to
  the reader's last-read position and composes prose.
- Fail closed: no events for a book means no recap, never a summary
  substitute.

### 5.5 Tropes and triggers

- Add `validation_state` to both claim tables so the app can badge them.
- Trope claims keep the existing candidate/confirmed/rejected lifecycle.
- Trigger rows keep severity, prominence, frequency, and quotes inside
  `evidence`; the app's taxonomy remains the display vocabulary.

## 6. App contract (for the later `cozylibram` work)

Required reads and filters:
1. Cast: `book_characters` where `status != 'merged'`; group by `role`;
   show `tier` and provenance.
2. Relationship inbox: pending `character_relationships`; show `evidence`,
   `evidence_source`, `validation_state`, `claimed_type`, confidence.
   Use `relationship_types` for the type dropdown, not a hardcoded list.
3. Wiki graph: JSONB relationships overlaid by `review_status='confirmed'`
   edges (current behavior), now with evidence available.
4. Character pages: `character_links.status='confirmed'` only; show
   suggestions separately.
5. Series pages: group works by `series_key`, order by `series_position`;
   character instances in that order.
6. Badges: `validation_state` maps to pill text:
   `needs_evidence` → "needs evidence";
   `review_required` → "review required";
   `budget_exhausted` → "not judged";
   `validator_error` → "judge error";
   `mined` → "mined evidence".
7. Type vocabulary: read `relationship_types`; the current dropdown omits
   step-parent, step-child, mate, bonded, handler, and others. A claim of
   `step_parent` currently renders with `spouse` selected, and accepting it
   would write the wrong type.
8. Quotes: use `chapter_index`, `spoiler`, and `verified` once emitted; do
   not display unverified quotes.

No app feature is required for the processor slices to ship. The processor
writes fields the app ignores until the corresponding UI lands.

## 7. Acceptance criteria

Measured on Daemon and Fourth Wing, with gold sets built before the run
(cast + aliases, all relationships with types, triggers, tropes).

| # | Criterion | Target |
|---|---|---|
| A1 | Extraction and analysis write nothing | zero non-GET requests from `extract`/`analyze` |
| A2 | Merge is the only writer | every written row carries `source_run`; no row from another path |
| A3 | Relationship recall | Fourth Wing true-pair survival ≥ 4/5 (baseline 2/5) |
| A4 | Relationship precision | false pairs ≤ 1/13; no false family type survives |
| A5 | Evidence coverage | 100% of shipped edges carry `evidence` or an explicit `needs_evidence` state |
| A6 | Full cast | every rostered character with ≥1 chapter ships; roles preserved |
| A7 | Identity | zero false merges; false splits ≤ 1 per book; no cross-author/cross-series auto-links |
| A8 | Series links | exact-name same-author same-series characters linked across 2 books, 0 wrong links |
| A9 | Idempotency | merge run twice → zero row diffs |
| A10 | Curation safety | rejected edges/claims never resurrected; confirmed rows never demoted |
| A11 | Fingerprint stops | mismatched extraction/analysis refuses by name |
| A12 | Test suite | Windows `unittest discover` green before the first measurement |

A1–A4 are gates. A5–A12 are release criteria.

## 8. Tests

- Artefact contract tests: schema, required keys, fingerprint binding,
  refusal on mismatch.
- Write-guard test: a fake `sb()` records methods; `extract`/`analyze` raise
  on POST/PATCH/DELETE.
- Idempotency test: apply a plan twice; assert identical row payloads.
- Preservation tests: rejected/confirmed edges and claims survive re-runs.
- Gold-fixture scorer: extend `preview_variance` usage to full cast and all
  relationship types, not only family pairs.
- Windows hermeticity: close SQLite handles in fixtures, isolate `config.json`
  (the current suite has 47 errors / 15 failures caused by open DB handles,
  real config leakage, and POSIX paths).

## 9. Rollout

1. Migration applied (additive, verified with a service-key round trip).
2. Ship `--stage extract` and the artefact contracts behind the stage flag;
   full runs keep their current path until `analyze` lands.
3. Ship `analyze`; compare its artefacts against full-run previews on the
   two gate books. No DB writes yet.
4. Ship `merge` with `--apply` off by default; review plans for three books.
5. Enable edges and links per book; watch the inbox and the graph.
6. Enable full cast in the app profile once the UI groups by role.
7. Event/recap phase starts after A1–A11 pass.

Rollback: flags off; delete rows by `source_run`; migrations are additive and
leave existing rows untouched. The legacy full run remains available as the
fallback until the three-run path passes the gates.

## 10. Risks

- **False merges from series priors.** Mitigation: hints only; auto-link
  limited to the exact-name rule; all other links suggested.
- **Volume.** Full cast may triple `book_characters`. Mitigation: `tier`,
  role grouping, and the app's existing filters.
- **Evidence-less edges flooding review.** Mitigation: `needs_evidence`
  state and ranking in the inbox; not a write blocker.
- **Two identity memories.** Mitigation: D3 makes the app tables
  authoritative; the registry is read-only input to analyze.
- **Type drift.** Mitigation: read `relationship_types` in both repos; the
  processor already derives its vocabulary from the same registry.
- **Recap spoilers.** Mitigation: events carry `spoiler_through_chapter`;
  composition fails closed.

## 11. Non-goals (this slice)

- No new series entity table; `works.series_key`/`series_position` only.
- No automatic canonical-character merge.
- No app UI changes here; the contract above is the handoff.
- No change to trope/trigger extraction prompts beyond the state fields.
- No backfill of historical rows in the migration; a separate
  `--stage merge --backfill` command may populate edges from stored JSONB.

## 12. Build status

Slice 1 is implemented on branch `wiki-ingestion-slice1`:

- `--stage extract` runs Stage E and writes
  `<learn>/artefacts/extract/<fingerprint>/<extract_key>/`
  (`manifest.json`, `meta.json`, `roster.json`, `chapter_<idx>.json`).
- `--stage analyze --file BOOK --extraction DIR` consumes that
  artefact through the checkpoint loader, runs the existing decide
  pipeline, and writes `<learn>/artefacts/analyze/.../analysis.json`.
  It is preview-shaped and never writes app data.
- `_WRITE_GUARD` makes `sb()` refuse non-GET app-schema calls during
  extract/analyze; processor-schema bookkeeping stays allowed.
- `--stage merge --analysis DIR [DIR ...]` writes a cross-book
  `plan.json` (characters, edges, links). `--apply` refuses until the
  app migration is present.
- `--auto-confirm-links` is opt-in; `wiki_links_auto_confirm`
  defaults false, so every proposed link lands `suggested` and the
  exact-name rule can be measured before it is trusted.
- `--stage decide` refuses with the rename message.

Not yet in this slice: the app migration, merge apply, discovery
promotion, full-cast writes, series-position writes, and events/recaps.
