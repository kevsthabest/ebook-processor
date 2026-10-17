"""Sweep Batch 1 — data-loss fixes (codebase sweep of master @ 2d8a7f4).

One test class per finding, each written against the defect first:

- F6: a failed book_meta GET (sb() -> None) was treated as "no existing
  data" and the upsert overwrote the shared per-ISBN blob with only this
  run's updates. A failed GET must abort the book_meta update (no POST)
  and count an error; an empty-but-successful GET still merges normally.
- F3: dedupe_characters remapped every relationship to a bare
  {from, to, type} dict, so a dedupe run's write classified them all
  evidence-less and wrote none; merged characters also lost dropped
  members' aliases / appearances / first-appearance / status / POV.
- F5: --retry-failed seeded the FINAL roster (which already contains a
  B-failed chapter's Call A fold) but no checkpoint for that chapter, so
  the retried run re-ran _roster_update and double-counted appearances.
  The test drives the REAL cmd_retry_failed + process_file_v2 (only the
  LLM backend is faked, at the network boundary).
- F16: _partition_claims preserves ''-status processor rows, but the
  replace DELETE (status=neq.*) matches '' in PostgREST, so a re-detected
  ''-status claim was deleted and never re-inserted. The DELETE must
  exclude '' exactly like the partition does.
- F17: the quotes and proposals sections of write_claims only ran when
  the new run produced quotes / unmatched tropes, so a zero-yield
  reprocess left stale processor rows in place. Both sections must run
  the full replace sequence unconditionally.

Stdlib unittest only. No network, no Supabase, no real ebook files
(the F5 EPUB is a synthetic zip built in-test).
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


class _StatefulSB:
    """In-memory Supabase stand-in implementing the PostgREST subset the
    write path uses: GET/POST/PATCH/DELETE with eq/neq/ilike filters.
    (Pattern copied from tests/test_replace_wholesale.py, plus fail_gets
    so a test can make one table's GET return None like sb() does on
    error.)"""

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
        # PostgREST/SQL semantics: a comparison against NULL never
        # matches (not even neq) — only is.null tests for NULL. An
        # empty-string cell DOES match neq.<nonempty> and does NOT
        # match neq. (empty value) — this is the F16 seam.
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
            self.tables[table] = [r for r in rows if not self._match(r, params)]
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


# ----------------------------------------------------------------------
# F6 — book_meta: failed GET must not become an empty-merge overwrite
# ----------------------------------------------------------------------

class TestBookMetaFailedGet(_WriteBase):
    def test_failed_get_aborts_update_and_counts_error(self):
        fake = self.install(_StatefulSB(fail_gets={"book_meta"}))
        res = _result(povs=["Hero"])
        errors = pp.write_claims(1, res)
        # No POST may be issued: posting would replace the shared blob
        # with only this run's updates.
        self.assertEqual(fake.calls_for("book_meta", "POST"), [])
        self.assertGreaterEqual(errors, 1)

    def test_empty_successful_get_merges_normally(self):
        fake = self.install(_StatefulSB())
        res = _result(povs=["Hero"])
        errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)
        posts = fake.calls_for("book_meta", "POST")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][3]["isbn"], "9780000000001")
        self.assertEqual(posts[0][3]["data"], {"povs": ["Hero"]})

    def test_existing_data_is_merged_not_replaced(self):
        fake = self.install(_StatefulSB({
            "book_meta": [{"isbn": "9780000000001",
                           "data": {"existing_key": "keep"}}]}))
        res = _result(povs=["Hero"])
        errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)
        posts = fake.calls_for("book_meta", "POST")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][3]["data"],
                         {"existing_key": "keep", "povs": ["Hero"]})


# ----------------------------------------------------------------------
# F16 — ''-status processor rows: partition preserves, DELETE deleted
# ----------------------------------------------------------------------

class TestEmptyStatusClaimSurvives(_WriteBase):
    def test_empty_status_row_survives_mixed_section_replace(self):
        fake = self.install(_StatefulSB({
            "book_trope_claims": [
                # Genuinely deletable: makes the section DELETE fire.
                {"id": 1, "work_id": 1, "trope_id": "cat-old",
                 "status": "candidate", "source_type": "ai"},
                # ''-status processor row whose claim is re-detected.
                {"id": 2, "work_id": 1, "trope_id": "cat-a",
                 "status": "", "source_type": "ai"},
            ]}))
        res = _result(
            tropes=["Mind Game"],
            trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]})
        errors = pp.write_claims(1, res, trope_mappings={"Mind Game": "cat-a"})
        self.assertEqual(errors, 0)
        rows = [r for r in fake.tables["book_trope_claims"]
                if r["work_id"] == 1]
        # cat-old is replaced away; cat-a survives EXACTLY ONCE, as the
        # original row (id 2, '' status intact) — not deleted, and not
        # duplicated by a fresh candidate insert beside it.
        self.assertEqual([(r["id"], r["trope_id"], r["status"]) for r in rows],
                         [(2, "cat-a", "")])
        # The DELETE itself must carry the '' exclusion, in the same
        # repeated-neq style as the rejected/confirmed exclusions.
        dels = fake.calls_for("book_trope_claims", "DELETE")
        self.assertEqual(len(dels), 1)
        params = dels[0][2]
        self.assertTrue("status=neq.&" in params
                        or params.endswith("status=neq."), params)


# ----------------------------------------------------------------------
# F17 — quotes / proposals replace unconditionally
# ----------------------------------------------------------------------

class TestQuotesReplaceUnconditionally(_WriteBase):
    def test_zero_quote_reprocess_clears_own_stale_quotes(self):
        fake = self.install(_StatefulSB({
            "book_quotes": [
                {"id": 1, "work_id": 1, "quote": "stale pipeline quote",
                 "source_type": "pipeline"},
                {"id": 2, "work_id": 1, "quote": "human quote",
                 "source_type": "user"},
                {"id": 3, "work_id": 2, "quote": "other work quote",
                 "source_type": "pipeline"},
            ]}))
        errors = pp.write_claims(1, _result(quotes=[]))
        self.assertEqual(errors, 0)
        remaining = sorted(r["quote"] for r in fake.tables["book_quotes"]
                           if r["work_id"] == 1)
        # The stale processor quote is gone; the human row is untouched.
        self.assertEqual(remaining, ["human quote"])
        self.assertEqual([r["quote"] for r in fake.tables["book_quotes"]
                          if r["work_id"] == 2], ["other work quote"])


class TestProposalsReplaceUnconditionally(_WriteBase):
    def test_all_mapped_reprocess_clears_own_pending_proposals(self):
        isbn = "9780000000001"
        fake = self.install(_StatefulSB({
            "trope_proposals": [
                {"id": 1, "name_key": "half remembered trope",
                 "book_key": isbn, "status": "pending", "proposed_by": None},
                {"id": 2, "name_key": "reviewed trope",
                 "book_key": isbn, "status": "rejected", "proposed_by": None},
                {"id": 3, "name_key": "foreign suggestion",
                 "book_key": isbn, "status": "pending",
                 "proposed_by": "user-uuid-1"},
            ]}))
        res = _result(
            tropes=["Mind Game"],
            trope_chapters={pp._tnorm("Mind Game"): ["Ch 1"]})
        errors = pp.write_claims(1, res, trope_mappings={"Mind Game": "cat-a"})
        self.assertEqual(errors, 0)
        # Every trope mapped -> unmatched is empty, yet the work's own
        # pending proposals are still replaced away. Human-reviewed and
        # foreign-proposer rows are preserved exactly.
        remaining = sorted(r["id"] for r in fake.tables["trope_proposals"])
        self.assertEqual(remaining, [2, 3])


# ----------------------------------------------------------------------
# F3 — dedupe preserves relationship payloads and unions merged members
# ----------------------------------------------------------------------

def _char(name, **over):
    c = {"name": name, "role": "supporting", "description": "",
         "appearance": "", "status": "alive", "aliases": [],
         "unresolved_mentions": [], "appearances": 1, "mention_count": 1,
         "chapters_present": 1, "chapter_count": 1,
         "first_appearance_chapter": 1, "first_appearance": 1,
         "evidence": "", "is_pov": False, "confidence": 0.5}
    c.update(over)
    return c


class TestDedupePreservesRelationships(_WriteBase):
    def test_zero_cluster_dedupe_keeps_full_relationships(self):
        chars = [_char("Hero", confidence=0.9), _char("Mentor", confidence=0.8)]
        rel = {"from": "Hero", "to": "Mentor", "type": "mentor",
               "evidence": "Hero trained under Mentor for a decade.",
               "evidence_source": "extracted", "confidence": 0.9,
               "chapter": "Ch 1"}
        with mock.patch.object(pp, "embed_vectors",
                               return_value=[[1.0, 0.0], [0.0, 1.0]]):
            out_chars, out_rels, out_povs, report = pp.dedupe_characters(
                chars, [rel], ["Hero"])
        self.assertEqual(report, [])  # zero clusters merged
        # Every non-endpoint key survives the remap verbatim.
        self.assertEqual(out_rels, [rel])
        # And the write path therefore embeds the relationship (before
        # the fix _build_rel_map classified it evidence-less and the
        # character row shipped with no relationships at all).
        fake = self.install(_StatefulSB())
        res = _result(characters=out_chars, relationships=out_rels)
        errors = pp.write_claims(7, res)
        self.assertEqual(errors, 0)
        hero = [r for r in fake.tables["book_characters"]
                if r["name"] == "Hero"]
        self.assertEqual(len(hero), 1)
        self.assertEqual(hero[0]["relationships"], [{
            "to": "Mentor", "type": "mentor",
            "evidence": "Hero trained under Mentor for a decade.",
            "evidence_source": "extracted",
            # Added 2026-10-17 (2.13.20, spec §1.5): stored rows
            # carry curation state + provenance. This hand-built
            # claim was never judged, so review_required is the
            # honest state.
            "validation_state": "review_required",
            "source": "extracted"}])

    def test_merging_cluster_unions_member_fields(self):
        canon = _char(
            "Alice Smith", confidence=0.9, appearances=3, mention_count=3,
            chapters_present=2, chapter_count=2, aliases=["Al"],
            unresolved_mentions=["Alice"], first_appearance_chapter=2,
            first_appearance=2, status="alive", appearance="tall",
            description="short", evidence="ev-a")
        dropped = _char(
            "Alice  Smith", confidence=0.8, appearances=4, mention_count=4,
            chapters_present=3, chapter_count=3, aliases=["Ali"],
            unresolved_mentions=["Ms Smith"], first_appearance_chapter=1,
            first_appearance=1, status="dead",
            appearance="tall with a long silver braid",
            description="a much longer description of the same woman",
            evidence="ev-b: longer evidence quote")
        rel = {"from": "Alice  Smith", "to": "Mentor", "type": "friend",
               "evidence": "Alice and Mentor laughed together."}
        with mock.patch.object(pp, "embed_vectors",
                               return_value=[[1.0, 0.0], [1.0, 0.0]]):
            out_chars, out_rels, _povs, report = pp.dedupe_characters(
                [canon, dropped], [rel], [])
        self.assertEqual(len(out_chars), 1)
        self.assertEqual(report[0]["canonical"], "Alice Smith")
        m = out_chars[0]
        self.assertEqual(m["appearances"], 7)       # summed, roster-style
        self.assertEqual(m["mention_count"], 7)
        self.assertEqual(set(m["aliases"]),
                         {"Al", "Ali", "Alice  Smith"})  # union + dropped name
        self.assertEqual(set(m["unresolved_mentions"]), {"Alice", "Ms Smith"})
        self.assertEqual(m["first_appearance_chapter"], 1)  # earliest
        self.assertEqual(m["first_appearance"], 1)
        self.assertEqual(m["status"], "dead")  # sticky death survives merge
        self.assertEqual(m["appearance"], "tall with a long silver braid")
        self.assertEqual(m["description"],
                         "a much longer description of the same woman")
        self.assertEqual(m["confidence"], 0.9)
        # The dropped member's relationship is remapped to the canonical
        # name with its evidence intact.
        self.assertEqual(len(out_rels), 1)
        self.assertEqual(out_rels[0]["from"], "Alice Smith")
        self.assertEqual(out_rels[0]["to"], "Mentor")
        self.assertEqual(out_rels[0]["evidence"],
                         "Alice and Mentor laughed together.")

    def test_merging_cluster_unions_pov(self):
        canon = _char("Bob Jones", confidence=0.9, is_pov=False)
        dropped = _char("Bob  Jones", confidence=0.8, is_pov=True)
        with mock.patch.object(pp, "embed_vectors",
                               return_value=[[1.0, 0.0], [1.0, 0.0]]):
            out_chars, _rels, _povs, _report = pp.dedupe_characters(
                [canon, dropped], [], [])
        self.assertEqual(len(out_chars), 1)
        self.assertTrue(out_chars[0]["is_pov"])


# ----------------------------------------------------------------------
# F5 — --retry-failed must not re-fold a B-failed chapter's Call A
# ----------------------------------------------------------------------

_HERO_SENTENCE = "Test Hero drew a deep breath and stepped forward."
_FILLER = ("<p>The valley was quiet and the river ran cold and bright "
           "under a wide northern sky full of slow clouds.</p>")


def _xhtml(body):
    return ("<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.idpf.org/2007/xhtml'><head><title>t</title></head>"
            f"<body>{body}</body></html>")


def _make_epub(path, files):
    manifest = "\n".join(
        f'    <item id="i{n}" href="{name.split("/", 1)[1]}" media-type="application/xhtml+xml"/>'
        for n, name in enumerate(files) if name != "META-INF/container.xml")
    spine = "\n".join(
        f'    <itemref idref="i{n}"/>' for n, name in enumerate(files)
        if name != "META-INF/container.xml")
    opf = ("""<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='bid'>
  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
    <dc:title>Retry Test Book</dc:title>
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


