-- Migration 089: wire the dormant hierarchy flags on rel_types
-- Date: 2026-06-14
-- Purpose: SCHEMA FOUNDATION for the hierarchy ladder (rung 4 + transitive trace-back).
--          See DEV/DESIGN-hierarchy-ladder-and-growth.md §"Hierarchy (rung 4)" / §"Build order #1".
--
-- WHAT
-- ----
-- The hierarchy rel_types already exist but their structural flags were left unset, so
-- traversal never walks them. This wires the CLOSED, CURATED hierarchy set:
--   hierarchy rels (is_hierarchy_rel=true): instance_of, is_a, member_of, part_of, subclass_of
--   transitive (has_transitivity=true + transitive_rel_types={self}): subclass_of, part_of, member_of
--   NON-transitive (left as-is): instance_of, is_a
-- ("my animals" walks DOWN transitive subclass_of to Rex; instance_of is a single hop.)
--
-- These are guarded UPDATEs against EXISTING curated rel_types — they touch nothing else and
-- never mint a rel. This is NOT keyword-promotion of novel rels (which stays disabled per the
-- design); it only flips the flags on the known closed hierarchy set.
--
-- public is the SEED SOURCE/TEMPLATE ONLY. Idempotent: pure UPDATEs, re-runnable.
-- NOTE: schema_manager bootstrap copies these columns from public on provisioning
--       (is_hierarchy_rel via the DO UPDATE set), so NEW tenants inherit the wired flags;
--       this migration also fans out to EXISTING tenants for parity.

-- ── 1. public (the template / seed source) ─────────────────────────────────
UPDATE public.rel_types
   SET is_hierarchy_rel = true
 WHERE rel_type IN ('instance_of', 'is_a', 'member_of', 'part_of', 'subclass_of');

UPDATE public.rel_types
   SET has_transitivity     = true,
       transitive_rel_types = ARRAY[rel_type]::TEXT[]
 WHERE rel_type IN ('subclass_of', 'part_of', 'member_of');

-- instance_of / is_a stay NON-transitive (instance ≠ subclass; rdf:type is not transitive).
UPDATE public.rel_types
   SET has_transitivity = false
 WHERE rel_type IN ('instance_of', 'is_a');

-- ── 2. Fan out to existing tenant schemas ───────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        EXECUTE format($upd$
            UPDATE %I.rel_types
               SET is_hierarchy_rel = true
             WHERE rel_type IN ('instance_of', 'is_a', 'member_of', 'part_of', 'subclass_of')
        $upd$, _schema);

        EXECUTE format($upd$
            UPDATE %I.rel_types
               SET has_transitivity     = true,
                   transitive_rel_types = ARRAY[rel_type]::TEXT[]
             WHERE rel_type IN ('subclass_of', 'part_of', 'member_of')
        $upd$, _schema);

        EXECUTE format($upd$
            UPDATE %I.rel_types
               SET has_transitivity = false
             WHERE rel_type IN ('instance_of', 'is_a')
        $upd$, _schema);

        RAISE NOTICE 'Migration 089: hierarchy flags wired in %', _schema;
    END LOOP;
END $$;
