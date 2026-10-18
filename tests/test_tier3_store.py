"""Tests for tier3_store.py, the standalone Tier 3 local store
(design: tier3-local-store-design-2026-10-18.md).

Hermetic throughout: synthetic preview artefacts and SQLite
databases in temp dirs, no network, no Supabase. The Supabase
stored-set path is exercised only in its config-absent form
(fall through to derivation), because the contract that must
never regress is the resolution order and the loud failures.

The central pins: the stored set is excluded from Tier 3 however
it was resolved; an import replaces a book's characters and
candidates atomically while the push ledger — the User's own
curation data — survives untouched; the rejected-types record
is curation data of the same standing: it survives untouched
too, and a later import annotates a re-proposed rejected string
instead of re-queueing it; unconfirmed candidates of
all four kinds are captured, including a dropped type proposal
found only in the chapter deltas; and the report's
reconciliation equation (roster == stored + Tier 3) is checked,
not assumed.

Stdlib unittest only. Run: python -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store = _load("tier3_store_under_test", "tier3_store.py")


def _char(name, appearances=3, mentions=None, **kw):
    entry = {
        "name": name, "aliases": kw.get("aliases", []),
        "unresolved_mentions": mentions or [],
        "role": kw.get("role", "minor"),
        "description": kw.get("description", f"About {name}."),
        "appearances": appearances,
        "chapters_present": kw.get("chapters_present", 2),
        "grounded": kw.get("grounded", True),
        "evidence": kw.get("evidence", f"{name} appeared."),
    }
    return entry


def _claim(frm, to, ctype="friend", state=None, p=None,
           note=None, chapter="Chapter 1"):
    claim = {"from": frm, "to": to, "type": ctype,
             "evidence": "A quote.", "chapter": chapter,
             "source": "extracted"}
    if state is not None:
        claim["validation_state"] = state
    if p is not None:
        claim["validation_p"] = p
    if note is not None:
        claim["validation_note"] = note
    return claim


def _artefact(**over):
    """A small synthetic book: two principals, three minors.
    Minor B earns a stored row (validated claim); Minor C's only
    claim is review_required WITHOUT a probability (unscored —
    no row under the shipped rule); Minor D sources only a
    budget_exhausted claim."""
    art = {
        "title": "Test Book", "author": "An Author",
        "work_id": "work-test-1",
        "characters": [
            _char("Principal A", appearances=40,
                  mentions=["the stranger"]),
            _char("Principal B", appearances=30),
        ],
        "minor_characters": [
            _char("Minor B", appearances=5),
            _char("Minor C", appearances=4),
            _char("Minor D", appearances=2),
        ],
        "relationships": [],
        "minor_sourced_relationships": [
            _claim("Minor B", "Principal A", "child",
                   state="validated", p=0.9, note="kept"),
            _claim("Minor C", "Principal A", "friend",
                   state="review_required"),
            _claim("Minor D", "Principal B", "crewmate",
                   state="budget_exhausted"),
        ],
        "dropped_relationships": [
            _claim("Minor D", "Principal A", "spouse",
                   state="rejected", note="no_evidence"),
        ],
        "possible_merges": [
            {"name_a": "Minor C", "name_b": "Minor Cee",
             "p_yes": 0.72, "reason": "below threshold (0.85)"},
        ],
        "chapter_data": {"chapters": {
            "1": {"a": {"relationships": [
                # registered type, delta-only: NOT a proposal
                _claim("Principal A", "Minor B", "sire"),
                # unregistered type: a dropped proposal
                _claim("Minor D", "Principal A", "boss"),
            ]}},
        }},
    }
    art.update(over)
    return art


class Tier3Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "tier3.db")
        self.missing_config = str(self.dir / "no-config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write_artefact(self, art, name="book.json"):
        path = self.dir / name
        path.write_text(json.dumps(art), encoding="utf-8")
        return str(path)

    def import_it(self, art=None, **kw):
        path = self.write_artefact(art or _artefact())
        kw.setdefault("config_path", self.missing_config)
        return store.import_preview(self.db, path, **kw)

    def tier3_names(self, work_id="work-test-1"):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM tier3_characters "
                "WHERE work_id = ?", (work_id,))}
        finally:
            conn.close()

    def candidate_kinds(self, work_id="work-test-1"):
        conn = sqlite3.connect(self.db)
        try:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT kind, COUNT(*) FROM tier3_candidates "
                "WHERE work_id = ? GROUP BY kind", (work_id,))}
        finally:
            conn.close()

    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = store.main(argv)
        return code, out.getvalue()


class ImportTests(Tier3Case):
    def test_derivation_excludes_principals_and_scored_earners(self):
        result = self.import_it()
        self.assertEqual(result["stored_source"], "derived")
        # 2 principals + Minor B (validated) stored; C and D not.
        self.assertEqual(result["stored_count"], 3)
        self.assertEqual(self.tier3_names(), {"Minor C", "Minor D"})
        self.assertEqual(result["tier3_count"], 2)

    def test_unscored_review_required_does_not_earn(self):
        # Minor C's claim is review_required with no probability:
        # the probability is the proof a draw happened, so C
        # stays in Tier 3 carrying that claim.
        self.import_it()
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT claims_json FROM tier3_characters "
                "WHERE name = 'Minor C'").fetchone()
        finally:
            conn.close()
        claims = json.loads(row[0])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["validation_state"],
                         "review_required")

    def test_candidates_of_all_four_kinds(self):
        self.import_it()
        kinds = self.candidate_kinds()
        self.assertEqual(kinds.get("merge"), 1)
        self.assertEqual(kinds.get("claim"), 1)   # the no_evidence drop
        self.assertEqual(kinds.get("mention"), 1)  # "the stranger"
        self.assertEqual(kinds.get("type_proposal"), 1)  # "boss"

    def test_registered_delta_only_type_is_not_a_proposal(self):
        self.import_it()
        conn = sqlite3.connect(self.db)
        try:
            payloads = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM tier3_candidates "
                "WHERE kind = 'type_proposal'")]
        finally:
            conn.close()
        self.assertEqual([p["type"] for p in payloads], ["boss"])

    def test_no_chapter_data_means_no_type_proposals(self):
        art = _artefact()
        del art["chapter_data"]
        result = self.import_it(art)
        self.assertFalse(result["has_chapter_data"])
        self.assertNotIn("type_proposal", self.candidate_kinds())

    def test_character_rows_carry_the_record(self):
        self.import_it()
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM tier3_characters "
                "WHERE name = 'Minor D'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["appearances"], 2)
        self.assertEqual(row["grounded"], 1)
        self.assertEqual(row["description"], "About Minor D.")
        claims = json.loads(row["claims_json"])
        # D's budget_exhausted shipped claim AND his dropped one.
        self.assertEqual(len(claims), 2)


class StoredSetResolutionTests(Tier3Case):
    def test_stored_names_file_wins_over_derivation(self):
        names_file = self.dir / "stored.txt"
        names_file.write_text("Principal A\nMinor B\nMinor C\n",
                              encoding="utf-8")
        result = self.import_it(stored_names_path=str(names_file))
        self.assertEqual(result["stored_source"], "stored-names")
        # Derivation would also store Principal B; the file does
        # not name B, so B lands in Tier 3 — the file wins.
        self.assertEqual(self.tier3_names(),
                         {"Principal B", "Minor D"})

    def test_missing_roster_lists_fail_loudly(self):
        art = _artefact()
        del art["characters"]
        del art["minor_characters"]
        with self.assertRaises(store.Tier3Error):
            self.import_it(art)

    def test_derivation_without_claim_lists_fails_loudly(self):
        art = _artefact()
        del art["minor_sourced_relationships"]
        with self.assertRaises(store.Tier3Error):
            self.import_it(art)

    def test_missing_stored_names_file_fails_loudly(self):
        with self.assertRaises(store.Tier3Error):
            self.import_it(
                stored_names_path=str(self.dir / "nope.txt"))


class LedgerTests(Tier3Case):
    def test_approve_marks_the_list_and_unapprove_removes(self):
        self.import_it()
        code, _ = self.run_cli(
            ["approve", "--db", self.db, "--work", "Test Book",
             "--name", "Minor C"])
        self.assertEqual(code, 0)
        code, out = self.run_cli(
            ["list", "--db", self.db, "--work", "test book"])
        self.assertEqual(code, 0)
        self.assertIn("Minor C — 4 appearance(s)  "
                      "[approved for push]", out)
        code, _ = self.run_cli(
            ["unapprove", "--db", self.db, "--work", "Test Book",
             "--name", "Minor C"])
        self.assertEqual(code, 0)
        code, out = self.run_cli(
            ["list", "--db", self.db, "--work", "Test Book"])
        self.assertNotIn("[approved for push]", out)

    def test_approve_requires_a_tier3_name(self):
        self.import_it()
        code, _ = self.run_cli(
            ["approve", "--db", self.db, "--work", "Test Book",
             "--name", "Principal A"])  # stored, not Tier 3
        self.assertEqual(code, 2)
        code, _ = self.run_cli(
            ["unapprove", "--db", self.db, "--work", "Test Book",
             "--name", "Minor C"])  # never approved
        self.assertEqual(code, 2)

    def test_reimport_replaces_book_rows_but_ledger_survives(self):
        self.import_it()
        self.run_cli(["approve", "--db", self.db,
                      "--work", "Test Book", "--name", "Minor C"])
        # Mark the ledger row as pushed, as a future console
        # build would; an import must not touch any of it.
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(
                "UPDATE push_ledger SET pushed_at = 'T', "
                "prod_row_id = 'prod-123' WHERE name = 'Minor C'")
        conn.close()
        # A newer run of the same book: roster changed — Minor D
        # gone, Minor E new; C still Tier 3.
        art2 = _artefact()
        art2["minor_characters"] = [
            _char("Minor B", appearances=5),
            _char("Minor C", appearances=9),
            _char("Minor E", appearances=1),
        ]
        art2["minor_sourced_relationships"] = [
            c for c in art2["minor_sourced_relationships"]
            if c["from"] != "Minor D"]
        art2["dropped_relationships"] = []
        self.import_it(art2)
        self.assertEqual(self.tier3_names(),
                         {"Minor C", "Minor E"})
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT decided_at, pushed_at, prod_row_id "
                "FROM push_ledger WHERE name = 'Minor C'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "T")
        self.assertEqual(row[2], "prod-123")


class ReportTests(Tier3Case):
    def test_report_reconciles(self):
        self.import_it()
        report = store.build_report(self.db)
        self.assertIn("roster 5 == stored 3 + Tier 3 2 = 5 — PASS",
                      report)
        self.assertIn("verdict: RECONCILED", report)

    def test_report_fails_when_stored_names_leave_the_roster(self):
        names_file = self.dir / "stored.txt"
        names_file.write_text(
            "Principal A\nPrincipal B\nMinor B\nGhost Name\n",
            encoding="utf-8")
        result = self.import_it(stored_names_path=str(names_file))
        self.assertEqual(result["stored_absent_from_roster"],
                         ["Ghost Name"])
        report = store.build_report(self.db)
        self.assertIn("FAIL", report)
        self.assertIn("verdict: NOT RECONCILED", report)


def _alerts_artefact():
    """A book built to exercise every alert source class (the
    alert band: scored but unconfirmed, User 2026-10-18). The
    stored set is supplied by file in the tests below, so
    claim-earner derivation never moves a claim's source out of
    Tier 3 behind the test's back."""
    return {
        "title": "Alerts Book", "author": "An Author",
        "work_id": "work-alerts-1",
        "characters": [_char("Principal A", appearances=40),
                       _char("Principal B", appearances=30)],
        "minor_characters": [
            _char("Stored Minor", appearances=6),
            _char("Drop Source", appearances=5),
            _char("Down Source", appearances=5),
            _char("Kept Source", appearances=5),
            _char("Quiet Source", appearances=3),
        ],
        "relationships": [],
        "minor_sourced_relationships": [
            # A scored downgrade inside a Tier 3 character's
            # recorded claim set: it shipped, but as `other` —
            # the claimed type never confirmed. An alert.
            {"from": "Down Source", "to": "Principal A",
             "type": "other", "evidence": "A quote.",
             "chapter": "Chapter 2", "source": "extracted",
             "validation_state": "validated", "validation_p": 0.55,
             "validation_note": "downgraded"},
            # Validated and kept under its claimed type: shipped
            # confirmed, never an alert however high the score.
            _claim("Kept Source", "Principal B", "friend",
                   state="validated", p=0.91, note="kept"),
            # Unscored: no probability, so nothing to band.
            _claim("Quiet Source", "Principal A", "ally",
                   state="review_required"),
        ],
        "dropped_relationships": [
            # A scored drop: an alert. The same claim also sits
            # in Drop Source's recorded claim set — the digest
            # must show it exactly once.
            _claim("Drop Source", "Principal B", "sibling",
                   state="rejected", p=0.42, note="dropped"),
            # An unscored drop: no alert.
            _claim("Quiet Source", "Principal B", "spouse",
                   state="rejected", note="no_evidence"),
        ],
        "possible_merges": [
            {"name_a": "Drop Source", "name_b": "Drop S.",
             "p_yes": 0.72, "reason": "below threshold (0.85)"},
            # At/above the auto-merge threshold the pair is not
            # in the ambiguous band: not an alert.
            {"name_a": "Kept Source", "name_b": "Kept S.",
             "p_yes": 0.9, "reason": "held above threshold"},
            # Unjudged name candidates carry no score at all.
            {"name_a": "Quiet Source", "name_b": "Quiet S.",
             "p_yes": None,
             "reason": "name candidate — not judged, not merged"},
            # Scored, but below the default floor.
            {"name_a": "Down Source", "name_b": "Down S.",
             "p_yes": 0.26, "reason": "below threshold (0.85)"},
        ],
    }


