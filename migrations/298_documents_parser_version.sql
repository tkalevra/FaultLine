-- Migration 298: documents.parser_version — the R5 PARSER SEAM stamp
-- Date: 2026-10-21
--
-- WHY
-- ---
-- Until now the text that entered `documents.chunks` was produced by exactly one
-- parser (the pdfplumber line-walk / raw pasted text), so "how was this parsed"
-- went without saying. R5 (DEV/RESEARCH/doc-ingestion-sota-research-20261021.md)
-- introduces a SECOND parser — IBM Docling in CPU mode, selected by the
-- `DOC_PARSER` env (`src/ingest/doc_parser.py`) — that renders layout-aware
-- markdown (headings, tables, reading order) ahead of the same chunker. A stored
-- parse without its parser identity is a derived layer that cannot be re-derived
-- or audited: the re-parse trigger (remine_with_parser) selects and rebuilds BY
-- STAMP, so the stamp must exist at the row before any re-parse does.
--
-- WHAT THIS COLUMN IS
-- -------------------
-- `parser_version TEXT NOT NULL DEFAULT 'naive_v1'` on the per-tenant `documents`
-- registry row. The stamp describes the TEXT in `chunks` — which parser produced
-- it — and it is deliberately a ROW-level column, not a per-chunk field:
--
--   * ONE ROW IS ONE PARSER'S OUTPUT. A row is written from a single parse of a
--     single source, so a row-level stamp covers every chunk it holds. This keeps
--     `chunks` a plain JSONB string list — every existing reader is untouched.
--   * THE DEFAULT IS HONEST, NOT NEUTRAL. Every legacy row WAS parsed by the
--     pdfplumber/paste path; `'naive_v1'` states that fact rather than punting
--     with NULL. No backfill is needed and none is performed.
--   * STAMPS ARE FORWARD-EXTENSIBLE STRINGS (`naive_v1`, `docling_v1`, …), never
--     an enum — a future parser versions itself without a migration.
--
-- ENQUEUE CONTRACT: `POST /documents/enqueue` accepts `parser_version` and writes
-- it (src/api/main.py; normalized + shape-checked `[a-z0-9_]+` at the door — the
-- stamp must never lie). A pre-298 schema that has not seen this migration falls
-- back to the 195-era INSERT inside the endpoint: the row then reads the column
-- default, which is honest for every LEGACY row but would mis-stamp a docling
-- parse enqueued during migration lag — so that degradation logs LOUDLY
-- (doc_registry.enqueued_pre298_schema, naming the dropped stamp) and the
-- response carries parser_version_recorded. Deploy-order contract: apply 298
-- BEFORE enabling DOC_PARSER=docling.
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public, so
-- the column MUST exist inside every tenant schema. New tenants get it from the
-- template (src/provisioning/templates/user_schema.sql), updated in this same
-- change.
--
-- GUARDED FANOUT: the loop joins `information_schema.schemata`, so a stale
-- registry row naming a schema that no longer exists cannot abort the DO block.
-- Additive + idempotent (ADD COLUMN IF NOT EXISTS); no DROP, no UPDATE, no
-- destructive SQL. Safe to re-run.

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
            'ADD COLUMN IF NOT EXISTS parser_version TEXT NOT NULL DEFAULT ''naive_v1''',
            _schema);

        EXECUTE format(
            'COMMENT ON COLUMN %I.documents.parser_version IS %L',
            _schema,
            'R5 parser seam (migration 298): which parser produced the text in '
            'chunks — naive_v1 (pdfplumber line-walk / raw pasted text, the honest '
            'default for every legacy row) or docling_v1 (Docling CPU layout-aware '
            'markdown, DOC_PARSER=docling). One row is one parser''s output: the '
            'stamp covers every chunk in the row and is the selection key for '
            're-parse (remine_with_parser).');
    END LOOP;
END $$;
