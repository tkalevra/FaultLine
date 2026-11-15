-- Migration 155: SaaS "Team" account tier — the cliff-filler between Individual and Enterprise
--                (CLOSED-SOURCE SaaS layer)
-- Date: 2026-07-09
-- Purpose: add a THIRD account_kind, 'team', between the self-serve Individual ($99 retail) and
--          the Enterprise 'tenant' ($1500/mo base package).
--
-- Owner-ratified model:
--   * ENTERPRISE (account_kind='tenant')           → $1500/mo base (5 Standard incl.) + UNLIMITED
--                                                     à-la-carte seats. SSO / dedicated isolation
--                                                     / SLA live here. UNCHANGED by this migration.
--   * TEAM       (account_kind='team', NEW)         → NO base package ($0 base), pure per-seat
--                                                     à-la-carte at the seat rates
--                                                     (Standard $10 / Advanced $25 / Expert $50 /
--                                                     Agent $80), self-serve, capped at 15 total
--                                                     ACTIVE seats. No SSO / dedicated / SLA.
--   * SINGLES    (account_kind='individual_house')  → EXEMPT individual-house grouping. UNCHANGED.
--
-- The 15-seat Team cap is enforced in code (saas/seats.py register_seat + a pinned
-- accounts.seat_limit = 15 at create, which the standard/auto-provision paths already gate on)
-- and mirrored by saas/entitlements.TEAM_SEAT_CAP — no per-row cap column is needed. A Team gets
-- NO base-5 $1500 entitlement seeded (saas/accounts.create_account), and its billing_view shows a
-- $0 base line + per-seat lines (saas/entitlements._team_billing_view).
--
-- This migration ONLY widens the account_kind CHECK constraint to admit 'team'. No account is
-- backfilled to 'team' (existing accounts are correctly 'tenant' / 'individual_house'; a Team is
-- created going forward via the operator console). The DEFAULT stays 'tenant'.
--
-- Control-plane object in public.* (read at request time). NEVER seeded into a tenant schema,
-- NEVER read from a tenant search_path. Idempotent (guarded); safe to re-run.

-- ── widen the account_kind CHECK to admit 'team' ─────────────────────────────────────────────
-- The column + the original 2-value CHECK were added by migration 152. Drop-and-recreate the
-- constraint (idempotent: DROP IF EXISTS, then guarded ADD) to include 'team'.
ALTER TABLE public.accounts
    DROP CONSTRAINT IF EXISTS accounts_account_kind_check;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'accounts_account_kind_check'
    ) THEN
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_account_kind_check
            CHECK (account_kind IN ('tenant', 'team', 'individual_house'));
    END IF;
END $$;
