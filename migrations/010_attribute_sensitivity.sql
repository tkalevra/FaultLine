-- Add sensitivity classification to entity_attributes
ALTER TABLE entity_attributes
ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'public'
CHECK (sensitivity IN ('public', 'private', 'secret'));

CREATE INDEX IF NOT EXISTS idx_entity_attributes_sensitivity
ON entity_attributes (user_id, sensitivity);

-- Update known sensitive attribute types
UPDATE entity_attributes SET sensitivity = 'private'
WHERE attribute IN ('phone', 'address', 'email', 'lives_at', 'lives_in', 'location');

UPDATE entity_attributes SET sensitivity = 'secret'
WHERE attribute IN ('password', 'token', 'api_key', 'secret', 'credential');
