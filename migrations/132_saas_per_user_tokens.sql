-- Migration 132: SaaS per-user onboarding tokens (CLOSED-SOURCE SaaS layer, Phase 4)
-- Date: 2026-07-04
-- Purpose: add PER-USER bearer tokens — a credential that authenticates as EXACTLY ONE
--          bound tenant UUID (a single seat). Distinct from the ACCOUNT keys of
--          migration 131 (public.account_api_keys): an account key resolves to
--          `account:<id>` and may act on ANY of its seats (BOLA-gated by bind_tenant);
--          a per-user token resolves to the BOUND UUID itself, activating bind_tenant's
--          "Option A" (principal IS the user → any other claimed X-OpenWebUI-User-Id is a
--          spoof → 403). A leaked per-user token compromises ONE seat, never the account.
--
-- CONTROL-PLANE registry table in `public.*` (read at request time like account_api_keys).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path. Additive — does
-- not weaken the per-tenant DATA rule (SET search_path WITHOUT public).
--
-- Read with: DEV/DESIGN-saas-multitenancy.md, DEV/SECURITY-saas-multitenancy.md.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

-- ── PER-USER TOKENS (hashed at rest — plaintext shown ONCE at mint / onboard) ───────
-- Token format: `flu_<prefix>_<secret>` (flu = FaultLine User). Like account keys, the
-- token is high-entropy random, so a deterministic fast hash (SHA-256) is correct and
-- enables the indexed WHERE token_hash=$1 lookup. token_prefix ('flu_a1b2c3d4') is
-- identifiable, NOT secret — safe to log / list.
CREATE TABLE IF NOT EXISTS public.account_user_tokens (
    token_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id    UUID NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    user_id       UUID NOT NULL,                   -- the ONE seat this token authenticates as
    token_prefix  TEXT NOT NULL,                   -- e.g. 'flu_a1b2c3d4' — identifiable, NOT secret
    token_hash    TEXT NOT NULL,                   -- SHA-256 hex of the full presented token
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at    TIMESTAMPTZ,                     -- set on revoke (immediate cutoff)
    last_used_at  TIMESTAMPTZ,
    UNIQUE (token_prefix),
    -- A token's (account, user) pair must reference a real seat row. ON DELETE CASCADE so
    -- a purged/removed seat's tokens are swept with it (dead credential can never resolve).
    FOREIGN KEY (account_id, user_id)
        REFERENCES public.account_users(account_id, user_id) ON DELETE CASCADE
);
-- Partial index on the hot auth lookup path (un-revoked tokens only).
CREATE INDEX IF NOT EXISTS idx_acct_user_tokens_hash
    ON public.account_user_tokens (token_hash)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_acct_user_tokens_user
    ON public.account_user_tokens (account_id, user_id);
