# Spine §8 validation gate — scorecard (2026-10-15)

Branch `agent/builder/spine` (base master @ `a7cd526`, PORTABLE_VERSION
2.9.14 — no version bump, per the work order). Evidence runner:
`eval/spine_gate_check.py` (21/21 checks passed in the build sandbox).
Full suite: **1206 tests OK** (baseline 1158; +48 spine tests in 7 new
files; the `tests/` diff against master is insertions-only — no
existing test weakened, deleted, or re-pointed; no bar moved).

What this sandbox cannot run is named as NOT RUN below, with the
reason. Nothing machine-owed is recorded as passed.

## Signal 1 — Migration safety: PASS

Over copies of real-shape stores (gate script + `tests/test_spine_migration.py`):

- Migration completes via the A1 protocol (build into
  `state.db.migrating`, verify, rename into place). Counts:
  learned 4 entries, staging 2 works / 3 claims, rosters 1, series 2.
- Per-table counts equal the file-derived counts; staging's
  `staged_ever` identity closes per work (UNACCOUNTED: 0); roster
  accounting closes (unaccounted 0); roster run-sequence semantics
  verified on the substrate (batch-2 claim_run parity test).
- Originals renamed `*.migrated-<date>`, never deleted (5 files).
- Rollback executed: delete `state.db`, strip suffixes → restored
  JSON **byte-identical** (SHA-256 compared per file).
- Deliberately corrupted `staging.json` → migration aborts, no
  `state.db`, JSON untouched and authoritative (delay-only).
- Simulated mid-migration crash (leftover `state.db.migrating` with
  junk bytes) → discarded and retried to completion (A1).
- Migration is idempotent; fresh installs get an empty substrate.

## Signal 2 — Behavioural parity: PASS at mechanism level; book-level NOT RUN (machine-owed)

**This signal is absolute** ("if the substrate moves any pipeline
output, the migration does not ship"). What was proven here:

- A synthetic book processed end-to-end (real `process_file_v2`,
  deterministic stub backend, seeded real-shape stores that the run
  actually reads) produced flag-off preview artefacts
  **byte-identical before and after the substrate migration**:
  preview JSON SHA-256 `63697e61671f60a8` on both sides; preview MD
  identical; staging-report and learn-status bodies identical.
- Full suite green with zero existing tests modified (see header).

NOT RUN here: the three gate books' (Fourth Wing / Daemon /
Freedom™) live flag-off previews. This sandbox has no books, no
`config.json`, and no LLM endpoint, so the book-level artefacts do
not exist to diff. **Owed before this branch merges:** on the
user's machine, capture the three books' flag-off preview
artefacts on master code, run once on this branch (the migration
fires automatically), and diff — any byte difference stops the
merge. Read-back is Supabase-config-gated, so on a configured
machine both sides attempt the same fetch; the substrate is not
in that path.

## Signal 3 — Replay soundness: PASS at harness level; book-level NOT RUN (machine-owed)

- Snapshot → replay under unchanged code: the live substrate's
  **logical** hash (canonical dump hash, A2) is identical across
  the replay; the snapshot's payload hash is identical; the
  sandbox is deleted afterwards; checkpoint writes during replay
  are suppressed (snapshot stays pristine); a fingerprint
  mismatched book is hard-refused (batch-1 shape, no override).
- Dashboard two numbers behave as designed: an unchanged replay
  scores pipeline-delta J = 1.000; a deliberately perturbed replay
  output moves pipeline-delta (J 0.000 on the moved field) while
  draw-variance is computed and reported separately from the fresh
  previews; a replay without its capture baseline in the set
  prints the refusal and is never scored as draw-variance.

NOT RUN here: the Fourth Wing snapshot/replay pair (no book in
this sandbox). Owed on the user's machine alongside signal 2's
artefacts. Cross-reference: G's gate signal G4 (invariance over
replayed extraction) stays PENDING-SPINE until a book-level replay
exists — this build provides the harness it was waiting for; it
is not scored here and was never stubbed.

## Signal 4 — Read-back honesty: PASS

