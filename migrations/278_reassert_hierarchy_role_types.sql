-- Migration 278: make the seeded HIERARCHY rels' head/tail types LIVE — the five
--                030_strengthen UPDATEs that were dead on every box — per tenant, UNION-widened
-- Date: 2026-09-16 (gauntlet l4-type-constraints-enforced, owner ruling 2026-09-16: 'yes')
--
-- WHY
-- ---
-- migrations/030_strengthen_rel_types_wikidata_ontology.sql carried five UPDATEs typing the
-- hierarchy rels (instance_of / subclass_of / part_of / is_a / member_of) behind the guard
-- `AND (is_hierarchy_rel IS NULL OR is_hierarchy_rel = false)`. 022_rel_types_metadata.sql —
-- eight files earlier — already sets is_hierarchy_rel = TRUE for exactly those five, so the guard
-- was unsatisfiable on every boot of every box since the file was written: measured on a
-- three-boot database (2026-09-16), all five carry head_types = tail_types = NULL in public and
-- in every tenant. The first-boot-migration-corpus lane DELETED the dead statements (an edited
-- early file re-runs once everywhere under the ledger) and recorded their values in that file's
-- comment; the owner ruled on 2026-09-16 that the seeded hierarchy types become LIVE, in a NEW
-- late file — this one. The recorded values, verified against
-- `git show 75a55f7e:migrations/030_strengthen_rel_types_wikidata_ontology.sql`:
--
--     rel_type      head_types   tail_types                ← 030 (2026-0x), never live
--     instance_of   {ANY}        {Concept}
--     subclass_of   {Concept}    {Concept}
--     part_of       {ANY}        {ANY}
--     is_a          {ANY}        {Concept}
--     member_of     {ANY}        {Concept, Organization}
--
-- WHAT IS ASSERTED, AND WHY IT IS {ANY} ON EVERY ROLE (measured, not assumed)
-- ---------------------------------------------------------------------------
-- The seed the ENGINE carries for these five rels is src/wgm/gate.py::SEED_ONTOLOGY:
-- "Hierarchy rel_types — head_types/tail_types are ANY (classification is unconstrained)", and
-- the gate's own constraint-widening writer (gate.py `_store_inferred_rel_type`) was FIXED to
-- make exactly that a hard rule: "a /expand batch ... drove instance_of/subclass_of (seeded ANY =
-- 'classify ANYTHING') down to {Concept}, after which the ingest type-fallback stamped every
-- instance_of OBJECT (e.g. an Animal 'dog') as Concept — cross-domain typing bleed ... a
-- universal classifier (instance_of/subclass_of/part_of) can never be narrowed". The SQL seed
-- carried NULL, which the gate reads as unconstrained — so the Python seed and the SQL seed have
-- always AGREED on {ANY}; 030's concrete values were the only dissenting declaration and they
-- never ran. This file makes the SQL seed say what the engine seed says, explicitly.
--
-- Why the three CONCRETE recorded values are NOT re-asserted — each one measured on a fresh
-- tenant carrying them, with the gate's concrete-type refusal (this lane's TC3) ON, evidence in
-- the internal design record
--   • instance_of / is_a tail {Concept}, subclass_of head/tail {Concept}: THE HARD LINE files a
--     memory AT a class node, and the engine stores the class node's MEMBER category in
--     entities.entity_type — the gate's hierarchy resolver (`_resolve_entity_type_via_hierarchy`,
--     "Rex → poodle → Animal") DEPENDS on it. GLiNER2 therefore types the class node `dog` as
--     Animal, and under {Concept} the founding rung is refused: measured
--     `(rex, instance_of, dog)` → REFUSED (tail seen ANIMAL, declared ['Concept']);
--     `(rex, instance_of, poodle)` → REFUSED. That is the ladder itself, not a mismatch. And with
--     the constraint present, the entailment lane (`_entailed_constraint_type`) stamps every
--     UNTYPED class node 'Concept' — the exact "cross-domain typing bleed" the gate was fixed for.
--   • member_of tail {Concept, Organization}: the seeded `household` grouping (members
--     {Person, Animal}, defining {has_pet, member_of}) is hollowed by migration 122's trigger at
--     its NEXT entity_taxonomies write — INVARIANT 2 strips a defining rel whose concrete type is
--     not a member, and ANY does not rescue a sibling — measured: after a taxonomy UPDATE,
--     household → ['has_pet'] (control with tail {ANY}: ['has_pet','member_of'] kept). The gate's
--     own widening writer also rewrites this tail to {…, ANY} on the first membership ingest
--     (measured on a fresh tenant: {Organization, ANY} after "I am a member of the chess club").
-- part_of is the recorded value verbatim ({ANY}/{ANY}); the heads of instance_of / is_a /
-- member_of are the recorded value verbatim ({ANY}).
--
-- HOW (per role, per row): SEEDED rows only (rel_types.source ∈ wikidata|builtin — the seed's own
-- provenance column, the tuple src/api/canonical.py::SEEDED_REL_SOURCES owns for Python
-- readers; a `source='user'`/`'engine'` row of the same name is never touched), in public and
-- in every tenant schema. {ANY} is the WIDEST value, so the write is a UNION-widening by
-- construction: a NULL/empty role becomes {ANY}; a tenant's grown concrete set (e.g. is_a tail
-- {Location}) is widened to {ANY}, never narrowed; a role already carrying ANY (production's
-- gate-widened member_of {Organization, ANY}) is left exactly as it is. Idempotent on a
-- three-boot box: the second run writes 0 rows. It does not touch is_symmetric /
-- inverse_rel_type / is_hierarchy_rel (022 and 089 carry those) and writes no entity_taxonomies
-- row, so no grouping array changes (measured before/after: identical).
--
-- WHAT "LIVE" MEANS HERE, honestly: {ANY} is unconstrained, so this file changes NO validation
-- outcome on its own — the gate already read NULL that way. What is live is the DECLARATION
-- (the seed says {ANY} in both places instead of one), and, from this lane's TC3, the gate now
-- REFUSES a concrete mismatch on every rel whose seed IS concrete (spouse, educated_at, has_pet,
-- lives_in, …). If the owner wants a concrete class-node constraint on the ladder, the value
-- that fits the engine is a ROLE check (the object of P31/P279 must be a PLACE, never a named
-- instance — main.py `_entity_is_l4_place`), not a GLiNER2 category — that is a separate design,
-- named in the lane report, not taken here.

