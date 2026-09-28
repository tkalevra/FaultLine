-- Migration 205: documents.chunk_state — a PER-CHUNK terminal ledger (PARALLEL)
-- Date: 2026-07-31
--
-- WHY
-- ---
-- The document lane's unit of redelivery is the DOCUMENT, and its unit of work is the CHUNK.
-- That mismatch is the whole defect:
--
--   (A) A CHUNK THAT FAILED WAS BURNED. `chunks_failed > 0` finalized the row TERMINAL
--       'partial' with no way to say WHICH chunks were unread — so there was no re-mine path
--       at all. ~1000 chunks were burned that way in a single day (2026-07-31). The verbatim
--       text survives in `documents.chunks`, but nothing recorded which slice of it still
--       needed reading, so recovering it meant re-running the entire document.
--
--   (B) A RECLAIM RE-RAN EVERYTHING. When a worker died mid-drain the lease expired and the
--       document was re-claimed — and every chunk it had ALREADY completed was extracted and
--       ingested a second time. Convergent writes make that safe (UUID v5 entity ids +
--       ON CONFLICT), but it is a full re-spend of the tenant's own metered brain, and it
--       bumps the staged-fact `confirmed_count` a second time for facts that occurred once.
--
-- WHAT THIS COLUMN IS
-- -------------------
-- `chunk_state` maps a chunk INDEX (as a JSONB object key, e.g. "7") to that chunk's terminal
-- state. Written only by the drain worker, on its own connection, in the main thread:
--
--     {"0": {"s":"done","c":4,"g":1},
--      "7": {"s":"failed","a":3,"e":"ReadTimeout: ..."}}
--
--     s — 'done' (read and ingested) | 'failed' (attempts exhausted; never read)
--     c — facts committed by that chunk        g — facts staged by that chunk
--     a — item-level attempts spent            e — the last failure, truncated
--
-- A chunk with NO entry has no terminal state and is therefore still owed: that — not a
-- counter — is what the drain re-queues. Deliberately a single JSONB column rather than a
-- child table: the update is one row-locked `jsonb_set`, so concurrent writers serialize at
-- the row instead of needing a cross-row transaction, and a document's whole state stays
-- atomic with the status flip that finalizes it.
--
-- BEHAVIOURALLY A NO-OP UNTIL THE FLAG IS ON. `DOC_CHUNK_QUEUE` defaults OFF; with it off
-- nothing reads or writes this column and the lane is byte-identical. Existing rows read back
-- as '{}' = "no chunk has a terminal state", which is exactly true of every document drained
-- before this migration.
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public, so the column
-- MUST exist inside every tenant schema. New tenants get it from the template
-- (src/provisioning/templates/user_schema.sql), which is updated in this same change.
--
-- GUARDED FANOUT: the loop joins `information_schema.schemata`, so a stale registry row
-- naming a schema that no longer exists cannot abort the DO block (that failure destroyed a
-- fanout earlier the same day). Additive + idempotent (ADD COLUMN IF NOT EXISTS); no DROP,
-- no UPDATE, no destructive SQL. Safe to re-run.

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
            'ADD COLUMN IF NOT EXISTS chunk_state JSONB NOT NULL DEFAULT ''{}''::jsonb',
            _schema);

        EXECUTE format(
            'COMMENT ON COLUMN %I.documents.chunk_state IS %L',
            _schema,
            'Per-chunk terminal ledger for the parallel drain (migration 205): '
            '{"<chunk_index>": {"s":"done|failed","c":committed,"g":staged,"a":attempts,'
            '"e":"last error"}}. A chunk with NO entry is still owed and is re-queued on '
            'reclaim; this is what makes a failed chunk re-minable instead of burned. '
            'Empty {} = legacy/no parallel drain has run.');
    END LOOP;
END $$;
