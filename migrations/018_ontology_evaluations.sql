-- 018: ONTOLOGY_EVALUATIONS — async novel rel_type evaluation by re-embedder
--
-- Ingest stores unknown rel_types here instead of auto-approving via LLM.
-- The re-embedder evaluates candidates based on usage patterns, semantic fit,
-- and graph structure — approving, mapping, or rejecting novel types.

CREATE TABLE IF NOT EXISTS ontology_evaluations (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,

    -- What the extraction couldn't match
    candidate_rel_type VARCHAR(128) NOT NULL,
    candidate_subject_type VARCHAR(64),
    candidate_object_type VARCHAR(64),

    -- Evidence
    first_text_snippet TEXT,
    extraction_confidence FLOAT DEFAULT 0.5,
    extraction_method VARCHAR(32) DEFAULT 'llm_extract',

    -- Reference facts
    sample_subject_id TEXT,
    sample_object TEXT,

    -- Tracking
    occurrence_count INT DEFAULT 1,
    last_seen_at TIMESTAMPTZ DEFAULT now(),
    pattern_similarity FLOAT,
    best_fit_rel_type VARCHAR(128),
    best_fit_score FLOAT,

    -- Re-embedder decision
    re_embedder_decision VARCHAR(32),   -- 'approved', 'rejected', 'mapped', NULL
    re_embedder_confidence FLOAT,
    decision_timestamp TIMESTAMPTZ,
    decision_reason TEXT,

    -- Result
    created_rel_type VARCHAR(128),
    promoted_to_facts BOOLEAN DEFAULT FALSE,

    UNIQUE(user_id, candidate_rel_type, sample_subject_id, sample_object)
);

CREATE INDEX IF NOT EXISTS idx_ontology_eval_decision
    ON ontology_evaluations(re_embedder_decision, last_seen_at)
    WHERE re_embedder_decision IS NULL;

CREATE INDEX IF NOT EXISTS idx_ontology_eval_candidate
    ON ontology_evaluations(candidate_rel_type, occurrence_count DESC);
