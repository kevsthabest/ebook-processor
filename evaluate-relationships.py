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

Exit codes: 0 = every gated case in band; 1 = a gated case out of
band; 2 = configuration error (no endpoint/key).
"""
import argparse
import importlib.util
import json
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
    answered = 0
    for i, case in enumerate(cases, 1):
        if args.dry_run:
            p = case.get("stub_score")
        else:
            _verdict, p = validator.validate_relationship(
                case["from"], case["to"], case["type"], case["quote"],
                context=case.get("_context", ""))
        band = _band(p, case["type"])
        expect = case.get("expect", "any")
        if p is not None:
            answered += 1
        gated = expect in ("keep", "drop")
        ok = (not gated) or band == expect
        status = "REPORT" if not gated else ("PASS" if ok else "MISS")
        if gated and not ok:
            misses.append(case)
        ctx_mark = ""
        if args.corpus:
            if case.get("_context"):
                ctx_mark = " [ctx]"
            elif not case.get("_located", True):
                ctx_mark = " [NO WINDOW: quote not in corpus text]"
        print(f"[{i:2}] {status:6} expect={expect:4} got={band:10} "
              f"P={p if p is not None else '—'}  "
              f"{case['from']} -> {case['to']} ({case['type']})"
              f"{ctx_mark}")
    print()
    print(f"{answered}/{len(cases)} answered; "
          f"{len(misses)} gated miss(es).")
    if misses:
        for case in misses:
            print(f"  MISS: {case['from']} -> {case['to']} "
                  f"({case['type']}): {case.get('note', '')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
