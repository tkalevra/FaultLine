-- Migration 156: SaaS tiered base + per-seat pricing — record the base package + included-Standard
--                allotments per account_kind (CLOSED-SOURCE SaaS layer)
-- Date: 2026-07-10
-- Purpose: the owner-ratified pricing rebuild — the BASE fee buys the tenant portal AND buys DOWN
--          the per-seat rate, so the SAME seat type costs three prices by account_kind:
--
--            Seat      Individual*   Team ($750 base)   Enterprise ($1500 base)
--            Standard      $15           $12.50               $10
--            Advanced      $40           $35                  $30
--            Expert        $60           $50                  $45
--            Agent        $120          $100                  $80
--            (* Individual = self-serve retail, priced in saas/plans.py, NOT this table.)
--
--          Base packages + included Standard allotments:
--            * ENTERPRISE (account_kind='tenant') → $1500/mo, 5 Standard included, UNLIMITED seats.
--            * TEAM       (account_kind='team')   → $750/mo,  2 Standard included, capped at 15.
--            * SINGLES    (account_kind='individual_house') → no base package (individual retail).
--
--          The AUTHORITATIVE numbers live in code constants (saas/entitlements.py: _RATE_TABLE,
--          BASE_PACKAGE_PRICE_CAD/ENTERPRISE_INCLUDED_STANDARD, TEAM_BASE_PRICE_CAD/
--          TEAM_INCLUDED_STANDARD) so billing needs no per-request DB read. THIS migration records
--          the same ratified figures in a queryable control-plane reference table (auditable /
--          operator-visible) and backfills existing Team accounts to the 15-seat cap.
--
-- Control-plane object in public.* only. NEVER seeded into a tenant schema, NEVER read from a
-- tenant search_path. Idempotent (guarded DDL + ON CONFLICT); safe to re-run / auto-applies.

-- ── 1) the pricing reference table (base package + included Standard, per account_kind) ──────
CREATE TABLE IF NOT EXISTS public.account_kind_pricing (
    account_kind      TEXT PRIMARY KEY,
    base_price_cad    NUMERIC(10,2) NOT NULL DEFAULT 0,
    included_standard INTEGER       NOT NULL DEFAULT 0,
    seat_cap          INTEGER,                    -- NULL = unlimited (Enterprise); 15 = Team
    currency          TEXT          NOT NULL DEFAULT 'CAD',
    note              TEXT,
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ── 2) seed / refresh the ratified figures (idempotent upsert) ───────────────────────────────
INSERT INTO public.account_kind_pricing
    (account_kind, base_price_cad, included_standard, seat_cap, currency, note)
VALUES
    ('tenant',           1500.00, 5, NULL, 'CAD', 'Enterprise — $1500 base, 5 Standard incl., unlimited seats'),
    ('team',              750.00, 2, 15,   'CAD', 'Team — $750 base, 2 Standard incl., capped at 15 seats'),
    ('individual_house',    0.00, 0, NULL, 'CAD', 'Singles — no base package; individual retail (saas/plans.py)')
ON CONFLICT (account_kind) DO UPDATE SET
    base_price_cad    = EXCLUDED.base_price_cad,
    included_standard = EXCLUDED.included_standard,
    seat_cap          = EXCLUDED.seat_cap,
    currency          = EXCLUDED.currency,
    note              = EXCLUDED.note,
    updated_at        = NOW();

-- ── 3) backfill: pin every existing Team account to the 15-seat cap (sane, idempotent) ───────
-- A Team is hard-capped at TEAM_SEAT_CAP total active seats via accounts.seat_limit (which the
-- register/auto-provision paths gate on). Ensure any Team created before this migration matches.
UPDATE public.accounts
   SET seat_limit = 15, updated_at = NOW()
 WHERE account_kind = 'team' AND seat_limit IS DISTINCT FROM 15;
