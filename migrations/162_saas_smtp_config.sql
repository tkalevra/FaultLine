-- Migration 162: platform SMTP (outbound email) config store — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-11
-- Purpose: hold the operator-chosen SMTP relay + its (encrypted) password so the platform can
--          send transactional email (the individual "My Memory" self-serve PASSWORD RESET is
--          the first consumer). The operator configures the endpoint + sent-from address in
--          the OPERATOR console → Settings; this is the CONFIG SURFACE + encrypted store only.
--
-- Control-plane public.* SINGLETON registry (one platform-wide config row), read at request
-- time exactly like migration 153's payment_config / 135's account_llm_config. NEVER seeded
-- into a tenant schema and NEVER read from a tenant's search_path. Additive — does not weaken
-- the per-tenant isolation rule.
--
-- Secret: password_ciphertext holds a Fernet-encrypted blob (saas/crypto.py, master key env
-- SAAS_LLM_CRED_KEY — the SAME crypto realm as the BYO-LLM + payment keys). The plaintext
-- password is NEVER stored and NEVER logged; the read side returns a password_set boolean
-- only (masked), never the secret.
--
-- Read with: saas/smtp_config.py, saas/smtp_sender.py, saas/crypto.py.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

CREATE TABLE IF NOT EXISTS public.smtp_config (
    -- Singleton latch: exactly one platform SMTP config row (id is always TRUE).
    singleton            BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),

    -- Relay endpoint.
    host                 TEXT,                              -- smtp host (e.g. mail.example.com)
    port                 INTEGER NOT NULL DEFAULT 587,      -- 465 (ssl) | 587 (starttls) | 25 (none)
    username             TEXT,                              -- SMTP auth user (may be blank for open relay)

    -- Encrypted credential (write-only; NEVER returned in full).
    password_ciphertext  TEXT,                              -- Fernet ciphertext of the SMTP password

    -- Envelope / sender identity.
    from_address         TEXT,                              -- MAIL FROM / From: address
    from_name            TEXT,                              -- optional display name ("FaultLine")

    -- Transport security: starttls (upgrade on 587) | ssl (implicit TLS on 465) | none (plain 25).
    security             TEXT NOT NULL DEFAULT 'starttls',

    enabled              BOOLEAN NOT NULL DEFAULT FALSE,    -- master send switch (fail-closed off)

    -- Public/base URL the reset links are built from (e.g. https://memory.example.com/dashboard/user.html).
    -- For pre-prod this may be the internal dashboard origin; the sender appends ?token=<secret>.
    reset_link_base_url  TEXT,

    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by           TEXT,                              -- operator identity that last wrote
    last_tested_at       TIMESTAMPTZ                        -- last POST /admin/smtp-config/test
);

-- Seed the singleton row (idempotent) so the read side always finds a row (defaults = the
-- honest 'not configured / disabled' state — nothing sends until the operator fills it in).
INSERT INTO public.smtp_config (singleton)
VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;

COMMENT ON TABLE public.smtp_config IS
    'Platform SMTP (outbound email) config SINGLETON (control-plane public.*). Holds the relay '
    'host/port/user + Fernet-encrypted password (saas/crypto.py) + sent-from address + security '
    'mode + reset_link_base_url. Config surface only; fail-closed disabled until the operator '
    'configures it. Consumed by the individual password-reset flow (saas/smtp_sender.py).';
