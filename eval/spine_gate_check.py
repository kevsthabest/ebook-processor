#!/usr/bin/env python3
"""Spine §8 validation gate — the runnable half, executed in the
build sandbox (2026-10-15).

Signals 1, 4, 5 run in full. Signals 2 and 3 are proven at the
mechanism level here (synthetic book, deterministic stub backend,
real-shape stores) — their book-level forms (the three gate books'
live previews; a Fourth Wing snapshot/replay) need the books, the
user's config and a live LLM, none of which exist in this sandbox;
the scorecard records those as machine-owed, not as passed.

Prints one line per check: PASS / FAIL / NOT-RUNNABLE + evidence.
Exit code 0 iff no FAIL.
"""
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
spec = importlib.util.spec_from_file_location(
    "process_portable", str(ROOT / "process-portable.py"))
pp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp)

RESULTS = []


def check(signal, name, ok, evidence=""):
    RESULTS.append((signal, name, ok, evidence))
    print(f"[{'PASS' if ok else 'FAIL'}] {signal} {name} {evidence}")


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


# ------------------------------------------------------------------
# Signal 1: migration safety over real-shape store copies
# ------------------------------------------------------------------
def signal_1():
    sys.path.insert(0, str(ROOT / "tests"))
    import test_spine_migration as tm
    td = tempfile.TemporaryDirectory()
    d = Path(td.name)
    tm._seed_json_stores(d)
    before_hashes = {p.name: _sha(p)
                     for p in d.rglob("*.json") if p.is_file()}
    before = pp.StagingState(learn_dir=d, enabled=True).data
    rep = pp.migrate_state_to_substrate(d)
    check("S1", "migration completes", rep["status"] == "migrated",
          f"counts={rep.get('counts')}")
    db = pp._state_db_for(d)
    counts = {t: db.query(f"SELECT COUNT(*) FROM {t}")[0][0]
              for t in ("learned_nicknames", "staging_works",
                        "staging_claims", "roster_works",
                        "series_state")}
    check("S1", "per-table counts", counts == {
        "learned_nicknames": 1, "staging_works": 2,
        "staging_claims": 3, "roster_works": 1, "series_state": 2},
        str(counts))
    after = pp.StagingState(learn_dir=d, enabled=True).data
    check("S1", "staging identity closes + content equal",
          pp.staging_report_data(
              pp.StagingState(learn_dir=d, enabled=True))
          ["unaccounted"] == 0 and after == before)
    roster = pp.RosterStore(learn_dir=d, work_key="wk-a")
    check("S1", "roster accounting closes",
          roster.accounting()["unaccounted"] == 0)
    renamed = list(d.glob("*.migrated-*"))
    check("S1", "originals renamed, never deleted",
          len(renamed) >= 5 and not (d / "staging.json").exists(),
          f"{len(renamed)} renamed")
    # Rollback: delete state.db, strip suffixes, bytes must match.
    pp._close_state_dbs()
    (d / "state.db").unlink()
    for p in list(d.rglob("*.migrated-*")):
        p.rename(Path(str(p).rsplit(".migrated-", 1)[0]))
    restored = {p.name: _sha(p) for p in d.rglob("*.json")
                if p.is_file()}
    check("S1", "rollback restores byte-identical JSON",
          restored == before_hashes)
    # Corrupt store aborts; JSON proceeds.
    (d / "staging.json").write_text("{not json", encoding="utf-8")
    rep2 = pp.migrate_state_to_substrate(d)
    check("S1", "corrupt store aborts, JSON authoritative",
          rep2["status"] == "aborted"
          and not (d / "state.db").exists())
    td.cleanup()


# ------------------------------------------------------------------
# Signal 2 (mechanism): flag-off preview bytes across the migration
# ------------------------------------------------------------------
_FILLER = ("<p>The valley was quiet and the river ran cold and "
           "bright under a wide northern sky.</p>")


