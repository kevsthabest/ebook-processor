# Option F — empty-evidence staging gate + persistence gate: design

Status: DESIGN ONLY (no code). Security Advisor red-team complete:
PROCEED-WITH-CHANGES, amendments folded in (marked "red-team fold-in"
inline; OC-7 elevated to a requirement). Awaiting user rulings on the
§9 open choices as amended. Rollout model follows the
`--roster-reconcile` precedent: named flag, default OFF, flag-off = no
code path and no preview key.

Doctrine, stated once and binding on everything below: **persistence is a
stability filter, not truth.** A promoted claim is a claim that stopped
flickering, not a claim that was verified. Promotion never raises a
claim's confidence, never applies to family relationships, and never
overrides a denylist, a rejection, or a human curation state.

---

## 0. What the code actually looks like today (evidence base)

- **Trope claims carry no quote evidence at all — by construction.**
  Extraction produces per-chapter candidates (Call B `trope_candidates`);
  `v2_trope_gate` (process-portable.py:5195) confirms/denies them against
  chapter *summaries* in one LLM call. The shipped DB row
  (`_build_trope_rows`, :7835) has `evidence = {source, chapters,
  chapter_count}` — chapter references only. There is no quote field
  anywhere in the trope path. "Chapter-refs only" is not a defect subset
  of trope claims; it is the shape of every trope claim.
- **Trope confidence does not encode evidence quality.** Auto-band and
  gate-band claims use the *same* formula — `min(0.9, 0.55 + 0.1 *
  chapters)` (auto: :9408; gate: :5195). A 2-chapter summary-judged claim
  and a 2-chapter frequency claim are indistinguishable by confidence.
  The only evidence-strength signal in the trope path is **chapter
  support + band**: auto band is `n_ch >= auto_min` where
  `auto_min = max(3, n // 20)` (:9362); the gated band is everything
  below it that survives (2..auto_min−1 chapters, or 1 chapter in short
  books).
- **Trigger claims are a different shape.** They DO carry quote evidence:
  `_build_trigger_rows` (:7883) ships `evidence.quotes` from the
  extraction, and already zeroes confidence when the evidence list is
  empty. An evidence-less trigger is therefore a clean, already-computed
  signal — no new instrumentation needed to detect it.
- **Relationships already have the strict version of staging.**
  Evidence-less relationships never reach the DB: `_build_rel_map`
  (:7918) skips them ("kept in preview") and family claims additionally
  fail closed at validation (`no_evidence` drop). They have no promotion
  path and v1 must not give them one (see §7).
- **The dashboard's empty-evidence metric is relationships-only.**
  `preview_variance.empty_evidence_rates` (:578) iterates
  `d["relationships"]`. There is no trope-side evidence-poverty metric
  today; §6 adds one rather than pretending the hook exists.
- **Review-queue surfaces** (what staging must keep claims out of):
  `book_trope_claims` / `book_trigger_claims` rows with
  `status="candidate"`, and `trope_proposals` rows with
  `status="pending"` (`_build_proposal_rows`, :7955). The app reads the
  same tables; there is no second filter downstream.
- **Curation anchor:** `_partition_claims` (:7776) preserves
  rejected + confirmed rows through replace-wholesale and dedupes new
  claims against them. Anything staging ships later re-enters through
  `write_claims`, so a curated claim can never be resurrected or
  reverted by a promotion — the skip set already guarantees it.

---

## 1. Scope — which claim classes enter staging in v1

**Recommendation: tropes (gate band only) + triggers (evidence-less
only). Relationships are out.**

| Class | Stageable when | Why |
|---|---|---|
| Tropes | Candidate survived the gate in the **gated band** (`n_ch < auto_min`) and passed denylist + rejection-learning filters | This is the measured queue cost: the Trope Review seat's abstentions (14–18 of 20) are claims judged from summaries with chapter-refs-only evidence. Auto-band claims carry frequency corroboration (≥5% of chapters) and never stage. |
| Triggers | The claim's evidence (quotes) list is empty — the exact condition that already zeroes its confidence in `_build_trigger_rows` | A quote-less trigger asks a reviewer (or the app) to trust an unverifiable safety claim. Triggers *with* quotes never stage. |
| Relationships | **Never in v1** | Evidence-less relationships already never ship, and family claims fail closed. Giving them a persistence-promotion path would *weaken* existing doctrine, not add a gate (§7). |
| Characters / quotes / spice | Never | Not claims in the review-queue sense; out of the finding. |

