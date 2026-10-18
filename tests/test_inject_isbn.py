"""Tests for inject_isbn.py. Stdlib unittest only.
All tests operate on temp EPUB copies in tmp dirs — never the real archive.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "inject_isbn.py")
_spec = importlib.util.spec_from_file_location("inject_isbn", _MOD_PATH)
ii = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ii)

CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

OPF_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <metadata>
    <dc:title>Test Book</dc:title>
    <dc:creator>Test Author</dc:creator>
{extra_identifiers}  </metadata>
  <manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""


def _make_epub(path, extra_identifiers="", no_container=False, bad_opf=False):
    """Build a minimal valid EPUB at path."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        if not no_container:
            z.writestr("META-INF/container.xml", CONTAINER_XML)
        opf = OPF_TEMPLATE.format(extra_identifiers=extra_identifiers)
        if bad_opf:
            opf = opf.replace("<metadata>", "<metadataX>")
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/ch1.xhtml",
                   "<html><body><p>Hello</p></body></html>")


def _read_opf(path):
    with zipfile.ZipFile(path, "r") as z:
        return z.read("OEBPS/content.opf").decode("utf-8")


class TestValidateIsbn(unittest.TestCase):
    def test_valid_isbn13(self):
        self.assertEqual(ii.validate_isbn("9781615871100"), "9781615871100")

    def test_valid_isbn13_hyphenated(self):
        self.assertEqual(ii.validate_isbn("978-1-61587-110-0"), "9781615871100")

    def test_valid_isbn10(self):
        self.assertEqual(ii.validate_isbn("0-306-40615-2"), "0306406152")

    def test_valid_isbn10_x_check(self):
        self.assertEqual(ii.validate_isbn("3-598-21508-8"), "3598215088")

    def test_invalid_check_digit(self):
        self.assertIsNone(ii.validate_isbn("9781615871101"))

    def test_bad_length(self):
        self.assertIsNone(ii.validate_isbn("97816158711"))

    def test_garbage(self):
        self.assertIsNone(ii.validate_isbn("not-an-isbn"))

    def test_empty(self):
        self.assertIsNone(ii.validate_isbn(""))
        self.assertIsNone(ii.validate_isbn(None))


class TestInjectIsbn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="isbn-test-")
        self.epub = os.path.join(self.tmp, "test.epub")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, epub_path, isbn):
        old_argv = sys.argv
        sys.argv = ["inject_isbn.py", epub_path, isbn]
        try:
            ii.main()
            return 0
        except SystemExit as e:
            return e.code
        finally:
            sys.argv = old_argv

    def test_valid_injection(self):
        _make_epub(self.epub)
        code = self._run_main(self.epub, "9781615871100")
        self.assertEqual(code, 0)
        opf = _read_opf(self.epub)
        self.assertIn("9781615871100", opf)
        self.assertIn('opf:scheme="ISBN"', opf)

    def test_invalid_isbn_refused(self):
        _make_epub(self.epub)
        with open(self.epub, "rb") as f:
            before = f.read()
        code = self._run_main(self.epub, "9781615871101")  # bad check digit
        self.assertEqual(code, 2)
        with open(self.epub, "rb") as f:
            self.assertEqual(f.read(), before)  # file untouched

    def test_backup_created(self):
        _make_epub(self.epub)
        self._run_main(self.epub, "9781615871100")
        backup = self.epub + ".bak"
        self.assertTrue(os.path.isfile(backup))
        # Backup should NOT contain the injected ISBN
        with zipfile.ZipFile(backup, "r") as z:
            opf = z.read("OEBPS/content.opf").decode("utf-8")
        self.assertNotIn("9781615871100", opf)

    def test_existing_isbn_not_duplicated(self):
        existing = '    <dc:identifier opf:scheme="ISBN">9781615871100</dc:identifier>\n'
        _make_epub(self.epub, extra_identifiers=existing)
        code = self._run_main(self.epub, "9781615871100")
        self.assertEqual(code, 0)
        opf = _read_opf(self.epub)
        self.assertEqual(opf.count("9781615871100"), 1)

    def test_replaces_different_isbn(self):
        old = '    <dc:identifier opf:scheme="ISBN">9780000000000</dc:identifier>\n'
        _make_epub(self.epub, extra_identifiers=old)
        self._run_main(self.epub, "9781615871100")
        opf = _read_opf(self.epub)
        self.assertIn("9781615871100", opf)
        self.assertNotIn("9780000000000", opf)

    def test_corrupt_epub_handled(self):
        with open(self.epub, "w") as f:
            f.write("this is not a zip file at all")
        code = self._run_main(self.epub, "9781615871100")
        self.assertNotEqual(code, 0)  # should fail, not crash

    def test_missing_file(self):
        code = self._run_main(os.path.join(self.tmp, "nope.epub"),
                             "9781615871100")
        self.assertEqual(code, 1)

    def test_plain_isbn10_already_present_not_duplicated(self):
        # Issue #63 rider: a plain ISBN-10 identifier is the same
        # ISBN as its 978- form, so injecting the 13-digit form
        # must see it and do nothing — 10<->13 canonicalisation
        # is the subtlest logic the #35 fix added and was covered
        # only by a manual probe before this test.
        existing = '    <dc:identifier>0306406152</dc:identifier>\n'
        _make_epub(self.epub, extra_identifiers=existing)
        code = self._run_main(self.epub, "9780306406157")
        self.assertEqual(code, 0)
        opf = _read_opf(self.epub)
        self.assertEqual(opf.count("0306406152"), 1)
        self.assertNotIn("9780306406157", opf)

    def test_plain_isbn13_seen_when_injecting_the_isbn10(self):
        existing = '    <dc:identifier>9780306406157</dc:identifier>\n'
        _make_epub(self.epub, extra_identifiers=existing)
        code = self._run_main(self.epub, "0306406152")
        self.assertEqual(code, 0)
        opf = _read_opf(self.epub)
        self.assertEqual(opf.count("9780306406157"), 1)
        # Nothing was written: the already-present path returns
        # before any rewrite, so no backup exists.
        self.assertFalse(os.path.isfile(self.epub + ".bak"))

    def test_stale_plain_isbn10_is_replaced(self):
        old = '    <dc:identifier>0306406152</dc:identifier>\n'
        _make_epub(self.epub, extra_identifiers=old)
        self._run_main(self.epub, "9781615871100")
        opf = _read_opf(self.epub)
        self.assertIn("9781615871100", opf)
        self.assertNotIn("0306406152", opf)

    def test_whitespace_before_closing_tag_gt_is_recognised(self):
        # Issue #63 rider: "</dc:identifier >" is valid XML, so
        # an ISBN carried by that spelling must be detected and
        # stripped consistently with the plain spelling.
        existing = ('    <dc:identifier opf:scheme="ISBN">'
                    '9781615871100</dc:identifier >\n')
        _make_epub(self.epub, extra_identifiers=existing)
        code = self._run_main(self.epub, "9781615871100")
        self.assertEqual(code, 0)
        opf = _read_opf(self.epub)
        self.assertEqual(opf.count("9781615871100"), 1)

    def test_mimetype_stored_uncompressed(self):
        _make_epub(self.epub)
        self._run_main(self.epub, "9781615871100")
        with zipfile.ZipFile(self.epub, "r") as z:
            for info in z.infolist():
                if info.filename == "mimetype":
                    self.assertEqual(info.compress_type, zipfile.ZIP_STORED)


