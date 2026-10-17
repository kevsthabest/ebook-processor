# Relationship discovery, judge-budget governance, and referent provenance — spec (rulings adopted 2026-10-17)

Status: rulings K1–K6 adopted by the user 2026-10-17, informed
by two external reviews of the 2.13.16 tree and of the first
draft of this spec, each reconciled against the code and the
run-17 data before adoption. Nothing in this document is built
yet; §6 is the build order. The points NOT taken from the
external reviews are recorded in §10 so they are not
re-litigated.

## Motivation — four measured problems

1. **The judge budget has five consumers and no accounting.**
   One pool (`_MERGE_TIER3_CAP` = 50/book) is drawn on, in
   pipeline order, by: roster-merge Tier 3 judgments, family
   relationship validation, non-family relationship validation,
   the trigger-evidence gate, and the corpus quote-selection
   poll. Run history records how much was used, never who asked.
   Measured consequence (Daemon run 17): the pool exhausted
   during relationship validation, and every trigger-evidence
   quote in that run fell back to the regex gate (preview JSON:
   decision_gate = fallback ×36, none ×7, decision ×0) — the
   judge the trigger gate exists to use never ran. Content
   warnings are the reader-safety pillar; on relationship-dense
   books they are currently the weakest-judged output.
2. **Validation order is list order.** Nothing ranks claims by
   importance, so Daemon run 17 left 8 of its 19 principal
   relationships unjudged while minor claims consumed calls.
3. **Validation cannot surface unclaimed bonds.** The validator
   only judges pairs extraction proposed. Jon Ross ↔ Peter
   Sebeck end Daemon as allies; extraction proposed "friend"
   (judged 0.09, dropped) and never proposed "ally", though the
   type exists. Side characters are worse covered still: their
   bonds are proposed sparsely and judged last, if the budget
   reaches them at all.
4. **Chapter-level referent knowledge is discarded before
   judging.** Extraction reads a whole chapter and correctly
   anchors a surname ("Sebeck" in chapter 22 is Peter — his
   scene). Validation re-judges from one mined sentence plus a
   ±2-sentence window; the anchoring is gone, and the claim
   scores as if the surname might mean anyone (Chris → Peter
   child: 0.42–0.46, the accepted middle-band miss).

## §1 — Budget governance

### 1.1 Consumer accounting (build first; it measures the rest)

Every `_dm_budget_claim` call carries a consumer tag:
`merge_tier3`, `rel_family`, `rel_nonfamily`, `trigger_gate`,
`quote_poll`, and (when §3 ships) `discovery`. The run's
decision events and the run-history upload record, per consumer:
granted, denied, and calls actually spent. Run-history totals
gain the same breakdown per book. No behaviour change.

### 1.2 Validation priority order (K1 — ruled)

Within a book, relationship validation stops walking the stored
list order. Order is: family before non-family; within family,
by stakes (spouse/parent before sibling — their errors cost
more); principal pairs before minor pairs within each group.
Within a group, claims order by extractor confidence ascending
**provisionally**: both external reviews noted extractor
confidence is a formula, not a calibrated probability, so the
direction is re-decided from §1.1 data (verdicts vs confidence
across at least three accounted books) at a named checkpoint
after build step 2. Until then ascending stands, and the eval
report (§4) records which order produced each result.

### 1.3 Reserve slices (K2 — ruled: hard reserves)

Each consumer is guaranteed a minimum slice of the pool; a
consumer may borrow unused reserve only after every earlier
consumer in pipeline order has finished. One per-book spend
ceiling is kept. Because the trigger shutout is a measured,
live defect (Motivation 1), a **provisional trigger-gate floor
ships together with §1.1** (build step 2), sized at build time
from run-17's observed trigger demand and re-sized from §1.1
data thereafter — the program does not wait several gates to
stop a known harm. Rejected alternatives, for the record:
separate per-consumer pools (loses the single ceiling) and
shed-order only (leaves triggers unprotected).

### 1.4 The cap itself

50 is a guess from before accounting existed. Once §1.1 has
produced per-consumer data from a few real books, the cap (and
any slices) are re-sized from that data — not before. Budget
efficiency is reported both per book and per eligible claim, so
book length and claim density are not mistaken for waste.

### 1.5 Unverified claims (K3 — ruled: surface, no cap)

