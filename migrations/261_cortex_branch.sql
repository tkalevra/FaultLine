-- 261_cortex_branch.sql — carry the BRANCH a note was learned on, as a RANKING signal only.
--
-- Defect this closes (replayed on real rows, DEV/replay_stale_vs_fresh.py, 2026-08-16):
-- cortex recall ranking was blind to time and branch, so a stale procedural note (hardened
-- by repetition, carrying an old evidence sha) outranked the FRESH CORRECTION that
-- superseded it. Park et al. (2023) score agent-memory retrieval as recency x importance x
-- relevance — relevance and importance existed; recency did not. The same block of work
-- adds the recency terms in code; this migration adds the column that carries WHERE the
-- lesson was learned (a git branch, a matter number, a ward name — an opaque label the
-- caller knows), used ONLY as an additive ranking bonus when a recall states the branch
-- being worked. It NEVER gates, filters, or excludes anything: a mismatched or NULL branch
-- gets no bonus and no penalty, and untagged legacy notes keep surfacing unchanged.
--
-- Idempotent across all per-seat cortex schemas (flagent_<uuid>). A lazy in-process ensure
-- (cortex._ensure_branch_column) back-fills the same column for seats that miss this
-- migration, so an old seat never silently drops a note on a missing column.
DO $$
DECLARE _schema TEXT;
BEGIN
  FOR _schema IN SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'flagent\_%' LOOP
    EXECUTE format('ALTER TABLE %I.cortex_episodes ADD COLUMN IF NOT EXISTS branch TEXT DEFAULT NULL', _schema);
  END LOOP;
END $$;
