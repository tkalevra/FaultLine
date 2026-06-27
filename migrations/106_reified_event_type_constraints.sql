-- 106: let participated_in / also_known_as accept the reified Event-occurrence.
--
-- Keystone A reifies each event as its OWN entity (typed Event) — the memory filed at its bare
-- type. But two rel_types the occurrence sits on excluded Event in their type constraints:
--   participated_in.tail_types = {Concept}                       → (user, participated_in, occurrence[Event])
--        tripped WGM type_mismatch → the reified occurrence edge was DEMOTED to Class B (staged)
--        instead of landing cleanly.
--   also_known_as.head_types  = {Person,Organization,Location,Animal,Object}
--        → (occurrence[Event], also_known_as, title) was head-inconsistent → re-type friction.
-- An Event occurrence is a legitimate participation tail and a legitimate name-bearer, so add
-- Event to both. Metadata-only; idempotent; public seed + fan-out to existing tenants.

DO $$
DECLARE r record;
BEGIN
    -- public seed
    UPDATE public.rel_types
       SET tail_types = (SELECT array_agg(DISTINCT x) FROM unnest(tail_types || ARRAY['Event']) AS x)
     WHERE rel_type = 'participated_in' AND NOT ('Event' = ANY(tail_types));
    UPDATE public.rel_types
       SET head_types = (SELECT array_agg(DISTINCT x) FROM unnest(head_types || ARRAY['Event']) AS x)
     WHERE rel_type = 'also_known_as' AND NOT ('Event' = ANY(head_types));

    -- fan out to existing tenant schemas
    FOR r IN SELECT nspname FROM pg_namespace WHERE nspname LIKE 'faultline\_%' LOOP
        EXECUTE format(
            'UPDATE %I.rel_types SET tail_types = (SELECT array_agg(DISTINCT x) FROM unnest(tail_types || ARRAY[''Event'']) AS x) WHERE rel_type = ''participated_in'' AND NOT (''Event'' = ANY(tail_types))',
            r.nspname);
        EXECUTE format(
            'UPDATE %I.rel_types SET head_types = (SELECT array_agg(DISTINCT x) FROM unnest(head_types || ARRAY[''Event'']) AS x) WHERE rel_type = ''also_known_as'' AND NOT (''Event'' = ANY(head_types))',
            r.nspname);
        RAISE NOTICE '106: participated_in.tail += Event, also_known_as.head += Event in %', r.nspname;
    END LOOP;
END $$;
