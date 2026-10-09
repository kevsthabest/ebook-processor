#!/usr/bin/env python3
"""Prototype: small-model trigger validator for the ebook processor.

Uses a local Ollama model to judge whether an evidence quote actually
depicts a trigger category. Replaces/augments the regex gates in
process-portable.py::_TRIGGER_EVIDENCE_RE.

Usage:
    python3 prototype-trigger-classifier.py --model qwen2.5-2k

    # Or classify a specific quote:
    python3 prototype-trigger-classifier.py --model qwen2.5:0.5b \
        --trigger suicide \
        --quote "He told her he would kill himself if she left"

Requires Ollama running on localhost:11434 with the model pulled.
For CPU testing: ollama pull qwen2.5:0.5b  (or :1.5b)
"""

import argparse
import json
import sys
import urllib.request

# Trigger definitions (same as proposed for Call B prompt)
TRIGGER_DEFS = {
    "suicide": "a character attempts or completes suicide on the page",
    "sexual_violence": "a character is subjected to non-consensual sexual acts",
    "child_abuse": "a child is physically harmed or abused",
    "self_harm": "a character deliberately injures themselves",
    "murder": "a character kills another character on the page",
    "graphic_violence": "violence described in graphic, visceral detail",
}

# Test cases: (trigger, quote, expected)
TEST_CASES = [
    # True positives — should be YES
    ("suicide", "He tied the rope around his neck and kicked the chair away.", True),
    ("suicide", "He told her he would kill himself if she left.", True),
    ("sexual_violence", "He forced himself on her despite her screams.", True),
    # False positives — should be NO
    ("suicide", "Taking that job would be suicide.", False),
    ("suicide", "He was committing digital suicide by deleting everything.", False),
    ("suicide", "He was hitting himself over the stupid mistake.", False),
    ("sexual_violence", "They danced closely, both laughing.", False),
]


def classify(model, trigger, quote, timeout=30):
    """Ask the model: does this quote depict the trigger? Returns (verdict, raw)."""
    definition = TRIGGER_DEFS.get(trigger, trigger)
    prompt = (
        f"Evidence quote: \"{quote}\"\n\n"
        f"Question: Does this passage depict {definition}?\n"
        f"Answer with only YES or NO."
    )
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 5},
    }).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return None, f"ERROR: {e}"
    raw = result.get("response", "").strip().upper()
    if raw.startswith("YES"):
        return True, raw
    elif raw.startswith("NO"):
        return False, raw
    else:
        return None, f"UNCLEAR: {raw}"


def main():
    ap = argparse.ArgumentParser(description="Prototype trigger classifier")
    ap.add_argument("--model", default="qwen2.5-2k",
                    help="Ollama model to use")
    ap.add_argument("--trigger", help="Trigger category for single classification")
    ap.add_argument("--quote", help="Evidence quote for single classification")
    args = ap.parse_args()

    if args.trigger and args.quote:
        verdict, raw = classify(args.model, args.trigger, args.quote)
        print(f"Verdict: {verdict} ({raw})")
        return

    # Run test suite
    print(f"Model: {args.model}\n")
    correct = 0
    for trigger, quote, expected in TEST_CASES:
        verdict, raw = classify(args.model, trigger, quote)
        ok = "✓" if verdict == expected else "✗"
        if verdict == expected:
            correct += 1
        print(f"{ok} [{trigger}] expected={expected} got={verdict}")
        print(f"  Quote: {quote[:60]}...")
        if verdict is None:
            print(f"  Raw: {raw}")
    print(f"\n{correct}/{len(TEST_CASES)} correct")


if __name__ == "__main__":
    main()
