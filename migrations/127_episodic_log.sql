-- Migration 127: episodic_log — durable append-only raw-text ingest log (per-tenant)
-- Date: 2026-07-02
--
-- WHY
-- ---
-- The ingest spine keeps only DERIVED structured facts. When extraction is weak
-- (short fragments, misrouted queries, no-triple ramblings), the ORIGINAL words are
-- lost forever. episodic_log captures EVERY ingest input verbatim BEFORE extraction
-- so nothing is unrecoverable and inputs can be re-mined later by a better model
-- (the reextracted_at column + partial index support that future backfill scan).
--
-- This is ADDITIVE and a pure safety net: it does NOT touch the WGM gate, class
-- assignment, or query scope. Writes to it must never block or fail ingest.
--
-- PER-TENANT: the ingest/query request connection runs with `SET search_path TO {schema}`
-- WITHOUT public, so the table must live INSIDE each tenant schema. NO public seed —
-- episodic rows are inherently user-specific (created empty per tenant). This migration
-- applies the CREATE to every already-provisioned tenant schema; NEW tenants get it
-- from the template (src/provisioning/templates/user_schema.sql).
--
-- user_id is stored for traceability (consistent with entity_attributes/staged_facts
-- convention); row isolation is provided by the schema, not by user_id scoping.
--
-- Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS. Safe to run repeatedly.
-- No DROP, no destructive SQL.

-- ============================================================================
-- Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
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
        EXECUTE format($t$
            CREATE TABLE IF NOT EXISTS %I.episodic_log (
                id                   BIGSERIAL   PRIMARY KEY,
                user_id              TEXT        NOT NULL,
                raw_text             TEXT        NOT NULL,
                source               TEXT,
                source_ref           TEXT,
                intent               TEXT,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
                extracted_fact_count INTEGER     DEFAULT NULL,
                reextracted_at       TIMESTAMPTZ DEFAULT NULL
            )
        $t$, _schema);

        -- Partial index supporting the future re-extraction backfill scan.
        EXECUTE format($t$
            CREATE INDEX IF NOT EXISTS idx_episodic_log_reextract
                ON %I.episodic_log (reextracted_at)
                WHERE reextracted_at IS NULL
        $t$, _schema);

        -- Chronological scan index.
        EXECUTE format($t$
            CREATE INDEX IF NOT EXISTS idx_episodic_log_created_at
                ON %I.episodic_log (created_at)
        $t$, _schema);

        RAISE NOTICE 'Migration 127: created episodic_log in %', _schema;
    END LOOP;
END $$;