Ordering inside the pipeline matters: staging eligibility is computed
**after** the denylist filters and `_apply_trope_rejections` (:5251,
called :9418). A denylisted or rejection-filtered claim is *dropped*, as
today — staging never shelters a claim an existing hard filter killed.

**The one-and-done tension, stated plainly:** the user's normal workflow
processes a book once. A persistence gate means a first write-mode run
ships auto-band tropes + evidenced triggers only; gate-band tropes ship
on the book's *second* independent run. That is the option working as
designed (it is why two external reviews ranked F first/second), but the
cost is real and §6's validation gate must measure it, not discover it
after shipping. Mitigations built into the semantics: support growth
promotes within the persistence rule (§4), and the trapped report makes
the held-back set visible after every run.

---

## 2. Staging store

**Recommendation: a local JSON store, `staging.json`, in the existing
learned-state directory (`~/.ebook-processor/learned/`), managed by a new
`StagingState` class that reuses LearnedState's persistence pattern — NOT
a new LearnedState file, and NOT a DB status.**

Why local:

- **App contract.** Staged claims must never render. If staging were a
  DB `status="staged"`, every app read of the claims tables and every
  review-queue query would need to learn to exclude it — a contract
  change in a second codebase, with a silent-render failure mode if any
  query misses it. A local store makes non-rendering *structural*: the
  rows are never written.
- **Review-queue contract.** Same argument for `trope_proposals` and the
  candidate sets: absence is guaranteed by never inserting.
- **Replace-wholesale semantics.** `write_claims` deletes and re-inserts
  per section with `_partition_claims` guarding curation. A DB-side
  staged row would sit inside that partition logic (deletable?
  preserved?) and need new rules in both `write_claims` and
  `plan_replace`. A local store leaves the write path untouched: staged
  claims simply aren't in the result the builders consume (§5).
- **Cross-machine reality.** Processing runs on the user's PC; the
  learned state already lives there and already persists across runs.
  The staging store has exactly one writer in practice.
- **F8 lessons, applied, not relearned.** Atomic save (temp file +
  `os.replace`, as `_save_json` :2723), loud corruption warning naming
  the file, original bytes preserved as `.corrupt` backups before any
  overwrite (`_note_corrupt` / `_preserve_corrupt`, :2638/:2661), and a
  summary line that admits state was not carried over. Reuse the pattern
  by extracting the three helpers or by subclassing — implementation
  choice, but the behaviour is not negotiable.
- **It is not LearnedState.** Learned state is cross-book *learning*
  (nicknames, rejection counts) and is the subject of open decision D1
  (learning governance). Staging is per-work *run bookkeeping* with the
  opposite lifecycle (entries expire; learning accumulates). Putting it
  in a LearnedState file would entangle F with D1's ruling. Separate
  file, separate class, same directory, same corruption discipline.
  **Concurrency (red-team fold-in, binding):** books run concurrently
  under `--jobs` (ThreadPoolExecutor), so `StagingState` serializes
  observe+save under a lock, reusing the LearnedState pattern including
  its RLock lesson (sweep F8) — or observe calls are serialized at the
  seam. Concurrent load-modify-save without a lock would lose
  sightings (last writer wins): delay-only, not a safety hole, but
  promotion timing would go nondeterministic and the accounting could
  silently undercount in production-shaped runs.

**Runner-up: DB-side `status="staged"`.** It wins the day processing
runs on more than one machine against the same library, or the user
wants a "pending evidence" surface in the app — both make local state
the wrong home. Neither is true today. If either becomes true, the
migration is one-directional (local entries upload as staged rows) and
nothing in this design blocks it.

Rejected: preview-only staging (derive state by diffing preview pairs).
No memory across non-adjacent runs, no aging, no account of books never
reprocessed — it cannot produce the "what's trapped" report, which is a
core requirement, not a nice-to-have.