`budget_exhausted` and `validator_error` claims keep their type
and ship; there is NO per-book cap on them (a cap deletes true
claims on exactly the dense books that need them). Their state
must be visible wherever claims are presented for curation,
under the internal state names — validated / budget_exhausted /
validator_error / review_required / rejected — with the app
translating states into reader-appropriate language rather than
exposing the internal terms. Claim provenance (extracted /
discovered / human) is preserved on the claim through every
stage. Headline metric added to §1.1 reporting and run history:
**judged coverage of principal claims per book.**

### 1.6 Validator-error policy (K4 — ruled: retry + circuit breaker)

Neither plain fail-open nor write-mode fail-closed. A validator
error retries once per claim. If a book's validator-error rate
crosses a threshold (set at build time, recorded in the run
record), a book-level circuit breaker trips: that book's
replace-wholesale write is SKIPPED and its previous rows remain
untouched — the same principle as "aborted runs never write".
New or changed claims from a tripped book do not write; the run
records the skip and the error rate. A failed judgment is never
recorded as a successful one.

### 1.7 Trigger-gate demand reduction (experiment)

The trigger gate currently judges up to 3 evidence quotes per
category. Experiment, measured under §1.1 accounting: judge the
top quote per category first and escalate to further quotes
only when the first judgment is ambiguous (near its band
edge). Compare trigger outcomes (keeps / downgrades / discards
per category) against the current all-quotes behaviour on the
same books before adopting; demand reduction may free more
budget than any reserve scheme.

## §2 — Referent resolution as provenance

Goal: give the validator back the chapter's own anchoring —
as a fact about the text, never as the conclusion.

### 2.1 Mechanism

For each chapter, a deterministic antecedent pass tracks
surname-only and first-name-only mentions of roster characters:
the nearest preceding full-form mention in the same chapter,
and the chapter's focal character (the character the narration
follows, where extraction already identified one). A mention
RESOLVES only when exactly one roster candidate is supported
by the antecedent chain. Otherwise it stays unresolved and
nothing is asserted.

### 2.2 Provenance record

Claims gain endpoint-match provenance: for each endpoint, the
form that matched in the evidence sentence (full name / first
name / surname / resolved-surname) and, for resolved forms, the
basis — the resolving mention stored VERBATIM with its chapter
and position, not a paraphrase of the inference, so a false
resolution can be diagnosed later. Provenance is stored on the
claim and mirrored in run history. Pronouns never resolve in
v1: an endpoint matched only by a pronoun keeps today's
unresolved treatment.

### 2.3 Consumption

The validator's context gains one line only when resolution is
unique, e.g. "In this chapter, 'Sebeck' refers to Peter Sebeck
(the scene follows him)." Ambiguity stays silent — no hint is
better than a guessed hint. The miner's grading does NOT
change: A1's pinned trade-off (a surname sentence grades
binding for the pair without identifying which member anchors
it) stands at grading time. Resolution is a separate, stricter
layer that runs after mining.

### 2.4 The test pair

