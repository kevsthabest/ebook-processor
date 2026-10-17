#!/usr/bin/env python3
"""Find kinship sentences for a pair in a corpus work's text.

Answers the question the relationship eval cannot: does this
book anywhere state the pair's family bond, and in what words?
Born from the Daemon surname case — Chris->Peter child could
not be confirmed from Daemon's own text (its only two binding
sentences are surname-anchored), and the natural next question
was whether the sequel states it outright. The scan reuses the
processor's own sentence splitter and name-form machinery:
every sentence carrying a kinship marker and a reference to
both endpoints (full name, first name, nickname, or surname)
is printed with the form each endpoint was matched by, so an
explicit statement ("Peter Sebeck's son, Chris") is visibly
different from a surname-anchored one ("Sebeck's son, Chris").

Usage:
    python3 find-kinship-evidence.py --work Freedom \
        --a "Peter Sebeck" --b "Chris Sebeck"
    (corpus opened read-only; --learn-dir overrides the
    standard per-user learn dir)

Exit codes: 0 = scan ran (hits may be zero); 2 = config error
(no corpus, work not found/readable).
"""
import argparse
import importlib.util
import re
import sys
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _form_class(name, matched_text):
    """Which form of `name` a matched string is: the full name,
    the first name, a nickname, or the surname alone."""
    if matched_text == name:
        return "full"
    parts = name.split()
    if len(parts) >= 2 and matched_text == parts[-1]:
        return "surname"
    if matched_text == parts[0]:
        return "first"
    return "nickname"


def _all_forms(pp, name, roster):
    """Every form the scan accepts for an endpoint: the miner's
    roster forms, the surname form, and nickname variants of the
    first name (a sequel may call Peter Sebeck 'Pete')."""
    forms = set(pp._pair_name_forms(name, roster))
    surname = pp._pair_surname_form(name, roster)
    if surname:
        forms.add(surname)
    parts = name.split()
    if parts:
        first = parts[0].lower()
        for nick, canon in pp._COMMON_NICKNAMES.items():
            if canon == first:
                forms.add(nick.capitalize())
                if len(parts) >= 2:
                    forms.add(f"{nick.capitalize()} {parts[-1]}")
    return sorted(forms)


def scan_pair(pp, chapters, name_a, name_b, roster=None):
    """Sentences with a kinship marker referencing both
    endpoints. Returns [{chapter, sentence, a_form, b_form}]."""
    if roster is None:
        # The form machinery trusts little without a roster
        # ("without a roster entry, only the full name") — give it
        # the pair itself, which is all the guards need here.
        # Caveat: a two-person roster cannot know that the book
        # also holds a "Sebeck Jones" (the surname guard) or a
        # second Peter (the shared-first-name guard), so in such
        # books a surname- or nickname-labelled hit may belong to
        # the namesake — the form labels exist so the reader can
        # discount exactly those hits.
        roster = {name_a.lower(): {"name": name_a, "aliases": []},
                  name_b.lower(): {"name": name_b, "aliases": []}}
    forms_a = _all_forms(pp, name_a, roster)
    forms_b = _all_forms(pp, name_b, roster)
    re_a = pp._forms_re(forms_a)
    re_b = pp._forms_re(forms_b)
    hits = []
    for ch in chapters or []:
        for sent in pp._split_sentences(ch.get("text", "")):
            if not pp._PAIR_MARKER_RE.search(sent):
                continue
            # One occurrence cannot place both endpoints: a
            # same-surname pair ("Sebeck" for both Peter and
            # Chris) needs two disjoint matches, or the sentence
            # "references" Chris with the Sebeck inside Peter's
            # own full name.
            placed = _place_both(
                re_a, re_b, sent,
                lambda t: _form_rank(name_a, t),
                lambda t: _form_rank(name_b, t))
            if placed is None:
                continue
            ma, mb = placed
            hits.append({
                "chapter": ch.get("label", ""),
                "sentence": sent,
                "a_form": _form_class(name_a, ma.group(0)),
                "b_form": _form_class(name_b, mb.group(0)),
            })
    # Strongest placements first: a probe's reader should meet
    # the explicit statements before the surname-anchored ones.
    _CLASS_RANK = {"full": 0, "first": 1, "nickname": 1, "surname": 2}
    hits.sort(key=lambda h: _CLASS_RANK[h["a_form"]]
              + _CLASS_RANK[h["b_form"]])
    return hits


def _form_rank(name, matched_text):
    """Placement preference: full name strongest, then
    first/nickname, surname weakest."""
    return {"full": 0, "first": 1, "nickname": 1,
            "surname": 2}[_form_class(name, matched_text)]


def _place_both(re_a, re_b, sent, rank_a, rank_b):
    """First pair of non-overlapping matches (a, b) in a
    sentence, preferring stronger forms for each side, or None.
    Surname matches are tried last because they are the weakest
    placement (and, for same-surname pairs, the ambiguous one)."""
    a_matches = list(re_a.finditer(sent))
    b_matches = list(re_b.finditer(sent))
    if not a_matches or not b_matches:
        return None
    for ma in sorted(a_matches, key=lambda m: rank_a(m.group(0))):
        for mb in sorted(b_matches, key=lambda m: rank_b(m.group(0))):
            if not (ma.start() < mb.end() and mb.start() < ma.end()):
                return ma, mb
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", required=True,
                    help="corpus work title (substring match)")
    ap.add_argument("--a", required=True, help="first endpoint "
                                                 "(canonical name)")
    ap.add_argument("--b", required=True, help="second endpoint")
    ap.add_argument("--learn-dir", default=str(
        Path.home() / ".ebook-processor" / "learned"))
    args = ap.parse_args()

    db = pp._corpus_db_for(args.learn_dir, mode="report")
    if db is None:
        print(f"Error: no corpus.db in learn dir {args.learn_dir}")
        return 2
    rows = db.query("SELECT fingerprint, title FROM corpus_works")
    needle = args.work.lower()
    found = 0
    for fp, title in rows:
        if needle not in (title or "").lower():
            continue
        chapters = pp.corpus_chapters_for(db, fp)
        if not chapters:
            continue
        found += 1
        hits = scan_pair(pp, chapters, args.a, args.b)
        print(f"{title}: {len(hits)} kinship sentence(s) naming "
              f"both {args.a} and {args.b}")
        for h in hits:
            print(f"  [{h['chapter']}] "
                  f"({args.a}: {h['a_form']}, {args.b}: {h['b_form']}) "
                  f"{h['sentence']}")
    if not found:
        print(f"Error: no readable corpus work matches {args.work!r}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
