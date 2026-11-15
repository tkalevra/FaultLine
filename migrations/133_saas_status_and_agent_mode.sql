-- Migration 133: SaaS account status lock + agent-mode ingest gate + panic latch
-- (CLOSED-SOURCE SaaS layer, Phase 5). Date: 2026-07-04
--
-- Adds the two fail-closed control surfaces on top of the Phase-1 control plane
-- (migration 131 public.accounts / public.account_users):
--   PART A  — operator "status lock": accounts.status enum {active,suspended,locked,archived}
--             + accounts.lock_origin {NULL,operator,tenant_panic} (break-glass one-way latch).
--   PART B  — tenant "agent mode" ingest gate: accounts.ingest_enabled (whole tenant) +
--             account_users.ingest_enabled (per seat) + account_users.label (name the agent).
--
-- All fields are CONTROL-PLANE columns in public.* (read at request time like the existing
-- account_api_keys). NEVER seeded into a tenant schema, NEVER read from a tenant search_path.
-- Additive — does NOT weaken the per-tenant DATA rule (SET search_path WITHOUT public).
--
-- Read with: DEV/DESIGN-saas-dashboards-and-observability.md §2, §2.1, §3.
-- Idempotent (IF NOT EXISTS / guarded constraint adds); safe to re-run.

-- ── PART A: account status lock + break-glass panic latch ──────────────────────────
-- accounts.status already exists from migration 131 (TEXT NOT NULL DEFAULT 'active', no
-- CHECK). Re-assert the column defensively, then bolt on the enum CHECK idempotently.
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

-- Widen the allowed status set to the Phase-5 enum. Guarded so re-runs don't error.
-- (Any pre-existing row is 'active' from the 131 backfill, so the CHECK validates clean.)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'accounts_status_check'
    ) THEN
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_status_check
            CHECK (status IN ('active', 'suspended', 'locked', 'archived'));
    END IF;
END $$;

-- Break-glass origin marker for the one-way panic latch (§2.1):
--   NULL          — not locked via the latch (or a plain operator status change)
--   'operator'    — operator-originated lock (operator-clearable, as today)
--   'tenant_panic'— account-owner panic; ONLY the operator key can clear it (one-way latch)
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS lock_origin TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'accounts_lock_origin_check'
    ) THEN
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_lock_origin_check
            CHECK (lock_origin IS NULL OR lock_origin IN ('operator', 'tenant_panic'));
    END IF;
END $$;

-- ── PART B: agent-mode ingest gate (train -> serve freeze) ──────────────────────────
-- Whole-tenant ingest switch (tenant-owner settable). true = train (writes on),
-- false = serve (READ-ONLY: recall on, ALL memory writes rejected).
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS ingest_enabled BOOLEAN NOT NULL DEFAULT true;

-- Per-seat ingest switch + human label ("name the agent").
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS ingest_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS label TEXT;
