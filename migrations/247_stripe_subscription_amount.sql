-- Migration 247: Stripe subscription RECURRING AMOUNT — CLOSED-SOURCE SaaS layer.
-- Date: 2026-08-12
-- Purpose: give public.stripe_subscriptions (migration 236) the four columns that state WHAT IS
--          BEING CHARGED. The snapshot recorded `price_id` — an opaque pointer — and nothing
--          else, so the operator billing dashboard could not answer "how much" without asking
--          Stripe. Three dashboard reads referenced `s.amount` / `s.currency` / `s.first_past_due_at`
--          columns that HAVE NEVER EXISTED, so the STRIPE BILLING view raised
--          `UndefinedColumn: column s.amount does not exist` on every load. This migration adds
--          the two that a snapshot legitimately owns (amount + currency) plus the billing
--          interval, WITHOUT which summing rows silently reports an annual price as monthly
--          revenue.
--
-- ⚠️ NULLABLE ON PURPOSE, AND THERE IS NO `DEFAULT 0`.
--    NULL means "not observed yet", which the read side renders as UNKNOWN. A `DEFAULT 0` would
--    turn every un-backfilled row into a confident, wrong CA$0.00 — the precise failure this
--    migration exists to end. A money figure that cannot be substantiated must read UNKNOWN,
--    never zero.
--
-- ⚠️ NO BACKFILL IS POSSIBLE HERE, AND THAT IS STATED RATHER THAN FAKED.
--    The recurring amount lives on the Stripe Price inside the Subscription object; recovering it
--    for an existing row would require a live Stripe API call, which a migration must never make.
--    Existing rows therefore stay NULL until their next subscription event re-upserts them
--    (saas/stripe_state.py::upsert_subscription). In the meantime the dashboard resolves a
--    SUBORDINATE, clearly-labelled figure from the shipped tier catalog by exact price_id /
--    plan_id equality, and marks it `amount_source='catalog'` so an operator can always tell a
--    Stripe-observed amount from a locally-derived one.
--
-- WHERE THE VALUE COMES FROM (evidence-grounded against Stripe's primary API reference):
--   * Subscription Item object — `price` (object) "The price the customer is subscribed to" and
--     `quantity` (integer, NULLABLE) — https://docs.stripe.com/api/subscription_items/object
--   * Price object — `unit_amount` (integer, NULLABLE) "The unit amount in the smallest currency
--     unit to be charged, represented as a whole integer if possible. Only set if
--     `billing_scheme=per_unit`."; `currency` (enum) "Three-letter ISO currency code, in
--     lowercase."; `recurring.interval` / `recurring.interval_count`
--     — https://docs.stripe.com/api/prices/object
--   Because `unit_amount` is NULL for a `billing_scheme=tiered` price and `quantity` is nullable,
--   an UNRESOLVABLE amount is a NORMAL Stripe state, not an error — hence nullable columns and an
--   UNKNOWN render rather than a fabricated zero.
--
-- Column naming: `billing_interval` / `billing_interval_count`, NOT `interval` — INTERVAL is a
-- PostgreSQL type name and a bare `interval` column forces quoting at every touch.
--
-- Control-plane public.* table. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path — additive only.
-- Read with: saas/stripe_state.py, saas/billing_dashboard_routes.py.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

ALTER TABLE public.stripe_subscriptions
    ADD COLUMN IF NOT EXISTS amount_cents           INT,
    ADD COLUMN IF NOT EXISTS currency               TEXT,
    ADD COLUMN IF NOT EXISTS billing_interval       TEXT,
    ADD COLUMN IF NOT EXISTS billing_interval_count INT;

-- A negative recurring amount is never a valid Stripe price. NOT VALID is deliberately NOT used:
-- every existing row is NULL, so the constraint can be added and validated immediately.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass('public.stripe_subscriptions')
          AND conname  = 'stripe_subscriptions_amount_nonneg'
    ) THEN
        ALTER TABLE public.stripe_subscriptions
            ADD CONSTRAINT stripe_subscriptions_amount_nonneg
            CHECK (amount_cents IS NULL OR amount_cents >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass('public.stripe_subscriptions')
          AND conname  = 'stripe_subscriptions_interval_count_positive'
    ) THEN
        ALTER TABLE public.stripe_subscriptions
            ADD CONSTRAINT stripe_subscriptions_interval_count_positive
            CHECK (billing_interval_count IS NULL OR billing_interval_count >= 1);
    END IF;
END
$$;

COMMENT ON COLUMN public.stripe_subscriptions.amount_cents IS
    'RECURRING amount per billing interval, smallest currency unit, as observed on the Stripe '
    'Subscription (sum over items of price.unit_amount * quantity). NULL = NOT OBSERVED YET — '
    'render as UNKNOWN, never as 0. NULL is also the correct value for a tiered/metered price, '
    'where Stripe itself sets unit_amount to null.';
COMMENT ON COLUMN public.stripe_subscriptions.currency IS
    'ISO-4217 code for amount_cents, stored lowercase exactly as Stripe reports it. NULL whenever '
    'amount_cents is NULL — an unknown amount has no known currency, and one is never invented.';
COMMENT ON COLUMN public.stripe_subscriptions.billing_interval IS
    'Stripe price.recurring.interval (day|week|month|year). Load-bearing for MRR: without it, '
    'summing amount_cents reports an ANNUAL price as monthly revenue. A non-monthly row is '
    'counted and surfaced separately by the dashboard, never silently folded into the total.';
COMMENT ON COLUMN public.stripe_subscriptions.billing_interval_count IS
    'Stripe price.recurring.interval_count (e.g. 3 with interval=month = quarterly).';
