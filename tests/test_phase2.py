"""Phase 2 tests: chapter splitter with a synthetic EPUB built in-test.
Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import unittest
import zipfile
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _xhtml(body):
    return ("<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.w3.org/1999/xhtml'><head><title>t</title></head>"
            f"<body>{body}</body></html>")


def _make_epub(path, files):
    """files: {arcname: content}. Writes container + OPF with spine in dict order."""
    manifest = "\n".join(
        f'    <item id="i{n}" href="{name.split("/", 1)[1]}" media-type="application/xhtml+xml"/>'
        for n, name in enumerate(files) if name != "META-INF/container.xml")
    spine = "\n".join(
        f'    <itemref idref="i{n}"/>' for n, name in enumerate(files)
        if name != "META-INF/container.xml")
    opf = ("""<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='bid'>
  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
    <dc:title>Synthetic Test Book</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:identifier>9780000000000</dc:identifier>
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


def _para(words=50):
    return "<p>" + " ".join(f"word{i}" for i in range(words)) + "</p>"


class TestChapterSplitter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        cls.epub = str(Path(cls.tmp.name) / "test.epub")
        # ~3000-char chapter; ~50000-char chapter; tiny chapter; front/back matter
        big = _xhtml("<h1>Big Chapter</h1>" + _para(200) * 40)  # ~50k chars
        _make_epub(cls.epub, {
            "OEBPS/cover.xhtml": _xhtml("<p>" + "coverart " * 60 + "</p>"),
            "OEBPS/title.xhtml": _xhtml("<h1>Synthetic Test Book</h1><p>" + "title " * 60 + "</p>"),
            "OEBPS/copyright.xhtml": _xhtml("<p>" + "copyright notice " * 60 + "</p>"),
            "OEBPS/toc.xhtml": _xhtml("<h1>Contents</h1><p>" + "chapter link " * 60 + "</p>"),
            "OEBPS/ch01.xhtml": _xhtml("<h1>First Chapter</h1>" + _para(100) * 6),
            "OEBPS/ch02.xhtml": _xhtml("<p>" + "tiny interlude " * 14 + "</p>"),
            "OEBPS/ch03.xhtml": _xhtml("<h1>Third Chapter</h1>" + _para(100) * 6),
            "OEBPS/ch04.xhtml": big,
            "OEBPS/about-author.xhtml": _xhtml("<h1>About the Author</h1><p>" + "bio " * 500 + "</p>"),
        })

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_units_in_spine_order(self):
        units, title, author, ids = pp.extract_epub_units(self.epub)
        self.assertEqual(title, "Synthetic Test Book")
        self.assertEqual(author, "Test Author")
        spines = [u["spine"] for u in units]
        self.assertEqual(spines[0], "OEBPS/cover.xhtml")
        self.assertEqual(spines[-1], "OEBPS/about-author.xhtml")
        self.assertTrue(all(u["text"] for u in units))

    def test_legacy_joined_text_unchanged(self):
        units, _, _, _ = pp.extract_epub_units(self.epub)
        text, _, _, _ = pp.extract_epub_text(self.epub)
        self.assertEqual(text, "\n\n".join(u["text"] for u in units))

    def test_split_chapters(self):
        units, _, _, _ = pp.extract_epub_units(self.epub)
        chapters = pp.split_chapters(units)
        labels = [c["label"] for c in chapters]
        # front/back matter skipped
        self.assertNotIn("cover", " ".join(labels).lower())
        self.assertFalse(any("about the author" in lb.lower() for lb in labels))
        self.assertFalse(any("contents" == lb.lower() for lb in labels))
        # tiny ch02 merged into ch03 (label of the larger unit wins)
        self.assertIn("First Chapter", labels)
        self.assertIn("Third Chapter", labels)
        self.assertFalse(any("ch02" in lb for lb in labels))
        merged = next(c for c in chapters if c["label"] == "Third Chapter")
        self.assertIn("tiny interlude", merged["text"])
        # big chapter split on paragraph boundaries
        big_parts = [c for c in chapters if c["label"].startswith("Big Chapter")]
        self.assertEqual(len(big_parts), 2)
        self.assertEqual(big_parts[1]["label"], "Big Chapter (part 2)")
        self.assertTrue(all(len(c["text"]) <= pp.MAX_CHAPTER_CHARS for c in chapters))
        # indices sequential from 1, order preserved
        self.assertEqual([c["index"] for c in chapters], list(range(1, len(chapters) + 1)))
        first_pos = labels.index("First Chapter")
        third_pos = labels.index("Third Chapter")
        self.assertLess(first_pos, third_pos)

    def test_no_text_lost(self):
        units, _, _, _ = pp.extract_epub_units(self.epub)
        kept_spines = [u["spine"] for u in units
                       if not pp._SKIP_UNIT_RE.search(u["spine"])]
        kept_chars = sum(len(u["text"]) for u in units if u["spine"] in kept_spines)
        chapters = pp.split_chapters(units)
        # merged/split only re-chunks; allow small joining overhead
        got_chars = sum(len(c["text"]) for c in chapters)
        self.assertGreaterEqual(got_chars, kept_chars)

    def test_all_tiny_units(self):
        tiny = [{"spine": "a.xhtml", "label": "a", "text": "x" * 200},
                {"spine": "b.xhtml", "label": "b", "text": "y" * 200}]
        chapters = pp.split_chapters(tiny)
        self.assertEqual(len(chapters), 1)
        self.assertIn("x" * 200, chapters[0]["text"])
        self.assertIn("y" * 200, chapters[0]["text"])


if __name__ == "__main__":
    unittest.main()
