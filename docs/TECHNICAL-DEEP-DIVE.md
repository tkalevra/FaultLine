# FaultLine Technical Deep Dive: Architecture & Implementation

Complete technical overview of FaultLine's architecture, data models, and algorithms.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      OpenWebUI                          │
│           (User-facing chat interface)                  │
└────────┬──────────────────────────────────────────────┘
         │
    ┌────┴────────────────────────┐
    │                             │
┌───▼────────────┐      ┌────────▼─────────┐
│  Filter Inlet  │      │ Filter Outlet    │
│ (Extract)      │      │ (Pass-through)   │
└───┬────────────┘      └────────▲─────────┘
    │                            │
    └───────┬────────────────────┘
            │
    ┌───────▼─────────────────────────────────────┐
    │     FaultLine API (FastAPI)                 │
    │  (src/api/main.py)                          │
    ├──────────────────────────────────────────────┤
    │  POST /extract/rewrite    (GLiNER2 triple)  │
    │  POST /ingest             (Validate+Store)  │
    │  POST /query              (Retrieve facts)  │
    │  POST /retract            (Delete facts)    │
    │  POST /store_context      (Embedding)       │
    │  GET  /health             (Status)          │
    └─────┬──────────────────┬───────────┬────────┘
          │                  │           │
    ┌─────▼──────┐    ┌──────▼────┐   ┌─▼──────────┐
    │ PostgreSQL │    │  Qdrant   │   │   Redis    │
    │  (Facts)   │    │  (Vector) │   │ (Cache)    │
    └────────────┘    └───────────┘   └────────────┘
          │
    ┌─────▼──────────────────────────────────────┐
    │         Re-Embedder (Background)           │
    │    (src/re_embedder/embedder.py)           │
    │  - Promotes staged facts                   │
    │  - Syncs to Qdrant                         │
    │  - Evaluates novel patterns                │
    │  - Resolves name conflicts                 │
    └────────────────────────────────────────────┘
```

---

## Data Model: Three Tables, One Graph

### 1. Facts Table (Relational + Hierarchical)

```sql
CREATE TABLE facts (
    id                    SERIAL PRIMARY KEY,
    user_id               TEXT NOT NULL,           -- User isolation
    subject_id            TEXT NOT NULL,           -- UUID or user_id
    object_id             TEXT NOT NULL,           -- UUID (never display name)
    rel_type              TEXT NOT NULL,           -- Relationship type
    confidence            FLOAT NOT NULL,          -- 0.0 to 1.0
    fact_class            CHAR(1),                 -- A, B, or C
    provenance            TEXT,                    -- source metadata
    created_at            TIMESTAMP,
    superseded_at         TIMESTAMP,               -- Soft delete
    qdrant_synced         BOOLEAN,
    UNIQUE(user_id, subject_id, object_id, rel_type)
);

-- Example rows:
-- (user1, user_uuid, marla_uuid, spouse, 1.0, A, ...)
-- (user1, user_uuid, des_uuid, parent_of, 1.0, A, ...)
```

**Key principle:** `object_id` is ALWAYS a UUID or user_id. Never a display name. This prevents alias multiplication bugs.

### 2. Entity Attributes Table (Scalar)

```sql
CREATE TABLE entity_attributes (
    user_id         TEXT NOT NULL,
    entity_id       TEXT NOT NULL,      -- UUID (normalized to "user" for user attributes)
    attribute       TEXT NOT NULL,      -- "age", "name", "height"
    value_text      TEXT,               -- "Chris", "14"
    value_int       INT,
    value_float     FLOAT,
    value_date      DATE,
    provenance      TEXT,
    sensitivity     TEXT,
    UNIQUE(user_id, entity_id, attribute)
);

-- Example rows:
-- (user1, user_uuid, pref_name, "Chris", NULL, NULL, ...)
-- (user1, des_uuid, age, "14", 14, NULL, ...)
```

**Key principle:** One value per attribute per entity. ON CONFLICT DO UPDATE ensures atomicity.

### 3. Entity Registry (Identity)

```sql
CREATE TABLE entities (
    id              TEXT PRIMARY KEY,   -- UUID v5 surrogate
    user_id         TEXT NOT NULL,
    entity_type     TEXT,               -- Person, Organization, Place
    created_at      TIMESTAMP
);

