-- Migration 164: two-tier password-reset APPROVAL workflow — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-11
-- Purpose: back DEV/DESIGN-reset-approval-workflow.md. A password reset can be held PENDING an
--          approver's decision before a link is emailed / token activated. Two tiers:
--            Tier 1  END-USER (seat) reset  → TENANT-ADMIN approval  (per-tenant flag, default OFF).
--            Tier 2  TENANT-ADMIN reset     → OPERATOR (global) approval (per-tenant, default ON).
--
-- LOCK SCOPE (load-bearing): the gate holds ONLY the control-plane LOGIN/credential. The DATA
-- plane — the seat's flu_ MCP token, recall/remember, the memory — is NEVER touched by a pending
-- reset. This migration only adds control-plane public.* tables/columns; it does NOT alter any
-- per-tenant DATA schema, token, or the search_path rule.
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path. Additive; idempotent (IF NOT EXISTS everywhere); safe to re-run.
--
-- Read/written with: saas/reset_approvals.py (state machine + flags), saas/dashboard_reset.py
-- (dashboard-user reset tokens for Tier 2), saas/password_reset.py (existing seat tokens).

-- ── (1) Per-account policy flags (on public.accounts, the billing/contract unit) ───────
--   require_user_reset_approval               — Tier 1 gate. DEFAULT FALSE (opt-in). When ON an
--                                                end-user /me forgot-password is held for the
--                                                tenant admin instead of emailing directly.
--   require_operator_approval_for_admin_reset — Tier 2 gate. DEFAULT TRUE (belt-and-suspenders).
--                                                A tenant-admin self-serve reset is held for the
--                                                operator. Per-tenant, operator-overridable.
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS require_user_reset_approval               BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS require_operator_approval_for_admin_reset BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN public.accounts.require_user_reset_approval IS
    'Tier 1: hold an END-USER (seat) password reset PENDING tenant-admin approval. Default OFF '
    '(opt-in). Read at request time by saas/reset_approvals.py.';
COMMENT ON COLUMN public.accounts.require_operator_approval_for_admin_reset IS
    'Tier 2: hold a TENANT-ADMIN self-serve password reset PENDING operator approval. Default ON, '
    'per-tenant + operator-overridable. Read at request time by saas/reset_approvals.py.';

-- ── (2) Dashboard-user reset tokens (mirror of individual_password_resets, for Tier 2) ─
-- The tenant-admin (dashboard_users) self-serve forgot-password needs the SAME hashed / single-
-- use / short-lived token machinery the individual flow has. Kept in its own table because the
-- FK target differs (dashboard_users, not individual_users). Stores ONLY the sha256 token HASH.
CREATE TABLE IF NOT EXISTS public.dashboard_password_resets (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dashboard_user_id UUID NOT NULL REFERENCES public.dashboard_users(id) ON DELETE CASCADE,
    token_hash        TEXT NOT NULL,               -- sha256 HEX of the secret; plaintext NEVER stored
    expires_at        TIMESTAMPTZ NOT NULL,        -- short TTL (~30 min)
    used_at           TIMESTAMPTZ,                 -- set on consume OR invalidation (single-use)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dashboard_pw_resets_token
    ON public.dashboard_password_resets (token_hash);
CREATE INDEX IF NOT EXISTS idx_dashboard_pw_resets_user
    ON public.dashboard_password_resets (dashboard_user_id);

COMMENT ON TABLE public.dashboard_password_resets IS
    'Tenant-admin / operator (dashboard_users) self-serve password-reset tokens (control-plane '
    'public.*). sha256 token HASH only; short-lived, single-use, invalidated on use/fresh request. '
    'Written by saas/dashboard_reset.py.';

-- ── (3) The approval QUEUE / state machine ────────────────────────────────────────────
-- One row per held reset request. The DASHBOARD PENDING QUEUE is authoritative; email is only
-- the notify. State: pending → (approved | denied | expired). On approval the router mints+emails
-- the reset token (gated here, never weakened). approver_realm partitions the two queues.
CREATE TABLE IF NOT EXISTS public.reset_approvals (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     UUID NOT NULL,                  -- the account the reset belongs to (queue scope)
    approver_realm TEXT NOT NULL,                  -- 'tenant' (Tier 1) | 'operator' (Tier 2)
    target_kind    TEXT NOT NULL,                  -- 'seat_login' (individual_users) | 'dashboard_user'
    target_id      UUID NOT NULL,                  -- individual_id OR dashboard_user_id
    target_email   TEXT,                           -- denormalized recipient (queue display + notify)
    status         TEXT NOT NULL DEFAULT 'pending',-- pending | approved | denied | expired
    requested_ip   TEXT,                           -- request source IP (audit)
    decided_by     TEXT,                           -- approver identity (dashboard_user_id / 'operator')
    decided_at     TIMESTAMPTZ,
    expires_at     TIMESTAMPTZ NOT NULL,           -- auto-expire window (default ~5 days)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT reset_approvals_realm_chk  CHECK (approver_realm IN ('tenant', 'operator')),
    CONSTRAINT reset_approvals_kind_chk   CHECK (target_kind IN ('seat_login', 'dashboard_user')),
    CONSTRAINT reset_approvals_status_chk CHECK (status IN ('pending', 'approved', 'denied', 'expired'))
);
-- Queue reads: pending rows for an account / realm, newest first.
CREATE INDEX IF NOT EXISTS idx_reset_approvals_account_status
    ON public.reset_approvals (account_id, status);
CREATE INDEX IF NOT EXISTS idx_reset_approvals_realm_status
    ON public.reset_approvals (approver_realm, status);
-- Supersede-siblings + expiry sweeps scope by target.
CREATE INDEX IF NOT EXISTS idx_reset_approvals_target
    ON public.reset_approvals (target_kind, target_id, status);

COMMENT ON TABLE public.reset_approvals IS
    'Two-tier password-reset APPROVAL queue (control-plane public.*). One row per held reset: '
    'approver_realm tenant (end-user→tenant) | operator (tenant-admin→operator); state '
    'pending→approved|denied|expired. The dashboard queue is authoritative; email only notifies. '
    'Gates token activation — never weakens it. Written by saas/reset_approvals.py.';
