-- ═══════════════════════════════════════════════════════════════════════════════════════
-- 263 — REPAIR: a TEAM account's Standard entitlement row holds the ENTERPRISE allotment,
--        and it is on the invoice.
--
-- WHAT WENT WRONG. Migration 139's backfill was
--     INSERT ... SELECT a.account_id, 'standard', 5, 5 FROM public.accounts a
-- with NO KIND FILTER, and docker-entrypoint.sh re-runs every migration on EVERY container
-- start — idempotency is the contract, so an unfiltered backfill is not a historical artefact,
-- it is a statement that re-fires forever. 139 predates the 'team' kind (155), so every boot it
-- handed every Team a `standard 5/5` row.
--
-- THAT IS MONEY, NOT A LABEL. ``entitlements._team_billing_view`` bills Standard on
-- ``max(licensed, occupied) - TEAM_INCLUDED_STANDARD``. A Team carrying 5/5 computes 5 - 2 = 3
-- billable à-la-carte Standard seats and invoices a two-person Team for three seats it never
-- bought. Measured on pre-prod 2026-08-17: 10 of 10 team accounts affected; Plan & Capacity read
-- "licensed 5 / base 5" four lines below a base-package line reading "2 Standard included"; the
-- invoice read CA$787.50 for a CA$750 package. The rows' created_at clustered exactly on
-- container-restart timestamps, which is what identified the migration rather than any code path.
--
-- 139 now seeds each kind's OWN allotment (tenant 5, team 2), and so does
-- ``entitlements.seed_base_entitlement``, so no new wrong rows appear. This repairs the ones
-- already written.
--
-- ── WHY THE CORRECT STATE IS 2/2 AND NOT "NO ROW" ────────────────────────────────────
-- Three things have to agree and, before this, none of them did:
--   * ``seats.register_seat`` gates a standard seat on SUM(entitlements.quantity). With NO row a
--     Team cannot register even its FIRST Standard seat (measured: 402 "no licensed seat
--     capacity (0 active / 0 licensed)"). Deleting the row is therefore NOT the fix — it would
--     trade an overcharge for a lockout.
--   * ``_team_billing_view`` bills max(licensed, occupied) - TEAM_INCLUDED_STANDARD.
--   * the pricing model says the base package INCLUDES 2 Standard seats.
-- A 2/2 row satisfies all three, measured: two seats register, the third is correctly refused as
-- à-la-carte, the invoice is exactly the base package, and Plan & Capacity agrees with the
-- payload's own included_standard.
--
-- ── WHAT THIS TOUCHES, AND WHAT IT REFUSES TO ────────────────────────────────────────
--     purchased    := quantity - base_quantity          (what the customer actually bought)
--     new_base     := 2                                 (TEAM_INCLUDED_STANDARD)
--     new_quantity := GREATEST(new_base + purchased, active standard seats)
--
-- The occupancy floor is not decoration: a Team that was allowed to register more seats while it
-- held the phantom capacity must not have licensed capacity pulled out from under an occupied
-- seat. ``_billable_count`` already treats occupancy as a floor, so the floor changes no bill —
-- it only prevents a live seat becoming unlicensed.
--
-- Verified on a real database: a Team that bought nothing goes 5/5 -> 2/2 and its invoice drops
-- CA$787.50 -> CA$750.00, exactly its package. A Team that really bought three Standard goes
-- 8/5 -> 5/2, still holds its three, and drops CA$825.00 -> CA$787.50 — it was being billed for
-- the three phantom seats as well. An ENTERPRISE row is untouched. EVERY MOVEMENT IS DOWNWARD.
--
-- ⚠️ CLINICAL AND INDIVIDUAL_HOUSE ARE DELIBERATELY NOT TOUCHED, and this is the honest
-- boundary of this repair rather than an oversight. Their included allotment is 0, so "repairing"
-- them means removing the row — and the registration gate above then refuses every Standard seat,
-- which for the Singles house account is EVERY INDIVIDUAL SIGNUP. Their 5/5 row is also an
-- UNDER-charge, not an over-charge (``invoicing._clinical_line_items`` bills quantity -
-- base_quantity, so those five are free), and deciding whether a clinic should hold free Standard
-- seats is a PRICING question for the owner. Changing it here would start invoicing clinics for
-- seats they have been using at no cost. Left alone, flagged, and NOT silently altered.
--
-- IDEMPOTENT: the WHERE clause tests for the defect, not for a version marker — which matters,
-- because this file re-runs on every container start.
-- SAFE ON A FRESH DB: guarded on the tables and the account_kind column existing.
-- ═══════════════════════════════════════════════════════════════════════════════════════

DO $repair_team_base5$
DECLARE
    fixed int := 0;
BEGIN
    IF to_regclass('public.account_entitlements') IS NULL
       OR to_regclass('public.accounts') IS NULL
       OR to_regclass('public.account_users') IS NULL
       OR NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = 'accounts'
                         AND column_name = 'account_kind') THEN
        RAISE NOTICE '263: control plane not present yet — nothing to repair';
        RETURN;
    END IF;

    -- 2 mirrors entitlements.TEAM_INCLUDED_STANDARD. Written out rather than read from the app so
    -- the repair does not depend on a running process; migration 139 carries the same table and
    -- tests/test_console_prices_trace_to_the_engine.py pins both against the engine.
    WITH target AS (
        SELECT e.account_id,
               2 AS new_base,
               GREATEST(
                   2 + (e.quantity - e.base_quantity),
                   COALESCE((SELECT count(*) FROM public.account_users u
                              WHERE u.account_id = e.account_id
                                AND u.status = 'active'
                                AND u.seat_type = 'standard'), 0)
               )::int AS new_quantity
          FROM public.account_entitlements e
          JOIN public.accounts a ON a.account_id = e.account_id
         WHERE e.seat_type = 'standard'
           AND a.account_kind = 'team'
           AND e.base_quantity <> 2          -- the defect itself, not a version marker
    ),
    upd AS (
        UPDATE public.account_entitlements e
           SET quantity      = t.new_quantity,
               base_quantity = t.new_base,
               updated_at    = NOW()
          FROM target t
         WHERE e.account_id = t.account_id
           AND e.seat_type  = 'standard'
        RETURNING 1
    )
    SELECT count(*) INTO fixed FROM upd;

    IF fixed > 0 THEN
        RAISE NOTICE '263: repaired % team standard row(s) from the ENTERPRISE allotment to '
                     'TEAM_INCLUDED_STANDARD; every affected invoice goes DOWN', fixed;
    END IF;
END
$repair_team_base5$;
