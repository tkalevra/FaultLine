-- Migration 037: Correction Patterns Metadata Table (dprompt-117)
-- Metadata-driven validation framework for correction extraction
-- Allows new correction patterns without code changes

CREATE TABLE IF NOT EXISTS correction_patterns (
  id SERIAL PRIMARY KEY,
  rel_type TEXT NOT NULL UNIQUE,

  -- Human-readable metadata
  label TEXT NOT NULL,
  description TEXT,

  -- Validation constraints
  immutable BOOLEAN DEFAULT FALSE,
  correction_class TEXT,  -- 'scalar_update', 'relationship_update', 'category_removal', etc.
  conflicts_with TEXT[],  -- rel_types that cannot coexist

  -- LLM extraction guidance (stored as JSONB for flexibility)
  semantic_intent TEXT,
  extraction_hints JSONB,

  -- Lifecycle
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_correction_patterns_rel_type ON correction_patterns(rel_type);
CREATE INDEX idx_correction_patterns_immutable ON correction_patterns(immutable) WHERE immutable = TRUE;

-- Seed initial correction patterns (dprompt-117)
INSERT INTO correction_patterns (rel_type, label, description, immutable, correction_class, conflicts_with, semantic_intent, extraction_hints)
VALUES
  ('pref_name', 'Preferred Name Change',
   'User prefers different primary name/alias',
   FALSE, 'scalar_update', NULL,
   'Change entity_aliases.is_preferred flag from old name to new name. Subject MUST be entity, not alias.',
   '{"allowed": ["entity pref_name new_name"], "forbidden": ["alias pref_name new_name"], "note": "Validate subject is real entity via entity_aliases lookup", "cardinality": "one_per_entity"}'::JSONB),

  ('also_known_as', 'Add Alias',
   'Entity has additional name or nickname',
   FALSE, 'scalar_update', NULL,
   'Add new alias without changing pref_name. Multiple values allowed.',
   '{"allowed": ["entity also_known_as alias"], "note": "Do NOT generate pref_name triple if correcting also_known_as", "cardinality": "multiple_allowed"}'::JSONB),

  ('age', 'Age Correction',
   'User corrects entity age value',
   FALSE, 'scalar_update', NULL,
   'Replace old age with new age. Validate: new_age >= 0.',
   '{"allowed": ["entity age new_value"], "validation": "age >= 0, age < 150", "note": "One triple only", "cardinality": "one_per_entity"}'::JSONB),

  ('height', 'Height Correction',
   'User corrects entity height value',
   FALSE, 'scalar_update', NULL,
   'Replace old height with new height. Validate: positive number.',
   '{"allowed": ["entity height new_value"], "validation": "height > 0", "note": "One triple only", "cardinality": "one_per_entity"}'::JSONB),

  ('weight', 'Weight Correction',
   'User corrects entity weight value',
   FALSE, 'scalar_update', NULL,
   'Replace old weight with new weight. Validate: positive number.',
   '{"allowed": ["entity weight new_value"], "validation": "weight > 0", "note": "One triple only", "cardinality": "one_per_entity"}'::JSONB),

  ('has_pet', 'Pet Category Removal',
   'User no longer has pets (or correction: no pets ever)',
   FALSE, 'category_removal', NULL,
   'Remove has_pet relationship facts. Mark as removal semantics in extraction.',
   '{"action": "removal", "entity": "user", "category": "pets", "note": "Generate single removal triple, not per-pet deletions"}'::JSONB),

  -- Immutable patterns (cannot be corrected)
  ('instance_of', 'Entity Type Classification',
   'Entity type is immutable once established',
   TRUE, 'none', NULL,
   'Corrections to instance_of forbidden. User type is foundation.',
   '{"forbidden": "all corrections", "note": "If truly wrong, contact admin"}'::JSONB),

  ('parent_of', 'Parent-Child Relationship',
   'Family relationships are immutable once established',
   TRUE, 'none', NULL,
   'Corrections to parent_of forbidden. Use is_actually_parent_of for disputes.',
   '{"forbidden": "all corrections", "note": "If truly wrong, contact admin"}'::JSONB),

  ('spouse', 'Spouse Relationship',
   'Spousal relationships are immutable once established',
   TRUE, 'none', NULL,
   'Corrections to spouse forbidden.',
   '{"forbidden": "all corrections", "note": "If truly wrong, contact admin"}'::JSONB)

ON CONFLICT (rel_type) DO UPDATE SET
  label = EXCLUDED.label,
  description = EXCLUDED.description,
  immutable = EXCLUDED.immutable,
  correction_class = EXCLUDED.correction_class,
  semantic_intent = EXCLUDED.semantic_intent,
  extraction_hints = EXCLUDED.extraction_hints,
  updated_at = NOW();
