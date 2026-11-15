-- Migration 251: SEAT → STRIPE SUBSCRIPTION ITEM map — CLOSED-SOURCE SaaS layer.
-- Date: 2026-08-12
-- Purpose: make Stripe's invoice the CUSTOMER'S invoice. Until now Stripe collected only the
--          flat base Price (`payments_stripe.create_checkout` sends
--          `line_items: [{price, quantity: 1}]`) while the per-seat money was assembled
--          internally by `saas/invoicing.py` — and NOTHING collected it
--          (`invoice_scheduler.run_due_invoices` has no caller; there are zero PaymentIntent /
--          Invoice.create calls). An Enterprise customer with 10 extra seats was charged
--          CA$1,500 and the other CA$280 was a number in a table.
--
--          Stripe supports exactly the shape we need: one subscription carrying many items,
--          billed together. Per https://docs.stripe.com/billing/subscriptions/multiple-products
--          — "This generates a single invoice each billing period that combines every price,
--          and the customer only needs to make a single payment." The documented limits are
--          "all the prices for those products must use the same currency" and "You're also
--          limited to 20 products in a single subscription": we carry at most 5 items (1 base +
--          4 seat types), all CAD, all monthly.
--
-- ── WHY A STORED MAP AND NOT A RE-DERIVE-FROM-STRIPE-EVERY-TIME LOOKUP ────────────────
-- The alternative is to call `GET /v1/subscription_items` on every capacity change and match
-- by price_id. That is correct but it puts a NETWORK ROUND TRIP INSIDE THE CRITICAL PATH OF
-- EVERY SEAT PURCHASE, and — worse on a money path — it makes Stripe's reachability a
-- precondition for knowing our own billing intent. Storing the map means:
--   * the intent (`desired_quantity`) is durable the instant the entitlement commits, so a
--     Stripe outage is a RETRYABLE BACKLOG rather than a lost instruction;
--   * `sync_seq` gives every write a stable idempotency-key discriminator (see below);
--   * the drift the stored copy risks is DISCOVERABLE by construction — that is exactly what
--     `stripe_seat_sync.reconcile()` reads, and it re-derives from live Stripe to compare.
-- A stored map whose drift is measured beats a live read whose failure is silent.
--
-- ── `sync_seq` IS LOAD-BEARING, NOT DECORATION ────────────────────────────────────────
-- Stripe idempotency keys are pruned after ~24h and the layer "compares incoming parameters to
-- those of the original request and errors if they're not the same"
-- (https://docs.stripe.com/api/idempotent_requests). A key built from (account, seat_type,
-- quantity) therefore has a live hazard: 10 → 11 → 10 inside one day would re-present the FIRST
-- key, and Stripe would return the SAVED RESPONSE of that first request WITHOUT applying the
-- change — leaving Stripe at 11 while we recorded 10. `sync_seq` increments on every genuine
-- change of intent, so a retry of one logical write reuses its key (absorbed, correctly) and a
-- new intent always mints a fresh one.
--
-- Control-plane public.* table (read/written at request time). NEVER seeded into a tenant
-- schema, NEVER read from a tenant search_path. Additive and idempotent; safe to re-run.