DO $$
DECLARE
    _schema TEXT;
    _n      INTEGER;
BEGIN
    FOR _schema IN
        SELECT 'public'
        UNION ALL
        SELECT nspname FROM pg_namespace WHERE nspname LIKE 'faultline\_%' ORDER BY 1
    LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema = _schema AND table_name = 'rel_types') THEN
            CONTINUE;
        END IF;
        EXECUTE format($u$
            UPDATE %I.rel_types t
               SET head_types = CASE
                       WHEN EXISTS (SELECT 1 FROM unnest(COALESCE(t.head_types, '{}')) h
                                    WHERE LOWER(h) = 'any')
                       THEN t.head_types ELSE ARRAY['ANY']::TEXT[] END,
                   tail_types = CASE
                       WHEN EXISTS (SELECT 1 FROM unnest(COALESCE(t.tail_types, '{}')) x
                                    WHERE LOWER(x) = 'any')
                       THEN t.tail_types ELSE ARRAY['ANY']::TEXT[] END
             WHERE t.rel_type IN ('instance_of', 'subclass_of', 'part_of', 'is_a', 'member_of')
               AND t.source IN ('wikidata', 'builtin')
               AND (NOT EXISTS (SELECT 1 FROM unnest(COALESCE(t.head_types, '{}')) h
                                WHERE LOWER(h) = 'any')
                    OR NOT EXISTS (SELECT 1 FROM unnest(COALESCE(t.tail_types, '{}')) x
                                   WHERE LOWER(x) = 'any'))
        $u$, _schema);
        GET DIAGNOSTICS _n = ROW_COUNT;
        IF _n > 0 THEN
            RAISE NOTICE 'Migration 278: hierarchy role types asserted {ANY} in % (% row)', _schema, _n;
        END IF;
    END LOOP;
END $$;
