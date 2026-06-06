-- Migration 070: work taxonomy rel_types + dangling taxonomy cleanup
-- Adds manages/managed_by/leads with full metadata (fact_class='B').
-- Corrects live DB state: manages was approved by WGM gate with fact_class='C'
-- due to the missing fact_class INSERT bug fixed in gate.py (dprompt-155 Fix 1).
-- Also removes dangling rel_type references from entity_taxonomies that do not
-- exist in the rel_types table (reports_to, has_component).

-- Part A: manages rel_type (Person → Organization/Person)
INSERT INTO rel_types (
    rel_type, label, natural_language,
    engine_generated, confidence, source,
    category, head_types, tail_types,
    is_symmetric, inverse_rel_type,
    is_hierarchy_rel, is_leaf_only,
    fact_class
) VALUES (
    'manages', 'manages', 'X manages Y',
    false, 0.8, 'builtin',
    'work', ARRAY['Person'], ARRAY['Organization', 'Person'],
    false, 'managed_by',
    false, false,
    'B'
) ON CONFLICT (rel_type) DO UPDATE SET
    fact_class = 'B',
    head_types = ARRAY['Person'],
    tail_types = ARRAY['Organization', 'Person'],
    category = 'work',
    natural_language = COALESCE(NULLIF(rel_types.natural_language, ''), 'X manages Y'),
    source = CASE WHEN rel_types.source = 'engine' THEN 'builtin' ELSE rel_types.source END;

-- Part A: managed_by rel_type (inverse of manages)
INSERT INTO rel_types (
    rel_type, label, natural_language,
    engine_generated, confidence, source,
    category, head_types, tail_types,
    is_symmetric, inverse_rel_type,
    is_hierarchy_rel, is_leaf_only,
    fact_class
) VALUES (
    'managed_by', 'managed by', 'X is managed by Y',
    false, 0.8, 'builtin',
    'work', ARRAY['Organization', 'Person'], ARRAY['Person'],
    false, 'manages',
    false, false,
    'B'
) ON CONFLICT (rel_type) DO UPDATE SET
    fact_class = 'B',
    category = 'work',
    natural_language = COALESCE(NULLIF(rel_types.natural_language, ''), 'X is managed by Y'),
    source = CASE WHEN rel_types.source = 'engine' THEN 'builtin' ELSE rel_types.source END;

-- Part A: leads rel_type (Person → Organization/Person)
INSERT INTO rel_types (
    rel_type, label, natural_language,
    engine_generated, confidence, source,
    category, head_types, tail_types,
    is_symmetric, inverse_rel_type,
    is_hierarchy_rel, is_leaf_only,
    fact_class
) VALUES (
    'leads', 'leads', 'X leads Y',
    false, 0.8, 'builtin',
    'work', ARRAY['Person'], ARRAY['Organization', 'Person'],
    false, NULL,
    false, false,
    'B'
) ON CONFLICT (rel_type) DO UPDATE SET
    fact_class = 'B',
    head_types = ARRAY['Person'],
    tail_types = ARRAY['Organization', 'Person'],
    category = 'work',
    natural_language = COALESCE(NULLIF(rel_types.natural_language, ''), 'X leads Y'),
    source = CASE WHEN rel_types.source = 'engine' THEN 'builtin' ELSE rel_types.source END;

-- Part B: add manages/leads/managed_by to work taxonomy rel_types_defining_group
UPDATE entity_taxonomies
SET rel_types_defining_group = array(
    SELECT DISTINCT unnest(rel_types_defining_group || ARRAY['manages', 'leads', 'managed_by'])
)
WHERE taxonomy_name = 'work';

-- Part C: remove reports_to from work taxonomy (rel_type doesn't exist in rel_types)
UPDATE entity_taxonomies
SET rel_types_defining_group = array_remove(rel_types_defining_group, 'reports_to')
WHERE taxonomy_name = 'work';

-- Part C: remove has_component from computer_system taxonomy (rel_type doesn't exist)
UPDATE entity_taxonomies
SET rel_types_defining_group = array_remove(rel_types_defining_group, 'has_component')
WHERE taxonomy_name = 'computer_system';

-- Part D: correct any engine-approved rel_types stuck at fact_class='C'
-- (handles live DB state from the now-fixed WGM gate INSERT bug)
UPDATE rel_types SET fact_class = 'B'
WHERE rel_type IN ('manages', 'leads', 'managed_by') AND fact_class = 'C';
