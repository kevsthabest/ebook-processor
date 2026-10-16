#!/usr/bin/env python3
"""Variance scorer for two previews of the SAME book from two runs.
Usage:
    py preview_variance.py OLD_JSON NEW_JSON

Diffs the fields that matter for the processor<->app quality loop (spice,
tropes, triggers, characters, relationships, POVs) and reports field-by-
field stability: set overlap, Jaccard similarity, added/removed items, and
a one-line score per field. Run-to-run variance becomes a number instead
of an impression, so two pipeline experiments can be compared at a glance.

Below the legacy report, a quality-dashboard block adds labelled
precision/recall (when an eval/ labels fixture matches the book, or one
is passed with --labels), roster-identity indicators, form-flip
explanations for Jaccard churn, unresolved-endpoint and empty-evidence
rates, spice threshold crossings, and queue churn. The legacy sections
are a frozen historical record; the dashboard only ever appends.

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


# --- quality dashboard ------------------------------------------------------
# Added measurement sections, appended after the legacy report. The legacy
# sections above are a frozen historical record: their lines and scores
# must stay byte-identical for the same inputs, so everything below only
# ever appends. The dashboard separates WHAT changed from HOW BAD it is:
# labelled precision/recall, roster-identity indicators, evidence quality,
# and the churn the raw Jaccards cannot explain.

# Mirrors _REL_FAMILY_TYPES in process-portable.py (same copy-paste
# convention as norm/norm_name above).
FAMILY_TYPES = frozenset({"spouse", "parent", "child", "sibling"})
_INVERSE_TYPE = {"parent": "child", "child": "parent"}


def _token_prefix(a, b):
    """Length of the shared leading-token prefix of two normalized names."""
    k = 0
    for x, y in zip(a.split(), b.split()):
        if x != y:
            break
        k += 1
    return k


def _prefix_related(a, b):
    """True when one normalized name is a leading-token prefix of the
    other ('violet' vs 'violet sorrengail'). Trailing tokens never match:
    a bare surname is shared by the whole family and identifies nobody."""
    if not a or not b or a == b:
        return False
    return _token_prefix(a, b) == min(len(a.split()), len(b.split()))


def _roster_entries(d):
    """Identity-bearing entries of a preview: chapter_data.roster when
    present (richest alias data), else characters + minor_characters.
    Returns [(entry_id, display_name, {normalized name forms})]."""
    cd = d.get("chapter_data")
    roster = cd.get("roster") if isinstance(cd, dict) else None
    entries = []
    if isinstance(roster, dict) and roster:
        for key, e in roster.items():
            if not isinstance(e, dict):
                continue
            forms = set()
            raw = [e.get("name")] + list(e.get("aliases") or []) \
                + list(e.get("alias_keys") or []) \
                + list(e.get("primary_keys") or [])
            for f in raw:
                k = norm_name(str(f or ""))
                if k:
                    forms.add(k)
            entries.append((str(key), (e.get("name") or str(key)).strip(),
                            forms))
        return entries
    for field in ("characters", "minor_characters"):
        for c in d.get(field) or []:
            name = c.get("name") if isinstance(c, dict) else str(c)
            aliases = c.get("aliases") if isinstance(c, dict) else None
            forms = set()
            for f in [name] + list(aliases or []):
                k = norm_name(str(f or ""))
                if k:
                    forms.add(k)
            entries.append((norm_name(name or ""), (name or "").strip(),
                            forms))
    return entries


class _Resolver:
    """Resolves a raw endpoint/name string to a roster entry id within
    one preview, mirroring the pipeline's own identity mechanism: exact
    alias/name forms first, then a unique leading-token match. Ambiguous
    or unknown strings resolve to their own normalized form, so they can
    still match an identical string on the other side."""

    def __init__(self, d):
        self.entries = _roster_entries(d)
        self._exact = {}
        for eid, _disp, forms in self.entries:
            for f in forms:
                self._exact.setdefault(f, set()).add(eid)

    def resolve(self, raw):
        n = norm_name(raw or "")
        if not n:
            return n
        hits = self._exact.get(n)
        if hits:
            return next(iter(hits)) if len(hits) == 1 else n
        best, best_len = set(), 0
        for eid, _disp, forms in self.entries:
            for f in forms:
                if _prefix_related(n, f):
                    k = _token_prefix(n, f)
                    if k > best_len:
                        best, best_len = {eid}, k
                    elif k == best_len:
                        best.add(eid)
        if len(best) == 1 and best_len:
            return next(iter(best))
        return n


def _pair_key(resolver, a, b):
    """Unordered resolved endpoint pair for a claim or labelled pair."""
    return tuple(sorted((resolver.resolve(a), resolver.resolve(b))))


def _endpoints_match(a, b):
    return a == b or _prefix_related(a, b)


def _type_verdict(label_pair, rel, resolver):
    """Classify a kept claim against a labelled pair whose endpoints it
    shares: 'kept' (claimed type, either orientation), 'downgraded'
    (survived as 'other'), 'wrong_type' (survived at another family
    type), or None (a different, non-family claim — not this pair).
    Orientation is determined by resolved entry ids first, then by the
    raw endpoint strings, so a claim stays attributable even when the
    run's roster conflated the labelled identities (see the roster-
    identity indicators for the conflation itself)."""
    ltype = norm(label_pair.get("type") or "")
    rtype = norm(rel.get("type") or "")
    la, lb = resolver.resolve(label_pair.get("from")), \
        resolver.resolve(label_pair.get("to"))
    ra, rb = resolver.resolve(rel.get("from")), resolver.resolve(rel.get("to"))
    if (ra, rb) == (la, lb):
        strict = rtype == ltype
    elif (ra, rb) == (lb, la):
        strict = rtype == _INVERSE_TYPE.get(ltype, ltype)
    else:
        lf, lto = norm_name(label_pair.get("from") or ""), \
            norm_name(label_pair.get("to") or "")
        rf, rto = norm_name(rel.get("from") or ""), \
            norm_name(rel.get("to") or "")
        if _endpoints_match(lf, rf) and _endpoints_match(lto, rto):
            strict = rtype == ltype
        elif _endpoints_match(lf, rto) and _endpoints_match(lto, rf):
            strict = rtype == _INVERSE_TYPE.get(ltype, ltype)
        else:
            return None
    if strict:
        return "kept"
    if rtype == "other":
        return "downgraded"
    if rtype in FAMILY_TYPES:
        return "wrong_type"
    return None


def _raw_form_match(label_pair, rel):
    """Tier signal: the claim's raw endpoint strings directly resemble
    the label's (normalized-equal or leading-token related) in the
    orientation that resolves. Preferred over resolver-only matches,
    which can arise when a run merged two labelled identities into one
    entry and one claim then serves two pairs."""
    lf, lt = norm_name(label_pair.get("from") or ""), \
        norm_name(label_pair.get("to") or "")
    rf, rt = norm_name(rel.get("from") or ""), norm_name(rel.get("to") or "")
    same = all(x == y or _prefix_related(x, y)
               for x, y in ((lf, rf), (lt, rt)))
    swap = all(x == y or _prefix_related(x, y)
               for x, y in ((lf, rt), (lt, rf)))
    return same or swap


def score_labelled_pairs(d, labels):
    """Per-pair outcomes for one preview against a labels fixture.
    Returns {'true': {...}, 'false': {...}} mapping outcome ->
    [display strings]. Outcomes: kept (at the claimed type),
    downgraded (survived as 'other'), wrong_type (survived at another
    family type), dropped (killed; a validation_note of 'no_evidence'
    counts, per eval/README.md), absent (never extracted as a family
    claim). A budget-exhaustion survival is a kept claim in the preview
    and is classified by its surviving type like any other."""
    resolver = _Resolver(d)
    kept = [r for r in d.get("relationships") or [] if isinstance(r, dict)]
    dropped = [r for r in d.get("dropped_relationships") or []
               if isinstance(r, dict)]
    kept_by_pair = {}
    for r in kept:
        kept_by_pair.setdefault(
            _pair_key(resolver, r.get("from"), r.get("to")), []).append(r)
    dropped_family = [r for r in dropped
                      if norm(r.get("type") or "") in FAMILY_TYPES
                      or norm(r.get("type") or "") == "other"]
    dropped_pairs = {
        _pair_key(resolver, r.get("from"), r.get("to"))
        for r in dropped_family}
    out = {}
    for kind, key in (("true", "true_pairs"), ("false", "false_pairs")):
        res = {"kept": [], "downgraded": [], "wrong_type": [],
               "dropped": [], "absent": []}
        for pair in labels.get(key) or []:
            disp = f"{pair.get('from')} -> {pair.get('to')} " \
                   f"[{pair.get('type')}]"
            pk = _pair_key(resolver, pair.get("from"), pair.get("to"))
            best = None  # (tier, rank, verdict)
            rank = {"kept": 3, "downgraded": 2, "wrong_type": 1}
            candidates = list(kept_by_pair.get(pk, []))
            for r in kept:
                if r not in candidates and _raw_form_match(pair, r):
                    candidates.append(r)
            for r in candidates:
                verdict = _type_verdict(pair, r, resolver)
                if verdict is None:
                    continue
                cand = (1 if _raw_form_match(pair, r) else 0,
                        rank[verdict], verdict)
                if best is None or cand[:2] > best[:2]:
                    best = cand
            if best is not None:
                res[best[2]].append(disp)
            elif pk in dropped_pairs or any(
                    _raw_form_match(pair, r) for r in dropped_family):
                res["dropped"].append(disp)
            else:
                res["absent"].append(disp)
        out[kind] = res
    return out


def _labelled_entities(labels):
    """Distinct endpoint display names across both pair lists."""
    seen, out = set(), []
    for key in ("true_pairs", "false_pairs"):
        for pair in labels.get(key) or []:
            for end in (pair.get("from"), pair.get("to")):
                k = norm_name(end or "")
                if k and k not in seen:
                    seen.add(k)
                    out.append((k, (end or "").strip()))
    return out


def identity_indicators(d, labels):
    """Roster-level false_merge / false_split indicators (heuristic).
    A roster entry CLAIMS a labelled entity when one of its name forms
    matches the entity's name exactly or by unique leading-token match
    (a form matching two entities claims neither — ambiguity is not
    evidence). A merge = one entry claiming >= 2 labelled entities; a
    split = one entity claimed by >= 2 entries. Returns
    ([merge lines], [split lines])."""
    entities = _labelled_entities(labels)
    entries = _roster_entries(d)
    claims = {}  # entry index -> {entity keys}
    for idx, (_eid, _disp, forms) in enumerate(entries):
        claimed = set()
        for f in forms:
            cands = [ek for ek, _ed in entities
                     if f == ek or _prefix_related(f, ek)]
            if len(cands) == 1:
                claimed.add(cands[0])
        if claimed:
            claims[idx] = claimed
    disp_of = dict(entities)
    merges = []
    for idx in sorted(claims):
        if len(claims[idx]) >= 2:
            names = ", ".join(disp_of[k] for k in sorted(claims[idx]))
            merges.append(
                f"entry '{entries[idx][1]}' carries forms of: {names}")
    by_entity = {}
    for idx, claimed in claims.items():
        for ek in claimed:
            by_entity.setdefault(ek, []).append(idx)
    splits = []
    for ek in sorted(by_entity):
        idxs = by_entity[ek]
        if len(idxs) >= 2:
            names = ", ".join(f"'{entries[i][1]}'" for i in sorted(idxs))
            splits.append(f"'{disp_of[ek]}' claimed by entries: {names}")
    return merges, splits


def form_flips(old_d, new_d):
    """Names that left one run and plausibly re-entered the other under
    a different form: (old display, new display, reason) triples where
    reason is 'token-subset' or 'alias-linked'. Heuristic — it explains
    raw-Jaccard churn and changes no score."""
    def universe(d):
        keys, labels = extract_full_roster(d)
        pk, plabels = extract_povs(d)
        merged = dict(labels)
        merged.update(plabels)
        return keys | pk, merged
    old_keys, old_labels = universe(old_d)
    new_keys, new_labels = universe(new_d)
    labels = {**old_labels, **new_labels}

    def alias_owner(d):
        owner = {}
        for _eid, disp, forms in _roster_entries(d):
            for f in forms:
                owner.setdefault(f, norm_name(disp))
        return owner
    old_owner, new_owner = alias_owner(old_d), alias_owner(new_d)
    flips = []
    for o in sorted(old_keys - new_keys):
        for n in sorted(new_keys - old_keys):
            ot, nt = set(o.split()), set(n.split())
            if ot < nt or nt < ot:
                flips.append((labels[o], labels[n], "token-subset"))
            elif new_owner.get(o) == n or old_owner.get(n) == o:
                flips.append((labels[o], labels[n], "alias-linked"))
    return flips


def unresolved_endpoints(d):
    """Kept relationships pointing at names with no character row:
    (bad count, total kept, [unresolved endpoint displays]). Resolution
    is against the character list (characters + minor_characters) by
    name or alias — the set the app can actually join on."""
    forms = set()
    for field in ("characters", "minor_characters"):
        for c in d.get(field) or []:
            name = c.get("name") if isinstance(c, dict) else str(c)
            aliases = c.get("aliases") if isinstance(c, dict) else None
            for f in [name] + list(aliases or []):
                k = norm_name(str(f or ""))
                if k:
                    forms.add(k)
    bad, names = 0, []
    rels = [r for r in d.get("relationships") or [] if isinstance(r, dict)]
    for r in rels:
        missing = [r.get(e) for e in ("from", "to")
                   if norm_name(r.get(e) or "") not in forms]
        if missing:
            bad += 1
            for m in missing:
                if (m or "").strip() not in names:
                    names.append((m or "").strip())
    return bad, len(rels), names


def empty_evidence_rates(d):
    """Kept claims with no evidence quote, split family / non-family:
    ((empty, total) family, (empty, total) non-family). This is the
    review-queue cost driver: evidence-less claims are the ones a human
    (or a later gate) has to re-derive from the text."""
    fam = [0, 0]
    nonfam = [0, 0]
    for r in d.get("relationships") or []:
        if not isinstance(r, dict):
            continue
        bucket = fam if norm(r.get("type") or "") in FAMILY_TYPES else nonfam
        bucket[1] += 1
        ev = r.get("evidence")
        if ev is None or not str(ev).strip():
            bucket[0] += 1
    return tuple(fam), tuple(nonfam)


def _rate(num, den):
    return f"{num / den:.3f}" if den else "n/a"


def _dashboard_run_lines(tag, d, labels):
    """Per-run dashboard lines for one preview."""
    lines = []
    if labels is not None:
        out = score_labelled_pairs(d, labels)
        t, f = out["true"], out["false"]
        nt = sum(len(v) for v in t.values())
        nf = sum(len(v) for v in f.values())
        lines.append(
            f"  {tag} true pairs: kept {len(t['kept'])}/{nt} "
            f"(recall {_rate(len(t['kept']), nt)}), "
            f"downgraded {len(t['downgraded'])}, "
            f"wrong family type {len(t['wrong_type'])}, "
            f"dropped {len(t['dropped'])}, "
            f"never extracted {len(t['absent'])}")
        fkept = len(f["kept"]) + len(f["downgraded"]) + len(f["wrong_type"])
        lines.append(
            f"  {tag} false pairs: kept {fkept}/{nf} (precision failures: "
            f"at claimed type {len(f['kept'])}, "
            f"downgraded {len(f['downgraded'])}, "
            f"wrong family type {len(f['wrong_type'])}), "
            f"dropped {len(f['dropped'])}, "
            f"never extracted {len(f['absent'])}")
        for disp in f["kept"] + f["downgraded"] + f["wrong_type"]:
            lines.append(f"    false pair survived: {disp}")
        merges, splits = identity_indicators(d, labels)
        src = "chapter_data.roster" if isinstance(
            d.get("chapter_data"), dict) and d["chapter_data"].get("roster") \
            else "characters"
        lines.append(
            f"  {tag} roster identity (heuristic, from {src}): "
            f"false merges {len(merges)}, false splits {len(splits)}")
        for m in merges[:MAX_LIST]:
            lines.append(f"    merge: {m}")
        for s in splits[:MAX_LIST]:
            lines.append(f"    split: {s}")
    bad, total, names = unresolved_endpoints(d)
    lines.append(
        f"  {tag} unresolved endpoints: {bad}/{total} kept relationships "
        f"({_rate(bad, total)})")
    if names:
        lines.append("    unresolved: " + ", ".join(
            f"'{n}'" for n in names[:MAX_LIST]))
    (fe, ft), (ne, nt2) = empty_evidence_rates(d)
    lines.append(
        f"  {tag} empty-evidence kept claims: family {fe}/{ft} "
        f"({_rate(fe, ft)}), non-family {ne}/{nt2} ({_rate(ne, nt2)})")
    return lines


def compare_dashboard(old_d, new_d, labels_data=None, labels_source=None):
    """The quality-dashboard block (lines after the legacy Summary)."""
    lines = ["Quality dashboard:"]
    if labels_data is not None:
        lines.append(
            f"  Labelled family pairs (shipped set — post-principals; "
            f"source: {labels_source}):")
    else:
        lines.append("  Labelled family pairs: n/a (no labels file "
                     "matched this book)")
        lines.append("  Roster identity: n/a (not determinable from "
                     "preview without labels)")
    lines += _dashboard_run_lines("old run:", old_d, labels_data)
    lines += _dashboard_run_lines("new run:", new_d, labels_data)
    flips = form_flips(old_d, new_d)
    lines.append(f"  Form flips (heuristic; scores unchanged): "
                 f"{len(flips)}")
    for o, n, why in flips[:MAX_LIST]:
        lines.append(f"    '{o}' (old) ~ '{n}' (new) [{why}]")
    if len(flips) > MAX_LIST:
        lines.append(f"    ... and {len(flips) - MAX_LIST} more")
    o_sp, n_sp = old_d.get("spice_level"), new_d.get("spice_level")
    if o_sp is None or n_sp is None:
        lines.append("  Spice threshold crossings: n/a (spice absent)")
    elif o_sp == n_sp:
        lines.append(f"  Spice threshold crossings: {o_sp} -> {n_sp}, "
                     f"no boundary crossed")
    else:
        crossed = list(range(min(o_sp, n_sp) + 1, max(o_sp, n_sp) + 1))
        lines.append(
            f"  Spice threshold crossings: {o_sp} -> {n_sp}, crossed: "
            + ", ".join(str(c) for c in crossed))
    ot, _ol = extract_tropes(old_d)
    nt_, _nl = extract_tropes(new_d)
    og, _ogl, _ogp = extract_triggers(old_d)
    ng, _ngl, _ngp = extract_triggers(new_d)
    lines.append(
        f"  Queue churn: tropes +{len(nt_ - ot)}/-{len(ot - nt_)}, "
        f"triggers +{len(ng - og)}/-{len(og - ng)} "
        f"(lists in the Tropes/Triggers sections above)")
    lines += staging_section(old_d, new_d)
    lines += trope_evidence_section(old_d, new_d)
    lines += entity_section(old_d, new_d)
    return lines


def trope_evidence_section(old_d, new_d):
    """Option G trope-evidence readout, read off the NEW preview's
    evidence_retrieval marker (design section 10 dashboard).
    Additions only: returns [] when the new preview carries no
    marker, so the dashboard's legacy output is byte-identical for
    marker-less previews. Marker claim keys are the pipeline's
    _tnorm keys; both sides are compared through this module's
    norm() — the two normalizers differ on punctuation, and
    applying one normalizer to both sides is what makes the keys
    meet."""
    nm = new_d.get("evidence_retrieval") or {}
    if not nm:
        return []
    claims = {norm(k): v for k, v in (nm.get("claims") or {}).items()
              if not k.startswith("trigger:")}
    shipped, labels = extract_tropes(new_d)
    with_support = sorted(
        k for k in shipped
        if claims.get(k) in ("supported-strong", "supported-single"))
    zero = sorted(k for k in shipped
                  if claims.get(k) == "abstained")
    pct = round(100 * len(with_support) / len(shipped)) \
        if shipped else 0
    lines = ["  Trope evidence (option G):"]
    lines.append(f"    shipped with passage support: "
                 f"{len(with_support)}/{len(shipped)} ({pct}%)")
    lines.append(f"    zero-support shipped (row stamp support "
                 f"'none'): {len(zero)}")
    for k in zero[:MAX_LIST]:
        lines.append(f"      - {labels.get(k, k)}")
    if len(zero) > MAX_LIST:
        lines.append(f"      ... and {len(zero) - MAX_LIST} more")
    n_abst = sum(1 for v in claims.values() if v == "abstained")
    n_adj = sum(1 for v in claims.values() if v == "adjacent-only")
    lines.append(f"    abstentions (all trope claims): {n_abst}; "
                 f"adjacent-only: {n_adj}")
    if nm.get("failed"):
        lines.append("    retrieval FAILED for every attempted "
                     "claim this run")
    if nm.get("evidence_recorded"):
        lines.append(
            f"    staging evidence recorded: "
            f"{nm['evidence_recorded']} staged claim(s); strong "
            f"support promotes on next sighting via evidence "
            f"({len(nm.get('promotes_next') or [])} claim(s))")
    return lines


def summary_gated_share(d):
    """Share of a run's trope candidates that sat in the gate band
    (below the auto band) — the claims the summary gate, not raw
    frequency, decided. Derived from the recorded candidate counts
    and the run's own auto band (max(3, chunks // 20), the seam's
    formula). 'n/a' when the preview recorded no counts."""
    counts = d.get("trope_candidate_counts") or {}
    if not counts:
        return "n/a (no candidate counts recorded)"
    auto_min = max(3, (d.get("chunks") or 0) // 20)
    gated = sum(1 for v in counts.values()
                if isinstance(v, (int, float)) and v < auto_min)
    pct = round(100 * gated / len(counts)) if counts else 0
    return f"{gated}/{len(counts)} ({pct}% below the auto band)"


def staging_section(old_d, new_d):
    """Option F staged-set comparison, read off the two previews'
    staging markers. Additions only: returns [] when neither run
    used the staging gate, so the dashboard's legacy output is
    byte-identical for unmarked previews."""
    om = old_d.get("staging") or {}
    nm = new_d.get("staging") or {}
    if not om and not nm:
        return []

    def held(m):
        return {s.get("key"): s for s in (m.get("staged") or [])}

    def prom(m):
        return {p.get("key"): p for p in (m.get("promoted") or [])}

    oh, nh, np_ = held(om), held(nm), prom(nm)
    stable = sorted(set(oh) & set(nh))
    newly = sorted(set(nh) - set(oh) - set(np_))
    promoted = sorted(set(np_) & set(oh))
    released = sorted(set(oh) - set(nh) - set(np_))
    lines = ["  Staging gate:"]

    def _names(keys, src):
        for k in keys[:MAX_LIST]:
            lines.append(f"      - {src[k].get('display') or k}")
        if len(keys) > MAX_LIST:
            lines.append(f"      ... and {len(keys) - MAX_LIST} more")

    lines.append(f"    stable traps (held in both runs): {len(stable)}")
    _names(stable, nh)
    lines.append(f"    newly staged (new run): {len(newly)}")
    _names(newly, nh)
    lines.append(f"    promoted between runs: {len(promoted)}")
    for k in promoted[:MAX_LIST]:
        # "promoted" is always paired with its via (design doctrine).
        lines.append(f"      - {np_[k].get('display') or k} "
                     f"(via {np_[k].get('via') or '?'})")
    if len(promoted) > MAX_LIST:
        lines.append(f"      ... and {len(promoted) - MAX_LIST} more")
    lines.append(
        f"    released (held in old, absent in new): {len(released)}")
    _names(released, oh)
    lines.append(f"    old run summary-gated share: "
                 f"{summary_gated_share(old_d)}")
    lines.append(f"    new run summary-gated share: "
                 f"{summary_gated_share(new_d)}")
    return lines


def entity_section(old_d, new_d):
    """Option B entity comparison, read off the two previews'
    roster markers + character/relationship identity annotations.
    Additions only: returns [] when neither preview carries roster
    data, so the dashboard's legacy output is byte-identical for
    previews from flag-off runs. Names can churn while identity
    holds (and vice versa); this section scores the identity."""
    om = old_d.get("roster_persistence") or {}
    nm = new_d.get("roster_persistence") or {}

    def entities(d):
        return {c.get("entity_id") for c in (d.get("characters") or [])
                if c.get("entity_id")}

    def principals(d):
        return {c.get("entity_id") for c in (d.get("characters") or [])
                if c.get("entity_id")
                and (c.get("frequency") or 0) >= 0.20}

    def triples(d):
        out = set()
        for r in (d.get("relationships") or []):
            a, b = r.get("from_id"), r.get("to_id")
            if a and b:
                out.add((min(a, b), max(a, b), r.get("type") or ""))
        return out

    oe, ne = entities(old_d), entities(new_d)
    if not om and not nm and not oe and not ne:
        return []
    lines = ["  Roster entities (option B):"]
    lines.append(f"    entity-set Jaccard: {jaccard(oe, ne):.3f} "
                 f"({len(oe)} old, {len(ne)} new, "
                 f"{len(oe & ne)} shared)")
    op, np_ = principals(old_d), principals(new_d)
    lines.append(f"    principal entity Jaccard "
                 f"(frequency >= 0.20): {jaccard(op, np_):.3f} "
                 f"({len(op)} old, {len(np_)} new)")
    ot, nt = triples(old_d), triples(new_d)
    lines.append(f"    relationship endpoint-ID Jaccard: "
                 f"{jaccard(ot, nt):.3f} ({len(ot)} old, "
                 f"{len(nt)} new endpoint triples)")
    oc = om.get("canonical_names") or {}
    nc = nm.get("canonical_names") or {}
    churn = sorted(e for e in (set(oc) & set(nc)) if oc[e] != nc[e])
    lines.append(f"    canonical-name churn: {len(churn)} "
                 f"entit(ies) renamed between runs")
    for e in churn[:MAX_LIST]:
        lines.append(f"      - {e}: {oc[e]!r} -> {nc[e]!r}")
    if len(churn) > MAX_LIST:
        lines.append(f"      ... and {len(churn) - MAX_LIST} more")
    lines.append(f"    split suspects: old "
                 f"{len(om.get('suspects') or [])}, new "
                 f"{len(nm.get('suspects') or [])}")
    return lines


def find_labels(old_d, new_d):
    """Locate a labels fixture for this book in the eval/ directory
    next to this script. Matches on work_id first, then on the fixture's
    book string containing both the previews' title and author.
    Returns (labels dict, source name) or None."""
    eval_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "eval")
    if not os.path.isdir(eval_dir):
        return None
    work_ids = {d.get("work_id") for d in (old_d, new_d)} - {None}
    title = norm((old_d.get("title") or new_d.get("title") or ""))
    author = norm((old_d.get("author") or new_d.get("author") or ""))
    for fname in sorted(os.listdir(eval_dir)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(eval_dir, fname),
                      encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "true_pairs" not in data:
            continue
        if data.get("work_id") and data.get("work_id") in work_ids:
            return data, f"eval/{fname}"
        book = norm(data.get("book") or "")
        if title and author and title in book and author in book:
            return data, f"eval/{fname}"
    return None


def compare_previews(old_d, new_d, labels_data=None, labels_source=None):
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
    lines.append("")
    lines += compare_dashboard(old_d, new_d, labels_data, labels_source)
    return lines


def load_preview(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    args = sys.argv[1:]
    labels_path = None
    if "--labels" in args:
        i = args.index("--labels")
        if i + 1 >= len(args):
            print("Usage: py preview_variance.py OLD_JSON NEW_JSON "
                  "[--labels LABELS_JSON]")
            sys.exit(1)
        labels_path = args[i + 1]
        del args[i:i + 2]
    if len(args) != 2:
        print("Usage: py preview_variance.py OLD_JSON NEW_JSON "
              "[--labels LABELS_JSON]")
        sys.exit(1)
    for p in args:
        if not os.path.isfile(p):
            print(f"Not found: {p}")
            sys.exit(1)
    try:
        old_d = load_preview(args[0])
        new_d = load_preview(args[1])
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"ERROR: could not parse preview JSON: {e}")
        sys.exit(2)
    labels_data = labels_source = None
    if labels_path is not None:
        try:
            labels_data = load_preview(labels_path)
            labels_source = labels_path
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"ERROR: could not parse labels JSON: {e}")
            sys.exit(2)
    else:
        found = find_labels(old_d, new_d)
        if found is not None:
            labels_data, labels_source = found
    print(f"Old: {args[0]}")
    print(f"New: {args[1]}")
    print()
    print("\n".join(compare_previews(old_d, new_d,
                                     labels_data, labels_source)))


if __name__ == "__main__":
    main()
