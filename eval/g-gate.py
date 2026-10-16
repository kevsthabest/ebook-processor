#!/usr/bin/env python3
"""Option G section 10 gate harness (design: eval/g-evidence-design.md).

Exercises, per the pre-registered gate:

  1. Judge fixture — the labelled passages in g-judge-fixture.json
     run through the PRODUCTION judge path (judge_evidence_batch).
     Reports per-class accuracy and the headline adjacent->supports
     leakage rate (the section 9.1 laundering vector, measured at
     its source).
  2. Planted cross-book false claims — trope claims true of one
     fixture book, asserted for the other and run through the full
     retrieval engine under a deliberately maximally-permissive
     judge (supports everything). Retrieval scope plus span
     verification are the bleed barriers, not the judge, so the
     judge is removed as a variable: every attached passage must
     be a literal span of the TARGET book. Bleed must be 0.
  3. Zero decision-budget proof — _dm_budget_claim is patched to a
     counting raiser around both exercises; the count must stay 0
     (OC-2: G draws zero decision budget).

Usage:
  python3 eval/g-gate.py           live: judge = the local model
                                   and embedder from the pipeline's
                                   own config (process-portable's
                                   CONFIG, loaded at import exactly
                                   as the pipeline loads it)
  python3 eval/g-gate.py --stub    mechanics only: stub judge and
                                   hashed-stem embedder. Proves the
                                   plumbing end to end; the numbers
                                   are NOT a model measurement and
                                   must never be quoted as one.

G4 (replayed-extraction invariance) is PENDING-SPINE: the replay
instrument does not exist yet; see the gate scorecard.
"""
import argparse
import importlib.util
import json
import re
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURE_PATH = HERE / "g-judge-fixture.json"


def load_pp():
    spec = importlib.util.spec_from_file_location(
        "process_portable", ROOT / "process-portable.py")
    pp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pp)
    return pp


def load_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def book_chapters(fx, book):
    return [{"index": i + 1, "label": c["label"], "text": c["text"]}
            for i, c in enumerate(fx["books"][book]["chapters"])]


# --- exercise 1: the judge fixture -------------------------------------

def run_fixture(pp, fx, call):
    jobs, cases = [], []
    for case in fx["cases"]:
        passage = {"quote": case["passage"],
                   "chapter": case["chapter"], "chapter_index": 0,
                   "offset": 0, "score": 1.0}
        jobs.append({"key": case["id"], "name": case["claim"],
                     "definition": fx["claims"][case["claim"]],
                     "passages": [passage]})
        cases.append(case)
    verdicts, failed = pp.judge_evidence_batch(call, jobs)
    classes = ("supports", "adjacent", "contradicts")
    confusion = {e: {g: 0 for g in classes + ("no-verdict",)}
                 for e in classes}
    for case in cases:
        got = (verdicts.get(case["id"]) or {}).get(0, "no-verdict")
        confusion[case["expected"]][got] += 1
    per_class = {}
    for e in classes:
        n = sum(confusion[e].values())
        per_class[e] = {"n": n, "correct": confusion[e][e],
                        "accuracy": (confusion[e][e] / n) if n else 0.0}
    n_adj = per_class["adjacent"]["n"]
    leakage = (confusion["adjacent"]["supports"] / n_adj) if n_adj \
        else 0.0
    return {"confusion": confusion, "per_class": per_class,
            "leakage": leakage, "failed_batches": len(failed),
            "n": len(cases)}


# --- exercise 2: planted cross-book claims ------------------------------

class _SupportAll:
    """Adversarial judge stub: rules EVERY passage 'supports'.
    Bleed must be impossible even so — retrieval only ever sees
    the target book's chapters and every attached span is
    re-verified against them."""

    def __call__(self, prompt, chunk, i, n):
        verdicts = [{"claim": int(a), "passage": int(b),
                     "verdict": "supports"}
                    for a, b in re.findall(
                        r"PASSAGE (\d+)\.(\d+) \(", prompt)]
        return json.dumps({"verdicts": verdicts})


def planted_specs(fx):
    """(source_book, target_book, claim) triples: claims with at
    least one supports case in the source book, asserted for the
    target book over ALL of the target's chapters — plus Dragons,
    true of neither book, asserted for both."""
    supported = {}
    for case in fx["cases"]:
        if case["expected"] == "supports":
            supported.setdefault(case["book"], set()).add(
                case["claim"])
    specs = []
    for src in sorted(supported):
        for tgt in sorted(fx["books"]):
            if tgt == src:
                continue
            labels = [c["label"] for c in fx["books"][tgt]
                      ["chapters"]]
            for claim in sorted(supported[src]):
                specs.append((src, tgt, claim, labels))
    for tgt in sorted(fx["books"]):
        labels = [c["label"] for c in fx["books"][tgt]["chapters"]]
        specs.append(("neither", tgt, "Dragons", labels))
    return specs


