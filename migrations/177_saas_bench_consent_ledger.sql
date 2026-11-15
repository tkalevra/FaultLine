-- Migration 177: benchmark CONSENT ledger — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-14
-- Purpose: an APPEND-ONLY ledger of every explicit consent a user gives to run the self-serve
--          benchmark. Because a run spends the user's OWN model compute/quota, we record the
--          consent event (who, when, which model, request origin) so there is an auditable trail
--          that consent was captured BEFORE the run. One row per accepted run.
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path. Additive; idempotent (IF NOT EXISTS everywhere); safe to re-run. Append-only by
-- convention (writers only INSERT). Read by the operator console audit view (require_operator).
--
-- Read/written with: saas/community_bench.py (record_consent / list_consent_ledger).

CREATE TABLE IF NOT EXISTS public.bench_consent_ledger (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id   UUID,                                  -- owning account (control-plane)
    user_id      UUID,                                  -- consenting seat/user
    model_label  TEXT,                                  -- the model the run will exercise
    consent      BOOLEAN NOT NULL DEFAULT true,         -- explicit acknowledgement captured
    ip           TEXT,                                  -- request origin (audit)
    user_agent   TEXT,                                  -- request UA (audit, truncated)
    consented_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Audit read: newest first, and per-user lookups.
CREATE INDEX IF NOT EXISTS idx_bench_consent_consented_at
    ON public.bench_consent_ledger (consented_at DESC);
CREATE INDEX IF NOT EXISTS idx_bench_consent_user
    ON public.bench_consent_ledger (user_id, consented_at DESC);

COMMENT ON TABLE public.bench_consent_ledger IS
    'Append-only ledger of explicit consent to run the self-serve benchmark (control-plane '
    'public.*). One row per accepted run: account_id, user_id, model_label, ip, user_agent, '
    'consented_at. A run spends the user''s OWN model quota, so consent is recorded BEFORE it '
    'starts. Read by the operator audit view; NEVER exposed on the public leaderboard.';
