# Option B — persisted per-work roster with stable entity IDs: design

Status: DESIGN ONLY (no code). Security Advisor red-team complete —
verdict PROCEED-WITH-CHANGES; amendments A1–A9 are folded into the
text below, and the §15 open choices are stated as amended. Awaiting
user rulings on the §15 open choices. Rollout model follows the
`--roster-reconcile` / `--staging-gate` precedent: named flag, default
OFF, flag-off = no code path, no store file touched, no preview key.

Doctrine, stated once and binding on everything below: **identity is a
hypothesis with provenance, never a fact the store asserts.** A
persisted link says "run N's entry X was judged the same person as
entity E, by tier T, on this evidence" — it is recorded, reversible,
and re-vetted against every later run's evidence. Names are
observations; identity is the thing observed. And the standing
corpus doctrine applies with full force: **a false character merge is
the worst error class in this system** — so where F's store could only
ever *delay* output, B's store can change *who a row is about*, and
every mechanism below is shaped by that asymmetry. Uncertain matches
create NEW entities, never forced links.

---

## 0. What the code actually looks like today (evidence base)

- **Names are the join keys at every layer — literally.** The roster
  is a dict keyed by `norm_name(name)` (`_roster_update`,
  process-portable.py:4414; entry creation :4523), and the entry's
  display `"name"` is whichever form was sighted *first*. The DB
  layer is the same: `_CLAIM_TABLES["book_characters"]["id_col"]` is
  `"name"` (:8710), `_claim_key` lowercases it (:8728),
  `_build_character_rows` dedupes by `c["name"].lower()` (:8898),
  and `_build_rel_map` keys relationships by `r["from"].lower()`
  with `"to"` carried as a bare name string (:8881). There is no
  relationships table in the write path — relationships are JSON
  embedded inside character rows, keyed by endpoint *names*.
- **Within-run identity machinery is tiered and veto-driven.**
  `_roster_update` merges on: primary-key match → learned nickname →
  series hint → `_merge_tier` (:8231; Tier 1 hard NO on same-chapter
  co-occurrence, Tier 2 YES on titles/spelling/nicknames/unambiguous
  subsequence). Proper-name aliases are *recorded, not trusted*.
  `_roster_reconcile` (:4918) adds whole-book repairs under a
  membership invariant (merges only fold sighted entries; nothing is
  created). `post_pass_merge` (:5084) runs Tier 1/2 to convergence,
  then Tier 3 asks the decision model (`ask_same_person`, :2477,
  called :5182) only for ambiguous pairs *with a similarity signal* —
  unsure pairs are left separate and reported, never force-merged.
  `dedupe_characters` (:9501) is a separate opt-in result-level pass
  (`CONFIG["dedupe"]`, called :10583).
- **Learned state replays name-keyed merges across ALL books.**
  `LearnedState` (:2583) keeps one global `nicknames` dict
  (nkey → full form); `record_merge` (:2751) is called by reconcile
  and post-pass merges, and `_roster_update` consults `nick_lookup`
  (:2784) on every chapter. `import_labels` skips
  `same_person == False` (:2816), so human "not the same person"
  verdicts retract nothing. This is decision **D1's** subject matter;
  B is designed to neither depend on nor worsen it (§7).
- **Curation preservation is name-keyed today.** `_partition_claims`
  (:8737) preserves rejected + confirmed + contributor rows through
  replace-wholesale, and new-run claims are deduped against those
  rows' *name* keys. A human who rejected "Violet Sorrengail" has
  not, by that act, rejected a future run's "Violet".
- **F's store is the persistence precedent — and it explicitly
  deferred this problem.** `StagingState` (:3287) is a separate class
  and file (`staging.json`) in the learned-state dir
  (`~/.ebook-processor/learned/`), with the F8 discipline (atomic
  save, loud corruption, `.corrupt` preservation, RLock) and
  work-identity machinery (`staging_work_key`, :3257; `adopt_for`,
  :3536). Its claim-key function *refuses* relationships outright:
  "endpoint identity is the unsolved roster problem" — unsolved
  until B. §12 lists what B reuses from F and what it must reject.
- **A series-scoped name store already exists.** LearnedState keeps
  per-series `series/<key>/roster.json` files (:3012, :3099) whose
  hints `_roster_update` consults (load_series_name_hints, :3103).
  They are hints — proposed forms, never identity — and B keeps
  them that way (§7).
- **Checkpoints carry the roster by name key.** The preview/result
  embeds `"roster": _ckpt_json_safe(roster)` (:10520) and resume
  restores entries through `_ckpt_restore_roster_entry` (:9877).
  Identity assignment in B happens *after* the roster is final, so
  checkpoints need no schema change (§7).

---

## 1. The problem (measured)

Option A (roster reconciliation, shipped v2.9.1) was the within-run
fix, and it was measured on Fourth Wing across runs −15/−16:

- Reconcile merged **0 variants in both runs** (pruning fired: 8 and
  4). There was no within-run duplicate to merge.
- Full-roster Jaccard −15 vs −16: **0.671**; principals **0.476**;
  relationships **0.051** (name-keyed sets, dashboard).
- The variance decomposes into two causes, both *cross-run*:
  (a) extraction chooses a different name form per run — "Violet"
  vs "Violet Sorrengail", and one outright wrong-surname flip,
  "Rhiannon Sorrengail" for Rhiannon Matthias; (b) co-occurring
  short/full pairs (Garrick / Garrick Tavis, Bodhi / Bodhi Durran)
  that the Tier 1 co-occurrence veto *correctly* refuses to merge
  within a run — the pair is two roster entries in every run, but
  not always the *same* two entries with the same claims attached.
- −16's dashboard output also showed the downstream cost: aliases
  of one character absorbed into other entries' rows, and a "Dad"
  entry carrying another character's primary key while the real
  entry also existed — a merge and a split in the same roster.

