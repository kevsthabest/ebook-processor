#!/usr/bin/env python3
r"""Interactive character merge labeler.

Presents candidate character pairs from a preview JSON and lets you
approve/deny whether they're the same person. Saves labels for use
as evaluation data for the decision model.

Usage:
    python3 label-merges.py --json '.\preview\Daemon - Daniel Suarez.json'

Controls:
    y = yes, same person (merge)
    n = no, different people (don't merge)
    m = maybe (leaning, but not confident)
    s = skip (come back later)
    q = quit and save
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


class LabelError(Exception):
    """A user-facing failure: an existing labels file that cannot
    be used. Printed as one line; no traceback for expected cases."""


def _atomic_write_text(path, text):
    """Write text to path atomically: the new content lands in a
    temp file in the same directory first and is then moved over
    the target with os.replace, so a crash mid-save leaves the
    previous good file intact instead of a torn one the next load
    would have to judge."""
    path = Path(path)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(path.parent),
                prefix=path.name + ".", suffix=".tmp",
                delete=False) as fh:
            tmp = Path(fh.name)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _load_merges(path):
    """Load the existing labels from path.

    A missing file is the normal fresh start and returns an empty
    store. A file that exists but cannot be parsed is a loud stop:
    the labels are hand-made curation state, so treating a corrupt
    file as empty would let the next save overwrite it and destroy
    them silently. The operator proceeds deliberately — fix the
    file, or move it aside to start fresh — never by default."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise LabelError(
            f"labels file is not valid JSON: {path} ({e}) — "
            f"refusing to start, because treating it as empty "
            f"would overwrite the labels it holds. Fix the file, "
            f"or move it aside to start fresh, then re-run.")
    except OSError as e:
        raise LabelError(
            f"labels file cannot be read: {path} ({e}) — "
            f"refusing to start, because treating it as empty "
            f"would overwrite the labels it holds. Fix the file, "
            f"or move it aside to start fresh, then re-run.")
    if not isinstance(data, dict):
        raise LabelError(
            f"labels file is not a JSON object: {path} — "
            f"refusing to start, because treating it as empty "
            f"would overwrite the labels it holds. Fix the file, "
            f"or move it aside to start fresh, then re-run.")
    return data


def _save_merges(path, labels):
    """Save the labels atomically — see _atomic_write_text for why
    an in-place write is not safe for a file the operator edits."""
    _atomic_write_text(path, json.dumps(labels, indent=2))


def load_pairs(json_path, max_pairs=100):
    """Extract candidate pairs: aliases + similar names."""
    data = json.load(open(json_path, encoding='utf-8'))
    chars = data.get('characters', [])
    pairs = []
    seen = set()

    def add_pair(a_name, a_desc, b_name, b_desc, source):
        key = "|||".join(sorted([a_name.lower(), b_name.lower()]))
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Interactive merge labeler")
    ap.add_argument('--json', required=True, help="Preview JSON file")
    ap.add_argument('--output', default='merge-labels.json',
                    help="Output labels file")
    ap.add_argument('--max-pairs', type=int, default=100)
    args = ap.parse_args(argv)

    # Load existing labels. A corrupt file stops here, before any
    # save can run — see _load_merges for why it is never treated
    # as an empty store.
    try:
        labels = _load_merges(args.output)
    except LabelError as e:
        print(f"label-merges: {e}", file=sys.stderr)
        return 2
    if Path(args.output).exists():
        print(f"Loaded {len(labels)} existing labels from {args.output}")

    pairs = load_pairs(args.json, args.max_pairs)
    # Skip already-labeled
    pairs = [p for p in pairs
             if "|||".join(sorted([p['a']['name'].lower(),
                                   p['b']['name'].lower()])) not in labels]
    print(f"{len(pairs)} unlabeled pairs to review\n")

    count = 0
    for p in pairs:
        key = "|||".join(sorted([p['a']['name'].lower(),
                                 p['b']['name'].lower()]))
        print(f"\n--- Pair {count+1}/{len(pairs)} [{p['source']}] ---")
        print(f"  A: {p['a']['name']}")
        if p['a']['desc']:
            print(f"     {p['a']['desc'][:120]}")
        print(f"  B: {p['b']['name']}")
        if p['b']['desc']:
            print(f"     {p['b']['desc'][:120]}")

        while True:
            ans = input("  Same person? [y/n/m/s/q]: ").strip().lower()
            if ans in ('y', 'n', 'm', 's', 'q'):
                break
            print("  Please enter y, n, m, s, or q")

        if ans == 'q':
            break
        if ans == 's':
            continue

        labels[key] = {
            'a': p['a']['name'],
            'b': p['b']['name'],
            'same_person': ans == 'y',
            'uncertain': ans == 'm',
            'source': p['source'],
        }
        count += 1

        # Save incrementally, atomically: a crash here must not
        # tear the labels file the operator has accumulated.
        _save_merges(args.output, labels)

    print(f"\nDone. {count} new labels saved to {args.output}")
    print(f"Total: {len(labels)} labels")
    return 0


if __name__ == '__main__':
    sys.exit(main())
