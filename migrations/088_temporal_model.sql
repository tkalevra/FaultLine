-- Migration 088: temporal model — temporal_status + event_date on facts & staged_facts
-- Date: 2026-06-14
-- Purpose: SCHEMA FOUNDATION for temporal reasoning.
--          See DEV/DESIGN-hierarchy-ladder-and-growth.md §"Temporal model — tense + event-time".
--
-- WHAT
-- ----
-- A fact carries a TENSE, not just a date. Two columns, on BOTH facts and staged_facts
-- (Class C mirror — identical structure across classes; class governs durability, NEVER
-- structure):
--   temporal_status TEXT NOT NULL DEFAULT 'now'  ∈ {now, past, future}  (verb tense)
--   event_date      TIMESTAMPTZ NULL              (the when, for past/future)
--
-- THREE dates that must NOT be conflated:
--   1. created_at — ingest time (already exists; system bookkeeping).
--   2. event_date + temporal_status — when the FACT is true/happened (THIS migration).
--   3. a date that IS the object (born_on) — a SCALAR in entity_attributes.value_date.
--
-- Orthogonal to supersession: superseded_at/archived_at = "still believed true";
-- temporal_status = "what time the fact refers to".
--
-- public is the SEED SOURCE/TEMPLATE ONLY. Idempotent: ADD COLUMN IF NOT EXISTS, the
-- named CHECK is added only if absent (catalog guard), indexes are IF NOT EXISTS.

-- ── 1. public (the template / seed source) ─────────────────────────────────
ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS temporal_status TEXT NOT NULL DEFAULT 'now';
ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS event_date TIMESTAMPTZ NULL;

ALTER TABLE public.staged_facts
    ADD COLUMN IF NOT EXISTS temporal_status TEXT NOT NULL DEFAULT 'now';
ALTER TABLE public.staged_facts
    ADD COLUMN IF NOT EXISTS event_date TIMESTAMPTZ NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_facts_temporal_status'
          AND conrelid = 'public.facts'::regclass
    ) THEN
        ALTER TABLE public.facts
            ADD CONSTRAINT chk_facts_temporal_status
            CHECK (temporal_status IN ('now', 'past', 'future'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_staged_facts_temporal_status'
          AND conrelid = 'public.staged_facts'::regclass
    ) THEN
        ALTER TABLE public.staged_facts
            ADD CONSTRAINT chk_staged_facts_temporal_status
            CHECK (temporal_status IN ('now', 'past', 'future'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_facts_event_date
    ON public.facts (event_date);
CREATE INDEX IF NOT EXISTS idx_facts_temporal
    ON public.facts (temporal_status, event_date);
CREATE INDEX IF NOT EXISTS idx_staged_facts_event_date
    ON public.staged_facts (event_date);
CREATE INDEX IF NOT EXISTS idx_staged_facts_temporal
    ON public.staged_facts (temporal_status, event_date);

-- ── 2. Fan out to existing tenant schemas ───────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- facts columns
        EXECUTE format(
            'ALTER TABLE %I.facts ADD COLUMN IF NOT EXISTS temporal_status TEXT NOT NULL DEFAULT ''now''',
            _schema);
        EXECUTE format(
            'ALTER TABLE %I.facts ADD COLUMN IF NOT EXISTS event_date TIMESTAMPTZ NULL',
            _schema);

        -- staged_facts columns
        EXECUTE format(
            'ALTER TABLE %I.staged_facts ADD COLUMN IF NOT EXISTS temporal_status TEXT NOT NULL DEFAULT ''now''',
            _schema);
        EXECUTE format(
            'ALTER TABLE %I.staged_facts ADD COLUMN IF NOT EXISTS event_date TIMESTAMPTZ NULL',
            _schema);

        -- named CHECK constraints (guarded — pg_constraint catalog check, per-schema)
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_facts_temporal_status'
              AND conrelid = format('%I.facts', _schema)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.facts ADD CONSTRAINT chk_facts_temporal_status '
                'CHECK (temporal_status IN (''now'', ''past'', ''future''))',
                _schema);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_staged_facts_temporal_status'
              AND conrelid = format('%I.staged_facts', _schema)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.staged_facts ADD CONSTRAINT chk_staged_facts_temporal_status '
                'CHECK (temporal_status IN (''now'', ''past'', ''future''))',
                _schema);
        END IF;

        -- indexes
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_facts_event_date ON %I.facts (event_date)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_facts_temporal ON %I.facts (temporal_status, event_date)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_staged_facts_event_date ON %I.staged_facts (event_date)', _schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_staged_facts_temporal ON %I.staged_facts (temporal_status, event_date)', _schema);

        RAISE NOTICE 'Migration 088: temporal model added to %', _schema;
    END LOOP;
END $$;