No within-run pass can fix any of this: each run, judged alone, is
defensible. What is missing is a memory that says *the entity you
called "Violet" last run is the entity you are calling "Violet
Sorrengail" this run* — identity that survives the name. That is B.

**What B does not claim to fix:** a wrong-surname flip with no
alias bridge and no corroborating signal ("Rhiannon Sorrengail"
sighted with no claim connecting it to Matthias) has no deterministic
path to the right entity. B creates a new entity for it, reports it
as a split suspect (§9), and moves on. B reduces variance where a
bridge exists; it does not abolish it, and §10's scorecard reports
the residual as a number rather than letting it hide.

---

## 2. Scope

- **Per-work, v2 pipeline only.** Entities belong to one work.
  Series-level identity is explicitly deferred (§7, OC-5).
- **Characters only.** B assigns identity to roster entries. It does
  not stage, promote, or judge claims (that is F, shipped), does not
  touch tropes/triggers/quotes, and does not change any acceptance
  threshold, validation tier, or confidence value anywhere.
- **Internal IDs only in v1.** The DB row shape does not change
  (§8): rows are still written by canonical name. The win shipped
  in v1 is that the *name strings themselves stop flickering*.
- **Flag:** `--persist-roster` → `CONFIG["persist_roster"]`,
  default OFF, argparse + CONFIG wiring exactly as
  `--roster-reconcile` (:11534, :11832) and `--staging-gate`
  (:11835). Flag-off runs do not open the store.

---

## 3. Data model

One store file per work: `~/.ebook-processor/learned/rosters/
<work_key>.json`, managed by a new `RosterStore` class (§6 covers
why per-work files, not F's single file). `work_key` reuses F's
chain — `work_id`, else `isbn:<isbn>`, else
`title:<norm title>|<norm author>` (`staging_work_key`, :3257) —
with the same adoption-on-resolution semantics (§8).

```json
{ "version": 1,
  "work_id": "a42e27eb-…", "title": "Fourth Wing",
  "runs_completed": 3,
  "last_run_fingerprint": "…",
  "next_entity_seq": 14,
  "entities": {
    "ent_0007": {
      "canonical": "Violet Sorrengail",
      "canonical_source": "rule | anchor | human",
      "status": "active | split",
      "split_from": null,
      "anchor": null,
      "observations": [
        {"run": 1, "form": "Violet Sorrengail", "form_key": "violet sorrengail",
         "chapters": [1,2,5,…], "appearances": 41, "role_hint": "protagonist",
         "aliases": ["Violet"], "desc_hash": "…"},
        {"run": 3, "form": "Violet", "form_key": "violet",
         "chapters": [1,3,…], "appearances": 38, "role_hint": "protagonist",
         "aliases": ["Violet Sorrengail"], "desc_hash": "…"}
      ],
      "links": [
        {"run": 3, "tier": "alias-cross", "state": "active | suspended", "evidence": "run form 'Violet' ∈ entity alias forms; entity canonical ∈ run entry aliases"}
      ]
    } },
  "retired": { "ent_0003": {"merged_into": "ent_0007", "run": 2,
                            "reason": "within-run merge"} }
}
```

The pieces that matter:

- **Entity IDs are minted, never derived.** `ent_<seq>` from a
  per-work counter. An ID is never computed from a name (a
  name-derived ID re-imports the disease), never reused, and never
  deleted: a merged-away ID becomes a tombstone in `retired` with a
  `merged_into` redirect, so any historical reference — a report, a
  preview, a human note — still resolves, and a wrong merge can be
  *unwound* by following the tombstones back.
- **Names are observations.** Each completed run appends at most
  one observation per entity: the form this run used, its
  normalized key, chapter set, appearance count, the aliases the
  run claimed, and a hash of the run's best description
  (`_roster_best_desc`, :5054) so description continuity is
  checkable without storing prose. Observations are provenance,
  not votes — but §5's canonical rule reads them.
- **Links are first-class records.** Every cross-run link stores
  the run, the tier that made it (§4), and a human-readable
  evidence string. A link with no recorded basis does not exist;
  this is what makes the roster report (§9) an audit surface
  rather than a dump.
- **Anchors are data, not a side channel.** An entity pinned by a
  human decision carries an `anchor` record (§4, tier 0):
  `{"kind": "confirmed" | "rejected" | "contributor" |
  "manual-alias", "name": <row name>, "source": "db" |
  "--alias", "seen_run": n}`. The DB is re-read at every write
  and wins over the cached anchor on conflict (§7).
- **Status, not deletion.** Entity statuses partition as
  {`active`, `merged`, `split`}. `entities` holds `active` and
  `split` (superseded by a split) entities; a `merged` entity
  leaves `entities` entirely and is recorded ONCE, in `retired`,
  with its `merged_into` redirect — merged is not also a status
  value (one representation, not two). Suspension is not an
  entity status at all: it is a state of a LINK (§4), and the
  store keeps a suspension ledger — suspended links plus the
  entities they touch — reported as its own line (§9), outside
  the ID partition. Nothing is ever silently dropped from the
  file; the report accounts for every ID ever minted (the F
  accounting discipline, applied to IDs:
  `minted = active + merged + split`, `UNACCOUNTED: 0`).

---

## 4. Identity matching across runs

The matcher runs once per run, at the seam in §7, over the run's
FINAL roster (post-reconcile, post-post-pass). It assigns each
roster entry to at most one entity, and each entity to at most
one roster entry per run (folds excepted, below). Tiers are tried
in order; the first tier that produces a *unique, unvetoed* match
wins. **Every tier is deterministic in v1** (OC-1).

**Tier 0 — curation anchors.** Before any name matching, entities
anchored to an existing DB row or a manual `--alias` claim their
run entries. The anchor's key set is closed and exact: the anchor
name's own form_key, plus the form_keys of the aliases recorded
on that DB row itself — no token decomposition, and a bare
surname or first name never claims an anchor. An entry claims
the anchor when its form_key or one of its claimed alias keys
is in that set, and links to the anchored entity. If two entries
claim the same anchor, the anchor claims NEITHER: both fall
through to the normal tiers, and the collision is reported in
the marker and the roster report. Anchored entities are never
auto-merged into another entity, never auto-split, and their
canonical name is the anchor's name (§5). A human decision pins
identity; the matcher routes around it, never through it.

**Tier 1 — exact form match.** The entry's `form_key`
(`norm_name` of its display form) equals a form_key the entity
has been observed under (as a primary form, in any prior run).
Unique candidate required: if two entities have both been
observed under the same form_key (a genuine same-name collision
— two people the book calls by one name), tier 1 abstains and
the pair falls through to the cannot-link rules; it does NOT
pick the higher-appearance entity. Picking would be a guess
wearing a rule's clothes.

**Tier 2 — alias cross-match.** The entry's form_key ∈ the
entity's observed alias keys AND the entity's canonical form_key
∈ the entry's claimed alias keys (mutual claim — the reconcile
doctrine from `_roster_reconcile` :4918, lifted across runs),
with no third entity claiming either side (the uniqueness veto,
same source). This is the tier that links "Violet" (run 3) to
the entity observed as "Violet Sorrengail" (run 1): each run's
extraction claims the other form as an alias. Alias keys
attested ONLY by suspect observations (below) do not count as
tier-2 evidence — a suspended link cannot keep voting through
the aliases it brought in.

