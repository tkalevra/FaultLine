-- Migration 068: Fix broken natural_language templates missing X subject placeholder
-- BUG-C2: 10 rel_types have natural_language values without the required X placeholder.
-- convert_to_prose() silently returns bare predicates when X is missing.
-- WHERE NOT LIKE '%X%' guard is idempotent — never overwrites a correctly-formed template.

UPDATE rel_types SET natural_language = 'X has affected body part Y'
  WHERE rel_type = 'affected_body_part' AND natural_language NOT LIKE '%X%';

UPDATE rel_types SET natural_language = 'X has allergy to Y'
  WHERE rel_type = 'has_allergy' AND natural_language NOT LIKE '%X%';

UPDATE rel_types SET natural_language = 'X has injury Y'
  WHERE rel_type = 'has_injury' AND natural_language NOT LIKE '%X%';

UPDATE rel_types SET natural_language = 'X has medical condition Y'
  WHERE rel_type = 'has_medical_condition' AND natural_language NOT LIKE '%X%';

UPDATE rel_types SET natural_language = 'X takes medication Y'
  WHERE rel_type = 'has_medication' AND natural_language NOT LIKE '%X%';

UPDATE rel_types SET natural_language = 'X has operating system Y'
  WHERE rel_type = 'has_os' AND natural_language NOT LIKE '%X%';

UPDATE rel_types SET natural_language = 'X has symptom Y'
  WHERE rel_type = 'has_symptom' AND natural_language NOT LIKE '%X%';

UPDATE rel_types SET natural_language = 'X has IP address Y'
  WHERE rel_type = 'ip_address' AND natural_language NOT LIKE '%X%';

UPDATE rel_types SET natural_language = 'X is a Y'
  WHERE rel_type = 'is_a' AND natural_language NOT LIKE '%X%';

UPDATE rel_types SET natural_language = 'X is located at Y'
  WHERE rel_type = 'located_at' AND natural_language NOT LIKE '%X%';