class _RetryBackend:
    """Fake LLM boundary. Call A always sights the hero; Call B fails
    (unparseable output) for chapters carrying a fail marker."""

    def __init__(self, fail_b_markers=()):
        self.fail_b_markers = set(fail_b_markers)
        self.a_calls = []

    def __call__(self, prompt, chunk, i, n):
        if prompt == pp.PROMPT_V2_CONTENT:
            if any(m in chunk for m in self.fail_b_markers):
                return "not json {"
            return json.dumps({
                "summary": "A chapter summary.",
                "spice_level": 0, "triggers": {}, "trigger_evidence": {},
                "trope_candidates": ["Found Family"], "quotes": []})
        # Call A is identified by its prompts (the full characters
        # prompt, or the simple fallback). Other backend calls in the
        # pipeline (e.g. the trope confirmation gate, which passes an
        # empty chunk) are NOT Call A and must not be counted.
        _a_head = pp.PROMPT_V2_CHARACTERS.split("{")[0]
        if prompt.startswith(_a_head) \
                or prompt == pp.PROMPT_V2_CHARACTERS_SIMPLE:
            self.a_calls.append("BETA" if "BETA" in chunk else "ALPHA")
            return json.dumps({
                "characters": [{
                    "name": "Test Hero", "role": "protagonist", "aliases": [],
                    "description": "The hero of the valley.",
                    "evidence": _HERO_SENTENCE}],
                "relationships": [], "pov_character": "Test Hero"})
        return json.dumps({})