**Tier 3 — subsequence / title forms.** `_merge_tier`'s Tier 2
signals (titles, unambiguous subsequence) applied cross-run
between the entry's form and the entity's observed forms, with
the same unambiguity requirement. This covers form growth
("Garrick" → "Garrick Tavis") when no alias claim was extracted.
In v1 the comparison set is closed: the entity's OWN observed
forms in THIS work's store — no series-hint forms, no
learned-state forms, nothing imported. Series material enters
§4 only as split-suspect proposal input (§7), never as link
evidence at any tier.

**Proposals, never decisions (v1).** Pairs no tier links but
which carry a similarity signal (shared first token + overlapping
chapter ranges, or description-hash proximity) are emitted as
**split suspects** in the run marker and the roster report —
the `post_pass_merge` `possible_merges` pattern (:5084): surfaced
for a human, never applied. JEV (`ask_same_person`) and
embeddings may be added as *proposal generators* behind OC-1;
in v1 they do not run at this seam at all, so B adds zero LLM
calls and zero budget pressure to the existing 50-call decision
budget.

**Folds.** If two entries of the SAME run both link to one entity
(the form-flip pair extracted as two entries), the link folds
them: the entity keeps both observations and the run ships one
character. A fold is permitted **only** when the two entries'
chapter sets are disjoint — the Tier 1 cannot-link, enforced
cross-run. **If the entries co-occur in any chapter, the fold is
refused**, the entity link for the weaker entry is suspended
(§12, risk 1), and the event is reported. At the moment of
suspension, the observations that link carried are flagged
`suspect`: they are excluded from canonical weighting (§5)
and from tier-2 alias evidence (above) until a human resolves
them via the §9 correction commands, and the roster report
lists suspect observations loudest (§9). B never
overrides a within-run cannot-link; persistence that outranks
the current run's own co-occurrence evidence would let last
run's mistake overrule this run's text.

**Cannot-link vetoes (any one blocks any tier):**

1. The entity is already linked to a different entry this run
   (outside a permitted disjoint fold).
2. The entry is anchored (tier 0) to a different entity.
3. The entity is `merged`/`split` (tombstones don't accept links;
   links follow the redirect to the surviving entity and are
   re-vetted there).
