-- Migration 170: CLINICAL ACCOUNT KIND — reframe the patient lane into its OWN account type
--                (CLOSED-SOURCE SaaS layer). Date: 2026-07-12.
--
-- RATIFIED MODEL (owner 2026-07-12): the clinical/patient lane is NOT a bolt-on add-on to a
-- standard/Enterprise account (migration 169's shape) — it is a DISTINCT account_kind='clinical',
-- NET-NEW SIGNUP ONLY (no conversion path from standard/Enterprise). A clinical account has
-- MULTIPLE doctor/professional SUB-ADMINS, each holding their OWN provisioning key; every patient
-- mint TRACES to the minting key AND its owning doctor (attribution). SEPARATION OF DUTIES: the
-- tenant ADMIN governs the doctors (create/disable, grant/revoke a doctor's key ability) but can
-- NEVER mint or HOLD a provisioning key itself — that capability belongs to the doctor sub-admin.
-- Every mint / key-grant / key-revoke / key-use / patient-access is written to a MEDICAL-GRADE
-- APPEND-ONLY audit trail (modeled on public.account_ledger / public.saas_audit_log immutability).
--
-- This migration REFRAMES (does NOT throw away) migration 169's substrate: the 'patient' seat
-- capacity class, account_api_keys.scopes, and accounts.patient_provisioning_enabled all remain
-- and are REUSED. It adds, all idempotent + forward-only + CONTROL-PLANE (public.*) ONLY (never
-- seeded into or read from a tenant schema — does not weaken the per-tenant DATA isolation rule):
--
--   1. 'clinical' widened into the accounts.account_kind CHECK (the DISTINCT account type).
--   2. accounts.account_kind_pricing 'clinical' row ($4000/mo base; the included patient
--      allotment is carried by the seeded account_entitlements(seat_type='patient') block).
--   3. ATTRIBUTION columns on account_users: provisioned_by_key_id + provisioned_by_sub_admin —
--      stamped on a patient mint so doctor 1's patients trace to doctor 1, doctor 2's to doctor 2.
--   4. account_api_keys.owner_dashboard_user_id — the doctor sub-admin who HOLDS a provisioning
--      key (NULL for ordinary account/memory keys). The separation-of-duties anchor: a key
--      carrying 'provision_patients' MUST be owned by a doctor (enforced in saas/accounts.mint_key
--      + saas/clinical.mint_provisioning_key), so an admin can never hold one.
--   5. dashboard_users.can_provision_patients — the admin-granted flag authorizing a DOCTOR
--      sub-admin to mint/hold a provisioning key. Admin GRANTS/REVOKES; doctor mints/holds.
--   6. public.clinical_audit_log + an append-only (BEFORE UPDATE/DELETE → RAISE) trigger: the
--      immutable compliance trail for the clinical account type.
--
-- Read with: DEV/DESIGN-elevated-medical-lane.md, migration 169, migration 139 (append-only
-- ledger + saas_audit_log), migration 152/155/156 (account_kind), migration 159 (dashboard roles).
-- Idempotent (ADD COLUMN IF NOT EXISTS + guarded constraint swaps + IF NOT EXISTS objects).

-- ── 1. Widen the account_kind CHECK to admit 'clinical' ─────────────────────────────
-- account_kind arrives via migration 152 (adds the column, DEFAULT 'tenant') and is progressively
-- widened by 152/155. Guard for the column's existence so this migration is safe even if the
-- 152/155 chain has not been applied yet on a given box (fail-safe forward-only).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'accounts'
          AND column_name = 'account_kind'
    ) THEN
        -- SEED-ORDER REPAIR (first-boot self-sufficiency): the old guard probed pg_constraint
        -- BY NAME GLOBALLY (conname only, any schema). A same-named check on ANY other relation
        -- satisfied that probe while public.accounts itself carried no such constraint, so the
        -- unqualified DROP raised 42704, the whole DO block aborted before the ADD ran, and the
        -- constraint stayed MISSING on every subsequent boot (the failed=9-class wound: never
        -- applied on any first boot). Scope the probe to THIS table via conrelid, then the
        -- drop-recreate below is self-sufficient: it creates the constraint on first boot even
        -- when it never existed here, and re-running is a clean drop+re-add (idempotent).
        -- 152/155's IF-NOT-EXISTS guards remain upstream history; this block OWNS the 4-value
        -- shape ('clinical' included) on every boot that reaches it.
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.accounts'::regclass
              AND conname = 'accounts_account_kind_check'
        ) THEN
            ALTER TABLE public.accounts DROP CONSTRAINT accounts_account_kind_check;
        END IF;
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_account_kind_check
            CHECK (account_kind IN ('tenant', 'team', 'individual_house', 'clinical'));
    END IF;
END $$;

