-- Migration 208: artefacts.chunk_index — TIE THE ARTEFACT BACK TO ITS CONTEXT
-- Date: 2026-08-01
-- Spec: the internal design record §3 (A.5), §6.2 (honest reporting)
--
-- WHY THIS COLUMN EXISTS
-- ---------------------
-- Migration 203 shipped `artefacts` with `document_id` and `page_index`, which says WHICH FILE
-- and WHICH PAGE an artefact came from. That is not enough to answer the thing that makes a
-- retained image worth keeping: **what was being SAID around it**.
--
-- The prose mined out of a document does not live on a page — it lives in `documents.chunks`,
-- a JSONB ARRAY, and every fact extracted from that document is mined per CHUNK. So the unit
-- that carries "the surrounding text" is the chunk INDEX, and without it an artefact and the
-- sentences it was printed beside are two rows with no join between them. An image alone is
-- not a memory; an image bound to what was written around it is.
--
-- `chunk_index` is the array position in `documents.chunks` whose text the artefact's caption
-- (or, failing that, its page's leading line) was found in. With `document_id` it is a
-- complete coordinate: this figure belongs beside THAT paragraph, and a walk that reaches the
-- paragraph can reach the figure.
--
-- WHY A SECOND COLUMN FOR THE METHOD
-- ----------------------------------
-- `chunk_bind_method` records HOW the tie was made, because the two ways are not equally
-- trustworthy and collapsing them would launder a coarse guess into a precise fact:
--   'caption'   — the A.5-bound caption text was located verbatim in that chunk. Precise.
--   'page_lead' — A.5 declined to bind a caption, so the page's first substantial line
--                 anchors it. Honest but coarse: "this came from around here".
-- NULL/NULL = neither resolved. A decline is RECORDED, never fabricated — the same rule
-- `caption_declined_reason` already follows.
--
-- THE HARD LINE (unchanged, and worth restating because this column is a BINDING)
-- ------------------------------------------------------------------------------
-- This ties a MEMORY (the artefact the user handed us) to other MEMORY (the text they handed
-- us in the same file). It creates NO L4 node and files nothing into the class hierarchy. An
-- artefact is content filed AT a place; it is never itself a place.
--
-- TIER: this is a binding the ENGINE computed by locating a string, so it inherits the
-- caption's Class-B posture. It asserts nothing new about the user, adds no fact row, and the
-- 30-day-fuse guard (`artefacts_caption_class_b`) is untouched.
--
-- PER-TENANT: bound by `SET search_path TO {schema}` WITHOUT public, so this column MUST exist
-- inside every tenant schema. NEW tenants get it from the template
-- (src/provisioning/templates/user_schema.sql — updated in the SAME change; a migration-only
-- change silently skips every newly-provisioned tenant, and a template-only change silently
-- skips every EXISTING one).
--
-- Idempotent + additive ONLY: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS. No DROP,
-- no UPDATE, no rewrite of an existing row. Re-runnable.

-- ⚠️ FANOUT GUARD (migration 203's note, kept verbatim in intent — MEASURED, not cosmetic).
-- A fanout driven by `public.user_provisioning` aborts the WHOLE DO block on the first
-- registry row whose schema no longer exists, because the block is one transaction — so NO
-- schema gets the change and the failure surfaces only at the end. A registry row outlives
-- its schema routinely (wiped tenant, failed provision, restored dump). This loop is driven
-- by the CATALOG, so a stale registry row is STRUCTURALLY unable to abort it, and it also
-- reaches tenant schemas missing from the registry.
--
-- The `to_regclass` guard is the second half: migration 203 is what CREATES `artefacts`, and a
-- schema that predates it (or whose 203 run was skipped) has no such table. Without the guard
-- this migration would abort the whole loop on the first such schema — the exact failure mode
-- the catalog-driven loop exists to prevent.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT s.schema_name
        FROM   information_schema.schemata s
        WHERE  s.schema_name LIKE 'faultline\_%'
    LOOP
        CONTINUE WHEN to_regclass(format('%I.artefacts', _schema)) IS NULL;

        EXECUTE format(
            'ALTER TABLE %I.artefacts ADD COLUMN IF NOT EXISTS chunk_index INTEGER', _schema);
        EXECUTE format(
            'ALTER TABLE %I.artefacts ADD COLUMN IF NOT EXISTS chunk_bind_method TEXT', _schema);

        -- A method may only be one of the two we can actually justify, and it may not exist
        -- without an index to describe. The database refuses an unexplained binding.
        BEGIN
            EXECUTE format($c$
                ALTER TABLE %I.artefacts ADD CONSTRAINT artefacts_chunk_bind_method
                    CHECK (chunk_bind_method IS NULL
                           OR (chunk_index IS NOT NULL
                               AND chunk_bind_method IN ('caption','page_lead')))
            $c$, _schema);
        EXCEPTION WHEN duplicate_object THEN
            NULL;  -- already applied; ADD CONSTRAINT has no IF NOT EXISTS
        END;

        -- The READ this column exists for: "what came in beside THIS chunk of THIS document".
        -- Partial, so it stays small — most artefacts never resolve a chunk.
        EXECUTE format($c$
            CREATE INDEX IF NOT EXISTS idx_artefacts_chunk
                ON %I.artefacts (document_id, chunk_index)
                WHERE chunk_index IS NOT NULL
        $c$, _schema);
    END LOOP;
END $$;
