# Evaluation fixtures

Labelled fixtures for scoring live processor runs against known answers.
A fixture is only useful if the scoring rules are written down before the
run — this page is those rules.

## Fixtures

- **`fw_family_labels.json`** — Fourth Wing family claims. Five true pairs
  and thirteen false pairs, each with its outcome and validation score from
  the baseline run (-13, Portable v2.9.0, pre-pair-evidence-mining). The
  bars a new run is judged against live in the file's `acceptance_signals`;
  they are part of the fixture and do not change when results come in.

## Error taxonomy

Every failure a run produces gets one of four classes, taken from the
character-extraction literature and phrased in this pipeline's terms (the
canonical definitions also live in the fixture's `error_taxonomy` block):

| Class | Meaning |
|---|---|
| `missing` | A true pair or character the pipeline failed to produce in correct form: never extracted, dropped before write, or stripped of its true type in validation (e.g. a true sibling claim downgraded to `other`). |
| `hallucinated` | A claim the pipeline produced with no basis in the text: an invented relationship, type, or character that validation exists to kill before it is written. |
| `false_merge` | Two distinct characters collapsed into one roster entry (usually via a shared or mis-assigned alias), so both characters' claims and relationships land on a single identity. |
| `false_split` | One character split across two roster entries (e.g. a title-name entry and a given-name entry), fragmenting their claims, relationships, and chapter counts. |

In the fixture, each true pair carries `taxonomy: "missing"` (the class its
failure takes if it recurs) and each false pair carries
`taxonomy: "hallucinated"` (the class of the claim itself). `false_merge`
and `false_split` are roster-level classes; they have no pair entries.

## Scoring a live run

1. Reprocess the fixture's book and keep the new preview artefact.
2. For each fixture pair, look the pair up (either endpoint order; types
   describe `from` after canonicalization):
   - Present in the new run's `relationships` → it **survived**. For a true
     pair, survival at the wrong type (e.g. `other`) still counts as a
     `missing` failure of the typed claim; for a false pair, any survival
     counts against the false bar, and survival at the claimed family type
     is the worst outcome.
   - Present in `dropped_relationships` → it was killed, with the reason
     and validation score recorded there.
   - Absent from both → it was never extracted this run. For a true pair
     that is still a `missing` failure (extraction is part of the
     pipeline); note it as never-extracted so validation isn't blamed for
     an extraction miss.
   - In preview mode, a claim recorded with `validation_note:
     "no_evidence"` counts as a drop — preview records the decision,
     write mode enacts it.
3. Count the evidence-less write skips from the run's
   `Skipped N evidence-less relationships` line.
4. Score the roster separately for `false_merge` / `false_split`: read the
   new roster for one entry carrying another character's name as an alias
   (merge) or two entries that are the same person under different names
   (split). `preview_variance.py OLD_JSON NEW_JSON` surfaces roster churn
   between runs, but the class call is a human read of the roster.
5. Report per-class counts next to the fixture's acceptance signals —
   pass/fail against the signals as written, not against how the run felt.
