# dBug-046: Ontology Growth Engine Centralization

**STATUS: READY FOR IMPLEMENTATION**

**PRIORITY: Medium** — Architectural cleanup, improves maintainability and reduces code bloat. Not blocking active features but impacts future growth (entity_types, new domains).

## Problem Statement

FaultLine has **three self-growing ontologies** (rel_types, retraction_signals, entity_types) but each implements its own evaluation logic. This creates:

1. **Code duplication:** Frequency tracking, confidence scoring, similarity matching implemented separately
2. **Inconsistency:** Different approval thresholds, different confidence algorithms, different semantics
3. **Maintenance burden:** Adding a new ontology type (e.g., domain_patterns) requires reimplementing the entire evaluation pipeline
4. **Testing complexity:** Must test approval logic in three different places
5. **Control fragmentation:** Approval logic scattered across re_embedder (rel_types), nowhere (retraction_signals), future code (entity_types)

## Current State

### rel_types Growth (re_embedder.py)
```python
# src/re_embedder/embedder.py:evaluate_ontology_candidates()
# - Tracks frequency in ontology_evaluations table
# - Computes avg_confidence
# - Performs cosine similarity matching
# - Approves if frequency >= 3 AND confidence strong
# - Maps to existing rel_type if similarity > 0.85
```

**Location:** `src/re_embedder/embedder.py` lines ~600–700
**Trigger:** Background poll every 10s
**Approval:** Frequency >= 3, cosine_similarity > 0.85 for mapping, else approve
**Table:** `ontology_evaluations` (id, rel_type, sample_object, frequency, avg_confidence, created_at, approved_at)

### retraction_signals Growth (NOT IMPLEMENTED)
```python
# openwebui/faultline_function.py:_detect_retraction_intent()
# - LLM semantic detection returns confidence score
# - No persistence of novel signals
# - No frequency tracking
# - No automatic approval mechanism
# → Falls back to pattern matching (requires manual DB population)
```

**Location:** None (should be in filter + backend)
**Trigger:** Could be on each retraction attempt OR backend batch evaluation
**Table:** `retraction_signals` (signal, signal_category, language, priority)
**Status:** Empty table, no growth logic

### entity_types Growth (PLANNED)
```python
# Future: entity type hierarchies (instance_of, subclass_of)
# - Novel types discovered via _hierarchy_expand()
# - Need approval before using in validation
# - Same pattern as rel_types
```

**Location:** Not yet implemented
**Trigger:** Ingest pipeline + background evaluation
**Table:** `entity_types` + `entity_hierarchies` (planned)

## Root Cause

Each ontology evolved independently without a common evaluation framework. Now that we're adding retraction_signals growth and planning entity_types, the pattern duplication is blocking progress.

## Solution aliceign

### New Module: `src/api/ontology_growth.py`

Unified evaluation engine for all self-growing ontologies.

#### Core Class: `OntologyGrowthEngine`

```python
class OntologyGrowthEngine:
    """
    Unified evaluation framework for self-growing ontologies (rel_types, retraction_signals, entity_types, etc.).
    
    Provialice:
    - Frequency tracking (table-agnostic)
    - Confidence scoring (unified algorithm)
    - Semantic similarity matching (unified cosine)
    - Approval/mapping/rejection logic (table-agnostic)
    - Logging and metrics
    """
    
    async def evaluate_candidate(self,
        table: str,                      # "rel_types" | "retraction_signals" | "entity_types"
        candidate: dict,                 # {pattern, frequency, avg_confidence, sample_contexts}
        approval_threshold: float = 0.8,
        frequency_threshold: int = 3,
        similarity_threshold: float = 0.85,
        db_conn: Any = None
    ) -> dict:
        """
        Evaluate a novel ontology candidate for approval/mapping/rejection.
        
        Returns: {
            'action': 'approve' | 'map' | 'reject' | 'hold',
            'target': str (for 'map': mapped rel_type; for 'approve': new rel_type),
            'confidence': float,
            'reason': str,
            'metadata': dict
        }
        
        Flow:
        1. Validate candidate (frequency >= threshold, confidence >= threshold)
        2. Search for similar existing patterns (cosine similarity)
        3. If high similarity match found → route to 'map' action
        4. Else → route to 'approve' action (add as new pattern)
        5. Return action + metadata for caller to persist
        """
```

#### Key Methods

**1. Frequency & Confidence Validation**
```python
async def _validate_candidate(self, 
    candidate: dict,
    frequency_threshold: int,
    approval_threshold: float
) -> tuple[bool, str]:
    """
    Check if candidate meets minimum thresholds.
    Returns: (is_valid: bool, reason: str)
    """
```

