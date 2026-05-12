-- Add source column to track provenance of each rel_type.
-- Sources:
--   'wikidata'  — W3C/Wikidata-aligned, immutable reference types
--   'builtin'   — FaultLine domain-specific, stable but not standards-aligned
--   'engine'    — Approved by Qwen at runtime, provisional
--   'user'      — Explicitly asserted by a user or admin

ALTER TABLE rel_types
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'engine'
    CHECK (source IN ('wikidata', 'builtin', 'engine', 'user'));

CREATE INDEX IF NOT EXISTS idx_rel_types_source ON rel_types (source);

-- Seed Wikidata-aligned core types (immutable source of truth)
INSERT INTO rel_types (rel_type, label, wikidata_pid, source, engine_generated, confidence, correction_behavior)
VALUES
  ('instance_of',   'Instance Of',   'P31',   'wikidata', false, 1.0, 'immutable'),
  ('subclass_of',   'Subclass Of',   'P279',  'wikidata', false, 1.0, 'immutable'),
  ('part_of',       'Part Of',       'P361',  'wikidata', false, 1.0, 'supersede'),
  ('created_by',    'Created By',    'P170',  'wikidata', false, 1.0, 'supersede'),
  ('works_for',     'Works For',     'P108',  'wikidata', false, 1.0, 'supersede'),
  ('parent_of',     'Parent Of',     'P40',   'wikidata', false, 1.0, 'immutable'),
  ('child_of',      'Child Of',      'P40',   'wikidata', false, 1.0, 'immutable'),
  ('spouse',        'Spouse',        'P26',   'wikidata', false, 1.0, 'supersede'),
  ('sibling_of',    'Sibling Of',    'P3373', 'wikidata', false, 1.0, 'immutable'),
  ('also_known_as', 'Also Known As', 'P742',  'wikidata', false, 1.0, 'supersede'),
  ('educated_at',   'Educated At',   'P69',   'wikidata', false, 1.0, 'supersede'),
  ('nationality',   'Nationality',   'P27',   'wikidata', false, 1.0, 'supersede'),
  ('occupation',    'Occupation',    'P106',  'wikidata', false, 1.0, 'supersede'),
  ('born_on',       'Born On',       'P569',  'wikidata', false, 1.0, 'immutable'),
  ('born_in',       'Born In',       'P19',   'wikidata', false, 1.0, 'immutable'),
  ('lives_in',      'Lives In',      'P551',  'wikidata', false, 1.0, 'supersede'),
  ('located_in',    'Located In',    'P131',  'wikidata', false, 1.0, 'supersede'),
  ('same_as',       'Same As',       NULL,    'wikidata', false, 1.0, 'immutable'),
  ('pref_name',     'Preferred Name',NULL,    'wikidata', false, 1.0, 'supersede')
ON CONFLICT (rel_type) DO UPDATE SET
    source = 'wikidata',
    wikidata_pid = EXCLUDED.wikidata_pid,
    correction_behavior = EXCLUDED.correction_behavior;

-- Seed FaultLine domain built-ins (stable, not standards-aligned)
INSERT INTO rel_types (rel_type, label, source, engine_generated, confidence, correction_behavior)
VALUES
  ('is_a',       'Is A',        'builtin', false, 1.0, 'supersede'),
  ('related_to', 'Related To',  'builtin', false, 1.0, 'supersede'),
  ('likes',      'Likes',       'builtin', false, 1.0, 'supersede'),
  ('dislikes',   'Dislikes',    'builtin', false, 1.0, 'supersede'),
  ('prefers',    'Prefers',     'builtin', false, 1.0, 'supersede'),
  ('owns',       'Owns',        'builtin', false, 1.0, 'supersede'),
  ('knows',      'Knows',       'builtin', false, 1.0, 'supersede'),
  ('friend_of',  'Friend Of',   'builtin', false, 1.0, 'supersede'),
  ('met',        'Met',         'builtin', false, 1.0, 'supersede'),
  ('has_gender', 'Has Gender',  'builtin', false, 1.0, 'supersede'),
  ('age',        'Age',         'builtin', false, 1.0, 'supersede')
ON CONFLICT (rel_type) DO UPDATE SET
    source = 'builtin';

-- Mark any existing engine-generated rows explicitly
UPDATE rel_types SET source = 'engine' WHERE engine_generated = true AND source = 'engine';
