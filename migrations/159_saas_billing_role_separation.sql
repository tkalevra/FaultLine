-- Migration 159: SaaS BILLING ROLE SEPARATION (CLOSED-SOURCE SaaS layer).
-- Date: 2026-07-11
--
-- Owner-ratified decision (CLAUDE.md SaaS §12): not every org wants the seat/security admin
-- to see spend. Two additive control-plane columns implement that split:
--
--   1. public.accounts.billing_contact_email  — a per-account billing-contact email DISTINCT
--      from any dashboard login. The Merchant of Record (Lemon Squeezy) emails invoices /
--      receipts HERE, so finance can receive billing without holding a dashboard seat and the
--      admin who runs seats/security need not be the one who sees money. Nullable — unset until
--      a can_view_billing admin fills it in.
--
--   2. public.dashboard_users.can_view_billing  — gates the in-console billing / cost VIEW
--      (invoice, ledger, plan-cost, billing-contact editing). Default FALSE: a freshly-minted
--      seat/security admin does NOT see spend. The account owner grants it explicitly.
--
-- BACKFILL: the feature is new, so EVERY existing tenant dashboard login currently sees billing
-- (there was no gate). Flipping the default to FALSE without a backfill would lock existing
-- admins out of their OWN billing on upgrade. So we backfill can_view_billing = TRUE for every
-- EXISTING tenant login (account_id IS NOT NULL — i.e. NOT an operator row). New rows minted
-- after this migration take the FALSE default and must be granted by the account owner. Operator
-- rows (account_id IS NULL, the platform super-admins) are irrelevant to tenant billing and are
-- left at the default.
--
-- NOTE ON "OWNER": there is no explicit owner column on dashboard_users (roles are
-- admin/viewer/operator). The account OWNER is defined operationally as the EARLIEST-created
-- dashboard login for the account (the primary admin minted at provisioning) — see
-- saas/dashboard_auth.is_account_owner. The owner-only grant/revoke control keys off that.
--
-- All columns are CONTROL-PLANE fields in public.* (read at request time like migrations 131/136).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path — additive, does not
-- weaken the per-tenant DATA rule (SET search_path WITHOUT public).
--
-- PAYMENT IS A STUB here: this migration only STORES the billing email. The Lemon Squeezy
-- customer sync / invoice email lives behind TODO(lemon-squeezy) in saas/tenant_console.py.
--
-- Read with: DEV/DESIGN-billing-accounting.md, DEV/SECURITY-saas-multitenancy.md.
-- Idempotent (ADD COLUMN IF NOT EXISTS); safe to re-run.

-- ── 1. per-account billing-contact email (where the MoR emails invoices) ────────────
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS billing_contact_email TEXT;

-- ── 2. per-login billing-view permission (default OFF; owner grants) ─────────────────
ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS can_view_billing BOOLEAN NOT NULL DEFAULT false;

-- ── BACKFILL: existing tenant logins keep their billing access (no lock-out on upgrade) ─
-- Only tenant rows (account_id IS NOT NULL). Idempotent: re-running only re-sets the same
-- rows TRUE; it never flips a newly-minted (post-migration, intentionally-FALSE) row, because
-- this DDL runs once at migration time — subsequent app-minted rows are created afterwards.
UPDATE public.dashboard_users
    SET can_view_billing = true
    WHERE account_id IS NOT NULL;

-- Fast "who on this account can see billing" scan for the owner's grant panel.
CREATE INDEX IF NOT EXISTS idx_dashboard_users_billing_view
    ON public.dashboard_users (account_id) WHERE can_view_billing = true;