CREATE TABLE entity_aliases (
    entity_id       TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    alias           TEXT NOT NULL,      -- Display name (lowercased)
    is_preferred    BOOLEAN,
    UNIQUE(user_id, alias)
);

-- Example:
-- entities: (uuid1, user1, Person)
-- aliases: (uuid1, user1, "Chris", true), (uuid1, user1, "christopher", false)
```

**Key principle:** UUID v5 surrogates are deterministic. Same name always → same UUID. Prevents duplicates.

---

## Pipeline 1: Extract (LLM Triple Inference)

**File:** `src/api/main.py:POST /extract/rewrite`

### Algorithm: Layered Extraction with Type Context

```python
def extract_rewrite(text: str, user_id: str) -> List[EdgeInput]:
    # Layer 0: Detect corrections and retractions
    is_correction = detect_correction_signal(text)  # "actually", "wrong", etc
    is_retraction = detect_retraction_signal(text)  # "forget", "delete", etc
    
    # Layer 1: Get type context (GLiNER2 preflight)
    typed_entities = gliner2_extract(text, prompt="person, organization, place")
    # Returns: [(entity_text, entity_type), ...]
    
    # Layer 2: LLM triple inference with type hints
    prompt = f"""
    Extract triples from: {text}
    Known entities: {typed_entities}
    Format: subject|rel_type|object
    """
    
    llm_output = llm.chat(prompt)
    triples = parse_triples(llm_output)
    
    # Layer 3: Validate against metadata
    validated_triples = []
    for subject, rel_type, object in triples:
        rel_meta = get_rel_type_metadata(rel_type)
        
        if rel_meta:
            # Known rel_type: validate against constraints
            confidence = validate_triple(subject, rel_type, object, rel_meta)
        else:
            # Unknown rel_type: mark as novel
            confidence = 0.6  # Penalty for new pattern
        
        validated_triples.append(EdgeInput(
            subject=subject,
            rel_type=rel_type,
            object=object,
            confidence=confidence,
            is_correction=is_correction,  # Flag for ingest
            is_retraction=is_retraction,  # Flag for ingest
        ))
    
    return validated_triples
```

**Validation Gates:**
```python
def _validate_triple_against_metadata(subject, rel_type, object, rel_meta):
    # Check: head_type matches?
    if subject.entity_type not in rel_meta.head_types:
        return 0.5  # Penalty
    
    # Check: tail_type matches?
    if rel_meta.tail_types == {SCALAR}:
        if not isinstance(object, str):
            return 0.1  # Major penalty
    elif object.entity_type not in rel_meta.tail_types:
        return 0.5  # Penalty
    
    # Check: directionality?
    if rel_meta.is_symmetric and inverse_exists(subject, rel_type, object):
        return 0.8  # Already have the inverse
    
    return 0.95  # Pass
```

---

## Pipeline 2: Ingest (Validation + Classification + Storage)

**File:** `src/api/main.py:POST /ingest`

### Three-Layer Learning

**Layer 1: Rel_type Discovery**
```python
def _handle_rel_type(rel_type: str):
    existing = db.query(rel_types).filter(rel_type=rel_type).first()
    
    if existing:
        return existing.metadata  # Use known rules
    
    # Unknown rel_type: engine learns it
    new_metadata = {
        'category': infer_category(rel_type),      # relationship, attribute, temporal?
        'is_symmetric': infer_symmetry(rel_type),  # spouse vs parent_of?
        'tail_types': infer_tail_types(rel_type),  # SCALAR vs UUID?
        'head_types': infer_head_types(rel_type),  # What can be subject?
    }
    
    db.insert(rel_types, (rel_type, new_metadata))
    return new_metadata
