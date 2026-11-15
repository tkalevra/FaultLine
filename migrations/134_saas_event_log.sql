-- Migration 134: SaaS structured event log (CLOSED-SOURCE SaaS layer, observability)
-- Date: 2026-07-04
-- Purpose: a control-plane, tenant-tagged structured log that powers BOTH the operator
--          log firehose (GET /admin/logs) AND the future per-tenant "advanced log" tab.
--
-- This is a CONTROL-PLANE table in `public.*` (like public.accounts / public.account_users
-- from migration 131). It is NEVER seeded into a tenant schema and NEVER read from a
-- tenant's search_path — it does NOT weaken the per-tenant DATA rule (SET search_path
-- WITHOUT public). Additive only.
--
-- Rows are BEST-EFFORT observability, written by saas/eventlog.py::emit() — an emit
-- failure is swallowed and never blocks a request. Only prefixes/ids/booleans/classified
-- messages are stored; NEVER secrets (tokens, hashes, raw auth headers, blobs). The
-- `redacted` flag marks a row as tenant-safe (surfaceable in the per-tenant console).
--
-- Read with: DEV/DESIGN-saas-multitenancy.md. Idempotent (IF NOT EXISTS everywhere).

CREATE TABLE IF NOT EXISTS public.saas_event_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    level       TEXT NOT NULL DEFAULT 'INFO' CHECK (level IN ('INFO', 'WARN', 'CRIT')),
    account_id  UUID,                    -- control-plane account (NULL = pre-auth / system)
    user_id     UUID,                    -- the per-user tenant UUID (NULL = account-level)
    event_type  TEXT,                    -- classified event key, e.g. 'request', 'auth.deny'
    message     TEXT,                    -- classified, secret-free message
    redacted    BOOLEAN NOT NULL DEFAULT FALSE  -- TRUE = tenant-safe (per-tenant console)
);

-- Hot read path for the per-account log tab: newest-first within an account.
CREATE INDEX IF NOT EXISTS idx_saas_event_log_account_ts
    ON public.saas_event_log (account_id, ts DESC);

-- Firehose severity filter (WARN/CRIT triage).
CREATE INDEX IF NOT EXISTS idx_saas_event_log_level
    ON public.saas_event_log (level);
