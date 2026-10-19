"""Decompression-cap tests (issue #1): the CLI EPUB parse paths must
cap every zip entry they read, the way the portal already does.

A crafted EPUB in the import folder is untrusted input to a folder
scan: a small compressed entry can decompress to gigabytes, and an
uncapped read() turns that into memory/disk exhaustion mid-scan.
These tests pin the discipline on both parse paths that read EPUBs
(process-portable.py's read_opf / extract_epub_units and
scaledown_lab.py's epub_chapters):

* a declared-size pre-check rejects an entry whose header size is
  already over the per-class cap;
* a capped streaming read rejects an entry whose header lies small
  while the stream actually yields more than the cap;
* an over-cap entry fails the whole book with the named cap error —
  it is never silently truncated, and an over-cap chapter is never
  silently dropped while the rest of the book is treated as complete;
* a legitimately large chapter (far above the corpus's typical
  chapter size, below the cap) extracts completely.

Caps are module constants, so most tests patch them down to a few
kilobytes instead of building multi-megabyte bombs; one OPF test
runs against the real 5 MiB cap. Patching uses create=True so the
pre-fix code — which has no caps at all — fails these tests by its
behaviour (extraction proceeds), not by a missing attribute.

Stdlib unittest only. No network, no real ebook files.
Run: python -m unittest tests.test_epub_decompression_caps -v
"""
import contextlib
import importlib.util
import io
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pp = _load("process_portable_caps", "process-portable.py")
lab = _load("scaledown_lab_caps", "scaledown_lab.py")

# Pre-fix there is no named cap error; fall back to Exception so the
# behavioural tests below fail because nothing is raised, while
# test_named_error_type_exists pins the name itself.
CapError = getattr(pp, "EpubEntryTooLargeError", Exception)


def _chapter_xhtml(body_text, heading="Chapter"):
    return ("<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.w3.org/1999/xhtml'>"
            "<head><title>t</title></head><body>"
            f"<h1>{heading}</h1><p>{body_text}</p></body></html>")


