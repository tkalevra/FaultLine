-- Migration 212: SEAT ENDPOINT CONTROL — make the tenant console's per-seat
--                "Own LLM endpoint · ALLOW | DENY" switch a REAL control.
-- Date: 2026-08-03
--
-- THE DEFECT
-- ----------
-- The tenant console renders a per-seat ALLOW/DENY switch for "Own LLM endpoint". It was
-- INERT in both directions, and the two halves of the inertness pointed OPPOSITE ways —
-- which is why it read as plausible:
--
--   WRITE  `UpdateSeatRequest` (saas/admin.py) had no `can_control_endpoint` field, so the
--          PUT body was dropped by the Pydantic model before any handler saw it. Verified:
--              UpdateSeatRequest(can_control_endpoint=True, label='x')
--                .model_dump(exclude_unset=True)  ->  {'label': 'x'}
--          `seats.update_seat()` has no such parameter either. Nothing was ever written.
--   READ   `seats.list_seats()` never projected the value, so the card rendered
--          `s.can_control_endpoint === true` on an ABSENT key -> DENY, for every seat,
--          forever, regardless of the real state.
--   GATE   Actual enforcement (`user_console._require_endpoint_control`) reads
--          `individual_users.can_control_endpoint` — a table the switch never touched.
--
-- The consequence is worse than "does nothing". Every code path that creates an individual
-- login (`user_console._create_individual`) hardcodes `tenant_context='none',
-- can_control_endpoint=TRUE` — INCLUDING `create_login_for_seat`, the path a CORPORATE
-- tenant admin uses to give a seat its "My Memory" login. So for exactly the seats this
-- switch exists to govern:
--       the console displayed  DENY
--       enforcement granted    ALLOW
-- The UI misrepresented the security posture in the PERMISSIVE direction. Today the ONLY
-- writer that ever produces a governed seat is the individual->tenant transfer
-- (saas/transfers.py:600).
--
-- THE TWO-SOURCES-OF-TRUTH QUESTION, AND THE ANSWER
-- -------------------------------------------------
-- "May this seat point the engine at its OWN brain?" is ONE fact, and this migration
-- deliberately does NOT add a second copy of it to `public.account_users`:
--
--   * `individual_users.can_control_endpoint` OWNS it. It is what every enforcement and
--     presentation reader already reads (`user_console._require_endpoint_control`,
--     `brain_health.seat_is_governed`, `brain_alerts`, `GET /me`, `user.js`).
--   * The fact is only MEANINGFUL where a `/me` login exists, because the surface it gates
--     is `PUT /me/llm-endpoint`. A seat with no individual login has no surface to gate, so
--     a per-seat column would be a permission with no subject.
--   * A duplicate permission that can drift is a security bug. The console reads THROUGH to
--     the owning table (`seats.list_seats` LEFT JOINs it) and writes THROUGH to it
--     (`seats.set_seat_endpoint_control`). One row, one truth.
--
-- `tenant_context` is the OTHER half, and it is a DERIVED fact that had drifted. A seat's
-- governance context is a function of its account's kind:
--       account_kind = 'individual_house'   -> SELF-OWNED   (no org; the person decides)
--       any other kind (tenant/team/clinical) -> ORG-GOVERNED ('real'; the admin decides)
-- `_create_individual` stamps 'none' unconditionally, so corporate seat logins claimed to be
-- standalone individuals. This migration repairs the drift and stops it recurring.
--
-- THE DEFAULT FOR A NEW SEAT, AND WHY
-- -----------------------------------
--   * ORG-GOVERNED seat  -> DENY  (`tenant_context='real'`, `can_control_endpoint=FALSE`).
--     Pointing the ingest engine at an arbitrary external endpoint is a data-EGRESS decision:
--     the seat's conversations, and the org's paid credential, leave by whatever route that
--     endpoint defines. That is the organisation's call to make, not the seat-holder's, and
--     it is the fail-closed direction. This is also what migration 149 already documented
--     ("real defaults false (org grants per seat)") and what the console has been DISPLAYING
--     all along — this migration makes the displayed state the true one.
--   * SELF-OWNED seat    -> ALLOW, and NOT admin-flippable. There is no organisation; the
--     person owns their own credential and their own data. `seats.set_seat_endpoint_control`
--     REFUSES on these seats rather than letting any admin strip an individual of their own
--     endpoint.
-- This is NOT "everyone defaults to DENY" — that blanket reading is precisely what made the
-- dead switch look plausible. The default is scoped to seats that actually have an org, it is
-- now genuinely enforced, and it is now genuinely flippable.
--
-- WHAT THIS DOES *NOT* DO. It does not delete or disable any brain config a seat has already
-- saved, and it does not touch `_require_endpoint_control`'s scope: that gate covers CHANGING
-- the endpoint (save / test / list-models / recalibrate / clear), never RESOLVING it. A
-- governed seat whose brain was already bound keeps working; what it loses is the ability to
-- change it unilaterally. No seat's service is interrupted by this migration.
--
-- CONTROL-PLANE ONLY. `public.individual_users` (migration 149) and `public.account_users`
-- (migration 131) exist ONLY in public. VERIFIED, not assumed: neither name appears anywhere
-- in src/provisioning/templates/user_schema.sql, so there is no per-tenant template twin to
-- keep in step and adding one would be wrong (it would place control-plane authorization
-- state inside the reach of the data-plane `search_path`). Nothing here is seeded into a
-- tenant schema or read from a tenant search_path.
--
-- SAAS_MODE OFF: these tables are never read when the flag is off, so behaviour is unchanged.
--
-- Idempotent; safe to re-run (the backfill is a no-op once converged; the trigger is
-- CREATE OR REPLACE + DROP/CREATE).


