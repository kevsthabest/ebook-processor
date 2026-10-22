"""Score ONE run of a gold book against the frozen gold pack.

    py score-gold.py --gold eval/daemon-labels.json \
                     --run  preview/Daemon...json

The gold file is the machine form of a frozen pack (eval/daemon-labels.json
is the transcription of `daemon-gold-pack-v1-draft(1).md`). It is never
edited mid-iteration: a new pack version is announced, then transcribed.

What is scored, and how:

  relationships   unordered pairs (norm_name on both endpoints); a claim
                  shipped in the reversed direction is compared through
                  _REL_INVERSE. `acceptable` defaults to [type]. recall =
                  must_ship entries shipped acceptably / len(must_ship);
                  precision = correct / labelled, where a labelled claim
                  is one whose pair appears in ANY bucket and a correct
                  one carries an acceptable type for that bucket. Claims
                  on unlabelled pairs are IGNORED (the pack excludes its
                  112 unlabelled observations).
  characters      deterministic: every shipped name (or its last token)
                  must occur in the book text; at most 20 principals; the
                  ruled-away screen name must not exist; ruled-identical
                  identities must not be two entries.
  quotes          every shipped quote must verify against the book text
                  and have its speaker within 300 characters of it.
  tropes          the pack's must-not-ship tropes and the code denylist
                  must not ship; every shipped trope needs 2+ chapters.
  triggers        every category at on_page/graphic needs a surviving
                  evidence quote at or above its decision threshold; no
                  shipped category may have zero chapter indices.

Exit code 1 when precision < target, recall < target, or any
deterministic check fails.
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

_MOD_PATH = SCRIPT_DIR / "process-portable.py"
_spec = importlib.util.spec_from_file_location("process_portable",
                                               str(_MOD_PATH))
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


_SEVERITY_ORDER = {"none": 0, "mentioned": 1, "on_page": 2, "graphic": 3}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm(name):
    """norm_name, plus punctuation the pipeline's own canonical form
    keeps but a name does not carry: "Russell Vanowen, Jr." and the
    pack's "Russell Vanowen Jr." are the same person, and a scorer
    that called them different pairs would be measuring commas."""
    n = pp.norm_name(name or "")
    n = n.replace(",", " ")
    return " ".join(n.split())


def _run_name_map(run):
    """{form: canonical name} from the run's OWN characters.

    A person the gold names by one form can ship under another: this
    run's canonical display name for Sobol is "Matthew A. Sobol", and
    Merritt's is "Agent Roy Merritt". The run's character list IS the
    run's identity, so a gold name resolves through it exactly as the
    pipeline resolves it — an entry's own name always wins, and a
    form two entries claim resolves to nothing (the pipeline's own
    ambiguity rule: a false collapse is worse than an unresolved
    name)."""
    entries = []
    for key in ("characters", "minor_characters"):
        for c in run.get(key) or []:
            if isinstance(c, dict) and c.get("name"):
                entries.append(c)
    owners = {}
    for i, c in enumerate(entries):
        forms = {_norm(c["name"])}
        for a in c.get("aliases") or []:
            if isinstance(a, str):
                forms.add(_norm(a))
        forms.discard("")
        for f in forms:
            owners.setdefault(f, set()).add(i)
    by_name = {_norm(c["name"]): i for i, c in enumerate(entries)}

    def canon(name):
        n = _norm(name)
        if not n:
            return n
        if n in by_name:
            return n
        ids = owners.get(n) or set()
        if len(ids) == 1:
            return _norm(entries[next(iter(ids))]["name"])
        return n
    return canon


def _pair_key(a, b, canon=None):
    if canon is not None:
        a, b = canon(a), canon(b)
    return tuple(sorted((_norm(a), _norm(b))))


def _form_re(form):
    return re.compile(r"(?<!\w)" + re.escape(form) + r"(?!\w)")


def _form_in(form, haystack):
    return bool(form) and bool(_form_re(form).search(haystack))


def _acceptable(entry):
    acc = entry.get("acceptable")
    if acc is None:
        return [entry.get("type", "")]
    return list(acc)


def _shipped_type(entry, claim):
    """The claim's type as it reads in the gold entry's direction."""
    s_from, s_to = _norm(claim.get("from")), _norm(claim.get("to"))
    e_from, e_to = _norm(entry.get("from")), _norm(entry.get("to"))
    t = (claim.get("type") or "").lower()
    if s_from == e_to and s_to == e_from and s_from != s_to:
        return pp._REL_INVERSE.get(t, t)
    return t


def _buckets(gold):
    return (("must_ship", gold.get("must_ship") or []),
            ("must_not_ship", gold.get("must_not_ship") or []),
            ("either", gold.get("either") or []))


# ---------------------------------------------------------------------------
# Relationship scoring
# ---------------------------------------------------------------------------
# Notes that mean "the write path deletes this row". A preview keeps
# them so the rejection is reviewable; the product ships neither.
_NOT_SHIPPED_NOTES = ("would_drop", "would_downgrade")


def shipped_relationships(run):
    """The run's relationship rows a WRITE would keep.

    A preview records a rejected claim as `would_drop` and leaves it in
    the row set for audit; the write path deletes it. Scoring the audit
    row would measure a claim that never reaches storage. (A pair whose
    winner dies is revived from its runner-up at write time, and that
    claim is not in the preview's rows — so recall measured this way is
    a floor, not a ceiling.)"""
    out = []
    for r in run.get("relationships") or []:
        if not isinstance(r, dict) or not r.get("from") or not r.get("to"):
            continue
        if (r.get("validation_note") or "") in _NOT_SHIPPED_NOTES:
            continue
        out.append(r)
    return out


def score_relationships(gold, run):
    rels = shipped_relationships(run)
    excluded = sum(1 for r in (run.get("relationships") or [])
                   if isinstance(r, dict)
                   and (r.get("validation_note") or "")
                   in _NOT_SHIPPED_NOTES
                   and r.get("from") and r.get("to"))
    canon = _run_name_map(run)
    by_key = {}
    for rel in rels:
        by_key.setdefault(
            _pair_key(rel["from"], rel["to"], canon), []).append(rel)

    must_ship = gold.get("must_ship") or []
    hits, misses = [], []
    for entry in must_ship:
        key = _pair_key(entry["from"], entry["to"], canon)
        ok = False
        for claim in by_key.get(key, ()):
            if _shipped_type(entry, claim) in _acceptable(entry):
                ok = True
                break
        (hits if ok else misses).append(
            f"{entry['from']} -> {entry['to']} [{entry['type']}]")
    recall = (len(hits) / len(must_ship)) if must_ship else 0.0

    entry_by_key = {}
    for _bucket, entries in _buckets(gold):
        for entry in entries:
            entry_by_key.setdefault(
                _pair_key(entry["from"], entry["to"], canon),
                []).append(entry)

    labelled, correct, wrong, offenders = 0, 0, 0, []
    for key, claims in by_key.items():
        entries = entry_by_key.get(key)
        if not entries:
            continue  # unlabelled pair: not part of the pack's score
        for claim in claims:
            labelled += 1
            if any(_shipped_type(e, claim) in _acceptable(e)
                   for e in entries):
                correct += 1
            else:
                wrong += 1
                offenders.append(
                    "%s -> %s [%s]" % (claim.get("from"), claim.get("to"),
                                       claim.get("type")))
    precision = (correct / labelled) if labelled else None
    return {
        "precision": precision,
        "recall": recall,
        "labelled": labelled,
        "correct": correct,
        "wrong": wrong,
        "must_ship_total": len(must_ship),
        "must_ship_hits": len(hits),
        "misses": misses,
        "wrong_claims": offenders,
        "excluded_would_drop": excluded,
    }


# ---------------------------------------------------------------------------
# Characters
# ---------------------------------------------------------------------------
def _character_entries(run):
    out = []
    for c in run.get("characters") or []:
        if isinstance(c, dict) and c.get("name"):
            out.append(c)
        elif isinstance(c, str) and c.strip():
            out.append({"name": c, "aliases": []})
    return out


def _covers(entry, form):
    forms = {_norm(entry.get("name"))}
    for a in entry.get("aliases") or []:
        if isinstance(a, str):
            forms.add(_norm(a))
    forms.discard("")
    if form in forms:
        return True
    return bool(re.search(r"(?<!\w)" + re.escape(form) + r"(?!\w)",
                          _norm(entry.get("name"))))


def _identity_entries(chars, form):
    """Entries that name `form` (as a name or an alias), by index."""
    key = _norm(form)
    return {i for i, c in enumerate(chars) if _covers(c, key)}


def score_characters(gold, run, book_text, max_principals=None):
    if max_principals is None:
        max_principals = int(gold.get("principal_cap") or 20)
    chars = _character_entries(run)
    text_norm = _norm(book_text or "")
    missed = []
    for c in chars:
        name = c.get("name") or ""
        form = _norm(name)
        last = form.split()[-1] if form.split() else ""
        if not (_form_in(form, text_norm) or _form_in(last, text_norm)):
            missed.append(name)
    banned_form = _norm("Major Pain in the Ass")
    banned = [c["name"] for c in chars
              if banned_form in _norm(c.get("name"))]
    splits = []
    for a, b in (("Loki", "Brian Gragg"), ("Karl Devon", "The Major"),
                 ("Karl Devon", "Major")):
        ia, ib = _identity_entries(chars, a), _identity_entries(chars, b)
        if ia and ib and not (ia & ib):
            splits.append(f"{a} / {b}")
    return [
        {"name": "character grounding", "ok": not missed,
         "value": "%d/%d names in text" % (len(chars) - len(missed),
                                           len(chars)),
         "offenders": missed},
        {"name": "character count", "ok": len(chars) <= max_principals,
         "value": len(chars), "offenders": []},
        {"name": "ruled-away name absent", "ok": not banned,
         "value": "Major Pain in the Ass" if banned else "absent",
         "offenders": banned},
        {"name": "identity rulings", "ok": not splits,
         "value": "one entry each" if not splits else "split",
         "offenders": splits},
    ]


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
def _chapter_texts(chapters):
    """{label: text} plus the joined book text."""
    by_label = {}
    for c in chapters or []:
        label = c.get("label") or ""
        by_label.setdefault(label, c.get("text") or "")
    return by_label


def _find_quote_text(quote, by_label, book_text):
    label = quote.get("chapter") or ""
    text = by_label.get(label)
    if text:
        return text
    # Label mismatch (re-acquisition can relabel a volume): the quote
    # itself is the locator.
    qtext = (quote.get("text") or "").strip()
    if qtext and qtext[:80] in (book_text or ""):
        return book_text
    return None


def _speaker_forms(speaker):
    form = _norm(speaker or "")
    forms = {form}
    toks = form.split()
    if toks:
        forms.add(toks[-1])
    return {f for f in forms if f}


def score_quotes(gold, run, chapters, min_shipped=4):
    quotes = [q for q in (run.get("quotes") or [])
              if isinstance(q, dict) and q.get("text")]
    by_label = _chapter_texts(chapters)
    book_text = "\n".join(c.get("text") or "" for c in chapters or [])
    unverified, ungrounded = [], []
    for q in quotes:
        text = q.get("text") or ""
        chapter_text = _find_quote_text(q, by_label, book_text)
        if not chapter_text or not pp.verify_evidence(text, chapter_text):
            unverified.append(text[:80])
            continue
        ctx = pp._extract_quote_context(chapter_text, text, window=300)
        hay = _norm(text + " " + ctx)
        if not any(_form_in(f, hay) for f in _speaker_forms(q.get("speaker"))):
            ungrounded.append("%s (%s)" % (text[:60], q.get("speaker")))
    n = len(quotes)
    verified_rate = ((n - len(unverified)) / n) if n else 0.0
    speaker_rate = (1.0 - (len(ungrounded) / n)) if n else 0.0
    return [
        {"name": "quote verification", "ok": n and not unverified,
         "value": "%.2f of %d" % (verified_rate, n),
         "offenders": unverified},
        {"name": "quote speaker grounding",
         "ok": n and not ungrounded,
         "value": "%.2f of %d" % (speaker_rate, n),
         "offenders": ungrounded},
        {"name": "quote count", "ok": n >= min_shipped,
         "value": n, "offenders": []},
    ]


# ---------------------------------------------------------------------------
# Tropes
# ---------------------------------------------------------------------------
def score_tropes(gold, run):
    tropes = [t.get("name") if isinstance(t, dict) else t
              for t in (run.get("tropes") or [])]
    tropes = [t for t in tropes if t]
    support = run.get("trope_chapters") or {}
    banned = [t for t in (gold.get("trope_rulings") or {})
              .get("must_not_ship") or []]
    shipped_banned = [t for t in tropes
                      if pp._tnorm(t) in {pp._tnorm(b) for b in banned}]
    shipped_denied = [t for t in tropes if pp._is_denylisted_trope(t)]
    thin = []
    for t in tropes:
        chapters = support.get(pp._tnorm(t)) or []
        if len(chapters) < 2:
            thin.append("%s (%d ch)" % (t, len(chapters)))
    return [
        {"name": "trope rulings", "ok": not shipped_banned,
         "value": "none shipped" if not shipped_banned else "violated",
         "offenders": shipped_banned},
        {"name": "trope denylist", "ok": not shipped_denied,
         "value": "none shipped" if not shipped_denied else "violated",
         "offenders": shipped_denied},
        {"name": "trope chapter support", "ok": not thin,
         "value": "%d tropes" % len(tropes), "offenders": thin},
    ]


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------
def score_triggers(gold, run):
    triggers = [t for t in (run.get("triggers") or [])
                if isinstance(t, dict) and t.get("warning")]
    unsupported, unsited = [], []
    for t in triggers:
        raw = t.get("severity", "none")
        sev = raw if isinstance(raw, int) else _SEVERITY_ORDER.get(
            str(raw).lower(), 0)
        if sev >= 2:
            hi = pp._DECISION_TRIGGER_THRESHOLDS_HI.get(t["warning"], 0.5)
            ok = any(isinstance(e, dict)
                     and isinstance(e.get("decision_p"), (int, float))
                     and e["decision_p"] >= hi
                     for e in (t.get("evidence") or []))
            if not ok:
                unsupported.append(t["warning"])
        if not (t.get("chapter_indices") or []):
            unsited.append(t["warning"])
    return [
        {"name": "trigger evidence", "ok": not unsupported,
         "value": "%d triggers" % len(triggers),
         "offenders": unsupported},
        {"name": "trigger chapter indices", "ok": not unsited,
         "value": "%d triggers" % len(triggers), "offenders": unsited},
    ]


# ---------------------------------------------------------------------------
# Whole run
# ---------------------------------------------------------------------------
def _resolve_epub(run, override=None):
    if override:
        p = Path(override)
        return p if p.is_file() else None
    name = run.get("file") or ""
    if not name:
        return None
    for cand in (Path(name), SCRIPT_DIR / "import" / Path(name).name,
                 SCRIPT_DIR / Path(name).name):
        if cand.is_file():
            return cand
    return None


def score_run(gold, run, epub=None, chapters=None):
    """Score one run document. Returns the report dict."""
    if chapters is None:
        path = _resolve_epub(run, epub)
        if path is None:
            raise SystemExit("cannot locate the book file for "
                             "deterministic checks (pass --epub)")
        acq = pp._acquire_chapters(path)
        if acq is None:
            raise SystemExit(f"cannot read {path}")
        chapters = acq["chapters"]
    book_text = "\n".join(c.get("text") or "" for c in chapters)
    rel = score_relationships(gold, run)
    checks = (score_characters(gold, run, book_text)
              + score_quotes(gold, run, chapters)
              + score_tropes(gold, run)
              + score_triggers(gold, run))
    target = gold.get("target") or {}
    p_target = float(target.get("precision", 0.90))
    r_target = float(target.get("recall", 0.70))
    rel_ok = (rel["precision"] is not None
              and rel["precision"] >= p_target
              and rel["recall"] >= r_target)
    return {
        "relationships": rel,
        "checks": checks,
        "precision_target": p_target,
        "recall_target": r_target,
        "relationships_ok": rel_ok,
        "ok": rel_ok and all(c["ok"] for c in checks),
    }


def _print_report(report):
    rel = report["relationships"]
    prec = ("%.3f" % rel["precision"]) if rel["precision"] is not None \
        else "n/a (0 labelled)"
    print("%s relationship precision %s (target %.2f; %d/%d labelled "
          "correct)" % ("PASS" if rel["precision"] is not None
                        and rel["precision"] >= report["precision_target"]
                        else "FAIL", prec, report["precision_target"],
                        rel["correct"], rel["labelled"]))
    for w in rel["wrong_claims"]:
        print("      wrong: %s" % w)
    print("%s relationship recall %s (target %.2f; %d/%d must_ship)" % (
        "PASS" if rel["recall"] >= report["recall_target"] else "FAIL",
        "%.3f" % rel["recall"], report["recall_target"],
        rel["must_ship_hits"], rel["must_ship_total"]))
    for m in rel["misses"]:
        print("      missing: %s" % m)
    if rel.get("excluded_would_drop"):
        print("      (%d preview row(s) the write path deletes were "
              "not scored; write-mode revival can still recover a pair)"
              % rel["excluded_would_drop"])
    for c in report["checks"]:
        print("%s %s %s" % ("PASS" if c["ok"] else "FAIL",
                            c["name"], c["value"]))
        for o in c["offenders"][:20]:
            print("      %s" % o)
    print("OVERALL %s" % ("PASS" if report["ok"] else "FAIL"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Score one run against the frozen gold pack.")
    ap.add_argument("--gold", required=True,
                    help="gold labels JSON (eval/daemon-labels.json)")
    ap.add_argument("--run", required=True,
                    help="preview JSON or analysis.json of the run to score")
    ap.add_argument("--epub", default=None,
                    help="override the book file (defaults to the run's "
                         "'file' entry, resolved beside the repo)")
    args = ap.parse_args(argv)
    gold = json.load(open(args.gold, encoding="utf-8"))
    run = json.load(open(args.run, encoding="utf-8"))
    report = score_run(gold, run, epub=args.epub)
    _print_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
