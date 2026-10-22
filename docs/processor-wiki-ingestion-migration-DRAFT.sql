-- =====================================================================
-- wiki ingestion — additive app-side migration (DRAFT 2026-10-22)
-- Drafted from docs/wiki-ingestion-spec.md §4.
--
-- POSTURE: Additive, re-runnable (IF NOT EXISTS), backfill-free.
-- TARGET: public schema on the CozyLibram Supabase database.
-- APPLY PATH: Admin SQL / cozylibram/supabase/migrations/ only on the
-- operator's explicit approval.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- §4.1 book_characters
-- ---------------------------------------------------------------------
ALTER TABLE public.book_characters
    ADD COLUMN IF NOT EXISTS name_norm               text,
    ADD COLUMN IF NOT EXISTS last_appearance_chapter integer,
    ADD COLUMN IF NOT EXISTS pipeline_key            text,
    ADD COLUMN IF NOT EXISTS source_run              text,
    ADD COLUMN IF NOT EXISTS analysis_version        text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_book_characters_pipeline_key
    ON public.book_characters (pipeline_key)
    WHERE pipeline_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_book_characters_work_name_norm
    ON public.book_characters (work_id, name_norm);

-- ---------------------------------------------------------------------
-- §4.2 character_relationships
-- ---------------------------------------------------------------------
ALTER TABLE public.character_relationships
    ADD COLUMN IF NOT EXISTS evidence          text,
    ADD COLUMN IF NOT EXISTS evidence_source   text,
    ADD COLUMN IF NOT EXISTS validation_state  text,
    ADD COLUMN IF NOT EXISTS claimed_type      text,
    ADD COLUMN IF NOT EXISTS confidence        numeric,
    ADD COLUMN IF NOT EXISTS pipeline_key      text,
    ADD COLUMN IF NOT EXISTS source_run        text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_character_relationships_pipeline_key
    ON public.character_relationships (pipeline_key)
    WHERE pipeline_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_character_relationships_review_status
    ON public.character_relationships (review_status);

-- ---------------------------------------------------------------------
-- §4.3 character_links and characters
-- ---------------------------------------------------------------------
ALTER TABLE public.character_links
    ADD COLUMN IF NOT EXISTS status      text NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('suggested', 'confirmed', 'rejected')),
    ADD COLUMN IF NOT EXISTS link_source text
        CHECK (link_source IS NULL OR link_source IN ('human', 'auto-series', 'pipeline')),
    ADD COLUMN IF NOT EXISTS confidence  numeric,
    ADD COLUMN IF NOT EXISTS evidence    text,
    ADD COLUMN IF NOT EXISTS source_run  text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_character_links_book_char_char
    ON public.character_links (book_character_id, character_id);

ALTER TABLE public.characters
    ADD COLUMN IF NOT EXISTS merged_into uuid REFERENCES public.characters(id);

CREATE INDEX IF NOT EXISTS idx_characters_name_norm
    ON public.characters (name_norm);

-- ---------------------------------------------------------------------
-- §4.4 works
-- ---------------------------------------------------------------------
ALTER TABLE public.works
    ADD COLUMN IF NOT EXISTS series_key      text,
    ADD COLUMN IF NOT EXISTS series_position numeric;

CREATE INDEX IF NOT EXISTS idx_works_series_key_position
    ON public.works (series_key, series_position);

-- ---------------------------------------------------------------------
-- §4.5 book_trope_claims, book_trigger_claims, book_quotes
-- ---------------------------------------------------------------------
ALTER TABLE public.book_trope_claims
    ADD COLUMN IF NOT EXISTS validation_state text,
    ADD COLUMN IF NOT EXISTS pipeline_key     text,
    ADD COLUMN IF NOT EXISTS source_run       text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_book_trope_claims_pipeline_key
    ON public.book_trope_claims (pipeline_key)
    WHERE pipeline_key IS NOT NULL;

ALTER TABLE public.book_trigger_claims
    ADD COLUMN IF NOT EXISTS validation_state text,
    ADD COLUMN IF NOT EXISTS pipeline_key     text,
    ADD COLUMN IF NOT EXISTS source_run       text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_book_trigger_claims_pipeline_key
    ON public.book_trigger_claims (pipeline_key)
    WHERE pipeline_key IS NOT NULL;

ALTER TABLE public.book_quotes
    ADD COLUMN IF NOT EXISTS chapter_index integer,
    ADD COLUMN IF NOT EXISTS chapter_label text,
    ADD COLUMN IF NOT EXISTS spoiler       boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS verified      boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pipeline_key  text,
    ADD COLUMN IF NOT EXISTS source_run    text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_book_quotes_pipeline_key
    ON public.book_quotes (pipeline_key)
    WHERE pipeline_key IS NOT NULL;

-- ---------------------------------------------------------------------
-- §4.6 book_events (Phase 4)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.book_events (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id                 uuid NOT NULL REFERENCES public.works(id) ON DELETE CASCADE,
    chapter_index           integer NOT NULL,
    event_index             integer NOT NULL,
    kind                    text NOT NULL
                            CHECK (kind IN ('plot', 'relationship', 'status', 'reveal')),
    summary                 text NOT NULL,
    participants            text[] NOT NULL DEFAULT '{}',
    evidence                text,
    spoiler_through_chapter integer,
    source_run              text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (work_id, chapter_index, event_index)
);

CREATE INDEX IF NOT EXISTS idx_book_events_work_chapter
    ON public.book_events (work_id, chapter_index);

ALTER TABLE public.book_events ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'book_events'
          AND policyname = 'book_events_authenticated_select'
    ) THEN
        CREATE POLICY book_events_authenticated_select
            ON public.book_events
            FOR SELECT
            TO authenticated
            USING (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'book_events'
          AND policyname = 'book_events_admin_write'
    ) THEN
        CREATE POLICY book_events_admin_write
            ON public.book_events
            FOR ALL
            TO authenticated
            USING (public.is_admin())
            WITH CHECK (public.is_admin());
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- §4.7 Security — harden character_relationships writes to is_admin()
-- (processor uses service_role and is unaffected)
-- ---------------------------------------------------------------------
DO $$
DECLARE
    pol record;
BEGIN
    FOR pol IN
        SELECT policyname
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'character_relationships'
          AND cmd IN ('INSERT', 'UPDATE', 'DELETE', 'ALL')
    LOOP
        EXECUTE format(
            'DROP POLICY IF EXISTS %I ON public.character_relationships',
            pol.policyname
        );
    END LOOP;

    CREATE POLICY character_relationships_admin_write
        ON public.character_relationships
        FOR ALL
        TO authenticated
        USING (public.is_admin())
        WITH CHECK (public.is_admin());
END
$$;

COMMIT;
