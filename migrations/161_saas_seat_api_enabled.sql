-- Migration 161: PER-SEAT API-ACCESS GRANT (CLOSED-SOURCE SaaS layer).
-- Date: 2026-07-11
--
-- Owner-ratified least-privilege refinement of migration 160. Migration 160 put the /v1
-- developer API behind ONE account-level lever (public.accounts.api_addon_enabled). But
-- enabling that ACCOUNT add-on silently armed EVERY seat's flu_ token as a live API
-- credential — over-exposure ("Jenn from accounting who got phished shouldn't have a live
-- API door she never uses"). Access must be TWO levels, least-privilege:
--
--   1. the ACCOUNT CEILING  — accounts.api_addon_enabled (migration 160): did the account BUY
--      API access at all? (the spend/billing decision — unchanged here.)
--   2. the PER-SEAT GRANT   — this column: has an admin GRANTED API to THIS specific seat?
--
-- A /v1 call is allowed ONLY when BOTH hold (account.api_addon_enabled AND seat.api_enabled).
-- Enabling the account no longer arms any seat; an admin grants API per-seat only to the seats
-- that need it. Granting a seat requires the account ceiling to already be ON (you cannot grant
-- what the account has not purchased). This is an ACCESS grant, NOT a spend act (the spend was
-- the account add-on) — so a normal tenant admin can flip it (no can_view_billing required).
--
-- ⚠️ NOT BACKFILLED: EVERY seat starts with api_enabled = false, INCLUDING seats on accounts
-- whose add-on is already ON. That is the whole point — closing the currently-open per-seat
-- doors. After this migration an admin must explicitly grant each seat that needs /v1.
--
-- CONTROL-PLANE field in public.account_users (read at request time like migrations 139/148/159).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path — additive, does not
-- weaken the per-tenant DATA rule (SET search_path WITHOUT public).
--
-- The account ceiling (migration 160) is untouched: it remains the billing/consent lever and the
-- hard prerequisite for any per-seat grant. Idempotent (ADD COLUMN IF NOT EXISTS); safe to re-run.

ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS api_enabled BOOLEAN NOT NULL DEFAULT false;

-- Fast "which seats have /v1 granted" scan for operator/tenant oversight.
CREATE INDEX IF NOT EXISTS idx_account_users_api_enabled
    ON public.account_users (account_id, user_id) WHERE api_enabled = true;