```

**Layer 2: Entity Type Discovery**
```python
def _handle_entity_type(entity_name: str, inferred_type: str):
    entity = registry.resolve(entity_name)  # Get or create UUID
    
    if entity.entity_type == 'unknown' and inferred_type != 'unknown':
        # Classify the entity
        db.insert(facts, (
            subject_id=entity.uuid,
            rel_type='instance_of',
            object_id=type_uuid,  # e.g., person_uuid
            confidence=0.95,
            fact_class='A'
        ))
        
        # Update entity record
        db.update(entities, entity.uuid, entity_type=inferred_type)
```

**Layer 3: Storage Path Routing**
```python
def classify_fact_type(rel_type: str, object_value):
    rel_meta = get_rel_type_metadata(rel_type)
    
    if rel_meta.tail_types == {SCALAR}:
        return 'SCALAR'  # entity_attributes table
    elif rel_meta.is_hierarchy_rel:
        return 'HIERARCHY'  # facts table with hierarchy semantics
    else:
        return 'RELATIONAL'  # facts table with graph semantics
```

### Validation Gates

**WGM Gate: Ontology Consistency**
```python
def wgm_validation_gate(subject, rel_type, object):
    rel_meta = get_rel_type_metadata(rel_type)
    
    # 1. Type constraints
    if subject.entity_type not in rel_meta.head_types:
        raise ValidationError(f"Invalid head type")
    
    # 2. Semantic conflicts
    if rel_type in OWNERSHIP_RELS and object.entity_type in ABSTRACT_TYPES:
        raise ValidationError(f"Can't own an abstract type")
    
    # 3. Hierarchy consistency
    if rel_meta.is_hierarchy_rel:
        if creates_cycle(subject, rel_type, object):
            raise ValidationError(f"Hierarchy cycle detected")
    
    return True
```

**Semantic Conflict Detection**
```python
def _detect_semantic_conflicts(subject, rel_type, object):
    # If object is a TYPE (instance_of or subclass_of), it can't be owned/worked_for
    if rel_type in OWNERSHIP_RELS:
        if exists(object, 'instance_of', abstract_type):
            conflicting_fact = get_fact(subject, rel_type, object)
            supersede(conflicting_fact)  # Auto-remove the conflicting ownership
```

### Confidence Classification

```python
def classify_confidence(subject, rel_type, object, is_correction, is_retraction):
    # CLASS A: User-stated facts
    if is_correction or source == 'user_direct':
        return (1.0, 'A')  # Immediate commitment
    
    # CLASS B: LLM-inferred, ontology established
    if get_rel_type_metadata(rel_type) and not is_new_type and not is_new_entity:
        return (0.8, 'B')  # Stage, promote at 3 confirmations
    
    # CLASS C: Novel pattern
    if is_new_type or is_new_entity:
        return (0.4, 'C')  # Stage, evaluate by re-embedder, expire in 30 days
    
    return (0.6, 'B')  # Default: medium confidence
```

---

## Pipeline 3: Query (Retrieval + Deduplication + Injection)

**File:** `src/api/main.py:POST /query`

### Four Parallel Retrieval Sources

```python
async def query(user_id: str, text: str) -> Dict[str, List[Dict]]:
    # Source 1: Baseline facts
    baseline = db.query(facts).filter(
        user_id=user_id,
        superseded_at=None
    ).all()
    
    # Source 2: Graph traversal (single-hop connectivity)
    connected = _graph_traverse(
        db, user_id, 
        max_hops=1,
        rel_types=RELATIONAL_TYPES  # spouse, parent_of, works_for
    )
    
    # Source 3: Hierarchy expansion (upward classification chains)
    hierarchy = _hierarchy_expand(
        db, user_id,
        direction='up',
        rel_types=HIERARCHY_TYPES  # instance_of, subclass_of
    )
    
    # Source 4: Scalar attributes converted to facts
    attributes = _attributes_to_facts(db, user_id)
    
    # Merge all sources
    all_facts = baseline + connected + hierarchy + attributes
```

### Deduplication Algorithm

```python
def deduplicate_facts(facts: List[Dict]) -> List[Dict]:
    # Group by (subject_uuid, rel_type, object_uuid)
    # Key insight: Use UUIDs not display names
    
    groups = defaultdict(list)
    for fact in facts:
        key = (
            fact['_subject_id'],    # UUID
            fact['rel_type'],
            fact['_object_id']      # UUID
        )
        groups[key].append(fact)
    
    # Keep highest confidence from each group
    deduped = []
    for key, group in groups.items():
        best = max(group, key=lambda f: f['confidence'])
        deduped.append(best)
    
    return deduped
