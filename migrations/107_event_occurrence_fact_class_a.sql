-- 107: event-occurrence rels → fact_class A (events are user truth, must not degrade/die).
--
-- participated_in and attended were fact_class=C. Consequence (assign_class_and_confidence):
--   user_stated  → B  (not authoritative — even though "I attended X" is user truth)
--   llm_inferred → C  → Qdrant vector tier, 30-day expiry → the event DEGRADES AND DIES.
-- Set them to A. With defined_class=A the assigner gives:
--   user_stated  → A  (authoritative, immediate facts-table, never expires)
--   llm_inferred → B  (durable staged, promotes — NOT C/vector/dying)
-- which is the correct tiering for events. `met` (event, already B) is left durable as-is.
-- Metadata-only; idempotent; public seed + fan-out to existing tenants.

UPDATE public.rel_types SET fact_class = 'A'
 WHERE rel_type IN ('participated_in', 'attended') AND fact_class <> 'A';

DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT nspname FROM pg_namespace WHERE nspname LIKE 'faultline\_%' LOOP
        EXECUTE format(
            'UPDATE %I.rel_types SET fact_class = ''A'' WHERE rel_type IN (''participated_in'',''attended'') AND fact_class <> ''A''',
            r.nspname);
        RAISE NOTICE '107: participated_in/attended → fact_class A in %', r.nspname;
    END LOOP;
END $$;
