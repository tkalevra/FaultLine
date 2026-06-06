-- Migration 069: Add mutually_exclusive_with column to rel_types
-- BUG-B1: _validate_bidirectional_relationships() only checks inverse_rel_type pairs.
-- Family roles (spouse, parent_of, child_of, sibling_of) can be mutually exclusive
-- but share no inverse_rel_type linkage. This column enables metadata-driven exclusion
-- checking without hardcoding rel_type names in the validation function.
--
-- New rel_types can self-describe their mutual exclusions by populating this column.
-- _validate_bidirectional_relationships() queries this at runtime — zero code changes
-- needed for future rel_types.

ALTER TABLE rel_types
  ADD COLUMN IF NOT EXISTS mutually_exclusive_with TEXT[] DEFAULT '{}';

-- Family role mutual exclusions: spouse cannot coexist with parent/child relationships
UPDATE rel_types SET mutually_exclusive_with = ARRAY['parent_of', 'child_of']
  WHERE rel_type = 'spouse';

UPDATE rel_types SET mutually_exclusive_with = ARRAY['spouse']
  WHERE rel_type IN ('parent_of', 'child_of');

-- sibling_of is also incompatible with spouse (different family roles)
UPDATE rel_types SET mutually_exclusive_with = ARRAY['spouse']
  WHERE rel_type = 'sibling_of';

UPDATE rel_types SET mutually_exclusive_with = array_append(mutually_exclusive_with, 'sibling_of')
  WHERE rel_type = 'spouse'
    AND NOT ('sibling_of' = ANY(mutually_exclusive_with));
