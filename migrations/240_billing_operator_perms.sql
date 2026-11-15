-- Migration 240: BILLING OPERATOR PERMISSIONS + BRANDING — CLOSED-SOURCE SaaS layer.
-- Date: 2026-08-11
--
-- Two additive changes for the GLOBAL ADMIN BILLING DASHBOARD (saas/billing_dashboard_routes.py,
-- served on the operator console under /admin/billing/*):
--
--   A. OPERATOR PERMISSION COLUMNS on public.dashboard_users. The billing dashboard surfaces
--      controls that go beyond "see money": ISSUING REFUNDS, EDITING BRANDING, TOGGLING THE
--      RETENTION-EMAIL CONTROLS, and REGISTERING THE STRIPE WEBHOOK. These are separate grants
--      so a locked-down operator login can be given read-only billing visibility without the
--      destructive verbs. Columns (all BOOLEAN NOT NULL DEFAULT FALSE):
--          can_issue_refunds
--          can_manage_branding
--          can_manage_email_controls
--          can_register_webhook
--
--   B. PAYMENT-SPECIFIC BRANDING columns on public.platform_settings (the singleton). These
--      extend the existing welcome_from_address (mig 174) with the sender identity the
--      retention-email / dunning / invoice flows send billing mail AS. All TEXT, nullable:
--          support_email           — where billing questions land
--          billing_from_address    — the From: for billing/dunning/retention mail
--          billing_from_name       — the display name on that From:
--
-- DEADLOCK GUARD (canary #1). The four permission columns DEFAULT FALSE, so a brand-new operator
-- login created AFTER this migration starts without the destructive verbs. To avoid locking
-- CURRENT operators out of the very console that would grant them, EVERY existing operator row
-- (role='operator' AND account_id IS NULL — the platform super-admin realm) is BACKFILLED to
-- TRUE. The grant panel for these perms is the operator console itself (reachable by any
-- operator via require_operator), so an existing backfilled operator can grant a new operator
-- later; the break-glass SAAS_OPERATOR_KEY is always a recovery path.
--
-- can_view_billing (migration 159) is ALSO backfilled to TRUE for operators here. Migration 159
-- backfilled it only for TENANT logins (account_id IS NOT NULL) and deliberately left operator
-- rows at the FALSE default — which means today no operator can see billing, a latent deadlock
-- for the billing dashboard. This migration closes that.
--
-- CONTROL-PLANE public.* ONLY. Additive; never weakens the per-tenant data rule
-- (SET search_path WITHOUT public). Idempotent (ADD COLUMN IF NOT EXISTS); safe to re-run.
-- Read with: saas/billing_dashboard_routes.py, saas/dashboard/billing.js.

-- ── A. operator permission columns ────────────────────────────────────────────────
ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS can_issue_refunds        BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS can_manage_branding      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS can_manage_email_controls BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS can_register_webhook     BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill EVERY existing operator (platform super-admin) to TRUE — the deadlock guard.
-- Operators are role='operator' AND account_id IS NULL (the dashboard_auth operator realm).
UPDATE public.dashboard_users
   SET can_issue_refunds        = TRUE,
       can_manage_branding      = TRUE,
       can_manage_email_controls= TRUE,
       can_register_webhook     = TRUE,
       can_view_billing         = TRUE
 WHERE role = 'operator' AND account_id IS NULL;

COMMENT ON COLUMN public.dashboard_users.can_issue_refunds IS
    'Operator may issue refunds from the billing dashboard (POST /admin/billing/refunds). '
    'Default FALSE; backfilled TRUE for existing operators by migration 240. Separated from '
    'can_view_billing so a read-only-billing operator can be granted visibility without the '
    'destructive verb.';
COMMENT ON COLUMN public.dashboard_users.can_manage_branding IS
    'Operator may write payment branding (support_email / billing_from_address / '
    'billing_from_name) via PUT /admin/billing/branding. Default FALSE; backfilled TRUE for '
    'existing operators by migration 240.';
COMMENT ON COLUMN public.dashboard_users.can_manage_email_controls IS
    'Operator may toggle the retention-email feature flag / kill-switch / capture-mode and '
    'clear/test the capture outbox via /admin/billing/email-controls. Default FALSE; backfilled '
    'TRUE for existing operators by migration 240.';
COMMENT ON COLUMN public.dashboard_users.can_register_webhook IS
    'Operator may register/re-register the Stripe webhook endpoint (the verb that obtains a '
    'fresh signing secret). Default FALSE; backfilled TRUE for existing operators by mig 240.';

-- ── B. payment branding columns on the platform singleton ─────────────────────────
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS support_email         TEXT;
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS billing_from_address  TEXT;
ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS billing_from_name     TEXT;

COMMENT ON COLUMN public.platform_settings.support_email IS
    'Where billing/support questions land (shown in dunning + retention emails). Nullable; '
    'falls back to platform_settings.notify_email when unset. Operator-written from the billing '
    'dashboard (can_manage_branding).';
COMMENT ON COLUMN public.platform_settings.billing_from_address IS
    'The From: address for billing/dunning/retention outbound mail. Nullable; falls back to '
    'welcome_from_address (mig 174) then the SMTP relay default. Operator-written '
    '(can_manage_branding).';
COMMENT ON COLUMN public.platform_settings.billing_from_name IS
    'The display name on billing outbound mail (e.g. "FaultLine Billing"). Nullable; falls back '
    'to the product name. Operator-written (can_manage_branding).';
