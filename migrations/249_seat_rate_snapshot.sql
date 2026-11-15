-- 249_seat_rate_snapshot.sql — SNAPSHOT THE SEAT RATE AT PURCHASE + make the rate card an
-- operator setting. CLOSED-SOURCE SaaS layer. Control-plane public.* ONLY.
-- Idempotent; safe to re-run (every statement is IF NOT EXISTS / guarded / WHERE-gated).
--
-- ══════════════════════════════════════════════════════════════════════════════════════
-- WHY THIS EXISTS — THE OWNER'S RULE
-- ══════════════════════════════════════════════════════════════════════════════════════
--
--   "any changes to those amounts is ONLY for new sign-ups, historic must be maintained
--    as it's accounting!"                                              — owner, 2026-08-12
--
--   "Snapshot the rate per entitlement — add a price column to account_entitlements,
--    stamped at purchase. Existing customers keep the rate they bought at; new purchases
--    use the current rate."                                            — owner, 2026-08-12
--
-- THE DEFECT THIS CLOSES. ``public.account_entitlements`` carried NO price at all — its
-- columns were exactly (account_id, seat_type, quantity, base_quantity, created_at,
-- updated_at). So ``saas/invoicing.py::build_invoice`` -> ``entitlements.billing_view`` ->
-- ``entitlements.price_for()`` resolved the rate LIVE, out of the in-code ``_RATE_TABLE``,
-- at EVERY invoice build. Editing a seat rate therefore re-priced every existing customer
-- on their next invoice — retroactively rewriting what a signed customer agreed to pay.
-- That is the accounting violation the owner's rule forbids, and it becomes far more
-- reachable the moment the rate card is editable from the operator console (below).
--
-- ══════════════════════════════════════════════════════════════════════════════════════
-- PART 1 — the per-entitlement PRICE SNAPSHOT
-- ══════════════════════════════════════════════════════════════════════════════════════
--
-- CENTS, INTEGER, NEVER FLOAT. The existing ``_RATE_TABLE`` holds floats (10.0 / 12.5 / …).
-- That is a wart being CONTAINED here, not propagated: money that is stored gets stored as
-- an integer number of cents. Binary floating point cannot represent 12.5-style rates
-- exactly at scale, and an accounting record is exactly where that stops being academic.
--
-- NULLABLE WITH NO DEFAULT — deliberately. A ``DEFAULT 0`` would make every row written by
-- a path that does not yet stamp look like a CONFIRMED rate of zero, i.e. free service,
-- silently. NULL means "this row carries no snapshot"; the resolver treats that as
-- "fall back to the configured rate AND SAY SO", never as zero.
--
-- ``price_source`` is the DISCRIMINATOR, and it is the whole reason the backfill below is
-- honest rather than a fabrication:
--     'snapshot'        — stamped at the moment capacity was purchased. Real evidence.
--     'legacy_backfill' — written by THIS migration, from TODAY's rate card, onto rows that
--                         predate the snapshot column.
--
-- ⚠️ THE BACKFILL IS AN ASSUMPTION, STATED PLAINLY. We do NOT know what these customers
-- were actually quoted. No record of the rate in force at their purchase exists anywhere —
-- that is precisely the defect. Today's rate card is the best available evidence, and it is
-- correct for every account provisioned since the rates last changed (which, at the time of
-- writing, is all of them: the rates have never been edited, because until this migration
-- there was no way to edit them). 'legacy_backfill' exists so that a future reader — an
-- accountant, an auditor, or the next engineer — can tell an INFERRED price apart from an
-- OBSERVED one and does not mistake this migration's guess for a purchase record.

ALTER TABLE public.account_entitlements
    ADD COLUMN IF NOT EXISTS unit_price_cents    INTEGER,
    ADD COLUMN IF NOT EXISTS unit_price_currency TEXT,
    ADD COLUMN IF NOT EXISTS price_source        TEXT,
    ADD COLUMN IF NOT EXISTS price_set_at        TIMESTAMPTZ;

-- CHECK: the discriminator is a closed vocabulary. NULL is permitted (an unstamped row).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = to_regclass('public.account_entitlements')
                      AND conname  = 'account_entitlements_price_source_check') THEN
        ALTER TABLE public.account_entitlements
            ADD CONSTRAINT account_entitlements_price_source_check
            CHECK (price_source IS NULL OR price_source IN ('snapshot', 'legacy_backfill'));
    END IF;
END $$;

-- CHECK: a stamped price is never negative. ``>= 0`` rather than ``> 0`` ON PURPOSE — the
-- constraint must not be able to ABORT a provisioning transaction. Refusing a zero/negative
-- rate is the SETTER's job (saas/platform_settings.set_seat_rates), and the STAMPER declines
-- to write a non-positive resolution at all (it leaves NULL and logs LOUD), so a 0 here
-- would already be an anomaly — one we want visible in a row, not raised in the middle of a
-- customer's purchase.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = to_regclass('public.account_entitlements')
                      AND conname  = 'account_entitlements_unit_price_cents_check') THEN
        ALTER TABLE public.account_entitlements
            ADD CONSTRAINT account_entitlements_unit_price_cents_check
            CHECK (unit_price_cents IS NULL OR unit_price_cents >= 0);
    END IF;
END $$;

COMMENT ON COLUMN public.account_entitlements.unit_price_cents IS
  'Per-seat monthly rate IN CENTS in force when this capacity was purchased. NULL = no '
  'snapshot; the invoice resolver falls back to the live configured rate and logs LOUD. '
  'NEVER given a DEFAULT — a default of 0 would read as a confirmed free seat.';
