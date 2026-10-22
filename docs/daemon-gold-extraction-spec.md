# Daemon gold extraction — spec and status

Status snapshot: **2026-10-22**. Every number here was produced on this tree.

This document is the handoff for anyone reviewing or changing the extraction →
analysis pipeline. It states the contracts the code now guarantees, how the
score is measured, what passes and what does not, and which experiments were
already tried and rejected (so they are not repeated).

---

## 1. Scope and goal

The pipeline must extract five claim types from a book — characters,
relationships, quotes, tropes, trigger warnings — validate each, and keep the
extraction in the book's checkpoint for human review.

`Daemon` (Daniel Suarez) is the gold book. The frozen gold pack defines the
target for **relationships**:

- precision ≥ **0.90**, recall ≥ **0.70** on 13 must-ship pairs
- 7 must-not-ship pairs, 2 either-way pairs
- deterministic checks for characters, quotes, tropes, triggers

The pack lives at `daemon-gold-pack-v1-draft(1).md` (human source) and
`eval/daemon-labels.json` (machine form; edit ONLY by announcing a new version).

---

## 2. Frozen artefacts (do not drift these silently)

| Thing | Value |
|---|---|
| Gold pack | `eval/daemon-labels.json`, **v1.2**, frozen 2026-10-22 |
| Frozen extraction | `extract-20261022-110407-15384` (OpenRouter GLM, ask Kevin before re-extracting) |
| `passed_with` | `null` — the 0.90 / 0.70 bar is **not** met |
| Principal cap | 30 (raised from the pack's original 20; see §5.3) |
| Judge | decision model `typesafe/jev-1.13` (provider `openrouter`), pinned |

Pack v1.2 changes (recorded in the file's `changes[]`):

1. Frozen against the 110407 extraction (Kevin ruled extraction is not this
   project's weakness).
2. `principal_cap` 20 → 30, because judge-confirmed relationships now promote
   their minor endpoint to principal (§4.6).
3. Scoring resolves gold names through the run's own roster aliases and ignores
   preview rows a write deletes (§5.2).
4. Ruling: `Steven Trear` / `Natalie Philips` re-centred on **colleague**
   (acceptable: colleague, ally, enemy).

---

## 3. Pipeline contracts

### 3.1 Extraction identity (§ Step 0)

`_extractor_identity()` returns `{"llm", "model"}` for the active backend and is
written into both the extraction stamp (`extraction.json`) and the artefact
manifest (`manifest.json`). `openai_model` is pinned in `config.json` so a run
can state which model produced it. `run_models` keeps the older `_history_models`
block.

### 3.2 Roster intake filter

`_is_person_like(name)` is the deterministic backstop at `_roster_update`.
Rejects, in order: lowercase plural job/group nouns, `the|a|an + group`, any
group noun in any case (`Go-Go Girls`), non-name punctuation (`Go Mets!`),
single ALL-CAPS tickers (`HSTM`), and names made only of role/descriptor tokens
(`Mother`, `Second Victim`, `Asian Woman`, `Unknown Female Voice`).
**Deliberate allowance:** the article form `the <role>` survives, because
Daemon's Major is called "The Major" throughout.

Tests: `tests/test_roster_filters.py` (9 rejected, 10 kept).

### 3.3 Character prompt

`PROMPT_V2_CHARACTERS` rule 3 is **NAMED INDIVIDUALS ONLY**: one named person
per entry, role nouns and unnamed descriptors omitted, company names / tickers /
games / places excluded, extra forms go to `aliases`. The SIMPLE prompt says the
same in one line.

### 3.4 Relationship assembly (`v2_reduce`)

Per **unordered** pair key:

- one winner from the competition — best confidence, then most specific type,
  then evidence-bearing, then earliest chapter;
- family guard: a family claim wins the pair when any family candidate exists;
  the suppressed non-family winner is audited;
- direction contradictions are recorded on the winner;
- every non-winning candidate goes to `pair_losers` (never to
  `dropped_relationships`);
- the highest-ranked loser is the pair's ONE revival candidate.

### 3.5 Evidence preference before judging

A claim is judged on a sentence that **names both endpoints** whenever one
exists: `_evidence_names_pair` (word-boundary match over
`_endpoint_forms`: primary keys, alias keys, canonical name, surname) replaces a
quote that names neither/both with a binding mined sentence. The claim's
original quote is never altered; the substitution is recorded as
`evidence_mined` / `evidence_grade: "binding"`.

### 3.6 Validation

Family claims: fail-closed on evidence (no quote and no binding mined sentence →
`no_evidence`, no judge call spent), then keep / downgrade-to-`other` / drop by
the judge's P. Non-family claims: keep if P ≥ `_REL_DROP_THRESHOLD` (0.30),
otherwise drop; `other` claims are skipped.

Runner-up revival: a pair whose winner dies gets one revival through the same
validator; a revived claim that cannot be judged for budget ships as
`review_required`.

The judge is given **rival types**: `_validation_context` appends

```
Extraction also asserted these other relationship types for this same pair
(each may be wrong — do not treat the claim's own type as the only possibility): ally, enemy
```

This is the single measured analysis-phase win: precision 0.700 → 0.778 on the
frozen extraction (it rejected `Lanthrop → Sobol [enemy]`, P=0.38).

### 3.7 One row per pair

After validation, `relationships` is collapsed to **one row per unordered pair**.
The surviving row is the one the write path ships (a `kept` claim beats a
`would_drop` one, then first row wins). The loser moves to
`dropped_relationships` with `validation_note: "duplicate_pair"`, `prior_note`
and `superseded_by_type`. Before this, the Daemon run shipped `child` **and**
`mentor` for one pair.

### 3.8 Preview mirrors write

A claim the write path deletes — `no_evidence`, `dropped`, `would_drop` — leaves
the shipped row set in **both** modes and stays in `dropped_relationships` under
a note that names the mode (`would_drop` in preview, `dropped` in write;
`no_evidence` in both). The runner-up revival therefore decides the pair exactly
as it will at write time. Preview can no longer advertise a row that will not be
stored.

Residual deltas (accepted, documented): in preview the family mid-band
downgrade keeps the claimed type and records `downgraded`, while write rewrites
the type to `other` (§8, open decision).

### 3.9 Principal filter with judged significance

`_filter_principals` passes: POV, then frequency ≥ `min_frequency`, then 3+
relationships with principals (×2), then **pass 4** — a `kept` claim anchored on
a principal promotes the other endpoint. Pass 4 iterates to a fixed point
(a promotion can anchor the next claim: the Major promotes Vanowen, whose
confirmed link to Lindhurst then promotes Lindhurst).

### 3.10 Triggers

A category whose every evidence quote the judge rejected is **downgraded to
`mentioned`**, never silently dropped: `severity: "mentioned"`,
`severity_claimed`, `decision_p` = max rejected P, `decision_gate: "rejected"`,
`detail: "judge rejected every evidence quote; category kept at mentioned"`,
plus its chapters and evidence. A missing content warning is the failure mode
this gate must not have.

### 3.11 Quotes

Two gates, then one poll:

1. **Speaker grounding** — the speaker's canonical name (or surname) must appear
   in the line or the ~300 chars around it. Alias keys are deliberately excluded
   (polluted aliases once "grounded" a Sobol quote on a mention of Merritt).
2. **Judge poll** — ONE batch call over the top 12 candidates using the four
   `corpus_quote_poll` aspects. `spoiler ≥ 0.5` and `heat ≥ 0.5` are absolute
   drops; `quotability` and `representative` rank (fills the cap rather than
   emptying the list). A failed poll leaves the deterministic ranking.
3. `result["quote_poll"] = {"polled", "kept", "failed"}`.

### 3.12 Tropes

When the judge is batch-capable, gated tropes are judged by the decision model in
one noul batch (candidate + the summaries of chapters that proposed it, book
order, ≤1400 chars), keeping `p ≥ 0.5` with the unchanged confidence formula.
No decision model, or a judge that cannot batch → the original
`v2_trope_gate` path.

### 3.13 Checkpoints are the review bundle

A completed run **keeps** `preview/.checkpoints/<book_key>/` (`meta.json`,
`chapter_*.json`, `roster.json`, `extraction.json`, `merged-roster.json`) and
writes `analysis.json` + `rel-types.json` into it after Stage D (best-effort:
one printed line on failure, never a run failure). `--stage extract` keeps its
directory too. The only automatic purge left is `_checkpoint_purge_stale`
(fingerprint-gated). `py process-portable.py --clear-checkpoints` is the explicit
purge (refused under replay).

### 3.14 Decision budget

One pool per book (`chapters` units), consumers in pipeline order
`merge_tier3 → rel_family → rel_nonfamily → trigger_gate → quote_poll →
discovery → untagged → jev_merge`; each stage's reserve is protected until every
earlier stage has finished. No new consumer was added.

---

## 4. How the score is measured

```
py score-gold.py --gold eval/daemon-labels.json --run <preview|analysis>.json
```

`score-gold.py` loads `process-portable.py` via importlib for `norm_name`,
`verify_evidence`, `_tnorm`, `_REL_FAMILY_TYPES`, `_REL_INVERSE`.

### 4.1 Relationships

- Pair key: unordered, `_norm` = `norm_name` + comma strip
  (`Russell Vanowen, Jr.` ≡ `Russell Vanowen Jr.`).
- Gold and shipped names both resolve **through the run's own roster aliases**
  (an entry's own name wins; a form two entries claim resolves to nothing).
- A reversed shipped direction is compared through `_REL_INVERSE`.
- `acceptable` defaults to `[type]`.
- recall = must-ship pairs shipped acceptably / 13.
- precision = correct / labelled, where labelled = shipped claims whose pair is
  in ANY bucket; claims on unlabelled pairs are ignored.
- Rows with `validation_note in {would_drop, would_downgrade}` are **not
  shipped** and are excluded (counted in `excluded_would_drop`).

### 4.2 Deterministic checks

| Check | Rule |
|---|---|
| character grounding | every shipped name (or its last token) appears in the re-acquired book text |
| character count | ≤ `principal_cap` (30) |
| ruled-away name | `Major Pain in the Ass` must not exist |
| identity rulings | `Loki`/`Brian Gragg` and `Karl Devon`/`The Major` must not be two entries |
| quotes | every shipped quote verifies against the book; its speaker within 300 chars; ≥ 4 shipped |
| tropes | none of the pack's must-not-ship tropes, none from `_is_denylisted_trope`, every shipped trope has ≥ 2 chapters |
| triggers | every category at on_page/graphic has an evidence quote with `decision_p ≥ hi`; no shipped category has zero chapter indices |

Exit code 1 if precision < 0.90, recall < 0.70, or any deterministic check fails.

**Judge variance:** the decision model is stochastic. Repeated analyses of the
*frozen* extraction move a claim's P by ±0.1 and occasionally flip a mid-band
claim, so precision has been observed at 0.778, 0.800 and 0.889 on the same code
while recall stayed 8/13. Treat a single sample as ±1 row.

---

## 5. Current status (frozen extraction, pack v1.2)

```
relationship precision  0.800  (8/10 labelled)   target 0.90   FAIL
relationship recall     0.615  (8/13 must_ship)  target 0.70   FAIL
character grounding     25/25                    PASS
character count         25 (cap 30)              PASS
ruled-away name absent                           PASS
identity rulings         one entry each          PASS
quote verification      1.00 of 8                PASS
quote speaker grounding 1.00 of 8                PASS
quote count             8                        PASS
trope rulings / denylist / support               PASS
trigger evidence / chapter indices (19)           PASS
```

Best single sample observed on this code: **0.889 / 0.615** (see §4.2 variance).

Remaining labelled errors: `Anji Anderson → Peter Sebeck [enemy]` (judge P≈0.69,
pack accepts only `other`) and, in the current sample,
`Cheryl Lanthrop → Matthew Sobol [other]` (an unjudged revival `other`; the pair
is extracted as `enemy`/`other`, never `colleague`).

Missing must-ship pairs and why:

| Pair | Why |
|---|---|
| `Gragg → Ross [enemy]` | never extracted (0 hits in 176 chapter claims) |
| `Sobol → Kirchner [enemy]` | never extracted |
| `Trear → Kirchner [colleague]` | never extracted |
| `Lanthrop → Sobol [colleague]` | extracted as `enemy`/`other` only |
| `Gragg → Sobol [ally]` | exists as a loser; the pair's winner (`child`, P=0.09) dies, revival picks `mentor` (P=0.03) which also dies → pair ships nothing |

Suite: `py -m unittest discover -s tests -q` → 2795 tests, 14 failures, all
pre-existing/environmental:

`test_corrupt_corpus_preserved_rename_in_place_open_none`,
`test_ingest_dir_isolates_failures`, `test_corrupt_corpus_is_unavailable_not_absent`,
`test_trope_chapters_built_without_embeddings`,
`test_isbn_book_replaces_exactly_as_today`, `test_resolve_provider_env_key`,
`test_resolve_provider_no_key_raises_clear_error`,
`test_series_roster_roundtrip`, `test_queue_stop_resume_end_to_end`,
`test_every_parser_dest_classified_exactly_once`,
`test_app_proposals_survive_reprocess`,
`test_flag_on_stages_then_promotes`,
`test_new_work_stages_under_fallback_then_adopts`,
`test_resolved_work_absent_claim_expires`.

They are config-dependent (a real `config.json` supplies Supabase/proposer values
the tests assume absent), Windows temp-file teardown (`WinError 32`), or the
in-flight `--apply-rel-types` work. **Do not "fix" them as part of this work.**

---

## 6. Experiment log (already tried — do not repeat)

| Change | Result | Kept? |
|---|---|---|
| Extractor: local gemma-3-4B | 68-entry roster with role nouns; relationships 0.33/0.08 | no |
| Extractor: Cohere command-a-plus | every call empty (reasoning exhausts 8k/16k/32k tokens) | no |
| Extractor: OpenRouter GLM flash | roster 43–97 names, grounded; best runs | **yes** |
| Prompt rule: off-page investigator/employer/adversary pairs | 130 claims (79 without evidence); precision 0.500, recall 0.154 | **reverted** |
| Evidence-first pair competition (evidence before confidence) | identical shipped set on both extractions | reverted |
| Closed-set type choice, pair rivals | zero retitles; 0.778/0.538 | reverted |
| Closed-set type choice, whole vocabulary | re-typed real hits away; 0.714/0.385 | reverted |
| Rival types in the judge's context | 0.700 → 0.778 precision, recall unchanged | **kept** |
| Non-family mid-band → `other` | converts five genuine hits to `other` | rejected (measured offline) |
| `enemy`-only mid-band → `other` | fixes Anji, costs `Gragg → Merritt` (must-ship) | rejected (measured offline) |
| `min_frequency` 0.05 | recall unchanged, precision 0.545, cast 22 | rejected |
| Preview revival + one row per pair + judged promotion | recall +1 pair, duplicates gone, cast 25–27 | **kept** |

---

## 7. How to reproduce

```powershell
cd C:\Users\kevst\ebook-processor

# 1. score the frozen extraction (fast, no model calls)
py score-gold.py --gold eval/daemon-labels.json `
  --run "<learned>\artefacts\analyze\<fp>\<newest analyze run>\analysis.json"

# 2. re-run the analysis phase only (≈80 s, judge calls only)
py process-portable.py --stage analyze `
  --file "import/Daemon - Daniel Suarez.epub" `
  --extraction "<learned>\artefacts\extract\2847ba64bb6969fc\extract-20261022-110407-15384" `
  --preview --llm openrouter --decision-model-provider openrouter

# 3. re-extract (≈30 min, charges OpenRouter) — only with permission
py process-portable.py --clear-checkpoints
py process-portable.py --stage extract --file "import/Daemon - Daniel Suarez.epub" --llm openrouter

# 4. regression
py -m unittest discover -s tests -q
```

Run `--stage analyze` on a **stable tree**: editing `process-portable.py` while
the suite or an analyze run is importing it produces phantom failures
(`inspect.getsource` returns one line, `_pair_losers` counts shift).

---

## 8. Open decisions for the next owner

1. **Preview family-downgrade type.** Preview records `downgraded` but keeps the
   claimed type; write rewrites it to `other`. Mirroring that too would make
   preview fully faithful (and needs `test_provenance_stamps.py:123` updated).
2. **Unjudged revival `other`.** A revived claim typed `other` is skipped by the
   validator and ships with no note. Either judge it (it costs one call) or
   ship it as `review_required`.
3. **Revival candidate quality.** `Gragg → Sobol [ally]` is never tried because
   revival takes the highest-ranked loser (`mentor`). Ranking losers by
   judge-plausibility, or trying the top two, would test that pair.
4. **Precision's last row.** `Anji → Sebeck [enemy]` sits at P≈0.69, just under
   the 0.70 keep line, and every threshold that removes it also removes a
   must-ship hit. The pack already declares `other` acceptable for that pair.
5. **Extraction gaps** (`Gragg → Ross`, both `Kirchner` pairs, `ally` for
   `Trear`/`Philips`): out of scope by Kevin's ruling, but they are the entire
   remaining recall gap.

---

## 9. File and test map

| File | Role |
|---|---|
| `process-portable.py` | the pipeline (`v2_reduce`, validators, filters, checkpoints) |
| `score-gold.py` | single-run gold scorer |
| `eval/daemon-labels.json` | frozen pack v1.2 |
| `daemon-gold-pack-v1-draft(1).md` | human pack (source) |
| `tests/test_roster_filters.py` | intake filter |
| `tests/test_gold_scorer.py` | scorer conventions |
| `tests/test_pair_evidence.py` | evidence preference, rival context, validators |
| `tests/test_runner_up_revival.py` | competition, losers, revival, one row per pair |
| `tests/test_extraction_preservation.py` | artefact + checkpoint review bundle |
| `tests/test_family_fail_closed.py`, `test_relationship_integrity.py` | family guard, preview==write |
| `tests/test_phase4.py` | quote gates, principal filter, preview decision |
| `tests/test_stage_artefact_contracts.py` | extraction stamp / manifest identity |