-- ── Part 1: REPAIR THE DRIFT ──────────────────────────────────────────────────────────
--
-- Every individual login sitting on an ORG account is stamped org-governed and defaulted to
-- DENY. Two properties make this safe to state plainly:
--   (a) It can only TIGHTEN. There is no existing grant to lose, because there has never been
--       a working grant path — the switch that was supposed to produce one dropped its input
--       at the door. (`transfers.py` writes only 'real' + FALSE, which this agrees with.)
--   (b) It aligns reality with what the admin has been SHOWN. The console has rendered DENY
--       for every seat since the switch shipped. Resolving the contradiction the other way
--       would mean silently telling admins their seats may egress to arbitrary endpoints —
--       a posture no admin ever approved.
-- A seat that genuinely needs its own brain is now one click away (and that click works).

UPDATE public.individual_users iu
SET    tenant_context       = 'real',
       can_control_endpoint = FALSE
FROM   public.accounts a
WHERE  a.account_id = iu.account_id
  AND  a.account_kind <> 'individual_house'
  AND  (iu.tenant_context IS DISTINCT FROM 'real' OR iu.can_control_endpoint);

-- Self-owned seats: assert the invariant the code relies on. Only ever LOOSENS toward the
-- individual's own control, and only on house accounts, so it cannot grant anything to an
-- org-governed seat.
UPDATE public.individual_users iu
SET    can_control_endpoint = TRUE
FROM   public.accounts a
WHERE  a.account_id = iu.account_id
  AND  a.account_kind = 'individual_house'
  AND  NOT iu.can_control_endpoint;


-- ── Part 2: STOP THE DRIFT RECURRING — an INSERT-time, TIGHTEN-ONLY derivation ────────
--
-- WHY A TRIGGER, STATED HONESTLY. A trigger that adjusts a supplied value is unusual, and the
-- alternative (fix the one INSERT site) was considered first. It is not sufficient: the
-- default is a SECURITY default, `_create_individual` is reached from several callers
-- (self-serve signup, tenant-admin `create_login_for_seat`, operator seat-login creation), and
-- the defect being fixed here IS "one insert site got the governance default wrong and nothing
-- caught it". The invariant must not depend on every future caller remembering.
--
-- Three deliberate limits keep it predictable:
--   1. BEFORE INSERT ONLY. It never fires on UPDATE, so the GRANT path
--      (`seats.set_seat_endpoint_control`) is untouched and an admin's ALLOW always sticks.
--   2. TIGHTEN ONLY. It can set can_control_endpoint TRUE->FALSE; it can never set
--      FALSE->TRUE. It cannot be the cause of a permission a nobody granted.
--   3. HOUSE ACCOUNTS ARE NOT TOUCHED AT ALL. Self-serve individual signup inserts are
--      byte-for-byte unchanged.
-- A future account_kind that is neither 'individual_house' nor corporate is treated as
-- org-governed (fail-closed). The failure mode is visible and one click from repair; the
-- opposite default is a silent egress grant.

CREATE OR REPLACE FUNCTION public.individual_users_derive_governance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    _kind TEXT;
BEGIN
    SELECT a.account_kind INTO _kind
    FROM   public.accounts a
    WHERE  a.account_id = NEW.account_id;

    -- Self-owned grouping (or an account row we cannot see — the FK makes that impossible,
    -- but do not tighten on a read we did not actually get): leave the row exactly as inserted.
    IF _kind IS NULL OR _kind = 'individual_house' THEN
        RETURN NEW;
    END IF;

    -- Org-governed: the seat belongs to an organisation, so the organisation decides.
    NEW.tenant_context := 'real';
    IF NEW.can_control_endpoint THEN
        NEW.can_control_endpoint := FALSE;    -- TIGHTEN ONLY; never the reverse
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_individual_users_derive_governance ON public.individual_users;
CREATE TRIGGER trg_individual_users_derive_governance
    BEFORE INSERT ON public.individual_users
    FOR EACH ROW
    EXECUTE FUNCTION public.individual_users_derive_governance();


-- ── Part 3: say what the columns MEAN, where the next reader will look ────────────────

COMMENT ON COLUMN public.individual_users.can_control_endpoint IS
    'THE SINGLE SOURCE OF TRUTH for "may this seat point the engine at its OWN LLM brain?". '
    'Read by user_console._require_endpoint_control (the gate on PUT /me/llm-endpoint and its '
    'test/models/recalibrate/clear siblings), brain_health.seat_is_governed and brain_alerts. '
    'Written by the seat holder NEVER, and by an account admin only through '
    'seats.set_seat_endpoint_control, which refuses on self-owned (individual_house) seats. '
    'public.account_users deliberately has NO copy of this column — a duplicated permission '
    'drifts, and drift in a permission is a security bug.';

COMMENT ON COLUMN public.individual_users.tenant_context IS
    'DERIVED from accounts.account_kind, not an independent fact: ''individual_house'' => '
    'self-owned (''none''/''house''), any other kind => ''real'' (org-governed). Enforced at '
    'INSERT by trg_individual_users_derive_governance (tighten-only) and re-asserted on every '
    'admin write in seats.set_seat_endpoint_control. It drifted before migration 212 because '
    'user_console._create_individual hardcoded ''none'' on every path, including the '
    'tenant-admin create_login_for_seat path — which made every corporate seat login claim to '
    'be a standalone individual and silently self-govern its own endpoint.';
