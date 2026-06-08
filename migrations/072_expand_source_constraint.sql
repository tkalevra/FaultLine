-- Migration 072: Add 'expand' to rel_types source CHECK constraint
-- The /expand command seeds operational rel_types with source='expand' for provenance tracking.
-- Without this value in the constraint, all operational ontology seeding silently fails.

ALTER TABLE rel_types DROP CONSTRAINT IF EXISTS rel_types_source_check;
ALTER TABLE rel_types ADD CONSTRAINT rel_types_source_check
    CHECK (source = ANY (ARRAY['wikidata', 'builtin', 'engine', 'user', 'expand']));
