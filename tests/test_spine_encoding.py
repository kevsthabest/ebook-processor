"""Spine-encoding tests (issue #29): spine XHTML must be decoded with
the encoding its XML declaration states, never with a hard-coded UTF-8
decode that silently discards every non-ASCII byte of a windows-1252
book ("Sebastien" with the accents stripped is a different name, and
the pipeline downstream — evidence, judging, claim writes — cannot
tell the text was mangled).

EPUBs are built in memory: extract_epub_units passes its argument
straight to zipfile.ZipFile, which accepts a file-like object, so no
temp files are needed and the crafted byte sequences below are exactly
what the decoder sees.
Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest tests.test_spine_encoding -v
"""
import contextlib
import importlib.util
import io
import unittest
import zipfile
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable_enc", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

# The reproduction sentence from issue #29: accents, curly quotes and
# a diaeresis — the characters a utf-8/errors="ignore" decode deletes.
_PROSE = ("Sébastien whispered, \u201cIt\u2019s the café on the rue de "
          "l\u2019école naïve, but ours.\u201d The résumé of the façade "
          "glowed in the crème light while Zoë counted the piñatas "
          "and the jalapeños by the piñata cart again and again.")


def _chapter(decl, prose, encoding):
    """One spine chapter as raw bytes: XML declaration `decl` (or None
    for no declaration at all), body carrying `prose`, encoded with
    the given codec — the caller controls declaration and bytes
    independently so lying declarations can be crafted too."""
    head = f"<?xml version='1.0' {decl}?>" if decl is not None else ""
    html = (head + "<html xmlns='http://www.w3.org/1999/xhtml'>"
            "<head><title>t</title></head><body>"
            f"<h1>First Chapter</h1><p>{prose}</p></body></html>")
    return html.encode(encoding)


def _epub_bytes(files):
    """files: {arcname: bytes}. Returns the EPUB as bytes with
    container + OPF spine in dict order (same shape as test_phase2)."""
    manifest = "\n".join(
        f'    <item id="i{n}" href="{name.split("/", 1)[1]}" media-type="application/xhtml+xml"/>'
        for n, name in enumerate(files))
    spine = "\n".join(f'    <itemref idref="i{n}"/>' for n in range(len(files)))
    opf = ("""<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='bid'>
  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
    <dc:title>Synthetic Encoding Book</dc:title>
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _extract(epub_bytes):
    """Extract units while capturing printed warnings."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        units, _title, _author, _ids = pp.extract_epub_units(io.BytesIO(epub_bytes))
    return units, out.getvalue()


class TestDeclaredEncoding(unittest.TestCase):
    def test_declared_windows1252_extracts_intact(self):
        # Issue #29 reproduction: pre-fix this extracted "Sbastien",
        # "caf", straight text with every curly quote deleted.
        epub = _epub_bytes({"OEBPS/ch01.xhtml": _chapter(
            "encoding='windows-1252'", _PROSE, "cp1252")})
        units, _warnings = _extract(epub)
        self.assertEqual(len(units), 1)
        text = units[0]["text"]
        for kept in ("Sébastien", "café", "Zoë", "piñatas", "jalapeños",
                     "\u201c", "\u201d", "l\u2019école"):
            self.assertIn(kept, text)
        self.assertNotIn("Sbastien", text)

    def test_declared_utf8_extracts_intact(self):
        epub = _epub_bytes({"OEBPS/ch01.xhtml": _chapter(
            "encoding='utf-8'", _PROSE, "utf-8")})
        units, _warnings = _extract(epub)
        self.assertEqual(len(units), 1)
        for kept in ("Sébastien", "café", "Zoë", "\u201c", "\u201d"):
            self.assertIn(kept, units[0]["text"])

    def test_undeclared_utf8_matches_declared_utf8(self):
        # A UTF-8 book without an encoding declaration must extract
        # exactly what the same book extracts with one — byte-identical
        # text to the pre-fix behaviour for the UTF-8 corpus.
        declared = _epub_bytes({"OEBPS/ch01.xhtml": _chapter(
            "encoding='utf-8'", _PROSE, "utf-8")})
        undeclared = _epub_bytes({"OEBPS/ch01.xhtml": _chapter(
            None, _PROSE, "utf-8")})
        d_units, _ = _extract(declared)
        u_units, _ = _extract(undeclared)
        self.assertEqual(u_units[0]["text"], d_units[0]["text"])
        self.assertIn("Sébastien", u_units[0]["text"])


class TestFallbackLadder(unittest.TestCase):
    def test_undeclared_windows1252_does_not_lose_characters(self):
        # No declaration, cp1252 bytes: strict UTF-8 fails, the ladder
        # lands on windows-1252, nothing is silently dropped, and the
        # fallback is announced instead of invisible.
        epub = _epub_bytes({"OEBPS/ch01.xhtml": _chapter(
            None, _PROSE, "cp1252")})
        units, warnings = _extract(epub)
        self.assertEqual(len(units), 1)
        self.assertIn("Sébastien", units[0]["text"])
        self.assertIn("\u201c", units[0]["text"])
        self.assertIn("windows-1252", warnings)

    def test_lying_utf8_declaration_falls_back(self):
        # Declares utf-8 but the bytes are cp1252: the declared codec
        # failing must not discard characters either — the next rung
        # of the ladder recovers the text and says so.
        epub = _epub_bytes({"OEBPS/ch01.xhtml": _chapter(
            "encoding='utf-8'", _PROSE, "cp1252")})
        units, warnings = _extract(epub)
        self.assertEqual(len(units), 1)
        self.assertIn("Sébastien", units[0]["text"])
        self.assertIn("windows-1252", warnings)

    def test_unknown_declared_encoding_is_ignored_not_fatal(self):
        # A declaration naming no real codec must not kill the file:
        # the declaration is ignored with a warning and the ladder
        # still recovers the (UTF-8) text.
        epub = _epub_bytes({"OEBPS/ch01.xhtml": _chapter(
            "encoding='x-not-a-codec'", _PROSE, "utf-8")})
        units, warnings = _extract(epub)
        self.assertEqual(len(units), 1)
        self.assertIn("Sébastien", units[0]["text"])
        self.assertIn("unknown declared encoding", warnings)

    def test_undecodable_bytes_become_replacement_not_silence(self):
        # 0x81 is undefined in windows-1252 and invalid in UTF-8: when
        # every rung fails, the character surfaces as U+FFFD with a
        # warning rather than vanishing — a visible defect beats a
        # silent one, and the surrounding prose survives.
        html = ("<?xml version='1.0' encoding='windows-1252'?>"
                "<html xmlns='http://www.w3.org/1999/xhtml'><head><title>t</title>"
                "</head><body><h1>First Chapter</h1><p>" + _PROSE +
                " Marker \x81 end of the marked passage here.</p></body></html>")
        chapter = html.encode("cp1252", errors="xmlcharrefreplace")
        # xmlcharrefreplace turns the unencodable control char into a
        # character reference; swap it for the raw undefined byte so
        # the file on the decoder's plate really carries 0x81.
        chapter = chapter.replace(b"&#129;", b"\x81")
        self.assertIn(b"\x81", chapter)
        epub = _epub_bytes({"OEBPS/ch01.xhtml": chapter})
        units, warnings = _extract(epub)
        self.assertEqual(len(units), 1)
        self.assertIn("Sébastien", units[0]["text"])
        self.assertIn("\ufffd", units[0]["text"])
        self.assertIn("replace", warnings)


if __name__ == "__main__":
    unittest.main()
