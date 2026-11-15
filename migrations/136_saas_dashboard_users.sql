-- Migration 136: SaaS TENANT-CONSOLE human auth realm (CLOSED-SOURCE SaaS layer)
-- Date: 2026-07-04
-- Purpose: the username/password/MFA login the tenant DASHBOARD needs. This is a
--          HUMAN auth realm, completely separate from the M2M account API keys
--          (public.account_api_keys, flk_/flu_). Different table, different hash,
--          different threat model:
--            * account_api_keys.key_hash = SHA-256 (high-entropy random keys).
--            * dashboard_users.password_hash = argon2id (SLOW, salted) — human
--              passwords are low-entropy, so a fast hash would be a crackable breach.
--
-- Control-plane registry table in public.* (read at request time like accounts /
-- account_users from migration 131). NEVER seeded into a tenant schema and NEVER read
-- from a tenant's search_path — additive, does not weaken the per-tenant isolation rule.
--
-- Read with: DEV/DESIGN-saas-multitenancy.md, DEV/SECURITY-saas-multitenancy.md.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

-- ── DASHBOARD_USERS = the human login roster for a tenant account ──────────────────
-- Each row is a person who can sign in to the tenant dashboard for ONE account. The
-- session issued at login carries this account_id; every tenant-console endpoint derives
-- its scope FROM the session, never from client input (BOLA-safe on the human plane).
CREATE TABLE IF NOT EXISTS public.dashboard_users (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     UUID NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,                  -- argon2id ($argon2id$...) — NEVER sha256
    totp_secret    TEXT,                           -- base32 TOTP secret (RFC 6238); NULL until enrolled
    mfa_enabled    BOOLEAN NOT NULL DEFAULT FALSE, -- gates session issuance until TOTP verified
    role           TEXT NOT NULL DEFAULT 'admin',  -- dashboard role (admin | viewer | ...)
    status         TEXT NOT NULL DEFAULT 'active', -- active | suspended | disabled
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at  TIMESTAMPTZ
);

-- Hot lookup path: username → row (login). UNIQUE(username) already backs this, but the
-- account fan-out (list a tenant's dashboard users) wants its own index.
CREATE INDEX IF NOT EXISTS idx_dashboard_users_account
    ON public.dashboard_users (account_id);
CREATE INDEX IF NOT EXISTS idx_dashboard_users_active
    ON public.dashboard_users (account_id) WHERE status = 'active';