Store shape (one file, keyed by work, then by claim key — §3):

```json
{ "version": 1,
  "works": {
    "<work_key>": {
      "work_id": "a42e27eb-…", "title": "Fourth Wing",
      "runs_completed": 4,
      "claims": {
        "trope:id:<catalog-uuid>": {
          "class": "trope", "display": "Enemies to Lovers",
          "name_forms": ["Enemies to Lovers"],
          "sightings": [1, 3], "first_seen": "2026-10-14",
          "last_seen": "2026-10-21", "best_chapters": 4,
          "evidence_ever": false, "state": "staged",
          "promoted_run": null },
        "trigger:non_consent": { "class": "trigger" }
      },
      "expired_recent": [ {"key": "…", "expired_run": 4,
                            "sightings": 1} ]
    } } }
```

`work_key` = `work_id` when resolved, else `isbn:<isbn>`, else
`title:<norm title>|<norm author>` (the same fallbacks `_proposal_state`
already uses for book identity). `sightings` stores per-work **run
ordinals**, not counts — see §3 for why that makes identity folding
exact. `expired_recent` is capped (200/work, FIFO) so expiry is never
silent but the file stays bounded.

---

## 3. Claim identity across runs

A false identity match is the precise failure to avoid: it promotes a
claim that was never persistent. Identity is therefore defined
conservatively — when in doubt, two keys stay separate (a split costs a
delayed promotion; a merge costs a false one).

**Tropes.** Key = `trope:id:<catalog trope_id>` when the claim resolves
to a catalog entry this run (via `result["trope_catalog_map"]` or
`_trope_lookup` — the same resolution `_build_trope_rows` performs);
otherwise `trope:name:<_tnorm(name)>`. The catalog id is the anchor: it
survives the name-form flips that the A measurement documented for
characters ("Violet" vs "Violet Sorrengail") because paraphrases
collapse to one id through the catalog map. `_tnorm` (:7377 — lowercase,
underscores→spaces, tidy) handles case/underscore/spacing variants of
unmapped names.

- **Folding.** When a name-keyed entry's claim later resolves to a
  catalog id, the name entry folds into the id entry:
  `sightings = union(ordinal lists)` — exact, because ordinals are
  per-work and each run contributes at most one ordinal per claim under
  exactly one key form. Never sum counts (the same run could otherwise
  be counted twice across a migration boundary).
- **False-match risks, named.**
  1. *Wrong catalog mapping.* The embedding/exact mapping sends a name
     to the wrong trope id; sightings then accrue to a claim that was
     never made. Mitigation: the id entry records every `name_forms`
     seen under it; the trapped report flags an id entry whose name
     forms differ beyond `_tnorm` equality, so a suspicious promotion is
     visible, and a promotion whose triggering sighting arrived under a
     *new* name form is marked `form_changed: true` in the preview
     marker. (A stricter rule — reset sightings on form change — is
     OC-6.)
  2. *`_tnorm` collision* between genuinely different unmapped names
     (e.g. punctuation-only differences). Bounded by the denylist
     pre-filter and by the fact that unmapped claims additionally face
     the proposal confidence bar before they can trouble the queue.
  3. *Per-work scoping.* Keys live under a work; the same trope in a
     different book is a different claim with its own sightings.
     Cross-book repetition NEVER promotes a per-work claim.

**Triggers.** Key = `trigger:<warning.lower().strip()>`. The warning
vocabulary is semi-controlled (the decision gate already works in
canonical snake_case categories — `non_consent`, `graphic_violence`,
… in live run logs), so collision risk is low; free-form variants
normalize by lower/strip only. No folding machinery in v1.

**Relationships.** No identity key is defined in v1, deliberately.
Endpoint identity is the roster form-flip problem, unsolved until
option B's stable IDs; inventing a name-based key here would import
exactly the false-match risk this section exists to prevent.

---

## 4. Promotion + aging semantics

State machine per claim entry: `staged → promoted` (terminal per work),
`staged → expired` (terminal, recorded in `expired_recent`).

