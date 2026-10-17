#!/usr/bin/env python3
"""Evaluate the relationship validator against labelled claims.

Production-faithful: every case is judged by the real
DecisionValidator.validate_relationship from process-portable.py —
the same prompt, the same thresholds (_REL_VALIDATION_THRESHOLD
0.70 / _REL_DROP_THRESHOLD 0.30) the pipeline applies in
v2_reduce. The set exists because of a specific failure: the judge
scored the surname-anchored construction "Sebeck's son, Chris" at
0.20 in production (Daemon run 16) while scoring first-person
constructions ("This is my sister, Violet") at 0.96, and true
family claims dropped. The cases pin the constructions — surname
possessives, pronoun apposition, third-party bindings, and the
non-family junk pairs the stored Fourth Wing data carried — so a
prompt change is gated on scores, not impressions.

Usage:
    python3 evaluate-relationships.py                  (openrouter;
        needs OPENROUTER_API_KEY or "openrouter_api_key" in the
        config passed with --config)
    python3 evaluate-relationships.py --base-url http://127.0.0.1:8888/v1
    python3 evaluate-relationships.py --dry-run        (no network)
    python3 evaluate-relationships.py --config config.json --corpus
        (production path: each case is judged WITH the context
        window the pipeline computes — the verbatim sentences
        around the quote, located in the corpus chapter texts of
        the case's work. Without --corpus the run is prompt-only
        and says so: it measures the bare validator, NOT what
        v2_reduce sends since 2.13.9.)

Labels (eval/relationship_cases.json): a list of {from, to, type,
quote, expect, note, stub_score}. expect is "keep" (P must reach
the keep threshold), "drop" (P must fall below the drop
threshold), or "any" (report-only: the score is recorded, never
gated — for shapes whose honest answer is "unjudgeable from the
quote alone"). --dry-run exercises the plumbing with each case's
stub_score instead of a model call.

Every case is judged repeatedly (spec §4): three draws, five
when the first draw lands within 0.15 of a band edge, because a
single draw near an edge is a coin flip and the eval exists to
measure the judge, not its luck. A case's outcome is the band
every answered draw agrees on; draws that straddle an edge make
the case UNSTABLE — reported, and failing the gate when gated —
rather than letting one draw pick a side. The report's headline
is four metrics over the gated cases (precision, recall, type
accuracy, abstention), each with its case counts, per book and
pooled; the old answered/miss count stays for continuity.

Exit codes: 0 = every gated case in band and stable; 1 = a gated
case out of band or unstable; 2 = configuration error (no
endpoint/key).
"""
import argparse
import importlib.util
import json
import statistics
import sys
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _context_for_case(pp, chapters, quote):
    """The production context for one case: locate the quote in
    the work's chapter texts and frame its neighbouring sentences
    exactly as v2_reduce does. Returns (context, located) —
    located False means the quote does not appear verbatim in the
    corpus text (an edition or normalization difference worth
    seeing, not papering over)."""
    for ch in chapters or []:
        text = ch.get("text", "")
        if pp._locate_quote_span(quote, text) is None:
            continue
        window = pp._evidence_window(quote, text)
        if not window:
            return "", True
        return ("Sentences around the quote, from the same "
                f"passage: {window}"), True
    return "", False


def _resolution_for_case(pp, case):
    """The referent-resolution measurement for one case (spec
    §2.4/§2.5): report-only plumbing, never a verdict.

    A case carrying a "resolution" fixture — a self-contained
    chapter text, a roster of names, the chapter's focal character
    where extraction would have identified one, and the endpoint
    to test — is run through production's provenance builder for
    that endpoint. Returns "resolved-surname -> <name>" when the
    resolution fires for the tested endpoint, "silent" when it
    does not (ambiguity, or a resolution to someone else: neither
    manufactures an antecedent for this claim), and None when the
    case carries no fixture. Whether a fired resolution moves the
    case's score is measured by the draws, not promised here."""
    fixture = case.get("resolution")
    if not fixture:
        return None
    roster = {}
    for name in fixture.get("roster") or []:
        pp._roster_update(
            roster,
            [{"name": name, "aliases": [], "role": "supporting",
              "description": "", "evidence": ""}],
            1)
    endpoint = fixture.get("endpoint") or ""
    rel = {"from": case["from"], "to": case["to"]}
    key = "from" if endpoint == case["from"] else "to"
    prov = pp._claim_endpoint_provenance(
        rel, case["quote"], fixture.get("chapter_text", ""),
        fixture.get("chapter_index", 0), roster,
        fixture.get("focal"))
    ep = prov.get(key) or {}
    if ep.get("matched_form") == pp.MATCH_FORM_RESOLVED_SURNAME:
        return f"resolved-surname -> {ep.get('endpoint')}"
    return "silent"


