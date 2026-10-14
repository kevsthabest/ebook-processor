#!/usr/bin/env python3
"""Variance scorer for two previews of the SAME book from two runs.
Usage:
    py preview_variance.py OLD_JSON NEW_JSON

Diffs the fields that matter for the processor<->app quality loop (spice,
tropes, triggers, characters, relationships, POVs) and reports field-by-
field stability: set overlap, Jaccard similarity, added/removed items, and
a one-line score per field. Run-to-run variance becomes a number instead
of an impression, so two pipeline experiments can be compared at a glance.

Stdlib only. Keys are compared through the pipeline's own normalization
(norm / norm_name from process-portable.py, copy-pasted so this stays a
standalone helper like inject_isbn.py).
"""
import json, os, re, sys, unicodedata

MAX_LIST = 20  # cap added/removed listings per field


def norm(s):
    """Pipeline norm(): lowercase, strip accents/punctuation, collapse ws.
    'Fourth Wing' -> 'fourth wing'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\W_]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def norm_name(n):
    """Pipeline norm_name(): casefold, drop leading article, tidy spaces."""
    n = (n or "").strip().casefold()
    n = re.sub(r"^(the|a|an)\s+", "", n)
    return re.sub(r"\s+", " ", n).strip()


def jaccard(a, b):
    """Jaccard similarity of two sets. Both empty -> 1.0 (perfect agreement
    on nothing). One empty, one not -> 0.0."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def set_diff(old, new):
    """(shared, added, removed) between two key sets."""
    return old & new, new - old, old - new


def _fmt_list(items, label_of):
    """Render a sorted list of keys, capped at MAX_LIST entries."""
    items = sorted(items)
    shown = items[:MAX_LIST]
    lines = [f"    {label_of(k)}" for k in shown]
    if len(items) > MAX_LIST:
        lines.append(f"    ... and {len(items) - MAX_LIST} more")
    return lines


# --- field extractors: preview dict -> (key set, {key: display}) -----------

def _missing(d, field):
    return field not in d or d.get(field) is None


def extract_tropes(d):
    """Tropes are a list of label strings."""
    labels = {}
    for t in d.get("tropes") or []:
        if isinstance(t, dict):
            t = t.get("label") or t.get("name") or ""
        k = norm(str(t))
        if k:
            labels.setdefault(k, str(t))
    return set(labels), labels


def extract_triggers(d):
    """Returns (keys, {key: display}, {key: prominence})."""
    labels, prom = {}, {}
    for t in d.get("triggers") or []:
        if isinstance(t, dict):
            w = t.get("warning") or t.get("label") or t.get("name") or ""
            p = t.get("prominence") or ""
        else:
            w, p = str(t), ""
        k = norm(str(w))
        if k:
            labels.setdefault(k, str(w))
            prom.setdefault(k, str(p).strip().lower())
    return set(labels), labels, prom


def extract_characters(d, field):
    """Character entries are dicts with 'name' (or bare strings)."""
    labels = {}
    for c in d.get(field) or []:
        name = c.get("name") if isinstance(c, dict) else str(c)
        k = norm_name(name or "")
        if k:
            labels.setdefault(k, (name or "").strip())
    return set(labels), labels


def extract_relationships(d):
    """Keyed by (from, to, type) normalized; pair is order-insensitive."""
    labels = {}
    for r in d.get("relationships") or []:
        if not isinstance(r, dict):
            continue
        a, b = norm_name(r.get("from") or ""), norm_name(r.get("to") or "")
        typ = norm(str(r.get("type") or ""))
        if not a or not b:
            continue
        pair = tuple(sorted((a, b)))
        key = (pair, typ)
        disp_a = (r.get("from") or "").strip()
        disp_b = (r.get("to") or "").strip()
        labels.setdefault(key, f"{disp_a} <-> {disp_b} [{r.get('type') or ''}]")
    return set(labels), labels


def extract_full_roster(d):
    """Principals + minor characters combined into one name set."""
    keys, labels = extract_characters(d, "characters")
    mkeys, mlabels = extract_characters(d, "minor_characters")
    return keys | mkeys, {**mlabels, **labels}


def extract_povs(d):
    labels = {}
    for p in d.get("povs") or []:
        name = p.get("name") if isinstance(p, dict) else str(p)
        k = norm_name(name or "")
        if k:
            labels.setdefault(k, (name or "").strip())
    return set(labels), labels


# --- comparison -------------------------------------------------------------

def compare_set_field(old_d, new_d, field, extractor, header):
    """Generic set comparison. Returns (report lines, score or None)."""
    lines = [f"{header}:"]
    if field is not None:
        if _missing(old_d, field) and _missing(new_d, field):
            lines.append("  absent in both")
            return lines, None
        if _missing(old_d, field):
            lines.append("  absent in old")
            return lines, None
        if _missing(new_d, field):
            lines.append("  absent in new")
            return lines, None
    old_keys, old_labels = extractor(old_d)
    new_keys, new_labels = extractor(new_d)
    labels = {**old_labels, **new_labels}
    shared, added, removed = set_diff(old_keys, new_keys)
    sim = jaccard(old_keys, new_keys)
    lines.append(
        f"  old {len(old_keys)}, new {len(new_keys)}, shared {len(shared)}, "
        f"Jaccard {sim:.3f}")
    if added:
        lines.append(f"  added ({len(added)}):")
        lines.extend(_fmt_list(added, labels.__getitem__))
    if removed:
        lines.append(f"  removed ({len(removed)}):")
        lines.extend(_fmt_list(removed, labels.__getitem__))
    if not added and not removed:
        lines.append("  identical sets")
    return lines, sim


