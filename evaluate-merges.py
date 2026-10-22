#!/usr/bin/env python3
"""Evaluate decision model merge predictions against labeled pairs.

Production-faithful: each pair is judged with the SAME payload shape
DecisionValidator.ask_same_person builds in process-portable.py —
names + best descriptions + evidence snippets, assembled by
build_same_person_state() below with the same part order and the same
truncation (descriptions 600 chars, evidence 300). Pairs without any
description/evidence run in name-only mode and are reported as such —
name-only scores measure a judge production never runs.

Two instruction variants, selectable with --prompt:
  identity  the production instruction text, verbatim (the default
            since the 2026-10-14 swap — see the variant block below);
  legacy    the pre-swap production text, kept as a historical
            variant so the 2026-10-14 experiment stays replicable.
The payload surrounding the instructions is identical for both, so a
run pair isolates the instruction text as the only variable. The
identity text shipped only after the live evaluation recorded in
eval/jev-prompt-eval-2026-10-14.md: equal accuracy @0.5, fewer false
merges @0.5, and zero false merges at the 0.85 production bar.

Usage:
    python3 evaluate-merges.py --labels eval/merge_pairs.json \
        --provider openrouter --prompt identity
    python3 evaluate-merges.py --labels merge-labels.json \
        --base-url http://127.0.0.1:8888/v1

Labels: either the pair-list format (eval/merge_pairs.json: a JSON
list of {name_a, name_b, label: "same"|"different", book, desc_a,
desc_b, evidence_a, evidence_b, source}) or the legacy label-merges.py
dict format ({key: {a, b, same_person, uncertain}}).

--dry-run exercises the plumbing against a stubbed endpoint (no
network): a pair's "stub_score" field wins when present (null forces
unanswered), else a deterministic token-overlap heuristic scores it.

Reports accuracy, precision, recall at thresholds 0.5 and 0.85, with
false merges and false splits listed separately by name — they are
not equally costly. Unanswered pairs (missing/invalid score) are
counted and excluded from every denominator.
"""

import argparse
import json
from pathlib import Path
import math
import os
import sys
import urllib.request


OPENROUTER_SYSTEMONE_BASE = "https://openrouter.ai/api/v1"
# Pinned Jev release; never the "latest" alias.
OPENROUTER_DECISION_MODEL = "typesafe/jev-1.13"
OPENROUTER_ATTRIBUTION_TITLE = "ebook-processor"


# --- Prompt variants ---------------------------------------------------
# DRIFT RISK: PRODUCTION_INSTRUCTIONS below duplicates the instruction
# literal in DecisionValidator.ask_same_person (process-portable.py)
# verbatim. This script is a standalone artefact (hyphenated module
# name, run directly against any checkout), so it cannot import the
# portable script; instead tests/test_jev_eval_harness.py captures the
# REAL production instructions + state via a stubbed _ask_noul and
# pins them against this constant. If production text changes, that
# test fails and this copy must be updated in the same commit.
#
# 2026-10-14 SWAP: production used LEGACY_INSTRUCTIONS until that
# date. A live evaluation over 112 scored labeled pairs
# (eval/jev-prompt-eval-2026-10-14.md) compared the two texts: equal
# accuracy @0.5 (83.04%); identity fewer false merges @0.5 (6 vs 7);
# @0.85 (the production merge bar) identity ZERO false merges vs
# legacy's one (MPITA @ 0.88). The identity text was approved and
# shipped as production; it is the default variant below.
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

# The shipped text began as the "identity" candidate variant:
# identity evidence, not first-name equality. YES needs positive
# evidence of one identity; NO needs positive evidence of two
# identities (or a contradiction / deterministic exclusion); anything
# else is explicitly an intermediate score, not a forced side.
# Deliberately contains no nickname-vs-first-names contradiction.
# The name is kept so --prompt identity and past run records still
# refer to the same text — which is now production itself.
IDENTITY_INSTRUCTIONS = PRODUCTION_INSTRUCTIONS

