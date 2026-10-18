"""Issue #2 (High, false-merge class): a learned "trusted title" can
be a person's first name.

Mechanism (verified against v2.13.36): `_roster_update` observes the
leading token of every 3+-token name as a title candidate, and
`LearnedState.observe_title` trusts it after only 2 distinct
remainders — so *James Robert Smith* + *James William Doe* mint
`james` as a trusted title (and *John Michael Smith* + *John David
Roe* mint `john`). `_strip_merge_noise` then reduced the brothers
"james smith" and "john smith" to the identical key ["smith"], and
`_merge_tier` returned a Tier-2 deterministic YES — checked before
the Tier-1 same-chapter cannot-link, and unreachable by the surname
trap, which needs >= 2 stripped tokens. Distinct characters merged
silently, with no judge involved.

The invariant this file pins: a LEARNED title (as opposed to a
built-in closed-class title: Dr, Mr, Herr, ...) must never make two
names with different given names identical for merge purposes.

Two seams establish it, both in what stripping / Tier-2 trust may
do with a learned title — learning itself still records
observations, and built-in titles are untouched:

(a) Strip floor: stripping via a learned-only title may not reduce
    a name below 2 tokens. A bare-surname result is exactly the
    danger shape; keeping the given name in the key also restores
    the surname trap, which can only fire on >= 2 tokens.
(b) Both-sides guard in `_merge_tier`: when the two names become
    comparable only because EACH side lost a DIFFERENT learned-only
    title token, no deterministic Tier-2 YES may be built on the
    stripped keys. The pair falls through to the Tier-1
    cannot-link / surname trap / Tier-3 judge like any other
    ambiguous pair. One-sided learned stripping (a title form
    against the bare name — the case title learning exists for)
    and same-title stripping are unaffected.

Stdlib unittest only. No network, no Supabase.
Run: python -m unittest tests.test_learned_title_merge_guard -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

# The issue's trigger names, observed exactly the way _roster_update
# observes them: the leading 1 token and the leading 2 tokens of each
# 3+-token name, each against the remaining tokens.
_ISSUE_NAMES = ["James Robert Smith", "James William Doe",
                "John Michael Smith", "John David Roe"]


class _LearnedTitlesCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_learned = pp._LEARNED
        self.addCleanup(self._restore_learned)
        self.learned = pp.LearnedState(learn_dir=Path(self._tmp.name),
                                       enabled=True)
        pp._LEARNED = self.learned
        for name in _ISSUE_NAMES:
            toks = name.split()
            self.learned.observe_title(toks[0], " ".join(toks[1:]))
            self.learned.observe_title(" ".join(toks[:2]),
                                       " ".join(toks[2:]))
        # Precondition of the whole class: the learning itself DID
        # record and trust both first names as "titles". The fix is
        # not to disable learning — it is in what stripping and the
        # deterministic tier may do with that trust.
        self.assertIn("james", self.learned.trusted_titles())
        self.assertIn("john", self.learned.trusted_titles())

    def _restore_learned(self):
        pp._LEARNED = self._old_learned


class TestIssueReproduction(_LearnedTitlesCase):
    def test_colisted_brothers_do_not_merge(self):
        # Issue evidence pair, co-listed as separate characters in
        # the same chapter: pre-fix this returned "yes".
        self.assertNotEqual(
            pp._merge_tier("james smith", "john smith",
                           chapter_keys={"james smith", "john smith"}),
            "yes")
        # Stronger: with the given names kept in the key, the Tier-1
        # same-chapter cannot-link reaches the pair.
        self.assertEqual(
            pp._merge_tier("james smith", "john smith",
                           chapter_keys={"james smith", "john smith"}),
            "no")

    def test_brothers_do_not_merge_without_chapter_info(self):
        # With the strip floor keeping both keys at 2 tokens, the
        # surname trap — unreachable on a 1-token stripped key
        # pre-fix — fires on its own.
        self.assertEqual(pp._merge_tier("james smith", "john smith"),
                         "no")

    def test_brothers_sharing_a_middle_name_do_not_merge(self):
        # The 3-token form of the same trap: a learned strip of each
        # side's (different) given name would leave the identical
        # ["robert", "smith"] — above the strip floor, so only the
        # both-sides guard in _merge_tier stops this one.
        self.assertNotEqual(
            pp._merge_tier("james robert smith", "john robert smith"),
            "yes")
        self.assertEqual(
            pp._merge_tier("james robert smith", "john robert smith",
                           chapter_keys={"james robert smith",
                                         "john robert smith"}),
            "no")


class TestStripFloorPin(_LearnedTitlesCase):
    def test_learned_title_cannot_reduce_a_name_below_two_tokens(self):
        # The seam-(a) pin: a 2-token name under a learned title
        # keeps both tokens instead of collapsing to a bare surname.
        self.assertEqual(pp._strip_merge_noise("james smith"),
                         ["james", "smith"])
        self.assertEqual(pp._strip_merge_noise("john smith"),
                         ["john", "smith"])

    def test_learned_title_still_strips_down_to_two_tokens(self):
        # The floor is exactly 2: a 3-token name may still shed a
        # learned title, which is what makes the one-sided title
        # form below mergeable.
        self.assertEqual(pp._strip_merge_noise("james robert smith"),
                         ["robert", "smith"])


class TestGenuineTitleControls(_LearnedTitlesCase):
    def test_builtin_dr_still_merges(self):
        self.assertEqual(pp._merge_tier("dr jon smith", "jon smith"),
                         "yes")

    def test_builtin_herr_still_merges(self):
        self.assertEqual(
            pp._merge_tier("herr heinrich boerner", "heinrich boerner"),
            "yes")

    def test_two_different_builtin_titles_still_merge(self):
        # Both sides lose a leading title, but both titles are
        # built-in closed-class — the both-sides guard applies only
        # to learned-only tokens, so this stays a Tier-2 YES.
        self.assertEqual(pp._merge_tier("dr jon smith", "herr jon smith"),
                         "yes")

    def test_one_sided_learned_title_still_merges(self):
        # A genuinely novel title, learned the same way "james" was:
        # two sightings mint it, and a title form against the bare
        # name is the case title learning exists for.
        self.learned.observe_title("sheikh", "jon smith")
        self.learned.observe_title("sheikh", "jane doe")
        self.assertIn("sheikh", self.learned.trusted_titles())
        self.assertEqual(pp._merge_tier("sheikh jon smith", "jon smith"),
                         "yes")
        # Same learned title on both sides is likewise unaffected.
        self.assertEqual(
            pp._merge_tier("sheikh jon smith", "sheikh jon smith"),
            "yes")


if __name__ == "__main__":
    unittest.main()
