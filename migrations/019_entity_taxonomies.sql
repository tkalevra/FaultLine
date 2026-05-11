-- 019_entity_taxonomies.sql
-- Data-driven entity grouping system.
-- Replaces brittle hardcoded extraction patterns with declarative taxonomies.
-- See dprompt-20.md for full spec.

CREATE TABLE IF NOT EXISTS entity_taxonomies (
    id BIGSERIAL PRIMARY KEY,
    taxonomy_name VARCHAR(64) NOT NULL UNIQUE,
    description TEXT,

    -- Which entity types belong to this group?
    member_entity_types TEXT[] NOT NULL DEFAULT '{}',

    -- Which relationships define membership in this group?
    rel_types_defining_group TEXT[] NOT NULL DEFAULT '{}',

    -- Can facts transitively propagate through group members?
    has_transitivity BOOLEAN DEFAULT false,
    transitive_rel_types TEXT[] DEFAULT '{}',

    -- Hierarchical grouping (for locations, systems)
    is_hierarchical BOOLEAN DEFAULT false,
    parent_rel_type VARCHAR(64),

    -- Provenance
    source VARCHAR(32) DEFAULT 'seeded',
    created_at TIMESTAMP DEFAULT now()
);

-- ── Pre-seed the 5 core taxonomies ──────────────────────────────────────

-- 1. Family: nuclear family relationships
INSERT INTO entity_taxonomies (taxonomy_name, description, member_entity_types,
    rel_types_defining_group, has_transitivity, transitive_rel_types, source)
VALUES (
    'family',
    'Nuclear family members linked by kinship relationships',
    ARRAY['Person'],
    ARRAY['parent_of', 'child_of', 'spouse', 'sibling_of'],
    true,
    ARRAY['lives_in', 'lives_at', 'works_for', 'has_pet', 'pref_name', 'also_known_as', 'age', 'born_on', 'occupation', 'nationality'],
    'seeded'
)
ON CONFLICT (taxonomy_name) DO NOTHING;

-- 2. Household: cohabitation grouping (people + pets)
INSERT INTO entity_taxonomies (taxonomy_name, description, member_entity_types,
    rel_types_defining_group, has_transitivity, transitive_rel_types, source)
VALUES (
    'household',
    'Entities living in the same residence — people and animals',
    ARRAY['Person', 'Animal'],
    ARRAY['lives_at', 'lives_in', 'member_of'],
    true,
    ARRAY['has_pet', 'spouse', 'parent_of', 'child_of', 'sibling_of', 'pref_name', 'also_known_as', 'works_for'],
    'seeded'
)
ON CONFLICT (taxonomy_name) DO NOTHING;

-- 3. Work: employment and organizational relationships
INSERT INTO entity_taxonomies (taxonomy_name, description, member_entity_types,
    rel_types_defining_group, has_transitivity, transitive_rel_types, source)
VALUES (
    'work',
    'Employment, team, and organizational relationships',
    ARRAY['Person', 'Organization'],
    ARRAY['works_for', 'part_of', 'reports_to'],
    true,
    ARRAY['located_in', 'occupation', 'educated_at', 'pref_name', 'also_known_as', 'lives_in'],
    'seeded'
)
ON CONFLICT (taxonomy_name) DO NOTHING;

-- 4. Location: geographic/spatial hierarchies
INSERT INTO entity_taxonomies (taxonomy_name, description, member_entity_types,
    rel_types_defining_group, has_transitivity, transitive_rel_types,
    is_hierarchical, parent_rel_type, source)
VALUES (
    'location',
    'Geographic and spatial containment hierarchies',
    ARRAY['Location'],
    ARRAY['located_in', 'located_at', 'lives_in', 'lives_at'],
    true,
    ARRAY['works_for', 'educated_at', 'born_in'],
    true,
    'located_in',
    'seeded'
)
ON CONFLICT (taxonomy_name) DO NOTHING;

-- 5. Computer System: infrastructure and component hierarchies
INSERT INTO entity_taxonomies (taxonomy_name, description, member_entity_types,
    rel_types_defining_group, has_transitivity, transitive_rel_types,
    is_hierarchical, parent_rel_type, source)
VALUES (
    'computer_system',
    'IT infrastructure, hardware, and software component hierarchies',
    ARRAY['Concept', 'Object'],
    ARRAY['instance_of', 'has_component', 'part_of'],
    true,
    ARRAY['located_in', 'created_by', 'hostname', 'fqdn', 'ip_address', 'has_ram', 'has_storage', 'expires_on'],
    true,
    'part_of',
    'seeded'
)
ON CONFLICT (taxonomy_name) DO NOTHING;
