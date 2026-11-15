-- Migration 138: SaaS account TYPE enum + ingest-lock capability gate
-- (CLOSED-SOURCE SaaS layer, Phase 6). Date: 2026-07-04
--
-- Adds a 4-value account_type enum that drives BOTH the create/edit dropdown and the
-- capability gate: ONLY account_type='agent' may lock ingest (the train->serve freeze).
-- A non-agent account is ALWAYS ingest-enabled (the effective_ingest formula ignores the
-- ingest_enabled flags for non-agent types) — so the agent-mode TOGGLE endpoints reject
-- for non-agent accounts and the dashboards hide the TRAIN<->SERVE controls.
--
--   account_type ∈ {standard, advanced, expert, agent}   (DEFAULT 'standard')
--
-- This is orthogonal to the legacy free-text `plan` column (migration 131), which is left
-- exactly as-is. `account_type` is the NEW enum that drives capability + the dropdown.
--
-- Control-plane column in public.accounts (read at request time like status/ingest_enabled).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path. Additive — does
-- NOT weaken the per-tenant DATA rule (SET search_path WITHOUT public).
--
-- Read with: DEV/DESIGN-saas-dashboards-and-observability.md §4 (account tiers).
-- Idempotent (IF NOT EXISTS / guarded constraint add + guarded backfill); safe to re-run.

-- ── the enum column (defaults every existing + new row to 'standard') ───────────────
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT 'standard';

-- Backfill any pre-existing row that predated this column to the safe default. The
-- NOT NULL DEFAULT above already backfills on ADD; this is a defensive no-op for re-runs
-- and for any row that somehow carries a NULL (e.g. a column added without a default).
UPDATE public.accounts SET account_type = 'standard' WHERE account_type IS NULL;

-- ── enum CHECK — guarded so re-runs (and the 131 backfilled row) validate clean ─────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'accounts_account_type_check'
    ) THEN
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_account_type_check
            CHECK (account_type IN ('standard', 'advanced', 'expert', 'agent'));
    END IF;
END $$;
