# Option G — targeted evidence retrieval for trope claims: design

Status: DESIGN ONLY (no code). Security Advisor red-team complete
— verdict PROCEED-WITH-CHANGES, amendments G1–G5 folded in below;
awaiting user rulings on the §12 open choices as amended. Rollout
model follows the
`--roster-reconcile` / `--staging-gate` / `--persist-roster` precedent:
named flag, default OFF, flag-off = no code path, no preview key, no
behaviour change.

Doctrine, stated once and binding on everything below: **retrieval
finds passages; it does not manufacture support.** A retrieved passage
is evidence only when it *depicts* the claim, not when it is merely
about the claim's neighbourhood — and when nothing in the book clears
that bar, the honest output is an abstention, recorded as one. G never
raises a claim's confidence, never drops a claim for lack of passages,
and never quotes the model: every passage G attaches is a literal span
of the book's own text, produced by the retriever, not by a generator.

---

## 0. What the code actually looks like today (evidence base)

All citations are process-portable.py on master @ 5945604 unless
noted.

- **Trope claims carry no quote evidence — by construction.**
  `_build_trope_rows` (:10749) ships
  `evidence = {"source", "chapters", "chapter_count"}` (:10776) —
  chapter references only. The single evidence-strength behaviour in
  the trope row builder is negative: no supporting chapters recorded
  ⇒ confidence forced to 0.0 (:10779). There is no quote field
  anywhere in the trope path.
- **Trope confidence is a pure function of chapter count.**
  `min(0.9, 0.55 + 0.1 * n_ch)` — auto band at :12416, gate band in
  `v2_trope_gate` (:8084, formula at :8117). Support *breadth* is the
  only signal the system has; nothing anywhere measures support
  *quality*.
- **The trope gate judges summaries, not text.** `v2_trope_gate`
  confirms/denies candidates against chapter *summaries*
  (`PROMPT_V2_TROPE_GATE` :8032) in batched local-model calls. The
  gate never sees a sentence of the book. A claim can therefore be
  confirmed by a summary of a summary, and its shipped evidence is the
  list of chapters whose Call B output named it.
- **The band structure.** `auto_min = max(3, n // 20)` (:12369);
  ≥ auto_min chapters auto-confirms, 2..auto_min−1 goes to the gate,
  single-chapter candidates drop in long books (:12404ff).
- **Trigger claims are the contrasting shape — and the template.**
  Call B evidence quotes accumulate per category
  (`{"quote": q, "chapter": label}`, :7669, capped at 3 per category),
  are regex- and (optionally) decision-model-gated in `v2_reduce`
  (:7690ff), and ship as `evidence.quotes` in `_build_trigger_rows`
  (:10798, quotes at :10818). A trigger with an empty evidence list
  carries confidence 0.0. The quote-entry shape, the cap of 3, and
  the no-evidence-no-confidence rule are all precedents G mirrors
  rather than invents parallels to.
- **Literal-span verification machinery already exists.**
  `verify_evidence(quote, source_text)` (:9058) checks a quote
  against source text on normalized strings with a light fuzzy
  fallback (`_verify_against_norm` :9027); `verify_all_evidence`
  (:9064) runs it per extraction chunk (:9277) so fabricated quotes
  are dropped before aggregation. The codebase already enforces
  "evidence must exist in the text" for model-quoted evidence. G
  inverts the direction — the system retrieves the span instead of
  the model quoting it — and reuses the same verifier as a
  belt-and-braces check before anything attaches.
- **The embedding path exists, with a known asymmetry.**
  `trope_catalog_vectors` (:9314) embeds catalog entries as
  `"name: description"` (cache-namespaced by `EMBED_TEXT_FORMAT =
  "name-desc-v1"`, :9310). Candidate tropes embed as **bare strings**
  (`embed_vectors(..., trope_candidates)`, :7871) for catalog
  mapping. `embed_vectors` (:11389) is a plain OpenAI-shaped POST to
  the configured local endpoint. Any retrieval query G builds must
  pick its text shape deliberately (§3) — the asymmetry is live in
  the mapping path and G does not silently inherit it.
- **Chapter text is in scope at the seam.** `v2_reduce` builds
  `_label_to_text = {label: text}` from the chapter list (:7274);
  the trigger decision gate already pulls per-chapter text this way
  to judge quotes in context (:7706). `trope_chapters` values are
  chapter *labels* (:7833, rebuilt catalog-collapsed at :7912), so a
  claim's support chapters resolve to text through the same mapping.
  Chapter granularity is bounded by `MAX_CHAPTER_CHARS` = 16000
  (:868); extraction chunking is `chunk_text` at `CHUNK_CHARS` =
  8000 (:164, :8188); sentence splitting exists (`_split_sentences`,
  :1982).
