-- Migration 026: Pre-seed body_parts taxonomy (dBug-018 Phase B)
-- Idempotent: safe to run multiple times.

-- Insert body_parts taxonomy if not exists
INSERT INTO entity_taxonomies (
    taxonomy_name,
    description,
    member_entity_types,
    rel_types_defining_group,
    is_hierarchical,
    parent_rel_type
) VALUES (
    'body_parts',
    'Human anatomical body parts — referenced by medical rel_types',
    ARRAY['Object']::text[],
    ARRAY['instance_of', 'part_of']::text[],
    true,
    'part_of'
) ON CONFLICT (taxonomy_name) DO NOTHING;

-- Pre-seed common body part entities (using deterministic UUIDs)
-- These are shared entities accessible to all users (user_id = 'anonymous')
INSERT INTO entities (id, user_id, entity_type)
SELECT v.id, 'anonymous', 'Object'
FROM (VALUES
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'back'), 'back'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'spine'), 'spine'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'knee'), 'knee'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'shoulder'), 'shoulder'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'head'), 'head'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'arm'), 'arm'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'leg'), 'leg'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'neck'), 'neck'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'wrist'), 'wrist'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'ankle'), 'ankle'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'elbow'), 'elbow'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'hip'), 'hip'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'hand'), 'hand'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'foot'), 'foot'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'chest'), 'chest')
) AS v(id, alias)
ON CONFLICT (id, user_id) DO NOTHING;

-- Add preferred aliases for body parts
INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred)
SELECT id, 'anonymous', alias, true FROM (
    VALUES
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'back'), 'back'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'spine'), 'spine'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'knee'), 'knee'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'shoulder'), 'shoulder'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'head'), 'head'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'arm'), 'arm'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'leg'), 'leg'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'neck'), 'neck'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'wrist'), 'wrist'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'ankle'), 'ankle'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'elbow'), 'elbow'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'hip'), 'hip'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'hand'), 'hand'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'foot'), 'foot'),
    (uuid_generate_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid, 'chest'), 'chest')
) AS bp(id, alias)
ON CONFLICT (entity_id, user_id, alias) DO NOTHING;
