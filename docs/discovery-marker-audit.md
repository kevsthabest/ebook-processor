# Marker-coverage audit for shadow discovery (spec §3.2)

Measured 2026-10-17 on the v2.13.23 tree, before the discovery
pre-filter was wired. Method: a labelled set of 22 book-shaped
sentences, each stating its bond in a binding construction
(possessive / of-phrase / pronoun possessive, equated by
apposition or copula — the constructions `_pair_binding` grades),
spanning seven relationship categories, plus three co-presence
negatives. The labelled set lives in
`tests/test_discovery_shadow.py` (`TestMarkerCoverageAudit`),
which pins every number below: if a future marker change moves
a number, the suite says so.

Coverage = fraction of the category's sentences grading
**binding** (the only grade that advances a discovery pair).

| category | n | `_PAIR_MARKER_RE` hits | kinship binding | extended binding |
|----------|---|------------------------|-----------------|------------------|
| kinship  | 4 | 4/4 | **4/4** | 4/4 |
| ally     | 3 | 0/3 | **0/3** | 3/3 |
| rival    | 3 | 0/3 | **0/3** | 3/3 |
| mentor   | 3 | 0/3 | **0/3** | 3/3 |
| friend   | 3 | 0/3 | **0/3** | 3/3 |
| romantic | 3 | 0/3 | **0/3** | 3/3 |
| enemy    | 3 | 0/3 | **0/3** | 3/3 |
| total    | 22 | 4/22 | 4/22 | 22/22 |

Finding: the gap is total, not partial. The production miner is
a kinship miner — on a family-heavy benchmark it looks healthy
(4/4) while missing every non-kinship bond construction (0/18).
Left unextended, shadow discovery's pre-filter would have
advanced only family-shaped pairs, and the §3.5 promotion
measurement would have silently measured kinship discovery
alone.

Extension taken: discovery grades with an extended marker
vocabulary (`_DISCOVERY_EXTRA_MARKERS`: ally, rival, mentor,
friend, enemy, lover, beloved, sweetheart, girlfriend,
boyfriend, nemesis, companion, partner, and plurals) through the
SAME binding constructions (`_pair_binding_with`). The
production miner's markers are unchanged: widening them would
re-grade evidence for claims extraction already made, which a
shadow step has no reason to do.

Precision check (the K6 line): all three negatives — plain
co-presence ("Violet and Xaden stood in the courtyard") and two
third-party bindings ("Violet watched Xaden's father from the
wall", "Her brother Brennan waved at Violet and Xaden") — grade
plain / co-occurrence under BOTH vocabularies, never binding.
The extension widens which bond words the constructions can
carry; it does not weaken what a construction must do to count,
so co-presence still never advances a pair.
