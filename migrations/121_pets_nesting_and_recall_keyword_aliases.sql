-- Migration 121: restore pets-group nesting + recall keyword aliases (scoped-walk fixes)
-- Date: 2026-06-29
--
-- WHY
-- ---
-- Live evidence on a realistic multi-subject tenant:
--   • "tell me about my family" omitted the nested pets, and "what about my pets" was hollow.
--   • "where do I live" and "how have I been feeling" resolved NO scope (fetch-all dump / empty).
--
-- ROOT CAUSE (metadata, two defects):
--   Defect A — the `pets` group lost its connecting rel. Migration 087 seeded
--     pets.rel_types_defining_group = {has_pet}, but the defining-group eligibility trigger
--     (enforce_defining_group_eligibility) STRIPS a rel whose concrete head/tail types are not
--     ALL members of the taxonomy. `has_pet` is Person->Animal but pets.member_entity_types was
--     {Animal} only, so the Person head failed the cross-type guard and has_pet was dropped ->
--     pets.defining = {} (hollow). With no defining rel, family -> pets nesting could not descend
--     and "my pets" had no scope. FIX: pets is the OWNER+PET group, so its member types are
--     {Person, Animal} (mirrors `household`). With Person a member, has_pet (Person->Animal)
--     passes the guard and survives as the defining rel — the nesting connecting edge is restored.
--
--   Defect B — recall keyword -> rel resolution had no entry for the residence/affect verbs.
--     determine_path resolves a query keyword to a rel by EXACT rel_type name OR a
--     rel_type_aliases row. "where do I LIVE" / "how have I been FEELING" produced no rel and so
--     no scope (autobiographical fetch-all). Residence rels are (by migration 119) intentionally
--     NOT taxonomy-defining (cross-type Person->Location), so the clean, deterministic hook is a
--     keyword alias straight to the rel. FIX: seed natural-language keyword aliases
--     live/lives -> lives_in and feel/feeling -> feels (same mechanism as boss->works_for,
--     birthday->born_on). The scoped walk then projects to the residence / affect rel only.
--
-- SUBJECT-AGNOSTIC, METADATA-DRIVEN: no place/concept literals in code; pure rel/type metadata.
-- Idempotent: guarded UPDATE + INSERT ... ON CONFLICT DO NOTHING. public is the SEED SOURCE;
-- the trigger re-validates on every write (so has_pet is only kept because Person is now a member).

-- ── 1. public (the template / seed source) ─────────────────────────────────
-- pets: owner+pet group so the Person->Animal connecting rel survives the eligibility trigger.
UPDATE public.entity_taxonomies
   SET member_entity_types     = ARRAY['Person','Animal']::TEXT[],
       rel_types_defining_group = ARRAY['has_pet']::TEXT[]
 WHERE taxonomy_name = 'pets'
   AND COALESCE(source, 'seeded') <> 'user_corrected';

-- recall keyword aliases (residence + affect). FK: canonical_rel_type must exist in rel_types.
INSERT INTO public.rel_type_aliases (canonical_rel_type, alias, source) VALUES
    ('lives_in', 'live',    'ontology'),
    ('lives_in', 'lives',   'ontology'),
    ('feels',    'feel',    'ontology'),
    ('feels',    'feeling', 'ontology')
ON CONFLICT (alias) DO NOTHING;

-- ── 2. Fan out to existing tenant schemas ───────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- pets group member types + defining rel (guarded; trigger re-validates has_pet)
        EXECUTE format($upd$
            UPDATE %I.entity_taxonomies
               SET member_entity_types      = ARRAY['Person','Animal']::TEXT[],
                   rel_types_defining_group  = ARRAY['has_pet']::TEXT[]
             WHERE taxonomy_name = 'pets'
               AND COALESCE(source, 'seeded') <> 'user_corrected'
        $upd$, _schema);

        -- recall keyword aliases (ON CONFLICT on the per-tenant alias unique constraint)
        EXECUTE format($al$
            INSERT INTO %I.rel_type_aliases (canonical_rel_type, alias, source)
            SELECT canonical_rel_type, alias, source
            FROM public.rel_type_aliases
            WHERE alias IN ('live','lives','feel','feeling')
            ON CONFLICT (alias) DO NOTHING
        $al$, _schema);

        RAISE NOTICE 'Migration 121: pets nesting + recall aliases applied to %', _schema;
    END LOOP;
END $$;
