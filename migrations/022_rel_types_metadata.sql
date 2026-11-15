-- Migration 022: rel_types metadata for dynamic validation framework
-- Adds validation properties to rel_types so the ontology self-describes
-- constraints. Replaces hardcoded rules in ingest validation.
-- dprompt-65: Metadata-Driven Validation Framework

-- Add validation metadata columns (idempotent via IF NOT EXISTS checks)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'rel_types' AND column_name = 'is_symmetric'
    ) THEN
        ALTER TABLE rel_types ADD COLUMN is_symmetric BOOLEAN DEFAULT FALSE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'rel_types' AND column_name = 'inverse_rel_type'
    ) THEN
        ALTER TABLE rel_types ADD COLUMN inverse_rel_type VARCHAR(100) DEFAULT NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'rel_types' AND column_name = 'is_leaf_only'
    ) THEN
        ALTER TABLE rel_types ADD COLUMN is_leaf_only BOOLEAN DEFAULT FALSE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'rel_types' AND column_name = 'is_hierarchy_rel'
    ) THEN
        ALTER TABLE rel_types ADD COLUMN is_hierarchy_rel BOOLEAN DEFAULT FALSE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'rel_types' AND column_name = 'allows_leaf_rels'
    ) THEN
        ALTER TABLE rel_types ADD COLUMN allows_leaf_rels TEXT[] DEFAULT NULL;
    END IF;
END $$;

-- Pre-populate metadata for existing rel_types (idempotent)

-- Symmetric relationships (both directions mean the same thing)
UPDATE rel_types SET is_symmetric = TRUE
WHERE rel_type IN ('spouse', 'sibling_of', 'knows', 'friend_of', 'met', 'same_as');

-- Inverse pairs (only one direction should exist for same entity pair)
UPDATE rel_types SET inverse_rel_type = 'child_of' WHERE rel_type = 'parent_of';
UPDATE rel_types SET inverse_rel_type = 'parent_of' WHERE rel_type = 'child_of';

-- Leaf-only relationships (cannot apply to type/hierarchy objects)
-- These should only apply to leaf/instance entities, never to types
UPDATE rel_types SET is_leaf_only = TRUE
WHERE rel_type IN ('owns', 'has_pet', 'works_for', 'lives_in', 'lives_at',
                   'educated_at', 'likes', 'dislikes', 'prefers');

-- Hierarchy relationships (define what entities ARE)
UPDATE rel_types SET is_hierarchy_rel = TRUE
WHERE rel_type IN ('instance_of', 'subclass_of', 'member_of', 'part_of', 'is_a');

-- Hierarchy rels: which leaf relationships can apply to their objects
-- (e.g., if fraggle instance_of dog → has_pet dog is allowed, but owns morkie is not
--  because morkie itself is a type defined by instance_of)
UPDATE rel_types SET allows_leaf_rels = ARRAY['has_pet', 'owns', 'works_for', 'lives_in', 'lives_at']
WHERE rel_type IN ('instance_of', 'subclass_of', 'member_of', 'part_of', 'is_a');
