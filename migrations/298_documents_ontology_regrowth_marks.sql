-- Migration 298: documents — ontology-regrowth re-mine marks (the migration-197 analog,
-- DOCUMENT lane; gauntlet R2 "document re-mine markers").
-- Date: 2026-10-21
--
-- WHY
-- ---
-- Migration 197 gave episodic_log the "a retained turn can be RE-READ against a grown
-- ontology" machinery: reextract_ontology_mark (the tenant's walkable-place count at the
-- moment the row was stamped zero-edge) + reextract_attempts (the consolidation-churn
-- bound). The DOCUMENT lane has no analog. A document whose chunks produced ZERO committed
-- edges, or that terminated 'partial', is terminal FOREVER: retry_document exists (the
-- manual door) but nothing ever drives it — measured on prod 2026-10-21: 11 research
-- papers, ~29h in the queue, facts_committed=0 on every one, rate-deferred withholdings
-- parked in episodic_log, and not one document auto-re-mined (litmus re-eval §5.5).
--
-- The re-mine driver itself lives in the drain (src/re_embedder/embedder.py
-- ::remine_documents_prepass), gated by DOC_REMINE_ENABLED (default OFF — the owner flips
-- it after deploy). THIS migration only supplies the bookkeeping the driver reads and
-- writes, so applying it changes NOTHING at runtime — the same "schema first, behaviour
-- behind a default-OFF flag" contract migration 197 shipped under.
--
-- WHAT THESE COLUMNS DO (mirrors migration 197, row-for-row semantics)
-- -------------------------------------------------------------------
--   reextract_ontology_mark  The tenant's WALKABLE-PLACE COUNT (rel_types with a real
--                            non-'pending_placement' category + entity_taxonomies rows —
--                            _tenant_walkable_place_count) at the moment this document was
--                            stamped into the re-mine cohort (terminal finalize with zero
--                            committed edges / 'partial') or actually re-mined. NULL =
--                            never stamped (legacy rows; treated as "no mark recorded").
--   reextract_attempts       How many times this document has been QUEUED for a re-mine
--                            pass. Bounds the loop so a document that is genuinely
--                            uncastable in any ontology cannot bill forever
--                            (DOC_REMINE_MAX_ATTEMPTS, default 3).
--
-- PROVENANCE / REPLAY: a re-mined document drains through the NORMAL pipeline with the
-- X-FL-Deferred-Replay: 1 + X-FL-Replay-Lane: reextract headers (the replay-transport
-- contract, src/api/ingest_transport.py), so a value /retract/correct retired in the
-- interleaving is NEVER resurrected by a re-mine. Source stays "document" — a re-mine
-- keeps the document tier (llm_inferred / staged B; owner direction 2026-08-21), never
-- elevating.
--
-- Additive + idempotent ONLY: ADD COLUMN IF NOT EXISTS with defaults that reproduce
-- today's behaviour exactly (NULL mark, 0 attempts). No DROP, no UPDATE, no backfill.
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public, so the
-- columns MUST exist in every tenant schema. New tenants get them from the template
-- (src/provisioning/templates/user_schema.sql — the documents CREATE TABLE), per the rule
-- migration 197 shipped with: "A migration-only change silently skips every new tenant".
-- Rides the boot-migration ledger (public.schema_migration_status): applied once per
-- schema, never re-swept.

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
        -- guard shape migration 197 uses for episodic_log/127.
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'documents'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.documents '
            'ADD COLUMN IF NOT EXISTS reextract_ontology_mark BIGINT  DEFAULT NULL, '
            'ADD COLUMN IF NOT EXISTS reextract_attempts      INTEGER NOT NULL DEFAULT 0',
            _schema);

        -- Supports the re-mine eligibility scan: the ONLY cohort the pass re-opens is
        -- terminal rows with zero committed edges or a 'partial' verdict — a small
        -- fraction of the table, so the partial index stays small. (ready|partial|error
        -- are the terminal statuses; 'pending'/'processing' belong to the live drain and
        -- are never touched by the pass.)
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_documents_regrowth '
            'ON %I.documents (created_at) '
            'WHERE status IN (''ready'', ''partial'', ''error'') '
            'AND (facts_committed = 0 OR status = ''partial'')',
            _schema);
    END LOOP;
END $$;
