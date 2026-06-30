-- Migration 119: residence/location rel typing + located_in as an L4 hierarchy rel.
-- Date: 2026-06-27
--
-- WHY
-- ---
-- Live pre-prod evidence for "I live in Riverton, Ontario":
--   wgm.category_invalid_subject        allowed_types={'location'} rel_type=lives_in subject_type=person
--   wgm.hierarchy_membership_violation   object_type=LOCATION ... hierarchy_members=['PERSON','ANIMAL'] / ['LOCATION']
--   wgm.hierarchy_violation_low_confidence  rel_type=lives_in  -> "stored as Class C (staged)"
-- The canonical residence fact (Person, lives_in, Location) was being flagged a type/hierarchy
-- violation and demoted to staged Class C.
--
-- ROOT CAUSE (metadata, two defects):
--   Defect 3 — `lives_in`/`lives_at` are ASYMMETRIC CROSS-TYPE rels (Person/Animal -> Location),
--     yet they were listed in the `rel_types_defining_group` of the `location` and `household`
--     taxonomies. The WGM gate's _validate_hierarchy_membership() requires BOTH ends of a
--     defining rel to be members of ONE homogeneous taxonomy. Person∈household but Location∉,
--     and Location∈location but Person∉ -> partial match -> false violation -> Class C demotion.
--     Residence rels are correctly typed by head_types/tail_types (head=Person/Animal,
--     tail=Location), NOT by same-type group membership.
--   Defect 4 — `located_in` had is_hierarchy_rel=false, so geographic containment
--     (Riverton located_in Ontario located_in Canada ...) was never walked as L4. It must be a
--     HIERARCHY rel (head=Location, tail=Location) so the deterministic L4 walk traverses it the
--     same way as subclass_of/part_of. Per the repo invariant (main.py _strip_hierarchy_rels_from_
--     defining / embedder.py guard) a hierarchy rel must NEVER appear in any
--     rel_types_defining_group, so located_in is also removed from the defining groups.
--
-- THE FIX (metadata-driven, subject-agnostic, NO place-name literals):
--   1. rel_types: located_in -> is_hierarchy_rel=TRUE, head=ANY, tail=ANY (UNIVERSAL containment,
--      mirrors part_of; locative discipline lives at the deriver, not the type constraint).
--      Re-affirm residence typing: lives_in (Person,Animal -> Location),
--      lives_at (Person,Animal -> Location|SCALAR), born_in (Person -> Location),
--      located_at (-> SCALAR).
--   2. entity_taxonomies: strip `located_in` from EVERY taxonomy's rel_types_defining_group
--      (hierarchy-rel invariant); strip the cross-type residence rels `lives_in`/`lives_at`
--      from the `location` and `household` defining groups. Keep `location` hierarchical
--      (is_hierarchical=true, parent_rel_type='located_in').
--   The companion gate change (src/wgm/gate.py _validate_hierarchy_membership) is the DURABLE
--   guard: it skips the homogeneous-membership check for any rel whose concrete head/tail type
--   sets are disjoint (cross-type asymmetric), so even if the growth engine later re-appends a
--   residence rel to a defining group, the false violation cannot recur.
--
-- SEEDING MODEL: public.rel_types / public.entity_taxonomies are the SEED SOURCE; NEW tenants are
-- provisioned by blanket-copy from public (schema_manager.py), so updating public makes every new
-- tenant correct. EXISTING tenants are fixed by the per-schema loop in Part 2. The per-tenant
-- template (user_schema.sql) carries only table DDL for these tables (no seed rows), so no
-- template change is required.
--
-- Idempotent: all statements are UPDATEs that set absolute values / array_remove (re-runnable).
-- NOTE: after applying, FLUSH the overlay caches (GET /internal/refresh-intent-pattern-caches)
-- or wait the 5s TTL so rel_type_overlay / taxonomy_overlay pick up the change.

-- ============================================================================
-- Part 1: public (SEED-SOURCE / TEMPLATE) — rel_types
-- ============================================================================

-- located_in: UNIVERSAL containment hierarchy. DEFECT 4.
-- Subject-agnostic: anything can be located inside anything (a city in a province, a server in a
-- rack, a file in a folder). Typing is intentionally permissive (ANY -> ANY, mirroring its sibling
-- hierarchy rel `part_of`); the spatial/locative discipline is enforced at the DERIVER (strong
-- ingest), NOT at the type constraint (lean query). Narrowing to Location->Location quarantines
-- legitimate non-geographic containment to Class C — exactly the brittleness we are removing.
UPDATE public.rel_types SET
    head_types       = ARRAY['ANY']::TEXT[],
    tail_types       = ARRAY['ANY']::TEXT[],
    is_hierarchy_rel = TRUE,
    is_symmetric     = FALSE
