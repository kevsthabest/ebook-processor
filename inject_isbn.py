#!/usr/bin/env python3
"""Inject an ISBN into an EPUB's OPF metadata. Usage:
    py inject_isbn.py "book.epub" 9781615871100
"""
import sys, zipfile, re, shutil, os


# An EPUB handed to this tool is untrusted input in the same way an
# EPUB in the import folder is: a zip entry whose compressed form is
# tiny can decompress to gigabytes, so every entry read here is capped
# per entry class instead of being inflated without a bound. The
# numbers mirror process-portable.py's caps where the classes match
# (container 1 MiB, OPF 5 MiB, portal parity); the remaining
# pass-through entries get that pipeline's document-class cap
# (32 MiB), which leaves generous room for chapters and images while
# still bounding one entry's cost. An over-cap entry fails the book
# rather than being truncated, because rewriting a book from a
# partial read would silently produce a damaged EPUB.
EPUB_CONTAINER_MAX_BYTES = 1 << 20
EPUB_OPF_MAX_BYTES = 5 << 20
EPUB_OTHER_MAX_BYTES = 32 << 20


class EpubEntryTooLargeError(Exception):
    """One EPUB entry exceeds its decompression cap (see the caps
    above). Raised instead of returning partial content."""


def _read_zip_entry_capped(z, name, cap):
    """Read one zip entry as bytes, enforcing `cap` twice.

    The declared-size check comes first because it is free — the
    header states the decompressed size, so an honest bomb is
    rejected before a single byte is inflated. The streamed read is
    capped too because the header can lie: a header that declares a
    small size while the deflate stream yields more must not be able
    to pull an unbounded read out of the second check's absence.
    Reading stops one byte past the cap, which is enough to know the
    entry is over without materialising the rest of it. (Mirrors
    process-portable.py's reader of the same name.)
    """
    info = z.getinfo(name)
    if info.file_size > cap:
        raise EpubEntryTooLargeError(
            f"EPUB entry {name} declares {info.file_size} bytes, over "
            f"the {cap}-byte cap for its entry class; refusing the book "
            f"rather than reading an unbounded entry")
    chunks = []
    total = 0
    with z.open(name) as fh:
        while True:
            chunk = fh.read(min(1 << 16, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                raise EpubEntryTooLargeError(
                    f"EPUB entry {name} decompressed past the "
                    f"{cap}-byte cap for its entry class despite "
                    f"declaring {info.file_size} bytes; refusing the "
                    f"book rather than reading an unbounded entry")
    return b"".join(chunks)


def validate_isbn(isbn):
    """Validate an ISBN string. Returns normalized digits-only ISBN or None.

    Strips hyphens, spaces, and urn:isbn: prefixes. Requires 10 or 13
    digits and a valid check digit (mod-11 for ISBN-10, mod-10 for
    ISBN-13). ISBN-10 may end in 'X'.
    """
    s = re.sub(r"(?i)^urn:isbn:", "", (isbn or "").strip())
    s = re.sub(r"[\s\-]", "", s).upper()
    if len(s) == 13 and s.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(s))
        return s if total % 10 == 0 else None
    if len(s) == 10 and s[:9].isdigit() and s[9] in "0123456789X":
        total = sum(int(s[i]) * (10 - i) for i in range(9))
        check_val = 10 if s[9] == "X" else int(s[9])
        return s if (total + check_val) % 11 == 0 else None
    return None


# One dc:identifier element: opening tag, body, closing tag plus any
# whitespace that trails it (the whitespace belongs to the element's
# line, so a removed element takes it along and a kept element keeps
# it byte-for-byte). The body is bounded to non-"<" characters
# because an identifier body is text-only: on a malformed OPF whose
# opening tag is never closed, the match then fails at the next tag
# instead of scanning to end-of-string from every opening tag,
# which made the scan quadratic and could hang the tool on an
# untrusted EPUB. Whitespace before the closing tag's ">" is valid
# XML ("</dc:identifier >") and is matched so that spelling is
# detected and stripped consistently with the plain one.
_IDENTIFIER_RE = re.compile(
    r"(<dc:identifier\b[^>]*>)([^<]*)(</dc:identifier\s*>\s*)",
    re.DOTALL | re.IGNORECASE)


def _to_isbn13(digits):
    """Convert a validated digits-only ISBN to its ISBN-13 form.

    The processor resolves identifiers to ISBN-13 (an ISBN-10 becomes
    its 978- form), so two spellings of the same ISBN — including the
    10- and 13-digit forms — must compare equal here, or an ISBN the
    book already carries in the other form would be injected twice.
    """
    if len(digits) == 13:
        return digits
    core = "978" + digits[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def _canonical_isbn(text):
    """ISBN-13 form of an identifier body, or None if it is not an ISBN.

    Validation is what keeps this safe to aim at arbitrary identifier
    bodies: DOIs, UUIDs, ASINs and publisher ids do not validate as
    ISBNs in any hyphenation, so they are never treated as ISBNs.
    """
    valid = validate_isbn((text or "").strip())
    return _to_isbn13(valid) if valid else None


def _is_isbn_identifier(open_tag, body):
    """Whether one dc:identifier element carries an ISBN.

    The body decides: a value that validates as an ISBN is an ISBN
    however it is spelled (plain, hyphenated, urn:isbn:-prefixed).
    Elements explicitly marked as ISBN — opf:scheme="ISBN" or a
    urn:isbn: body — also count even when the value itself would not
    validate, preserving this tool's long-standing removal of the
    decorated forms it already handled. Unmarked non-ISBN values are
    legitimate identifiers and must survive untouched.
    """
    if _canonical_isbn(body) is not None:
        return True
    if re.search(r"""opf:scheme\s*=\s*["']ISBN["']""", open_tag,
                 re.IGNORECASE):
        return True
    return (body or "").strip().lower().startswith("urn:isbn:")


def _opf_has_isbn(opf, isbn):
    """Whether the OPF already carries `isbn` as an identifier.

    Detection has to recognise the ISBN in the forms it can actually
    appear in: a bare substring test only sees the exact digits-only
    spelling, so a hyphenated identifier for the very same ISBN was
    missed and the ISBN got written a second time.
    """
    target = _to_isbn13(isbn)
    return any(_canonical_isbn(m.group(2)) == target
               for m in _IDENTIFIER_RE.finditer(opf))


def _strip_isbn_identifiers(opf):
    """Remove every ISBN-carrying dc:identifier element from the OPF.

    A stale ISBN left in place defeats the injection: the processor
    resolves a book to the first valid ISBN in document order, so a
    surviving stale identifier keeps resolving the book to the wrong
    ISBN while this tool reports success. Non-ISBN identifiers are
    returned exactly as matched, byte-for-byte.
    """
    def repl(m):
        return "" if _is_isbn_identifier(m.group(1), m.group(2)) \
            else m.group(0)
    return _IDENTIFIER_RE.sub(repl, opf)


def main():
    if len(sys.argv) != 3:
        print("Usage: py inject_isbn.py \"book.epub\" 9781615871100")
        sys.exit(1)
    epub_path = sys.argv[1]
    isbn = validate_isbn(sys.argv[2])
    if not isbn:
        print(f"ERROR: {sys.argv[2]!r} is not a valid ISBN "
              f"(bad length or check digit); refusing to inject.")
        sys.exit(2)
    if not os.path.isfile(epub_path):
        print(f"Not found: {epub_path}")
        sys.exit(1)

    # Read the EPUB
    try:
        with zipfile.ZipFile(epub_path, "r") as z:
            names = z.namelist()
            # Find the OPF file via container.xml
            container = _read_zip_entry_capped(
                z, "META-INF/container.xml",
                EPUB_CONTAINER_MAX_BYTES).decode("utf-8")
            m = re.search(r'full-path="([^"]+\.opf)"', container)
            if not m:
                print("Could not find OPF file in container.xml")
                sys.exit(1)
            opf_path = m.group(1)
            opf = _read_zip_entry_capped(
                z, opf_path, EPUB_OPF_MAX_BYTES).decode("utf-8")
            other_files = {
                n: _read_zip_entry_capped(z, n, EPUB_OTHER_MAX_BYTES)
                for n in names if n != opf_path}
    except zipfile.BadZipFile:
        print(f"ERROR: {epub_path} is not a valid EPUB/zip file.")
        sys.exit(3)
    except KeyError as e:
        print(f"ERROR: {epub_path} is missing required file: {e}")
        sys.exit(3)
    except UnicodeDecodeError as e:
        # A container/OPF that is not UTF-8 is invalid EPUB content,
        # the same failure class as the bad-zip and missing-file cases
        # above (exit 3): report it in the tool's numbered convention
        # instead of dying with a traceback. The book is not decoded
        # lossily and rewritten — an operator who wants it fixed
        # re-encodes it deliberately, outside a silent rewrite here.
        print(f"ERROR: {epub_path} has a container.xml or OPF that is "
              f"not valid UTF-8 ({e}); refusing to inject.")
        sys.exit(3)
    except EpubEntryTooLargeError as e:
        print(f"ERROR: {e}")
        sys.exit(3)

    # Check if ISBN already present
    if _opf_has_isbn(opf, isbn):
        print(f"ISBN {isbn} already in {opf_path}, nothing to do.")
        return

    # Remove any existing ISBN identifiers to avoid duplicates
    opf = _strip_isbn_identifiers(opf)

    # Inject after <dc:title> or at start of <metadata>
    isbn_tag = f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>'
    if "<dc:title>" in opf:
        opf = opf.replace("<dc:title>", isbn_tag + "\n    <dc:title>", 1)
    else:
        opf = re.sub(r'(<metadata[^>]*>)', r'\1\n    ' + isbn_tag, opf, count=1)

    # Write back (mimetype must be first, uncompressed)
    backup = epub_path + ".bak"
    shutil.copy2(epub_path, backup)
    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype first, stored (not compressed) per EPUB spec
        if "mimetype" in other_files:
            z.writestr("mimetype", other_files.pop("mimetype"), compress_type=zipfile.ZIP_STORED)
        z.writestr(opf_path, opf.encode("utf-8"))
        for n, data in other_files.items():
            z.writestr(n, data)

    print(f"Injected ISBN {isbn} into {epub_path}")
    print(f"Backup saved to {backup}")

if __name__ == "__main__":
    main()
