# Option G — §10 gate scorecard (build-side record, 2026-10-15)

Design: `eval/g-evidence-design.md`. Harness: `eval/g-gate.py`.
Fixture: `eval/g-judge-fixture.json` (28 hand-labelled passages
over two fictional mini-books: 10 supports / 11 adjacent /
7 contradicts, incl. negation ×2, discussion ×3, backstory,
hypothetical, accusation cases; every passage is a literal span
of its chapter's text, asserted by test).

This record separates what was exercised in the build sandbox
from what only the live gate can measure. **No stub number in
this document is a model measurement.**

## Exercised in-sandbox (harness mechanics, stub judge/embedder)

Run: `python3 eval/g-gate.py --stub` → mechanical verdict PASS.

- **Judge fixture plumbing:** 28/28 cases judged, 0 failed
  batches; the production batching/sanitizing path produces a
  full confusion matrix. The stub judge's own accuracy
  (supports 3/10, adjacent 3/11, contradicts 7/7; stub
  adjacent→supports leakage 0/11) measures the STUB's marker
  rules — it exists to prove the plumbing, and is recorded here
  only so the live numbers have something to be compared
  against for shape, never for value.
- **Cross-book bleed: 0.** 9 planted-claim runs (claims true of
  one fixture book asserted for the other, plus Dragons asserted
  for both) under a supports-everything adversarial judge:
  6 passages attached, every one re-verified as a literal span
  of the TARGET book's chapters. Retrieval scope + span
  verification hold even with the judge removed as a barrier.
  Also pinned in the unit suite with the suite's own fakes
  (4 attached, bleed 0).
- **Zero decision budget: proven.** `_dm_budget_claim` patched
  to a counting raiser around both gate exercises: 0 calls.
  The same pin exists as unit tests over the judge, the trope
  orchestrator, and the trigger half (OC-2).
- **Accounting identity: closes** on mixed fixtures in the unit
  suite (attempted = supported-strong + supported-single +
  adjacent-only + abstained + failed, before and after the
  trope/trigger marker merge).
- **Span verification: 100% by construction + verifier** — every
  attached passage in every exercise and unit test passed
  `verify_evidence` against its chapter (any miss would be a
  build bug, per §10.5; none occurred).

## Pending the live §10 gate (user's machine, real books)

Not exercised here, and not claimed:

1. **Judge accuracy with the real local model** — per-class
   accuracy on this fixture and the adjacent→supports leakage
   headline: `python3 eval/g-gate.py` (live mode builds the
   judge and embedder from the pipeline's own config).
2. **False-support rate < 10% at n ≥ 30** — the false pool
   (human-killed claims for Fourth Wing / Daemon / Freedom™
   plus planted cross-book claims on the real books) does not
   exist in the sandbox; the fictional planted set above is a
   mechanism check at n = 9 runs, not the gate measurement.
3. **Coverage ≥ 60%** of shipped tropes carrying ≥1 supporting
   passage on the three gate books' flag-on preview passes,
   with every zero-support shipped trope listed by name (the
   dashboard's trope-evidence section reads these off the
   marker).
4. **FW wall-clock overhead ≤ 10%** of the flag-off run.
5. **G4 invariance — PENDING-SPINE.** The replayed-extraction
   instrument does not exist yet (checkpoint resume
   refuses/clears across code changes); the spine build's
   slice 3 provides the replay harness and G4 is scored when
   it lands. It is deliberately NOT stubbed with two fresh
   extractions — a stubbed G4 is a false pin, worse than a
   deferred one. (Also recorded in the design's §10.)

## Fallback posture (pre-registered, unchanged)

If any §10 bar fails at the live gate, G ships **report-only**
— passages in the preview and the marker, never in shipped
rows — rather than the bars being renegotiated after the fact.

## Build record

Branch `agent/builder/g-evidence`, six slices, one commit each;
flag `--evidence-retrieval` default OFF, flag-off behaviour
byte-identical (pinned per slice). Suite: 1158 at base →
1218 at slice 6, all green; py_compile clean. No
PORTABLE_VERSION bump (assigned at merge). Not merged, not
pushed — Advisor gate follows this build; the §10 gate is
exercised before any default-on conversation.
