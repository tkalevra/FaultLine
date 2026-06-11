-- Migration 073: Provenance cleanup — enforce canonical three-value model
-- Date: 2026-06-09
-- Purpose: Backfill stale fact_provenance values, add NOT NULL + CHECK constraints,
--          drop dead SQL functions, add rel_types.source CHECK in per-user schemas.
--
-- Canonical provenance values: 'user_stated', 'llm_inferred', 'llm_learned'
-- Application code committed in 9be7145; this migration aligns the database.
--
-- Idempotent: safe to run multiple times.

-- ============================================================================
-- Part 1: Public schema fixes
-- ============================================================================

-- 1a. Backfill stale fact_provenance values in public.facts
UPDATE public.facts
SET fact_provenance = CASE
    WHEN fact_provenance = 'user_correction' THEN 'user_stated'
    WHEN fact_provenance IN ('openwebui', 'llm_promoted', 'gliner2', 'confirmed') THEN 'llm_inferred'
    WHEN fact_provenance IS NULL THEN 'llm_inferred'
    WHEN fact_provenance NOT IN ('user_stated', 'llm_inferred', 'llm_learned') THEN 'llm_inferred'
    ELSE fact_provenance
END
WHERE fact_provenance IS NULL
   OR fact_provenance NOT IN ('user_stated', 'llm_inferred', 'llm_learned');

-- 1b. Backfill stale fact_provenance values in public.staged_facts (if table exists)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'staged_facts'
    ) THEN
        UPDATE public.staged_facts
        SET fact_provenance = CASE
            WHEN fact_provenance = 'user_correction' THEN 'user_stated'
            WHEN fact_provenance IN ('openwebui', 'llm_promoted', 'gliner2', 'confirmed') THEN 'llm_inferred'
            WHEN fact_provenance IS NULL THEN 'llm_inferred'
            WHEN fact_provenance NOT IN ('user_stated', 'llm_inferred', 'llm_learned') THEN 'llm_inferred'
            ELSE fact_provenance
        END
        WHERE fact_provenance IS NULL
           OR fact_provenance NOT IN ('user_stated', 'llm_inferred', 'llm_learned');
    END IF;
END $$;

-- 1c. Change public.facts.fact_provenance DEFAULT from 'user_stated' to 'llm_inferred'
ALTER TABLE public.facts ALTER COLUMN fact_provenance SET DEFAULT 'llm_inferred';

-- 1d. Set NOT NULL on public.facts.fact_provenance (NULLs already backfilled above)
ALTER TABLE public.facts ALTER COLUMN fact_provenance SET NOT NULL;

-- 1e. Add CHECK constraint on public.facts.fact_provenance (drop first for idempotency)
ALTER TABLE public.facts DROP CONSTRAINT IF EXISTS chk_facts_fact_provenance;
ALTER TABLE public.facts ADD CONSTRAINT chk_facts_fact_provenance
    CHECK (fact_provenance IN ('user_stated', 'llm_inferred', 'llm_learned'));

-- 1f. Same default/NOT NULL/CHECK on public.staged_facts.fact_provenance (if table exists)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'staged_facts'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'staged_facts'
          AND column_name = 'fact_provenance'
    ) THEN
        ALTER TABLE public.staged_facts ALTER COLUMN fact_provenance SET DEFAULT 'llm_inferred';
        ALTER TABLE public.staged_facts ALTER COLUMN fact_provenance SET NOT NULL;

        ALTER TABLE public.staged_facts DROP CONSTRAINT IF EXISTS chk_staged_facts_fact_provenance;
        ALTER TABLE public.staged_facts ADD CONSTRAINT chk_staged_facts_fact_provenance
            CHECK (fact_provenance IN ('user_stated', 'llm_inferred', 'llm_learned'));
    END IF;
END $$;

-- 1g. Backfill non-conforming rel_types.source values in public schema
-- Code previously used fine-grained values (gliner2_discovery, llm_evaluated, etc.)
-- that the CHECK constraint doesn't allow. All engine-derived values → 'engine'.
UPDATE public.rel_types
SET source = 'engine'
WHERE source IS NOT NULL
  AND source NOT IN ('wikidata', 'builtin', 'engine', 'user', 'expand');

-- 1h. Drop dead SQL functions (zero call sites in Python; re_embedder does direct SQL)
DROP FUNCTION IF EXISTS public.promote_staged_fact(BIGINT);
DROP FUNCTION IF EXISTS public.record_confirmation(BIGINT, TEXT, TEXT);

