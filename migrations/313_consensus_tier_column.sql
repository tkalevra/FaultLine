-- 313_consensus_tier_column.sql — the extractor-consensus EVIDENCE-STACK TIER column (A25)
--
-- WHAT
-- ----
-- Adds one ADDITIVE metadata column to facts + staged_facts in EVERY schema that holds them:
--
--     consensus_tier SMALLINT  -- NULL | 1 | 2 | 3
--
-- The tier is a PROVENANCE-STACK INDICATOR — how many of the four fixed extraction readers
-- (parse_arm, gliner2_span, atomizer_surface, register_frame) TOUCHED the fact — 3-4 distinct
-- readers = tier 1, two = tier 2, one = tier 3, NULL = no reader / legacy / not computed. It is
-- NEVER a calibrated probability and NEVER overloads unified_confidence/confidence: WGM reads
-- those as provenance authority, and a tier-3 1.0 must never look like a provenance-authority
-- correction to WGM. Written ONLY when the CONSENSUS_TIER flag (default OFF — byte-parity of
-- serving behavior) is ON at the /ingest seams; legacy rows stay NULL by design and nothing in
-- this increment reads the column (the recall-side tier ordering is a DEFERRED increment behind
-- its own flag + the user_stated-never-below-llm_* pin).
--
-- WHY A MIGRATION NOW
-- -------------------
-- Template + migration, never one without the other (the 197/273/298/299/300/301 rule): the
-- provisioning template (src/provisioning/templates/user_schema.sql) gains the same columns in
-- the same commit, so fresh-mint and migration-parity deployments agree. ADDITIVE + IDEMPOTENT
-- only: ADD COLUMN IF NOT EXISTS, a guarded CHECK constraint (tier IN (1,2,3) — NULL passes a
-- CHECK by SQL standard), NO backfill, NO update of any existing row.
--
-- THE CARRIER RULING (AMENDMENT 3 revisit — measured 2026-11-12, keep honest)
-- ---------------------------------------------------------------------------
-- The A24 co-firing matrix (65 facts through the REAL MCP door, pre-prod image
-- e0528b8a69d1) measured marginals parse_arm .892 / gliner2_span .800 / atomizer_surface .062 /
-- register_frame .077; phi parse+gliner +0.695 (the expected parse-fed correlation),
-- parse+register −0.831 (complementary lanes, not costumes); effective rank participation
-- ratio 1.962 but 90%-variance rank 3. SKEW CAVEAT (re-cut before freezing tier semantics):
-- n=65 on a deliberately-simple corpus, on the deterministic-first segmentation lane where the
-- atomizer only escalates residual units (its marginal is lane-shaped silence, not reader
-- weakness). On that data the COLUMN carrier stands over flag-only; a flag-only shape remains
-- a one-commit flip (stop computing, the column stays NULL).
--
-- FAN-OUT: every schema holding a facts table (public + faultline_% + pin/seat schemas), the
-- 308/309 pattern; the guarded loop skips a partially-provisioned seat instead of aborting the
-- whole DO block. staged_facts gets the same column (its rows are the B/C lane the keep-both
-- tagging reads later).
DO $mig$
DECLARE
    _sch text;
BEGIN
    FOR _sch IN
        SELECT n.nspname
          FROM pg_namespace n
         WHERE EXISTS (
               SELECT 1
                 FROM pg_class c
                WHERE c.relnamespace = n.oid
                  AND c.relname = 'facts'
                  AND c.relkind = 'r')
         ORDER BY n.nspname
    LOOP
        BEGIN
            EXECUTE format('ALTER TABLE %I.facts ADD COLUMN IF NOT EXISTS consensus_tier SMALLINT', _sch);
            EXECUTE format('ALTER TABLE %I.facts ADD CONSTRAINT consensus_tier_domain CHECK (consensus_tier IN (1,2,3))', _sch);
        EXCEPTION
            WHEN duplicate_object THEN NULL;  -- the CHECK already exists (idempotent re-run)
            WHEN undefined_table THEN NULL;   -- facts dropped between probe and ALTER
            WHEN OTHERS THEN NULL;
        END;
        BEGIN
            EXECUTE format('ALTER TABLE %I.staged_facts ADD COLUMN IF NOT EXISTS consensus_tier SMALLINT', _sch);
            EXECUTE format('ALTER TABLE %I.staged_facts ADD CONSTRAINT staged_consensus_tier_domain CHECK (consensus_tier IN (1,2,3))', _sch);
        EXCEPTION
            WHEN duplicate_object THEN NULL;
            WHEN undefined_table THEN NULL;   -- a schema with facts but no staged_facts
            WHEN OTHERS THEN NULL;
        END;
    END LOOP;
END
$mig$;
