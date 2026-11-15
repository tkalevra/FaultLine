-- Migration 135: per-account BYO-LLM (engine-brain) config — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-04
-- Purpose: let a paying account bring its OWN LLM endpoint for the ENGINE BRAIN — the
--          model FaultLine calls INTERNALLY for extraction / atomize / validation /
--          correction / growth (this REPLACES the process-global WGM_LLM_MODEL, PER
--          TENANT). This is NOT the chat model; it is the internal reasoning brain.
--
-- This is a CONTROL-PLANE registry table in public.* (read at request time, exactly like
-- migration 131's account tables). It is NEVER seeded into a tenant schema and NEVER read
-- from a tenant's search_path. Additive — does not weaken the per-tenant data rule.
--
-- Secrets: api_key_ciphertext / embedding_key_ciphertext hold Fernet-encrypted blobs
-- (saas/crypto.py, master key from env SAAS_LLM_CRED_KEY). Plaintext keys are NEVER stored
-- and NEVER logged. base_url / embedding_url are SSRF-guarded at save AND at resolve.
--
-- Read with: DEV/DESIGN-saas-multitenancy.md (§ BYO-LLM), saas/llm_config.py, saas/crypto.py.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

CREATE TABLE IF NOT EXISTS public.account_llm_config (
    account_id               UUID PRIMARY KEY
                                REFERENCES public.accounts(account_id) ON DELETE CASCADE,

    -- ── engine-brain LLM (replaces WGM_LLM_MODEL per tenant) ──────────────────────
    backend_type             TEXT,           -- openwebui|ollama|openai|anthropic|lm_studio|groq|localai|raw
    base_url                 TEXT,           -- SSRF-guarded https endpoint (the tenant's brain)
    model                    TEXT,           -- model id the tenant's brain answers as
    api_key_ciphertext       TEXT,           -- Fernet ciphertext of the brain key (write-only)
    extra_headers_json       JSONB,          -- optional extra request headers (e.g. org id)
    extra_params_json        JSONB,          -- advanced params: temperature/top_p/top_k/penalties

    -- ── BYO-embedding (storage + resolution ONLY this pass — NOT yet wired) ────────
    embedding_backend        TEXT DEFAULT 'internal',  -- 'internal' = use FaultLine's own embedder
    embedding_url            TEXT,           -- SSRF-guarded https endpoint
    embedding_model          TEXT,
    embedding_key_ciphertext TEXT,           -- Fernet ciphertext of the embedding key (write-only)
    embedding_dim            INT,            -- MUST match the per-user Qdrant collection dim

    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_tested_at           TIMESTAMPTZ     -- last successful extraction-probe (POST /admin/llm-config/test)
);

-- No secondary index needed: account_id is the PK and the only lookup key (one config
-- per account, joined from the account resolved on the hot auth path).

COMMENT ON TABLE public.account_llm_config IS
    'Per-account BYO engine-brain LLM config (replaces WGM_LLM_MODEL per tenant). '
    'Control-plane public.* registry; never seeded into a tenant schema. '
    'Keys are Fernet ciphertext (saas/crypto.py); base_url/embedding_url are SSRF-guarded.';
