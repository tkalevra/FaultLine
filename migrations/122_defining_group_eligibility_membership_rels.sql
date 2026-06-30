-- Migration 122: defining-group eligibility allows MEMBERSHIP/COMPOSITION hierarchy rels
-- Date: 2026-06-29
--
-- WHY
-- ---
-- The engine grows an entity's FACTS for any novel subject, but the GROUPING node the scoped
-- query walk needs (entity_taxonomies) was 100% hand-seeded. A novel collective ("my band has
-- a guitarist named Jax, a drummer named Reef, a singer named Coda") captures `member_of <group>`
-- edges, yet no `band` taxonomy was minted -> "tell me about my band" could not scope and leaked
-- to fetch-all on a multi-subject tenant. The new ingest producer
-- (_mint_groupings_from_membership) auto-mints the grouping from those observed member_of edges.
--
-- BLOCKER this migration removes:
--   enforce_defining_group_eligibility() (migration 119) INVARIANT 1 stripped EVERY hierarchy rel
--   from a defining group. `member_of` is is_hierarchy_rel=TRUE, so a minted `band` defined by
--   member_of had its only defining rel stripped -> hollow taxonomy -> the walk skipped it ->
--   fetch-all leak persisted. But member_of is a MEMBERSHIP rel, NOT a TYPE-CLASSIFICATION rel
--   (it has no P31/P279 PID); it only matches edges into a specific named group, so it does NOT
--   intercept every typed entity (the real danger INVARIANT 1 targeted, i.e. instance_of/
--   subclass_of/is_a).
--
-- FIX (subject-agnostic, metadata-driven): INVARIANT 1 now drops a hierarchy rel ONLY when it is
-- a TYPE-CLASSIFICATION rel (wikidata_pid IN ('P31','P279')). Membership/composition hierarchy
-- rels (member_of/part_of/located_in) are kept and arbitrated by INVARIANT 2 (cross-type guard).
-- This mirrors the P31/P279 split already used by _get_classification_rels. No rel/place literals.
--
-- This re-creates the trigger FUNCTION in every existing tenant schema; the template
-- (src/provisioning/templates/user_schema.sql) carries the same change for fresh tenants.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format('SET search_path TO %I', _schema);
        EXECUTE $fn$
            CREATE OR REPLACE FUNCTION enforce_defining_group_eligibility()
            RETURNS TRIGGER AS $body$
            DECLARE
                _rel     TEXT;
                _kept    TEXT[] := '{}';
                _is_hier BOOLEAN;
                _pid     TEXT;
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
                    SELECT is_hierarchy_rel, wikidata_pid, head_types, tail_types
                      INTO _is_hier, _pid, _heads, _tails
                      FROM rel_types WHERE rel_type = LOWER(_rel);

                    IF NOT FOUND THEN
                        _kept := array_append(_kept, _rel);
                        CONTINUE;
                    END IF;

                    -- INVARIANT 1 (refined): drop only TYPE-CLASSIFICATION hierarchy rels.
                    IF COALESCE(_is_hier, FALSE) AND COALESCE(_pid, '') IN ('P31', 'P279') THEN
                        RAISE WARNING 'faultline: dropped classification rel % from defining group of taxonomy %',
                                      _rel, NEW.taxonomy_name;
                        CONTINUE;
                    END IF;

                    -- INVARIANT 2 (cross-type guard): every CONCRETE head/tail type must be a member.
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
    END LOOP;
END $$;