**2. Semantic Similarity Matching**
```python
async def find_similar_patterns(self,
    table: str,
    candidate: dict,
    similarity_threshold: float = 0.85,
    limit: int = 5
) -> list[dict]:
    """
    Find existing patterns similar to candidate using cosine similarity.
    
    Uses nomic-embed-text for embeddings (same as Qdrant).
    Works for any table (rel_types, retraction_signals, entity_types).
    
    Returns: [
        {'pattern': str, 'similarity': float, 'embedding': list[float], 'metadata': dict},
        ...
    ]
    """
```

**3. Approval Logic (Table-Specific)**
```python
async def route_approval(self,
    table: str,
    candidate: dict,
    similar_patterns: list[dict]
) -> dict:
    """
    Route candidate to table-specific approval logic.
    Delegates to _approve_rel_type(), _approve_retraction_signal(), etc.
    
    Returns action dict.
    """

async def _approve_rel_type(self, 
    candidate: dict,
    similar: list[dict]
) -> dict:
    """rel_types specific: validate head_types/tail_types consistency, etc."""

async def _approve_retraction_signal(self,
    candidate: dict,
    similar: list[dict]
) -> dict:
    """retraction_signals specific: validate signal pattern, false-positive rate, etc."""

async def _approve_entity_type(self,
    candidate: dict,
    similar: list[dict]
) -> dict:
    """entity_types specific: validate hierarchy consistency, taxonomy membership, etc."""
```

**4. Batch Evaluation (Re-embedder Integration)**
```python
async def evaluate_all_candidates(self,
    table: str,
    db_conn: Any,
    batch_size: int = 100
) -> dict:
    """
    Batch evaluate all pending candidates in a table.
    Called by re_embedder loop for rel_types, retraction_signals, entity_types.
    
    Returns: {
        'evaluated': int,
        'approved': int,
        'mapped': int,
        'rejected': int,
        'held': int,
        'results': [action dicts]
    }
    """
```

#### Integration Points

**1. re_embedder.py**
```python
# Replace current evaluate_ontology_candidates() with:
async def reconcile_ontologies(self):
    engine = OntologyGrowthEngine(db_conn=self.db)
    
    results = await engine.evaluate_all_candidates(
        table="rel_types",
        db_conn=self.db,
        batch_size=100
    )
    
    for result in results['results']:
        if result['action'] == 'approve':
            # Insert into rel_types
        elif result['action'] == 'map':
            # Update candidate to point to mapped rel_type
        # etc.
```

**2. faultline_function.py (Filter)**
```python
# In _detect_retraction_intent():
async def _evaluate_novel_signal(self, signal: str, confidence: float):
    engine = OntologyGrowthEngine()
    
    candidate = {
        'pattern': signal,
        'frequency': 1,  # increment on repeated detections
        'avg_confidence': confidence,
        'sample_contexts': [text]
    }
    
    result = await engine.evaluate_candidate(
        table="retraction_signals",
        candidate=candidate
    )
    
    if result['action'] == 'approve':
        # Insert into retraction_signals
```

**3. src/api/main.py (Ingest)**
```python
# In WGM gate or post-ingest:
async def evaluate_novel_entity_type(self, entity_type: str, confidence: float):
    engine = OntologyGrowthEngine()
    
    candidate = {
        'pattern': entity_type,
        'frequency': 1,
        'avg_confidence': confidence,
        'sample_contexts': [...]
    }
    
    result = await engine.evaluate_candidate(
        table="entity_types",
        candidate=candidate
    )
```

## Implementation Details

### Database Schema Updates

**1. Unified evaluation table (for all ontologies)**
```sql
CREATE TABLE ontology_growth_evaluations (
    id BIGSERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,              -- "rel_types" | "retraction_signals" | "entity_types"
    pattern TEXT NOT NULL,                 -- rel_type | signal | entity_type
    frequency INT DEFAULT 1,
    avg_confidence FLOAT,
    sample_contexts TEXT[] DEFAULT '{}',   -- JSON array of sample texts
    embedding VECTOR(768),                 -- nomic-embed-text embedding
    embedding_provider TEXT DEFAULT 'nomic-embed-text-v1.5',
    created_at TIMESTAMP DEFAULT now(),
    evaluated_at TIMESTAMP,
    evaluation_result JSONB,                -- {action, target, confidence, reason}
    
    UNIQUE(table_name, pattern),
    INDEX(table_name, created_at),
    INDEX(table_name, evaluated_at)
);
```

**2. retraction_signals table (add frequency tracking)**
```sql
ALTER TABLE retraction_signals ADD COLUMN (
    frequency INT DEFAULT 1,
    avg_confidence FLOAT,
    last_seen_at TIMESTAMP,
    evaluation_status TEXT DEFAULT 'manual'  -- "manual" | "auto_approved" | "auto_mapped"
);
```

### Embedding Strategy

Use same embedding model as Qdrant (nomic-embed-text-v1.5):
- Standardized across all similarity matching
- Allows cross-ontology semantic comparison (future)
- Stored in `ontology_growth_evaluations.embedding` for reuse

### Confidence Scoring Algorithm

