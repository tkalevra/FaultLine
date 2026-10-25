-- Migration 301: documents registry — the per-tenant document lane base table (FOSS port)
-- Date: 2026-10-25
--
-- WHY
-- ---
-- The R6/R7/R8/R9 recall-uplift port (passage lane, miss-escalation, growth workstack,
-- deterministic-first uplift) rides the per-tenant `documents` registry: the verbatim
-- chunks live there, `stub_tsv` (migration 299) indexes them, `chunk_state` +
-- reextract marks (migration 298) make chunks re-minable, and `growth_work_queue`
-- (migration 300) names the chunk a recall-miss re-opens. The table itself never
-- existed on this lineage — the /documents/enqueue and /documents/pending endpoints
-- have been soft-failing against a missing table since they landed. This migration
-- mints the registry for EXISTING tenants; fresh tenants get it from the provisioning
-- template (src/provisioning/templates/user_schema.sql, updated in this same change —
-- "a migration-only change silently skips every new tenant", the rule migrations
-- 197/273/298 shipped under).
--
-- SHAPE
-- -----
-- One row = one accepted (part of a) document: verbatim chunks (JSONB array), the
-- drain's counters, the parallel per-chunk terminal ledger (chunk_state), the R2
-- re-mine marks (reextract_ontology_mark / reextract_attempts), the R5 parser stamp
-- (parser_version, default 'naive_v1' — honest for every row), and the R6/W2
-- generated keyword-search vector (stub_tsv: title A / source_ref B / chunks,
-- 'simple' config — exact-token, multilingual-safe). The generated column is
-- recomputed by the INSERT itself: a document becomes keyword-findable the moment
-- the row commits, hours before extraction — the whole premise of the R6 passage
-- lane. Column-level commentary lives in the template (single source of shape).
--
-- GUARDED FANOUT: the loop joins information_schema.schemata (a stale registry row
-- naming a dropped schema cannot abort the DO block). Additive + idempotent
-- (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS); no DROP, no UPDATE,
-- no destructive SQL. Safe to re-run. Migrations 298/299 already ran by now and
-- SKIPPED tenants without a documents table — this migration supplies the table
-- with every column inline (298's marks + parser stamp + 299's stub_tsv included),
-- so those tenants need no re-sweep: ADD COLUMN IF NOT EXISTS belt-and-braces below
-- covers any interim shape.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.documents ('
            '    id                        BIGSERIAL   PRIMARY KEY,'
            '    user_id                   TEXT        NOT NULL,'
            '    source_ref                TEXT,'
            '    title                     TEXT,'
            '    chunks                    JSONB       NOT NULL DEFAULT ''[]''::jsonb,'
            '    chunk_count               INTEGER     NOT NULL DEFAULT 0,'
            '    status                    TEXT        NOT NULL DEFAULT ''pending'','
            '    chunks_failed             INTEGER     NOT NULL DEFAULT 0,'
            '    facts_committed           INTEGER     NOT NULL DEFAULT 0,'
            '    facts_staged              INTEGER     NOT NULL DEFAULT 0,'
            '    context_stored            INTEGER     NOT NULL DEFAULT 0,'
            '    truncated                 BOOLEAN     NOT NULL DEFAULT false,'
            '    part_index                INTEGER     NOT NULL DEFAULT 0,'
            '    part_count                INTEGER     NOT NULL DEFAULT 1,'
            '    total_chunks              INTEGER     NOT NULL DEFAULT 0,'
            '    attempts                  INTEGER     NOT NULL DEFAULT 0,'
            '    brain_deferred_since      TIMESTAMPTZ DEFAULT NULL,'
            '    parser_version            TEXT        NOT NULL DEFAULT ''naive_v1'','
            '    chunk_state               JSONB       NOT NULL DEFAULT ''{}''::jsonb,'
            '    reextract_ontology_mark   BIGINT      DEFAULT NULL,'
            '    reextract_attempts        INTEGER     NOT NULL DEFAULT 0,'
            '    stub_tsv                  TSVECTOR    GENERATED ALWAYS AS ('
            '        setweight(to_tsvector(''simple'', coalesce(title, '''')), ''A'') ||'
            '        setweight(to_tsvector(''simple'', coalesce(source_ref, '''')), ''B'') ||'
            '        to_tsvector(''simple'', chunks)'
            '    ) STORED,'
            '    error                     TEXT,'
            '    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),'
            '    started_at                TIMESTAMPTZ DEFAULT NULL,'
            '    ready_at                  TIMESTAMPTZ DEFAULT NULL'
            ')',
            _schema);

        -- Belt-and-braces for a pre-existing partial shape (all idempotent).
        EXECUTE format(
            'ALTER TABLE %I.documents '
            'ADD COLUMN IF NOT EXISTS parser_version TEXT NOT NULL DEFAULT ''naive_v1'', '
            'ADD COLUMN IF NOT EXISTS chunk_state JSONB NOT NULL DEFAULT ''{}''::jsonb, '
            'ADD COLUMN IF NOT EXISTS reextract_ontology_mark BIGINT DEFAULT NULL, '
            'ADD COLUMN IF NOT EXISTS reextract_attempts INTEGER NOT NULL DEFAULT 0',
            _schema);
        -- stub_tsv is a generated column: ADD COLUMN IF NOT EXISTS cannot repair a
        -- table that somehow lacks it after the CREATE above, and a generated column
        -- cannot be added by ALTER on this shape anyway — the CREATE carries it.

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_documents_active '
            'ON %I.documents (created_at) '
            'WHERE status IN (''pending'', ''processing'')',
            _schema);

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_documents_regrowth '
            'ON %I.documents (created_at) '
            'WHERE status IN (''ready'', ''partial'', ''error'') '
            'AND (facts_committed = 0 OR status = ''partial'')',
            _schema);

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_documents_stub_tsv '
            'ON %I.documents USING GIN (stub_tsv)',
            _schema);

        EXECUTE format(
            'COMMENT ON TABLE %I.documents IS %L',
            _schema,
            'Migration 301 (FOSS CBA port): per-tenant document registry — verbatim '
            'chunks retained at enqueue, keyword-searchable the moment the row commits '
            '(stub_tsv, migration 299 shape), per-chunk terminal ledger (chunk_state) '
            'and re-mine marks (migration 298 shape) for ontology regrowth, parser '
            'stamp (naive_v1/docling_v1). Drained by the re_embedder document lane; '
            'the R6 passage lane, R7 recall-miss markers, and R9 deterministic uplift '
            'all read and re-open rows here.');
    END LOOP;
END $$;