COMMENT ON COLUMN public.account_entitlements.price_source IS
  'snapshot = stamped at purchase (evidence). legacy_backfill = written by migration 249 '
  'from the then-current rate card onto a pre-existing row (an ASSUMPTION, not a record).';

-- ── the BACKFILL ────────────────────────────────────────────────────────────────────
--
-- IDEMPOTENT by ``WHERE unit_price_cents IS NULL`` — a second run matches zero rows.
--
-- SCOPE, and why each exclusion is deliberate:
--   * seat_type ∈ (standard, advanced, expert, agent) ONLY. 'patient' capacity is priced by
--     a completely separate volume ladder (entitlements.patient_unit_price) and never by the
--     seat rate card; stamping it with a rate-card number would invent a price nobody uses.
--   * account_kind 'tenant' and 'clinical' take the Enterprise column; 'team' takes the Team
--     column. That mirrors the live billing code exactly: _tenant_billing_view prices at
--     'tenant', _team_billing_view at 'team', and invoicing._clinical_line_items prices a
--     clinical account's human seats at 'tenant'.
--   * account_kind 'individual_house' is EXCLUDED. A self-serve individual is billed by the
--     INDIVIDUAL plan catalog (saas/plans.py::individual_price_for), never by price_for().
--     Stamping a tenant rate onto those rows would attach a number that no invoice reads and
--     that actively misleads anyone who later looks at the row.
--
-- The literal rates below are the shipped card as of 2026-08-12 (the same numbers that live
-- in saas/platform_settings._SHIPPED_SEAT_RATES_CENTS, which is now their one source of
-- truth in code). They are written out here rather than read from the settings row on
-- purpose: a backfill is a POINT-IN-TIME statement, and it must not silently change meaning
-- if it is ever re-run after an operator edits the card.

WITH card(kind, seat_type, cents) AS (
    VALUES
        ('tenant', 'standard',  1000),   -- CAD 10.00   Enterprise
        ('tenant', 'advanced',  3000),   -- CAD 30.00
        ('tenant', 'expert',    4500),   -- CAD 45.00
        ('tenant', 'agent',     8000),   -- CAD 80.00
        ('team',   'standard',  1250),   -- CAD 12.50   Team
        ('team',   'advanced',  3500),   -- CAD 35.00
        ('team',   'expert',    5000),   -- CAD 50.00
        ('team',   'agent',    10000)    -- CAD 100.00
)
UPDATE public.account_entitlements e
   SET unit_price_cents    = card.cents,
       unit_price_currency = 'CAD',
       price_source        = 'legacy_backfill',
       price_set_at        = NOW(),
       updated_at          = NOW()
  FROM public.accounts a, card
 WHERE e.account_id        = a.account_id
   AND e.unit_price_cents IS NULL
   AND card.seat_type      = e.seat_type
   AND card.kind           = CASE a.account_kind
                                 WHEN 'team' THEN 'team'
                                 ELSE 'tenant'
                             END
   AND a.account_kind <> 'individual_house';

-- ══════════════════════════════════════════════════════════════════════════════════════
-- PART 2 — the rate card as an OPERATOR SETTING
-- ══════════════════════════════════════════════════════════════════════════════════════
--
-- One JSONB column on the platform_settings singleton, shaped
--     {"tenant": {"standard": 1000, ...}, "team": {"standard": 1250, ...}}   (CENTS, ints)
--
-- NULLABLE, and NULL means "never configured — use the shipped card". That is the fail-safe
-- direction demanded by the billing core: a rate that cannot be resolved must fall back to
-- the CONFIGURED/shipped rate and scream, never to 0 (free service) and never to a guess.
-- An empty object, a missing kind, a missing seat type, or a junk value are all resolved the
-- same way, per key, by saas/platform_settings.seat_rates().
--
-- Deliberately NOT added to platform_settings._SETTABLE: the generic bulk-save writer applies
-- a raw value with no structural validation, and a rate card needs range + shape checks on
-- every leaf. It has its own writer, ``set_seat_rates``, which validates and refuses 0 or
-- negative, and its own audit row.

ALTER TABLE public.platform_settings
    ADD COLUMN IF NOT EXISTS seat_rates_cents JSONB;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = to_regclass('public.platform_settings')
                      AND conname  = 'platform_settings_seat_rates_cents_check') THEN
        ALTER TABLE public.platform_settings
            ADD CONSTRAINT platform_settings_seat_rates_cents_check
            CHECK (seat_rates_cents IS NULL OR jsonb_typeof(seat_rates_cents) = 'object');
    END IF;
END $$;

COMMENT ON COLUMN public.platform_settings.seat_rates_cents IS
  'Operator-configured per-seat rate card, {account_kind: {seat_type: unit_price_cents}}, '
  'integer CENTS. NULL / missing key = use the shipped default in '
  'saas/platform_settings._SHIPPED_SEAT_RATES_CENTS. Editing this affects NEW purchases '
  'only — existing capacity keeps account_entitlements.unit_price_cents (migration 249).';

-- ── review query, for whoever audits this later ─────────────────────────────────────
-- Rows still carrying no snapshot (these price at the LIVE rate and log a warning at every
-- invoice build — each one is a write path that has not been taught to stamp):
--
--   SELECT a.account_kind, e.seat_type, count(*)
--     FROM public.account_entitlements e
--     JOIN public.accounts a USING (account_id)
--    WHERE e.unit_price_cents IS NULL
--    GROUP BY 1, 2 ORDER BY 1, 2;
--
-- Rows whose price is an ASSUMPTION rather than a purchase record:
--
--   SELECT * FROM public.account_entitlements WHERE price_source = 'legacy_backfill';
