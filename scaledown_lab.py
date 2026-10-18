#!/usr/bin/env python3
"""Experimental evaluation harness for the ScaleDown model family.

This is a lab tool, not part of the shipped pipeline. Nothing in
process-portable.py or portal.py imports it, it carries no version
of its own, and its results never write anywhere near production
data — every run lands in ./scaledown-lab-out/ as a JSON results
file plus a short Markdown summary for a human to read.

It exists to answer three questions with the pipeline's own
fixtures and thresholds, so the answers compare against the
incumbents instead of against vendor benchmarks:

  classify   scaledown/classify-1 vs the incumbent decision model
             (typesafe/jev-1.13) on the relationship eval cases in
             eval/relationship_cases.json, scored with the same
             four metrics the relationship eval harness reports.
  extract    scaledown/extract vs the pipeline's chapter
             extraction, as a throughput and output-shape
             experiment over one real chapter.
  summarize  scaledown/summarize vs the pipeline's chapter
             summaries, side by side over the same chapter.

Usage:
    python3 scaledown_lab.py classify [--dry-run]
    python3 scaledown_lab.py extract --epub book.epub [--dry-run]
    python3 scaledown_lab.py extract --text chapter.txt \\
        --preview-json preview/<book>.json
    python3 scaledown_lab.py summarize --epub book.epub --chapter 2

--dry-run prints the exact request payloads and sends nothing.

The OpenRouter key is resolved exactly the way the shipped
processor resolves its decision-model key: "openrouter_api_key"
in config.json, then "openrouter_key", then the
OPENROUTER_API_KEY environment variable.
The key is never printed, logged, or written to an output file.

Stdlib only, Python 3.8+, Windows and Linux: the User runs this
from the repo root on his own machine, where his key, his EPUBs
and his corpus live.
"""
import argparse
import json
import math
import posixpath
import statistics
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

# ---------------------------------------------------------------------------
# Model ids and endpoints (verified against the live OpenRouter
# catalog on 2026-10-17).
#
# Extract and Summarize are ordinary chat-completions models and
# appear in GET /api/v1/models. Classify-1 does NOT appear there:
# it is served through OpenRouter's Decisions API (its output
# modality is "decisions", not text), and its catalog entry lives
# at /api/v1/models/scaledown/classify-1/endpoints instead. Anyone
# concluding "classify does not exist" from the models list alone
# has been fooled by that split — check the endpoints route.
# ---------------------------------------------------------------------------
CLASSIFY_MODEL = "scaledown/classify-1"
EXTRACT_MODEL = "scaledown/extract"
SUMMARIZE_MODEL = "scaledown/summarize"
INCUMBENT_MODEL = "typesafe/jev-1.13"

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
# The documented Decisions API route. The shipped processor pins
# its own systemone route for the incumbent; --endpoint can point
# the lab there instead if a run needs the comparison on one wire.
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
SYSTEMONE_URL = "https://openrouter.ai/api/v1/systemone"

# Catalog prices, USD per token, verified 2026-10-17: all three
# ScaleDown models bill prompt tokens at $0.04 per million and
# completion tokens at $0 (decisions and span extractions emit no
# billable prose; the list price shows $0.05 with a 20% promotional
# discount, and the endpoints route reports the discounted rate —
# plan on the discounted number, treat the discount as revocable).
PRICE_PROMPT_PER_TOKEN = 0.00000004
PRICE_COMPLETION_PER_TOKEN = 0.0

# Scoring mirrors production, deliberately: the lab's numbers are
# only worth anything if a band here means the same thing as a
# band in v2_reduce. evaluate-relationships.py is the definition
# being mirrored (thresholds, draw counts, metric formulas); when
# process-portable.py imports cleanly, its live constants win over
# these fallbacks so a threshold retune cannot silently desync
# the comparison.
KEEP_THRESHOLD = 0.70
DROP_THRESHOLD = 0.30
FAMILY_TYPES = frozenset(
    {"spouse", "parent", "child", "sibling", "step_parent", "step_child"})
DRAWS_BASE = 3
DRAWS_NEAR_EDGE = 5
EDGE_MARGIN = 0.15

# The abstain class in the classify choice question. The vendor's
# own guidance is that this model family always returns a winner,
# so a catch-all option is not optional in practice: without it,
# unsupported claims would be forced onto the least-wrong type
# and the distribution would look decisive about junk.
NONE_OPTION = "none"

DEFAULT_OUT_DIR = "scaledown-lab-out"
DEFAULT_MAX_TOKENS = 1000000
EXCERPT_CHARS = 200


class LabError(Exception):
    """A user-facing failure: message is safe to print as-is."""


# The lab opens EPUBs the operator points it at, and those files get
# the same suspicion as the processor's import folder: a zip entry
# can decompress far beyond its compressed size, so every entry read
# below is capped per entry class, using the processor's numbers
# (container 1 MiB, OPF 5 MiB, spine documents 32 MiB — documents get
# the headroom because a legitimate book may carry a whole novel in
# a single XHTML file). An over-cap entry raises LabError instead of
# being truncated or skipped, because chapters measured from partial
# text would silently corrupt the experiment's numbers.
EPUB_CONTAINER_MAX_BYTES = 1 << 20
EPUB_OPF_MAX_BYTES = 5 << 20
EPUB_DOCUMENT_MAX_BYTES = 32 << 20


