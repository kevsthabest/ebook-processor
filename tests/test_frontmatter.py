"""Front/back matter exclusion tests.

Front/back matter (dedication, acknowledgments, about-the-author,
also-by, copyright/colophon) is not story content: it must never be
evidence. split_chapters tags such chapters `is_frontmatter` (keeping
their positional slots — indices must not shift), the v2 map phase
never spends Call A/B on them, and v2_reduce excludes them from trope
candidates, trigger evidence, quotes, and POV counting — mirroring the
is_toc exclusions.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


# ---------------------------------------------------------------------------
# Tagger unit tests (pure function seam)
# ---------------------------------------------------------------------------

class TestFrontmatterTagger(unittest.TestCase):
    def tag(self, label, text, index, total):
        return pp._is_frontmatter_chapter(label, text, index, total)

    def test_dedication_first_chapter_tagged(self):
        text = ("Dedication\n\nFor Anna, who believed in this story "
                "when no one else did. Thank you for everything.")
        self.assertTrue(self.tag("Dedication", text, 1, 20))

    def test_acknowledgments_first_chapter_tagged(self):
        text = ("Acknowledgments\n\nI owe thanks to many people who "
                "helped this book come into being over the years.")
        self.assertTrue(self.tag("Acknowledgments", text, 1, 20))

    def test_about_the_author_last_chapter_tagged(self):
        text = ("About the Author\n\nThe author lives in a small town "
                "and writes novels about rivers, roads, and weather.")
        self.assertTrue(self.tag("About the Author", text, 20, 20))

    def test_also_by_last_chapter_tagged(self):
        text = ("Also by Test Author\n\nThe First River\nThe Second Road\n"
                "The Third Winter\nThe Fourth Harbor")
        self.assertTrue(self.tag("Also by Test Author", text, 19, 20))

    def test_copyright_first_chapter_tagged(self):
        text = "Copyright\n\nCopyright 2020 by Test Author."
        self.assertTrue(self.tag("Copyright", text, 1, 20))

    def test_filename_style_label_tagged(self):
        # Spine-filename fallback labels use hyphens, not spaces.
        text = "The author lives in a small town and writes novels."
        self.assertTrue(self.tag("about-the-author", text, 20, 20))

    def test_real_first_chapter_dedication_like_prose_not_tagged(self):
        # Precision probe: a real chapter whose opening LINE is a long
        # dedication-like prose sentence is story content, not a heading.
        text = ("Dedicated to the memory of her father, she had built "
                "the house with her own hands over one long summer, and "
                "the house stood above the river for forty years after.")
        text = text + " The story continues here. " * 100
        self.assertFalse(self.tag("Chapter 1", text, 1, 20))

    def test_real_first_chapter_short_dedication_like_line_not_tagged(self):
        # Precision probe: even a SHORT dedication-like first line is
        # prose when it carries sentence punctuation (a comma) — a real
        # heading never does.
        text = ("Dedicated to her, he raised the sword.\n\n"
                "The charge began at dawn and did not stop. " * 60)
        self.assertFalse(self.tag("Chapter 1", text, 1, 20))

    def test_epilogue_not_tagged(self):
        text = ("Epilogue\n\nYears later, Mira returned to the valley "
                "and found the river running cold and bright. " * 30)
        self.assertFalse(self.tag("Epilogue", text, 20, 20))

    def test_middle_dedication_not_tagged(self):
        # Position gate: an fm-shaped chapter mid-book is not front or
        # back matter (precision over recall — a false tag loses content).
        text = "Dedication\n\nFor Anna, who believed in this story."
        self.assertFalse(self.tag("Dedication", text, 10, 20))

    def test_long_acknowledgments_chapter_not_tagged(self):
        # Length cap: back matter merged into / appended to a full-length
        # final chapter must not take the real chapter down with it.
        text = "Acknowledgments\n\n" + ("Thank you to everyone. " * 400)
        self.assertGreater(len(text), 8000)
        self.assertFalse(self.tag("Acknowledgments", text, 20, 20))


# ---------------------------------------------------------------------------
# split_chapters tagging + index stability
# ---------------------------------------------------------------------------

def _fm_unit(spine, label, heading, body_sentence, reps):
    return {"spine": spine, "label": label,
            "text": heading + "\n\n" + (body_sentence * reps)}


def _story_unit(spine, label, reps=55):
    return {"spine": spine, "label": label,
            "text": label + "\n\n" + (
                "Mira walked the river road and thought about the war. "
                * reps)}


class TestSplitChaptersTagging(unittest.TestCase):
    def _units(self):
        # Generic spine names + single-signal content: these units
        # survive the pre-split drop heuristics (the FW-style leak) and
        # must be caught by the tagger instead.
        dedication = _fm_unit(
            "part0001.html", "Dedication", "Dedication",
            "For Anna, who believed in this story when no one else "
            "did, thank you for every late night and kind word. ", 18)
        about = _fm_unit(
            "part0099.html", "About the Author", "About the Author",
            "The author lives in a small town near the river and "
            "writes novels about roads, weather, and quiet people. ", 18)
        units = [dedication,
                 _story_unit("part0002.html", "Chapter 1"),
                 _story_unit("part0003.html", "Chapter 2"),
                 _story_unit("part0004.html", "Chapter 3"),
                 about]
        for u in units:
            self.assertGreaterEqual(len(u["text"]), 1500, u["label"])
        return units

    def test_tagged_chapters_keep_slots_and_indices(self):
        chapters = pp.split_chapters(self._units())
        # Nothing removed: count and sequential indices unchanged.
        self.assertEqual(len(chapters), 5)
        self.assertEqual([c["index"] for c in chapters], [1, 2, 3, 4, 5])
        self.assertEqual([c["label"] for c in chapters],
                         ["Dedication", "Chapter 1", "Chapter 2",
                          "Chapter 3", "About the Author"])
        by_label = {c["label"]: c for c in chapters}
        self.assertTrue(by_label["Dedication"]["is_frontmatter"])
        self.assertTrue(by_label["About the Author"]["is_frontmatter"])
        for lb in ("Chapter 1", "Chapter 2", "Chapter 3"):
            self.assertFalse(by_label[lb]["is_frontmatter"])

    def test_is_frontmatter_json_round_trip(self):
        chapters = pp.split_chapters(self._units())
        for c in chapters:
            rt = json.loads(json.dumps(c))
            self.assertEqual(rt["is_frontmatter"], c["is_frontmatter"])
            self.assertEqual(set(rt), set(c))


# ---------------------------------------------------------------------------
# v2_reduce exclusion (mirrors the is_toc exclusion tests)
# ---------------------------------------------------------------------------

def _ch(idx, label, text="x " * 200, fm=False):
    d = {"index": idx, "label": label, "text": text}
    if fm:
        d["is_frontmatter"] = True
    return d


def _b(triggers=None, spice=0, tropes=None, summary="sum", quotes=None,
       tev=None):
    full = {c: "none" for c in pp.TRIGGER_CATEGORIES}
    full.update(triggers or {})
    return {"summary": summary, "spice_level": spice,
            "triggers": full, "trigger_evidence": tev or {},
            "trope_candidates": tropes or [], "quotes": quotes or []}


class TestReduceExclusion(unittest.TestCase):
    def test_fm_chapter_contributes_no_trope_trigger_quote(self):
        chapters = [_ch(1, "Dedication", fm=True), _ch(2, "Ch 2")]
        quote = {"text": "The author thanks everyone who ever wrote "
                         "a kind review of this book.",
                 "speaker": "Author", "spoiler": False}
        bs = {1: _b(triggers={"murder": "on_page"},
                    tev={"murder": "He was murdered in the bio."},
                    tropes=["Secret Royal Baby"], quotes=[quote]),
              2: _b()}
        red = pp.v2_reduce({}, bs, {}, chapters)
        self.assertNotIn("Secret Royal Baby", red["trope_candidates"])
        self.assertNotIn("secret royal baby",
                         {k.lower() for k in red["trope_chapters"]})
        trig_names = [t.get("warning") or t.get("name")
                      for t in red["triggers"]]
        self.assertNotIn("murder", trig_names)
        self.assertNotIn("murder", red["trigger_chapters"])
        self.assertEqual(red["quotes"], [])

    def test_fm_chapters_contribute_no_pov(self):
        chapters = [_ch(1, "Dedication", fm=True), _ch(2, "A"),
                    _ch(3, "B"), _ch(4, "Also By", fm=True)]
        a_fm = {"characters": [], "relationships": [],
                "pov_character": "Author"}
        red = pp.v2_reduce({1: a_fm, 4: dict(a_fm)}, {}, {}, chapters)
        self.assertNotIn("Author", red["povs"])

    def test_chapter_spice_slots_unchanged_with_fm(self):
        # Positional stability: the fm chapter keeps its (zero) slot;
        # later chapters' spice stays at their own indices.
        chapters = [_ch(1, "Dedication", fm=True), _ch(2, "A"),
                    _ch(3, "B")]
        bs = {2: _b(spice=3), 3: _b(spice=1)}
        red = pp.v2_reduce({}, bs, {}, chapters)
        self.assertEqual(red["chapter_spice"], [0, 0, 3, 1])
        self.assertEqual(red["chapters_total"], 3)


# ---------------------------------------------------------------------------
# End-to-end: the map phase never calls the LLM on tagged chapters
# (fake backend = network boundary; real Call A/B, roster, reduce run)
# ---------------------------------------------------------------------------

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
    <dc:title>Frontmatter Test Book</dc:title>
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


_FM_MARKER = "ZQXJKV"
_FM_SENTENCE = "The ZQXJKV marker sentence stands here."
_STORY_SENTENCE = "Mira Solace drew her sword and faced the dawn."


class _FakeBackend:
    """Stands in for the LLM backend (network boundary). Records every
    chunk it is asked about; answers as a model would — including
    inventing an 'Author' character from front/back matter text."""

    def __init__(self):
        self.chunks = []

    def __call__(self, prompt, chunk, i, n):
        self.chunks.append(chunk)
        if prompt == pp.PROMPT_V2_CONTENT:
            return json.dumps({
                "summary": "A chapter summary.",
                "spice_level": 0, "triggers": {}, "trigger_evidence": {},
                "trope_candidates": ["Found Family"], "quotes": []})
        if _FM_MARKER in chunk:
            return json.dumps({
                "characters": [{"name": "Author", "role": "minor",
                                "description": "The book's author.",
                                "evidence": _FM_SENTENCE}],
                "relationships": [], "pov_character": None})
        return json.dumps({
            "characters": [{"name": "Mira Solace", "role": "protagonist",
                            "description": "A swordswoman of the valley.",
                            "evidence": _STORY_SENTENCE}],
            "relationships": [], "pov_character": "Mira Solace"})


class TestMapPhaseExclusion(unittest.TestCase):
    def test_no_llm_calls_or_roster_entries_from_frontmatter(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        epub = str(Path(tmp.name) / "fmtest.epub")
        story = f"<p>{_STORY_SENTENCE}</p>" + (
            "<p>The valley was quiet and the river ran cold and bright "
            "under a wide northern sky full of slow clouds.</p>" * 22)
        dedication = f"<h1>Dedication</h1><p>{_FM_SENTENCE}</p>" + (
            "<p>For Anna, who believed in this story when no one else "
            "did, thank you for every late night and kind word.</p>" * 18)
        about = f"<h1>About the Author</h1><p>{_FM_SENTENCE}</p>" + (
            "<p>The author lives in a small town near the river and "
            "writes novels about roads, weather, and quiet people.</p>" * 18)
        files = {"OEBPS/p01.xhtml": _xhtml(dedication)}
        for k in range(1, 5):
            files[f"OEBPS/ch{k:02d}.xhtml"] = _xhtml(
                f"<h1>Chapter {k}</h1>" + story)
        files["OEBPS/p99.xhtml"] = _xhtml(about)
        _make_epub(epub, files)

        backend = _FakeBackend()
        preview_json = pp.SCRIPT_DIR / "preview" / "fmtest.json"
        self.addCleanup(lambda: preview_json.unlink(missing_ok=True))
        self.addCleanup(lambda: (pp.SCRIPT_DIR / "preview" / "fmtest.md")
                        .unlink(missing_ok=True))
        self.addCleanup(lambda: shutil.rmtree(
            pp._checkpoint_dir(pp._checkpoint_book_key(Path(epub))),
            ignore_errors=True))
        with mock.patch.object(pp, "llm_ollama", backend), \
                mock.patch.object(pp, "_lookup_series_openlibrary",
                                  lambda *a, **k: (None, None)), \
                mock.patch.object(pp, "resolve_work",
                                  lambda *a, **k: (None, "test")), \
                mock.patch.dict(pp.CONFIG, {"llm": "ollama"}):
            ok = pp.process_file_v2(Path(epub), preview=True)
        self.assertTrue(ok)
        # The LLM was never shown front/back matter text.
        self.assertTrue(backend.chunks, "backend was never called at all")
        self.assertFalse(any(_FM_MARKER in c for c in backend.chunks),
                         "front/back matter reached an LLM call")
        result = json.loads(preview_json.read_text(encoding="utf-8"))
        # No roster entries, characters, or minors from front/back matter.
        roster = result["chapter_data"]["roster"]
        self.assertFalse(any("author" in k for k in roster),
                         f"roster polluted by front matter: {list(roster)}")
        names = [c["name"] for c in result["characters"]]
        names += [c["name"] for c in result["minor_characters"]]
        self.assertIn("Mira Solace", names)
        self.assertNotIn("Author", names)
        # Slots kept: all 6 chapters present, fm chapters carry no results.
        self.assertEqual(len(result["chapter_summaries"]), 6)
        chdata = result["chapter_data"]["chapters"]
        self.assertIsNone(chdata["1"]["a"])
        self.assertIsNone(chdata["1"]["b"])
        self.assertIsNone(chdata["6"]["a"])
        self.assertIsNone(chdata["6"]["b"])
        self.assertIsNotNone(chdata["2"]["a"])
        self.assertIsNotNone(chdata["2"]["b"])


if __name__ == "__main__":
    unittest.main()
