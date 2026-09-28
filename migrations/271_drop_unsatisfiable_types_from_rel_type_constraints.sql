-- 271: strip UNSATISFIABLE entity types from rel_types head_types/tail_types
--
-- WHY. Migration 122's trigger INVARIANT 2 requires every CONCRETE head/tail type of a
-- defining rel to be a member of the taxonomy (`any`/`scalar` exempt, and each concrete type
-- checked INDIVIDUALLY -- the presence of ANY does NOT rescue a sibling concrete type). A
-- constraint type that NO ENTITY IN THE TENANT CARRIES can never be satisfied, so it can only
-- ever strip a rel out of `rel_types_defining_group`. `determine_path` then reads the empty PG
-- array as FALSY and the grouping is INERT at query time.
--
-- MEASURED CASE. Live tenants carry `member_of.tail_types = {Organization, Group, ANY}`.
-- `Group` is not one of `_CANONICAL_ENTITY_TYPES` (main.py:5243 -- the closed six-label set
-- GLiNER2 purity fixes, Pitfall 11), no entity anywhere carries it, and no taxonomy admits it.
-- It did not come from a seed: migration 030 set `{Concept, Organization}`, `public.rel_types`
-- is NULL, gate.py SEED_ONTOLOGY declares ANY/ANY. It arrived via the gate's type-constraint
-- WIDENING path (gate.py:53). Simulated against the real predicate:
--     members {person}               tails {Organization,Group,ANY} -> DROPPED [Organization,Group]
--     members {person,organization}  tails {Organization,Group,ANY} -> DROPPED [Group]
--     members {person,organization}  tails {Organization,ANY}       -> KEPT
-- With c2f4f1b9 (the mint unions the GROUP OBJECT's own type into member_entity_types),
-- `Group` is the ONLY remaining blocker on membership groupings.
--
-- WHY THE PREDICATE IS "NO ENTITY CARRIES IT" AND NOT "NOT CANONICAL". A blanket
-- non-canonical strip was written first and REJECTED after measurement: 48 rows across local
-- tenants name 16 non-canonical types, and several (`Device`, `Server`, `Router`, `Switch`,
-- `Firewall`, `Network`) are GROWN domain types from `/learn networking`. Deleting those would
-- have a MIGRATION DELETE GROWTH, inverting this project's authority order (user > seed >
-- growth: growth may ADD/widen but must never be overridden). Measured: only `Event` is
-- actually carried by an entity (4 rows), and it is PRESERVED IN THE TENANTS THAT CARRY IT.
-- MEASURED AFTER APPLYING: rows naming `Event` went 18 -> 12 and `Group` went 9 -> 0. The six
-- `Event` rows that lost it belong to tenants where NO entity is typed Event, so the constraint
-- was unsatisfiable THERE. That is the predicate working as specified, not over-reach -- and it
-- cannot tighten validation: gate.py:826 reads ANY/SCALAR/EMPTY as unconstrained-pass, so
-- removing a type only ever WIDENS. A tenant that later grows an Event entity is unaffected in
-- validation terms; if a concrete constraint is wanted back, the gate's widening path restores
-- it from observation. Nothing a tenant can currently satisfy is touched.
--
-- WHAT THIS IS NOT. It does NOT widen any tail to ANY to slip past INVARIANT 2. That guard is
-- deliberate (migrations 119/122 bar a CROSS-TYPE defining rel because the gate needs both
-- ends inside one homogeneous taxonomy -- the same guard that correctly keeps `lives_in` out
-- of the family grouping). Every SATISFIABLE concrete type is preserved.
--
-- Subject-agnostic, metadata-driven, no rel_type named. Idempotent. Per-tenant; `public` is
-- skipped (it is a seed template with no entity population to measure against).

DO $$
DECLARE
    _schema TEXT;
    _ok     TEXT[];
BEGIN
    FOR _schema IN
        SELECT nspname FROM pg_namespace WHERE nspname LIKE 'faultline\_%'
    LOOP
        EXECUTE format('SET search_path TO %I', _schema);

        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema = _schema AND table_name = 'rel_types')
           OR NOT EXISTS (SELECT 1 FROM information_schema.tables
                          WHERE table_schema = _schema AND table_name = 'entities') THEN
            CONTINUE;
        END IF;

        -- SATISFIABLE = the canonical six (+ ANY/SCALAR) UNION every type an entity in THIS
        -- tenant actually carries. Grown domain types that are in use are therefore preserved.
        EXECUTE $q$
            SELECT ARRAY(
                SELECT LOWER(entity_type) FROM entities WHERE entity_type IS NOT NULL
                UNION
                SELECT unnest(ARRAY['person','animal','organization','location',
                                    'object','concept','any','scalar'])
            )
        $q$ INTO _ok;

        EXECUTE $q$
            UPDATE rel_types r SET
                head_types = CASE WHEN r.head_types IS NULL THEN NULL ELSE
                    ARRAY(SELECT h FROM unnest(r.head_types) h WHERE LOWER(h) = ANY($1)) END,
                tail_types = CASE WHEN r.tail_types IS NULL THEN NULL ELSE
                    ARRAY(SELECT t FROM unnest(r.tail_types) t WHERE LOWER(t) = ANY($1)) END
            WHERE EXISTS (
                SELECT 1
                FROM unnest(COALESCE(r.head_types, '{}') || COALESCE(r.tail_types, '{}')) x
                WHERE LOWER(x) <> ALL($1)
            )
        $q$ USING _ok;
    END LOOP;

    EXECUTE 'SET search_path TO public';
END $$;
