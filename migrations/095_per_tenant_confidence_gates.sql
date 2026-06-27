-- Migration 095: Per-tenant confidence_gates
-- Date: 2026-06-16
-- Purpose: Make confidence_gates a PER-TENANT table so the intent-classification gate
--          self-tuning loop reads/writes the user's OWN schema, never public.
--
-- WHY THIS IS NEEDED
-- ------------------
-- The ingest/query request connection runs with `SET search_path TO {schema}` WITHOUT public
-- (commit 31580f6). confidence_gates existed ONLY in public (migration 043). The runtime
-- previously read+wrote public.confidence_gates (the per-user adaptive intent gate): a public
-- runtime touch-point and a cross-tenant seam. The whole self-tuning loop is now per-tenant:
--   • /classify-intent  reads <schema>.confidence_gates (gate) and writes
--     <schema>.intent_confidence_feedback (signal),
--   • the re_embedder    reads each tenant's intent_confidence_feedback and writes that
--     tenant's confidence_gates,
--   • /confidence-gate   reads <schema>.confidence_gates.
-- public.confidence_gates is now TEMPLATE-ONLY — never read or written at runtime.
--
-- confidence_gates is RUNTIME-POPULATED (the re_embedder writes a row once a tenant has
-- enough feedback). It is NOT seeded — an absent row correctly falls back to GATE_DEFAULT.
-- So this migration only CREATEs the table per-tenant; there is nothing to copy from public.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS only. No DROP, no destructive SQL, no data move.
-- Safe to run repeatedly. Reviewer applies live. The provisioning template
-- (templates/user_schema.sql) also creates this table so new tenants get it at provisioning.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- Mirrors migration 043 (and the provisioning template): per-user adaptive gate,
        -- one row per user_id, default 0.70. Created in the tenant schema (no public ref).
        EXECUTE format($ddl$
            CREATE TABLE IF NOT EXISTS %I.confidence_gates (
                id          SERIAL PRIMARY KEY,
                user_id     UUID NOT NULL UNIQUE,
                threshold   FLOAT DEFAULT 0.70,
                adjusted_at TIMESTAMP DEFAULT now(),
                created_at  TIMESTAMP DEFAULT now()
            )$ddl$, _schema);

        RAISE NOTICE 'Migration 095: ensured confidence_gates in %', _schema;
    END LOOP;
END $$;
