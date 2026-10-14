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

## The quality dashboard (`preview_variance.py`)

Comparing two previews of the same book prints the legacy Jaccard report
(frozen — its lines and scores are the historical record and never
change) followed by a **Quality dashboard** block. The dashboard answers
the questions the Jaccards can't: not just *that* two runs differ, but
which differences are errors, and of what class.

- **Labelled family pairs** — only when a labels fixture matches the
  book (found in `eval/` by `work_id`, else by the fixture's `book`
  string containing the previews' title and author; or passed
  explicitly with `--labels`). Per run, every fixture pair is classified
  with the rules in "Scoring a live run" above: **kept** (present at the
  claimed type, either endpoint orientation — a swapped parent/child is
  the same claim), **downgraded** (survived as `other`; a true pair here
  is a `missing` failure, a false pair here is still a precision
  failure), **wrong family type**, **dropped** (present in
  `dropped_relationships`; `no_evidence` counts as dropped), or **never
  extracted**. A budget-exhaustion survival is a kept claim in the
  preview and is classified by its surviving type. True-pair **recall**
  counts kept-at-claimed-type only. Endpoint matching resolves names
  through the run's own identity data (roster alias forms first, then a
  unique leading-token match — a bare surname never identifies anyone);
  when a run's roster has conflated two labelled identities, claims are
  attributed by their raw endpoint strings as a fallback, and the
  conflation itself shows up under roster identity.
- **Roster identity** — `false_merge` / `false_split` *indicators*
  (heuristic; the class call in step 4 above remains a human read). An
  entry **claims** a labelled entity when one of its name forms matches
  the entity's name exactly or by unique leading-token match — a form
  matching two entities claims neither. A merge is one entry claiming
  two or more labelled entities; a split is one entity claimed by two
  or more entries. Without a labels fixture this is reported as
  `n/a (not determinable from preview without labels)`: preview data
  alone cannot say which identities *should* exist.
- **Form flips** — names present in only one run that plausibly denote
  the same entity in the other (token-subset or alias-linked). This
  section explains raw-Jaccard churn (e.g. a POV scored 0.000 purely
  because one run wrote "Violet" and the other "Violet Sorrengail");
  it changes no score.
- **Unresolved endpoints** — per run, kept relationships whose `from`
  or `to` resolves to no character in that run's character list (by
  name or alias): the claims the app cannot join to a character row.
- **Empty-evidence kept claims** — per run, kept relationships with no
  evidence quote, split family vs non-family. This is the review-queue
  cost driver: evidence-less claims are what a reviewer has to
  re-derive from the text.
- **Spice threshold crossings** — the integer level boundaries the
  book's spice level crossed between runs (all of them listed; which
  boundary matters is a product call, not the scorer's).
- **Queue churn** — trope and trigger claim counts entering/leaving
  between runs (the item lists live in the legacy Tropes/Triggers
  sections).