def _resolve_case_context(pp, chapter_sets, quote):
    """Pick a case's context across candidate chapter sets.

    A corpus can hold the same book twice under different keys
    (print ISBN vs ASIN editions), so a work name may match
    several readable copies; the text itself decides which copy
    a quote came from — the first set where the quote locates
    wins. Returns (context, located, ambiguous): ambiguous says
    more than one readable copy matched the work name."""
    ambiguous = len(chapter_sets) > 1
    for chapters in chapter_sets:
        ctx, located = _context_for_case(pp, chapters, quote)
        if located:
            return ctx, True, ambiguous
    return "", False, ambiguous


def _band(p, rel_type=""):
    """The production outcome for a score, by claim family.
    Family claims are three-tier (keep at the validation
    threshold, downgrade in the middle, drop below the drop
    threshold); non-family claims are two-tier — at or above the
    drop threshold the claim keeps its type, below it drops.
    (An earlier version of this harness banded every case with
    the family thresholds, which misreported non-family keeps:
    Imogen->Xaden friend at P=0.43 is a production keep.)"""
    if p is None:
        return "unanswered"
    if rel_type in pp._REL_FAMILY_TYPES:
        if p >= pp._REL_VALIDATION_THRESHOLD:
            return "keep"
        if p < pp._REL_DROP_THRESHOLD:
            return "drop"
        return "middle"
    return "keep" if p >= pp._REL_DROP_THRESHOLD else "drop"


# Repeated draws (spec §4.1). A judge's single score near a band
# edge flips sides between calls, so one draw cannot distinguish
# a real miss from sampling luck; cases whose first draw lands
# near an edge earn extra draws, where the instability actually
# lives, instead of spending them everywhere.
_DRAWS_BASE = 3
_DRAWS_NEAR_EDGE = 5
_EDGE_MARGIN = 0.15


def _band_edges(rel_type):
    """The score boundaries a claim of this family can straddle:
    family claims have two (drop below, keep at), non-family
    claims only the drop threshold — above it they keep."""
    if rel_type in pp._REL_FAMILY_TYPES:
        return (pp._REL_DROP_THRESHOLD, pp._REL_VALIDATION_THRESHOLD)
    return (pp._REL_DROP_THRESHOLD,)


def _draw_count(first_p, rel_type):
    """How many draws a case gets, decided by its first score.

    The epsilon keeps "within 0.15" honest at decimal boundaries:
    0.85 - 0.70 is 0.15000000000000008 in binary floating point,
    and a case exactly at the margin is exactly the case the
    extra draws exist for."""
    if first_p is None:
        return _DRAWS_BASE
    if any(abs(first_p - edge) <= _EDGE_MARGIN + 1e-9
           for edge in _band_edges(rel_type)):
        return _DRAWS_NEAR_EDGE
    return _DRAWS_BASE


def _case_outcome(answers, rel_type):
    """One case's verdict from its answered draws.

    Returns (outcome, median, spread). The outcome is the band
    every draw agrees on; disagreement means the draws straddle
    a band edge, and the honest verdict is "unstable" — bands
    are intervals, so no single band describes the case. Zero
    answered draws is "unanswered". Median and spread describe
    the draws; both are None when nothing answered."""
    if not answers:
        return "unanswered", None, None
    bands = {_band(p, rel_type) for p in answers}
    median = statistics.median(answers)
    spread = max(answers) - min(answers)
    if len(bands) > 1:
        return "unstable", median, spread
    return bands.pop(), median, spread


