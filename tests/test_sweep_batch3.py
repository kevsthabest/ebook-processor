"""Sweep batch 3 — silent-wrongness fixes (codebase sweep 2026-10-14,
report-2d8a7f4, Findings 8, 12, 15, 19, 23).

F8: corrupt learned-state JSON was silently reset to empty, then
overwritten by save() — accumulated learning vanished with no record.
Fix: a load failure warns LOUDLY (file + error), the original bytes are
preserved as a .corrupt backup before any overwrite, and pop_summary
does not print counts implying persistence for state whose load failed.
Series rosters share the same load/save seam and get the same rule.

F12: a garbled or missing per-chapter spice level became 0 in the
sanitizers — indistinguishable from a genuinely clean chapter — and
the resulting false-low book level overwrote works.spice_detected.
Fix: unknown spice is None at the sanitizer; the book level is computed
from known chapters only; when NO chapter has a known level the book
level is unknown (None) and the works overwrite is skipped (the write
site already guards on None).

F15: the decision-model budget was claimed up front for Tier-3 merge
candidates the validator never saw (validator off, merged-away pairs),
while the trigger decision-gate never claimed at all. Fix: claim
immediately before each actual validator call at both gates, never
for skipped candidates (the claim itself was already atomic under
the budget lock; per-book reset semantics unchanged).

F19: with the principals filter off, ghost-endpoint relationships
diverged silently between preview and DB: from-ghost entries were
keyed into the write rel_map and never consumed; to-ghost entries
were embedded pointing at a character with no row. Fix: relationships
whose either endpoint has no character row in the final set are moved
to dropped_relationships with note "ghost_endpoint".

F23 minor bundle:
(a) persisted chunks_failed counted Call A failures only while the
    abort decision uses A+B — now the persisted count matches.
(b) viz chapter display was off by one (first chapter "Ch. 2/N") —
    the loop index is already 1-based.
(c) evaluate-merges.py turned a missing score into p_yes = 0.0 and
    dropped errored pairs from the denominators invisibly — now it
    matches the production validator's strictness (missing/malformed
    score is unanswered) and prints an unanswered count.
(d) overnight-watch.sh persisted malformed sb-query payloads as
    empty-count history entries — now a parse failure skips the
    history entry (detection still alerts on the raw output).
(e) _trope_lookup conflated a failed catalog read (sb() -> None) with
    "not in catalog", diverting catalogued tropes to proposals —
    now a lookup failure is an error for the book, never a proposal.
    cmd_verify read sb() failures as an empty DB — now a failed read
    reports a DB failure distinctly.

Stdlib unittest only. No network, no Supabase, no real models (tiny
EPUBs in temp dirs, LLM faked at the backend seam, sb faked).
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_EM_PATH = str(Path(__file__).resolve().parent.parent / "evaluate-merges.py")
_espec = importlib.util.spec_from_file_location("evaluate_merges", _EM_PATH)
em = importlib.util.module_from_spec(_espec)
_espec.loader.exec_module(em)


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


class _ConfigGuard(unittest.TestCase):
    """Snapshot/restore the module CONFIG dict in place."""

    def setUp(self):
        self._config = dict(pp.CONFIG)

    def tearDown(self):
        pp.CONFIG.clear()
        pp.CONFIG.update(self._config)


def _ch(idx, label=None, text="x " * 200):
    return {"index": idx, "label": label or f"Ch {idx}", "text": text}


# ----------------------------------------------------------------------
# F8 — corrupt learned-state JSON: loud, preserved, never silently reset
# ----------------------------------------------------------------------

class TestCorruptLearnedState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))

    def _state_with(self, files):
        for name, content in files.items():
            p = Path(self._tmp) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        ls, out = _capture(pp.LearnedState, learn_dir=self._tmp,
                           enabled=True)
        return ls, out

    def test_corrupt_file_warns_loudly_and_starts_empty(self):
        ls, out = self._state_with({"nicknames.json": b"{not json"})
        self.assertEqual(ls.nicknames, {})
        self.assertIn("WARNING", out)
        self.assertIn("nicknames.json", out)

    def test_save_preserves_corrupt_bytes_before_overwriting(self):
        original = b"{not json at all"
        ls, _ = self._state_with({"nicknames.json": original})
        ls.record_alias("Loki", "Loki Laufeyson")
        ls.save()
        backup = Path(self._tmp) / "nicknames.json.corrupt"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), original)
        # The live file is valid new state now.
        data = json.loads((Path(self._tmp) / "nicknames.json")
                          .read_text(encoding="utf-8"))
        self.assertIn("loki", data)

    def test_second_save_does_not_back_up_the_new_file(self):
        ls, _ = self._state_with({"nicknames.json": b"broken"})
        ls.save()
        ls.save()
        backups = sorted(p.name for p in Path(self._tmp).glob("*.corrupt*"))
        self.assertEqual(backups, ["nicknames.json.corrupt"])

    def test_existing_backup_is_never_clobbered(self):
        ls, _ = self._state_with({
            "nicknames.json": b"broken-now",
            "nicknames.json.corrupt": b"older-evidence"})
        ls.save()
        self.assertEqual(
            (Path(self._tmp) / "nicknames.json.corrupt").read_bytes(),
            b"older-evidence")
        self.assertEqual(
            (Path(self._tmp) / "nicknames.json.corrupt-1").read_bytes(),
            b"broken-now")

    def test_healthy_files_get_no_backup(self):
        ls, out = self._state_with({"nicknames.json": b"{}"})
        self.assertNotIn("WARNING", out)
        ls.record_alias("Loki", "Loki Laufeyson")
        ls.save()
        self.assertEqual(list(Path(self._tmp).glob("*.corrupt*")), [])

    def test_pop_summary_flags_failed_load(self):
        ls, _ = self._state_with({"nicknames.json": b"broken"})
        ls.record_alias("Loki", "Loki Laufeyson")
        summary = ls.pop_summary()
        self.assertIn("1 nickname", summary)
        self.assertIn("failed to load", summary)

    def test_pop_summary_healthy_has_no_caveat(self):
        ls, _ = self._state_with({})
        ls.record_alias("Loki", "Loki Laufeyson")
        summary = ls.pop_summary()
        self.assertIn("1 nickname", summary)
        self.assertNotIn("failed to load", summary)


class TestCorruptSeriesRoster(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self.ls = pp.LearnedState(learn_dir=self._tmp, enabled=True)

    def test_save_series_roster_preserves_corrupt_file(self):
        p = self.ls._series_path("author_test")
        p.write_bytes(b"[corrupt")
        _, out = _capture(
            self.ls.save_series_roster, "author_test", "Title", "Author",
            "9780000000001", [{"name": "Mira Solace", "aliases": ["Mira"]}])
        self.assertIn("WARNING", out)
        self.assertEqual(p.with_name(p.name + ".corrupt").read_bytes(),
                         b"[corrupt")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual([c["name"] for c in data["characters"]],
                         ["Mira Solace"])

    def test_load_series_hints_warns_on_corrupt_file(self):
        p = self.ls._series_path("author_test")
        p.write_bytes(b"[corrupt")
        hints, out = _capture(self.ls.load_series_hints, "author_test")
        self.assertEqual(hints, {})
        self.assertIn("WARNING", out)
        self.assertIn("author_test", out)

    def test_healthy_series_save_no_backup(self):
        self.ls.save_series_roster(
            "author_test", "Title", "Author", "9780000000001",
            [{"name": "Mira Solace", "aliases": []}])
        series_dir = Path(self._tmp) / "series"
        self.assertEqual(list(series_dir.glob("*.corrupt*")), [])


# ----------------------------------------------------------------------
# F12 — unknown spice stays unknown; never a false-low 0 overwrite
# ----------------------------------------------------------------------

class TestSanitizeSpiceUnknown(unittest.TestCase):
    def test_v2b_garbled_spice_is_unknown(self):
        self.assertIsNone(
            pp._sanitize_v2b({"spice_level": "garbage"})["spice_level"])

    def test_v2b_missing_spice_is_unknown(self):
        self.assertIsNone(pp._sanitize_v2b({})["spice_level"])

    def test_v2b_explicit_none_is_unknown(self):
        self.assertIsNone(
            pp._sanitize_v2b({"spice_level": None})["spice_level"])

    def test_v2b_known_levels_pass_through(self):
        self.assertEqual(
            pp._sanitize_v2b({"spice_level": 0})["spice_level"], 0)
        self.assertEqual(
            pp._sanitize_v2b({"spice_level": 3})["spice_level"], 3)
        self.assertEqual(
            pp._sanitize_v2b({"spice_level": "4"})["spice_level"], 4)

    def test_legacy_garbled_spice_is_unknown(self):
        self.assertIsNone(pp.sanitize({"spice_level": "garbage"})
                          ["spice_level"])

    def test_legacy_missing_spice_is_unknown(self):
        self.assertIsNone(pp.sanitize({})["spice_level"])

    def test_legacy_known_levels_pass_through(self):
        self.assertEqual(pp.sanitize({"spice_level": 0})["spice_level"], 0)
        self.assertEqual(pp.sanitize({"spice_level": 5})["spice_level"], 5)


def _b(spice):
    """Sanitized Call B shape with a given (possibly unknown) spice."""
    return {"summary": "s", "spice_level": spice,
            "triggers": {c: "none" for c in pp.TRIGGER_CATEGORIES},
            "trigger_evidence": {}, "trope_candidates": [], "quotes": []}


class TestReduceSpiceUnknown(unittest.TestCase):
    def test_all_unknown_book_level_is_unknown(self):
        red = pp.v2_reduce({}, {1: _b(None), 2: _b(None)}, {},
                           [_ch(1), _ch(2)])
        self.assertIsNone(red["spice_level"])

    def test_unknown_plus_clean_is_a_real_zero(self):
        red = pp.v2_reduce({}, {1: _b(None), 2: _b(0)}, {},
                           [_ch(1), _ch(2)])
        self.assertEqual(red["spice_level"], 0)

    def test_unknown_chapter_excluded_from_frequency(self):
        # Known chapters only: [2,0,0,0,0,0] over 6 -> peak moderate,
        # 1/6 at peak -> "frequent" -> level 3. Folding the unknown
        # chapter in as a seventh (zero) chapter would give 1/7 ->
        # "occasional" -> level 2.
        bs = {1: _b(None), 2: _b(2)}
        for i in range(3, 8):
            bs[i] = _b(0)
        red = pp.v2_reduce({}, bs, {}, [_ch(i) for i in range(1, 8)])
        self.assertEqual(red["spice_level"], 3)

    def test_chapter_spice_array_keeps_int_schema(self):
        red = pp.v2_reduce({}, {1: _b(None), 2: _b(5)}, {},
                           [_ch(1), _ch(2)])
        self.assertEqual(red["chapter_spice"], [0, 0, 5])
        self.assertEqual(red["spice_level"], 5)


# --- write-path fake (mirrors tests/test_sweep_batch1.py's _StatefulSB) ---

class _StatefulSB:
    """In-memory sb() fake with PostgREST-ish GET/POST/PATCH/DELETE."""

    def __init__(self, tables=None, fail_deletes=(), fail_gets=()):
        self.tables = {t: [dict(r) for r in rows]
                       for t, rows in (tables or {}).items()}
        self.calls = []
        self.fail_deletes = set(fail_deletes)
        self.fail_gets = set(fail_gets)
        self._next_id = 1000

    def _match(self, row, params):
        for part in (params or "").lstrip("?").split("&"):
            if not part or "=" not in part:
                continue
            key, val = part.split("=", 1)
            if key in ("select", "limit", "on_conflict", "order"):
                continue
            if key == "or":
                inner = val.strip()
                if inner.startswith("(") and inner.endswith(")"):
                    inner = inner[1:-1]
                conds = [c for c in inner.split(",") if c]
                if not any(self._match_cond(row, c) for c in conds):
                    return False
                continue
            op, _, v = val.partition(".")
            if not self._match_val(row.get(key), op, unquote(v)):
                return False
        return True

    def _match_cond(self, row, cond):
        col, _, rest = cond.partition(".")
        op, _, v = rest.partition(".")
        return self._match_val(row.get(col), op, unquote(v))

    @staticmethod
    def _match_val(cell, op, v):
        if op == "is":
            if v == "null":
                return cell is None
            return cell is not None and str(cell).lower() == v
        if cell is None:
            return False
        if op == "eq":
            return str(cell) == v
        if op == "neq":
            return str(cell) != v
        if op == "ilike":
            return str(cell).lower() == v.lower()
        return False

    def __call__(self, table, params=None, method="GET", data=None,
                 prefer=None):
        self.calls.append((table, method, params or "", data))
        rows = self.tables.setdefault(table, [])
        if method == "GET":
            if table in self.fail_gets:
                return None
            return [dict(r) for r in rows if self._match(r, params)]
        if method == "POST":
            out = []
            items = data if isinstance(data, list) else [data]
            for d in items:
                r = dict(d)
                r.setdefault("id", self._next_id)
                self._next_id += 1
                rows.append(r)
                out.append(dict(r))
            return out
        if method == "PATCH":
            out = []
            for r in rows:
                if self._match(r, params):
                    r.update(data or {})
                    out.append(dict(r))
            return out
        if method == "DELETE":
            if table in self.fail_deletes:
                return None
            gone = [r for r in rows if self._match(r, params)]
            self.tables[table] = [r for r in rows
                                  if not self._match(r, params)]
            return [dict(r) for r in gone]
        return []

    def calls_for(self, table, method):
        return [c for c in self.calls if c[0] == table and c[1] == method]


def _result(**over):
    r = {"title": "Test Book", "isbn": "9780000000001", "tropes": [],
         "trope_confidence": {}, "trope_catalog_map": {}, "trope_chapters": {},
         "triggers": [], "characters": [], "relationships": [], "quotes": [],
         "povs": []}
    r.update(over)
    return r


class _WriteBase(unittest.TestCase):
    def setUp(self):
        self._old_sb = pp.sb

    def tearDown(self):
        pp.sb = self._old_sb

    def install(self, fake):
        pp.sb = fake
        self.fake = fake
        return fake


class TestSpiceWriteSkip(_WriteBase):
    def test_unknown_spice_skips_works_overwrite(self):
        fake = self.install(_StatefulSB())
        errors = pp.write_claims(1, _result(spice_level=None))
        self.assertEqual(errors, 0)
        self.assertEqual(fake.calls_for("works", "PATCH"), [])

    def test_clean_book_still_writes_zero(self):
        fake = self.install(_StatefulSB())
        errors = pp.write_claims(1, _result(spice_level=0))
        self.assertEqual(errors, 0)
        patches = fake.calls_for("works", "PATCH")
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][3], {"spice_detected": 0})

    def test_known_level_writes_through(self):
        fake = self.install(_StatefulSB())
        pp.write_claims(1, _result(spice_level=3))
        patches = fake.calls_for("works", "PATCH")
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][3], {"spice_detected": 3})


class TestSummarySpiceUnknown(unittest.TestCase):
    def test_unknown_level_is_not_called_no_spicy_content(self):
        r = {"title": "T", "author": "A", "spice_level": None,
             "characters": [], "relationships": [], "triggers": [],
             "tropes": []}
        out = pp._preview_summary(r)
        spice_line = [ln for ln in out.split("\n") if "Spice:" in ln][0]
        self.assertIn("?/5", spice_line)
        self.assertNotIn("no spicy content", spice_line)


# ----------------------------------------------------------------------
# F15 — budget is claimed per actual call, at both gates
# ----------------------------------------------------------------------

def _roster_entry(name, appearances=5, chapters=(1,)):
    """Roster entry in the exact shape _roster_update produces."""
    from collections import Counter
    nk = pp.norm_name(name)
    return nk, {"name": name, "aliases": set(), "alias_keys": {nk},
                "primary_keys": {nk}, "_generic_keys": set(),
                "appearances": appearances,
                "last_seen": max(chapters, default=0),
                "chapters": set(chapters), "roles": Counter(),
                "descriptions": [], "evidence": ""}


def _jon_roster():
    """Three Tier-3 candidates: shared first name (signal 1), disjoint
    chapters (tier 'ambiguous'), nothing for Tiers 1/2 to merge."""
    roster = {}
    for name, apps, chs in (("Jon Ross", 9, (1,)), ("Jon Smith", 5, (2,)),
                            ("Jon Reed", 3, (3,))):
        k, e = _roster_entry(name, appearances=apps, chapters=chs)
        roster[k] = e
    return roster


class _StubSamePerson:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []
        self.disabled = False

    def ask_same_person(self, na, da, ea, nb, db, eb):
        self.calls.append((na, nb))
        return self._results.pop(0) if self._results else (False, 0.2)


class TestTier3BudgetClaims(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()

    def tearDown(self):
        pp._dm_budget_reset()

    def test_validator_off_consumes_no_budget(self):
        n, possible = pp.post_pass_merge(_jon_roster(), validator=None)
        self.assertEqual(n, 0)
        self.assertEqual(len(possible), 3)
        self.assertTrue(all(p["reason"].endswith("decision model disabled)")
                            for p in possible))
        self.assertEqual(pp._DM_BUDGET_USED, 0)

    def test_disabled_validator_consumes_no_budget(self):
        stub = _StubSamePerson([])
        stub.disabled = True
        n, possible = pp.post_pass_merge(_jon_roster(), validator=stub)
        self.assertEqual(stub.calls, [])
        self.assertEqual(len(possible), 3)
        self.assertEqual(pp._DM_BUDGET_USED, 0)

    def test_each_real_call_claims_exactly_one(self):
        stub = _StubSamePerson([(False, 0.2)] * 3)
        pp.post_pass_merge(_jon_roster(), validator=stub)
        self.assertEqual(len(stub.calls), 3)
        self.assertEqual(pp._DM_BUDGET_USED, 3)

    def test_merged_away_pairs_consume_nothing(self):
        # First pair merges (Jon Ross keeps, Jon Smith drops); the
        # (Smith, Reed) candidate is then skipped — no call, no claim.
        stub = _StubSamePerson([(True, 0.9), (False, 0.2)])
        roster = _jon_roster()
        n, possible = pp.post_pass_merge(roster, validator=stub)
        self.assertEqual(n, 1)
        self.assertEqual(len(roster), 2)
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(pp._DM_BUDGET_USED, 2)

    def test_exhaustion_after_exactly_n_real_calls(self):
        # Amended 2026-10-17 (2.13.18): merge claims are a named
        # budget consumer now and the trigger gate's floor
        # reserves the pool's last _TRIGGER_GATE_FLOOR calls from
        # them, so the merge ceiling is cap - floor, not the whole
        # cap. The property under test — one claim per real call,
        # exhaustion after exactly the remaining units — is
        # unchanged, measured against the consumer's ceiling.
        # Amended again 2026-10-17 (2.13.20): hard reserves (spec
        # §1.3) — the drain now runs as merge_tier3 claims with the
        # other validation reserves released (they are not what
        # this test measures), leaving the trigger reserve as the
        # merge ceiling, exactly the quantity the assertions use.
        for _c in ("rel_family", "rel_nonfamily", "quote_poll"):
            pp._dm_budget_finish(_c)
        self.assertTrue(pp._dm_budget_claim(
            pp._MERGE_TIER3_CAP - pp._TRIGGER_GATE_FLOOR - 1,
            consumer="merge_tier3"))
        stub = _StubSamePerson([(False, 0.2)] * 3)
        n, possible = pp.post_pass_merge(_jon_roster(), validator=stub)
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(pp._DM_BUDGET_USED,
                         pp._MERGE_TIER3_CAP - pp._TRIGGER_GATE_FLOOR)
        capped = [p for p in possible
                  if p["reason"] == "unresolved (Tier 3 cap reached)"]
        self.assertEqual(len(capped), 2)


class TestTriggerGateBudget(unittest.TestCase):
    """The trigger decision-gate must claim per validate() call."""

    def setUp(self):
        self._old_validator = pp._DECISION_VALIDATOR
        pp._dm_budget_reset()
        self.calls = []

        outer = self

        class StubValidator:
            model = "stub"

            def validate(self, trigger, quote, context=""):
                outer.calls.append((trigger, quote))
                return True, 0.9

        pp._DECISION_VALIDATOR = StubValidator()

    def tearDown(self):
        pp._DECISION_VALIDATOR = self._old_validator
        pp._dm_budget_reset()

    @staticmethod
    def _reduce_two_quote_category():
        chs = [{"index": 1, "label": "Ch 1",
                "text": "the boy cried out in the dark hall"},
               {"index": 2, "label": "Ch 2",
                "text": "the boy ran toward the gate"}]
        def b(quote):
            trig = {c: "none" for c in pp.TRIGGER_CATEGORIES}
            trig["child_abuse"] = "graphic"
            return {"summary": "s", "spice_level": 0, "triggers": trig,
                    "trigger_evidence": {"child_abuse": quote},
                    "trope_candidates": [], "quotes": []}
        chapter_bs = {1: b("the boy cried"), 2: b("the boy ran")}
        return pp.v2_reduce({}, chapter_bs, {}, chs)

    @staticmethod
    def _child_abuse(red):
        return [t for t in red["triggers"]
                if t.get("warning") == "child_abuse"][0]

    def test_gate_claims_exactly_per_call(self):
        red = self._reduce_two_quote_category()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(pp._DM_BUDGET_USED, 2)
        trig = self._child_abuse(red)
        self.assertTrue(all(e.get("decision_gate") == "decision"
                            for e in trig["evidence"]))

    def test_budget_out_mid_gate_falls_back_to_regex(self):
        # One claim left: exactly one quote is judged; the category
        # then falls back to the regex gates (not silently trusted).
        # Amended 2026-10-17 (2.13.20): hard reserves (spec §1.3)
        # cap an untagged drain at the general pool, so leaving
        # exactly one claim first releases every reserve.
        # Assertions unchanged.
        for _c in pp._DM_BUDGET_ORDER:
            pp._dm_budget_finish(_c)
        self.assertTrue(pp._dm_budget_claim(pp._MERGE_TIER3_CAP - 1))
        red = self._reduce_two_quote_category()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(pp._DM_BUDGET_USED, pp._MERGE_TIER3_CAP)
        trig = self._child_abuse(red)
        self.assertTrue(all(e.get("decision_gate") == "fallback"
                            for e in trig["evidence"]))


# ----------------------------------------------------------------------
# F19 — ghost-endpoint relationships are dropped and accounted
# ----------------------------------------------------------------------

class TestGhostRelationships(unittest.TestCase):
    def _result(self):
        return {
            "characters": [{"name": "Alice"}, {"name": "Bob"}],
            "relationships": [
                {"from": "Alice", "to": "Bob", "type": "friend",
                 "evidence": "e"},
                {"from": "Ghost Person", "to": "Alice", "type": "friend",
                 "evidence": "e"},
                {"from": "Alice", "to": "Ghost Person", "type": "friend",
                 "evidence": "e"},
            ],
            "dropped_relationships": [],
        }

    def test_both_ghost_directions_dropped_and_accounted(self):
        result = self._result()
        n = pp._drop_ghost_relationships(result)
        self.assertEqual(n, 2)
        self.assertEqual(result["relationships"],
                         [{"from": "Alice", "to": "Bob", "type": "friend",
                           "evidence": "e"}])
        dropped = result["dropped_relationships"]
        self.assertEqual(len(dropped), 2)
        self.assertTrue(all(d["validation_note"] == "ghost_endpoint"
                            for d in dropped))
        self.assertEqual({(d["from"], d["to"]) for d in dropped},
                         {("Ghost Person", "Alice"),
                          ("Alice", "Ghost Person")})

    def test_source_relationship_dicts_not_mutated(self):
        result = self._result()
        originals = list(result["relationships"])
        pp._drop_ghost_relationships(result)
        for r in originals:
            self.assertNotIn("validation_note", r)

    def test_no_ghosts_is_a_noop(self):
        result = self._result()
        result["relationships"] = result["relationships"][:1]
        n = pp._drop_ghost_relationships(result)
        self.assertEqual(n, 0)
        self.assertEqual(len(result["relationships"]), 1)
        self.assertEqual(result["dropped_relationships"], [])

    def test_write_map_embeds_neither_ghost(self):
        result = self._result()
        pp._drop_ghost_relationships(result)
        rel_map, _nev = pp._build_rel_map(result)
        self.assertNotIn("ghost person", rel_map)
        embedded = [e for entries in rel_map.values() for e in entries]
        self.assertTrue(all(e["to"] != "Ghost Person" for e in embedded))
        self.assertEqual(len(embedded), 1)

    def test_principals_on_output_unchanged_by_partition(self):
        # With the filter on, surviving relationships already have both
        # endpoints in the character set — the partition must be a no-op
        # on that output (principals-on behavior unchanged).
        chars = [{"name": "Alice", "is_pov": True, "frequency": 0.5},
                 {"name": "Bob", "is_pov": False, "frequency": 0.3},
                 {"name": "Ghost Person", "is_pov": False,
                  "frequency": 0.01}]
        rels = [{"from": "Alice", "to": "Bob", "type": "friend"},
                {"from": "Alice", "to": "Ghost Person", "type": "friend"}]
        princs, _minors, prels = pp._filter_principals(chars, rels)
        result = {"characters": princs, "relationships": list(prels),
                  "dropped_relationships": []}
        n = pp._drop_ghost_relationships(result)
        self.assertEqual(n, 0)
        self.assertEqual(result["relationships"], prels)
        self.assertEqual(result["dropped_relationships"], [])


# ----------------------------------------------------------------------
# Shared tiny-EPUB harness (mirrors tests/test_sweep_batch2.py)
# ----------------------------------------------------------------------

def _xhtml(body):
    return ("<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.idpf.org/2007/xhtml'><head>"
            "<title>t</title></head>"
            f"<body>{body}</body></html>")


def _make_epub(path, files, title="Batch3 Test Book"):
    manifest = "\n".join(
        f'    <item id="i{n}" href="{name.split("/", 1)[1]}" '
        f'media-type="application/xhtml+xml"/>'
        for n, name in enumerate(files))
    spine = "\n".join(
        f'    <itemref idref="i{n}"/>' for n, name in enumerate(files))
    opf = ("""<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='bid'>
  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
    <dc:title>""" + title + """</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:identifier>9780306406157</dc:identifier>
  </metadata>
  <manifest>
