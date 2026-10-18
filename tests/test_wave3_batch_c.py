"""Wave 3 batch C: standalone tools (issues #35, #36, #39 + cap rider).

  #35 inject_isbn: a stale ISBN in plain <dc:identifier> form (the
      most common form, hyphenated or not, no opf:scheme / urn:isbn:
      decoration) must be detected and removed — an injection that
      reports success must not leave the stale ISBN first in document
      order for the processor's first-valid-ISBN resolution to pick.
      Non-ISBN identifiers (DOI, UUID, ASIN, publisher ids) survive.
  #36 inject_isbn: a non-UTF-8 container.xml / OPF must produce a
      clean printed ERROR and the tool's numbered exit for invalid
      EPUB content (3, the BadZipFile / missing-file exit), never a
      raw UnicodeDecodeError traceback, and must not rewrite the book.
  Rider (from the #1 gate): inject_isbn's zip reads are capped the
      way process-portable.py's are — declared-size pre-check plus a
      streaming cap, container 1 MiB / OPF 5 MiB portal-parity limits,
      other pass-through entries capped per entry; an over-cap entry
      fails loudly (ERROR + exit 3) and the EPUB stays unmodified.
  #39 deterministic-match: an empty or all-uncertain labels file
      leaves zero pairs after the uncertainty filter; the summary
      must print truthfully (0/0 at 0.0%) and exit normally instead
      of dying with ZeroDivisionError.

Stdlib unittest only. Run: python -m unittest tests.test_wave3_batch_c -v
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
import zipfile
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
_MOD_PATH = str(_ROOT / "inject_isbn.py")
_spec = importlib.util.spec_from_file_location("inject_isbn_c", _MOD_PATH)
ii = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ii)

INJECT = str(_ROOT / "inject_isbn.py")
DETMATCH = str(_ROOT / "deterministic-match.py")

CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

# Identifiers go BEFORE <dc:title> so document-order resolution sees
# the stale identifier first — the filing's reproduction layout.
OPF_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <metadata>
{identifiers}    <dc:title>Test Book</dc:title>
    <dc:creator>Test Author</dc:creator>
  </metadata>
  <manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""


def _make_epub(path, identifiers="", opf_bytes=None, extra_files=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        if opf_bytes is not None:
            z.writestr("OEBPS/content.opf", opf_bytes)
        else:
            z.writestr("OEBPS/content.opf",
                       OPF_TEMPLATE.format(identifiers=identifiers))
        z.writestr("OEBPS/ch1.xhtml",
                   "<html><body><p>Hello</p></body></html>")
        for name, data in (extra_files or {}).items():
            z.writestr(name, data)


def _read_opf(path):
    with zipfile.ZipFile(path, "r") as z:
        return z.read("OEBPS/content.opf").decode("utf-8")


def _first_isbn(opf_text):
    """First valid ISBN in document order, ISBN-13 form — the rule
    process-portable.py's _pick_identifiers applies to OPF
    identifiers (validate, then convert ISBN-10 to ISBN-13)."""
    for m in re.finditer(r"<dc:identifier[^>]*>(.*?)</dc:identifier>",
                         opf_text, re.DOTALL):
        valid = ii.validate_isbn(m.group(1).strip())
        if not valid:
            continue
        if len(valid) == 13:
            return valid
        core = "978" + valid[:9]
        total = sum(int(c) * (1 if i % 2 == 0 else 3)
                    for i, c in enumerate(core))
        return core + str((10 - total % 10) % 10)
    return None


def _run_main_inprocess(epub_path, isbn):
    old_argv = sys.argv
    sys.argv = ["inject_isbn.py", epub_path, isbn]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ii.main()
        return 0, buf.getvalue()
    except SystemExit as e:
        return e.code, buf.getvalue()
    finally:
        sys.argv = old_argv


def _run_subprocess(args):
    return subprocess.run([sys.executable] + args, capture_output=True,
                          text=True, timeout=60)


class _EpubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wave3c-")
        self.epub = os.path.join(self.tmp, "test.epub")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bytes(self):
        with open(self.epub, "rb") as f:
            return f.read()


# ----------------------------------------------------------------------
# #35: plain dc:identifier stale ISBN
# ----------------------------------------------------------------------

class TestPlainIdentifierStaleIsbn(_EpubCase):
    STALE_HYPHEN = "978-0-13-468599-1"   # digits: 9780134685991

    def test_plain_hyphenated_stale_removed_injected_resolves_first(self):
        _make_epub(self.epub, identifiers=(
            f"    <dc:identifier>{self.STALE_HYPHEN}</dc:identifier>\n"))
        code, _out = _run_main_inprocess(self.epub, "9781615871100")
        self.assertEqual(code, 0)
        opf = _read_opf(self.epub)
        self.assertNotIn("978-0-13-468599-1", opf)
        self.assertNotIn("9780134685991", opf)
        # Document-order resolution now finds the injected ISBN.
        self.assertEqual(_first_isbn(opf), "9781615871100")

    def test_plain_unhyphenated_stale_removed(self):
        _make_epub(self.epub, identifiers=(
            "    <dc:identifier>9780134685991</dc:identifier>\n"))
        code, _out = _run_main_inprocess(self.epub, "9781615871100")
        self.assertEqual(code, 0)
        opf = _read_opf(self.epub)
        self.assertNotIn("9780134685991", opf.replace("9781615871100", ""))
        self.assertEqual(_first_isbn(opf), "9781615871100")

    def test_urn_isbn_stale_still_removed(self):
        # The previously handled decorated form keeps working.
        _make_epub(self.epub, identifiers=(
            "    <dc:identifier>urn:isbn:9780134685991</dc:identifier>\n"))
        code, _out = _run_main_inprocess(self.epub, "9781615871100")
        self.assertEqual(code, 0)
        self.assertEqual(_first_isbn(_read_opf(self.epub)),
                         "9781615871100")

    def test_already_present_plain_hyphenated_no_duplicate(self):
        _make_epub(self.epub, identifiers=(
            "    <dc:identifier>978-1-61587-110-0</dc:identifier>\n"))
        before = self._bytes()
        code, out = _run_main_inprocess(self.epub, "9781615871100")
        self.assertEqual(code, 0)
        self.assertIn("already", out)
        # Nothing to do means nothing written: no duplicate element,
        # no rewritten file.
        self.assertEqual(self._bytes(), before)
        opf = _read_opf(self.epub)
        self.assertEqual(opf.count("<dc:identifier"), 1)

    def test_non_isbn_identifiers_survive_byte_for_byte(self):
        keep = (
            "    <dc:identifier>10.1000/xyz123</dc:identifier>\n"
            "    <dc:identifier>urn:uuid:123e4567-e89b-12d3-a456-426614174000</dc:identifier>\n"
            "    <dc:identifier opf:scheme=\"ASIN\">B012345678</dc:identifier>\n")
        _make_epub(self.epub, identifiers=keep)
        code, _out = _run_main_inprocess(self.epub, "9781615871100")
        self.assertEqual(code, 0)
        opf = _read_opf(self.epub)
        for line in keep.splitlines():
            self.assertIn(line.strip(), opf)
        self.assertIn("9781615871100", opf)


# ----------------------------------------------------------------------
# #36: non-UTF-8 container / OPF -> clean numbered error, no traceback
# ----------------------------------------------------------------------

class TestNonUtf8CleanError(_EpubCase):
    def test_windows1252_opf(self):
        opf_text = OPF_TEMPLATE.format(identifiers="")
        opf_text = opf_text.replace("Test Book", "Café Book")
        opf_bytes = opf_text.encode("windows-1252")  # é -> 0xe9, bad UTF-8
        _make_epub(self.epub, opf_bytes=opf_bytes)
        before = self._bytes()
        r = _run_subprocess([INJECT, self.epub, "9781615871100"])
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("ERROR", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("UnicodeDecodeError", r.stderr)
        self.assertEqual(self._bytes(), before)

    def test_windows1252_container(self):
        with zipfile.ZipFile(self.epub, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip",
                       compress_type=zipfile.ZIP_STORED)
            container = CONTAINER_XML.replace(
                "<container ", "<!-- Café --> <container ")
            z.writestr("META-INF/container.xml",
                       container.encode("windows-1252"))
            z.writestr("OEBPS/content.opf",
                       OPF_TEMPLATE.format(identifiers=""))
            z.writestr("OEBPS/ch1.xhtml", "<html/>")
        before = self._bytes()
        r = _run_subprocess([INJECT, self.epub, "9781615871100"])
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("ERROR", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self._bytes(), before)


# ----------------------------------------------------------------------
# Rider: capped zip reads in inject_isbn
# ----------------------------------------------------------------------

class TestInjectCaps(_EpubCase):
    def test_cap_constants_match_portal_parity(self):
        self.assertEqual(ii.EPUB_CONTAINER_MAX_BYTES, 1 << 20)
        self.assertEqual(ii.EPUB_OPF_MAX_BYTES, 5 << 20)
        # Other pass-through entries: a sane per-entry cap exists and
        # is the document-class size process-portable.py uses (32 MiB).
        self.assertEqual(ii.EPUB_OTHER_MAX_BYTES, 32 << 20)

    def test_oversized_container_refused_unmodified(self):
        _make_epub(self.epub)
        before = self._bytes()
        with mock.patch.object(ii, "EPUB_CONTAINER_MAX_BYTES", 64,
                               create=True):
            code, out = _run_main_inprocess(self.epub, "9781615871100")
        self.assertEqual(code, 3)
        self.assertIn("ERROR", out)
        self.assertEqual(self._bytes(), before)
        self.assertFalse(os.path.exists(self.epub + ".bak"))

    def test_oversized_opf_refused_unmodified(self):
        _make_epub(self.epub)
        before = self._bytes()
        with mock.patch.object(ii, "EPUB_OPF_MAX_BYTES", 64,
                               create=True):
            code, out = _run_main_inprocess(self.epub, "9781615871100")
        self.assertEqual(code, 3)
        self.assertIn("ERROR", out)
        self.assertEqual(self._bytes(), before)

    def test_oversized_other_entry_refused_unmodified(self):
        _make_epub(self.epub, extra_files={
            "OEBPS/big.bin": b"x" * 8192})
        before = self._bytes()
        with mock.patch.object(ii, "EPUB_OTHER_MAX_BYTES", 1024,
                               create=True):
            code, out = _run_main_inprocess(self.epub, "9781615871100")
        self.assertEqual(code, 3)
        self.assertIn("ERROR", out)
        self.assertEqual(self._bytes(), before)

    def test_oversized_opf_real_cap_refused(self):
        # No patched cap: an OPF padded past the shipped 5 MiB cap
        # (XML comment pad, still a parseable OPF shape) is refused.
        pad = "<!-- " + "p" * ((5 << 20) + 1024) + " -->"
        opf = OPF_TEMPLATE.format(identifiers="").replace(
            "</package>", pad + "</package>")
        _make_epub(self.epub, opf_bytes=opf.encode("utf-8"))
        before = self._bytes()
        r = _run_subprocess([INJECT, self.epub, "9781615871100"])
        self.assertEqual(r.returncode, 3, (r.stdout + r.stderr)[:500])
        self.assertIn("ERROR", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self._bytes(), before)

    def test_streaming_cap_catches_lying_header(self):
        # The declared size passes the pre-check but the stream yields
        # more than the cap: only the capped streaming read catches it.
        self.assertTrue(hasattr(ii, "_read_zip_entry_capped"))
        self.assertTrue(hasattr(ii, "EpubEntryTooLargeError"))
        from types import SimpleNamespace

        class _FakeZip:
            def __init__(self, data, declared):
                self._data, self._declared = data, declared

            def getinfo(self, name):
                return SimpleNamespace(file_size=self._declared[name])

            def open(self, name):
                return io.BytesIO(self._data[name])

        fake = _FakeZip({"x.bin": b"p" * 4096}, {"x.bin": 10})
        with self.assertRaises(ii.EpubEntryTooLargeError):
            ii._read_zip_entry_capped(fake, "x.bin", 1024)


# ----------------------------------------------------------------------
# #39: deterministic-match with zero surviving pairs
# ----------------------------------------------------------------------

class TestDeterministicMatchZeroPairs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wave3c-dm-")
        self.labels = os.path.join(self.tmp, "labels.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, labels):
        Path(self.labels).write_text(json.dumps(labels), encoding="utf-8")
        return _run_subprocess([DETMATCH, "--labels", self.labels])

    def test_empty_labels_clean_exit(self):
        r = self._run({})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Testing 0 pairs", r.stdout)
        self.assertIn("Decided: 0/0 (0.0%)", r.stdout)
        self.assertIn("Ambiguous (needs model/human): 0/0 (0.0%)",
                      r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("ZeroDivisionError", r.stderr)

    def test_all_uncertain_labels_clean_exit(self):
        r = self._run({
            "p1": {"a": "Alice Amber", "b": "Alicia Amber",
                   "same_person": True, "uncertain": True},
            "p2": {"a": "Bob Birch", "b": "Rob Birch",
                   "same_person": False, "uncertain": 1},
        })
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Testing 0 pairs", r.stdout)
        self.assertIn("Decided: 0/0 (0.0%)", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("ZeroDivisionError", r.stderr)

    def test_normal_mixed_labels_output_unchanged(self):
        r = self._run({
            "p1": {"a": "Matthew Sobol", "b": "Matthew Sobol",
                   "same_person": True},
            "p2": {"a": "Chris Sebeck", "b": "Peter Sebeck",
                   "same_person": False},
            "p3": {"a": "Ross", "b": "Jon Ross",
                   "same_person": False},
        })
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Testing 3 pairs", r.stdout)
        self.assertIn("Decided: 2/3 (66.7%)", r.stdout)
        self.assertIn("Ambiguous (needs model/human): 1/3 (33.3%)",
                      r.stdout)
        self.assertIn("Accuracy on decided: 100.0% (2/2)", r.stdout)


if __name__ == "__main__":
    unittest.main()
