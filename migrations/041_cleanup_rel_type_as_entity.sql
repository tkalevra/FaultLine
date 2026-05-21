-- Migration: cleanup_rel_type_as_entity
-- Date: 2026-05-21
-- Purpose: Remove corrupted entities where rel_type names were registered as entities

-- This fixes the issue where rel_type names (parent_of, instance_of, etc.) were
-- being registered as entities in entity_aliases table, causing false entities
-- and corrupted facts in the knowledge graph.

-- PART 1: Identify corrupted entity IDs (rel_type names registered as entities)
-- These will be deleted from entity_aliases and potentially entities tables

WITH corrupted_entities AS (
  -- Find entity_aliases rows where the alias is a known rel_type
  SELECT DISTINCT ea.entity_id, ea.alias
  FROM entity_aliases ea
  INNER JOIN rel_types rt ON LOWER(ea.alias) = LOWER(rt.rel_type)
)
-- PART 2: Clean up entity_aliases
-- Delete all aliases that match rel_type names
DELETE FROM entity_aliases
WHERE alias IN (
  SELECT DISTINCT LOWER(rel_type) FROM rel_types
);

-- PART 3: Clean up facts that reference corrupted entities
-- These are facts where subject or object resolved to a rel_type name entity
-- This is a soft delete (won't actually execute, but documents the cleanup needed)
-- Once aliases are removed, these facts will be unreachable but not deleted

-- Verification query (run after migration):
-- SELECT ea.entity_id, ea.alias, COUNT(*) as fact_count
-- FROM entity_aliases ea
-- INNER JOIN rel_types rt ON LOWER(ea.alias) = LOWER(rt.rel_type)
-- GROUP BY ea.entity_id, ea.alias;
-- Should return 0 rows after this migration

-- Note: This migration runs idempotently - DELETE is safe to re-run
