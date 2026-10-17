# Processor Run History in Supabase — Spec (DRAFT for approval)

Status: draft, 2026-10-17. Nothing built, nothing migrated. A schema change is a
gated decision; this spec is the thing being approved, not the build.

## 1. Purpose

Give every processor run a permanent, queryable record in the Cozy Supabase
project, so questions about runs ("what did the watch do this week", "did the
judge fix change abstention rates", "which run produced the claims now live for
this book") are answered by query instead of by re-running drills and pasting
terminal output.

Non-goals: changing extraction behaviour; replacing the local run-events
JSONL (it stays the ground truth — this is a mirror); storing any book
content; any Cozy app/UI surface (the app never reads these tables).

## 2. Principles

- **Metadata only.** Counts, verdicts, fingerprints, versions, outcomes.
  Never text (see the denylist, §5).
- **Unknown is NULL, never 0.** A count the processor did not measure is
  stored as NULL. 0 means "measured, and it was zero".
- **Best-effort writes.** A failed history write warns once and never fails,
  slows, or alters a run (the processor's standing degradation rule).
- **Variables ride with results.** Every run row stamps the processor
  version, models, prompt variants, thresholds and flags in force, so any
  two runs are comparable without guessing what produced them.
- **Additive only.** New tables, no changes to existing tables or data.

## 3. Architecture

- The writer lives in `process-portable.py`, behind a flag (§7), and reuses
  the existing Supabase REST helper and the `supabase_url` / `supabase_key`
  config the processor already uses for claim writes. No new credentials,
  no new dependencies.
- The run's identity is the existing run-events `run_id`
  (`run-<YYYYMMDD-HHMMSS>-<pid>`), so a history row, its JSONL artifact and
  its portal Live Run view all name the same run.
- Payload assembly happens at run end (`_run_events_finish` hook point):
  one upsert for the run row, one batched upsert for its book rows.
  Interrupted runs are written with `status='interrupted'` via the same
  finish path that already records `interrupted` in the JSONL.
- The JSONL currently names books by filename; the history writer does NOT
  transfer filenames. Book identity is title + corpus fingerprint /
  source hash + resolved Supabase `work_id` when the run resolved one.

## 4. Schema

### processor_runs — one row per run

| Column | Type | Notes |
|---|---|---|
| id | uuid PK, default gen_random_uuid() | |
| run_key | text UNIQUE NOT NULL | the run-events run_id |
| started_at / finished_at | timestamptz | finished NULL while running |
| duration_ms | int | NULL if not measured |
| status | text | running / completed / interrupted / failed |
| processor_version | text | PORTABLE_VERSION at run start |
| machine_label | text NULL | optional config label (e.g. "LAZYDOG"); NULL unless the user sets one — no hostname is ever auto-collected |
| mode | text | scan / watch_cycle / ingest / replay |
| preview / dry_run | bool | |
| flags | jsonb | allowlisted booleans only (§5): use_corpus, corpus_only, evidence_retrieval, roster_reconcile, watch, watch_ingest, emit_run_events, rename_no_isbn, decision_model … |
| models | jsonb | decision-model provider+model, llm backend+model, judge identity (backend+model as resolved by the effective-identity rule), embedder generation string |
| prompt_variants | jsonb | per-stage variant name + short content hash for each prompt in force (extractor, judge, merge/identity, diminutive…) — computed from the live prompt constants, so an unlabelled prompt edit still changes the hash |
| thresholds | jsonb | the numeric bars in force (merge threshold, rel validation/drop, claim acceptance…) |
| config_fingerprint | text | hash of the allowlisted effective config; detects "same version, different setup" |
| corpus_generation | text NULL | embedder generation stamp of the corpus used, when one was |
| totals | jsonb | files_seen, admitted, skipped_not_in_corpus, processed, succeeded, failed; claims derived/reused/abstained/failed summed over books; quotes verified/rejected |
| error_summary | text NULL | first failure's class + message, truncated to 500 chars, sanitized (§5) |

### processor_run_books — one row per book per run

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| run_id | uuid FK → processor_runs.id ON DELETE CASCADE | |
| book_key | text NOT NULL | corpus fingerprint when corpus-known, else the file's source-hash; UNIQUE with run_id (upsert target) |
| title | text | as resolved by the run |
| work_id | uuid NULL | the Cozy `works` row this book resolved to, when it did — this is what makes run↔data joins possible |
| source | text | corpus / file |
| outcome | text | processed / skipped / failed |
| skip_reason | text NULL | not_in_corpus (extensible) |
| chapters | int NULL | |
| duration_ms | int NULL | |
| decision_calls | int NULL | decision-model calls used (already counted by run events) |
| claims | jsonb | per axis (tropes / triggers): derived, reused, abstained, failed |
| characters | jsonb | roster size, merges proposed/applied, drops — counts only |
| quotes | jsonb | verified, rejected, selected counts |
| replace_result | jsonb NULL | write runs only: would_delete / deleted, inserted, preserved_rejected, preserved_confirmed |
| error_class / error_message | text NULL | message truncated + sanitized (§5) |

Indexes: processor_runs(started_at), processor_runs(processor_version),
processor_run_books(work_id), processor_run_books(book_key),
processor_run_books(run_id).

### processor_run_claim_outcomes — one row per claim per run (IN v1 per RH-3)

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| run_book_id | uuid FK → processor_run_books.id ON DELETE CASCADE | internal to the history store, like run_id above |
| axis | text | tropes / triggers (CHECK) |
| claim_key | text NOT NULL | hash of the claim's normalized identity (work + axis + catalogue identity) — stable across runs, carries no text; UNIQUE with run_book_id + axis. INPUT DISCIPLINE (required, Advisor team-review round 2): the identity hashed is catalogue identity plus the book fingerprint ONLY — never a raw free-text or proposed label; a not-yet-catalogued claim is keyed salted with the book fingerprint, so no bare label is ever globally dictionary-matchable. The writer's suite asserts this input rule. |
| verdict | text | the run's outcome for this claim (derived / reused / abstained / failed, and the retrieve verdict vocabulary where applicable). Deliberately NO database CHECK (Advisor ruling): verdict is an evolving measurement vocabulary, and under best-effort writes a CHECK violation would silently drop rows; the writer's suite pins emitted values against the code-side vocabulary allowlist instead. |
| confidence | numeric NULL | the stage's RAW numeric score, stored unbucketed — a deliberate change from the original RH-3 sketch (which said "confidence bucket"): raw scores are the processor's own telemetry and are more useful for abstention analysis. |
| uploaded_at | timestamptz default now() | |

Its acceptance questions (named by Trope Review, team review 2026-10-17):
(a) which claims churned between two runs of the same book (set membership
over claim keys — counts cannot distinguish stability from turnover);
(b) which claims abstain repeatedly (3+ consecutive runs), reported
directly instead of reconstructed.

## 5. Transfer list

**Transferred:** everything in §4 — timestamps, version, flags, model names,
prompt hashes, thresholds, counts, verdicts, outcomes, fingerprints,
resolved work_id, truncated error class/message.

**Never transferred:**
- Book text, passages, quotes, evidence strings — in any field, at any size.
- File names and file paths.
- API keys, tokens, or raw config values (config appears only as the §4
  fingerprint + allowlisted flag booleans).
- Endpoint or base URLs of any backend (a local model's LAN address is a
  hostname by another name). Model *names* only, anywhere in the payload
  or the fingerprint input. (Added by the Security Advisor, team review
  2026-10-17.)
- Anything about Cozy users (accounts, shelves, reading state).
- Hostnames or machine identifiers (machine_label is opt-in and user-chosen).

Error messages are the one free-text field: truncated to 500 chars and
passed through a sanitizer that strips anything path-shaped or key-shaped
before upload. The payload builder is unit-tested against this denylist
(a test scans a built payload for path/key patterns and fails the suite).

## 6. Security

- Both tables: RLS ENABLED, no policies for `anon` or `authenticated`, and
  explicit REVOKE of all grants from those roles. The Cozy app's keys cannot
  read or write these tables at all.
- Writes/reads happen only via the service key the processor already holds,
  and via the admin SQL path used for Luna's queries.
- Verification before the flag is ever enabled live (standing preamble):
  one marker query proving the connection + table fingerprint, plus an
  RLS-faithful check that an authenticated-role query sees nothing.

## 7. Switches

- `--run-history` CLI flag and `run_history` config key; **default OFF** in
  v1. Flag off = no payload built, no network call, behaviour identical to
  today (pinned by test).
- Enabling requires supabase_url/supabase_key to be configured; if they are
  missing, the flag warns once and the run proceeds unmirrored.

## 8. Write semantics

- Upserts only: runs on `run_key`, books on `(run_id, book_key)` — a
  retried or duplicated send is harmless.
- One batch at run end (books in a single request). No per-event streaming
  in v1; the JSONL already covers live detail and the portal reads it.
- A history POST that fails is logged as one warning line
  (`Run history: upload failed (<class>); run unaffected`) and dropped.
  A `--run-history-backfill <jsonl>` re-send tool is phase 1.1, named here
  so the failure mode has a designed remedy.

## 9. Questions this must answer (acceptance-shaped)

1. Last 10 runs: when, version, mode, totals, status.
2. One book's full history: every run that touched it, source, outcome,
   claim counts per run.
3. Before/after a version or prompt change on the benchmark books
   (Daemon, Fourth Wing, Freedom): abstentions, reuse, derived, decision
   calls, duration, side by side.
4. Watch audit for a date range: cycles, per-cycle totals, any cycle that
   admitted 0, any book failing 3+ runs running.
5. Run↔data join: for a write run, replace_result vs the live claim rows
   for its work_ids (deleted/inserted/preserved reconcile).
6. Prompt-variant comparison: same book, variant A vs B rows, outcomes
   side by side — the JEV evaluation pattern, generalized.

## 10. Acceptance criteria for the build

- Migration SQL is additive-only, reviewed by the Security Advisor before
  it is applied; applied only on the user's explicit go.
- Suite pins: payload builder emits only allowlisted keys (denylist scan);
  flag-off = zero network calls; POST failure leaves run result and exit
  behaviour unchanged; upsert payloads carry the §4 stamps (version,
  prompt hashes, judge identity); interrupted run writes status correctly;
  claim_key inputs never include a raw free-text/proposed label (catalogue
  identity + book fingerprint only); emitted verdict values are pinned
  against the processor's code-side verdict vocabulary allowlist.
- First live enable is verified by the §6 marker + RLS checks and one
  real run whose rows are read back and reconciled against its JSONL.

## 11. Rollout

1. Spec approved (with RH decisions below).
2. Migration drafted → Advisor review → user applies/approves apply.
3. Processor writer built behind the flag (Builder → Advisor gate →
   version bump, the standing lane).
4. User enables on one machine; first-run readback verified; then decide
   whether watch runs default to history-on.

## 12. Open decisions (user rulings)

- **RH-1 Retention: RULED by the user 2026-10-17** — keep forever, with
  the optional drop retained: the prune/drop statements stay in the
  migration draft as the named exit, to be used only on a future
  explicit ruling. (Team-consensus position, all five seats.)
- **RH-2 Machine label: RULED by the user 2026-10-17** — set the label;
  the user's chosen text is `Lazydog` (his casing), to be set via the
  `machine_label` config key on his machine. Never auto-collected.
- **RH-3 Claim-level rows: RULED by the user 2026-10-17** — include
  `processor_run_claim_outcomes` in v1 ("accept the larger build"),
  against the team recommendation of aggregates-only. The churn and
  repeat-abstainer questions (§4) are therefore answerable from the
  first mirrored run.
- **RH-4 Table home:** `public` with RLS + revoke (simpler; the app's
  PostgREST schema) vs a dedicated `processor` schema kept out of the
  exposed API schemas (cleaner isolation; slightly more setup).
  *Recommendation: dedicated schema if the Advisor confirms the REST
  helper can target it cleanly; otherwise public + revoke.*

## 13. Team review (2026-10-17) — verdicts and amendments

All five seats consulted; **all agree** (four with changes). Amendments
below are adopted into the spec unless they conflict with a user ruling.

- **Trope Review Agent — AGREE WITH CHANGES.** Add to `processor_run_books`:
  `suppressed_by_verdict` (verdict read-back suppression count — the
  regeneration metric); evidence-tier counts (Tier 1/2/3) per axis in
  `claims`; `promoted` count per axis (persistence promotions vs fresh
  derivations). `book_key` is the canonical longitudinal join; `work_id`
  is documented as a convenience snapshot of one run's resolution.
  Silent-gap risk: best-effort drops bias before/after analyses — the
  phase-1.1 backfill must not slip, or a JSONL-vs-mirror reconciliation
  read joins the first-live verification.
- **Security Advisor — AGREE WITH CHANGES.** RH-4 ruled for the dedicated
  `processor` schema, preconditioned on an end-to-end proof (schema
  exposed, profile headers, explicit service_role grant, write/read round
  trip); public + revoke is the accepted fallback if the proof is fiddly.
  Required before production: four-part verification (grants enumerated
  from information_schema for anon/authenticated = none; anon-key
  PostgREST probe sees nothing; RLS-faithful authenticated check; positive
  service-key round trip); error sanitizer hardened with adversarial
  fixtures (Windows paths, URLs with query strings, email-shaped strings,
  bare filenames) covering both error fields; the payload test also
  asserts the config fingerprint's INPUT carries no key/token-named
  fields. The phase-1.1 backfill must route through the same payload
  builder (the JSONL contains filenames). Recommended: name the RH-1
  exit (prune/drop statements) in the spec now.
- **Data & Sync Engineer — AGREE WITH CHANGES.** `work_id`: soft reference,
  NO foreign key (verified live: every works-referencing table in this
  project is ON DELETE CASCADE — an FK would let a works clear destroy
  the audit of itself; SET NULL only if an FK is insisted on, plain is
  preferred). The `run_id → processor_runs` CASCADE is correct (internal
  to the history store). Add CHECK constraints on the closed vocabularies
  (status/mode/source/outcome) and `uploaded_at timestamptz default now()`
  to both tables (distinguishes live rows from backfilled rows).
  **run_key collision guard:** run-<timestamp>-<pid> can repeat (same
  second + pid reuse; multi-machine under RH-2); the writer must never
  overwrite a row whose started_at differs — suffix the key and warn
  loudly. Book rows are written only after the run upsert returns its
  uuid. The §10 pin test must assert the INNER key sets of the jsonb
  counters and emit explicit NULLs for unmeasured counters (a missing key
  is indistinguishable from unknown). Drop the standalone
  `processor_run_books(run_id)` index — the UNIQUE (run_id, book_key)
  index already serves it. Named v1 simplification: a book touched twice
  in one run collapses to its last outcome. Migration integrity: CREATE +
  RLS ENABLE + REVOKE in the SAME migration (no default-grant window);
  rollback section gains drop order (books first) and the note that
  post-enable rollback destroys the mirror — a data decision, not an
  undo. RH-4 position: public + revoke as primary (see §14).
- **CozyLibram Builder — AGREE WITH CHANGES.** Grants check explicit
  (converges with the Advisor's four-part verification). work_id: plain
  uuid, no FK by design, stated in the migration; symmetric rule — no app
  table FKs into processor_* either. Migration custody: the SQL lives in
  the processor repo, applies via the admin path, is never numbered into
  the app's vNNN sequence, and is logged in both places. Coexistence
  contract: all processor-owned objects carry the `processor_` prefix;
  corpus clear/rebuild table sets never include processor_* without its
  own ruling. RH-4 position: public + revoke (see §14).
- **Product Strategist — AGREE.** Aggregates-first is correct; the named
  phase-2 candidate is per-claim churn on shipped claims. Bonuses the spec
  under-plays: `decision_calls` makes this a cost ledger as well as a
  quality record. Sequencing: land it BEFORE the next prompt-variant or
  judge evaluation (comparisons need a "before" picture); at trial
  enable, turn it on for watch runs early — the watch audit is a top
  use and longitudinal data accrues there on its own.

## 14. RH-4 — RULED by the user 2026-10-17

**Ruling: dedicated `processor` schema** (the Advisor's position), with
public + revoke as the accepted fallback if the end-to-end proof
(schema exposure, profile headers, service_role grant, round trip)
fails or proves fiddly — no re-escalation needed in that case.
The arguments that were put to the user:

- **Advisor: dedicated `processor` schema** (with proof precondition;
  public is the accepted fallback). Reason: a fresh schema inherits none
  of Supabase's public-schema default grants, and this project runs many
  agent-drafted migrations in public — one careless GRANT there could
  sweep these tables in.
- **Builder + Data & Sync: public + RLS + revoke as primary.** Reason:
  PostgREST can only serve exposed schemas, so the dedicated schema must
  be exposed anyway, after which the operative control is the same
  grants + RLS discipline; public matches the project's shipped posture
  (e.g. gift-claims).
- **Common ground (all three):** the operative control is identical in
  both homes — service-role-only access, verified from the database's own
  grant tables plus a PostgREST probe and a positive round trip, never
  trusted from migration text.
