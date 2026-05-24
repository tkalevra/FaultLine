# LLMOutputValidator Integration Guide

**Reference:** dBug-046 generalized — Unified LLM→Postgres Control Architecture

## Overview

The `LLMOutputValidator` centralizes validation and storage routing for **all LLM outputs** in FaultLine. It replaces scattered validation logic with a unified control framework.

**Before:** Each module (WGM gate, filter, re-embedder) implements its own validation/confidence/storage logic → code duplication, inconsistency, hard to maintain.

**After:** All modules use `LLMOutputValidator` → single source of truth for what gets stored where, unified confidence scoring, global metrics.

## Architecture

```
LLM Output → LLMOutputValidator → Storage Decision → Database/Qdrant
                    ↓
            - Validate (frequency, confidence)
            - Score (unified algorithm)
            - Find similar patterns
            - Route (direct|staged|rejected|hold)
            - Update metrics
```

## Output Types & Storage Routing

| Output Type | Storage Table | Confidence Path | Routing Logic |
|---|---|---|---|
| `fact` | `facts` / `staged_facts` | 0.9+ → direct, 0.6+ → staged, <0.6 → rejected | WGM gate decides class (A/B/C) |
| `retraction_signal` | `retraction_signals` | 0.85+ → direct, 0.6+ → staged | Filter detection → immediate store |
| `entity_type` | `entities` | Variable | Ingest pipeline → hierarchy validation |
| `rel_type` | `rel_types` | Frequency 3+ → approve/map | Re-embedder batch evaluation |
| `context` | `qdrant` | 0.4+ → staged | `/store_context` → Qdrant direct |

## Integration Points

### 1. **WGM Gate** (src/wgm/gate.py)

**Current:** Inline fact validation (`validate_edge()`)

**Integration:**
```python
# In WGMValidationGate.__init__()
from src.api.llm_output_validator import LLMOutputValidator

self.validator = LLMOutputValidator(db_conn=db_conn, llm_endpoint=llm_endpoint)

# In validate_edge()
# After basic validation, call:
validation = await self.validator.validate_output(
    output_type='fact',
    payload={
        'subject_id': subject_id,
        'object_id': object_id,
        'rel_type': rel_type,
        'subject_type': subject_type,
        'object_type': object_type,
    },
    source='llm',  # or 'user' if is_correction=True
    llm_confidence=edge_kwargs.get('confidence', 0.8),
    frequency=1
)

# Use validation.storage_decision to route (direct → staged)
# Keep existing type constraint checking
# Keep existing semantic supersession logic
```

**Changes:** Add validator initialization, call validate_output() for routing decision, use returned storage decision.

**No breaking changes:** Type constraint logic, supersession logic, return values all unchanged.

### 2. **Filter - Retraction Detection** (openwebui/faultline_function.py)

**Current:** `_detect_retraction_intent()` returns boolean

**Integration:**
```python
# In _detect_retraction_intent()
from src.api.llm_output_validator import LLMOutputValidator

validator = LLMOutputValidator(llm_endpoint=openwebui_url)

# After LLM detects signal:
validation = await validator.validate_output(
    output_type='retraction_signal',
    payload={'signal': signal_text, 'category': 'user_negation'},
    source='llm',
    llm_confidence=llm_confidence,
    frequency=1
)

if validation.storage_decision == 'direct':
    # Store signal immediately
    db.insert('retraction_signals', {...})
elif validation.storage_decision == 'staged':
    # Store for learning (frequency tracking)
    db.insert('staged_signals', {...})
```

**Changes:** Call validator instead of inline detection, route based on decision.

**Note:** No configuration needed at filter level — filter can be async-friendly (validator has sync helpers for non-async contexts).

### 3. **Re-embedder - Ontology Evaluation** (src/re_embedder/embedder.py)

**Current:** `evaluate_ontology_candidates()` with embedded approval/mapping/rejection logic

**Integration:**
```python
# Replace evaluate_ontology_candidates() with:
from src.api.llm_output_validator import LLMOutputValidator

validator = LLMOutputValidator(db_conn=db, llm_endpoint=qwen_api_url)

# In main loop:
results = await validator.evaluate_batch(
    output_type='rel_type',
    table='ontology_evaluations',
    batch_size=100
)

# results contains: {'evaluated': N, 'approved': M, 'mapped': K, 'rejected': J, 'results': [...]}
# Process results and persist decisions to database

for result in results['results']:
    if result['decision'] == 'approved':
        # INSERT into rel_types
        db.insert('rel_types', {rel_type: result['pattern'], ...})
    elif result['decision'] == 'mapped':
        # UPDATE ontology_evaluations to point to mapped type
        db.update('ontology_evaluations', {rel_type: result['target']})
    elif result['decision'] == 'rejected':
        # Archive candidate in ontology_evaluations
        db.update('ontology_evaluations', {archived_at: now()})
```

