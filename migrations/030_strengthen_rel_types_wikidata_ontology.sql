-- Migration 030: Strengthen rel_types metadata with Wikidata ontology constraints
-- Populates tail_types, head_types, is_symmetric, inverse_rel_type, is_hierarchy_rel
-- Based on CLAUDE.md WGM Ontology (Wikipedia/Wikidata alignment)
-- dprompt-97: Metadata-driven validation requires complete rel_types configuration

-- ============================================================================
-- SCALAR REL_TYPES (tail_types = ARRAY['SCALAR'])
-- ============================================================================
UPDATE rel_types SET
  tail_types = ARRAY['SCALAR']::TEXT[],
  head_types = ARRAY['Person', 'Organization', 'Location', 'Animal', 'Object']::TEXT[]
WHERE rel_type IN (
  'pref_name',      -- entity preferred name (skos:prefLabel)
  'also_known_as',  -- entity alias (skos:altLabel)
  'age',            -- person age (years)
  'height',         -- physical height
  'weight',         -- physical weight
  'born_on',        -- birth date
  'occupation',     -- person profession
  'nationality'     -- person country of origin
) AND (tail_types IS NULL OR tail_types = '{}');

-- ============================================================================
-- RELATIONSHIP REL_TYPES: Family & Identity (SCALAR tail for identity, PERSON for relations)
-- ============================================================================

-- parent_of / child_of: asymmetric pair
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Person']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = 'child_of',
  is_hierarchy_rel = false
WHERE rel_type = 'parent_of' AND (head_types IS NULL OR head_types = '{}');

UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Person']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = 'parent_of',
  is_hierarchy_rel = false
WHERE rel_type = 'child_of' AND (head_types IS NULL OR head_types = '{}');

-- spouse: symmetric pair
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Person']::TEXT[],
  is_symmetric = true,
  inverse_rel_type = 'spouse',
  is_hierarchy_rel = false
WHERE rel_type = 'spouse' AND (head_types IS NULL OR head_types = '{}');

-- sibling_of: symmetric
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Person']::TEXT[],
  is_symmetric = true,
  inverse_rel_type = 'sibling_of',
  is_hierarchy_rel = false
WHERE rel_type = 'sibling_of' AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- RELATIONSHIP REL_TYPES: Association & Connectivity
-- ============================================================================

-- knows / friend_of / met: symmetric social connections
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Person', 'Organization']::TEXT[],
  is_symmetric = true,
  inverse_rel_type = 'knows',
  is_hierarchy_rel = false
WHERE rel_type = 'knows' AND (head_types IS NULL OR head_types = '{}');

UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Person']::TEXT[],
  is_symmetric = true,
  inverse_rel_type = 'friend_of',
  is_hierarchy_rel = false
WHERE rel_type = 'friend_of' AND (head_types IS NULL OR head_types = '{}');

UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Person']::TEXT[],
  is_symmetric = true,
  inverse_rel_type = 'met',
  is_hierarchy_rel = false
WHERE rel_type = 'met' AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- RELATIONSHIP REL_TYPES: Work & Organization
-- ============================================================================

-- works_for: person employed by organization (asymmetric)
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Organization', 'Person']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type = 'works_for' AND (head_types IS NULL OR head_types = '{}');

-- educated_at: person studied at institution (asymmetric)
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Organization', 'Location']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type = 'educated_at' AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- RELATIONSHIP REL_TYPES: Ownership & Possession
-- ============================================================================

-- owns: person owns object/animal (asymmetric)
UPDATE rel_types SET
  head_types = ARRAY['Person', 'Organization']::TEXT[],
  tail_types = ARRAY['Animal', 'Object', 'Organization']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type = 'owns' AND (head_types IS NULL OR head_types = '{}');

-- has_pet: person owns animal (asymmetric specialization of owns)
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Animal']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type = 'has_pet' AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- RELATIONSHIP REL_TYPES: Location & Residence
-- ============================================================================

-- lives_at: person resides at address (asymmetric)
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['SCALAR']::TEXT[],  -- address is scalar string
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type = 'lives_at' AND (head_types IS NULL OR head_types = '{}');

-- lives_in: person resides in location (asymmetric)
UPDATE rel_types SET
  head_types = ARRAY['Person', 'Organization', 'Animal']::TEXT[],
  tail_types = ARRAY['Location']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type = 'lives_in' AND (head_types IS NULL OR head_types = '{}');

-- born_in: person birthplace (asymmetric)
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['Location']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type = 'born_in' AND (head_types IS NULL OR head_types = '{}');

-- located_in: entity location (asymmetric, general)
UPDATE rel_types SET
  head_types = ARRAY['ANY']::TEXT[],
  tail_types = ARRAY['Location']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type = 'located_in' AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- RELATIONSHIP REL_TYPES: Preferences & Attributes