- **The decision-model budget is shared and contended.**
  `_MERGE_TIER3_CAP = 50` (:10050) per book; `_dm_budget_claim`
  (:10078) is the single gate. Consumers today: roster merges first
  (comment :10065), family validation (:7587), and the trigger
  decision gate — which claims **per quote** (:7707). Family
  validation is the highest-stakes consumer (family is the worst
  claim class). Any design that lets trope verification draw on this
  pool can starve family validation; §4 makes that structurally
  impossible in v1.
- **F's staging already knows what to do with evidence arrival.**
  `StagingState._observe_trigger` promotes a staged trigger on a
  sighting whose extraction carries evidence (design §4 rule 3,
  implemented via `has_evidence` in `staging_observe_result`,
  :3995). F's design states the trope analogue explicitly: "If
  option G later gives tropes real passage evidence, it slots in
  here as a fourth rule." G is that mechanism. Trope staging keys
  (catalog id / `_tnorm` name, `staging_claim_key` :3248) are
  unaffected — evidence hangs off the claim, never the key.
- **The review queue reads rows, not previews, for tropes.**
  Candidate rows (`book_trope_claims`) and `trope_proposals`
  (`_build_proposal_rows` :10915 — whose row shape today has *no*
  evidence field) are what the Trope Review seat judges. The app
  renders accepted claims with no second confidence filter, so
  whatever ships in a row's evidence dict is the whole evidentiary
  surface a reviewer or reader ever sees.

---

## 1. The problem (measured)

1. **Evidence poverty is the trope queue's dominant cost.** The
   Trope Review seat's own accounting: 14/20, then 18/20 reviews in
   consecutive batches ended in **abstention for lack of evidence** —
   not verdict instability, not disagreement. Reviewers are being
   handed chapter numbers and asked to trust a summary-chain. Their
   standing request is the design target in one line: *send the
   diff, not the pile.*
2. **Trope sets are the least stable surface in the pipeline.**
   FW −16 vs −17: trope Jaccard **0.391**, against relationships
   0.421 and triggers 0.688 (triggers, the surface *with* quotes,
   are the most stable claim class the pipeline ships). Candidate
   tropes churn run to run, and the chapter-count gate cannot
   distinguish a real trope from a repeated hallucination, because
   both look identical in its only currency: breadth.
3. **Two independent external reviews converged on this item
   unprompted** (evidence retrieval for tropes), and the landscape
   research found the same discipline in the closest analogue
   system: LoreGraph's verification pass rejects claims whose
   evidence span is not a ≥95% literal match to source. Our own
   Phase 1 verifier (§0) is the same instinct, already shipped —
   for every claim class *except* tropes.
4. **F is waiting on it.** Staging promotes triggers on evidence
   arrival and holds gate-band tropes for persistence or support
   growth. For tropes, "evidence arrival" does not exist yet as an
   event. G manufactures the event honestly — or certifies its
   absence, which is also information F and the dashboard can use.

---

## 2. Scope

**In v1: trope claims** — every trope in the post-gate confirmed set
(ship set and staged set alike), i.e. claims that survived the
denylist, the chapter-count bands, the summary gate, and rejection
learning. Retrieval never runs for gate-denied or denylisted claims:
evidence for a claim the pipeline already killed is waste, and
evidence must never become a resurrection path around a hard filter.

**In v1, constrained: evidence-less trigger claims** — retrieval runs
only for triggers whose extraction evidence list is empty (the exact
set F stages). Retrieved trigger passages serve two purposes only:
(a) the staging evidence-arrival signal (§6), and (b) row quotes
with retrieval provenance. They pass the trigger category's own
regex discipline first (§4), never change severity, and never
change the confidence the reduce computed (§6).

**Out: relationships** (they have their own binding-evidence mining
and fail-closed doctrine; passage retrieval for relationships is a
different design), **characters, quotes, spice** (not claims in the
evidence-poverty sense), **the legacy pipeline** (v2 only, like F
and B), and **the catalog-mapping embedding path** (the
catalog-vs-candidate asymmetry in *mapping* belongs to the
hygiene/experiment bundle; G fixes the shape only for its own
queries, §3).

---

## 3. Retrieval mechanism

**Recommendation: targeted, two-stage retrieval over the claim's own
support chapters — lexical prefilter, then embedding rank — returning
literal spans. No whole-book scan, no generative step.**

### Why targeted

A trope claim already names its hunting ground: `trope_chapters`
(the chapters whose Call B output produced it). Searching the whole
book for a claim the extraction only ever saw in 4 chapters buys
recall the claim's own provenance contradicts, at 25× the cost, and
creates a new failure: a passage from a chapter that never supported
the claim "confirming" it (the retriever outvoting the extractor).
Retrieval's job is to *exhibit* the support the claim already
asserts, or honestly report that the asserted chapters don't contain
any. **If the passages aren't in the support chapters, that is the
finding** — the claim is weaker than its chapter list claims, and §6
records it as such rather than shopping the rest of the book for a
better answer.

