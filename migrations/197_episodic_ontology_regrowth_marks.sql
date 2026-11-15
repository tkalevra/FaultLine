-- Migration 197: episodic_log — ontology-regrowth re-eligibility marks (CTIER increment 2)
-- Date: 2026-07-31
--
-- WHY
-- ---
-- The retained-turn tier is supposed to be the "we don't forget" net, and its whole
-- justification over an embedding is that a RETAINED TURN CAN BE RE-READ AGAINST A GROWN
-- ONTOLOGY: /expand adds a place, and a turn that was untypeable last month becomes
-- typeable now. That is the growth engine running BACKWARDS over history.
--
-- It does not currently run backwards. `reextract_episodic`
-- (src/re_embedder/embedder.py) stamps a turn that yielded ZERO edges as a terminal
-- SUCCESS — `reextracted_at = now(), extracted_fact_count = 0` — and the eligibility
-- scan is `WHERE reextracted_at IS NULL`. So a turn the ontology could not cast is
-- frozen out of the re-mining loop PERMANENTLY, no matter how much the ontology grows
-- afterwards. The in-code note is explicit that this is deliberate ("Retrying every
-- cycle forever is waste" — and that part is right; every retry is an LLM call) and
-- defers the fix to "a future re-mine-all admin action". This is that mechanism, except
-- it fires on EVIDENCE OF GROWTH rather than on a human remembering to press a button.
--
-- MEASURED (pre-prod, 2026-07-31, 34 tenants): of 542 episodic turns that have been
-- drained, 102 (18.8%) are stamped extracted_fact_count = 0. Those 102 turns are the
-- entire retained-turn tier's reason to exist, and today not one of them will ever be
-- looked at again.
--
-- WHAT THESE COLUMNS DO
-- ---------------------
--   reextract_ontology_mark  The tenant's WALKABLE-PLACE COUNT at the moment this turn
--                            was stamped zero-edge. A zero-edge turn becomes eligible
--                            again only once that count has grown by a configured delta
--                            — i.e. only when the tenant actually gained somewhere to
--                            file the turn. NULL = never stamped under the new lane
--                            (legacy rows; treated as "no mark recorded").
--   reextract_attempts       How many times this turn has been through the drain. Bounds
--                            the re-eligibility so a turn that is genuinely uncastable in
--                            any ontology cannot bill forever.
--
-- WHY "WALKABLE PLACE COUNT" AND NOT A ROW COUNT OF rel_types
-- -----------------------------------------------------------
-- The mark counts rel_types whose `category` is NOT 'pending_placement', plus
-- entity_taxonomies rows. That is deliberate and it is the VERIFY-THE-READ half of this
-- change. A novel rel minted in-flow lands with category='pending_placement' and is
-- NEVER appended to entity_taxonomies.rel_types_defining_group (src/api/main.py:5502) —
-- so the query path's `rel_type = ANY(allowed_rels)` projection can never admit it, and
-- a fact filed under it does not come back from a scoped recall. Only once
-- `drain_pending_placement_by_morphology` (src/re_embedder/embedder.py:6701) has folded
-- that rel onto a seeded canonical does it adopt a real category and enter a taxonomy —
-- becoming WALKABLE. Marking on walkable places therefore means a turn is re-mined when
-- there is somewhere RETRIEVABLE to put it, not merely somewhere to write it. Re-mining
-- into an unwalkable pending rel would be a write with no reader: exactly the failure
-- this migration exists to avoid repeating.
--
-- DETERMINISTIC: two integer counts and a comparison. No cosine, no embedding, no LLM
-- in the eligibility decision. Subject-agnostic: counts places, never names them.
--
-- PROVENANCE UNCHANGED: a re-mined turn still ingests with source="reextract" →
-- fact_provenance="llm_inferred". A re-derivation lacks the live turn's context, so it
-- is inference, not user testimony. Re-mining more often must not launder that.
--
-- Additive + idempotent ONLY: ADD COLUMN IF NOT EXISTS with defaults that reproduce
-- today's behaviour exactly (NULL mark, 0 attempts). No DROP, no UPDATE, no backfill.
-- The reading code is behind REEXTRACT_ONTOLOGY_REGROWTH (default OFF), so applying
-- this migration alone changes NOTHING at runtime.
--
-- PER-TENANT: the runtime binds `SET search_path TO {schema}` WITHOUT public, so the
-- columns MUST be added inside every tenant schema. New tenants get them from the
-- template (src/provisioning/templates/user_schema.sql).

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- Skip a tenant that predates migration 127 (no episodic_log table yet).
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'episodic_log'
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.episodic_log '
            'ADD COLUMN IF NOT EXISTS reextract_ontology_mark BIGINT  DEFAULT NULL, '
            'ADD COLUMN IF NOT EXISTS reextract_attempts      INTEGER NOT NULL DEFAULT 0',
            _schema);

        -- Supports the regrowth re-eligibility scan: the zero-edge cohort is the only
        -- one the new predicate re-opens, and it is a small fraction of the table.
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_episodic_log_regrowth '
            'ON %I.episodic_log (reextract_ontology_mark) '
            'WHERE extracted_fact_count = 0',
            _schema);
    END LOOP;
END $$;