class TestIdentifierRegexShape(unittest.TestCase):
    """Issue #63: the identifier regex's body must stay bounded
    on a malformed OPF, and its behaviour on a well-formed OPF
    must be exactly what it was before the bound."""

    def test_unclosed_identifiers_terminate_fast(self):
        # Repeated opening tags with no closing tag anywhere:
        # the pre-fix lazy body scanned to end-of-string from
        # every opening tag (64 KiB measured at ~4.6 s, scaling
        # quadratically). Two seconds is a generous bound the
        # pre-fix code blows at this size; the bounded body
        # finishes in milliseconds.
        opf = "<dc:identifier>" * 4096
        start = time.perf_counter()
        found = ii._opf_has_isbn(opf, "9781615871100")
        stripped = ii._strip_isbn_identifiers(opf)
        elapsed = time.perf_counter() - start
        self.assertFalse(found)
        self.assertEqual(stripped, opf)
        self.assertLess(elapsed, 2.0)

    def test_wellformed_opf_behaviour_unchanged(self):
        opf = (
            "<metadata>\n"
            '    <dc:identifier opf:scheme="ISBN">'
            "978-1-61587-110-0</dc:identifier>\n"
            "    <dc:identifier>urn:uuid:12345678-1234-1234-1234-"
            "123456789012</dc:identifier>\n"
            "    <dc:identifier>0306406152</dc:identifier>\n"
            "</metadata>")
        self.assertTrue(ii._opf_has_isbn(opf, "9781615871100"))
        self.assertTrue(ii._opf_has_isbn(opf, "0306406152"))
        self.assertFalse(ii._opf_has_isbn(opf, "9780000000000"))
        # Stripping removes the two ISBN elements (taking their
        # trailing line whitespace along, as before) and leaves
        # the non-ISBN identifier byte-for-byte. The expected
        # string is the pre-fix output verbatim: the regex bound
        # must not move well-formed behaviour at all.
        self.assertEqual(
            ii._strip_isbn_identifiers(opf),
            "<metadata>\n"
            "    <dc:identifier>urn:uuid:12345678-1234-1234-1234-"
            "123456789012</dc:identifier>\n"
            "    </metadata>")


if __name__ == "__main__":
    unittest.main()