### Stage 1 — lexical prefilter (deterministic, free)

Within each support chapter, split text into candidate windows:
overlapping sentence-anchored windows of ~500–800 chars (built on
`_split_sentences`, :1982). A window survives if it contains at
least one query term: content tokens from the trope name, plus —
for catalog-mapped claims — content tokens from the catalog
description. This stage is deliberately permissive (recall-oriented)
and costs nothing but string scans.

### Stage 2 — embedding rank (existing path, symmetric shape)

Surviving windows embed via `embed_vectors` (:11389) against the
same local endpoint and model as the catalog. The **query** embeds
in the catalog's own text shape — `"name: description"` for mapped
claims (the `name-desc-v1` format, :9310) — *not* as a bare name:
cosine between a bare-name vector and prose windows measures
name-mention, not trope presence, and would systematically reward
windows that merely name-drop. For unmapped claims (no catalog
description), the query is the name plus the gate's own chapter
summaries for the support chapters — the richest honest description
available. Windows are embedded **once per book** (cached by chapter
+ window offset), shared across claims whose support sets overlap;
per-claim work is then dot products (`_dot`, :9302) only.
**Cache lifetime (amendment G5): the window-embedding cache is
per-run and in-memory only** — keyed by chapter + window offset
within the run, discarded when the run ends, never persisted and
never shared across runs. If a future slice ever persists it, the
hygiene batch's namespacing discipline applies: the cache key must
carry the embedding model plus a window-format version, and a
mismatch invalidates wholesale. The cache is also G-internal — it
neither reads from nor writes to the catalog mapping path's cache
(§2 scope), so G's symmetric query shape cannot leak into mapping.
Rank per claim: cosine descending, with a floor (OC-3) below which
a window is not a candidate at all.

Granularity note: per-chapter ranking, then merge. A claim's top
windows are chosen across its chapters with a **distinct-chapter
preference** (§4) — three windows from one paragraph must not
impersonate book-wide support.

### What retrieval returns

Literal spans with provenance: `{quote, chapter, chapter_index,
score}` — `quote` an exact substring of the chapter text (offsets
recorded at retrieval; re-verified with `verify_evidence`, :9058,
before attach — if verification fails, the span is a bug, dropped
and counted, never shipped). This is the structural answer to the
fabricated-quote problem Phase 1 verification fights elsewhere:
G's passages cannot be fabricated, because no model writes them.

---

## 4. What counts as support — the bar, verification, abstention

Retrieval rank is **not** support. A window can be maximally *about*
a trope's ingredients and depict none of it (a discussion of
betrayal is not a betrayal scene; two rivals sharing a page is not
enemies-to-lovers). The design therefore separates three verdicts,
and only one of them attaches:

- **supports** — the passage itself depicts the trope's defining
  dynamic or event: a reader shown only this passage would say the
  trope is *happening here*, not merely being discussed, remembered
  as backstory exposition, or negated.