4. A human NO exists for the pair of forms — a
   `same_person == False` label (from `--import-labels` data —
   read here even though D1 leaves its *learned-state* effect
   undecided; a recorded human NO is a veto in B regardless of
   D1's ruling on replay), or a `--roster-unlink` correction
   recorded in the store (§9).

**The human NO is a store-level invariant, not a matcher
courtesy.** The store refuses to record ANY union contradicting
a recorded NO for the involved forms — a matcher link, a fold,
the within-run-merge tombstone (below), or a work-key adoption
(§8) — and reports the collision in the marker and the roster
report. The veto governs what persisted identity may re-form;
it does not reach into the current run: the run's own within-run
merges still ship their rows for that run exactly as today.
What the NO prevents is the store carrying that identity into
the next run.

**Identity-level suppression (OC-3's rule).** A rejected-anchor
entity suppresses a run entry under any observed form ONLY when
the entry links to that entity at tier 0 or tier 1. Tier 2/3
links never carry suppression — they are inference, and a
human's rejection must not travel on inference. Every
suppression is an event: listed per run in the marker and the
roster report (§9), and the validation gate checks the converse
— non-rejected entities' rows unaffected (§10, signal 4).

**New entities.** An entry no tier links mints a new entity,
status `active`, marked **provisional** until it has ≥2
observations (OC-7). Provisional entities accept links normally
— otherwise every second run would re-mint the world — but a
provisional entity whose link is later contradicted has that
link suspended first and is asked about loudest in the report.
Provisional is a visibility state, not a weaker identity.

**Within-run merges after the seam.** If a later pass
(`dedupe_characters`, opt-in) merges two result characters that
carry different entity IDs, the merge is recorded in the store:
the lower-observation ID tombstones into the other with
`reason: "within-run merge"`, and both IDs' observations are
preserved under the survivor. The store learns from the run's
own final shape, not from its intermediate one. The tombstone
passes through the store-level human-NO invariant (above): if
the two IDs' forms carry a recorded NO, the store refuses the
tombstone, reports the collision, and the IDs stay separate in
the store — the run's shipped rows are unaffected.

---

## 5. Canonical name

Identity and display are separate decisions. The entity is who
someone is; the canonical name is what the DB row and the app
call them. Rule, applied at write/preview assembly from the
entity's observations:

1. **Anchor wins.** If the entity is anchored, canonical = the
   anchor row's name. Never changed by the rule below.
2. **Human override wins.** A `--alias` permanent alias for any
   observed form sets canonical to the alias target.
3. **Deterministic rule (OC-4):** among observed primary forms,
   the form with the greatest total appearances across runs;
   ties (and near-ties within 80% of the leader) break toward
   the **fullest** form (most tokens, then longest); remaining
   ties break toward the most recently observed. Rationale:
   appearance-weighting tracks the book's own usage, fullest-form
   preference matches reader expectation ("Violet Sorrengail"
   over "Violet"), and the rule is a pure function of the
   observation list — any canonical can be re-derived and audited.
4. **First-run behaviour is unchanged.** An entity observed once
   ships under that run's form — exactly today's behaviour. The
   rule only bites from the second observation on, which is
   precisely where today starts flickering.

**Suspect observations do not vote.** Observations flagged
`suspect` (§4 — attached via a link that later suspended) are
excluded from the appearance totals and tie-breaks above until
a human resolves them: a canonical name never rests on evidence
a suspended link carried in.

**Canonical changes are events.** When a run's computed canonical
differs from the name the work's rows currently carry, the run
marker records `{entity, from, to, reason}` and the roster report
lists it. A canonical change rewrites the character's row name
and every embedded relationship `"to"` string pointing at it —
that is the point (one stable name, everywhere) — but it is never
silent, because on the app surface it looks like a rename
(§8, §10).

---

## 6. Storage

**Recommendation: one JSON file per work under the learned-state
directory — `~/.ebook-processor/learned/rosters/<work_key>.json` —
managed by a new `RosterStore` class that copies StagingState's
persistence code shape. NOT a LearnedState file, NOT the staging
file, NOT a DB table.**

Why per-work files, where F chose one shared file:

- **Blast radius.** F's corruption cost is delay; B's corruption
  cost is a work's identity history. Per-work files bound a
  corrupt store to one book — every other work's identities are
  untouched. The series roster precedent (`series/<key>/
  roster.json`, :3099) already establishes per-scope files in
  this directory.
- **Reset is one file.** A work whose store has gone wrong
  (§10) is reset by deleting one file; the next run is stateless
  for that work only. No surgery inside a shared file.
- **Concurrency.** Books run concurrently under `--jobs`; per-work
  files mean concurrent books never contend on one file. Two runs
  of the SAME work concurrently remain last-writer-wins on that
  file — matching is read-then-write within one book's processing
  and books are the concurrency unit; the store takes an RLock
  per instance regardless (the F8/StagingState lesson).

Persistence discipline, copied from StagingState/LearnedState
without deviation: atomic save (temp file + `os.replace`),
loud corruption warning naming the file, original bytes preserved
as `.corrupt` before any overwrite, size guard warning.

**Store-level invariant (the human NO).** `RosterStore` itself
refuses any recorded union — link, fold, merge tombstone,
adoption — that contradicts a recorded human NO for the involved
forms (§4), and surfaces the refusal. Enforcement lives in the
store, not the caller, so no future code path can re-form a
vetoed identity by forgetting to check.

**Failure direction — stated honestly, because it differs from
F.** A *missing or corrupt* store degrades to the stateless path:
the run proceeds exactly as today and says so on its face. That
direction is safe. A *loadable but wrong* store is the dangerous
case, and no load-time check can detect it — that is what link
provenance, suspension, anchors, and the report (§4, §9, §12)
exist for. B does not claim F's "corruption can only delay"
property, because B's store holds authority F's never did. What
B claims instead: every exercise of that authority is recorded,
attributable to a tier and a run, and reversible.

**Growth.** Bounded by a work's cast size across runs: entities
≤ distinct persons ever extracted + split residue; observations
one per entity per run. A long-lived work reprocessed monthly
accrues ~12 observations/entity/year — trivially small. No
absence-expiry for entities (§12): a character missing from one
run's extraction is extraction noise, not a death certificate.
Retirement happens only via merge, split, or human action.

---

## 7. Interaction with existing machinery

- **Roster reconcile (A).** Unchanged, runs first (seam order
  below). B's membership discipline is reconcile's, lifted: B
  never creates a *character* — it mints IDs only for entries the
  run itself sighted, and folds only sighted entries. Reconcile
  merges *within* the run before B sees the roster, so B never
  has to re-decide them; reconcile's merges are simply part of
  the run's final shape B observes.
- **Learned state + D1 (open).** B writes NOTHING to
  LearnedState: no `record_merge` calls, no nickname entries.
  Cross-run roster links live and die inside the per-work store;
  they never become cross-book learning and never replay into
  another book. This is deliberate containment: D1 (learned-state
  governance — replay guards weaker than earning guards, no
  negation, preview learning) is undecided, and B must not add a
  second, larger replay surface while it is. Two honest notes:
  (a) within-run passes still call `record_merge` exactly as
  today — B neither adds to nor removes that behaviour;
  (b) B's own matcher was designed with the veto parity D1 asks
  about (cannot-link checks run on *every* link, *every* run —
  there is no "trusted because persisted" path), so a broad D1
  ruling lands on B as a reporting question, not a redesign.
  The preview-observation question is B's OC-2, framed as
  independent of D1 on the F/OC-4 pattern.
- **Replace-wholesale write path.** Untouched mechanics.
  `_partition_claims` still partitions by name key; B feeds it
  *stable* names. Anchor seeding (§8) reads the same fetch
  `write_claims` performs. One improvement falls out: preserved-
  row dedupe keyed by canonical name now matches across form
  flips, because the run ships the anchored entity under the
  anchor's name (§5 rule 1) instead of under whatever form this
  run's extraction happened to pick.
- **Anchor-fetch failure.** Anchor seeding re-reads the DB
  rows at write time (§8). If that re-read FAILS, the
  discipline splits on what the store already holds: a store
  that already holds anchored entities DISENGAGES B for the
  run — the run proceeds exactly as flag-off and says so
  loudly in the marker and on the preview's face — rather
  than matching against anchors it could not verify. A store
  with no anchors yet proceeds with a warning (no curated
  substrate exists to protect) and seeds anchors on the first
  successful fetch.
- **Checkpoint / resume.** The seam runs after the roster is
  final, on every completion path — fresh, resumed, or retried —
  over the same final roster data, so resume needs no store
  awareness and checkpoints need no new fields. Observation
  idempotency: a completing run records observations under its
  run ordinal; if the run's roster fingerprint (hash of sorted
  form_keys + summed appearances + chapter count) equals
  `last_run_fingerprint`, the run is the same extraction
  completing again (resume-after-
  crash at the seam boundary) — observations refresh in place,
  no new ordinal is minted, and canonical weights do not
  double-count one extraction. This is F's "resumed runs are not
  independent sightings" discipline, adapted: for B the unit is
  the *extraction*, not the invocation.
- **Series scope.** v1 is per-work. The existing series hint
  store feeds split-suspect PROPOSALS only — surfaced in the
  report like any other suspect pair — and is never link
  evidence at any tier, tier 3 included (series-mates share
  names across books; the same name in book 2 is not
  automatically book 1's person, and v1 does not build the
  machinery to tell). Entity IDs are work-scoped; nothing in
  the layout blocks a later series layer that *references*
  per-work entities.
- **dedupe_characters (opt-in).** Runs after the seam on result
  characters that now carry `entity_id`; its merges fold IDs
  per §4. Flag-off for dedupe = unchanged behaviour.
- **Principals filter.** Operates on result characters as today.
  The store observes the FULL roster (the series-roster
  precedent at :10596: non-principals are recorded too — a minor
  in this run may be a principal in the next, and identity must
  already exist when that happens).

**Seam placement (one seam, like F).** In `process_file_v2`,
immediately after `post_pass_merge` (:10332) and before
`v2_reduce` (:10343):

```
roster_assign_entities(roster, store, *, anchors, run_ordinal)
    -> annotates each roster entry with entity_id;
       folds disjoint dual-linked entries;
       returns link/fold/suspect stats for the marker
```

`v2_reduce` then carries `entity_id` onto result characters and
relationships (endpoints gain `from_id`/`to_id` alongside the
name strings); the write builders (`_build_character_rows`,
`_build_rel_map`) resolve IDs → canonical names at row assembly
and otherwise ignore them. Every downstream consumer inherits
identity from that one annotation — there is no second matching
pass for `plan_replace` to disagree with. The result gains a
`roster_persistence` marker **only when the flag is on** (the
reconcile/staging marker discipline): `{linked, folded, minted,
suspects: [{a, b, why}], suspended: [...], suppressed: [...],
canonical_changes: [...]}`. `save_preview` prints a one-line summary; a preview that
records observations declares it on its face (OC-2).

---

## 8. Migration + the app seat

**Works already in the DB.** Nothing migrates in the DB. The
first flag-on run for an existing work:

1. Seeds **anchor entities** from the work's existing
   `book_characters` rows before matching: every preserved row
   (confirmed / rejected / contributor, per `_partition_claims`)
   becomes an anchored entity whose canonical is the row's name.
   Human-curated identity therefore exists *before* the run's
   own roster gets to define anything — the first run links
   INTO the human's identity substrate instead of minting a
   parallel one beside it (§12, risk 2). If this seeding fetch
   fails, §7's anchor-fetch discipline governs: disengage
   loudly when the store already holds anchors; proceed with
   a warning when it holds none yet.
2. Runs the matcher; unanchored entries link or mint per §4.
3. Ships rows under canonical names — for anchored entities,
   the names the DB already carries, so a first B run is
   app-invisible except where it repairs a form flip — and a
   rejected anchor suppresses per §4's identity-suppression
   rule (tier 0/1 links only), never on a tier 2/3 inference.

Works never reprocessed keep today's rows forever; B is inert
until a work is run with the flag. A work processed on a machine
without the store (the learned dir is per-machine — the same
caveat learned state and staging already carry) behaves
statelessly for that run and re-adopts on return via the work-key
chain (F's `adopt_for` pattern, :3536: fallback-key entries are
adopted into the work_id entry on exact title+author match, and
adoptions are listed in the next roster report). Adoptions are
recorded through the store, so the store-level human-NO
invariant (§4, §6) governs them like any other union.

**A named limit B inherits, not creates: the canonical
collision.** Two genuinely distinct entities can compute the
same canonical form_key (two people the book calls by one
name, both stabilized to it). At row assembly the name-keyed
write path emits ONE `book_characters` row for that name — the
two entities' claims land in a single row. That is a
pre-existing limit of the name-keyed DB: today's database
cannot represent two same-named characters in one work at all.
B does not create the limit and v1 does not lift it. What v1
does: the roster report lists canonical collisions explicitly
(§9), so the conflation is a named, countable event rather
than a silent one. Lifting it requires `entity_id` in the
shipped rows — that is OC-6.

**The app seat's stake — what must be confirmed, not assumed.**
In v1 the DB contract is byte-identical: same tables, same
columns, same name strings in `book_characters.name`, same
embedded relationship JSON with name-valued `"to"`. Three things
the app Builder must confirm before B's validation gate (§10),
because the processor cannot see the app's read paths:

1. The app does not join characters across works by name
   (character pages, "more by this character", reciprocal
   relationship rendering). If it does, canonical stabilization
   changes what those joins return — almost certainly for the
   better, but it is the app's call to know.
2. No app surface treats a character-name change as an error or
   a deletion-plus-creation (reading progress, shelves, or notes
   keyed by name would break silently on a canonical change).
3. Whether the app wants the `entity_id` exposed later (a stable
   handle for character pages) — v1 does not write it anywhere
   in the DB; if the app seat wants it, that is a v2 conversation
   with its own migration, not a v1 rider (OC-6).
4. The canonical-collision limit above: where two distinct
   entities share one canonical name, the app receives one row
   for both — as it would today. The app confirms no surface
   assumes one row = one distinct person for same-named
   characters; and if the app ever needs same-named people
   kept distinct, that requirement is exactly the OC-6
   `entity_id` case.

---

## 9. Reporting

**`--roster-report`** (command precedent: `--learn-status`,
`--staging-report`). Per work: entities minted/active/merged/
split with the accounting line `minted = active + merged +
split`, `UNACCOUNTED: 0`; the suspension ledger — suspended
links plus the entities they touch — as its own separate line;
suspect observations (§4) listed FIRST and loudest, with the
suspending evidence — the same treatment provisional
contradictions get; per entity — canonical, all observed forms
with run ordinals, anchor if any, status; links by tier;
suspended links with the contradicting evidence; suppression
events for the run (entity, suppressed form, the tier that
carried it, §4); canonical collisions (§8) listed explicitly;
split suspects awaiting a human; canonical changes to date.
This report is the correction surface: a human who spots a
wrong link edits it via
**`--roster-unlink <work> <entity> <run-form>`** /
**`--roster-link`** companion commands (build slice 5) — the
corrections are themselves recorded as anchor-grade events in
the store, so a fixed link cannot silently re-form (the human's
NO is a store-level invariant from then on, §4). The third
correction is **`--roster-split <work> <entity> <form…>`**: it
partitions a polluted entity's observations by form into
successor entities — each successor minted fresh, carrying its
form's observations and history — and the original tombstones
as `split` into those successors, its links re-vetted against
each successor on the next run. Split is the correction the
worst error class needs: without it, a false merge absorbed
into one entity could be vetoed going forward but never taken
apart.

**Dashboard hooks (`preview_variance.py`, additions only — the
legacy name-keyed output stays frozen, per the dashboard's own
rule):** an entity-keyed section reading `entity_id` from the
previews' characters/roster: entity-set Jaccard (full +
principals), endpoint-ID relationship Jaccard over
`(from_id, to_id, type)` triples, canonical-name churn (names
that changed for a persistent entity), and the split-suspect
count. The legacy name-Jaccards print alongside — under B they
are expected to *rise* (canonical names stop flickering), and
reporting both keeps the improvement honest: entity Jaccard
measures identity stability; name Jaccard measures what the app
actually shows.

