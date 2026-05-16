-- Migration 029: Remove is_leaf_only constraint (dprompt-86)
-- Reset is_leaf_only to FALSE for all rel_types.
-- Rationale: is_leaf_only constraint was too restrictive and prevented
-- legitimate facts like (user, lives_at, address) when address has instance_of type info.
-- Idempotent: safe to run multiple times.

UPDATE rel_types SET is_leaf_only = FALSE;
