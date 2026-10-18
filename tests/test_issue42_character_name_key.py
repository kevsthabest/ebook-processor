"""Issue #42: character dedupe and backfill disagree on name forms.

The character write path used to compute "the same name" three ways in
one run, over three different name strings:

  * the partition (_claim_key / _partition_claims) keyed the rows
    already in the DB by their STORED name, plain .lower() — no trim,
    no punctuation handling;
  * the insert builder (_build_character_rows) keyed each run
    character by its CANONICAL name (the roster seam's rename, when
    one exists), plain .lower();
  * the backfill loop in write_claims keyed by the RAW run name,
    plain .lower(), and never resolved the canonical name at all —
    its row fetch ilike-matched the raw name too.

So a character whose forms diverged shipped wrong in both directions:
a stored row "Hero " (trailing space — an LLM artefact) did not stop
an incoming "Hero" candidate, and a canonical rename either inserted
a duplicate beside the surviving curated row (stored and run raw name
agree, canonical differs) or silently skipped the NULL-field backfill
for that row (canonical and stored agree, raw name differs).

Pinned behaviour after the fix: ONE canonical character-name key
(_char_name_key) is computed at every one of those sites, and the
builder and the backfill test BOTH name forms a run character carries
(its raw extracted name and its roster canonical name) against the
preserved set, so a rename cannot split one person into two rows and
a variant form cannot lose the backfill. The key itself is the
roster's own norm_name (casefold, leading-article drop, whitespace
tidy) with punctuation runs folded to a single space first — the
work-identity norm's punctuation-as-separator convention. It is key
hygiene only: nicknames, titles, initials and spelling variants are
NOT equated (those are matching rules, and they live upstream), so
two genuinely different names still key differently.

Stdlib unittest only; the Supabase stand-in, base class, and result
builder are the replace-wholesale suite's, imported so both suites
exercise the write path against identical fake semantics.
Run: python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_replace_wholesale import _ReplaceBase, _StatefulSB, _result, pp  # noqa: E402


class TestCanonicalCharacterNameKey(unittest.TestCase):
    """Direct unit pin on the canonical key function."""

    def test_case_and_whitespace_variants_key_identically(self):
        k = pp._char_name_key
        self.assertEqual(k("Bob Smith"), k("BOB SMITH"))
        self.assertEqual(k("Bob Smith"), k("bob smith"))
        self.assertEqual(k("Bob Smith"), k("  Bob Smith  "))
        self.assertEqual(k("Bob Smith"), k("Bob Smith "))
        self.assertEqual(k("Bob Smith"), k("Bob   Smith"))
        self.assertEqual(k("Bob Smith"), k("\tBob Smith\n"))

    def test_punctuation_variants_key_identically(self):
        k = pp._char_name_key
        self.assertEqual(k("Bob Smith"), k("Bob Smith."))
        self.assertEqual(k("Bob Smith"), k("Bob Smith,"))
        self.assertEqual(k("Bob S Smith"), k("Bob S. Smith"))
        self.assertEqual(k("Mary Jane Watson"), k("Mary-Jane Watson"))
        self.assertEqual(k("O Brien"), k("O'Brien"))

    def test_roster_convention_carries_over(self):
        # The key builds on norm_name, the roster's own name key, so
        # its leading-article rule carries over unchanged — the write
        # path must not invent a second article convention.
        self.assertEqual(pp._char_name_key("The Bob Smith"),
                         pp._char_name_key("Bob Smith"))
        self.assertEqual(pp._char_name_key("Bob Smith"),
                         pp.norm_name("Bob Smith"))

    def test_genuinely_different_names_key_differently(self):
        k = pp._char_name_key
        # A nickname is a different name FORM, not a punctuation or
        # case variant: the key must not equate it with the full name
        # (that equation is the canonical map's job, upstream).
        self.assertNotEqual(k("Bob Smith"), k("Robert Smith"))
        self.assertNotEqual(k("Bob Smith"), k("Bobby Smith"))
        self.assertNotEqual(k("Bob Smith"), k("Bob Jones"))
        # Siblings sharing a surname are different people.
        self.assertNotEqual(k("Chris Sebeck"), k("Peter Sebeck"))
        self.assertNotEqual(k("Bob Smith"), k("Bob Smithers"))
        self.assertNotEqual(k("Bob Smith"), k("Alice Smith"))

    def test_empty_and_none_key_empty(self):
        self.assertEqual(pp._char_name_key(""), "")
        self.assertEqual(pp._char_name_key(None), "")
        self.assertEqual(pp._char_name_key(" . "), "")


class TestPartitionAndBuilderAgree(_ReplaceBase):
    """The partition's preserved keys and the builder's skip test must
    be the same key space, over both forms a run character carries."""

    def test_partition_key_is_the_canonical_key(self):
        rows = [{"id": 1, "work_id": 1, "name": "Bob Smith ",
                 "status": "confirmed", "source_type": "ai"}]
        preserved, _rejected, ndel = pp._partition_claims(
            "book_characters", rows)
        self.assertEqual(preserved, {pp._char_name_key("Bob Smith")})
        self.assertEqual(ndel, 0)
        # _claim_key is the partition's key site: pin it directly.
        self.assertEqual(
            pp._claim_key("book_characters", {"name": "Bob Smith."}),
            pp._char_name_key("Bob Smith"))

    def test_builder_skips_trailing_space_and_punctuation_variants(self):
        preserved, _r, _n = pp._partition_claims("book_characters", [
            {"id": 1, "work_id": 1, "name": "Hero ",
             "status": "confirmed", "source_type": "ai"},
            {"id": 2, "work_id": 1, "name": "Bob Smith.",
             "status": "candidate", "source_type": "user"},
        ])
        result = _result(characters=[
            {"name": "Hero", "role": "protagonist"},
            {"name": "Bob Smith", "role": "supporting"},
        ])
        rows = pp._build_character_rows(1, result, {}, preserved)
        self.assertEqual(rows, [])

    def test_builder_dedupes_variant_forms_within_one_run(self):
        result = _result(characters=[
            {"name": "Bob Smith", "role": "protagonist"},
            {"name": "bob smith.", "role": "protagonist"},
            {"name": " Bob  Smith ", "role": "protagonist"},
        ])
        rows = pp._build_character_rows(1, result, {}, set())
        self.assertEqual([r["name"] for r in rows], ["Bob Smith"])

    def test_builder_skips_on_raw_form_when_canonical_renamed(self):
        # Issue arm 1 at the builder unit: stored and raw agree, the
        # canonical rename must not mint a second row for the person.
        preserved, _r, _n = pp._partition_claims("book_characters", [
            {"id": 1, "work_id": 1, "name": "Bob Smith",
             "status": "confirmed", "source_type": "ai"}])
        result = _result(
            characters=[{"name": "Bob Smith", "role": "supporting",
                         "entity_id": "ent1"}],
            roster_persistence={"canonical_names":
                                {"ent1": "Robert Smith"}})
        rows = pp._build_character_rows(1, result, {}, preserved)
        self.assertEqual(rows, [])

    def test_builder_still_emits_genuinely_different_names(self):
        preserved, _r, _n = pp._partition_claims("book_characters", [
            {"id": 1, "work_id": 1, "name": "Bob Smith",
             "status": "confirmed", "source_type": "ai"}])
        result = _result(characters=[
            {"name": "Robert Smith", "role": "supporting"},
            {"name": "Bob Jones", "role": "supporting"},
        ])
        rows = pp._build_character_rows(1, result, {}, preserved)
        self.assertEqual([r["name"] for r in rows],
                         ["Robert Smith", "Bob Jones"])

    def test_rel_map_and_builder_use_the_same_key(self):
        # A relationship sourced under a variant form of the name
        # must still embed on the character's row: the rel-map key
        # and the builder's lookup key are the same function.
        result = _result(
            characters=[{"name": "Bob Smith", "role": "protagonist"}],
            relationships=[{"from": "Bob Smith.", "to": "Alice Jones",
                            "type": "spouse", "evidence": "A sentence."}])
        rel_map, _nev = pp._build_rel_map(result)
        rows = pp._build_character_rows(1, result, rel_map, set())
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["relationships"]), 1)
        self.assertEqual(rows[0]["relationships"][0]["to"], "Alice Jones")


class TestWriteClaimsNameForms(_ReplaceBase):
    """End-to-end through write_claims against the stateful fake."""

    def test_trailing_space_stored_row_not_duplicated_and_backfilled(self):
        # The brief's reproduction: a preserved row stored as
        # "Name " and an incoming "Name" in one write must ship
        # exactly one row for that character, backfilled in place.
        fake = self.install(_StatefulSB({
            "book_characters": [
                {"id": 3, "work_id": 1, "name": "Hero ",
                 "status": "candidate", "source_type": "user",
                 "appearance": None, "vitality": None, "aliases": [],
                 "first_appearance_chapter": None}],
        }))
        res = _result(characters=[
            {"name": "Hero", "role": "protagonist",
             "appearance": "tall", "status": "alive"}])
        errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)
        chars = fake.tables["book_characters"]
        self.assertEqual(len(chars), 1)
        self.assertEqual(chars[0]["id"], 3)
        # The backfill site agrees too: the surviving row gained the
        # NULL fields from this run instead of being skipped.
        self.assertEqual(chars[0]["appearance"], "tall")
        self.assertEqual(chars[0]["vitality"], "alive")

    def test_canonical_rename_inserts_no_duplicate_beside_curated_row(self):
        # Issue arm 1: stored "Bob Smith" (confirmed), run raw name
        # "Bob Smith", canonical map renames the entity to
        # "Robert Smith" — no second candidate row may ship.
        fake = self.install(_StatefulSB({
            "book_characters": [
                {"id": 4, "work_id": 1, "name": "Bob Smith",
                 "status": "confirmed", "source_type": "ai",
                 "appearance": "x", "vitality": "alive",
                 "aliases": ["BS"], "first_appearance_chapter": 1}],
        }))
        res = _result(
            characters=[{"name": "Bob Smith", "role": "supporting",
                         "entity_id": "ent1"}],
            roster_persistence={"canonical_names":
                                {"ent1": "Robert Smith"}})
        errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)
        chars = fake.tables["book_characters"]
        self.assertEqual(len(chars), 1)
        self.assertEqual(chars[0]["id"], 4)
        self.assertEqual(chars[0]["name"], "Bob Smith")
        self.assertEqual(chars[0]["status"], "confirmed")

    def test_backfill_runs_when_canonical_matches_stored_not_raw(self):
        # Issue arm 2: run raw name "Bobby Smith", canonical ==
        # stored "Bob Smith". The builder already skipped the insert;
        # the backfill must not silently skip the surviving row.
        fake = self.install(_StatefulSB({
            "book_characters": [
                {"id": 5, "work_id": 1, "name": "Bob Smith",
                 "status": "candidate", "source_type": "user",
                 "appearance": None, "vitality": None, "aliases": [],
                 "first_appearance_chapter": None}],
        }))
        res = _result(
            characters=[{"name": "Bobby Smith", "role": "supporting",
                         "entity_id": "ent1", "appearance": "tall",
                         "status": "alive"}],
            roster_persistence={"canonical_names":
                                {"ent1": "Bob Smith"}})
        errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)
        chars = fake.tables["book_characters"]
        self.assertEqual(len(chars), 1)
        self.assertEqual(chars[0]["id"], 5)
        self.assertEqual(chars[0]["appearance"], "tall")
        self.assertEqual(chars[0]["vitality"], "alive")

    def test_punctuation_variant_stored_row_backfilled_not_duplicated(self):
        fake = self.install(_StatefulSB({
            "book_characters": [
                {"id": 6, "work_id": 1, "name": "Bob Smith.",
                 "status": "candidate", "source_type": "user",
                 "appearance": None, "vitality": None, "aliases": [],
                 "first_appearance_chapter": None}],
        }))
        res = _result(characters=[
            {"name": "Bob Smith", "role": "supporting",
             "appearance": "tall", "status": "alive"}])
        errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)
        chars = fake.tables["book_characters"]
        self.assertEqual(len(chars), 1)
        self.assertEqual(chars[0]["id"], 6)
        self.assertEqual(chars[0]["appearance"], "tall")

    def test_genuinely_different_character_still_inserts_and_backfills_noone(self):
        fake = self.install(_StatefulSB({
            "book_characters": [
                {"id": 7, "work_id": 1, "name": "Bob Smith",
                 "status": "candidate", "source_type": "user",
                 "appearance": None, "vitality": None, "aliases": [],
                 "first_appearance_chapter": None}],
        }))
        res = _result(characters=[
            {"name": "Robert Smith", "role": "supporting",
             "appearance": "short", "status": "alive"},
            {"name": "Bob Jones", "role": "minor",
             "appearance": "tall", "status": "alive"},
        ])
        errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)
        chars = fake.tables["book_characters"]
        self.assertEqual(sorted(r["name"] for r in chars),
                         ["Bob Jones", "Bob Smith", "Robert Smith"])
        # The preserved Bob Smith row was NOT backfilled from a
        # different person's extraction.
        by_id = {r["id"]: r for r in chars}
        self.assertIsNone(by_id[7]["appearance"])


if __name__ == "__main__":
    unittest.main()
