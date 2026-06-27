-- Migration 114: polarity (assertion polarity) on facts & staged_facts
-- Date: 2026-06-22
-- Purpose: Q1 — capture a NEGATED genuine STATE ("the GPS is not functioning") as a first-class
--          assertion-polarity column (ConText/NegEx assertion model), mirroring temporal_status.
--
-- WHAT
-- ----
-- A fact carries the POLARITY of the user's assertion:
--   polarity TEXT NOT NULL DEFAULT 'affirmed'  ∈ {affirmed, negated}
-- 'negated' marks a NEGATED genuine state — a definite non-functional fact that must read back
-- NEGATED, never as its positive opposite. This is the ASSERTION-MODEL polarity column (NOT
-- reification, NOT a correction/retraction — corrections are routed by the intent gate BEFORE
-- extraction and never produce a fact). It sits alongside temporal_status (verb tense) and is
-- orthogonal to supersession/archival (belief currency). The deriver sets it deterministically
-- from the spaCy `neg` dependency already at the state lanes; query reads the column, never reasons.
--
-- Additive + back-compatible: every existing row defaults to 'affirmed' (today's behavior — every
-- pre-114 fact was an affirmed assertion). No backfill beyond the DEFAULT.
-- public is the SEED SOURCE/TEMPLATE ONLY. Idempotent: ADD COLUMN IF NOT EXISTS, the named CHECK is
-- added only if absent (catalog guard). Mirrors migrations 088/096/098 shape.

-- ── 1. public (the template / seed source) ─────────────────────────────────
ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS polarity TEXT NOT NULL DEFAULT 'affirmed';
ALTER TABLE public.staged_facts
    ADD COLUMN IF NOT EXISTS polarity TEXT NOT NULL DEFAULT 'affirmed';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_facts_polarity'
          AND conrelid = 'public.facts'::regclass
    ) THEN
        ALTER TABLE public.facts
            ADD CONSTRAINT chk_facts_polarity
            CHECK (polarity IN ('affirmed', 'negated'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_staged_facts_polarity'
          AND conrelid = 'public.staged_facts'::regclass
    ) THEN
        ALTER TABLE public.staged_facts
            ADD CONSTRAINT chk_staged_facts_polarity
            CHECK (polarity IN ('affirmed', 'negated'));
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
            'ALTER TABLE %I.facts ADD COLUMN IF NOT EXISTS polarity TEXT NOT NULL DEFAULT ''affirmed''',
            _schema);
        EXECUTE format(
            'ALTER TABLE %I.staged_facts ADD COLUMN IF NOT EXISTS polarity TEXT NOT NULL DEFAULT ''affirmed''',
            _schema);

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_facts_polarity'
              AND conrelid = format('%I.facts', _schema)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.facts ADD CONSTRAINT chk_facts_polarity '
                'CHECK (polarity IN (''affirmed'', ''negated''))',
                _schema);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_staged_facts_polarity'
              AND conrelid = format('%I.staged_facts', _schema)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.staged_facts ADD CONSTRAINT chk_staged_facts_polarity '
                'CHECK (polarity IN (''affirmed'', ''negated''))',
                _schema);
        END IF;

        RAISE NOTICE 'Migration 114: polarity added to %', _schema;
    END LOOP;
END $$;
