-- 306: saved-brain snapshots for the tenant brain-picker (DEV/DESIGN-tenant-brain-picker.md).
--
-- Named snapshots of an account's BYO engine-brain config. The picker lists these; 'apply'
-- copies a snapshot back into public.account_llm_config server-side (SSRF re-validated there).
--
-- CONTROL-PLANE public.* registry — exactly like 135_account_llm_config: NEVER seeded into a
-- tenant schema (the migration runner applies control-plane files to public only).
--
-- KEY RULE: the Fernet ciphertext columns are COPIED AS-IS between the two tables — never
-- decrypted, never re-encrypted, never echoed. _encode_fields()/build_llm_config_fields() take
-- PLAINTEXT and encrypt; the picker paths bypass them entirely (a ciphertext fed through
-- _encode_fields would be encrypted a SECOND time and resolve as key_undecryptable).
--
-- Deliberately NOT snapshotted: max_rpm / latency_profile* (live per-account tuning, not brain
-- identity) and last_tested_at (probe bookkeeping). UNIQUE (account_id, name): re-saving a name
-- OVERWRITES that snapshot (idempotent capture).

CREATE TABLE IF NOT EXISTS public.account_llm_config_saved (
    saved_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id               UUID NOT NULL
                               REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    name                     TEXT NOT NULL,          -- display name, unique per account

    -- snapshot of the brain identity (column-for-column the migration-135 subset; keys are
    -- Fernet ciphertext copied as-is, never decrypted, never echoed):
    backend_type             TEXT,
    base_url                 TEXT,
    model                    TEXT,
    api_key_ciphertext       TEXT,
    extra_headers_json       JSONB,
    extra_params_json        JSONB,
    embedding_backend        TEXT,
    embedding_url            TEXT,
    embedding_model          TEXT,
    embedding_key_ciphertext TEXT,
    embedding_dim            INT,

    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (account_id, name)
);

CREATE INDEX IF NOT EXISTS idx_account_llm_config_saved_account
    ON public.account_llm_config_saved (account_id);

COMMENT ON TABLE public.account_llm_config_saved IS
    'Named snapshots of an account''s BYO engine-brain config (tenant brain-picker). '
    'Control-plane public.*; ciphertext copied as-is; apply = server-side copy into '
    'account_llm_config with SSRF re-validation.';
