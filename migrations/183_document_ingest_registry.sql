-- Migration 183: documents — per-tenant async document-ingestion registry
-- Date: 2026-07-16
--
-- WHY
-- ---
-- `ingest_document` is the flagship "dump a corpus / curriculum" lane. It used to
-- run the WHOLE hybrid extraction synchronously inside the tool call, blocking the
-- caller for the entire document. This registry makes the lane ASYNC:
--
--   1. `ingest_document` chunks the doc, INSERTs one `documents` row status='pending'
--      (with the deterministic chunk list retained verbatim), and returns FAST with
--      the document id + a modest ETA.
--   2. The re_embedder poll loop drains pending rows, runs the per-chunk hybrid
--      extraction (deterministic spine FIRST, the tenant-brain LLM /extract/rewrite
--      picking up the sentences the spine dropped), ingests through the WGM gate
--      (source="mcp" → user_stated, durable), and flips status → 'ready' (or 'error').
--   3. Recall surfaces an honest "still processing — check back in ~N s" line while a
--      document is pending, WITHOUT blocking recall of already-ready facts.
--
-- The `chunks` JSONB IS the per-document verbatim safety net (the raw text is never
-- lost even if the worker never runs). This is INFRA, not a domain seed — no domain
-- literals, no ontology rows. Additive: does NOT touch the WGM gate, class
-- assignment, or query scope.
--
-- PER-TENANT: the ingest/query request connection runs with `SET search_path TO {schema}`
-- WITHOUT public, so the table MUST live INSIDE each tenant schema. NO public seed —
-- document rows are inherently user-specific (created empty per tenant). This migration
-- applies the CREATE to every already-provisioned tenant schema; NEW tenants get it
-- from the template (src/provisioning/templates/user_schema.sql).
--
-- Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS. Safe to run repeatedly.
-- No DROP, no destructive SQL.

-- ============================================================================
-- Per-user schemas (loop over faultline_* schemas) — EXISTING tenants
-- ============================================================================
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format($t$
            CREATE TABLE IF NOT EXISTS %I.documents (
                id               BIGSERIAL   PRIMARY KEY,
                user_id          TEXT        NOT NULL,
                source_ref       TEXT,
                title            TEXT,
                chunks           JSONB       NOT NULL DEFAULT '[]'::jsonb,
                chunk_count      INTEGER     NOT NULL DEFAULT 0,
                status           TEXT        NOT NULL DEFAULT 'pending',
                chunks_failed    INTEGER     NOT NULL DEFAULT 0,
                facts_committed  INTEGER     NOT NULL DEFAULT 0,
                facts_staged     INTEGER     NOT NULL DEFAULT 0,
                context_stored   INTEGER     NOT NULL DEFAULT 0,
                truncated        BOOLEAN     NOT NULL DEFAULT false,
                error            TEXT,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                started_at       TIMESTAMPTZ DEFAULT NULL,
                ready_at         TIMESTAMPTZ DEFAULT NULL
            )
        $t$, _schema);

        -- Partial index driving BOTH the worker drain and the recall not-ready probe:
        -- only pending/processing rows matter to either, so keep the index tiny.
        EXECUTE format($t$
            CREATE INDEX IF NOT EXISTS idx_documents_active
                ON %I.documents (created_at)
                WHERE status IN ('pending', 'processing')
        $t$, _schema);
    END LOOP;
END $$;
