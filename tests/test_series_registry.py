"""Series registry tests (series-linking spec, Phase 2): the
write-side store, the fold of one finished book record, and the
pipeline seam beside the identity landing.

The Fourth Wing fixture is hand-trimmed from a real record —
the run-20261019-114632 preview in the user's files: names,
aliases and edges below are that preview's own (Violet
Sorrengail, Xaden Riorson, Tairn, Mira Sorrengail as principals,
Andarna and Oren Seifert as minors, the Mira->Violet sibling
row, the Brennan->Violet row whose source is outside the
trimmed cast, a rejected Tairn->Xaden sibling row, and a
non-family Garrick->Imogen romantic row). The series identity
fields (series_key hc-series:41764, The Empyrean #1) are the
values the Phase 0 probe verified and Phase 1 lands; fields the
fold never reads were dropped. The Iron Flame record is
synthetic, in the same shape, labelled as such where it matters.

Stdlib unittest only, no network, stores live in temp dirs.
Run: python -m unittest tests.test_series_registry -v
"""
import copy
import importlib.util
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, str(_ROOT / fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sr = _load("series_registry", "series_registry.py")
pp = _load("pp_series_registry", "process-portable.py")

EMPYREAN = "hc-series:41764"
FW_BOOK = "a42e27eb-f5c4-4082-be90-31ef9c6183e7"  # the preview's work_id
IF_BOOK = "if-synthetic-book-2"

FW_RECORD = {
    "title": "Fourth Wing",
    "author": "Rebecca Yarros",
    "work_id": FW_BOOK,
    "series_name": "The Empyrean",
    "series_position": 1,
    "series_source": "hardcover",
    "series_key": EMPYREAN,
    "hardcover": {"book_id": 714600, "series_id": 41764,
                  "compilation": False, "match_path": "isbn"},
    "characters": [
        {"name": "Violet Sorrengail",
         "aliases": ["Cadet Sorrengail", "I", "Vi", "Violence"]},
        {"name": "Xaden Riorson", "aliases": ["Resson", "Riorson"]},
        {"name": "Tairn", "aliases": ["Tairneanach"]},
        {"name": "Mira Sorrengail",
         "aliases": ["Lieutenant Sorrengail", "Mom"]},
    ],
    "minor_characters": [
        {"name": "Andarna", "aliases": ["golden dragon"]},
        {"name": "Oren Seifert", "aliases": ["Second-year rider"]},
    ],
    "relationships": [
        {"from": "Mira Sorrengail", "to": "Violet Sorrengail",
         "type": "sibling", "validation_state": "review_required"},
        {"from": "Brennan Sorrengail", "to": "Violet Sorrengail",
         "type": "sibling", "validation_state": "validated"},
        {"from": "Tairn", "to": "Xaden Riorson",
         "type": "sibling", "validation_state": "rejected"},
        {"from": "Garrick Aetos", "to": "Imogen Aetos",
         "type": "romantic", "validation_state": "rejected"},
    ],
}

# Synthetic second book of the same series: three returning
# characters (Andarna promoted to the principal list), one new
# minor, one new alias attestation for Violet, and the sibling
# edge again, validated this time.
IF_RECORD = {
    "title": "Iron Flame",
    "author": "Rebecca Yarros",
    "work_id": IF_BOOK,
    "series_name": "The Empyrean",
    "series_position": 2,
    "series_source": "hardcover",
    "series_key": EMPYREAN,
    "hardcover": {"book_id": 747398, "series_id": 41764,
                  "compilation": False, "match_path": "title"},
    "characters": [
        {"name": "Violet Sorrengail",
         "aliases": ["Vi", "Violence", "Sorrengail"]},
        {"name": "Xaden Riorson", "aliases": ["Riorson"]},
        {"name": "Andarna", "aliases": ["golden dragon"]},
    ],
    "minor_characters": [
        {"name": "Sloane Mairi", "aliases": []},
    ],
    "relationships": [
        {"from": "Mira Sorrengail", "to": "Violet Sorrengail",
         "type": "sibling", "validation_state": "validated"},
    ],
}


def _registry(tmp):
    return sr.SeriesRegistry(path=Path(tmp) / "series_registry.db")


def _by_name(chars):
    return {c["canonical_name"]: c for c in chars}


class FoldTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reg = _registry(self._tmp.name)
        self.addCleanup(self.reg.close)

    def test_fold_one_book(self):
        delta = sr.populate_from_record(self.reg, copy.deepcopy(FW_RECORD))
        self.assertEqual(delta, {
            "series_key": EMPYREAN,
            "characters_added": 6,
            "characters_updated": 0,
            # 4 (Violet) + 2 (Xaden) + 1 (Tairn) + 2 (Mira)
            # + 1 (Andarna) + 1 (Oren)
            "aliases_added": 11,
            # Mira->Violet and Tairn->Xaden resolve; Brennan is
            # not in the cast (skipped); the romantic row is not
            # a family type and is neither recorded nor skipped.
            "family_links_recorded": 2,
            "family_links_skipped": 1,
        })
        chars = _by_name(self.reg.characters(EMPYREAN))
        self.assertEqual(set(chars), {
            "Violet Sorrengail", "Xaden Riorson", "Tairn",
            "Mira Sorrengail", "Andarna", "Oren Seifert"})
        violet = chars["Violet Sorrengail"]
        self.assertEqual(violet["books"], [{
            "book_key": FW_BOOK, "list_kind": "principal",
            "position": 1}])
        self.assertEqual(violet["first_position"], 1)
        self.assertEqual(violet["last_position"], 1)
        self.assertEqual(
            {a["alias"] for a in violet["aliases"]},
            {"Cadet Sorrengail", "I", "Vi", "Violence"})
        self.assertTrue(all(
            a["books"] == [FW_BOOK] for a in violet["aliases"]))
        self.assertEqual(chars["Andarna"]["books"][0]["list_kind"],
                         "minor")
        links = self.reg.family_links(EMPYREAN)
        self.assertEqual(
            {(l["from"], l["to"], l["type"], l["validation_state"])
             for l in links},
            {("Mira Sorrengail", "Violet Sorrengail", "sibling",
              "review_required"),
             ("Tairn", "Xaden Riorson", "sibling", "rejected")})
        self.assertTrue(all(l["book_key"] == FW_BOOK for l in links))

    def test_record_is_not_mutated(self):
        record = copy.deepcopy(FW_RECORD)
        before = copy.deepcopy(record)
        sr.populate_from_record(self.reg, record)
        self.assertEqual(record, before)

    def test_refold_is_idempotent(self):
        first = sr.populate_from_record(self.reg, copy.deepcopy(FW_RECORD))
        snapshot = (self.reg.characters(EMPYREAN),
                    self.reg.family_links(EMPYREAN))
        second = sr.populate_from_record(self.reg, copy.deepcopy(FW_RECORD))
        # Same delta (measured against the other books, never the
        # book itself) and byte-identical store contents.
        self.assertEqual(first, second)
        self.assertEqual(snapshot, (self.reg.characters(EMPYREAN),
                                    self.reg.family_links(EMPYREAN)))

    def test_second_book_accumulates(self):
        sr.populate_from_record(self.reg, copy.deepcopy(FW_RECORD))
        delta = sr.populate_from_record(self.reg, copy.deepcopy(IF_RECORD))
        self.assertEqual(delta, {
            "series_key": EMPYREAN,
            "characters_added": 1,      # Sloane Mairi
            "characters_updated": 3,    # Violet, Xaden, Andarna
            # Only Violet's "Sorrengail" is new to the series;
            # every other attested pair exists from book 1. The
            # Mira->Violet edge is skipped here: Mira is not in
            # book 2's shipped cast, so her endpoint cannot
            # resolve from this record.
            "aliases_added": 1,
            "family_links_recorded": 0,
            "family_links_skipped": 1,
        })
        chars = _by_name(self.reg.characters(EMPYREAN))
        violet = chars["Violet Sorrengail"]
        self.assertEqual(
            {(b["book_key"], b["position"]) for b in violet["books"]},
            {(FW_BOOK, 1), (IF_BOOK, 2)})
        self.assertEqual(violet["first_position"], 1)
        self.assertEqual(violet["last_position"], 2)
        by_alias = {a["alias"]: a for a in violet["aliases"]}
        self.assertEqual(by_alias["Sorrengail"]["books"], [IF_BOOK])
        self.assertEqual(by_alias["Vi"]["books"], [FW_BOOK, IF_BOOK])
        # Andarna was a minor in book 1 and a principal in book 2;
        # presence keeps each book's own kind.
        kinds = {b["book_key"]: b["list_kind"]
                 for b in chars["Andarna"]["books"]}
        self.assertEqual(kinds, {FW_BOOK: "minor", IF_BOOK: "principal"})
        # Book 1's family rows are untouched by book 2's fold.
        self.assertEqual(len(self.reg.family_links(EMPYREAN)), 2)

    def test_cross_series_isolation(self):
        sr.populate_from_record(self.reg, copy.deepcopy(FW_RECORD))
        daemon = {
            "title": "Daemon", "author": "Daniel Suarez",
            "work_id": "daemon-book-1",
            "series_name": "Daemon", "series_position": 1,
            "series_source": "hardcover",
            "series_key": "hc-series:1056",
            "hardcover": {"compilation": False},
            "characters": [
                # The same surface name as an Empyrean character:
                # keyed per series, it must stay a separate
                # registry character.
                {"name": "Tairn", "aliases": []},
                {"name": "Peter Sebeck", "aliases": []},
            ],
            "minor_characters": [],
            "relationships": [],
        }
        sr.populate_from_record(self.reg, daemon)
        self.assertEqual(set(self.reg.series_keys()),
                         {EMPYREAN, "hc-series:1056"})
        emp = _by_name(self.reg.characters(EMPYREAN))
        dae = _by_name(self.reg.characters("hc-series:1056"))
        self.assertEqual(len(emp["Tairn"]["books"]), 1)
        self.assertEqual(emp["Tairn"]["aliases"],
                         [{"alias": "Tairneanach", "books": [FW_BOOK]}])
        self.assertEqual(dae["Tairn"]["books"], [{
            "book_key": "daemon-book-1", "list_kind": "principal",
            "position": 1}])
        self.assertEqual(dae["Tairn"]["aliases"], [])

    def test_compilation_is_skipped(self):
        record = copy.deepcopy(FW_RECORD)
        record["hardcover"] = {"compilation": True}
        self.assertIsNone(sr.populate_from_record(self.reg, record))
        self.assertEqual(self.reg.series_keys(), [])

    def test_no_series_key_is_skipped(self):
        record = copy.deepcopy(FW_RECORD)
        del record["series_key"]
        self.assertIsNone(sr.populate_from_record(self.reg, record))
        record["series_key"] = None
        self.assertIsNone(sr.populate_from_record(self.reg, record))
        self.assertEqual(self.reg.series_keys(), [])

    def test_family_endpoint_resolves_via_alias(self):
        record = copy.deepcopy(FW_RECORD)
        # "Violence" is Violet's shipped alias in this record: an
        # endpoint recorded under the alias form resolves to her.
        record["relationships"].append({
            "from": "Violence", "to": "Mira Sorrengail",
            "type": "sibling", "validation_state": "validated"})
        delta = sr.populate_from_record(self.reg, record)
        self.assertEqual(delta["family_links_recorded"], 3)
        self.assertEqual(delta["family_links_skipped"], 1)

    def test_later_book_surface_becomes_alias_not_rename(self):
        sr.populate_from_record(self.reg, copy.deepcopy(FW_RECORD))
        record = copy.deepcopy(IF_RECORD)
        # Book 2 ships Violet under a fuller surface with the
        # same normalized key. The registry canonical (book 1's
        # writer) must not move; the new surface attests as an
        # alias of book 2 instead.
        record["characters"][0] = {
            "name": "Violet  Sorrengail", "aliases": []}
        sr.populate_from_record(self.reg, record)
        violet = _by_name(self.reg.characters(EMPYREAN))[
            "Violet Sorrengail"]
        self.assertEqual(violet["canonical_name"], "Violet Sorrengail")
        by_alias = {a["alias"]: a for a in violet["aliases"]}
        self.assertEqual(by_alias["Violet  Sorrengail"]["books"],
                         [IF_BOOK])

    def test_position_unknown_book(self):
        record = copy.deepcopy(FW_RECORD)
        record["series_position"] = None
        sr.populate_from_record(self.reg, record)
        violet = _by_name(self.reg.characters(EMPYREAN))[
            "Violet Sorrengail"]
        self.assertIsNone(violet["books"][0]["position"])
        self.assertIsNone(violet["first_position"])
        self.assertIsNone(violet["last_position"])

    def test_book_key_fallback_without_work_id(self):
        record = copy.deepcopy(FW_RECORD)
        del record["work_id"]
        sr.populate_from_record(self.reg, record)
        violet = _by_name(self.reg.characters(EMPYREAN))[
            "Violet Sorrengail"]
        self.assertEqual(violet["books"][0]["book_key"],
                         "title:fourth wing|rebecca yarros")

    def test_custom_family_types_are_honoured(self):
        # The pipeline passes its live vocabulary; a type outside
        # it is not a family link even when an endpoint resolves.
        delta = sr.populate_from_record(
            self.reg, copy.deepcopy(FW_RECORD),
            family_types=frozenset({"sibling"}))
        self.assertEqual(delta["family_links_recorded"], 2)
        delta = sr.populate_from_record(
            self.reg, copy.deepcopy(FW_RECORD),
            family_types=frozenset())
        self.assertEqual(delta["family_links_recorded"], 0)
        self.assertEqual(delta["family_links_skipped"], 0)


class SeamTests(unittest.TestCase):
    """The pipeline wrapper beside _land_series_identity."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reg = _registry(self._tmp.name)
        self.addCleanup(self.reg.close)
        self._saved_registry = pp._SR_REGISTRY
        self._saved_config = pp.CONFIG.get("series_registry")
        pp._SR_REGISTRY = (sr, self.reg)
        pp.CONFIG["series_registry"] = True
        self.addCleanup(self._restore)

    def _restore(self):
        pp._SR_REGISTRY = self._saved_registry
        if self._saved_config is None:
            pp.CONFIG.pop("series_registry", None)
        else:
            pp.CONFIG["series_registry"] = self._saved_config

    def test_wrapper_lands_delta_block(self):
        record = copy.deepcopy(FW_RECORD)
        pp._populate_series_registry(record)
        self.assertEqual(record["series_registry"]["series_key"],
                         EMPYREAN)
        self.assertEqual(
            record["series_registry"]["characters_added"], 6)
        self.assertEqual(len(self.reg.characters(EMPYREAN)), 6)

    def test_non_series_record_is_byte_identical(self):
        record = {"title": "Standalone", "author": "Some Author",
                  "characters": [{"name": "Lone", "aliases": []}],
                  "minor_characters": [], "relationships": []}
        before = json.dumps(record, sort_keys=True)
        pp._populate_series_registry(record)
        self.assertEqual(json.dumps(record, sort_keys=True), before)
        self.assertEqual(self.reg.series_keys(), [])

    def test_kill_switch_off_writes_nothing(self):
        pp.CONFIG["series_registry"] = False
        record = copy.deepcopy(FW_RECORD)
        before = copy.deepcopy(record)
        pp._populate_series_registry(record)
        self.assertEqual(record, before)
        self.assertNotIn("series_registry", record)
        self.assertEqual(self.reg.series_keys(), [])

    def test_registry_failure_never_fails_the_book(self):
        class _Boom:
            def other_books_state(self, *a):
                raise RuntimeError("store on fire")

            def canonical_for(self, *a):
                raise RuntimeError("store on fire")

        pp._SR_REGISTRY = (sr, _Boom())
        record = copy.deepcopy(FW_RECORD)
        # Must not raise, and must not leave a block behind.
        pp._populate_series_registry(record)
        self.assertNotIn("series_registry", record)

    def test_population_failure_latches_off_for_the_run(self):
        # A store that fails once is broken, not per-book: the
        # wrapper warns once and latches off, so a folder scan
        # against a corrupt registry prints one warning, not one
        # per book (Phase 2 gate follow-up).
        class _Boom:
            def other_books_state(self, *a):
                raise RuntimeError("store on fire")

            def canonical_for(self, *a):
                raise RuntimeError("store on fire")

        pp._SR_REGISTRY = (sr, _Boom())
        out = io.StringIO()
        with redirect_stdout(out):
            pp._populate_series_registry(copy.deepcopy(FW_RECORD))
            pp._populate_series_registry(copy.deepcopy(FW_RECORD))
        self.assertEqual(
            out.getvalue().count("series registry population failed"),
            1)
        self.assertFalse(pp._SR_REGISTRY)


class RekeyTests(unittest.TestCase):
    """rekey_book: the store-level restatement the pipeline seam
    runs when work resolution completes (Phase 3, Step 0). The
    fold lands under the pre-resolution fallback key; resolution
    moves it under the work_id so one book has one presence key
    however many times, and from whichever artefact, it folds."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reg = _registry(self._tmp.name)
        self.addCleanup(self.reg.close)

    def _fold_without_work_id(self):
        record = copy.deepcopy(FW_RECORD)
        record.pop("work_id")
        sr.populate_from_record(self.reg, record)
        return sr.fallback_book_key_for(record)

    def test_rekey_moves_presence_aliases_and_family(self):
        fallback = self._fold_without_work_id()
        self.assertNotEqual(fallback, FW_BOOK)
        self.assertTrue(
            self.reg.rekey_book(EMPYREAN, fallback, FW_BOOK))
        chars = _by_name(self.reg.characters(EMPYREAN))
        violet = chars["Violet Sorrengail"]
        self.assertEqual([b["book_key"] for b in violet["books"]],
                         [FW_BOOK])
        self.assertTrue(all(a["books"] == [FW_BOOK]
                            for a in violet["aliases"]))
        self.assertTrue(all(
            l["book_key"] == FW_BOOK
            for l in self.reg.family_links(EMPYREAN)))

    def test_rekey_replaces_a_stale_fold_under_the_new_key(self):
        # The finished record folded first (work_id key); the
        # live fold then landed under the fallback and restates.
        # The restated fold replaces the stale one — rows never
        # merge, and no character keeps a double attestation.
        sr.populate_from_record(self.reg, copy.deepcopy(FW_RECORD))
        fallback = self._fold_without_work_id()
        self.assertTrue(
            self.reg.rekey_book(EMPYREAN, fallback, FW_BOOK))
        chars = _by_name(self.reg.characters(EMPYREAN))
        self.assertTrue(all(len(c["books"]) == 1
                            for c in chars.values()))
        self.assertTrue(all(
            a["books"] == [FW_BOOK]
            for c in chars.values() for a in c["aliases"]))

    def test_rekey_noop_cases(self):
        fallback = self._fold_without_work_id()
        self.assertFalse(
            self.reg.rekey_book(EMPYREAN, fallback, fallback))
        self.assertFalse(self.reg.rekey_book(EMPYREAN, "", "x"))
        self.assertFalse(
            self.reg.rekey_book(EMPYREAN, "no-such-book", "x"))
        self.assertFalse(self.reg.rekey_book("", fallback, "x"))
        # Nothing moved: the fold is still under the fallback.
        chars = _by_name(self.reg.characters(EMPYREAN))
        self.assertEqual(
            [b["book_key"]
             for b in chars["Violet Sorrengail"]["books"]],
            [fallback])


if __name__ == "__main__":
    unittest.main()
