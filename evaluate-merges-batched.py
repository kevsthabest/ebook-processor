#!/usr/bin/env python3
"""Batched vs single-question Jev evaluation on hand-labeled merge pairs.

Tests two things:
  1. Whether the SystemOne API accepts multiple questions per request.
  2. Whether per-pair accuracy holds when judging pairs in bulk.

Usage:
    python3 evaluate-merges-batched.py --labels merge-labels.json \
        --provider openrouter --batch-sizes 1 5 10 --threshold 0.85

Needs OPENROUTER_API_KEY (or openrouter_api_key / openrouter_key) in the
environment for provider=openrouter. Costs fractions of a cent per run
(Jev: $0.042/MTok input, output free).

Batch size 1 uses the exact single-question production code path and serves
as the baseline; larger sizes pack N pairs into one request as N questions
sharing one state block. Agreement is measured against the baseline.
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

YES_NO_CRITERIA = (
    "Are these two names referring to the same person? "
    "Answer YES when: the names are variants of each other "
    "(nickname, shortened form, title + surname matching full name, "
    "same first name with compatible details). "
    "Answer NO when: different first names, different people with "
    "similar surnames (siblings, parent/child), or insufficient "
    "evidence they are the same individual."
)


def resolve_provider(provider, base_url, model):
    """Same contract as evaluate-merges.py: env-only key, never logged."""
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


def _post(base_url, model, api_key, payload, extra_headers, timeout):
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
        f"{base}/v1/systemone", data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _extract_pyes(ans):
    """Strict per-question score validation, mirroring _ask_noul."""
    p_yes = ans.get("noul")
    if p_yes is None:
        p_yes = ans.get("probabilities", {}).get("yes")
    if p_yes is None:
        raise ValueError("response missing 'noul' score")
    if isinstance(p_yes, bool) or not isinstance(p_yes, (int, float)):
        raise ValueError(f"non-numeric noul score: {p_yes!r}")
    import math
    if math.isnan(p_yes) or math.isinf(p_yes):
        raise ValueError(f"non-finite noul score: {p_yes!r}")
    if not 0 <= p_yes <= 1:
        raise ValueError(f"noul score out of range [0,1]: {p_yes!r}")
    return float(p_yes)


def ask_single(base_url, model, api_key, name_a, name_b, extra_headers):
    """One pair, one question: the production code path (baseline)."""
    payload = {
        "model": model or "default",
        "state": f"Character A: {name_a}\nCharacter B: {name_b}",
        "questions": {
            "same_person": {"type": "noul", "instructions": YES_NO_CRITERIA},
        },
    }
    try:
        result = _post(base_url, model, api_key, payload, extra_headers, 30)
        return _extract_pyes(result["answers"]["same_person"])
    except Exception as e:
        print(f"  ERROR single {name_a}/{name_b}: {e}")
        return None


def ask_batched(base_url, model, api_key, pairs, extra_headers):
    """N pairs, one request, N questions sharing one state block.

    Returns {pair_key: p_yes or None}. A question that fails strict
    validation yields None for that pair (no silent negatives).
    """
    state_parts = []
    questions = {}
    keys = []
    for i, (key, lbl) in enumerate(pairs):
        tag = f"pair_{i}"
        keys.append((tag, key))
        state_parts.append(
            f"Pair {i + 1}:\nCharacter A: {lbl['a']}\nCharacter B: {lbl['b']}")
        questions[tag] = {
            "type": "noul",
            "instructions": f"For Pair {i + 1} in the state above. {YES_NO_CRITERIA}",
        }
    payload = {
        "model": model or "default",
        "state": "\n\n".join(state_parts),
        "questions": questions,
    }
    out = {}
    try:
        result = _post(base_url, model, api_key, payload, extra_headers, 60)
        answers = result.get("answers", {})
    except Exception as e:
        print(f"  ERROR batch({len(pairs)}): {e}")
        return {key: None for _, key in keys}
    for tag, key in keys:
        try:
            out[key] = _extract_pyes(answers[tag])
        except Exception as e:
            print(f"  ERROR {tag} ({key}): {e}")
            out[key] = None
    return out


def metrics(results, threshold):
    tp = sum(1 for e, s in results if e and s is not None and s >= threshold)
    tn = sum(1 for e, s in results if not e and s is not None and s < threshold)
    fp = sum(1 for e, s in results if not e and s is not None and s >= threshold)
    fn = sum(1 for e, s in results if e and s is not None and s < threshold)
    total = tp + tn + fp + fn
    acc = (tp + tn) / total if total else 0
    prec = tp / (tp + fp) if (tp + fp) else 0
    rec = tp / (tp + fn) if (tp + fn) else 0
    return acc, prec, rec, tp, tn, fp, fn, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--labels', default='merge-labels.json')
    ap.add_argument('--base-url', default='http://127.0.0.1:8888/v1')
    ap.add_argument('--provider', choices=['local', 'openrouter'],
                    default='openrouter')
    ap.add_argument('--model', default='')
    ap.add_argument('--threshold', type=float, default=0.85)
    ap.add_argument('--batch-sizes', default='1,5,10',
                    help='comma-separated batch sizes; 1 = single-question baseline')
    ap.add_argument('--max-pairs', type=int, default=0,
                    help='limit pairs for a quick probe (0 = all)')
    args = ap.parse_args()

    try:
        base_url, model, api_key, extra_headers = resolve_provider(
            args.provider, args.base_url, args.model)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(2)

    labels = json.load(open(args.labels, encoding='utf-8'))
    pairs = [(k, v) for k, v in labels.items() if not v.get('uncertain')]
    if args.max_pairs:
        pairs = pairs[:args.max_pairs]
    print(f"{len(pairs)} labeled pairs, threshold={args.threshold}, "
          f"provider={args.provider} model={model or 'default'}\n")

    batch_sizes = [int(s) for s in args.batch_sizes.split(',')]
    all_scores = {}  # batch_size -> {pair_key: p_yes}

    for bs in batch_sizes:
        scores = {}
        if bs == 1:
            for i, (key, lbl) in enumerate(pairs):
                scores[key] = ask_single(
                    base_url, model, api_key,
                    lbl['a'], lbl['b'], extra_headers)
                if (i + 1) % 20 == 0:
                    print(f"  [bs=1] {i + 1}/{len(pairs)}")
        else:
            for i in range(0, len(pairs), bs):
                chunk = pairs[i:i + bs]
                scores.update(ask_batched(
                    base_url, model, api_key, chunk, extra_headers))
                print(f"  [bs={bs}] {min(i + bs, len(pairs))}/{len(pairs)}")
        all_scores[bs] = scores

    baseline = all_scores[1]
    print(f"\n=== RESULTS (threshold={args.threshold}) ===")
    for bs in batch_sizes:
        scored = [(labels[k]['same_person'], s)
                  for k, s in all_scores[bs].items() if s is not None]
        acc, prec, rec, tp, tn, fp, fn, total = metrics(scored, args.threshold)
        line = (f"batch={bs:<3} n={total:<4} acc={acc:.2%} "
                f"prec={prec:.2%} rec={rec:.2%} "
                f"(TP={tp} TN={tn} FP={fp} FN={fn})")
        if bs != 1:
            agree = sum(1 for k in all_scores[bs]
                        if all_scores[bs][k] is not None
                        and baseline.get(k) is not None
                        and (all_scores[bs][k] >= args.threshold)
                        == (baseline[k] >= args.threshold))
            comp = sum(1 for k in all_scores[bs]
                       if all_scores[bs][k] is not None
                       and baseline.get(k) is not None)
            flips = []
            for k in all_scores[bs]:
                sb, bb = baseline.get(k), all_scores[bs][k]
                if sb is not None and bb is not None and \
                        (sb >= args.threshold) != (bb >= args.threshold):
                    flips.append((k, sb, bb))
            line += f"  agreement-vs-single={agree}/{comp} ({agree/comp:.1%})" \
                if comp else "  agreement-vs-single=n/a"
            if flips:
                line += f"\n  FLIPS ({len(flips)}):"
                for k, sb, bb in flips[:10]:
                    line += f"\n    {k}: single={sb:.2f} batch={bb:.2f}"
        print(line)

    n_calls = {1: len(pairs)}
    for bs in batch_sizes[1:]:
        n_calls[bs] = (len(pairs) + bs - 1) // bs
    print(f"\nAPI calls used: "
          + ", ".join(f"batch={bs}: {n_calls[bs]}" for bs in batch_sizes))


if __name__ == '__main__':
    main()
