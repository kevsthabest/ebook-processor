# Relationship validation fixes — spec (shipped 2.13.8)

Motivation: the 2026-10-17 relationships audit (Daemon run 16 +
stored Fourth Wing data, `reviews/relationships-audit-2026-10-17.md`
in the sweep workspace). Three stacked defects:

1. The pair-evidence grader could not see surname-anchored
   kinship, so true family claims reached the validator looking
   unproven and dropped (Daemon run 16: Laura Sebeck spouse
   P=0.12, Chris Sebeck child P=0.20).
2. The validator had never been told a surname alone names a
   person, and scored the establishing construction
   "Sebeck's son, Chris" at 0.20 while scoring "my sister"
   constructions at 0.96.
3. Non-family claims (friend/enemy/colleague/…) were never
   validated at all, and the stored data shows the cost: Fourth
   Wing pairs typed on quotes that do not involve the pair
   (Dain→Xaden "friend" on a Dain–Violet quote) and pairs whose
   only support is two names co-occurring in one sentence.

## A1 — Surname as a mining form

`_pair_surname_form(name, roster)` returns the endpoint's surname
(last token of the canonical roster name) under guards: at least
two name parts; not the first name itself; not an initial; not a
kinship marker word; not another roster character's first name
(then the bare word more likely names that person). It is NOT
required to be roster-unique — sharing it is what family means.

The miner (`_mine_pair_evidence`) builds surname-augmented
patterns for grading, so possessive/of constructions anchored on
the surname can grade `binding`. Placement uses a span rule
(`_appears`): a surname-only match does not place an endpoint
when it is merely the tail of the OTHER endpoint's full name
("Fen Riorson" does not place Xaden Riorson). `_pair_name_forms`
itself is unchanged.

Pinned trade-off: for a pair sharing a surname, a surname-anchored
sentence grades binding even when the text does not say WHICH
family member anchors it ("Sebeck's son, Chris" mined for
(Laura, Chris)). Which Sebeck it means is the validator's
question; the grader's is whether the sentence is
establishing-shaped.

## A2 — Validator prompt: judge the construction

`DecisionValidator.validate_relationship` now teaches: a person
may be referred to by full name, first name, surname alone, or a
clear pronoun; a possessive construction naming one person's
relative with the relative alongside ("Sebeck's son, Chris")
states the relationship directly; a quote binding a relative to
a third person does not support a claim about this pair. The
printed band action no longer claims "downgrade to other" for
non-family types.

Gate: `evaluate-relationships.py` +
`eval/relationship_cases.json` — 12 gated cases (real quotes from
Daemon and Fourth Wing wherever a real quote exists) and 2
report-only cases documenting shapes that are unjudgeable from a
quote alone. Live run (needs the decision-model key):

    py evaluate-relationships.py --config config.json

`--dry-run` exercises the plumbing with stub scores. The prompt
guidance is also pinned by unit tests so it cannot silently
regress.

## A3 — Non-family claims are validated

In `v2_reduce`, after the family pass (family claims claim budget
first), every non-family claim that has evidence to judge — its
extracted quote, or a mined both-names sentence when it has no
quote — goes through the same validator. Outcomes are two-tier:

- P ≥ drop threshold (0.30): the claim keeps its type,
  `validation_note="kept"`, P recorded.
- P < 0.30: the claim drops (write mode: into
  `dropped_relationships`; preview: stays, note `would_drop`).

There is no downgrade tier: a doubtful friend claim has no
meaningful "other" to become. `"other"` claims are not judged
(the judge cannot weigh a typeless claim; family claims
downgraded to "other" were already judged). Evidence-less,
unmineable claims are untouched — the write pass's
mined-evidence rules decide them, as before. Validator errors
fail open (`validator_error`), budget exhaustion records
`budget_exhausted` — both identical to the family pass.

Interaction with the mined write-evidence pass: the family bar
there is now traced by `validation_note == "downgraded"`
specifically, not by the mere presence of a note. A non-family
claim judged "kept" on a plain mined sentence still receives
that sentence as its write evidence.

Judging the mined sentence IS the interaction test for
evidence-less non-family claims: a sentence where the pair
merely co-occurs no longer ships unexamined.

## Data follow-up (not in this change)

Stored character data is NOT hand-patched: processor rows are
replaceable, and a hand edit would be wiped by the next write
run. Once this ships, reprocess Fourth Wing from the corpus
(115 chapters, 100% vectors) and Daemon, and let replace-wholesale
refresh the ai rows under the fixed behaviour. The live eval
above should be run first; if gated cases miss, the prompt —
not the thresholds — is the tuning surface.
