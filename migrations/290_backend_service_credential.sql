-- Migration: backend service secret (FOSS #121)
-- Purpose: the shared secret every trusted caller of the backend API (:8000) presents as
--          X-FaultLine-Backend-Secret — the MCP server, the in-container re-embedder and
--          the backend's own self-calls. When FAULTLINE_BACKEND_SECRET is not set in the
--          environment, the backend mints one random value at boot and stores it HERE, so
--          every process that already reaches the shared database (the only thing they all
--          share without extra compose wiring) can read it. Zero-config, never logged.
--
-- Single row by construction (id is pinned to 1). Operator control-plane data in public,
-- never a tenant schema. Idempotent.

CREATE TABLE IF NOT EXISTS public.backend_service_secret (
    id          SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    secret      TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