The eval gains the Chris pair as its referent test: Chris →
Peter (child) on the chapter-22 quote, where resolution MUST
fire (Peter is the scene's antecedent); Chris → Laura (child)
on the same quote, where resolution must NOT manufacture a
Laura antecedent from that chapter. Whether the Peter claim's
score then crosses the keep band is measured, not promised —
the accepted-miss status of the case is revisited only on
eval evidence.

### 2.5 Anti-circularity gate and adversarial cases

Resolution must be provably independent of the claim it
annotates: for every eval case, resolution output is asserted
UNCHANGED when the evidence sentence itself is removed from
the chapter text. The resolving mention may never be the
evidence sentence, nor any kinship/binding sentence — an
antecedent that itself states a bond would smuggle the
conclusion into the provenance. Adversarial cases join the
eval alongside the Chris pair: a scene transition within a
chapter; a point-of-view change; the nearest preceding
full-form mention being the WRONG same-surname character
(e.g. a Laura-focal scene where "Sebeck" in dialogue means
Peter). Uniqueness-or-silence governs all of them: when the
chapter does not decide, resolution says nothing.

### 2.6 Scope limits (v1)

Resolution is same-chapter only. It never crosses chapters, and
it is never used at extraction time to create claims — it
annotates claims that already exist. A wrong resolution is a
false-evidence risk, so the bar is uniqueness or silence.

## §3 — Relationship discovery (shadow first)

### 3.1 Candidate generation — all tiers, one-pass index

Per-pair rescans do not scale to the minor tier, so candidate
generation is built on a one-pass sentence index: for each
book, sentences are scanned ONCE for roster mentions and
bucketed by the character pair they mention (sentence-level
co-presence, not chapter-level — chapter presence is dominated
by protagonists and would rank every pair they touch first).
Candidate pairs are buckets with no surviving claim between
the two characters, in two tiers: T1 any pair involving a
principal; T2 minor ↔ minor pairs (the user's multi-fold
requirement — side characters' bonds are in scope by design,
not as a later extension). Pairs are capped per book by
bucket strength, strongest first.

### 3.2 Miner pre-filter (zero judge calls)

`_mine_pair_evidence`'s grading (`_pair_binding`) runs over the
bucket sentences of each candidate pair; only pairs with at
least one binding-grade sentence advance. This is what makes
T2 affordable: side characters cost nothing until the text
actually states a bond. First build task is a marker-coverage
audit: `_PAIR_MARKER_RE` and `_pair_binding` were built
kinship-first — measure their coverage of ally / rival /
mentor / friend language on labelled sentences before
extending them, with coverage reported PER RELATIONSHIP
CATEGORY (a kinship-strong miner can look healthy on a
family-heavy benchmark while missing every friendship).

### 3.3 Ranking aid (experimental, one variable)

Binding sentences for a surviving pair may be ranked two ways:
the miner's existing order (grade, earliest chapter, shortest)
or embedding similarity to relationship-dynamic queries. Both
arms run in shadow mode and are compared on the §4 benchmark;
the loser is dropped. Embeddings never become a confidence
score — ranking only.

### 3.4 Discovery judging — one closed-set call, then the gate

For discovered candidates, the discovery judgment is a SINGLE
closed-set call (not two): "which of these types, if any, do
these sentences establish between the two characters?" with an
explicit "none" outcome and the full vocabulary offered. The
call receives up to two binding sentences from DIFFERENT
chapters when they exist, so the judgment is not hostage to
one sentence's phrasing. A discovered claim then faces the
standard validator against its best sentence before it is
writable — the validator remains a separate, independent
gate. Discovery proposes; the existing gate disposes. (The
extracted-claim pass keeps its current single-question form;
nothing here changes it.)

### 3.5 Shadow mode and promotion (K5 — ruled, strengthened)

Discovery ships in shadow mode first: it emits a report section
in preview output and writes nothing. Promotion to writable
requires ALL of: at least 30 pooled shadow keeps across at
least two books, at least one of them not by the two authors
the eval already covers; curator-blind labelling of those keeps
(the curator scores without knowing which stage produced them);
precision ≥ 90% scored on TYPE-CORRECT keeps (a real bond with
the wrong type does not count as a hit); recall against the §4
benchmark's known omitted bonds reported alongside (no fixed
bar — the number is read, not gamed); no regression in the
existing extraction and validation gates; and the §1.6 circuit
breaker in place, since discovery multiplies the partial-
failure surface. Promotion grants a FLAG (default off), not
default-on — enabling by default is a separate, later
permission on production precision data. On promotion,
discovery writes carry provenance `source: "discovered"` + the
§2.2 endpoint-match record. Replace-wholesale treats
discovered claims as processor claims; human confirmations and
rejections stay protected. Rollback path: flag off restores
extraction-only output; discovered claims already written are
processor claims and are replaced on the next run.

### 3.6 Budget

Discovery judging draws on the shared pool under its own
consumer tag (§1.1), last in priority, after validation and
the trigger reserve are served. In shadow mode it may use a
separate experimental allowance so measurements do not starve
production consumers — the allowance is reported, not hidden.

### 3.7 Evidence doctrine (K6 — ruled: binding-grade only)

Discovery's evidence bar is the programme's standing doctrine:
only syntactically binding evidence may write a relationship;
co-occurrence never does. The motivating case is therefore a
RECORDED UNREACHABLE, on evidence: Daemon contains 64 sentences
naming both Jon Ross and Peter Sebeck, and not one states a
bond — their alliance is enacted across scenes, never stated
in a sentence (probe run 2026-10-17 against the book text).
Discovery will recover bonds that ARE stated somewhere and
missed by extraction (the expected case for minor pairs and
non-family types); enacted-but-unstated bonds are a named
frontier, not a silent gap. A second, weaker evidence class
(multi-passage pattern evidence, quarantined to curation) was
considered and is OUT of this programme; it may be proposed
later as its own design, with this probe as its first exhibit.

## §4 — Evaluation upgrades

