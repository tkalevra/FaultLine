-- Add storage_target column to rel_types table (routing destination)
ALTER TABLE rel_types 
ADD COLUMN IF NOT EXISTS storage_target TEXT DEFAULT 'facts';

-- Add constraint for storage_target
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'check_storage_target'
    ) THEN
        ALTER TABLE rel_types 
        ADD CONSTRAINT check_storage_target 
        CHECK (storage_target IN ('facts', 'events', 'staged_only'));
    END IF;
END $$;

-- Add fact_class column to rel_types table (A/B/C classification)
ALTER TABLE rel_types 
ADD COLUMN IF NOT EXISTS fact_class TEXT DEFAULT 'C';

-- Add constraint for fact_class
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'check_fact_class'
    ) THEN
        ALTER TABLE rel_types 
        ADD CONSTRAINT check_fact_class 
        CHECK (fact_class IN ('A', 'B', 'C'));
    END IF;
END $$;

-- Seed storage_target routing metadata

-- Class A identity facts → facts table (write-through)
UPDATE rel_types SET storage_target = 'facts' 
WHERE rel_type IN (
    'pref_name', 'also_known_as', 'same_as',
    'parent_of', 'child_of', 'spouse', 'sibling_of',
    'born_on', 'born_in', 'has_gender', 'nationality',
    'instance_of', 'subclass_of', 'age', 'height', 'weight'
);

-- Temporal events → events table (calendar recurrence)
UPDATE rel_types SET storage_target = 'events'
WHERE rel_type IN (
    'anniversary_on', 'met_on', 'married_on', 'appointment_on'
);

-- Class B behavioral facts → facts table (promoted from staged)
UPDATE rel_types SET storage_target = 'facts'
WHERE rel_type IN (
    'lives_at', 'lives_in', 'works_for', 'occupation', 'educated_at',
    'owns', 'likes', 'dislikes', 'prefers', 'friend_of', 'knows', 'met',
    'located_in', 'related_to', 'has_pet', 'part_of', 'created_by', 'member_of'
);

-- All unmatched rel_types default to 'facts' (via DEFAULT)
-- Covers novel/engine-generated and system types

-- Seed fact_class classification metadata

-- Class A: Identity/structural facts — write-through to facts table immediately
UPDATE rel_types SET fact_class = 'A'
WHERE rel_type IN (
    'pref_name', 'also_known_as', 'same_as',
    'parent_of', 'child_of', 'spouse', 'sibling_of',
    'born_on', 'born_in', 'has_gender', 'nationality',
    'instance_of', 'subclass_of', 'age', 'height', 'weight'
);

-- Class B: Behavioral/contextual facts — staged, promote on confirmation
UPDATE rel_types SET fact_class = 'B'
WHERE rel_type IN (
    'lives_at', 'lives_in', 'works_for', 'occupation', 'educated_at',
    'owns', 'likes', 'dislikes', 'prefers', 'friend_of', 'knows', 'met',
    'located_in', 'related_to', 'has_pet', 'part_of', 'created_by'
);

-- Class C is the DEFAULT for all others (temporal events, novel types, system types)
-- anniversary_on, met_on, married_on, appointment_on → C
-- member_of, is_a, has_ip, has_os, hostname, fqdn, ip_address, located_at → C
