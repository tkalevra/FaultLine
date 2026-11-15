-- Migration 204: brain_probe_state — the per-seat ALARM STREAM (last-write-wins)
-- Date: 2026-07-31
--
-- WHY THIS IS NOT brain_health_samples
-- ------------------------------------
-- Migration 200 stores a rolling COUNT bucketed by latency. That answers "how often lately"
-- (trend/diagnosis) and it answers it well. It structurally CANNOT answer "is it broken right
-- now", because a monotonic counter over a 24h window keeps an alarm lit long after the
-- condition ended. Measured here on 2026-07-31: the API stall was EPISODIC — 32.4% of minutes
-- during a backfill storm, then re_embedder CPU 93%->2%, longest LLM call 762s->8s, and 14
-- minutes of sampling with ZERO stalls. A band computed from the 24h aggregate alone would
-- have been screaming "degraded" at a system that had already recovered. A stale alarm is
-- worse than no alarm: it trains the user to ignore the real one.
--
-- THE MECHANISM: ONE EVENT, AN OUTCOME FIELD, LAST WRITE WINS
-- ----------------------------------------------------------
-- If only FAILURES are recorded, the newest failure is the newest record forever — fifty
-- successes later the alarm is still lit, and clearing it needs a TTL, a decay job or a
-- reconciliation sweep. Every one of those is a stale-state bug waiting to happen.
--
-- So BOTH outcomes are written, in ONE shape, to ONE place:
--     brain_probe outcome=fail op=REFRAME elapsed=45.0 endpoint=<host> seat=<id>
--     brain_probe outcome=ok   op=REFRAME elapsed=1.8  endpoint=<host> seat=<id>
-- ...and the newest record IS the current state. A user who fails, clicks "Test & calibrate",
-- and succeeds is GREEN on the next read, because their success is the last event. No ageing,
-- no clear-the-alarm path to get wrong. THIS IS WHY THE SUCCESS PATH MUST EMIT TOO — success
-- is normally the silent case, and a lane that records only its errors breaks last-write-wins
-- permanently for that lane.
--
-- The row is the database analogue of `... | tail -1`: an UPSERT keyed on `lane`, so the table
-- holds at most one row per lane per seat and CANNOT grow with traffic. The read is a
-- single-row SELECT — no history scan, no ring buffer, no log retention dependency. (That last
-- one is not theoretical: this host emits ~360k log lines/day and its log ring turned over
-- from 565 minutes of retention to 9 during one investigation, so the greppable log line
-- cannot be the system of record. It is emitted as well, not instead.)
--
-- outcome_history — THE THIRD STATE
-- ---------------------------------
-- `tail -1` gives OK or FAIL. That is not enough: an endpoint that ALTERNATES is a different
-- problem from one that is down, with different causes (overload/rate-limit, a load balancer
-- with one bad backend, our own circuit breaker cycling, or a latency that straddles the
-- budget) and a different remedy. Evidence: this API flapped all night — `FailingStreak: 13`
-- was a flap caught mid-storm, not a continuous outage.
-- So the row also carries the last N outcomes (default 5, oldest first, capped on write). The
-- state is computed as OK / FAIL / FLAPPING by counting ok<->fail TRANSITIONS, not failures:
-- 2 failures in a row is one incident; ok/fail/ok/fail is instability. FLAPPING OUTRANKS the
-- newest line — a user shown green on an intermittent endpoint has been actively misled.
-- Still one row: the array is capped at N, so this remains O(1) storage per lane.
--
-- PRIVACY / SECURITY
-- ------------------
--   endpoint_host  HOST[:PORT] ONLY, never a path or query string. Some providers carry the
--                  key in the URL (?api-key=...), so storing a full URL here would put a
--                  credential into a log line and a console card. saas/brain_health.py
--                  ::endpoint_host enforces this and also strips any user:pass@ prefix.
--   detail         a SHORT (<=160 char) echo of the user's own input, so the message can say
--                  WHAT timed out. It is the seat's own content inside the seat's own schema —
--                  the same isolation boundary as every other fact they own.
--
-- PER-TENANT, per the hard isolation rule: this lives INSIDE each tenant schema (the request
-- search_path has NO public), so one seat's alarm can never light another seat's card.
--
-- FAIL-SAFE: telemetry. Every read/write in saas/brain_health.py is wrapped; the table's
-- absence is a NO-OP, not an error (a pre-204 schema simply reports no state).
--
-- Two parts, exactly like migration 200:
--   Part 1 — create in all already-provisioned tenant schemas (status='ready').
--   Part 2 — NEW schemas are covered by src/provisioning/templates/user_schema.sql, updated in
--            the SAME change. (Migration 197 shipped migration-only and therefore silently
--            skipped every NEW tenant — that defect is deliberately not repeated.)
--
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

-- ── Part 1: create brain_probe_state in every currently-provisioned tenant schema ──
--
-- The fanout is JOINed to information_schema.schemata, not driven off user_provisioning alone:
-- a registry row can outlive its schema (wiped tenant, failed provision, restored dump), and
-- because the whole DO block is one transaction a single stale row means NO schema gets the
-- table. Same guard migration 200 documents.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT up.schema_name
        FROM   public.user_provisioning up
        JOIN   information_schema.schemata s ON s.schema_name = up.schema_name
        WHERE  up.status = 'ready'
          AND  up.schema_name IS NOT NULL
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.brain_probe_state (
                lane            TEXT        PRIMARY KEY,
                outcome         TEXT        NOT NULL,
                observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                elapsed_ms      INTEGER,
                budget_ms       INTEGER,
                endpoint_host   TEXT,
                reason          TEXT,
                detail          TEXT,
                outcome_history TEXT[]      NOT NULL DEFAULT ARRAY[]::TEXT[],
                CONSTRAINT brain_probe_state_outcome_chk CHECK (outcome IN (''ok'', ''fail''))
            )',
            _schema
        );
        RAISE NOTICE 'Created brain_probe_state in schema %', _schema;
    END LOOP;
END $$;