```

**Why UUID-based?** Prevents alias multiplication:
- "Chris", "Christopher", "chris" → all same UUID → one fact
- Without this: 3 separate facts for same person (wrong!)

### Prose Formatting

```python
def format_fact_for_injection(fact):
    subject_name = entity_registry.get_preferred_name(fact['subject_id'])
    object_name = (
        entity_registry.get_preferred_name(fact['object_id'])
        if fact['object_id'] != fact['object_value']
        else fact['object_value']  # Scalar value
    )
    
    # Use natural language template
    template = REL_TYPE_TEMPLATES[fact['rel_type']]
    prose = template.format(subject=subject_name, object=object_name)
    
    # Example:
    # REL_TYPE_TEMPLATES['spouse'] = "{subject} is your spouse, {object}"
    # prose = "Marla is your spouse"
    
    # Add metadata
    metadata_prose = (
        f"({fact['fact_class']}, confidence {fact['confidence']:.0%})"
    )
    
    return f"{prose} {metadata_prose}"
```

---

## Background Process: Re-Embedder

**File:** `src/re_embedder/embedder.py`

### Main Loop (Configurable Interval)

```python
async def reembed_loop(interval: int = 60):  # seconds
    while True:
        # 1. Promote staged facts (Class B at 3+ confirmations)
        promoted = promote_staged_facts(threshold=3)
        for fact in promoted:
            db.insert(facts, fact)  # Move to facts table
            db.delete(staged_facts, fact.id)
        
        # 2. Sync unsynced facts to Qdrant
        unsynced = db.query(facts).filter(qdrant_synced=False).all()
        for fact in unsynced:
            embedding = embed(fact.text)
            qdrant.upsert(collection=f"faultline-{user_id}", fact_id, embedding)
            db.update(facts, fact.id, qdrant_synced=True)
        
        # 3. Expire Class C facts
        expired = db.query(staged_facts).filter(
            fact_class='C',
            expires_at <= now()
        ).all()
        for fact in expired:
            db.delete(staged_facts, fact.id)
            qdrant.delete(collection, fact.id)
        
        # 4. Evaluate ontology candidates (novel rel_types)
        candidates = db.query(ontology_evaluations).filter(
            frequency >= 3  # Seen 3+ times
        ).all()
        for candidate in candidates:
            approval = evaluate_novel_rel_type(candidate)
            if approval.approved:
                db.insert(rel_types, candidate)
        
        # 5. Resolve name conflicts (LLM-powered disambiguation)
        conflicts = db.query(entity_name_conflicts).filter(
            status='pending'
        ).all()
        for conflict in conflicts:
            winner = resolve_conflict_via_llm(conflict)
            db.update(entity_aliases, 
                     filter(alias=conflict.alias),
                     is_preferred=(entity_id == winner))
        
        await asyncio.sleep(interval)
```

---

## Performance Considerations

### Connection Pooling
```python
pool = asyncpg.create_pool(
    dsn=POSTGRES_DSN,
    min_size=5,
    max_size=DB_POOL_SIZE,  # 15 default
    timeout=DB_TIMEOUT,      # 30s
)
```

### Caching Layers
```python
@cache(ttl=300)  # 5 minutes
def get_rel_type_metadata(rel_type: str):
    # Query DB once, cache for 5min
    return db.query(rel_types).filter(rel_type=rel_type).first()

