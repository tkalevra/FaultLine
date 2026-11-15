-- Migration 139: SaaS per-account (per-SEAT) licensing model — entitlements + ledger
-- (CLOSED-SOURCE SaaS layer, Phase 9). Date: 2026-07-05
--
-- Re-architects the licensing model: the account TYPE moves from the TENANT
-- (accounts.account_type, migration 138) to each individual SEAT (account_users.seat_type).
-- A tenant now holds a MIX of typed seats.
--
--   seat_type ∈ {standard, advanced, expert, agent}   (DEFAULT 'standard')
--   ladder (upgrade-only): standard < advanced < expert < agent
--   ONLY seat_type='agent' can lock ingest (the train->serve freeze) — per SEAT.
--
-- Base allowance: 5 Standard seats, INCLUDED + IMMUTABLE (base_quantity=5, is_base=true).
-- Provision beyond the base à-la-carte or via bundles → account_entitlements capacity.
-- Every add / upgrade / bundle / removal writes an append-only public.account_ledger row.
-- Contract term per tenant (accounts.term_months default 6 + accounts.expires_at).
--
-- accounts.account_type (138) is LEFT IN PLACE but is NO LONGER the capability tier —
-- nothing reads it as the ingest gate anymore (the gate keys off account_users.seat_type).
--
-- Control-plane columns/tables in public.* (read at request time). NEVER seeded into a
-- tenant schema, NEVER read from a tenant search_path. Idempotent + forward-only + backfilled.
-- Read with: DEV/DESIGN-saas-licensing-model.md

-- ── per-SEAT type + base flag ───────────────────────────────────────────────────────
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS seat_type TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS is_base BOOLEAN NOT NULL DEFAULT false;
-- Scheduled-at-renewal DOWNGRADE (never mid-term): a set flag changes nothing now, it schedules
-- the seat_type change for the next cycle (renewal) and is cancellable before it applies.
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS scheduled_seat_type TEXT;
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS scheduled_effective TIMESTAMPTZ;
ALTER TABLE public.account_users
    ADD COLUMN IF NOT EXISTS scheduled_by TEXT;

UPDATE public.account_users SET seat_type = 'standard' WHERE seat_type IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'account_users_seat_type_check'
    ) THEN
        ALTER TABLE public.account_users
            ADD CONSTRAINT account_users_seat_type_check
            CHECK (seat_type IN ('standard', 'advanced', 'expert', 'agent'));
    END IF;
END $$;

-- ── contract term + expiry (per tenant) ─────────────────────────────────────────────
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS term_months INTEGER NOT NULL DEFAULT 6;
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
-- TAX is OPERATOR-configurable and currently OFF (default 0 / not registered). Kept as a rate
-- the operator sets per account; NO tax is computed yet — the fields just must not be lost.
ALTER TABLE public.accounts
    ADD COLUMN IF NOT EXISTS tax_rate NUMERIC(6,4) NOT NULL DEFAULT 0;

-- Backfill expiry for any account missing one: created_at + the contract term.
UPDATE public.accounts
   SET expires_at = created_at + (term_months || ' months')::interval
 WHERE expires_at IS NULL;

-- ── licensed capacity per type (the entitlement ledger of "what you may run") ────────
CREATE TABLE IF NOT EXISTS public.account_entitlements (
    account_id     UUID NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    seat_type      TEXT NOT NULL CHECK (seat_type IN ('standard', 'advanced', 'expert', 'agent')),
    quantity       INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    base_quantity  INTEGER NOT NULL DEFAULT 0 CHECK (base_quantity >= 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, seat_type),
    CHECK (quantity >= base_quantity)
);