WHERE rel_type = 'located_in';

-- lives_in: animate resident -> Location (asymmetric, NOT a hierarchy rel). DEFECT 3.
UPDATE public.rel_types SET
    head_types       = ARRAY['Person', 'Animal']::TEXT[],
    tail_types       = ARRAY['Location']::TEXT[],
    is_hierarchy_rel = FALSE,
    is_symmetric     = FALSE
WHERE rel_type = 'lives_in';

-- lives_at: animate resident -> Location or address SCALAR (asymmetric). DEFECT 3.
UPDATE public.rel_types SET
    head_types       = ARRAY['Person', 'Animal']::TEXT[],
    tail_types       = ARRAY['Location', 'SCALAR']::TEXT[],
    is_hierarchy_rel = FALSE,
    is_symmetric     = FALSE
WHERE rel_type = 'lives_at';

-- born_in: person -> birthplace Location (re-affirm coherent asymmetric typing).
UPDATE public.rel_types SET
    head_types       = ARRAY['Person']::TEXT[],
    tail_types       = ARRAY['Location']::TEXT[],
    is_hierarchy_rel = FALSE,
    is_symmetric     = FALSE
WHERE rel_type = 'born_in';

-- located_at: entity -> address SCALAR (re-affirm; NOT a hierarchy rel).
UPDATE public.rel_types SET
    tail_types       = ARRAY['SCALAR']::TEXT[],
    is_hierarchy_rel = FALSE,
    is_symmetric     = FALSE
WHERE rel_type = 'located_at';

-- ============================================================================
-- Part 1b: public (SEED-SOURCE / TEMPLATE) — entity_taxonomies
-- ============================================================================

-- Hierarchy-rel invariant: located_in is now a hierarchy rel -> it must NOT appear in any
-- taxonomy's rel_types_defining_group (subject-agnostic, applies to all taxonomies).
UPDATE public.entity_taxonomies SET
    rel_types_defining_group = COALESCE(array_remove(rel_types_defining_group, 'located_in'), '{}')
WHERE rel_types_defining_group @> ARRAY['located_in']::TEXT[];

-- Cross-type residence rels do not belong in a homogeneous-membership defining group: strip them
-- from the seeded `location` and `household` taxonomies.
UPDATE public.entity_taxonomies SET
    rel_types_defining_group = COALESCE(
        array_remove(array_remove(rel_types_defining_group, 'lives_in'), 'lives_at'), '{}')
WHERE taxonomy_name IN ('location', 'household');

-- Keep `location` a walkable geographic containment hierarchy.
UPDATE public.entity_taxonomies SET
    is_hierarchical = TRUE,
    parent_rel_type = 'located_in'
