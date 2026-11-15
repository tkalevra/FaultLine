-- Migration 254: TERM ACCELERATION on cancellation — CLOSED-SOURCE SaaS layer.
-- Date: 2026-08-12
--
-- Purpose: give the commercial policy in the SIGNED terms (migration 253 §3, bullet "If your
--          subscription ends before your term does") a place to LIVE and a state machine that
--          can be read, rendered and audited.
--
--          The signed wording, verbatim:
--            "If you cancel during a term, or if we close your account after a suspension for
--             non-payment, the base fee for the remaining months of your term becomes due as a
--             single amount and we will send you one final invoice for it, stating the months and
--             the amount. Only the base fee is included — seats and add-ons are not, because they
--             were never part of your commitment (section 2). You then have two ways to resolve
--             it, and the choice is yours: 1. Settle it. … 2. Resume. Bring your subscription back
--             and we cancel that final invoice entirely … We will not send the amount to
--             collections, or charge interest on it, without giving you at least 30 days from that
--             written notice to choose one of the two."
--
-- ── WHY A TABLE AND NOT "DERIVE IT FROM THE LEDGER" ───────────────────────────────────
-- public.account_ledger is APPEND-ONLY and is the ACCOUNTING record: what was charged, what was
-- paid, what was reversed. It is exactly the wrong shape for the three things this feature needs
-- to answer on every page load:
--   * WHICH Stripe invoice is the open one (so RESUME can void the right object);
--   * WHEN the written notice went out (so the 30-day grace deadline is COMPUTABLE, not folklore);
--   * WHAT the current state is (invoiced / settled / voided) without replaying history.
-- So the ledger keeps the money and this table keeps the STATE, and each row points at the ledger
-- rows it produced. Neither is derived from the other; they are cross-checkable, which is the
-- point.
--
-- ── 🔴 THE ACCOUNTING SPLIT THIS TABLE EXISTS TO KEEP HONEST ──────────────────────────
-- An obligation that has NOT been invoiced is not a receivable. An invoice that HAS been issued
-- is. `entitlements.ledger_summary` folds every non-payment `amount` into `grand_total`, which the
-- operator Billing card labels "charged" (saas/dashboard/app.js — "Ledger — charged … paid …
-- balance …"). The existing `commitment_broken` row therefore carries amount 0 ON PURPOSE: at that
-- moment we had observed a broken commitment and invoiced nothing, and booking arrears into
-- "charged" would have been an active misstatement of revenue.
--
-- This migration does NOT change that row. It adds the state that comes AFTER it, and the four
-- states stay distinguishable IN THE DATA — by `status` here and by ledger `action` there — never
-- by inference:
--
--   1. OWED, NOT YET INVOICED   ledger 'commitment_broken'         amount 0        (no receivable)
--   2. INVOICED, UNPAID         ledger 'term_acceleration'         amount +arrears (a receivable)
--   3. SETTLED                  ledger 'term_acceleration_settled' amount +arrears, and that
--                               action is in PAYMENT_ACTIONS so it credits `paid_total`,
--                               never inflating "charged". Balance returns to zero.
--   4. VOIDED (customer resumed) ledger 'term_acceleration_voided' amount -arrears with
--                               `reverses_ledger_id` pointing at the state-2 row. "Charged" falls
--                               back to where it was. A voided invoice is NOT revenue and it is
--                               NOT a write-off — it is a reversal, so it must leave BOTH totals
--                               as if the charge had never been raised.
--
-- Control-plane public.* table (read/written at request time). NEVER seeded into a tenant schema,
-- NEVER read from a tenant search_path. Additive and idempotent; safe to re-run.

CREATE TABLE IF NOT EXISTS public.term_accelerations (
    id                    BIGSERIAL PRIMARY KEY,
    account_id            UUID        NOT NULL,

    -- WHAT WAS ACCELERATED, and the evidence for every number on the final invoice.
    -- `base_fee_cents` is copied from public.signed_contracts.agreed_amount_cents — the amount
    -- recorded WITH THE CUSTOMER'S SIGNATURE, which migration 243's append-only trigger makes
    -- immutable. It is deliberately NOT the live rate card: migration 253 §3 says outright
    -- "A price change never applies to a term you are already in — the base fee recorded with
    -- your signature is the base fee for that whole term." Snapshotting it here means the final
    -- invoice can still be explained years later even if the catalog has moved on.
    contract_id           BIGINT,               -- public.signed_contracts.id (the authority)
    subscription_id       TEXT,                 -- the Stripe subscription that was cancelled
    base_fee_cents        INTEGER     NOT NULL, -- e.g. 150000 = CA$1,500.00 / month, SIGNED
    currency              TEXT        NOT NULL DEFAULT 'CAD',
    months_remaining      INTEGER     NOT NULL, -- whole committed months left at notice time
    amount_cents          INTEGER     NOT NULL, -- months_remaining * base_fee_cents
    term_end              TIMESTAMPTZ,          -- accounts.term_months_set_at + term_months

    -- PROOF THE ELASTIC ITEMS WERE STRIPPED BEFORE ANY ARITHMETIC HAPPENED.
    -- The owner's instruction is a SAFETY property, not tidiness: if the seat items are not on
    -- the subscription, no arithmetic error anywhere downstream can accelerate them. These
    -- columns record that the strip ran and what the subscription looked like afterwards, so a
    -- later reader can tell "we removed 3 items and verified 1 remained" from "we never checked".
    items_stripped        INTEGER     NOT NULL DEFAULT 0,
    items_remaining       INTEGER,              -- NULL = never verified (refuses to invoice)
    strip_verified        BOOLEAN     NOT NULL DEFAULT FALSE,

    -- THE STRIPE INVOICE. NULL until it is created and finalized.
    stripe_invoice_id     TEXT,
    stripe_invoice_number TEXT,
    stripe_invoice_status TEXT,                 -- as last OBSERVED from Stripe, never assumed

    -- THE STATE MACHINE.
    --   pending  — computed, nothing issued yet (a refusal or a failed Stripe write lands here)
    --   invoiced — the final invoice is finalized and open. THE RECEIVABLE EXISTS.
    --   settled  — paid in full; account closes with nothing further owed
    --   voided   — the customer resumed; the invoice is voided IN FULL and normal monthly
    --              billing continues for the rest of the term
    status                TEXT        NOT NULL DEFAULT 'pending',

    -- 🔴 THE 30-DAY GRACE, MODELLED SO THE DEADLINE IS COMPUTABLE.
    -- Migration 253 §3: "We will not send the amount to collections, or charge interest on it,
    -- without giving you at least 30 days from that written notice to choose one of the two."
    -- A promise measured from "that written notice" is unenforceable unless the notice DATE is a
    -- stored fact. `grace_until` is a STORED column rather than a view over notice_sent_at + 30d
    -- so that a later policy change cannot retroactively shorten a grace period already granted
    -- to a specific customer. Authority order: what this customer was actually told outranks the
    -- current setting.
    notice_sent_at        TIMESTAMPTZ,
    grace_until           TIMESTAMPTZ,
    notice_channel        TEXT,                 -- how the written notice was delivered

    -- CROSS-LINKS INTO THE APPEND-ONLY ACCOUNTING RECORD (states 2/3/4 above).
    ledger_id_invoiced    BIGINT,
    ledger_id_settled     BIGINT,
    ledger_id_voided      BIGINT,

    -- WHO / WHY.
    trigger               TEXT        NOT NULL DEFAULT 'cancellation',  -- cancellation | non_payment
    actor                 TEXT,
    actor_user            TEXT,
    last_error            TEXT,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at           TIMESTAMPTZ
);

-- Re-runnable against a schema created by an earlier cut of this file.
ALTER TABLE public.term_accelerations ADD COLUMN IF NOT EXISTS items_remaining INTEGER;
ALTER TABLE public.term_accelerations ADD COLUMN IF NOT EXISTS strip_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.term_accelerations ADD COLUMN IF NOT EXISTS notice_channel TEXT;
ALTER TABLE public.term_accelerations ADD COLUMN IF NOT EXISTS stripe_invoice_number TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'term_accelerations_status_ck') THEN
        ALTER TABLE public.term_accelerations
            ADD CONSTRAINT term_accelerations_status_ck
            CHECK (status IN ('pending', 'invoiced', 'settled', 'voided'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'term_accelerations_trigger_ck') THEN
        ALTER TABLE public.term_accelerations
            ADD CONSTRAINT term_accelerations_trigger_ck
            CHECK (trigger IN ('cancellation', 'non_payment'));
    END IF;
    -- Money columns may not be negative. An acceleration of a NEGATIVE number of months is an
    -- arithmetic failure upstream, and the one thing worse than refusing to invoice is inventing
    -- a credit nobody is owed.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'term_accelerations_amount_ck') THEN
        ALTER TABLE public.term_accelerations
            ADD CONSTRAINT term_accelerations_amount_ck
            CHECK (amount_cents >= 0 AND base_fee_cents >= 0 AND months_remaining >= 0);
    END IF;
END $$;

-- 🔴 ONE OPEN ACCELERATION PER ACCOUNT, ENFORCED BY THE DATABASE.
-- Two open final invoices for one cancellation is double-billing, and the window that produces it
-- is a double-submitted button or a webhook redelivery — i.e. the normal case, not the exotic one.
-- A PARTIAL unique index is the right instrument: it constrains only the live states and leaves
-- history (settled / voided) unconstrained, so an account that cancels, resumes, and later cancels
-- again is a legal sequence rather than a constraint violation.
CREATE UNIQUE INDEX IF NOT EXISTS uq_term_accelerations_one_open
    ON public.term_accelerations (account_id)
 WHERE status IN ('pending', 'invoiced');

CREATE INDEX IF NOT EXISTS ix_term_accelerations_account
    ON public.term_accelerations (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_term_accelerations_status
    ON public.term_accelerations (status)
 WHERE status = 'invoiced';
CREATE INDEX IF NOT EXISTS ix_term_accelerations_invoice
    ON public.term_accelerations (stripe_invoice_id)
 WHERE stripe_invoice_id IS NOT NULL;

COMMENT ON TABLE public.term_accelerations IS
    'Term-acceleration state on cancellation (migration 253 section 3). One row per acceleration: '
    'the base fee accelerated, the Stripe invoice raised for it, the 30-day grace clock, and which '
    'of the two resolutions the customer chose. The MONEY lives in public.account_ledger '
    '(actions term_acceleration / term_acceleration_settled / term_acceleration_voided); this '
    'table holds the STATE. Seats and add-ons are never included — they are stripped from the '
    'subscription BEFORE the amount is computed.';

COMMENT ON COLUMN public.term_accelerations.base_fee_cents IS
    'The SIGNED base fee (public.signed_contracts.agreed_amount_cents), never the live rate card. '
    'Migration 253 section 3: "A price change never applies to a term you are already in."';

COMMENT ON COLUMN public.term_accelerations.strip_verified IS
    'TRUE only when the post-strip subscription was RE-READ from Stripe and found to carry exactly '
    'one item (the base). FALSE blocks invoicing: the safety property is that seats cannot be '
    'accelerated by any downstream arithmetic error because they are not on the subscription.';

COMMENT ON COLUMN public.term_accelerations.grace_until IS
    'notice_sent_at + 30 days, STORED not derived, so a later policy change cannot retroactively '
    'shorten a grace period already granted to this customer.';
