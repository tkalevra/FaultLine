-- Migration 071: taxonomy rel_type repairs — add has_pet to household
-- The household taxonomy currently defines rel_types_defining_group as
-- {lives_at, lives_in, member_of}.  Pets are household members but
-- has_pet is absent, causing "who is in my household" to skip pets.
--
-- Family taxonomy: has_pet is intentionally NOT added here — whether pets
-- are considered family members is a user-configurable policy decision.

-- Add has_pet to household.rel_types_defining_group (idempotent)
UPDATE entity_taxonomies
SET rel_types_defining_group = array_append(rel_types_defining_group, 'has_pet')
WHERE taxonomy_name = 'household'
  AND NOT (rel_types_defining_group @> ARRAY['has_pet']);
