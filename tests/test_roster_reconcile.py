"""Roster reconciliation (Experiment 1, --roster-reconcile).

Motivation (landscape research + FW run variance, 2026-10): in
process_file_v2, Call A builds the roster sequentially, chapter by
chapter. Early chapters extract against a thin roster, so name variants
and alias groupings formed early persist downstream; the reduce-time
post-pass merge only partially heals them (its tier rules hard-NO
same-surname pairs via the surname trap, so a late-revealed full name
never reunites with its early short form). Roster churn is the headline
variance metric (FW roster Jaccard 0.155 across runs).

_roster_reconcile runs ONCE after Call A completes, before
post_pass_merge / v2_reduce consume the roster, and canonicalizes with
whole-book hindsight using only existing machinery:

- Mutual alias claims (each entry claims the other's primary, via
  alias_keys or the preserved unresolved_mentions) + never co-listed in
  a chapter (Tier 1 cannot-link) + no third claimant -> merge.
- Learned/manual alias links (nick_lookup) -> merge, same vetoes.
- Stale shadow pruning: alias claims that are another entry's primary
  in the FINAL roster are pollution and are pruned (hindsight version
  of the _roster_update adoption guard).

A lone proper-name alias claim NEVER merges (codebase doctrine:
"Proper-name aliases never trigger a merge (they're recorded, not
trusted)"). Membership invariant: output entries are a subset of what
extraction sighted; the canonical name is always a sighted form.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest discover -s tests -v
"""
import importlib.util
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _fold(roster, name, aliases, chapter_idx, role="supporting"):
    """Fold one extracted character into the roster (real producer)."""
    pp._roster_update(
        roster,
        [{"name": name, "aliases": aliases, "role": role,
          "description": "", "evidence": ""}],
        chapter_idx)


def _late_reveal_roster():
    """Gen. Sorrengail sighted ch.1 (claiming the full name as alias);
    Lilith Sorrengail sighted ch.9 (claiming the short form back)."""
    roster = {}
    _fold(roster, "Gen. Sorrengail",
          ["Lilith Sorrengail", "General Sorrengail"], 1)
    _fold(roster, "Lilith Sorrengail", ["Gen. Sorrengail"], 9)
    return roster


def _poisoned_roster():
    """Violet's entry (folded first) claims two real characters' names
    as aliases; both are sighted later as their own entries."""
    roster = {}
    _fold(roster, "Violet Sorrengail",
          ["Lilith Sorrengail", "Mira Sorrengail"], 1, role="protagonist")
    _fold(roster, "Lilith Sorrengail", [], 2)
    _fold(roster, "Mira Sorrengail", [], 3)
    return roster


class TestPoisonedRoster(unittest.TestCase):
    """The historical pollution shape must be pruned, never merged."""

    def test_shadow_claims_pruned_victims_intact(self):
        roster = _poisoned_roster()
        stats = pp._roster_reconcile(roster)
        # Nobody merges: all three real characters survive as entries.
        self.assertEqual(sorted(roster),
                         ["lilith sorrengail", "mira sorrengail",
                          "violet sorrengail"])
        self.assertEqual(roster["lilith sorrengail"]["name"],
                         "Lilith Sorrengail")
        self.assertEqual(roster["mira sorrengail"]["name"],
                         "Mira Sorrengail")
        # The false groupings are pruned from the claimant, in both the
        # key set and the display aliases.
        violet = roster["violet sorrengail"]
        self.assertNotIn("lilith sorrengail", violet["alias_keys"])
        self.assertNotIn("mira sorrengail", violet["alias_keys"])
        self.assertNotIn("Lilith Sorrengail", violet["aliases"])
        self.assertNotIn("Mira Sorrengail", violet["aliases"])
        self.assertEqual(stats["variants_merged"], 0)
        self.assertEqual(stats["entries_before"], 3)
        self.assertEqual(stats["entries_after"], 3)
        # 2 alias keys + 2 display forms pruned.
        self.assertEqual(stats["aliases_pruned"], 4)

    def test_victims_still_resolve_to_themselves(self):
        roster = _poisoned_roster()
        pp._roster_reconcile(roster)
        self.assertEqual(pp._roster_canonical(roster, "Lilith Sorrengail"),
                         "Lilith Sorrengail")
        self.assertEqual(pp._roster_canonical(roster, "Mira Sorrengail"),
                         "Mira Sorrengail")


