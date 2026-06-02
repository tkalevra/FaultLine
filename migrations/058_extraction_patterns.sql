-- Migration 058: Extraction Patterns Metadata-Driven Table
-- Purpose: Move hardcoded regex patterns from compound.py to database
-- Bootstrap with patterns extracted from compound.py
-- Re-embedder evaluates patterns asynchronously, updates global_confidence

CREATE TABLE extraction_patterns (
    id SERIAL PRIMARY KEY,
    pattern_regex VARCHAR(1024) NOT NULL,
    rel_type VARCHAR(128) NOT NULL,

    -- Confidence metrics (weak supervision)
    frequency INT DEFAULT 0,
    confirmed_count INT DEFAULT 0,
    rejected_count INT DEFAULT 0,
    correction_count INT DEFAULT 0,
    global_confidence FLOAT DEFAULT 0.5,

    -- Metadata
    description TEXT,
    example_text TEXT,
    category VARCHAR(64),
    source VARCHAR(64),  -- 'hardcoded_legacy', 'llm_discovered'

    -- Lifecycle
    is_active BOOLEAN DEFAULT true,
    archived_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    last_matched_at TIMESTAMP,

    UNIQUE(pattern_regex, rel_type)
);

CREATE INDEX idx_extraction_patterns_active_confidence
  ON extraction_patterns(is_active, global_confidence DESC);
CREATE INDEX idx_extraction_patterns_rel_type
  ON extraction_patterns(rel_type);
CREATE INDEX idx_extraction_patterns_category
  ON extraction_patterns(category);

