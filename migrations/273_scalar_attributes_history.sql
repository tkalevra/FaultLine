-- Migration 273: scalar-supersession — append-only history ledger for entity_attributes.
-- Date: 2026-09-03. Campaign: the internal design record (bars SS1-SS5).
--
-- WHY
-- ---
-- The attribute lane is the ONLY user-value store with no supersession semantics: a scalar
-- correction OVERWRITES the value in place (ON CONFLICT (entity_id, attribute) DO UPDATE SET
-- value_* = EXCLUDED.*) and, because the PK is single-row, the PRIOR VALUE IS ANNIHILATED --
-- no history, no audit trail, no retirement stamp -- even though the table HAS superseded_at /
-- valid_until columns. Measured live (PHASE1-REPRO.md, fresh nonsense seat): member_of went
-- eleven -> twelve with superseded_at NULL and the prior value recoverable NOWHERE (zero
-- history/audit tables in the tenant schema; episodic_log is retention, not recovery).
--
-- This migration lands the SS1 "append-only audit record" surface IN EVERY TENANT SCHEMA
-- (never public -- per-tenant isolation, SS5): entity_attributes_history, created identically
-- to the tenant-template definition (src/provisioning/templates/user_schema.sql) so new and
-- existing tenants converge on ONE shape. Append-only: nothing here ever UPDATEs or DELETEs
-- user data; THE HARD LINE holds -- history rows are content, never places (no entity/L4
-- minting anywhere in this migration).
--
-- Boot-migration ledger: docker-entrypoint runs migrations through the boot gate
-- (src/provisioning/boot_migrations.py) which stamps each (schema, file) as applied, so this
-- runs exactly once per tenant schema and never re-runs. Idempotent DDL regardless
-- (IF NOT EXISTS).

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
        -- Skip a schema with no entity_attributes table (partial / aborted provisioning).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema
              AND table_name   = 'entity_attributes'
        ) THEN
            RAISE NOTICE 'migration 273: skipping % (no entity_attributes)', _schema;
            CONTINUE;
        END IF;

        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.entity_attributes_history ('
            '    id BIGSERIAL PRIMARY KEY,'
            '    user_id TEXT,'
            '    entity_id TEXT NOT NULL,'
            '    attribute TEXT NOT NULL,'
            '    prior_value_text TEXT,'
            '    prior_value_int INT,'
            '    prior_value_float DOUBLE PRECISION,'
            '    prior_value_date DATE,'
            '    prior_provenance TEXT,'
            '    prior_datatype TEXT,'
            '    prior_created_at TIMESTAMP WITH TIME ZONE,'
            '    new_value_text TEXT,'
            '    new_value_int INT,'
            '    new_value_float DOUBLE PRECISION,'
            '    new_value_date DATE,'
            '    new_provenance TEXT,'
            '    action TEXT NOT NULL CHECK (action IN (''superseded'', ''retired'', ''restated'')),'
            '    cause TEXT NOT NULL,'
            '    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()'
            ')', _schema);

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_entity_attributes_history_slot'
            '    ON %I.entity_attributes_history (entity_id, attribute)', _schema);

        RAISE NOTICE 'migration 273: entity_attributes_history ensured in %', _schema;
    END LOOP;
END $$;
