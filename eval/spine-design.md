# The measurement spine — deterministic replay, review fixture, SQLite substrate, verdict read-back: design

Status: DESIGN ONLY (no code). User approved the direction
2026-10-15: *"Sequence Tier 1 first (replay standardization makes
everything else measurable), with the SQLite substrate designed
alongside the replay harness — one project."* This document is that
project's design. It has not yet had its Security Advisor red-team;
open choices (§7) are the User's to rule.

Doctrine, stated once and binding on everything below: **the spine
is measurement and state infrastructure; it decides nothing about
any claim.** Replay freezes draws so pipeline changes can be
attributed; the fixture scores releases against human verdicts it
does not create; the substrate stores exactly what the three
existing stores store, in a queryable form; read-back carries human
verdicts *into* the processor and never writes a verdict back.
Precision-over-volume is untouched: no part of this design ships,
promotes, merges, or drops a claim except §4's suppression, which
drops only claims a human has already rejected for that work —
the conservative direction, by construction.

One scoping rule, explicit: **decision D1 (learned-state
governance) is NOT reopened or pre-decided anywhere below.** The
corrections ledger is a later consumer of this substrate; §3.6
designs the seams it would need and nothing more. Where a design
point touches D1's subject matter (what a correction is, who may
issue one, retention), it is flagged and left open.

---

## 0. What the code actually looks like today (evidence base)

Three of the commissioning brief's assumptions do not survive
contact with the repo. They are corrected here, because the design
changes shape around the corrections; §0.4 collects them.

### 0.1 Replay: the pieces exist; the instrument does not

- **Checkpointing already saves the raw extraction.**
  `_checkpoint_save_chapter` (process-portable.py:12282) persists
  per-chapter Call A + Call B payloads under
  `preview/.checkpoints/<book_key>/`, plus a roster snapshot
  (`_checkpoint_save_roster`, :12290) and meta
  (`_checkpoint_save_meta`, :12228) carrying a flags hash, the
  book fingerprint (fix-queue batch 1), chapter count, and the
  writing PORTABLE_VERSION. `_checkpoint_load` (:12297) returns
  them, and a resumed run re-derives everything downstream of
  extraction from those payloads without re-calling the extraction
  model.
- **But resume is a correctness path, not a measurement path.**
  `_checkpoint_resume_check` (:12339) exists to protect a book
  from stale or foreign state: a fingerprint mismatch hard-refuses
  (batch 1), and a flags-hash mismatch warns and, in a
  non-interactive run, **refuses and clears the checkpoints**.
  The flags hash (`_checkpoint_flags_hash`, :12106) covers output-
  affecting CONFIG keys *and the current git commit SHA* — so any
  code change invalidates every checkpoint for unattended resume.
  That is correct behaviour for resume and fatal for replay: the
  entire point of replay is running *changed* code over a *fixed*
  draw. Reusing `--resume` as the replay instrument would either
  refuse or, on the interactive path, train operators to override
  the guard that batch 1 just built.
- **G already assumes the instrument.** The G design (§10,
  amendment G4) measures invariance "over a replayed extraction —
  the same saved extraction run flag-off vs flag-on — not over two
  fresh extractions." Nothing in the repo today can produce that
  run on demand: no snapshot registry, no frozen-draw store, no
  replay mode, no record of which draw a preview came from.
- **The dashboard compares exactly two preview files.**
  `preview_variance.py OLD NEW` prints the frozen legacy Jaccard
  report plus the Quality dashboard block (eval/README.md). It has
  no notion of *why* two previews differ — pipeline change and
  extraction draw are indistinguishable in its output. Both the F
  gate and the B gate paid for this: gate effort went to
  attribution archaeology (which differences are the change, which
  are the draw?) instead of to the change itself.

### 0.2 The three stores: one pattern, five-plus files, no queries

All processing state lives under `~/.ebook-processor/learned/`
(overridable via `--learn-dir`), under one persistence discipline
(sweep F8): atomic temp-file + `os.replace` save, loud corruption
warning, original bytes preserved as `.corrupt` backups, in-memory
dict models guarded by an RLock. The inventory is larger than
"three stores":

- **LearnedState** (:2603) — four files: `nicknames.json`
  (nkey → {full, count, books, human, manual}), `titles.json`,
  `threshold_stats.json` (per-trigger kept/dropped samples, capped
  500/bucket), `rejected_tropes.json` (trej_key → {count, books,
  reasons, human}) — **plus** `series/*.json`, per-author series
  roster/hint files (`_series_path`, :3041).
- **StagingState** (:3307) — one file, `staging.json`:
  {version, works: {work_key → {work_id, title, runs_completed,
  claims, expired_recent}}}. Invariants per the F design: ordinal
  sightings, union folding, accounting identity
  (`staged_ever = promoted + still_staged + expired`, UNACCOUNTED
  must be 0), corruption is delay-only.
- **RosterStore** (:4261) — one file per work under `rosters/`
  (filename = sanitized work_key + hash suffix, :4247):
  {version, work_key, work_id, title, author, runs_completed,
  last_run_fingerprint, next_entity_seq, minted_total, entities,
  retired, no_pairs, adoptions, …}. Invariants per the B design:
  the 300-step randomized accounting pin, `claim_run` fingerprint
  equality (no double-count on re-completion), anchors, NO pairs,
  tombstones.
