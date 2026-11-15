-- Migration 154: SaaS INDIVIDUAL first-month-free trial + owner-ratified paid ladder
--                (CLOSED-SOURCE SaaS layer)
-- Date: 2026-07-09
-- Purpose: apply the owner-ratified individual pricing ladder and the first-month-free trial.
--
--   * PRICING (saas/plans.py): the FREE tier is DROPPED entirely. Three PAID tiers only, riding
--     the migration-139 seat ladder: Pro=$15 (advanced), Power=$40 (expert), Agent=$99 (agent).
--     The $99 Agent plan lines up with the AGENT seat tier (agent-mode voice + largest quota).
--
--   * TRIAL: every self-serve individual gets a FIRST MONTH FREE. We record trial_ends_at on the
--     individual_users row (now + 30 days at signup). The plan is ACTIVE during the trial; the
--     billing views show "trial — converts <date>" instead of an amount; when the window passes
--     the individual simply converts to the paid plan state. The REAL charge is deferred to a
--     future payment adapter (Lemon Squeezy — Merchant of Record; see saas/payment_config.py) —
--     NOTHING is charged here.
--
-- Control-plane objects in public.* (read at request time). NEVER seeded into a tenant schema,
-- NEVER read from a tenant search_path. Idempotent (IF NOT EXISTS / guarded); safe to re-run.

-- ── (A) trial window on the individual login roster ──────────────────────────────────────────
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ;

-- Backfill: give every EXISTING individual an honest 30-day trial measured from signup. Rows
-- already past created_at + 30d simply read as "converted" (trial_ends_at in the past).
UPDATE public.individual_users
   SET trial_ends_at = created_at + INTERVAL '30 days'
 WHERE trial_ends_at IS NULL;

-- ── (B) grant the Singles house account typed-tier entitlement capacity ──────────────────────
-- Individuals now register on the PAID entry tier (advanced/Pro) rather than the free standard
-- tier. register_seat() caps a typed seat against that type's entitlement quantity, so Singles
-- needs generous capacity for advanced/expert/agent. We add these WITHOUT recomputing
-- accounts.seat_limit (Singles carries a large flat cap of 100000 that must NOT collapse to
-- Σ entitlements). Idempotent: only raises quantity, never lowers it.
DO $$
DECLARE
    v_singles UUID;
    v_seats   INTEGER;
    st        TEXT;
BEGIN
    SELECT account_id INTO v_singles
      FROM public.accounts
     WHERE slug = 'singles' AND account_kind = 'individual_house'
     LIMIT 1;
    IF v_singles IS NULL THEN
        RAISE NOTICE 'migration 154: Singles account missing — skipping capacity grant';
        RETURN;
    END IF;

    -- A high ceiling that comfortably covers the individual roster now and as it grows.
    SELECT GREATEST(1000000, COUNT(*)) INTO v_seats
      FROM public.account_users
     WHERE account_id = v_singles;

    FOREACH st IN ARRAY ARRAY['advanced', 'expert', 'agent'] LOOP
        INSERT INTO public.account_entitlements (account_id, seat_type, quantity, base_quantity)
        VALUES (v_singles, st, v_seats, 0)
        ON CONFLICT (account_id, seat_type)
        DO UPDATE SET quantity = GREATEST(public.account_entitlements.quantity, EXCLUDED.quantity),
                      updated_at = NOW();
    END LOOP;

    -- ── (C) drop the free tier for EXISTING individuals: move standard individual seats → Pro ──
    -- The free 'standard' tier is retired. Migrate every EXISTING individual seat still sitting on
    -- standard onto the Pro entry tier (advanced), clearing is_base (a typed seat is never base).
    -- Scoped to individual logins so no tenant seat is touched. They keep their (backfilled) trial.
    UPDATE public.account_users au
       SET seat_type = 'advanced', is_base = FALSE
      FROM public.individual_users iu
     WHERE au.account_id = v_singles
       AND au.user_id = iu.user_id
       AND au.seat_type = 'standard';
END $$;
