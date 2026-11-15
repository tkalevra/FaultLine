-- Migration 171: MONTHLY INVOICE SCHEDULER support (CLOSED-SOURCE SaaS layer). Date: 2026-07-12.
--
-- Adds the control-plane state the monthly invoice job needs, on top of migration 159's
-- accounts.billing_contact_email (the delivery address) + can_view_billing (the view gate):
--
--   1. accounts.invoice_send_day  — the day-of-month (1..28) the monthly invoice email goes out
--      for this account. DEFAULT 1. Bounded to 28 so it fires in every month (no 29/30/31 gap in
--      February). Operator- or billing-admin-editable alongside billing_contact_email. The
--      scheduler is designed to run DAILY and only send for accounts whose invoice_send_day ==
--      today's day (or a catch-up when a run was missed) — so the send-day is the schedule knob.
--
--   2. public.invoice_sends  — the idempotency + audit ledger for emailed invoices. ONE row per
--      (account_id, period): the scheduler inserts BEFORE sending and will not double-send a
--      period. Records the invoice number, the address it went to, the CAD total, and the
--      send status/error. Append-mostly (status updated from 'pending' → 'sent'/'failed').
--
-- All CONTROL-PLANE (public.*) — NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path (does not weaken the per-tenant DATA isolation rule). Idempotent + forward-only
-- (ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS); safe to re-run / auto-applies.
--
-- Read with: DEV/DESIGN-billing-accounting.md, migration 159 (billing role separation),
-- migration 139 (append-only ledger), saas/invoicing.py + saas/invoice_scheduler.py.

-- ── 1. per-account send-day (default the 1st; capped at 28 so it exists every month) ──────
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS invoice_send_day SMALLINT NOT NULL DEFAULT 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.constraint_column_usage
        WHERE table_schema = 'public' AND table_name = 'accounts'
          AND constraint_name = 'accounts_invoice_send_day_range'
    ) THEN
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_invoice_send_day_range
            CHECK (invoice_send_day BETWEEN 1 AND 28);
    END IF;
EXCEPTION WHEN duplicate_object THEN
    -- already present on a prior run
    NULL;
END $$;

-- ── 2. the emitted-invoice idempotency / audit ledger ────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.invoice_sends (
    id             BIGSERIAL PRIMARY KEY,
    account_id     UUID        NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    period         TEXT        NOT NULL,                 -- billing period, YYYY-MM
    invoice_number TEXT        NOT NULL,
    to_email       TEXT,                                 -- the billing_contact_email at send time
    amount_total   NUMERIC(12,2),
    currency       TEXT        NOT NULL DEFAULT 'CAD',
    status         TEXT        NOT NULL DEFAULT 'pending', -- pending | sent | failed | skipped
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at        TIMESTAMPTZ,
    UNIQUE (account_id, period)                          -- the double-send firewall
);

CREATE INDEX IF NOT EXISTS idx_invoice_sends_period
    ON public.invoice_sends (period);
