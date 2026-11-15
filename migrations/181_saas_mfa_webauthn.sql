-- Migration 181: WebAuthn / FIDO2 passkeys as an MFA factor type — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-15
-- Purpose: extend public.mfa_factors (migration 180) with a third factor_type, 'webauthn', backing
--          hardware security keys + synced passkeys (Bitwarden / 1Password / Windows Hello / Touch
--          ID). Standard WebAuthn — the browser/provider handles the pop-up + credential storage;
--          the server persists, per registered credential: the credential id, the COSE public key,
--          and a signature counter (clone-detection). All base64url TEXT.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS; each CHECK is DROP IF EXISTS then re-ADD (so a re-run
-- converges); partial UNIQUE index IF NOT EXISTS.

ALTER TABLE public.mfa_factors ADD COLUMN IF NOT EXISTS webauthn_credential_id TEXT;  -- base64url
ALTER TABLE public.mfa_factors ADD COLUMN IF NOT EXISTS webauthn_public_key    TEXT;  -- base64url COSE
ALTER TABLE public.mfa_factors ADD COLUMN IF NOT EXISTS webauthn_sign_count    BIGINT NOT NULL DEFAULT 0;
-- AAGUID / transports are useful for display but not required; keep the row lean for now.

-- Widen the factor_type domain (the inline check from 180 is auto-named <table>_<column>_check).
ALTER TABLE public.mfa_factors DROP CONSTRAINT IF EXISTS mfa_factors_factor_type_check;
ALTER TABLE public.mfa_factors ADD CONSTRAINT mfa_factors_factor_type_check
    CHECK (factor_type IN ('totp', 'email', 'webauthn'));

-- Extend the shape guard: a webauthn factor MUST carry a credential id + public key.
ALTER TABLE public.mfa_factors DROP CONSTRAINT IF EXISTS mfa_factor_shape;
ALTER TABLE public.mfa_factors ADD CONSTRAINT mfa_factor_shape CHECK (
    (factor_type = 'totp'     AND secret IS NOT NULL) OR
    (factor_type = 'email'    AND email_address IS NOT NULL) OR
    (factor_type = 'webauthn' AND webauthn_credential_id IS NOT NULL AND webauthn_public_key IS NOT NULL)
);

-- A given authenticator credential may be registered at most ONCE (across all principals) — the
-- WebAuthn credential id is globally unique by construction; a duplicate signals a replay/mistake.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mfa_factors_webauthn_cred
    ON public.mfa_factors (webauthn_credential_id) WHERE webauthn_credential_id IS NOT NULL;
