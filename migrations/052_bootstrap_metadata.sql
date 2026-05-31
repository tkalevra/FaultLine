-- Migration: DEPRECATED — Bootstrap metadata now handled in provisioning
-- Date: 2026-05-27
-- Purpose: Seed rel_types, entity_taxonomies, and negation_patterns
-- NOTE: This file is DEPRECATED. Bootstrap now happens in schema_manager.py
--       when per-user schemas are provisioned. This ensures metadata is only
--       inserted into per-user schemas (where the tables exist), not the
--       global public schema (where these tables don't exist).
--
-- Old behavior: Migration ran on public schema at startup → transaction aborts
-- New behavior: schema_manager.py provisioning → safe per-user bootstrap
--
-- This file is kept for backwards compatibility in case old migrations list
-- it, but it does nothing at startup. Bootstrap logic moved to:
--   src/provisioning/schema_manager.py:create_user_schema() → _execute_bootstrap_queries()

-- No-op: All bootstrap now handled in per-user provisioning
SELECT 1;
