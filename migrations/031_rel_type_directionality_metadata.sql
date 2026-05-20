-- dprompt-126: Rel-type directionality & natural language ontology
-- Extends rel_type_aliases with directionality rules
-- Extends rel_types with natural language descriptions
-- Extends ontology_evaluations with LLM-generated metadata

-- Phase 1: Alias directionality preservation
ALTER TABLE rel_type_aliases ADD COLUMN IF NOT EXISTS requires_inversion BOOLEAN DEFAULT FALSE;
ALTER TABLE rel_type_aliases ADD COLUMN IF NOT EXISTS is_symmetric BOOLEAN DEFAULT FALSE;
ALTER TABLE rel_type_aliases ADD COLUMN IF NOT EXISTS inverse_alias VARCHAR(255);

-- Phase 2: Natural language descriptions for rel_types
ALTER TABLE rel_types ADD COLUMN IF NOT EXISTS natural_language VARCHAR(500);
ALTER TABLE rel_types ADD COLUMN IF NOT EXISTS examples TEXT;

-- Phase 3: LLM-generated metadata for ontology evaluation
ALTER TABLE ontology_evaluations ADD COLUMN IF NOT EXISTS llm_natural_language VARCHAR(500);
ALTER TABLE ontology_evaluations ADD COLUMN IF NOT EXISTS llm_is_symmetric BOOLEAN;
ALTER TABLE ontology_evaluations ADD COLUMN IF NOT EXISTS llm_inverse_rel_type VARCHAR(255);
ALTER TABLE ontology_evaluations ADD COLUMN IF NOT EXISTS llm_category VARCHAR(100);
ALTER TABLE ontology_evaluations ADD COLUMN IF NOT EXISTS llm_fact_class CHAR(1);
ALTER TABLE ontology_evaluations ADD COLUMN IF NOT EXISTS llm_confidence FLOAT;
ALTER TABLE ontology_evaluations ADD COLUMN IF NOT EXISTS llm_metadata_json JSONB;

-- Populate directionality rules for family relations (critical path)
-- son_of, daughter_of, etc. → parent_of with inversion
UPDATE rel_type_aliases SET
    requires_inversion = TRUE,
    is_symmetric = FALSE,
    inverse_alias = 'parent_of'
WHERE alias IN ('son_of', 'daughter_of', 'kid_of', 'child_of');

-- has_child, has_son, etc. → parent_of without inversion
UPDATE rel_type_aliases SET
    requires_inversion = FALSE,
    is_symmetric = FALSE,
    inverse_alias = 'child_of'
WHERE alias IN ('has_child', 'has_children', 'has_son', 'has_daughter', 'has_kid');

-- spouse relations → spouse (symmetric, no inversion)
UPDATE rel_type_aliases SET
    requires_inversion = FALSE,
    is_symmetric = TRUE,
    inverse_alias = 'spouse_of'
WHERE alias IN ('spouse_of', 'married_to', 'husband_of', 'wife_of');

-- sibling relations (symmetric)
UPDATE rel_type_aliases SET
    requires_inversion = FALSE,
    is_symmetric = TRUE,
    inverse_alias = alias
WHERE alias IN ('sibling_of', 'brother_of', 'sister_of');

-- Populate natural language descriptions for Wikidata rel_types
UPDATE rel_types SET natural_language = 'X is the parent of Y' WHERE rel_type = 'parent_of';
UPDATE rel_types SET natural_language = 'X is the child of Y' WHERE rel_type = 'child_of';
UPDATE rel_types SET natural_language = 'X and Y are spouses/partners' WHERE rel_type = 'spouse';
UPDATE rel_types SET natural_language = 'X and Y are siblings' WHERE rel_type = 'sibling_of';
UPDATE rel_types SET natural_language = 'X has a pet that is Y' WHERE rel_type = 'has_pet';
UPDATE rel_types SET natural_language = 'X works for Y' WHERE rel_type = 'works_for';
UPDATE rel_types SET natural_language = 'X manages Y' WHERE rel_type = 'manages';
UPDATE rel_types SET natural_language = 'X is located in Y' WHERE rel_type = 'located_in';
UPDATE rel_types SET natural_language = 'X lives in Y (residence)' WHERE rel_type = 'lives_in';
UPDATE rel_types SET natural_language = 'X lives at Y (address)' WHERE rel_type = 'lives_at';
UPDATE rel_types SET natural_language = 'X was born in Y' WHERE rel_type = 'born_in';
UPDATE rel_types SET natural_language = 'X was born on Y (date)' WHERE rel_type = 'born_on';
UPDATE rel_types SET natural_language = 'X is Y years old' WHERE rel_type = 'age';
UPDATE rel_types SET natural_language = 'X''s occupation is Y' WHERE rel_type = 'occupation';
UPDATE rel_types SET natural_language = 'X is an instance of Y (type)' WHERE rel_type = 'instance_of';
UPDATE rel_types SET natural_language = 'X is a subclass of Y' WHERE rel_type = 'subclass_of';
UPDATE rel_types SET natural_language = 'X is a part of Y' WHERE rel_type = 'part_of';
UPDATE rel_types SET natural_language = 'X is a member of Y' WHERE rel_type = 'member_of';
UPDATE rel_types SET natural_language = 'X''s preferred name is Y' WHERE rel_type = 'pref_name';
UPDATE rel_types SET natural_language = 'X is also known as Y' WHERE rel_type = 'also_known_as';
UPDATE rel_types SET natural_language = 'X and Y are the same entity' WHERE rel_type = 'same_as';
UPDATE rel_types SET natural_language = 'X knows Y' WHERE rel_type = 'knows';
UPDATE rel_types SET natural_language = 'X is a friend of Y' WHERE rel_type = 'friend_of';
UPDATE rel_types SET natural_language = 'X has met Y' WHERE rel_type = 'met';
UPDATE rel_types SET natural_language = 'X likes Y' WHERE rel_type = 'likes';
UPDATE rel_types SET natural_language = 'X dislikes Y' WHERE rel_type = 'dislikes';
UPDATE rel_types SET natural_language = 'X owns Y' WHERE rel_type = 'owns';
UPDATE rel_types SET natural_language = 'X was educated at Y' WHERE rel_type = 'educated_at';
UPDATE rel_types SET natural_language = 'X''s nationality is Y' WHERE rel_type = 'nationality';
UPDATE rel_types SET natural_language = 'X''s gender is Y' WHERE rel_type = 'has_gender';
UPDATE rel_types SET natural_language = 'X created Y' WHERE rel_type = 'created_by';
UPDATE rel_types SET natural_language = 'X is related to Y' WHERE rel_type = 'related_to';
UPDATE rel_types SET natural_language = 'X prefers Y' WHERE rel_type = 'prefers';
UPDATE rel_types SET natural_language = 'X''s height is Y' WHERE rel_type = 'height';
UPDATE rel_types SET natural_language = 'X''s weight is Y' WHERE rel_type = 'weight';
