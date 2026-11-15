-- Migration 160: SaaS API-ACCESS ADD-ON LEVER (CLOSED-SOURCE SaaS layer).
-- Date: 2026-07-11
--
-- Owner-ratified decision (DEV/DESIGN-api-access-addon-lever.md, 2026-07-11): the public
-- ``/v1`` developer API currently bills ANY valid ``flk_`` account key — an "unknown billable
-- door". API access must be an explicit, transparent, OPT-IN, billable ADD-ON at the ACCOUNT
-- level, OFF by default. A valid key AUTHENTICATES but no longer IMPLIES API entitlement; ``/v1``
-- fails closed (403 api_not_enabled) until a billing admin turns the add-on on and signs a
-- consent attestation (base add-on fee + per-usage metering).
--
-- The lever lives on public.accounts (the ACCOUNT is the billing/contract unit — NOT the seat,
-- NOT the key). Four additive columns:
--
--   1. api_addon_enabled BOOLEAN NOT NULL DEFAULT false — the lever. OFF for every account.
--   2. api_addon_enabled_at    TIMESTAMPTZ — WHEN consent was given (the door opened).
--   3. api_addon_enabled_by    TEXT        — WHICH dashboard login signed (audit).
--   4. api_addon_terms_version TEXT        — WHICH terms/pricing version was agreed.
--
-- ⚠️ NOT BACKFILLED (unlike migration 159's can_view_billing → TRUE backfill): EVERY existing
-- account starts with the door CLOSED. That is the whole point — close the currently-open door.
-- Any pre-prod/pilot account relying on /v1 must have a billing admin re-enable it (bounded
-- blast radius: /v1 shipped days ago and is not yet marketed — DESIGN §6).
--
-- These are CONTROL-PLANE fields in public.* (read at request time like migrations 131/136/159).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path — additive, does not
-- weaken the per-tenant DATA rule (SET search_path WITHOUT public).
--
-- PAYMENT IS A STUB: this migration + gate only STORE consent. The Lemon Squeezy add-on
-- subscription line / metered-usage report lives behind TODO(lemon-squeezy) in
-- saas/tenant_console.py. Enable/disable also appends a signed, attested account_ledger row
-- (action 'api_addon' / 'api_addon_off') — no DB change needed there (ledger.action is free TEXT).
--
-- Read with: DEV/DESIGN-api-access-addon-lever.md, DEV/DESIGN-public-api.md,
-- DEV/DESIGN-billing-accounting.md.
-- Idempotent (ADD COLUMN IF NOT EXISTS); safe to re-run.

ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS api_addon_enabled       BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS api_addon_enabled_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS api_addon_enabled_by     TEXT,
    ADD COLUMN IF NOT EXISTS api_addon_terms_version  TEXT,
    ADD COLUMN IF NOT EXISTS api_addon_disabled_at    TIMESTAMPTZ;

-- Fast "which accounts have the API door open" scan for operator oversight.
CREATE INDEX IF NOT EXISTS idx_accounts_api_addon_enabled
    ON public.accounts (account_id) WHERE api_addon_enabled = true;
