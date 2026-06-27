-- Migration 082: Revert verbose intent_classes descriptions to concise zero-shot labels
-- Date: 2026-06-12
--
-- WHY THIS IS NEEDED
-- ------------------
-- /classify-intent feeds GLiNER2 classify_text 4 intent labels built from intent_classes.
-- The descriptions had drifted back to VERBOSE, example-laden form (Pitfall 11 violation):
--   STATEMENT/CORRECTION carried embedded example phrases and negation enumerations.
-- A benchmark on the real model fastino/gliner2-base-v1 (39 cases) measured:
--   verbose labels  = 48.7% accuracy, 8-of-10 editor-usage statements mis-scored CORRECTION
--                     (live bug: "My favorite editor is vim" -> CORRECTION -> never ingests)
--   concise labels  = 87.2% accuracy, 0-of-10 false-CORRECTION, 20-of-20 STATEMENT
-- This migration reverts to the concise "D1_HYBRID" label set.
--
-- The runtime seed (src/api/main.py _check_and_seed + _build_intent_descriptions_for_gliner2)
-- uses INSERT ... ON CONFLICT DO NOTHING, so existing rows in public.intent_classes AND in
-- every per-tenant faultline_* schema already hold the verbose text and will NOT self-repair.
-- This migration UPDATEs them in place.
--
-- GUARD: genuine user customizations (refined_by = 'user') are left untouched.
-- IDEMPOTENT: re-running is a no-op once descriptions are already concise (no WHERE-match).
-- Mirrors the guard/version-bump style of migration 064 and the tenant-loop idiom of
-- migration 079. No DROP, no destructive SQL.

-- ============================================================================
-- 1. public.intent_classes (seed source / template)
-- ============================================================================

UPDATE public.intent_classes
SET description = 'asking a question',
    version     = version + 1,
    refined_at  = NOW(),
    updated_at  = NOW(),
    refined_by  = 'bootstrap'
WHERE intent_name = 'QUERY'
  AND refined_by <> 'user'
  AND description IS DISTINCT FROM 'asking a question';

UPDATE public.intent_classes
SET description = 'stating a fact, preference, or habit',
    version     = version + 1,
    refined_at  = NOW(),
    updated_at  = NOW(),
    refined_by  = 'bootstrap'
WHERE intent_name = 'STATEMENT'
  AND refined_by <> 'user'
  AND description IS DISTINCT FROM 'stating a fact, preference, or habit';

UPDATE public.intent_classes
SET description = 'deleting or forgetting information',
    version     = version + 1,
    refined_at  = NOW(),
    updated_at  = NOW(),
    refined_by  = 'bootstrap'
WHERE intent_name = 'RETRACTION'
  AND refined_by <> 'user'
  AND description IS DISTINCT FROM 'deleting or forgetting information';

UPDATE public.intent_classes
SET description = 'replacing a previously stated value with a different one',
    version     = version + 1,
    refined_at  = NOW(),
    updated_at  = NOW(),
    refined_by  = 'bootstrap'
WHERE intent_name = 'CORRECTION'
  AND refined_by <> 'user'
  AND description IS DISTINCT FROM 'replacing a previously stated value with a different one';

-- ============================================================================
-- 2. Every per-tenant faultline_% schema (runtime-read, NOT public)
-- ============================================================================

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- Skip schemas that don't have the table (defensive; older tenants).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'intent_classes'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format($u$
            UPDATE %I.intent_classes
            SET description = 'asking a question',
                version     = version + 1,
                refined_at  = NOW(),
                updated_at  = NOW(),
                refined_by  = 'bootstrap'
            WHERE intent_name = 'QUERY'
              AND refined_by <> 'user'
              AND description IS DISTINCT FROM 'asking a question'
        $u$, _schema);

        EXECUTE format($u$
            UPDATE %I.intent_classes
            SET description = 'stating a fact, preference, or habit',
                version     = version + 1,
                refined_at  = NOW(),
                updated_at  = NOW(),
                refined_by  = 'bootstrap'
            WHERE intent_name = 'STATEMENT'
              AND refined_by <> 'user'
              AND description IS DISTINCT FROM 'stating a fact, preference, or habit'
        $u$, _schema);

        EXECUTE format($u$
            UPDATE %I.intent_classes
            SET description = 'deleting or forgetting information',
                version     = version + 1,
                refined_at  = NOW(),
                updated_at  = NOW(),
                refined_by  = 'bootstrap'
            WHERE intent_name = 'RETRACTION'
              AND refined_by <> 'user'
              AND description IS DISTINCT FROM 'deleting or forgetting information'
        $u$, _schema);

        EXECUTE format($u$
            UPDATE %I.intent_classes
            SET description = 'replacing a previously stated value with a different one',
                version     = version + 1,
                refined_at  = NOW(),
                updated_at  = NOW(),
                refined_by  = 'bootstrap'
            WHERE intent_name = 'CORRECTION'
              AND refined_by <> 'user'
              AND description IS DISTINCT FROM 'replacing a previously stated value with a different one'
        $u$, _schema);

        RAISE NOTICE 'Migration 082: reverted intent_classes descriptions in %', _schema;
    END LOOP;
END $$;
