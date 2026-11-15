-- 217_cortex_client_source.sql — capture WHICH MCP client (which LLM/tool) wrote each cortex note.
--
-- The cortex is a per-seat, model-agnostic working memory: the SAME seat can be served by many
-- clients/LLMs over its life. `client_source` tags each note with the client that added it, so a
-- multi-model seat can tell its own notes apart and a "mine" recall lane can filter to them. Same
-- seat, same schema — just tagged with who wrote it. NULL on legacy rows and on any note the model
-- didn't tag; recall's `client_source IS NULL` lane keeps untagged notes visible to everyone
-- (never lose a lesson to a missing tag).
--
-- Idempotent across all per-seat cortex schemas (`flagent_<uuid>`). The column is added with
-- ADD COLUMN IF NOT EXISTS so re-runs are safe.
DO $$
DECLARE _schema TEXT;
BEGIN
  FOR _schema IN SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'flagent\_%' LOOP
    EXECUTE format('ALTER TABLE %I.cortex_episodes ADD COLUMN IF NOT EXISTS client_source TEXT DEFAULT NULL', _schema);
  END LOOP;
END $$;
