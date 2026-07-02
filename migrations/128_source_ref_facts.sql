-- Migration 128: source_ref on facts + staged_facts — citable fact provenance
-- Date: 2026-07-02
--
-- WHY
-- ---
-- The ingest-retention spine's document lane (ingest_document MCP tool) carries a
-- source reference (URL, filename, title) into the ingest request; without this
-- column the citation dies at the episodic_log (migration 127). This adds a
-- nullable source_ref column to BOTH facts and staged_facts so citations survive
-- staging, promotion (re_embedder promote_staged_facts / promote_class_c_hits),
-- and the Qdrant payload sync. NULL for conversational facts — zero behavior
-- change when absent.
--
-- PER-TENANT: the ingest/query request connection runs with `SET search_path TO
-- {schema}` WITHOUT public, so the column must exist INSIDE each tenant schema.
-- This migration applies the ALTER to every already-provisioned tenant schema AND
-- to public (the seed-source/template — the template facts/staged_facts live
-- there); NEW tenants get the column from the template
-- (src/provisioning/templates/user_schema.sql).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, guarded by table existence. Safe to run
-- repeatedly. No DROP, no destructive SQL.

DO $$
DECLARE
    _schema TEXT;
    _table  TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM   information_schema.schemata
        WHERE  schema_name LIKE 'faultline\_%'
           OR  schema_name = 'public'
    LOOP
        FOREACH _table IN ARRAY ARRAY['facts', 'staged_facts']
        LOOP
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE  table_schema = _schema
                  AND  table_name   = _table
            ) THEN
                EXECUTE format(
                    'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS source_ref TEXT DEFAULT NULL',
                    _schema, _table
                );
                RAISE NOTICE 'Migration 128: added source_ref to %.%', _schema, _table;
            END IF;
        END LOOP;
    END LOOP;
END $$;
