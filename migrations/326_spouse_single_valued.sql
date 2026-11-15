-- Migration 326: spouse is_single_valued — the state-rel slot supersede (LIVE-OPENWEBUI FIX 4)
-- Date: 2026-11-13
-- Purpose: DEV/BUGS/family-smoke-20261111/LIVE-OPENWEBUI-DIAGNOSIS.md, the B2 class.
--
-- THE MEASURED WOUND (the owner's prod seat, 2026-11-13): "My spouse is Katya Feld and
-- she prefers to be called Kat" landed BESIDE the prior spouse slot live —
-- ``ingest.temporal_coexist`` "same-slot state conflict kept BOTH rows live (keep-both)" —
-- and five test-junk spouse rows stayed live with it. The recall then served both readings.
--
-- WHAT
-- ----
-- Migration 168 seeded ``is_single_valued`` for lives_in/lives_at ONLY and left spouse
-- FALSE "pending review". The owner's live prod wound IS the review verdict: a spouse slot
-- is OWL-FUNCTIONAL (one current holder). This migration seeds spouse TRUE — the same
-- guarded, idempotent shape 168 uses — so the ingest decider's single-valued state-slot
-- branch (``_decide_supersede_or_coexist`` under INGEST_STATEREL_SLOT_SUPERSEDE, default
-- ON) supersedes the held slot instead of keep-both. The retirement is SOFT
-- (superseded_at) — the old rows stay queryable history (NEVER-MODIFY-MEMORY intact);
-- the authority ladder (user_stated > user_correction > llm_learned > llm_inferred) still
-- guards every direction, and multi-valued family rels (knows/friend_of/sibling_of) are
-- deliberately LEFT FALSE (a person holds several friends; coexist is correct there).
--
-- public is the SEED SOURCE/TEMPLATE ONLY. Idempotent: guarded UPDATE (WHERE rel_type IN).
-- Mirrors migration 168's shape exactly.

-- ── 1. public (the template / seed source) ─────────────────────────────────
UPDATE public.rel_types SET is_single_valued = true
 WHERE rel_type IN ('spouse') AND is_single_valued IS DISTINCT FROM true;

-- ── 2. Fan out to existing tenant schemas ───────────────────────────────────
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        BEGIN
            EXECUTE format(
                'UPDATE %I.rel_types SET is_single_valued = true '
                'WHERE rel_type IN (''spouse'') AND is_single_valued IS DISTINCT FROM true',
                _schema);
        EXCEPTION WHEN undefined_table THEN
            NULL;  -- a schema without rel_types yet is provisioned from public later
        END;
        RAISE NOTICE 'Migration 326: spouse is_single_valued=true in %', _schema;
    END LOOP;
END $$