---

## 10. Measurement — the validation gate for B itself

Fixed set, before/after, signals named in advance (the
processor-quality-loop discipline, same shape as F's §6):

- **Books:** Fourth Wing (the measured patient) + Daemon (second
  corpus; carries the Sebeck surname-trap family and the
  Pete/Peter nickname pair). **Two fresh runs each** with
  `--persist-roster` (run 1 seeds, run 2 is where cross-run
  identity can fire), dashboard-scored against each other and
  against the existing flag-off pairs (−15/−16 for FW).
- **Pre-registered acceptance signals:**
  1. **Identity stability:** entity full-roster Jaccard ≥ 0.90
     and entity principals Jaccard ≥ 0.80 across the two flag-on
     runs (name-keyed baselines: 0.671 / 0.476). The shipped-name
     Jaccards for the same pair are reported and must not fall
     below the flag-off baselines.
  2. **Relationship stability:** endpoint-ID relationship
     Jaccard ≥ 0.40 (name-keyed baseline: 0.051), with the
     caveat printed on the scorecard that claim *presence*
     variance is extraction's, not B's — B is scored on the
     endpoint-identity component it controls.
  3. **No veto violations:** zero folds over co-occurring
     entries; zero links contradicting a within-run cannot-link
     in either run; every suspension the runs produced is listed
     and explained on the scorecard.
  4. **Anchor respect:** 100% of the works' pre-existing
     confirmed/rejected/contributor character rows are anchored
     before matching; zero anchored canonicals changed by the
     rule; a rejected-anchor entity ships no candidate row
     under any observed form (per §4's suppression rule — tier
     0/1 links only); and the converse — every non-rejected
     entity's rows on the gate books are exactly what the
     flag-off runs ship (suppression must not overreach).
  5. **Family invariance:** labelled family outcomes identical
     to the flag-off runs modulo pre-existing extraction
     variance — B changes identity bookkeeping only; any family
     delta beyond that is a defect, not a side effect.
  6. **Accounting closes + residual is a number:** minted =
     active + merged + split, `UNACCOUNTED: 0` over IDs, with
     the suspension ledger (suspended links + affected
     entities) reported as its own line; split suspects are
     listed by name (the wrong-surname residue is counted, not
     waved at). The scorecard additionally reports, per book:
     suppression events (§4), canonical collisions (§8), and
     suspect-observation counts (§4) — zero included; absence
     is a result, not an omission.
- Scorecard format follows F's: trapped/suspect claims **by
  name**, per book, with link tiers shown — the design's worst
  cases countable at a glance.

---

## 11. Failure modes + abuse cases

- **Corrupt store.** F8 pattern: loud warning, `.corrupt`
  preservation, that work's store starts EMPTY → the run is
  stateless for that work and says so in the marker. Identity
  history is lost; correctness is not (stateless is today's
  behaviour). Per-work files bound this to one book (§6).
- **Store/DB divergence.** The work was deleted and re-ingested
  under a new `work_id`: the old store sits under the old key,
  inert; the new work seeds fresh, anchors first. The roster
  report lists stores whose work_id no longer resolves, so
  orphans are visible, never silently authoritative.
- **Two extractions, one identity (name collision in the book
  itself).** Two genuinely distinct people sharing a name form:
  tier 1 abstains (§4), entries mint/remain separate entities,
  and the collision is reported. The failure B refuses is the
  confident wrong link; the cost it accepts is an unresolved
  collision a human settles once via `--roster-link`.
- **Edited/revised editions.** Same work_id, materially
  different text (a revised edition reprocessed): observations
  accrue to the same entities — usually correct (same cast),
  and form drift shows in the report. A genuinely different
  book under a recycled work_id is a work-identity problem B
  inherits from `resolve_work` (:8573), not one it creates.
- **Runaway minting.** A pathological extraction (every run
  inventing forms) mints entities without bound in principle;
  bounded in practice by cast size, and the report's
  minted-per-run line makes the pathology visible in one run.
- **The store as a target.** The store is local JSON written
  only by the processor; a hand-edited store can forge links.
  Same trust posture as learned state today (local, single
  user); anchors re-read from the DB at write time bound the
  damage a forged store can do to curated identity (§7).

---

## 12. Red team — the three most likely ways B makes things worse

**1. A wrong persisted link poisons every future run.**
The head-on case. A false cross-run link — the extraction error
"Rhiannon Sorrengail" linked to Rhiannon Matthias's entity, or
two distinct same-named people folded — does not flicker away
next run the way today's errors do. It *persists*: canonical
choice, anchor suppression, and relationship endpoints all build
on it, and replace-wholesale faithfully re-ships the merged
identity every run. B converts a one-run variance error into a
permanent corpus error — the exact error class the doctrine
calls the worst. *Mitigations:* deterministic-only auto-link
tiers with uniqueness + cannot-link vetoes on every link (§4);
JEV/embeddings propose-only in v1; every link carries tier +
evidence provenance; contradicted links auto-suspend rather
than silently persist; merges tombstone with redirects so a
wrong link can be unwound by a human via `--roster-unlink`,
whose NO then vetoes re-formation. The residual risk is real
and is why the validation gate (§10) scores veto violations
at zero tolerance.

**2. Anchor capture by an early bad run.**
The first persisted run defines the entity substrate every
later run matches against. A polluted first roster — and −16
produced one: aliases absorbed into wrong entries, a "Dad"
entry carrying Brennan's key — becomes the identity bedrock.
Later, better extractions link into the pollution and inherit
its shape; the store launderers one bad night into ground
truth. *Mitigations:* human rows seed FIRST (§8) — the
substrate's most-trusted layer is curation, not extraction;
single-observation entities are visibly provisional (OC-7);
the matcher re-vets links against each new run's evidence, so
a first-run shape contradicted by run 2's co-occurrence data
suspends instead of compounding; and the per-work file makes
the ultimate correction cheap — delete one file, re-seed from
anchors + a good run. Capture is possible; it is neither
silent nor permanent.

**3. Canonical lock-in + rename shock on the app surface.**
Persistence stabilizes names — including, sometimes, the wrong
one: appearance-weighting can lock a short form ("Violet")
where readers expect the full name, and once locked, the name
stops tracking the book's best evidence. Conversely, when the
canonical DOES change, it changes everywhere at once: the row
name and every embedded relationship `"to"` string rewrite in
one run, which on the app surface can look like a character
vanishing and a stranger arriving. *Mitigations:* the rule
prefers the fullest form on near-ties (§5); anchored names are
rule-proof; canonical changes are marker + report events,
never silent, so a rename is always attributable to a run and
a reason; and the validation gate requires shipped-name
Jaccards to be reported alongside entity Jaccards, so a
canonical pathology shows up as a number before B is anyone's
default.

---

## 13. What B reuses from F — and what it rejects

**Reused (same patterns, same discipline):**

- Separate store class + file in the learned-state dir, kept
  clear of LearnedState and of D1 — now for the stronger reason
  that B's links must never become cross-book learning.
- The F8 persistence discipline verbatim: atomic save, loud
  corruption with `.corrupt` preservation, RLock serialization.
- The work-key fallback chain + adoption on work_id resolution
  (`staging_work_key` :3257, `adopt_for` :3536), adoptions
  listed in the report.
- Run-ordinal provenance and the completed-extraction-only
  observation rule (adapted to fingerprint idempotency, §7).
- Flag + marker discipline: default OFF, marker only when on,
  state effects declared on the preview's face.
- The accounting identity in reporting (every minted ID
  accounted for; `UNACCOUNTED: 0`) and the pre-registered
  fixed-book validation gate.

**Rejected / adapted (F's answers do not transfer):**

- **F's delay-only failure guarantee — rejected as a claim.**
  F's store holds no authority to create output; B's store
  asserts identity. B substitutes provenance, suspension, and
  reversibility for F's structural safety, and says so (§6).
- **F's absence-based expiry — rejected for entities.** A
  staged claim unsighted for 3 runs is evidence of absence; a
  character unsighted in one extraction is evidence of nothing
  (extraction misses people constantly — that is the measured
  disease). Entities retire only via merge/split/human action;
  observations age in the report instead.
- **F's single shared store file — rejected.** Per-work files,
  for blast radius and reset cost (§6).
- **F's identity keys — rejected in kind.** F keys claims by
  catalog id / normalized name because claims have no deeper
  identity available. B exists because name-keyed identity is
  the defect; its IDs are minted opaque and names are demoted
  to observations.
- **F's sticky promotion — no analogue.** A promoted claim
  stays promoted; a persisted *link* is re-vetted every run
  and can suspend. Identity faces fresh evidence each run in
  a way claim sightings do not.

---

## 14. Build plan (slices in order; each independently testable)

1. **`RosterStore`.** Per-work file load/save with the F8
   pattern, entity minting (never-reused seq), observation
   append with fingerprint idempotency, tombstone/redirect
   handling (merged recorded once, in `retired`), link-state
   (suspension) ledger, the store-level human-NO invariant
   (§4, §6), and the accounting accessor (`minted = active +
   merged + split`, `UNACCOUNTED: 0`). Tests: corruption
   degrades to stateless + warns; IDs never reused across
   merge/split fixtures; fingerprint-equal rerun adds no
   observation; accounting closes on randomized merge/split
   sequences; a recorded union contradicting a human NO is
   refused by the store itself.
2. **Matcher (tiers 0–3 + vetoes + folds).** Pure function over
   (roster, store snapshot, anchors). Tests: exact-form link;
   alias-cross link (the Violet case); uniqueness abstention
   on same-form collision; co-occurring fold REFUSED +
   suspension recorded; anchor claim precedes all tiers;
   human-NO veto; tombstone redirect re-vetted.
3. **Seam + threading.** `roster_assign_entities` at the §7
   seam behind `--persist-roster`; `entity_id` through
   `v2_reduce` onto characters + relationship endpoints;
   write builders resolve canonical names; marker +
   `save_preview` line. Tests: flag-off output byte-identical;
   flag-on first run = today's names (anchors aside); resumed
   run records one observation set; dedupe fold tombstones
   the folded ID.
4. **Canonical rule + anchor seeding.** DB-row anchor fetch at
   work resolution; canonical computation + change events.
   Tests: anchor name immutable under the rule; appearance/
   fullest-form tie-breaking on fixture observation lists;
   rejected-anchor entity suppresses its forms at row build.
5. **Reporting + correction commands.** `--roster-report`,
   `--roster-link` / `--roster-unlink` (recorded as tier-0
   events), `--roster-split` (observation partition into
   successor entities, the original tombstoned as `split`),
   dashboard entity section (additions only). Tests:
   report accounts for every minted ID; unlink vetoes the
   link's re-formation on the next run; split partitions a
   fixture entity's observations by form and the original
   accounts as `split`; dashboard lines render
   from entity-carrying previews and are absent otherwise.
6. **Validation gate (not code).** The §10 protocol, run and
   scored, scorecard written to `eval/` beside this doc;
   ship/no-ship on the pre-registered signals. App-seat
   confirmations (§8) are gate prerequisites.

---

## 15. Open choices (recommendation in bold)

- **OC-1 — Auto-link authority.** **Deterministic tiers only;
  JEV/embeddings propose split suspects but never link** vs
  adding a JEV auto-link at p ≥ 0.85 with uniqueness + vetoes
  for the residual set. The deterministic-only form adds zero
  LLM calls and keeps every link rule-auditable; the JEV form
  would catch bridge-less flips at the price of letting the
  judged mechanism grade its own homework.
- **OC-2 — Preview runs record observations.** **Yes, with the
  F/OC-4 conditions** (declared on the preview's face; framed
  as independent of D1's subject matter — per-work bookkeeping,
  not cross-book learning; reopens if D1 rules broadly) vs
  write-runs only (persistence then cannot accumulate during
  the validation gate that must measure it).
- **OC-3 — Rejected-anchor suppression by identity.** **A
  rejected character's entity suppresses matching run entries
  under ANY observed form** (the human rejected the person,
  not the spelling) vs today's name-exact suppression only.
  The identity form closes a real evasion — a rejected
  character resurrecting under a form flip — at the price of
  trusting the link that carries the suppression. As amended
  by the red team, its conditions are rule text, not asides:
  suppression applies ONLY to entries linked at tier 0/1
  (§4), every suppression is listed per run in the marker and
  the roster report (§9), and the validation gate checks the
  converse — non-rejected entities' rows unaffected (§10,
  signal 4).
- **OC-4 — Canonical rule.** **Appearance-weighted, fullest
  form on near-ties** (§5) vs longest observed form always vs
  first-observed form frozen. Longest-always is simpler but
  crowns one-off formal sightings ("Violet Sorrengail,
  Dragon-Queen" style flourishes); frozen-first makes run 1
  canonical forever, which is anchor capture (§12) as policy.
- **OC-5 — Series scope.** **Per-work only in v1; series
  hints feed split-suspect proposals only, never link
  evidence** vs series-level entities
  in v1. Series identity is where the same-name-different-
  person risk is highest and the measurement corpus is
  thinnest; v1 earns it later or not at all.
- **OC-6 — DB footprint.** **Internal-only IDs; DB rows
  byte-identical in v1** vs also stamping `entity_id` into the
  shipped rows (new column / relationships JSON) for the app.
  Stamping is the app's feature request to make, with its own
  migration — not a v1 rider (§8). Disclosure (red-team
  amendment): internal-only also keeps the canonical-collision
  limit (§8) — two distinct entities computing the same
  canonical form_key still write as one DB row, as they do
  today. Exposing `entity_id` is the unlock that would let
  the app represent same-named people distinctly; if that
  need ever becomes real, it argues for the stamping option
  on its own merits.
- **OC-7 — Provisional entities.** **Single-observation
  entities link-eligible immediately, flagged provisional in
  every report until a second observation** vs requiring two
  observations before an entity may accept links (an
  F-style persistence bar for identity itself). The bar form
  is safer against first-run capture and slower to help:
  nothing stabilizes until a work's third run. Controls
  added by the red team to the immediate form: observations
  attached via a link that later suspends are flagged
  `suspect` — excluded from canonical weighting (§5) and
  from tier-2 alias evidence (§4) until a human resolves
  them — and are listed loudest in the roster report (§9),
  so a provisional link that goes wrong stops voting at once
  and shows up first.
