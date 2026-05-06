ALTER TABLE entity_attributes
    DROP CONSTRAINT IF EXISTS entity_attributes_pkey,
    DROP CONSTRAINT IF EXISTS entity_attributes_unique;

ALTER TABLE entity_attributes
    ADD CONSTRAINT entity_attributes_unique
    UNIQUE (user_id, entity_id, attribute);
