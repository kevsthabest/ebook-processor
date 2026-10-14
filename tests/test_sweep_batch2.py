"""Sweep batch 2 — crash-surface fixes (codebase sweep 2026-10-14,
report-2d8a7f4, Findings 13, 14, 10, 18, 20, 21).

F13: checkpoint restore coerced only PRESENT roster fields; an entry
from an older roster.json (or a retry-failed seed from an older
preview) missing a core field KeyErrored in _merge_roster_entries /
v2_reduce. Fix: schema completion at the roster-ingesting seams —
absent core fields get their canonical empty defaults; present values
are never altered.

F14: cmd_push_preview crashed mid-push on older/partial previews
(builders direct-index sections and per-item keys), after earlier
sections had already been replaced. Fix: validate the required shape
up front; refuse with zero writes. Only the two semantically safe
backfills (trope_catalog_map / trope_confidence) are kept.

F10: --dedupe with an unavailable embedding endpoint discarded the
whole book after full extraction. Fix: the embedding step degrades —
un-deduped results are written with a loud warning; genuine dedupe
errors still raise.

F18: --rename-no-isbn renamed the source file even in preview/dry-run
mode. Fix: the rename is mode-aware; preview/dry-run prints what
WOULD happen and touches nothing.

F20: process_file_v2's result dict forwarded none of the reduce's
chapter_spice / chapters_successful / trigger_chapters / spice_peak*
/ avg_dialogue_ratio, so --recalibrate-spice returned no_data for
every real preview, the summary printed "(no spicy content)" for a
spice-4 book, and save_preview silently omitted the dialogue line.
Fix: forward the fields; "(no spicy content)" only when true.

F21: --chunks subset runs produced an all-zero chapter_spice: the
array was bounded by the subset count while its keys are original
chapter indices. Fix: bound by the book's true chapter count.

Stdlib unittest only. No network, no Supabase, no real ebook files
(tiny EPUBs are built in temp dirs, LLM faked at the backend seam).
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class _ConfigGuard(unittest.TestCase):
    """Snapshot/restore the module CONFIG dict in place."""

    def setUp(self):
        self._config = dict(pp.CONFIG)

    def tearDown(self):
        pp.CONFIG.clear()
        pp.CONFIG.update(self._config)


# ----------------------------------------------------------------------
# F13 — roster field absence
# ----------------------------------------------------------------------

_CORE_FIELDS = ("name", "aliases", "alias_keys", "primary_keys",
                "_generic_keys", "appearances", "last_seen", "chapters",
                "roles", "descriptions")


def _producer_entry(name="Violet Sorrengail", key="violet sorrengail"):
    """One roster entry in the exact shape _roster_update creates and
    maintains, every core + optional typed field populated."""
    return {
        "name": name,
        "aliases": {"Violence"},
        "alias_keys": {key, "violence"},
        "primary_keys": {key},
        "_generic_keys": {"i"},
        "appearances": 12, "last_seen": 40,
        "chapters": {3, 40},
        "roles": Counter({"protagonist": 10}),
        "statuses": Counter({"alive": 10}),
        "descriptions": [(3, "A description.", True)],
        "appearances_desc": ["Silver hair."],
        "death_reports": [40],
        "alive_reports": {3},
        "unresolved_mentions": {"Vi"},
        "evidence": "I am Violet.",
    }


def _ch(idx, label=None, text="x " * 200):
    return {"index": idx, "label": label or f"Ch {idx}", "text": text}


class _CheckpointDirCase(_ConfigGuard):
    """Redirect the checkpoint dir to a temp location (same pattern as
    tests/test_checkpoint_restore_types.py)."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp()
        self._orig_dir = pp._checkpoint_dir
        tmp = self._tmp
        pp._checkpoint_dir = lambda bk, _t=tmp: Path(_t) / bk
        orig = self._orig_dir
        self.addCleanup(lambda: setattr(pp, "_checkpoint_dir", orig))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

    def _roundtrip(self, roster):
        pp._checkpoint_save_meta("bk", 3)
        pp._checkpoint_save_roster("bk", roster)
        _meta, loaded, _chapters = pp._checkpoint_load("bk")
        return loaded


