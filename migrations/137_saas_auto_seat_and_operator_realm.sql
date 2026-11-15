-- Migration 137: SaaS auto-seat provisioning flag + operator auth realm
-- (CLOSED-SOURCE SaaS layer). Date: 2026-07-04
--
-- PART A — AUTO-SEAT PROVISIONING:
--   accounts.auto_provision_seats (default true). When true, an ACCOUNT-principal request
--   whose claimed X-OpenWebUI-User-Id is not yet a seat auto-registers it through the SAME
--   cap-enforcing path as register_seat (row-locked count vs seat_limit). Over cap = HARD
--   reject. When false, an unknown UUID is rejected exactly as today (manual/token only).
--
-- PART B — OPERATOR AUTH REALM:
--   The SaaS super-admin (operator) is represented as a public.dashboard_users row with
--   account_id IS NULL AND role='operator' — a HUMAN login (argon2id + signed session + MFA),
--   scoped to ALL accounts. Loosen account_id to NULLable (operator rows have no single
--   account); keep the ON DELETE CASCADE for tenant rows. A CHECK pins the realm split:
--   role='operator' <=> account_id IS NULL, so a tenant/account-scoped login can NEVER be
--   minted as an operator at the DB layer (defense in depth behind the app-level auth gate).
--
-- All columns are CONTROL-PLANE fields in public.* (read at request time like migration 131).
-- NEVER seeded into a tenant schema, NEVER read from a tenant search_path — additive, does not
-- weaken the per-tenant DATA rule (SET search_path WITHOUT public).
--
-- Read with: DEV/DESIGN-saas-dashboards-and-observability.md, DEV/SECURITY-saas-multitenancy.md.
-- Idempotent (IF NOT EXISTS / guarded); safe to re-run.

-- ── PART A: auto-seat provisioning flag ────────────────────────────────────────────
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS auto_provision_seats BOOLEAN NOT NULL DEFAULT true;

-- ── PART B: operator dashboard-user realm ──────────────────────────────────────────
-- Operator rows carry NO single account; loosen the NOT NULL (idempotent — re-run is inert).
ALTER TABLE public.dashboard_users
    ALTER COLUMN account_id DROP NOT NULL;

-- Realm split guard: an operator login has account_id NULL; every tenant login has a real
-- account. This makes "an account key can never mint an operator" true at the DB layer too —
-- /admin/dashboard-users always supplies an account_id, so role='operator' there fails the
-- CHECK. Existing rows (role in admin/viewer, account_id NOT NULL) validate clean.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'dashboard_users_operator_scope_check'
    ) THEN
        ALTER TABLE public.dashboard_users
            ADD CONSTRAINT dashboard_users_operator_scope_check
            CHECK (
                (role = 'operator' AND account_id IS NULL)
                OR (role <> 'operator' AND account_id IS NOT NULL)
            );
    END IF;
END $$;

-- Fast lookup of operator logins (small set; partial index keeps it tiny).
CREATE INDEX IF NOT EXISTS idx_dashboard_users_operator
    ON public.dashboard_users (username)
    WHERE role = 'operator' AND status = 'active';
