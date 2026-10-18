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

Batch size 1 is the single-question baseline; larger sizes pack N pairs
into one request as N questions sharing one state block. Agreement is
measured against the baseline — and only when batch size 1 was actually
requested: without it there is no baseline, the run says so, and no
agreement column is printed.

Baseline honesty: the baseline judges with the production instruction
text (PRODUCTION_INSTRUCTIONS, copied verbatim from evaluate-merges.py,
which pins it against production) and the production state shape
(names + descriptions + evidence, build_same_person_state). Label
entries that carry no descriptions/evidence therefore run NAME-ONLY,
and the report names that mode plainly: name-only scores measure a
judge production never runs, because production's judge always
receives descriptions and evidence when the book provides them.
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

# The production judge instructions, copied verbatim from
# evaluate-merges.py's PRODUCTION_INSTRUCTIONS (which is itself pinned
# against DecisionValidator.ask_same_person in process-portable.py by
# tests/test_jev_eval_harness.py). This script is a standalone artefact
# and cannot import either file, so the text is duplicated here — if
# the production text changes, evaluate-merges.py's pin fails and this
# copy must be updated in the same commit. The previous YES_NO_CRITERIA
# text (name-variant -> YES, different first names -> NO, insufficient
# evidence -> NO) was a different judge production never runs, which
# made every "agreement vs baseline" number a comparison against a
# name-only judge; it is gone.
PRODUCTION_INSTRUCTIONS = (
    "Are these two character entries referring to the same person? "
    "Answer YES only when there is positive evidence of one identity: "
    "a supported alias or nickname linking the two names, a reliable "
    "name variant (shortened form, title change, minor spelling "
    "difference) with compatible attributes, or contextual evidence "
    "that both entries describe the same individual. "
    "Answer NO when the book supports two distinct identities, when "
    "the descriptions meaningfully contradict each other (roles, "
    "relationships, or histories one person could not hold), or when "
    "a deterministic constraint rules the pair out. "
    "Similar descriptions alone never establish identity: different "
    "people can share a surname, a role, and a description. "
    "If the evidence is insufficient either way, score near the "
    "middle of the scale rather than guessing — an unresolved pair "
    "stays separate and is reviewed; it is not merged on a guess."
)


def build_same_person_state(name_a, desc_a, ev_a, name_b, desc_b, ev_b):
    """Assemble the judge state exactly as production does: same
    parts, same order, same truncation (descriptions 600 chars,
    evidence 300), empty fields omitted. Mirrors evaluate-merges.py's
    build_same_person_state; with no descriptions/evidence the state
    is names only — the name-only mode the report names."""
    parts = [f"Character A: {name_a}"]
    if desc_a:
        parts.append(f"Description A: {desc_a[:600]}")
    if ev_a:
        parts.append(f"Evidence A: {ev_a[:300]}")
    parts.append(f"Character B: {name_b}")
    if desc_b:
        parts.append(f"Description B: {desc_b[:600]}")
    if ev_b:
        parts.append(f"Evidence B: {ev_b[:300]}")
    return "\n".join(parts)


def _pair_state(lbl):
    """Production-shape state for one label entry. Legacy label
    entries may carry desc_a/desc_b/evidence_a/evidence_b; entries
    without them are name-only."""
    return build_same_person_state(
        lbl["a"], lbl.get("desc_a") or "", lbl.get("evidence_a") or "",
        lbl["b"], lbl.get("desc_b") or "", lbl.get("evidence_b") or "")


def _pair_is_name_only(lbl):
    return not (lbl.get("desc_a") or lbl.get("desc_b")
                or lbl.get("evidence_a") or lbl.get("evidence_b"))


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