1. **Repeated draws.** Each gated case runs 3 times (5 for
   cases within 0.15 of a band edge); a case passes only if
   every draw lands in band. Per-case spread is reported —
   measured judge variance is ±~0.1, and single-draw passes at
   0.33 vs a 0.30 threshold are coin flips today. A case whose
   spread STRADDLES a band edge is reported UNSTABLE rather
   than pass/fail: the instability itself is the finding.
   Model variance and benchmark-coverage uncertainty are
   reported as separate things — repeated draws measure the
   first, not the second.
2. **Four-metric reporting.** Every eval report shows
   precision, recall, type accuracy, and abstention, each with
   its case count, per book and pooled — replacing the single
   pass count.
3. **Benchmark growth and stability.** Before discovery
   promotion, labelled cases from at least two further books
   join the fixture, including known omitted bonds (discovery
   positives), known non-bonds with heavy co-presence
   (discovery negatives), and the §2.4 referent pair. Cases
   are never silently retired: a fixed failure stays in the
   set, and any reclassification is recorded with its reason
   (the acceptance-note practice already used for the Chris
   and Liam cases is the model).

## §5 — README corrections (rides with the first build)

- Version line: replace the hand-maintained string with a test
  asserting the README version equals `PORTABLE_VERSION`, so
  drift fails the suite instead of misleading reviewers
  (it read 2.13.5 at the 2.13.16 review).
- Relationship status: relationships ARE written — embedded in
  `book_characters` rows; the archived artefact was the
  standalone `character_relationships` table (2026-10-09).
- Validation scope: non-family validation shipped in 2.13.8;
  the README's family-only wording is stale.

## §6 — Implementation order (each step gated separately)

1. README corrections + version-drift test.
2. §1.1 consumer accounting + run-history breakdown, WITH the
   §1.3 provisional trigger floor (K2's sequencing amendment:
   the measured trigger harm does not wait for step 4).
3. §4.1–4.2 eval repeated draws + four-metric report.
4. §1.2 priority order + §1.3 full reserves + §1.7 trigger
   demand experiment + §1.6 circuit breaker.
5. §2 referent resolution + the §2.4/§2.5 eval cases.
6. §3 discovery shadow mode (one-pass index, marker audit,
   benchmark).
7. Discovery promotion — the user's decision on §3.5 evidence.

Steps 1–3 are independent and may be built in parallel;
4–6 are serial through their gates.

## §7 — Rulings (adopted 2026-10-17)

- **K1** — priority order: family > non-family, stakes within
  family, principals before minors; confidence direction
  provisional pending calibration data (§1.2).
- **K2** — hard reserves with controlled borrowing;
  provisional trigger floor ships with accounting (§1.3).
- **K3** — unverified claims surface with their state; no
  per-book cap (§1.5).
- **K4** — per-claim retry, then a book-level circuit breaker
  that skips the book's write (§1.6).
- **K5** — promotion: ≥30 pooled keeps, ≥2 books, curator-blind,
  type-correct precision ≥ 90%, flag before default (§3.5).
- **K6** — discovery evidence is binding-grade only; enacted
  bonds are a recorded unreachable in this programme (§3.7).

## §8 — Non-goals

- No change to extraction models or prompts in this programme.
- No cross-chapter or cross-book referent resolution in v1
  (§2.6) — the sequel-evidence question stays a separate,
  later design.
- Discovery never writes in shadow mode, and never bypasses
  the standard validator after promotion.

## §9 — Known limitations recorded during review

- **Character-claim preservation is name-matched.** Verified
  in code during review: trope verdicts anchor on the
  catalogue id when resolved (renames cannot orphan them) and
  triggers on the warning; relationships are not in the
  staging/verdict system at all in v1 (endpoint identity is
  the unsolved roster problem, per the staging code's own
  note). Human edits to CHARACTERS, however, are preserved
  across replace-wholesale by name within the work — a roster
  reconcile that merges or splits a character can therefore
  orphan a human character edit. Recorded as a standing
  limitation of the curation guarantee; fixing it is its own
  design (stable character identity), outside this programme.
- **Type granularity is untouched.** The programme's non-goal
  "no extraction prompt changes" leaves the foster-sibling vs
  friend boundary (the Liam case) exactly where it was: a
  recorded accepted miss, not a solved one.
- **Genre coverage.** Thresholds and markers are tuned on a
  techno-thriller and a fantasy romance. §3.5's second-author
  requirement mitigates discovery promotion only; validation
  thresholds carry the same exposure, unmeasured for other
  genres (romance in particular).