**Expected code reduction:** ~150+ lines (removes _cosine_similarity, embedded confidence scoring, similarity matching logic).

**Changes:** Replace approval logic with validator.evaluate_batch(), keep database persistence logic.

### 4. **Ingest Pipeline** (src/api/main.py - Future)

**Current:** Inline entity type inference in fact classification

**Integration (Future Phase):**
```python
# In classify_fact_type() or similar:
validation = await validator.validate_output(
    output_type='entity_type',
    payload={'entity_type': inferred_type, 'source_entity': entity_id},
    source='llm',
    llm_confidence=gliner_confidence,
    frequency=1
)

# Route based on storage_decision
if validation.storage_decision == 'direct':
    db.update('entities', {entity_type: inferred_type})
elif validation.storage_decision == 'staged':
    db.insert('staged_entities', {...})  # Await confirmation
```

**Note:** Not required for Phase 1. Integrate after WGM gate + filter + re-embedder are complete.

## Unified Confidence Scoring

The validator computes confidence consistently across all outputs:

```python
base_confidence = (frequency / frequency_threshold) * 0.5 + llm_confidence * 0.5

Where:
- frequency: occurrence count (1 for single detections)
- llm_confidence: LLM's own confidence [0.0, 1.0]
- frequency_threshold: minimum (usually 1-3)

For user outputs (source='user'): confidence = 1.0 (always)
For engine outputs (source='engine'): confidence = llm_confidence * 0.6 (lower weight)
```

**Benefits:**
- Facts, signals, patterns all scored the same way
- High-confidence LLM outputs treated similarly across modules
- User corrections always authoritative (1.0)

## Semantic Similarity Matching

For pattern candidates (rel_types, retraction_signals, entity_types), validator finds similar existing patterns:

```python
similar = await validator.find_similar_patterns(
    pattern='works_with',  # candidate
    table='rel_types',     # where to search
    similarity_threshold=0.85
)

# Returns [{'pattern': 'works_for', 'similarity': 0.92, 'metadata': {...}}, ...]
```

Uses **nomic-embed-text-v1.5** (same as Qdrant) for consistency.

## Storage Decisions

The validator returns one of four decisions:

1. **`'direct'`** — Write immediately to primary table (facts, rel_types, retraction_signals)
   - Used for: user outputs, high-confidence LLM outputs, frequent patterns
   - Triggers: source='user' OR confidence >= approval_threshold

2. **`'staged'`** — Write to staging table for learning/confirmation
   - Used for: medium-confidence outputs, novel patterns awaiting evaluation
   - Triggers: confidence >= 0.6 but < approval_threshold OR novel pattern awaiting approval
   - Lifecycle: Awaits confirmation (frequency tracking) or evaluation (re-embedder)

3. **`'rejected'`** — Discard (log and drop)
   - Used for: low-confidence outputs, invalid structure
   - Triggers: confidence < minimum OR validation failures

4. **`'hold'`** — Retain for future evaluation
   - Used for: patterns not meeting frequency threshold yet
   - Triggers: frequency < threshold (waiting for more occurrences)

## Global Metrics

The validator tracks metrics across **all output types**:

```python
validator.metrics = {
    'fact': {'validated': 1050, 'direct': 900, 'staged': 120, 'rejected': 30},
    'retraction_signal': {'validated': 45, 'direct': 40, 'staged': 4, 'rejected': 1},
    'entity_type': {'validated': 20, 'direct': 15, 'staged': 5, 'rejected': 0},
    'rel_type': {'validated': 18, 'approved': 3, 'mapped': 2, 'rejected': 1, 'held': 12},
    'context': {'validated': 5000, 'direct': 4800, 'rejected': 200},
}
```

**Benefits:**
- Monitor what LLM is creating across entire system
- Detect anomalies (too many rejected facts, stalled pattern approvals)
- Per-type metrics for debugging

## Integration Checklist

### Phase 1: Core Validator (Complete)
- [x] Create `src/api/llm_output_validator.py`
- [x] Implement `validate_output()` method
- [x] Implement `find_similar_patterns()` (semantic matching)
- [x] Implement `evaluate_batch()` (batch evaluation)
- [x] Implement metrics tracking

