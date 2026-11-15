-- Migration 145: per-key label on account connection keys (CLOSED-SOURCE SaaS layer).
-- Date: 2026-07-07
--
-- Tenants run MULTIPLE endpoints (OpenWebUI, Claude Desktop, a script, …) and want a
-- SEPARATE bearer per endpoint so any one can be revoked without disturbing the others.
-- The control plane already supports many active keys per account (mint adds, never
-- replaces); this adds an optional human LABEL so a tenant can tell them apart
-- ("owui-prod", "claude-desktop") instead of only by prefix.
--
-- Additive, nullable, idempotent, control-plane public.*. NO-OP when SAAS is off.
ALTER TABLE public.account_api_keys
    ADD COLUMN IF NOT EXISTS label TEXT;
