-- Migration 097: deleted_at tombstone on facts & staged_facts
-- Date: 2026-06-18
-- Purpose: PHASE 4 of DEV/DESIGN-memory-temporal-lifecycle.md §3.2 / §6 (Delete-Safety Lifecycle).
--
-- WHAT
-- ----
-- A TOMBSTONE column distinct from superseded_at / archived_at:
--
--   deleted_at TIMESTAMPTZ NULL   -- NULL = live; non-NULL = tombstoned (FORGOTTEN)
--
-- superseded_at / archived_at = "no longer the current belief, but kept as history,
-- queryable with include_archived=true". deleted_at = "the user asked us to FORGET this".
-- A SUPERSEDED fact has superseded_at but NOT deleted_at; a FORGOTTEN fact has deleted_at.
--
-- A forget TOMBSTONES (sets archived_at + deleted_at, fully recoverable) — it does NOT
-- physically DELETE. The physical purge of tombstones older than the grace window is a
-- LATER phase (Phase 5); this migration only adds the column + index. Setting archived_at
-- alongside deleted_at means the MANY existing `archived_at IS NULL` read filters already
-- hide a tombstoned row, so no blanket read-site edit is required here.
--
-- Additive + back-compatible: all existing rows have deleted_at NULL (= live).
-- public is the SEED SOURCE/TEMPLATE ONLY. Idempotent: ADD COLUMN IF NOT EXISTS, the named
-- partial index is created with IF NOT EXISTS. Mirrors migrations 088/096/098 shape.

-- ── 1. public (the template / seed source) ─────────────────────────────────
ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;
ALTER TABLE public.staged_facts
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_facts_deleted_at
    ON public.facts (deleted_at) WHERE deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_staged_facts_deleted_at
    ON public.staged_facts (deleted_at) WHERE deleted_at IS NOT NULL;

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
            'ALTER TABLE %I.facts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL',
            _schema);
        EXECUTE format(
            'ALTER TABLE %I.staged_facts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL',
            _schema);

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_facts_deleted_at '
            'ON %I.facts (deleted_at) WHERE deleted_at IS NOT NULL',
            _schema);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_staged_facts_deleted_at '
            'ON %I.staged_facts (deleted_at) WHERE deleted_at IS NOT NULL',
            _schema);

        RAISE NOTICE 'Migration 097: deleted_at tombstone added to %', _schema;
    END LOOP;
END $$;
