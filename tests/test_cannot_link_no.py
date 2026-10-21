"""Cannot-link NOs must bite inside a run, not only across runs
(Decision, 2026-10-20).

The ruling: two named people must NEVER be linked — no merge, no
shared entry, and neither may carry the other's name forms as
aliases. The case that forced it: a Daemon run shipped "Major Pain
in the Ass" (a game screen name, operator unknown) carrying Karl
Devon's name forms ('Karl Devon', 'Major Karl Devon', 'Major
Devon', 'Devon') as aliases, while Devon himself had no entry.

The human-NO store already existed (RosterStore no_pairs, written
by --roster-unlink), but every enforcement point lived in the
cross-run store layer — and only under --persist-roster:

  * _roster_reconcile's alias pruning removed only aliases
    shadowed by another entry's primary, so a person with no
    entry of their own was never pruned from anyone's list;
  * the in-run merge passes (_roster_reconcile, post_pass_merge)
    never consulted a NO at all;
  * and the NO itself was unrecordable for this shape:
    --roster-unlink detaches an OBSERVED primary form from an
    entity, and an alias-only person has no form to detach.

These tests pin the in-run half: with a NO recorded for a pair,
reconcile prunes the other person's forms from an entry's aliases
and refuses to merge the pair; post_pass_merge never proposes the
pair to its tiers or its judge; unrelated pairs (a same-person
handle, an ordinary variant split) are untouched. The recording
half is pinned too: --roster-no writes the pair into a work's
store (found by key/id/title, or created under a work_id), and
the run-side loader finds it again.

Names from the ruling appear here as TEST DATA only — the ban
itself is data in the store, never pipeline code.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import copy
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

# The recorded NO, as form keys (what RosterStore.record_no stores).
NO_DEVON_MP = [("karl devon", "major pain in the ass")]
DEVON_FORMS = {"karl devon", "major karl devon", "major devon", "devon"}
DEVON_DISPLAY = {"Karl Devon", "Major Karl Devon", "Major Devon", "Devon"}
_FAKE_WID = "11111111-2222-3333-4444-555555555555"


def _fold(roster, name, aliases, chapter_idx, role="supporting"):
    """Fold one extracted character into the roster (real producer)."""
    pp._roster_update(
        roster,
        [{"name": name, "aliases": aliases, "role": role,
          "description": "", "evidence": ""}],
        chapter_idx)


def _run3_roster():
    """Daemon run-20261020-225628's shape, from its preview artefact:
    the screen name's entry carries Devon's forms (and Merritt's);
    Merritt's entry carries Devon forms too; Devon has no entry."""
    roster = {}
    _fold(roster, "Major Pain in the Ass",
          ["Karl Devon", "Major Devon", "Major Karl Devon", "Devon",
           "MPITA", "Merritt", "Agent Roy Merritt", "Tripwire"], 3)
    _fold(roster, 'Agent Roy "Tripwire" Merritt',
          ["Karl Devon", "MPITA", "Major Devon", "Merritt",
           "Roy Merritt", "Tripwire"], 5)
    return roster


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class RecordingJudge:
    """Tier 3 stub: records the pairs it is asked about."""

    model = "stub"

    def __init__(self, verdict=True, p=0.9):
        self.verdict, self.p = verdict, p
        self.calls = []

    def ask_same_person(self, na, da, ea, nb, db, eb):
        self.calls.append((na, nb))
        return self.verdict, self.p

    def asked_about(self, a, b):
        return any({x, y} == {a, b} for x, y in self.calls)


class TestReconcilePruning(unittest.TestCase):
    def test_run3_shape_devon_forms_pruned(self):
        roster = _run3_roster()
        stats = pp._roster_reconcile(roster, no_pairs=NO_DEVON_MP)
        self.assertEqual(stats["variants_merged"], 0)
        self.assertEqual(len(roster), 2)
        mp = roster["major pain in the ass"]
        # Every Devon form is gone, from the key set and the
        # display aliases alike.
        self.assertFalse(DEVON_FORMS & set(mp["alias_keys"]),
                         mp["alias_keys"])
        self.assertFalse(DEVON_DISPLAY & set(mp["aliases"]),
                         mp["aliases"])
        # The entry's OWN forms survive: the screen name's handle
        # is his, and the NO is not a deletion of the entry.
        self.assertIn("mpita", mp["alias_keys"])
        self.assertIn("MPITA", mp["aliases"])
        self.assertEqual(mp["name"], "Major Pain in the Ass")

    def test_run3_shape_without_no_is_untouched(self):
        # Control: master behaviour — the pollution is exactly what
        # extraction produced, so the pruning test above cannot be
        # passing vacuously.
        roster = _run3_roster()
        pp._roster_reconcile(roster)
        mp = roster["major pain in the ass"]
        self.assertTrue(DEVON_FORMS <= set(mp["alias_keys"]))

    def test_pruning_with_devon_entry_present(self):
        # Second variant: Devon HAS his own entry. Shadow pruning
        # alone removes only his exact primary from the claimant;
        # the variant forms need the NO.
        roster = {}
        _fold(roster, "Major Pain in the Ass",
              ["Karl Devon", "Major Devon", "Major Karl Devon",
               "Devon", "MPITA"], 3)
        _fold(roster, "Karl Devon", ["The Major"], 9)
        stats = pp._roster_reconcile(roster, no_pairs=NO_DEVON_MP)
        self.assertEqual(stats["variants_merged"], 0)
        self.assertEqual(len(roster), 2)
        mp = roster["major pain in the ass"]
        self.assertFalse(DEVON_FORMS & set(mp["alias_keys"]),
                         mp["alias_keys"])
        devon = roster["karl devon"]
        self.assertEqual(devon["name"], "Karl Devon")
        # His own forms are intact: the bare-title claim rides in
        # unresolved_mentions (intake records title-only claims as
        # clues, not adopted aliases) and survives reconciliation.
        self.assertIn("The Major", devon["unresolved_mentions"])


class TestReconcileMergeVeto(unittest.TestCase):
    def _mutual_roster(self):
        """Each entry claims the other's primary: the exact shape
        reconcile's mutual-claim pass merges."""
        roster = {}
        _fold(roster, "Major Pain in the Ass", ["Karl Devon"], 3)
        _fold(roster, "Karl Devon", ["Major Pain in the Ass"], 7)
        return roster

    def test_mutual_claim_pair_merges_without_no(self):
        roster = self._mutual_roster()
        stats = pp._roster_reconcile(roster)
        self.assertEqual(stats["variants_merged"], 1)
        self.assertEqual(len(roster), 1)

    def test_no_blocks_the_merge(self):
        roster = self._mutual_roster()
        stats = pp._roster_reconcile(roster, no_pairs=NO_DEVON_MP)
        self.assertEqual(stats["variants_merged"], 0)
        self.assertEqual(stats["no_vetoed_merges"], 1)
        self.assertEqual(len(roster), 2)
        # And the surviving claimant no longer carries the form.
        mp = roster["major pain in the ass"]
        self.assertNotIn("karl devon", mp["alias_keys"])
        self.assertEqual(roster["karl devon"]["name"], "Karl Devon")


class TestPostPassVeto(unittest.TestCase):
    def setUp(self):
        pp._dm_budget_reset()

    def tearDown(self):
        pp._dm_budget_reset()

    def test_tier12_deterministic_merge_vetoed(self):
        # Two Devon entries, one carrying the screen name as an
        # alias claim. Their keys are title-variants, which Tier 2
        # merges deterministically — unless the NO vetoes the pair.
        roster = {}
        _fold(roster, "Karl Devon", ["Major Pain in the Ass"], 3)
        _fold(roster, "Major Pain in the Ass", ["MPITA"], 5)
        base = copy.deepcopy(roster["karl devon"])
        base["name"] = "Major Karl Devon"
        base["primary_keys"] = {"major karl devon"}
        base["alias_keys"] = {"major karl devon"}
        base["aliases"] = set()
        base["chapters"] = {8}
        base["appearances"] = 5
        roster["major karl devon"] = base

        merged, _ = pp.post_pass_merge(roster, validator=None)
        self.assertEqual(merged, 1)          # control: Tier 2 merges
        self.assertEqual(len(roster), 2)

        roster = {}
        _fold(roster, "Karl Devon", ["Major Pain in the Ass"], 3)
        _fold(roster, "Major Pain in the Ass", ["MPITA"], 5)
        base = copy.deepcopy(roster["karl devon"])
        base["name"] = "Major Karl Devon"
        base["primary_keys"] = {"major karl devon"}
        base["alias_keys"] = {"major karl devon"}
        base["aliases"] = set()
        base["chapters"] = {8}
        base["appearances"] = 5
        roster["major karl devon"] = base

        merged, _ = pp.post_pass_merge(roster, validator=None,
                                       no_pairs=NO_DEVON_MP)
        self.assertEqual(merged, 0)
        self.assertEqual(len(roster), 3)

    def test_tier3_judge_never_asked_about_no_pair(self):
        # The Fourth Wing late-reveal pair reaches Tier 3 through
        # the name-candidate machinery. A human NO on the pair
        # must keep it from the judge entirely — an always-yes
        # judge is the adversarial case.
        def fw_roster():
            roster = {}
            _fold(roster, "General Sorrengail", [], 1)
            _fold(roster, "Violet Sorrengail", [], 2)
            _fold(roster, "Mira Sorrengail", [], 3)
            _fold(roster, "Lilith Sorrengail", [], 9)
            return roster

        judge = RecordingJudge(verdict=True, p=0.99)
        pp.post_pass_merge(fw_roster(), validator=judge)
        self.assertTrue(judge.asked_about("General Sorrengail",
                                          "Lilith Sorrengail"))

        roster = fw_roster()
        judge = RecordingJudge(verdict=True, p=0.99)
        merged, _ = pp.post_pass_merge(
            roster, validator=judge,
            no_pairs=[("general sorrengail", "lilith sorrengail")])
        self.assertFalse(judge.asked_about("General Sorrengail",
                                           "Lilith Sorrengail"))
        names = {e["name"] for e in roster.values()}
        self.assertTrue({"General Sorrengail", "Lilith Sorrengail"}
                        <= names)


class TestControlsUnaffected(unittest.TestCase):
    def test_same_person_handle_alias_survives_unrelated_no(self):
        # Gragg's own handle is a legitimate alias of the SAME
        # person; a NO about two other people must not touch it.
        roster = {}
        _fold(roster, "Brian Gragg", ["Loki"], 2)
        _fold(roster, "Major Pain in the Ass", ["Karl Devon"], 3)
        pp._roster_reconcile(roster, no_pairs=NO_DEVON_MP)
        gragg = roster["brian gragg"]
        self.assertIn("loki", gragg["alias_keys"])
        self.assertIn("Loki", gragg["aliases"])

    def test_ordinary_variant_merge_survives_unrelated_no(self):
        roster = {}
        _fold(roster, "Gen. Sorrengail",
              ["Lilith Sorrengail", "General Sorrengail"], 1)
        _fold(roster, "Lilith Sorrengail", ["Gen. Sorrengail"], 9)
        stats = pp._roster_reconcile(roster, no_pairs=NO_DEVON_MP)
        self.assertEqual(stats["variants_merged"], 1)
        self.assertEqual(len(roster), 1)


class TestRecordingPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_store(self):
        store = pp.RosterStore(learn_dir=self.dir, work_key="wk-daemon")
        store.data["title"] = "Daemon"
        store.save()
        return store

    def test_record_into_existing_store_by_title(self):
        self._seed_store()
        text = _capture(pp.cmd_roster_no, "Daemon", "Karl Devon",
                        "Major Pain in the Ass", learn_dir=self.dir)
        self.assertIn("Recorded NO", text)
        store = pp.RosterStore(learn_dir=self.dir, work_key="wk-daemon")
        self.assertTrue(store.has_no("karl devon",
                                     "major pain in the ass"))
        # Idempotent: a second recording adds no duplicate pair.
        text = _capture(pp.cmd_roster_no, "Daemon", "Karl Devon",
                        "Major Pain in the Ass", learn_dir=self.dir)
        self.assertIn("already recorded", text)
        store = pp.RosterStore(learn_dir=self.dir, work_key="wk-daemon")
        self.assertEqual(len(store.data["no_pairs"]), 1)

    def test_record_creates_store_under_work_id(self):
        # No store exists yet: passing the work_id creates the
        # store under the very key a run derives for the work.
        text = _capture(pp.cmd_roster_no, _FAKE_WID, "Karl Devon",
                        "Major Pain in the Ass", learn_dir=self.dir)
        self.assertIn("Recorded NO", text)
        store, err = pp.find_roster_store(self.dir, _FAKE_WID)
        self.assertIsNone(err)
        self.assertTrue(store.has_no("karl devon",
                                     "major pain in the ass"))

    def test_record_refuses_unknown_work_and_self_pair(self):
        text = _capture(pp.cmd_roster_no, "No Such Work", "Karl Devon",
                        "Major Pain in the Ass", learn_dir=self.dir)
        self.assertIn("no roster store found", text)
        self.assertEqual(pp.roster_store_paths(self.dir), [])
        self._seed_store()
        text = _capture(pp.cmd_roster_no, "Daemon", "Karl Devon",
                        "Karl Devon", learn_dir=self.dir)
        self.assertIn("Refused", text)
        store = pp.RosterStore(learn_dir=self.dir, work_key="wk-daemon")
        self.assertEqual(store.data["no_pairs"], [])

    def test_loader_finds_recorded_pairs(self):
        key = pp.staging_work_key(None, None, "Daemon", "Daniel Suarez")
        store = pp.RosterStore(learn_dir=self.dir, work_key=key)
        store.record_no("Karl Devon", "Major Pain in the Ass",
                        source="test")
        store.save()
        learned = pp.LearnedState(learn_dir=self.dir, enabled=True)
        with mock.patch.object(pp, "_LEARNED", learned), \
                mock.patch.object(pp, "resolve_work",
                                  return_value=(None, "test")):
            pairs = pp._roster_no_pairs_for("Daemon", "Daniel Suarez",
                                            {})
        self.assertEqual(pairs,
                         (("karl devon", "major pain in the ass"),))

    def test_loader_fail_soft_without_store(self):
        learned = pp.LearnedState(learn_dir=self.dir, enabled=True)
        with mock.patch.object(pp, "_LEARNED", learned), \
                mock.patch.object(pp, "resolve_work",
                                  return_value=(None, "test")):
            self.assertEqual(
                pp._roster_no_pairs_for("Daemon", "Daniel Suarez", {}),
                ())


if __name__ == "__main__":
    unittest.main()
