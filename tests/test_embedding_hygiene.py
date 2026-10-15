"""Embedding hygiene batch (2026-10-14) — two verified defects from a
firsthand audit of the embedding code.

Fix 1: trope_catalog_vectors() cached catalog vectors in
CONFIG["trope_map_cache"] keyed ONLY by catalog row id. Changing
embed_model (or the embedded text format) silently reused stale
vectors and mixed them with new ones — cosine comparisons across
different vector spaces. Fix: the cache file is namespaced by
(embed_model, EMBED_TEXT_FORMAT) as {"model", "format", "vectors"};
a missing/mismatched namespace — including the legacy bare-dict file
shape — invalidates the whole cache with a printed notice and
re-embeds. The (id, name, vector) return shape is unchanged.

Fix 2 (F10 amendment): dedupe_characters() degraded on embedding
failure by returning the inputs UN-deduped, discarding the Signal-1
(norm_name exact-match) unions already computed. Signal-1 unions are
the safest merge class in the system — exact normalized-name matches —
and a transient endpoint failure should not throw them away. The
failure path now applies DETERMINISTIC merges only: clustering runs
on the Signal-1 unions, the Signal-2 (cosine) loop is skipped, and
the warning says so. Genuine non-embedding dedupe errors still raise.

Stdlib unittest only. Supabase and the embedding endpoint are faked at
their seams (pp.sb / pp.embed_vectors); no network.
Run: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


class _ConfigGuard(unittest.TestCase):
    """Snapshot/restore the module CONFIG dict in place."""

    def setUp(self):
        self._config = dict(pp.CONFIG)

    def tearDown(self):
        pp.CONFIG.clear()
        pp.CONFIG.update(self._config)


# ----------------------------------------------------------------------
# Fix 1 — trope catalog vector cache namespacing
# ----------------------------------------------------------------------

class TestTropeCatalogCacheNamespace(_ConfigGuard):
    ROWS = [
        {"id": "t1", "name": "Enemies to Lovers",
         "description": "rivals fall in love"},
        {"id": "t2", "name": "Found Family",
         "description": "a chosen kin group"},
    ]
    STALE = [1.0, 0.0]
    FRESH = [0.0, 1.0]

    def setUp(self):
        super().setUp()
        self.td = tempfile.mkdtemp()
        self.cache_path = str(Path(self.td) / "trope-vectors.json")
        pp.CONFIG.update({
            "supabase_url": "http://fake.invalid",
            "embed_url": "http://fake.invalid/v1",
            "embed_model": "model-a",
            "trope_map_cache": self.cache_path,
        })
        self.embed_calls = []

        def fake_embed(url, model, texts):
            self.embed_calls.append(list(texts))
            return [list(self.FRESH) for _ in texts]

        self._sb = mock.patch.object(
            pp, "sb", return_value=[dict(r) for r in self.ROWS])
        self._embed = mock.patch.object(pp, "embed_vectors",
                                        side_effect=fake_embed)
        self._sb.start()
        self._embed.start()

    def tearDown(self):
        self._embed.stop()
        self._sb.stop()
        shutil.rmtree(self.td, ignore_errors=True)
        super().tearDown()

    def _write_cache(self, payload):
        Path(self.cache_path).write_text(json.dumps(payload),
                                         encoding="utf-8")

    def _read_cache(self):
        return json.loads(Path(self.cache_path).read_text(encoding="utf-8"))

    def test_legacy_bare_dict_cache_is_invalidated(self):
        # The pre-fix on-disk shape: {catalog_id: vector}, no namespace.
        self._write_cache({"t1": self.STALE, "t2": self.STALE})
        out, printed = _capture(pp.trope_catalog_vectors)
        self.assertIn("invalidated", printed)
        self.assertEqual(len(self.embed_calls), 1)
        self.assertEqual(len(self.embed_calls[0]), 2)  # whole catalog
        # Returned vectors are the FRESH embeddings, not the stale ones.
        self.assertEqual([(r[0], r[1]) for r in out],
                         [("t1", "Enemies to Lovers"),
                          ("t2", "Found Family")])
        self.assertEqual([list(r[2]) for r in out],
                         [self.FRESH, self.FRESH])
        # The rewritten cache carries the namespace.
        saved = self._read_cache()
        self.assertEqual(saved["model"], "model-a")
        self.assertEqual(saved["format"], pp.EMBED_TEXT_FORMAT)
        self.assertEqual(saved["vectors"],
                         {"t1": self.FRESH, "t2": self.FRESH})

    def test_model_change_invalidates_cache(self):
        self._write_cache({
            "model": "some-other-model",
            "format": pp.EMBED_TEXT_FORMAT,
            "vectors": {"t1": self.STALE, "t2": self.STALE},
        })
        out, printed = _capture(pp.trope_catalog_vectors)
        self.assertIn("invalidated", printed)
        self.assertEqual(len(self.embed_calls), 1)
        self.assertEqual([list(r[2]) for r in out],
                         [self.FRESH, self.FRESH])
        self.assertEqual(self._read_cache()["model"], "model-a")

    def test_format_change_invalidates_cache(self):
        self._write_cache({
            "model": "model-a",
            "format": "obsolete-format-v0",
            "vectors": {"t1": self.STALE, "t2": self.STALE},
        })
        out, printed = _capture(pp.trope_catalog_vectors)
        self.assertIn("invalidated", printed)
        self.assertEqual(len(self.embed_calls), 1)
        self.assertEqual([list(r[2]) for r in out],
                         [self.FRESH, self.FRESH])

    def _hash(self, row):
        return pp._trope_text_hash(
            f"{row['name']}: {row.get('description') or ''}")

    def _valid_cache(self, vectors=None, rows=None):
        rows = self.ROWS if rows is None else rows
        vectors = {r["id"]: self.STALE for r in rows} \
            if vectors is None else vectors
        return {"model": "model-a",
                "format": pp.EMBED_TEXT_FORMAT,
                "vectors": vectors,
                "hashes": {r["id"]: self._hash(r) for r in rows}}

    def test_matching_namespace_cache_is_reused(self):
        # Pin updated for fix-queue batch 3 item 2: reuse now also
        # requires each entry's content hash to match the catalog
        # row's current text — a namespaced cache WITHOUT hashes
        # migrates by re-embedding (see the stale-text test below).
        self._write_cache(self._valid_cache())
        out, printed = _capture(pp.trope_catalog_vectors)
        self.assertNotIn("invalidated", printed)
        self.assertEqual(self.embed_calls, [])  # no re-embedding at all
        # The cached (stale-looking but namespaced-valid) vectors return.
        self.assertEqual([list(r[2]) for r in out],
                         [self.STALE, self.STALE])

    def test_catalog_text_edit_reembeds_only_the_edited_id(self):
        # Same row id, edited description: the id key alone used to
        # serve the stale vector forever. The content hash turns the
        # edited entry into a miss; the untouched entry stays a hit.
        self._write_cache(self._valid_cache())
        edited = [dict(self.ROWS[0]),
                  {"id": "t2", "name": "Found Family",
                   "description": "a chosen kin group, revised"}]
        pp.sb.return_value = [dict(r) for r in edited]
        out, printed = _capture(pp.trope_catalog_vectors)
        self.assertEqual(self.embed_calls,
                         [["Found Family: a chosen kin group, "
                           "revised"]])
        by_id = {r[0]: list(r[2]) for r in out}
        self.assertEqual(by_id["t1"], self.STALE)  # untouched: cached
        self.assertEqual(by_id["t2"], self.FRESH)  # edited: re-embedded
        saved = self._read_cache()
        self.assertEqual(saved["hashes"]["t2"], self._hash(edited[1]))
        self.assertEqual(saved["hashes"]["t1"],
                         self._hash(self.ROWS[0]))
        self.assertEqual(saved["vectors"]["t2"], self.FRESH)

    def test_hashless_namespaced_cache_migrates_by_reembedding(self):
        # A cache written before content hashing (namespace matches,
        # no "hashes" at all) cannot prove any entry matches the
        # current text: every entry is a miss, re-embedded once, and
        # the rewritten cache carries hashes.
        self._write_cache({
            "model": "model-a",
            "format": pp.EMBED_TEXT_FORMAT,
            "vectors": {"t1": self.STALE, "t2": self.STALE},
        })
        out, printed = _capture(pp.trope_catalog_vectors)
        self.assertEqual(len(self.embed_calls), 1)
        self.assertEqual(len(self.embed_calls[0]), 2)
        self.assertEqual([list(r[2]) for r in out],
                         [self.FRESH, self.FRESH])
        saved = self._read_cache()
        self.assertEqual(saved["hashes"],
                         {r["id"]: self._hash(r) for r in self.ROWS})

    def test_deleted_catalog_id_is_pruned_from_cache(self):
        cache = self._valid_cache()
        cache["vectors"]["tGONE"] = self.STALE
        cache["hashes"]["tGONE"] = "0" * 64
        self._write_cache(cache)
        out, printed = _capture(pp.trope_catalog_vectors)
        # Both live entries are valid hits — nothing re-embeds — but
        # the dead id must not linger in the file.
        self.assertEqual(self.embed_calls, [])
        self.assertEqual([r[0] for r in out], ["t1", "t2"])
        saved = self._read_cache()
        self.assertNotIn("tGONE", saved["vectors"])
        self.assertNotIn("tGONE", saved["hashes"])
        self.assertEqual(saved["vectors"],
                         {"t1": self.STALE, "t2": self.STALE})

    def test_cache_write_is_atomic_temp_then_replace(self):
        import os
        real_replace = os.replace
        calls = []

        def spy_replace(src, dst):
            calls.append((src, dst))
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=spy_replace):
            out, printed = _capture(pp.trope_catalog_vectors)
        self.assertEqual([list(r[2]) for r in out],
                         [self.FRESH, self.FRESH])
        self.assertEqual(calls, [(self.cache_path + ".tmp",
                                  self.cache_path)])
        self.assertFalse(Path(self.cache_path + ".tmp").exists())
        self.assertTrue(Path(self.cache_path).exists())

    def test_missing_cache_embeds_and_writes_namespaced(self):
        out, printed = _capture(pp.trope_catalog_vectors)
        self.assertEqual(len(self.embed_calls), 1)
        self.assertEqual([list(r[2]) for r in out],
                         [self.FRESH, self.FRESH])
        saved = self._read_cache()
        self.assertEqual(saved["model"], "model-a")
        self.assertEqual(saved["format"], pp.EMBED_TEXT_FORMAT)
        self.assertEqual(saved["vectors"],
                         {"t1": self.FRESH, "t2": self.FRESH})
        self.assertEqual(saved["hashes"],
                         {r["id"]: self._hash(r) for r in self.ROWS})
        # Second call reuses what the first wrote — no further embeds.
        out2, _ = _capture(pp.trope_catalog_vectors)
        self.assertEqual(len(self.embed_calls), 1)
        self.assertEqual([list(r[2]) for r in out2],
                         [self.FRESH, self.FRESH])


# ----------------------------------------------------------------------
# Fix 2 — F10 amendment: deterministic merges survive embedding failure
# ----------------------------------------------------------------------

class TestDedupeDeterministicFallback(_ConfigGuard):

    def _run(self, chars, rels, povs):
        buf = io.StringIO()
        with mock.patch.dict(pp.CONFIG,
                             {"embed_url": "http://127.0.0.1:9/v1"}), \
                mock.patch.object(pp, "embed_vectors",
                                  side_effect=RuntimeError("could not reach")), \
                contextlib.redirect_stdout(buf):
            out = pp.dedupe_characters(chars, rels, povs)
        return out, buf.getvalue()

    def test_embed_failure_applies_signal1_merges(self):
        # norm_name() casefolds: these two entries are ONE character by
        # the deterministic signal — an exact normalized-name match.
        chars = [{"name": "Violet Sorrengail", "description": "a rider",
                  "confidence": 0.8},
                 {"name": "violet sorrengail", "description": "a rider",
                  "confidence": 0.9}]
        rels = [{"from": "Violet Sorrengail", "to": "Xaden Riorson",
                 "type": "friend", "evidence": "q1"},
                {"from": "violet sorrengail", "to": "Xaden Riorson",
                 "type": "friend", "evidence": "q2"}]
        povs = ["violet sorrengail", "Xaden Riorson"]
        (out_chars, out_rels, out_povs, report), printed = self._run(
            chars, rels, povs)
        self.assertIn("DETERMINISTIC merges only", printed)
        self.assertNotIn("UN-DEDUPED", printed)
        # The two entries merged via the normal cluster path: canonical
        # member is the highest-confidence one, fields unioned.
        self.assertEqual(len(out_chars), 1)
        self.assertEqual(out_chars[0]["name"], "violet sorrengail")
        self.assertEqual(out_chars[0]["confidence"], 0.9)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["canonical"], "violet sorrengail")
        self.assertEqual(report[0]["merged"], ["Violet Sorrengail"])
        # Both relationship dicts remapped onto the canonical name and
        # deduped to one; the full dict (evidence included) survives.
        self.assertEqual(len(out_rels), 1)
        self.assertEqual(out_rels[0]["from"], "violet sorrengail")
        self.assertEqual(out_rels[0]["to"], "Xaden Riorson")
        self.assertEqual(out_rels[0]["type"], "friend")
        self.assertIn(out_rels[0]["evidence"], ("q1", "q2"))
        # POVs remapped + deduped (the function returns them sorted).
        self.assertEqual(out_povs, ["Xaden Riorson", "violet sorrengail"])

    def test_embed_failure_no_signal1_dupes_output_identical(self):
        chars = [{"name": "Jon Pryce", "description": "a knight",
                  "confidence": 0.8},
                 {"name": "Mira Sorrengail", "description": "a rider",
                  "confidence": 0.9}]
        rels = [{"from": "Jon Pryce", "to": "Mira Sorrengail",
                 "type": "friend", "evidence": "q"}]
        povs = ["Jon Pryce"]
        (out_chars, out_rels, out_povs, report), printed = self._run(
            chars, rels, povs)
        self.assertEqual([c["name"] for c in out_chars],
                         ["Jon Pryce", "Mira Sorrengail"])
        self.assertEqual(out_rels, rels)
        self.assertEqual(out_povs, povs)
        self.assertEqual(report, [])
        self.assertIn("DETERMINISTIC merges only", printed)
        self.assertNotIn("UN-DEDUPED", printed)

    def test_embed_failure_never_applies_cosine_merge(self):
        # Identical descriptions: with working embeddings these two
        # would cosine-match and merge. With embeddings DOWN, the
        # Signal-2 loop must not run at all — names are not norm-equal,
        # so both entries survive.
        chars = [{"name": "Jon Pryce", "description": "a knight",
                  "confidence": 0.8},
                 {"name": "Jonathan Pryce", "description": "a knight",
                  "confidence": 0.9}]
        (out_chars, out_rels, out_povs, report), printed = self._run(
            chars, [], [])
        self.assertEqual([c["name"] for c in out_chars],
                         ["Jon Pryce", "Jonathan Pryce"])
        self.assertEqual(report, [])
        self.assertIn("DETERMINISTIC merges only", printed)


if __name__ == "__main__":
    unittest.main()