class TestRosterFieldAbsence(_CheckpointDirCase):
    def _pair(self):
        keep = _producer_entry()
        drop = _producer_entry("Violet", "violet")
        drop["alias_keys"] = {"violet"}
        drop["primary_keys"] = {"violet"}
        return {"violet sorrengail": keep, "violet": drop}

    def test_merge_survives_each_core_field_missing_from_keeper(self):
        for field in _CORE_FIELDS:
            with self.subTest(field=field):
                roster = self._pair()
                del roster["violet sorrengail"][field]
                loaded = self._roundtrip(roster)
                # Must not raise (pre-fix: KeyError on descriptions,
                # chapters, roles, alias_keys, aliases, primary_keys).
                pp._merge_roster_entries(loaded, "violet sorrengail",
                                         "violet")
                self.assertNotIn("violet", loaded)
                e = loaded["violet sorrengail"]
                for f in _CORE_FIELDS:
                    self.assertIn(f, e, f"merged entry missing {f}")

    def test_reduce_survives_each_core_field_missing(self):
        for field in _CORE_FIELDS:
            with self.subTest(field=field):
                entry = _producer_entry()
                del entry[field]
                loaded = self._roundtrip({"violet sorrengail": entry})
                # Must not raise (pre-fix: KeyError on roles, name,
                # appearances — the fields v2_reduce direct-indexes).
                red = pp.v2_reduce({}, {}, loaded, [_ch(3), _ch(40)])
                self.assertIn("characters", red)

    def test_completion_fills_canonical_empties(self):
        entry = _producer_entry()
        for f in _CORE_FIELDS:
            del entry[f]
        loaded = self._roundtrip({"violet sorrengail": entry})
        e = loaded["violet sorrengail"]
        self.assertEqual(e["name"], "")
        self.assertEqual(e["aliases"], set())
        self.assertEqual(e["alias_keys"], set())
        self.assertEqual(e["primary_keys"], set())
        self.assertEqual(e["_generic_keys"], set())
        self.assertEqual(e["appearances"], 0)
        self.assertEqual(e["last_seen"], 0)
        self.assertEqual(e["chapters"], set())
        self.assertEqual(e["roles"], Counter())
        self.assertEqual(e["descriptions"], [])

    def test_present_values_never_altered(self):
        roster = self._pair()
        loaded = self._roundtrip(roster)
        for key, orig in roster.items():
            e = loaded[key]
            self.assertEqual(e["name"], orig["name"])
            self.assertEqual(e["aliases"], orig["aliases"])
            self.assertEqual(e["alias_keys"], orig["alias_keys"])
            self.assertEqual(e["primary_keys"], orig["primary_keys"])
            self.assertEqual(e["_generic_keys"], orig["_generic_keys"])
            self.assertEqual(e["appearances"], orig["appearances"])
            self.assertEqual(e["last_seen"], orig["last_seen"])
            self.assertEqual(e["chapters"], orig["chapters"])
            self.assertEqual(e["roles"], orig["roles"])
            self.assertEqual(e["descriptions"], orig["descriptions"])
            self.assertEqual(e["statuses"], orig["statuses"])
            self.assertEqual(e["death_reports"], orig["death_reports"])
            self.assertEqual(e["evidence"], orig["evidence"])

    def test_retry_failed_seed_completes_roster(self):
        # A preview written by an older version: its chapter_data
        # roster entries lack core fields. cmd_retry_failed seeds
        # checkpoints from it; the seeded roster must come back from
        # the load seam complete.
        td = Path(self._tmp)
        epub = td / "oldbook.epub"
        epub.write_bytes(b"fake")
        entry = json.loads(json.dumps(
            pp._ckpt_json_safe(_producer_entry())))
        del entry["descriptions"]
        del entry["roles"]
        preview = {
            "file": str(epub), "title": "Old Book",
            "chapter_data": {
                "chapters": {
                    "1": {"a": {"characters": []}, "b": None},
                    "2": {"a": {"characters": []},
                          "b": {"summary": "s"}},
                },
                "roster": {"violet sorrengail": entry},
            },
        }
        ppath = td / "oldbook.json"
        ppath.write_text(json.dumps(preview), encoding="utf-8")
        with mock.patch.object(pp, "process_file_v2",
                               return_value=True) as pfv:
            _capture(pp.cmd_retry_failed, str(ppath))
        pfv.assert_called_once()
        book_key = pp._checkpoint_book_key(epub)
        _meta, loaded, _chs = pp._checkpoint_load(book_key)
        e = loaded["violet sorrengail"]
        for f in _CORE_FIELDS:
            self.assertIn(f, e, f"seeded roster entry missing {f}")
        self.assertIsInstance(e["descriptions"], list)
        self.assertIsInstance(e["roles"], Counter)
        self.assertEqual(e["name"], "Violet Sorrengail")


# ----------------------------------------------------------------------
# F14 — cmd_push_preview shape validation
# ----------------------------------------------------------------------

