-- Migration 214: entity_aliases.display_form — RETAIN the casing of a name at ingest.
-- Date: 2026-08-04
--
-- THE DEFECT
-- ----------
-- Every entity name in every recall response renders lowercase — "diane does not live in
-- toronto". `entity_aliases` has no display column at all, and the pipeline lowercases the
-- surface long before the registry sees it (measured on a live stack:
-- `entity_registry.resolve_start` logs `original_name=diane` / `original_name=ibm` — the
-- registry is handed a name that has ALREADY been folded).
--
-- WHY A COLUMN AND NOT RENDER-TIME CAPITALISATION
-- ------------------------------------------------
-- Restoring case from a folded string is TRUECASING (Lita et al., "tRuEcasIng", ACL 2003).
-- It is lossy by construction — `ebay`, `ibm`, `mcdonald`, `iphone` come back as `Ebay`,
-- `Ibm`, `Mcdonald`, `Iphone` and nothing in the stored string can say otherwise, because
-- Unicode case mappings are non-invertible (Unicode Standard §4.2). It would also put
-- reconstruction on the QUERY path, which this codebase forbids. So the fix is to RETAIN
-- what the user typed, at ingest.
--
-- WHAT THIS COLUMN IS — AND IS NOT
-- ---------------------------------
-- `display_form` is a pure CASING OVERLAY of `alias`: the write seam enforces
-- `display_form.lower() = alias`, so it can never become a second, divergent name. The
-- lowercase `alias` column is UNTOUCHED and keeps every one of its current semantics — it
-- remains the matching key, the dedup key, the UUID-v5 input, the `UNIQUE (entity_id, alias)`
-- key, the `ON CONFLICT` target and the `idx_entity_aliases_one_preferred` subject. Nothing
-- reads `display_form` except the ONE sanctioned read-time presentation seam in /query.
--
-- Mirrors the W3C SKOS split between a lexical label presented as authored (`skos:prefLabel`)
-- and the normalised key a system matches on.
--
-- NO CHECK CONSTRAINT, DELIBERATELY
-- ----------------------------------
-- The `lower(display_form) = alias` invariant is enforced in Python at the write seam, NOT as
-- a SQL CHECK. PostgreSQL's `lower()` is collation-dependent and can disagree with Python's
-- `str.lower()` on non-ASCII input (this deployment's DB collation is driven by the install
-- language). A CHECK would convert that cosmetic disagreement into a constraint violation on
-- the alias INSERT — which propagates out of the /ingest edge loop as an HTTP 400 the MCP does
-- not retry, i.e. THE USER'S SENTENCE LOST OVER A CAPITAL LETTER. The alias is user content
-- and is sacred; its casing is presentation. Fail toward keeping the sentence.
--
-- ADDITIVE + IDEMPOTENT ONLY
-- ---------------------------
-- ADD COLUMN IF NOT EXISTS, nullable, no default, no backfill, no index, no DROP. Safe to
-- re-run. Every existing row stays NULL, and NULL means "no casing observed" — the renderer
-- falls back to `alias`, i.e. BYTE-IDENTICAL to today's output. Existing rows self-heal the
-- next time the user names the entity (see the deliberate no-backfill note below).
--
-- DELIBERATELY NO BACKFILL. Casing is already destroyed in `entity_aliases` and cannot be
-- recovered from it. It IS partially recoverable from `episodic_log` (verbatim turns,
-- migration 127) by re-parsing every retained turn per tenant — but that is a long-running
-- read of the largest per-tenant table across every schema, it only covers turns still inside
-- retention, and it is not needed for correctness: an entity the user mentions again is
-- repaired by the normal ingest path at zero cost. Recommended as a separate, opt-in,
-- resumable job if it is ever wanted — not as part of a schema migration.
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public, so the column
-- MUST be added inside every tenant schema. New tenants get it from the template
-- (src/provisioning/templates/user_schema.sql) — a migration WITHOUT the template edit
-- silently omits the column for every tenant provisioned afterwards.

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- Skip a schema that has no entity_aliases table (partial/aborted provisioning).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'entity_aliases'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.entity_aliases '
            'ADD COLUMN IF NOT EXISTS display_form TEXT DEFAULT NULL',
            _schema);
    END LOOP;
END $$;