Scratch-work checks (stubbed `sb`, the suite's `tests/test_spine_verdicts.py`):

- A seeded rejected claim is suppressed at emission (net A) and
  named; a staged-then-rejected claim does **not** ship via
  promotion (net B — the A3 pin: staged → rejected → no ship);
  the union of both nets is counted **exactly once** per claim in
  `rejection_events`.
- Read-back is read-only: the stub asserts every Supabase call is
  a GET (zero POST/PATCH/DELETE).
- Supabase unreachable: with a cache, the run uses it and the note
  carries the cache's stamp ("using cached verdicts from …"); with
  no cache, nothing is suppressed and the note says so. A failed
  read is never treated as an empty verdict set.
- A lifted rejection (status flips to confirmed) stops suppressing
  on the next fetch.
- Confirmed verdicts do nothing at emission — no force-ship, no
  confidence change (authority creep excluded by construction).

## Signal 5 — Fixture truthfulness: PASS (with the sandbox's kevin limit stated)

Fixture v1 built from the five real seat files
(`~/workspace/trope-review/`, 2026-10-10 → 2026-10-15):

- 160 verdict lines parsed → 61 unique (work, claim) pairs →
  **29 scored CONFIRM/REJECT entries** (the honest n — the design's
  "~150" was the corpus's line count, not its verdict count), 32
  ABSTAINs counted and excluded from scoring, 1 verdict flip
  retained in the appearances.
- Per-class n: premise-judgment 28, genre-mismatch 1 (Daemon —
  Enemies to Lovers, the structural headline case). Per-authority
  n: reviewer 29, kevin 0 — **in this sandbox** Supabase is not
  configured, so no kevin verdicts could join; the builder reports
  that instead of inventing authority. The join itself (kevin
  beats seat, disagreement recorded, never resolved away) is
  pinned by synthetic test with a stubbed read-back.
- Every seat line is accounted for: parsed, deduped (same-day ID
  restatements collapse into the day's appearance), or named —
  unresolved lines: **0**.
- Scorer runs against the fixture: with no book previews in this
  sandbox all 29 entries score `no-preview` and the headline
  (structural-REJECT shipped) names an empty list — recorded as
  vacuous here; scoring against the gate books' current previews
  is machine-owed with signals 2/3.

## Build interpretations recorded (design addendum, commit `652687b`)

- §6's slice order governs (substrate → migration → replay →
  dashboard → read-back → fixture → queue): dashboard before
  read-back because both dashboard numbers are read-only over
  preview markers, while suppression writes result fields.
- A store uses the substrate iff `state.db` exists in its learn
  dir; constructors never auto-migrate (migration triggers from
  `main()` only, before every state-reading dispatch).
- The `replay_runs` row is written into the sandbox substrate
  (the run's own working record); the durable record of a replay
  is the preview artefact's `replay` marker.
- Suppression applies to the v2 pipeline only; legacy
  `process_file` is untouched.

## Commits

| Slice | Commit | Content |
|---|---|---|
| addendum | `652687b` | Rulings of record folded into `eval/spine-design.md` (OC approvals, A1–A6, build interpretations) |
| 1 | `c463e14` | Substrate core (StateDB, WAL + synchronous=FULL, backup-API `.bak`, logical dump hash) + LearnedState seam |
| 2 | `3cb53cd` | StagingState + RosterStore seams; A1 build-verify-rename migration with rollback |
| 3 | `85b4306` | Snapshot store keyed by fingerprint (A5) + sandboxed replay (A2 seeding, decision calls live) |
| 4 | `5d49e37` | Dashboard replay-variance section (pipeline-delta vs draw-variance) |
| 5 | `4aec7f5` | Verdict read-back + two-net suppression (A3) + rejection counter |
| 6 | `a41b10a` | Fixture builder + scorer; `eval/review_fixture.json` (n=29) |
| 7 | `2123928` | Uncertainty queue (4 classes + 4:1 controls, label intake → `eval/merge_pairs.json`) |
| 8 | (this commit) | Gate runner + this scorecard |

**Gate verdict (sandbox scope): signals 1, 4, 5 PASS; signals 2, 3
PASS at mechanism/harness level with their book-level halves
explicitly machine-owed.** Signal 2 is absolute, so the branch is
ready for Advisor review, not for merge: the merge gate is the
three books' byte-diff on the user's machine.