class _RecordingSB:
    """Supabase stand-in: GETs return empty (nothing pre-existing),
    every call is recorded so tests can prove zero writes happened."""

    def __init__(self):
        self.calls = []

    def __call__(self, table, params=None, method="GET", data=None,
                 prefer=None):
        self.calls.append((table, method, params or "", data))
        if method == "GET":
            return []
        items = data if isinstance(data, list) else [data]
        return [dict(d, id=1000 + i) for i, d in enumerate(items)]

    def mutating_calls(self):
        return [c for c in self.calls if c[1] in ("POST", "PATCH", "DELETE")]


class TestPushPreviewValidation(_ConfigGuard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self._old_sb = pp.sb
        self.addCleanup(lambda: setattr(pp, "sb", self._old_sb))
        pp.CONFIG["supabase_url"] = "http://fake.invalid"
        pp.CONFIG["supabase_key"] = "fake-key"
        self.fake = _RecordingSB()
        pp.sb = self.fake
        self.resolve = mock.Mock(return_value=(1, "existing"))
        self._patches = [
            mock.patch.object(pp, "resolve_work", self.resolve),
            mock.patch.object(pp, "correct_metadata",
                              lambda t, a, ids: (t, a)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _preview_path(self, **over):
        data = {"file": "PushBook.epub", "title": "Push Book",
                "author": "Some Author", "isbn": "9780000000001",
                "tropes": ["Mind Game"], "trope_confidence": {},
                "trope_catalog_map": {pp._tnorm("Mind Game"): "cat-a"},
                "trope_chapters": {pp._tnorm("Mind Game"): ["Ch 1"]},
                "triggers": [],
                "characters": [{"name": "Hero", "role": "protagonist"}],
                "relationships": [], "quotes": []}
        data.update(over)
        p = self.td / "push.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    def _assert_refused_no_writes(self, out, needle):
        self.assertIn("Refusing to push", out)
        self.assertIn(needle, out)
        self.assertEqual(self.fake.mutating_calls(), [])
        self.resolve.assert_not_called()

    def test_character_without_role_refused(self):
        out = _capture(pp.cmd_push_preview, self._preview_path(
            characters=[{"name": "Hero"}]))
        self._assert_refused_no_writes(out, "role")

    def test_missing_relationships_section_refused(self):
        data = {"file": "PushBook.epub", "title": "Push Book",
                "author": "Some Author", "isbn": "9780000000001",
                "tropes": [], "triggers": [],
                "characters": [{"name": "Hero", "role": "protagonist"}]}
        p = self.td / "partial.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        out = _capture(pp.cmd_push_preview, str(p))
        self._assert_refused_no_writes(out, "relationships")

    def test_relationship_missing_type_refused(self):
        out = _capture(pp.cmd_push_preview, self._preview_path(
            relationships=[{"from": "Hero", "to": "Villain",
                            "evidence": "a quote"}]))
        self._assert_refused_no_writes(out, "type")

    def test_trigger_dict_without_warning_refused(self):
        out = _capture(pp.cmd_push_preview, self._preview_path(
            triggers=[{"severity": "high"}]))
        self._assert_refused_no_writes(out, "warning")

    def test_valid_preview_still_pushes(self):
        out = _capture(pp.cmd_push_preview, self._preview_path())
        self.assertIn("Push complete, no errors", out)
        self.assertTrue(self.fake.mutating_calls())


# ----------------------------------------------------------------------
# F10 — dedupe embedding degradation
# ----------------------------------------------------------------------

class TestDedupeEmbedDegradation(_ConfigGuard):
    def _chars(self):
        return [{"name": "Jon Pryce", "description": "a knight",
                 "confidence": 0.8},
                {"name": "Jonathan Pryce", "description": "a knight",
                 "confidence": 0.9}]

    def test_empty_embed_url_writes_undeduped(self):
        chars = self._chars()
        buf = io.StringIO()
        with mock.patch.dict(pp.CONFIG, {"embed_url": ""}), \
                contextlib.redirect_stdout(buf):
            out, rels, povs, report = pp.dedupe_characters(
                chars, [], [])
        self.assertEqual([c["name"] for c in out],
                         ["Jon Pryce", "Jonathan Pryce"])
        self.assertEqual(report, [])
        self.assertIn("DETERMINISTIC merges only", buf.getvalue())

    def test_embed_endpoint_failure_writes_undeduped(self):
        chars = self._chars()
        buf = io.StringIO()
        with mock.patch.dict(pp.CONFIG,
                             {"embed_url": "http://127.0.0.1:9/v1"}), \
                mock.patch.object(
                    pp, "embed_vectors",
                    side_effect=RuntimeError("could not reach")), \
                contextlib.redirect_stdout(buf):
            out, rels, povs, report = pp.dedupe_characters(
                chars, [], [])
        self.assertEqual(len(out), 2)
        self.assertEqual(report, [])
        self.assertIn("DETERMINISTIC merges only", buf.getvalue())

    def test_embeddings_available_still_dedupes(self):
        chars = self._chars()
        with mock.patch.dict(pp.CONFIG,
                             {"embed_url": "http://fake.invalid/v1"}), \
                mock.patch.object(pp, "embed_vectors",
                                  return_value=[[1.0, 0.0], [1.0, 0.0]]):
            out, rels, povs, report = pp.dedupe_characters(
                chars, [], [])
        self.assertEqual([c["name"] for c in out], ["Jonathan Pryce"])
        self.assertEqual(len(report), 1)


# ----------------------------------------------------------------------
# F18 — preview/dry-run rename
# ----------------------------------------------------------------------

class TestPreviewRename(_ConfigGuard):
    def _book(self, td):
        fpath = Path(td) / "noisbn-book.epub"
        fpath.write_bytes(b"fake")
        return fpath

    def test_preview_mode_does_not_rename(self):
        pp.CONFIG["rename_no_isbn"] = True
        with tempfile.TemporaryDirectory() as td:
            fpath = self._book(td)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cont = pp._handle_missing_isbn(fpath, preview=True)
            self.assertFalse(cont)  # book still skipped, as in write mode
            self.assertTrue(fpath.exists())
            self.assertFalse(
                (Path(td) / "NO-ISBN-noisbn-book.epub").exists())
            self.assertIn("would rename", buf.getvalue())

    def test_dry_run_mode_does_not_rename(self):
        pp.CONFIG["rename_no_isbn"] = True
        with tempfile.TemporaryDirectory() as td:
            fpath = self._book(td)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cont = pp._handle_missing_isbn(fpath, dry_run=True)
            self.assertFalse(cont)
            self.assertTrue(fpath.exists())
            self.assertFalse(
                (Path(td) / "NO-ISBN-noisbn-book.epub").exists())
            self.assertIn("would rename", buf.getvalue())

    def test_write_mode_still_renames(self):
        pp.CONFIG["rename_no_isbn"] = True
        with tempfile.TemporaryDirectory() as td:
            fpath = self._book(td)
            with contextlib.redirect_stdout(io.StringIO()):
                cont = pp._handle_missing_isbn(fpath)
            self.assertFalse(cont)
            self.assertFalse(fpath.exists())
            self.assertTrue(
                (Path(td) / "NO-ISBN-noisbn-book.epub").exists())


# ----------------------------------------------------------------------
# F20 + F21 — end-to-end through process_file_v2 (tiny EPUB, fake LLM
# at the backend boundary; real Call A/B handling, roster and reduce)
# ----------------------------------------------------------------------

def _xhtml(body):
    return ("<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.idpf.org/2007/xhtml'><head>"
            "<title>t</title></head>"
            f"<body>{body}</body></html>")


def _make_epub(path, files, title="Batch2 Test Book"):
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


_STORY_SENTENCE = "Mira Solace drew her sword and faced the dawn."
_DIALOGUE = ('"We ride at dawn," Mira said. "Then we ride," '
             'Tomas answered, and the hall erupted in cheers. ')


class _SpiceBackend:
    """Fake LLM backend: Call A returns one character, Call B returns
    spice 4 plus a murder trigger with evidence from the chapter text."""

    def __call__(self, prompt, chunk, i, n):
        if prompt == pp.PROMPT_V2_CONTENT:
            trig = {c: "none" for c in pp.TRIGGER_CATEGORIES}
            trig["murder"] = "mentioned"
            return json.dumps({
                "summary": "A chapter summary.", "spice_level": 4,
                "triggers": trig,
                "trigger_evidence": {"murder": _STORY_SENTENCE},
                "trope_candidates": [], "quotes": []})
        return json.dumps({
            "characters": [{"name": "Mira Solace", "role": "protagonist",
                            "description": "A swordswoman of the valley.",
                            "evidence": _STORY_SENTENCE}],
            "relationships": [], "pov_character": "Mira Solace"})


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
        result = json.loads(preview_json.read_text(encoding="utf-8"))
        md = preview_md.read_text(encoding="utf-8")
        return result, md


class TestResultForwarding(_E2ECase):
    def test_result_carries_reduce_fields(self):
        story = (f"<p>{_STORY_SENTENCE}</p>"
                 f"<p>{_DIALOGUE}</p>" + (
                     "<p>The valley was quiet and the river ran cold "
                     "and bright under a wide northern sky full of "
                     "slow clouds.</p>" * 22))
        files = {f"OEBPS/ch{k:02d}.xhtml": _xhtml(
            f"<h1>Chapter {k}</h1>" + story) for k in range(1, 5)}
        result, md = self._run_book("fwdbook", files, _SpiceBackend())
        # The fields v2_reduce computed must survive assembly (pre-fix:
        # none of them were in the result at all).
        self.assertEqual(result["chapter_spice"], [0, 4, 4, 4, 4])
        self.assertEqual(result["chapters_successful"], 4)
        self.assertEqual(result["spice_peak"], 4)
        self.assertEqual(result["spice_peak_band"], "explicit")
        self.assertIn("murder", result["trigger_chapters"])
        self.assertIn("avg_dialogue_ratio", result)
        # --recalibrate-spice works on a real preview (pre-fix: no_data
        # for every real preview ever written).
        status, _old, _new, _detail = pp._recalibrate_spice(result)
        self.assertIn(status, ("ok", "no_change"))
        # Summary label tells the truth about a spice-4 book.
        summary = pp._preview_summary(result)
        spice_line = [ln for ln in summary.split("\n")
                      if "Spice:" in ln][0]
        self.assertIn("peak=explicit", spice_line)
        self.assertNotIn("no spicy content", spice_line)
        # save_preview emits the dialogue line now that the data exists.
        self.assertIn("**Dialogue:**", md)


class TestSummarySpiceLabel(unittest.TestCase):
    def test_spice4_without_peak_data_is_not_no_spicy_content(self):
        r = {"title": "T", "author": "A", "spice_level": 4,
             "characters": [], "relationships": [], "triggers": [],
             "tropes": []}
        out = pp._preview_summary(r)
        spice_line = [ln for ln in out.split("\n") if "Spice:" in ln][0]
        self.assertIn("Spice: 4/5", spice_line)
        self.assertNotIn("no spicy content", spice_line)

    def test_spice0_still_says_no_spicy_content(self):
        r = {"title": "T", "author": "A", "spice_level": 0,
             "spice_peak": 0, "characters": [], "relationships": [],
             "triggers": [], "tropes": []}
        self.assertIn("Spice: 0/5 (no spicy content)",
                      pp._preview_summary(r))


class TestSubsetChapterSpice(_E2ECase):
    def test_subset_spice_at_original_indices(self):
        # Unit seam (the sweep probe): chapter_bs keyed by ORIGINAL
        # indices 40/41, chapters list holding only the subset.
        def _b(spice):
            full = {c: "none" for c in pp.TRIGGER_CATEGORIES}
            return {"summary": "s", "spice_level": spice,
                    "triggers": full, "trigger_evidence": {},
                    "trope_candidates": [], "quotes": []}
        red = pp.v2_reduce({}, {40: _b(5), 41: _b(3)}, {},
                           [_ch(40), _ch(41)], n_chapters_total=41)
        self.assertEqual(len(red["chapter_spice"]), 42)
        self.assertEqual(red["chapter_spice"][40], 5)
        self.assertEqual(red["chapter_spice"][41], 3)

    def test_chunks_subset_preview_spice(self):
        # End-to-end: a 41-chapter book processed with --chunks 40,41.
        class _MarkerBackend:
            def __call__(self, prompt, chunk, i, n):
                if prompt == pp.PROMPT_V2_CONTENT:
                    spice = (5 if "SPICE40MARKER" in chunk else
                             3 if "SPICE41MARKER" in chunk else 0)
                    return json.dumps({
                        "summary": "s", "spice_level": spice,
                        "triggers": {}, "trigger_evidence": {},
                        "trope_candidates": [], "quotes": []})
                return json.dumps({"characters": [], "relationships": [],
                                   "pov_character": None})

        files = {}
        for k in range(1, 42):
            marker = (" SPICE40MARKER" if k == 40 else
                      " SPICE41MARKER" if k == 41 else "")
            body = (f"<h1>Chapter {k}</h1><p>Chapter {k} begins.{marker}</p>"
                    + "<p>The valley was quiet and the river ran cold "
                      "and bright under a wide northern sky full of "
                      "slow clouds and slow water.</p>" * 24)
            files[f"OEBPS/ch{k:02d}.xhtml"] = _xhtml(body)
        result, _md = self._run_book(
            "subsetbook", files, _MarkerBackend(),
            config={"only_chunks": "40,41"})
        self.assertEqual(len(result["chapter_spice"]), 42)
        self.assertEqual(result["chapter_spice"][40], 5)
        self.assertEqual(result["chapter_spice"][41], 3)


if __name__ == "__main__":
    unittest.main()