""" + manifest + """
  </manifest>
  <spine>
""" + spine + """
  </spine>
</package>""")
    container = ("""<?xml version='1.0' encoding='utf-8'?>
<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' version='1.0'>
  <rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles>
</container>""")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, content in files.items():
            z.writestr(name, content)


class _E2ECase(_ConfigGuard):
    def _run_book(self, stem, files, backend, config=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        epub = str(Path(tmp.name) / f"{stem}.epub")
        _make_epub(epub, files)
        preview_json = pp.SCRIPT_DIR / "preview" / f"{stem}.json"
        preview_md = pp.SCRIPT_DIR / "preview" / f"{stem}.md"
        self.addCleanup(lambda: preview_json.unlink(missing_ok=True))
        self.addCleanup(lambda: preview_md.unlink(missing_ok=True))
        self.addCleanup(lambda: shutil.rmtree(
            pp._checkpoint_dir(pp._checkpoint_book_key(Path(epub))),
            ignore_errors=True))
        cfg = {"llm": "ollama"}
        cfg.update(config or {})
        with mock.patch.object(pp, "llm_ollama", backend), \
                mock.patch.object(pp, "_lookup_series_openlibrary",
                                  lambda *a, **k: (None, None)), \
                mock.patch.object(pp, "resolve_work",
                                  lambda *a, **k: (None, "test")), \
                mock.patch.dict(pp.CONFIG, cfg):
            ok = pp.process_file_v2(Path(epub), preview=True)
        self.assertTrue(ok)
        return json.loads(preview_json.read_text(encoding="utf-8"))


_FILLER = ("<p>The valley was quiet and the river ran cold and bright "
           "under a wide northern sky full of slow clouds.</p>")


class TestGhostRelationshipE2E(_E2ECase):
    """Principals OFF: a relationship naming a person who appears in
    the prose but was never extracted as a character must not reach
    the preview's relationships (it could never be written to the DB
    faithfully) — it is accounted in dropped_relationships instead."""

    _STORY = "Mira Solace drew her sword and faced the dawn."
    _FENWICK = "Old Fenwick mended the nets by the harbour wall."

    class _Backend:
        def __call__(self, prompt, chunk, i, n):
            if prompt == pp.PROMPT_V2_CONTENT:
                trig = {c: "none" for c in pp.TRIGGER_CATEGORIES}
                return json.dumps({
                    "summary": "s", "spice_level": 0, "triggers": trig,
                    "trigger_evidence": {}, "trope_candidates": [],
                    "quotes": []})
            return json.dumps({
                "characters": [{
                    "name": "Mira Solace", "role": "protagonist",
                    "description": "A swordswoman of the valley.",
                    "evidence": TestGhostRelationshipE2E._STORY}],
                "relationships": [{
                    "from": "Mira Solace", "to": "Old Fenwick",
                    "type": "friend",
                    "evidence": TestGhostRelationshipE2E._FENWICK}],
                "pov_character": "Mira Solace"})

    def test_ghost_relationship_accounted_not_shipped(self):
        story = (f"<p>{self._STORY}</p><p>{self._FENWICK}</p>"
                 + _FILLER * 22)
        files = {f"OEBPS/ch{k:02d}.xhtml": _xhtml(
            f"<h1>Chapter {k}</h1>" + story) for k in range(1, 3)}
        result = self._run_book("ghostbook", files, self._Backend(),
                                config={"principals_only": False})
        self.assertEqual(result["relationships"], [])
        ghosts = [d for d in result["dropped_relationships"]
                  if d.get("validation_note") == "ghost_endpoint"]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual(ghosts[0]["to"], "Old Fenwick")


# ----------------------------------------------------------------------
# F23(a) — persisted chunks_failed counts A+B, matching the abort call
# ----------------------------------------------------------------------

class TestChunksFailedCountsBothCalls(_E2ECase):
    class _Backend:
        def __call__(self, prompt, chunk, i, n):
            if prompt == pp.PROMPT_V2_CONTENT:
                if "BFAILMARKER" in chunk:
                    return None  # Call B fails for this chapter
                trig = {c: "none" for c in pp.TRIGGER_CATEGORIES}
                return json.dumps({
                    "summary": "s", "spice_level": 0, "triggers": trig,
                    "trigger_evidence": {}, "trope_candidates": [],
                    "quotes": []})
            return json.dumps({
                "characters": [{
                    "name": "Mira Solace", "role": "protagonist",
                    "description": "A swordswoman of the valley.",
                    "evidence": "Mira Solace drew her sword."}],
                "relationships": [], "pov_character": "Mira Solace"})

    def test_b_failure_is_in_persisted_count(self):
        files = {}
        for k in range(1, 5):
            marker = " BFAILMARKER" if k == 2 else ""
            body = (f"<h1>Chapter {k}</h1>"
                    f"<p>Mira Solace drew her sword.{marker}</p>"
                    + _FILLER * 22)
            files[f"OEBPS/ch{k:02d}.xhtml"] = _xhtml(body)
        result = self._run_book("bfailbook", files, self._Backend())
        self.assertEqual(result["chunks"], 4)
        # One Call B failure, zero Call A failures: the persisted
        # count must match the abort decision's A+B definition.
        self.assertEqual(result["chunks_failed"], 1)


# ----------------------------------------------------------------------
# F23(b) — viz chapter index is already 1-based
# ----------------------------------------------------------------------

class TestVizChapterIndex(_E2ECase):
    class _Backend:
        def __call__(self, prompt, chunk, i, n):
            if prompt == pp.PROMPT_V2_CONTENT:
                trig = {c: "none" for c in pp.TRIGGER_CATEGORIES}
                return json.dumps({
                    "summary": "s", "spice_level": 0, "triggers": trig,
                    "trigger_evidence": {}, "trope_candidates": [],
                    "quotes": []})
            return json.dumps({"characters": [], "relationships": [],
                               "pov_character": None})

    def test_first_chapter_is_ch_1(self):
        seen = []
        story = "<p>Mira Solace drew her sword.</p>" + _FILLER * 22
        files = {f"OEBPS/ch{k:02d}.xhtml": _xhtml(
            f"<h1>Chapter {k}</h1>" + story) for k in range(1, 3)}
        with mock.patch.object(
                pp.UI, "viz_chapter",
                lambda *a, **k: seen.append(k.get("chap_idx"))):
            self._run_book("vizbook", files, self._Backend())
        self.assertEqual(seen, [1, 2])


# ----------------------------------------------------------------------
# F23(c) — evaluate-merges.py: strict scores + unanswered count
# ----------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_returning(payload):
    def _open(req, timeout=None):
        return _FakeResp(payload)
    return _open


def _urlopen_by_state(mapping):
    def _open(req, timeout=None):
        state = json.loads(req.data)["state"]
        for marker, payload in mapping:
            if marker in state:
                return _FakeResp(payload)
        raise AssertionError(f"no stub for state: {state!r}")
    return _open


class TestEvaluateMergesStrictScores(unittest.TestCase):
    def _ask(self, payload):
        with mock.patch.object(urllib.request, "urlopen",
                               _urlopen_returning(payload)):
            return em.ask_same_person("http://x/v1", "", "", "A", "B")

    def test_noul_score_returned(self):
        self.assertEqual(
            self._ask({"answers": {"same_person": {"noul": 0.8}}}), 0.8)

    def test_probabilities_fallback_returned(self):
        self.assertEqual(
            self._ask({"answers": {"same_person":
                                   {"probabilities": {"yes": 0.7}}}}), 0.7)

    def test_missing_score_is_unanswered_not_zero(self):
        self.assertIsNone(
            self._ask({"answers": {"same_person":
                                   {"probabilities": {}}}}))

    def test_empty_answer_is_unanswered(self):
        self.assertIsNone(self._ask({"answers": {"same_person": {}}}))

    def test_string_score_is_unanswered(self):
        self.assertIsNone(
            self._ask({"answers": {"same_person": {"noul": "0.7"}}}))

    def test_bool_score_is_unanswered(self):
        self.assertIsNone(
            self._ask({"answers": {"same_person": {"noul": True}}}))

    def test_out_of_range_score_is_unanswered(self):
        self.assertIsNone(
            self._ask({"answers": {"same_person": {"noul": 1.5}}}))

    def test_nan_score_is_unanswered(self):
        self.assertIsNone(
            self._ask({"answers": {"same_person":
                                   {"noul": float("nan")}}}))

    def test_main_prints_unanswered_count(self):
        labels = {
            "p1": {"a": "Scored A", "b": "Scored B", "same_person": True},
            "p2": {"a": "Silent A", "b": "Silent B", "same_person": False},
        }
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        labels_path = str(Path(tmp) / "labels.json")
        Path(labels_path).write_text(json.dumps(labels), encoding="utf-8")
        stub = _urlopen_by_state([
            ("Scored", {"answers": {"same_person": {"noul": 0.9}}}),
            ("Silent", {"answers": {"same_person": {}}}),
        ])
        with mock.patch.object(urllib.request, "urlopen", stub), \
                mock.patch.object(sys, "argv",
                                  ["evaluate-merges.py", "--labels",
                                   labels_path]):
            _, out = _capture(em.main)
        # The unanswered pair is excluded from accuracy AND counted.
        self.assertIn("Accuracy:  100.00% (1/1)", out)
        self.assertIn("Unanswered: 1", out)


# ----------------------------------------------------------------------
# F23(d) — overnight-watch.sh: malformed payloads are not persisted
# ----------------------------------------------------------------------

class TestOvernightWatchHistory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        (Path(self._tmp) / "workspace" / "ebook-processor").mkdir(
            parents=True)
        script = (Path(__file__).resolve().parent.parent
                  / "overnight-watch.sh").read_text(encoding="utf-8")
        self._py = script.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
        self._state = (Path(self._tmp) / "workspace" / "ebook-processor"
                       / ".overnight-watch.json")

    def _run(self, counts, new_rows, alert=""):
        env = dict(os.environ, HOME=self._tmp)
        return subprocess.run(
            [sys.executable, "-c", self._py, counts, new_rows, alert],
            env=env, capture_output=True, text=True)

    def test_malformed_payload_is_not_persisted(self):
        proc = self._run("not json at all", "{also broken", " BAD_SPICE;")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self._state.exists())

    def test_valid_payload_is_persisted(self):
        counts = json.dumps([{"tbl": "book_meta", "n": 52},
                             {"tbl": "book_characters", "n": 10}])
        new_rows = json.dumps([{"k": "claims", "n": 3}])
        proc = self._run(counts, new_rows)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        hist = json.loads(self._state.read_text(encoding="utf-8"))
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["counts"],
                         {"book_meta": 52, "book_characters": 10})
        self.assertEqual(hist[0]["new_rows"], {"claims": 3})


# ----------------------------------------------------------------------
# F23(e) — failed trope lookup is an error, not a proposal;
#          failed verify read is a DB failure, not an empty DB
# ----------------------------------------------------------------------

class TestTropeLookupFailure(_WriteBase):
    def test_lookup_db_error_raises(self):
        pp.sb = lambda *a, **k: None
        with self.assertRaises(pp.TropeLookupError):
            pp._trope_lookup("Enemies to Lovers")

    def test_lookup_not_found_returns_none(self):
        pp.sb = lambda *a, **k: []
        self.assertIsNone(pp._trope_lookup("Enemies to Lovers"))

    def test_lookup_found_returns_id(self):
        pp.sb = lambda *a, **k: [{"id": "trope-1"}]
        self.assertEqual(pp._trope_lookup("Enemies to Lovers"), "trope-1")

    def test_failed_lookup_is_not_diverted_to_unmatched(self):
        pp.sb = lambda *a, **k: None
        rows, unmatched, lookup_failed = pp._build_trope_rows(
            1, _result(tropes=["Mystery Trope"]), {}, None, "", set())
        self.assertEqual(rows, [])
        self.assertEqual(unmatched, [])
        self.assertEqual(lookup_failed, ["Mystery Trope"])

    def test_write_counts_lookup_failure_as_error_not_proposal(self):
        fake = self.install(_StatefulSB(fail_gets={"tropes"}))
        errors = pp.write_claims(
            1, _result(tropes=["Mystery Trope"]))
        self.assertGreaterEqual(errors, 1)
        self.assertEqual(fake.tables.get("trope_proposals"), [])


class TestVerifyFailedRead(unittest.TestCase):
    def setUp(self):
        self._old_sb = pp.sb
        self._old_resolve = pp.resolve_work
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self._preview = str(Path(self._tmp) / "p.json")
        Path(self._preview).write_text(json.dumps({
            "title": "Verify Book", "isbn": "9780000000001",
            "characters": [], "tropes": [], "triggers": []}),
            encoding="utf-8")
        pp.resolve_work = lambda *a, **k: ("abcdef123456", "isbn")

    def tearDown(self):
        pp.sb = self._old_sb
        pp.resolve_work = self._old_resolve

    def test_failed_read_reports_db_failure(self):
        pp.sb = lambda *a, **k: None
        _, out = _capture(pp.cmd_verify, self._preview)
        self.assertIn("DB read failed", out)

    def test_empty_db_is_not_a_failure(self):
        pp.sb = lambda *a, **k: []
        _, out = _capture(pp.cmd_verify, self._preview)
        self.assertNotIn("DB read failed", out)
        self.assertIn("Verifying", out)


if __name__ == "__main__":
    unittest.main()