-- ── append-only billing ledger (every provisioning event is a dated line item) ──────
-- STRICTLY append-only / immutable (tax + audit integrity): no UPDATE/DELETE path — a
-- correction is a NEW offsetting/credit row that references the original via reverses_ledger_id.
-- Each row is an AUTHORIZATION record: the typed signature (name) + the server-side SESSION
-- identity (actor_user/actor_role, never client-supplied) + amount/currency/date for records.
CREATE TABLE IF NOT EXISTS public.account_ledger (
    ledger_id          BIGSERIAL PRIMARY KEY,
    account_id         UUID NOT NULL REFERENCES public.accounts(account_id) ON DELETE CASCADE,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action             TEXT NOT NULL,   -- provision | bundle | upgrade | removal | reversal | base_grant
    seat_type          TEXT,
    quantity           INTEGER NOT NULL DEFAULT 0,
    unit_price         NUMERIC(12,2) NOT NULL DEFAULT 0,
    amount             NUMERIC(12,2) NOT NULL DEFAULT 0,
    currency           TEXT NOT NULL DEFAULT 'CAD',   -- CAD only (no USD anywhere in the model)
    tax_rate           NUMERIC(6,4),    -- nullable — future tax handling (no computation now)
    tax_amount         NUMERIC(12,2),
    term_months        INTEGER,
    contract_end       TIMESTAMPTZ,     -- the charge is honoured/billed monthly THROUGH this date
    effective_date     TIMESTAMPTZ,     -- when the change applies (now for adds; renewal for downgrades)
    actor              TEXT,            -- 'operator' | 'tenant:<acct>' | 'system'
    actor_user         TEXT,           -- server-side session identity (dashboard/operator user id)
    actor_role         TEXT,           -- 'operator' | 'tenant'
    signature          TEXT,           -- typed name authorizing the billable action
    attestation        TEXT,           -- "Taken on <date> by <name> (<user>), valid until <end>."
    reverses_ledger_id BIGINT REFERENCES public.account_ledger(ledger_id),
    billing_note       TEXT,
    note               TEXT
);
CREATE INDEX IF NOT EXISTS idx_account_ledger_account_ts
    ON public.account_ledger (account_id, ts);

-- Defensive ADD COLUMN for a table that pre-existed an earlier draft of this migration.
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'CAD';
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS tax_rate NUMERIC(6,4);
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS tax_amount NUMERIC(12,2);
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS contract_end TIMESTAMPTZ;
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS effective_date TIMESTAMPTZ;
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS actor_user TEXT;
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS actor_role TEXT;
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS signature TEXT;
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS attestation TEXT;
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS reverses_ledger_id BIGINT;
ALTER TABLE public.account_ledger ADD COLUMN IF NOT EXISTS billing_note TEXT;

-- APPEND-ONLY: block UPDATE + DELETE on the ledger (a billing record is immutable).
CREATE OR REPLACE FUNCTION public.account_ledger_append_only()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'account_ledger is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_account_ledger_no_update ON public.account_ledger;
CREATE TRIGGER trg_account_ledger_no_update
    BEFORE UPDATE OR DELETE ON public.account_ledger
    FOR EACH ROW EXECUTE FUNCTION public.account_ledger_append_only();

