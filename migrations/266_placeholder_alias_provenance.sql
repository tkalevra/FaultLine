-- Migration 266: Placeholder alias provenance (identity-honesty / ALIAS-PROVENANCE-DESIGN)
-- Date: 2026-08-22
-- Purpose: Stamp the provisioning placeholder preferred alias 'user' as preference_source
--          ='provisioned' on every existing seat, mirroring the bootstrap fix in
--          src/provisioning/schema_manager.py (which now writes 'provisioned' directly).
--
-- Why: the placeholder was historically inserted WITHOUT preference_source, so it landed at
--      the column default 'unspecified' (rank 0). Every read-time guard that refuses to
--      surface a placeholder keys on preference_source = 'provisioned' — the query display
--      gate (_populate_preferred_names), the fallback SQL (preference_source != 'provisioned')
--      and the re_embedder's suspect_preferred_name flag — so an unstamped placeholder was
--      accepted by ALL of them and recall rendered the literal 'user' forever (measured on the
--      a live demo 2026-08-21: 'My name is Alexander' registered non-preferred every
--      time; corrections were idempotent no-ops).
--
-- Scope guard — this stamps ONLY the one known placeholder shape:
--      alias = 'user' AND is_preferred AND preference_source = 'unspecified'
-- A seat where the user has already stated a real name (any stronger source) is untouched;
--      a seat with NO such row has nothing to fix. Non-preferred 'user' aliases are left
--      alone (they are inert for display).
--
-- Idempotent: the UPDATE's WHERE clause is self-limiting. Safe to run repeatedly.
-- Applies to public schema AND all faultline_% per-user schemas (074/076 pattern).

-- ============================================================================
-- Part 1: Public schema (best-effort; the legacy public table may be absent)
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'entity_aliases'
    ) THEN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'entity_aliases'
              AND column_name = 'preference_source'
        ) THEN
            UPDATE public.entity_aliases
            SET preference_source = 'provisioned'
            WHERE alias = 'user' AND is_preferred = true
              AND preference_source = 'unspecified';
            RAISE NOTICE '266: public.entity_aliases placeholder rows stamped';
        END IF;
    END IF;
END $$;

-- ============================================================================
-- Part 2: Per-user schemas (loop over ALL faultline_% schemas, 076 pattern)
-- ============================================================================
DO $$
DECLARE
    _schema TEXT;
    _stamped INT;
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

        -- Skip schemas without the preference_source column (pre-076, nothing to stamp)
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = _schema AND table_name = 'entity_aliases'
              AND column_name = 'preference_source'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'UPDATE %I.entity_aliases '
            'SET preference_source = ''provisioned'' '
            'WHERE alias = ''user'' AND is_preferred = true '
            '  AND preference_source = ''unspecified''',
            _schema
        );
        GET DIAGNOSTICS _stamped = ROW_COUNT;
        IF _stamped > 0 THEN
            RAISE NOTICE '266: %.entity_aliases placeholder stamped (%)', _schema, _stamped;
        END IF;
    END LOOP;
END $$;