def _make_epub(path):
    def xhtml(body):
        return ("<?xml version='1.0' encoding='utf-8'?>"
                "<html xmlns='http://www.idpf.org/2007/xhtml'>"
                f"<body>{body}</body></html>")
    files = {}
    for i in range(1, 4):
        files[f"OEBPS/ch{i}.xhtml"] = xhtml(
            f"<h1>Chapter {i}</h1><p>Mira Solace drew her sword "
            "and faced the dawn.</p>" + _FILLER * 22)
    manifest = "\n".join(
        f'    <item id="i{n}" href="{n_.split("/", 1)[1]}" '
        'media-type="application/xhtml+xml"/>'
        for n, n_ in enumerate(files))
    spine = "\n".join(f'    <itemref idref="i{n}"/>'
                      for n in files)
    opf = ("<?xml version='1.0' encoding='utf-8'?>\n"
           "<package xmlns='http://www.idpf.org/2007/opf' "
           "version='3.0' unique-identifier='bid'>\n"
           "  <metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>\n"
           "    <dc:title>Gate Book</dc:title>\n"
           "    <dc:creator>Test Author</dc:creator>\n"
           "    <dc:identifier>9780306406157</dc:identifier>\n"
           "  </metadata>\n  <manifest>\n" + manifest +
           "\n  </manifest>\n  <spine>\n" + spine +
           "\n  </spine>\n</package>")
    container = ("<?xml version='1.0' encoding='utf-8'?>\n"
                 "<container xmlns='urn:oasis:names:tc:opendocument:"
                 "xmlns:container' version='1.0'>\n"
                 "  <rootfiles><rootfile full-path='OEBPS/"
                 "content.opf'/></rootfiles>\n</container>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, content in files.items():
            z.writestr(name, content)


class _Backend:
    def __call__(self, prompt, chunk, i, n):
        if prompt == pp.PROMPT_V2_CONTENT:
            return json.dumps({
                "summary": "A chapter summary.", "spice_level": 0,
                "triggers": {}, "trigger_evidence": {},
                "trope_candidates": ["Found Family"], "quotes": []})
        if "confirming trope candidates" in prompt:
            return json.dumps({"verdicts": [
                {"trope": "Found Family", "verdict": "yes"}]})
        return json.dumps({
            "characters": [{
                "name": "Mira Solace", "role": "protagonist",
                "description": "A swordswoman of the valley.",
                "evidence": "Mira Solace drew her sword."}],
            "relationships": [], "pov_character": "Mira Solace"})


def _seed_learn(d):
    d.mkdir(parents=True, exist_ok=True)
    (d / "nicknames.json").write_text(json.dumps(
        {"mira": {"full": "mira solace", "count": 2,
                  "books": ["x"], "human": False, "manual": False}}),
        encoding="utf-8")
    (d / "titles.json").write_text(json.dumps(
        {"captain": {"count": 2, "rests": ["a"]}}), encoding="utf-8")
    (d / "threshold_stats.json").write_text(json.dumps(
        {"gate": {"kept": [0.9], "dropped": []}}), encoding="utf-8")
    (d / "rejected_tropes.json").write_text(json.dumps(
        {"heist": {"count": 1, "books": ["Other"],
                   "reasons": ["no"], "human": True}}),
        encoding="utf-8")
    (d / "series").mkdir(exist_ok=True)
    (d / "series" / "author_test_author.json").write_text(json.dumps(
        {"characters": [{"name": "Mira Solace",
                         "aliases": ["Mira"]}], "books": []}),
        encoding="utf-8")


def _run_preview(book, learn_dir):
    pp._LEARNED = pp.LearnedState(learn_dir=learn_dir, enabled=True)
    pp._STAGING_STATE = None
    stem = Path(book).stem
    pj = pp.SCRIPT_DIR / "preview" / f"{stem}.json"
    pm = pp.SCRIPT_DIR / "preview" / f"{stem}.md"
    with mock.patch.object(pp, "llm_ollama", _Backend()), \
            mock.patch.object(pp, "_lookup_series_openlibrary",
                              lambda *a, **k: (None, None)), \
            mock.patch.object(pp, "resolve_work",
                              lambda *a, **k: (None, "test")), \
            mock.patch.dict(pp.CONFIG, {"llm": "ollama"}):
        ok = pp.process_file_v2(Path(book), preview=True)
    assert ok
    out = (pj.read_bytes(), pm.read_bytes())
    pj.unlink(missing_ok=True)
    pm.unlink(missing_ok=True)
    shutil.rmtree(pp._checkpoint_dir(
        pp._checkpoint_book_key(Path(book))), ignore_errors=True)
    return out


def signal_2():
    td = tempfile.TemporaryDirectory()
    base = Path(td.name)
    learn = base / "learned"
    _seed_learn(learn)
    book = base / "gatebook.epub"
    _make_epub(book)

    def reports():
        st = pp.StagingState(learn_dir=learn, enabled=True)
        ls = pp.LearnedState(learn_dir=learn, enabled=True)
        return (pp._staging_report_text(st),
                pp._learn_status_text(ls))

    pre_reports = reports()
    pre = _run_preview(book, learn)
    rep = pp.migrate_state_to_substrate(learn)
    assert rep["status"] == "migrated", rep
    post_reports = reports()
    post = _run_preview(book, learn)
    check("S2", "flag-off preview JSON byte-identical across "
          "migration", pre[0] == post[0],
          f"sha {_sha_bytes(pre[0])} vs {_sha_bytes(post[0])}")
    check("S2", "flag-off preview MD byte-identical across "
          "migration", pre[1] == post[1])
    check("S2", "store reports identical across migration "
          "(staging + learn-status body)",
          pre_reports == post_reports)
    td.cleanup()


def _sha_bytes(b):
    return hashlib.sha256(b).hexdigest()[:16]


# ------------------------------------------------------------------
# Signal 3 (harness): replay soundness + the two numbers
# ------------------------------------------------------------------
def signal_3():
    sys.path.insert(0, str(ROOT / "tests"))
    import test_spine_replay as tr
    import preview_variance as pv
    td = tempfile.TemporaryDirectory()
    base = Path(td.name)
    learn = base / "learned"
    learn.mkdir()
    case = tr.TestReplay("test_replay_never_writes_live_stores")
    case.dir = base
    case.learn = learn
    case._saved_learned = pp._LEARNED
    case._saved_replay = pp._REPLAY_CTX
    case._saved_capture = pp._CAPTURE_CTX
    case._saved_ckpt_dir = pp._checkpoint_dir
    try:
        book, fp, live_hash = case._seed_live()
        snap_dir = learn / "snapshots" / fp / "snap-1"
        snap_hash = hashlib.sha256(
            (snap_dir / "chapter_1.json").read_bytes()).hexdigest()
        code, report = pp.run_replay(
            "snap-1", book, learn,
            process_fn=lambda *a, **k: True)
        check("S3", "replay runs; live substrate logical hash "
              "unchanged", code == 0
              and pp._state_db_for(learn).dump_hash() == live_hash)
        check("S3", "snapshot payload hash unchanged",
              hashlib.sha256((snap_dir / "chapter_1.json")
                             .read_bytes()).hexdigest() == snap_hash
              and report["sandbox_deleted"])
    finally:
        pp._LEARNED = case._saved_learned
        pp._REPLAY_CTX = case._saved_replay
        pp._CAPTURE_CTX = case._saved_capture
        pp._checkpoint_dir = case._saved_ckpt_dir
        pp._close_state_dbs()
        td.cleanup()
    # The two numbers: unchanged replay -> J 1.000; a perturbed
    # replay moves pipeline-delta only.
    base_pv = {"title": "B", "author": "A",
               "tropes": [{"name": "X"}], "triggers": [],
               "characters": [{"name": "X", "entity_id": "ent_0001"}],
               "snapshot_captured": "s1"}
    same = dict(base_pv)
    same.pop("snapshot_captured")
    same["replay"] = {"snapshot_id": "s1", "source_code_sha": "a",
                      "source_config_digest": "d",
                      "replay_code_sha": "a",
                      "replay_config_digest": "d"}
    moved = dict(same)
    moved["tropes"] = [{"name": "Y"}]
    moved["replay"] = dict(same["replay"], replay_code_sha="b")
    fresh2 = {"title": "B", "author": "A", "tropes": [{"name": "X"}],
              "triggers": [],
              "characters": [{"name": "X", "entity_id": "ent_0001"}]}
    sec_same = "\n".join(pv.replay_variance_section([base_pv, same]))
    sec_moved = "\n".join(pv.replay_variance_section(
        [base_pv, moved, fresh2]))
    check("S3", "unchanged replay: pipeline-delta J = 1.000",
          "tropes J 1.000" in sec_same)
    check("S3", "perturbed replay: pipeline-delta moves, "
          "draw-variance reported separately",
          "tropes J 0.000" in sec_moved
          and "draw-variance" in sec_moved)


# ------------------------------------------------------------------
# Signal 4: read-back honesty on a scratch work
# ------------------------------------------------------------------
def signal_4():
    td = tempfile.TemporaryDirectory()
    d = Path(td.name)
    pp.StateDB.create(d)
    saved_sb = pp.sb
    saved_cfg = dict(pp.CONFIG)
    pp.CONFIG["supabase_url"] = "https://example.invalid"
    pp.CONFIG["supabase_key"] = "k"
    state = {"fail": False, "status": "rejected"}
    tid = "11111111-2222-3333-4444-555555555555"

    def fake_sb(table, method="GET", data=None, params="",
                prefer=None):
        assert method == "GET"
        if state["fail"]:
            return None
        if table == "book_trope_claims":
            return [{"trope_id": tid, "status": state["status"]}]
        return []

    pp.sb = fake_sb
    try:
        vs, _ = pp.work_verdicts("wid", d)
        out = pp.apply_verdict_suppression(
            ["Found Family"], {}, [], vs.rejected_keys(),
            {"found family": tid})
        check("S4", "seeded rejection suppresses + names the claim",
              out[0] == [] and out[3] == {f"trope:id:{tid}"}
              and out[4] == ["Found Family"])
        pp.record_rejection_events(d, "run", "wk", out[3])
        rows = pp._state_db_for(d).query(
            "SELECT COUNT(*) FROM rejection_events")
        check("S4", "suppression counted exactly once",
              rows[0][0] == 1)
        # Unreachable + cache present: cache used, age printed in
        # the note; then cache-less dir: nothing suppressed.
        state["fail"] = True
        vs2, note2 = pp.work_verdicts("wid", d)
        check("S4", "failed read falls back to cache with its age",
              vs2 is not None and vs2.from_cache
              and "cached verdicts from" in note2)
        td2 = tempfile.TemporaryDirectory()
        d2 = Path(td2.name)
        pp.StateDB.create(d2)
        vs3, note3 = pp.work_verdicts("wid", d2)
        check("S4", "failed read, no cache: nothing suppressed",
              vs3 is None and "nothing suppressed" in note3)
        td2.cleanup()
        # A lifted rejection stops suppressing on the next fetch.
        state["fail"] = False
        state["status"] = "confirmed"
        vs4, _ = pp.work_verdicts("wid", d)
        check("S4", "lifted rejection stops suppressing",
              vs4.rejected_keys() == set())
    finally:
        pp.sb = saved_sb
        pp.CONFIG.clear()
        pp.CONFIG.update(saved_cfg)
        pp._close_state_dbs()
        td.cleanup()


# ------------------------------------------------------------------
# Signal 5: fixture truthfulness
# ------------------------------------------------------------------
def signal_5():
    import build_review_fixture as bf
    import score_review_fixture as sf
    seat = Path.home() / "workspace" / "trope-review"
    if not seat.is_dir():
        print("[NOT-RUNNABLE] S5 seat files absent")
        return
    fixture, report = bf.build_fixture(seat)
    check("S5", "fixture builds; every seat line accounted for",
          report["unresolved"] == []
          and report["lines_parsed"] > 0,
          f"parsed={report['lines_parsed']} "
          f"unique={report['unique_claims']} "
          f"scored={report['scored']} "
          f"abstains={report['abstains_excluded']} "
          f"flips={report['flips']} "
          f"per-class={report['per_class']} "
          f"per-authority={report['per_authority']} "
          f"kevin={report['kevin_note']}")
    rep = sf.score_fixture(fixture, {})
    check("S5", "scorer runs (no previews in sandbox: all "
          "no-preview, headline named)",
          rep["by_state"]["no-preview"] == report["scored"]
          and rep["headline"]["structural_reject_shipped"] == [],
          f"by_state={rep['by_state']}")


def main():
    signal_1()
    signal_2()
    signal_3()
    signal_4()
    signal_5()
    fails = [r for r in RESULTS if not r[2]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks "
          "passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
