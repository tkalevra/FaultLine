-- Migration 090: severed_taxonomies — the durable structural-correction sever lock
-- Date: 2026-06-14
-- Purpose: STRUCTURAL CORRECTION (DESIGN-hierarchy-ladder-and-growth.md §"user correction
--          is REAL" → "Correction works at BOTH levels"). A user statement that edits the
--          HIERARCHY itself ("my pets are not part of my family") severs the nesting and must
--          stick PERMANENTLY: the background nesting-growth engine can NEVER re-link the pair.
--
-- WHAT
-- ----
-- Add `severed_taxonomies TEXT[] DEFAULT '{}'` on entity_taxonomies (public + every tenant).
-- When the user severs `family ⊃ pets`, the structural-correction handler:
--   1. removes 'pets' from family.member_taxonomies (acts on the structure), and
--   2. appends 'pets' to family.severed_taxonomies, and sets source='user_corrected'.
-- The nesting-growth engine MUST consult severed_taxonomies (and honor source='user_corrected')
-- before ADDing a member_taxonomy, and refuse to re-add a user-severed link. This is the
-- NOT-superseded, Class-A-authoritative, "user > engine, durably" lock. No row is deleted —
-- the severance is recorded, non-destructive.
--
-- public is the SEED SOURCE/TEMPLATE ONLY — never read at runtime (overlays union it).
-- Idempotent: ADD COLUMN IF NOT EXISTS only. Re-runnable.

-- ── 1. public (the template / seed source) ─────────────────────────────────
ALTER TABLE public.entity_taxonomies
    ADD COLUMN IF NOT EXISTS severed_taxonomies TEXT[] DEFAULT '{}';

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
            'ALTER TABLE %I.entity_taxonomies ADD COLUMN IF NOT EXISTS severed_taxonomies TEXT[] DEFAULT ''{}''',
            _schema);
        RAISE NOTICE 'Migration 090: severed_taxonomies added to %', _schema;
    END LOOP;
END $$;
