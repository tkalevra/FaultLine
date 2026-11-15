-- Migration 236: Stripe STATE SNAPSHOT — CLOSED-SOURCE SaaS layer.
-- Date: 2026-08-11
-- Purpose: local snapshot of Stripe customer + subscription state — the dashboard's READ
--          source and the authoritative input to retention._is_subscribed (the purge gate).
--          The local billing ledger (public.account_ledger) stays AUTHORITATIVE for money;
--          drift between this snapshot and Stripe is SURFACED by the dashboard, never hidden.
--
-- Snapshot upserts are IDEMPOTENT on the Stripe object id (ON CONFLICT DO UPDATE), because
-- Stripe does NOT guarantee event ordering — an out-of-order 'updated' must not clobber a
-- 'deleted', so the snapshot is a last-writer-wins-by-event-payload, reconciled by the
-- webhook processor ordering.
--
-- Control-plane public.* tables (read/written at request time, like public.payment_config).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path — additive.
--
-- Read with: saas/stripe_state.py (updaters + reads), saas/retention.py (_is_subscribed),
--            saas/stripe_routes.py (dashboard views).
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

-- ── customers ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.stripe_customers (
    customer_id    TEXT PRIMARY KEY,                  -- stripe customer id (cus_...)
    account_id     UUID,                              -- our account (nullable for individuals)
    user_id        UUID,                              -- our user (the individual seat)
    email          TEXT,
    display_name   TEXT,
    livemode       BOOLEAN NOT NULL,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_stripe_customers_account ON public.stripe_customers (account_id);
CREATE INDEX IF NOT EXISTS idx_stripe_customers_user    ON public.stripe_customers (user_id);

-- ── subscriptions ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.stripe_subscriptions (
    subscription_id       TEXT PRIMARY KEY,           -- stripe subscription id (sub_...)
    customer_id           TEXT NOT NULL REFERENCES public.stripe_customers(customer_id) ON DELETE CASCADE,
    account_id            UUID,                       -- our account (nullable for individuals)
    user_id               UUID,                       -- our user (the individual seat)
    status                TEXT NOT NULL,              -- trialing|active|past_due|canceled|unpaid|incomplete
    plan_id               TEXT,                       -- our plan id (from metadata)
    price_id              TEXT,                       -- stripe price id (price_...)
    current_period_start  TIMESTAMPTZ,
    current_period_end    TIMESTAMPTZ,
    cancel_at_period_end  BOOLEAN NOT NULL DEFAULT FALSE,
    canceled_at           TIMESTAMPTZ,
    livemode              BOOLEAN NOT NULL,
    first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_stripe_subs_customer  ON public.stripe_subscriptions (customer_id);
CREATE INDEX IF NOT EXISTS idx_stripe_subs_user      ON public.stripe_subscriptions (user_id);
CREATE INDEX IF NOT EXISTS idx_stripe_subs_account   ON public.stripe_subscriptions (account_id);
CREATE INDEX IF NOT EXISTS idx_stripe_subs_status    ON public.stripe_subscriptions (status);
CREATE INDEX IF NOT EXISTS idx_stripe_subs_period_end ON public.stripe_subscriptions (current_period_end);

COMMENT ON TABLE public.stripe_customers IS
    'Stripe customer SNAPSHOT (control-plane public.*). Local read source for the billing '
    'dashboard; upserted idempotently from signature-verified webhook events.';
COMMENT ON TABLE public.stripe_subscriptions IS
    'Stripe subscription SNAPSHOT (control-plane public.*). The retention._is_subscribed purge '
    'gate reads this. status IN (active,trialing,past_due) counts as subscribed (past_due is '
    'dunning — a paying customer mid-dunning is never purgeable).';