class TestLateRevealMerge(unittest.TestCase):
    def test_mutual_claims_merge_to_one_entry(self):
        roster = _late_reveal_roster()
        self.assertEqual(len(roster), 2)  # fixture sanity: split today
        stats = pp._roster_reconcile(roster)
        self.assertEqual(len(roster), 1)
        entry = next(iter(roster.values()))
        # Canonical name is one of the sighted forms.
        self.assertIn(entry["name"],
                      ("Gen. Sorrengail", "Lilith Sorrengail"))
        # Both sightings' chapters and name forms are preserved.
        self.assertEqual(entry["chapters"], {1, 9})
        self.assertIn("gen. sorrengail", entry["primary_keys"])
        self.assertIn("lilith sorrengail", entry["primary_keys"])
        self.assertEqual(stats["entries_before"], 2)
        self.assertEqual(stats["entries_after"], 1)
        self.assertEqual(stats["variants_merged"], 1)

    def test_membership_invariant_no_invented_forms(self):
        sighted = {"Gen. Sorrengail", "Lilith Sorrengail",
                   "General Sorrengail"}
        sighted_keys = {pp.norm_name(s) for s in sighted}
        roster = _late_reveal_roster()
        pp._roster_reconcile(roster)
        for entry in roster.values():
            self.assertIn(entry["name"], sighted)
            for alias in entry["aliases"]:
                self.assertIn(alias, sighted)
            for key in entry["alias_keys"]:
                self.assertIn(key, sighted_keys)
            for key in entry["primary_keys"]:
                self.assertIn(key, sighted_keys)

    def test_one_directional_claim_does_not_merge(self):
        # A lone proper-name alias claim is recorded, never trusted:
        # Gen claims Lilith, Lilith never claims back -> no merge, and
        # the stale claim is pruned once Lilith exists as an entry.
        roster = {}
        _fold(roster, "Gen. Sorrengail", ["Lilith Sorrengail"], 1)
        _fold(roster, "Lilith Sorrengail", [], 9)
        stats = pp._roster_reconcile(roster)
        self.assertEqual(len(roster), 2)
        self.assertEqual(stats["variants_merged"], 0)
        self.assertNotIn("lilith sorrengail",
                         roster["gen. sorrengail"]["alias_keys"])

    def test_mutual_claims_with_shared_chapter_do_not_merge(self):
        # Tier 1 cannot-link: both forms were listed as separate
        # characters in the same chapter -> two people, whatever the
        # alias claims say.
        roster = {}
        _fold(roster, "Gen. Sorrengail", ["Lilith Sorrengail"], 1)
        _fold(roster, "Lilith Sorrengail", ["Gen. Sorrengail"], 1)
        stats = pp._roster_reconcile(roster)
        self.assertEqual(len(roster), 2)
        self.assertEqual(stats["variants_merged"], 0)

    def test_third_party_claim_blocks_merge(self):
        # Canonical pass-2 rule lifted to entries: a primary claimed by
        # two different entries is ambiguous and resolves to nothing.
        roster = _late_reveal_roster()
        _fold(roster, "Aunt Sorrengail", ["Lilith Sorrengail"], 5)
        stats = pp._roster_reconcile(roster)
        self.assertEqual(stats["variants_merged"], 0)
        self.assertIn("gen. sorrengail", roster)
        self.assertIn("lilith sorrengail", roster)


class TestLearnedLink(unittest.TestCase):
    def test_manual_alias_link_merges(self):
        # No extraction claims at all; a human-validated --alias link
        # (learned state) is the only signal. Both entries exist, so
        # update-time lookups missed; reconciliation applies the link
        # with whole-roster hindsight.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        learned = pp.LearnedState(learn_dir=tmp.name, enabled=True)
        learned.record_alias("Gen. Sorrengail", "Lilith Sorrengail")
        roster = {}
        _fold(roster, "Gen. Sorrengail", [], 1)
        _fold(roster, "Lilith Sorrengail", [], 9)
        self.assertEqual(len(roster), 2)
        with mock.patch.object(pp, "_LEARNED", learned):
            stats = pp._roster_reconcile(roster)
        self.assertEqual(len(roster), 1)
        self.assertEqual(stats["variants_merged"], 1)


# ---------------------------------------------------------------------------
# End-to-end through the real seam: flag off = today's roster, byte-for-
# byte at the artefact level; flag on = reconciled roster + marker.
# Fake backend is the network boundary; Call A/B, roster, post-pass and
# reduce all run for real (pattern from test_frontmatter).
# ---------------------------------------------------------------------------

_GEN_SENTENCE = "Gen. Sorrengail addressed the assembled cadets at dawn."
_LILITH_SENTENCE = "Lilith Sorrengail reviewed the roster in silence."
_MIRA_SENTENCE = "Mira Solace drew her sword and faced the dawn."
_FILLER = ("<p>The valley was quiet and the river ran cold and bright "
           "under a wide northern sky full of slow clouds.</p>")


def _xhtml(body):
    return ("<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.idpf.org/2007/xhtml'><head><title>t</title></head>"
            f"<body>{body}</body></html>")


def _make_epub(path, files):
    manifest = "\n".join(
        f'    <item id="i{n}" href="{name.split("/", 1)[1]}" media-type="application/xhtml+xml"/>'
        for n, name in enumerate(files) if name != "META-INF/container.xml")
    spine = "\n".join(
        f'    <itemref idref="i{n}"/>' for n, name in enumerate(files)
        if name != "META-INF/container.xml")
    opf = ("""<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf' version='3.0' unique-identifier='bid'>
  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>
    <dc:title>Reconcile Test Book</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:identifier>9780306406157</dc:identifier>
  </metadata>
  <manifest>
""" + manifest + """
  </manifest>
  <spine>
""" + spine + """
  </spine>
</package>""")
    container = ("""<?xml version='1.0' encoding='utf-8'?>
<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' version='1.0'>
  <rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles>
</container>""")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, content in files.items():
            z.writestr(name, content)


