-- 242_stripe_subscription_event_ordering.sql — event-ordering guard against status resurrection
--
-- PROBLEM (caught by the Stripe-docs compliance critic, 2026-08-11):
--   Stripe does NOT guarantee event delivery order (https://docs.stripe.com/webhooks#event-
--   ordering). A stale `customer.subscription.updated` (status=active) delivered AFTER
--   `customer.subscription.deleted` (status=canceled) used to overwrite the snapshot's status
--   back to `active` — which `retention._is_subscribed` reads directly, so a CANCELED customer
--   kept access AND was exempt from the retention purge. Last-writer-wins on `status` is wrong.
--
-- FIX:
--   Track the timestamp of the Stripe EVENT that last wrote the row (``last_event_at``).
--   The upsert's ON CONFLICT DO UPDATE now carries a WHERE clause that refuses to apply an
--   update from an OLDER event (``EXCLUDED.last_event_at >=
--   public.stripe_subscriptions.last_event_at``). A stale event becomes a no-op; a genuine
--   later re-activation (newer event timestamp) still updates. This is the documented Stripe
--   mitigation: "Make sure that your event destination isn't dependent on receiving events in
--   a specific order. You can also use the API to retrieve any missing objects."
--
-- The column is NULLable so existing rows backfill cleanly; NULL is treated as -infinity by
-- the WHERE (an incoming event always wins over a NULL stored value).

ALTER TABLE public.stripe_subscriptions
    ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_stripe_subscriptions_last_event_at
    ON public.stripe_subscriptions (last_event_at);

COMMENT ON COLUMN public.stripe_subscriptions.last_event_at IS
    'The created timestamp of the Stripe Event that last wrote this row. Used by the upsert '
    'guard to refuse out-of-order updates (a stale event must not resurrect a canceled status). '
    'https://docs.stripe.com/webhooks#event-ordering';
