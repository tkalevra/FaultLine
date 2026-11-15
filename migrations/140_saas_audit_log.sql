-- Migration 139: SaaS immutable control-plane AUDIT LOG (CLOSED-SOURCE SaaS layer)
-- Date: 2026-07-05
-- Purpose: an APPEND-ONLY, tamper-evident audit trail of security-relevant control-plane
--          actions (key mint/revoke, account status/panic, account-type + seat-limit
--          changes, seat assign/deprovision, dashboard/operator login success+failure).
--          This is the SOC 2 CC7.2/CC7.3 (monitoring) + CC6.1 (privileged-access
--          accountability) evidence trail, and the HIPAA "immutable audit log" primitive.
--
-- DISTINCT from public.saas_event_log (migration 134): that is BEST-EFFORT operational
-- observability (mutable, tenant-surfaceable, request firehose). THIS is the control-plane
-- security ledger — append-only, operator-only, and never surfaced to a tenant.
--
-- CONTROL-PLANE table in public.* (read/written at request time, like public.accounts).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path — additive; it
-- does not weaken the per-tenant DATA rule (SET search_path WITHOUT public).
--
-- Rows carry ONLY classified ids/prefixes/booleans/secret-free detail — NEVER a token,
-- hash, password, or raw auth header. Written by saas/audit.py::record().
--
-- Read with: DEV/SECURITY-saas-multitenancy.md §10 (TB10 audit), DEV/SOC2-READINESS-PLAN.md.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

CREATE TABLE IF NOT EXISTS public.saas_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_type   TEXT,                              -- operator | account | tenant | system
    actor_id     TEXT,                              -- operator/dashboard user id, account_id, or key prefix
    account_id   UUID,                              -- the affected account (control plane), when applicable
    target_type  TEXT,                              -- account | api_key | seat | dashboard_user | session
    target_id    TEXT,                              -- the affected object id (secret-free)
    action       TEXT NOT NULL,                     -- classified event key, e.g. 'key.mint', 'auth.login'
    result       TEXT NOT NULL DEFAULT 'success'
                 CHECK (result IN ('success', 'failure', 'denied')),
    detail       TEXT,                              -- classified, secret-free message
    source_ip    TEXT                               -- client host, when the caller has it
);

-- Hot read path for the operator audit view: newest-first, per account and by action.
CREATE INDEX IF NOT EXISTS idx_saas_audit_log_account_ts
    ON public.saas_audit_log (account_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_saas_audit_log_action_ts
    ON public.saas_audit_log (action, ts DESC);

-- ── APPEND-ONLY enforcement (tamper-evidence) ──────────────────────────────────────
-- A BEFORE UPDATE/DELETE trigger raises, so no code path — not even the app's own DB
-- role — can silently rewrite or erase an audit row. INSERT is the only permitted write.
-- (A privileged DBA can still drop the trigger; that is out of scope for app-layer
-- tamper-evidence and is covered by DB-role least-privilege in the infra runbook.)
CREATE OR REPLACE FUNCTION public.saas_audit_log_immutable()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'public.saas_audit_log is append-only — % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_saas_audit_log_immutable ON public.saas_audit_log;
CREATE TRIGGER trg_saas_audit_log_immutable
    BEFORE UPDATE OR DELETE ON public.saas_audit_log
    FOR EACH ROW EXECUTE FUNCTION public.saas_audit_log_immutable();
