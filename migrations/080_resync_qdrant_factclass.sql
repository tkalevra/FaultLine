-- Migration 080: Force Qdrant re-sync to backfill fact_class into point payloads
-- Date: 2026-06-11
-- Purpose: Existing Qdrant points were written before re_embedder.upsert_to_qdrant
--          included `fact_class` in the payload. The reconcile path compares only
--          rel_type + confidence, so those points will NOT self-heal — every
--          facts-table (Class A/B) point reads back as Class C in /query and gets
--          mis-tiered into the "less certain" hold bucket in the MCP renderer.
--
--          Fix: flip qdrant_synced=false on all live (non-superseded, non-expired)
--          facts/staged_facts rows so the re_embedder re-upserts them with the new
--          payload (now carrying fact_class). The re_embedder marks them synced
--          again after a successful upsert.
--
-- Scope: public schema + every per-user faultline_* schema.
-- Idempotent: re-running just re-flips already-synced rows to false; the next
--             re_embedder poll re-syncs them. No data is mutated beyond the
--             qdrant_synced bookkeeping flag. Guarded by column/table IF EXISTS.

-- ============================================================================
-- Part 1: Public schema
-- ============================================================================

DO $$
BEGIN
    -- 1a. facts
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'facts'
          AND column_name = 'qdrant_synced'
    ) THEN
        UPDATE public.facts
        SET qdrant_synced = false
        WHERE superseded_at IS NULL;
    END IF;

    -- 1b. staged_facts (live = not promoted, not expired)
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'staged_facts'
          AND column_name = 'qdrant_synced'
    ) THEN
        UPDATE public.staged_facts
        SET qdrant_synced = false
        WHERE promoted_at IS NULL
          AND expires_at > now();
    END IF;
END $$;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas)
-- ============================================================================

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline_%'
    LOOP
        -- 2a. facts
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = _schema AND table_name = 'facts'
              AND column_name = 'qdrant_synced'
        ) THEN
            EXECUTE format(
                'UPDATE %I.facts SET qdrant_synced = false
                 WHERE superseded_at IS NULL',
                _schema
            );
        END IF;

        -- 2b. staged_facts (live = not promoted, not expired)
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = _schema AND table_name = 'staged_facts'
              AND column_name = 'qdrant_synced'
        ) THEN
            EXECUTE format(
                'UPDATE %I.staged_facts SET qdrant_synced = false
                 WHERE promoted_at IS NULL
                   AND expires_at > now()',
                _schema
            );
        END IF;
    END LOOP;
END $$;
