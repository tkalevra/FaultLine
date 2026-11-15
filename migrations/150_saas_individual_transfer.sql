-- Migration 150: SaaS INDIVIDUAL → TENANT promotion (transfer) flow (CLOSED-SOURCE SaaS layer)
-- Date: 2026-07-09
-- Purpose: the ratified individual→tenant promotion flow. A self-serve individual (backed by a
--          single-seat "house account", tenant_context 'none'/'house', self-owned) can transfer
--          INTO a corporate tenant. The transfer is OPERATOR-GATED, a USER-FILLED REQUEST is the
--          explicit CONSENT, and it is ONE-WAY ONLY (join a tenant, never leave, never hop
--          between tenants). Governance materially changes on execute: self-owned → tenant-
--          governed (loses self-Purge, the tenant takes endpoint control, the org owns the data
--          lifecycle). Safety valve: an export-before-transfer snapshot is generated at execute.
--
-- This migration adds (a) the ONE-WAY LOCK columns on public.individual_users and (b) the
-- public.transfer_requests queue the individual writes and the operator approves/denies.
--
-- Control-plane objects in public.* (read/written at request time like accounts /
-- individual_users). NEVER seeded into a tenant schema, NEVER read from a tenant search_path —
-- additive, does not weaken the per-tenant DATA rule (SET search_path WITHOUT public).
-- Idempotent (IF NOT EXISTS / guarded); safe to re-run.

-- ── PART A: ONE-WAY LOCK on the individual login ────────────────────────────────────
-- transfer_locked pins the one-way rule at the row: once TRUE, a second transfer request is
-- refused (cannot leave, cannot move to another org). transferred_at / transferred_from_account
-- are provenance for the operator audit view.
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS transfer_locked BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS transferred_at TIMESTAMPTZ;
ALTER TABLE public.individual_users
    ADD COLUMN IF NOT EXISTS transferred_from_account UUID;

-- ── PART B: the transfer-request queue ──────────────────────────────────────────────
-- One row = one individual's request to join a corporate tenant. The individual writes it
-- (pending) with the CONSENT captured inline; the operator moves it approved→executed | denied.
CREATE TABLE IF NOT EXISTS public.transfer_requests (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    individual_id       UUID NOT NULL,                 -- public.individual_users.id (the login)
    user_id             UUID NOT NULL,                 -- the individual's owned memory uuid (data plane)
    from_account_id     UUID,                          -- the house account at request time
    target_identifier   TEXT NOT NULL,                 -- the raw join code the individual entered
    target_account_id   UUID,                          -- resolved target (public.accounts) at request time
    target_account_name TEXT,                          -- snapshot for the operator display
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'denied', 'executed', 'failed')),
    -- CONSENT is the crux: governance changes (self-owned → tenant-governed), so the explicit
    -- user acknowledgement + the exact language shown are captured with the request.
    consent_ack         BOOLEAN NOT NULL DEFAULT FALSE,
    consent_text        TEXT,                          -- the exact consent language presented
    consent_signature   TEXT,                          -- optional typed name authorizing the request
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at          TIMESTAMPTZ,
    decided_by          TEXT,                          -- operator identity (server-derived, secret-free)
    decision_note       TEXT,
    executed_at         TIMESTAMPTZ,
    -- Export-before-transfer marker (the snapshot itself is returned/held out-of-band, not
    -- stored here — control-plane rows carry no tenant memory content).
    export_generated_at TIMESTAMPTZ,
    export_fact_count   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_transfer_requests_status
    ON public.transfer_requests (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_transfer_requests_user
    ON public.transfer_requests (user_id);

-- At most ONE live (pending/approved) request per individual at a time — a partial UNIQUE
-- index makes a duplicate submit a clean conflict rather than a queue of stale rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_transfer_requests_live
    ON public.transfer_requests (user_id)
    WHERE status IN ('pending', 'approved');
