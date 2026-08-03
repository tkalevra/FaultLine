-- Migration: Dashboard control-plane tables (FOSS operator console)
-- Date: 2026-08-03
-- Purpose: Backing store for the FOSS webui control plane — seat roster +
--          hashed seat tokens, rotated MCP keys, the operator action log, and
--          the persisted LLM Brain override. All tables live in the SHARED
--          public schema (operator control-plane data, not a user's memory).
--
-- Leak-gate: this is the FOSS line. No reference to the hosted package; the
-- LLM panel concept is "Brain" only.
--
-- Idempotent: every CREATE uses IF NOT EXISTS so re-runs are no-ops.

-- ── Seat roster ──────────────────────────────────────────────────────────────
-- One row per minted seat. The seat token is stored HASHED (sha256 hex); the
-- plaintext is returned exactly once at mint time and never persisted. A seat
-- is revoked by tombstone (active=false, revoked_at set); rows are retained for
-- the operator's own visibility (never hard-deleted by the dashboard).
CREATE TABLE IF NOT EXISTS public.dashboard_seats (
    user_id     UUID        PRIMARY KEY,
    label       TEXT        NOT NULL DEFAULT '',
    token_hash  TEXT        NOT NULL,                -- sha256(token).hexdigest()
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at  TIMESTAMPTZ,                          -- NULL = active
    active      BOOLEAN     NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_dashboard_seats_active
    ON public.dashboard_seats (active);

-- ── Rotated MCP / OpenWebUI keys ─────────────────────────────────────────────
-- The dashboard "rotate MCP key" action writes a new active row and revokes the
-- rest. The MCP server consults this table (constant-time hash compare) so a
-- rotated-then-revoked key stops working immediately. When the table has no
-- active rows the MCP server falls back to the env MCP_API_KEY (back-compat).
CREATE TABLE IF NOT EXISTS public.dashboard_mcp_keys (
    id          SERIAL      PRIMARY KEY,
    key_hash    TEXT        NOT NULL UNIQUE,          -- sha256(key).hexdigest()
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at  TIMESTAMPTZ,                           -- NULL = active
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_dashboard_mcp_keys_active_hash
    ON public.dashboard_mcp_keys (is_active, key_hash);

-- ── Append-only operator action log ──────────────────────────────────────────
-- Minimal local audit trail for the operator's OWN instance visibility
-- (mint / revoke / rotate / llm-change). Append-only by convention; the
-- dashboard never deletes from here.
CREATE TABLE IF NOT EXISTS public.dashboard_action_log (
    id              BIGSERIAL   PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action          TEXT        NOT NULL,
    target_user_id  UUID,
    detail          JSONB
);
CREATE INDEX IF NOT EXISTS idx_dashboard_action_log_ts
    ON public.dashboard_action_log (ts DESC);

-- ── Persisted LLM Brain override (singleton) ────────────────────────────────
-- Single row (id locked to 1). When the operator PUTs a new Brain config from
-- the webui, the dashboard writes it here AND applies it to the running
-- process (os.environ mutation + cached-URL refresh). On backend startup the
-- lifespan loader reads this row back into os.environ BEFORE the LLM URL is
-- resolved, so the override survives restart. The api_key is stored in a form
-- the backend can USE (it must authenticate to the LLM provider); GET /llm
-- only ever reports api_key_set:bool, never the value.
CREATE TABLE IF NOT EXISTS public.dashboard_llm_config (
    id           INTEGER     PRIMARY KEY CHECK (id = 1),
    backend_type TEXT,
    base_url     TEXT,
    model        TEXT,
    api_key      TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Pre-seed the singleton row so PUT can use a plain UPDATE; absence of values
-- means "no operator override yet — env is authoritative".
INSERT INTO public.dashboard_llm_config (id, backend_type, base_url, model, api_key)
VALUES (1, NULL, NULL, NULL, NULL)
ON CONFLICT (id) DO NOTHING;
