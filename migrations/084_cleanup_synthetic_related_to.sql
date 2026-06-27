-- Migration 085: Hard-delete the retired synthetic `user related_to {topic}` rows
-- Date: 2026-06-12
-- Purpose: Blow away the leftover fabricated rows from the old `/expand` hack.
--
-- WHAT / WHY
-- ----------
-- `/expand` used to emit a synthetic `user related_to {topic}` edge to bind an
-- expanded topic back to the user. That hack was retired in commit 8bcc833
-- (`/expand` now writes `has_interest_in` instead), but old fabricated rows from
-- prior `/expand` runs still sit in tenant DBs. They no longer BLEED into recall —
-- the default subject-anchored query scope excludes them by projection
-- (`related_to` is not in the allowed_rels for a bare user-anchored query) — so
-- this is low-urgency HYGIENE, not a correctness fix. The owner explicitly wants
-- them BLOWN AWAY (hard DELETE, not a soft-archive via superseded_at/archived_at).
--
-- THE EXACT FILTER (verified live against the pre-prod tenant — do NOT widen it)
-- ----------------------------------------------------------------------------
-- A row is the hack's leftover IFF ALL THREE hold:
--   rel_type        = 'related_to'
--   subject_id      = user_id        (the row's subject IS the user's own entity UUID)
--   fact_provenance = 'llm_learned'
--
-- Live signature confirmed in the pre-prod tenant: 2 such rows in `staged_facts`,
-- 0 in `facts` (they were Class B and never promoted). subject_id = the user UUID,
-- objects = expanded-topic UUIDs. We delete from BOTH tables for safety /
-- idempotency even though `facts` currently has 0 matches.
--
-- WHY EACH PREDICATE PROTECTS LEGIT DATA (do not drop either guard):
--   * `subject_id = user_id` — EXCLUDES the orphan-binder `child -> seed` related_to
--     edges (subject = a child entity's UUID, NOT the user's UUID). Those are
--     legitimate grounding edges produced by the reachability sweep and MUST survive.
--   * `fact_provenance = 'llm_learned'` — EXCLUDES any genuine `user_stated`
--     related_to a user may have actually spoken. The hack's rows are always
--     `llm_learned` (≤0.6, never gated to user_stated). A real user-spoken
--     related_to would be user_stated and is preserved.
--
-- QDRANT
-- ------
-- These `staged_facts` are synced to the per-user Qdrant collection. NO manual
-- Qdrant delete is needed here: after this PG delete the re-embedder's
-- reconciliation pass (`reconcile` / orphan sweep) drops points that are absent
-- from PostgreSQL, deleting by the `(source_table, fact_id)` payload filter. The
-- orphaned vectors are removed on the re-embedder's next cycle. PostgreSQL is
-- authoritative; Qdrant is a derived view that self-heals against it.
--
-- Per-tenant loop over every `faultline_%` schema (mirrors migration 079's idiom).
-- Idempotent: re-running deletes nothing once the rows are gone. No DROP, no DDL.

DO $$
DECLARE
    _schema  TEXT;
    _deleted BIGINT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        -- facts (currently 0 matches in pre-prod; included for safety/idempotency)
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'facts'
        ) THEN
            EXECUTE format($del$
                DELETE FROM %I.facts
                WHERE rel_type = 'related_to'
                  AND subject_id = user_id
                  AND fact_provenance = 'llm_learned'
            $del$, _schema);
            GET DIAGNOSTICS _deleted = ROW_COUNT;
            RAISE NOTICE 'Migration 085: deleted % synthetic related_to row(s) from %.facts', _deleted, _schema;
        END IF;

        -- staged_facts (the hack's rows live here: Class B, never promoted)
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'staged_facts'
        ) THEN
            EXECUTE format($del$
                DELETE FROM %I.staged_facts
                WHERE rel_type = 'related_to'
                  AND subject_id = user_id
                  AND fact_provenance = 'llm_learned'
            $del$, _schema);
            GET DIAGNOSTICS _deleted = ROW_COUNT;
            RAISE NOTICE 'Migration 085: deleted % synthetic related_to row(s) from %.staged_facts', _deleted, _schema;
        END IF;
    END LOOP;
END $$;
