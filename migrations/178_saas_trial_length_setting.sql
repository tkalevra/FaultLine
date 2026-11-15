-- Migration 178: operator-configurable FREE-TRIAL LENGTH — CLOSED-SOURCE SaaS layer.
-- Date: 2026-07-15
-- Purpose: turn the first-month-free trial length from a hardcoded constant (saas/plans.py
--          TRIAL_DAYS = 30) into an OPERATOR-owned platform setting the global admin can change
--          from the operator console WITHOUT a redeploy. Lands on the SAME control-plane
--          singleton as the offline switch + signup-approval policy (migration 174).
--
--   trial_days — the length, in days, of a new signup's first-month-free trial. 0 = NO free trial
--                (the signup converts to paid immediately). The default is 30 = one month, which
--                keeps the static landing copy ("first month free") accurate at the default value.
--                The application clamps any read to the [0, 365] range (defense in depth) and the
--                operator write endpoint rejects out-of-range values with 400.
--
-- Read/written with: saas/platform_settings.py (get/set), saas/plans.py (trial_days() resolver),
-- saas/user_console.py (stamps trial_ends_at at signup + at approval), saas/admin.py (operator
-- get/set endpoint, require_operator).
--
-- Control-plane public.* ONLY. NEVER seeded into a tenant schema, NEVER read from a tenant
-- search_path — additive; does not weaken per-tenant isolation. Idempotent (ADD COLUMN IF NOT
-- EXISTS); safe to re-run.

ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS trial_days INTEGER NOT NULL DEFAULT 30;

COMMENT ON COLUMN public.platform_settings.trial_days IS
    'Operator-configurable first-month-free trial length in DAYS (default 30 = one month; 0 = no '
    'free trial). Read by saas/plans.py::trial_days() when stamping individual_users.trial_ends_at '
    'at signup/approval. Clamped to [0,365] by the app; the operator write endpoint rejects '
    'out-of-range with 400. Changing it takes effect on the NEXT signup — no redeploy.';
