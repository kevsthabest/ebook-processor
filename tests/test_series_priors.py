"""Series priors (series-linking spec, Phase 3) and the Step 0
registry key unification.

Phase 3 offers a series registry's character memory to the v2
roster as hints through three seams — a canonical-form tie-break
in the merge funnel, registry forms parked as unresolved
mentions, and merge-candidate nominations into the existing
judge flow. Every test here pins a seam AND one of the spec's
hard limits: priors never create a character, never touch
relationship evidence, never cross series, and never outrank
the book's own attestations (a Tier 1 cannot-link vetoes a
nomination outright; a strictly cleaner name form beats the
registry's spelling).

Step 0: the registry population seam folds before work
resolution (fallback book key) while a finished record folds
under its work_id. These tests pin the seam's restatement step:
live fold, then resolution, then a finished-record re-fold must
leave ONE presence key with single attestations.

Fixtures are synthetic but shaped like the real artefacts (the
Daemon cast, whose Loki/Gragg handle pair is the book's own
same-person-two-forms case). Stdlib unittest only, no network,
stores in temp dirs.
Run: python -m unittest tests.test_series_priors -v
"""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sr = _load("series_registry", "series_registry.py")
pp = _load("pp_series_priors", "process-portable.py")

SERIES = "hc-series:999"        # the Daemon series, probe id 1056
OTHER_SERIES = "hc-series:555"  # a different series entirely
EMPYREAN = "hc-series:41764"    # the probe-verified Empyrean id

# The "earlier book" whose fold populates the registry: Peter
# Sebeck attested under his nickname and surname, Brian Gragg
# under his game handle and surname (Daemon's own facts).
BOOK1 = {
    "title": "Daemon", "author": "Daniel Suarez",
    "work_id": "book-1",
    "series_name": "Daemon", "series_position": 1,
    "series_source": "hardcover", "series_key": SERIES,
    "hardcover": {"compilation": False},
    "characters": [
        {"name": "Peter Sebeck", "aliases": ["Pete Sebeck", "Sebeck"]},
        {"name": "Brian Gragg", "aliases": ["Loki", "Gragg"]},
        {"name": "Jon Ross", "aliases": []},
    ],
    "relationships": [],
}

OTHER_BOOK = {
    "title": "Other Book", "author": "Other Author",
    "work_id": "other-1",
    "series_name": "Other", "series_position": 1,
    "series_source": "hardcover", "series_key": OTHER_SERIES,
    "hardcover": {"compilation": False},
    "characters": [
        {"name": "Petra Sebeck", "aliases": ["Zebeck"]},
    ],
    "relationships": [],
}

ZELDA_BOOK = {
    "title": "Daemon", "author": "Daniel Suarez",
    "work_id": "book-0",
    "series_name": "Daemon", "series_position": 0,
    "series_source": "hardcover", "series_key": SERIES,
    "hardcover": {"compilation": False},
    "characters": [
        {"name": "Zelda Zebra", "aliases": ["Zed"]},
    ],
    "relationships": [],
}

# A Step 0 book: folded live (no work_id yet), resolved, then
# re-folded from the finished record (work_id present).
LIVE_RECORD = {
    "title": "Fourth Wing", "author": "Rebecca Yarros",
    "series_name": "The Empyrean", "series_position": 1,
    "series_source": "hardcover", "series_key": EMPYREAN,
    "hardcover": {"compilation": False},
    "characters": [
        {"name": "Violet Sorrengail", "aliases": ["Vi"]},
        {"name": "Brennan Sorrengail", "aliases": []},
    ],
    "relationships": [
        {"from": "Brennan Sorrengail", "to": "Violet Sorrengail",
         "type": "sibling", "validation_state": "validated"},
    ],
}
FALLBACK_KEY = "title:fourth wing|rebecca yarros"
WORK_ID = "work-42"

_CHS = [{"index": 1, "label": "Ch1",
         "text": "Pete Sebeck met Jon Ross at the precinct. "
                 "'We work together now,' Ross said. " * 10}]
_CH_A = {1: {"characters": [], "pov_character": None,
             "relationships": [
                 {"from": "Pete Sebeck", "to": "Jon Ross",
                  "type": "colleague",
                  "evidence": "We work together now"}]}}


