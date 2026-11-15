-- Migration 206: episodic_log.source is LOAD-BEARING for provenance preservation
-- Date: 2026-08-01
--
-- WHY
-- ---
-- `reextract_episodic` (src/re_embedder/embedder.py) re-mines retained turns and used to
-- re-ingest them with source="reextract", which falls through the /ingest provenance router
-- to fact_provenance="llm_inferred". A fact the USER STATED therefore came back DEMOTED to
-- Class B/C instead of the Class A it earned the first time.
--
-- OWNER RULING (binding): "Re-stated should be able to go from B->A. User is truth should be
-- respected." A fact does not become less true because our pipeline had to read it twice.
--
-- The fix (REEXTRACT_PRESERVE_PROVENANCE, default ON) re-ingests each retained turn under its
-- ORIGINAL ingest source, resolved from `episodic_log.source`. That column already existed and
-- is already populated by every writer -- NO new column is added here, deliberately: a second
-- column recording the same fact is a second source of truth, and this project has already
-- paid for that once (three drifting copies of the tool descriptions).
--
-- What this migration DOES is make the new contract discoverable ON THE COLUMN, so that anyone
-- adding a writer -- or reading the table during an incident -- sees that `source` now decides
-- whether a re-mined fact is user testimony or engine inference.
--
-- THE FOOTGUN THIS DOCUMENTS: EpisodicAppendRequest.source (src/api/models.py) DEFAULTS to
-- 'mcp'. A caller that simply omits the field is therefore recorded as a user turn. All three
-- current writers set it explicitly; a new one must too.
--
-- Recognised origin lanes and their re-mine authority:
--   'mcp'                    -> user-stated (live path ingests this same text as source="mcp")
--   'document'               -> user-stated (live lane ingests chunks as "mcp"/"assistant")
--   'store_context_deferred' -> NOT elevatable (Class-C residue, never produced a triple)
--   NULL / anything else     -> NOT elevatable (origin unknown -- never guessed)
--
-- PER-TENANT: search_path has NO public, so episodic_log lives inside each tenant schema.
-- This applies the COMMENT to every already-provisioned tenant; NEW tenants get it from the
-- template (src/provisioning/templates/user_schema.sql). A migration-only change silently
-- skips every newly provisioned tenant -- a recorded failure mode here (migration 197).
--
-- Idempotent: COMMENT ON COLUMN is a pure overwrite and carries no data. Metadata only --
-- no DDL, no DML, nothing destructive, safe to run repeatedly.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT table_schema
        FROM information_schema.tables
        WHERE table_name = 'episodic_log'
          AND table_schema LIKE 'faultline\_%'
    LOOP
        EXECUTE format(
            'COMMENT ON COLUMN %I.episodic_log.source IS %L',
            _schema,
            'ORIGIN LANE of this retained turn. LOAD-BEARING: reextract_episodic resolves the '
            'original /ingest source from this value, so it decides whether a re-mined fact '
            'returns as USER TESTIMONY (Class A) or ENGINE INFERENCE. '
            '''mcp'' = chat turn (user-stated); ''document'' = document chunk (user-stated); '
            '''store_context_deferred'' = Class-C residue (NOT elevatable); '
            'NULL/unknown = origin unestablished (NOT elevatable -- never guessed). '
            'A new writer MUST set this honestly: EpisodicAppendRequest.source defaults to '
            '''mcp'', so omitting the field records the row as a user turn. See migration 206.'
        );
    END LOOP;

    RAISE NOTICE 'Migration 206: episodic_log.source provenance contract documented';
END $$;
