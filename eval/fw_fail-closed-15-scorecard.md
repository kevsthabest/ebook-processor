# Fourth Wing −15 run — fail-closed scorecard (pre-registered)

Draft prepared 2026-10-14, before the −15 run exists. Purpose: score −15
against the bars named when the fail-closed fix shipped (master 70a5310),
not against the older mining-experiment signals in fw_family_labels.json.

## Primary bar — path-independence (the −15 verdict)

- General Melgren → Violet Sorrengail and Sgaeyl → Violet Sorrengail must
  DROP, no matter which extraction path they arrive by.
- In −14 both survived as "other" only because their claims were
  evidence-less and bypassed validation entirely; identical claims with
  junk quotes died at 0.02–0.03 in −13. −15 passes only if that coin flip
  is gone.
- Scoring rule for preview runs: a claim labelled "[would drop: no
  evidence]" counts as a drop for this bar.

## Guards — survivals that are NOT failures

- A family claim WITH a quote whose validation was skipped because the
  decision-model budget ran out keeps its type, by design. Budget
  exhaustion is not evidence-lessness; do not score it against the fix.
- Non-family claims are unchanged by the fix; their behaviour is not
  part of this verdict.

## Explicitly NOT bars for −15

- True-pair rescue. The real-text audit of the actual EPUB showed
  Dad → Violet and Brennan → Violet have no sentence containing both
  endpoints at all, Fen → Xaden binds only through the narrator's "I",
  and General → Violet's best find is co-occurrence via the "Mom" alias.
  The rescue ceiling is prose form, not the mechanism — survival counts
  for these pairs cannot judge −15.
- The fw_family_labels.json target of >= 4/5 true pairs surviving was
  written for the mining experiment and is superseded for this run.
- Raw dropped-count going up. On evidence-poor (especially first-person)
  books the fix makes the dropped count tick up; that is the fix being
  visible, not a regression.

## Context numbers to compare against

- −13: 23 relationships in preview, 18 evidence-less (78%), 5 written to DB.
- −14: 36 relationships extracted, 12 evidence-less (33%), 24 DB entries.
- Extraction variance is still the upstream caveat: −14 vs −13 differed
  by 13 relationships, and only 2 of 5 labelled true pairs even reached
  validation in −14. Per-pair conclusions beyond the primary bar above
  are underpowered until variance is tamed.
