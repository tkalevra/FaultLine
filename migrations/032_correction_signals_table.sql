-- migrations/032_correction_signals_table.sql
-- dprompt-112: Metadata-driven implicit correction signal detection
-- Solves dBug-041: Corrections-Ignored

CREATE TABLE IF NOT EXISTS correction_signals (
    id SERIAL PRIMARY KEY,
    pattern TEXT NOT NULL UNIQUE,
    pattern_type VARCHAR(50) NOT NULL,
    applicable_rel_types TEXT[] DEFAULT ARRAY[]::TEXT[],
    priority INT DEFAULT 1,
    confidence FLOAT DEFAULT 0.8,
    category VARCHAR(50),
    example_usage TEXT,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_correction_signals_type ON correction_signals(pattern_type);
CREATE INDEX IF NOT EXISTS idx_correction_signals_priority ON correction_signals(priority);

-- Seed initial patterns (zero hardcoding in code)
INSERT INTO correction_signals
(pattern, pattern_type, priority, confidence, category, example_usage, notes)
VALUES
  -- Negation patterns: "X is Y not Z"
  ('is .+ not', 'negation', 1, 0.9, 'family', 'Sampson is 14 not 12', 'Covers age, height, weight corrections'),
  ('are .+ not', 'negation', 1, 0.9, 'family', 'They are X not Y', 'Plural form of negation'),

  -- Reclarification patterns: "Actually/Wait/Sorry + fact"
  ('actually', 'reclarification', 1, 0.85, NULL, 'Actually, I work for Acme', 'Explicit reclarification marker'),
  ('wait,', 'reclarification', 2, 0.75, NULL, 'Wait, my name is Alexander', 'User catches own error mid-conversation'),
  ('wait ', 'reclarification', 2, 0.75, NULL, 'Wait my name is Alexander', 'Wait without comma'),
  ('sorry,', 'reclarification', 2, 0.75, NULL, 'Sorry, we don''t have pets', 'User apologizes while correcting'),
  ('sorry ', 'reclarification', 2, 0.75, NULL, 'Sorry we don''t have pets', 'Sorry without comma'),
  ('i meant', 'reclarification', 1, 0.8, NULL, 'I meant to say Quinn', 'Explicit intention marker'),

  -- Contradiction patterns: "I was wrong about X"
  ('was wrong', 'contradiction', 1, 0.85, NULL, 'I was wrong about that', 'Explicit contradiction'),
  ('mistake', 'contradiction', 2, 0.75, NULL, 'My mistake about the age', 'Informal contradiction'),

  -- Negation with "not" at sentence end
  ('not true', 'negation', 2, 0.8, NULL, 'That''s not true', 'Blanket negation'),
  ('not right', 'negation', 2, 0.8, NULL, 'That''s not right', 'Correcting prior statement')
ON CONFLICT (pattern) DO NOTHING;