-- Bootstrap: Identity patterns (self-identification via "my name is", "I am", "call me")
INSERT INTO extraction_patterns (pattern_regex, rel_type, description, example_text, category, source, global_confidence)
VALUES
  ('\bmy\s+name\s+is\s+([A-Z][a-z]+)', 'also_known_as', 'User identity: my name is X', 'my name is Christopher', 'identity', 'hardcoded_legacy', 0.90),
  ('\bi\s+am\s+([A-Z][a-z]+)', 'also_known_as', 'User identity: I am X', 'I am Christopher', 'identity', 'hardcoded_legacy', 0.88),
  ('\bi''m\s+([A-Z][a-z]+)', 'also_known_as', 'User identity: I''m X', 'I''m Christopher', 'identity', 'hardcoded_legacy', 0.87),
  ('\bcall\s+me\s+([A-Z][a-z]+)', 'pref_name', 'User preference: call me X', 'call me Chris', 'identity', 'hardcoded_legacy', 0.92),
  ('\bpeople\s+call\s+me\s+([A-Z][a-z]+)', 'also_known_as', 'User identity: people call me X', 'people call me Chris', 'identity', 'hardcoded_legacy', 0.85),

  -- First-person preference patterns
  ('(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bprefers?\s+to\s+be\s+called\s+([A-Z][a-z]+)', 'pref_name', 'First-person: prefers to be called X', 'I prefer to be called Chris', 'preference', 'hardcoded_legacy', 0.89),
  ('(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bgoes\s+by\s+([A-Z][a-z]+)', 'pref_name', 'First-person: goes by X', 'I go by Chris', 'preference', 'hardcoded_legacy', 0.88),
  ('(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bpreferred\s+name\s+is\s+([A-Z][a-z]+)', 'pref_name', 'First-person: preferred name is X', 'My preferred name is Chris', 'preference', 'hardcoded_legacy', 0.87),
  ('\bplease\s+call\s+me\s+([A-Z][a-z]+)', 'pref_name', 'First-person: please call me X', 'please call me Chris', 'preference', 'hardcoded_legacy', 0.90),
  ('(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bknown\s+as\s+([A-Z][a-z]+)', 'pref_name', 'First-person: known as X', 'I''m known as Chris', 'preference', 'hardcoded_legacy', 0.86),
  ('(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\blike\s+to\s+(?:be|go)\s+(?:by|called)\s+([A-Z][a-z]+)', 'pref_name', 'First-person: like to be called X', 'I like to be called Chris', 'preference', 'hardcoded_legacy', 0.85),
  ('\bi\s+wants?\s+to\s+be\s+called\s+([A-Z][a-z]+)', 'pref_name', 'First-person: I want to be called X', 'I want to be called Chris', 'preference', 'hardcoded_legacy', 0.87),

  -- Third-person preference patterns
  ('([A-Z][a-z]+)(?:(?:,\s*age\s+\d+|,\s*our\s+(?:son|daughter|child)|,\s*a\s+(?:son|daughter|child))\s*,?\s*)?,?\s*who\s+prefers?\s+(?:to\s+be\s+called\s+)?([A-Z][a-z]+)', 'pref_name', 'Third-person: Name, age N, who prefers X', 'Marla, age 10, who prefers emma', 'preference', 'hardcoded_legacy', 0.88),
  ('([A-Z][a-z]+)\s+prefers?\s+to\s+be\s+called\s+([A-Z][a-z]+)', 'pref_name', 'Third-person: Name prefers to be called X', 'Marla prefers to be called emma', 'preference', 'hardcoded_legacy', 0.89),
  ('([A-Z][a-z]+)(?:(?:,\s*age\s+\d+|,\s*our\s+(?:son|daughter|child)|,\s*a\s+(?:son|daughter|child))\s*,?\s*)?,?\s*who\s+goes\s+by\s+([A-Z][a-z]+)', 'pref_name', 'Third-person: Name, age N, who goes by X', 'Diana, age 12, who goes by alice', 'preference', 'hardcoded_legacy', 0.87),
  ('(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )([A-Z][a-z]+)\s+goes\s+by\s+([A-Z][a-z]+)', 'pref_name', 'Third-person: Name goes by X', 'Marla goes by emma', 'preference', 'hardcoded_legacy', 0.86),
  ('([A-Z][a-z]+)\s*,\s*known\s+as\s+([A-Z][a-z]+)', 'pref_name', 'Third-person: Name, known as X', 'Marla, known as emma', 'preference', 'hardcoded_legacy', 0.85),
  ('who\s+prefers?\s+([A-Z][a-z]+)', 'pref_name', 'Third-person: who prefers X (bare)', 'who prefers bob', 'preference', 'hardcoded_legacy', 0.80),
  ('(?:she|he|it)\s+wants?\s+to\s+be\s+called\s+([A-Z][a-z]+)', 'pref_name', 'Third-person pronoun: wants to be called X', 'she wants to be called Thumbelina', 'preference', 'hardcoded_legacy', 0.84),
  ('(?:she|he|it)\s+prefers?\s+to\s+be\s+called\s+([A-Z][a-z]+)', 'pref_name', 'Third-person pronoun: prefers to be called X', 'he prefers to be called bob', 'preference', 'hardcoded_legacy', 0.86),

  -- Marriage patterns
  ('\b(?:i\s+am|i''m)\s+married\s+to\s+([A-Z][a-z]+)', 'spouse', 'User: I am married to X', 'I am married to Marla', 'relationship', 'hardcoded_legacy', 0.91),
  ('\bmarried\s+to\s+([A-Z][a-z]+)', 'spouse', 'Married to X', 'married to Marla', 'relationship', 'hardcoded_legacy', 0.88),
  ('\bmy\s+(wife|husband|spouse|partner)\s+([A-Z][a-z]+)', 'spouse', 'My spouse/wife/husband X', 'my wife Marla', 'relationship', 'hardcoded_legacy', 0.90),
  ('([A-Z][a-z]+)\s+is\s+my\s+(wife|husband|spouse|partner)', 'spouse', 'Name is my spouse/wife/husband', 'Marla is my wife', 'relationship', 'hardcoded_legacy', 0.89),

  -- Child/parent patterns
  ('a\s+(daughter|son|child)\s+([A-Z][a-z]+)', 'parent_of', 'Child pattern: a daughter/son X', 'a daughter Diana', 'family', 'hardcoded_legacy', 0.87),
  ('our\s+(daughter|son|child)\s+(?:is\s+)?(?:named\s+)?([A-Z][a-z]+)', 'parent_of', 'Child pattern: our son/daughter X', 'our son Charlie', 'family', 'hardcoded_legacy', 0.88),
  ('a\s+(daughter|son|child)\s+named\s+([A-Z][a-z]+)', 'parent_of', 'Child pattern: a son/daughter named X', 'a daughter named Diana', 'family', 'hardcoded_legacy', 0.89),
  ('(daughter|son|child)\s+([A-Z][a-z]+)', 'parent_of', 'Child pattern: son/daughter X', 'son Charlie', 'family', 'hardcoded_legacy', 0.82),
  (',\s+(?:and\s+)?(?:a\s+)?(?:daughter|son|child)\s+(?:named\s+)?([A-Z][a-z]+)', 'parent_of', 'Child pattern: comma-separated X', ', a daughter named Diana', 'family', 'hardcoded_legacy', 0.80),

  -- Age patterns
  ('([A-Z][a-z]+)\s*,\s*age\s+(\d+)', 'age', 'Age pattern: Name, age N', 'Diana, age 10', 'attribute', 'hardcoded_legacy', 0.92),
  ('([A-Z][a-z]+)\s+age\s+(\d+)', 'age', 'Age pattern: Name age N', 'Diana age 10', 'attribute', 'hardcoded_legacy', 0.90),
  ('([A-Z][a-z]+)(?:[\s,]+(?:our|a)\s+(?:son|daughter|child))?\s+is\s+(\d+)', 'age', 'Age pattern: Name is N (years old)', 'Charlie is 19', 'attribute', 'hardcoded_legacy', 0.85),
  ('\bi\s+am\s+(\d+)\s*(?:years?\s*old)?', 'age', 'Age pattern: I am N', 'I am 35', 'attribute', 'hardcoded_legacy', 0.93),

  -- Generic technical patterns
  ('the\s+([\w\s]+?)\s+is\s+(?:a\s+)?([\w.]+(?:\s+[\w.]+)*?)(?=\s*(?:,|\.(?:\s+|$)|$|\s+with\s|\s+running\s|\s+and\s+the|\s+and\s+a|\s+the\s+|\s*$))', 'instance_of', 'Generic: the X is Y', 'the system is a Ryzen 7', 'technical', 'hardcoded_legacy', 0.75),
  ('running\s+(.+?)(?=\s*(?:,|\.|$|\s+the\s+))', 'instance_of', 'Generic: running X', 'running Windows 11', 'technical', 'hardcoded_legacy', 0.80),
  ('certificate\s+expires?\s+on\s+(.+?)(?=\s*(?:,|\.|$))', 'expires_on', 'Generic: certificate expires on X', 'certificate expires on November 27th 2026', 'technical', 'hardcoded_legacy', 0.85),
  ('fqdn\s+of\s+(\S+(?:\.\S+)*)', 'fqdn', 'Generic: fqdn of X', 'fqdn of example.com', 'technical', 'hardcoded_legacy', 0.88),
  ('(?:with|has)\s+(\d+\s*(?:GB|MB|TB|GHz|MHz|cores?|RAM|SSD|HDD|storage|memory))\s+of\s+([\w\s]+?)(?=\s*(?:,|\.|$|\s+and|\s+the\s+))', 'has_ram', 'Hardware spec: with/has N GB/MHz/cores of X (requires numeric measurement)', 'with 64GB of ram', 'technical', 'hardcoded_legacy', 0.78),
  (',?\s*a\s+(.+?)(?=\s*(?:,|\.(?:\s+|$)|$|\s+and\s+the|\s+the\s+|\s+running))', 'has_spec', 'Generic: a X (spec)', 'a 2TB M.2 Hard drive', 'technical', 'hardcoded_legacy', 0.72)
ON CONFLICT (pattern_regex, rel_type) DO NOTHING;

-- Create tracking table for pattern match feedback (supports weak supervision)
CREATE TABLE IF NOT EXISTS extraction_pattern_matches (
    id SERIAL PRIMARY KEY,
    pattern_id INT REFERENCES extraction_patterns(id) ON DELETE CASCADE,
    user_id UUID,
    matched_text TEXT,
    matched_at TIMESTAMP DEFAULT NOW(),
    confirmed BOOLEAN,  -- true=user confirmed, false=user rejected, null=unreviewed
    confirmed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_extraction_pattern_matches_pattern_id
  ON extraction_pattern_matches(pattern_id);
CREATE INDEX idx_extraction_pattern_matches_confirmed
  ON extraction_pattern_matches(confirmed);
