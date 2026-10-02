-- Migration: persisted operator credential (FOSS #145)
-- Purpose: when FAULTLINE_ADMIN_TOKEN is not set in the environment, the backend mints ONE
--          operator token on its first boot, prints it once, and stores only its SHA-256 here,
--          so the token survives restarts (it used to live only in the process environment and
--          rotated on every boot). Rotate with: python -m src.api.operator_token --rotate
--
-- Single row by construction (id is pinned to 1). Operator control-plane data in public,
-- never a tenant schema. Hash only: a database dump does not reveal the credential. Idempotent.

CREATE TABLE IF NOT EXISTS public.operator_admin_token (
    id            SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    token_sha256  TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