def _metrics(rows):
    """The four headline metrics (spec §4.2) over gated cases.

    Each value is a (numerator, denominator) pair so the report
    always shows the case counts behind a rate — a precision of
    1.00 over two cases and over two hundred must not read the
    same. Unstable, middle, and unanswered cases abstain: they
    leave the precision/recall/type denominators and count in
    the abstention rate, so a judge cannot look precise by
    refusing the hard cases."""
    pred_keep = sum(1 for r in rows if r["outcome"] == "keep")
    exp_keep = sum(1 for r in rows if r["expect"] == "keep")
    true_keep = sum(1 for r in rows if r["expect"] == "keep"
                    and r["outcome"] == "keep")
    decided = sum(1 for r in rows if r["outcome"] in ("keep", "drop"))
    exact = sum(1 for r in rows if r["outcome"] == r["expect"]
                and r["outcome"] in ("keep", "drop"))
    abstained = sum(1 for r in rows if r["outcome"] in
                    ("middle", "unstable", "unanswered"))
    return {
        "cases": len(rows),
        "precision": (true_keep, pred_keep),
        "recall": (true_keep, exp_keep),
        "type_accuracy": (exact, decided),
        "abstention": (abstained, len(rows)),
    }


def _fmt_metric(label, pair):
    num, den = pair
    if den == 0:
        return f"{label} n/a ({num}/{den})"
    return f"{label} {num / den:.2f} ({num}/{den})"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default=str(
        Path(__file__).resolve().parent / "eval"
        / "relationship_cases.json"))
    ap.add_argument("--provider", default="openrouter",
                    choices=["openrouter", "local"])
    ap.add_argument("--base-url", default="",
                    help="local provider endpoint (Unsloth /v1)")
    ap.add_argument("--config", default="",
                    help="path to a processor config.json (for the "
                         "openrouter_api_key; the key is never printed)")
    ap.add_argument("--dry-run", action="store_true",
                    help="score from each case's stub_score; no network")
    ap.add_argument("--corpus", action="store_true",
                    help="judge each case with its production "
                         "context window, located in the corpus "
                         "chapter texts (needs corpus.db)")
    ap.add_argument("--learn-dir", default=str(
        Path.home() / ".ebook-processor" / "learned"),
        help="learn dir holding corpus.db (default: the standard "
             "per-user learn dir)")
    args = ap.parse_args()

    cases = json.loads(Path(args.labels).read_text(encoding="utf-8"))

    if args.corpus:
        db = pp._corpus_db_for(args.learn_dir, mode="report")
        if db is None:
            print(f"Error: no corpus.db in learn dir "
                  f"{args.learn_dir}")
            return 2
        rows = db.query("SELECT fingerprint, title "
                        "FROM corpus_works")
        sets_by_work = {}
        ambiguous_works = set()
        for case in cases:
            work = (case.get("work") or "").lower()
            if work not in sets_by_work:
                hits = [fp for fp, title in rows
                        if work and work in (title or "").lower()]
                sets = []
                for fp in hits:
                    chapters = pp.corpus_chapters_for(db, fp)
                    if chapters:
                        sets.append(chapters)
                if not sets:
                    print(f"Error: work {case.get('work')!r} matched "
                          f"{len(hits)} corpus works, none with "
                          f"readable chapters (state not "
                          f"text-complete)")
                    return 2
                sets_by_work[work] = sets
            ctx, located, ambiguous = _resolve_case_context(
                pp, sets_by_work[work], case["quote"])
            if ambiguous:
                ambiguous_works.add(case.get("work"))
            case["_context"] = ctx
            case["_located"] = located
        for work in sorted(ambiguous_works):
            print(f"Note: work {work!r} matched several readable "
                  f"corpus copies; each case used the copy its "
                  f"quote locates in.")
    print("Mode: " + ("corpus context windows (production path)"
                      if args.corpus else
                      "prompt-only (no context windows)"))

    validator = None
    if not args.dry_run:
        config = {}
        if args.config:
            config = json.loads(Path(args.config).read_text(
                encoding="utf-8"))
        try:
            endpoint, model, key, headers = \
                pp.resolve_decision_model(
                    args.provider, args.base_url, config)
        except ValueError as e:
            print(f"Error: {e}")
            return 2
        validator = pp.DecisionValidator(
            endpoint, model=model, api_key=key,
            extra_headers=headers)

    misses = []
    unstables = []
    answered = 0
    gated_rows = []
    for i, case in enumerate(cases, 1):
        # Draw repeatedly: the first draw decides how many the
        # case gets (near an edge earns more), and a judge that
        # stops answering ends the case with what it gave — an
        # unanswered draw is not retried into a different answer.
        answers = []
        n_draws = _DRAWS_BASE
        while len(answers) < n_draws:
            if args.dry_run:
                p = case.get("stub_score")
            else:
                _verdict, p = validator.validate_relationship(
                    case["from"], case["to"], case["type"],
                    case["quote"], context=case.get("_context", ""))
            if p is None:
                break
            answers.append(p)
            if len(answers) == 1:
                n_draws = _draw_count(p, case["type"])
        outcome, median, spread = _case_outcome(answers, case["type"])
        expect = case.get("expect", "any")
        if answers:
            answered += 1
        gated = expect in ("keep", "drop")
        if gated:
            gated_rows.append({"work": case.get("work") or "",
                               "expect": expect, "outcome": outcome})
            if outcome == "unstable":
                unstables.append(case)
                status = "UNSTABLE"
            elif outcome == expect:
                status = "PASS"
            else:
                misses.append(case)
                status = "MISS"
        else:
            status = "REPORT"
        ctx_mark = ""
        if args.corpus:
            if case.get("_context"):
                ctx_mark = " [ctx]"
            elif not case.get("_located", True):
                ctx_mark = " [NO WINDOW: quote not in corpus text]"
        _res = _resolution_for_case(pp, case)
        if _res is not None:
            ctx_mark += f" [resolution: {_res}]"
        draws_s = ",".join(f"{p:.2f}" for p in answers) or "—"
        print(f"[{i:2}] {status:8} expect={expect:4} got={outcome:10} "
              f"P={median if median is not None else '—'} "
              f"draws=[{draws_s}] "
              f"spread={spread if spread is not None else '—'}  "
              f"{case['from']} -> {case['to']} ({case['type']})"
              f"{ctx_mark}")
    print()
    print(f"{answered}/{len(cases)} answered; "
          f"{len(misses)} gated miss(es); "
          f"{len(unstables)} gated unstable.")
    # Headline: the four metrics over gated cases, per book and
    # pooled — the pass count alone hid WHERE the judge fails
    # (wrong keeps vs missed keeps vs refusing to answer).
    scopes = []
    seen_works = []
    for row in gated_rows:
        if row["work"] not in seen_works:
            seen_works.append(row["work"])
    for work in seen_works:
        scopes.append((f"book {work!r}" if work else "book (unnamed)",
                       [r for r in gated_rows if r["work"] == work]))
    scopes.append(("all books", gated_rows))
    for label, rows in scopes:
        m = _metrics(rows)
        print(f"  {label} ({m['cases']} gated): "
              + "; ".join([
                  _fmt_metric("precision", m["precision"]),
                  _fmt_metric("recall", m["recall"]),
                  _fmt_metric("type accuracy", m["type_accuracy"]),
                  _fmt_metric("abstention", m["abstention"])]))
    if misses:
        for case in misses:
            print(f"  MISS: {case['from']} -> {case['to']} "
                  f"({case['type']}): {case.get('note', '')}")
    if unstables:
        for case in unstables:
            print(f"  UNSTABLE: {case['from']} -> {case['to']} "
                  f"({case['type']}): {case.get('note', '')}")
    if misses or unstables:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
