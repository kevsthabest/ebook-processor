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
import sys
import urllib.request

TRIGGER_DEFS = {
    "suicide": "a character attempts or completes suicide on the page",
    "sexual_violence": "a character is subjected to non-consensual sexual acts",
    "child_abuse": "a child is physically harmed or abused",
    "self_harm": "a character deliberately injures themselves",
    "murder": "a character kills another character on the page",
    "graphic_violence": "violence described in graphic, visceral detail",
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


def classify(base_url, model, api_key, trigger, quote, timeout=30):
    """Returns (verdict_bool, p_yes, raw_response)."""
    definition = TRIGGER_DEFS.get(trigger, trigger)
    req_body = {"state": quote,
    }
    if model:
        req_body["model"] = model
    payload = json.dumps({
        **req_body,
        "questions": {
            "trigger_check": {
                "type": "noul",
                "instructions": f"Does this passage depict {definition}?",
            }
        },
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/systemone", data=payload, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return None, None, f"ERROR: {e}"

    # Jev-compatible response: answers -> {question_id: {label, probabilities}}
    try:
        ans = result["answers"]["trigger_check"]
        p_yes = ans.get("probabilities", {}).get("yes", 0)
        label = ans.get("label", "").lower()
        verdict = p_yes >= 0.5 if p_yes else label in ("yes", "true")
        return verdict, p_yes, json.dumps(ans)
    except (KeyError, TypeError):
        return None, None, f"UNEXPECTED: {json.dumps(result)[:200]}"


def main():
    ap = argparse.ArgumentParser(description="Trigger classifier via /v1/systemone")
    ap.add_argument("--base-url", default="http://localhost:11434")
    ap.add_argument("--model", default="",
                    help="Model name (empty = use already-loaded model)")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--trigger", help="Trigger for single classification")
    ap.add_argument("--quote", help="Quote for single classification")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    if args.trigger and args.quote:
        verdict, p_yes, raw = classify(
            args.base_url, args.model, args.api_key, args.trigger, args.quote)
        p_str = f"{p_yes:.2f}" if p_yes is not None else "?"
        print(f"Verdict: {verdict} (P(yes)={p_str})")
        return

    print(f"Model: {args.model} @ {args.base_url}\n")
    correct = 0
    for trigger, quote, expected in TEST_CASES:
        verdict, p_yes, raw = classify(
            args.base_url, args.model, args.api_key, trigger, quote)
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
