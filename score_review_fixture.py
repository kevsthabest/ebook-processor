#!/usr/bin/env python3
"""Score previews against the human-review fixture.

Spine design §3.6.3, applying the eval/README scoring rules to
trope claims: for each fixture entry, the claim's state in the
book's preview is one of
  shipped  — the trope ships in the preview's trope list;
  staged   — held in the preview's staging marker (it did not
             ship, but the pipeline produced and trapped it);
  dropped  — produced as a candidate but neither shipped nor
             staged (the gate/rejection/staging machinery killed
             it; a preview `no_evidence` record counts as dropped,
             per the README);
  absent   — not a candidate at all (never extracted this run).

Headline (target ZERO, each named): structural-class REJECT
entries that SHIPPED. Also reported: structural-CONFIRM retention
(shipped / scored structural CONFIRMs that had a preview) and the
regenerated-rejected count (REJECT entries the pipeline produced
again in any state but absent). Interpretation-only classes
(OC-8) are scored and reported separately, never in the headline.

Usage: py score_review_fixture.py FIXTURE_JSON PREVIEW_JSON [...]
       [--learn-dir DIR]   (record the score in fixture_scores)
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

STRUCTURAL = ("wrong-book-quote", "genre-mismatch",
              "toc-as-evidence", "front-back-matter",
              "empty-evidence")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def claim_state(entry, preview):
    name = _norm(entry["trope"])
    shipped = {_norm(t.get("name") if isinstance(t, dict) else t)
               for t in preview.get("tropes") or []}
    if name in shipped:
        return "shipped"
    marker = preview.get("staging") or {}
    staged = {_norm(s.get("display") or "")
              for s in marker.get("staged") or []}
    if name in staged:
        return "staged"
    candidates = {_norm(c) for c in
                  (preview.get("trope_candidate_counts") or {})}
    candidates |= {_norm(c.get("name") if isinstance(c, dict) else c)
                   for c in preview.get("trope_candidates") or []}
    if name in candidates:
        return "dropped"
    return "absent"


def score_fixture(fixture, previews):
    """previews: {book_norm: preview dict}. Returns the report."""
    rows = []
    for e in fixture.get("entries") or []:
        preview = previews.get(_norm(e["book"]))
        state = (claim_state(e, preview) if preview is not None
                 else "no-preview")
        rows.append({**e, "state": state})
    structural_reject_shipped = [
        r for r in rows
        if r["failure_class"] in STRUCTURAL
        and r["verdict"] == "REJECT" and r["state"] == "shipped"]
    s_conf = [r for r in rows
              if r["failure_class"] in STRUCTURAL
              and r["verdict"] == "CONFIRM"
              and r["state"] != "no-preview"]
    retention = (
        sum(1 for r in s_conf if r["state"] == "shipped")
        / len(s_conf)) if s_conf else None
    regenerated = [r for r in rows if r["verdict"] == "REJECT"
                   and r["state"] in ("shipped", "staged", "dropped")]
    interp = [r for r in rows if r.get("interpretation_only")]
    return {
        "entries": rows,
        "headline": {
            "structural_reject_shipped": [
                f"{r['book']} — {r['trope']}"
                for r in structural_reject_shipped],
            "structural_reject_shipped_count":
                len(structural_reject_shipped),
            "structural_confirm_retention": retention,
            "structural_confirm_n": len(s_conf),
            "regenerated_rejected_count": len(regenerated),
        },
        "interpretation_only": {
            "n": len(interp),
            "shipped_rejects": [
                f"{r['book']} — {r['trope']}" for r in interp
                if r["verdict"] == "REJECT"
                and r["state"] == "shipped"],
        },
        "by_state": {s: sum(1 for r in rows if r["state"] == s)
                     for s in ("shipped", "staged", "dropped",
                               "absent", "no-preview")},
    }


def _record(report, learn_dir):
    spec = importlib.util.spec_from_file_location(
        "process_portable",
        str(Path(__file__).resolve().parent / "process-portable.py"))
    pp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pp)
    db = pp._state_db_for(learn_dir)
    if db is None:
        print("  (no substrate in --learn-dir; score not recorded)")
        return
    with db.write_txn() as conn:
        conn.execute(
            "INSERT INTO fixture_scores VALUES (NULL,?,?,?,?)",
            (pp.PORTABLE_VERSION, "1", pp._now_iso(),
             json.dumps(report["headline"], ensure_ascii=False)))
    print("  score recorded in fixture_scores")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    learn_dir = None
    if "--learn-dir" in argv:
        i = argv.index("--learn-dir")
        learn_dir = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__)
        return 2
    fixture = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    previews = {}
    for p in argv[1:]:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        previews[_norm(d.get("title"))] = d
    report = score_fixture(fixture, previews)
    h = report["headline"]
    print(f"Review fixture score: {len(report['entries'])} entries, "
          f"states {report['by_state']}")
    print(f"  HEADLINE structural-REJECT shipped: "
          f"{h['structural_reject_shipped_count']} (target 0)")
    for name in h["structural_reject_shipped"]:
        print(f"    SHIPPED REJECT: {name}")
    if h["structural_confirm_retention"] is None:
        print("  structural-CONFIRM retention: n/a "
              f"({h['structural_confirm_n']} structural CONFIRMs "
              "with previews)")
    else:
        print(f"  structural-CONFIRM retention: "
              f"{h['structural_confirm_retention']:.3f} "
              f"(n={h['structural_confirm_n']})")
    print(f"  regenerated-rejected: "
          f"{h['regenerated_rejected_count']}")
    io_ = report["interpretation_only"]
    print(f"  interpretation-only classes (OC-8, reported "
          f"separately): n={io_['n']}, shipped rejects: "
          f"{io_['shipped_rejects']}")
    if learn_dir:
        _record(report, learn_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