Unified confidence score based on:
```
base_confidence = (frequency / frequency_threshold) * 0.5 + extracted_confidence * 0.5

Where:
- frequency: number of times pattern detected
- extracted_confidence: confidence from LLM/extractor
- frequency_threshold: 3 (default)

Result: float [0.0, 1.0]

Approval: base_confidence >= approval_threshold (0.8 default)
```

### Logging & Metrics

```python
# Structured logging for all evaluations
log.info(f"ontology_growth.candidate_evaluated",
    table=table,
    pattern=pattern,
    frequency=frequency,
    confidence=confidence,
    action=action,
    similarity_match=similar_pattern if action=='map' else None
)

# Metrics (for monitoring growth rate)
engine.metrics = {
    'rel_types': {'evaluated': 42, 'approved': 5, 'mapped': 2, 'rejected': 0},
    'retraction_signals': {'evaluated': 12, 'approved': 3, 'mapped': 1, 'rejected': 0},
    'entity_types': {'evaluated': 0, 'approved': 0, 'mapped': 0, 'rejected': 0}
}
```

## Test Plan

### Unit Tests

1. **Frequency/confidence validation**
   - Test with frequency < threshold → hold
   - Test with confidence < threshold → hold
   - Test with both >= threshold → proceed

2. **Similarity matching**
   - Exact match (similarity = 1.0) → map
   - High similarity (similarity > 0.85) → map
   - Low similarity (similarity < 0.85) → approve

3. **Table-specific approval logic**
   - rel_types: validate head_types/tail_types
   - retraction_signals: validate signal pattern (no regex)
   - entity_types: validate hierarchy consistency

4. **Edge cases**
   - Null/empty candidate
   - Duplicate patterns detected simultaneously
   - No similar patterns found
   - All candidates held (waiting for frequency)

### Integration Tests

1. **re_embedder to rel_types growth**
   - Novel rel_type detected → frequency tracked → approved/mapped

2. **Filter to retraction_signals growth**
   - Novel signal detected → frequency tracked → approved/mapped

3. **Ingest to entity_types growth** (future)
   - Novel entity_type detected → frequency tracked → approved/mapped

### Validation Checks

After implementation, verify:
- ✓ Novel rel_types still approved at frequency >= 3
- ✓ Retraction signals now auto-populate from LLM detections
- ✓ Entity types can be added to rel_types via same engine
- ✓ Code in re_embedder reduced by ~100 lines
- ✓ No code added to filter (uses backend engine only)
- ✓ Confidence scoring consistent across all tables

## Migration Path

### Phase 1: Extract & Centralize (non-breaking)
1. Create `src/api/ontology_growth.py` with unified engine
2. Update re_embedder to use engine (replace `evaluate_ontology_candidates()`)
3. Test rel_types growth still works
4. **No changes to filter or retraction_signals yet**

### Phase 2: Retraction Signals Growth (new feature)
1. Populate `retraction_signals` table from migrations
2. Add frequency tracking to retraction_signals table
3. Update filter to call engine for novel signals
4. Test signal auto-growth from LLM detections

### Phase 3: Entity Types Growth (future)
1. Add entity_types support to engine
2. Implement entity type evaluation in ingest
3. Enable automatic taxonomy building

## Related Issues

- **dBug-045:** Embedding URL (fixed) — engine uses same embedding endpoint
- **dBug-016:** dBug-016 chat_id (fixed) — engine can be called from filter safely now
- **dprompt-104:** Re-embedder scalar inference (related) — could be moved to engine
- **Retraction signals:** Currently manual, would be auto-growing with this fix

## Success Criteria

- ✅ Single evaluation framework for all ontologies
- ✅ rel_types growth unchanged (backward compatible)
- ✅ Retraction signals auto-grow from LLM detections
- ✅ re_embedder code reduced by ≥20%
- ✅ New ontologies need only 10–20 lines of integration code
- ✅ All existing tests pass
- ✅ New engine tests cover all approval paths

## Effort Estimate

- **Implementation:** 2–3 hours (extract, test, integrate with re_embedder)
- **Retraction signals integration:** 1 hour (filter + test)
- **Testing:** 1 hour (unit + integration)
- **Total:** ~4–5 hours for full implementation including retraction signals

## Notes for Implementation

1. **Embedding cost:** Similarity matching will call `/api/embeddings` for each candidate. Cache embeddings in `ontology_growth_evaluations` to avoid redundant calls.

2. **Database connection:** Engine should accept db_conn as parameter (for use in re_embedder loop) OR create its own (for filter/ingest calls). Provide both patterns.

3. **Async requirement:** Engine must be async-compatible (filter is async, re_embedder is sync). Provide sync wrapper if needed.

4. **Logging:** Use structured logging consistent with existing modules (loguru/structlog).

5. **Default thresholds:** Make frequency_threshold, approval_threshold, similarity_threshold configurable via env vars or module-level constants.
