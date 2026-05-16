-- Migration 028: rel_type_definition column (dprompt-85)
-- Stores LLM-generated semantic definitions alongside facts so
-- downstream models understand hierarchy vs relational semantics.
-- Idempotent: safe to run multiple times.

ALTER TABLE facts ADD COLUMN IF NOT EXISTS rel_type_definition TEXT DEFAULT '';
ALTER TABLE staged_facts ADD COLUMN IF NOT EXISTS rel_type_definition TEXT DEFAULT '';
