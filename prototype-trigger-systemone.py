#!/usr/bin/env python3
"""Prototype: trigger validation via Unsloth /v1/systemone decision API.

Uses a dedicated decision model (e.g. Laya) through Unsloth's Jev-compatible
endpoint. Returns P(yes) for "does this quote depict [trigger]?" — no
prompt engineering, no parsing YES/NO text.

Usage:
    python3 prototype-trigger-systemone.py --base-url http://localhost:11434 \
        --model laya --api-key sk-unsloth-...

    # Single classification:
    python3 prototype-trigger-systemone.py --trigger suicide \
        --quote "He told her he would kill himself if she left"
"""

import argparse
import json
import os
import sys
import urllib.request

OPENROUTER_SYSTEMONE_BASE = "https://openrouter.ai/api/v1"
# Pinned Jev release; never the "latest" alias.
OPENROUTER_DECISION_MODEL = "typesafe/jev-1.13"
OPENROUTER_ATTRIBUTION_TITLE = "ebook-processor"


def resolve_provider(provider, base_url, model, api_key):
    """Resolve decision-model connection details.

    Returns (base_url, model, api_key, extra_headers). Raises ValueError
    when the openrouter provider lacks an API key; the key is never logged.
    """
    if provider == "openrouter":
        key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise ValueError(
                "provider 'openrouter' needs an API key: pass --api-key "
                "or set OPENROUTER_API_KEY")
        return (OPENROUTER_SYSTEMONE_BASE,
                model or OPENROUTER_DECISION_MODEL,
                key,
                {"X-Title": OPENROUTER_ATTRIBUTION_TITLE})
    return (base_url, model, api_key, {})

TRIGGER_DEFS = {
    "suicide": (
        "Does this passage depict suicide?",
        "A character attempts or completes suicide; expresses clear intent to kill themselves",
        "Metaphorical use ('career suicide', 'digital suicide'); hypothetical; someone else's death",
    ),
    "sexual_violence": (
        "Does this passage depict sexual violence?",
        "Non-consensual sexual acts, assault, or coercion described or clearly implied",
        "Consensual intimacy; romantic tension without coercion",
    ),
    "child_abuse": (
        "Does this passage depict child abuse?",
        "A child is physically harmed, sexually abused, or severely neglected",
        "Discipline without injury; adults arguing around children",
    ),
    "self_harm": (
        "Does this passage depict self-harm?",
        "A character deliberately injures themselves (cutting, burning, etc.)",
        "Metaphorical ('beating himself up over it'); accidental injury",
    ),
    "murder": (
        "Does this passage depict murder?",
        "A character kills another character on the page",
        "Threats without action; deaths by accident or natural causes",
    ),
    "graphic_violence": (
        "Does this passage depict graphic violence?",
        "Violence described in visceral, bloody, or disturbing detail",
        "Mild action violence; violence mentioned but not described",
    ),
}

# Per-trigger decision thresholds (tuned from prototype runs)
TRIGGER_THRESHOLDS = {
    "suicide": 0.82,
    "sexual_violence": 0.20,
    "child_abuse": 0.50,
    "self_harm": 0.50,
    "murder": 0.50,
    "graphic_violence": 0.50,
}

TEST_CASES = [
    ("suicide", "He tied the rope around his neck and kicked the chair away.", True),
    ("suicide", "He told her he would kill himself if she left.", True),
    ("sexual_violence", "He forced himself on her despite her screams.", True),
    ("suicide", "Taking that job would be suicide.", False),
    ("suicide", "He was committing digital suicide by deleting everything.", False),
    ("suicide", "He was hitting himself over the stupid mistake.", False),
    ("sexual_violence", "They danced closely, both laughing.", False),
]


def classify(base_url, model, api_key, trigger, quote, timeout=30,
             extra_headers=None):
    """Returns (verdict_bool, p_yes, raw_response)."""
    q, yes_when, no_when = TRIGGER_DEFS.get(
        trigger, (f"Does this depict {trigger}?", "", ""))
    threshold = TRIGGER_THRESHOLDS.get(trigger, 0.5)
    req_body = {"state": quote, "model": model or "default"}
    payload = json.dumps({
        **req_body,
        "questions": {
            "trigger_check": {
                "type": "noul",
                "instructions": f"{q} Answer YES when: {yes_when}. Answer NO when: {no_when}.",
            }
        },
    }).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(extra_headers or {})
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    base = base_url.rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    req = urllib.request.Request(
        f"{base}/v1/systemone", data=payload, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return None, None, f"ERROR: {e}"

    # Response shape: {"answers": {"trigger_check": {"type": "noul", "noul": 0.97}}}
    try:
        ans = result["answers"]["trigger_check"]
        p_yes = ans.get("noul")
        if p_yes is None:
            # fallback: probabilities dict
            p_yes = ans.get("probabilities", {}).get("yes", 0)
        verdict = p_yes >= threshold
        return verdict, p_yes, json.dumps(ans)
    except (KeyError, TypeError):
        return None, None, f"UNEXPECTED: {json.dumps(result)[:200]}"


def main():
    ap = argparse.ArgumentParser(description="Trigger classifier via /v1/systemone")
    ap.add_argument("--base-url", default="http://localhost:11434")
    ap.add_argument("--provider", choices=["local", "openrouter"],
                    default="local",
                    help="decision-model backend: 'local' Unsloth server "
                         "(default) or 'openrouter' hosted Jev")
    ap.add_argument("--model", default="",
                    help="Model name (empty = use already-loaded model)")
    ap.add_argument("--api-key", default="",
                    help="API key override (or OPENROUTER_API_KEY env)")
    ap.add_argument("--trigger", help="Trigger for single classification")
    ap.add_argument("--quote", help="Quote for single classification")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    try:
        base_url, model, api_key, extra_headers = resolve_provider(
            args.provider, args.base_url, args.model, args.api_key)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(2)

    if args.trigger and args.quote:
        verdict, p_yes, raw = classify(
            base_url, model, api_key, args.trigger, args.quote,
            extra_headers=extra_headers)
        p_str = f"{p_yes:.2f}" if p_yes is not None else "?"
        print(f"Verdict: {verdict} (P(yes)={p_str})")
        return

    print(f"Model: {model} @ {base_url} (provider={args.provider})\n")
    correct = 0
    for trigger, quote, expected in TEST_CASES:
        verdict, p_yes, raw = classify(
            base_url, model, api_key, trigger, quote,
            extra_headers=extra_headers)
        ok = "✓" if verdict == expected else "✗"
        if verdict == expected:
            correct += 1
        p_str = f"{p_yes:.2f}" if p_yes is not None else "?"
        print(f"{ok} [{trigger}] expected={expected} got={verdict} P(yes)={p_str}")
        print(f"  Quote: {quote[:60]}...")
        if verdict is None:
            print(f"  Raw: {raw}")
    print(f"\n{correct}/{len(TEST_CASES)} correct")


if __name__ == "__main__":
    main()
