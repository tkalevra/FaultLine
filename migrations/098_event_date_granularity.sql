-- Migration 098: event_date_granularity on facts & staged_facts
-- Date: 2026-06-17
-- Purpose: PHASE 1 of DEV/DESIGN-memory-temporal-lifecycle.md §3.3.
--
-- WHAT
-- ----
-- event_date is a TIMESTAMPTZ (a single point). "in 2025" (year) and "last Tuesday" (day)
-- and a precise timestamp have different GRANULARITY. Without recording it, a year-range
-- query ("places visited in 2025") breaks because "2025" was stored as a fabricated
-- 2025-01-01T00:00 instant. This adds a sibling granularity tag so Phase 2 range queries
-- can expand granularity -> [start, end) deterministically.
--
--   event_date_granularity TEXT NULL  ∈ {year, month, day, timestamp}
--   NULL = no event_date (unstamped). The extractor stamps event_date at the START of the
--   granule (year->Jan 1, day->midnight, matching _parse_relative_date's midnight rule).
--
-- Additive + back-compatible: existing 088 rows have event_date with NULL granularity.
-- public is the SEED SOURCE/TEMPLATE ONLY. Idempotent: ADD COLUMN IF NOT EXISTS, the named
-- CHECK is added only if absent (catalog guard). Mirrors migrations 088/096 shape.

-- ── 1. public (the template / seed source) ─────────────────────────────────
ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS event_date_granularity TEXT NULL;
ALTER TABLE public.staged_facts
    ADD COLUMN IF NOT EXISTS event_date_granularity TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_facts_event_date_granularity'
          AND conrelid = 'public.facts'::regclass
    ) THEN
        ALTER TABLE public.facts
            ADD CONSTRAINT chk_facts_event_date_granularity
            CHECK (event_date_granularity IS NULL
                   OR event_date_granularity IN ('year', 'month', 'day', 'timestamp'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_staged_facts_event_date_granularity'
          AND conrelid = 'public.staged_facts'::regclass
    ) THEN
        ALTER TABLE public.staged_facts
            ADD CONSTRAINT chk_staged_facts_event_date_granularity
            CHECK (event_date_granularity IS NULL
                   OR event_date_granularity IN ('year', 'month', 'day', 'timestamp'));
    END IF;
END $$;

-- ── 2. Fan out to existing tenant schemas ───────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.facts ADD COLUMN IF NOT EXISTS event_date_granularity TEXT NULL',
            _schema);
        EXECUTE format(
            'ALTER TABLE %I.staged_facts ADD COLUMN IF NOT EXISTS event_date_granularity TEXT NULL',
            _schema);

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_facts_event_date_granularity'
              AND conrelid = format('%I.facts', _schema)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.facts ADD CONSTRAINT chk_facts_event_date_granularity '
                'CHECK (event_date_granularity IS NULL OR '
                'event_date_granularity IN (''year'', ''month'', ''day'', ''timestamp''))',
                _schema);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_staged_facts_event_date_granularity'
              AND conrelid = format('%I.staged_facts', _schema)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.staged_facts ADD CONSTRAINT chk_staged_facts_event_date_granularity '
                'CHECK (event_date_granularity IS NULL OR '
                'event_date_granularity IN (''year'', ''month'', ''day'', ''timestamp''))',
                _schema);
        END IF;

        RAISE NOTICE 'Migration 098: event_date_granularity added to %', _schema;
    END LOOP;
END $$;
