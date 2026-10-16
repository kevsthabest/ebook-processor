#!/usr/bin/env python3
"""Build the human-review fixture from the Trope Review seat files.

Spine design §3.6: the seat's daily recommendations
(~/workspace/trope-review/*.md) are RECOMMENDATIONS (authority
`reviewer`); the verdicts the user actually applied live in
Supabase (authority `kevin`, via the slice-5 read-back shapes).
One fixture entry per (work, claim): the latest seat verdict wins,
every appearance is retained, verdict flips are counted, and a
kevin verdict — when the DB has one for the claim — BEATS the seat
recommendation (the disagreement is recorded, never resolved
away). ABSTAINs are counted and listed but EXCLUDED from the
scored fixture: an abstain is the absence of a verdict, and a
scorer fed absences would grade silence.

Failure classes: the structural classes (genre mismatch,
TOC-as-evidence, wrong-book quote, front/back-matter, empty
evidence) are the headline — they are pipeline failure shapes.
The fine-distinction classes (voluntary-vs-coerced,
backstory-vs-in-book, standoff-vs-captor-captive) are labelled
interpretation-only (OC-8) and reported separately.

Output: eval/review_fixture.json + a printed build report. The
honest n is whatever the seats actually decided — most seat lines
are re-reported holds of the same claims, so the unique scored
CONFIRM/REJECT set is small by construction. The report prints
the real number; nothing here pads it.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

FIXTURE_SCHEMA_VERSION = 1

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CANDIDATE_RE = re.compile(
    r"^(?:-\s*)?(.+?)\s+—\s+(.+?):\s+(CONFIRM|REJECT|ABSTAIN)\b"
    r"\s*(?:—\s*(.*))?$")
_TABLE_RE = re.compile(
    r"^\|\s*(" + _UUID + r")\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|"
    r"\s*[\d.]+\s*\|\s*(CONFIRM|REJECT|ABSTAIN)\s*\|")
_IDLIST_RE = re.compile(
    r"^-\s*(" + _UUID + r")\s+—\s+(.+?)\s+—\s+(.+?)\s+—\s*[\d.]+\s+—"
    r"\s*(CONFIRM|REJECT|ABSTAIN)\b")
_ESCALATION_RE = re.compile(
    r"^-\s*(.+?)\s+—\s+(.+?)\s+\(claim\s+(" + _UUID + r")\)")

STRUCTURAL_CLASSES = ("wrong-book-quote", "genre-mismatch",
                      "toc-as-evidence", "front-back-matter",
                      "empty-evidence")
INTERPRETATION_CLASSES = ("voluntary-vs-coerced",
                          "backstory-vs-in-book",
                          "standoff-vs-captor-captive")
_CLASS_RULES = [
    ("wrong-book-quote",
     ("wrong book", "wrong-book", "different book's quote",
      "from another book")),
    ("genre-mismatch",
     ("non-romance book", "genre mismatch",
      "romance trope on a non-romance")),
    ("toc-as-evidence",
     ("table of contents", "chapter titles", "headings, not",
      "headings rather than", "toc text", "toc-as-evidence")),
    ("front-back-matter",
     ("front matter", "back matter", "front/back matter",
      "copyright page")),
    ("empty-evidence",
     ("empty evidence", "no evidence at all", "tier 3",
      "evidence list is empty")),
    ("voluntary-vs-coerced", ("voluntary", "coerced")),
    ("backstory-vs-in-book", ("backstory", "back-story")),
    ("standoff-vs-captor-captive",
     ("standoff", "captor-captive", "captor/captive",
      "captive / captor")),
]


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def classify(rationale):
    text = (rationale or "").lower()
    for cls, needles in _CLASS_RULES:
        if any(n in text for n in needles):
            return cls
    return "premise-judgment"


def parse_seat_file(path):
    """One seat file -> (appearances, claim_id_links, unresolved).

    appearances: dicts {book, trope, verdict, rationale}.
    claim_id_links: {(book, trope): claim_uuid} from the ID tables,
    ID lists and escalation lines (whichever the file carries).
    unresolved: lines that name a verdict word but parse as nothing
    (counted and shown — a silently dropped verdict is a fixture
    bug, not a small n)."""
    date = path.name[:10]
    appearances = []
    links = {}
    unresolved = []
    for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        m = _TABLE_RE.match(line)
        if m:
            cid, book, trope, verdict = m.groups()
            appearances.append(
                {"book": book, "trope": trope, "verdict": verdict,
                 "rationale": "", "date": date,
                 "source": f"{path.name}:{lineno}"})
            links[(_norm(book), _norm(trope))] = cid
            continue
        m = _IDLIST_RE.match(line)
        if m:
            cid, book, trope, verdict = m.groups()
            appearances.append(
                {"book": book, "trope": trope, "verdict": verdict,
                 "rationale": "", "date": date,
                 "source": f"{path.name}:{lineno}"})
            links[(_norm(book), _norm(trope))] = cid
            continue
        m = _ESCALATION_RE.match(line)
        if m:
            book, trope, cid = m.groups()
            links[(_norm(book), _norm(trope))] = cid
            continue
        m = _CANDIDATE_RE.match(line)
        if m:
            book, trope, verdict, rationale = m.groups()
            appearances.append(
                {"book": book, "trope": trope, "verdict": verdict,
                 "rationale": (rationale or "").strip(),
                 "date": date,
                 "source": f"{path.name}:{lineno}"})
            continue
        m = re.search(r":\s*(CONFIRM|REJECT|ABSTAIN)\b", line)
        if m and "—" in line and not line.startswith("#"):
            # A verdict line's pre-colon segment (after the last
            # em dash) is a trope name: short, no sentence break.
            # Prose that merely mentions ": CONFIRM" mid-sentence
            # is not an unresolved verdict.
            segment = line[:m.start()].rsplit("—", 1)[-1]
            if len(segment) <= 70 and "." not in segment:
                unresolved.append(
                    f"{path.name}:{lineno}: {line[:100]}")
    return appearances, links, unresolved


def _kevin_verdicts():
    """The user's applied verdicts from Supabase, joined to names.
    Returns ({claim_row_id: verdict}, {book_norm: {trope_norm:
    verdict}}, note). Unconfigured/unreachable -> empty + note
    (the fixture then carries reviewer authority only, and the
    build report says so)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "process_portable",
            str(Path(__file__).resolve().parent
                / "process-portable.py"))
        pp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pp)
        pp.load_config()
        if not (pp.CONFIG.get("supabase_url")
                and pp.CONFIG.get("supabase_key")):
            return {}, {}, "Supabase not configured"
        rows = pp.sb(
            "book_trope_claims",
            params="?status=in.(confirmed,rejected)"
                   "&select=id,work_id,trope_id,status&limit=5000")
        if rows is None:
            return {}, {}, "Supabase read failed"
        works = pp.sb("works", params="?select=id,title&limit=5000")
        tropes = pp.sb("tropes", params="?select=id,name&limit=5000")
        if works is None or tropes is None:
            return {}, {}, "Supabase read failed (names)"
        wtitle = {str(w["id"]): w.get("title") or ""
                  for w in works}
        tname = {str(t["id"]): t.get("name") or "" for t in tropes}
        by_claim = {}
        by_pair = {}
        for r in rows:
            verdict = ("CONFIRM" if r.get("status") == "confirmed"
                       else "REJECT")
            if r.get("id"):
                by_claim[str(r["id"])] = verdict
            bn = _norm(wtitle.get(str(r.get("work_id")), ""))
            tn = _norm(tname.get(str(r.get("trope_id")), ""))
            if bn and tn:
                by_pair.setdefault(bn, {})[tn] = verdict
        return by_claim, by_pair, f"{len(by_claim)} curated verdicts"
    except Exception as e:  # noqa: BLE001 — report, never crash
        return {}, {}, f"kevin read-back unavailable ({e})"


