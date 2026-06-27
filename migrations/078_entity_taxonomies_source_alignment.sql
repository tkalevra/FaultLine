-- Migration 078: entity_taxonomies.source alignment (Phase 2 / 2C)
-- Date: 2026-06-11
-- Purpose: Align the per-tenant entity_taxonomies DDL with migration 019 (public),
--          which has a `source` column. The tenant template
--          (src/provisioning/templates/user_schema.sql) historically omitted it,
--          so existing tenant schemas lack `source`. The per-tenant taxonomy
--          overlay (Phase 2 scope reads) and Phase 3 scope corrections need it to
--          distinguish a user-corrected scope row ('user_corrected') from a seeded
--          one ('seeded'). 019 also relies on ON CONFLICT (taxonomy_name) — already
--          present in the tenant template's UNIQUE constraint.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Applies to public AND all faultline_%
-- per-user schemas (076/077 loop pattern). Safe to run repeatedly.

-- ============================================================================
-- Part 1: Public schema (no-op if 019 already created the column)
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'entity_taxonomies'
    ) THEN
        ALTER TABLE public.entity_taxonomies
            ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'seeded';
        RAISE NOTICE '078: public.entity_taxonomies.source ensured';
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
        -- Skip schemas that lack the entity_taxonomies table entirely
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'entity_taxonomies'
        ) THEN
            CONTINUE;
        END IF;

        -- Additive column (idempotent). Default 'seeded' so pre-existing rows —
        -- which were copied from the public seed at provisioning time — read as
        -- seeded, not user-corrected.
        EXECUTE format(
            'ALTER TABLE %I.entity_taxonomies '
            'ADD COLUMN IF NOT EXISTS source TEXT DEFAULT ''seeded''',
            _schema
        );

        RAISE NOTICE '078: %.entity_taxonomies.source ensured', _schema;
    END LOOP;
END $$;