-- ============================================================================
-- Part 2: Per-user schema fixes (loop over faultline_* schemas)
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
        -- ----------------------------------------------------------------
        -- 2a. Backfill facts.fact_provenance
        -- ----------------------------------------------------------------
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = _schema AND table_name = 'facts'
              AND column_name = 'fact_provenance'
        ) THEN
            EXECUTE format(
                'UPDATE %I.facts
                 SET fact_provenance = CASE
                     WHEN fact_provenance = ''user_correction'' THEN ''user_stated''
                     WHEN fact_provenance IN (''openwebui'', ''llm_promoted'', ''gliner2'', ''confirmed'') THEN ''llm_inferred''
                     WHEN fact_provenance IS NULL THEN ''llm_inferred''
                     WHEN fact_provenance NOT IN (''user_stated'', ''llm_inferred'', ''llm_learned'') THEN ''llm_inferred''
                     ELSE fact_provenance
                 END
                 WHERE fact_provenance IS NULL
                    OR fact_provenance NOT IN (''user_stated'', ''llm_inferred'', ''llm_learned'')',
                _schema
            );

            -- Default + NOT NULL + CHECK
            EXECUTE format('ALTER TABLE %I.facts ALTER COLUMN fact_provenance SET DEFAULT ''llm_inferred''', _schema);
            EXECUTE format('ALTER TABLE %I.facts ALTER COLUMN fact_provenance SET NOT NULL', _schema);
            EXECUTE format('ALTER TABLE %I.facts DROP CONSTRAINT IF EXISTS chk_facts_fact_provenance', _schema);
            EXECUTE format(
                'ALTER TABLE %I.facts ADD CONSTRAINT chk_facts_fact_provenance
                     CHECK (fact_provenance IN (''user_stated'', ''llm_inferred'', ''llm_learned''))',
                _schema
            );
        END IF;

        -- ----------------------------------------------------------------
        -- 2b. Backfill staged_facts.fact_provenance
        -- ----------------------------------------------------------------
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = _schema AND table_name = 'staged_facts'
              AND column_name = 'fact_provenance'
        ) THEN
            EXECUTE format(
                'UPDATE %I.staged_facts
                 SET fact_provenance = CASE
                     WHEN fact_provenance = ''user_correction'' THEN ''user_stated''
                     WHEN fact_provenance IN (''openwebui'', ''llm_promoted'', ''gliner2'', ''confirmed'') THEN ''llm_inferred''
                     WHEN fact_provenance IS NULL THEN ''llm_inferred''
                     WHEN fact_provenance NOT IN (''user_stated'', ''llm_inferred'', ''llm_learned'') THEN ''llm_inferred''
                     ELSE fact_provenance
                 END
                 WHERE fact_provenance IS NULL
                    OR fact_provenance NOT IN (''user_stated'', ''llm_inferred'', ''llm_learned'')',
                _schema
            );

            -- Default + NOT NULL + CHECK
            EXECUTE format('ALTER TABLE %I.staged_facts ALTER COLUMN fact_provenance SET DEFAULT ''llm_inferred''', _schema);
            EXECUTE format('ALTER TABLE %I.staged_facts ALTER COLUMN fact_provenance SET NOT NULL', _schema);
            EXECUTE format('ALTER TABLE %I.staged_facts DROP CONSTRAINT IF EXISTS chk_staged_facts_fact_provenance', _schema);
            EXECUTE format(
                'ALTER TABLE %I.staged_facts ADD CONSTRAINT chk_staged_facts_fact_provenance
                     CHECK (fact_provenance IN (''user_stated'', ''llm_inferred'', ''llm_learned''))',
                _schema
            );
        END IF;

        -- ----------------------------------------------------------------
        -- 2c. Drop dead functions in per-user schemas
        -- ----------------------------------------------------------------
        EXECUTE format('DROP FUNCTION IF EXISTS %I.promote_staged_fact(BIGINT)', _schema);
        EXECUTE format('DROP FUNCTION IF EXISTS %I.record_confirmation(BIGINT, TEXT, TEXT)', _schema);

        -- ----------------------------------------------------------------
        -- 2d. Backfill + CHECK constraint on rel_types.source (if rel_types table exists)
        -- ----------------------------------------------------------------
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'rel_types'
        ) THEN
            -- Backfill non-conforming source values before adding constraint
            EXECUTE format(
                'UPDATE %I.rel_types SET source = ''engine''
                 WHERE source IS NOT NULL
                   AND source NOT IN (''wikidata'', ''builtin'', ''engine'', ''user'', ''expand'')',
                _schema
            );

            EXECUTE format('ALTER TABLE %I.rel_types DROP CONSTRAINT IF EXISTS rel_types_source_check', _schema);
            EXECUTE format(
                'ALTER TABLE %I.rel_types ADD CONSTRAINT rel_types_source_check
                     CHECK (source = ANY (ARRAY[''wikidata'', ''builtin'', ''engine'', ''user'', ''expand'']))',
                _schema
            );
        END IF;
    END LOOP;
END $$;
