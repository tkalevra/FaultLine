-- Ontology standards alignment: RDF/SKOS/OWL semantics
-- Add W3C-aligned relationship types

-- Add instance_of and subclass_of (split from is_a)
INSERT INTO rel_types (rel_type, label, wikidata_pid, engine_generated, confidence)
VALUES
    ('instance_of',  'instance of',  'P31',  false, 1.0),
    ('subclass_of',  'subclass of',  'P279', false, 1.0),
    ('same_as',      'same as',      'Q39893449', false, 1.0),
    ('pref_name',    'preferred name', null, false, 1.0)
ON CONFLICT (rel_type) DO NOTHING;

-- Mark is_a as deprecated but keep it valid for backward compatibility
UPDATE rel_types
SET label = 'is a (deprecated: use instance_of or subclass_of)',
    confidence = 0.5
WHERE rel_type = 'is_a';

-- Update also_known_as label to reflect SKOS semantics
UPDATE rel_types
SET label = 'also known as (skos:altLabel; use pref_name for preferred display name)'
WHERE rel_type = 'also_known_as';

-- Add inverse_of column to track OWL inverseOf relationships
ALTER TABLE rel_types ADD COLUMN IF NOT EXISTS
    inverse_of TEXT REFERENCES rel_types(rel_type) ON DELETE SET NULL;

-- Set up inverse relationships
UPDATE rel_types SET inverse_of = 'parent_of' WHERE rel_type = 'child_of';
UPDATE rel_types SET inverse_of = 'child_of'  WHERE rel_type = 'parent_of';

-- Symmetric relationships (inverse = self)
UPDATE rel_types SET inverse_of = 'spouse'     WHERE rel_type = 'spouse';
UPDATE rel_types SET inverse_of = 'sibling_of' WHERE rel_type = 'sibling_of';
UPDATE rel_types SET inverse_of = 'same_as'    WHERE rel_type = 'same_as';
UPDATE rel_types SET inverse_of = 'friend_of'  WHERE rel_type = 'friend_of';
UPDATE rel_types SET inverse_of = 'knows'      WHERE rel_type = 'knows';
UPDATE rel_types SET inverse_of = 'met'        WHERE rel_type = 'met';

-- Create index on inverse_of for efficient lookups
CREATE INDEX IF NOT EXISTS idx_rel_types_inverse_of
    ON rel_types (inverse_of) WHERE inverse_of IS NOT NULL;