def build_fixture(seat_dir):
    seat_dir = Path(seat_dir)
    files = sorted(seat_dir.glob("*.md"))
    appearances = []
    links = {}
    unresolved = []
    for f in files:
        apps, lnk, unr = parse_seat_file(f)
        appearances += apps
        links.update(lnk)
        unresolved += unr
    grouped = {}
    for a in appearances:
        key = (_norm(a["book"]), _norm(a["trope"]))
        grouped.setdefault(key, []).append(a)
    # The claim-ID tables/lists RESTATE the day's candidate lines;
    # an appearance is one day's verdict, not one textual mention.
    # Same (claim, date, verdict) mentions collapse into one
    # appearance carrying the richest rationale of the day.
    for key, apps in grouped.items():
        by_date = {}
        for a in apps:
            by_date.setdefault(a["date"], []).append(a)
        collapsed = []
        for date, day in sorted(by_date.items()):
            if len({a["verdict"] for a in day}) == 1 and len(day) > 1:
                day = [max(day, key=lambda a: len(a["rationale"]))]
            collapsed += day
        grouped[key] = collapsed
    by_claim, by_pair, kevin_note = _kevin_verdicts()
    entries = []
    abstains = []
    for (bn, tn), apps in sorted(grouped.items()):
        apps.sort(key=lambda a: (a["date"], a["source"]))
        latest = apps[-1]
        verdicts = [a["verdict"] for a in apps]
        flips = sum(1 for i in range(1, len(verdicts))
                    if verdicts[i] != verdicts[i - 1])
        claim_ids = sorted({links[k] for k in links
                            if k == (bn, tn)})
        seat_verdict = latest["verdict"]
        kevin_verdict = None
        for cid in claim_ids:
            if cid in by_claim:
                kevin_verdict = by_claim[cid]
        if kevin_verdict is None:
            kevin_verdict = by_pair.get(bn, {}).get(tn)
        verdict = kevin_verdict or seat_verdict
        # Classify on the richest rationale the seat ever wrote for
        # this claim (the latest line may be a bare restatement).
        richest = max(apps, key=lambda a: len(a["rationale"]))
        cls = classify(richest["rationale"])
        entry = {
            "book": latest["book"], "trope": latest["trope"],
            "claim_ids": claim_ids,
            "verdict": verdict,
            "authority": "kevin" if kevin_verdict else "reviewer",
            "seat_verdict": seat_verdict,
            "kevin_verdict": kevin_verdict,
            "disagreement": bool(kevin_verdict
                                 and kevin_verdict != seat_verdict),
            "failure_class": cls,
            "interpretation_only": cls in INTERPRETATION_CLASSES,
            "appearances": apps, "flips": flips,
            "rationale": richest["rationale"],
        }
        if verdict == "ABSTAIN":
            abstains.append(entry)
        else:
            entries.append(entry)
    report = {
        "files": [f.name for f in files],
        "lines_parsed": len(appearances),
        "unique_claims": len(grouped),
        "scored": len(entries),
        "abstains_excluded": len(abstains),
        "flips": sum(e["flips"] for e in entries + abstains),
        "unresolved": unresolved,
        "kevin_note": kevin_note,
        "per_class": {},
        "per_authority": {},
    }
    for e in entries:
        report["per_class"][e["failure_class"]] = \
            report["per_class"].get(e["failure_class"], 0) + 1
        report["per_authority"][e["authority"]] = \
            report["per_authority"].get(e["authority"], 0) + 1
    fixture = {"schema_version": FIXTURE_SCHEMA_VERSION,
               "entries": entries, "abstains": abstains,
               "build_report": report}
    return fixture, report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    seat_dir = Path.home() / "workspace" / "trope-review"
    out = Path(__file__).resolve().parent / "eval" / "review_fixture.json"
    if "--seat-dir" in argv:
        seat_dir = Path(argv[argv.index("--seat-dir") + 1])
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
    fixture, report = build_fixture(seat_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=1, ensure_ascii=False)
    print(f"Review fixture built -> {out}")
    print(f"  seat files: {len(report['files'])} "
          f"({', '.join(report['files'])})")
    print(f"  verdict lines parsed: {report['lines_parsed']}")
    print(f"  unique (work, claim) pairs: {report['unique_claims']}")
    print(f"  SCORED entries (CONFIRM/REJECT): {report['scored']}")
    print(f"  ABSTAINs excluded from scoring: "
          f"{report['abstains_excluded']}")
    print(f"  verdict flips across appearances: {report['flips']}")
    print(f"  kevin authority: {report['kevin_note']}")
    print(f"  per-class: {report['per_class']}")
    print(f"  per-authority: {report['per_authority']}")
    if report["unresolved"]:
        print(f"  UNRESOLVED verdict lines: "
              f"{len(report['unresolved'])}")
        for u in report["unresolved"][:10]:
            print(f"    {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