**A sighting counts only when all of these hold:**

- the run completed the book (not aborted — an aborted run's partial
  extraction is not an observation);
- the run was a **fresh extraction**: `--resume` and `--retry-failed`
  runs do NOT increment sightings. A resumed run re-derives from the
  same checkpointed extraction — counting it would manufacture
  persistence out of one observation (the −15 resumed run is the
  concrete case). Resumed runs may update `last_seen` metadata but
  never `sightings`. Implementation: `process_file_v2` knows how many
  chapters were checkpoint-loaded vs freshly extracted; the observe
  step receives an `independent: bool` (true iff no chapter payloads
  were reused);
- the staging flag was ON for that run (flag-off runs are invisible to
  the store in both directions — §5).

**Promotion (any one suffices), evaluated at observe time:**

1. **Persistence:** `len(sightings) >= 2` (two independent runs).
2. **Support growth (tropes):** a sighting whose `n_ch >= auto_min` for
   that run's book — the claim grew into the frequency-corroborated
   band. This is evidence arrival in the only currency tropes have.
3. **Evidence arrival (triggers):** a sighting whose extraction carries
   a non-empty quotes list. (For tropes there is no quote currency;
   support growth is the analogue. If option G later gives tropes real
   passage evidence, it slots in here as a fourth rule.)

**What promotion does:** the claim joins the ship set for that run
(§5) with its **unchanged** pipeline confidence — the formula value,
never boosted — and the preview marker records
`promoted: [{name, via: persistence|support|evidence, sightings: N}]`.
The shipped DB row is an ordinary candidate; its evidence dict gains
one transparency key, `"persistence": {"sightings": N, "via": …}` —
this stamp is a **requirement, not an option** (OC-7, elevated by the
red-team: §7's containment claim depends on it — without the stamp, a
promoted claim in the review queue is indistinguishable from a natural
candidate). On every user-facing surface the word "promoted" is always
paired with its `via` mechanism; the word "verified" appears nowhere.

**Stickiness / oscillation.** Promotion is sticky per work: once
promoted, later weak sightings ship (the claim's standing was earned;
re-staging it would recreate the ship/stage flicker staging exists to
damp). A promoted claim that later stops being extracted is ordinary
replace-wholesale churn — absence is not demotion. The only exits from
promoted are the pre-staging hard filters (denylist, rejection
learning), which run before staging on every run and therefore apply
to promoted claims exactly as to any other.

**Flicker-down (red-team fold-in — stated here because it is the one
place staging can remove a previously-shipped claim).** Observe tracks
only stageable claims, so a claim first sighted in the AUTO band ships
with no store entry. If a later run extracts it in the GATE band, it
stages as a first sighting: its previously-shipped row is held back
(replaced away by that write run) and persistence restarts from that
run. That is the gate working — flicker damping cuts both ways — and
it is the conservative direction, but it is a real removal, visible in
practice via the summary-gated share and queue churn (§6).

**Aging / expiry.** Expiry is driven by *absence evidence*, not the
clock: a staged entry expires when **3 subsequent completed runs of the
same work** pass without sighting it (`runs_completed - max(sightings)
>= 3` while `state == staged`). A book that is never reprocessed
accrues no absences, so its staged claims wait — correctly: no new
information has arrived, and the trapped report (§6) ages them visibly
with a `STALE` flag at 180 days since `last_seen`. Expiry moves the
entry to `expired_recent` (bounded, reported once in the next trapped
report) — never a silent delete.

**Curation interaction (hard rules):**

- A claim whose DB row is human-**confirmed** or **rejected** is never
  staged, demoted, or expired — `_partition_claims`' preserved set is
  consulted by the write path as today, and the staging split (§5)
  operates on the *new run's* claims only. If a human rejected a claim
  in the DB, rejection learning (raised threshold / denylist) filters
  it before staging ever sees it; if a human confirmed it, the
  preserved row stands and the new-run duplicate is deduped against it
  as today.
- Nothing in the staging store is curation. It records observations,
  not verdicts; no human action is ever inferred from it.

---

## 5. Pipeline integration

