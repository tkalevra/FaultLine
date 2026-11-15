-- Migration 211: COMPED ACCOUNTS — an operator lever to bill an account ZERO, auditably
--                (CLOSED-SOURCE SaaS layer)
-- Date: 2026-08-03
-- Purpose: the owner asked for operator-level account control — "comp'd accounts such as my
--          own" — alongside "extend trial". This migration lands the COMP half.
--
--   A comp does NOT delete or zero-price the bill's line items. The account's real bill is
--   computed exactly as it would be for a paying customer and then ONE explicit credit line
--   waives it (saas/entitlements._apply_comp). So the operator can always see what the comp
--   COSTS (billing_view.comped_value / gross_subtotal), the customer's invoice EXPLAINS the
--   $0 instead of silently showing nothing, and clearing the comp restores the true bill with
--   no re-derivation.
--
--   comped       — the flag billing honours (with comp_until).
--   comp_reason  — WHY. Required by the setter; an unexplained $0 bill is exactly the silent
--                  zero this feature exists to prevent.
--   comp_by      — WHO (the server-derived operator session identity, never client-supplied).
--   comp_at      — WHEN it was granted.
--   comp_until   — optional EXPIRY (DATE). NULL = indefinite. A lapsed comp bills again on its
--                  own — no reaper, no job: comp_state() compares against today at read time.
--
--   HISTORY (as opposed to current state) is the append-only public.account_ledger: every
--   set/clear writes a 'comp' / 'comp_off' row carrying the waived monthly amount, the actor
--   and the reason. The security ledger (public.saas_audit_log) gets 'account.comp' /
--   'account.comp_off'. Neither is added by this migration — both tables already exist
--   (migration 139) and neither constrains `action` to an enum.
--
-- NOT comp-able: account_kind='individual_house' (the shared Singles grouping) — comping the
-- house account would comp every self-serve individual inside it at once. The setter REFUSES
-- it with a 400; the per-person lever is the trial extension (individual_users.trial_ends_at).
--
-- Read/written with: saas/entitlements.py (comp_state / set_comp / _apply_comp),
-- saas/admin.py (operator endpoints, require_operator), saas/invoicing.py (invoice credit line).
--
-- Control-plane object in public.* ONLY. public.accounts has NO tenant-schema counterpart —
-- it is not in src/provisioning/templates/user_schema.sql and is never read from a tenant
-- search_path — so there is deliberately NO template change paired with this migration.
-- Idempotent (ADD COLUMN IF NOT EXISTS); safe to re-run. Additive: every existing account
-- reads comped=false, i.e. byte-for-byte today's billing.

ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS comped BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS comp_reason TEXT;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS comp_by TEXT;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS comp_at TIMESTAMPTZ;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS comp_until DATE;

-- A comp must always carry its WHY. Enforced in the app (400 without a reason) AND here, so no
-- direct-SQL path can leave a silently-unexplained $0 bill behind.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'accounts_comp_reason_required'
    ) THEN
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_comp_reason_required
            CHECK (comped = false OR (comp_reason IS NOT NULL AND length(btrim(comp_reason)) > 0));
    END IF;
END $$;

-- Fast lookup of the live give-away roster for the operator billing overview.
CREATE INDEX IF NOT EXISTS idx_accounts_comped
    ON public.accounts (comped) WHERE comped;

COMMENT ON COLUMN public.accounts.comped IS
    'Operator COMP flag: the account bills ZERO while set (and comp_until is unexpired). The '
    'bill is still computed in full and waived by ONE explicit credit line — never silently '
    'zeroed. Set/cleared only by saas/entitlements.set_comp (operator-gated, reason required, '
    'ledgered as comp/comp_off + audited as account.comp).';
COMMENT ON COLUMN public.accounts.comp_until IS
    'Optional comp EXPIRY (DATE, NULL = indefinite). Evaluated at READ time by '
    'entitlements.comp_state — a lapsed comp resumes billing with no job or reaper involved.';