-- ── BACKFILL: seed the base-5 standard entitlement for ENTERPRISE accounts ──────────
-- ⚠️ THIS RUNS ON EVERY CONTAINER START, NOT ONCE. docker-entrypoint.sh loops over
-- migrations/*.sql unconditionally on every boot; idempotency is the contract. So an
-- unfiltered backfill is not a historical artefact — it is a statement that re-fires forever.
--
-- It used to be `FROM public.accounts a` with NO KIND FILTER, and it predates the 'team' kind
-- (migration 155) and 'clinical' (170). Every boot it therefore handed EVERY account of EVERY
-- kind a `standard 5/5` row, and that is money, not a label:
--
--     _team_billing_view bills Standard on max(licensed, occupied) - TEAM_INCLUDED_STANDARD.
--     A Team with a phantom standard 5/5 row computes 5 - 2 = 3 billable a-la-carte Standard
--     seats and invoices a two-person Team for three seats it never bought.
--
-- Measured on pre-prod 2026-08-17: 10 of 10 team accounts carried this row, their Plan &
-- Capacity page said "licensed 5 / base 5" four lines under a base-package line saying
-- "2 Standard included", and their invoice read CA$787.50 for a CA$750 package. The rows'
-- created_at clustered on container-restart timestamps, which is what identified this
-- statement rather than any application code path.
--
-- THE FIX IS PER-KIND AMOUNTS, NOT A BLANKET REFUSAL. Seeding NOTHING for Team is not safe
-- either: seats.register_seat gates a standard seat on SUM(entitlements.quantity) and answers
-- 402 "no licensed seat capacity (0 active / 0 licensed)" with no row, so a Team would be unable
-- to register even its first Standard seat. This statement was the crutch nobody knew they were
-- leaning on. Each kind gets ITS OWN allotment, mirroring entitlements.included_standard_for():
--     tenant -> 5   team -> 2
-- Measured: a Team at 2/2 registers exactly two Standard seats, is correctly refused the third
-- ("provision more capacity first" — that seat is à-la-carte), invoices exactly the base package
-- with no add-on line, and reads licensed 2 / base 2 on Plan & Capacity.
--
-- CLINICAL and INDIVIDUAL_HOUSE keep the legacy 5 DELIBERATELY. Their allotment is 0, but the
-- same registration gate applies, and this row is the only reason their seats register at all.
-- Whether a clinic should hold free Standard seats is a PRICING question for the owner, and
-- "correcting" it here would lock a clinic out of provisioning its own doctors and stop every
-- individual signing up. Left alone, on purpose, and flagged rather than silently changed.
-- Migration 263 repairs the TEAM rows this statement already created.
-- NULL account_kind = a pre-155 row, which can only be Enterprise.
--
-- Wrapped in a DO block that TESTS FOR THE COLUMN, because 139 runs BEFORE 155 adds
-- accounts.account_kind. On a first boot the column does not exist yet; referencing it there
-- would emit an ERROR line the entrypoint reports, and the loud "a NEW migration appearing here
-- means its schema change may NOT have applied" warning must keep meaning something. On a first
-- boot there are also no accounts, so the unfiltered branch is a no-op and safe.
DO $backfill_base5$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'accounts'
                  AND column_name = 'account_kind') THEN
        INSERT INTO public.account_entitlements (account_id, seat_type, quantity, base_quantity)
        SELECT a.account_id, 'standard', al.included, al.included
          FROM public.accounts a
          JOIN (VALUES ('tenant', 5), ('team', 2), ('clinical', 5), ('individual_house', 5))
                 AS al(kind, included)
            ON al.kind = COALESCE(a.account_kind, 'tenant')
        ON CONFLICT (account_id, seat_type) DO NOTHING;
    ELSE
        INSERT INTO public.account_entitlements (account_id, seat_type, quantity, base_quantity)
        SELECT a.account_id, 'standard', 5, 5
          FROM public.accounts a
        ON CONFLICT (account_id, seat_type) DO NOTHING;
    END IF;
END
$backfill_base5$;

-- Flag the first (up to) 5 active standard seats per account as the immutable base-5.
WITH ranked AS (
    SELECT account_id, user_id,
           ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY created_at, user_id) AS rn
      FROM public.account_users
     WHERE status = 'active' AND seat_type = 'standard'
)
UPDATE public.account_users au
   SET is_base = true
  FROM ranked r
 WHERE au.account_id = r.account_id
   AND au.user_id = r.user_id
   AND r.rn <= 5
   AND au.is_base = false;

-- base_grant ledger line for provenance (one per account, idempotent-ish: only if none yet).
-- The ledger line states the allotment that was actually granted. A Team was never granted 5
-- Standard, and claiming it is a false provenance record written onto an APPEND-ONLY table
-- (there is a trigger; it cannot be corrected later, only annotated).
DO $backfill_grant$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'accounts'
                  AND column_name = 'account_kind') THEN
        INSERT INTO public.account_ledger (account_id, action, seat_type, quantity, unit_price, amount, actor, note)
        SELECT a.account_id, 'base_grant', 'standard', al.included, 0, 0, 'system',
               'included base allowance (' || al.included || ' standard)'
          FROM public.accounts a
          JOIN (VALUES ('tenant', 5), ('team', 2), ('clinical', 5), ('individual_house', 5))
                 AS al(kind, included)
            ON al.kind = COALESCE(a.account_kind, 'tenant')
         WHERE NOT EXISTS (SELECT 1 FROM public.account_ledger l
                            WHERE l.account_id = a.account_id AND l.action = 'base_grant');
    ELSE
        INSERT INTO public.account_ledger (account_id, action, seat_type, quantity, unit_price, amount, actor, note)
        SELECT a.account_id, 'base_grant', 'standard', 5, 0, 0, 'system', 'included base allowance (5 standard)'
          FROM public.accounts a
         WHERE NOT EXISTS (SELECT 1 FROM public.account_ledger l
                            WHERE l.account_id = a.account_id AND l.action = 'base_grant');
    END IF;
END
$backfill_grant$;