class AlertsTests(Tier3Case):
    def import_alerts_book(self):
        names_file = self.dir / "alerts-stored.txt"
        names_file.write_text(
            "Principal A\nPrincipal B\nStored Minor\n",
            encoding="utf-8")
        path = self.write_artefact(_alerts_artefact(),
                                   name="alerts.json")
        return store.import_preview(
            self.db, path, stored_names_path=str(names_file),
            config_path=self.missing_config)

    def test_alerts_cover_each_source_class_once(self):
        self.import_alerts_book()
        alerts = store.collect_alerts(self.db)
        # 0.72 merge + 0.55 downgrade + 0.42 scored drop. The
        # 0.42 claim lives both in the candidates table and in
        # Drop Source's claim set; identity dedupe shows it once.
        self.assertEqual([a["score"] for a in alerts],
                         [0.72, 0.55, 0.42])
        by_score = {a["score"]: a for a in alerts}
        self.assertEqual(by_score[0.72]["kind"], "merge")
        self.assertEqual(by_score[0.55]["kind"], "claim")
        self.assertEqual(by_score[0.55]["reason"], "downgraded")
        self.assertEqual(by_score[0.42]["kind"], "claim")
        self.assertEqual(by_score[0.42]["type"], "sibling")
        self.assertEqual(
            sum(1 for a in alerts if a["score"] == 0.42), 1)

    def test_unscored_and_confirmed_claims_never_alert(self):
        self.import_alerts_book()
        alerts = store.collect_alerts(self.db)
        labels = " ".join(a["label"] for a in alerts)
        # Quiet Source: unscored drop, unscored review_required
        # claim, unjudged merge — nothing to band anywhere.
        self.assertNotIn("Quiet Source", labels)
        # Kept Source's 0.91 friend claim shipped validated under
        # its claimed type, and his 0.9 merge sits above the band.
        self.assertNotIn("Kept Source", labels)
        self.assertFalse(any(a["type"] == "friend"
                             for a in alerts))

    def test_floor_is_respected(self):
        self.import_alerts_book()
        self.assertEqual(
            [a["score"] for a in
             store.collect_alerts(self.db, floor=0.5)],
            [0.72, 0.55])
        self.assertEqual(
            [a["score"] for a in
             store.collect_alerts(self.db, floor=0.2)],
            [0.72, 0.55, 0.42, 0.26])

    def test_alerts_text_groups_sorts_and_explains(self):
        self.import_alerts_book()
        text = store.build_alerts(self.db)
        self.assertIn("Alerts Book", text)
        self.assertIn("downgraded", text)
        self.assertLess(text.index("0.72"), text.index("0.55"))
        self.assertLess(text.index("0.55"), text.index("0.42"))
        code, out = self.run_cli(
            ["alerts", "--db", self.db, "--floor", "0.5"])
        self.assertEqual(code, 0)
        self.assertIn("0.55", out)
        self.assertNotIn("0.42", out)

    def test_work_filter_and_unknown_work(self):
        self.import_alerts_book()
        self.import_it()  # the standard Test Book as a second book
        only = store.collect_alerts(self.db, work="Alerts Book")
        self.assertEqual(len(only), 3)
        self.assertTrue(all(a["title"] == "Alerts Book"
                            for a in only))
        other = store.collect_alerts(self.db, work="Test Book")
        # Test Book's sole alert: its 0.72 possible merge (its
        # dropped claim is unscored, its Tier 3 claims unscored).
        self.assertEqual([a["score"] for a in other], [0.72])
        with self.assertRaises(store.Tier3Error):
            store.collect_alerts(self.db, work="No Such Book")

    def test_alerts_are_read_only(self):
        self.import_alerts_book()
        before = Path(self.db).read_bytes()
        store.collect_alerts(self.db)
        store.build_alerts(self.db)
        self.assertEqual(Path(self.db).read_bytes(), before)

    def test_alerts_without_a_store_fail_loudly(self):
        with self.assertRaises(store.Tier3Error):
            store.collect_alerts(str(self.dir / "nope.db"))


