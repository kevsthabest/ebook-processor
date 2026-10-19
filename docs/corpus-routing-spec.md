# Corpus routing and scan-cost reduction

## Status

**Proposed.** This is an opt-in measurement and routing design for the v2
pipeline. It does not change the default full chapter scan until the defined
gates have passed on replayed, labelled books.

## Problem and scope

`corpus.db` already avoids reparsing books and re-embedding passage windows.
It does not currently reduce the two extraction calls made for every eligible
chapter in `process_file_v2`:

* Call A extracts characters, relationships, and POV in chapter order because
  each result updates the next chapter's roster.
* Call B extracts summaries, tropes, triggers, spice, and quotes. It can run
  in parallel, but it still sees every chapter.

The existing `--evidence-retrieval` path is downstream of extraction. It
retrieves support for already-extracted trope and trigger claims; it cannot
reduce the initial chapter scans.

The aim of this design is to determine whether corpus-derived routing can
reduce local-model input tokens and wall time without suppressing supported
characters, tropes, or trigger warnings. It starts with measurements, then
limits any skipping to tasks for which the data supports it.

The design deliberately does **not** alter:

* corpus ingestion, source hashes, passage IDs, or vector-generation checks;
* evidence verification, relationship validation, or the existing
  fail-closed family-claim behaviour;
* the default v2 scan until a routed mode passes its acceptance gates.

## Non-goal: using retrieval to skip Call A initially

Call A should remain chapter-complete in the first implementation. A missed
early character can corrupt later roster resolution, aliases, relationships,
and POV attribution. The corpus may attach routing observations to Call A,
but it must not shorten or skip Call A in any release covered by this spec.

This also means that corpus routing is unlikely to halve total scan time by
itself. Its first credible savings target is downstream evidence and
decision-model work, followed by selectively routed content work only after
measurement.

## Data contracts

No new permanent corpus table is required in the first slice. Routing results
are versioned `stage_outputs`, because they are derived from existing chapter
text, passage spans, the router query set, and the current vector generation.

### New stage

`stage = "route"`

`stage_version = "route-v1"`

One output exists for each `output_key` below:

* `trigger:<category>`
* `relationship:<canonical-pair>` once Call A has created a candidate pair
* `trope:<normalised-catalog-or-display-name>` only for an already proposed
  trope; it is evidence routing, not trope discovery

Each payload must be plain JSON and contain:

```json
{
  "task": "trigger",
  "query_version": "trigger-lexical-v1",
  "vector_model": "...",
  "vector_format": "raw-f32-v1",
  "coverage": {"passages_total": 0, "passages_current": 0},
  "candidates": [
    {
      "passage_id": "...",
      "chapter_index": 12,
      "start_offset": 345,
      "end_offset": 912,
      "lexical_rank": 1,
      "vector_score": 0.71,
      "selection_reason": ["fts", "vector"]
    }
  ],
  "skipped_reason": null
}
```

The payload holds offsets and scores, never a duplicate copy of book text.
At read time, text is sliced from `corpus_chapters.text` and its hash is
checked against `corpus_passages.text_hash`. A stale offset, missing passage,
or hash mismatch is a routing failure and falls back to the current scan
path; it never silently supplies a different excerpt.

### New run marker

Add `corpus_routing` to the preview JSON and run-event book-end payload:

```json
{
  "mode": "off|shadow|route",
  "stage_version": "route-v1",
  "eligible": true,
  "corpus_state": "complete",
  "tasks": {
    "trigger": {
      "queries": 25,
      "candidate_chapters": 0,
      "candidate_passages": 0,
      "full_scan_evidence_covered": 0,
      "full_scan_evidence_total": 0,
      "estimated_input_chars_avoided": 0
    }
  },
  "fallbacks": []
}
```

`full_scan_evidence_*` is populated only in shadow mode, after normal v2
extraction is complete. It measures coverage of existing verified evidence;
it is a diagnostic baseline, not a correctness claim or human label.

## Configuration and CLI

Add these defaults to `CONFIG` and document them in `config.example.json`:

```python
"corpus_routing": "off",              # off | shadow | route
"corpus_route_max_passages": 3,
"corpus_route_context_chars": 900,
"corpus_route_min_vector_coverage": 0.90,
"corpus_route_skip_audit_rate": 0.10,
```

Add CLI arguments:

```text
--corpus-routing {off,shadow,route}
--corpus-route-audit-rate FLOAT
```

Rules:

* `off` is the default and is byte-compatible with current output aside from
  an absent routing marker.
* `shadow` requires a corpus work in `complete` state. It performs routing,
  records the marker and stage outputs, but does not change model inputs or
  skip calls.
* `route` is initially accepted only for trigger evidence/validation routing,
  never for Call A or initial trope discovery. It is rejected for write runs
  until the acceptance gates below are recorded as passed for the active
  router version and model generation.
* If FTS is unavailable, vectors are incomplete, a selected span does not
  verify, or routing errors, record the reason and run the existing path.

The active model and prompt stamps must include the routing mode and router
versions, so a replay report can distinguish a changed input-selection policy
from a changed extraction prompt.

