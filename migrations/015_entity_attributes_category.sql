ALTER TABLE entity_attributes ADD COLUMN IF NOT EXISTS category TEXT;

-- Backfill existing rows from rel_types
UPDATE entity_attributes ea
SET category = rt.category
FROM rel_types rt
WHERE ea.attribute = rt.rel_type;