# Historical variant: the production text until 2026-10-14. Kept
# selectable (--prompt legacy) so the swap experiment can be re-run
# and earlier results replicated. NOT what production runs.
LEGACY_INSTRUCTIONS = (
    "Are these two character entries referring to the same person? "
    "Answer YES when: the names are variants of each other (nickname, "
    "shortened form, title + name matching a fuller name, minor "
    "spelling differences) AND the descriptions are compatible. "
    "Answer NO when: different first names, different people sharing "
    "a surname (siblings, parent/child), incompatible descriptions "
    "or roles, or insufficient evidence they are the same individual."
)

PROMPT_VARIANTS = {
    "legacy": LEGACY_INSTRUCTIONS,
    "identity": IDENTITY_INSTRUCTIONS,
}

# Thresholds every run reports in full (the production merge bar and
# the historical experiment bar).
REPORT_THRESHOLDS = (0.5, 0.85)


def build_same_person_state(name_a, desc_a, ev_a, name_b, desc_b, ev_b):
    """Assemble the judge state exactly as production does.

    Mirrors DecisionValidator.ask_same_person in process-portable.py:
    same parts, same order, same truncation (desc 600, evidence 300),
    empty fields omitted. Pinned against the real validator by
    tests/test_jev_eval_harness.py — change both or neither.
    """
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


