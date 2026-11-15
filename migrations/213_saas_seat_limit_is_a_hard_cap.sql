-- 213 — accounts.seat_limit is a HARD CAP bound to the account KIND, not a mirror of licensing.
--
-- WHY THIS EXISTS
-- ---------------
-- `_bump_seat_limit` used to run on every capacity change:
--     UPDATE public.accounts SET seat_limit = Σ account_entitlements.quantity
-- That inverted the relationship. A cap is a CONSTRAINT ON licensing; making it a CONSEQUENCE OF
-- licensing meant buying more licences silently raised your own ceiling, and it DESTROYED the
-- Team 15-seat pin (accounts.py sets it at create; the next entitlement change overwrote it).
--
-- Owner's ruling: "that is a hard cap, separate value entirely that's bound to the account type,
-- not in any way linked to their seats beyond a max provision check."
--
-- The code half is already in place: `accounts.hard_seat_cap(kind)` is the single derivation, and
-- `register_seat` now enforces TWO separate ceilings — the hard cap AND live licensed capacity
-- (Σ human entitlements). This migration only repairs rows the old mirror already wrote.
--
-- WHAT THIS DOES, AND WHAT IT DELIBERATELY DOES NOT
-- --------------------------------------------------
-- ONLY rows whose seat_limit is PROVABLY mirror-written are touched — i.e. where the stored value
-- exactly equals Σ human entitlement quantity, which is the arithmetic the mirror performed.
-- Anything else is left ALONE, because a value that differs was set deliberately by an operator
-- through `accounts.set_seat_limit` (measured on the dev control plane: clinical rows at 250 vs
-- Σ 5, and one tenant at 10 vs Σ 5 — all operator intent, none of it ours to overwrite).
--
-- ⚠️ THIS CAN ONLY LOOSEN, AND THAT IS SAFE ONLY BECAUSE THE LICENSED GATE NOW EXISTS.
-- A tenant moves 5 → 100000 (tenant kind is uncapped by design; tenants are limited by what they
-- PAY for, not by a ceiling). Occupancy does not become unlimited: `register_seat` independently
-- refuses a standard seat once `active_seats >= Σ licensed human entitlements`, so the effective
-- limit for that tenant is still 5 — the same number, now enforced by the correct gate with an
-- honest reason ("no licensed seat capacity") instead of a cap pretending to be one.
-- If that licensed gate is ever removed, THIS BACKFILL BECOMES AN OVER-PROVISIONING HOLE.
--
-- NOT BILLING-AFFECTING: no entitlement row, ledger row, quantity or price is read or written
-- here. What an account is billed for is Σ entitlements, which this does not touch.
--
-- REVERSIBLE: the operator lever `accounts.set_seat_limit` sets any account's cap directly, so a
-- wrong outcome is a one-call fix and needs no down-migration.

DO $$
DECLARE
    _team_cap      INT := 15;      -- mirrors entitlements.TEAM_SEAT_CAP
    _uncapped      INT := 100000;  -- mirrors accounts.UNCAPPED_SEAT_LIMIT
    _repaired      INT;
BEGIN
    WITH sigma AS (
        SELECT a.account_id,
               a.account_kind,
               a.seat_limit,
               COALESCE((SELECT SUM(e.quantity)
                           FROM public.account_entitlements e
                          WHERE e.account_id = a.account_id
                            -- patients are a PEER capacity class with their own entitlement
                            -- block; the mirror summed the human pool only.
                            AND e.seat_type <> 'patient'), 0) AS sigma_human
          FROM public.accounts a
    )
    UPDATE public.accounts a
       SET seat_limit = CASE WHEN lower(coalesce(s.account_kind, 'tenant')) = 'team'
                             THEN _team_cap ELSE _uncapped END,
           updated_at = NOW()
      FROM sigma s
     WHERE a.account_id = s.account_id
       -- PROVABLY mirror-written: stored value == the sum the mirror computed.
       AND s.seat_limit = s.sigma_human
       -- and only when it actually changes something
       AND a.seat_limit <> (CASE WHEN lower(coalesce(s.account_kind, 'tenant')) = 'team'
                                 THEN _team_cap ELSE _uncapped END);

    GET DIAGNOSTICS _repaired = ROW_COUNT;
    RAISE NOTICE '213: repaired % mirror-written seat_limit row(s); operator-set values left alone',
                 _repaired;
END $$;

COMMENT ON COLUMN public.accounts.seat_limit IS
  'HARD CAP bound to the account KIND (see accounts.hard_seat_cap) — the most this account type '
  'may EVER hold. NOT licensed capacity and NOT occupancy: what an account has paid for is '
  'Σ public.account_entitlements.quantity, enforced separately by seats.register_seat. Written '
  'ONLY at account creation and by the operator lever accounts.set_seat_limit. Licensing code '
  'must never write this column — that inversion was migration 213''s reason for existing.';
