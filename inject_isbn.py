#!/usr/bin/env python3
"""Inject an ISBN into an EPUB's OPF metadata. Usage:
    py inject_isbn.py "book.epub" 9781615871100
"""
import sys, zipfile, re, shutil, os


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
    with zipfile.ZipFile(epub_path, "r") as z:
        names = z.namelist()
        # Find the OPF file via container.xml
        container = z.read("META-INF/container.xml").decode("utf-8")
        m = re.search(r'full-path="([^"]+\.opf)"', container)
        if not m:
            print("Could not find OPF file in container.xml")
            sys.exit(1)
        opf_path = m.group(1)
        opf = z.read(opf_path).decode("utf-8")
        other_files = {n: z.read(n) for n in names if n != opf_path}

    # Check if ISBN already present
    if isbn in opf:
        print(f"ISBN {isbn} already in {opf_path}, nothing to do.")
        return

    # Remove any existing ISBN identifiers to avoid duplicates
    opf = re.sub(r'<dc:identifier[^>]*opf:scheme="ISBN"[^>]*>.*?</dc:identifier>\s*', '', opf)
    opf = re.sub(r'<dc:identifier[^>]*>urn:isbn:.*?</dc:identifier>\s*', '', opf)

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
