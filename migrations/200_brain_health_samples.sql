-- Migration 200: brain_health_samples — per-tenant BYO-brain health telemetry (INCREMENT 1)
-- Date: 2026-07-31
--
-- WHY
-- ---
-- FaultLine's SaaS promise is that a tenant points THEIR OWN LLM endpoint at it. When that
-- endpoint underperforms, the product degrades SILENTLY: every failure is a log.warning in a
-- container log the tenant cannot read (DEV/DESIGN-byo-brain-health-and-alerting.md §"The
-- problem, stated plainly"). This table is the persisted half of the health signal — the
-- rolling, per-seat aggregate of signals the engine ALREADY emits (reframe.llm_failed,
-- reframe.sacred_token_dropped, llm_call.attempt_failed + elapsed_seconds, circuit_breaker_open,
-- saas_no_brain_failclosed), so a console card can show the tenant WHAT is degrading, BY HOW
-- MUCH, and what to do about it.
--
-- WHY NOT system_alerts
-- ---------------------
-- The design doc asks for system_alerts first. It genuinely does not fit, for three reasons
-- that are structural, not stylistic:
--   1. `UNIQUE (alert_type)` + a monotonic `alert_count` — there is no time dimension at all,
--      so a ROLLING 24h window (the whole point: "is it degrading NOW") cannot be expressed.
--      A count that only ever goes up cannot answer "in the last 24 hours".
--   2. No numeric payload and no way to carry a LATENCY DISTRIBUTION, which is the primary
--      calibration input (a p95 against an operation budget).
--   3. Its semantics are "a warning to show the user N times then suppress" (alerts_shown /
--      max_alerts). Health telemetry is a continuously-recomputed aggregate, not a
--      show-it-four-times notice.
-- system_alerts stays exactly as it is and is untouched by this work.
--
-- SHAPE: a COUNT BUCKETED BY LATENCY, not raw latencies
-- ------------------------------------------------------
-- Per Google SRE Book Ch.6 "Monitoring Distributed Systems"
-- (https://sre.google/sre-book/monitoring-distributed-systems/): "The simplest way to
-- differentiate between a slow average and a very slow 'tail' of requests is to collect
-- request counts bucketed by latencies (suitable for rendering a histogram), rather than
-- actual latencies", with boundaries "distributed ... approximately exponentially". So a row
-- is (minute bucket, operation, metric, latency bucket) → COUNT. Percentiles are read off the
-- cumulative histogram, and are therefore reported as a RANGE ("p95 ≤ 20s"), never as a
-- falsely-precise number. Storage is bounded by the bucket cross-product, not by traffic.
--
--   bucket_start   minute-truncated event time (the rolling-window key).
--   operation      the LLM operation ('REFRAME', 'EXTRACT', ...) or '*' for op-less events.
--   metric         deterministic vocabulary, see saas/brain_health.py METRICS.
--   latency_le_ms  the histogram bucket's UPPER BOUND in ms; -1 means "count-only metric"
--                  (-1 rather than NULL so it can sit in the primary key).
--   n              the count for that cell; flush is ON CONFLICT DO UPDATE n = n + EXCLUDED.n.
--
-- PER-TENANT, per the hard isolation rule: this lives INSIDE each tenant schema (the request
-- search_path has NO public), so a seat's health numbers are physically unreachable from
-- another seat's bound schema. The console read is BOLA-safe by construction — the principal
-- is derived from the session, and only that principal's schema is opened.
--
-- FAIL-SAFE: this lane is TELEMETRY. Every write and read in saas/brain_health.py is wrapped;
-- a monitoring failure must never become an outage. The table's absence is a no-op, not an error.
--
-- Two parts, exactly like migration 062:
--   Part 1 — create in all already-provisioned tenant schemas (status='ready').
--   Part 2 — NEW schemas are covered by src/provisioning/templates/user_schema.sql, which is
--            updated in the SAME change. (Migration 197 shipped migration-only and therefore
--            silently skips every NEW tenant — that defect is explicitly not repeated here.)
--
-- Idempotent (IF NOT EXISTS everywhere); safe to re-run.

-- ── Part 1: create brain_health_samples in every currently-provisioned tenant schema ──

-- ⚠️ NOTE ON THE FANOUT GUARD (this is NOT cosmetic — it was MEASURED).
-- Migration 062's pattern iterates `user_provisioning WHERE status='ready'` and trusts the row.
-- Applying that pattern verbatim ABORTED on the first stale row on a real database:
--     ERROR: schema "faultline_c0000000_9999_..." does not exist
-- A registry row can outlive its schema (a wiped tenant, a failed provision, a restored dump).
-- Because the whole DO block runs in ONE transaction, a single stale row means NO schema gets
-- the table — and the migration reports the failure only at the very end, after some schemas
-- appear to have succeeded. So the loop is joined to `information_schema.schemata`: the
-- registry says which tenants SHOULD have it; the catalog says which schemas ACTUALLY exist,
-- and only their intersection is touched. Same fix belongs in future fanout migrations.

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
            'CREATE TABLE IF NOT EXISTS %I.brain_health_samples (
                bucket_start  TIMESTAMPTZ NOT NULL,
                operation     TEXT        NOT NULL,
                metric        TEXT        NOT NULL,
                latency_le_ms INTEGER     NOT NULL DEFAULT -1,
                n             INTEGER     NOT NULL DEFAULT 0,
                PRIMARY KEY (bucket_start, operation, metric, latency_le_ms)
            )',
            _schema
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_brain_health_window
                 ON %I.brain_health_samples (bucket_start DESC)',
            _schema
        );
        RAISE NOTICE 'Created brain_health_samples in schema %', _schema;
    END LOOP;
END $$;