## §10 — External-review points NOT adopted (for the record)

- "Relationships are not written to the app" (OpenAI review):
  incorrect — see §5; the archived table misled the reviewer
  via our own stale README line.
- "Validator errors can pass as validated" (OpenAI review):
  errors already record an explicit `validator_error` state
  with no score; the live question is policy only (§1.6).
- "Gender/roster elimination resolves the Sebeck surname"
  (Claude review): the sentence does not gender the parent and
  two roster Sebecks are plausible referents in the abstract;
  §2 resolves by chapter antecedent, not by elimination, and
  stays silent where the chapter does not decide.
- Raising the judge-call cap as a first move (both reviews
  gesture at it): rejected until §1.1 accounting shows where
  calls actually go (§1.4).

## Addendum note (2026-10-17)

Parked idea, recorded at the user's direction — NOT part of
this programme: a relationship claims TABLE (rows keyed by
stable character IDs, types held in a shared types table),
with book × claim matrices as the analytics surface. It would
address the §9 name-matching limitation and give K7-style
drift enforcement for relationships, at cross-repo cost (the
app reads relationships from character JSON today). A
relationship table existed once and was archived 2026-10-09;
its history should shape any revival. Revisit only at the
user's initiative.

## Addendum — relationship type registry (ruled 2026-10-17)

The user ruled: add the missing types, AND make types addable
without a code change — the Fourth Wing preview (run 23) showed
why: dragon-rider bonds were typed spouse/sibling/friend and
all dropped, because the vocabulary has no bond type and the
judge correctly rejected the force-fits.

Design: a `relationship_types` registry table in Supabase —
the same pattern the app already uses for `trigger_taxonomy`
(seeded, curated, DB-held) — read by BOTH the processor and
the app, so the vocabulary has one source of truth. Each type
carries: id, label, a one-sentence gloss (injected into the
extraction and validator prompts, so a new type is judged on
its definition, not its name), direction (symmetric or
directed), family flag (routes validation), exclusivity
(exclusive with a precedence rank, or a non-exclusive extra),
app graph group, and whether readers may suggest it (flywheel
allowlist derives from the same rows). Extraction prompts,
the validator prompt, the reduce lattice, the app graph map,
and the suggestion allowlist all derive from the registry —
adding a type becomes inserting a row, not editing four
places in two repos.

First entry: **bonded** — "a supernatural or formal bond
linking the pair (dragon and rider, familiar and keeper): a
persistent, named connection, not mere companionship."
Symmetric; not family; exclusive, precedence alongside
romantic (above friend/colleague). Validated as a non-family
claim at the standard bar.

Build position: after steps 1–3, alongside steps 4–6, and in
any case BEFORE any Fourth Wing write reprocess — the book
cannot be written well until the type exists and extraction
proposes it. The processor falls back to its built-in type set
when the registry is unreachable (offline runs must not gain
a hard network dependency for vocabulary).

### Registry seed: prefill set (ruled 2026-10-17)

When the registry is built, seed it with the current vocabulary
plus these nine types (glosses are seed text; each states its
boundary against its nearest neighbour). step_parent ships
with its directional pair step_child, as parent does with
child.

- bonded — symmetric; not family; exclusive, precedence at
  romantic's rank. "A supernatural or formal bond linking the
  pair (dragon and rider, familiar and keeper, human and AI
  companion): a persistent, named connection, not mere
  companionship or affection."
- mate — symmetric; not family; exclusive, precedence at
  bonded's rank. "A fated supernatural bond chosen by forces
  outside the pair (a mating bond): may exist unaccepted, and
  is distinct from marriage, ordinary romance, and
  friendship."
- sire — directed (sire -> made); not family; exclusive,
  precedence at parent's rank. "The being who turned the
  other (vampire or similar): a maker-made bond of authority
  and obligation; not a biological parent."
- packmate — symmetric; not family; non-exclusive extra.
  "Members of the same pack: a group-loyalty bond of mutual
  obligation and hierarchy, distinct from friendship and
  family."
- crewmate — symmetric; not family; non-exclusive extra.
  "Members of the same ship's crew: a working bond of daily
  proximity and mutual survival — more than a colleague, not
  necessarily friendship."
- sworn — either direction; not family; exclusive,
  precedence at bonded's rank. "Bound by a spoken oath —
  fealty, a blood oath, sworn brotherhood or service: the
  oath itself is the bond, distinct from loyalty that was
  never sworn."
