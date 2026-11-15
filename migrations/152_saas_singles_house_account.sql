-- Migration 152: SaaS "Singles" individual house account + account_kind (CLOSED-SOURCE SaaS)
-- Date: 2026-07-09
-- Purpose: fix the individual billing model. Previously EACH self-serve individual got its OWN
--          single-seat house account (slug `ind-<name>`, plan='individual', seat_limit 5), and
--          the operator billing overview priced every one of them at the TENANT base package
--          (CAD $1500/mo) — an enterprise seat rate, wrong for a self-serve individual.
--
-- Owner-ratified model: ONE shared house account ("Singles") holds ALL self-serve individuals
-- as SEATS. It is a distinct account KIND (`individual_house`) that is EXEMPT from the tenant
-- MRR / seat costing (contributes ZERO tenant $). Each individual KEEPS its own per-user
-- `faultline_<uuid>` schema (isolation preserved) — Singles is only the billing/governance group.
-- Individuals are priced by the INDIVIDUAL plan catalog (saas/plans.py), NOT price_for() tenant
-- seat rates; a new individual defaults to the free/entry tier ($0).
--
-- This migration:
--   (A) adds accounts.account_kind ∈ {tenant, individual_house} (DEFAULT 'tenant');
--   (B) creates the single shared Singles house account (stable slug 'singles', large seat cap),
--       WITHOUT the base-5 $1500 entitlement (it is exempt from the tenant billing model);
--   (C) migrates every EXISTING per-individual house account's seat(s) + flu_ tokens + login
--       onto Singles, then ARCHIVES the now-empty per-individual account (archive, not delete —
--       the append-only account_ledger blocks hard-delete). A transferred individual already
--       living under a REAL corporate tenant is NOT touched (only ind-*/plan='individual' houses).
--
-- Control-plane objects in public.* (read at request time). NEVER seeded into a tenant schema,
-- NEVER read from a tenant search_path. Idempotent (IF NOT EXISTS / guarded); safe to re-run.

-- ── (A) account KIND — separates a paying tenant from the individual-house grouping ──────────
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS account_kind TEXT NOT NULL DEFAULT 'tenant';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'accounts_account_kind_check'
    ) THEN
        ALTER TABLE public.accounts
            ADD CONSTRAINT accounts_account_kind_check
            CHECK (account_kind IN ('tenant', 'individual_house'));
    END IF;
END $$;

-- ── (B) the ONE shared Singles house account (idempotent by slug) ────────────────────────────
-- Large seat cap so every self-serve individual can be a seat. No base-5 entitlement is seeded
-- (it must NOT carry the $1500 tenant base package — it is billing-exempt). plan is a label only.
INSERT INTO public.accounts (name, slug, plan, account_type, account_kind, seat_limit, status)
SELECT 'Singles', 'singles', 'individual_house', 'standard', 'individual_house', 100000, 'active'
WHERE NOT EXISTS (SELECT 1 FROM public.accounts WHERE slug = 'singles');

-- Ensure an existing 'singles' row is correctly kinded + active + high-capacity (self-heal).
UPDATE public.accounts
   SET account_kind = 'individual_house',
       seat_limit   = GREATEST(seat_limit, 100000),
       status       = CASE WHEN status = 'archived' THEN 'active' ELSE status END,
       updated_at   = NOW()
 WHERE slug = 'singles';

-- ── (C) migrate EXISTING per-individual house accounts onto Singles ──────────────────────────
DO $$
DECLARE
    v_singles UUID;
    r_house   RECORD;
    r_seat    RECORD;
BEGIN
    SELECT account_id INTO v_singles
      FROM public.accounts
     WHERE slug = 'singles' AND account_kind = 'individual_house'
     LIMIT 1;
    IF v_singles IS NULL THEN
        RAISE NOTICE 'migration 152: Singles account missing — skipping individual migration';
        RETURN;
    END IF;

    FOR r_house IN
        SELECT account_id
          FROM public.accounts
         WHERE account_kind = 'tenant'                       -- not already a house grouping
           AND account_id <> v_singles
           AND status = 'active'                             -- retired ind-* houses left as-is
           AND (slug LIKE 'ind-%' OR plan = 'individual')    -- a per-individual house account
    LOOP
        -- Move each seat (and its flu_ connection tokens) from the per-individual house → Singles.
        -- account_user_tokens has a composite FK (account_id,user_id)→account_users with ON DELETE
        -- (not ON UPDATE) CASCADE, so we snapshot → delete → move the seat → re-insert the tokens
        -- (token_hash/prefix preserved so the individual's live connection keeps working).
        FOR r_seat IN
            SELECT user_id
              FROM public.account_users
             WHERE account_id = r_house.account_id
        LOOP
            CREATE TEMP TABLE _tok152 ON COMMIT DROP AS
                SELECT * FROM public.account_user_tokens
                 WHERE account_id = r_house.account_id AND user_id = r_seat.user_id;

            DELETE FROM public.account_user_tokens
             WHERE account_id = r_house.account_id AND user_id = r_seat.user_id;

            UPDATE public.account_users
               SET account_id = v_singles, is_base = FALSE
             WHERE account_id = r_house.account_id AND user_id = r_seat.user_id;

            INSERT INTO public.account_user_tokens
                (token_id, account_id, user_id, token_prefix, token_hash,
                 created_at, revoked_at, last_used_at)
                SELECT token_id, v_singles, user_id, token_prefix, token_hash,
                       created_at, revoked_at, last_used_at
                  FROM _tok152;

            DROP TABLE _tok152;
        END LOOP;

        -- Repoint the individual login(s) onto Singles.
        UPDATE public.individual_users
           SET account_id = v_singles
         WHERE account_id = r_house.account_id;

        -- Archive the now-vacated per-individual house account (archive, NOT delete: the
        -- append-only account_ledger trigger blocks a hard delete of its base_grant row).
        UPDATE public.accounts
           SET status = 'archived', updated_at = NOW()
         WHERE account_id = r_house.account_id;
    END LOOP;
END $$;
