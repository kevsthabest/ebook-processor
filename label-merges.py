#!/usr/bin/env python3
"""Interactive character merge labeler.

Presents candidate character pairs from a preview JSON and lets you
approve/deny whether they're the same person. Saves labels for use
as evaluation data for the decision model.

Usage:
    python3 label-merges.py --json '.\preview\Daemon - Daniel Suarez.json'

Controls:
    y = yes, same person (merge)
    n = no, different people (don't merge)
    s = skip (unsure)
    q = quit and save
"""

import argparse
import json
import sys


def load_pairs(json_path, max_pairs=100):
    """Extract candidate pairs: aliases + similar names."""
    data = json.load(open(json_path, encoding='utf-8'))
    chars = data.get('characters', [])
    pairs = []
    seen = set()

    def add_pair(a_name, a_desc, b_name, b_desc, source):
        key = tuple(sorted([a_name.lower(), b_name.lower()]))
        if key in seen or a_name.lower() == b_name.lower():
            return
        seen.add(key)
        pairs.append({
            'a': {'name': a_name, 'desc': a_desc[:200]},
            'b': {'name': b_name, 'desc': b_desc[:200]},
            'source': source,
        })

    # Alias pairs (pipeline thinks these are the same)
    for c in chars:
        name = c.get('name', '')
        desc = c.get('description', '')
        for alias in c.get('aliases', []):
            a_name = alias if isinstance(alias, str) else alias.get('name', '')
            if a_name:
                add_pair(name, desc, a_name, '', 'alias')

    # Similar name pairs (shared surname or token overlap, different characters)
    for i, c1 in enumerate(chars):
        for c2 in chars[i+1:]:
            n1, n2 = c1.get('name', ''), c2.get('name', '')
            t1 = set(n1.lower().split())
            t2 = set(n2.lower().split())
            # Shared surname or significant token overlap
            if t1 & t2 and len(t1 & t2) >= 1:
                # Skip if one is clearly a subset (title + name)
                add_pair(n1, c1.get('description', ''),
                         n2, c2.get('description', ''), 'similar')

    return pairs[:max_pairs]


def main():
    ap = argparse.ArgumentParser(description="Interactive merge labeler")
    ap.add_argument('--json', required=True, help="Preview JSON file")
    ap.add_argument('--output', default='merge-labels.json',
                    help="Output labels file")
    ap.add_argument('--max-pairs', type=int, default=100)
    args = ap.parse_args()

    # Load existing labels
    labels = {}
    try:
        labels = json.load(open(args.output, encoding='utf-8'))
        print(f"Loaded {len(labels)} existing labels from {args.output}")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    pairs = load_pairs(args.json, args.max_pairs)
    # Skip already-labeled
    pairs = [p for p in pairs
             if tuple(sorted([p['a']['name'].lower(),
                              p['b']['name'].lower()])) not in labels]
    print(f"{len(pairs)} unlabeled pairs to review\n")

    count = 0
    for p in pairs:
        key = tuple(sorted([p['a']['name'].lower(),
                            p['b']['name'].lower()]))
        print(f"\n--- Pair {count+1}/{len(pairs)} [{p['source']}] ---")
        print(f"  A: {p['a']['name']}")
        if p['a']['desc']:
            print(f"     {p['a']['desc'][:120]}")
        print(f"  B: {p['b']['name']}")
        if p['b']['desc']:
            print(f"     {p['b']['desc'][:120]}")

        while True:
            ans = input("  Same person? [y/n/s/q]: ").strip().lower()
            if ans in ('y', 'n', 's', 'q'):
                break
            print("  Please enter y, n, s, or q")

        if ans == 'q':
            break
        if ans == 's':
            continue

        labels[key] = {
            'a': p['a']['name'],
            'b': p['b']['name'],
            'same_person': ans == 'y',
            'source': p['source'],
        }
        count += 1

        # Save incrementally
        json.dump(labels, open(args.output, 'w', encoding='utf-8'), indent=2)

    print(f"\nDone. {count} new labels saved to {args.output}")
    print(f"Total: {len(labels)} labels")


if __name__ == '__main__':
    main()
