-- =====================================================================
-- processor run history — migration (APPLIED 2026-10-17)
-- Drafted 2026-10-17 from run-history-supabase-spec.md (incl. the §13
-- team-review amendments and the §14 RH-4 ruling). Applied via the
-- Management API SQL path after the Advisor's PASS_WITH_CHANGES (all
-- required edits folded in first) and the user's implementation go.
-- PostgREST exposed schemas now include 'processor'. Four-part
-- verification PASSED: catalog grants (service_role only) + RLS on all
-- three tables with zero policies; anon PostgREST probe 401;
-- authenticated role 42501 permission denied for schema; service-key
-- round trip (insert 201 -> read 200 -> delete) with tables verified
-- empty afterwards. The --run-history writer ships separately.
--
-- CUSTODY: these tables are PROCESSOR-owned. This file lives with the
-- ebook-processor records and must never be numbered into the Cozy app's
-- vNNN migration sequence or placed in the app repo's migrations folder.
-- Cozy-side migrations never alter these tables.
--
-- APPLY PATH: admin SQL only, and only on the user's explicit approval
-- (schema change = stop-for-the-user class, even additive).
--
-- HOME: dedicated schema "processor" (RH-4 ruling). Accepted fallback if
-- the precondition proof below fails or proves fiddly: the same tables
-- in "public" + explicit REVOKEs from anon/authenticated. WARNING: the
-- fallback is a RE-DRAFT written for public's default grants, never a
-- schema-name swap of this file — this file contains no REVOKEs because
-- a fresh schema needs none, and in public a mechanically swapped copy
-- would apply WIDE OPEN (Supabase grants anon/authenticated at CREATE
-- TABLE time there) while looking identical to the reviewed draft.
--
-- APPLY-TIME PREREQUISITES (project config, outside this SQL):
--   1. Add "processor" to PostgREST's exposed schemas
--      (Dashboard -> Settings -> API -> Exposed schemas).
--   2. The processor's REST helper targets the schema with
--      Content-Profile (writes) / Accept-Profile (reads) headers.
-- PRECONDITION PROOF (before --run-history is ever enabled live):
--   exposed schema + headers + the service_role grant below + one
--   service-key write/read round trip, all verified (spec §6/§10).
-- =====================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS processor;
-- A fresh schema inherits none of public's default grants. The only
-- grant issued in this file is to service_role, at the end.

CREATE TABLE processor.processor_runs (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_key            text UNIQUE NOT NULL,  -- the run-events run_id
    started_at         timestamptz NOT NULL,
    finished_at        timestamptz,
    duration_ms        int,
    status             text NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running','completed','interrupted','failed')),
    processor_version  text,
    machine_label      text,  -- opt-in, user-chosen (RH-2); never auto-collected
    mode               text CHECK (mode IN ('scan','watch_cycle','ingest','replay')),
    preview            boolean NOT NULL DEFAULT false,
    dry_run            boolean NOT NULL DEFAULT false,
    flags              jsonb NOT NULL DEFAULT '{}'::jsonb,  -- allowlisted booleans only
    models             jsonb NOT NULL DEFAULT '{}'::jsonb,  -- names only, never endpoint URLs
    prompt_variants    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- variant name + content hash
    thresholds         jsonb NOT NULL DEFAULT '{}'::jsonb,
    config_fingerprint text,
    corpus_generation  text,
    totals             jsonb NOT NULL DEFAULT '{}'::jsonb,  -- explicit NULLs for unmeasured counters
    error_summary      text,  -- truncated + sanitized before upload (spec §5)
    uploaded_at        timestamptz NOT NULL DEFAULT now()  -- distinguishes live rows from backfilled ones
);