def _config_openrouter_key(config_path=None):
    """Read the OpenRouter key from config.json, production-style.

    Mirrors process-portable.py: config.json lives beside the script
    (Path(__file__).parent); the working directory is also checked, as
    the two coincide in the normal checkout layout. Returns "" when no
    config file exists or it carries no key. The key is never logged.
    """
    candidates = []
    if config_path is not None:
        candidates.append(Path(config_path))
    else:
        candidates.append(Path(__file__).resolve().parent / "config.json")
        candidates.append(Path.cwd() / "config.json")
    for path in candidates:
        try:
            if not path.exists() or (config_path is None and "unittest" in sys.modules):
                continue
            with open(path, encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        key = (cfg.get("openrouter_api_key", "") or
               cfg.get("openrouter_key", ""))
        if key:
            return key
    return ""


def resolve_provider(provider, base_url, model, config_path=None):
    """Resolve decision-model connection details.

    Returns (base_url, model, api_key, extra_headers). Raises ValueError
    when the openrouter provider lacks an API key; the key is never
    logged and never taken from the command line.

    Key resolution mirrors process-portable.py's precedence, so the
    harness accepts every credential source production does, in the
    same order: config.json "openrouter_api_key", then config.json
    "openrouter_key", then the OPENROUTER_API_KEY environment variable
    — with the harness's historical extra env spellings
    (openrouter_api_key, openrouter_key) as final fallbacks.
    """
    if provider == "openrouter":
        key = (_config_openrouter_key(config_path) or
               os.environ.get("OPENROUTER_API_KEY", "") or
               os.environ.get("openrouter_api_key", "") or
               os.environ.get("openrouter_key", ""))
        if not key:
            raise ValueError(
                "provider 'openrouter' needs an API key: set "
                "\"openrouter_api_key\" in config.json (beside this script "
                "or in the working directory), or set OPENROUTER_API_KEY "
                "(or openrouter_api_key / openrouter_key) in the environment")
        return (OPENROUTER_SYSTEMONE_BASE,
                model or OPENROUTER_DECISION_MODEL,
                key,
                {"X-Title": OPENROUTER_ATTRIBUTION_TITLE})
    return (base_url, model, "", {})


def ask_same_person(base_url, model, api_key, name_a, name_b, timeout=30,
                    extra_headers=None, desc_a="", ev_a="", desc_b="",
                    ev_b="", instructions=None):
    """Ask the decision model about one pair. Returns p_yes or None.

    The payload state is the production shape (build_same_person_state)
    and the default instructions are the production text verbatim; pass
    instructions=PROMPT_VARIANTS["legacy"] for the pre-swap historical
    text.
    """
    if instructions is None:
        instructions = PRODUCTION_INSTRUCTIONS
    payload = json.dumps({
        "model": model or "default",
        "state": build_same_person_state(name_a, desc_a, ev_a,
                                         name_b, desc_b, ev_b),
        "questions": {
            "same_person": {
                "type": "noul",
                "instructions": instructions,
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
            p_yes = ans.get("probabilities", {}).get("yes")
        # Strict score handling, matching the production validator
        # (DecisionValidator._ask_noul): a missing or malformed score
        # makes the pair UNANSWERED (None, excluded from accuracy) —
        # never a silent 0.0 "no" that poisons the denominator.
        if p_yes is None:
            raise ValueError("response missing score")
        if isinstance(p_yes, bool) or not isinstance(p_yes, (int, float)):
            raise ValueError(f"non-numeric score: {p_yes!r}")
        if math.isnan(p_yes) or math.isinf(p_yes):
            raise ValueError(f"non-finite score: {p_yes}")
        if not 0 <= p_yes <= 1:
            raise ValueError(f"score out of range [0,1]: {p_yes}")
        return float(p_yes)
    except Exception as e:
        print(f"  ERROR for {name_a}/{name_b}: {e}")
        return None


# --- Labeled pairs -----------------------------------------------------

def _normalize_pair(entry, where):
    """Validate one pair-list entry; return the internal pair dict."""
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: pair entry is not an object")
    name_a = entry.get("name_a") or ""
    name_b = entry.get("name_b") or ""
    if not name_a or not name_b:
        raise ValueError(f"{where}: pair needs non-empty name_a/name_b")
    label = entry.get("label")
    if label not in ("same", "different"):
        raise ValueError(
            f"{where}: label must be 'same' or 'different', got {label!r}")
    pair = {
        "name_a": name_a,
        "name_b": name_b,
        "expected": label == "same",
        "book": entry.get("book") or "unknown",
        "desc_a": entry.get("desc_a") or "",
        "desc_b": entry.get("desc_b") or "",
        "evidence_a": entry.get("evidence_a") or "",
        "evidence_b": entry.get("evidence_b") or "",
        "source": entry.get("source") or "",
        # "scored": false marks an EXHIBIT — judged and printed, but
        # excluded from every metric. Scored ground truth must be
        # observed, not inferred: a label derived by transitivity
        # through a roster entry uses the very mechanism under
        # evaluation, so it enters the scored set only with independent
        # corroboration of that exact pair (Advisor ruling, 2026-10-14).
        "scored": bool(entry.get("scored", True)),
    }
    if "stub_score" in entry:
        pair["stub_score"] = entry["stub_score"]
    return pair


def load_pairs_file(path):
    """Load labeled pairs from a labels file.

    Accepts the pair-list format (list of entries with name_a/name_b/
    label/book + optional desc/evidence) and the legacy label-merges.py
    dict format ({key: {a, b, same_person, uncertain}} — uncertain
    entries are skipped, as the old main() did). Returns a list of
    normalized pair dicts. Raises ValueError on malformed input: an
    eval tool fails loud on a bad pair file rather than silently
    dropping pairs from the denominator.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [_normalize_pair(e, f"entry {i}")
                for i, e in enumerate(data)]
    if isinstance(data, dict):
        pairs = []
        for key, lbl in data.items():
            if not isinstance(lbl, dict) or lbl.get("uncertain"):
                continue
            if "a" not in lbl or "b" not in lbl \
                    or "same_person" not in lbl:
                raise ValueError(f"legacy entry {key!r}: needs a/b/"
                                 f"same_person")
            pairs.append({
                "name_a": lbl["a"],
                "name_b": lbl["b"],
                "expected": bool(lbl["same_person"]),
                "book": lbl.get("book") or "unknown",
                "desc_a": "", "desc_b": "",
                "evidence_a": "", "evidence_b": "",
                "source": lbl.get("source") or "",
            })
        return pairs
    raise ValueError("labels file must be a pair list or a legacy "
                     "label dict")


def pair_mode(pair):
    """'full' when the judge receives any description/evidence for the
    pair (the production shape), else 'name-only'."""
    if pair.get("desc_a") or pair.get("desc_b") \
            or pair.get("evidence_a") or pair.get("evidence_b"):
        return "full"
    return "name-only"


# --- Scoring -----------------------------------------------------------

def score_summary(scored, threshold):
    """Confusion counts + rates for scored pairs at one threshold.

    scored: pair dicts carrying "p_yes" (float) or None (unanswered).
    Unanswered pairs are counted and excluded from every denominator.
    False merges (different people predicted same) and false splits
    (same person predicted different) are returned as separate named
    lists — they are not equally costly.
    """
    answered = [p for p in scored if p.get("p_yes") is not None]
    tp = sum(1 for p in answered if p["expected"] and p["p_yes"] >= threshold)
    tn = sum(1 for p in answered
             if not p["expected"] and p["p_yes"] < threshold)
    false_merges = [p for p in answered
                    if not p["expected"] and p["p_yes"] >= threshold]
    false_splits = [p for p in answered
                    if p["expected"] and p["p_yes"] < threshold]
    fp, fn = len(false_merges), len(false_splits)
    total = len(answered)
    return {
        "threshold": threshold,
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "answered": total,
        "unanswered": len(scored) - total,
        "accuracy": (tp + tn) / total if total else 0,
        "precision": tp / (tp + fp) if (tp + fp) else 0,
        "recall": tp / (tp + fn) if (tp + fn) else 0,
        "false_merges": false_merges,
        "false_splits": false_splits,
    }


def stub_score(pair):
    """Deterministic dry-run score: the pair's explicit "stub_score"
    when present (null/invalid -> unanswered), else token overlap."""
    if "stub_score" in pair:
        v = pair["stub_score"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        if math.isnan(v) or math.isinf(v) or not 0 <= v <= 1:
            return None
        return float(v)
    a = set(pair["name_a"].lower().split())
    b = set(pair["name_b"].lower().split())
    return 0.9 if a & b else 0.1


# --- Main --------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--labels', required=True)
    ap.add_argument('--base-url', default='http://127.0.0.1:8888/v1')
    ap.add_argument('--provider', choices=['local', 'openrouter'],
                    default='local',
                    help="decision-model backend: 'local' Unsloth server "
                         "(default) or 'openrouter' hosted Jev")
    ap.add_argument('--model', default='')
    ap.add_argument('--threshold', type=float, default=0.5)
    ap.add_argument('--prompt', choices=sorted(PROMPT_VARIANTS),
                    default='identity',
                    help="instruction variant: 'identity' (production "
                         "text, verbatim — the default) or 'legacy' "
                         "(pre-2026-10-14 production text, historical)")
    ap.add_argument('--dry-run', action='store_true',
                    help="stubbed endpoint, no network: prove the "
                         "plumbing with deterministic synthetic scores")
    args = ap.parse_args(argv)

    instructions = PROMPT_VARIANTS[args.prompt]
    if args.dry_run:
        base_url, model, api_key, extra_headers = "", "stub", "", {}
    else:
        try:
            base_url, model, api_key, extra_headers = resolve_provider(
                args.provider, args.base_url, args.model)
        except ValueError as e:
            print(f"Error: {e}")
            return 2

    pairs = load_pairs_file(args.labels)
    n_full = sum(1 for p in pairs if pair_mode(p) == "full")
    n_name_only = len(pairs) - n_full
    print(f"Evaluating {len(pairs)} labeled pairs "
          f"(threshold={args.threshold}) prompt={args.prompt} "
          f"provider={'dry-run' if args.dry_run else args.provider} "
          f"model={model or 'default'}")
    print(f"Pair modes: {n_full} with descriptions/evidence (production "
          f"shape), {n_name_only} name-only")
    if n_full == 0:
        print("WARNING: every pair is name-only — these scores measure "
              "a name-only judge, NOT production, which receives "
              "descriptions + evidence.")
    if args.dry_run:
        print("DRY RUN: stubbed endpoint — scores are synthetic and "
              "say nothing about the model.")
    print()

    scored = []
    for i, pair in enumerate(pairs):
        if args.dry_run:
            p_yes = stub_score(pair)
        else:
            p_yes = ask_same_person(
                base_url, model, api_key,
                pair["name_a"], pair["name_b"],
                extra_headers=extra_headers,
                desc_a=pair["desc_a"], ev_a=pair["evidence_a"],
                desc_b=pair["desc_b"], ev_b=pair["evidence_b"],
                instructions=instructions)
        entry = dict(pair)
        entry["p_yes"] = p_yes
        scored.append(entry)
        if p_yes is None:
            continue
        predicted = p_yes >= args.threshold
        correct = predicted == pair["expected"]
        mark = "✓" if correct else "✗"
        if not pair.get("scored", True):
            mark = "·"  # exhibit: judged and shown, never scored
        print(f"{mark} [{i+1}/{len(pairs)}] {pair['name_a']} vs "
              f"{pair['name_b']}: exp={pair['expected']} "
              f"pred={predicted} P={p_yes:.2f}")

    # Exhibits (scored=false) are judged above but excluded from every
    # metric below.
    exhibits = [p for p in scored if not p.get("scored", True)]
    scored = [p for p in scored if p.get("scored", True)]
    if exhibits:
        print(f"\nEXHIBITS (unscored — derived labels, shown for the "
              f"record, excluded from all metrics): {len(exhibits)}")
        for p in exhibits:
            ptxt = "unanswered" if p["p_yes"] is None \
                else f"P={p['p_yes']:.2f}"
            print(f"  {p['name_a']} vs {p['name_b']} ({p['book']}): "
                  f"derived label={p['expected']} {ptxt} — "
                  f"{p.get('source') or 'no provenance recorded'}")

    # Primary-threshold results (the historical report shape).
    s = score_summary(scored, args.threshold)
    print(f"\n=== RESULTS (threshold={args.threshold}) "
          f"prompt={args.prompt} ===")
    print(f"Accuracy:  {s['accuracy']:.2%} ({s['tp'] + s['tn']}/"
          f"{s['answered']})")
    print(f"Precision: {s['precision']:.2%} (of predicted merges, "
          f"how many correct)")
    print(f"Recall:    {s['recall']:.2%} (of true merges, "
          f"how many caught)")
    print(f"TP={s['tp']} TN={s['tn']} FP={s['fp']} FN={s['fn']}")
    print(f"Unanswered: {s['unanswered']} of {len(scored)} scored pairs "
          f"(excluded from the metrics above)")

    # Errors at the primary threshold.
    errors = [p for p in scored if p["p_yes"] is not None
              and (p["p_yes"] >= args.threshold) != p["expected"]]
    if errors:
        print(f"\n=== ERRORS ({len(errors)}) ===")
        for p in errors:
            print(f"  {p['name_a']} vs {p['name_b']}: "
                  f"expected={p['expected']} "
                  f"got={p['p_yes'] >= args.threshold} "
                  f"P={p['p_yes']:.2f}")

    # Full report at both standing thresholds, false merges and false
    # splits named separately — they are not equally costly.
    print("\n=== FALSE MERGES / FALSE SPLITS BY THRESHOLD ===")
    for thresh in REPORT_THRESHOLDS:
        st = score_summary(scored, thresh)
        print(f"--- threshold {thresh:.2f} ---")
        print(f"Accuracy:  {st['accuracy']:.2%}  "
              f"Precision: {st['precision']:.2%}  "
              f"Recall: {st['recall']:.2%}")
        print(f"False merges ({st['fp']}) — different people the "
              f"judge would merge:")
        for p in st["false_merges"]:
            print(f"  {p['name_a']} vs {p['name_b']} ({p['book']}): "
                  f"P={p['p_yes']:.2f}")
        print(f"False splits ({st['fn']}) — same person the judge "
              f"would keep apart:")
        for p in st["false_splits"]:
            print(f"  {p['name_a']} vs {p['name_b']} ({p['book']}): "
                  f"P={p['p_yes']:.2f}")

    # Threshold sweep
    print("\n=== THRESHOLD SWEEP ===")
    for thresh in [0.3, 0.5, 0.7, 0.8, 0.9]:
        st = score_summary(scored, thresh)
        print(f"  thresh={thresh:.1f}: prec={st['precision']:.2%} "
              f"rec={st['recall']:.2%}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