def _stored_source_artefact():
    """A book whose stored set (supplied by file) includes a
    minor whose shipped claims exercise the stored-source pass:
    a downgrade and a scored review_required claim must be
    recorded as candidates; a clean keep and an unscored claim
    must not. A Tier 3 minor carries a downgrade of his own,
    which must keep flowing through his character row only."""
    return {
        "title": "Stored Source Book", "author": "An Author",
        "work_id": "work-stored-1",
        "characters": [_char("Principal A", appearances=40),
                       _char("Principal B", appearances=30)],
        "minor_characters": [
            _char("Stored Spouse", appearances=8),
            _char("Tier3 Minor", appearances=4),
        ],
        "relationships": [],
        "minor_sourced_relationships": [
            # The canonical stored-source downgrade: shipped,
            # state validated, type rewritten to `other` — the
            # claimed type never confirmed.
            {"from": "Stored Spouse", "to": "Principal A",
             "type": "other", "evidence": "A quote.",
             "chapter": "Chapter 5", "source": "extracted",
             "validation_state": "validated",
             "validation_p": 0.58,
             "validation_note": "downgraded"},
            # A stored source's review_required claim WITH a
            # probability: scored, unconfirmed — same class.
            _claim("Stored Spouse", "Principal B", "sibling",
                   state="review_required", p=0.47,
                   chapter="Chapter 6"),
            # A clean validated keep under its claimed type:
            # never recorded, however high the score.
            _claim("Stored Spouse", "Principal A", "spouse",
                   state="validated", p=0.93, note="kept",
                   chapter="Chapter 7"),
            # Unscored: no probability, nothing to record.
            _claim("Stored Spouse", "Principal B", "friend",
                   state="review_required", chapter="Chapter 8"),
            # A Tier 3 source's downgrade: recorded on his
            # character row, never as a candidate.
            {"from": "Tier3 Minor", "to": "Principal A",
             "type": "other", "evidence": "A quote.",
             "chapter": "Chapter 9", "source": "extracted",
             "validation_state": "validated",
             "validation_p": 0.66,
             "validation_note": "downgraded"},
        ],
        "dropped_relationships": [],
        "possible_merges": [],
    }