### Phase 2a: WGM Gate Integration
- [ ] Add validator initialization to `WGMValidationGate.__init__()`
- [ ] Call `validate_output()` in `validate_edge()`
- [ ] Use returned storage decision for routing
- [ ] Test fact validation still works
- [ ] Verify confidence scoring matches expectations

### Phase 2b: Filter Integration
- [ ] Add validator to retraction detection
- [ ] Call `validate_output()` for signal detection
- [ ] Store signals based on decision
- [ ] Enable auto-growth of retraction_signals table

### Phase 2c: Re-embedder Integration
- [ ] Replace `evaluate_ontology_candidates()` with `evaluate_batch()`
- [ ] Remove embedded approval logic
- [ ] Test rel_types growth still works
- [ ] Verify code reduction (~150 lines)

### Phase 3: Testing & Validation
- [ ] Unit tests for confidence scoring
- [ ] Unit tests for storage routing
- [ ] Unit tests for similarity matching
- [ ] Integration tests for each module
- [ ] Full pipeline test via curl
- [ ] Metrics validation

## Backward Compatibility

**All changes are backward compatible:**

- Existing WGM gate behavior unchanged (just delegated to validator)
- Existing re-embedder behavior unchanged (same approval logic, now unified)
- Existing filter behavior unchanged (same detection, now validated)
- Database schemas unchanged
- Return values unchanged

## Configuration

**Environment variables (optional overrides):**
```bash
LLM_VALIDATOR_APPROVAL_THRESHOLD=0.8      # Default confidence for approval
LLM_VALIDATOR_FREQUENCY_THRESHOLD=3       # Default frequency for approval
LLM_VALIDATOR_SIMILARITY_THRESHOLD=0.85   # Default for pattern matching
```

**In code (per-instance configuration):**
```python
validator = LLMOutputValidator(
    db_conn=db,
    llm_endpoint='http://localhost:8080',
    thresholds={
        'fact': {'frequency': 1, 'approval': 0.8},
        'rel_type': {'frequency': 3, 'approval': 0.8},
        # ... per-type overrides
    }
)
```

## Testing Strategy

### Unit Tests (tests/test_llm_output_validator.py)
```python
# Test confidence scoring
assert validator._compute_confidence(3, 0.9, frequency_threshold=3) == 0.95  # (1.0*0.5 + 0.9*0.5)
assert validator._compute_confidence(1, 0.8, frequency_threshold=3) == 0.57  # (0.33*0.5 + 0.8*0.5)

# Test storage routing
result = await validator.validate_output('fact', {...}, llm_confidence=0.95)
assert result.storage_decision == 'direct'

# Test similarity matching
similar = await validator.find_similar_patterns('works_with', 'rel_types')
assert similar[0]['pattern'] == 'works_for'
assert similar[0]['similarity'] > 0.85
```

### Integration Tests
```python
# Test WGM gate uses validator
gate = WGMValidationGate(db, validator=validator)
result = gate.validate_edge(...)
assert result['storage_decision'] in ['direct', 'staged']

# Test re-embedder uses validator
results = await validator.evaluate_batch('rel_type', 'ontology_evaluations')
assert results['evaluated'] > 0
```

### Full Pipeline Test (curl)
```bash
# Send test conversation to ${OPENWEBUI_DOMAIN}
# Verify:
# - Facts stored with correct confidence
# - Metrics updated globally
# - Qdrant contains expected embeddings
# - PostgreSQL contains expected facts
```

## Monitoring & Debugging

**Structured logging:**
```
[INFO] llm_output_validator.validate_output output_type=fact, confidence=0.9, decision=direct
[INFO] llm_output_validator.batch_evaluated output_type=rel_type, pattern=works_for, decision=approved
[WARN] llm_output_validator.similarity_search_failed pattern=xyz (embedding service unavailable)
```

**Metrics inspection:**
```python
metrics = validator.get_metrics()
print(metrics)  # Shows what LLM created across all output types
```

**Debugging threshold issues:**
```python
# Lower approval threshold if facts are rejected too aggressively
validator.thresholds['fact']['approval'] = 0.7  # More permissive

# Increase frequency threshold for patterns if approval is too eager
validator.thresholds['rel_type']['frequency'] = 4  # Require 4 occurrences
```

## Next Steps

1. **Review & approve** this integration plan
2. **Implement Phase 2a** (WGM gate integration)
3. **Test WGM gate** end-to-end
4. **Implement Phase 2b** (filter integration)
5. **Implement Phase 2c** (re-embedder integration)
6. **Full system test** via curl + ssh inspection
7. **Copy to pre-prod** and rebuild
8. **Monitor metrics** for 24 hours before production

---

**Status:** ✅ Validator implemented, ready for integration testing