redis_cache = RedisCache(
    url=REDIS_URL,
    ttl=EMBEDDING_CACHE_TTL  # 86400 seconds (1 day)
)
```

### Indexing Strategy
```sql
CREATE INDEX idx_facts_user_id ON facts(user_id);
CREATE INDEX idx_facts_subject_id ON facts(subject_id);
CREATE INDEX idx_facts_rel_type ON facts(rel_type);
CREATE INDEX idx_entity_aliases_user_id ON entity_aliases(user_id);
CREATE INDEX idx_entity_aliases_alias ON entity_aliases(alias);
```

---

## Metadata-Driven Everything

**The Core Philosophy:** No hardcoding. Let the data describe the rules.

```python
# rel_types table columns describe the rules:
# - category: "family", "work", "location", "attribute"
# - is_symmetric: true/false
# - inverse_rel_type: "child_of" for "parent_of"
# - head_types: ["Person", "Organization"]
# - tail_types: ["Person", "SCALAR"]
# - is_hierarchy_rel: true/false
# - is_leaf_only: true/false

# At runtime:
def _get_rel_type_metadata(rel_type: str) -> RelTypeMetadata:
    return db.query(rel_types).filter(rel_type=rel_type).first()

# This query happens everywhere validation is needed:
# - extract.py uses it for type validation
# - main.py uses it for storage path routing
# - main.py uses it for confidence classification
# - wgm/gate.py uses it for semantic validation

# No hardcoded lists. No code changes for new rel_types.
# Just insert a row in rel_types, and the whole system knows the rules.
```

---

## Key Architectural Wins

1. **UUID v5 Surrogates** → Deterministic identity, prevents duplicates
2. **Write-Time Normalization** → All validation happens before storage
3. **Three Storage Paths** → Scalar/Relational/Hierarchical routed by metadata
4. **Metadata-Driven Validation** → Rules come from data, not code
5. **Background Re-Embedder** → Async evaluation, no blocking
6. **Staged Fact Promotion** → Facts graduate from ephemeral → confirmed → permanent
7. **User-Stated Priority** → Corrections (Class A) always override inferences
8. **Per-User Isolation** → Every query scoped by user_id

---

## Code Structure

```
src/
├── api/
│   ├── main.py              # /ingest, /query, /retract, /store_context
│   ├── idempotency.py       # Redis dedup cache for inlet
│   ├── llm_client.py        # Centralized LLM endpoint detection
│   └── models.py            # Pydantic request/response models
├── fact_store/
│   └── store.py             # FactStoreManager (INSERT/UPDATE/DELETE)
├── entity_registry/
│   └── registry.py          # UUID v5 surrogates, alias tracking
├── wgm/
│   └── gate.py              # WGMValidationGate (ontology + conflict)
└── re_embedder/
    └── embedder.py          # Background re-embedding + promotion + expiry

openwebui/
├── faultline_tool.py        # Filter inlet (retraction + extraction)
└── faultline_function.py    # Function (manual store_fact)
```

---

## Debugging & Observability

**Health Endpoint:**
```python
GET /health
{
    "status": "ok",
    "database": "ok",
    "qdrant": "ok",
    "llm": "ok",
    "re_embedder_running": true,
    "cache_hits": 1234,
    "cache_misses": 56
}
```

**Logging:**
```python
log.info("fact_committed", 
    user_id=user_id,
    rel_type=rel_type,
    fact_class=fact_class,
    confidence=confidence,
    elapsed_ms=elapsed
)
```

---

## Testing Strategy

```bash
# Unit tests (no dependencies)
pytest tests/unit/

# Integration tests (with containers)
pytest tests/integration/

# Full pipeline test
bash /tmp/TESTS/comprehensive_family_pipeline_test.sh
```

Example: Test three-layer learning
```python
def test_layer1_rel_type_creation():
    """Novel rel_type should be created and stored"""
    ingest(novel_rel_type='admires')
    assert db.query(rel_types).filter(rel_type='admires').exists()

def test_layer2_entity_classification():
    """Unknown entity should be classified on ingest"""
    ingest(entity='Des', inferred_type='Person')
    assert db.query(facts).filter(
        subject_id=des_uuid,
        rel_type='instance_of'
    ).exists()

def test_layer3_storage_routing():
    """Scalar rel_type should route to entity_attributes"""
    ingest(rel_type='age', object='14')
    assert db.query(entity_attributes).filter(
        attribute='age'
    ).exists()
```

---

This is how FaultLine works under the hood: data-driven, metadata-driven, no hardcoding, every component validates before commit.