def ask_single(base_url, model, api_key, name_a, name_b, extra_headers,
               desc_a="", ev_a="", desc_b="", ev_b=""):
    """One pair, one question: the baseline. The state is the
    production shape (build_same_person_state) and the instructions
    are the production text — a caller with only names gets a
    name-only state, which the run report names as such."""
    payload = {
        "model": model or "default",
        "state": build_same_person_state(name_a, desc_a, ev_a,
                                         name_b, desc_b, ev_b),
        "questions": {
            "same_person": {"type": "noul",
                            "instructions": PRODUCTION_INSTRUCTIONS},
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
        state_parts.append(f"Pair {i + 1}:\n{_pair_state(lbl)}")
        questions[tag] = {
            "type": "noul",
            "instructions": f"For Pair {i + 1} in the state above. "
                            f"{PRODUCTION_INSTRUCTIONS}",
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
                    help='comma-separated batch sizes; 1 = single-question '
                         'baseline (optional — without it, no agreement '
                         'against a baseline is reported)')
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
    # A malformed entry (missing a/b/same_person, or not an object at
    # all) is reported by key and skipped BEFORE any API spend —
    # never discovered as a KeyError traceback after every requested
    # batch has already been paid for.
    pairs = []
    malformed = []
    for k, v in labels.items():
        if not isinstance(v, dict):
            malformed.append((k, "entry is not an object"))
            continue
        if v.get('uncertain'):
            continue
        missing = [f for f in ('a', 'b', 'same_person') if f not in v]
        if missing:
            malformed.append((k, f"missing {', '.join(missing)}"))
            continue
        pairs.append((k, v))
    for k, why in malformed:
        print(f"  MALFORMED pair {k!r}: {why} — skipped")
    if malformed:
        print(f"{len(malformed)} malformed pair(s) skipped")
    if args.max_pairs:
        pairs = pairs[:args.max_pairs]
    print(f"{len(pairs)} labeled pairs, threshold={args.threshold}, "
          f"provider={args.provider} model={model or 'default'}")
    # Name the judge mode honestly, the way evaluate-merges.py does:
    # pairs without descriptions/evidence are judged name-only, and
    # name-only scores measure a judge production never runs.
    n_name_only = sum(1 for _k, lbl in pairs if _pair_is_name_only(lbl))
    print(f"Pair modes: {len(pairs) - n_name_only} with "
          f"descriptions/evidence (production shape), "
          f"{n_name_only} name-only")
    if n_name_only and n_name_only == len(pairs):
        print("WARNING: every pair is name-only — these scores measure "
              "a name-only judge, NOT production, which receives "
              "descriptions + evidence.")
    print()

    batch_sizes = [int(s) for s in args.batch_sizes.split(',')]
    all_scores = {}  # batch_size -> {pair_key: p_yes}

    for bs in batch_sizes:
        scores = {}
        if bs == 1:
            for i, (key, lbl) in enumerate(pairs):
                scores[key] = ask_single(
                    base_url, model, api_key,
                    lbl['a'], lbl['b'], extra_headers,
                    desc_a=lbl.get("desc_a") or "",
                    ev_a=lbl.get("evidence_a") or "",
                    desc_b=lbl.get("desc_b") or "",
                    ev_b=lbl.get("evidence_b") or "")
                if (i + 1) % 20 == 0:
                    print(f"  [bs=1] {i + 1}/{len(pairs)}")
        else:
            for i in range(0, len(pairs), bs):
                chunk = pairs[i:i + bs]
                scores.update(ask_batched(
                    base_url, model, api_key, chunk, extra_headers))
                print(f"  [bs={bs}] {min(i + bs, len(pairs))}/{len(pairs)}")
        all_scores[bs] = scores

    # The baseline exists only if batch size 1 was requested. The old
    # code indexed all_scores[1] unconditionally and died with a
    # KeyError after every requested batch had completed — all the
    # API spend done, no results printed.
    baseline = all_scores.get(1)
    print(f"\n=== RESULTS (threshold={args.threshold}) ===")
    if baseline is None:
        print("No single-question baseline: batch size 1 was not "
              "requested, so agreement against a baseline is not "
              "reported (pass --batch-sizes including 1 to measure it).")
    else:
        print("Baseline: batch size 1 (single-question), production "
              "instructions + production state shape"
              + (" — name-only pairs, see the pair-modes warning above"
                 if n_name_only else ""))
    for bs in batch_sizes:
        scored = [(labels[k]['same_person'], s)
                  for k, s in all_scores[bs].items() if s is not None]
        acc, prec, rec, tp, tn, fp, fn, total = metrics(scored, args.threshold)
        line = (f"batch={bs:<3} n={total:<4} acc={acc:.2%} "
                f"prec={prec:.2%} rec={rec:.2%} "
                f"(TP={tp} TN={tn} FP={fp} FN={fn})")
        if bs != 1 and baseline is not None:
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

    # Only batches that actually ran get a tally — the old seed entry
    # for batch 1 fabricated a call count for a batch that may never
    # have been requested.
    n_calls = {bs: len(pairs) if bs == 1
               else (len(pairs) + bs - 1) // bs
               for bs in batch_sizes}
    print(f"\nAPI calls used: "
          + ", ".join(f"batch={bs}: {n_calls[bs]}" for bs in batch_sizes))


if __name__ == '__main__':
    main()
