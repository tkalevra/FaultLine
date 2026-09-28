-- Migration 062: system_alerts table — persistent per-user warnings with countdown suppression
-- Date: 2026-06-01
--
-- Purpose: Tracks recurring warnings (e.g., Qdrant collection mismatch, provisioning drift)
-- so the backend can surface them to users without spamming on every request.
-- Each alert_type is unique per schema; alerts_shown counts how many times the warning
-- has been surfaced; once alerts_shown >= max_alerts the backend suppresses further noise.
-- resolved_at is set when the condition that triggered the alert is cleared.
--
-- Two parts:
--   Part 1 — Apply to all already-provisioned user schemas (status='ready').
--   Part 2 — Template update is in 051_template_user_schema.sql (appended below).
--
-- The template file (051_template_user_schema.sql) is the canonical DDL for NEW schemas.
-- This migration handles EXISTING schemas that were provisioned before this DDL existed.

-- ── Part 1: Create system_alerts in all currently-provisioned user schemas ────────────

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM   public.user_provisioning up
        WHERE  up.status = 'ready'
          AND  up.schema_name IS NOT NULL
          -- SKIP A REGISTRY ROW WHOSE SCHEMA NO LONGER EXISTS. Without this join a stale
          -- `user_provisioning` row (registry outliving its schema — deprovision, manual drop,
          -- restore) raises `schema "faultline_..." does not exist` INSIDE this single-transaction
          -- DO block, which aborts the WHOLE block, so NO schema gets the object. Found 2026-07-31
          -- when migration 200 copied this pattern verbatim and failed on 11 stale rows.
          AND  EXISTS (SELECT 1 FROM information_schema.schemata s
                       WHERE s.schema_name = up.schema_name)
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.system_alerts (
                id            SERIAL      PRIMARY KEY,
                alert_type    TEXT        NOT NULL,
                alert_count   INTEGER     NOT NULL DEFAULT 1,
                alerts_shown  INTEGER     NOT NULL DEFAULT 0,
                max_alerts    INTEGER     NOT NULL DEFAULT 4,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                resolved_at   TIMESTAMPTZ,
                UNIQUE (alert_type)
            )',
            _schema
        );
        RAISE NOTICE 'Created system_alerts in schema %', _schema;
    END LOOP;
END $$;
