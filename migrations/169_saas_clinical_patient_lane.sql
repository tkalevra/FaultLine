-- Migration 169: ELEVATED CLINICAL / PATIENT LANE (CLOSED-SOURCE SaaS layer).
-- Date: 2026-07-12
--
-- Owner-vision (DEV/DESIGN-elevated-medical-lane.md): a clinical app holds an ELEVATED account
-- key (flk_, never shown to the doctor) that MINTS A SEAT PER PATIENT. Each patient becomes a
-- physically-isolated ``faultline_<uuid>`` schema (``SET search_path`` WITHOUT ``public`` — the
-- SAME isolation every seat already has, the compliance moat). This lane is a COMPOSITION of
-- shipped primitives; this migration only adds the two-level authority gate + the patient
-- CAPACITY CLASS, mirroring the API add-on lever (migrations 160/161) one level down.
--
-- Three additive, idempotent, forward-only changes — all CONTROL-PLANE (public.*), NEVER seeded
-- into a tenant schema, NEVER read from a tenant search_path (does not weaken the per-tenant
-- DATA rule):
--
--   1. accounts.patient_provisioning_enabled  — the ACCOUNT CEILING (mirrors 160's
--      api_addon_enabled). OFF by default, NOT backfilled: enabling it is a first-class consent
--      moment (base add-on fee + patient-capacity pricing shown BEFORE consent + a typed-name
--      signature attestation + an append-only ledger row — saas/entitlements.py). A bigger consent
--      moment than the API add-on (regulated/PHI-adjacent responsibility) — see DESIGN §1.2/§6.
--
--   2. account_api_keys.scopes TEXT[]  — the KEY-LEVEL GRANT (mirrors 161's least-privilege
--      shape, but on the KEY not the seat). A key needs 'provision_patients' in scopes to call
--      the mint/deprovision verbs. Existing keys default to '{memory}' (today's behavior — the
--      ordinary /v1 memory surface never reads scopes, so this column is ADDITIVE and INERT until
--      read by the new gate). Recommended practice: the app uses a DIFFERENT key for provisioning
--      than for day-to-day memory calls (leaked ordinary key cannot mint; leaked provisioning key
--      need not carry 'memory').
--
--   3. 'patient' widened into the seat_type CHECK on public.account_users AND
--      public.account_entitlements. Patients are their OWN capped capacity class (a full physical
--      schema each) — NOT the human accounts.seat_limit / standard pool, and EXCLUDED from the
--      upgrade LADDER (saas/entitlements.py LADDER stays the 4 human tiers; a patient seat is a
--      peer capacity class, never "upgraded"). register_seat's typed-seat branch already caps a
--      non-standard type against account_entitlements(seat_type='patient').quantity — so the cap
--      is a SAFETY RAIL (a compromised provisioning key hits a clean 402, not a schema fanout
--      incident) as well as the billing lever (DESIGN §1.4/§4).
--
-- The lane also composes BEHIND the general /v1 API add-on (migration 160): an account must have
-- bought /v1 access before it can enable the patient lane on top (enforced in the tenant-console
-- enable path + the mint gate). The two ceilings compose, they are not redundant.
--
-- PAYMENT IS A STUB: this migration + the entitlement functions only STORE consent + capacity.
-- The Lemon Squeezy subscription line lives behind TODO(lemon-squeezy) in saas/entitlements.py /
-- saas/tenant_console.py. Enable/disable append signed ledger rows (action 'patient_addon' /
-- 'patient_addon_off') — no DB change needed (account_ledger.action is free TEXT).
--
-- Read with: DEV/DESIGN-elevated-medical-lane.md, DEV/DESIGN-api-access-addon-lever.md,
-- DEV/DESIGN-per-account-grain-and-api-subject.md, DEV/DESIGN-billing-accounting.md.
-- Idempotent (ADD COLUMN IF NOT EXISTS + guarded constraint swaps); safe to re-run.

-- ── 1. ACCOUNT CEILING + consent-audit columns (mirror migration 160) ───────────────
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS patient_provisioning_enabled       BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS patient_provisioning_enabled_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS patient_provisioning_enabled_by    TEXT,
    ADD COLUMN IF NOT EXISTS patient_provisioning_terms_version TEXT,
    ADD COLUMN IF NOT EXISTS patient_provisioning_disabled_at   TIMESTAMPTZ;

-- Fast "which accounts have the patient lane open" scan for operator oversight.
CREATE INDEX IF NOT EXISTS idx_accounts_patient_provisioning_enabled
    ON public.accounts (account_id) WHERE patient_provisioning_enabled = true;

-- ── 2. KEY-LEVEL SCOPES (additive + inert until the mint gate reads it) ─────────────
ALTER TABLE public.account_api_keys
    ADD COLUMN IF NOT EXISTS scopes TEXT[] NOT NULL DEFAULT '{memory}';

-- Any key row that pre-existed the column default (defensive; ADD COLUMN already applied
-- the default to existing rows, but a NULL from an older partial apply would break the gate).
UPDATE public.account_api_keys SET scopes = '{memory}' WHERE scopes IS NULL;

-- ── 3. Widen 'patient' into the seat_type CHECK constraints (account_users + entitlements) ──
-- account_users: the constraint is named (migration 139). Drop + re-add including 'patient'.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'account_users_seat_type_check') THEN
        ALTER TABLE public.account_users DROP CONSTRAINT account_users_seat_type_check;
    END IF;
    ALTER TABLE public.account_users
        ADD CONSTRAINT account_users_seat_type_check
        CHECK (seat_type IN ('standard', 'advanced', 'expert', 'agent', 'patient'));
END $$;

-- account_entitlements: the CHECK was inline (auto-named account_entitlements_seat_type_check).
-- Drop it if present under either the conventional name or any check referencing seat_type, then
-- re-add the widened, explicitly-named constraint.
DO $$
DECLARE
    cname TEXT;
BEGIN
    SELECT conname INTO cname
      FROM pg_constraint
     WHERE conrelid = 'public.account_entitlements'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) ILIKE '%seat_type%'
     LIMIT 1;
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE public.account_entitlements DROP CONSTRAINT %I', cname);
    END IF;
    ALTER TABLE public.account_entitlements
        ADD CONSTRAINT account_entitlements_seat_type_check
        CHECK (seat_type IN ('standard', 'advanced', 'expert', 'agent', 'patient'));
END $$;