def _epub_bytes(files, opf_pad=0):
    """files: {arcname: str content} for spine documents. Returns EPUB
    bytes with container + OPF in dict order. opf_pad inflates the OPF
    with an XML comment of that many bytes (comments are legal OPF and
    ignored by parsers, so a padded OPF is still a valid book)."""
    manifest = "\n".join(
        f'    <item id="i{n}" href="{name.split("/", 1)[1]}" '
        f'media-type="application/xhtml+xml"/>'
        for n, name in enumerate(files))
    spine = "\n".join(f'    <itemref idref="i{n}"/>'
                      for n in range(len(files)))
    pad = f"<!-- {'p' * opf_pad} -->\n" if opf_pad else ""
    opf = ("""<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='bid'>
  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
    <dc:title>Capped Book</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:identifier>9780000000000</dc:identifier>
  </metadata>
""" + pad + """  <manifest>
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
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _extract(epub_bytes):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        return pp.extract_epub_units(io.BytesIO(epub_bytes))


class _FakeZip:
    """A zip stand-in whose headers lie: getinfo declares the sizes in
    `declared`, while read()/open() yield the full payloads in `data`.
    Both access styles are implemented so the same fake drives the
    pre-fix read() code and the post-fix capped reader."""

    def __init__(self, data, declared):
        self._data = data
        self._declared = declared

    def getinfo(self, name):
        return SimpleNamespace(file_size=self._declared[name])

    def read(self, name):
        return self._data[name]

    def open(self, name):
        return io.BytesIO(self._data[name])

    def namelist(self):
        return list(self._data)


class TestCapConstants(unittest.TestCase):
    def test_named_error_type_exists(self):
        self.assertTrue(hasattr(pp, "EpubEntryTooLargeError"))
        self.assertTrue(
            issubclass(pp.EpubEntryTooLargeError, Exception))

    def test_caps_match_portal_numbers_and_document_headroom(self):
        # Portal parity where the entry classes match: container
        # 1 MiB, OPF 5 MiB (portal.py _zip_entry_text call sites).
        self.assertEqual(pp.EPUB_CONTAINER_MAX_BYTES, 1 << 20)
        self.assertEqual(pp.EPUB_OPF_MAX_BYTES, 5 << 20)
        # Spine documents get a much larger cap than metadata: a
        # legitimate book may carry a whole novel in one XHTML file.
        self.assertEqual(pp.EPUB_DOCUMENT_MAX_BYTES, 32 << 20)


class TestProcessorDeclaredSizeCaps(unittest.TestCase):
    def test_container_over_cap_fails_book(self):
        epub = _epub_bytes({"OEBPS/ch01.xhtml": _chapter_xhtml("word " * 100)})
        with mock.patch.object(pp, "EPUB_CONTAINER_MAX_BYTES", 64,
                               create=True):
            with self.assertRaises(CapError) as cm:
                _extract(epub)
        self.assertIn("container.xml", str(cm.exception))
        self.assertIn("cap", str(cm.exception).lower())

    def test_opf_over_cap_fails_book(self):
        epub = _epub_bytes({"OEBPS/ch01.xhtml": _chapter_xhtml("word " * 100)},
                           opf_pad=4096)
        with mock.patch.object(pp, "EPUB_OPF_MAX_BYTES", 2048,
                               create=True):
            with self.assertRaises(CapError) as cm:
                _extract(epub)
        self.assertIn("content.opf", str(cm.exception))
        self.assertIn("cap", str(cm.exception).lower())

    def test_opf_over_real_cap_fails_book(self):
        # No patched cap: a padded OPF past the shipped 5 MiB cap
        # must fail against the defaults a folder scan actually runs.
        epub = _epub_bytes({"OEBPS/ch01.xhtml": _chapter_xhtml("word " * 100)},
                           opf_pad=(5 << 20) + 1024)
        with self.assertRaises(CapError):
            _extract(epub)

    def test_chapter_over_cap_fails_whole_book_not_skips(self):
        # One good chapter and one over-cap chapter: the book must
        # fail, never extract as if only the good chapter existed.
        epub = _epub_bytes({
            "OEBPS/ch01.xhtml": _chapter_xhtml("word " * 100, "Good"),
            "OEBPS/ch02.xhtml": _chapter_xhtml("word " * 5000, "Bomb"),
        })
        with mock.patch.object(pp, "EPUB_DOCUMENT_MAX_BYTES", 4096,
                               create=True):
            with self.assertRaises(CapError) as cm:
                _extract(epub)
        self.assertIn("ch02.xhtml", str(cm.exception))


class TestProcessorStreamedCap(unittest.TestCase):
    def test_lying_header_opf_is_capped_by_the_stream_read(self):
        # The declared sizes pass every pre-check (container is
        # honest, the OPF declares 10 bytes) but the OPF stream
        # actually yields more than the cap: only a capped streaming
        # read catches this, a declared-size check alone cannot.
        container = (b"<?xml version='1.0'?>"
                     b"<container xmlns='urn:oasis:names:tc:opendocument:"
                     b"xmlns:container'><rootfiles>"
                     b"<rootfile full-path='OEBPS/content.opf'/>"
                     b"</rootfiles></container>")
        opf = (b"<?xml version='1.0'?><package><!-- " + b"p" * 4096 +
               b" --></package>")
        fake = _FakeZip(
            data={"META-INF/container.xml": container,
                  "OEBPS/content.opf": opf},
            declared={"META-INF/container.xml": len(container),
                      "OEBPS/content.opf": 10})
        with mock.patch.object(pp, "EPUB_OPF_MAX_BYTES", 1024,
                               create=True):
            with self.assertRaises(CapError) as cm:
                pp.read_opf(fake)
        self.assertIn("content.opf", str(cm.exception))


class TestProcessorLegitimateBook(unittest.TestCase):
    def test_large_chapter_extracts_completely(self):
        # ~1.5 MiB of prose in one chapter: far above the synthetic
        # corpus's typical chapter (the phase-2 fixture's big chapter
        # is ~50 KB) and above the metadata caps, below the document
        # cap. The tail sentinel past the 1 MiB mark proves nothing
        # was clipped.
        prose = "lorem ipsum dolor sit amet " * 55000
        chapter = _chapter_xhtml(prose + " TAILSENTINEL")
        epub = _epub_bytes({"OEBPS/ch01.xhtml": chapter})
        units, title, _author, _ids = _extract(epub)
        self.assertEqual(title, "Capped Book")
        self.assertEqual(len(units), 1)
        self.assertGreater(len(units[0]["text"]), 1_000_000)
        self.assertIn("TAILSENTINEL", units[0]["text"])


class TestAggregateBudget(unittest.TestCase):
    """Issue #54: the per-entry caps bound one entry, but nothing
    bounded the TOTAL decompressed bytes across all entries of one
    book, so many just-under-cap entries could still sum to an
    unbounded decompression. One aggregate budget must cover every
    capped read of the book's extraction (container, OPF, and every
    spine document), failing the whole book with the same named
    error when the total would pass it."""

    def test_aggregate_budget_constant_in_hundreds_of_mib(self):
        self.assertTrue(hasattr(pp, "EPUB_TOTAL_MAX_BYTES"))
        # Real books decompress to under 2 MiB (issue #54), so a
        # budget in the hundreds of MiB cannot clip a legitimate
        # book while still bounding a crafted one.
        self.assertGreaterEqual(pp.EPUB_TOTAL_MAX_BYTES, 100 << 20)
        self.assertLess(pp.EPUB_TOTAL_MAX_BYTES, 1 << 30)

    def test_many_under_cap_chapters_over_total_budget_fails_book(self):
        # Ten chapters, each far below the 32 MiB document cap and
        # below the patched aggregate budget on its own — but their
        # sum (with container and OPF) passes the budget, so the
        # book must refuse rather than extract.
        files = {f"OEBPS/ch{n:02d}.xhtml":
                 _chapter_xhtml("word " * 120, f"Chapter {n}")
                 for n in range(10)}
        epub = _epub_bytes(files)
        with mock.patch.object(pp, "EPUB_TOTAL_MAX_BYTES", 4096,
                               create=True):
            with self.assertRaises(CapError) as cm:
                _extract(epub)
        msg = str(cm.exception).lower()
        self.assertTrue("total" in msg or "aggregate" in msg
                        or "budget" in msg, msg)

    def test_normal_epub_extracts_byte_identical(self):
        # A normal EPUB-shaped archive under the budget extracts
        # exactly the units its chapters carry — same texts, same
        # order, same metadata as the uncapped-aggregate behaviour.
        files = {
            "OEBPS/ch01.xhtml": _chapter_xhtml("alpha beta gamma " * 40,
                                               "One"),
            "OEBPS/ch02.xhtml": _chapter_xhtml("delta epsilon zeta " * 40,
                                               "Two"),
        }
        epub = _epub_bytes(files)
        units, title, author, _ids = _extract(epub)
        self.assertEqual(title, "Capped Book")
        self.assertEqual(author, "Test Author")
        self.assertEqual([u["label"] for u in units], ["One", "Two"])
        self.assertIn("alpha beta gamma", units[0]["text"])
        self.assertIn("delta epsilon zeta", units[1]["text"])
        again = _extract(epub)
        self.assertEqual([(u["spine"], u["label"], u["text"])
                          for u in units],
                         [(u["spine"], u["label"], u["text"])
                          for u in again[0]])


class TestScaledownLabCaps(unittest.TestCase):
    def _write(self, td, files, opf_pad=0):
        path = Path(td) / "book.epub"
        data = _epub_bytes(files, opf_pad=opf_pad)
        path.write_bytes(data)
        return str(path)

    def test_lab_opf_over_cap_raises_lab_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            epub = self._write(
                td, {"OEBPS/ch01.xhtml": _chapter_xhtml("word " * 300)},
                opf_pad=4096)
            with mock.patch.object(lab, "EPUB_OPF_MAX_BYTES", 2048,
                                   create=True):
                with self.assertRaises(lab.LabError) as cm:
                    lab.epub_chapters(epub, min_chars=10)
        self.assertIn("cap", str(cm.exception).lower())

    def test_lab_chapter_over_cap_raises_not_skips(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            epub = self._write(
                td, {"OEBPS/ch01.xhtml": _chapter_xhtml("word " * 5000)})
            with mock.patch.object(lab, "EPUB_DOCUMENT_MAX_BYTES", 4096,
                                   create=True):
                with self.assertRaises(lab.LabError) as cm:
                    lab.epub_chapters(epub, min_chars=10)
        self.assertIn("cap", str(cm.exception).lower())

    def test_lab_container_over_cap_raises_lab_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            epub = self._write(
                td, {"OEBPS/ch01.xhtml": _chapter_xhtml("word " * 300)})
            with mock.patch.object(lab, "EPUB_CONTAINER_MAX_BYTES", 64,
                                   create=True):
                with self.assertRaises(lab.LabError) as cm:
                    lab.epub_chapters(epub, min_chars=10)
        self.assertIn("cap", str(cm.exception).lower())


if __name__ == "__main__":
    unittest.main()
