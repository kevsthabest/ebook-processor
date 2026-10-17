-- =====================================================================
-- processor corpus snapshots — migration (APPLIED 2026-10-17)
-- Drafted 2026-10-17 from corpus-snapshot-spec.md. Applied via the
-- Management API SQL path after the Security Advisor gate
-- (PASS_WITH_CHANGES; the completion marker and finalize-PATCH
-- verification below are its required edits, folded in) and under
-- the user's implementation go for the corpus-snapshot build
-- (2026-10-17). PostgREST already exposes the "processor" schema
-- (run history, 2026-10-17); no project-config change was needed.
-- Verification PASSED 2026-10-17: catalog grants service_role only
-- (anon/authenticated zero), RLS true on both tables, zero policies,
-- columns 17/26 as drafted; anon PostgREST probe 401 (42501
-- permission denied for schema processor); service-key round trip
-- (insert complete=false -> book insert -> finalize PATCH ->
-- read -> delete with CASCADE) with tables verified empty after.
-- (Two round-trip responses were lost in transit mid-check; each
-- write was confirmed landed via SQL before continuing — the exact
-- lost-response case the completion marker exists for.)
--
-- CUSTODY: these tables are PROCESSOR-owned. This file lives with the
-- ebook-processor records and must never be numbered into the Cozy
-- app's vNNN migration sequence or placed in the app repo's
-- migrations folder. Cozy-side migrations never alter these tables.
--
-- HOME: the dedicated "processor" schema (RH-4 ruling). Locked down
-- exactly like the run-history tables: RLS enabled, zero policies,
-- grants to service_role only.
--
-- RETENTION: snapshots are kept (the RH-1 posture). The exit —
-- delete by captured_at, or drop both tables — is available only on
-- a future explicit ruling; see the rollback/exit block at the end.
-- =====================================================================

BEGIN;

CREATE TABLE processor.processor_corpus_snapshots (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_key        text UNIQUE NOT NULL,  -- snapshot-<uuid4 hex>, writer-generated
    captured_at         timestamptz NOT NULL,
    machine_label       text,  -- opt-in, user-chosen; never auto-collected
    processor_version   text,
    corpus_parser_version text,
    corpus_v            text,
    corpus_fingerprint  text NOT NULL,  -- sha256 over the canonical per-work identity set (spec section 6)
    works_count         int NOT NULL CHECK (works_count >= 0),
    corpus_bytes        bigint CHECK (corpus_bytes IS NULL OR corpus_bytes >= 0),
    size_breakdown      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- numbers only
    fts_available       boolean,
    current_generation  jsonb,  -- projected {model, format, dim, canary, born, checked}; NEVER sentinel vectors/cosines
    generations         jsonb NOT NULL DEFAULT '[]'::jsonb,  -- same projection, all recorded generations
    totals              jsonb NOT NULL DEFAULT '{}'::jsonb,  -- measured counts, explicit values
    complete            boolean NOT NULL DEFAULT false,  -- set true by the writer's finalize PATCH only after every book chunk lands with its expected count; complete=false rows are partial uploads, never verification sources
    uploaded_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE processor.processor_corpus_snapshot_books (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id                uuid NOT NULL
                               REFERENCES processor.processor_corpus_snapshots(id) ON DELETE CASCADE,
    fingerprint                text NOT NULL,  -- corpus fingerprint; the canonical join
    book_key                   text,
    work_id                    uuid,  -- SOFT reference, NO foreign key by design:
                                      -- snapshots must survive works merges/clears;
                                      -- a dangling value records what the corpus
                                      -- row held at capture time. Symmetric rule:
                                      -- no app table FKs into processor_* either.
    title                      text,
    author                     text,
    isbn                       text,  -- cleaned ISBN-13 only; ASIN and raw identifier lists never transfer
    series                     text,
    series_index               numeric,
    status                     text,  -- stored corpus status; deliberately NO CHECK:
                                      -- the corpus state vocabulary is the
                                      -- processor's to evolve, and a CHECK
                                      -- violation would fail a whole snapshot
                                      -- for one work's new state
    parser_version             text,
    chapters                   int CHECK (chapters IS NULL OR chapters >= 0),
    words                      int CHECK (words IS NULL OR words >= 0),
    passages                   int CHECK (passages IS NULL OR passages >= 0),
    vectors                    int CHECK (vectors IS NULL OR vectors >= 0),
    vector_coverage            numeric CHECK (vector_coverage IS NULL
                               OR (vector_coverage >= 0 AND vector_coverage <= 1)),
    chapter_detection_fallback boolean,
    text_root_hash             text,
    source_hash                text,
    quotes                     jsonb NOT NULL DEFAULT '{}'::jsonb,  -- counts only, never quote text
    envelope_corpus_v          text,
    envelope_computed_at       timestamptz,
    ingested_at                timestamptz,
    updated_at                 timestamptz,
    uploaded_at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, fingerprint)  -- also serves snapshot_id lookups (leftmost prefix)
);