-- ============================================================================

-- likes / dislikes / prefers: person sentiment (asymmetric)
UPDATE rel_types SET
  head_types = ARRAY['Person', 'Animal']::TEXT[],
  tail_types = ARRAY['ANY']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type IN ('likes', 'dislikes', 'prefers') AND (head_types IS NULL OR head_types = '{}');

-- has_gender: person gender (asymmetric, scalar or entity)
UPDATE rel_types SET
  head_types = ARRAY['Person']::TEXT[],
  tail_types = ARRAY['SCALAR', 'Concept']::TEXT[],
  is_symmetric = false,
  inverse_rel_type = NULL,
  is_hierarchy_rel = false
WHERE rel_type = 'has_gender' AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- IDENTITY REL_TYPES: Equivalence (symmetric)
-- ============================================================================

-- same_as: entity equivalence (symmetric owl:sameAs)
UPDATE rel_types SET
  head_types = ARRAY['ANY']::TEXT[],
  tail_types = ARRAY['ANY']::TEXT[],
  is_symmetric = true,
  inverse_rel_type = 'same_as',
  is_hierarchy_rel = false
WHERE rel_type = 'same_as' AND (head_types IS NULL OR head_types = '{}');

-- related_to: loose association (symmetric, generic)
UPDATE rel_types SET
  head_types = ARRAY['ANY']::TEXT[],
  tail_types = ARRAY['ANY']::TEXT[],
  is_symmetric = true,
  inverse_rel_type = 'related_to',
  is_hierarchy_rel = false
WHERE rel_type = 'related_to' AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- HIERARCHY REL_TYPES: Classification & Composition (is_hierarchy_rel = true)
-- ============================================================================

-- instance_of: entity is instance of class (NOT transitive per CLAUDE.md)
UPDATE rel_types SET
  head_types = ARRAY['ANY']::TEXT[],
  tail_types = ARRAY['Concept']::TEXT[],
  is_symmetric = false,
  is_hierarchy_rel = true,
  inverse_rel_type = NULL
WHERE rel_type = 'instance_of' AND (is_hierarchy_rel IS NULL OR is_hierarchy_rel = false);

-- subclass_of: class hierarchy (transitive, IS_HIERARCHY_REL)
UPDATE rel_types SET
  head_types = ARRAY['Concept']::TEXT[],
  tail_types = ARRAY['Concept']::TEXT[],
  is_symmetric = false,
  is_hierarchy_rel = true,
  inverse_rel_type = NULL
WHERE rel_type = 'subclass_of' AND (is_hierarchy_rel IS NULL OR is_hierarchy_rel = false);

-- part_of: composition (object is part of subject, hierarchical)
UPDATE rel_types SET
  head_types = ARRAY['ANY']::TEXT[],
  tail_types = ARRAY['ANY']::TEXT[],
  is_symmetric = false,
  is_hierarchy_rel = true,
  inverse_rel_type = NULL
WHERE rel_type = 'part_of' AND (is_hierarchy_rel IS NULL OR is_hierarchy_rel = false);

-- is_a: general inheritance (hierarchical alias for subclass_of)
UPDATE rel_types SET
  head_types = ARRAY['ANY']::TEXT[],
  tail_types = ARRAY['Concept']::TEXT[],
  is_symmetric = false,
  is_hierarchy_rel = true,
  inverse_rel_type = NULL
WHERE rel_type = 'is_a' AND (is_hierarchy_rel IS NULL OR is_hierarchy_rel = false);

-- member_of: entity membership in group (hierarchical)
UPDATE rel_types SET
  head_types = ARRAY['ANY']::TEXT[],
  tail_types = ARRAY['Concept', 'Organization']::TEXT[],
  is_symmetric = false,
  is_hierarchy_rel = true,
  inverse_rel_type = NULL
WHERE rel_type = 'member_of' AND (is_hierarchy_rel IS NULL OR is_hierarchy_rel = false);

-- created_by: creation/authorship relationship
UPDATE rel_types SET
  head_types = ARRAY['ANY']::TEXT[],
  tail_types = ARRAY['Person', 'Organization']::TEXT[],
  is_symmetric = false,
  is_hierarchy_rel = false,
  inverse_rel_type = NULL
WHERE rel_type = 'created_by' AND (head_types IS NULL OR head_types = '{}');

-- ============================================================================
-- Verify updates
-- ============================================================================
SELECT rel_type, head_types, tail_types, is_symmetric, inverse_rel_type, is_hierarchy_rel
FROM rel_types
WHERE rel_type IN (
  'pref_name', 'also_known_as', 'age', 'spouse', 'parent_of', 'child_of',
  'instance_of', 'subclass_of', 'knows', 'works_for', 'has_pet'
)
ORDER BY rel_type;
