-- ============================================================================
-- 173_cortex_evidence.sql — EVIDENCE-BOUND cortex notes.
--
-- WHY (the failure this closes): a cortex note is a CLAIM about the world, and a claim with
-- no proof rots into a TRAP. Twice on 2026-07-13 stale knowledge manufactured phantom work:
--   * a CLAUDE.md "Known Bug" said the Q1 deterministic walk had a gap — it had been CLOSED
--     weeks earlier (12b6e80); the doc commit was an ANCESTOR of the fix and nobody re-checked.
--   * two test files asserted contracts for code that had been DELETED — they could never pass.
-- Both cost real time, because nothing recorded WHAT the claim was checked against, or WHEN.
--
-- So: a note may now carry EVIDENCE — the artifact that produced it. `evidence_sha` is the
-- git commit the claim was verified against, which makes staleness DECIDABLE: an agent can ask
-- `git merge-base --is-ancestor <evidence_sha> HEAD` and know the claim is unverified since.
-- `evidence` holds the rest (the ref, the command, the actual output excerpt).
--
-- A note WITH evidence outranks a note without it at recall (see _EVIDENCE_BONUS): a lesson you
-- can check beats a lesson you must take on faith. Evidence is OPTIONAL — a note without it is
-- still a note (never lose the lesson), it simply carries less weight and cannot self-date.
--
-- Per-seat: cortex lives in flagent_<uuid> (the truth firewall). This migration is applied to
-- EVERY existing cortex schema; the template (templates/cortex_schema.sql) carries it forward
-- for newly provisioned seats.
-- ============================================================================

DO $$
DECLARE
    s TEXT;
BEGIN
    FOR s IN
        SELECT nspname FROM pg_namespace WHERE nspname LIKE 'flagent\_%'
    LOOP
        -- The commit the claim was checked against. NULL = never verified against code state.
        EXECUTE format(
            'ALTER TABLE IF EXISTS %I.cortex_episodes '
            '  ADD COLUMN IF NOT EXISTS evidence_sha TEXT', s);
        -- {ref, command, output, artifact, at} — the proof, in the agent''s own capture.
        EXECUTE format(
            'ALTER TABLE IF EXISTS %I.cortex_episodes '
            '  ADD COLUMN IF NOT EXISTS evidence JSONB', s);
        -- When the claim was last CONFIRMED true (not merely re-observed).
        EXECUTE format(
            'ALTER TABLE IF EXISTS %I.cortex_episodes '
            '  ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ', s);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_%s_cortex_evidence_sha '
            '  ON %I.cortex_episodes (evidence_sha) WHERE evidence_sha IS NOT NULL',
            replace(s, 'flagent_', ''), s);
    END LOOP;
END $$;