WHERE taxonomy_name = 'location';

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
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
        -- rel_types
        EXECUTE format($u$
            UPDATE %I.rel_types SET
                head_types = ARRAY['ANY']::TEXT[],
                tail_types = ARRAY['ANY']::TEXT[],
                is_hierarchy_rel = TRUE, is_symmetric = FALSE
            WHERE rel_type = 'located_in'
        $u$, _schema);

        EXECUTE format($u$
            UPDATE %I.rel_types SET
                head_types = ARRAY['Person','Animal']::TEXT[],
                tail_types = ARRAY['Location']::TEXT[],
                is_hierarchy_rel = FALSE, is_symmetric = FALSE
            WHERE rel_type = 'lives_in'
        $u$, _schema);

        EXECUTE format($u$
            UPDATE %I.rel_types SET
                head_types = ARRAY['Person','Animal']::TEXT[],
                tail_types = ARRAY['Location','SCALAR']::TEXT[],
                is_hierarchy_rel = FALSE, is_symmetric = FALSE
            WHERE rel_type = 'lives_at'
        $u$, _schema);

        EXECUTE format($u$
            UPDATE %I.rel_types SET
                head_types = ARRAY['Person']::TEXT[],
                tail_types = ARRAY['Location']::TEXT[],
                is_hierarchy_rel = FALSE, is_symmetric = FALSE
            WHERE rel_type = 'born_in'
        $u$, _schema);

        EXECUTE format($u$
            UPDATE %I.rel_types SET
                tail_types = ARRAY['SCALAR']::TEXT[],
                is_hierarchy_rel = FALSE, is_symmetric = FALSE
            WHERE rel_type = 'located_at'
        $u$, _schema);

        -- entity_taxonomies
        EXECUTE format($u$
            UPDATE %I.entity_taxonomies SET
                rel_types_defining_group =
                    COALESCE(array_remove(rel_types_defining_group, 'located_in'), '{}')
            WHERE rel_types_defining_group @> ARRAY['located_in']::TEXT[]
        $u$, _schema);

        EXECUTE format($u$
            UPDATE %I.entity_taxonomies SET
                rel_types_defining_group = COALESCE(
                    array_remove(array_remove(rel_types_defining_group, 'lives_in'), 'lives_at'),
                    '{}')
            WHERE taxonomy_name IN ('location', 'household')
        $u$, _schema);

        EXECUTE format($u$
            UPDATE %I.entity_taxonomies SET
                is_hierarchical = TRUE, parent_rel_type = 'located_in'
            WHERE taxonomy_name = 'location'
        $u$, _schema);

        -- Producer-side invariant: install the defining-group eligibility trigger so growth /
        -- in-flow can never RE-introduce a cross-type or hierarchy rel into a defining group.
        -- Created under the tenant search_path (unqualified refs resolve to the tenant; the
        -- function body's RAISE WARNING '%' placeholders would break format(), so we SET path
        -- and EXECUTE plain literals instead of qualifying with %I).
        EXECUTE format('SET search_path TO %I', _schema);
        EXECUTE $fn$
            CREATE OR REPLACE FUNCTION enforce_defining_group_eligibility()
            RETURNS TRIGGER AS $body$
            DECLARE
                _rel     TEXT;
                _kept    TEXT[] := '{}';
                _is_hier BOOLEAN;
                _heads   TEXT[];
                _tails   TEXT[];
                _members TEXT[];
            BEGIN
                IF NEW.rel_types_defining_group IS NULL
                   OR array_length(NEW.rel_types_defining_group, 1) IS NULL THEN
                    RETURN NEW;
                END IF;
                IF NEW.member_entity_types IS NULL
                   OR array_length(NEW.member_entity_types, 1) IS NULL THEN
                    RETURN NEW;
                END IF;

                SELECT array_agg(LOWER(m)) INTO _members FROM unnest(NEW.member_entity_types) m;

                FOREACH _rel IN ARRAY NEW.rel_types_defining_group LOOP
                    SELECT is_hierarchy_rel, head_types, tail_types
                      INTO _is_hier, _heads, _tails
                      FROM rel_types WHERE rel_type = LOWER(_rel);

                    IF NOT FOUND THEN
                        _kept := array_append(_kept, _rel);
                        CONTINUE;
                    END IF;

                    IF COALESCE(_is_hier, FALSE) THEN
                        RAISE WARNING 'faultline: dropped hierarchy rel % from defining group of taxonomy %',
                                      _rel, NEW.taxonomy_name;
                        CONTINUE;
                    END IF;

                    IF NOT ('any' = ANY(_members)) THEN
                        IF EXISTS (SELECT 1 FROM unnest(COALESCE(_heads, '{}')) h
                                   WHERE LOWER(h) NOT IN ('any','scalar') AND LOWER(h) <> ALL(_members))
                           OR EXISTS (SELECT 1 FROM unnest(COALESCE(_tails, '{}')) t
                                      WHERE LOWER(t) NOT IN ('any','scalar') AND LOWER(t) <> ALL(_members))
                        THEN
                            RAISE WARNING 'faultline: dropped cross-type rel % from defining group of taxonomy % (head/tail not all members)',
                                          _rel, NEW.taxonomy_name;
                            CONTINUE;
                        END IF;
                    END IF;

                    _kept := array_append(_kept, _rel);
                END LOOP;

                NEW.rel_types_defining_group := _kept;
                RETURN NEW;
            END;
            $body$ LANGUAGE plpgsql;
        $fn$;
        EXECUTE 'DROP TRIGGER IF EXISTS enforce_defining_group_eligibility_ins ON entity_taxonomies';
        EXECUTE 'CREATE TRIGGER enforce_defining_group_eligibility_ins'
             || ' BEFORE INSERT ON entity_taxonomies'
             || ' FOR EACH ROW EXECUTE FUNCTION enforce_defining_group_eligibility()';
        EXECUTE 'DROP TRIGGER IF EXISTS enforce_defining_group_eligibility_upd ON entity_taxonomies';
        EXECUTE 'CREATE TRIGGER enforce_defining_group_eligibility_upd'
             || ' BEFORE UPDATE ON entity_taxonomies'
             || ' FOR EACH ROW EXECUTE FUNCTION enforce_defining_group_eligibility()';

        RAISE NOTICE 'Migration 119: residence/location rel typing + defining-group trigger installed in %', _schema;
    END LOOP;
    RESET search_path;
END $$;
