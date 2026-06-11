-- Migration 074: Fix ontology_evaluations constraint mismatch (dBug-074)
-- Date: 2026-06-09
-- Purpose: Drop old 4-column UNIQUE constraint that includes user_id,
--          ensure correct 3-column constraint exists, drop redundant user_id column.
--
-- Root cause: migration 018 defined UNIQUE(user_id, candidate_rel_type, sample_subject_id, sample_object)
-- but per-user schemas do not use user_id (per-schema scoping makes it redundant).
-- Code uses ON CONFLICT (candidate_rel_type, sample_subject_id, sample_object).
-- Migration 059 tried to add the 3-column constraint but never dropped the old 4-column one.
-- When only the 4-column constraint exists, PostgreSQL raises:
--   "there is no unique or exclusion constraint matching the ON CONFLICT specification"
-- which poisons the entire ingest transaction.
--
-- This migration:
--   1. Drops any unique constraint referencing user_id on ontology_evaluations
--   2. Ensures the correct 3-column constraint (uq_ontology_eval_candidate) exists
--   3. Drops the user_id column if present (redundant in per-user schemas)
--
-- Idempotent: safe to run multiple times.

-- ============================================================================
-- Part 1: Public schema fix
-- ============================================================================

DO $$
DECLARE
    _con RECORD;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'ontology_evaluations'
    ) THEN
        -- Step 1: Drop any unique constraint that includes user_id
        FOR _con IN
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_namespace nsp ON nsp.oid = con.connamespace
            JOIN pg_class cls ON cls.oid = con.conrelid
            WHERE nsp.nspname = 'public'
              AND cls.relname = 'ontology_evaluations'
              AND con.contype = 'u'
              AND EXISTS (
                  SELECT 1
                  FROM unnest(con.conkey) AS col_num
                  JOIN pg_attribute att ON att.attrelid = cls.oid AND att.attnum = col_num
                  WHERE att.attname = 'user_id'
              )
        LOOP
            EXECUTE format(
                'ALTER TABLE public.ontology_evaluations DROP CONSTRAINT %I',
                _con.conname
            );
            RAISE NOTICE '074: Dropped old constraint % from public.ontology_evaluations', _con.conname;
        END LOOP;

        -- Step 2: Add correct 3-column constraint (idempotent)
        BEGIN
            ALTER TABLE public.ontology_evaluations
                ADD CONSTRAINT uq_ontology_eval_candidate
                UNIQUE (candidate_rel_type, sample_subject_id, sample_object);
            RAISE NOTICE '074: Added 3-column constraint to public.ontology_evaluations';
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE '074: public.ontology_evaluations constraint: % (likely already exists)', SQLERRM;
        END;

        -- Note: Do NOT drop user_id from public schema — it is used there.
    END IF;
END $$;

-- ============================================================================
-- Part 2: Per-user schema fixes (loop over ALL faultline_% schemas)
-- ============================================================================

DO $$
DECLARE
    _schema TEXT;
    _con    RECORD;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- Skip schemas that lack the ontology_evaluations table entirely
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'ontology_evaluations'
        ) THEN
            CONTINUE;
        END IF;

        -- ----------------------------------------------------------------
        -- Step 1: Drop any unique constraint that includes user_id
        -- ----------------------------------------------------------------
        FOR _con IN
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_namespace nsp ON nsp.oid = con.connamespace
            JOIN pg_class cls ON cls.oid = con.conrelid
            WHERE nsp.nspname = _schema
              AND cls.relname = 'ontology_evaluations'
              AND con.contype = 'u'
              AND EXISTS (
                  SELECT 1
                  FROM unnest(con.conkey) AS col_num
                  JOIN pg_attribute att ON att.attrelid = cls.oid AND att.attnum = col_num
                  WHERE att.attname = 'user_id'
              )
        LOOP
            EXECUTE format(
                'ALTER TABLE %I.ontology_evaluations DROP CONSTRAINT %I',
                _schema, _con.conname
            );
            RAISE NOTICE '074: Dropped old constraint % from %.ontology_evaluations', _con.conname, _schema;
        END LOOP;

        -- ----------------------------------------------------------------
        -- Step 2: Add correct 3-column constraint (idempotent)
        -- ----------------------------------------------------------------
        BEGIN
            EXECUTE format(
                'ALTER TABLE %I.ontology_evaluations ADD CONSTRAINT uq_ontology_eval_candidate UNIQUE (candidate_rel_type, sample_subject_id, sample_object)',
                _schema
            );
            RAISE NOTICE '074: Added 3-column constraint to %.ontology_evaluations', _schema;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE '074: %.ontology_evaluations constraint: % (likely already exists)', _schema, SQLERRM;
        END;

        -- ----------------------------------------------------------------
        -- Step 3: Drop user_id column if present (redundant in per-user schemas)
        -- ----------------------------------------------------------------
        BEGIN
            EXECUTE format(
                'ALTER TABLE %I.ontology_evaluations DROP COLUMN IF EXISTS user_id',
                _schema
            );
            RAISE NOTICE '074: Dropped user_id column from %.ontology_evaluations (if existed)', _schema;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE '074: user_id column drop on %.ontology_evaluations: %', _schema, SQLERRM;
        END;

    END LOOP;
END $$;
