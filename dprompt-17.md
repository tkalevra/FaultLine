# dprompt-17 — Self-Building Ontology: From Ingest Extraction to Re-Embedder Validation

## The Problem with Current Novelty Approval

Current flow: Ingest LLM extracts rel_type → WGM gate checks novelty → LLM approves on-the-fly (confidence ≥ 0.7) → fact stored

**Issues:**
1. Ingest is bottleneck for novelty decisions (slow, limited context)
2. LLM decides if rel_type is valid (gatekeeper role)
3. Novel types approved without seeing full usage patterns
4. Brittle: relies on single extraction moment

## Core Principle (User-Authoritative)

**The user is always right.** If they say something, it's true. Never reject facts.

But confidence reflects **how well facts fit the ontology**:
- User-stated fact: confidence 1.0
- Inferred from graph: confidence 0.7-0.8
- Novel rel_type (unmatched): confidence 0.4-0.6
- RAG-derived: confidence 0.3-0.5

Confidence is **adjusted during re-embed** based on:
- Pattern frequency (how many times is this used?)
- Semantic similarity to existing ontology
- Graph references and structural fit
- User reinforcement (confirmation count)

## New Architecture

### Ingest Layer (Fast, Deterministic)

```
Text extraction:
1. LLM extracts entities + relationships
2. For each rel_type: Is it in rel_types table?
   - YES: Store as Class A/B with confidence 0.7-1.0
   - NO: Store as Class C (RAG only) with confidence 0.4-0.6
3. For NO matches: Add to ONTOLOGY_EVALUATIONS table
```

**Key:** Ingest only uses **known ontology** (Wikipedia + previously approved novel types). No novelty approval at ingest time.

### ONTOLOGY_EVALUATIONS Table

```sql
CREATE TABLE ontology_evaluations (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL,
  
  -- What the extraction couldn't match
  candidate_rel_type VARCHAR(128) NOT NULL,      -- "custom_alias"
  candidate_subject_type VARCHAR(64),             -- "Person"
  candidate_object_type VARCHAR(64),              -- "Person"
  
  -- Evidence
  first_text_snippet TEXT,                        -- "My ride is a Tesla"
  extraction_confidence FLOAT DEFAULT 0.5,        -- LLM's confidence in extraction
  extraction_method VARCHAR(32),                  -- "llm_extract"
  
  -- Reference facts
  sample_subject_id UUID,
  sample_object VARCHAR(256),
  
  -- Tracking
  occurrence_count INT DEFAULT 1,                 -- How many times pattern appeared
  last_seen_at TIMESTAMP DEFAULT now(),
  pattern_similarity FLOAT,                       -- Cosine sim to existing rel_types
  best_fit_rel_type VARCHAR(128),                 -- "owns" (if mapped)
  best_fit_score FLOAT,                           -- 0.82
  
  -- Re-embedder decision
  re_embedder_decision VARCHAR(32),               -- "approved", "rejected", "mapped", NULL
  re_embedder_confidence FLOAT,                   -- 0.91
  decision_timestamp TIMESTAMP,
  decision_reason TEXT,                           -- "Maps to 'owns' pattern"
  
  -- Result
  created_rel_type VARCHAR(128),                  -- New rel_type if approved
  promoted_to_facts BOOLEAN DEFAULT FALSE,
  
  UNIQUE(user_id, candidate_rel_type, sample_subject_id, sample_object)
);
```

### Re-Embedder Layer (Intelligent, Contextual)

Runs periodically (every poll cycle or dedicated schedule):

1. **Scan ONTOLOGY_EVALUATIONS** for `re_embedder_decision IS NULL`
2. **For each candidate_rel_type:**
   - Count total occurrences across all users
   - Compute embedding similarity to existing rel_types
   - Analyze graph structure: How do subjects/objects cluster?
   - Decision logic:
     ```
     IF best_fit_score > 0.85:
       decision = "mapped" → rewrite facts to best_fit_rel_type
     ELSE IF occurrence_count >= 3 AND pattern_consistent:
       decision = "approved" → INSERT INTO rel_types
     ELSE:
       decision = "rejected" → leave in Class C, expire after 30 days
     ```