- step_parent / step_child — directed; FAMILY (routes to
  family validation); exclusive, precedence at parent's rank.
  "A parent's spouse who is not the biological parent: judge
  the marriage-created kinship on its own terms; a
  step-parent claim is not a parent claim."
- ex — symmetric; not family; exclusive, precedence between
  romantic and friend; app graph group romance. "A former
  romantic partner or spouse: the relationship is in the
  past — judge the history the text establishes, not
  present-day warmth or hostility, which belong to friend or
  enemy."
- handler — directed; not family; exclusive, precedence at
  colleague's rank. "An intelligence handler and their asset:
  a directed bond of tasking, trust, and control, distinct
  from a colleague and from a mentor."

Watchlist (demand-driven, NOT seeded): betrothed, in-law,
roommate, captor, thrall, commanding officer, police partner
(name collides with the app's romantic "partner").

### Registry: built (2026-10-17, processor v2.13.21)

The registry addendum above is implemented. The
`relationship_types` table is created and seeded by the app repo's
migration v614 (current thirteen types with their properties
derived from the code as it stood, plus the ten prefill rows —
the nine ruled types with step_parent/step_child as the directed
pair). The processor loads the registry at run start through its
existing Supabase read path; on any failure it falls back to a
built-in vocabulary that mirrors the seed exactly, records the
fallback and its reason in the run record's thresholds stamp and
on each reduce result, and never raises — offline runs gain no
hard dependency. All consumers (extraction prompts, the validator
prompt with each claimed type's gloss, the reduce precedence
lattice, family routing, the vocabulary validity check) derive
from the single vocabulary object the load installs, proven by a
synthetic-type test: a type present only in a fixture registry
payload reaches every consumer with no code change. The app loads
the same rows and derives its graph groups and suggestion
allowlist from them, with today's hardcoded map kept as the
fallback. Seed judgment calls made at build time and recorded
here: ex takes precedence 3 (the addendum places it between
romantic at 3 and friend at 4; the column is an integer, and ex
shares romantic's rank as the same bond at a later time);
step_child's gloss is the reciprocal of the ruled step_parent
gloss; ally and other seed as suggestible=false (neither is in
the app's reader-facing allowlist today); graph groups for
prefill types follow their nearest seeded neighbour (bonded/mate
→ romance, sire → mentor, handler → friend as colleague's group,
packmate/crewmate/sworn → friend, step types → family).

## Errata — 2026-10-17: corrections to the phase-2 interim record

The phase-2 releases (2.13.20–2.13.24) shipped gated and
remote-verified, but several particulars in the interim reports
were written from texts delivered while builders were still
amending. The coordinator's corrective addendum is the source;
this errata supersedes the interim record in the particulars
named, and rides with the run-history migration change
(Decision, 2026-10-17) rather than an ungated docs commit.

- Release B (type registry): the gate on the reviewed tree
  initially returned FAIL on one blocker — a stale generated
  module map in the app repo. The final commit regenerated the
  map (module-map suite 10/10 on the final tree); the
  final-tree verdict is PASS_WITH_RISK.
- Release B, app side: derivation in v614 is partial. The Char
  Lab picker consumes the registry opportunistically; the
  reader suggestion dialog still hardcodes the previous type
  list. Both paths fail safe to today's behaviour.
- Release B, comparability: under 2.13.21 the extraction and
  validator prompts gain the type-gloss definitions even when
  the registry read falls back to the built-in vocabulary, so
  2.13.21 runs are not byte-comparable to 2.13.20. That is the
  ruled gloss mechanism working, not a defect — but any
  claim of byte-identity between those versions is wrong.
- Release C (referent resolution): endpoint provenance is NOT
  copied into stored book_characters entries. It lives on the
  in-run claim and in the run-history mirror — a durability
  gap recorded as gate risk R2, not a compatibility break.
- Release D (discovery shadow): the allowance is a separate
  experimental budget of 40; the shared decision pool is never
  claimed. The shadow section's shared_pool_spent: 0 figure is
  therefore structural, not instrumented. And §3.3 was not
  deferred: both ranking arms are built, the embedding arm
  behind its own default-off flag.
- Release 2.13.24: the suite is 1841 (four new tests), and the
  R1 carriers are the skip_reason column plus the
  validator_breaker_* keys inside the characters jsonb — not
  thresholds_json, as one interim record sentence said. The
  gate confirmed the fix end-to-end; R1 stays closed.
