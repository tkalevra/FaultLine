-- Migration 131: SaaS account/org control plane (CLOSED-SOURCE SaaS layer, Phase 1)
-- Date: 2026-07-04
-- Purpose: add a CUSTOMER/ACCOUNT control-plane layer ABOVE FaultLine's existing
--          per-user data-plane tenancy so FaultLine can be offered as a hosted service.
--
-- These are CONTROL-PLANE registry tables in `public.*` (read at request time, like the
-- existing public.users / public.user_provisioning from migration 050). They are NEVER
-- seeded into a tenant schema and NEVER read from a tenant's search_path. This does NOT
-- weaken the per-tenant DATA rule (SET search_path WITHOUT public) — it is additive.
--
-- Read with: DEV/DESIGN-saas-multitenancy.md §2.1, DEV/SECURITY-saas-multitenancy.md §3.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

-- ── ACCOUNT = the paying customer/org (isolation boundary #1, control plane) ───────
CREATE TABLE IF NOT EXISTS public.accounts (
    account_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,
    slug         TEXT NOT NULL UNIQUE,             -- for vanity subdomain / silo routing
    plan         TEXT NOT NULL DEFAULT 'standard',
    seat_limit   INTEGER NOT NULL DEFAULT 0 CHECK (seat_limit >= 0),
    status       TEXT NOT NULL DEFAULT 'active',   -- active | suspended | closed
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── ACCOUNT API KEYS (hashed at rest — plaintext shown ONCE at mint) ───────────────
-- Keys are high-entropy random, so a deterministic fast hash (SHA-256) is the correct
-- choice: it is cryptographically appropriate for high-entropy secrets AND enables the
-- indexed hash-lookup path (argon2id's per-key salt would make WHERE key_hash=$1
-- impossible). See DEV/SECURITY-saas-multitenancy.md §4.
CREATE TABLE IF NOT EXISTS public.account_api_keys (
    key_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id    UUID NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    key_prefix    TEXT NOT NULL,                   -- e.g. 'flk_a1b2c3d4' — identifiable, NOT secret
    key_hash      TEXT NOT NULL,                   -- SHA-256 hex of the full presented key
    status        TEXT NOT NULL DEFAULT 'active',  -- active | revoked
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ,                     -- optional time-based rotation
    revoked_at    TIMESTAMPTZ,                     -- set on revoke (immediate cutoff)
    last_used_at  TIMESTAMPTZ,
    UNIQUE (key_prefix)
);
-- Partial index on the hot auth lookup path (active, un-revoked keys only).
CREATE INDEX IF NOT EXISTS idx_acct_keys_hash
    ON public.account_api_keys (key_hash)
    WHERE status = 'active' AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_acct_keys_account
    ON public.account_api_keys (account_id);

-- ── ACCOUNT_USERS = the licensed seat roster (the authorization set) ────────────────
-- This is the set bind_tenant() checks against: a claimed user UUID MUST be an ACTIVE
-- row here for THE authenticated account, else cross-account BOLA is denied (403).
CREATE TABLE IF NOT EXISTS public.account_users (
    account_id   UUID NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    user_id      UUID NOT NULL,                    -- the per-user tenant UUID (data plane)
    status       TEXT NOT NULL DEFAULT 'active',   -- active | revoked | deprovisioned
    group_id     UUID,                             -- RESERVED, unused v1 (future teams axis)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, user_id),
    -- A user UUID belongs to AT MOST ONE account (prevents cross-account claim). C8.
    UNIQUE (user_id)
);
CREATE INDEX IF NOT EXISTS idx_account_users_user ON public.account_users (user_id);
CREATE INDEX IF NOT EXISTS idx_account_users_active
    ON public.account_users (account_id) WHERE status = 'active';

-- ── BACKFILL: register the current deployment as account #1 owning existing tenants ─
-- So the flag-on transition never orphans a live tenant. Guarded on "no accounts yet",
-- so re-runs are inert. No API key is minted here (operator mints one via /admin);
-- SAAS_MODE stays OFF by default so this row is dormant until the flag is turned on.
DO $$
DECLARE
    v_account_id UUID;
    v_seats      INTEGER;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.accounts) THEN
        SELECT COUNT(*) INTO v_seats FROM public.user_provisioning;

        INSERT INTO public.accounts (name, slug, plan, seat_limit, status)
        VALUES ('default', 'default', 'legacy', GREATEST(v_seats, 100), 'active')
        RETURNING account_id INTO v_account_id;

        -- Seat every already-provisioned user under the default account.
        INSERT INTO public.account_users (account_id, user_id, status)
        SELECT v_account_id, up.user_id, 'active'
        FROM public.user_provisioning up
        ON CONFLICT (user_id) DO NOTHING;

        RAISE NOTICE 'migration 131: backfilled default account % with % seat(s)',
            v_account_id, v_seats;
    END IF;
END $$;