class _MergeJudge:
    """post_pass_merge Tier 3 stub: records every pair it is
    asked about and returns a fixed verdict."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    def ask_same_person(self, na, _da, _ea, nb, _db, _eb):
        self.calls.append((na, nb))
        return self.verdict


class _RelValidator:
    model = "stub"

    def validate_relationship(self, frm, to, rtype, quote,
                              context=""):
        return True, 0.95


def _fold(roster, name, aliases, chapter_idx):
    pp._roster_update(
        roster,
        [{"name": name, "aliases": aliases, "role": "supporting",
          "description": "", "evidence": ""}],
        chapter_idx)


def _entry(name, chapters, appearances=None, aliases=()):
    """A roster entry in the full shape _roster_update builds,
    for tests that must control entries the update-time tiers
    would have merged before the post-pass ever saw them."""
    key = pp.norm_name(name)
    return {"name": name, "aliases": set(aliases),
            "alias_keys": {key}
            | {pp.norm_name(a) for a in aliases},
            "primary_keys": {key}, "_generic_keys": set(),
            "appearances": (len(chapters) if appearances is None
                            else appearances),
            "last_seen": max(chapters) if chapters else 0,
            "chapters": set(chapters), "roles": Counter(),
            "descriptions": [], "evidence": ""}


def _roster_snapshot(roster):
    return {k: {"name": e["name"],
                "aliases": sorted(e["aliases"]),
                "unresolved": sorted(
                    e.get("unresolved_mentions", ())),
                "appearances": e["appearances"],
                "chapters": sorted(e["chapters"]),
                "primary_keys": sorted(e["primary_keys"])}
            for k, e in sorted(roster.items())}


class _PriorsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reg = sr.SeriesRegistry(
            path=Path(self._tmp.name) / "series_registry.db")
        self.addCleanup(self.reg.close)
        sr.populate_from_record(self.reg, copy.deepcopy(BOOK1))
        sr.populate_from_record(self.reg, copy.deepcopy(OTHER_BOOK))
        self._saved_registry = pp._SR_REGISTRY
        pp._SR_REGISTRY = (sr, self.reg)
        self._saved_flag = pp.CONFIG.get("series_priors")
        pp.CONFIG["series_priors"] = True
        self._saved_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.addCleanup(self._restore)

    def _restore(self):
        pp._SR_REGISTRY = self._saved_registry
        pp._DIAG.series_priors = None
        if self._saved_flag is None:
            pp.CONFIG.pop("series_priors", None)
        else:
            pp.CONFIG["series_priors"] = self._saved_flag
        pp._DECISION_VALIDATOR = self._saved_validator
        pp._dm_budget_reset()

    def ctx(self, series=SERIES):
        ctx = pp._load_series_priors(series)
        self.assertIsNotNone(ctx)
        pp._DIAG.series_priors = ctx
        return ctx


class TestLoader(_PriorsCase):
    def test_flag_off_loads_nothing(self):
        pp.CONFIG["series_priors"] = False
        self.assertIsNone(pp._load_series_priors(SERIES))
        self.assertIsNone(pp._series_priors_for_book(
            {}, "Daemon", "Daniel Suarez", "Daemon"))

    def test_unknown_series_loads_nothing(self):
        self.assertIsNone(pp._load_series_priors("hc-series:nope"))

    def test_compilation_consumes_nothing(self):
        saved = pp._hardcover_lookup
        pp._hardcover_lookup = lambda ids, t, a: {
            "compilation": True, "series_id": 999,
            "series_name": "Daemon",
            "authors": ["Daniel Suarez"]}
        try:
            self.assertIsNone(pp._series_priors_for_book(
                {}, "Daemon", "Daniel Suarez", "Daemon"))
        finally:
            pp._hardcover_lookup = saved

    def test_non_compilation_loads(self):
        saved = pp._hardcover_lookup
        pp._hardcover_lookup = lambda ids, t, a: {
            "compilation": False, "series_id": 999,
            "series_name": "Daemon",
            "authors": ["Daniel Suarez"]}
        try:
            ctx = pp._series_priors_for_book(
                {}, "Daemon", "Daniel Suarez", "Daemon")
        finally:
            pp._hardcover_lookup = saved
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["series_key"], SERIES)

    def test_shared_form_is_poisoned(self):
        # Two characters of one series both claim the alias
        # "Sam": the shared form identifies nobody and must not
        # match either character.
        for wid, name in (("book-a", "Samuel One"),
                          ("book-b", "Samantha Two")):
            sr.populate_from_record(self.reg, {
                "title": "T", "author": "A", "work_id": wid,
                "series_name": "S", "series_position": 1,
                "series_source": "hardcover",
                "series_key": "hc-series:777",
                "hardcover": {"compilation": False},
                "characters": [{"name": name, "aliases": ["Sam"]}],
                "relationships": []})
        ctx = self.ctx("hc-series:777")
        self.assertNotIn("sam", ctx["form_index"])
        roster = {}
        _fold(roster, "Sam", [], 1)
        self.assertEqual(
            roster["sam"].get("unresolved_mentions", set()), set())
        self.assertEqual(ctx["matched_chars"], set())


class TestSeeding(_PriorsCase):
    def test_registry_forms_park_as_unresolved_mentions(self):
        ctx = self.ctx()
        roster = {}
        _fold(roster, "Pete Sebeck", [], 1)
        entry = roster["pete sebeck"]
        # The character's other attested forms — the canonical
        # spelling and the surname — park as mentions. Nothing
        # was asserted: no alias, no rename, no new entry.
        self.assertEqual(entry["unresolved_mentions"],
                         {"Peter Sebeck", "Sebeck"})
        self.assertEqual(entry["aliases"], set())
        self.assertEqual(entry["name"], "Pete Sebeck")
        self.assertEqual(len(roster), 1)
        report = pp._series_priors_report(ctx)
        self.assertEqual(report["aliases_seeded"], 2)
        self.assertEqual(report["registry_characters_matched"], 1)

    def test_unextracted_character_creates_nothing(self):
        ctx = self.ctx()
        roster = {}
        _fold(roster, "Jon Ross", [], 1)
        # Sebeck and Gragg are in the registry but not in this
        # book: priors must not conjure them.
        self.assertEqual(set(roster), {"jon ross"})
        self.assertEqual(
            roster["jon ross"].get("unresolved_mentions", set()),
            set())
        self.assertEqual(
            pp._series_priors_report(ctx)["aliases_seeded"], 0)

    def test_seeded_mention_promotes_on_in_book_attestation(self):
        self.ctx()
        roster = {}
        _fold(roster, "Pete Sebeck", [], 1)
        entry = roster["pete sebeck"]
        self.assertIn("Peter Sebeck",
                      entry["unresolved_mentions"])
        # A later chapter attests the form as this character's
        # alias: the ordinary adoption path promotes it — the
        # book confirmed the prior, the prior asserted nothing.
        _fold(roster, "Pete Sebeck", ["Peter Sebeck"], 2)
        self.assertIn("Peter Sebeck", entry["aliases"])

    def test_seeding_is_scoped_to_the_books_own_series(self):
        ctx = self.ctx(OTHER_SERIES)
        roster = {}
        _fold(roster, "Pete Sebeck", [], 1)
        # Pete is a form of THIS series' Sebeck (SERIES); the
        # other series' registry knows a different Sebeck. The
        # other series' context must not seed this book.
        self.assertEqual(
            roster["pete sebeck"].get("unresolved_mentions",
                                      set()), set())
        self.assertEqual(ctx["matched_chars"], set())
        # ...while the other series' own forms do match their
        # own character (the context is live, not broken).
        _fold(roster, "Petra Sebeck", [], 1)
        self.assertEqual(
            roster["petra sebeck"]["unresolved_mentions"],
            {"Zebeck"})


class TestTieBreak(_PriorsCase):
    def _pair_roster(self):
        roster = {}
        roster["pete sebeck"] = _entry("Pete Sebeck", {1, 2, 3})
        roster["peter sebeck"] = _entry("Peter Sebeck", {4},
                                        appearances=1)
        return roster

    def test_registry_spelling_wins_a_tie(self):
        ctx = self.ctx()
        roster = self._pair_roster()
        pp.post_pass_merge(roster, validator=None)
        # Tier 2 merged the pair deterministically, keeping the
        # better-attested entry — whose surface is the nickname
        # form. The existing rule is indifferent between the two
        # forms (equal stripped length), so the registry's
        # canonical spelling decides the surviving name.
        self.assertEqual(len(roster), 1)
        entry = next(iter(roster.values()))
        self.assertEqual(entry["name"], "Peter Sebeck")
        self.assertEqual(ctx["counts"]["form_tiebreaks"], 1)

    def test_without_priors_the_keeper_form_wins(self):
        pp._DIAG.series_priors = None
        roster = self._pair_roster()
        pp.post_pass_merge(roster, validator=None)
        entry = next(iter(roster.values()))
        self.assertEqual(entry["name"], "Pete Sebeck")

    def test_strictly_cleaner_form_beats_the_registry(self):
        ctx = self.ctx()
        roster = {}
        roster["sebeck"] = _entry("Sebeck", {1},
                                  appearances=3)
        roster["peter sebeck"] = _entry("Peter Sebeck", {2},
                                        appearances=1)
        judge = _MergeJudge((True, 0.9))
        pp.post_pass_merge(roster, validator=judge)
        # The pair merged on the judge's yes. The surviving form
        # question is NOT a tie — "Sebeck" is strictly shorter
        # after noise-stripping — so the book's own rule keeps
        # it and the registry's longer canonical does not fire.
        entry = next(iter(roster.values()))
        self.assertEqual(entry["name"], "Sebeck")
        self.assertEqual(ctx["counts"]["form_tiebreaks"], 0)


class TestNomination(_PriorsCase):
    def _handle_roster(self, chapters_a, chapters_b):
        roster = {}
        roster["loki"] = _entry("Loki", chapters_a)
        roster["gragg"] = _entry("Gragg", chapters_b)
        return roster

    def test_nomination_reaches_the_judge(self):
        ctx = self.ctx()
        roster = self._handle_roster({1}, {2})
        judge = _MergeJudge((False, 0.1))
        _n, possible = pp.post_pass_merge(roster, validator=judge)
        # Without priors this pair is invisible: no name-shape
        # candidate, no similarity signal. The nomination put it
        # in front of the judge — and the judge's NO governed:
        # no merge, the pair listed with the judge's own P.
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(set(judge.calls[0]), {"Loki", "Gragg"})
        self.assertEqual(len(roster), 2)
        pair = [m for m in possible
                if {m["name_a"], m["name_b"]} == {"Loki", "Gragg"}]
        self.assertEqual(len(pair), 1)
        self.assertEqual(pair[0]["p_yes"], 0.1)
        self.assertEqual(ctx["counts"]["pairs_nominated"], 1)

    def test_no_nomination_without_priors(self):
        pp._DIAG.series_priors = None
        roster = self._handle_roster({1}, {2})
        judge = _MergeJudge((True, 0.99))
        pp.post_pass_merge(roster, validator=judge)
        self.assertEqual(judge.calls, [])

    def test_tier1_cannot_link_vetoes_the_nomination(self):
        ctx = self.ctx()
        # Co-listed in one chapter: the book itself says two
        # people. The prior loses — the pair is never judged.
        roster = self._handle_roster({1}, {1})
        judge = _MergeJudge((True, 0.99))
        _n, possible = pp.post_pass_merge(roster, validator=judge)
        self.assertEqual(judge.calls, [])
        self.assertEqual(
            [m for m in possible
             if {m["name_a"], m["name_b"]} == {"Loki", "Gragg"}],
            [])
        self.assertEqual(ctx["counts"]["pairs_nominated"], 0)


class TestRelationshipUntouched(_PriorsCase):
    def _run_book(self, ctx):
        pp._DIAG.series_priors = ctx
        pp._DECISION_VALIDATOR = _RelValidator()
        roster = {}
        _fold(roster, "Pete Sebeck", [], 1)
        _fold(roster, "Jon Ross", [], 1)
        pp.post_pass_merge(roster, validator=None)
        pp._dm_budget_reset()
        red = pp.v2_reduce(copy.deepcopy(_CH_A), {}, roster,
                           _CHS, preview=False)

        def shape(red):
            chars = [{k: v for k, v in c.items()
                      if k != "unresolved_mentions"}
                     for c in red["characters"]]
            return json.dumps({
                "relationships": red["relationships"],
                "dropped": red["dropped_relationships"],
                "losers": red["pair_losers"],
                "characters": chars,
            }, sort_keys=True, default=str)

        return shape(red)

    def test_relationships_identical_with_matching_priors(self):
        off = self._run_book(None)
        ctx = pp._load_series_priors(SERIES)
        on = self._run_book(ctx)
        self.assertEqual(off, on)
        # The identity above is not vacuous: the priors were
        # live and did seed this book's roster.
        self.assertEqual(ctx["counts"]["aliases_seeded"], 2)


class TestInvariance(_PriorsCase):
    def test_nonmatching_priors_change_nothing(self):
        # A registry for the book's series exists and the flag is
        # on, but its characters share no form with this book:
        # every seam runs and does nothing.
        reg2 = sr.SeriesRegistry(
            path=Path(self._tmp.name) / "reg2.db")
        self.addCleanup(reg2.close)
        sr.populate_from_record(reg2, copy.deepcopy(ZELDA_BOOK))
        pp._SR_REGISTRY = (sr, reg2)
        ctx = pp._load_series_priors(SERIES)
        self.assertIsNotNone(ctx)

        def run(active_ctx):
            pp._DIAG.series_priors = active_ctx
            pp._DECISION_VALIDATOR = _RelValidator()
            roster = {}
            _fold(roster, "Pete Sebeck", [], 1)
            _fold(roster, "Jon Ross", [], 1)
            _fold(roster, "Loki", [], 1)
            _n, possible = pp.post_pass_merge(
                roster, validator=None)
            pp._dm_budget_reset()
            red = pp.v2_reduce(copy.deepcopy(_CH_A), {}, roster,
                               _CHS, preview=False)
            return json.dumps({
                "roster": _roster_snapshot(roster),
                "possible": possible,
                "relationships": red["relationships"],
                "characters": red["characters"],
            }, sort_keys=True, default=str)

        off = run(None)
        on = run(ctx)
        self.assertEqual(off, on)
        self.assertEqual(pp._series_priors_report(ctx), {
            "registry_characters_matched": 0, "aliases_seeded": 0,
            "form_tiebreaks": 0, "pairs_nominated": 0})


class TestStep0KeyUnification(unittest.TestCase):
    """The population seam folds before work resolution; the
    finished record folds under its work_id. The seam's
    restatement must leave one book under one key."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reg = sr.SeriesRegistry(
            path=Path(self._tmp.name) / "series_registry.db")
        self.addCleanup(self.reg.close)
        self._saved_registry = pp._SR_REGISTRY
        pp._SR_REGISTRY = (sr, self.reg)
        self.addCleanup(self._restore)

    def _restore(self):
        pp._SR_REGISTRY = self._saved_registry

    def _book_keys(self):
        keys = set()
        for c in self.reg.characters(EMPYREAN):
            keys |= {b["book_key"] for b in c["books"]}
            for a in c["aliases"]:
                keys |= set(a["books"])
        keys |= {l["book_key"]
                 for l in self.reg.family_links(EMPYREAN)}
        return keys

    def test_live_fold_then_resolution_then_refold(self):
        live = copy.deepcopy(LIVE_RECORD)
        pp._populate_series_registry(live)
        self.assertEqual(self._book_keys(), {FALLBACK_KEY})
        self.assertIn("series_registry", live)

        pp._adopt_series_registry_book_key(live, WORK_ID)
        self.assertEqual(self._book_keys(), {WORK_ID})

        done = copy.deepcopy(LIVE_RECORD)
        done["work_id"] = WORK_ID
        pp._populate_series_registry(done)
        self.assertEqual(self._book_keys(), {WORK_ID})
        violet = {c["canonical_name"]: c
                  for c in self.reg.characters(EMPYREAN)}[
                      "Violet Sorrengail"]
        self.assertEqual([b["book_key"] for b in violet["books"]],
                         [WORK_ID])
        self.assertTrue(all(a["books"] == [WORK_ID]
                            for a in violet["aliases"]))
        self.assertTrue(all(
            l["book_key"] == WORK_ID
            for l in self.reg.family_links(EMPYREAN)))

    def test_refold_first_then_live_fold_then_resolution(self):
        done = copy.deepcopy(LIVE_RECORD)
        done["work_id"] = WORK_ID
        pp._populate_series_registry(done)
        self.assertEqual(self._book_keys(), {WORK_ID})

        live = copy.deepcopy(LIVE_RECORD)
        pp._populate_series_registry(live)
        # The split, visible until resolution restates it:
        self.assertEqual(self._book_keys(),
                         {FALLBACK_KEY, WORK_ID})
        pp._adopt_series_registry_book_key(live, WORK_ID)
        self.assertEqual(self._book_keys(), {WORK_ID})
        violet = {c["canonical_name"]: c
                  for c in self.reg.characters(EMPYREAN)}[
                      "Violet Sorrengail"]
        self.assertEqual(len(violet["books"]), 1)

    def test_adopt_without_a_fold_is_a_noop(self):
        record = {"title": "Standalone", "author": "Someone"}
        pp._populate_series_registry(record)
        self.assertNotIn("series_registry", record)
        pp._adopt_series_registry_book_key(record, WORK_ID)
        self.assertEqual(self.reg.series_keys(), [])


if __name__ == "__main__":
    unittest.main()