class _ReconBackend:
    """Fake LLM: early chapters know the general by her short form,
    late chapters by her full name; each form claims the other."""

    def __call__(self, prompt, chunk, i, n):
        if prompt == pp.PROMPT_V2_CONTENT:
            return json.dumps({
                "summary": "A chapter summary.",
                "spice_level": 0, "triggers": {}, "trigger_evidence": {},
                "trope_candidates": ["Found Family"], "quotes": []})
        if "GENMARK" in chunk:
            return json.dumps({
                "characters": [{
                    "name": "Gen. Sorrengail", "role": "supporting",
                    "aliases": ["Lilith Sorrengail", "General Sorrengail"],
                    "description": "The commanding general.",
                    "evidence": _GEN_SENTENCE}],
                "relationships": [], "pov_character": None})
        if "LILITHMARK" in chunk:
            return json.dumps({
                "characters": [{
                    "name": "Lilith Sorrengail", "role": "supporting",
                    "aliases": ["Gen. Sorrengail"],
                    "description": "The commanding general.",
                    "evidence": _LILITH_SENTENCE}],
                "relationships": [], "pov_character": None})
        return json.dumps({
            "characters": [{
                "name": "Mira Solace", "role": "protagonist",
                "description": "A swordswoman of the valley.",
                "evidence": _MIRA_SENTENCE}],
            "relationships": [], "pov_character": "Mira Solace"})


def _recon_book(epub_path):
    def chapter(heading, sentence, marker):
        return _xhtml(f"<h1>{heading}</h1><p>{marker}</p>"
                      f"<p>{sentence}</p>" + _FILLER * 22)
    files = {
        "OEBPS/ch01.xhtml": chapter("Chapter 1", _GEN_SENTENCE, "GENMARK"),
        "OEBPS/ch02.xhtml": chapter("Chapter 2", _GEN_SENTENCE, "GENMARK"),
        "OEBPS/ch03.xhtml": chapter("Chapter 3", _MIRA_SENTENCE, ""),
        "OEBPS/ch04.xhtml": chapter("Chapter 4", _LILITH_SENTENCE,
                                    "LILITHMARK"),
        "OEBPS/ch05.xhtml": chapter("Chapter 5", _LILITH_SENTENCE,
                                    "LILITHMARK"),
    }
    _make_epub(epub_path, files)


class TestReconcileFlagWiring(unittest.TestCase):
    def _run(self, stem, extra_config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        epub = str(Path(tmp.name) / f"{stem}.epub")
        _recon_book(epub)
        preview_json = pp.SCRIPT_DIR / "preview" / f"{stem}.json"
        self.addCleanup(lambda: preview_json.unlink(missing_ok=True))
        self.addCleanup(lambda: (pp.SCRIPT_DIR / "preview" / f"{stem}.md")
                        .unlink(missing_ok=True))
        self.addCleanup(lambda: shutil.rmtree(
            pp._checkpoint_dir(pp._checkpoint_book_key(Path(epub))),
            ignore_errors=True))
        config = {"llm": "ollama"}
        config.update(extra_config)
        with mock.patch.object(pp, "llm_ollama", _ReconBackend()), \
                mock.patch.object(pp, "_lookup_series_openlibrary",
                                  lambda *a, **k: (None, None)), \
                mock.patch.object(pp, "resolve_work",
                                  lambda *a, **k: (None, "test")), \
                mock.patch.dict(pp.CONFIG, config):
            ok = pp.process_file_v2(Path(epub), preview=True)
        self.assertTrue(ok)
        return json.loads(preview_json.read_text(encoding="utf-8"))

    def test_flag_off_roster_unchanged_and_no_marker(self):
        result = self._run("reconoff", {})
        roster = result["chapter_data"]["roster"]
        # Today's behaviour: the surname trap keeps the two sightings
        # as separate entries all the way into the artefact.
        self.assertIn("gen. sorrengail", roster)
        self.assertIn("lilith sorrengail", roster)
        self.assertNotIn("roster_reconcile", result)

    def test_flag_on_reconciles_and_records_marker(self):
        result = self._run("reconon", {"roster_reconcile": True})
        roster = result["chapter_data"]["roster"]
        general_keys = [k for k in roster if "sorrengail" in k]
        self.assertEqual(len(general_keys), 1)
        self.assertIn("mira solace", roster)
        marker = result["roster_reconcile"]
        self.assertEqual(marker["entries_before"], 3)
        self.assertEqual(marker["entries_after"], 2)
        self.assertEqual(marker["variants_merged"], 1)
        # The surviving character appears exactly once in the output.
        names = [c["name"] for c in result["characters"]]
        names += [c["name"] for c in result["minor_characters"]]
        generals = [n for n in names if "Sorrengail" in n]
        self.assertEqual(len(generals), 1)


if __name__ == "__main__":
    unittest.main()