def _read_zip_entry_capped(zf, name, cap):
    """Read one zip entry as bytes, enforcing `cap` twice: the
    declared-size check rejects an honestly labelled bomb before any
    inflation, and the streamed read is capped as well because a
    header can declare a small size while its stream yields more.
    Reading stops one byte past the cap — enough to know the entry
    is over without materialising the rest of it."""
    info = zf.getinfo(name)
    if info.file_size > cap:
        raise LabError(
            f"EPUB entry {name} declares {info.file_size} bytes, over "
            f"the {cap}-byte cap for its entry class; refusing the book")
    chunks = []
    total = 0
    with zf.open(name) as fh:
        while True:
            chunk = fh.read(min(1 << 16, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                raise LabError(
                    f"EPUB entry {name} decompressed past the "
                    f"{cap}-byte cap for its entry class despite "
                    f"declaring {info.file_size} bytes; refusing the book")
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Small shared helpers.
# ---------------------------------------------------------------------------

def estimate_tokens(text):
    """Rough input-token estimate for guardrails and pre-flight
    cost printouts: ~4 characters per token for English prose.
    Deliberately crude — actuals come from the response's usage
    block whenever the API reports them, and every estimate the
    lab prints is labelled as one."""
    return max(1, math.ceil(len(text) / 4))


def estimate_cost(prompt_tokens, completion_tokens=0):
    return (prompt_tokens * PRICE_PROMPT_PER_TOKEN
            + completion_tokens * PRICE_COMPLETION_PER_TOKEN)


def resolve_api_key(config_path):
    """The processor's decision-model key resolution, mirrored
    (process-portable.py's resolve_decision_model): config.json's
    openrouter_api_key, then its openrouter_key, then the
    OPENROUTER_API_KEY environment variable. Returns "" when no
    key is available. The value is only ever placed in an
    Authorization header."""
    import os
    if config_path:
        try:
            cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        for field in ("openrouter_api_key", "openrouter_key"):
            value = cfg.get(field) or ""
            if value:
                return value
    return os.environ.get("OPENROUTER_API_KEY", "")


def http_post_json(url, payload, api_key, timeout=120):
    """One JSON POST. Raises LabError with a safe one-line message
    on any failure — response bodies can echo request material, so
    errors carry the HTTP status and nothing else."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "X-Title": "ebook-processor-scaledown-lab"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LabError(f"HTTP {e.code} from the API endpoint")
    except (urllib.error.URLError, OSError) as e:
        raise LabError(f"network error calling the API endpoint: "
                       f"{e.__class__.__name__}")
    except ValueError:
        raise LabError("API endpoint returned a non-JSON response")


def http_get_json(url, timeout=30):
    """One JSON GET (the public catalog needs no key)."""
    req = urllib.request.Request(url, headers={"User-Agent": "scaledown-lab"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def fetch_model_info(model_id):
    """Live catalog entry for a model id, or None when unreachable.

    Chat models are found in the /models list; Classify-1 is not
    in that list (see the header note), so the per-model endpoints
    route is tried as well. Best-effort pre-flight only: a failed
    lookup never blocks a run, it just leaves the stamped prices
    in force."""
    data = http_get_json(MODELS_URL)
    if isinstance(data, dict):
        for entry in data.get("data") or []:
            if entry.get("id") == model_id:
                return entry
    detail = http_get_json(f"{MODELS_URL}/{model_id}/endpoints")
    if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
        return detail["data"]
    return None


def catalog_price(model_info):
    """(prompt, completion) USD-per-token from a catalog entry, or
    None when the entry carries no usable pricing — callers fall
    back to the stamped constants."""
    if not isinstance(model_info, dict):
        return None
    pricing = model_info.get("pricing")
    if not pricing and model_info.get("endpoints"):
        pricing = (model_info["endpoints"][0] or {}).get("pricing")
    if not isinstance(pricing, dict):
        return None
    try:
        return float(pricing["prompt"]), float(pricing["completion"])
    except (KeyError, TypeError, ValueError):
        return None


class TokenBudget:
    """The --max-tokens guardrail: a hard ceiling on input tokens
    for one experiment run. Estimates gate the plan before any
    call is made; actuals (or estimates, when the API reports no
    usage) are charged per call, and a charge that would cross
    the ceiling stops the run instead of overspending quietly."""

    def __init__(self, limit):
        self.limit = limit
        self.spent = 0

    def charge(self, tokens):
        self.spent += tokens
        return self.spent <= self.limit

    @property
    def exceeded(self):
        return self.spent > self.limit


def excerpt(text, limit=EXCERPT_CHARS):
    """A whitespace-collapsed excerpt capped for output files:
    lab outputs never carry full chapter text, whatever rides in."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit - 1] + "…"


def write_outputs(out_dir, kind, results, md_text):
    """Write one run's JSON + Markdown pair and return the paths.
    The timestamp keeps repeat runs side by side instead of
    overwriting the run a human is mid-way through reading."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out / f"{kind}-{stamp}.json"
    md_path = out / f"{kind}-{stamp}.md"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    md_path.write_text(md_text, encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# Scoring — the relationship eval harness's definitions, mirrored.
#
# These functions intentionally duplicate evaluate-relationships.py
# rather than importing it: the harness is a script with its own
# main(), and the lab must be able to run (and be unit-tested)
# without dragging a second harness's argument handling along.
# If the harness's definitions change, change these in step and
# say so in the commit — the comparison is the product.
# ---------------------------------------------------------------------------

def band(p, rel_type, scoring):
    """The production outcome for one score, by claim family.
    Family claims are three-tier (keep at the validation
    threshold, middle band below it, drop under the drop
    threshold); non-family claims are two-tier — at or above the
    drop threshold the claim keeps its type, below it drops."""
    if p is None:
        return "unanswered"
    if rel_type in scoring["family"]:
        if p >= scoring["keep"]:
            return "keep"
        if p < scoring["drop"]:
            return "drop"
        return "middle"
    return "keep" if p >= scoring["drop"] else "drop"


def band_edges(rel_type, scoring):
    """The score boundaries a claim of this family can straddle."""
    if rel_type in scoring["family"]:
        return (scoring["drop"], scoring["keep"])
    return (scoring["drop"],)


def draw_count(first_p, rel_type, scoring):
    """How many draws a case gets, decided by its first score:
    three, or five when the first draw lands within the edge
    margin of a band boundary — a single draw near an edge is a
    coin flip, and the extra draws belong where flips live. The
    epsilon keeps "within 0.15" honest at decimal boundaries
    (0.85 - 0.70 is 0.15000000000000008 in binary floating point)."""
    if first_p is None:
        return DRAWS_BASE
    if any(abs(first_p - edge) <= EDGE_MARGIN + 1e-9
           for edge in band_edges(rel_type, scoring)):
        return DRAWS_NEAR_EDGE
    return DRAWS_BASE


def case_outcome(answers, rel_type, scoring):
    """One case's verdict from its answered draws: the band every
    draw agrees on. Disagreement means the draws straddle an
    edge, and the honest verdict is "unstable" rather than one
    draw picking a side. Returns (outcome, median, spread)."""
    if not answers:
        return "unanswered", None, None
    bands = {band(p, rel_type, scoring) for p in answers}
    median = statistics.median(answers)
    spread = max(answers) - min(answers)
    if len(bands) > 1:
        return "unstable", median, spread
    return bands.pop(), median, spread


def compute_metrics(rows):
    """The four headline metrics over gated cases, as (numerator,
    denominator) pairs so the counts behind a rate always show:
    a precision of 1.00 over two cases and over two hundred must
    not read the same. Middle, unstable, and unanswered cases
    abstain — they leave the precision/recall/type denominators
    and count in the abstention rate, so a model cannot look
    precise by refusing the hard cases."""
    pred_keep = sum(1 for r in rows if r["outcome"] == "keep")
    exp_keep = sum(1 for r in rows if r["expect"] == "keep")
    true_keep = sum(1 for r in rows if r["expect"] == "keep"
                    and r["outcome"] == "keep")
    decided = sum(1 for r in rows if r["outcome"] in ("keep", "drop"))
    exact = sum(1 for r in rows if r["outcome"] == r["expect"]
                and r["outcome"] in ("keep", "drop"))
    abstained = sum(1 for r in rows if r["outcome"] in
                    ("middle", "unstable", "unanswered"))
    return {
        "cases": len(rows),
        "precision": (true_keep, pred_keep),
        "recall": (true_keep, exp_keep),
        "type_accuracy": (exact, decided),
        "abstention": (abstained, len(rows)),
    }


def fmt_metric(label, pair):
    num, den = pair
    if den == 0:
        return f"{label} n/a ({num}/{den})"
    return f"{label} {num / den:.2f} ({num}/{den})"


def metric_pair_json(pair):
    num, den = pair
    return {"value": (num / den) if den else None, "num": num, "den": den}


def production_scoring():
    """The live production constants when process-portable.py
    imports cleanly (thresholds, family set, per-type glosses);
    the stamped fallbacks above when it does not. The glosses
    matter: production judges a registry type on its definition,
    so the lab's rubrics carry the same definitions rather than
    letting the model guess what a type name means."""
    scoring = {"keep": KEEP_THRESHOLD, "drop": DROP_THRESHOLD,
               "family": FAMILY_TYPES, "glosses": {}}
    try:
        import importlib.util
        path = Path(__file__).resolve().parent / "process-portable.py"
        spec = importlib.util.spec_from_file_location(
            "process_portable_for_lab", str(path))
        pp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pp)
        scoring["keep"] = pp._REL_VALIDATION_THRESHOLD
        scoring["drop"] = pp._REL_DROP_THRESHOLD
        scoring["family"] = frozenset(pp._REL_VOCAB["family"])
        for entry in pp._BUILTIN_REL_TYPES:
            if entry.get("gloss"):
                scoring["glosses"][entry["id"]] = entry["gloss"]
    except Exception:
        pass
    return scoring


# ---------------------------------------------------------------------------
# E1 — classify: ScaleDown Classify-1 vs the incumbent decision
# model on the relationship eval fixtures.
#
# The task is shaped the way Classify is documented to work: one
# choice question whose options are the candidate relationship
# types, each carrying a rubric (the type's gloss, when production
# has one, plus what would make the quote support it for THIS
# pair), plus the explicit "none" catch-all. One probability —
# the mass on the claimed type — is then banded with the exact
# thresholds the incumbent's scores are banded with, so the four
# metrics compare like with like. What differs from the incumbent
# is the question form (choice over types vs a yes/no on the one
# proposed type), which is precisely the experiment.
# ---------------------------------------------------------------------------

def candidate_types(cases):
    """The label set for the choice question: every type the
    fixture set claims, in a stable order, with the catch-all
    last. A production version of this seat would offer the full
    registry vocabulary; the eval can only gold-label the types
    its fixtures use, so the label set is the fixtures' universe
    — noted in every report so nobody over-reads the result."""
    seen = []
    for case in cases:
        if case["type"] not in seen:
            seen.append(case["type"])
    return seen


def _state_for_case(case):
    """The incumbent's state assembly, mirrored: the quote alone,
    or the evidence window in front of it when the case carries
    one. (The shipped fixtures carry quotes only; their ±2
    windows are computed from the corpus by the eval harness, so
    a lab run without corpus access is quote-only and says so.)"""
    ctx = case.get("context") or case.get("_context") or case.get("window")
    if ctx:
        return f"Context:\n{ctx}\n\nQuote:\n{case['quote']}"
    return f"Quote:\n{case['quote']}"


def build_classify_payload(case, types, scoring):
    """One Decisions API request for one claim. Pure function of
    the case, so --dry-run prints exactly what a live run sends
    and the tests can pin the shape without a network."""
    from_name, to_name = case["from"], case["to"]
    criteria = {}
    for rel_type in types:
        rubric = (f"The quote directly states or clearly implies "
                  f"that {from_name} is the {rel_type} of {to_name}.")
        gloss = (scoring.get("glosses") or {}).get(rel_type)
        if gloss:
            rubric = f"Here, \"{rel_type}\" means: {gloss} " + rubric
        criteria[rel_type] = rubric
    criteria[NONE_OPTION] = (
        f"The quote does not state or clearly imply any of the "
        f"listed relationships between {from_name} and {to_name}: "
        f"it may describe a different relationship, bind a "
        f"relative to a third person, or support no relationship "
        f"claim about this pair at all.")
    instructions = (
        f"Which relationship, if any, does the quote establish "
        f"between {from_name} and {to_name}? Choose the single "
        f"option the quote best supports. A person may be referred "
        f"to by full name, first name, surname alone, or a pronoun "
        f"the quote makes clear; judge the construction, not the "
        f"name form.")
    return {
        "model": CLASSIFY_MODEL,
        "state": _state_for_case(case),
        "questions": {
            "rel_type": {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        },
    }


def parse_choice_answer(answer):
    """Read one choice answer. Returns (distribution, picked):
    distribution is {option: probability} when the answer carries
    per-option probabilities, else None — a bare pick with no
    probability for the claimed type cannot be banded, and the
    lab records the draw as unanswered rather than inventing a
    distribution around a confidence number."""
    if not isinstance(answer, dict):
        return None, None
    probs = None
    for key in ("probabilities", "distribution", "scores"):
        raw = answer.get(key)
        if isinstance(raw, dict) and raw:
            probs = raw
            break
    picked = None
    for key in ("choice", "label", "top_label", "answer"):
        if isinstance(answer.get(key), str):
            picked = answer[key]
            break
    if probs is None:
        return None, picked
    dist = {}
    for option, value in probs.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isnan(value) or math.isinf(value):
            continue
        dist[option] = float(value)
    if not dist:
        return None, picked
    if picked is None:
        picked = max(dist, key=dist.get)
    return dist, picked


def run_classify(cases, post_json, api_key, endpoint, scoring, budget):
    """Run the draws for every case. post_json is the transport
    (url, payload, key) -> parsed response, injected so tests can
    drive the whole experiment with a fake. Returns per-case
    records; a transport failure or a spent budget ends the
    affected case with the draws it already has — an unanswered
    case is data, not a crash."""
    types = candidate_types(cases)
    records = []
    for case in cases:
        payload = build_classify_payload(case, types, scoring)
        draws_p = []
        dists = []
        picked_seen = []
        n_draws = DRAWS_BASE
        error = ""
        while len(draws_p) < n_draws:
            if budget.exceeded:
                error = "token budget exhausted"
                break
            try:
                resp = post_json(endpoint, payload, api_key)
            except LabError as e:
                error = str(e)
                break
            usage = resp.get("usage") or {}
            charged = usage.get("prompt_tokens")
            if not isinstance(charged, int) or charged < 0:
                charged = estimate_tokens(json.dumps(payload))
            budget.charge(charged)
            answers = resp.get("answers") or {}
            dist, picked = parse_choice_answer(answers.get("rel_type"))
            if picked is not None:
                picked_seen.append(picked)
            if dist is None:
                # No bandable probability: the eval harness stops
                # the case here too — an unanswered draw is not
                # retried into a different answer.
                if not error:
                    error = "answer carried no per-option probabilities"
                break
            dists.append(dist)
            draws_p.append(dist.get(case["type"], 0.0))
            if len(draws_p) == 1:
                n_draws = draw_count(draws_p[0], case["type"], scoring)
        outcome, median, spread = case_outcome(
            draws_p, case["type"], scoring)
        avg_dist = {}
        if dists:
            for option in dists[0]:
                avg_dist[option] = (sum(d.get(option, 0.0) for d in dists)
                                    / len(dists))
        records.append({
            "from": case["from"], "to": case["to"], "type": case["type"],
            "work": case.get("work") or "", "expect": case.get("expect", "any"),
            "quote_excerpt": excerpt(case["quote"]),
            "draws": [round(p, 4) for p in draws_p],
            "median": median, "spread": spread, "outcome": outcome,
            "predicted_type": (max(avg_dist, key=avg_dist.get)
                               if avg_dist else None),
            "p_none_median": (statistics.median(
                [d.get(NONE_OPTION, 0.0) for d in dists]) if dists else None),
            "error": error,
        })
    return records, types


def classify_reports(records):
    """Per-book and pooled metric blocks over the gated records
    (expect keep/drop), in the harness's scope order."""
    gated = [r for r in records if r["expect"] in ("keep", "drop")]
    scopes = []
    seen = []
    for row in gated:
        if row["work"] not in seen:
            seen.append(row["work"])
    for work in seen:
        scopes.append((f"book {work!r}" if work else "book (unnamed)",
                       [r for r in gated if r["work"] == work]))
    scopes.append(("all books", gated))
    return [(label, compute_metrics(rows)) for label, rows in scopes]


def cmd_classify(args):
    labels_path = Path(args.labels)
    try:
        cases = json.loads(labels_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"Error: cannot read labels file {labels_path}: {e}")
        return 2
    scoring = production_scoring()
    types = candidate_types(cases)
    if args.dry_run:
        print(f"Endpoint: {args.endpoint}  Model: {CLASSIFY_MODEL}")
        print(f"{len(cases)} cases; label set: "
              f"{', '.join(types + [NONE_OPTION])}")
        print("One payload per case (draws repeat it verbatim):\n")
        for case in cases:
            print(json.dumps(
                build_classify_payload(case, types, scoring), indent=2))
        return 0
    api_key = resolve_api_key(args.config)
    if not api_key:
        print("Error: no OpenRouter key available (set "
              "OPENROUTER_API_KEY or openrouter_api_key in config.json); "
              "live classify cannot run.")
        return 2
    info = fetch_model_info(CLASSIFY_MODEL)
    if info is None:
        print(f"Note: live catalog check for {CLASSIFY_MODEL} was "
              f"unreachable; proceeding on the stamped catalog facts.")
    # Pre-flight budget check on the worst case: every case at the
    # maximum draw count. Refusing up front beats dying mid-run.
    worst = 0
    for case in cases:
        payload = build_classify_payload(case, types, scoring)
        worst += estimate_tokens(json.dumps(payload)) * DRAWS_NEAR_EDGE
    if worst > args.max_tokens:
        print(f"Error: worst-case plan needs ~{worst} input tokens, "
              f"over the --max-tokens budget of {args.max_tokens}; "
              f"raise the budget to run.")
        return 2
    print(f"Plan: {len(cases)} cases, {DRAWS_BASE} draws each "
          f"({DRAWS_NEAR_EDGE} near a band edge); worst case "
          f"~{worst} input tokens, estimated cost "
          f"~${estimate_cost(worst):.4f} at catalog prices.")
    budget = TokenBudget(args.max_tokens)
    started = time.perf_counter()
    records, _ = run_classify(
        cases, http_post_json, api_key, args.endpoint, scoring, budget)
    wall = time.perf_counter() - started
    reports = classify_reports(records)
    agreed = sum(1 for r in records
                 if r["predicted_type"] == r["type"])
    results = {
        "experiment": "classify",
        "model": CLASSIFY_MODEL,
        "incumbent": INCUMBENT_MODEL,
        "endpoint": args.endpoint,
        "labels_file": str(labels_path),
        "label_set": types + [NONE_OPTION],
        "scoring": {"keep": scoring["keep"], "drop": scoring["drop"],
                    "family": sorted(scoring["family"])},
        "wall_seconds": round(wall, 2),
        "input_tokens_charged": budget.spent,
        "estimated_cost_usd": round(estimate_cost(budget.spent), 6),
        "type_agreement": {"value": (agreed / len(records)) if records
                           else None, "num": agreed, "den": len(records)},
        "metrics": {label: {k: (metric_pair_json(v)
                                if isinstance(v, tuple) else v)
                            for k, v in m.items()}
                    for label, m in reports},
        "cases": records,
    }
    lines = [
        f"# ScaleDown lab — classify: {CLASSIFY_MODEL} vs "
        f"{INCUMBENT_MODEL}",
        "",
        f"Relationship eval fixtures ({len(records)} cases, "
        f"{sum(1 for r in records if r['expect'] in ('keep', 'drop'))} "
        f"gated), quote-only states (the fixtures carry no corpus "
        f"windows). Wall clock {wall:.1f}s; input tokens charged "
        f"{budget.spent}; estimated cost "
        f"${estimate_cost(budget.spent):.4f}.",
        "",
        "## Classify-1 metrics (harness definitions)",
        "",
    ]
    for label, m in reports:
        lines.append(f"- {label} ({m['cases']} gated): " + "; ".join([
            fmt_metric("precision", m["precision"]),
            fmt_metric("recall", m["recall"]),
            fmt_metric("type accuracy", m["type_accuracy"]),
            fmt_metric("abstention", m["abstention"])]))
    lines += [
        "",
        f"Supplementary: the choice question's top pick matched the "
        f"claimed type in {agreed}/{len(records)} cases (all cases, "
        f"gated or not).",
        "",
        "## Incumbent numbers",
        "",
        "No per-case verdicts from the incumbent are stored in the "
        "repo's artefacts for these fixtures, and the lab does not "
        "re-run the incumbent. For the side-by-side, run the "
        "incumbent's own harness on the same fixtures:",
        "",
        "    python3 evaluate-relationships.py --config config.json",
        "",
        "It prints the same four metrics, per book and pooled, for "
        f"{INCUMBENT_MODEL} (add --corpus there for the production "
        "context-window path; this lab run is quote-only).",
        "",
        "## Cases",
        "",
        "| Case | Expect | Outcome | Draws (P on claimed type) | "
        "Top pick | P(none) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in records:
        draws = ", ".join(f"{p:.2f}" for p in r["draws"]) or "—"
        p_none = (f"{r['p_none_median']:.2f}"
                  if r["p_none_median"] is not None else "—")
        lines.append(
            f"| {r['from']} → {r['to']} ({r['type']}) | {r['expect']} "
            f"| {r['outcome']} | {draws} | {r['predicted_type'] or '—'} "
            f"| {p_none} |")
    json_path, md_path = write_outputs(args.out_dir, "classify",
                                       results, "\n".join(lines) + "\n")
    print(f"Wrote {json_path} and {md_path}")
    for label, m in reports:
        print(f"  {label}: " + "; ".join([
            fmt_metric("precision", m["precision"]),
            fmt_metric("recall", m["recall"]),
            fmt_metric("type accuracy", m["type_accuracy"]),
            fmt_metric("abstention", m["abstention"])]))
    return 0


# ---------------------------------------------------------------------------
# Chapter input for E2/E3. The User's books live on his machine,
# so the lab takes --epub or --text and explains itself when it
# gets neither — there is deliberately no bundled sample text,
# because numbers measured on synthetic prose would be quoted
# back as if they described a real book.
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """XHTML to plain text with nothing but the stdlib: block
    elements become line breaks, script/style content is dropped,
    entities are decoded by the parser itself."""

    _BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5",
              "h6", "tr", "blockquote", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "title"):
            self._skip += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "title") and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self):
        raw = "".join(self._parts)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def epub_chapters(epub_path, min_chars=800):
    """Chapter texts from an EPUB, spine order, stdlib only.

    A minimal reader, not a second EPUB engine: container -> OPF
    -> manifest/spine -> XHTML text. Documents under min_chars
    are dropped (covers, title pages, and other front matter are
    not chapters, and feeding one to the models would measure
    them on a copyright page). Returns [{index, label, text}]
    with index counting the kept documents."""
    try:
        zf = zipfile.ZipFile(epub_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise LabError(f"cannot open EPUB {epub_path}: {e.__class__.__name__}")
    with zf:
        names = set(zf.namelist())
        if "META-INF/container.xml" not in names:
            raise LabError("not an EPUB: META-INF/container.xml missing")
        container = ElementTree.fromstring(_read_zip_entry_capped(
            zf, "META-INF/container.xml", EPUB_CONTAINER_MAX_BYTES))
        opf_path = None
        for el in container.iter():
            if _local(el.tag) == "rootfile" and el.get("full-path"):
                opf_path = el.get("full-path")
                break
        if not opf_path:
            raise LabError("EPUB container names no rootfile")
        opf = ElementTree.fromstring(_read_zip_entry_capped(
            zf, opf_path, EPUB_OPF_MAX_BYTES))
        manifest = {}
        spine = []
        for el in opf.iter():
            kind = _local(el.tag)
            if kind == "item" and el.get("id") and el.get("href"):
                manifest[el.get("id")] = el.get("href")
            elif kind == "itemref" and el.get("idref"):
                if el.get("linear") != "no":
                    spine.append(el.get("idref"))
        opf_dir = posixpath.dirname(opf_path)
        chapters = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            full = posixpath.normpath(posixpath.join(opf_dir, href))
            if full not in names:
                continue
            # The capped read sits outside the per-chapter try on
            # purpose: that try skips a chapter it cannot parse, but
            # an over-cap chapter must fail the book — skipping it
            # would measure the remaining chapters as if they were
            # the whole book.
            raw = _read_zip_entry_capped(zf, full,
                                         EPUB_DOCUMENT_MAX_BYTES)
            try:
                parser = _TextExtractor()
                parser.feed(raw.decode("utf-8", errors="replace"))
                text = parser.text()
            except Exception:
                continue
            if len(text) >= min_chars:
                chapters.append({"index": len(chapters),
                                 "label": Path(href).stem,
                                 "text": text})
    return chapters


def text_chapters(text_path):
    """A plain-text file is one chapter: the lab's unit of work
    is one chapter, and slicing a file it knows nothing about
    into fake chapters would invent structure."""
    try:
        text = Path(text_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise LabError(f"cannot read text file {text_path}: "
                       f"{e.__class__.__name__}")
    return [{"index": 0, "label": Path(text_path).stem, "text": text}]


def load_chapters(args):
    """(source_label, chapters) or raises LabError explaining
    what input is needed."""
    if args.epub:
        return f"EPUB {args.epub}", epub_chapters(args.epub,
                                                  args.min_chapter_chars)
    if args.text:
        return f"text file {args.text}", text_chapters(args.text)
    raise LabError(
        "no chapter input: pass --epub <book.epub> or --text "
        "<chapter.txt> (the lab ships no sample text — results on "
        "invented prose would not describe a real book)")


def pick_chapter(chapters, wanted):
    if not chapters:
        raise LabError("no text-bearing chapters found in the input")
    for ch in chapters:
        if ch["index"] == wanted:
            return ch
    raise LabError(f"chapter {wanted} not among the "
                   f"{len(chapters)} text-bearing chapters "
                   f"(indexes 0..{len(chapters) - 1})")


def _names_from_preview(data):
    """Roster names from a pipeline preview artefact: the main
    characters list plus the minor list, since the pipeline's
    roster is the union of both."""
    names = []
    for key in ("characters", "minor_characters"):
        for entry in data.get(key) or []:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(entry["name"])
            elif isinstance(entry, str) and entry:
                names.append(entry)
    return names


def load_preview(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LabError(f"cannot read preview artefact {path}: {e}")


def parse_roster_arg(value):
    """--roster accepts a comma-separated list or a path to a
    text file with one name per line; a value that names an
    existing file is read as the file."""
    if not value:
        return []
    path = Path(value)
    if path.is_file():
        return [ln.strip() for ln in
                path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [part.strip() for part in value.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# E2 — extract: ScaleDown Extract over one chapter.
#
# Extract is span extraction driven by field descriptions (the
# OpenRouter cookbook's shape: provider.options.scaledown.entities,
# one {name, description} per field; the reply's message.content is
# a JSON string whose entities carry type/text/start/end/confidence,
# with start/end indexing into the exact message sent). The lab
# therefore asks for mention spans — character names, alias forms,
# relationship phrases — and assembles the roster view itself. It
# reports speed, tokens, cost, and the found spans; pairing spans
# into claims and judging them is the User's eyeball work, exactly
# as this experiment was scoped.
# ---------------------------------------------------------------------------

EXTRACT_ENTITIES = [
    {"name": "character_name",
     "description": "The name of a character — a person, a named "
                    "creature, or a named artificial intelligence — "
                    "exactly as written in the text. One entry per "
                    "mention."},
    {"name": "character_alias",
     "description": "An alternate way the text refers to a "
                    "character: a nickname, a shortened or "
                    "surname-only form, or a title used in place of "
                    "the name."},
    {"name": "relationship_phrase",
     "description": "A short phrase that states a relationship "
                    "between two characters, such as a possessive "
                    "kinship phrase ('his wife', 'Sebeck's son, "
                    "Chris'), a stated bond, or a stated role one "
                    "character holds toward another."},
]

EXTRACT_MAX_TOKENS = 16000


def build_extract_payload(text, roster):
    """The chat-completions request for one chapter. The known
    roster rides at the top of the message when one is supplied —
    the model has no other context channel, and whether the
    roster helps or anchors it wrongly is part of what the
    experiment shows. Offsets in the reply index into this exact
    message, preamble included, and the outputs say so."""
    content = text
    if roster:
        content = ("Known characters in this book: "
                   + ", ".join(roster) + "\n\n" + text)
    return {
        "model": EXTRACT_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": EXTRACT_MAX_TOKENS,
        "provider": {"options": {"scaledown": {
            "entities": EXTRACT_ENTITIES}}},
    }


def parse_extract_content(content):
    """Parse the reply's JSON string into a normalized entity
    list. Defensive on purpose: the model does not support
    response_format enforcement, so a reply wrapped in prose is
    salvaged by slicing to the outermost braces rather than
    failing the whole run. Returns (entities, note)."""
    if not isinstance(content, str):
        return [], "reply content was not text"
    parsed = None
    try:
        parsed = json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(content[start:end + 1])
            except ValueError:
                parsed = None
    if not isinstance(parsed, dict):
        return [], "reply did not parse as JSON"
    entities = []
    for raw in parsed.get("entities") or []:
        if not isinstance(raw, dict) or not raw.get("text"):
            continue
        entities.append({
            "type": raw.get("type") or "",
            "text": raw["text"],
            "start": raw.get("start"), "end": raw.get("end"),
            "confidence": raw.get("confidence"),
        })
    return entities, ""


def group_character_names(entities):
    """Mention spans to a roster view: one row per distinct name
    (case- and whitespace-insensitive), with its mention count
    and strongest confidence. The pipeline's roster discipline
    (alias pruning, variant matching) is NOT reproduced here —
    this is the model's raw yield, which is the point."""
    grouped = {}
    for ent in entities:
        if ent["type"] != "character_name":
            continue
        key = " ".join(ent["text"].split()).casefold()
        row = grouped.setdefault(
            key, {"name": ent["text"], "mentions": 0, "max_confidence": None})
        row["mentions"] += 1
        conf = ent["confidence"]
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            if row["max_confidence"] is None or conf > row["max_confidence"]:
                row["max_confidence"] = conf
    return sorted(grouped.values(),
                  key=lambda r: (-r["mentions"], r["name"].casefold()))


def _span_rows(entities, kind, source_text):
    rows = []
    for ent in entities:
        if ent["type"] != kind:
            continue
        ctx = ""
        if isinstance(ent["start"], int) and isinstance(ent["end"], int):
            lo = max(0, ent["start"] - 80)
            hi = min(len(source_text), ent["end"] + 80)
            ctx = excerpt(source_text[lo:hi])
        rows.append({"text": excerpt(ent["text"]),
                     "start": ent["start"], "end": ent["end"],
                     "confidence": ent["confidence"], "context": ctx})
    return rows


def _chat_call(post_json, url, payload, api_key, budget):
    """One chat-completions call with budget charging and usage
    capture. Returns (response, usage_dict, wall_seconds)."""
    if budget.exceeded:
        raise LabError("token budget exhausted before the call")
    started = time.perf_counter()
    resp = post_json(url, payload, api_key)
    wall = time.perf_counter() - started
    usage = resp.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    if not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        prompt_tokens = estimate_tokens(json.dumps(payload))
    completion_tokens = usage.get("completion_tokens")
    if not isinstance(completion_tokens, int) or completion_tokens < 0:
        completion_tokens = 0
    budget.charge(prompt_tokens)
    return resp, {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "cost_usd": usage.get("cost")}, wall


def _reply_content(resp):
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def cmd_extract(args):
    return _cmd_extract_or_summarize(args, "extract")


def cmd_summarize(args):
    return _cmd_extract_or_summarize(args, "summarize")


def _cmd_extract_or_summarize(args, kind):
    try:
        source_label, chapters = load_chapters(args)
        chapter = pick_chapter(chapters, args.chapter)
    except LabError as e:
        print(f"Error: {e}")
        return 2
    preview = None
    if args.preview_json:
        try:
            preview = load_preview(args.preview_json)
        except LabError as e:
            print(f"Error: {e}")
            return 2
    roster = parse_roster_arg(getattr(args, "roster", ""))
    if not roster and preview is not None:
        roster = _names_from_preview(preview)
    text = chapter["text"]
    if kind == "extract":
        payload = build_extract_payload(text, roster)
    else:
        payload = build_summarize_payload(text, args.instruction)
    if args.dry_run:
        print(f"Source: {source_label}; chapter "
              f"{chapter['index']} ({chapter['label']}), "
              f"{len(text)} chars, ~{estimate_tokens(text)} tokens.")
        print(f"Endpoint: {CHAT_URL}")
        print(json.dumps(payload, indent=2))
        return 0
    api_key = resolve_api_key(args.config)
    if not api_key:
        print("Error: no OpenRouter key available (set "
              "OPENROUTER_API_KEY or openrouter_api_key in config.json); "
              f"live {kind} cannot run.")
        return 2
    model = EXTRACT_MODEL if kind == "extract" else SUMMARIZE_MODEL
    if fetch_model_info(model) is None:
        print(f"Note: live catalog check for {model} was unreachable; "
              f"proceeding on the stamped catalog facts.")
    planned = estimate_tokens(json.dumps(payload))
    if planned > args.max_tokens:
        print(f"Error: this chapter plans ~{planned} input tokens, over "
              f"the --max-tokens budget of {args.max_tokens}; raise the "
              f"budget or pick a shorter chapter.")
        return 2
    print(f"Plan: one {kind} call on chapter {chapter['index']} "
          f"({chapter['label']}), ~{planned} input tokens, estimated "
          f"cost ~${estimate_cost(planned):.4f} at catalog prices.")
    budget = TokenBudget(args.max_tokens)
    try:
        resp, usage, wall = _chat_call(
            http_post_json, CHAT_URL, payload, api_key, budget)
    except LabError as e:
        print(f"Error: {e}")
        return 2
    sent_text = payload["messages"][0]["content"]
    if kind == "extract":
        return _finish_extract(args, source_label, chapter, roster,
                               resp, usage, wall, sent_text, preview)
    return _finish_summarize(args, source_label, chapter, resp, usage,
                             wall, preview)


def _finish_extract(args, source_label, chapter, roster, resp, usage,
                    wall, sent_text, preview):
    content = _reply_content(resp)
    entities, note = parse_extract_content(content)
    names = group_character_names(entities)
    aliases = _span_rows(entities, "character_alias", sent_text)
    rels = _span_rows(entities, "relationship_phrase", sent_text)
    diff = None
    if preview is not None:
        preview_names = _names_from_preview(preview)
        norm = lambda s: " ".join(s.split()).casefold()
        extract_keys = {norm(n["name"]) for n in names}
        preview_keys = {norm(n) for n in preview_names}
        display = {norm(n["name"]): n["name"] for n in names}
        display.update({norm(n): n for n in preview_names})
        diff = {
            "only_extract": sorted(display[k] for k in
                                   extract_keys - preview_keys),
            "only_pipeline": sorted(display[k] for k in
                                    preview_keys - extract_keys),
            "both": len(extract_keys & preview_keys),
        }
    results = {
        "experiment": "extract", "model": EXTRACT_MODEL,
        "source": source_label,
        "chapter": {"index": chapter["index"], "label": chapter["label"],
                    "chars": len(chapter["text"])},
        "roster_supplied": roster,
        "wall_seconds": round(wall, 2),
        "usage": usage,
        "estimated_cost_usd": round(
            estimate_cost(usage["prompt_tokens"],
                          usage["completion_tokens"]), 6),
        "parse_note": note,
        "entities_total": len(entities),
        "characters": names,
        "aliases": aliases,
        "relationship_phrases": rels,
        "diff_vs_preview": diff,
    }
    lines = [
        f"# ScaleDown lab — extract: {EXTRACT_MODEL}",
        "",
        f"Source {source_label}; chapter {chapter['index']} "
        f"({chapter['label']}), {len(chapter['text'])} chars. "
        f"Roster supplied: {len(roster)} names. Wall clock "
        f"{wall:.2f}s; prompt tokens {usage['prompt_tokens']}, "
        f"completion tokens {usage['completion_tokens']}; estimated "
        f"cost ${estimate_cost(usage['prompt_tokens'], usage['completion_tokens']):.4f}.",
        "",
        f"## Characters found ({len(names)} distinct, "
        f"{sum(n['mentions'] for n in names)} mention spans)",
        "",
    ]
    for n in names:
        conf = (f"{n['max_confidence']:.2f}"
                if n["max_confidence"] is not None else "—")
        lines.append(f"- {n['name']} — {n['mentions']} mention(s), "
                     f"max confidence {conf}")
    lines += ["", f"## Relationship phrases ({len(rels)})", ""]
    for r in rels:
        lines.append(f"- “{r['text']}” (offset {r['start']}) — "
                     f"{r['context']}")
    if note:
        lines += ["", f"Parse note: {note}"]
    if diff is not None:
        lines += [
            "", "## Diff vs the pipeline preview artefact", "",
            f"Found by both: {diff['both']}. Only Extract: "
            f"{len(diff['only_extract'])} ({', '.join(diff['only_extract'])}). "
            f"Only the pipeline: {len(diff['only_pipeline'])} "
            f"({', '.join(diff['only_pipeline'])}).",
        ]
    json_path, md_path = write_outputs(args.out_dir, "extract", results,
                                       "\n".join(lines) + "\n")
    print(f"Wrote {json_path} and {md_path}")
    print(f"  {len(names)} distinct character names, {len(rels)} "
          f"relationship phrases, {wall:.2f}s, "
          f"${estimate_cost(usage['prompt_tokens'], usage['completion_tokens']):.4f} est.")
    return 0


# ---------------------------------------------------------------------------
# E3 — summarize: ScaleDown Summarize over one chapter, next to
# the pipeline's own summary of the same chapter when a preview
# artefact supplies one. Positional matching: the lab's chapter
# N is the Nth text-bearing chapter of the input; the artefact's
# chapter_summaries are in the pipeline's chapter order. For a
# well-formed EPUB those orders agree, but the report says the
# matching is positional so a shifted pairing is visible rather
# than silent.
# ---------------------------------------------------------------------------

SUMMARIZE_MAX_TOKENS = 2000


def build_summarize_payload(text, instruction=""):
    """The chat-completions request for one chapter. The text goes
    in bare by default — the model is a purpose-built summarizer,
    and adding the lab's own phrasing would measure the phrasing.
    --instruction exists so the User can test whether a steer
    (length, focus) helps on his books."""
    content = text
    if instruction:
        content = instruction + "\n\n" + text
    return {
        "model": SUMMARIZE_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": SUMMARIZE_MAX_TOKENS,
    }


def _finish_summarize(args, source_label, chapter, resp, usage, wall,
                      preview):
    summary = _reply_content(resp) or ""
    pipeline_summary = None
    pipeline_label = None
    if preview is not None:
        summaries = [s for s in (preview.get("chapter_summaries") or [])
                     if isinstance(s, dict) and s.get("summary")]
        if 0 <= chapter["index"] < len(summaries):
            pipeline_summary = summaries[chapter["index"]]["summary"]
            pipeline_label = summaries[chapter["index"]].get("label")
    results = {
        "experiment": "summarize", "model": SUMMARIZE_MODEL,
        "source": source_label,
        "chapter": {"index": chapter["index"], "label": chapter["label"],
                    "chars": len(chapter["text"])},
        "instruction": args.instruction,
        "wall_seconds": round(wall, 2),
        "usage": usage,
        "estimated_cost_usd": round(
            estimate_cost(usage["prompt_tokens"],
                          usage["completion_tokens"]), 6),
        "scaledown_summary": summary,
        "pipeline_summary": pipeline_summary,
        "pipeline_chapter_label": pipeline_label,
    }
    lines = [
        f"# ScaleDown lab — summarize: {SUMMARIZE_MODEL}",
        "",
        f"Source {source_label}; chapter {chapter['index']} "
        f"({chapter['label']}), {len(chapter['text'])} chars. Wall "
        f"clock {wall:.2f}s; prompt tokens {usage['prompt_tokens']}, "
        f"completion tokens {usage['completion_tokens']}; estimated "
        f"cost ${estimate_cost(usage['prompt_tokens'], usage['completion_tokens']):.4f}.",
        "",
        "## Side by side",
        "",
        "| Pipeline summary | ScaleDown summary |",
        "| --- | --- |",
    ]
    def cell(s):
        return (s or "_(none available)_").replace("|", "\\|").replace(
            "\n", " ")
    if pipeline_summary is None:
        lines.append(f"| _(no pipeline summary — pass --preview-json "
                     f"with a pipeline preview artefact for this book)_ "
                     f"| {cell(summary)} |")
    else:
        lines.append(f"| {cell(pipeline_summary)} | {cell(summary)} |")
    lines += ["",
              "Matching is positional: the lab's chapter index counts "
              "text-bearing chapters of the input; the artefact's "
              "summaries ride in pipeline chapter order.",
              ""]
    json_path, md_path = write_outputs(args.out_dir, "summarize", results,
                                       "\n".join(lines) + "\n")
    print(f"Wrote {json_path} and {md_path}")
    print(f"  summary {len(summary)} chars, {wall:.2f}s, "
          f"${estimate_cost(usage['prompt_tokens'], usage['completion_tokens']):.4f} est.")
    return 0


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def _add_common(ap):
    ap.add_argument("--config", default=str(
        Path(__file__).resolve().parent / "config.json"),
        help="processor config.json holding the OpenRouter key "
             "(default: config.json next to this script)")
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                    help="hard ceiling on input tokens for the run "
                         f"(default {DEFAULT_MAX_TOKENS})")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help=f"output directory (default {DEFAULT_OUT_DIR})")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact request payloads; send nothing")


def _add_chapter_input(ap):
    ap.add_argument("--epub", default="",
                    help="path to an EPUB on this machine")
    ap.add_argument("--text", default="",
                    help="path to a plain-text chapter file")
    ap.add_argument("--chapter", type=int, default=0,
                    help="which text-bearing chapter (0-based, "
                         "default 0)")
    ap.add_argument("--min-chapter-chars", type=int, default=800,
                    help="EPUB documents shorter than this are front "
                         "matter, not chapters (default 800)")
    ap.add_argument("--preview-json", default="",
                    help="a pipeline preview artefact for the same "
                         "book (roster source + comparison)")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Experimental ScaleDown evaluation harness "
                    "(lab tool; not part of the shipped pipeline).")
    sub = ap.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser(
        "classify", help="Classify-1 vs the incumbent decision model "
                         "on the relationship eval fixtures")
    _add_common(p_classify)
    p_classify.add_argument("--labels", default=str(
        Path(__file__).resolve().parent / "eval"
        / "relationship_cases.json"),
        help="relationship eval fixtures (default: the repo's "
             "eval/relationship_cases.json)")
    p_classify.add_argument("--endpoint", default=DECISIONS_URL,
                            help="Decisions API endpoint (default: the "
                                 "documented decisions route; the "
                                 "processor's systemone route is "
                                 f"{SYSTEMONE_URL})")
    p_classify.set_defaults(func=cmd_classify)

    p_extract = sub.add_parser(
        "extract", help="Extract over one chapter: throughput + shape")
    _add_common(p_extract)
    _add_chapter_input(p_extract)
    p_extract.add_argument("--roster", default="",
                           help="known roster: comma-separated names, "
                                "or a path to a one-name-per-line file "
                                "(default: from --preview-json when given)")
    p_extract.set_defaults(func=cmd_extract)

    p_summarize = sub.add_parser(
        "summarize", help="Summarize over one chapter, side by side "
                          "with the pipeline's summary")
    _add_common(p_summarize)
    _add_chapter_input(p_summarize)
    p_summarize.add_argument("--instruction", default="",
                             help="optional steering line prepended to "
                                  "the chapter text (default: none — "
                                  "the text goes in bare)")
    p_summarize.set_defaults(func=cmd_summarize)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
