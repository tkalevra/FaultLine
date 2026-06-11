-- Migration 075: Fix type constraints for owns and other rel_types (dBug-075)
-- Date: 2026-06-09
-- Purpose: Unconditionally set correct head_types/tail_types for rel_types that
--          migration 030 may have missed due to conditional WHERE clauses.
--          Also propagates fixes to all per-user schemas.
--
-- Root cause: Migration 030 used conditional WHERE (head_types IS NULL OR head_types = '{}')
-- which was a no-op if head_types was already set to ANY or some other value.
-- Per-user schemas provisioned before 030 retained stale NULL/ANY constraints via
-- schema_manager.py's ON CONFLICT DO NOTHING.
--
-- Idempotent: safe to run multiple times. Unconditional UPDATEs always set correct values.

-- ============================================================================
-- Part 1: Public schema — unconditional type constraint fixes
-- ============================================================================

-- owns: only Person/Organization can own things; owned things are Animal/Object/Organization
UPDATE public.rel_types SET
    head_types = ARRAY['Person', 'Organization']::TEXT[],
    tail_types = ARRAY['Animal', 'Object', 'Organization']::TEXT[]
WHERE rel_type = 'owns';

-- has_pet: only Person can have pets; pets are Animal
UPDATE public.rel_types SET
    head_types = ARRAY['Person']::TEXT[],
    tail_types = ARRAY['Animal']::TEXT[]
WHERE rel_type = 'has_pet';

-- works_for: Person works for Person or Organization
UPDATE public.rel_types SET
    head_types = ARRAY['Person']::TEXT[],
    tail_types = ARRAY['Person', 'Organization']::TEXT[]
WHERE rel_type = 'works_for';

-- educated_at: Person educated at Organization
UPDATE public.rel_types SET
    head_types = ARRAY['Person']::TEXT[],
    tail_types = ARRAY['Organization']::TEXT[]
WHERE rel_type = 'educated_at';

-- lives_in: Person lives in Location
UPDATE public.rel_types SET
    head_types = ARRAY['Person']::TEXT[],
    tail_types = ARRAY['Location']::TEXT[]
WHERE rel_type = 'lives_in';

-- lives_at: Person lives at Location or SCALAR (address string)
UPDATE public.rel_types SET
    head_types = ARRAY['Person']::TEXT[],
    tail_types = ARRAY['Location', 'SCALAR']::TEXT[]
WHERE rel_type = 'lives_at';

-- born_in: Person born in Location
UPDATE public.rel_types SET
    head_types = ARRAY['Person']::TEXT[],
    tail_types = ARRAY['Location']::TEXT[]
WHERE rel_type = 'born_in';

-- ============================================================================
-- Part 2: Per-user schemas — same unconditional fixes
-- ============================================================================

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- Only apply if rel_types table exists in this schema
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'rel_types'
        ) THEN
            -- owns
            EXECUTE format(
                'UPDATE %I.rel_types SET
                    head_types = ARRAY[''Person'', ''Organization'']::TEXT[],
                    tail_types = ARRAY[''Animal'', ''Object'', ''Organization'']::TEXT[]
                 WHERE rel_type = ''owns''',
                _schema
            );

            -- has_pet
            EXECUTE format(
                'UPDATE %I.rel_types SET
                    head_types = ARRAY[''Person'']::TEXT[],
                    tail_types = ARRAY[''Animal'']::TEXT[]
                 WHERE rel_type = ''has_pet''',
                _schema
            );

            -- works_for
            EXECUTE format(
                'UPDATE %I.rel_types SET
                    head_types = ARRAY[''Person'']::TEXT[],
                    tail_types = ARRAY[''Person'', ''Organization'']::TEXT[]
                 WHERE rel_type = ''works_for''',
                _schema
            );

            -- educated_at
            EXECUTE format(
                'UPDATE %I.rel_types SET
                    head_types = ARRAY[''Person'']::TEXT[],
                    tail_types = ARRAY[''Organization'']::TEXT[]
                 WHERE rel_type = ''educated_at''',
                _schema
            );

            -- lives_in
            EXECUTE format(
                'UPDATE %I.rel_types SET
                    head_types = ARRAY[''Person'']::TEXT[],
                    tail_types = ARRAY[''Location'']::TEXT[]
                 WHERE rel_type = ''lives_in''',
                _schema
            );

            -- lives_at
            EXECUTE format(
                'UPDATE %I.rel_types SET
                    head_types = ARRAY[''Person'']::TEXT[],
                    tail_types = ARRAY[''Location'', ''SCALAR'']::TEXT[]
                 WHERE rel_type = ''lives_at''',
                _schema
            );

            -- born_in
            EXECUTE format(
                'UPDATE %I.rel_types SET
                    head_types = ARRAY[''Person'']::TEXT[],
                    tail_types = ARRAY[''Location'']::TEXT[]
                 WHERE rel_type = ''born_in''',
                _schema
            );
        END IF;
    END LOOP;
END $$;