## Required code changes

### 1. Corpus read helpers

File: `process-portable.py`, `CorpusDB` and corpus helper section.

Add read-only helpers; they must use the existing `CorpusDB` handle and make
no schema writes:

```python
def corpus_passages_for_chapters(db, fingerprint, chapter_indices): ...
def corpus_passage_text(db, passage_id): ...
def corpus_vector_coverage(db, fingerprint, model, fmt): ...
def corpus_fts_search(db, fingerprint, terms, limit): ...
```

`corpus_passage_text` joins `corpus_passages` to `corpus_chapters`, slices the
stored chapter using its offsets, and verifies the passage text hash before
returning text. It must return `None` on any mismatch.

Do not store passage text redundantly. `corpus_passages` intentionally stores
only spans; the chapter remains the source of truth.

Extend `CorpusDB.fts_search_chapters()` or add the scoped helper above so
queries are restricted to one fingerprint. The existing method searches all
works, which is unsafe for routing a single book if its result is used to
select model input.

### 2. Router implementation

File: `process-portable.py`, add a corpus-routing section adjacent to
`evidence_retrieve_result()` and `evidence_retrieve_triggers()`.

Add:

```python
ROUTE_STAGE_VERSION = "route-v1"

def corpus_route_trigger_candidates(db, fingerprint, chapters, category, ...): ...
def corpus_route_relationship_candidates(db, fingerprint, chapters, pair, ...): ...
def corpus_route_trope_support(db, fingerprint, chapters, trope, ...): ...
def corpus_route_shadow_report(route, full_result, chapters): ...
```

Routing is a hybrid ranker:

1. lexical retrieval from a versioned, in-code query dictionary;
2. vector ranking of passages within the lexical hit chapters;
3. deterministic de-duplication by passage ID;
4. select at most `corpus_route_max_passages` passages, expanding each with
   `corpus_route_context_chars` of same-chapter context;
5. record every candidate and selection reason.

The lexical dictionary must be conservative and versioned per trigger
category. Its results nominate passages; they do not create a trigger claim.
The relationship router may use roster names and aliases only after Call A
has produced them. Co-occurrence, surname overlap, or a kinship term alone is
never enough to create a relationship.

### 3. Stage cache integration

File: `process-portable.py`, reuse the current `corpus_stage_*` functions.

Route-output input hashes must cover:

* fingerprint and selected chapter text hashes;
* route stage and query-dictionary versions;
* category or relationship/trope query;
* active embedding model and vector format;
* candidate-count and context-window settings.

They must not include mutable run time or model output. A changed trigger
query invalidates only that category's route record. A changed chapter text
invalidates only records that cover the changed chapter.

### 4. Shadow-mode pipeline seam

File: `process-portable.py`, `process_file_v2()`.

After normal chapter Call B reduction and before the optional
`evidence_retrieve_triggers()` call:

1. obtain the corpus handle and verify that the presented file maps to the
   same fingerprint and a complete current-generation corpus work;
2. construct trigger routes for the categories emitted by the normal run;
3. compare route passage spans to verified trigger evidence in the normal
   result;
4. write the route stage records and `corpus_routing` preview marker.

Shadow mode must not invoke extra LLM or decision-model calls. It may use the
local embedding endpoint only for vectors missing from a complete current
generation; in the first implementation, prefer marking that route
ineligible instead, so the experiment has stable cost.

### 5. Routed trigger-evidence seam

File: `process-portable.py`, `evidence_retrieve_triggers()`.

When `corpus_routing == "route"`, use the selected, verified passage spans as
the candidate pool for trigger evidence retrieval. Preserve the existing
negative-regex and judge path. If fewer than the required candidates can be
read or verified, fall back to the current support-chapter route for that
category.

This is deliberately a *downstream* optimization. It may reduce retrieval
and decision input size, but does not skip Call B or change trigger severity.

Do not route initial trope discovery in this slice: a trope is unknown before
Call B proposes it. Do not route Call A in this slice.

### 6. Telemetry

Files: `process-portable.py`, preview writer, `_RunEventSink` call sites.

Add stage timing and token accounting where the OpenAI-compatible response
already records `usage` and `timings`. Emit per-book totals for:

* Call A and Call B request count, prompt tokens or prompt characters,
  completion tokens, and elapsed seconds;
* merge, relationship, trigger, and quote decision calls and elapsed seconds;
* corpus route lookup time, candidate count, cache hits, and fallbacks.

Prompt-token accounting should use server-reported usage when supplied. When
the endpoint does not report prompt tokens, record UTF-8 character count as
`prompt_chars_estimate`; do not present it as token usage.

### 7. Tests

Add focused tests, without altering existing corpus or extraction snapshots:

* FTS routing is fingerprint-scoped and cannot select another book's passage.
* Passage offsets and hashes are verified before text is returned.
* A partial, stale, suspect, or absent vector generation makes the route
  ineligible and uses the existing path.
* Shadow mode does not change Call A/B inputs, call counts, extraction output,
  or shipped preview fields.
