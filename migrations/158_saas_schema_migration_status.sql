-- Migration 158: per-schema migration STATUS LEDGER (CLOSED-SOURCE SaaS layer).
-- Date: 2026-07-11
--
-- Infrastructure for the per-ACCOUNT isolation grain
-- (DEV/DESIGN-per-account-grain-and-api-subject.md §5.1 — the schema-fanout migration runner).
--
-- WHY: schema-per-tenant's real tax is migration FANOUT — the same DDL must be applied to
-- every tenant schema on every DB box. At thousands of schemas a plain `psql -f` sweep is slow
-- and partial-failure prone: a crash mid-fanout leaves the fleet half-migrated with NO record
-- of where it stopped. This ledger is that record. The runner (saas/migration_runner.py) writes
-- exactly one row per (schema_name, migration_id) so a re-run resumes only the pending/failed
-- rows and an already-applied migration is a checksum-verified no-op.
--
--   schema_name   the tenant schema the migration was applied TO (fanout target).
--   migration_id  the migration's stable id (its NNN_* file basename, no .sql extension).
--   status        pending  — recorded but not yet successfully applied (resume candidate)
--                 applied  — DDL committed in the SAME txn that wrote this row (durable)
--                 failed   — DDL raised + rolled back; `error` holds the reason; retryable
--   checksum      sha256 of the migration sql body. Guards against silently applying DIFFERENT
--                 content under an already-applied migration_id (the runner throws on mismatch).
--   started_at    when this (schema, migration) attempt began.
--   applied_at    when it committed (NULL unless status='applied').
--   error         the failure reason (NULL unless status='failed').
--
-- Lives in `public` (the control plane) — NOT in any tenant schema. Fully additive + idempotent;
-- creating it changes ZERO existing behavior. Existing migrations still apply via
-- docker-entrypoint.sh exactly as before; nothing reads or writes this table until a future
-- phase wires the runner into provisioning/deploy.

CREATE TABLE IF NOT EXISTS public.schema_migration_status (
    schema_name   TEXT NOT NULL,
    migration_id  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'applied', 'failed')),
    checksum      TEXT,
    started_at    TIMESTAMPTZ,
    applied_at    TIMESTAMPTZ,
    error         TEXT,
    PRIMARY KEY (schema_name, migration_id)
);

-- Fast "what's left for this schema" resume scan.
CREATE INDEX IF NOT EXISTS idx_schema_migration_status_status
    ON public.schema_migration_status (status);
