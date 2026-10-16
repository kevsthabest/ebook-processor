"""Replace-wholesale claim writes (approved 2026-10-14).

Reprocessing a work replaces the claims the processor previously wrote:
  * only processor-sourced rows are deleted (per-table source_type marker
    is a condition on every DELETE);
  * contributor/human rows survive untouched;
  * human rejections (status='rejected') are never deleted and never
    resurrected — the new run dedupes against them;
  * denylisted tropes stay filtered from proposals;
  * preview planning (plan_replace) is read-only: zero mutating calls;
  * a fresh work (no existing rows) behaves like the old insert path.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from urllib.parse import unquote

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _StatefulSB:
    """In-memory Supabase stand-in implementing the PostgREST subset the
    write path uses: GET/POST/PATCH/DELETE with eq/neq/ilike filters."""

    def __init__(self, tables=None, fail_deletes=()):
        self.tables = {t: [dict(r) for r in rows]
                       for t, rows in (tables or {}).items()}
        self.calls = []
        self.fail_deletes = set(fail_deletes)
        self._next_id = 1000

    def _match(self, row, params):
        for part in (params or "").lstrip("?").split("&"):
            if not part or "=" not in part:
                continue
            key, val = part.split("=", 1)
            if key in ("select", "limit", "on_conflict", "order"):
                continue
            if key == "or":
                # or=(col.op.val,col.op.val,...) — any condition may match.
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
        # PostgREST/SQL semantics: a comparison against NULL never
        # matches (not even neq) — only is.null tests for NULL.
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
        return False  # unknown operator matches nothing

    def __call__(self, table, params=None, method="GET", data=None, prefer=None):
        self.calls.append((table, method, params or "", data))
        rows = self.tables.setdefault(table, [])
        if method == "GET":
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
            self.tables[table] = [r for r in rows if not self._match(r, params)]
            return [dict(r) for r in gone]
        return []

    def mutating_calls(self):
        return [c for c in self.calls if c[1] in ("POST", "PATCH", "DELETE")]

    def deletes_for(self, table):
        return [c for c in self.calls if c[0] == table and c[1] == "DELETE"]


def _result(**over):
    r = {"title": "Test Book", "isbn": "9780000000001", "tropes": [],
         "trope_confidence": {}, "trope_catalog_map": {}, "trope_chapters": {},
         "triggers": [], "characters": [], "relationships": [], "quotes": []}
    r.update(over)
    return r


def _trig(warning, conf=0.8):
    return {"warning": warning, "severity": "mentioned",
            "severity_claimed": "mentioned", "chapters": ["A"],
            "chapter_count": 1, "frequency": 0.5, "prominence": "low",
            "evidence": [{"quote": "q", "chapter": "A"}], "confidence": conf}


class _ReplaceBase(unittest.TestCase):
    def setUp(self):
        self._old_sb = pp.sb
        self._old_cfg = {k: pp.CONFIG.get(k)
                         for k in ("supabase_url", "supabase_key")}

    def tearDown(self):
        pp.sb = self._old_sb
        for k, v in self._old_cfg.items():
            if v is None:
                pp.CONFIG.pop(k, None)
            else:
                pp.CONFIG[k] = v

    def install(self, fake):
        pp.sb = fake
        self.fake = fake
        return fake


class TestReplaceDeletesOnlyProcessorRows(_ReplaceBase):
    def test_reprocess_replaces_processor_claims(self):
        fake = self.install(_StatefulSB({
            "book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-old",
                 "status": "candidate", "source_type": "ai"},
                {"id": 2, "work_id": 1, "trope_id": "cat-a",
                 "status": "candidate", "source_type": "ai"},
                {"id": 3, "work_id": 2, "trope_id": "cat-other-work",
                 "status": "candidate", "source_type": "ai"},
            ],
            "book_trigger_claims": [
                {"id": 4, "work_id": 1, "warning": "old warning",
                 "status": "candidate", "source_type": "ai"},
            ],
            "book_characters": [
                {"id": 5, "work_id": 1, "name": "Old Char",
                 "status": "candidate", "source_type": "ai"},
            ],
            "book_quotes": [
                {"id": 6, "work_id": 1, "quote": "old pipeline quote",
                 "source_type": "pipeline"},
            ],
        }))
        res = _result(
            tropes=["Mind Game"],
            trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]},
            triggers=[_trig("murder")],
            characters=[{"name": "Hero", "role": "protagonist"}],
            quotes=[{"text": "a brand new quote", "chapter": "Ch 1"}],
        )
        errors = pp.write_claims(1, res, trope_mappings={"Mind Game": "cat-a"})
        self.assertEqual(errors, 0)

        # Stale processor rows are gone; the re-detected claim exists once.
        tropes = fake.tables["book_trope_claims"]
        self.assertEqual([r["trope_id"] for r in tropes if r["work_id"] == 1],
                         ["cat-a"])
        # Another work's rows are never touched.
        self.assertEqual([r["trope_id"] for r in tropes if r["work_id"] == 2],
                         ["cat-other-work"])
        self.assertEqual([r["warning"] for r in fake.tables["book_trigger_claims"]],
                         ["murder"])
        self.assertEqual([r["name"] for r in fake.tables["book_characters"]],
                         ["Hero"])
        self.assertEqual([r["quote"] for r in fake.tables["book_quotes"]],
                         ["a brand new quote"])

        # Every DELETE carries the processor scope: work + source_type
        # (+ status=neq.rejected on the claims tables).
        for tbl in ("book_trope_claims", "book_trigger_claims",
                    "book_characters", "book_quotes"):
            dels = fake.deletes_for(tbl)
            self.assertEqual(len(dels), 1, tbl)
            params = dels[0][2]
            self.assertIn("work_id=eq.1", params)
            self.assertIn("source_type=eq.", params)
            if tbl != "book_quotes":
                self.assertIn("status=neq.rejected", params)

    def test_delete_failure_blocks_insert_and_counts_error(self):
        fake = self.install(_StatefulSB(
            {"book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-old",
                 "status": "candidate", "source_type": "ai"}]},
            fail_deletes={"book_trope_claims"}))
        res = _result(tropes=["Mind Game"],
                      trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]})
        errors = pp.write_claims(1, res, trope_mappings={"Mind Game": "cat-a"})
        self.assertGreaterEqual(errors, 1)
        # Old rows remain and nothing was inserted on top of them.
        self.assertEqual([r["trope_id"] for r in fake.tables["book_trope_claims"]],
                         ["cat-old"])
        self.assertFalse([c for c in fake.calls
                          if c[0] == "book_trope_claims" and c[1] == "POST"])


class TestContributorRowsSurvive(_ReplaceBase):
    def test_contributor_rows_untouched_and_deduped(self):
        fake = self.install(_StatefulSB({
            "book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-h",
                 "status": "candidate", "source_type": "user"}],
            "book_trigger_claims": [
                {"id": 2, "work_id": 1, "warning": "murder",
                 "status": "candidate", "source_type": "user"}],
            "book_characters": [
                {"id": 3, "work_id": 1, "name": "Hero", "role": "supporting",
                 "status": "candidate", "source_type": "user",
                 "appearance": None, "vitality": None, "aliases": [],
                 "first_appearance_chapter": None}],
            "book_quotes": [
                {"id": 4, "work_id": 1, "quote": "a human-added quote",
                 "source_type": "user"}],
        }))
        res = _result(
            tropes=["Human Trope", "Mind Game"],
            trope_chapters={pp._tnorm("Human Trope"): ["Ch 1"],
                            pp._tnorm("Mind Game"): ["Ch 2"]},
            triggers=[_trig("murder")],
            characters=[{"name": "Hero", "role": "protagonist",
                         "appearance": "tall", "status": "alive"}],
            quotes=[{"text": "a human-added quote", "chapter": "Ch 1"},
                    {"text": "fresh quote", "chapter": "Ch 2"}],
        )
        errors = pp.write_claims(
            1, res, trope_mappings={"Human Trope": "cat-h", "Mind Game": "cat-a"})
        self.assertEqual(errors, 0)

        # Contributor trope claim: still exactly one row, the original.
        rows = [r for r in fake.tables["book_trope_claims"]
                if r["trope_id"] == "cat-h"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        # The genuinely new trope was inserted alongside it.
        self.assertEqual(
            sorted(r["trope_id"] for r in fake.tables["book_trope_claims"]),
            ["cat-a", "cat-h"])
        # Contributor trigger not duplicated.
        self.assertEqual(len(fake.tables["book_trigger_claims"]), 1)
        self.assertEqual(fake.tables["book_trigger_claims"][0]["id"], 2)
        # Contributor character not duplicated; legacy NULL backfill still
        # applies to the surviving row.
        chars = fake.tables["book_characters"]
        self.assertEqual(len(chars), 1)
        self.assertEqual(chars[0]["id"], 3)
        self.assertEqual(chars[0]["appearance"], "tall")
        # Contributor quote survives; only the fresh quote was added.
        self.assertEqual(
            sorted(r["quote"] for r in fake.tables["book_quotes"]),
            ["a human-added quote", "fresh quote"])


class TestRejectionPreservation(_ReplaceBase):
    def test_rejected_claims_not_deleted_not_resurrected(self):
        fake = self.install(_StatefulSB({
            "book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-r",
                 "status": "rejected", "source_type": "ai"},
                {"id": 2, "work_id": 1, "trope_id": "cat-old",
                 "status": "candidate", "source_type": "ai"}],
            "book_trigger_claims": [
                {"id": 3, "work_id": 1, "warning": "murder",
                 "status": "rejected", "source_type": "ai"}],
            "book_characters": [
                {"id": 4, "work_id": 1, "name": "Rejected Char",
                 "status": "rejected", "source_type": "ai",
                 "appearance": "x", "vitality": "alive", "aliases": ["RC"],
                 "first_appearance_chapter": 1}],
        }))
        res = _result(
            tropes=["Dark Secret"],
            trope_chapters={pp._tnorm("Dark Secret"): ["Ch 1"]},
            triggers=[_trig("murder")],
            characters=[{"name": "Rejected Char", "role": "minor"}],
        )
        errors = pp.write_claims(1, res, trope_mappings={"Dark Secret": "cat-r"})
        self.assertEqual(errors, 0)

        # The rejected trope row is the ONLY cat-r row, still rejected —
        # the new run did not insert a fresh candidate over it.
        rows = [r for r in fake.tables["book_trope_claims"]
                if r["trope_id"] == "cat-r"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[0]["status"], "rejected")
        # Un-rejected stale processor row WAS replaced (deleted, not
        # re-detected, so simply gone).
        self.assertNotIn("cat-old",
                         [r["trope_id"] for r in fake.tables["book_trope_claims"]])
        # Rejected trigger and character likewise survive alone.
        trigs = fake.tables["book_trigger_claims"]
        self.assertEqual(len(trigs), 1)
        self.assertEqual(trigs[0]["status"], "rejected")
        chars = fake.tables["book_characters"]
        self.assertEqual(len(chars), 1)
        self.assertEqual(chars[0]["status"], "rejected")


class TestDenylistStillFiltered(_ReplaceBase):
    def test_static_denylist_trope_not_proposed(self):
        fake = self.install(_StatefulSB({}))
        res = _result(
            tropes=["Thriller", "Zqx Wobble"],
            trope_confidence={pp._tnorm("Thriller"): 0.95,
                              pp._tnorm("Zqx Wobble"): 0.95},
        )
        errors = pp.write_claims(1, res)  # no mappings -> both unmapped
        self.assertEqual(errors, 0)
        props = fake.tables.get("trope_proposals", [])
        self.assertEqual([r["name_key"] for r in props],
                         [pp._tnorm("Zqx Wobble")])

    def test_learned_denylist_trope_not_proposed(self):
        fake = self.install(_StatefulSB({}))

        class _StubLearn:
            def is_trope_denylisted(self, key):
                return key == pp._trej_key("Learned Banned Trope")

        old_learn = pp._learn
        pp._learn = lambda: _StubLearn()
        try:
            res = _result(
                tropes=["Learned Banned Trope"],
                trope_confidence={pp._tnorm("Learned Banned Trope"): 0.95},
            )
            errors = pp.write_claims(1, res)
        finally:
            pp._learn = old_learn
        self.assertEqual(errors, 0)
        self.assertEqual(fake.tables.get("trope_proposals", []), [])


class TestPreviewPlanReadOnly(_ReplaceBase):
    def _seed(self):
        return _StatefulSB({
            "book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-a",
                 "status": "candidate", "source_type": "ai"},
                {"id": 2, "work_id": 1, "trope_id": "cat-r",
                 "status": "rejected", "source_type": "ai"},
                {"id": 3, "work_id": 1, "trope_id": "cat-h",
                 "status": "candidate", "source_type": "user"}],
            "book_trigger_claims": [
                {"id": 4, "work_id": 1, "warning": "murder",
                 "status": "candidate", "source_type": "ai"}],
            "book_characters": [
                {"id": 5, "work_id": 1, "name": "Old Char",
                 "status": "candidate", "source_type": "ai"},
                {"id": 6, "work_id": 1, "name": "Rejected Char",
                 "status": "rejected", "source_type": "ai"}],
            "book_quotes": [
                {"id": 7, "work_id": 1, "quote": "old pipeline quote",
                 "source_type": "pipeline"}],
        })

    def _plan_result(self):
        return _result(
            tropes=["Mind Game", "Dark Secret", "Human Trope", "Fresh Trope"],
            # The two tropes expected to insert carry recorded
            # support (the evidence floor holds support-less claims);
            # the rejected/contributor tropes are skipped before the
            # floor ever sees them.
            trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"],
                            pp._tnorm("Fresh Trope"): ["Ch 1"]},
            triggers=[_trig("murder"), _trig("violence")],
            characters=[{"name": "Hero", "role": "protagonist"},
                        {"name": "Rejected Char", "role": "minor"}],
            quotes=[{"text": "new quote", "chapter": "Ch 1"}],
        )

    _MAP = {"Mind Game": "cat-a", "Dark Secret": "cat-r",
            "Human Trope": "cat-h", "Fresh Trope": "cat-f"}

    def test_plan_counts_and_zero_writes(self):
        fake = self.install(self._seed())
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "test-key"
        plan = pp.plan_replace(1, self._plan_result(), trope_mappings=self._MAP)
        self.assertIsNotNone(plan)
        self.assertTrue(plan)
        # Deletes: only processor, non-rejected rows (1 per claims table).
        self.assertEqual(plan["would_delete"]["book_trope_claims"], 1)
        self.assertEqual(plan["would_delete"]["book_trigger_claims"], 1)
        self.assertEqual(plan["would_delete"]["book_characters"], 1)
        self.assertEqual(plan["would_delete"]["book_quotes"], 1)
        # Inserts: cat-a replaced + cat-f fresh; rejected (cat-r) and
        # contributor (cat-h) matches are NOT re-inserted.
        self.assertEqual(plan["would_insert"]["tropes"], 2)
        self.assertEqual(plan["would_insert"]["triggers"], 2)
        self.assertEqual(plan["would_insert"]["characters"], 1)
        self.assertEqual(plan["would_insert"]["quotes"], 1)
        # Rejections preserved: rejected trope + rejected character.
        self.assertEqual(plan["rejections_preserved"], 2)
        # HARD RULE: preview planning performs no writes of any kind.
        self.assertEqual(fake.mutating_calls(), [])
        # And the seeded state is byte-identical afterwards.
        self.assertEqual(len(fake.tables["book_trope_claims"]), 3)

    def test_print_replace_plan_logs_counts(self):
        fake = self.install(self._seed())
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "test-key"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pp.print_replace_plan(1, self._plan_result(),
                                  trope_mappings=self._MAP)
        out = buf.getvalue()
        self.assertIn("would delete 4 processor claims", out)
        self.assertIn("insert 6 new rows", out)
        self.assertIn("preserve 2 rejections", out)
        self.assertEqual(fake.mutating_calls(), [])


class TestFreshWorkInsertOnly(_ReplaceBase):
    def test_fresh_work_behaves_like_insert_path(self):
        fake = self.install(_StatefulSB({}))
        res = _result(
            tropes=["Mind Game"],
            trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]},
            triggers=[_trig("murder")],
            characters=[{"name": "Hero", "role": "protagonist"}],
            quotes=[{"text": "a quote", "chapter": "Ch 1"}],
        )
        errors = pp.write_claims(1, res, trope_mappings={"Mind Game": "cat-a"})
        self.assertEqual(errors, 0)
        # No existing rows -> no DELETEs at all.
        self.assertEqual([c for c in fake.calls if c[1] == "DELETE"], [])
        self.assertEqual(len(fake.tables["book_trope_claims"]), 1)
        self.assertEqual(len(fake.tables["book_trigger_claims"]), 1)
        self.assertEqual(len(fake.tables["book_characters"]), 1)
        self.assertEqual(len(fake.tables["book_quotes"]), 1)


class TestProposalScopingStructural(_ReplaceBase):
    """F1: the trope_proposals replace must be scoped to the processor's
    OWN rows structurally (proposed_by), not incidentally via key
    formats. App suggestions carry a real user uuid in proposed_by;
    processor rows carry NULL (or the configured proposer_user_id)."""

    _APP_UID = "aaaaaaaa-1111-2222-3333-444444444444"

    def test_app_proposals_survive_reprocess(self):
        fake = self.install(_StatefulSB({
            "trope_proposals": [
                # App-shaped: prefixed key, foreign proposer.
                {"id": 1, "name": "App Suggested", "name_key": "app suggested",
                 "book_key": "isbn:9780000000001",
                 "proposed_by": self._APP_UID, "status": "pending"},
                # Same bare book_key as the processor + foreign proposer:
                # the future key-alignment hazard — must survive too.
                {"id": 2, "name": "Aligned Key", "name_key": "aligned key",
                 "book_key": "9780000000001",
                 "proposed_by": self._APP_UID, "status": "pending"},
                # The processor's own pending proposal — replaced.
                {"id": 3, "name": "Stale Own Trope",
                 "name_key": pp._tnorm("Stale Own Trope"),
                 "book_key": "9780000000001",
                 "proposed_by": None, "status": "pending"},
            ],
        }))
        res = _result(
            tropes=["Stale Own Trope", "Fresh Own Trope"],
            trope_confidence={pp._tnorm("Stale Own Trope"): 0.95,
                              pp._tnorm("Fresh Own Trope"): 0.95},
        )
        errors = pp.write_claims(1, res)  # no mappings -> proposals path
        self.assertEqual(errors, 0)

        props = fake.tables["trope_proposals"]
        # Both app rows survive untouched, ids and all.
        self.assertEqual(
            sorted(r["id"] for r in props
                   if r.get("proposed_by") == self._APP_UID),
            [1, 2])
        # Own stale proposal was replaced: exactly one row, a new id.
        own = [r for r in props
               if r["name_key"] == pp._tnorm("Stale Own Trope")]
        self.assertEqual(len(own), 1)
        self.assertNotEqual(own[0]["id"], 3)
        # The fresh trope was proposed alongside.
        self.assertEqual(
            len([r for r in props
                 if r["name_key"] == pp._tnorm("Fresh Own Trope")]), 1)
        # The DELETE itself carries the structural proposer scope.
        dels = fake.deletes_for("trope_proposals")
        self.assertEqual(len(dels), 1)
        self.assertIn("proposed_by=is.null", dels[0][2])

    def test_configured_proposer_scopes_to_own_rows(self):
        old_uid = pp.CONFIG.get("proposer_user_id")
        pp.CONFIG["proposer_user_id"] = "proc-user-uuid"
        try:
            fake = self.install(_StatefulSB({
                "trope_proposals": [
                    # Own row under the configured proposer — replaced.
                    {"id": 1, "name": "Stale Own Trope",
                     "name_key": pp._tnorm("Stale Own Trope"),
                     "book_key": "9780000000001",
                     "proposed_by": "proc-user-uuid", "status": "pending"},
                    # Legacy own row (NULL proposer) — also replaced.
                    {"id": 2, "name": "Legacy Own Trope",
                     "name_key": pp._tnorm("Legacy Own Trope"),
                     "book_key": "9780000000001",
                     "proposed_by": None, "status": "pending"},
                    # Another user's suggestion, same book_key — survives.
                    {"id": 3, "name": "Foreign Trope",
                     "name_key": pp._tnorm("Foreign Trope"),
                     "book_key": "9780000000001",
                     "proposed_by": "other-user-uuid", "status": "pending"},
                ],
            }))
            res = _result(
                tropes=["Stale Own Trope"],
                trope_confidence={pp._tnorm("Stale Own Trope"): 0.95},
            )
            errors = pp.write_claims(1, res)
            self.assertEqual(errors, 0)

            props = fake.tables["trope_proposals"]
            # Foreign suggestion survives untouched.
            foreign = [r for r in props
                       if r.get("proposed_by") == "other-user-uuid"]
            self.assertEqual([r["id"] for r in foreign], [3])
            # Legacy own row is gone and was not re-proposed.
            self.assertNotIn(pp._tnorm("Legacy Own Trope"),
                             [r["name_key"] for r in props])
            # Stale own trope re-proposed exactly once, under the
            # configured proposer id.
            own = [r for r in props
                   if r["name_key"] == pp._tnorm("Stale Own Trope")]
            self.assertEqual(len(own), 1)
            self.assertEqual(own[0].get("proposed_by"), "proc-user-uuid")
            # The DELETE carries the or-scope over both own shapes.
            dels = fake.deletes_for("trope_proposals")
            self.assertEqual(len(dels), 1)
            self.assertIn(
                "or=(proposed_by.is.null,proposed_by.eq.proc-user-uuid)",
                dels[0][2])
        finally:
            if old_uid is None:
                pp.CONFIG.pop("proposer_user_id", None)
            else:
                pp.CONFIG["proposer_user_id"] = old_uid


class TestNullStatusPreserved(_ReplaceBase):
    """F2: partition and DELETE must agree about NULL-status rows.
    PostgREST status=neq.rejected never matches NULL, so a NULL-status
    processor row survives the DELETE — it must sit in the preserved
    set, or the re-proposed claim inserts a duplicate beside it."""

    def test_null_status_row_not_duplicated_on_reprocess(self):
        fake = self.install(_StatefulSB({
            "book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-a",
                 "status": None, "source_type": "ai"}],
        }))
        res = _result(tropes=["Mind Game"],
                      trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]})
        errors = pp.write_claims(1, res, trope_mappings={"Mind Game": "cat-a"})
        self.assertEqual(errors, 0)
        # The NULL-status row survives AND is not duplicated: the
        # re-detected claim dedupes against it.
        rows = [r for r in fake.tables["book_trope_claims"]
                if r["trope_id"] == "cat-a"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        # No DELETE was issued for a row the DELETE cannot match.
        self.assertEqual(fake.deletes_for("book_trope_claims"), [])

    def test_plan_would_delete_excludes_null_and_empty_status(self):
        fake = self.install(_StatefulSB({
            "book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-a",
                 "status": None, "source_type": "ai"},
                {"id": 2, "work_id": 1, "trope_id": "cat-b",
                 "status": "", "source_type": "ai"}],
        }))
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "test-key"
        res = _result(tropes=["Mind Game"],
                      trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]})
        plan = pp.plan_replace(1, res, trope_mappings={"Mind Game": "cat-a"})
        self.assertTrue(plan)
        # Neither NULL- nor empty-status rows are deletable, so the
        # plan's would-delete count is truthful: zero.
        self.assertEqual(plan["would_delete"]["book_trope_claims"], 0)
        # And the re-detected trope is not planned for re-insert either.
        self.assertEqual(plan["would_insert"]["tropes"], 0)
        self.assertEqual(fake.mutating_calls(), [])


class TestConfirmedPreservation(_ReplaceBase):
    """Decision (A), approved 2026-10-14: a claim a human reviewed and
    confirmed is curation state exactly like a rejection, with the
    opposite sign. On reprocess a status='confirmed' processor row must
    survive untouched — never deleted, and the new run must not insert
    a fresh 'candidate' copy of the same claim over it."""

    def test_confirmed_claims_not_deleted_not_reinserted(self):
        fake = self.install(_StatefulSB({
            "book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-c",
                 "status": "confirmed", "source_type": "ai"},
                {"id": 2, "work_id": 1, "trope_id": "cat-old",
                 "status": "candidate", "source_type": "ai"}],
            "book_trigger_claims": [
                {"id": 3, "work_id": 1, "warning": "murder",
                 "status": "confirmed", "source_type": "ai"}],
            "book_characters": [
                {"id": 4, "work_id": 1, "name": "Confirmed Char",
                 "status": "confirmed", "source_type": "ai",
                 "appearance": "x", "vitality": "alive", "aliases": ["CC"],
                 "first_appearance_chapter": 1}],
        }))
        res = _result(
            tropes=["Mind Game"],
            trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]},
            triggers=[_trig("murder")],
            characters=[{"name": "Confirmed Char", "role": "minor"}],
        )
        errors = pp.write_claims(1, res, trope_mappings={"Mind Game": "cat-c"})
        self.assertEqual(errors, 0)

        # The confirmed trope row is the ONLY cat-c row, still confirmed —
        # not deleted, not duplicated by a fresh candidate.
        rows = [r for r in fake.tables["book_trope_claims"]
                if r["trope_id"] == "cat-c"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[0]["status"], "confirmed")
        # An un-curated stale processor row WAS replaced as before.
        self.assertNotIn("cat-old",
                         [r["trope_id"] for r in fake.tables["book_trope_claims"]])
        # Confirmed trigger and character likewise survive alone.
        trigs = fake.tables["book_trigger_claims"]
        self.assertEqual(len(trigs), 1)
        self.assertEqual(trigs[0]["id"], 3)
        self.assertEqual(trigs[0]["status"], "confirmed")
        chars = fake.tables["book_characters"]
        self.assertEqual(len(chars), 1)
        self.assertEqual(chars[0]["id"], 4)
        self.assertEqual(chars[0]["status"], "confirmed")
        # Every claims-table DELETE excludes confirmed alongside rejected.
        for tbl in ("book_trope_claims", "book_trigger_claims",
                    "book_characters"):
            for _t, _m, params, _d in fake.deletes_for(tbl):
                self.assertIn("status=neq.rejected", params)
                self.assertIn("status=neq.confirmed", params)

    def test_plan_counts_treat_confirmed_as_preserved(self):
        fake = self.install(_StatefulSB({
            "book_trope_claims": [
                {"id": 1, "work_id": 1, "trope_id": "cat-c",
                 "status": "confirmed", "source_type": "ai"},
                {"id": 2, "work_id": 1, "trope_id": "cat-old",
                 "status": "candidate", "source_type": "ai"}],
            "book_trigger_claims": [
                {"id": 3, "work_id": 1, "warning": "murder",
                 "status": "confirmed", "source_type": "ai"}],
            "book_characters": [
                {"id": 4, "work_id": 1, "name": "Confirmed Char",
                 "status": "confirmed", "source_type": "ai"}],
        }))
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "test-key"
        res = _result(
            tropes=["Mind Game"],
            trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]},
            triggers=[_trig("murder")],
            characters=[{"name": "Confirmed Char", "role": "minor"}],
        )
        plan = pp.plan_replace(1, res, trope_mappings={"Mind Game": "cat-c"})
        self.assertTrue(plan)
        # Only the un-curated candidate row would be deleted; confirmed
        # rows are preserved on every claims table.
        self.assertEqual(plan["would_delete"]["book_trope_claims"], 1)
        self.assertEqual(plan["would_delete"]["book_trigger_claims"], 0)
        self.assertEqual(plan["would_delete"]["book_characters"], 0)
        # Re-detected confirmed claims are NOT planned for re-insert.
        self.assertEqual(plan["would_insert"]["tropes"], 0)
        self.assertEqual(plan["would_insert"]["triggers"], 0)
        self.assertEqual(plan["would_insert"]["characters"], 0)
        # Confirmed rows are preserved, but they are not rejections and
        # must not inflate the rejections-preserved count.
        self.assertEqual(plan["rejections_preserved"], 0)
        self.assertEqual(fake.mutating_calls(), [])


if __name__ == "__main__":
    unittest.main()
