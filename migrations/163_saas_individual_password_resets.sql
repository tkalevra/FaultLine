-- Migration 163: individual "My Memory" self-serve PASSWORD RESET tokens — CLOSED-SOURCE SaaS.
-- Date: 2026-07-11
-- Purpose: back the email-based forgot-password flow for the individual console (/me realm).
--          A reset is a short-lived, single-use, HASHED secret mailed to the individual; this
--          table stores ONLY the token HASH (sha256 of a high-entropy secret) — never the
--          plaintext, which lives only in the emailed link and is discarded server-side.
--
-- Control-plane public.* registry table, read at request time like individual_users. NEVER
-- seeded into a tenant schema, NEVER read from a tenant search_path. Additive.
--
-- Read/written with: saas/password_reset.py. Idempotent; safe to re-run.

CREATE TABLE IF NOT EXISTS public.individual_password_resets (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The individual login this reset is for. ON DELETE CASCADE so tearing down the login
    -- clears any outstanding resets.
    individual_id  UUID NOT NULL REFERENCES public.individual_users(id) ON DELETE CASCADE,
    -- sha256 HEX of the high-entropy reset secret. The plaintext secret is NEVER stored.
    token_hash     TEXT NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,          -- short TTL (~30 min); expired tokens are rejected
    used_at        TIMESTAMPTZ,                   -- set when consumed OR invalidated (single-use)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Consume path looks up by token_hash (unused + unexpired).
CREATE INDEX IF NOT EXISTS idx_individual_pw_resets_token
    ON public.individual_password_resets (token_hash);
-- Invalidate-siblings path scopes by individual_id.
CREATE INDEX IF NOT EXISTS idx_individual_pw_resets_individual
    ON public.individual_password_resets (individual_id);

COMMENT ON TABLE public.individual_password_resets IS
    'Individual (/me console) self-serve password-reset tokens (control-plane public.*). Stores '
    'ONLY the sha256 token HASH — never the plaintext. Short-lived, single-use, invalidated on '
    'use and on a fresh request. Written by saas/password_reset.py.';