CREATE INDEX processor_corpus_snapshots_captured_at_idx
    ON processor.processor_corpus_snapshots (captured_at);
CREATE INDEX processor_corpus_snapshots_machine_captured_idx
    ON processor.processor_corpus_snapshots (machine_label, captured_at);
CREATE INDEX processor_corpus_snapshots_fingerprint_idx
    ON processor.processor_corpus_snapshots (corpus_fingerprint);
CREATE INDEX processor_corpus_snapshot_books_fingerprint_idx
    ON processor.processor_corpus_snapshot_books (fingerprint);
CREATE INDEX processor_corpus_snapshot_books_book_key_idx
    ON processor.processor_corpus_snapshot_books (book_key);
CREATE INDEX processor_corpus_snapshot_books_work_id_idx
    ON processor.processor_corpus_snapshot_books (work_id);
CREATE INDEX processor_corpus_snapshot_books_status_idx
    ON processor.processor_corpus_snapshot_books (status);

-- Locked down: RLS on, NO policies for anon/authenticated, and service
-- role is the only grantee. The app's keys can neither read nor write.
ALTER TABLE processor.processor_corpus_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE processor.processor_corpus_snapshot_books ENABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON processor.processor_corpus_snapshots,
       processor.processor_corpus_snapshot_books
    TO service_role;
-- Deliberately NO grants to anon or authenticated, anywhere above.
-- (Schema USAGE for service_role was granted by the run-history
-- migration and already covers these tables.)

COMMIT;

-- =====================================================================
-- VERIFICATION (run after apply, before first use — spec section 10):
-- 1. Grants, from the catalog:
--      SELECT grantee, table_name, privilege_type
--        FROM information_schema.role_table_grants
--       WHERE table_schema = 'processor'
--         AND table_name LIKE 'processor_corpus%';
--    Expect: service_role rows only; nothing for anon/authenticated.
-- 1b. RLS from the catalog:
--      SELECT c.relname, c.relrowsecurity FROM pg_class c
--        JOIN pg_namespace n ON n.oid = c.relnamespace
--       WHERE n.nspname = 'processor' AND c.relkind = 'r'
--         AND c.relname LIKE 'processor_corpus%';
--    Expect relrowsecurity = true for both tables; and
--      SELECT count(*) FROM pg_policies
--       WHERE schemaname = 'processor'
--         AND tablename LIKE 'processor_corpus%';
--    Expect 0.
-- 2. Anon-key PostgREST probe of both tables returns nothing/error.
-- 3. RLS-faithful authenticated-role read returns zero rows.
-- 4. Positive service-key REST round trip (insert marker snapshot ->
--    insert marker book -> read back -> finalize PATCH complete=true
--    -> read back -> delete snapshot; the book must follow via the
--    internal CASCADE).
--
-- ROLLBACK (destroys the snapshot record — a data decision, not an
-- undo; child first for FK order):
--   DROP TABLE processor.processor_corpus_snapshot_books;
--   DROP TABLE processor.processor_corpus_snapshots;
--
-- RH-1-STYLE EXIT (prune instead of drop, only on a future explicit
-- ruling; book rows follow via the internal CASCADE):
--   DELETE FROM processor.processor_corpus_snapshots
--    WHERE captured_at < now() - interval '180 days';
-- =====================================================================
