-- Migration 076: Alias preference provenance (ALIAS-PROVENANCE-DESIGN)
-- Date: 2026-06-10
-- Purpose: Add `preference_source` to entity_aliases so consumers (ingest, merge,
--          re-embedder, query) can read WHY an alias is preferred instead of guessing.
--          `is_preferred` keeps its meaning ("the row to display"); preference_source
--          answers the orthogonal "how much do we trust that choice?" question.
--
-- Domain (intentionally NOT a CHECK constraint — kept open for growth):
--   'user_stated'   user explicitly chose it ("call me / goes by / prefers to be called")
--   'rel_default'   pref_name rel_type default, no explicit signal
--   'inferred'      same-batch / heuristic
--   'provisioned'   seeded placeholder (UUID slug etc.) — never display, never protect
--   'merge'         assigned structurally during a merge
--   'unspecified'   legacy rows written before this column existed
--
-- Backfill: rows whose alias matches the provisioning UUID-slug pattern are marked
-- 'provisioned'. Everything else stays 'unspecified' (do not guess).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + conditional backfill. Safe to run repeatedly.
-- Applies to public schema AND all faultline_% per-user schemas (074/075 pattern).

-- ============================================================================
-- Part 1: Public schema
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'entity_aliases'
    ) THEN
        -- Additive column, backward compatible
        ALTER TABLE public.entity_aliases
            ADD COLUMN IF NOT EXISTS preference_source TEXT NOT NULL DEFAULT 'unspecified';

        -- Backfill: UUID-slug aliases are provisioning placeholders.
        UPDATE public.entity_aliases
        SET preference_source = 'provisioned'
        WHERE preference_source = 'unspecified'
          AND alias ~ '^[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{12}$';

        RAISE NOTICE '076: public.entity_aliases.preference_source ensured + backfilled';
    END IF;
END $$;

-- ============================================================================
-- Part 2: Per-user schemas (loop over ALL faultline_% schemas)
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
        -- Skip schemas that lack the entity_aliases table entirely
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'entity_aliases'
        ) THEN
            CONTINUE;
        END IF;

        -- Additive column (idempotent)
        EXECUTE format(
            'ALTER TABLE %I.entity_aliases '
            'ADD COLUMN IF NOT EXISTS preference_source TEXT NOT NULL DEFAULT ''unspecified''',
            _schema
        );

        -- Backfill UUID-slug placeholders to 'provisioned'
        EXECUTE format(
            'UPDATE %I.entity_aliases '
            'SET preference_source = ''provisioned'' '
            'WHERE preference_source = ''unspecified'' '
            'AND alias ~ ''^[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{12}$''',
            _schema
        );

        RAISE NOTICE '076: %.entity_aliases.preference_source ensured + backfilled', _schema;
    END LOOP;
END $$;