* Route-stage cache reuses unchanged records and invalidates only the changed
  category/chapter/query version.
* Routed trigger evidence retains the current negative-regex rejection and
  exact-text verification.
* A route read failure degrades to current behaviour and is visible in the
  marker.
* Run-event and preview metrics never borrow token or timing totals from a
  prior book.

## Rollout and acceptance gates

### Phase 0 — profile current scans

Run full previews with run events enabled for Daemon and Fourth Wing. Record
per-stage wall time and server-reported usage. This decides whether corpus
routing is worth pursuing: if Call A/B dominates, routing downstream evidence
cannot materially reduce total time.

### Phase 1 — shadow only

Run `--corpus-routing shadow` on replayed extraction for the same books. No
claim or model-input difference is allowed. Inspect every verified full-scan
trigger evidence span that the router did not select.

The shadow report is acceptable only when:

* every labelled high-severity trigger's verified evidence is selected;
* trigger evidence coverage against the full-scan baseline is at least 95%;
* every miss is listed with category, chapter, and route reason;
* routing's local lookup cost is substantially below the decision-model time
  it is expected to save.

The 95% baseline is a diagnostic bar, not proof of recall. Human-labelled
trigger cases override it: any labelled miss blocks advancement.

### Phase 2 — routed downstream evidence

Enable `--corpus-routing route` for preview runs only. Compare it with the
full/replay baseline. It may proceed to write runs only if it shows:

* no labelled trigger recall loss;
* no new unverified evidence reaching the final claim;
* no change in trigger severity caused by routing;
* at least 20% lower decision-model input characters for routed trigger
  evidence, or an otherwise demonstrated wall-time improvement.

### Phase 3 — decide whether Call B routing is warranted

Do not implement chapter-skipping for Call B until Phase 2 shows that the
local model's main cost lies in task-specific evidence work. If Call B is the
dominant cost, design a separate experiment with a held-out, human-labelled
set and an explicit skipped-chapter audit. This is a new decision because it
can suppress initial trope and trigger discovery.

## Expected outcome

The first release adds trustworthy measurement and should lower the cost of
retrieval and decision work for corpus-complete books. It does not promise a
large end-to-end speedup, because the sequential character pass still scans
the book. The profiling gate makes that limitation explicit before more
invasive Call B refactoring is attempted.

---

## Adopted amendments (2026-10-18, Decision)

The spec above is adopted for phased testing with these amendments, which
govern wherever they differ from the text above.

**A1 — The gates record is a checked-in artefact.** "Gates recorded as
passed" means a scorecard file in `eval/` (the pattern of the existing
G-gate and spine scorecards) naming the router version, the embedding
model generation, and the Phase 2 comparison artefact the verdict rests
on. Route mode on a write run checks that record; no record, no route.

**A2 — Phase 0 is telemetry-only, with a kill bar.** Phase 0 ships only
the §6 per-phase accounting (independently owed to the calibration and
reserve-sizing work). If, on both Daemon and Fourth Wing, decision-model
plus evidence work is under 15% of total wall time, corpus routing is
parked — recorded in the Phase 0 report, not silently dropped — and only
the telemetry remains.

**A3 — The shadow savings estimate is defined against the routed step's
real input.** `estimated_input_chars_avoided` for a category is measured
against what the existing trigger-evidence path would actually have sent
for that category (its support-chapter pool), not against chapter text
in general.

**A4 — Two honesty clauses.** (a) Shadow mode writes `route` stage
records into `corpus.db` during preview runs; that is the corpus's
derived-cache convention and is expected, not a side effect to discover
later. (b) The Phase 1 acceptance bars cover triggers only. Relationship
route records are collected during shadow for later analysis, but the
relationship router has no Phase 1 bar; its coverage metric is deferred
to the slice that first proposes routing it.

**Sequencing note.** The §6 telemetry lands after the `--run-history-backfill`
build (v2.13.57), whose writer reads the same run-event book-end payload.

---

## Phase 0 outcome (2026-10-18) — ROUTING PARKED per amendment A2

The telemetry shipped (v2.13.58, corrected v2.13.59). Both Phase 0 profile
runs are recorded (Daemon run 24, Fourth Wing run 25; per-phase splits in the
programme's Phase 0 report):

- Daemon: wall 2,402s — extraction 94.3%, decision model 0.67% (16.2s);
  decision plus the entire untimed remainder is at most 5.7% of wall.
- Fourth Wing: wall 3,580s — extraction 96.8%, decision model 0.73% (26.1s);
  decision plus the entire untimed remainder is at most 3.2% of wall.

Decision plus evidence work is under the 15% kill bar on both books under
every reading of the instrumentation (evidence retrieval is not separately
timed; the bound credits it with all untimed time). Per amendment A2, corpus
routing is PARKED: Phases 1–3 are not built. The §6 telemetry remains in the
product — it is independently owed to the decision-budget calibration and
reserve-sizing work. Reopening condition: a future profile, on this same
telemetry, showing decision plus evidence work at or above 15% of wall time
on a representative book.