class StoredSourceCandidateTests(Tier3Case):
    def import_stored_book(self, art=None):
        names_file = self.dir / "stored-source-names.txt"
        names_file.write_text(
            "Principal A\nPrincipal B\nStored Spouse\n",
            encoding="utf-8")
        path = self.write_artefact(
            art or _stored_source_artefact(), name="stored.json")
        return store.import_preview(
            self.db, path, stored_names_path=str(names_file),
            config_path=self.missing_config)

    def claim_candidate_payloads(self, work_id="work-stored-1"):
        conn = sqlite3.connect(self.db)
        try:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM tier3_candidates "
                "WHERE work_id = ? AND kind = 'claim'",
                (work_id,))]
        finally:
            conn.close()

    def test_stored_source_downgrade_is_recorded_and_flagged(self):
        self.import_stored_book()
        payloads = self.claim_candidate_payloads()
        # Exactly the downgrade and the scored review_required
        # claim — the keep and the unscored claim stay out.
        self.assertEqual(len(payloads), 2)
        self.assertTrue(all(p.get("stored_source") is True
                            for p in payloads))
        by_p = {p["validation_p"]: p for p in payloads}
        self.assertEqual(by_p[0.58]["validation_note"],
                         "downgraded")
        self.assertEqual(by_p[0.47]["validation_state"],
                         "review_required")

    def test_stored_source_alerts_carry_the_marker(self):
        self.import_stored_book()
        alerts = store.collect_alerts(self.db)
        # Closest calls first: the Tier 3 downgrade (via his
        # claim set), then the two stored-source candidates.
        self.assertEqual([a["score"] for a in alerts],
                         [0.66, 0.58, 0.47])
        flagged = {a["score"] for a in alerts
                   if a.get("stored_source")}
        self.assertEqual(flagged, {0.58, 0.47})
        text = store.build_alerts(self.db)
        self.assertEqual(text.count("[stored source]"), 2)

    def test_downgrade_alert_names_the_claimed_type_when_stamped(self):
        # Artefacts from 2.13.31 on stamp the pre-downgrade type
        # on the claim (claimed_type): the digest line renders
        # the downgrade as "claimed spouse → other" instead of
        # the bare "type other" every downgrade used to share.
        art = _stored_source_artefact()
        for rel in art["minor_sourced_relationships"]:
            if rel.get("validation_note") == "downgraded" \
                    and rel["from"] == "Stored Spouse":
                rel["claimed_type"] = "spouse"
        self.import_stored_book(art)
        text = store.build_alerts(self.db)
        self.assertIn("claimed spouse → other", text)
        # The Tier 3 minor's own downgrade carries no stamp (his
        # artefact predates it, in story terms): the fallback
        # rendering still shows the recorded type plainly.
        self.assertIn("type other", text)

    def test_stored_source_keep_and_unscored_never_recorded(self):
        self.import_stored_book()
        pairs = {(p["from"], p.get("type"))
                 for p in self.claim_candidate_payloads()}
        # The 0.93 spouse keep confirmed under its claimed type.
        self.assertNotIn(("Stored Spouse", "spouse"), pairs)
        # The unscored friend claim carries no probability.
        self.assertNotIn(("Stored Spouse", "friend"), pairs)

    def test_tier3_source_downgrade_stays_on_the_character_row(self):
        self.import_stored_book()
        self.assertEqual(self.tier3_names("work-stored-1"),
                         {"Tier3 Minor"})
        self.assertFalse(any(
            p["from"] == "Tier3 Minor"
            for p in self.claim_candidate_payloads()))
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT claims_json FROM tier3_characters "
                "WHERE name = 'Tier3 Minor'").fetchone()
        finally:
            conn.close()
        claims = json.loads(row[0])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["validation_note"],
                         "downgraded")
        # The roster reconciliation is untouched by the new
        # candidates: they are not characters.
        report = store.build_report(self.db)
        self.assertIn("roster 4 == stored 3 + Tier 3 1 = 4 — "
                      "PASS", report)

    def test_stored_source_claim_in_two_lists_recorded_once(self):
        art = _stored_source_artefact()
        # The downgrade's identity ALSO recorded in the dropped
        # list. A claim lives in one list or the other in a real
        # artefact — the dedupe invariant is asserted, not
        # assumed from that.
        art["dropped_relationships"] = [
            {"from": "Stored Spouse", "to": "Principal A",
             "type": "other", "evidence": "A quote.",
             "chapter": "Chapter 5", "source": "extracted",
             "validation_state": "rejected",
             "validation_p": 0.58,
             "validation_note": "dropped"}]
        self.import_stored_book(art)
        same = [p for p in self.claim_candidate_payloads()
                if p["from"] == "Stored Spouse"
                and p["to"] == "Principal A"
                and p.get("chapter") == "Chapter 5"]
        self.assertEqual(len(same), 1)
        alerts = store.collect_alerts(self.db)
        self.assertEqual(
            sum(1 for a in alerts if a["score"] == 0.58), 1)


