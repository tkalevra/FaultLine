-- 262_cortex_env_supersession.sql — ENV scoping + explicit SUPERSESSION links (Build 5).
--
-- OWNER DESIGN INTENT (2026-08-16): the cortex is the agents' own memory; anything
-- repetitive a human would "just know" must surface with least latency/friction.
--
-- ENV: a note's knowledge is often environment-BOUND (a deploy procedure is true on
-- truenas and wrong on p70). 'env' carries that binding as a RANKING signal only:
-- a recall that states its env gets same-env notes ranked first, other-env notes
-- demoted slightly and STAMPED at render (so the agent SEES why it ranked low), and
-- untagged (conceptual) notes stay neutral. Nothing is ever gated or hidden — a
-- truenas note still surfaces on p70 when it is the only match, visibly stamped.
--
-- SUPERSEDED_BY: explicit tombstone link from an outdated procedural note to the
-- correction that replaced it. Ranking already demotes pre-correction procedure
-- implicitly; this makes the replacement EXPLAINABLE at render ("OUTDATED — do X
-- instead because ..."). Set automatically when a correction lands that shares a tag
-- and >= 2 distinct text tokens with an older procedural note. Never a delete: the
-- old note stays queryable, it just points forward.
--
-- Idempotent across all per-seat cortex schemas (flagent_<uuid>). Lazy in-process
-- back-fill (cortex._ensure_ranking_columns) covers seats that miss this migration.
DO $$
DECLARE _schema TEXT;
BEGIN
  FOR _schema IN SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'flagent\_%' LOOP
    EXECUTE format('ALTER TABLE %I.cortex_episodes ADD COLUMN IF NOT EXISTS env TEXT DEFAULT NULL', _schema);
    EXECUTE format('ALTER TABLE %I.cortex_episodes ADD COLUMN IF NOT EXISTS superseded_by BIGINT DEFAULT NULL', _schema);
  END LOOP;
END $$;
