-- Migration 065: Backfill hit_count into all existing per-user staged_facts tables
--
-- Migration 063 added hit_count to the public staged_facts table but per-user schemas
-- (faultline_{user_slug}) are created from 051_template_user_schema.sql, which did not
-- include hit_count. This migration patches all existing per-user schemas so the
-- re-embedder's class_c_decay_hits() and promote_class_c_hits() jobs stop crashing.
--
-- 051 template has been updated to include hit_count for all newly provisioned schemas.
-- This migration handles the backfill for schemas that already exist.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS is safe to re-run.
-- Idempotent: CREATE INDEX IF NOT EXISTS is safe to re-run.

DO $$
DECLARE
    schema_rec RECORD;
BEGIN
    FOR schema_rec IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
          AND schema_name != 'faultline_test'
    LOOP
        -- Add hit_count if missing
        EXECUTE format(
            'ALTER TABLE %I.staged_facts ADD COLUMN IF NOT EXISTS hit_count INT NOT NULL DEFAULT 1',
            schema_rec.schema_name
        );

        -- Make rel_type nullable (migration 063 requirement for rough/unclassified Class C)
        EXECUTE format(
            $sql$
            DO $inner$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = %L
                      AND table_name = 'staged_facts'
                      AND column_name = 'rel_type'
                      AND is_nullable = 'NO'
                ) THEN
                    ALTER TABLE %I.staged_facts ALTER COLUMN rel_type DROP NOT NULL;
                END IF;
            END $inner$;
            $sql$,
            schema_rec.schema_name,
            schema_rec.schema_name
        );

        -- Add lifecycle index if missing
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_staged_facts_class_c_lifecycle
             ON %I.staged_facts (fact_class, expires_at, hit_count)
             WHERE fact_class = ''C'' AND promoted_at IS NULL',
            schema_rec.schema_name
        );

        RAISE NOTICE 'Patched schema: %', schema_rec.schema_name;
    END LOOP;
END $$;
