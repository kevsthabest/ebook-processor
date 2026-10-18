"""Claim-earner character storage (ruled 2026-10-18).

The principal filter keeps a claim out of the stored set whenever either
endpoint is a non-principal, and character rows are built only for the
filtered character list — so a judged claim sourced by a minor character
shipped in the run's judgement but was never stored: the claim had no
source row to embed on, and the wholesale rewrite deleted the character's
previously stored row with it. (Live case, Daemon run 20: a spouse claim
the judge kept at 0.94 and two child claims downgraded to "other", all
sourced by non-principals, none stored.)

The rule: the stored character set is the principals plus any character
who sources at least one shipping claim the validator actually judged
(validation_state validated or review_required). Never-judged claims
(budget_exhausted / validator_error) earn no row.

Fixtures are built in the post-filter shape the v2 pipeline hands
write_claims: characters = principals, minor_characters = the rest of
the roster, relationships = both-endpoints-principal claims, and
minor_sourced_relationships = the surviving claims the filter removed
solely because their source is not a principal.

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

_PRINCIPAL = {"name": "Detective Peter Sebeck", "role": "protagonist",
              "description": "Ventura County detective.",
              "status": "alive", "confidence": 0.95}
_LAURA = {"name": "Laura Sebeck", "role": "minor",
          "description": "Wife of Peter Sebeck.", "status": "alive"}
_CHRIS = {"name": "Chris Sebeck", "role": "supporting",
          "description": "Son of Peter and Laura Sebeck.",
          "status": "alive"}
_MARILYN = {"name": "Marilyn Sebeck", "role": "minor", "status": "dead"}
_WAYNE = {"name": "Wayne Sebeck", "role": "minor", "status": "dead"}
_JOSEPH = {"name": "Joseph Sebeck", "role": "minor", "status": "alive"}


def _claim(frm, to, typ, state, note, p=0.9, evidence="A binding sentence."):
    return {"from": frm, "to": to, "type": typ, "evidence": evidence,
            "source": "extracted", "validation_note": note,
            "validation_p": p, "validation_state": state}


def _laura_claim():
    # The run-20 case: mined-window spouse claim, judge kept it at 0.94.
    return _claim("Laura Sebeck", "Peter Andrew Sebeck", "spouse",
                  "validated", "kept", p=0.94)


def _chris_claim():
    # The run-20 case: child claim judged, downgraded to "other".
    return _claim("Chris Sebeck", "Detective Peter Sebeck", "other",
                  "validated", "downgraded", p=0.64)


def _daemon_result(**over):
    base = {"characters": [_PRINCIPAL],
            "minor_characters": [_LAURA, _CHRIS],
            "relationships": [],
            "minor_sourced_relationships": [_laura_claim(), _chris_claim()]}
    base.update(over)
    return _result(**base)


def _stored_rows(fake):
    return {r["name"]: r for r in fake.tables.get("book_characters", [])}


class TestValidatedKeepEarnsRow(_ReplaceBase):
    def test_minor_sourcing_validated_keep_gets_row(self):
        fake = self.install(_StatefulSB({}))
        errors = pp.write_claims(1, _daemon_result(
            minor_characters=[_LAURA],
            minor_sourced_relationships=[_laura_claim()]))
        self.assertEqual(errors, 0)
        rows = _stored_rows(fake)
        self.assertIn("Laura Sebeck", rows)
        laura = rows["Laura Sebeck"]
        # Stored identically to a principal: same row shape and fields
        # from her roster entry.
        self.assertEqual(laura["role"], "minor")
        self.assertEqual(laura["description"], "Wife of Peter Sebeck.")
        self.assertEqual(laura["vitality"], "alive")
        self.assertEqual(laura["status"], "candidate")
        self.assertEqual(laura["source_type"], "ai")
        rels = laura["relationships"]
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["to"], "Peter Andrew Sebeck")
        self.assertEqual(rels[0]["type"], "spouse")
        self.assertEqual(rels[0]["validation_state"], "validated")


class TestDowngradeEarnsRow(_ReplaceBase):
    def test_minor_sourcing_downgrade_gets_row_typed_other(self):
        fake = self.install(_StatefulSB({}))
        errors = pp.write_claims(1, _daemon_result(
            minor_characters=[_CHRIS],
            minor_sourced_relationships=[_chris_claim()]))
        self.assertEqual(errors, 0)
        rows = _stored_rows(fake)
        self.assertIn("Chris Sebeck", rows)
        rels = rows["Chris Sebeck"]["relationships"]
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["to"], "Detective Peter Sebeck")
        # The downgrade is the stored type — the claim ships as "other".
        self.assertEqual(rels[0]["type"], "other")
        self.assertEqual(rels[0]["validation_state"], "validated")


class TestUnjudgedClaimsEarnNoRow(_ReplaceBase):
    # Preservation pin by design: this passes before the fix too (no
    # minor stored anything), and its job is to fail any implementation
    # that overreaches — storing every minor, or treating an unjudged
    # claim as a verdict.
    def test_budget_exhausted_and_validator_error_earn_no_row(self):
        fake = self.install(_StatefulSB({}))
        claims = [
            _claim("Marilyn Sebeck", "Detective Peter Sebeck", "spouse",
                   "budget_exhausted", "budget_exhausted", p=None),
            _claim("Wayne Sebeck", "Detective Peter Sebeck", "spouse",
                   "validator_error", "validator_error", p=None),
        ]
        errors = pp.write_claims(1, _daemon_result(
            minor_characters=[_MARILYN, _WAYNE],
            minor_sourced_relationships=claims))
        self.assertEqual(errors, 0)
        rows = _stored_rows(fake)
        self.assertNotIn("Marilyn Sebeck", rows)
        self.assertNotIn("Wayne Sebeck", rows)
        self.assertEqual(sorted(rows), ["Detective Peter Sebeck"])


class TestEvidencelessClaimEarnsNoRow(_ReplaceBase):
    # A claim the write pass cannot store (no evidence — it stays in
    # the preview only) is not a shipping claim, so even in a judged
    # state it earns its source no row: the row would embed nothing.
    def test_evidenceless_validated_claim_earns_no_row(self):
        fake = self.install(_StatefulSB({}))
        claim = _laura_claim()
        claim["evidence"] = ""
        errors = pp.write_claims(1, _daemon_result(
            minor_characters=[_LAURA],
            minor_sourced_relationships=[claim]))
        self.assertEqual(errors, 0)
        rows = _stored_rows(fake)
        self.assertNotIn("Laura Sebeck", rows)
        self.assertEqual(sorted(rows), ["Detective Peter Sebeck"])


class TestReviewRequiredEarnsRow(_ReplaceBase):
    def test_minor_sourcing_review_required_claim_gets_row(self):
        fake = self.install(_StatefulSB({}))
        claim = _claim("Joseph Sebeck", "Detective Peter Sebeck", "parent",
                       "review_required", "", p=0.5)
        errors = pp.write_claims(1, _daemon_result(
            minor_characters=[_JOSEPH],
            minor_sourced_relationships=[claim]))
        self.assertEqual(errors, 0)
        rows = _stored_rows(fake)
        self.assertIn("Joseph Sebeck", rows)
        rels = rows["Joseph Sebeck"]["relationships"]
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["validation_state"], "review_required")


class TestDerivedStateEarnsRow(_ReplaceBase):
    # Claims built outside the stamping pass may carry a note but no
    # recorded state; the row decision derives the state from the note
    # exactly as _build_rel_map does when stamping the stored claim.
    def test_note_only_claim_derives_validated_and_earns(self):
        fake = self.install(_StatefulSB({}))
        claim = _laura_claim()
        del claim["validation_state"]
        errors = pp.write_claims(1, _daemon_result(
            minor_characters=[_LAURA],
            minor_sourced_relationships=[claim]))
        self.assertEqual(errors, 0)
        rows = _stored_rows(fake)
        self.assertIn("Laura Sebeck", rows)
        self.assertEqual(
            rows["Laura Sebeck"]["relationships"][0]["validation_state"],
            "validated")


class TestFullClaimSetRidesAlong(_ReplaceBase):
    # The qualifying claim earns the row; the character's OTHER shipping
    # claims embed too — the stored set is the character's full shipping
    # claim set, not the one claim that qualified.
    def test_unjudged_claim_of_an_earner_embeds_too(self):
        fake = self.install(_StatefulSB({}))
        extra = _claim("Laura Sebeck", "Detective Peter Sebeck", "spouse",
                       "budget_exhausted", "budget_exhausted", p=None)
        errors = pp.write_claims(1, _daemon_result(
            minor_characters=[_LAURA],
            minor_sourced_relationships=[_laura_claim(), extra]))
        self.assertEqual(errors, 0)
        rels = _stored_rows(fake)["Laura Sebeck"]["relationships"]
        self.assertEqual(len(rels), 2)
        self.assertEqual({r["validation_state"] for r in rels},
                         {"validated", "budget_exhausted"})


class TestPrincipalStorageUnchanged(_ReplaceBase):
    # Preservation pin: results in the classic shape (no retained-claims
    # keys at all) store exactly what they stored before the rule, and
    # an empty retention changes nothing either.
    def _principal_result(self, **over):
        claim = _claim("Detective Peter Sebeck", "Jon Ross", "colleague",
                       "validated", "kept", p=0.42)
        base = {"characters": [_PRINCIPAL,
                               {"name": "Jon Ross", "role": "protagonist",
                                "status": "alive"}],
                "relationships": [claim]}
        base.update(over)
        return _result(**base)

    def test_classic_shape_stores_principals_only(self):
        fake = self.install(_StatefulSB({}))
        errors = pp.write_claims(1, self._principal_result())
        self.assertEqual(errors, 0)
        rows = _stored_rows(fake)
        self.assertEqual(sorted(rows),
                         ["Detective Peter Sebeck", "Jon Ross"])
        rels = rows["Detective Peter Sebeck"]["relationships"]
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["type"], "colleague")

    def test_empty_retention_stores_identically(self):
        fake_a = self.install(_StatefulSB({}))
        self.assertEqual(pp.write_claims(1, self._principal_result()), 0)

        def strip(rows):
            return sorted(({k: v for k, v in r.items() if k != "id"}
                           for r in rows), key=lambda r: r["name"])

        before = strip(fake_a.tables["book_characters"])
        fake_b = self.install(_StatefulSB({}))
        res = self._principal_result(minor_characters=[],
                                     minor_sourced_relationships=[])
        self.assertEqual(pp.write_claims(1, res), 0)
        self.assertEqual(before, strip(fake_b.tables["book_characters"]))


class TestPreservationWithGrownSet(_ReplaceBase):
    def test_human_rows_survive_and_earner_inserts(self):
        fake = self.install(_StatefulSB({
            "book_characters": [
                # A human contributor row under the earner's own name:
                # preserved, and the run must not duplicate it.
                {"id": 7, "work_id": 1, "name": "Laura Sebeck",
                 "role": "supporting", "status": "candidate",
                 "source_type": "user", "appearance": None,
                 "vitality": None, "aliases": [],
                 "first_appearance_chapter": None},
                # A human rejection: preserved exactly.
                {"id": 8, "work_id": 1, "name": "Old Junk",
                 "status": "rejected", "source_type": "ai",
                 "appearance": "x", "vitality": "alive", "aliases": [],
                 "first_appearance_chapter": 1},
                # A stale processor principal row: replaced wholesale.
                {"id": 9, "work_id": 1, "name": "Detective Peter Sebeck",
                 "role": "supporting", "status": "candidate",
                 "source_type": "ai", "appearance": "stale",
                 "vitality": "alive", "aliases": [],
                 "first_appearance_chapter": 1},
            ]}))
        errors = pp.write_claims(1, _daemon_result())
        self.assertEqual(errors, 0)
        rows = _stored_rows(fake)
        # The human Laura row is the ONLY Laura row, untouched identity.
        lauras = [r for r in fake.tables["book_characters"]
                  if r["name"] == "Laura Sebeck"]
        self.assertEqual(len(lauras), 1)
        self.assertEqual(lauras[0]["id"], 7)
        self.assertEqual(lauras[0]["source_type"], "user")
        # The rejection survives.
        self.assertEqual(rows["Old Junk"]["id"], 8)
        self.assertEqual(rows["Old Junk"]["status"], "rejected")
        # The stale principal was replaced by a fresh row.
        self.assertNotEqual(rows["Detective Peter Sebeck"]["id"], 9)
        # And the set still grew: Chris's judged downgrade earned a row.
        self.assertIn("Chris Sebeck", rows)
        self.assertEqual(
            rows["Chris Sebeck"]["relationships"][0]["type"], "other")


class TestPlanMatchesWrite(_ReplaceBase):
    # plan_replace shares the row builders with write_claims, so the
    # preview plan must count the earner rows the write would insert —
    # plan and write drifting apart is how previews learn to understate.
    def test_plan_would_insert_counts_earners(self):
        fake = self.install(_StatefulSB({}))
        pp.CONFIG["supabase_url"] = "http://example.invalid"
        pp.CONFIG["supabase_key"] = "test-key"
        res = _daemon_result()
        plan = pp.plan_replace(1, res)
        self.assertEqual(plan["would_insert"]["characters"], 3)
        errors = pp.write_claims(1, res)
        self.assertEqual(errors, 0)
        self.assertEqual(len(fake.tables["book_characters"]), 3)


if __name__ == "__main__":
    unittest.main()
