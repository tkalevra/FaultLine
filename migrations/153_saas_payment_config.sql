-- Migration 153: platform payment-provider config store — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-09
-- Purpose: hold the operator-chosen payment PROVIDER + its (encrypted) credentials so a real
--          checkout integration can light up later. This is the CONFIG SURFACE ONLY — no live
--          card processing lives here; the shipped StubPaymentProvider keeps returning
--          'provider_not_configured' until a real adapter (saas/payments.py) is written.
--
-- Control-plane public.* SINGLETON registry (one platform-wide config row), read at request
-- time exactly like migration 135's account_llm_config. NEVER seeded into a tenant schema and
-- NEVER read from a tenant's search_path. Additive — does not weaken the per-tenant rule.
--
-- Secrets: api_key_ciphertext / api_secret_ciphertext / webhook_secret_ciphertext hold
-- Fernet-encrypted blobs (saas/crypto.py, master key env SAAS_LLM_CRED_KEY — the SAME crypto
-- realm as the BYO-LLM keys). Plaintext secrets are NEVER stored and NEVER logged; the read
-- side returns *_set booleans only (masked), never the secret.
--
-- Read with: saas/payment_config.py, saas/payments.py, saas/crypto.py.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

CREATE TABLE IF NOT EXISTS public.payment_config (
    -- Singleton latch: exactly one platform config row (id is always TRUE).
    singleton                 BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),

    -- Chosen provider + operating mode.
    provider                  TEXT NOT NULL DEFAULT 'manual',   -- manual|lemonsqueezy|stripe|paddle|paypal
    mode                      TEXT NOT NULL DEFAULT 'test',     -- test|live

    -- Encrypted credentials (write-only; NEVER returned in full).
    api_key_ciphertext        TEXT,   -- Fernet ciphertext of the provider API/public key
    api_secret_ciphertext     TEXT,   -- Fernet ciphertext of the provider secret key
    webhook_secret_ciphertext TEXT,   -- Fernet ciphertext of the webhook signing secret

    -- Enabled payment methods (provider-dependent), e.g. ["card","paypal","apple_pay"].
    enabled_methods_json      JSONB NOT NULL DEFAULT '["card"]'::jsonb,

    currency                  TEXT NOT NULL DEFAULT 'CAD',
    configured                BOOLEAN NOT NULL DEFAULT FALSE,   -- a real processor is selected

    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by                TEXT,                             -- operator identity that last wrote
    last_tested_at            TIMESTAMPTZ                       -- last POST /admin/payment-config/test
);

-- Seed the singleton row (idempotent) so the read side always finds a row (defaults = the
-- honest 'manual / not configured' state — nothing fakes a charge).
INSERT INTO public.payment_config (singleton)
VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;

COMMENT ON TABLE public.payment_config IS
    'Platform payment-provider config SINGLETON (control-plane public.*). Holds the chosen '
    'provider + mode + Fernet-encrypted credentials (saas/crypto.py). Config surface only — '
    'the StubPaymentProvider never fakes a charge until a real adapter is written.';
