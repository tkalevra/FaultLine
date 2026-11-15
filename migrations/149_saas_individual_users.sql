-- Migration 149: SaaS INDIVIDUAL self-serve realm (CLOSED-SOURCE SaaS layer)
-- Date: 2026-07-09
-- Purpose: the self-serve "My Memory" console needs a HUMAN auth realm scoped to ONE
--          individual (exactly one user_id), plus a GATED signup. This is a THIRD human
--          realm alongside dashboard_users (tenant) and the operator realm — same argon2id
--          password model, same TOTP, same signed-session pattern, but the session resolves
--          to a single USER, never an account admin (no seats, no other users).
--
--          Each individual is backed UNDER THE HOOD by a single-seat account (a per-user
--          "house account"), so the existing per-user-schema provisioning, per-user flu_
--          tokens (account_user_tokens), per-seat response_type, and per-account BYO LLM
--          config are all REUSED verbatim. The individual_users row is a thin governance
--          wrapper: login credentials + which user_id it owns + the 3 governance flags.
--
-- Control-plane registry tables in public.* (read at request time like accounts /
-- dashboard_users). NEVER seeded into a tenant schema, NEVER read from a tenant search_path —
-- additive, does not weaken per-tenant isolation. Idempotent; safe to re-run.

-- ── INDIVIDUAL_USERS = the self-serve login roster (one row = one person = one user_id) ──
CREATE TABLE IF NOT EXISTS public.individual_users (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The tenant memory UUID this login OWNS (its per-user schema faultline_<slug>). One login
    -- resolves to EXACTLY this user_id — the session never expresses any other id (BOLA-safe).
    user_id              UUID NOT NULL UNIQUE,
    -- The backing single-seat account (house account). ON DELETE CASCADE so a full account
    -- teardown removes the login too. The individual never sees this account directly.
    account_id           UUID NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    email                TEXT NOT NULL UNIQUE,          -- login id (globally unique, lowercased)
    password_hash        TEXT NOT NULL,                 -- argon2id ($argon2id$...) — NEVER sha256
    totp_secret          TEXT,                          -- base32 TOTP (RFC 6238); NULL until enrolled
    mfa_enabled          BOOLEAN NOT NULL DEFAULT FALSE,
    display_name         TEXT,
    plan                 TEXT NOT NULL DEFAULT 'individual',
    status               TEXT NOT NULL DEFAULT 'active', -- active | suspended | disabled
    -- GOVERNANCE (drives what the console lets them control):
    --   tenant_context ∈ 'none' (standalone) | 'house' (our platform account) | 'real' (corporate)
    --   can_control_endpoint — none/house always true; real defaults false (org grants per seat)
    tenant_context       TEXT NOT NULL DEFAULT 'none',
    can_control_endpoint  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_individual_users_user
    ON public.individual_users (user_id);
CREATE INDEX IF NOT EXISTS idx_individual_users_account
    ON public.individual_users (account_id);

-- ── INDIVIDUAL_INVITES = the signup GATE (self-serve is NOT open; an invite is required) ──
-- Operator (or the owner) mints a code; signup consumes one use. Prevents the self-serve
-- endpoint from being an open abuse/provisioning surface. An env master code
-- (SAAS_INDIVIDUAL_INVITE_CODE) is also honored by the router for owner bootstrap.
CREATE TABLE IF NOT EXISTS public.individual_invites (
    code         TEXT PRIMARY KEY,                       -- the invite code (opaque)
    note         TEXT,                                   -- optional human note ("daniel test seat")
    max_uses     INTEGER NOT NULL DEFAULT 1,
    used_count   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ,                            -- NULL = never expires
    revoked_at   TIMESTAMPTZ
);
