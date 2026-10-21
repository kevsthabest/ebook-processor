"""Embedding-input length discipline: the unmapped evidence query and
embed_vectors' overlong-input warning.

The embedding endpoint (embeddinggemma-2) caps an input at 510 tokens
and truncates the excess silently server-side — a book-wide trope's
unmapped query (name + every support chapter's summary joined) ran to
~7,900 chars and its passage ranking was computed over a vector of
only the first third. The query builder now samples summaries evenly
across the support list within a safe character budget, and
embed_vectors prints one warning line when a batch carries an input
over that budget (it still sends and returns exactly what it did).

Stdlib unittest only; transport is stubbed at urllib.request.urlopen.
Run: python -m unittest discover -s tests -v
"""
import io
import json
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import importlib.util

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable",
                                              _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

BUDGET = pp._EMBED_INPUT_CHAR_BUDGET


def _summary(i, words=40):
    """A support-chapter summary carrying a unique position marker."""
    return (f"chaptermark{i:02d} " + " ".join(["prose"] * words)).strip()


class TestUnmappedQueryCap(unittest.TestCase):
    def test_many_summaries_query_fits_budget(self):
        sums = [_summary(i) for i in range(35)]
        q = pp.evidence_query_text("Red Herring", summaries=sums)
        self.assertLessEqual(len(q), BUDGET)

    def test_sampling_spans_start_middle_and_end(self):
        sums = [_summary(i) for i in range(35)]
        q = pp.evidence_query_text("Red Herring", summaries=sums)
        self.assertIn("chaptermark00", q)   # first support chapter
        self.assertIn("chaptermark17", q)   # middle support chapter
        self.assertIn("chaptermark34", q)   # last support chapter

    def test_single_oversized_summary_trimmed_to_budget(self):
        q = pp.evidence_query_text(
            "Red Herring", summaries=["x" * (BUDGET * 3)])
        self.assertLessEqual(len(q), BUDGET)
        self.assertTrue(q.startswith("Red Herring: "))

    def test_under_budget_query_byte_identical(self):
        sums = ["Mara storms the gate at dawn.",
                "  The fleet burns in the harbour.  "]
        expected = ("Rescue Mission: Mara storms the gate at dawn. "
                    "The fleet burns in the harbour.")
        self.assertEqual(
            pp.evidence_query_text("Rescue Mission", summaries=sums),
            expected)

    def test_mapped_branch_unchanged(self):
        desc = "A trusted ally defects. " * 200  # far over the budget
        q = pp.evidence_query_text("Betrayal", description=desc.strip())
        self.assertEqual(q, f"Betrayal: {desc.strip()}")

    def test_no_summaries_falls_back_to_name(self):
        self.assertEqual(pp.evidence_query_text("Betrayal"), "Betrayal")
        self.assertEqual(
            pp.evidence_query_text("Betrayal", summaries=["  "]),
            "Betrayal")


class TestQueryShapeVersion(unittest.TestCase):
    def test_query_shape_version_bumped(self):
        # The version is an enumerated input of the retrieve stage's
        # input hash: bumping it re-derives stored outputs whose
        # query text the cap changed instead of reusing them.
        self.assertEqual(pp._EVIDENCE_QUERY_SHAPE_VERSION, "2")


class _FakeEmbedResponse:
    def __init__(self, n):
        self._body = json.dumps(
            {"data": [{"index": i, "embedding": [0.1, 0.2]}
                      for i in range(n)]}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestEmbedVectorsWarning(unittest.TestCase):
    def _run(self, texts):
        def fake_urlopen(req, timeout=None):
            payload = json.loads(req.data.decode("utf-8"))
            return _FakeEmbedResponse(len(payload["input"]))
        buf = io.StringIO()
        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            with redirect_stdout(buf):
                vecs = pp.embed_vectors("http://embed.invalid/v1",
                                        "embed-model", texts)
        return vecs, buf.getvalue()

    def test_overlong_input_warns_and_vectors_unchanged(self):
        long_text = "prose " * (BUDGET // 2)  # well over the budget
        vecs, out = self._run(["short input", long_text])
        self.assertEqual(vecs, [[0.1, 0.2], [0.1, 0.2]])
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("1 of 2", lines[0])
        self.assertIn(str(len(long_text)), lines[0])
        self.assertIn(long_text[:50], lines[0])

    def test_all_fitting_batch_is_silent(self):
        vecs, out = self._run(["short input", "another short one"])
        self.assertEqual(vecs, [[0.1, 0.2], [0.1, 0.2]])
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
