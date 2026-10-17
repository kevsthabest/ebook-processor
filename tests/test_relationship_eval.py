"""The relationship eval harness's corpus-context mode (2.13.10).

The 2.13.9 context window lives in v2_reduce; the eval script
originally judged bare quotes, so its first live run after
2.13.9 measured the prompt only and could not see the fix it
was meant to gate. _context_for_case is the bridge: it locates
a case's quote in the work's corpus chapter texts and frames
the neighbouring sentences exactly as production does.

Stdlib unittest only. No network, no corpus.db (fake chapters).
"""
import importlib.util
import unittest
from pathlib import Path

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "evaluate-relationships.py")
_spec = importlib.util.spec_from_file_location("evaluate_relationships",
                                               _MOD_PATH)
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)
pp = er.pp


def _chs(*texts):
    return [{"index": i + 1, "label": f"Ch{i + 1}", "title": "",
             "text": t} for i, t in enumerate(texts)]


class TestContextForCase(unittest.TestCase):
    def test_located_quote_gets_production_framed_context(self):
        chs = _chs("Mira faced the scribe. This is my sister, "
                   "Violet. The scribe wrote it down.")
        ctx, located = er._context_for_case(
            pp, chs, "This is my sister, Violet.")
        self.assertTrue(located)
        self.assertTrue(ctx.startswith(
            "Sentences around the quote, from the same passage: "))
        self.assertIn("Mira faced the scribe", ctx)
        self.assertIn("The scribe wrote it down", ctx)

    def test_quote_in_a_later_chapter_is_found(self):
        chs = _chs("Nothing here at all.",
                   "Before this. The target quote stands here. "
                   "After this.")
        ctx, located = er._context_for_case(
            pp, chs, "The target quote stands here.")
        self.assertTrue(located)
        self.assertIn("Before this", ctx)
        self.assertIn("After this", ctx)

    def test_unlocated_quote_reports_not_located(self):
        ctx, located = er._context_for_case(
            pp, _chs("Some other text entirely."),
            "A quote that is not in the text.")
        self.assertEqual(ctx, "")
        self.assertFalse(located)

    def test_located_but_windowless_quote(self):
        # The quote is the whole chapter: located, but there are
        # no neighbouring sentences to offer.
        ctx, located = er._context_for_case(
            pp, _chs("The only sentence in the chapter."),
            "The only sentence in the chapter.")
        self.assertEqual(ctx, "")
        self.assertTrue(located)


class TestResolveCaseContext(unittest.TestCase):
    """Duplicate corpus copies (the same book under a print ISBN
    and an ASIN key) must not abort the run: the copy a quote
    locates in is the copy that judges it."""

    def test_quote_locates_in_the_second_copy(self):
        copy_a = _chs("An edition with different wording entirely.")
        copy_b = _chs("Before this. The target quote stands here. "
                      "After this.")
        ctx, located, ambiguous = er._resolve_case_context(
            pp, [copy_a, copy_b], "The target quote stands here.")
        self.assertTrue(located)
        self.assertTrue(ambiguous)
        self.assertIn("Before this", ctx)

    def test_single_copy_is_not_ambiguous(self):
        ctx, located, ambiguous = er._resolve_case_context(
            pp, [_chs("Before. A quote lives here. After.")],
            "A quote lives here.")
        self.assertTrue(located)
        self.assertFalse(ambiguous)

    def test_quote_in_no_copy_reports_unlocated(self):
        ctx, located, ambiguous = er._resolve_case_context(
            pp, [_chs("First copy text."), _chs("Second copy text.")],
            "A quote in neither copy.")
        self.assertEqual(ctx, "")
        self.assertFalse(located)
        self.assertTrue(ambiguous)


if __name__ == "__main__":
    unittest.main()
