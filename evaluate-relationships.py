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


def _band(p):
    if p is None:
        return "unanswered"
    if p >= pp._REL_VALIDATION_THRESHOLD:
        return "keep"
    if p < pp._REL_DROP_THRESHOLD:
        return "drop"
    return "middle"


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
    args = ap.parse_args()

    cases = json.loads(Path(args.labels).read_text(encoding="utf-8"))

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
                case["from"], case["to"], case["type"], case["quote"])
        band = _band(p)
        expect = case.get("expect", "any")
        if p is not None:
            answered += 1
        gated = expect in ("keep", "drop")
        ok = (not gated) or band == expect
        status = "REPORT" if not gated else ("PASS" if ok else "MISS")
        if gated and not ok:
            misses.append(case)
        print(f"[{i:2}] {status:6} expect={expect:4} got={band:10} "
              f"P={p if p is not None else '—'}  "
              f"{case['from']} -> {case['to']} ({case['type']})")
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