- **adjacent** — topically neighbouring but not depicting:
  mentions, discussions, negations ("she would never fall for
  him"), hypotheticals, single-ingredient presence. Adjacent is a
  **named, counted outcome** (marker + dashboard), never silently
  dropped and never attached — because adjacency is precisely the
  laundering vector (§9), it must be visible to be governed.
- **contradicts / nothing** — no candidate window supports the
  claim: **abstention**. No passage attaches, and the claim's
  quotes stay exactly as today — but the shipped row carries the
  retrieval stamp with `support: "none"` (§5, amendment G1), so
  *ran and found nothing* is distinguishable on the row's own face
  from *never ran* (no stamp at all). The abstention is also
  recorded in the run marker and the preview (claim name + support
  chapters searched), so a reviewer can see that G *looked* and
  found nothing, which is different from G not running.

### Verification stage (the judge)

**Recommendation: a batched local-model support check — not the
decision model, and not retrieval score alone.**

- The judge is the pipeline's own local extraction model, called
  through the same `call`/`_run_task` channel as `v2_trope_gate`
  (:8084) — batched (claims × their top-3 candidate windows,
  ~10 claims per call), returning per-passage verdicts from the
  three classes above. It draws **zero** decision-model budget:
  `_dm_budget_claim` is never called on G's path in v1, pinned by
  test. The shared 50-call pool (:10050) keeps its existing
  priority — merges, then family validation, then the trigger
  gate — untouched. If the user later wants decision-model judging
  for borderline passages, that is a separate, capped, opt-in
  allowance that yields to family validation; it is OC-2's
  alternative, not v1's default.
- Why not retrieval-only: cosine has no concept of negation or
  depiction; shipping its top hit as "evidence" is the laundering
  failure (§9.1) with extra steps. Why not decision-model-only:
  budget starvation of the worst claim class for the
  evidence-poorest one is the wrong trade (§9.3), and the local
  model's verdicts here are auditable against the very passages
  they judged — a wrong "supports" ships next to its own refutation,
  readable by any reviewer.
- **Trigger passages** additionally pass the category's existing
  regex discipline before judging: `_TRIGGER_NEGATIVE_RE` hits
  disqualify a window outright, exactly as in the reduce gate
  (:7690ff). Retrieved trigger evidence is stamped
  `decision_gate: "retrieval"` alongside the existing
  regex/decision/none stamps, so its provenance is never confused
  with extraction evidence.

### Support grade

Attached evidence carries a grade, computed deterministically:
**strong** = supporting passages in ≥2 distinct chapters;
**single** = support in exactly 1 chapter; **none** = retrieval
ran and no passage supported the claim (the abstention grade —
it ships in the stamp, §5, with zero counts). The grade ships in
the provenance (§5). It is *not* folded into confidence (§6) — it is
the breadth of the passage support, stated next to the chapter
count breadth the row already shows, so a claim whose 9-chapter
support yields passages in 1 chapter reads exactly as strong as it
is, on its face.

---

## 5. Where the evidence attaches

- **Run result.** `result["trope_evidence"]`: `{_tnorm(name):
  [passage, …]}` plus `result["trope_retrieval"]` run accounting
  (attempted / supported / adjacent-only / abstained, per claim).
  Plain JSON types only (strings, numbers, lists, dicts) — the
  repo's standing rule: generated JSON changes require
  serialization/round-trip coverage, and slice 3 pins the
  checkpoint/preview round-trip for both new keys.
- **Shipped trope rows.** `_build_trope_rows` gains, when passages
  exist: `evidence["quotes"] = [{quote, chapter}, …]` — the *same
  key and entry shape triggers already ship* (:10818, :7669) — plus
  `evidence["retrieval"] = {method: "targeted-v1", support:
  "strong"|"single", passages: n, chapters_with_support: m}`.
  Cap: **3 passages, ≤600 chars each** (the trigger cap and the
  quote-length precedent, :9179). A claim with no supporting
  passage ships **byte-identical evidence to today except the
  retrieval stamp** (amendment G1): `evidence["retrieval"] =
  {method: "targeted-v1", support: "none", passages: 0,
  chapters_with_support: 0}`. The stamp rides *every* G-processed
  shipped claim — supported claims carry their grade and counts,
  abstentions carry `"none"` and zeros — so the three states are
  distinguishable on the row's own face: no `retrieval` key = G
  never ran on this claim; key with `support: "none"` = G ran and
  found nothing; key with a grade = G ran and attached passages.
  This is the D3 lesson applied to rows: the review queue reads
  rows, not reports, and a zero-support claim indistinguishable
  from a supported one would repeat that invisibility at exactly
  the surface G exists to fix. The same discipline applies to
  G-processed trigger rows: their evidence dict gains the same
  `retrieval` stamp, `"none"` included, wherever the row ships.
- **The app surface.** The app renders claims with no second
  filter, so what ships *is* the review surface. Passages in
  `evidence.quotes` put the trope row on the same footing as the
  trigger row: a reviewer (or the Trope Review seat) opening the
  claim sees the three passages instead of a chapter list — the
  diff, not the pile. Provenance rides in the same dict, so a
  retrieval-attached quote is never mistaken for an
  extraction-attached one.
- **Preview.** `save_preview` (:13083) serializes the result
  wholesale, so passages appear in the preview JSON automatically;
  the `.md` summary gains one line ("Trope evidence: N claims with
  passages (M strong), K abstentions") — abstentions are thus
  visible in the artefact the user actually reads, named per claim
  in the JSON. Two legibility rules ride with it (amendment G2):
  a staged claim displayed with strong passages reads as
  *promotes on next sighting via evidence* — the one-run promotion
  lag (§6) must be legible as the design working, not as a bug;
  and because a preview run's retrieval writes `evidence_ever`
  into F's staging store (§6), the preview declares that
  recording on its face, per F's OC-4 condition — G inherits that
  condition the moment it writes staging state, "no store of its
  own" notwithstanding (§8).
- **Proposals.** `_build_proposal_rows`' row shape has no evidence
  field today; adding one is an app-facing shape change. v1 does
  **not** alter proposal rows — unmapped claims' passages live in
  the preview and, once mapped/shipped as candidates, in the row.
  Whether proposals gain an evidence field is OC-4.

---

## 6. Interactions

### The chapter-count gate: supplement, never replace (v1)

The bands stay exactly as they are. Chapter count remains the
ship/stage signal; passage support is an *evidentiary* layer the
bands never had. Specifically in v1:

- **Confidence is unchanged.** Passage support does not raise it
  (F's doctrine: provenance is stamped, confidence is the
  pipeline's own). A gate-band claim with strong passage support
  keeps its formula confidence; the support is visible, not
  monetized into a number.
- **Nothing drops on abstention.** An auto-band claim with zero
  supporting passages ships as today — no demotion, no confidence
  change — carrying the G1 `support: "none"` stamp on its row
  (§5). Its zero-support status is also a **dashboard metric and
  a trapped-report line** (§10) — the measured false-positive
  signal this program has never had — and whether it should ever
  *demote* a claim is a v2 conversation that must be argued from
  gate data, not assumed here (OC-6).
- The gate itself still judges summaries. G runs *after* the gate
  (§8 seam), so gate behaviour, budget, and batching are untouched.

### F staging

- **Tropes — promotion rule 4 (evidence arrival), as F's design
  pre-registered.** A staged trope promotes via evidence when G
  has found **strong** support (≥2 distinct chapters) for it.
  Single-chapter support attaches as evidence but does not
  promote: one chapter's passage is one observation, and
  promotion-by-evidence should demand the same multiplicity
  persistence demands (2 independent sightings).
  **Timing (amendment G2): promotion via retrieved evidence takes
  effect at the NEXT run's staging observe, never in the run that
  retrieved it.** There is exactly one promotion decision point —
  the staging seam's observe inside `staging_observe_result`
  (:3966) — and there is **no post-seam promotion pass in v1**.
  Trope retrieval runs after the seam (§8), so same-run promotion
  is not merely disallowed, it is structurally impossible: what
  run N's retrieval does is record the evidence on the claim's
  staging entry (`evidence_ever` — the field already exists in
  the store shape — plus the support grade), and run N+1's
  observe evaluates that recorded state under the fourth
  promotion rule in `_observe_trope`, beside persistence/support.
  The one-run lag is the doctrine working, not a delay: promotion
  decisions read recorded state, never the current run's in-flight
  retrieval. The shipped row's OC-7 stamp reads `via: "evidence"`
  — the word "promoted" stays paired with its mechanism, and
  "verified" appears nowhere.
- **Triggers — the arrival F already implements.** G runs before
  the staging seam (§8); an evidence-less trigger that gains
  regex-clean, judge-supported passages has them in its evidence
  list when `staging_observe_result` computes `has_evidence`
  (:3999), so the existing promotion rule fires with no staging
  change at all. This side has no G2 lag and needs none: retrieval
  is pre-seam, `has_evidence` reads the *current* run's claim,
  and F's rule 3 fires as designed; G records evidence only — it
  never increments a sighting. Its confidence stays the reduce's 0.0-derived
  value until a *natural* evidenced sighting — no: stated plainly,
  `_build_trigger_rows` zeroes confidence only when the evidence
  list is empty (:10812); a G-filled list ships the reduce's
  computed confidence for that claim, which for an evidence-less
  extraction was 0.0 (:7781). Retrieval changes the row's evidence,
  never its number. (If a future design wants retrieved evidence to
  restore a computed confidence, it must argue it separately.)
  Recorded here so the two are not rediscovered separately: the
  trigger row builder's missing-confidence default
  (`td.get("confidence", 0.7)`) is D4's open decision surfacing in
  this path. In practice the reduce always sets the key, so G
  never meets the default — but the default lives in code G walks
  past, and the doc ties it to D4 rather than leaving it implicit.
- **Flag independence.** G and F compose but neither requires the
  other: G without F attaches evidence to the ordinary ship set;
  F without G behaves exactly as today.

### B roster

No interaction in v1. Trope identity for G is the claim's own
`_tnorm`/catalog key — the same keys the row builder and staging
already use; B's entity IDs are a character-roster concern.

---

## 7. Performance / cost (115-chapter book)

Worked on the FW reference run (115 chapters, ~1.01M chars,
~25–45 confirmed + staged trope claims, ~5 evidence-less triggers):

- **Windows:** support chapters only. A claim's support set is
  2–9 chapters; the union over all claims is realistically 60–100
  chapters × ~20 windows ≈ **1,200–2,000 windows**, embedded once
  per book in batched POSTs to the **local** embed endpoint (the
  catalog mapping already embeds per book at this order of
  magnitude). Lexical prefiltering typically cuts embedding volume
  further; windows are cached across claims.
- **Judge calls:** top-3 windows × ~45 claims ≈ 135 passage
  verdicts, batched ~10 claims/call → **~5 local-model calls**.
  Zero decision-model calls. Zero change to the 50-call pool.
- **Wall clock:** embedding + judging are minutes at the outside
  on the user's local stack (6GB GPU local model + local embed
  endpoint), against a ~1-hour full-book run. Retrieval failure
  (endpoint down, judge error) is **fail-open and loud**: the run
  proceeds with today's evidence shapes, the marker records
  `retrieval: "failed"`, and nothing about the ship set changes.
  A per-book cap on claims/window volume, with skipped claims
  counted in the marker, bounds pathological books.

---

## 8. Pipeline integration

**One seam, after F's.** In `process_file_v2`, immediately after
the staging seam block (:12434ff) and before result assembly —
the last point where the confirmed trope set, the trigger list,
chapter texts, and the staging outcome are all in scope:

```python
evidence_retrieve_result(result_parts, chapters, *, call, embedder)
    -> annotates result["trope_evidence"], fills evidence-less
       trigger evidence lists, returns marker accounting
```

Ordering constraints that fix this placement:

- **After** denylist + gate + rejection filters (scope, §2) and
  after v2_reduce's trigger finalization (trigger evidence lists
  are final there).
- **Relative to F:** trigger retrieval must land *before*
  `staging_observe_result` computes `has_evidence`. The seam
  therefore splits: trigger retrieval runs just before the staging
  seam; trope retrieval runs just after it (so staged tropes are
  retrieved for too — their promotion evaluation needs the
  passages). One helper, two call points, both inside the same
  flag check. Flag-off (or dry-run, or aborted): neither call
  happens — no embed calls, no judge calls, no marker, byte-
  identical behaviour. (Preview runs DO retrieve: previews are the
  measurement instrument and the review queue's source, per the
  F/B precedent. Retrieval keeps no store of its own — but it is
  not state-free: evidence for a staged trope is recorded into
  F's staging store (`evidence_ever`, §6), so the
  preview-state-write question that F's OC-4 and B's OC-2 had to
  answer *does* arise for G, and G inherits F's OC-4 condition —
  a preview run that records evidence declares it on the
  preview's face (§5), "no store of its own" notwithstanding.)
- **Result consumers inherit.** `write_claims`/`_build_trope_rows`,
  `plan_replace`, `save_preview`, and the dashboard read the
  annotated result; there is no second copy of the evidence
  decision for any consumer to understate (the F17 lesson).

**Flag.** `--evidence-retrieval` → `CONFIG["evidence_retrieval"]`,
default OFF, argparse + CONFIG wiring as per precedent. A
`result["evidence_retrieval"]` marker exists only when the flag is
on: per-claim outcomes + totals (attempted, supported-strong,
supported-single, adjacent-only, abstained, failed).

---

## 9. Red team — the three most likely ways G makes things worse

1. **Laundering a false trope into shipped evidence.** Retrieval
   finds a topically-adjacent passage for a trope that isn't in
   the book; a lenient judge calls it support; the false claim now
   ships *with a real quote*, which disarms exactly the human
   review that chapter-refs-only evidence used to provoke. The
   claim is worse than unproven — it looks proven.
   *Mitigations:* the three-verdict judge with **adjacent** as a
   named, counted, never-attached outcome; the supports test is
   depiction, not topic (§4); passages ship with retrieval
   provenance so their origin is on the row's face; the
   validation gate (§10) pre-registers a **false-support rate**
   measured on labelled *false* claims, with a ship bar — G does
   not ship on coverage numbers alone. Residual risk, stated: a
   passage can genuinely depict a dynamic the book includes once,
   in passing, for a trope the book is not *about*. The grade
   (single vs strong) and the unchanged confidence are the
   containment; the reviewer still decides.
2. **Cherry-picked breadth.** Top-3 selection concentrates on the
   claim's single strongest chapter; the shipped evidence then
   *implies* pervasiveness the chapter list doesn't support — or,
   inverted, one anomalous chapter manufactures "strong" support
   for a marginal trope. *Mitigations:* distinct-chapter
   preference in selection; every passage carries its chapter
   label and the provenance counts chapters-with-support *next
   to* the row's existing chapter_count, so overstated breadth is
   arithmetically visible on the row's face; promotion via
   evidence demands ≥2 chapters (§6); confidence never moves.
3. **Starving the important for the evidentiary.** If passage
   verification drew on the decision-model pool, trope evidence
   would compete with merges and family validation — the worst
   claim class losing budget to the least dangerous one — and if
   retrieval ran whole-book or per-chunk generative passes, a
   115-chapter book's runtime would grow by a second extraction.
   *Mitigations:* structural, not procedural — v1 code never calls
   `_dm_budget_claim` on G's path (test-pinned); the judge is the
   local model in ~5 batched calls; retrieval is support-chapters
   only, embeddings local and cached; fail-open on any retrieval
   error. The cost ceiling is in §7 and is a §10 gate signal.

A fourth, quieter failure is worth naming: **abstention as
stealth denial.** Implicit/arc-level tropes ("found family"
enacted across forty chapters) may have no single depicting
passage. If abstention ever fed a gate, G would systematically
punish exactly the tropes least reducible to a scene. That is why
§6 forbids it in v1: abstention is reported, never enforced.

---

## 10. Measurement — the validation gate for G itself

Per processor-quality-loop discipline: fixed books, signals
named in advance. Books: Fourth Wing, Daemon, Freedom™ (the F
gate set). Two flag-on preview runs are not needed — retrieval
is deterministic given a run's extraction. The invariance
signal (3) is measured over a **replayed extraction** — the same
saved extraction run flag-off vs flag-on — not over two fresh
extractions (amendment G4: fresh-run extraction variance would
swamp the very signal being read). The quality signals (1, 2)
run over the flag-on pass plus the labelled exercises below.
**Every rate in the scorecard reports its n beside it**
(amendment G3) — a percentage without its denominator is not a
measurement.

1. **False-support rate (the ship bar).** On labelled FALSE trope
   claims, the share receiving a *supports* verdict with attached
   passages must be **< 10%** (n reported beside the rate). The
   false pool has two sources, broken down per source in the
   scorecard (amendment G3): (a) claims humans already killed for
   these books (Trope Review history + rejection records), and
   (b) **planted cross-book false claims** — trope claims true of
   another gate book, asserted for this one and run through
   retrieval — augmenting the pool **to a stated floor of n ≥ 30
   false claims**, so the rate rests on a denominator that is not
   a small-n coin flip. Adjacent verdicts are reported separately
   and do not count against the bar, but a false claim whose
   passages a human spot-check judges *depicting* counts
   double-weighted in the report narrative — that is failure mode
   §9.1 observed, and it gets named, not averaged away.
   **Judge fixture (amendment G3).** The local judge is also
   measured directly, not only through shipped claims: a
   labelled-passage fixture with known supports / adjacent /
   contradicts verdicts — including negation,
   discussion-not-depiction, and backstory-mention cases — is run
   through the judge; per-class accuracy is reported, and the
   **adjacent→supports leakage rate is the headline number** —
   the laundering vector (§9.1) measured at its source.
   **Fallback posture, named in advance:** if any bar in this
   section fails, G ships **report-only** — passages appear in
   the preview and the marker, never in shipped rows — rather
   than the bars being renegotiated after the fact.
2. **Coverage.** ≥ 60% of shipped tropes carry ≥1 supporting
   passage (n reported); every zero-support shipped trope is
   listed **by name** in the scorecard (the price and the
   false-positive candidates, in one list) — each carrying its
   G1 `support: "none"` row stamp, so the list can be checked
   against the rows the review queue actually reads.
3. **Invariance.** Measured over a **replayed extraction**
   (amendment G4): the same saved extraction, run flag-off vs
   flag-on. Family outcomes, merges, trigger severities and
   confidences, spice, and roster outputs identical; trope and
   trigger *sets* identical (G changes evidence, not membership)
   — any set difference fails the gate outright. The only
   permitted row differences are G's own evidence additions: the
   `quotes` entries and the `retrieval` stamp (§5).
   *Gate record 2026-10-15: this signal is **PENDING-SPINE** —
   the replay instrument does not exist yet (checkpoint resume
   refuses/clears across code changes); the spine build's slice 3
   provides the replay harness, and G4 is scored when it lands.
   It is not stubbed with two fresh extractions: a stubbed G4 is
   a false pin, worse than a deferred one.*
4. **Budget + runtime.** Decision-model claims attributable to G
   = 0 (log-asserted); FW wall-clock overhead ≤ 10% of the
   flag-off run.
5. **Accounting closes.** attempted = supported-strong +
   supported-single + adjacent-only + abstained + failed, per
   book, printed in the scorecard; every attached passage
   re-verified as a literal span of its chapter (100%, by
   construction + verifier — any miss is a build bug, not a
   tuning issue).

Dashboard: `preview_variance.py` gains an additions-only trope-
evidence section (legacy output frozen): shipped-with-support
share, zero-support shipped list, abstention and adjacency counts
— read from the `evidence_retrieval` marker, absent for
marker-less previews.

---

## 11. Build plan (slices in order; each independently testable)

1. **Retrieval core (pure).** Windowing over chapter text
   (sentence-anchored, offsets), lexical prefilter, embedding rank
   with the symmetric query shape, span extraction + verifier
   check. No pipeline wiring. Tests: known-support synthetic
   chapters rank their passage first; negation/discussion windows
   survive prefilter (judge's problem, not the filter's); spans
   verify 100%; window cache shared across claims.
2. **Support judge.** Batched local-model verdicts
   (supports/adjacent/contradicts), grade computation,
   abstention accounting. Tests: stubbed-call fixtures for each
   verdict class; **zero `_dm_budget_claim` calls on the whole G
   path** (the §9.3 pin); judge failure ⇒ fail-open marker.
3. **Trope seam + row attach.** Seam in `process_file_v2` (§8),
   `result["trope_evidence"]`, `_build_trope_rows` evidence
   gains `quotes` + `retrieval` provenance, caps enforced.
   Tests: flag-off byte-identical rows; supported claim ships
   trigger-shaped quotes; abstained claim's row byte-identical
   to today **except the G1 retrieval stamp** (`support: "none"`,
   zero counts — the narrowed byte-identity pin, named as such);
   serialization round-trip for both new result keys.
4. **Trigger retrieval + F interplay.** Evidence-less triggers:
   regex-gated retrieval before the staging seam; staging trope
   promotion rule 4 (strong support, `via: "evidence"` stamp).
   Tests: negative-regex passage never attaches; retrieved
   trigger promotes its staged entry via evidence arrival and
   ships confidence unchanged (and records no sighting — G is
   evidence, not occurrence); **the G2 lag, pinned explicitly**:
   a staged trope whose strong support is retrieved in run N
   does NOT promote in run N — the evidence is recorded
   (`evidence_ever` + grade) and promotion fires at run N+1's
   observe; single-chapter support records evidence but never
   promotes; no post-seam promotion pass exists to test around.
5. **Reporting.** Preview `.md` line, marker totals, dashboard
   trope-evidence section (additions only). Tests: accounting
   identity closes on fixtures; dashboard section absent for
   marker-less previews, legacy output byte-identical.
6. **Validation gate (not code).** The §10 protocol on the three
   books: replayed-extraction invariance (G4), the
   labelled-passage judge fixture with its leakage headline, and
   the false pool (killed claims + planted cross-book claims) at
   its stated n ≥ 30 floor (G3); scorecard written to `eval/`
   beside this doc; ship/no-ship on the pre-registered signals,
   with the G3 fallback — report-only — applied if a bar fails.

---

## 12. Open choices (recommendation in bold)

Advisor red-team outcome (amendments G1–G5 folded above): OC-1
endorsed; OC-2 endorsed as amended by G3 (the judge is measured
on the labelled-passage fixture, adjacent→supports leakage the
headline); OC-3 endorsed (the floor stays a named tunable,
reported in the marker); OC-4 endorsed; OC-5 endorsed as amended
by G2 (next-observe promotion timing); OC-6 endorsed as amended
by G1 (the row stamp); OC-7 endorsed. Nothing requires a
re-red-team. The rulings requested below are on the amended text.

- **OC-1 — Trigger scope.** **Evidence-less triggers in v1**
  (promotion input + row quotes, regex-gated, severity and
  confidence untouched) vs tropes only. The trigger work is small
  *because* F already built the arrival mechanism; deferring it
  strands staged triggers on the persistence path alone.
- **OC-2 — Verification depth.** **Batched local-model judge,
  zero decision-budget draw** vs retrieval-score-only (rejected:
  §9.1) vs decision-model judging under a new capped allowance
  (rejected for v1: §9.3; arguable later from gate data).
- **OC-3 — Retrieval floor + caps.** **Cosine floor tuned on the
  gate books, 3 passages / 600 chars (trigger precedent)** vs no
  floor (judge sees everything lexical — costlier, noisier) vs
  tighter caps. The floor is a named tunable, reported in the
  marker, not buried.
- **OC-4 — Proposal evidence.** **Preview-only in v1** (no
  `trope_proposals` shape change) vs adding an evidence field to
  proposal rows now (app-facing change; the app seat's call as
  much as ours).
- **OC-5 — Trope promotion via evidence.** **Strong support only
  (≥2 distinct chapters), effective at the next run's staging
  observe** (amendment G2: evidence recorded in run N is state
  that run N+1's observe evaluates — no same-run promotion, no
  post-seam promotion pass) vs any support promotes vs evidence
  never promotes tropes (persistence/support-growth only, as
  today). The middle option equates one passage with two
  independent sightings; the design judges that trade wrong.
- **OC-6 — Zero-support shipped claims.** **Report-visible AND
  row-stamped in v1** (amendment G1: dashboard + trapped lines,
  plus `support: "none"` in the row's own retrieval stamp; still
  no demotion, no confidence change) vs auto-band zero-support
  claims stage under F. Report-and-stamp is the honest v1: the
  demotion rule should be written from the gate's zero-support
  lists, not before they exist — and the lists must be checkable
  against the rows the review queue actually reads.
- **OC-7 — Query shape for unmapped claims.** **Name + support-
  chapter summaries** vs name only. Name-only repeats the
  asymmetry §3 rejects for mapped claims, in the population
  (unmapped/proposal-bound) whose evidence poverty is worst.

---

*Sources: process-portable.py and preview_variance.py on master
@ 5945604 (read firsthand for every citation); eval/
f-staging-design.md (staging semantics, OC-7 stamp doctrine, the
§4 note pre-registering this option); eval/b-roster-design.md
(precedent + scope boundary); Trope Review seat consult
2026-10-14 (abstention accounting, "send the diff, not the
pile"); landscape research 2026-10-14 (LoreGraph ≥95%
literal-span verification pass).*