-- ── 2. account_kind_pricing 'clinical' row ($4000/mo base) ──────────────────────────
-- Only if the pricing table exists (migration 156). The included patient allotment is NOT a
-- Standard-seat allowance, so included_standard = 0; the real allotment is a seeded
-- account_entitlements(seat_type='patient', base_quantity=N) block (saas/entitlements.py).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'account_kind_pricing'
    ) THEN
        INSERT INTO public.account_kind_pricing
            (account_kind, base_price_cad, included_standard, seat_cap, currency, note)
        VALUES
            ('clinical', 4000, 0, 0, 'CAD',
             'Clinical account: $4000/mo base (patient allotment included) + per-patient overage 8/6/4 CAD')
        ON CONFLICT (account_kind) DO UPDATE SET
            base_price_cad    = EXCLUDED.base_price_cad,
            included_standard = EXCLUDED.included_standard,
            seat_cap          = EXCLUDED.seat_cap,
            currency          = EXCLUDED.currency,
            note              = EXCLUDED.note;
    END IF;
END $$;

-- ── 3. ATTRIBUTION columns on account_users (patient → minting key + owning doctor) ──
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS provisioned_by_key_id    UUID,
    ADD COLUMN IF NOT EXISTS provisioned_by_sub_admin UUID;

-- FKs added guarded (ON DELETE SET NULL so revoking a key / removing a doctor never orphans or
-- deletes the audit-relevant patient seat row; attribution history stays in clinical_audit_log).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'account_users_provisioned_by_key_id_fkey') THEN
        ALTER TABLE public.account_users
            ADD CONSTRAINT account_users_provisioned_by_key_id_fkey
            FOREIGN KEY (provisioned_by_key_id)
            REFERENCES public.account_api_keys (key_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'account_users_provisioned_by_sub_admin_fkey') THEN
        ALTER TABLE public.account_users
            ADD CONSTRAINT account_users_provisioned_by_sub_admin_fkey
            FOREIGN KEY (provisioned_by_sub_admin)
            REFERENCES public.dashboard_users (id) ON DELETE SET NULL;
    END IF;
END $$;

-- Fast "which patients did this doctor mint" attribution scan.
CREATE INDEX IF NOT EXISTS idx_account_users_provisioned_by_sub_admin
    ON public.account_users (account_id, provisioned_by_sub_admin)
    WHERE provisioned_by_sub_admin IS NOT NULL;

-- ── 4. account_api_keys.owner_dashboard_user_id (the doctor who HOLDS a provisioning key) ──
ALTER TABLE public.account_api_keys
    ADD COLUMN IF NOT EXISTS owner_dashboard_user_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'account_api_keys_owner_dashboard_user_id_fkey') THEN
        ALTER TABLE public.account_api_keys
            ADD CONSTRAINT account_api_keys_owner_dashboard_user_id_fkey
            FOREIGN KEY (owner_dashboard_user_id)
            REFERENCES public.dashboard_users (id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_acct_keys_owner
    ON public.account_api_keys (owner_dashboard_user_id)
    WHERE owner_dashboard_user_id IS NOT NULL;

-- ── 5. dashboard_users.can_provision_patients (admin-granted doctor key ability) ────
-- Default FALSE: a doctor cannot mint/hold a provisioning key until the tenant admin grants it.
ALTER TABLE public.dashboard_users
    ADD COLUMN IF NOT EXISTS can_provision_patients BOOLEAN NOT NULL DEFAULT false;

-- ── 6. public.clinical_audit_log — immutable, medical-grade compliance trail ─────────
CREATE TABLE IF NOT EXISTS public.clinical_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    account_id  UUID NOT NULL,
    actor_type  TEXT,                       -- 'admin' | 'doctor' | 'operator' | 'app' | 'system'
    actor_id    TEXT,                       -- dashboard_user id / operator id / key prefix
    actor_role  TEXT,                       -- the actor's role at time of action
    key_id      UUID,                       -- the provisioning key involved (mint / key events)
    patient_id  TEXT,                       -- the patient seat UUID (mint / access / deprovision)
    action      TEXT NOT NULL,              -- patient_mint | patient_deprovision | key_grant |
                                            -- key_revoke | key_mint | key_use | patient_access
    result      TEXT NOT NULL DEFAULT 'success'
                CHECK (result IN ('success', 'failure', 'denied')),
    detail      TEXT,
    source_ip   TEXT
);

CREATE INDEX IF NOT EXISTS idx_clinical_audit_account_ts
    ON public.clinical_audit_log (account_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_clinical_audit_action_ts
    ON public.clinical_audit_log (action, ts DESC);
CREATE INDEX IF NOT EXISTS idx_clinical_audit_patient
    ON public.clinical_audit_log (patient_id) WHERE patient_id IS NOT NULL;

-- APPEND-ONLY: block UPDATE + DELETE (a compliance record is immutable). Modeled on
-- public.account_ledger_append_only / public.saas_audit_log_immutable (migration 139/141).
CREATE OR REPLACE FUNCTION public.clinical_audit_log_immutable()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'public.clinical_audit_log is append-only — % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_clinical_audit_log_immutable ON public.clinical_audit_log;
CREATE TRIGGER trg_clinical_audit_log_immutable
    BEFORE UPDATE OR DELETE ON public.clinical_audit_log
    FOR EACH ROW EXECUTE FUNCTION public.clinical_audit_log_immutable();
