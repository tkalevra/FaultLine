-- Migration 033: Add growth-model validation to rel_types table
-- Enables learning value distributions from confirmed facts instead of hardcoded rules

ALTER TABLE rel_types
  ADD COLUMN IF NOT EXISTS value_distribution JSONB DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS approved_exceptions JSONB DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS anomaly_threshold FLOAT DEFAULT 2.0;

-- Create index for efficient growth model lookups
CREATE INDEX IF NOT EXISTS idx_rel_types_value_distribution
  ON rel_types USING GIN (value_distribution);

-- Seed value distributions for numeric rel_types based on domain knowledge
UPDATE rel_types
SET value_distribution = jsonb_build_object(
  'type', 'numeric',
  'mean', 42,
  'stddev', 20,
  'min', 0,
  'max', 120,
  'outliers', '[]'::jsonb,
  'confirmed_count', 0,
  'last_updated', NOW()::text
)
WHERE rel_type IN ('age', 'height', 'weight')
  AND value_distribution IS NULL;

-- Seed with appropriate thresholds
UPDATE rel_types
SET anomaly_threshold = 1.5
WHERE rel_type IN ('age', 'height', 'weight')
  AND anomaly_threshold IS NULL;

COMMENT ON COLUMN rel_types.value_distribution IS 'Growth model: learned distribution of observed values for this rel_type. Updated by re_embedder after facts are confirmed.';
COMMENT ON COLUMN rel_types.approved_exceptions IS 'Growth model: exceptions to distribution rules that have been confirmed/approved. Prevents false positives on historical data, fictional entities, etc.';
COMMENT ON COLUMN rel_types.anomaly_threshold IS 'Growth model: standard deviations (Z-score) beyond which a value is flagged as anomaly. Applied as confidence penalty, not rejection.';