class TestRetryFailedDoesNotRefoldCallA(unittest.TestCase):
    def test_a_ok_b_fail_chapter_is_not_refolded(self):
        stem = "sweepb1retry"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        td = Path(tmp.name)
        epub = td / f"{stem}.epub"

        def chapter(heading, marker):
            return _xhtml(f"<h1>{heading}</h1><p>{marker}</p>"
                          f"<p>{_HERO_SENTENCE}</p>" + _FILLER * 22)
        _make_epub(epub, {
            "OEBPS/ch01.xhtml": chapter("Chapter 1", "ALPHA"),
            "OEBPS/ch02.xhtml": chapter("Chapter 2", "BETA"),
        })
        preview_dir = pp.SCRIPT_DIR / "preview"
        artefacts = [preview_dir / f"{stem}.json", preview_dir / f"{stem}.md",
                     preview_dir / f"{stem}_retried.json",
                     preview_dir / f"{stem}_retried.md"]
        for a in artefacts:
            self.addCleanup(a.unlink, missing_ok=True)
        self.addCleanup(lambda: shutil.rmtree(
            pp._checkpoint_dir(pp._checkpoint_book_key(epub)),
            ignore_errors=True))

        patches = lambda backend: (
            mock.patch.object(pp, "llm_ollama", backend),
            mock.patch.object(pp, "_lookup_series_openlibrary",
                              lambda *a, **k: (None, None)),
            mock.patch.object(pp, "resolve_work",
                              lambda *a, **k: (None, "test")),
            mock.patch.dict(pp.CONFIG, {"llm": "ollama"}))

        # Original run: chapter 2's Call B fails; its Call A fold is
        # already inside the persisted final roster (hero seen twice).
        backend1 = _RetryBackend(fail_b_markers={"BETA"})
        p1 = patches(backend1)
        for p in p1:
            p.start()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ok = pp.process_file_v2(epub, preview=True)
        finally:
            for p in p1:
                p.stop()
        self.assertTrue(ok)
        original = json.loads(artefacts[0].read_text(encoding="utf-8"))
        self.assertIsNotNone(original["chapter_data"]["chapters"]["1"]["b"])
        self.assertIsNone(original["chapter_data"]["chapters"]["2"]["b"])
        self.assertEqual(
            original["chapter_data"]["roster"]["test hero"]["appearances"], 2)

        # Retry through the REAL cmd_retry_failed -> process_file_v2
        # resume path (process_file_v2 is NOT mocked). cwd holds the
        # book file, which is how cmd_retry_failed locates it.
        backend2 = _RetryBackend()
        p2 = patches(backend2)
        for p in p2:
            p.start()
        _cwd = os.getcwd()
        os.chdir(td)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pp.cmd_retry_failed(str(artefacts[0]))
        finally:
            os.chdir(_cwd)
            for p in p2:
                p.stop()

        # Chapter 2's saved Call A was reused: the LLM never re-ran
        # Call A for it (chapter 1 was fully checkpointed too).
        self.assertEqual(backend2.a_calls, [])
        retried = json.loads(artefacts[2].read_text(encoding="utf-8"))
        # Call B now succeeds for chapter 2...
        self.assertIsNotNone(retried["chapter_data"]["chapters"]["2"]["b"])
        # ...and the hero's appearances are counted ONCE per chapter:
        # 2, not the double-folded 3.
        self.assertEqual(
            retried["chapter_data"]["roster"]["test hero"]["appearances"], 2)


if __name__ == "__main__":
    unittest.main()
