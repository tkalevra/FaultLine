-- Migration 235: Stripe webhook EVENT LEDGER — CLOSED-SOURCE SaaS layer.
-- Date: 2026-08-11
-- Purpose: append-only record of every signature-verified Stripe webhook event received.
--          This is the DEDUPE + processing-state ledger: event_id is the primary key, so a
--          redelivered event (Stripe retries for up to 3 days live) is recognized as a
--          duplicate and the processor is NOT re-run. The local snapshot
--          (migration 236) + the account billing ledger stay AUTHORITATIVE; this table is the
--          durable receipt trail + the at-least-once-delivery guard.
--
-- Control-plane public.* table (read/written at request time, like public.payment_config).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path — additive; it does
-- not weaken the per-tenant DATA rule (SET search_path WITHOUT public).
--
-- APPEND-ONLY: payload / event_id / received_at are immutable after insert (BEFORE UPDATE
-- trigger raises, mirroring migration 140's saas_audit_log pattern). The processing-state
-- columns (processing_status, processed_at, processing_error, attempt_count) ARE mutable so the
-- processor can advance a row pending → processing → processed | error without rewriting history.
--
-- Read with: saas/stripe_webhooks.py, saas/stripe_state.py.
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

CREATE TABLE IF NOT EXISTS public.stripe_events (
    event_id           TEXT PRIMARY KEY,                -- stripe event id (evt_...); dedupe key
    event_type         TEXT NOT NULL,                   -- e.g. checkout.session.completed
    api_version        TEXT,                            -- the stripe api version on the event
    livemode           BOOLEAN NOT NULL,                -- test (FALSE) vs live (TRUE)
    received_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload            JSONB NOT NULL,                  -- the full signed event payload
    processing_status  TEXT NOT NULL DEFAULT 'pending', -- pending|processing|processed|error
    processed_at       TIMESTAMPTZ,
    processing_error   TEXT,                            -- populated when processing_status='error'
    attempt_count      INT NOT NULL DEFAULT 0
);

-- Hot read paths: the operator "recent events" view (by type, newest first) and the
-- reprocessor sweep (pending/error rows oldest first).
CREATE INDEX IF NOT EXISTS idx_stripe_events_type_received
    ON public.stripe_events (event_type, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_stripe_events_status_received
    ON public.stripe_events (processing_status, received_at);

-- ── APPEND-ONLY enforcement on the immutable columns ──────────────────────────────
-- A BEFORE UPDATE trigger raises if a code path tries to rewrite the event identity
-- (event_id / event_type / api_version / livemode / received_at / payload). The mutable
-- processing-state columns are explicitly allowed through. INSERT is always permitted; the
-- processing lifecycle mutates ONLY the state columns. (A privileged DBA can still drop the
-- trigger; that is out of scope for app-layer tamper-evidence.)
CREATE OR REPLACE FUNCTION public.stripe_events_immutable_columns()
RETURNS trigger AS $$
DECLARE
    bad TEXT;
BEGIN
    -- DELETE is always blocked (the whole row is append-only).
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'public.stripe_events is append-only — DELETE is not permitted';
    END IF;
    -- On UPDATE, detect any change to the immutable identity columns (NEW differs from OLD).
    IF NEW.event_id      IS DISTINCT FROM OLD.event_id      THEN bad := 'event_id';     END IF;
    IF NEW.event_type    IS DISTINCT FROM OLD.event_type    AND bad IS NULL THEN bad := 'event_type';    END IF;
    IF NEW.api_version   IS DISTINCT FROM OLD.api_version   AND bad IS NULL THEN bad := 'api_version';   END IF;
    IF NEW.livemode      IS DISTINCT FROM OLD.livemode      AND bad IS NULL THEN bad := 'livemode';      END IF;
    IF NEW.received_at   IS DISTINCT FROM OLD.received_at   AND bad IS NULL THEN bad := 'received_at';   END IF;
    IF NEW.payload       IS DISTINCT FROM OLD.payload       AND bad IS NULL THEN bad := 'payload';       END IF;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'public.stripe_events column % is append-only — UPDATE is not permitted', bad;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_stripe_events_immutable_columns ON public.stripe_events;
CREATE TRIGGER trg_stripe_events_immutable_columns
    BEFORE UPDATE OR DELETE ON public.stripe_events
    FOR EACH ROW EXECUTE FUNCTION public.stripe_events_immutable_columns();

COMMENT ON TABLE public.stripe_events IS
    'Stripe webhook EVENT LEDGER (control-plane public.*). Append-only receipt of every '
    'signature-verified event; event_id is the dedupe key. The payload/identity columns are '
    'immutable (BEFORE UPDATE trigger); only the processing-state columns are mutable.';
