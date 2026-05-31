-- Migration: Create entity_types table for database-driven type validation
-- Date: 2026-05-29
-- Purpose: Replace hardcoded VALID_ENTITY_TYPES with database-driven system

CREATE TABLE IF NOT EXISTS entity_types (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type VARCHAR(50) NOT NULL UNIQUE,
    description TEXT,
    category VARCHAR(50),  -- Person, Organization, Location, Object, Concept, Animal
    created_at TIMESTAMP DEFAULT now(),
    is_learnable BOOLEAN DEFAULT true  -- Can this type be learned by re_embedder?
);

-- Seed initial types (must match VALID_ENTITY_TYPES from main.py)
INSERT INTO entity_types (entity_type, category, description, is_learnable) VALUES
    ('Person', 'Person', 'A human being', true),
    ('Organization', 'Organization', 'A company, institution, or group', true),
    ('Location', 'Location', 'A place, city, country, or geographic area', true),
    ('Object', 'Object', 'A physical thing or item', true),
    ('Event', 'Event', 'An occurrence or happening', true),
    ('Animal', 'Animal', 'A non-human living creature', true)
ON CONFLICT (entity_type) DO NOTHING;

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_entity_types_lookup ON entity_types(entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_types_learnable ON entity_types(is_learnable) WHERE is_learnable = true;
