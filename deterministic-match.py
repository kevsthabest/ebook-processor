#!/usr/bin/env python3
"""Deterministic character name matching tiers (per Claude's suggestion).

Tier 1 (Hard NO): different given names, or same-chapter co-occurrence
Tier 2 (Deterministic YES): title stripping + token subsequence matching

Usage:
    python3 deterministic-match.py --labels merge-labels.json
"""

import argparse
import json
import re

# Titles/ranks to strip (case-insensitive, prefix only)
TITLES = {
    'agent', 'special', 'detective', 'sergeant', 'major', 'colonel',
    'general', 'captain', 'lieutenant', 'doctor', 'dr', 'mr', 'mrs',
    'ms', 'sir', 'officer', 'chief', 'deputy', 'inspector', 'herr',
    'oberstleutnant', 'oberst', 'ss',
}


def normalize(name):
    """Lowercase, strip titles, normalize whitespace/punctuation."""
    # Lowercase and strip
    name = name.lower().strip()
    # Remove parentheticals like "(assumed identity)"
    name = re.sub(r'\s*\([^)]*\)', '', name)
    # Split into tokens
    tokens = re.findall(r"[a-z0-9']+", name)
    # Strip leading titles
    while tokens and tokens[0] in TITLES:
        tokens = tokens[1:]
    return tokens


def hard_no(tokens_a, tokens_b):
    """Tier 1: definitely different people."""
    if not tokens_a or not tokens_b:
        return False
    # Different given names (first tokens differ and both look like first names)
    # Heuristic: if first tokens differ and neither is a title remnant
    if len(tokens_a) >= 2 and len(tokens_b) >= 2:
        if tokens_a[0] != tokens_b[0]:
            # Check if they share a surname but different first names
            # e.g. "peter sebeck" vs "laura sebeck"
            if tokens_a[-1] == tokens_b[-1]:
                return True
    return False


def deterministic_yes(tokens_a, tokens_b):
    """Tier 2: definitely same person (after normalization)."""
    if not tokens_a or not tokens_b:
        return False
    # Identical
    if tokens_a == tokens_b:
        return True
    shorter, longer = sorted([tokens_a, tokens_b], key=len)
    # Single token vs multi-token: only merge if no first-name conflict
    # e.g. ["ross"] vs ["jon", "ross"] -> yes (no conflict)
    # e.g. ["sebeck"] vs ["chris", "sebeck"] -> ambiguous (sibling trap)
    # We can't know from names alone, so defer to ambiguous
    if len(shorter) == 1 and len(longer) > 1:
        return False  # Let roster context or model decide
    # Multi-token subsequence: e.g. ["neal", "decker"] vs ["neal", "decker"]
    for i in range(len(longer) - len(shorter) + 1):
        if longer[i:i+len(shorter)] == shorter:
            return True
    return False


def classify_pair(name_a, name_b):
    """Returns 'yes', 'no', or 'ambiguous'."""
    ta = normalize(name_a)
    tb = normalize(name_b)
    if hard_no(ta, tb):
        return 'no'
    if deterministic_yes(ta, tb):
        return 'yes'
    return 'ambiguous'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--labels', required=True)
    args = ap.parse_args()

    labels = json.load(open(args.labels, encoding='utf-8'))
    pairs = [(k, v) for k, v in labels.items() if not v.get('uncertain')]
    print(f"Testing {len(pairs)} pairs\n")

    results = {'yes': [], 'no': [], 'ambiguous': []}
    for key, lbl in pairs:
        expected = lbl['same_person']
        verdict = classify_pair(lbl['a'], lbl['b'])
        results[verdict].append((expected, lbl))

    # Metrics for deterministic decisions only
    tp = sum(1 for e, _ in results['yes'] if e)
    fp = sum(1 for e, _ in results['yes'] if not e)
    tn = sum(1 for e, _ in results['no'] if not e)
    fn = sum(1 for e, _ in results['no'] if e)

    total_det = tp + fp + tn + fn
    total = len(pairs)
    ambig = len(results['ambiguous'])

    print(f"=== DETERMINISTIC TIERS ===")
    print(f"Decided: {total_det}/{total} ({total_det/total:.1%})")
    print(f"Ambiguous (needs model/human): {ambig}/{total} ({ambig/total:.1%})")
    if total_det:
        acc = (tp + tn) / total_det
        print(f"Accuracy on decided: {acc:.1%} ({tp+tn}/{total_det})")
        print(f"  TP={tp} TN={tn} FP={fp} FN={fn}")
        if fp:
            print(f"\n  FALSE POSITIVES ({fp}):")
            for e, lbl in results['yes']:
                if not e:
                    print(f"    {lbl['a']} vs {lbl['b']}")
        if fn:
            print(f"\n  FALSE NEGATIVES ({fn}):")
            for e, lbl in results['no']:
                if e:
                    print(f"    {lbl['a']} vs {lbl['b']}")

    print(f"\n=== AMBIGUOUS PAIRS ({ambig}) ===")
    print("These would go to the decision model or human review:")
    for expected, lbl in results['ambiguous'][:20]:
        exp_str = "YES" if expected else "NO"
        print(f"  [{exp_str}] {lbl['a']} vs {lbl['b']}")
    if ambig > 20:
        print(f"  ... and {ambig-20} more")


if __name__ == '__main__':
    main()
