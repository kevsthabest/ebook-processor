# JEV same-person prompt evaluation — 2026-10-14

Live comparison of the two `ask_same_person` instruction texts,
run with `evaluate-merges.py` against the production payload shape
(names + descriptions + evidence). Model: `typesafe/jev-1.13` via
OpenRouter. Pairs: `eval/merge_pairs.json` — 113 pairs: 112 scored
(56 "same" / 56 "different") plus 1 unscored exhibit. Pair modes: 74 with
descriptions/evidence (production shape), 39 name-only.

## Results

### Threshold 0.5

| Variant | Accuracy | Precision | Recall | TP | TN | FP | FN |
|---|---|---|---|---|---|---|---|
| legacy (pre-swap production) | 83.04% (93/112) | 86.27% | 78.57% | 44 | 49 | 7 | 12 |
| identity | 83.04% (93/112) | 87.76% | 76.79% | 43 | 50 | 6 | 13 |

False merges @0.5 — legacy (7): Andrew Guerner vs Gruder 0.51;
Charles Mosely vs Chaz 0.73; Major Pain in the Ass vs MPITA 0.88;
Russell Vanowen, Jr. vs Russ 0.78; Sebeck vs Chris Sebeck 0.68;
Sebeck vs Wayne Sebeck 0.72; The Voice vs Voice 0.82.
Identity (6): Charles Mosely vs Chaz 0.67; Major Pain in the Ass vs
MPITA 0.80; Russell Vanowen, Jr. vs Russ 0.83; Sebeck vs Chris
Sebeck 0.63; Sebeck vs Wayne Sebeck 0.64; The Voice vs Voice 0.72.

### Threshold 0.85 (the production merge bar)

| Variant | Accuracy | Precision | Recall | FP | FN |
|---|---|---|---|---|---|
| legacy | 64.29% | 94.44% | 30.36% | 1 | 39 |
| identity | 58.04% | 100.00% | 16.07% | 0 | 47 |

Legacy's one false merge at the bar: Major Pain in the Ass vs
MPITA @ 0.88 — a confident wrong acronym expansion, over the bar
that actually merges roster entries. Identity scores that pair
0.80 and has zero false merges at the bar.

## Observations

- The two prompts make 19 errors each @0.5, and 17 of the 19 are
  the same pairs (legacy-only error: Guerner/Gruder false merge;
  identity-only error: Brian Gragg vs official Brian Gragg false
  split @ 0.49). The shared failures are evidence-bound, not
  wording-bound: unknowable aliases (Gragg/Loki, Merritt/Tripwire),
  spelling/bare-name variants with no supporting payload
  (Joseph/Josef Pavlos, Sebeck vs Pete), and extraction-invented
  surnames (Rhiannon Matthias vs Rhiannon Sorrengail, 0.04/0.06
  under both).
- Identity is uniformly ~0.1 more conservative on true pairs —
  the mid-scale instruction working as designed.
- On the Fourth Wing subset the two prompts are error-for-error
  identical (3 false splits, 0 false merges each); the false-merge
  difference is carried by Daemon pairs.
- Exhibit (unscored, transitivity-derived label): Pete Sebeck vs
  Peter Sebeck — identity 0.69, legacy 0.83.
- Chris Sebeck vs Peter Sebeck (the surname trap, scored): both
  prompts answer a confident NO (identity 0.27, legacy 0.23).

## Decision

Ship the identity text as the production `ask_same_person`
prompt (user-approved 2026-10-14). Rationale: equal accuracy,
strictly fewer false merges at both thresholds, and zero false
merges at the 0.85 bar that merges people — the worst error class
in the system. The recall cost lands where the architecture can
bear it: deterministic layers (roster reconcile, persisted-roster
tiers) own the name-form merges, leaving JEV as the judge of the
ambiguous middle, where a NO is recoverable and a YES is forever.
Earlier experiments' threshold conclusions (names-only payloads,
drifted prompt text) remain void; this run is the first to measure
the judge production actually runs.
