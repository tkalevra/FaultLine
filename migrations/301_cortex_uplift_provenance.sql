-- 301_cortex_uplift_provenance.sql — declarative fan-out of the SCHEMA UPLIFT provenance
-- columns (model / note_type / project) into every existing per-seat cortex schema.
--
-- WHY A MIGRATION NOW
-- -------------------
-- The uplift (owner directive, commit e0dd8168) added three provenance columns to
-- cortex_episodes: `model` (which AI model wrote the note — the agent-identity half of the
-- product story "stronger agents uplift weaker ones": recall renders `model=<x>` so any
-- consumer can see WHO learned the lesson and weight it), `note_type` (free-form owner-facing
-- type; the fixed engine enum stays `category`), and `project`. Two of the three carriers
-- already existed when those columns landed:
--   * the TEMPLATE (src/provisioning/templates/cortex_schema.sql) — new seats are born with
--     them;
--   * the LAZY IN-PROCESS ENSURE (cortex.py::_ensure_branch_column, the belt-and-braces
--     ALTER ... IF NOT EXISTS that runs at note-write time) — so an old seat never drops a
--     note on a missing column.
-- What did NOT exist is a numbered migration — the declarative record that makes the schema
-- complete without relying on a process having once written a note. For the Volenti Cortex
-- product split that matters: a standalone deployment replays migrations as the schema
-- definition (fresh-mint vs migration-parity), and a column that only exists because the
-- app lazily ALTERed it is invisible to that replay. Same rule migrations 197/273/298/299/300
-- shipped under: template + migration, never one without the other.
--
-- The lazy ensure STAYS (it is the runtime fail-safe); this migration makes the declarative
-- record match it. Columns are added with ADD COLUMN IF NOT EXISTS, so seats already healed
-- by the lazy ensure are untouched — additive + idempotent only: no DROP, no UPDATE, no
-- DELETE, no backfill. NULL on legacy rows stays NULL (the columns are provenance that was
-- never stated; recall renders legacy rows unchanged).
--
-- GUARDED FANOUT: the loop joins information_schema.schemata AND to_regclass (the seat's
-- cortex_episodes actually exists) — `ADD COLUMN IF NOT EXISTS` guards the COLUMN, not the
-- TABLE, and a partially-provisioned seat (schema minted, table creation failed) would abort
-- the whole DO block on undefined_table, skipping every seat after it. Skipped here instead.
-- It fans out over the CORTEX schema family only — `flagent\_<uuid>` — never `faultline\_%`:
-- the truth firewall (the cortex schema namespace is disjoint from the user-fact schema
-- namespace) is untouched by construction.
DO $$
DECLARE _schema TEXT;
BEGIN
  FOR _schema IN
    SELECT schema_name FROM information_schema.schemata
    WHERE schema_name LIKE 'flagent\_%'
      AND to_regclass(format('%I.cortex_episodes', schema_name)) IS NOT NULL
  LOOP
    EXECUTE format('ALTER TABLE %I.cortex_episodes ADD COLUMN IF NOT EXISTS model TEXT DEFAULT NULL', _schema);
    EXECUTE format('ALTER TABLE %I.cortex_episodes ADD COLUMN IF NOT EXISTS note_type TEXT DEFAULT NULL', _schema);
    EXECUTE format('ALTER TABLE %I.cortex_episodes ADD COLUMN IF NOT EXISTS project TEXT DEFAULT NULL', _schema);
  END LOOP;
END $$;
