#!/usr/bin/env python3
"""Prototype: decision-model character merge validation via /v1/systemone.

Tests whether Laya can answer "Are X and Y the same person?" using
character data from a pre-generated preview JSON.

Usage:
    python3 prototype-merge-check.py --json Daemon_-_Daniel_Suarez-4.json \
        --base-url http://127.0.0.1:8888/v1

    # Single pair:
    python3 prototype-merge-check.py --a "Peter Sebeck" --b "Pete" \
        --base-url http://127.0.0.1:8888/v1
"""

import argparse
import json
import sys
import urllib.request


def ask_same_person(base_url, model, api_key, name_a, name_b, desc_a="",
                    desc_b="", timeout=30):
    """Returns (verdict_bool, p_yes, raw)."""
    state = f"Character A: {name_a}"
    if desc_a:
        state += f" ({desc_a})"
    state += f"\nCharacter B: {name_b}"
    if desc_b:
        state += f" ({desc_b})"

    payload = json.dumps({
        "model": model or "default",
        "state": state,
        "questions": {
            "same_person": {
                "type": "noul",
                "instructions": (
                    "Are these two names referring to the same person? "
                    "Answer YES when: the names are variants of each other "
                    "(nickname, shortened form, title + surname matching full name, "
                    "same first name with compatible details). "
                    "Answer NO when: different first names, different people with "
                    "similar surnames (siblings, parent/child), or insufficient "
                    "evidence they are the same individual."
                ),
            }
        },
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    base = base_url.rstrip('/')
    if base.endswith('/v1/systemone'):
        base = base[:-len('/v1/systemone')]
    elif base.endswith('/v1'):
        base = base[:-3]
    req = urllib.request.Request(
        f"{base}/v1/systemone", data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return None, None, f"ERROR: {e}"
    try:
        ans = result["answers"]["same_person"]
        p_yes = ans.get("noul")
        if p_yes is None:
            p_yes = ans.get("probabilities", {}).get("yes", 0)
        return p_yes >= 0.5, float(p_yes), json.dumps(ans)
    except (KeyError, TypeError):
        return None, None, f"UNEXPECTED: {json.dumps(result)[:200]}"


def main():
    ap = argparse.ArgumentParser(description="Character merge check via systemone")
    ap.add_argument("--json", help="Preview JSON to test against")
    ap.add_argument("--base-url", default="http://127.0.0.1:8888/v1")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--a", help="First name for single-pair test")
    ap.add_argument("--b", help="Second name for single-pair test")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    if args.a and args.b:
        verdict, p_yes, raw = ask_same_person(
            args.base_url, args.model, args.api_key, args.a, args.b)
        p_str = f"{p_yes:.2f}" if p_yes is not None else "?"
        print(f"Same person? {verdict} (P(yes)={p_str})")
        return

    if not args.json:
        print("Provide --json or --a/--b", file=sys.stderr)
        sys.exit(1)

    data = json.load(open(args.json))
    chars = data.get('characters', [])

    # Build test cases: aliases of same character (should be YES),
    # and similar names of different characters (should be NO)
    print(f"Loaded {len(chars)} characters\n")

    # Test 1: alias pairs (should be YES)
    print("=== ALIAS PAIRS (expect YES) ===")
    tested = 0
    for c in chars[:20]:  # first 20 to keep it quick
        name = c.get('name', '')
        aliases = c.get('aliases', [])
        if aliases and tested < 5:
            alias = aliases[0] if isinstance(aliases[0], str) else aliases[0].get('name', '')
            if alias and alias.lower() != name.lower():
                verdict, p_yes, raw = ask_same_person(
                    args.base_url, args.model, args.api_key,
                    name, alias,
                    c.get('description', '')[:100])
                p_str = f"{p_yes:.2f}" if p_yes is not None else "?"
                mark = "✓" if verdict else "✗"
                print(f"{mark} '{name}' vs '{alias}': {verdict} (P={p_str})")
                tested += 1

    # Test 2: similar surnames, different people (should be NO)
    print("\n=== SIMILAR SURNAMES (expect NO) ===")
    # Find pairs sharing a surname but listed as separate characters
    by_surname = {}
    for c in chars:
        parts = c.get('name', '').split()
        if len(parts) >= 2:
            surname = parts[-1].lower()
            by_surname.setdefault(surname, []).append(c)

    tested = 0
    for surname, group in by_surname.items():
        if len(group) >= 2 and tested < 5:
            a, b = group[0], group[1]
            # Only test if they're actually different characters
            # (not already merged)
            verdict, p_yes, raw = ask_same_person(
                args.base_url, args.model, args.api_key,
                a['name'], b['name'])
            p_str = f"{p_yes:.2f}" if p_yes is not None else "?"
            mark = "✓" if not verdict else "✗"
            print(f"{mark} '{a['name']}' vs '{b['name']}': {verdict} (P={p_str})")
            tested += 1


if __name__ == "__main__":
    main()