def compare_triggers(old_d, new_d):
    lines = ["Triggers:"]
    if _missing(old_d, "triggers") and _missing(new_d, "triggers"):
        lines.append("  absent in both")
        return lines, None
    if _missing(old_d, "triggers"):
        lines.append("  absent in old")
        return lines, None
    if _missing(new_d, "triggers"):
        lines.append("  absent in new")
        return lines, None
    old_keys, old_labels, old_prom = extract_triggers(old_d)
    new_keys, new_labels, new_prom = extract_triggers(new_d)
    labels = {**old_labels, **new_labels}
    shared, added, removed = set_diff(old_keys, new_keys)
    sim = jaccard(old_keys, new_keys)
    lines.append(
        f"  old {len(old_keys)}, new {len(new_keys)}, shared {len(shared)}, "
        f"Jaccard {sim:.3f}")
    if added:
        lines.append(f"  added ({len(added)}):")
        lines.extend(_fmt_list(added, labels.__getitem__))
    if removed:
        lines.append(f"  removed ({len(removed)}):")
        lines.extend(_fmt_list(removed, labels.__getitem__))
    changed = [k for k in sorted(shared)
               if old_prom.get(k) and new_prom.get(k)
               and old_prom[k] != new_prom[k]]
    if changed:
        lines.append(f"  prominence changes ({len(changed)}):")
        for k in changed[:MAX_LIST]:
            lines.append(
                f"    {labels[k]}: {old_prom[k]} -> {new_prom[k]}")
        if len(changed) > MAX_LIST:
            lines.append(f"    ... and {len(changed) - MAX_LIST} more")
    elif shared:
        lines.append("  prominence: no changes on shared triggers")
    if not added and not removed and not changed:
        lines.append("  identical (labels + prominence)")
    return lines, sim


def compare_spice(old_d, new_d):
    lines = ["Spice:"]
    o, n = old_d.get("spice_level"), new_d.get("spice_level")
    if o is None and n is None:
        lines.append("  absent in both")
        return lines, None
    if o is None:
        lines.append("  absent in old")
        return lines, None
    if n is None:
        lines.append("  absent in new")
        return lines, None
    lines.append(f"  old {o}, new {n}, delta {n - o:+d}")
    return lines, 1.0 if o == n else 0.0


def compare_previews(old_d, new_d):
    """Full report as a list of lines; last block is the summary scores."""
    lines = []
    ot, nt = old_d.get("title") or "?", new_d.get("title") or "?"
    oa, na = old_d.get("author") or "?", new_d.get("author") or "?"
    lines.append(f"Book: {ot} by {oa}")
    if norm(ot) != norm(nt) or norm(oa) != norm(na):
        lines.append(f"WARNING: identity differs — new file is "
                     f"'{nt}' by {na}")
    lines.append("")

    scores = {}
    sec, scores["spice"] = compare_spice(old_d, new_d)
    lines += sec + [""]
    sec, scores["tropes"] = compare_set_field(
        old_d, new_d, "tropes", extract_tropes, "Tropes")
    lines += sec + [""]
    sec, scores["triggers"] = compare_triggers(old_d, new_d)
    lines += sec + [""]
    sec, scores["characters"] = compare_set_field(
        old_d, new_d, "characters",
        lambda d: extract_characters(d, "characters"),
        "Characters (principals)")
    lines += sec + [""]
    if not _missing(old_d, "minor_characters") or \
            not _missing(new_d, "minor_characters"):
        # Full roster = principals + minors; only comparable when both
        # sides carry the minor list.
        if _missing(old_d, "minor_characters"):
            lines += ["Characters (full roster):", "  absent in old", ""]
            scores["characters (full roster)"] = None
        elif _missing(new_d, "minor_characters"):
            lines += ["Characters (full roster):", "  absent in new", ""]
            scores["characters (full roster)"] = None
        else:
            sec, scores["characters (full roster)"] = compare_set_field(
                old_d, new_d, None, extract_full_roster,
                "Characters (full roster)")
            lines += sec + [""]
    sec, scores["relationships"] = compare_set_field(
        old_d, new_d, "relationships", extract_relationships, "Relationships")
    lines += sec + [""]
    sec, scores["povs"] = compare_set_field(
        old_d, new_d, "povs", extract_povs, "POVs")
    lines += sec + [""]

    lines.append("Summary (1.000 = identical):")
    for name, s in scores.items():
        lines.append(f"  {name:<26} {'n/a' if s is None else f'{s:.3f}'}")
    present = [s for s in scores.values() if s is not None]
    if present:
        lines.append(f"  {'overall (mean)':<26} "
                     f"{sum(present) / len(present):.3f}")
    return lines


def load_preview(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    if len(sys.argv) != 3:
        print("Usage: py preview_variance.py OLD_JSON NEW_JSON")
        sys.exit(1)
    for p in sys.argv[1:]:
        if not os.path.isfile(p):
            print(f"Not found: {p}")
            sys.exit(1)
    try:
        old_d = load_preview(sys.argv[1])
        new_d = load_preview(sys.argv[2])
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"ERROR: could not parse preview JSON: {e}")
        sys.exit(2)
    print(f"Old: {sys.argv[1]}")
    print(f"New: {sys.argv[2]}")
    print()
    print("\n".join(compare_previews(old_d, new_d)))


if __name__ == "__main__":
    main()
