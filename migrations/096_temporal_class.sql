-- Migration 096: temporal_class on rel_types — drives supersede-vs-coexist deterministically
-- Date: 2026-06-17
-- Purpose: PHASE 0 of DEV/DESIGN-memory-temporal-lifecycle.md §3.1.
--
-- WHAT
-- ----
-- One metadata enum on rel_types saying whether a rel's facts are
--   immutable | state | event
-- This is PURE METADATA (Phase 0): nothing CONSUMES it for behavior yet. It is added so
-- the per-tenant overlay resolves it at runtime (rel_type_overlay._SELECT_COLS), and
-- later phases (supersede-vs-coexist matrix; event_date gating) read it.
--
-- SAFE DEFAULT = 'state' — the NON-DESTRUCTIVE class. An unknown / newly-grown rel can
-- therefore NEVER wrongly hard-supersede on first sight (only 'immutable' is
-- destructive-leaning, and we never default to it). Per §3/§9.
--
-- Subject-agnostic: the seed UPDATEs key on canonical WGM rel_type names that already
-- exist in the seed (verified against migrations 016/030/091). Anything not listed KEEPS
-- the 'state' default. The UPDATEs are guarded `WHERE rel_type IN (...)` so a rel that is
-- absent in a given schema is simply a no-op — idempotent + safe to re-run.
--
-- public is the SEED SOURCE/TEMPLATE ONLY. Idempotent: ADD COLUMN IF NOT EXISTS, the
-- named CHECK is added only if absent (catalog guard). Mirrors migration 088's shape.

-- ── 1. public (the template / seed source) ─────────────────────────────────
ALTER TABLE public.rel_types
    ADD COLUMN IF NOT EXISTS temporal_class TEXT NOT NULL DEFAULT 'state';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_rel_types_temporal_class'
          AND conrelid = 'public.rel_types'::regclass
    ) THEN
        ALTER TABLE public.rel_types
            ADD CONSTRAINT chk_rel_types_temporal_class
            CHECK (temporal_class IN ('immutable', 'state', 'event'));
    END IF;
END $$;

-- Seed the obvious existing rels by behavior (data, not runtime logic).
--   immutable: identity / forever-slot facts (a value change is a CORRECTION, archives old)
--   event:     distinct datable occurrences that COEXIST
--   everything else KEEPS the 'state' default.
UPDATE public.rel_types SET temporal_class = 'immutable'
 WHERE rel_type IN ('pref_name', 'also_known_as', 'has_gender',
                    'born_in', 'born_on', 'nationality');

UPDATE public.rel_types SET temporal_class = 'event'
 WHERE rel_type IN ('met');

-- The 'state' rels are explicitly named for documentation / to repair any schema where a
-- prior non-default crept in; the column default already makes them 'state', so this is a
-- belt-and-suspenders normalization (still idempotent).
UPDATE public.rel_types SET temporal_class = 'state'
 WHERE rel_type IN ('feels', 'occupation', 'lives_in', 'lives_at',
                    'likes', 'dislikes', 'prefers', 'age', 'height', 'weight',
                    'owns', 'has_pet', 'located_in', 'educated_at',
                    'works_for', 'member_of');

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
            'ALTER TABLE %I.rel_types ADD COLUMN IF NOT EXISTS temporal_class TEXT NOT NULL DEFAULT ''state''',
            _schema);

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_rel_types_temporal_class'
              AND conrelid = format('%I.rel_types', _schema)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.rel_types ADD CONSTRAINT chk_rel_types_temporal_class '
                'CHECK (temporal_class IN (''immutable'', ''state'', ''event''))',
                _schema);
        END IF;

        EXECUTE format(
            'UPDATE %I.rel_types SET temporal_class = ''immutable'' '
            'WHERE rel_type IN (''pref_name'', ''also_known_as'', ''has_gender'', '
            '''born_in'', ''born_on'', ''nationality'')',
            _schema);

        EXECUTE format(
            'UPDATE %I.rel_types SET temporal_class = ''event'' '
            'WHERE rel_type IN (''met'')',
            _schema);

        EXECUTE format(
            'UPDATE %I.rel_types SET temporal_class = ''state'' '
            'WHERE rel_type IN (''feels'', ''occupation'', ''lives_in'', ''lives_at'', '
            '''likes'', ''dislikes'', ''prefers'', ''age'', ''height'', ''weight'', '
            '''owns'', ''has_pet'', ''located_in'', ''educated_at'', '
            '''works_for'', ''member_of'')',
            _schema);

        RAISE NOTICE 'Migration 096: temporal_class added to %', _schema;
    END LOOP;
END $$;