CREATE TABLE IF NOT EXISTS public.stripe_seat_items (
    id                BIGSERIAL PRIMARY KEY,
    account_id        UUID    NOT NULL,
    seat_type         TEXT    NOT NULL,

    -- WHERE the item lives. Resolved from accounts.external_billing_ref (the same deterministic
    -- linkage entitlements.external_collected_monthly uses), never guessed.
    subscription_id   TEXT,
    -- si_… — NULL means there is NO item at Stripe right now: either never created, or deleted
    -- because the billable quantity fell to zero. NULL is a real state, not "unknown".
    stripe_item_id    TEXT,
    price_id          TEXT,
    price_lookup_key  TEXT,

    -- What Stripe charges per unit, as OBSERVED on the Price at sync time. Nullable with NO
    -- default: a tiered/metered Price legitimately has no unit amount
    -- (https://docs.stripe.com/api/prices/object — unit_amount is "(integer, NULLABLE)"), and a
    -- DEFAULT 0 would turn every un-synced row into a confident, wrong CA$0.00.
    unit_amount_cents INTEGER,
    currency          TEXT,

    -- desired = what OUR billing math says Stripe should charge for (the billable add-on count,
    -- i.e. AFTER the tier's included allotment). synced = the last quantity Stripe CONFIRMED.
    -- They differ exactly while a sync is pending or has failed — which is the reconciliation
    -- read's entire signal. synced_quantity is NULLABLE: NULL = never confirmed by Stripe, and
    -- that is deliberately distinct from a confirmed 0.
    desired_quantity  INTEGER NOT NULL DEFAULT 0,
    synced_quantity   INTEGER,

    sync_seq          BIGINT  NOT NULL DEFAULT 0,
    sync_state        TEXT    NOT NULL DEFAULT 'pending',
    last_error        TEXT,
    last_attempt_at   TIMESTAMPTZ,
    synced_at         TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT stripe_seat_items_account_seat_uniq UNIQUE (account_id, seat_type),
    CONSTRAINT stripe_seat_items_qty_nonneg        CHECK (desired_quantity >= 0),
    CONSTRAINT stripe_seat_items_synced_nonneg     CHECK (synced_quantity IS NULL
                                                          OR synced_quantity >= 0),
    CONSTRAINT stripe_seat_items_state_valid       CHECK (sync_state IN
                                                          ('pending', 'in_sync', 'failed',
                                                           'not_billable'))
);

CREATE INDEX IF NOT EXISTS idx_stripe_seat_items_account
    ON public.stripe_seat_items (account_id);
CREATE INDEX IF NOT EXISTS idx_stripe_seat_items_subscription
    ON public.stripe_seat_items (subscription_id);
-- The reconciliation read's hot path: everything NOT settled.
CREATE INDEX IF NOT EXISTS idx_stripe_seat_items_unsettled
    ON public.stripe_seat_items (sync_state)
    WHERE sync_state <> 'in_sync';

COMMENT ON TABLE public.stripe_seat_items IS
    'Seat-type -> Stripe subscription-item map (control-plane public.*). One row per '
    '(account, seat_type). desired_quantity is OUR billing intent, written in the same breath '
    'as the entitlement; synced_quantity is what Stripe last CONFIRMED. A row where the two '
    'disagree, or whose sync_state is not in_sync, is money Stripe is NOT collecting — and '
    'saas/stripe_seat_sync.py deliberately keeps such a seat PAYABLE on the internal invoice '
    'so a failed push can never make a charge disappear from both rails at once.';
COMMENT ON COLUMN public.stripe_seat_items.sync_seq IS
    'Monotonic per-row revision, bumped only when desired_quantity CHANGES. Forms the Stripe '
    'idempotency key so a retry of one logical write is absorbed while a genuinely new '
    'intent always mints a fresh key. See the migration header for the 10->11->10 hazard this '
    'closes.';
COMMENT ON COLUMN public.stripe_seat_items.sync_state IS
    'pending = intent recorded, Stripe not yet confirmed. in_sync = Stripe confirmed '
    'desired_quantity. failed = the push errored (last_error holds a secret-free reason). '
    'not_billable = this account kind is not seat-synced (no tenant subscription bound).';

-- ── REVIEW QUERY (paste into psql) ────────────────────────────────────────────────────
-- Everything Stripe is not currently collecting that our billing math says it should:
--
--   SELECT account_id, seat_type, desired_quantity, synced_quantity, sync_state,
--          last_attempt_at, left(last_error, 120) AS err
--     FROM public.stripe_seat_items
--    WHERE sync_state <> 'in_sync'
--       OR synced_quantity IS DISTINCT FROM desired_quantity
--    ORDER BY last_attempt_at NULLS FIRST;
--
-- The same comparison, but against LIVE Stripe rather than our stored copy, is
-- saas/stripe_seat_sync.reconcile(deep=True).
