-- Migration 059: Add missing unique constraints to existing user schemas
--
-- Per-user schemas created before migration 051 was updated may be missing
-- unique constraints that code relies on for ON CONFLICT clauses.
-- Loops over all ready user schemas and adds them idempotently.
--
-- Constraints added (idempotent: errors caught individually per schema):
--   entity_aliases:         UNIQUE (entity_id, alias)
--   ontology_evaluations:   UNIQUE (candidate_rel_type, sample_subject_id, sample_object)

DO $$
DECLARE
    r RECORD;
    sql_text TEXT;
BEGIN
    FOR r IN
        SELECT schema_name FROM public.user_provisioning WHERE status = 'ready'
    LOOP
        -- entity_aliases: required by registry.py ON CONFLICT (entity_id, alias)
        BEGIN
            sql_text := format(
                'ALTER TABLE %I.entity_aliases ADD CONSTRAINT uq_entity_aliases_entity_alias UNIQUE (entity_id, alias)',
                r.schema_name
            );
            EXECUTE sql_text;
            RAISE NOTICE 'Added uq_entity_aliases_entity_alias to %', r.schema_name;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'entity_aliases constraint on %: % (likely already exists)', r.schema_name, SQLERRM;
        END;

        -- ontology_evaluations: required by ingest ON CONFLICT (candidate_rel_type, sample_subject_id, sample_object)
        BEGIN
            sql_text := format(
                'ALTER TABLE %I.ontology_evaluations ADD CONSTRAINT uq_ontology_eval_candidate UNIQUE (candidate_rel_type, sample_subject_id, sample_object)',
                r.schema_name
            );
            EXECUTE sql_text;
            RAISE NOTICE 'Added uq_ontology_eval_candidate to %', r.schema_name;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'ontology_evaluations constraint on %: % (likely already exists)', r.schema_name, SQLERRM;
        END;

    END LOOP;
END $$;
