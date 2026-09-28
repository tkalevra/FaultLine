-- Migration 195: documents — part accounting + drain attempt counter (DOCLOSS)
-- Date: 2026-07-28
--
-- WHY
-- ---
-- `ingest_document` lost user content in TWO places, both silently:
--
--   (A) HARD 200-CHUNK TRUNCATION. src/mcp/server.py discarded chunks[200:] after a
--       container-log warning and still returned a success-shaped "pending" response.
--       The discarded tail was truncated BEFORE the enqueue, so it never reached
--       `documents.chunks` either — migration 183's "the chunks JSONB IS the per-document
--       verbatim safety net (the raw text is never lost)" did NOT hold for it.
--       FIX: _DOC_MAX_CHUNKS becomes a per-ROW SEGMENT SIZE. An over-cap document is
--       enqueued as consecutive PARTS (one row each) and ingested in full. These columns
--       record which slice of the original document a row carries, so a terminal signal
--       read off the registry can state the truth about the WHOLE document.
--
--   (B) BREAKER-REJECTED / FAILED CHUNKS COUNTED AS "no facts here". The drain worker
--       could not tell "the extractor never read this chunk" from "this chunk had no
--       facts", so a document whose chunks were all rejected by an OPEN circuit breaker
--       finalized as status='ready', chunks_failed=0. FIX: a degraded extraction now
--       raises; a brain-level outage RE-PENDS the whole document (at-least-once
--       redelivery) and `attempts` bounds that redelivery so a permanently-sick document
--       cannot re-pend forever — it terminates as 'error' with the reason. A document
--       that finishes with chunks_failed > 0 terminates as 'partial', never 'ready'.
--
-- Additive + idempotent ONLY: ADD COLUMN IF NOT EXISTS with safe defaults that reproduce
-- a legacy single-part document exactly. No DROP, no UPDATE, no destructive SQL. Rows
-- written before this migration read back as part 1 of 1 with 0 attempts.
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public, so the
-- columns MUST be added inside every tenant schema. New tenants get them from the
-- template (src/provisioning/templates/user_schema.sql).
--
-- NOTE ON `status`: the column is free-text TEXT (no CHECK / no enum), so the new
-- 'partial' terminal value needs no DDL. Readers that treat anything not in
-- ('pending','processing') as terminal are already correct.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- Skip a tenant that predates migration 183 (no documents table yet).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'documents'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.documents '
            'ADD COLUMN IF NOT EXISTS part_index   INTEGER NOT NULL DEFAULT 0, '
            'ADD COLUMN IF NOT EXISTS part_count   INTEGER NOT NULL DEFAULT 1, '
            'ADD COLUMN IF NOT EXISTS total_chunks INTEGER NOT NULL DEFAULT 0, '
            'ADD COLUMN IF NOT EXISTS attempts     INTEGER NOT NULL DEFAULT 0',
            _schema);
    END LOOP;
END $$;
