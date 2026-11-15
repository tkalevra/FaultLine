-- Migration 237: Stripe CHARGES + REFUNDS ledger — CLOSED-SOURCE SaaS layer.
-- Date: 2026-08-11
-- Purpose: local append-only record of every charge + refund that hit the Stripe account —
--          the drift-detection surface vs the authoritative account_ledger. MRR + reconciliation
--          reads from here; the dashboard surfaces any mismatch with the billing ledger rather
--          than hiding it.
--
-- Append-only in the same sense as migration 235's event ledger: a charge/refund row is INSERTed
-- once from a signature-verified event and is not rewritten. (No BEFORE UPDATE trigger here — a
-- charge's refunded_amount_cents IS legitimately incremented by a later refund event, and a
-- refund's status can transition pending→succeeded. Idempotency is on the stripe id at INSERT.)
--
-- amount_cents is POSITIVE in the SMALLEST currency unit (cents for USD/CAD/EUR). Never negative;
-- a refund is its own row in stripe_refunds with its own positive amount_cents.
--
-- Control-plane public.* tables (read/written at request time, like public.payment_config).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path — additive.
--
-- Read with: saas/stripe_state.py (record_charge/record_refund, mrr_snapshot).
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

CREATE TABLE IF NOT EXISTS public.stripe_charges (
    charge_id             TEXT PRIMARY KEY,           -- stripe charge id (ch_...)
    customer_id           TEXT,
    subscription_id       TEXT,
    invoice_id            TEXT,
    amount_cents          INT NOT NULL,               -- POSITIVE, smallest currency unit
    currency              TEXT NOT NULL,
    refunded_amount_cents INT NOT NULL DEFAULT 0,     -- incremented by later refund events
    status                TEXT NOT NULL,              -- succeeded|pending|failed
    livemode              BOOLEAN NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL,       -- stripe created timestamp
    recorded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT stripe_charges_amount_nonneg CHECK (amount_cents >= 0),
    CONSTRAINT stripe_charges_refunded_nonneg CHECK (refunded_amount_cents >= 0)
);

CREATE INDEX IF NOT EXISTS idx_stripe_charges_customer     ON public.stripe_charges (customer_id);
CREATE INDEX IF NOT EXISTS idx_stripe_charges_subscription ON public.stripe_charges (subscription_id);
CREATE INDEX IF NOT EXISTS idx_stripe_charges_invoice       ON public.stripe_charges (invoice_id);
CREATE INDEX IF NOT EXISTS idx_stripe_charges_created       ON public.stripe_charges (created_at DESC);

CREATE TABLE IF NOT EXISTS public.stripe_refunds (
    refund_id    TEXT PRIMARY KEY,                    -- stripe refund id (re_...)
    charge_id    TEXT NOT NULL,
    amount_cents INT NOT NULL,                        -- POSITIVE, smallest currency unit
    currency     TEXT NOT NULL,
    reason       TEXT,                                -- duplicate|fraudulent|requested_by_customer
    status       TEXT NOT NULL,                       -- succeeded|pending|failed|canceled
    livemode     BOOLEAN NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,                -- stripe created timestamp
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT stripe_refunds_amount_nonneg CHECK (amount_cents >= 0)
);

CREATE INDEX IF NOT EXISTS idx_stripe_refunds_charge  ON public.stripe_refunds (charge_id);
CREATE INDEX IF NOT EXISTS idx_stripe_refunds_created ON public.stripe_refunds (created_at DESC);

COMMENT ON TABLE public.stripe_charges IS
    'Stripe CHARGE ledger (control-plane public.*). Append-only record of every charge from a '
    'signature-verified event; amount_cents is positive in the smallest currency unit. MRR + '
    'reconciliation read from here; drift vs account_ledger is surfaced, not hidden.';
COMMENT ON TABLE public.stripe_refunds IS
    'Stripe REFUND ledger (control-plane public.*). Append-only record of every refund from a '
    'signature-verified charge.refunded event.';
