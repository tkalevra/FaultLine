-- Migration 087: entity_taxonomy nesting (member_taxonomies) + demo animal/pets/family nesting
-- Date: 2026-06-14
-- Purpose: SCHEMA FOUNDATION for the hierarchy-ladder redesign (rung 4 — the backbone).
--          See DEV/DESIGN-hierarchy-ladder-and-growth.md §"Hierarchy (rung 4)".
--
-- WHAT
-- ----
-- A hierarchical taxonomy contains ONLY (a) an entity of an allowed type, or (b) a
-- reference to another (hierarchical) taxonomy. Membership therefore becomes:
--     member_entity_types (entities)  ∪  member_taxonomies (sub-group refs)
-- Today entity_taxonomies has only member_entity_types and is flat — `family` is
-- Person-only with is_hierarchical=false, so "my family" misses the pet. This adds the
-- dormant nesting column and seeds the demo nesting `family ⊃ pets ⊃ animal`.
--
-- COLUMN: member_taxonomies TEXT[] DEFAULT '{}'  on entity_taxonomies (public + every tenant).
--
-- DEMO NESTING (seed source = public, fanned out + ON CONFLICT DO NOTHING):
--   animal : member_entity_types={Animal},                       is_hierarchical=true
--   pets   : member_entity_types={Animal}, rel_types_defining_group={has_pet},
--            is_hierarchical=true, member_taxonomies={animal}
--   family : guarded UPDATE → member_taxonomies={pets}, is_hierarchical=true
--
-- public is the SEED SOURCE/TEMPLATE ONLY — never read at runtime (overlays union it).
-- Idempotent: ADD COLUMN IF NOT EXISTS, INSERT ... ON CONFLICT DO NOTHING, guarded UPDATE.

-- ── 1. public (the template / seed source) ─────────────────────────────────
ALTER TABLE public.entity_taxonomies
    ADD COLUMN IF NOT EXISTS member_taxonomies TEXT[] DEFAULT '{}';

-- animal: the leaf classification group (a hierarchical group of Animal entities)
INSERT INTO public.entity_taxonomies
    (taxonomy_name, description, member_entity_types, rel_types_defining_group,
     has_transitivity, transitive_rel_types, is_hierarchical, member_taxonomies, source)
VALUES (
    'animal',
    'Animals — the classification leaf group (animal→…→species, filled reactively)',
    ARRAY['Animal'],
    ARRAY[]::TEXT[],
    true,
    ARRAY['subclass_of', 'instance_of']::TEXT[],
    true,
    ARRAY[]::TEXT[],
    'seeded'
)
ON CONFLICT (taxonomy_name) DO NOTHING;

-- pets: the user's pets group; nests the animal classification group
INSERT INTO public.entity_taxonomies
    (taxonomy_name, description, member_entity_types, rel_types_defining_group,
     has_transitivity, transitive_rel_types, is_hierarchical, member_taxonomies, source)
VALUES (
    'pets',
    'A subject''s pets — Animal entities linked by has_pet; nests the animal group',
    ARRAY['Animal'],
    ARRAY['has_pet']::TEXT[],
    true,
    ARRAY['has_pet']::TEXT[],
    true,
    ARRAY['animal']::TEXT[],
    'seeded'
)
ON CONFLICT (taxonomy_name) DO NOTHING;

-- family: nest pets under it and make it hierarchical (guarded — only if family exists
-- and has not already been user-corrected to a different nesting).
UPDATE public.entity_taxonomies
   SET member_taxonomies = ARRAY['pets']::TEXT[],
       is_hierarchical   = true
 WHERE taxonomy_name = 'family'
   AND COALESCE(source, 'seeded') <> 'user_corrected'
   AND (member_taxonomies IS NULL OR member_taxonomies = '{}');

-- ── 2. Fan out to existing tenant schemas ───────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- column
        EXECUTE format(
            'ALTER TABLE %I.entity_taxonomies ADD COLUMN IF NOT EXISTS member_taxonomies TEXT[] DEFAULT ''{}''',
            _schema);

        -- seed animal + pets from public (explicit columns, ON CONFLICT DO NOTHING)
        EXECUTE format($seed$
            INSERT INTO %I.entity_taxonomies
                (taxonomy_name, description, member_entity_types, rel_types_defining_group,
                 has_transitivity, transitive_rel_types, is_hierarchical, member_taxonomies, source)
            SELECT taxonomy_name, description, member_entity_types, rel_types_defining_group,
                   has_transitivity, transitive_rel_types, is_hierarchical, member_taxonomies,
                   COALESCE(source, 'seeded')
            FROM public.entity_taxonomies
            WHERE taxonomy_name IN ('animal', 'pets')
            ON CONFLICT (taxonomy_name) DO NOTHING
        $seed$, _schema);

        -- guarded family nesting (do NOT clobber a user-corrected row)
        EXECUTE format($upd$
            UPDATE %I.entity_taxonomies
               SET member_taxonomies = ARRAY['pets']::TEXT[],
                   is_hierarchical   = true
             WHERE taxonomy_name = 'family'
               AND COALESCE(source, 'seeded') <> 'user_corrected'
               AND (member_taxonomies IS NULL OR member_taxonomies = '{}')
        $upd$, _schema);

        RAISE NOTICE 'Migration 087: nested taxonomies seeded into %', _schema;
    END LOOP;
END $$;
