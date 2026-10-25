-- Migration 299: documents.stub_tsv — keyword-searchable at the ENQUEUE COMMIT (plan W2.1)
-- Date: 2026-10-22
--
-- WHY
-- ---
-- A document's value waited on the drain: between the enqueue commit and the per-chunk
-- extraction landing, the ONLY queryable artifact was the honest "still importing" line —
-- the verbatim text sat in `documents.chunks` unsearchable for hours (measured on the
-- openalex bench: ~29h queue, round-0 recall 1/36). The research pass
-- (DEV/RESEARCH/fast-document-availability-sota-20261022.md, commit 77ff96d6) established
-- the fix is one derived layer, not a pipeline change: `to_tsvector(config, jsonb)` is a
-- DOCUMENTED overload that walks EVERY string value in the JSON document INCLUDING array
-- elements (PostgreSQL docs, Text Search Functions — functions-textsearch.html), so
-- `chunks JSONB` (an array of chunk strings) is directly indexable with no
-- jsonb_array_elements_text LATERAL. Verified before writing: pg_proc.provolatile = 'i'
-- (immutable) for to_tsvector(regconfig, jsonb), so the expression is generated-column
-- legal; the full CREATE TABLE + GIN + @@/ts_rank/ts_headline path smoke-tested on PG16.
--
-- WHAT THIS COLUMN IS
-- -------------------
-- `stub_tsv tsvector GENERATED ALWAYS AS (...) STORED` on the per-tenant `documents` row:
--
--     setweight(to_tsvector('simple', coalesce(title,      '')), 'A')   -- the title
--  || setweight(to_tsvector('simple', coalesce(source_ref, '')), 'B')   -- the filename/URL
--  || to_tsvector('simple', chunks)                                     -- the verbatim text
--
--   * THE ENQUEUE COMMIT IS THE VISIBILITY EVENT. A generated column is recomputed by the
--     INSERT itself — there is no second writer, no trigger, no drain involvement: the
--     verbatim text becomes keyword-searchable the instant the registry row commits, hours
--     before extraction. There is deliberately NO "make it searchable NOW" synchronous
--     path beyond that (the refresh=true anti-pattern — forced visibility is where the
--     stall risks live; the commit already IS the event).
--   * 'simple' CONFIG, PINNED (2-arg form everywhere). A pinned regconfig is frozen into
--     the index at write time — you cannot re-stem at query time without a reindex — and
--     the corpus is multilingual by design, so exact-token behavior (the same shape the
--     cortex keyword lane uses) beats guessing a stemmer per tenant. 'english' would
--     mis-stem every non-English corpus silently.
--   * WEIGHTS: title A > source_ref B > chunks (default D). The weighted concat is the
--     documented structured-document recipe (textsearch-controls.html). Weights only
--     affect ts_rank ordering, never @@ matching — a chunk token matches just as well as
--     a title token; the title just ranks higher.
--   * DERIVED, RE-DERIVABLE: a generated column is a pure function of (title, source_ref,
--     chunks). It is the "stamped, backfillable" derived layer — engine-growth compatible
--     by construction. It carries NO new truth: the chunks are the same verbatim safety
--     net migration 183 wrote; this only indexes them.
--
-- THE QUERY SIDE STAYS DUMB (owner directive — recall is a literal SELECT)
-- -----------------------------------------------------------------------
--     ... WHERE stub_tsv @@ websearch_to_tsquery('simple', :q)
--
-- ranked by ts_rank, LIMIT 3 — implemented in _document_hits_for_query (src/api/main.py)
-- and surfaced as the document_hits section. No LLM, no embedding, no query-time
-- smartening: GIN answers the @@, the row's own columns answer everything else.
--
-- THE STUB IS HONEST, NOT SILENT ABOUT PROCESSING: a hit on a status='pending' row is a
-- hit on VERBATIM TEXT ONLY — the recall surface labels every hit with its status and
-- never counts stub hits as facts_committed (the research doc's "stub-lane leak" risk,
-- mitigated by labeling, not by hiding).
--
-- OPERATIONS NOTE (GIN fastupdate): corpus dumps fill the GIN pending list and searches
-- scan it until vacuum folds it (gin.html §65.4.4.1). The drain already batches; a
-- per-index gin_pending_list_limit tune is the runbook lever if a tenant ever dumps
-- thousands of documents — noted in the research doc, no action in this migration.
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public, so the
-- column MUST exist inside every tenant schema. New tenants get it from the template
-- (src/provisioning/templates/user_schema.sql), updated in this same change — "A
-- migration-only change silently skips every new tenant" (the rule migrations 197/273/
-- 298 shipped under). Rides the boot-migration ledger (public.schema_migration_status):
-- applied once per schema, never re-swept.
--
-- GUARDED FANOUT: the loop joins information_schema.schemata (a stale registry row
-- naming a dropped schema cannot abort the DO block) and skips any tenant predating
-- migration 183 (no documents table — BASE TABLE only: a stray VIEW named documents
-- must not pass the guard into an ALTER). Additive + idempotent (ADD COLUMN IF NOT
-- EXISTS + CREATE INDEX IF NOT EXISTS); no DROP, no UPDATE, no destructive SQL. Safe
-- to re-run.
-- NOTE the ALTER rewrites the table to compute the column for existing rows — a one-time
-- cost per tenant, proportional to row count, exactly like migration 298's ADD COLUMN —
-- and tenants are locked SERIALLY within one boot (ACCESS EXCLUSIVE per tenant, the GIN
-- build non-concurrent inside the DO block). Run in the pinned boot window; measure the
-- largest tenant first if the corpus is big (the 298 deploy did the same).

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- Skip a tenant that predates migration 183 (no documents table yet) — the same
        -- guard shape migrations 197 and 298 use. BASE TABLE only: a stray VIEW named
        -- documents must not pass the guard into an ALTER.
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'documents'
              AND table_type = 'BASE TABLE'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.documents '
            'ADD COLUMN IF NOT EXISTS stub_tsv tsvector '
            'GENERATED ALWAYS AS ( '
            '    setweight(to_tsvector(''simple'', coalesce(title, '''')), ''A'') || '
            '    setweight(to_tsvector(''simple'', coalesce(source_ref, '''')), ''B'') || '
            '    to_tsvector(''simple'', chunks) '
            ') STORED',
            _schema);

        -- The one access path the recall lane takes: stub_tsv @@ query. GIN is the index
        -- text search exists for (gin.html); one row per document keeps posting-list
        -- pressure low (the array folds into ONE tsvector — no per-chunk rows).
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_documents_stub_tsv '
            'ON %I.documents USING GIN (stub_tsv)',
            _schema);

        EXECUTE format(
            'COMMENT ON COLUMN %I.documents.stub_tsv IS %L',
            _schema,
            'Migration 299 (plan W2.1): generated keyword-search vector over the stub — '
            'setweight(title,''A'') || setweight(source_ref,''B'') || to_tsvector(''simple'', '
            'chunks). Recomputed by the enqueue INSERT itself, so the verbatim text is '
            'keyword-searchable at the enqueue commit, hours before extraction. simple '
            'config = exact-token (multilingual-safe, matches the cortex keyword lane); '
            'the jsonb overload walks every chunk string including array elements. Purely '
            'derived: a pure function of the row''s own columns, re-derivable by backfill. '
            'A hit on a pending row is VERBATIM TEXT ONLY — surfaces must label the hit '
            'with documents.status and never count it as facts_committed.');
    END LOOP;
END $$;