def run_bleed(pp, fx, embedder):
    catalog = {name: {"name": name, "description": desc}
               for name, desc in fx["claims"].items()}
    runs, attached, bleed = 0, 0, 0
    bleed_detail = []
    for src, tgt, claim, labels in planted_specs(fx):
        chapters = book_chapters(fx, tgt)
        parts = {"claims": [{"name": claim, "support_refs": labels,
                             "catalog_id": claim, "staged": False}],
                 "catalog": catalog, "summaries": {}}
        out = pp.evidence_retrieve_result(
            parts, chapters, call=_SupportAll(), embedder=embedder)
        runs += 1
        texts = [c["text"] for c in chapters]
        for plist in (out["trope_evidence"] or {}).values():
            for p in plist:
                attached += 1
                home = next(
                    (c for c in chapters
                     if c["label"] == p.get("chapter")), None)
                if home is None or not pp.verify_evidence(
                        p["quote"], home["text"]):
                    bleed += 1
                    bleed_detail.append(
                        f"{claim} ({src}->{tgt}): attached passage "
                        f"not a span of {p.get('chapter')!r}")
                elif not any(p["quote"] in t for t in texts):
                    bleed += 1
                    bleed_detail.append(
                        f"{claim} ({src}->{tgt}): span check "
                        f"inconsistent")
    return {"runs": runs, "attached": attached, "bleed": bleed,
            "detail": bleed_detail}


# --- stubs for --stub mode (mechanics only) ------------------------------

def _stub_call(prompt, chunk, i, n):
    """A small marker stub: depiction markers rule supports, talk
    markers rule adjacent, the rest contradict. Deliberately
    imperfect — its accuracy numbers measure the STUB, never a
    model."""
    rules = [("betrayed the watch", "supports"),
             ("sold the watch rotation", "supports"),
             ("kissed her rival", "supports"),
             ("debated", "adjacent"), ("talked about", "adjacent"),
             ("would never betray", "adjacent")]
    verdicts = []
    for a, b, text in re.findall(
            r"PASSAGE (\d+)\.(\d+) \(from [^)]*\): (.*?)(?=\nPASSAGE "
            r"\d+\.\d+ |\nCLAIM \d+: |\Z)", prompt, re.S):
        v = "contradicts"
        for marker, verdict in rules:
            if marker in text:
                v = verdict
                break
        verdicts.append({"claim": int(a), "passage": int(b),
                         "verdict": v})
    return json.dumps({"verdicts": verdicts})


class _HashEmbedder:
    """Vocabulary-free fake embedder: hashed content stems into
    128 buckets. Deterministic (crc32, never salted hash())."""

    def __call__(self, texts):
        out = []
        for t in texts:
            vec = [0.0] * 128
            pp_tokens = re.findall(r"[a-z]{3,}", (t or "").lower())
            for tok in pp_tokens:
                vec[zlib.crc32(tok.encode()) % 128] += 1.0
            out.append(vec)
        return out


# --- main -----------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stub", action="store_true",
                    help="mechanics only: stub judge + fake embedder "
                         "(NOT a model measurement)")
    args = ap.parse_args(argv)

    pp = load_pp()
    fx = load_fixture()

    budget = {"calls": 0}
    orig_claim = pp._dm_budget_claim

    def _counting_raiser(n=1):
        budget["calls"] += 1
        raise AssertionError("_dm_budget_claim called on the G path")

    pp._dm_budget_claim = _counting_raiser
    try:
        if args.stub:
            call = _stub_call
            embedder = _HashEmbedder()
        else:
            backends = {"ollama": pp.llm_ollama,
                        "openrouter": pp.llm_openrouter,
                        "openai": pp.llm_openai_compat}
            call = backends.get(pp.CONFIG["llm"], pp.llm_ollama)
            embedder = lambda texts: pp.embed_vectors(  # noqa: E731
                pp.CONFIG.get("embed_url", ""),
                pp.CONFIG.get("embed_model", ""), texts)
        fix = run_fixture(pp, fx, call)
        bleed = run_bleed(pp, fx, embedder)
    finally:
        pp._dm_budget_claim = orig_claim

    mode = "STUB (mechanics only — NOT a model measurement)" \
        if args.stub else f"LIVE (judge: {pp.CONFIG['llm']})"
    print(f"# Option G gate exercises — {mode}")
    print()
    print(f"Judge fixture: n={fix['n']} cases, "
          f"failed batches: {fix['failed_batches']}")
    for cls in ("supports", "adjacent", "contradicts"):
        pc = fix["per_class"][cls]
        print(f"  {cls}: {pc['correct']}/{pc['n']} correct "
              f"({pc['accuracy']:.0%})")
    print(f"  HEADLINE adjacent->supports leakage: "
          f"{fix['confusion']['adjacent']['supports']}/"
          f"{fix['per_class']['adjacent']['n']} "
          f"({fix['leakage']:.0%})")
    print(f"  confusion: {json.dumps(fix['confusion'])}")
    print()
    print(f"Planted cross-book claims: {bleed['runs']} runs, "
          f"{bleed['attached']} passages attached under a "
          f"supports-everything judge, BLEED: {bleed['bleed']}")
    for line in bleed["detail"]:
        print(f"  BLEED: {line}")
    print()
    print(f"Decision-budget claims during both exercises: "
          f"{budget['calls']} (must be 0)")
    ok = bleed["bleed"] == 0 and budget["calls"] == 0
    print(f"Gate exercises mechanical verdict: "
          f"{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