- **The cost of the shape:** every cross-cutting question the
  program has asked this month — partition agreement between two
  runs (owed for B's re-score), churn decomposition, sighting
  histories, "which works hold state at all" — is answered by
  loading some or all of these files and joining them by hand, or
  not answered. Nothing can be queried across works.

### 0.3 Verdicts: the processor already reads — partially, and never per-work

The brief's premise that the processor "currently only writes" to
Supabase is wrong. Existing read paths, all purpose-built:

- **`LearnedState.import_rejections_from_supabase`** (:2989) reads
  `book_trope_claims?status=eq.rejected` (global, limit 2000) plus
  the trope catalog, and feeds `record_trope_rejection`. Returns
  −1 when Supabase is unconfigured/unreachable; callers proceed
  without it. This *is* verdict read-back — for one claim class,
  one verdict, at learning granularity.
- **`_apply_trope_rejections`** (:8381) is what that learning does
  at emission: a *global, count-based* filter keyed by trope name —
  3+ rejections anywhere denylist the trope everywhere; 1–2 raise
  its confidence bar by 0.10 each. It is deliberately **not** a
  per-work verdict: a trope rejected once for Daemon raises the bar
  in every other book, and a trope rejected *for this work* still
  ships here if its confidence clears the raised bar.
- **`_partition_claims`** (:10985) reads existing rows at write
  time; preserved (human confirmed/rejected) rows are the curation
  anchor replace-wholesale dedupes against. **`cmd_verify_preview`**
  reads DB rows to check a preview against them, and models the
  failure discipline §4 adopts: `sb()` returns None on error, and
  the code refuses to conclude anything from a failed read ("a
  failed read is not an empty DB"). B's anchor seam reads
  `book_characters` per work (:6248).
- **What does not exist:** a per-work, verdict-exact read at
  emission time. The Trope Review seat's standing exhibit — the
  extractor regenerating claims Kevin already rejected *for that
  work* (Daemon — Enemies to Lovers, rejected and re-proposed the
  same day; Unreliable Narrator + Whodunit, rejected 2026-10-11,
  re-proposed 2026-10-15) — is fully consistent with the code
  above: one rejection moves a global threshold; it does not
  suppress the pair. And nothing counts regenerations: the error
  is invisible in every report the program produces.

### 0.4 The review history: real, smaller than billed, and unjoined

- The Trope Review seat's records are daily files in
  `~/workspace/trope-review/` (2026-10-10 → 2026-10-15, five
  files): per-claim lines `Book — Trope: CONFIRM | REJECT |
  ABSTAIN — rationale`, ~100 lines in total. **Most lines are
  re-reported held verdicts** (14 of 20 on 2026-10-15), and most
  verdicts are ABSTAIN (17 of 20 on 2026-10-15). Unique
  CONFIRM/REJECT claims — the scored-fixture raw material — are
  a few dozen, not ~150. The fixture's n is a build output to be
  reported honestly (G's rule: every rate beside its n), not a
  number this design asserts in advance.
- The seat's verdicts are **recommendations** ("Advisory only —
  no database writes made. Kevin reviews these before any status
  changes"). Kevin-applied verdicts live in Supabase as curated
  row status. **No artefact joins the two**: the seat's lines
  identify claims by book title + trope name (claim UUIDs appear
  only for escalations), titles arrive in the seat's casing
  ("radicalized"), and nothing records which recommendations
  Kevin applied, amended, or ignored.
- The seat's rejection-pattern checklist names the deterministic
  classes the fixture should headline: genre-as-trope mismatch,
  table-of-contents text as evidence, wrong-book quote,
  front/back-matter treated as story content, empty evidence.
  Its escalations name the fine-distinction classes that stall
  on interpretation: voluntary-vs-coerced, backstory-vs-in-book,
  standoff-vs-captor-captive.

---

## 1. Deterministic replay, standardized

**Goal:** any pipeline change can be regression-run against fixed
extraction draws, and the dashboard reports two numbers separately
— **pipeline-delta on fixed draws** and **draw-variance across
fresh draws** — so a change and a lucky draw stop being
indistinguishable.

### 1.1 Definitions

- A **draw** is one fresh extraction of a book: the per-chapter
  Call A/B payloads a run produced. A draw is identified by the
  book fingerprint (`_checkpoint_fingerprint` — ISBN/ASIN +
  normalized title/author + first-substantial-chapter hash) plus a
  per-draw id.
- A **snapshot** is a frozen draw, stored once and never mutated:
  the chapter payloads + roster snapshot + a **manifest**
  (below). Snapshots are the fixed points every gate measures
  against.
- A **replay** is a pipeline run whose extraction phase is served
  entire from a snapshot. Everything downstream of extraction —
  reduce, roster update, validation, JEV calls, trope gate,
  staging observe — runs the *current* code and config. Decision-
  model calls are **not** frozen (§1.5): their variance is part of
  the pipeline being measured.
- A **fresh run** is an ordinary run. The dashboard's second
  number needs at least two fresh draws of the same book.

### 1.2 Snapshot capture and the manifest

Snapshots are captured deliberately, per book, by a named command
(`--snapshot <book>` riding a normal fresh preview run — the run
extracts, and its checkpoint payloads are copied into the snapshot
store before the checkpoint dir's normal lifecycle touches them).
Auto-capture on every run is rejected: a baseline that silently
re-baselines itself is the frozen-draw ageing trap with extra
steps (the Advisor's warning, adopted as a rule: **snapshots
refresh deliberately, per book, never silently** — a refresh is a
registry event with a reason string).

Snapshot store: `<learn_dir>/snapshots/<book_key>/<snapshot_id>/`
(chapter payload files in checkpoint format, copied verbatim) plus
a registry row (§3.5) and a `manifest.json` beside the payloads, so
a snapshot is inspectable and portable without the DB:

```json
{ "snapshot_id": "fw-2026-10-15-a",
  "book_key": "a42e27eb-…", "fingerprint": "…",
  "title": "Fourth Wing", "author": "…",
  "identifiers": {"isbn": "…"},
  "chapter_count": 0, "payload_sha256": "…",
  "created": "2026-10-15T…", "reason": "F/G gate baseline",
  "extraction": {"llm": "…", "model": "…", "pipeline": "v2",
                 "portable_version": "2.9.14", "code_sha": "…",
                 "flags_hash": "…", "config_digest": "…"} }
```

`config_digest` hashes the full output-affecting CONFIG (a
superset of `_CHECKPOINT_FLAGS`, which was chosen for resume
safety, not for measurement completeness). The manifest records
what the draw *was made with*; a replay manifest records what it
was *replayed under*. The pair is the attribution record.

Gate books for v1: **Fourth Wing, Daemon, Freedom™** — the F/G
gate set, for continuity of every pre-registered comparison the
program already has (OC-6).

### 1.3 The replay run

`--replay <snapshot_id>` (preview mode forced — a replay can never
write to Supabase; it is a measurement, and preview ≡ write for
the ship set by F's doctrine, so nothing measurable is lost):

1. Load the snapshot payloads into the extraction seam exactly
   where `_checkpoint_resume_check`'s output is consumed today —
   the same shape (chapters dict + roster), so downstream code
   cannot tell replay from resume. The freshness guard is **not**
   consulted: identity is still enforced (the snapshot's
   fingerprint must match the book file presented, hard-refuse as
   in batch 1), but flags/SHA mismatch is the *purpose* of the
   run, recorded in the replay manifest rather than refused.
2. **State effects are sandboxed.** A replay runs the F/B seams
   (their behaviour on a fixed draw is often exactly what's being
   measured), but against a **throwaway copy of the state
   substrate** (§3): the live stores are opened read-only to seed
   the copy and are never written by a replay. A replay can
   therefore promote nothing, mint no roster run on the live
   store, and record no live staging sighting. The replay's own
   state outcomes are readable from the copy for the duration of
   the report and then discarded. (The alternative — running on
   live stores under the existing non-independent flags — leaves
   every future seam to remember replay is special; the sandbox
   makes "replays don't mutate the world" a property of the
   harness instead of a discipline per seam. §3 makes the copy
   one file copy.)
3. The replay stamps its preview artefact:
   `result["replay"] = {snapshot_id, source_code_sha,
   source_config_digest, replay_code_sha}` — the marker
   discipline of the staging/roster markers (present only when
   the mode is on), which is how the dashboard (§1.4) and the
   registry know this preview's provenance. A replay also records
   its row in `replay_runs` (§3.5).
4. Snapshot immutability is enforced by hash: the replay verifies
   `payload_sha256` before use and refuses on mismatch (a snapshot
   that changed is not a baseline; refusal is loud, and the
   snapshot store is never written by anything except capture and
   deliberate refresh).

### 1.4 The dashboard's two numbers

`preview_variance.py` gains an additions-only **Replay &
variance** section (legacy Jaccard report and existing Quality
dashboard lines stay frozen, per the dashboard's own rule). It
renders when the inputs carry replay markers:

- **Pipeline-delta:** for a snapshot with a baseline preview
  (recorded at capture) and ≥1 replay preview: the existing
  dashboard metric set (labelled family outcomes, roster identity
  indicators, entity/endpoint Jaccards, staged-set section,
  empty-evidence rates) computed baseline-vs-replay. Because the
  draw is identical, **every delta in this section is pipeline**
  (code, config, prompts, or decision-call variance — §1.5).
- **Draw-variance:** for a book with ≥2 fresh previews and no
  replay relationship between them: the same metric set computed
  across the fresh pair(s). This is the noise floor the pipeline-
  delta must clear to be believed, printed beside it, in the same
  units — the section's whole point is that the two numbers are
  never printed apart.
- The section is absent for ordinary preview pairs (no markers),
  and a replay preview compared against a non-snapshot preview is
  refused with a printed reason rather than silently scored as
  draw-variance (provenance confusion is the failure this section
  exists to kill).

### 1.5 What replay does not fix

Decision-model calls (JEV merges, family validation, the trope
gate) run live during a replay, against the live provider. Their
variance — sampling, provider drift — lands in pipeline-delta.
That attribution is defensible (the decision layer *is* pipeline)
but it means pipeline-delta is not pure code-delta. Recording and
replaying decision responses was considered and deferred (OC-5):
the JEV harness (`evaluate-merges.py`, `eval/merge_pairs.json`)
already measures the judge against fixed pairs, and a second,
response-recording harness is a project of its own. The honest
v1 claim is: **replay removes extraction variance — the measured
dominant term in both gates — and names what remains.**

### 1.6 Invariants (tests-to-be)

- `test_replay_never_writes_live_stores` — live substrate file
  hash identical before/after a replay that exercises the F and
  B seams; the throwaway copy shows the seam effects instead.
- `test_replay_refuses_fingerprint_mismatch` — snapshot of book A
  + book file B (same stem) refuses, batch-1 shape.
- `test_snapshot_immutable` — payload hash mismatch on load ⇒
  loud refusal; capture is the only writer (directory listing
  assertion over a replay).
- `test_replay_preview_marker_roundtrip` — the `replay` marker
  survives `save_preview` serialization (generated-JSON rule).
- `test_dashboard_two_numbers` — fixture previews (baseline,
  replay, two fresh) render both sections with correct pairing;
  marker-less pairs render neither; cross-provenance comparison
  refuses.
- `test_replay_forces_preview` — `--replay` + write flags ⇒
  preview semantics; no `write_claims` call (stubbed-sb assertion,
  the G §9.3 pattern).


---

## 2. Review-history eval fixture + active learning

**Goal:** the human verdicts the program has already produced —
the Trope Review seat's recommendations and Kevin's applied
curation — become a versioned fixture scored per release, so
quality claims about shipped claims rest on labelled data instead
of per-incident anecdotes (the seat's own framing), and the
labelling effort that continues goes where the pipeline is
actually uncertain.

### 2.1 Fixture sources and the authority model

The fixture joins the two sources §0.4 found unjoined:

- **Reviewer verdicts** — the seat's daily files
  (`~/workspace/trope-review/*.md`). Parsed per-claim lines:
  book title, trope name, verdict (CONFIRM / REJECT / ABSTAIN),
  rationale, date, evidence tier. Claim UUIDs where the file
  carries them (escalations); otherwise identity is
  (resolved work, catalog trope) via the same resolution
  `_build_trope_rows` performs (work by title/author, trope by
  catalog name/id). Title-casing variants in the seat's files
  ("radicalized") resolve through normalized matching; an
  unresolvable line is reported by the builder, never silently
  dropped or force-matched.
- **Kevin-applied verdicts** — Supabase curated rows via §4's
  read-back: `book_trope_claims` / `book_trigger_claims` rows
  with status confirmed / rejected, joined on (work_id, trope /
  warning). These rows *are* applied verdicts — the strongest
  authority the system has.

Each fixture entry carries `verdict` and `authority`:

| authority | meaning | source |
|---|---|---|
| `kevin` | Kevin applied this status in the DB | §4 read-back |
| `reviewer` | the seat recommended it; no DB status (yet) | seat files |

When both sources name a claim, the Kevin-applied verdict is the
entry's verdict and the reviewer verdict is kept as provenance —
including when they **disagree**, which is recorded, not resolved
away (a reviewer CONFIRM Kevin rejected is exactly the kind of
entry a fixture exists to remember). Authority is never upgraded
silently: a `reviewer` entry becomes `kevin` only when a DB row
with that status exists.

**Dedup rule.** The seat re-reports held verdicts daily. The
fixture holds **one entry per (work, claim)**: the latest seat
verdict, with `appearances` (how many daily files carried it) and
the full verdict history retained in provenance. A seat verdict
that *flips* between files keeps the latest as the verdict and
records the flip — flips are data about claim hardness, not
embarrassments to launder.

### 2.2 What's scored, what's labelled, what's excluded

Per the brief and the doctrine that interpretation is not truth:

- **ABSTAIN entries are excluded from the scored fixture.** An
  abstain is "chapter-refs cannot settle this" — a statement about
  evidence, not about the claim. Scoring the pipeline against
  abstains would grade it against non-verdicts. Abstains are
  counted and reported (they are G's target backlog, §2.4) but
  carry no pass/fail.
- **Fine-distinction classes are included, labelled
  `interpretation-only`, and reported separately** — never in the
  headline rate. These are the escalation stall classes
  (voluntary-vs-coerced sacrifice, backstory-vs-in-book
  mentorship, standoff-vs-captor-captive, thread-vs-central-arc):
  real verdicts, honestly labelled as resting on interpretation
  of the catalog's definitions.
- **Structural classes are the headline.** Verdicts resting on
  the seat's deterministic rejection patterns — genre mismatch,
  TOC-as-evidence, wrong-book quote, front/back-matter as story,
  empty evidence — plus plain premise-level CONFIRMs. A shipped
  claim contradicting a structural REJECT is an error event with
  no interpretive wiggle, which is what a release gate needs.
- Every entry carries its class (`structural` |
  `interpretation-only`), the error-taxonomy mapping from
  eval/README.md (a shipped structural REJECT = `hallucinated`;
  a confirmed claim the pipeline no longer produces = `missing`),
  and its evidence tier at review time (Tier 2 today; G changes
  this, and the fixture should be able to show it changing).

### 2.3 Fixture form and per-release scoring

The fixture is a **versioned JSON file in `eval/`**
(`eval/review_fixture.json`), following the `fw_family_labels.json`
precedent: acceptance signals live inside the file and do not
change when results come in; schema version on the file; built by
a builder script (`build_review_fixture.py`, eval tooling, not the
processor) from the two sources, with the build report (lines
parsed, unresolved, deduped, flipped, per-class/per-authority n)
printed and committed alongside. **Git is the fixture's home**
because a fixture must be reviewable, diffable, and pre-registered
— properties a DB row has none of. The substrate (§3) stores
*scoring runs* against fixture versions, never the fixture itself.

Per-release scoring (`score_review_fixture.py`, or a dashboard
section once previews carry the needed markers — builder's choice,
one scorer only): given a release's gate previews (or write-run
artefacts) for the fixture's books, per claim entry:

- shipped (present in the run's ship set) / staged / dropped /
  absent, using the eval/README scoring rules incl. the preview
  `no_evidence` convention;
- **headline metrics:** structural-REJECT shipped count (target
  0; each one named), structural-CONFIRM retention (shipped ÷
  confirmed, n beside it), regenerated-rejected count (the §4
  counter, same event, two views);
- interpretation-only results printed separately, no target;
- results recorded in `fixture_scores` (§3.5) and published in
  the release scorecard **alongside the churn numbers** — the
  dashboard's per-run view and the scorecard's per-release view
  are the same numbers at two cadences.

### 2.4 Active learning: the uncertainty queue

The fixture grows from history; the queue grows it *toward the
boundary*. At the end of a run (preview or write; flag
`--emit-uncertainty`, default OFF in v1, on for gate runs), the
processor appends candidate labelling items to the substrate's
`uncertainty_queue` (§3.5), drawn from classes where the pipeline
itself recorded disagreement or proximity to a boundary:

1. **Tier-vs-judge disagreements** — the deterministic roster
   tiers' verdict and the JEV score point opposite ways on a
   merge pair (either direction).
2. **Near-threshold pairs** — JEV merge scores within ±0.10 of
   the 0.85 merge bar; family validations landing in the
   0.30–0.70 downgrade band near either edge.
3. **Unresolved suspects** — roster split-suspects still
   unresolved after ≥2 runs of their work (a substrate query —
   the kind §0.2 said was impractical over JSON files).
4. **Form-changed promotions** (F's OC-6 line) — promotions whose
   triggering sighting arrived under a new name form.

**Selection-bias control (binding):** every labelling batch mixes
queue items with a **random sample of the run's decided pairs**
(recommended ratio 4:1 uncertainty:random, OC-4) — a corpus built
only from boundary cases teaches the boundary and nothing else,
and quietly stops representing the easy mass the pipeline mostly
handles. **Batch discipline (binding):** batches are small and
infrequent (cap ~20 items per labelling round, OC-4) — a queue
that becomes homework dies, and a dead queue is worse than none
(Advisor's risk note, adopted).

Each queue item is a **labelling packet**: everything needed to
rule without re-reading the book — the pair/claim, both pipeline
verdicts with scores, chapter co-listing, the evidence the
pipeline held, and provenance (run, book, class). Labels return
through a labels file (the `label-merges.py` precedent) and land
in the right existing fixture: merge pairs → `eval/merge_pairs.json`
(JEV eval), family pairs → the family fixture pattern, trope
claims → §2's fixture at the next build, with the labeller as
authority (`kevin` / `reviewer` per who labelled). Labelled items
leave the queue (status + label recorded; the queue table keeps
the history — it is also the audit trail for what the corpus was
built from).

### 2.5 Invariants (tests-to-be)

- `test_fixture_builder_dedupes_held_verdicts` — a claim in three
  daily files yields one entry, latest verdict, appearances = 3.
- `test_fixture_authority_never_upgrades` — a reviewer CONFIRM
  with no DB row stays `reviewer`; with a contradicting DB row,
  verdict = Kevin's, disagreement recorded.
- `test_fixture_abstains_unscored` — abstain entries absent from
  the scored set, present in the count report.
- `test_uncertainty_mix_ratio` — batch composition honours the
  uncertainty:random ratio and the cap, over a fixture run
  emitting more candidates than the cap.
- `test_labelling_packet_self_contained` — packet fields suffice
  to reconstruct both pipeline verdicts (round-trip assertion on
  the packet schema).

---

## 3. The SQLite substrate

**Goal:** one local SQLite database replaces the JSON persistence
layer under LearnedState, StagingState, and the per-work
RosterStore files — preserving every invariant those stores carry
today — so that processing state becomes queryable across works
and runs (the questions §0.2 listed), copyable in one operation
(the replay sandbox, §1.3), and migratable as one artefact.
Supabase remains the publication/curation layer; **the hot loop
never touches the network** (unchanged), and nothing in this
section moves any data off the user's machine.

### 3.1 The stdlib fact, leaned on

`sqlite3` is Python standard library — no dependency, no install,
identical on the user's Windows PC and this Linux VM, one file,
transactional, with integrity checking built in. The single-file
portable doctrine ("share code as raw .py") is preserved exactly:
the processor stays one file; the *state* becomes one file too
(`<learn_dir>/state.db`, OC-1). This is the only database the
design introduces, and it is local by construction.

### 3.2 Architectural rule: persistence seam, not a rewrite

**The stores keep their in-memory dict models, their locks, and
every line of their decision logic. SQLite replaces only the
persistence layer beneath them** — the `_load_json` / `_save_json`
/ `_load` / `save` seam each store already has. Rationale, stated
plainly: F's and B's invariants live in the in-memory semantics
(ordinal unions, `claim_run` equality, the accounting identity),
and a relational rewrite of store *logic* would re-open every one
of them to a new implementation for zero decision benefit. The
queries the spine needs (§3.4) are served by promoting **keys and
counters to real columns** while nested per-entry records (a
roster entity's observations, a staging claim's sighting list)
ride as JSON text columns — queryable where the spine queries,
byte-faithful where the stores round-trip. The alternative
(full normalization of entity/claim internals) is named and
rejected in OC-2.

### 3.3 Schema

`PRAGMA user_version` carries the substrate schema version;
a `meta` table records store-format versions and the migration
provenance (§3.7). All tables below; `payload` = JSON text of the
entry's remaining record, sufficient with the promoted columns to
reconstruct today's in-memory dict **exactly** (round-trip is a
test, §3.8).

**LearnedState** (mirrors the four files 1:1 in shape):

```sql
learned_nicknames(nkey TEXT PRIMARY KEY, full TEXT, count INTEGER,
                  books TEXT, human INTEGER, manual INTEGER, payload TEXT)
learned_titles(title TEXT PRIMARY KEY, count INTEGER, rests TEXT, payload TEXT)
learned_threshold_stats(trigger TEXT PRIMARY KEY, kept TEXT, dropped TEXT)
learned_rejected_tropes(trej_key TEXT PRIMARY KEY, count INTEGER,
                        books TEXT, reasons TEXT, human INTEGER, payload TEXT)
series_state(series_key TEXT PRIMARY KEY, payload TEXT)
```

`series_state` holds today's `series/*.json` contents as payload
in v1: its only consumer is `load_series_hints` (a key lookup),
and promoting its internals buys no named query. Noted as the
first candidate for promotion if a series-level query is ever
named (the series-roster improvement candidate would name one).

**StagingState** (works and claims promoted — the trapped report
and the accounting identity become queries):

```sql
staging_works(work_key TEXT PRIMARY KEY, work_id TEXT, title TEXT,
              author TEXT, runs_completed INTEGER, staged_ever INTEGER)
staging_claims(work_key TEXT, claim_key TEXT, class TEXT, display TEXT,
               state TEXT, promoted_run INTEGER, promoted_via TEXT,
               best_chapters INTEGER, evidence_ever INTEGER,
               first_seen TEXT, last_seen TEXT,
               sightings TEXT, name_forms TEXT, payload TEXT,
               PRIMARY KEY (work_key, claim_key))
staging_expired(work_key TEXT, claim_key TEXT, expired_run INTEGER,
                sightings TEXT, payload TEXT)
```

**RosterStore** (per-work tables; `work_key` is the partition):

```sql
roster_works(work_key TEXT PRIMARY KEY, work_id TEXT, title TEXT,
             author TEXT, runs_completed INTEGER,
             last_run_fingerprint TEXT, next_entity_seq INTEGER,
             minted_total INTEGER, payload TEXT)
roster_entities(work_key TEXT, entity_id TEXT, status TEXT,
                canonical TEXT, payload TEXT,
                PRIMARY KEY (work_key, entity_id))
roster_retired(work_key TEXT, entity_id TEXT, payload TEXT,
               PRIMARY KEY (work_key, entity_id))
roster_no_pairs(work_key TEXT, a_key TEXT, b_key TEXT, source TEXT,
                payload TEXT, PRIMARY KEY (work_key, a_key, b_key))
roster_adoptions(work_key TEXT, seq INTEGER, payload TEXT)
roster_anchor_retirements(work_key TEXT, entity_id TEXT, run INTEGER,
                          payload TEXT)
```

The entity payload keeps observations with their run ordinals —
which is what makes the **partition-agreement metric** (owed for
B's re-score) computable: the partition at run N is the grouping
of run N's sightings by entity, derivable from payloads today
only by loading every roster file; here it is one pass over one
table, across all works at once.

### 3.4 What the schema must answer (the query contract)

The substrate is justified by these queries, each named by the
part that needs it; a schema that cannot answer one is not done:

1. Trapped report per work + global totals, and the accounting
   identity per work, as SQL counts (§3.3 staging tables). [F's
   report, minus the file-loading.]
2. Partition agreement between two runs of a work; split-suspects
   aged ≥2 runs across all works. [B re-score; §2.4 class 3.]
3. Sightings history for any claim, across works. [F/G audits.]
4. Suppression lookup: exact (work, class, key) membership in the
   current verdict set — served by §3.5's `verdicts_cache`, not
   by a network call at emission. [§4.]
5. Snapshot registry lookups: snapshots by book; replay runs by
   snapshot; the baseline preview for a snapshot. [§1.]
6. Fixture scoring history by release/fixture version; the
   uncertainty queue's pending composition by class. [§2.]
7. Regenerated-rejection events per run/work, trend over time.
   [§4's counter.]

### 3.5 Spine tables (new state this design introduces)

```sql
snapshots(snapshot_id TEXT PRIMARY KEY, book_key TEXT, fingerprint TEXT,
          title TEXT, author TEXT, chapter_count INTEGER,
          payload_sha256 TEXT, store_path TEXT, baseline_preview TEXT,
          created TEXT, reason TEXT, extraction TEXT)  -- manifest JSON
replay_runs(run_id TEXT PRIMARY KEY, snapshot_id TEXT, started TEXT,
            replay_code_sha TEXT, replay_config_digest TEXT,
            preview_path TEXT)
verdicts_cache(work_id TEXT, class TEXT, claim_key TEXT, verdict TEXT,
               fetched_at TEXT, PRIMARY KEY (work_id, class, claim_key))
verdict_fetches(id INTEGER PRIMARY KEY, fetched_at TEXT, ok INTEGER,
                works INTEGER, claims INTEGER, note TEXT)
fixture_scores(id INTEGER PRIMARY KEY, release TEXT, fixture_version TEXT,
               scored_at TEXT, payload TEXT)  -- per-class rates + n
uncertainty_queue(id INTEGER PRIMARY KEY, created TEXT, run_ref TEXT,
                  work_key TEXT, class TEXT, subject TEXT, packet TEXT,
                  status TEXT, label TEXT, labelled_by TEXT, labelled_at TEXT)
rejection_events(id INTEGER PRIMARY KEY, run_ref TEXT, work_key TEXT,
                 class TEXT, claim_key TEXT, created TEXT)
```

`verdicts_cache` holds the **last fetched** Kevin-applied verdict
set (§4); `verdict_fetches` is its provenance log, so a stale
cache is always *measurably* stale. `rejection_events` is §4's
counter made queryable (one row per regenerated rejected claim
per run — the dashboard reads counts, the scorecard reads trend).

### 3.6 Seams for the corrections ledger (later consumer — seams only)

D1 will decide what a correction is. So that decision, when it
lands, does not require a second migration, the substrate
reserves two things and designs nothing further:

- **An append-only events shape.** A `correction_events` table
  is *reserved in the schema registry* (name + column sketch in
  this doc: id, ts, actor, kind, work_key, subject_key, payload,
  supersedes) but **not created and not written** by this project.
  The append-only, supersede-never-delete *shape* is the seam;
  its semantics are D1's.
- **A consultation point.** Each store's decision paths already
  pass through identifiable lookups (learned hit, staging
  observe, roster link). The substrate's access layer is the one
  place a future "consult corrections before applying" hook can
  be inserted per store without touching store logic — the same
  seam discipline as §3.2. Whether corrections *veto*, *annotate*,
  or *retract* is D1's question, untouched here.

### 3.7 Migration from the live JSON stores

The user's disk holds live state that must survive: staging
(3 works, 46 claims at last inspection), rosters for the gate
books and more, learned files accumulated over weeks. Migration
is a first-class slice, not a footnote:

1. **Trigger:** first run on the new code when `state.db` is
   absent and JSON stores are present (OC-3 offers the explicit-
   command alternative). Migration runs before any store is used,
   prints exactly what it is doing, and is **idempotent** (a
   `meta.migrated_from` record; a second attempt is a no-op).
2. **Import:** per store, inside one transaction: read the JSON
   files *read-only*, insert, then verify — entry counts per
   table equal entry counts per file; staging's accounting
   identity closes per work (UNACCOUNTED: 0); roster
   `minted_total` / `next_entity_seq` consistency per work. Any
   verification failure aborts the whole migration: the partial
   DB is deleted, the JSON stores remain authoritative, the run
   proceeds on JSON, and the failure is printed loudly (migration
   retries next run). Migration failure is therefore **delay-only
   by construction** — the same failure direction as the stores
   themselves.
3. **Retirement of the originals:** only after verification
   passes, each imported JSON file is renamed in place to
   `<name>.migrated-<date>` — never deleted. The rename is the
   rollback: stop the processor, delete `state.db`, strip the
   suffixes, and the pre-migration world is restored byte-for-
   byte. The migration prints these instructions. (Leaving the
   originals live-looking in place was considered and rejected:
   a later downgrade would silently resurrect stale state —
   corrections made since migration would vanish without a
   sound.)
4. **Series files** migrate with LearnedState (all of
   `series/*.json` → `series_state` rows), same verify-rename
   discipline.
5. **The gate runs it against copies of the real stores first**
   (§8): the live-shape discipline this program has used since F
   — the migration's test fixtures are the user's actual store
   files (copied, anonymized only if the user asks), not
   synthetic miniatures.

### 3.8 Corruption and failure posture

The bar is explicit: **at least as safe as the JSON status quo.**
The honest delta: one file replaces many, so per-file corruption
isolation is lost — a corrupt `state.db` implicates all three
stores at once. The design compensates at the points where the
JSON pattern's safety actually came from:

- **Atomicity:** every store save is a transaction; a crash mid-
  save leaves the last committed state, exactly as temp+replace
  leaves the last complete file. `PRAGMA synchronous = FULL` and
  journal mode per OC-1 (recommended WAL: report commands opening
  the DB read-only while a run holds it is a designed use, §3.4).
- **Detection:** on open, `PRAGMA quick_check`; on failure, the
  F8 discipline verbatim — loud warning naming the file, original
  bytes preserved as `state.db.corrupt` (with the WAL sidecars,
  which are part of the state), stores start empty, and a summary
  line admits state was not carried over. **No automatic restore
  from any backup** — auto-restore can resurrect retracted state
  (a merge correction, an anchor retirement) without a sound;
  recovery from `.corrupt` or a backup is a human act with the
  bytes preserved for it.
- **Rolling backup:** before the first write of a run, if the DB
  changed since the last backup, copy it to `state.db.bak`
  (single generation, atomic copy). The backup exists for manual
  recovery and for the migration gate's comparisons; nothing in
  the running code ever reads it.
- **Failure directions are inherited, per store** (the real
  safety argument): staging loss = sightings reset, promotions
  delayed (delay-only, F §7). Learned loss = learning resets
  loudly (degrade-to-no-op, as today). Roster loss = cross-run
  identity continuity resets for affected works — shipped DB rows
  are untouched, anchors re-seed from Supabase on the next run
  (they are fetched fresh by design), and the loss is loud and
  reported; NO pairs are the sharpest loss and are named as such
  (they are also the D1-adjacent record whose governance is
  undecided — this design does not pretend a backup policy
  settles it). In no store does substrate corruption create
  output: the worst case everywhere is delay, re-derivation, or
  a louder report.
- **Lock contention:** `--jobs` threads share one connection
  (`check_same_thread=False`) serialized by the stores' existing
  RLocks — the same discipline as today's load-modify-save, with
  strictly less lost-update surface (transactions replace whole-
  file replacement). A locked DB (another process mid-write)
  surfaces as a bounded retry, then the store's existing
  not-writable degrade — never a hang, never a crash.
- **Size:** the JSON stores are kilobytes-to-megabytes; SQLite
  adds page overhead and buys it back with no per-file rewrites.
  The 5 MB staging size-guard's analogue (a DB size warning in
  the learn-status report) is carried over.

### 3.9 What does NOT move (each ruled, with a reason)

- **Checkpoints** (`preview/.checkpoints/`) — do not move.
  Ephemeral by design: code-SHA-bound resume state whose correct
  fate on staleness is deletion; large chapter payloads; lifetime
  of one processing session. Snapshots (§1) are the *deliberate*
  frozen copies of this material, in the snapshot store — the
  distinction between disposable checkpoint and curated snapshot
  is the design, and merging them would blur it.
- **`config.json`** — does not move. Hand-edited, holds API keys:
  a state file that gets backed up, copied for gates, and shipped
  in bug reports must never contain credentials. Config is also
  read before any store opens; it cannot depend on the substrate.
- **Profiles** (`~/.ebook-processor/profiles/`) — do not move.
  User-authored JSON, same class as config: edited by hand,
  shared as files.
- **Preview artefacts** (`.json` / `.md`) — do not move. They are
  *products*, the dashboard's inputs, and the gate record; they
  live with the books and in git-able paths.
- **Eval fixtures** (`merge_pairs.json`, `fw_family_labels.json`,
  the §2 fixture) — do not move. Git-versioned, pre-registered,
  diffable; §3.5 stores scores against them, never the fixtures.
- **The trope catalog vector cache** — does not move. A derived
  cache, rebuildable from the catalog, already content-hash
  invalidated (fix-queue batch 2); it answers no cross-run query
  and its home (a cache directory concern) is unchanged.
- **`book_meta` / anything already in Supabase** — untouched.
  Supabase's role is unchanged by this design except §4's reads.

### 3.10 Invariants (tests-to-be)

- `test_substrate_roundtrip_fully_populated` — a staging entry
  and a roster entity with *every* field populated round-trip
  JSON → substrate → in-memory dict byte-identically (closes the
  deep review's spot gap: no single test round-trips a fully-
  populated staging entry today).
- `test_migration_counts_and_identity` — fixture copies of real-
  shape stores migrate with counts equal and staging
  UNACCOUNTED: 0 per work; originals renamed, rollback restores
  byte-identical JSON authority.
- `test_migration_failure_is_delay_only` — a store file corrupted
  in the fixture aborts migration, JSON stays authoritative, the
  run proceeds, the failure is loud.
- `test_corrupt_db_f8_discipline` — corrupt `state.db` ⇒ loud
  warning, `.corrupt` preservation (incl. sidecars), empty start,
  no output created in any store.
- `test_claim_run_parity` — B's fingerprint-equality semantics on
  the substrate: re-completion refreshes in place, exactly as on
  JSON (the batch-2 combined adoption test re-run against the
  substrate backend).
- `test_concurrent_observe_no_lost_sightings` — F's two-thread
  contention test, re-run on the substrate.


---

## 4. Verdict read-back + rejection feedback

**Goal:** one per-run, per-work, verdict-exact read of the human
verdicts already sitting in Supabase — powering (a) the fixture's
Kevin-applied authority (§2.1) and (b) **rejection feedback**:
claims a human rejected *for this work* stop being re-emitted,
and every regeneration is counted as the error event it is.

### 4.1 The read shape

Once per run, per work being processed, when Supabase is
configured (the `import_rejections_from_supabase` gate:
`CONFIG["supabase_url"/"supabase_key"]` present) and the work
resolves to a `work_id`:

```
GET book_trope_claims?work_id=eq.<id>&status=in.(confirmed,rejected)
    &select=trope_id,status
GET book_trigger_claims?work_id=eq.<id>&status=in.(confirmed,rejected)
    &select=warning,status
(+ the tropes catalog id→name map — already fetched by the
 existing import path; the two fetches share it)
```

The result is a **VerdictSet** for the work:
`{confirmed: set[(class, key)], rejected: set[(class, key)]}`,
keys in the staging claim-key currency (`trope:id:<uuid>` /
`trope:name:<tnorm>` / `trigger:<warning>` — §3 of the F design),
stored into `verdicts_cache` with its fetch timestamp
(`verdict_fetches` logs the attempt, success or failure). Scope
rulings:

- **Tropes + triggers only in v1** (OC-3). Characters are out:
  curated characters already have an owner — B's anchor machinery
  fetches and reconciles them per run — and a second, verdict-
  shaped character path would create two authorities over the
  same rows.
- **The read never writes.** No status is ever set, cleared, or
  annotated by read-back. Replace-wholesale and `_partition_claims`
  are untouched: preserved rows remain the write path's anchor,
  exactly as today; read-back is an *emission-time* consumer of
  the same curated truth.
- The existing global import (`import_rejections_from_supabase` →
  threshold learning) is **not replaced**: it is cross-book
  learning, D1's subject matter, semantics unchanged. Read-back
  is per-work and verdict-exact; the two coexist because they
  answer different questions ("how much do we distrust this trope
  generally" vs "did a human kill *this* claim").

### 4.2 Suppression at emission

Placement mirrors F §1's ordering discipline: suppression is
computed **after** the trope gate + denylist + rejection-learning
filters and **before** staging eligibility and result assembly —
the same seam neighbourhood as the staging split, for the same
reason (one decision point every consumer inherits). For each
emitted trope/trigger claim of the work:

- `(class, key) ∈ rejected` ⇒ the claim is **dropped from the
  emitted set**, counted (§4.3), and named in the run's summary
  line (`Suppressed N human-rejected claims for this work` + the
  names in the preview `.md` — suppression is never silent,
  because a silent filter is how corrections become invisible).
  It is **not staged**: F §4's hard rule already bars human-
  rejected claims from staging on the write path; suppression
  extends the same protection to emission, so persistence can
  never resurrect a killed claim into the queue via promotion.
- `(class, key) ∈ confirmed` ⇒ no force-ship, no boost — nothing.
  A confirmed claim the extractor no longer produces stays
  preserved by replace-wholesale as today. Read-back's confirmed
  set is consumed by the fixture (§2) and by dedupe reporting,
  not by emission. (Force-shipping confirmed claims was
  considered and rejected: it would make read-back a *source* of
  output — the exact authority creep the doctrine forbids.)
- **Corrections propagate** (the deep review's meta-pattern,
  closed for this path): suppression is recomputed from the
  freshest fetch every run. If Kevin lifts a rejection (status
  changed in the app), the claim stops being suppressed on the
  next run with no local state to unwind. There is no suppression
  *list* stored anywhere to go stale — only the verdict cache,
  whose staleness is bounded and printed (§4.4).

Suppression is deliberately **not** extractor learning: the
extractor will keep proposing the claim (that's what the counter
measures), and suppression keeps the proposal out of the ship
set, the preview's claim list, and the review queue. The seat
stops re-litigating settled verdicts; the underlying extractor
behaviour stays visible in the counter instead of being laundered
by the filter. Teaching the extractor itself is a later,
separate question (it is also D1-adjacent: what may learning
write, and from which runs).

### 4.3 The counter: regenerated rejections as an error event

Before suppression drops it, each regenerated rejected claim
writes one `rejection_events` row (§3.5): run ref, work, class,
claim key. The dashboard gains an additions-only line — per run,
`regenerated human-rejected claims: N` (named in the preview
`.md` summary as well) — and the release scorecard reads the
trend from the substrate. This is the Trope seat's minimum
disposition from the deep review, built: regenerated (work,
trope) pairs scored as a labelled error event. A run that
proposes zero previously-rejected claims prints 0; the metric's
existence, not any threshold, is v1's deliverable (a target —
e.g. trend to 0 across a series' books — is the User's to set
once the baseline is visible).

### 4.4 Failure posture when Supabase is unreachable

Processing must not depend on the read. The posture is
`cmd_verify_preview`'s, generalized:

- `sb()` returns None (unreachable/error) ⇒ **the fetch failed**,
  recorded in `verdict_fetches` with `ok = 0`. A failed read is
  not an empty verdict set, and nothing in the run may treat it
  as one.
- On failure, the run falls back to the **last cached** verdict
  set for the work (`verdicts_cache`), of any age, and **prints
  the cache's age on the run's face** (console + preview `.md`:
  `verdicts: cached, 3d old — Supabase unreachable`). Suppression
  from a stale cache fails safe in both directions: it can
  over-suppress a claim whose rejection was lifted while offline
  (delay — the claim returns next reachable run), and it can miss
  a rejection made while offline (the claim is emitted once more
  — the status quo, counted). It cannot ship anything a reachable
  read would have suppressed *and* hide that it did so: the age
  line is the disclosure.
- No cache for the work (first-ever run, offline) ⇒ no
  suppression, a printed line saying so, processing proceeds.
  Preview and write runs behave identically here, as everywhere
  in this design.
- Read-back failure never blocks, retries-storms, or degrades
  any other part of the run: one attempt per work per run, then
  the cache path. (`import_rejections_from_supabase`'s −1
  convention is the precedent; §4 keeps it for the shared fetch.)

### 4.5 Invariants (tests-to-be)

- `test_suppression_exact_pair_only` — a claim rejected for work
  A is suppressed in A and emitted normally in work B; a same-name
  different-trope claim in A is untouched.
- `test_suppressed_claim_never_stages` — a suppressed claim
  presented at the staging seam leaves no staging entry (F §4's
  rule, enforced at emission).
- `test_failed_read_is_not_empty` — stubbed `sb()` → None: the
  run uses cache or none, prints the posture line, and suppresses
  **nothing** on the strength of the failed read.
- `test_rejection_lifted_propagates` — fetch N rejects, fetch
  N+1 (status changed) does not; no local state edited between.
- `test_counter_counts_before_suppression` — a regenerated claim
  increments `rejection_events` exactly once per run even though
  it never reaches the ship set.
- `test_readback_never_writes` — stubbed sb asserts zero
  POST/PATCH/DELETE across a full read-back + suppression run.

---

## 5. Failure modes + abuse cases (cross-cutting)

- **Baseline rot.** Snapshots freeze draws made by old prompts
  and old models; a gate that keeps passing against a fossil draw
  proves the pipeline reproduces the fossil, nothing more.
  Containment: refresh is deliberate and reasoned (§1.2), the
  registry records each snapshot's extraction provenance, and
  draw-variance across *fresh* draws is printed beside every
  pipeline-delta — a baseline drifting away from fresh reality
  shows up as draw-variance growth before it shows up as a bad
  ship decision. There is no automatic refresh, by design; rot
  is managed by visibility, not automation.
- **Replay pollution.** A replay that wrote live state could
  promote claims early, mint roster runs, or age suspects —
  measurement quietly changing the measured system. §1.3's
  throwaway-substrate rule makes this structural, and
  `test_replay_never_writes_live_stores` pins it by file hash.
- **Fixture capture.** A fixture built from the seat's
  recommendations alone would grade the pipeline against one
  reviewer's judgment; the authority model (§2.1) keeps Kevin-
  applied verdicts supreme and disagreements visible. The
  reverse capture — fixture entries quietly edited to match a
  release's output — is what git-versioning + in-file acceptance
  signals exist to prevent: a fixture change is a diff a human
  reviews, like the bars in `fw_family_labels.json`.
- **Suppression as a memory hole.** §4's filter could hide the
  extractor's worst habit from everyone except the counter.
  That is why the counter is not optional garnish: events are
  recorded *before* suppression, named in the run summary, and
  trended in the scorecard. If the counter and the suppression
  ever disagree, the counter is the one that's believed and the
  bug is in the filter.
- **Substrate as single point of failure.** §3.8 states the delta
  honestly (one file, all stores) and its compensations. The
  residual risk worth naming here: an operator (or a cleanup
  script) deleting `state.db` as "just a cache." It is not a
  cache. The learn-status report names it as state, the `.bak`
  exists, and deletion's consequences are exactly §3.8's
  per-store failure directions — loud, delay-shaped, and
  recoverable by re-derivation everywhere except NO pairs, which
  are named.
- **Verdict-cache authority creep.** A cache consulted at
  emission could drift into being treated as the verdict store.
  It is a cache: refreshed per run when reachable, age-printed
  when not, and *wrong* by definition whenever it disagrees with
  Supabase. §4.5's lift test pins the direction of truth.
- **The spine measuring itself into doctrine.** Two-number
  reporting and fixture scores create pressure to optimize the
  numbers (draws chosen to flatter, fixture classes gamed). The
  defences are the standing ones: bars pre-registered in files,
  rates beside their n, and this design's rule that the spine
  decides nothing about claims.

---

## 6. Build plan (slices in order; each independently testable)

Order rationale: the substrate is first because replay's sandbox,
read-back's cache, and the queue all store into it; read-back
precedes the fixture because the fixture's authority join consumes
it; the fixture precedes the queue because the queue's labels land
in fixtures.

1. **Substrate core.** SQLite open/schema/meta, journal +
   synchronous pragmas, quick-check + F8 corruption discipline,
   rolling `.bak`. LearnedState moved onto the seam (§3.2) — the
   store with the simplest semantics first. Tests: §3.10
   round-trip + corruption; learned-state suite green unchanged.
2. **Staging + roster on the substrate, + migration.** Both
   stores' seams moved; migration (§3.7) with verify + rename +
   printed rollback; migration fixtures = copies of real-shape
   stores. Tests: §3.10 (accounting identity, claim_run parity,
   contention), F/B suites green unchanged.
3. **Replay harness.** Snapshot capture command + manifest +
   registry; `--replay` on snapshot payloads with the throwaway-
   substrate sandbox; preview `replay` marker. Tests: §1.6.
4. **Dashboard two numbers.** `preview_variance.py` Replay &
   variance section (additions only). Tests: §1.6 dashboard pins;
   legacy output byte-identical on marker-less pairs.
5. **Verdict read-back + suppression + counter.** Shared fetch,
   `verdicts_cache`, the emission seam, run-summary + preview
   lines, `rejection_events`, dashboard line. Tests: §4.5.
6. **Fixture builder + scorer.** `build_review_fixture.py` (seat
   files + read-back → `eval/review_fixture.json` + build
   report), per-release scorer, `fixture_scores` recording.
   Tests: §2.5 builder pins; scorer against fixture previews.
7. **Uncertainty queue.** `--emit-uncertainty` emission (four
   classes + random mix), labelling-packet report, labels-file
   intake into the existing fixtures. Tests: §2.5 queue pins.
8. **Validation gate (not code).** §8's protocol, run and scored,
   results written to `eval/` as a scorecard beside this doc.

---

## 7. Open choices (recommendation in bold)

- **OC-1 — Substrate file + journal mode.** **`state.db` in the
  learn dir, WAL journal, `synchronous=FULL`.** WAL because
  read-only report/dashboard processes opening the DB during a
  run is a designed use (§3.4); FULL because write volume is tiny
  and durability is the point. Alternative (rollback journal):
  one file, no `-wal`/`-shm` sidecars to confuse file management
  — costs reader/writer blocking during commits, and the
  sidecars are named in §3.8 so nobody "cleans them up."
- **OC-2 — Persistence seam vs full relational normalization.**
  **Seam (§3.2): keys/counters as columns, nested records as JSON
  payload.** Full normalization costs a rewrite of store logic
  and re-opens every F/B invariant, to serve queries nobody has
  named; the seam serves every query in §3.4. If a future query
  needs inside a payload, promote that field then — the schema
  version makes it a migration, not a redesign.
- **OC-3 — Read-back scope.** **Tropes + triggers.** Tropes only
  costs the safety class: rejected *triggers* (the claims where a
  regeneration is worst) keep re-emitting. Adding characters
  costs a second authority over curated character rows colliding
  with B's anchors — that needs its own design, not a row here.
- **OC-4 — Uncertainty batch shape.** **4:1 uncertainty:random,
  cap ~20 per labelling round.** Uncapped costs the homework
  death (§2.4); a higher random share costs boundary density —
  the queue exists for the boundary; 4:1 keeps one in five labels
  honest about the easy mass.
- **OC-5 — Decision-call recording for replay.** **Deferred:
  replay leaves decision calls live (§1.5).** Recording responses
  costs a second harness (storage, stubbing seams at every call
  site, provider-response drift in the fixtures) to remove a
  variance term the JEV eval already measures separately.
  Revisit if pipeline-delta proves decision-noise-dominated in
  the §8 gate — the gate reports the split, so the revisit has
  evidence to stand on.
- **OC-6 — Gate book set.** **Fourth Wing, Daemon, Freedom™**
  (the F/G set). Rotating or widening the set costs continuity
  with every pre-registered comparison since F; a fourth book
  can be added deliberately later the same way snapshots
  refresh — named, reasoned, recorded.
- **OC-7 — Migration trigger.** **Automatic on first run, with
  verify-then-rename and printed rollback (§3.7).** The explicit
  `--migrate-state` alternative costs a step the user must know
  to take, and leaves two live backends in the code until he
  does; automatic migration's risk is bounded by the abort path
  (failure = JSON stays authoritative, migration retries). If
  the User prefers to watch it happen, the same code runs under
  the flag with no design change.
- **OC-8 — Fine-distinction fixture classes.** **Included,
  labelled `interpretation-only`, reported separately (§2.2).**
  Excluding them entirely costs the escalation backlog as a
  measurable target for G (its §10 false pool is partly built
  from these claims); scoring them in the headline costs the
  headline's meaning (interpretation graded as truth).

---

## 8. Validation gate (pre-registered)

Fixed set, signals named in advance, per processor-quality-loop
discipline. The gate is passed slice-by-slice as §6 lands; the
full protocol runs before any default changes (there are no
default-on flags in this design except the substrate itself,
whose "flag" is the migration).

1. **Migration safety.** Migration over copies of the user's
   live stores: counts equal per table, staging UNACCOUNTED: 0
   per work, roster sequence consistency per work; originals
   intact as `.migrated-*`; rollback executed once and the
   restored JSON verified byte-identical. A deliberately corrupted
   fixture store aborts migration and the run proceeds on JSON.
2. **Behavioural parity.** The three gate books' flag-off preview
   artefacts are **byte-identical** before and after the substrate
   migration (stores are state; pipeline output must not move),
   and the full suite passes with no weakened or deleted tests
   (bar-moves, if any, named separately as always).
3. **Replay soundness.** Snapshot Fourth Wing; replay under
   unchanged code: the dashboard's deterministic sections match
   the baseline (decision-call sections may vary — the gate
   reports how much, feeding OC-5's revisit). Replay under a
   deliberately perturbed throwaway change: pipeline-delta moves,
   draw-variance (from two fresh draws) is reported separately
   and does not move. Live substrate hash identical across both
   replays; snapshot hash identical across everything.
4. **Read-back honesty.** Against a scratch work with a seeded
   rejected claim: the claim is suppressed, named, and counted
   exactly once. With Supabase made unreachable (bad URL in a
   test config): the run completes, the cache-age line prints,
   and nothing is suppressed on the strength of the failed read.
   A lifted rejection stops suppressing on the next fetch.
5. **Fixture truthfulness.** Fixture v1 builds with its report:
   per-class and per-authority n printed (no rate without its n);
   every `kevin` entry traceable to a DB row; every unresolved
   seat line accounted for in the build report (parsed, deduped,
   or named unresolved — none vanished). The scorer runs against
   the gate books' current previews and the numbers are written
   into the scorecard beside this design, whatever they say.

Ship/no-ship rests on these signals as written — in particular,
signal 2 is absolute: if the substrate moves any pipeline output,
the migration does not ship until that is understood, because a
state layer that changes behaviour is not a state layer.

