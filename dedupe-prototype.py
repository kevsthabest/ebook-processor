#!/usr/bin/env python3
"""
Dedup prototype v2 for the Cozy Libram ebook processor.

Reads a preview JSON produced by `process-portable.py --preview` and
reports character merge candidates using two signals:

1. NAME NORMALIZATION (deterministic): casefold, strip leading articles
   ("the"/"a"/"an") and collapse whitespace. Exact matches on the
   normalized name merge for free, no embeddings needed.
   ("The narrator" == "the narrator", "Landlord" == "the landlord")

2. EMBEDDINGS (semantic): each character's "name: description" is embedded
   via an OpenAI-compatible `/v1/embeddings` endpoint; pairs at/above the
   cosine threshold are merge candidates. Catches cases normalization
   cannot ("Daddy" == "the father").

Read-only: never touches Supabase, never modifies the preview file.
Merge decisions stay human-reviewed (candidates feed Trope Lab).

Usage:
  python dedupe-prototype.py "preview/Life Among the Savages - Shirley Jackson.json"
  python dedupe-prototype.py preview.json --embed-model my-model --threshold 0.80
"""

PROTOTYPE_VERSION = "v2.1"

import argparse
import json
import math
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def norm_name(n):
    """Deterministic name key: casefold, drop leading article, tidy spaces."""
    n = (n or "").strip().casefold()
    n = re.sub(r"^(the|a|an)\s+", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def embed_all(base_url, model, texts):
    """Return a list of embedding vectors, one per text (order preserved)."""
    url = base_url.rstrip("/") + "/embeddings"
    payload = {"model": model, "input": texts}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: {url} -> HTTP {e.code}: "
                 f"{e.read().decode('utf-8', 'ignore')[:300]}")
    except Exception as e:
        sys.exit(f"ERROR: could not reach {url}: {e}")
    vecs = [None] * len(texts)
    for item in data.get("data", []):
        vecs[item["index"]] = item["embedding"]
    if any(v is None for v in vecs):
        sys.exit("ERROR: server did not return an embedding for every input")
    return vecs


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def main():
    ap = argparse.ArgumentParser(
        description="Embedding + name-normalization character dedup report.")
    ap.add_argument("preview", help="preview JSON from process-portable.py --preview")
    ap.add_argument("--embed-url", default="http://127.0.0.1:8888/v1")
    ap.add_argument("--embed-model", default="")
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="cosine similarity at/above which two characters are merge candidates")
    args = ap.parse_args()
    print(f"dedupe-prototype {PROTOTYPE_VERSION}")

    try:
        d = json.loads(Path(args.preview).read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"ERROR: cannot read preview JSON: {e}")
    chars = d.get("characters") or []
    if not chars:
        sys.exit("No characters in preview JSON.")
    names = [c.get("name") or "" for c in chars]

    parent = list(range(len(chars)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    merges = []  # (i, j, source, score_or_None)

    # --- Signal 1: deterministic name normalization ---
    buckets = {}
    for i, n in enumerate(names):
        buckets.setdefault(norm_name(n), []).append(i)
    for key, idxs in buckets.items():
        if len(idxs) > 1 and key:  # ignore empty names
            for a, b in zip(idxs, idxs[1:]):
                union(a, b)
                merges.append((a, b, "name", None))
    n_name = sum(1 for m in merges if m[2] == "name")
    print(f"Signal 1 (name normalization): {n_name} deterministic merges")
    # Snapshot signal-1 clusters: signal 2 skips pairs already merged here,
    # but still records every above-threshold pair it finds itself.
    s1_root = [find(i) for i in range(len(chars))]

    # --- Signal 2: embeddings ---
    texts = [f"{n}: {chars[i].get('description') or ''}" for i, n in enumerate(names)]
    print(f"Signal 2 (embeddings): embedding {len(texts)} characters via {args.embed_url} ...")
    vecs = embed_all(args.embed_url, args.embed_model, texts)
    print(f"OK ({len(vecs[0])}-dim vectors)")
    pairs = []
    for i in range(len(chars)):
        for j in range(i + 1, len(chars)):
            if s1_root[i] == s1_root[j]:
                continue  # already merged by signal 1
            s = cos(vecs[i], vecs[j])
            if s >= args.threshold:
                union(i, j)
                merges.append((i, j, "embed", s))
                pairs.append((s, i, j))
    pairs.sort(reverse=True)
    print(f"Signal 2: {len(pairs)} pairs at/above {args.threshold}\n")

    clusters = {}
    for i in range(len(chars)):
        clusters.setdefault(find(i), []).append(i)
    multi = sorted((m for m in clusters.values() if len(m) > 1),
                   key=len, reverse=True)

    print(f"== Merge candidates ==")
    print(f"{len(chars)} characters -> {len(clusters)} clusters "
          f"({len(multi)} with 2+ members)\n")
    for m in multi:
        canon = max(m, key=lambda i: (chars[i].get("confidence") or 0,
                                      len(names[i])))
        print(f"[cluster of {len(m)}] canonical suggestion: {names[canon]!r}")
        for i in sorted(m, key=lambda i: names[i].casefold()):
            c = chars[i]
            conf = c.get("confidence")
            print(f"    - {names[i]!r} (role={c.get('role')}, "
                  f"conf={conf if conf is not None else '?'})")
        # show which signal(s) built this cluster
        ev = []
        for (a, b, src, s) in merges:
            if find(a) == find(m[0]):
                ev.append(f"{src}:{s:.3f}" if s is not None else src)
        print(f"    via: {', '.join(sorted(set(ev)))}\n")

    print("== Strongest embedding pairs ==")
    for s, i, j in pairs[:15]:
        print(f"  {s:.3f}  {names[i]!r}  <->  {names[j]!r}")
    if not pairs:
        print("  (none above threshold)")


if __name__ == "__main__":
    main()