CREATE TABLE processor.processor_run_books (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                uuid NOT NULL
                          REFERENCES processor.processor_runs(id) ON DELETE CASCADE,
    book_key              text NOT NULL,  -- corpus fingerprint, else source hash; the canonical join
    title                 text,
    work_id               uuid,  -- SOFT reference, NO foreign key by design:
                                 -- history must survive works merges/clears;
                                 -- a dangling value records what the run
                                 -- resolved at the time. Symmetric rule: no
                                 -- app table FKs into processor_* either.
    source                text CHECK (source IN ('corpus','file')),
    outcome               text CHECK (outcome IN ('processed','skipped','failed')),
    skip_reason           text,
    chapters              int,
    duration_ms           int,
    decision_calls        int,
    claims                jsonb NOT NULL DEFAULT '{}'::jsonb,
                          -- per axis: derived/reused/abstained/failed,
                          -- promoted, tier1/tier2/tier3 counts
    characters            jsonb NOT NULL DEFAULT '{}'::jsonb,
    quotes                jsonb NOT NULL DEFAULT '{}'::jsonb,
    suppressed_by_verdict int,  -- verdict read-back suppressions (regeneration metric)
    replace_result        jsonb,  -- write runs: deleted/inserted/preserved counts
    error_class           text,
    error_message         text,  -- truncated + sanitized before upload (spec §5)
    uploaded_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, book_key)  -- upsert target; also serves run_id lookups (leftmost prefix)
);

CREATE TABLE processor.processor_run_claim_outcomes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_book_id uuid NOT NULL
                REFERENCES processor.processor_run_books(id) ON DELETE CASCADE,
    axis        text NOT NULL CHECK (axis IN ('tropes','triggers')),
    claim_key   text NOT NULL,  -- hash of the claim's normalized identity;
                                -- stable across runs, carries no text
    verdict     text,           -- derived / reused / abstained / failed,
                                -- or the retrieve verdict vocabulary
    confidence  numeric,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_book_id, axis, claim_key)  -- upsert target
);

CREATE INDEX processor_runs_started_at_idx
    ON processor.processor_runs (started_at);
CREATE INDEX processor_runs_version_idx
    ON processor.processor_runs (processor_version);
CREATE INDEX processor_run_books_work_id_idx
    ON processor.processor_run_books (work_id);
CREATE INDEX processor_run_books_book_key_idx
    ON processor.processor_run_books (book_key);

-- Locked down: RLS on, NO policies for anon/authenticated, and service
-- role is the only grantee. The app's keys can neither read nor write.
ALTER TABLE processor.processor_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE processor.processor_run_books ENABLE ROW LEVEL SECURITY;
ALTER TABLE processor.processor_run_claim_outcomes ENABLE ROW LEVEL SECURITY;

GRANT USAGE ON SCHEMA processor TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON processor.processor_runs, processor.processor_run_books,
       processor.processor_run_claim_outcomes
    TO service_role;
-- Deliberately NO grants to anon or authenticated, anywhere above.

COMMIT;

-- =====================================================================
-- VERIFICATION (run after apply, before first enable — spec §6/§10):
-- 1. Grants, positive and negative:
--      SELECT grantee, table_name, privilege_type
--        FROM information_schema.role_table_grants
--       WHERE table_schema = 'processor';
--    Expect: service_role rows only; nothing for anon/authenticated.
-- 1b. RLS verified from the catalog, not trusted from this file:
--      SELECT c.relname, c.relrowsecurity FROM pg_class c
--        JOIN pg_namespace n ON n.oid = c.relnamespace
--       WHERE n.nspname = 'processor' AND c.relkind = 'r';
--    Expect relrowsecurity = true for all three tables; and
--      SELECT count(*) FROM pg_policies WHERE schemaname = 'processor';
--    Expect 0.
-- 2. Anon-key PostgREST probe of all three tables returns nothing/error.
-- 3. RLS-faithful authenticated-role read returns zero rows.
-- 4. Positive service-key REST round trip (insert marker row -> read
--    back -> delete), proving "locked down" is not "locked out".
--
-- ROLLBACK (post-enable this destroys the only mirror of affected runs —
-- a data decision, not an undo; children first for FK order):
--   DROP TABLE processor.processor_run_claim_outcomes;
--   DROP TABLE processor.processor_run_books;
--   DROP TABLE processor.processor_runs;
--   DROP SCHEMA processor;
--   ...and remove 'processor' from PostgREST's exposed schemas
--   (apply-time prerequisite 1, reversed) so no dangling config entry
--   points at a dropped schema.
--
-- RH-1 EXIT (prune instead of drop, if retention is ever revisited;
-- book rows follow via the internal CASCADE):
--   DELETE FROM processor.processor_runs
--    WHERE started_at < now() - interval '180 days';
-- =====================================================================
