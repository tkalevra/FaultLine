-- Migration 144: SaaS per-tenant origin ACL — allowed browser origins + per-account toggle
-- (CLOSED-SOURCE SaaS layer, Phase 9). Date: 2026-07-07
--
-- Productizes the single global MCP_ALLOWED_ORIGINS env knob into a managed, per-tenant ACL:
--   * public.allowed_origins — rows of permitted browser origins. account_id NULL = a GLOBAL
--     entry (operator-managed, applies to every tenant); a non-NULL account_id = that tenant's
--     own entry (tenant self-service). Uniqueness is per-scope via two partial indexes (a plain
--     UNIQUE(account_id, origin) would let duplicate GLOBAL rows through, since NULL != NULL).
--   * accounts.origin_acl_enabled — the per-tenant ON/OFF toggle. DEFAULT false = today's
--     behavior EXACTLY (no per-tenant origin restriction; only the global CORS union applies).
--     When TRUE, the post-auth gate rejects a browser Origin that is not in (this account's
--     origins ∪ the global entries). OFF is deliberately "insecure" (allow any origin) — an
--     accepted, explicit tenant choice.
--
-- ENFORCEMENT SPLIT (see saas/acl.py + src/mcp/http_server.py):
--   - CORS (pre-auth, no tenant known): a dynamic UNION of env ∪ global ∪ all tenants' origins,
--     purely so a browser doesn't hard-block. NOT the security boundary.
--   - Post-auth per-tenant gate (bearer resolved → account known): the real ACL + the toggle.
--   Tenant data isolation remains the bearer + per-tenant schema — CORS is never that boundary.
--
-- Control-plane public.* (read at request time). NEVER seeded into a tenant schema. Idempotent +
-- forward-only. NO-OP when SAAS_MODE is off (tables sit unread; CORS falls back to the env list).
-- Read with: saas/acl.py, DEV/DESIGN-saas-multitenancy.md, src/mcp/http_server.py (CORS + gate).

-- ── per-tenant toggle ────────────────────────────────────────────────────────────────
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS origin_acl_enabled BOOLEAN NOT NULL DEFAULT false;

-- ── allowed-origins ACL ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.allowed_origins (
    id          BIGSERIAL PRIMARY KEY,
    account_id  UUID REFERENCES public.accounts(account_id) ON DELETE CASCADE,  -- NULL = GLOBAL
    origin      TEXT NOT NULL,                     -- exact scheme://host[:port], lowercased by app
    created_by  TEXT,                              -- 'operator:<u>' | 'tenant:<account>' (audit hint)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-tenant uniqueness (one row per (account, origin)).
CREATE UNIQUE INDEX IF NOT EXISTS uq_allowed_origins_tenant
    ON public.allowed_origins (account_id, origin) WHERE account_id IS NOT NULL;
-- Global uniqueness (one row per origin among the account_id-NULL entries).
CREATE UNIQUE INDEX IF NOT EXISTS uq_allowed_origins_global
    ON public.allowed_origins (origin) WHERE account_id IS NULL;
-- Fast per-tenant + union scans.
CREATE INDEX IF NOT EXISTS idx_allowed_origins_account
    ON public.allowed_origins (account_id);
