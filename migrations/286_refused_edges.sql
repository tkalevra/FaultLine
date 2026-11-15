-- Migration 286: refused_edges — the gate's refusal memory (issue #38 item 6)
-- Date: 2026-09-29
--
-- WHY
-- ---
-- "user owns chess rating" was minted by the atomizer and REFUSED by the WGM gate (type_refused —
-- a rating is not an ownable object). A later harvest turn re-typed the entity as Object and wrote
-- a separate, valid-looking Class-B owns row (llm_inferred; staged_facts 65 / facts 80), which then
-- rendered under "Less certain". Nothing recorded the refusal, so no later lane had anything to
-- consult. This table is that record: one row per (subject_id, rel_type, object_id) the gate
-- refused, written at the refusal site (main.py::_ingest_impl) and consulted before any later
-- non-correction write of the SAME triple. A user correction is the only lane that clears it.
--
-- Per-tenant only (the bound search_path is the scope — no user_id column). New tenants get it
-- from templates/user_schema.sql; this fans it out to EXISTING tenants. Idempotent.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
        ORDER BY schema_name
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema
              AND table_name   = 'facts'
        ) THEN
            RAISE NOTICE 'migration 286: skipping % (no facts table)', _schema;
            CONTINUE;
        END IF;

        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.refused_edges ('
            '    id BIGSERIAL PRIMARY KEY,'
            '    subject_id TEXT NOT NULL,'
            '    rel_type TEXT NOT NULL,'
            '    object_id TEXT NOT NULL,'
            '    role TEXT,'
            '    entity_type TEXT,'
            '    reason TEXT,'
            '    provenance TEXT,'
            '    refusal_count INTEGER NOT NULL DEFAULT 1,'
            '    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),'
            '    last_refused_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),'
            '    CONSTRAINT uq_refused_edges_triple UNIQUE (subject_id, rel_type, object_id)'
            ')', _schema);

        RAISE NOTICE 'migration 286: refused_edges ensured in %', _schema;
    END LOOP;
END $$;
