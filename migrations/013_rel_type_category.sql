ALTER TABLE rel_types ADD COLUMN IF NOT EXISTS category TEXT;

UPDATE rel_types SET category = 'location'  WHERE rel_type IN ('lives_at','lives_in','located_in','address','born_in');
UPDATE rel_types SET category = 'family'    WHERE rel_type IN ('parent_of','child_of','spouse','sibling_of');
UPDATE rel_types SET category = 'work'      WHERE rel_type IN ('works_for','occupation','educated_at');
UPDATE rel_types SET category = 'physical'  WHERE rel_type IN ('height','weight','has_gender');
UPDATE rel_types SET category = 'temporal'  WHERE rel_type IN ('born_on','age','anniversary_on','met_on');
UPDATE rel_types SET category = 'pets'      WHERE rel_type IN ('has_pet');
UPDATE rel_types SET category = 'identity'  WHERE rel_type IN ('also_known_as','pref_name','same_as','is_a','instance_of','subclass_of');