3. **Update staged facts** using candidate rel_type:
   - If mapped: `rel_type := best_fit_rel_type`
   - Adjust confidence: `extraction_confidence * 0.7 + re_embedder_confidence * 0.3`
   - If confidence > 0.4: Move to Class B (staged, ready for promotion)
   - If confidence < 0.4: Stay Class C (RAG only)

4. **Promote staged facts:**
   - Class B with confirmed_count ≥ 3 → Move to facts
   - User reinforces Class C → confidence bumps → promotes

### Query/Recall Path

**For any rel_type (matched or novel):**
- Facts in `facts` table returned with full confidence
- Facts in `staged_facts` returned with Class B/C marker
- Facts in RAG (Class C) returned with confidence score
- User can recall any fact, even if still in RAG

**User recall strengthens facts:**
- "Yes, that's right" → confidence +0.2 (capped at 1.0)
- "No, actually..." → New fact created with confidence 1.0

## Example Flow

**User says:** "My ride is a Tesla"

**Ingest:**
- Extract: subject="user", rel_type="has_vehicle", object="tesla" ✓ (in ontology)
- Confidence: 0.8 (user-stated, inferred type)
- Stored as Class A fact immediately

**User says:** "My whip is electric"

**Ingest:**
- Extract: subject="user", rel_type="whip_ownership"(?), object="electric"
- No match in ontology → Confidence 0.5
- Stored as Class C (RAG only)
- Row added to ONTOLOGY_EVALUATIONS: candidate_rel_type="whip_ownership"

**Re-embedder (next cycle):**
- occurrence_count=1 (only one user, one time)
- Analyze: "whip" in context of "vehicle" → similarity to "has_vehicle" = 0.79
- Pattern not strong enough for approval (need 3+ occurrences)
- Decision: "rejected"
- Fact stays in Class C for 30 days, then expires from RAG

**User says again:** "My whip is fast"

**Ingest:**
- Same: rel_type="whip_ownership", Class C
- occurrence_count increases to 2

**User queries:** "Tell me about my transportation"

**Query:**
- `/query` returns: `has_vehicle(user, tesla)` confidence=0.8 + RAG results including `whip_ownership` facts with confidence=0.5
- User sees both, system shows confidence scores
- If user engages: "Yeah, my whip is my main ride" → confidence bumps, system learns the pattern

**Re-embedder (after 5+ occurrences):**
- pattern_consistency now high
- Semantic similarity to "has_vehicle" ≈ 0.83
- Decision: "mapped" to "has_vehicle"
- All staged `whip_ownership` facts rewritten to `has_vehicle`
- Promoted to facts

## Result

**The system self-builds ontology by:**
1. Accepting everything the user says (no rejection)
2. Storing unmatched patterns as low-confidence Class C facts (RAG-visible)
3. Letting re-embedder evaluate patterns with full context and time
4. Promoting novel rel_types when usage patterns validate them
5. Adjusting confidence based on semantic fit and user reinforcement

**No brittleness because:**
- Ingest uses known ontology (fast, deterministic)
- Novel types evaluated intelligently (async, contextual)
- User never told "that's not valid" (always stored, confidence reflects validity)
- Ontology evolves naturally from real usage

## Implementation Checklist

- [ ] Create ONTOLOGY_EVALUATIONS table (schema above)
- [ ] Modify `/ingest` to add unmatched rel_types to evaluation table
- [ ] Modify re-embedder to evaluate and decide on novel rel_types
- [ ] Implement confidence adjustment logic (extraction + re-embed + usage weights)
- [ ] Update Class C promotion logic (staged facts with approved rel_types)
- [ ] Wire ONTOLOGY_EVALUATIONS into re-embedder's main decision loop
- [ ] Test end-to-end: user says unmatched rel_type → evaluated → promoted
