-- Migration 180: MULTI-FACTOR MFA — a per-principal factors table — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-15
-- Purpose: today MFA is a SINGLE `totp_secret` column on each principal row (public.dashboard_users
--          for operator+tenant, public.individual_users for individual) → exactly ONE authenticator
--          per person, no email OTP, no add/remove. This introduces public.mfa_factors so a principal
--          can register MULTIPLE second factors and add/remove them: multiple authenticator apps
--          (TOTP — covers Bitwarden/1Password/Google Authenticator/etc.) and email one-time codes.
--          (WebAuthn/FIDO2 hardware keys are a later phase; the factor_type CHECK is widened then.)
--
--   public.mfa_factors — one row per registered factor, keyed by (principal_realm, principal_id):
--     * principal_realm ∈ 'dashboard' (operator+tenant share public.dashboard_users)
--                        | 'individual' (public.individual_users). The operator-vs-tenant split is
--       already expressed on the dashboard_users row (role/account_id) — not needed here.
--     * factor_type ∈ 'totp' (secret = base32 RFC-6238 secret) | 'email' (email_address = target).
--     * enabled — FALSE while pending (enrolled but not yet confirmed); TRUE once a code verified.
--       "MFA is on for this principal" ≡ EXISTS an enabled factor (the legacy mfa_enabled column is
--       kept in sync as a denormalized mirror so any remaining legacy reader stays correct).
--   No FK to the principal tables (two possible parents); teardown of a principal is rare and the
--   orphan sweep / account purge removes factors explicitly. Fail-safe by construction: an empty
--   factor set = no MFA required (same as mfa_enabled=FALSE today).
--
-- Idempotent (IF NOT EXISTS + guarded backfill); re-running the whole runner is safe.

CREATE TABLE IF NOT EXISTS public.mfa_factors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_realm TEXT NOT NULL CHECK (principal_realm IN ('dashboard', 'individual')),
    principal_id    UUID NOT NULL,
    factor_type     TEXT NOT NULL CHECK (factor_type IN ('totp', 'email')),
    label           TEXT NOT NULL,                  -- human name ("iPhone", "Bitwarden", "work email")
    secret          TEXT,                           -- base32 TOTP secret (factor_type='totp')
    email_address   TEXT,                           -- lowercased target address (factor_type='email')
    enabled         BOOLEAN NOT NULL DEFAULT FALSE, -- FALSE = pending enroll; TRUE = confirmed + active
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ,
    -- Transient email one-time-code state (factor_type='email'), used for BOTH enroll-confirm and
    -- login verification. Never plaintext: pending_code_hash is HMAC-SHA256(code) under the app
    -- secret. Short expiry + an attempts counter bound brute force on the low-entropy 6-digit code.
    -- Cleared (NULLed) on success. NULL for a totp factor and for an email factor with no code in
    -- flight.
    pending_code_hash       TEXT,
    pending_code_expires_at TIMESTAMPTZ,
    pending_code_attempts   INT NOT NULL DEFAULT 0,
    -- Shape guard: a totp factor MUST carry a secret; an email factor MUST carry a target address.
    CONSTRAINT mfa_factor_shape CHECK (
        (factor_type = 'totp'  AND secret IS NOT NULL) OR
        (factor_type = 'email' AND email_address IS NOT NULL)
    )
);

-- Hot path: list / gate factors for a principal.
CREATE INDEX IF NOT EXISTS idx_mfa_factors_principal
    ON public.mfa_factors (principal_realm, principal_id);
-- "does this principal have any ACTIVE factor" (the login gate) — partial index on enabled rows.
CREATE INDEX IF NOT EXISTS idx_mfa_factors_enabled
    ON public.mfa_factors (principal_realm, principal_id) WHERE enabled;

-- ── Backfill: migrate every already-enrolled principal's single TOTP secret into a factor row,
--    so nobody's existing authenticator stops working. Guarded by NOT EXISTS → idempotent, and
--    scoped to principals that actually have a secret. Label 'Authenticator' (they can rename/add
--    more from the console). enabled mirrors the legacy mfa_enabled bit exactly.
INSERT INTO public.mfa_factors (principal_realm, principal_id, factor_type, label, secret, enabled, created_at)
SELECT 'dashboard', du.id, 'totp', 'Authenticator', du.totp_secret, du.mfa_enabled,
       COALESCE(du.created_at, NOW())
FROM public.dashboard_users du
WHERE du.totp_secret IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.mfa_factors f
      WHERE f.principal_realm = 'dashboard' AND f.principal_id = du.id AND f.factor_type = 'totp'
  );

INSERT INTO public.mfa_factors (principal_realm, principal_id, factor_type, label, secret, enabled, created_at)
SELECT 'individual', iu.id, 'totp', 'Authenticator', iu.totp_secret, iu.mfa_enabled,
       COALESCE(iu.created_at, NOW())
FROM public.individual_users iu
WHERE iu.totp_secret IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.mfa_factors f
      WHERE f.principal_realm = 'individual' AND f.principal_id = iu.id AND f.factor_type = 'totp'
  );
