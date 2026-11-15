-- Migration 302: REQUIRE-EMAIL-ACTIVATION signup gate — CLOSED-SOURCE SaaS layer.
-- Date: 2026-10-26
-- Purpose: an operator valve + the token table for "a new signup cannot sign in until they
--          click a verification link in their email". Closes the fake/throwaway-mailbox hole:
--          every anti-abuse gate upstream (disposable-domain, spam filter, ban list, honeypot)
--          is a BLOCKLIST; this is the one PROOF-OF-DELIVERABILITY gate — the seat is only
--          provisioned after the mailbox answers.
--
-- (1) PLATFORM SETTINGS — the operator valve. DEFAULT FALSE = today's behavior byte-for-byte
--     (backward compatibility is the whole point of the default: flipping this ON is a
--     deliberate operator act). Read as an APPENDED to_jsonb(ps.*) ->> expression (see the
--     🔴 deploy rule in saas/platform_settings.py): a pre-migration database yields SQL NULL,
--     which resolves to the documented default FALSE — the product stays UP and unchanged.
--
-- (2) ACTIVATION TOKENS — single-use, short-lived, HASHED email-verification tokens. Deliberately
--     mirrors migration 163's individual_password_resets (same shape, same contracts): only the
--     sha256 HASH is stored; the plaintext lives only in the emailed link; minting supersedes
--     outstanding siblings; consumption is one guarded UPDATE so a link can never double-spend.
--
-- Read/written with: saas/email_activation.py, saas/user_console.py (/me/signup, /me/verify-email),
-- saas/platform_settings.py, saas/admin.py. Idempotent; safe to re-run.

-- ── (1) PLATFORM_SETTINGS — the new operator valve ────────────────────────────────────
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS require_email_activation BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.platform_settings.require_email_activation IS
    'TRUE = a new /me/signup is held status=pending_email and cannot sign in until the mailbox '
    'clicks the /me/verify-email link. DEFAULT FALSE = today''s behavior byte-for-byte. '
    'Read by saas/platform_settings.require_email_activation().';

-- ── (2) ACTIVATION TOKENS — the click-to-activate secrets ─────────────────────────────
CREATE TABLE IF NOT EXISTS public.individual_email_activations (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The individual login this activation is for. ON DELETE CASCADE so tearing down the
    -- login clears any outstanding activation links.
    individual_id  UUID NOT NULL REFERENCES public.individual_users(id) ON DELETE CASCADE,
    -- sha256 HEX of the high-entropy activation secret. The plaintext secret is NEVER stored.
    token_hash     TEXT NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,          -- short TTL (48h); expired tokens are rejected
    used_at        TIMESTAMPTZ,                   -- set when consumed OR invalidated (single-use)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- A token can never double-spend and cannot collide. The UNIQUE constraint already indexes
-- token_hash — no second index on that column (a duplicate is pure write amplification).
ALTER TABLE public.individual_email_activations
    DROP CONSTRAINT IF EXISTS uq_individual_email_activations_token_hash;
ALTER TABLE public.individual_email_activations
    ADD CONSTRAINT uq_individual_email_activations_token_hash UNIQUE (token_hash);

-- Consume path looks up by token_hash (the UNIQUE constraint's index serves it).
-- 🔴 THE ONE-LIVE-LINK GUARANTEE: at most one OUTSTANDING link per individual, enforced by the
-- DB rather than by mint()'s check-then-act sequence (two concurrent mints would otherwise both
-- pass the invalidate-then-insert and leave two live links). A losing concurrent mint fails its
-- INSERT here and resolves to None (fail-closed → CRIT log → operator manual activate), so the
-- "supersede, never stack" contract holds under concurrency too.
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_live_activation_per_individual
    ON public.individual_email_activations (individual_id)
    WHERE used_at IS NULL;
-- Invalidate-siblings + the resend-interval check scope by individual_id + recency.
CREATE INDEX IF NOT EXISTS idx_individual_email_activations_individual
    ON public.individual_email_activations (individual_id, created_at DESC);

COMMENT ON TABLE public.individual_email_activations IS
    'Individual (/me console) email-activation tokens for the click-to-activate signup gate '
    '(control-plane public.*). Stores ONLY the sha256 token HASH — never the plaintext. '
    'Short-lived, single-use, superseded on a fresh mint. Written by saas/email_activation.py.';
