"""Option B — persisted per-work roster, slice 5: the report, the
correction commands, and the dashboard entity section.

Design: eval/b-roster-design.md sections 10-13. The report is a
read model over the stores (accounting closed, suspects loudest);
--roster-link / --roster-unlink / --roster-split are the human
correction surface — attributed, recorded, and the only writers
besides the seam itself. The dashboard section is additions-only:
absent when neither preview carries roster data.

Stdlib unittest only. No network, no Supabase, no real ebook
files. Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from collections import Counter
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                               _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

_spec2 = importlib.util.spec_from_file_location(
    "preview_variance",
    str(Path(__file__).resolve().parent.parent
        / "preview_variance.py"))
pv = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(pv)


def _entry(name, chapters=(1,), appearances=10, alias_keys=()):
    nkey = pp.norm_name(name)
    return {"name": name, "aliases": set(),
            "alias_keys": {nkey} | {pp.norm_name(a)
                                    for a in alias_keys},
            "primary_keys": {nkey}, "_generic_keys": set(),
            "appearances": appearances, "last_seen": max(chapters),
            "chapters": set(chapters), "roles": Counter(),
            "descriptions": [], "evidence": ""}


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*args, **kwargs)
    return out, buf.getvalue()


class _ReportCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _store(self):
        return pp.RosterStore(learn_dir=self.dir, work_key="wk-x")

    def _two_form_entity(self):
        """One entity observing both 'Violet Sorrengail' (run 1)
        and 'Violet' (run 2, tier-2 link). Returns the entity ID."""
        st = self._store()
        r1 = {"violet sorrengail": _entry(
            "Violet Sorrengail", alias_keys=["Violet"])}
        pp.roster_assign_entities(r1, st, anchors=[])
        eid = r1["violet sorrengail"]["entity_id"]
        st2 = self._store()
        r2 = {"violet": _entry("Violet",
                               alias_keys=["Violet Sorrengail"])}
        pp.roster_assign_entities(r2, st2, anchors=[])
        self.assertEqual(r2["violet"]["entity_id"], eid)
        return eid


class TestRosterReport(_ReportCase):
    def test_report_closes_accounting_and_lists_entities(self):
        eid = self._two_form_entity()
        _out, text = _capture(pp.cmd_roster_report, learn_dir=self.dir)
        self.assertIn("UNACCOUNTED: 0", text)
        self.assertIn(eid, text)
        self.assertIn("Violet Sorrengail", text)
        self.assertIn("alias-cross", text)

    def test_suspect_observations_listed_first_and_loud(self):
        eid = self._two_form_entity()
        st = self._store()
        ent = st.get_entity(eid)
        ent["observations"][0]["suspect"] = True
        st.save()
        _out, text = _capture(pp.cmd_roster_report, learn_dir=self.dir)
        self.assertIn("!! SUSPECT OBSERVATIONS", text)
        self.assertLess(text.index("!! SUSPECT OBSERVATIONS"),
                        text.index("Accounting"))

    def test_corrupt_store_is_reported_not_hidden(self):
        self._two_form_entity()
        bad = self.dir / "rosters" / "broken-1234.json"
        bad.write_text("{not json", encoding="utf-8")
        _out, text = _capture(pp.cmd_roster_report, learn_dir=self.dir)
        self.assertIn("CORRUPT roster store", text)
        self.assertIn("UNACCOUNTED: 0", text)  # good store still read

    def test_no_stores_message(self):
        _out, text = _capture(pp.cmd_roster_report, learn_dir=self.dir)
        self.assertIn("No roster stores found", text)


class TestCorrectionCommands(_ReportCase):
    def test_link_clears_suspect_and_records_human_tier(self):
        eid = self._two_form_entity()
        st = self._store()
        st.get_entity(eid)["observations"][1]["suspect"] = True
        st.save()
        _out, text = _capture(pp.cmd_roster_link, "wk-x", eid,
                              "Violet", learn_dir=self.dir)
        self.assertIn("Linked:", text)
        ent = self._store().get_entity(eid)
        self.assertFalse(any(o["suspect"]
                             for o in ent["observations"]))
        self.assertTrue(any(lnk["tier"] == "human"
                            for lnk in ent["links"]))

    def test_link_refuses_unobserved_form(self):
        eid = self._two_form_entity()
        _out, text = _capture(pp.cmd_roster_link, "wk-x", eid,
                              "Nobody", learn_dir=self.dir)
        self.assertIn("Refused", text)

    def test_unlink_records_no_and_next_run_cannot_reform(self):
        eid = self._two_form_entity()
        _out, text = _capture(pp.cmd_roster_unlink, "wk-x", eid,
                              "Violet", learn_dir=self.dir)
        self.assertIn("Unlinked:", text)
        st = self._store()
        self.assertTrue(st.has_no("violet", "violet sorrengail"))
        # Next run: the very extraction that linked before now
        # mints a NEW entity — the NO vetoes re-formation (A2).
        r3 = {"violet": _entry("Violet",
                               alias_keys=["Violet Sorrengail"])}
        marker = pp.roster_assign_entities(r3, st, anchors=[])
        self.assertNotEqual(r3["violet"]["entity_id"], eid)
        self.assertGreaterEqual(marker["minted"], 1)

    def test_link_after_unlink_supersedes_the_no_for_that_pair(self):
        eid = self._two_form_entity()
        _capture(pp.cmd_roster_unlink, "wk-x", eid, "Violet",
                 learn_dir=self.dir)
        _out, text = _capture(pp.cmd_roster_link, "wk-x", eid,
                              "Violet", learn_dir=self.dir)
        self.assertIn("Linked:", text)
        self.assertIn("Retired 1 recorded NO", text)
        st = self._store()
        self.assertFalse(st.has_no("violet", "violet sorrengail"))
        ent = st.get_entity(eid)
        obs = [o for o in ent["observations"]
               if o["form_key"] == "violet"]
        self.assertTrue(obs and not obs[0]["detached"])

    def test_split_moves_form_and_original_accounts_as_split(self):
        eid = self._two_form_entity()
        _out, text = _capture(pp.cmd_roster_split, "wk-x", eid,
                              ["Violet Sorrengail", "Violet"],
                              learn_dir=self.dir)
        self.assertIn("Split:", text)
        st = self._store()
        acc = st.accounting()
        self.assertEqual(acc["unaccounted"], 0)
        self.assertEqual(st.get_entity(eid)["status"], "split")
        succ = [e for e in st.data["entities"].values()
                if e.get("split_from") == eid]
        self.assertEqual(len(succ), 2)

    def test_split_refuses_unobserved_forms(self):
        eid = self._two_form_entity()
        _out, text = _capture(pp.cmd_roster_split, "wk-x", eid,
                              ["Nobody"], learn_dir=self.dir)
        self.assertIn("Refused", text)

    def test_unknown_work_and_entity_report_cleanly(self):
        _out, text = _capture(pp.cmd_roster_link, "no-such-work",
                              "ent_0001", "Violet",
                              learn_dir=self.dir)
        self.assertIn("no roster store found", text)
        eid = self._two_form_entity()
        _out, text = _capture(pp.cmd_roster_unlink, "wk-x",
                              "ent_9999", "Violet",
                              learn_dir=self.dir)
        self.assertIn("not found", text)


class TestDashboardEntitySection(unittest.TestCase):
    def _preview(self, entities, marker=None, rels=()):
        d = {"characters": [
                 {"name": e, "entity_id": e, "frequency": 0.5}
                 for e in entities],
             "relationships": list(rels)}
        if marker is not None:
            d["roster_persistence"] = marker
        return d

    def test_absent_without_roster_data(self):
        a = {"characters": [{"name": "X"}], "relationships": []}
        self.assertEqual(pv.entity_section(a, dict(a)), [])

    def test_identical_previews_score_perfect_identity(self):
        marker = {"canonical_names": {"ent_0001": "Violet"},
                  "suspects": []}
        rel = {"from": "Violet", "to": "Xaden", "type": "romantic",
               "from_id": "ent_0001", "to_id": "ent_0002"}
        a = self._preview(["ent_0001", "ent_0002"], marker, [rel])
        lines = pv.entity_section(a, a)
        text = "\n".join(lines)
        self.assertIn("entity-set Jaccard: 1.000", text)
        self.assertIn("principal entity Jaccard", text)
        self.assertIn("endpoint-ID Jaccard: 1.000", text)
        self.assertIn("canonical-name churn: 0", text)

    def test_churn_and_identity_loss_are_visible(self):
        a = self._preview(
            ["ent_0001"], {"canonical_names": {"ent_0001": "Violet"},
                           "suspects": [{"a": "x", "b": "y",
                                         "why": "w"}]})
        b = self._preview(
            ["ent_0002"],
            {"canonical_names": {"ent_0001": "Violet Sorrengail"},
             "suspects": []})
        text = "\n".join(pv.entity_section(a, b))
        self.assertIn("entity-set Jaccard: 0.000", text)
        self.assertIn("split suspects: old 1, new 0", text)


if __name__ == "__main__":
    unittest.main()
