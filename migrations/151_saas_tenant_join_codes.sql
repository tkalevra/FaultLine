-- Migration 151: SaaS TENANT-MINTED JOIN CODES (CLOSED-SOURCE SaaS layer)
-- Date: 2026-07-09
-- Purpose: the individual→tenant transfer flow needs a code the TENANT hands out — not the raw
--          account_id/slug. A tenant admin mints a join code (tied to their account_id), gives it
--          to the individual, who pastes it into "Join an organization". The transfer request
--          resolves the code → target account_id (clear error if invalid/expired/revoked). Codes
--          are rotatable (mint another) and revocable (soft revoke), so a leaked code is cheaply
--          cut off without touching the account.
--
-- Control-plane table in public.* (read/written at request time like accounts / individual_users).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path — additive, does not
-- weaken the per-tenant DATA rule (SET search_path WITHOUT public). Idempotent; safe to re-run.

CREATE TABLE IF NOT EXISTS public.tenant_join_codes (
    code        TEXT PRIMARY KEY,                       -- the opaque, human-typable join code
    account_id  UUID NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by  TEXT,                                   -- minting dashboard user (secret-free)
    expires_at  TIMESTAMPTZ,                            -- NULL = never expires
    revoked_at  TIMESTAMPTZ,                            -- set on revoke (immediate cutoff)
    note        TEXT
);

-- Hot path: list a tenant's codes; resolve an ACTIVE (unrevoked) code fast.
CREATE INDEX IF NOT EXISTS idx_tenant_join_codes_account
    ON public.tenant_join_codes (account_id);
CREATE INDEX IF NOT EXISTS idx_tenant_join_codes_active
    ON public.tenant_join_codes (code)
    WHERE revoked_at IS NULL;
