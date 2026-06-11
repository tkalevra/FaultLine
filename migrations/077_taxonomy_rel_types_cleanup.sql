-- Migration 077: Remove hierarchy rel_types from entity_taxonomies.rel_types_defining_group
-- Date: 2026-06-10
-- Purpose: instance_of, subclass_of, and related hierarchy rels (is_hierarchy_rel=true) are
--          classification rel_types that describe what an entity IS, not what it DOES.
--          They must never appear in rel_types_defining_group because:
--          1. determine_path() uses rel_types_defining_group as an exclusive gate
--          2. instance_of matches nearly every typed entity
--          3. Any taxonomy containing instance_of intercepts nearly all entity queries
--             and silently drops facts for entities whose type doesn't match member_entity_types
--
-- Second contamination cleaned: /expand computer_system fan-out leaked infrastructure rel_types
-- (monitors, runs, hosts, manages, etc.) into semantic-domain taxonomies (family, household,
-- location, work, body_parts) that share member_entity_types by coincidence.
--
-- Idempotent: array_remove is a no-op when element is absent. Safe to run repeatedly.
-- Applies to public schema AND all faultline_% per-user schemas (076 pattern).

-- ============================================================================
-- Part 1: Public schema
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'entity_taxonomies'
    ) THEN
        -- Remove instance_of and subclass_of universally (never valid in defining_group)
        UPDATE public.entity_taxonomies
        SET rel_types_defining_group = array_remove(
            array_remove(rel_types_defining_group, 'instance_of'),
            'subclass_of'
        )
        WHERE rel_types_defining_group && ARRAY['instance_of', 'subclass_of']::TEXT[];

        -- Remove infrastructure rel_types that leaked from /expand computer_system fan-out
        -- into semantic domain taxonomies where they don't belong
        UPDATE public.entity_taxonomies
        SET rel_types_defining_group = (
            SELECT ARRAY(
                SELECT unnest(rel_types_defining_group)
                EXCEPT SELECT unnest(ARRAY[
                    'monitors','runs','hosts','manages','provides','protects',
                    'contains','backs_up','stores','connects_to','builds','defines',
                    'links','instantiates','depends_on','listens_on'
                ]::TEXT[])
            )
        )
        WHERE taxonomy_name IN ('family', 'household', 'location', 'work', 'body_parts');

        RAISE NOTICE '077: public.entity_taxonomies hierarchy contamination cleaned';
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

        -- Remove instance_of and subclass_of (never valid in defining_group)
        EXECUTE format(
            'UPDATE %I.entity_taxonomies '
            'SET rel_types_defining_group = array_remove('
            '    array_remove(rel_types_defining_group, ''instance_of''), '
            '    ''subclass_of'') '
            'WHERE rel_types_defining_group && ARRAY[''instance_of'', ''subclass_of'']::TEXT[]',
            _schema
        );

        -- Remove infrastructure rel_types leaked from /expand fan-out
        EXECUTE format(
            'UPDATE %I.entity_taxonomies '
            'SET rel_types_defining_group = ('
            '    SELECT ARRAY('
            '        SELECT unnest(rel_types_defining_group)'
            '        EXCEPT SELECT unnest(ARRAY['
            '            ''monitors'',''runs'',''hosts'',''manages'',''provides'',''protects'','
            '            ''contains'',''backs_up'',''stores'',''connects_to'',''builds'',''defines'','
            '            ''links'',''instantiates'',''depends_on'',''listens_on'''
            '        ]::TEXT[])'
            '    )'
            ') '
            'WHERE taxonomy_name IN (''family'', ''household'', ''location'', ''work'', ''body_parts'')',
            _schema
        );

        RAISE NOTICE '077: %.entity_taxonomies hierarchy contamination cleaned', _schema;
    END LOOP;
END $$;
