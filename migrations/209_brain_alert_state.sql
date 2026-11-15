-- Migration 209: public.brain_alert_state — the per-seat ALERT ledger for endpoint health.
-- Date: 2026-08-01
-- CLOSED-SOURCE SaaS layer (SAAS_MODE). Inert when SaaS is off: nothing reads or writes it.
--
-- WHY
-- ---
-- Measured on pre-prod 2026-08-01: 41 x `GET /tenant/alerts` -> HTTP 401 in 20 minutes. The
-- endpoint-health surface was reached ONLY by a console poll, so it evaluated only while a human
-- was signed in and looking at it — and 401'd when they were not. A health check that requires an
-- audience cannot do the one job it exists for. This host has already run a 13-day production
-- event-loop stall that nobody was watching.
--
-- WHAT ALREADY EXISTED, AND IS NOT REBUILT HERE
-- --------------------------------------------
-- Migration 204 shipped `<tenant>.brain_probe_state`: a per-seat, per-lane, LAST-WRITE-WINS alarm
-- stream written at the site where real work succeeds or fails (saas/brain_health.py probe_event).
-- The VERDICT is therefore already computed continuously, in the background, with no session
-- anywhere and with ZERO extra LLM spend — it is a by-product of traffic the tenant was paying
-- for regardless. What did not exist was anything that ACTS on it.
--
-- So this migration does NOT add a prober. It adds the ledger that lets a background sweep answer
-- "has this seat's state CHANGED since the last time we told somebody?" — which is the only
-- question an alerting lane may ask, because per-incident mail is spam, and spam gets filtered,
-- and then the one that mattered is invisible too.
--
-- WHY public.* AND NOT A PER-TENANT TABLE
-- ---------------------------------------
-- Same reasoning as migrations 202 and 207. This is CONTROL-PLANE state, not tenant data:
--   1. The sweep iterates ALL seats to find the few that changed. A per-tenant table would force
--      one connection + one `SET search_path` per seat per cycle just to discover there is
--      nothing to do — precisely the O(seats)-forever cost migration 207 exists to remove.
--   2. It stores an ADMIN'S email address (`alerted_to`), which belongs to the account's control
--      plane, not to the seat's memory. The per-tenant runtime search_path has no `public`, so a
--      data-plane connection cannot reach this row at all. That is the stronger boundary.
--   3. `alerts_enabled` is a per-seat preference that must survive a tenant-schema rebuild.
--
-- FAIL-SAFE BY CONSTRUCTION
-- -------------------------
-- This is a TELEMETRY/UX lane. Every read and write of this table is wrapped and swallowed by
-- saas/brain_alerts.py. A monitoring bug must never become an outage: if this table is missing,
-- unreadable or wedged, ingest and recall are completely unaffected and the console falls back to
-- exactly today's behaviour.

CREATE TABLE IF NOT EXISTS public.brain_alert_state (
    -- The SEAT. One row per seat, created lazily on first evaluation.
    user_id            uuid PRIMARY KEY,
    account_id         uuid,

    -- The user-visible toggle ("alert on failure/flapping"). DEFAULT true: the whole point is
    -- catching degradation when nobody is watching, and a monitor that ships off by default is a
    -- monitor nobody turns on. Opt-OUT, not opt-in.
    alerts_enabled     boolean      NOT NULL DEFAULT true,

    -- The state the LAST SWEEP OBSERVED (ok | fail | flapping | no_brain | unknown). Written on
    -- every evaluation, whether or not it produced mail — this is the console's "as of" value.
    last_state         text,
    evaluated_at       timestamptz,

    -- The state we last actually SENT MAIL about, and to whom. `alerted_state` is the dedup key:
    -- mail is sent only when the observed state DIFFERS from this. Equal states are silence.
    -- NULL = we have never alerted this seat.
    alerted_state      text,
    alerted_at         timestamptz,
    alerted_to         text,

    -- Rate-limit bookkeeping. `fault_alert_at` is the floor consulted before a FAULT alert
    -- (see BRAIN_ALERT_MIN_INTERVAL_S); a RECOVERY notice is deliberately exempt so a resolved
    -- alert can always close itself.
    fault_alert_at     timestamptz,
    alerts_sent        integer      NOT NULL DEFAULT 0,
    alerts_suppressed  integer      NOT NULL DEFAULT 0,

    -- Last delivery outcome, for the console and for debugging. Never a secret: smtp_sender
    -- returns short, secret-free reasons by contract.
    last_send_error    text,

    updated_at         timestamptz  NOT NULL DEFAULT now()
);

-- The sweep's only scan: "which seats have we never evaluated, or evaluated longest ago".
CREATE INDEX IF NOT EXISTS idx_brain_alert_state_evaluated
    ON public.brain_alert_state (evaluated_at NULLS FIRST);

COMMENT ON TABLE public.brain_alert_state IS
    'Per-seat endpoint-health ALERT ledger (migration 209). Dedup + rate-limit state for '
    'background FAIL/FLAPPING email. Control-plane; never read on a data-plane search_path.';
COMMENT ON COLUMN public.brain_alert_state.alerted_state IS
    'The state mail was last SENT for. The dedup key: alerts fire only on a CHANGE from this.';
COMMENT ON COLUMN public.brain_alert_state.alerts_enabled IS
    'User-visible per-seat toggle. Opt-OUT (default true).';
