#!/usr/bin/env python3
"""Evaluate decision model merge predictions against hand-labeled pairs.

Usage:
    python3 evaluate-merges.py --labels merge-labels.json \
        --base-url http://127.0.0.1:8888/v1

Reports accuracy, precision, recall, and per-threshold analysis.
"""

import argparse
import json
import os
import sys
import urllib.request


OPENROUTER_SYSTEMONE_BASE = "https://openrouter.ai/api/v1"
# Pinned Jev release; never the "latest" alias.
OPENROUTER_DECISION_MODEL = "typesafe/jev-1.13"
OPENROUTER_ATTRIBUTION_TITLE = "ebook-processor"


def resolve_provider(provider, base_url, model):
    """Resolve decision-model connection details.

    Returns (base_url, model, api_key, extra_headers). Raises ValueError
    when the openrouter provider lacks an API key; the key is never logged.
    The key comes from environment variables only (never the command line):
    OPENROUTER_API_KEY, openrouter_api_key, or openrouter_key.
    """
    if provider == "openrouter":
        key = (os.environ.get("OPENROUTER_API_KEY", "") or
               os.environ.get("openrouter_api_key", "") or
               os.environ.get("openrouter_key", ""))
        if not key:
            raise ValueError(
                "provider 'openrouter' needs an API key: set OPENROUTER_API_KEY "
                "(or openrouter_api_key / openrouter_key) in the environment")
        return (OPENROUTER_SYSTEMONE_BASE,
                model or OPENROUTER_DECISION_MODEL,
                key,
                {"X-Title": OPENROUTER_ATTRIBUTION_TITLE})
    return (base_url, model, "", {})


def ask_same_person(base_url, model, api_key, name_a, name_b, timeout=30,
                    extra_headers=None):
    payload = json.dumps({
        "model": model or "default",
        "state": f"Character A: {name_a}\nCharacter B: {name_b}",
        "questions": {
            "same_person": {
                "type": "noul",
                "instructions": (
                    "Are these two names referring to the same person? "
                    "Answer YES when: the names are variants of each other "
                    "(nickname, shortened form, title + surname matching full name, "
                    "same first name with compatible details). "
                    "Answer NO when: different first names, different people with "
                    "similar surnames (siblings, parent/child), or insufficient "
                    "evidence they are the same individual."
                ),
            }
        },
    }).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(extra_headers or {})
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    base = base_url.rstrip('/')
    if base.endswith('/v1/systemone'):
        base = base[:-len('/v1/systemone')]
    elif base.endswith('/v1'):
        base = base[:-3]
    req = urllib.request.Request(
        f"{base}/v1/systemone", data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        ans = result["answers"]["same_person"]
        p_yes = ans.get("noul")
        if p_yes is None:
            p_yes = ans.get("probabilities", {}).get("yes", 0)
        return float(p_yes)
    except Exception as e:
        print(f"  ERROR for {name_a}/{name_b}: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--labels', required=True)
    ap.add_argument('--base-url', default='http://127.0.0.1:8888/v1')
    ap.add_argument('--provider', choices=['local', 'openrouter'],
                    default='local',
                    help="decision-model backend: 'local' Unsloth server "
                         "(default) or 'openrouter' hosted Jev")
    ap.add_argument('--model', default='')
    ap.add_argument('--threshold', type=float, default=0.5)
    args = ap.parse_args()

    try:
        base_url, model, api_key, extra_headers = resolve_provider(
            args.provider, args.base_url, args.model)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(2)

    labels = json.load(open(args.labels, encoding='utf-8'))
    # Skip uncertain labels for evaluation
    pairs = [(k, v) for k, v in labels.items() if not v.get('uncertain')]
    print(f"Evaluating {len(pairs)} labeled pairs (threshold={args.threshold}) "
          f"provider={args.provider} model={model or 'default'}\n")

    results = []
    for i, (key, lbl) in enumerate(pairs):
        expected = lbl['same_person']
        p_yes = ask_same_person(
            base_url, model, api_key,
            lbl['a'], lbl['b'], extra_headers=extra_headers)
        if p_yes is None:
            continue
        predicted = p_yes >= args.threshold
        correct = predicted == expected
        results.append((expected, predicted, p_yes, lbl))
        mark = "✓" if correct else "✗"
        print(f"{mark} [{i+1}/{len(pairs)}] {lbl['a']} vs {lbl['b']}: "
              f"exp={expected} pred={predicted} P={p_yes:.2f}")

    # Metrics
    tp = sum(1 for e, p, _, _ in results if e and p)
    tn = sum(1 for e, p, _, _ in results if not e and not p)
    fp = sum(1 for e, p, _, _ in results if not e and p)
    fn = sum(1 for e, p, _, _ in results if e and not p)
    total = len(results)
    acc = (tp + tn) / total if total else 0
    prec = tp / (tp + fp) if (tp + fp) else 0
    rec = tp / (tp + fn) if (tp + fn) else 0

    print(f"\n=== RESULTS (threshold={args.threshold}) ===")
    print(f"Accuracy:  {acc:.2%} ({tp+tn}/{total})")
    print(f"Precision: {prec:.2%} (of predicted merges, how many correct)")
    print(f"Recall:    {rec:.2%} (of true merges, how many caught)")
    print(f"TP={tp} TN={tn} FP={fp} FN={fn}")

    # Show errors
    errors = [(e, p, s, l) for e, p, s, l in results if e != p]
    if errors:
        print(f"\n=== ERRORS ({len(errors)}) ===")
        for expected, predicted, p_yes, lbl in errors:
            print(f"  {lbl['a']} vs {lbl['b']}: "
                  f"expected={expected} got={predicted} P={p_yes:.2f}")

    # Threshold sweep
    print("\n=== THRESHOLD SWEEP ===")
    for thresh in [0.3, 0.5, 0.7, 0.8, 0.9]:
        tp_ = sum(1 for e, _, s, _ in results if e and s >= thresh)
        fp_ = sum(1 for e, _, s, _ in results if not e and s >= thresh)
        fn_ = sum(1 for e, _, s, _ in results if e and s < thresh)
        prec_ = tp_ / (tp_ + fp_) if (tp_ + fp_) else 0
        rec_ = tp_ / (tp_ + fn_) if (tp_ + fn_) else 0
        print(f"  thresh={thresh:.1f}: prec={prec_:.2%} rec={rec_:.2%}")


if __name__ == '__main__':
    main()
