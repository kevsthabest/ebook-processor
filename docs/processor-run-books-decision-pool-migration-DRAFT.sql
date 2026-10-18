-- =====================================================================
-- processor run books — decision_pool column (APPLIED 2026-10-18)
-- Drafted 2026-10-18 as step 2 of the v2.13.35 fix. v2.13.35 took
-- the armed decision pool out of the run-history thresholds stamp
-- because that dict is the config fingerprint's input and the pool
-- is per-book runtime state; the per-book stamp itself was deferred
-- because this table had no decision_pool column and the history
-- payload's keys are pinned to the table's columns — a new payload
-- key against a missing column would make PostgREST reject every
-- book-row upload and silently cost the run its whole history.
-- The placement was ruled Option A (the User's call, 2026-10-18,
-- with explicit migration approval): a real column here, applied
-- BEFORE the writer ships, and verified from information_schema
-- before any code landed against it.
--
-- CUSTODY: this table is PROCESSOR-owned. This file lives with the
-- ebook-processor records and must never be numbered into the Cozy
-- app's vNNN migration sequence or placed in the app repo's
-- migrations folder. Cozy-side migrations never alter these tables.
--
-- APPLY PATH: admin SQL only, and only on the user's explicit
-- approval (schema change = stop-for-the-user class, even
-- additive). Approval for THIS migration is on record 2026-10-18.
--
-- NULL MEANS "NOT RECORDED", ON PURPOSE: the column is nullable
-- with no default and no backfill, so every historical row stays
-- NULL — the truthful reading, because those runs never recorded
-- a per-book pool in history. The writer likewise stamps NULL for
-- a book whose pool it cannot attribute (parallel jobs share the
-- budget counters across books, and a book skipped before its pool
-- was armed never measured one); a guessed or floor value would
-- read as a measurement that never happened.
-- =====================================================================

BEGIN;

ALTER TABLE processor.processor_run_books
    ADD COLUMN decision_pool int;

COMMIT;

-- =====================================================================
-- VERIFICATION (run after apply, before the writer ships):
--   SELECT column_name, data_type, is_nullable
--     FROM information_schema.columns
--    WHERE table_schema = 'processor'
--      AND table_name = 'processor_run_books'
--      AND column_name = 'decision_pool';
--   Expect exactly one row: decision_pool / integer / YES.
--   Verified 2026-10-18, before any code landed: the query
--   returned exactly that one row.
--
-- ROLLBACK (drops every recorded per-book pool with the column —
-- a data decision, not an undo):
--   ALTER TABLE processor.processor_run_books
--       DROP COLUMN decision_pool;
-- =====================================================================