class RejectedTypesTests(Tier3Case):
    """The rejected-wins record for type proposals (this seat's
    ruling on the Data & Sync parity flag): a type string the
    User rejected is never re-queued as a fresh proposal by a
    later import, but the proposal row is kept — annotated with
    the recorded reason and date — because its occurrence is
    evidence. The record itself is curation data, like the push
    ledger: imports never modify it."""

    def proposals(self, work_id="work-test-1"):
        conn = sqlite3.connect(self.db)
        try:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM tier3_candidates "
                "WHERE work_id = ? AND kind = 'type_proposal'",
                (work_id,))]
        finally:
            conn.close()

    def fresh_proposal_types(self, work_id="work-test-1"):
        return [p["type"] for p in self.proposals(work_id)
                if "previously_rejected" not in p]

    def reject_boss(self, reason="not a relationship"):
        code, _ = self.run_cli(
            ["reject-type", "--db", self.db, "--type", "boss",
             "--reason", reason])
        self.assertEqual(code, 0)

    def test_reject_then_reimport_annotates_and_does_not_requeue(self):
        result = self.import_it()  # 'boss' proposed fresh
        self.assertEqual(self.fresh_proposal_types(), ["boss"])
        self.assertEqual(result["previously_rejected_types"], [])
        self.reject_boss("duplicate of an existing type")
        result = self.import_it()
        # The row is kept (occurrence evidence) — the candidates
        # table still holds one type_proposal — but it is no
        # longer a fresh proposal: it carries the rejection.
        self.assertEqual(len(self.proposals()), 1)
        self.assertEqual(self.fresh_proposal_types(), [])
        hit = self.proposals()[0]["previously_rejected"]
        self.assertEqual(hit["reason"],
                         "duplicate of an existing type")
        self.assertTrue(hit["decided_at"])
        self.assertEqual(result["previously_rejected_types"],
                         ["boss"])

    def test_rejection_recorded_before_any_import_applies(self):
        # The record is consulted at import, so rejecting a
        # string no run has proposed yet governs the first
        # import that proposes it.
        self.reject_boss()
        self.import_it()
        self.assertEqual(self.fresh_proposal_types(), [])
        self.assertEqual(len(self.proposals()), 1)

    def test_unreject_restores_fresh_on_next_import(self):
        self.import_it()
        self.reject_boss()
        self.import_it()
        self.assertEqual(self.fresh_proposal_types(), [])
        code, _ = self.run_cli(
            ["unreject-type", "--db", self.db, "--type", "boss"])
        self.assertEqual(code, 0)
        # Already-imported rows keep the annotation; only the
        # NEXT import re-derives the book's candidates.
        self.assertEqual(self.fresh_proposal_types(), [])
        self.import_it()
        self.assertEqual(self.fresh_proposal_types(), ["boss"])
        code, _ = self.run_cli(
            ["unreject-type", "--db", self.db, "--type", "boss"])
        self.assertEqual(code, 2)  # nothing left to remove

    def test_reject_without_a_reason_fails(self):
        # Missing entirely: an argparse usage error (SystemExit
        # 2 before any command runs). Blank: refused as a
        # Tier3Error for the same reason — a rejection whose
        # reason says nothing is not a reasoned rejection.
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                store.main(["reject-type", "--db", self.db,
                            "--type", "boss"])
        self.assertEqual(cm.exception.code, 2)
        code, _ = self.run_cli(
            ["reject-type", "--db", self.db, "--type", "boss",
             "--reason", "   "])
        self.assertEqual(code, 2)
        # Neither failure path recorded anything (both refuse
        # before the store is even opened, so the record is
        # read back through the tool's own connection).
        conn = store._connect(self.db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM rejected_types") \
                .fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_record_survives_reimport_byte_intact(self):
        self.import_it()
        self.reject_boss("covered by crewmate")
        conn = sqlite3.connect(self.db)
        try:
            before = conn.execute(
                "SELECT type, reason, decided_at "
                "FROM rejected_types").fetchall()
        finally:
            conn.close()
        self.import_it()
        self.import_it()
        conn = sqlite3.connect(self.db)
        try:
            after = conn.execute(
                "SELECT type, reason, decided_at "
                "FROM rejected_types").fetchall()
        finally:
            conn.close()
        self.assertEqual(before, after)
        self.assertEqual(after[0][0], "boss")

    def test_list_groups_previously_rejected_apart(self):
        self.import_it()
        self.reject_boss("not a relationship")
        self.import_it()
        code, out = self.run_cli(["list", "--db", self.db])
        self.assertEqual(code, 0)
        self.assertIn("previously rejected type proposals: "
                      "boss — not a relationship", out)
        # ...and the fresh candidate counts no longer include
        # it: the standard book's only type proposal is 'boss'.
        cand_line = next(l for l in out.splitlines()
                         if l.strip().startswith("candidates:"))
        self.assertNotIn("type_proposal", cand_line)

    def test_report_shows_the_record(self):
        self.import_it()
        self.reject_boss()
        report = store.build_report(self.db)
        self.assertIn("rejected types: 1 — boss", report)

    def test_alerts_ignore_type_proposals(self):
        # The digest reads claims and merges only — a vocabulary
        # question is not a near-miss claim, fresh or rejected.
        self.import_it()
        self.reject_boss()
        self.import_it()
        alerts = store.collect_alerts(self.db)
        self.assertTrue(alerts)
        self.assertTrue(all(a["kind"] in ("claim", "merge")
                            for a in alerts))
        self.assertEqual([a["score"] for a in alerts], [0.72])


if __name__ == "__main__":
    unittest.main()