**One seam, deliberately.** The split happens once, in `process_file_v2`,
immediately after the trope gate + `_apply_trope_rejections` block
(:9403–:9423) and the trigger finalization, before `result` assembly
(:9428). A single helper:

```python
staging_observe(result_parts, state, *, independent, mode)
    -> annotates ship/stage split + store updates
```

- `result["tropes"]` / `result["triggers"]` become the **ship set**
  (staged claims removed). Every downstream consumer — `write_claims`,
  `plan_replace`, `save_preview`, `cmd_push_preview`, the dashboard —
  then inherits the split with zero parallel logic. This is the F17
  lesson applied prospectively: there must not be a second, gated copy
  of the decision for `plan_replace` to understate. `plan_replace`
  additionally reports held-back counts from the marker (below) so the
  plan Kevin reads shows `would_insert` AND `held_back (staged)`.
- Staged claims are **not** passed to `_build_proposal_rows` either: an
  unmapped gate-band trope is held whole (no candidate row, no
  proposal). On promotion it re-enters both paths normally.
- `result["staging"]` marker, present **only when the flag is on**
  (the `roster_reconcile` marker discipline, :9484):
  `{staged: [{key, display, class, sightings}], promoted: [{key,
  display, via, sightings}], trapped_total: N, expired_this_run: M}`.
  `save_preview` prints a one-line summary in the `.md` ("Staging: 7
  held back, 2 promoted (persistence), 41 trapped across all works")
  and `_preview_summary` gains the held-back count. Preview ≡ write:
  a preview run performs the same observe + split, so what a preview
  shows as staged is exactly what a write run would hold back.
  A flag-on preview also declares its state write on its face
  (red-team condition on OC-4 — the F18 doctrine: a preview that
  writes state and doesn't say so is not a dry run): the `.md`
  summary gains "this preview recorded N sightings."
- `cmd_push_preview` on a preview saved WITHOUT the flag ships its
  full recorded set (nothing was staged — flag-off semantics exactly);
  on a flag-on preview it ships the recorded ship set regardless of
  later store state, so store loss can never resurrect staged claims
  into a push (red-team fold-in: stated because every reader asks).

**Flag / config rollout.** `--staging-gate` → `CONFIG["staging_gate"]`,
default OFF, not in built-in profiles, argparse + CONFIG wiring exactly
as `--roster-reconcile` (:10734). The observe step also records
sightings in **preview mode** — persistence can never accumulate if
only write runs count, and preview runs are the program's measurement
instrument (both A-experiment runs were previews). This is flagged as
OC-4 and framed precisely (red-team fold-in): staging sightings are
**independent of D1's subject matter** — learning is cross-book state
that shapes future *extraction*; sightings are per-work bookkeeping
that only filters the *ship set* of what a run itself produced, and no
path exists where preview sightings alone ship anything. The defence
is disclosure + boundedness, not exemption: flag-on is an explicit
opt-in, the store can only delay (never create output), previews
declare recorded sightings on their face (§5), and deleting
staging.json resets everything harmlessly. **If D1 rules broadly on
preview state-writes, OC-4 reopens.**

**Write-mode behaviour.** A write run ships promoted + never-staged
claims through untouched `write_claims`; staged claims are absent from
the result, so replace-wholesale simply doesn't re-insert them (their
previous candidate rows, if any, are replaced away like any claim that
stops being produced — staging introduces no new delete semantics).
`cmd_push_preview` on a saved preview pushes the ship set the preview
recorded — consistent by construction.

**Never-reprocessed books.** Their entries sit in the store, visible in
every trapped report with age in runs (0 absences) and days, `STALE`
after 180 days. Nothing expires, nothing ships, nothing is forgotten
silently — the report is the answer to "what happens": they wait,
on the record.

**Legacy pipeline.** Out of scope (v2 only, like the gate itself).
The legacy path's `write_claims` call (:6749) is unaffected.

---

## 6. Measurement

**"What's trapped" report — `--staging-report`** (command precedent:
`--learn-status` / `cmd_learn_status`, :3296). Per work: staged count,
promoted-ever count, expired count, oldest staged age (runs + days),
and the claim list with sightings + best support. Global header:
totals + the arithmetic check that closes the books —
`staged_ever = promoted + still_staged + expired` per work; the report
prints `UNACCOUNTED: n` and it must always be 0. A JSON form
(`--staging-report --json` or the marker aggregated from previews)
feeds the dashboard.

**Dashboard hooks (`preview_variance.py`, additions only — legacy
output stays frozen, per the dashboard's own rule):**

- **Staged-set section** per preview pair: staged in both (stable
  trap), newly staged, promoted between runs, expired between runs —
  read from the `staging` markers.
- **Summary-gated share** (new trope evidence-poverty metric, since
  `empty_evidence_rates` covers relationships only): per run, shipped
  tropes whose support was gate-band vs auto-band, from
  `trope_chapters` + `auto_min`. This is the number F exists to drive
  down *in the shipped set*, and the number that tells us the price
  (claims held) of driving it down.
- Queue churn already counts shipped trope/trigger deltas; staged
  claims appear there as leaving the shipped set, which is correct
  and needs no change.

**Preview-validation gate for shipping F itself** (per
processor-quality-loop discipline; fixed set, before/after, signals
named in advance):

- Books: Fourth Wing, Daemon, Freedom™ — **two fresh runs each** with
  `--staging-gate` (run 1 stages, run 2 is where promotion can fire),
  scored with the dashboard against the flag-off previews of the same
  books.
- Acceptance signals, pre-registered:
  1. **Family invariance:** labelled family outcomes identical to the
     flag-off runs modulo extraction variance already present between
     two flag-off runs; zero family claims staged or promoted (pinned
     by unit test, confirmed by dashboard).
  2. **Bounded blast radius:** run 1's shipped trope set loses *only*
     gate-band claims; auto-band ship set identical to flag-off run 1.
  3. **Accounting closes:** every staged claim across both runs is
     promoted, still-staged, or expired in the report —
     `UNACCOUNTED: 0`.
  4. **Queue delta is exact:** candidate + proposal rows a write run
     would produce drop by exactly the staged count (plan_replace
     held-back counts == write-path skips).
  5. **No invisible trap:** after run 2, the trapped report lists
     100% of still-staged claims with ages; any claim staged in run 1
     and re-extracted in run 2 has sightings == 2 (no resume-style
     double-count, no missed count).
  Scorecard format (red-team fold-in): the summary-gated share is
  reported **per book** alongside signal 2 (the price is a number per
  book, not a vibe), the trapped claims are listed **by name**, and
  `form_changed` promotions get their **own count/line** (§3/OC-6),
  so the design's worst case is countable at a glance.

---

## 7. Failure modes + abuse cases

- **Laundering by repetition — the head-on case.** Can persistence
  alone promote an evidence-less **family** claim? **No. Never.**
  Family claims are not stageable (§1); there is no code path from
  staging to a relationship row. The fail-closed drop
  (`validation_note: "no_evidence"`) remains the only fate of an
  evidence-less family claim, and slice tests pin that a family claim
  presented to the staging helper is refused, not staged. More
  generally: persistence promotion exists only for trope/trigger
  claims, ships them as ordinary candidates at unchanged confidence,
  and stamps the provenance into the evidence dict. Repetition moves a
  claim from "unstable" to "stable"; it never moves it from
  "unverified" to "verified", in data or in wording.
- **Correlated error (the honest limit of persistence).** Re-extraction
  samples the same model over the same text; a *systematic*
  hallucination will repeat and will promote. Persistence filters
  variance noise (the measured disease: trope Jaccard 0.182 across
  runs), not bias. The design accepts this knowingly — it is the
  external reviews' own framing ("a stability filter, not truth") —
  and contains it: promotion requires the claim to have passed the
  trope gate's judgment on each sighting (the gate *resamples* the
  judgment — same model, same summaries; it does not independently
  corroborate), confidence is not inflated, provenance is stamped,
  and the review queue still sees the claim (as a candidate) with its
  persistence stamp visible. **That last containment is conditional
  on OC-7 = yes** (red-team fold-in): without the in-row stamp, a
  promoted claim in the queue is indistinguishable from a natural
  candidate and containment reduces to out-of-band reports. OC-7 is
  therefore a requirement (§4), not a preference.
- **Corrupt staging store.** F8 pattern: loud warning, `.corrupt`
  preservation, store starts EMPTY. The failure direction is safe by
  construction: lost state means sightings reset, claims re-stage,
  promotions are delayed — corruption can never cause an early ship,
  because promotion requires state the corruption destroyed. (Contrast
  learned merges, where corruption-adjacent replay was the D1 risk;
  here the store holds no authority to create output, only to delay
  it.)
- **Unbounded growth.** Entries per work are bounded by that work's
  candidate counts; expiry prunes staged entries, `expired_recent` is
  capped, promoted entries are one small record per claim per work.
  A size guard in `StagingState.save()` warns past 5 MB. The realistic
  growth driver is works never reprocessed — bounded by library size
  and fully visible in the report.
- **Stuck forever.** A claim sighted once, on a book reprocessed
  exactly once more without it, then never again: it sits staged with
  2 absences — under the expiry bar, never expiring. That is the
  absence rule working (insufficient evidence to kill it), and the
  `STALE` report flag is its visibility. OC-5 offers a wall-clock
  hard expiry for those who want the store self-cleaning.
- **Resume/retry double-count.** Covered in §4: non-independent runs
  never increment sightings. A test drives the −15 shape (full resume)
  and asserts sightings unchanged.
- **Flag flapping.** Runs with the flag off neither record nor
  promote; a book processed flag-off between two flag-on runs simply
  has a gap in ordinals — absence counting uses only flag-on completed
  runs, so flag-off runs neither help nor harm a staged claim.
- **Store/work identity collision.** Two different books resolving to
  the same fallback `work_key` (no ISBN, same normalized title+author —
  e.g. distinct editions) would share sightings. Bounded: editions of
  the same work extracting the same trope is close to the claim being
  about that work; and once either resolves a `work_id`, keys split
  (entries under the fallback key are adopted only when title+author
  match exactly — OC-8 if the Advisor wants stricter).

---

## 8. Build plan (slices in order; each independently testable)

1. **`StagingState` store.** Load/save with the F8 pattern (atomic
   write, loud corruption, `.corrupt` preservation), **observe+save
   serialized under a lock** (red-team binding constraint — `--jobs`
   runs books concurrently; include a two-thread contention test),
   keying + ordinal sightings + name→id folding (union semantics),
   expiry arithmetic, trapped-report data model. Tests: corruption
   resets safely + warns; fold unions ordinals without double-count;
   expiry at exactly 3 absences; `expired_recent` cap; store loss ⇒
   nothing promotes; concurrent observes lose no sightings.
2. **Trope observe/split behind `--staging-gate`.** Eligibility
   (gate band only, after denylist + rejection filters), sighting
   rules (independent runs only), promotion rules 1–2, ship-set split,
   `staging` preview marker + `.md` line. Tests: flag-off output
   byte-identical; first run stages gate-band, ships auto-band;
   second independent run promotes; full-resume run does not increment;
   aborted run records nothing; denylisted claim drops, never stages.
3. **Trigger observe/split.** Evidence-empty eligibility, evidence
   arrival promotion (rule 3), persistence promotion. Tests:
   quote-carrying trigger never stages; evidence-less stages; the
   same claim sighted again WITH quotes promotes via evidence
   arrival on that sighting (a first sighting that already carries
   quotes never enters staging at all).
4. **Write-path honoring.** Proposals interception (staged unmapped
   tropes propose only on promotion), `plan_replace` held-back counts,
   `cmd_push_preview` consistency, family-refusal pin. Tests:
   plan ≡ write counts; staged claim produces zero rows in every
   table; family claim presented to the helper is refused;
   confirmed/rejected DB rows untouched across a staging write run.
5. **Reporting.** `--staging-report` (+ JSON), dashboard staged-set
   section + summary-gated share, `UNACCOUNTED` arithmetic. Tests:
   report accounts for every entry across stage/promote/expire
   fixtures; dashboard lines render from marker-carrying previews and
   are absent for marker-less ones.
6. **Validation gate (not code).** The §6 fixed-book protocol, run and
   scored, results written to `eval/` as a scorecard next to this doc;
   ship/no-ship decision on the pre-registered signals.

---

## 9. Open choices (recommendation in bold)

- **OC-1 — v1 scope.** **Tropes (gate band) + triggers (evidence-less)**
  vs tropes only vs adding non-family relationships. Adding
  relationships is recommended against in the strongest terms (§7).
- **OC-2 — Trope staging threshold.** **All gate-band claims stage on
  first sighting** vs only gate-band claims below a confidence cut.
  (Confidence is a pure function of chapter count here, so a cut *is*
  a chapter-count cut; "all gate-band" is the honest form of the rule.)
- **OC-3 — Sightings to promote.** **2** vs 3. Two matches the
  measurement protocol (pairs of runs) and the external reviews'
  staging sketches; three costs another full reprocess per book for
  marginal filtering.
- **OC-4 — Preview runs record sightings.** **Yes, with two
  conditions** (red-team): (a) a flag-on preview declares recorded
  sightings on its face (§5); (b) framed as *independent of D1's
  subject matter* — if D1 rules broadly on preview state-writes,
  OC-4 reopens (§5). Alternative: write-runs only (persistence then
  can't accumulate during validation — the gate in §6 becomes
  unmeasurable).
- **OC-5 — Expiry.** **Absence-based (3 runs) + STALE flag at 180
  days, no wall-clock expiry** vs adding a hard wall-clock expiry
  (e.g. 365 days) for store hygiene.
- **OC-6 — Name-form change under a catalog id.** **Flag in report +
  `form_changed` marker, sightings kept — with `form_changed`
  promotions counted on their own line** in the trapped report and
  the §6 scorecard (red-team strengthening) vs reset sightings on
  form change (strictest against wrong-mapping promotion; costs
  legitimate promotions when a paraphrase legitimately remaps).
- **OC-7 — Provenance in the shipped row. RULED A REQUIREMENT by the
  red-team (no longer an open choice):** promoted claims carry
  `evidence.persistence = {sightings, via}` (§4, §7 — the correlated-
  error containment depends on the stamp being in the row a reviewer
  opens). The evidence dict is a JSON blob; no app change is needed.
- **OC-8 — Fallback work-key adoption.** When a work first resolves a
  `work_id`, **adopt its fallback-key entries** (exact title+author
  match), **with adoptions listed in the next trapped report**
  (red-team: a wrong adoption must be visible to be correctable) vs
  start fresh under the id key.

---

## Addendum (2026-10-15) — seam correction: canonical-first observe keying

The §5 seam as first built observed under the fallback work key
unconditionally and relied on OC-8 adoption, after resolution, to
fold the entry into the canonical work-id entry. Live gate runs
exposed the flaw: an ingested work resolves on *every* run, so
adoption fired on every run and deleted the fallback entry each
time. Every observe therefore started a virgin fallback entry at
run ordinal 1, and the persistence check (2+ sightings at decision
time) could never be true on a resolved work — promotion by
persistence and absence-expiry were structurally unreachable live,
while stores accumulated correctly-keyed sightings nobody ever
observed. Failure direction stayed delay-only throughout: traps,
accounting and the store itself were never wrong.

Correction: the seam now resolves the work read-only
(`resolve_work(..., create=False)` — no write in preview or write
mode; the creating resolution after result assembly is unchanged)
before observing. When a work_id resolves, the observe lands on
the canonical entry directly, so sightings decide promotions and
absences on the entry that accumulates them. When nothing resolves
(a genuinely new work), the original wiring is untouched: fallback
keying, then OC-8 adoption when a later run resolves — adoption
remains the safety net for that path, with its ordinal-shift
semantics unchanged. Stores already holding accumulated canonical
sightings (e.g. [1, 2] in state "staged") promote on the next
canonical observe, with the OC-7 stamp on the shipped row.
Regression coverage: `TestStagingResolvedWorkWiring` in
tests/test_staging_gate.py (resolved-work promotion, resolved-work
absence expiry, accumulated-store promotion, new-work fallback
path preserved).
