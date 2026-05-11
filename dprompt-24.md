# dprompt-24 — Nested Taxonomy Layers: Cascading Scope Architecture

## Overview

**This is a big one.** Replace flat taxonomies (dprompt-20) with nested, cascading layers where each layer **contains and inherits** all previous layers' data.

**Vision:** Query intent determines entry scope, which automatically cascades through parent layers.
- "tell me about myself" → layer 1 (self) → identity facts only
- "tell me about my family" → layer 2 (family) → identity + family facts
- "tell me about my species" → layer 3 (species) → identity + family + biological facts
- "tell me about Earth" → layer 4 (location) → everything

## Architecture

### Layer Hierarchy

```
Layer 1 (self)
  ├─ Entity types: Person (user only)
  ├─ Rel types: pref_name, also_known_as, same_as, age, height, weight, born_on, nationality, has_gender
  └─ Facts: identity-defining only

Layer 2 (family) — CONTAINS Layer 1
  ├─ Entity types: Person (spouse, children, siblings)
  ├─ Rel types: spouse, parent_of, child_of, sibling_of, lives_at, lives_in, has_pet
  ├─ Transitive: has_pet via household taxonomy
  └─ Inherits: all Layer 1 facts for user

Layer 3 (species) — CONTAINS Layer 1 + 2
  ├─ Entity types: Species, Genus, Classification
  ├─ Rel types: is_species, genus_of, belongs_to_phylum
  └─ Inherits: all Layer 1 + 2 facts

Layer 4 (location/earth) — CONTAINS Layer 1 + 2 + 3
  ├─ Entity types: Location, City, Country, Continent, Planet
  ├─ Rel types: lives_in, located_in, capital_of, timezone, geography
  └─ Inherits: all Layer 1 + 2 + 3 facts
```

### Two Separate Concerns (DO NOT CONFLATE)

**1. Graph Traversal (relational, existing code)**
- "How are entities connected?"
- User → spouse → Mars → has_pet → Fraggle
- Follows relationship edges regardless of layer
- Unaffected by this change

**2. Layer Containment (NEW, scope-based)**
- "What's in scope for this query intent?"
- Determines which entities and facts are **relevant** to the query
- Constrains results: only return entities + facts from entry_layer and lower
- Independent of graph traversal

**Critical:** Graph traversal finds the connections. Layers determine which connections are in scope for the response.

## Database Schema Changes

### entities table — Add layer column

```sql
ALTER TABLE entities ADD COLUMN layer INT DEFAULT 1;
-- layer 1: user identity only
-- layer 2: family members (spouse, children)
-- layer 3: species/classification entities
-- layer 4: geographic/location entities

CREATE INDEX idx_entities_layer_user ON entities(user_id, layer);
```

**Entity layer assignment rules:**
- User entity: always layer 1
- Spouse, children, siblings: layer 2
- Pets, household members: layer 2
- Species, genus, phylum: layer 3
- Cities, countries, locations: layer 4

**Ingest must classify:** When resolving entity, determine its layer based on:
- Explicit: if rel_type is "is_species" → object is layer 3
- Relational: if rel_type is "spouse" → object is layer 2
- Default: layer 1 (assume identity unless proven otherwise)

### facts & staged_facts tables — Add layer column

```sql
ALTER TABLE facts ADD COLUMN layer INT DEFAULT 1;
ALTER TABLE staged_facts ADD COLUMN layer INT DEFAULT 1;

-- Layer assignment: same as highest entity layer in the fact
-- spouse(user:L1, marla:L2) → fact is layer 2
-- is_species(user:L1, homo_sapiens:L3) → fact is layer 3

CREATE INDEX idx_facts_cascade ON facts(layer, user_id, rel_type) 
  INCLUDE (subject_id, object_id, confidence);
```

### entity_taxonomies table — Add parent_layer column

```sql
ALTER TABLE entity_taxonomies ADD COLUMN layer INT NOT NULL;
ALTER TABLE entity_taxonomies ADD COLUMN parent_taxonomy_name VARCHAR(64);

-- family taxonomy: layer=2, parent=NULL (top-level at this layer)
-- household taxonomy: layer=2, parent=family (expands family scope)
-- species taxonomy: layer=3, parent=family (adds biological classification)
-- location taxonomy: layer=4, parent=species (adds geographic context)

CREATE FOREIGN KEY (parent_taxonomy_name) 
  REFERENCES entity_taxonomies(taxonomy_name);
```

## Query Flow (The Cascade)

```python
def query(text, user_id):
    # 1. Detect intent and map to layer
    entry_layer = detect_layer_intent(text)  # "family" → 2, "species" → 3
    
    # 2. Load taxonomy for entry_layer
    taxonomy = load_taxonomy(entry_layer)
    
    # 3. Fetch facts from ALL layers 1...N where layer <= entry_layer
    facts = []
    for layer in range(1, entry_layer + 1):
        layer_rel_types = get_rel_types_for_layer(layer)
        layer_facts = fetch_layer_facts(user_id, layer, layer_rel_types)
        facts.extend(layer_facts)
    
    # 4. Graph traversal (unchanged) — find connected entities
    # This happens AFTER we have the fact set scoped to entry_layer
    connected_entities = traverse_graph(facts, user_id)
    
    # 5. Filter results to only entities in entry_layer scope
    scoped_entities = [e for e in connected_entities if entities[e].layer <= entry_layer]
    scoped_facts = [f for f in facts if f.subject_id in scoped_entities and f.object_id in scoped_entities]
    
    return scoped_facts
```

**Key insight:** Don't filter by relevance score. Layer membership is deterministic, not probabilistic.

## Database Optimization: Composite Index Strategy (NOT Materialized Views)

### Why NOT Materialized Views

**Rejected:** Cascade performance via pre-computed materialized views.

**Reason:** Freshness latency in a real-time memory system.
- User says "I have wife Marla" → fact ingested → view refresh needed → if refresh hasn't happened, next query sees stale cascade
- For personal knowledge graph, that's unacceptable (you just told it something, it should remember it **now**)
- View refresh adds 100ms+ latency per ingest
- Defeats the purpose of real-time memory injection

### Composite Index Strategy (Better for FaultLine)

```sql
-- Single index covers most queries
CREATE INDEX idx_facts_cascade ON facts(layer, user_id, rel_type)
  INCLUDE (subject_id, object_id, confidence, fact_class);

-- Query execution: Index covers layer=2, user_id=X, rel_type IN [spouse, parent_of, child_of, ...]
-- No full table scan, no view refresh latency
```

**Why this works:**
- Indexes are always fresh (no refresh lag)
- Cascade loop is simple: 4 iterations max (layers 1-4)
- Index covers filtering, no table scan needed
- Suitable for typical personal graphs (family ≤ 100 entities)

**Scale boundary:** If query latency becomes bottleneck at 100k+ users per instance, then:
- Add materialized views with on-ingest refresh (atomic: INSERT → REFRESH)
- Or partition by (layer, user_id) at database level
- But start with indexes, optimize when metrics show need

## Hard Constraints & Limitations

### 1. Entity Layer Assignment is Deterministic

**Hard constraint:** Each entity can belong to only ONE layer.
```python
entities.layer IN (1, 2, 3, 4)
# NOT multiple layers
# NOT inherited (Mars is layer 2, not "layer 2 + inherits layer 1")
```

**Why:** Layer membership determines scope. Ambiguity breaks cascade logic.

**Implication:** When ingesting "I am homo sapiens", you MUST classify homo sapiens as layer 3, not layer 1 or "both".

**How to enforce:** Ingest classification logic:
- rel_type context (is_species → layer 3)
- Entity type (Species → layer 3)
- User override (if unsure, ask LLM)

### 2. Graph Traversal Doesn't Cross Layer Boundaries

**Hard constraint:** Graph traversal finds connections, layers constrain results.

**NOT:** "Follow relationships across any layer"
**IS:** "Follow relationships, but only return entities in scope"

```python
# User (layer 1) → spouse (layer 2) → pet (layer 2) 
# Query at layer 2: return all three ✓
# Query at layer 1: return only user ✗ (spouse + pet outside scope)
```

**Implication:** Layer 1 queries are **sparse** (identity only). Scope expands as you move up layers.

### 3. Freshness Over Optimization

**Hard constraint:** No pre-computed cascade. Query must see latest facts immediately.

**Why:** Real-time memory system. User says X, system should remember X on next query without refresh lag.

**Implication:** Accept ~1-4ms cascade latency (4 iterations × index lookup) instead of optimizing it away at freshness cost.

### 4. Rel_Type Coverage Per Layer is Complete

**Hard constraint:** Every rel_type must be assigned to exactly ONE layer.

**Why:** Prevents ambiguity when cascading.

```python
# MUST define:
rel_types_layer1 = [pref_name, also_known_as, same_as, ...]
rel_types_layer2 = [spouse, parent_of, child_of, sibling_of, ...]
rel_types_layer3 = [is_species, genus_of, ...]
rel_types_layer4 = [lives_in, located_in, ...]

# NOT allowed: rel_type in multiple layers
```

**Implication:** Taxonomy definition is rigid. New rel_types require explicit layer assignment.

### 5. Entity Type Constraints Per Layer

**Hard constraint:** Only specific entity types allowed per layer.

```python
# Layer 1: Person (user only)
# Layer 2: Person (family), Animal (pets)
# Layer 3: Species, Genus, Phylum, Concept
# Layer 4: Location, City, Country, Planet

# Ingest must validate: 
# if rel_type == "is_species", object_type must be Species/Genus/Concept
# if rel_type == "spouse", object_type must be Person AND object.layer must be 2
```

**Implication:** Type validation becomes more strict. Layer enforcement is baked into WGM gate.

### 6. No Partial Inheritance

**Hard constraint:** Layer N gets ALL facts from layers 1...N-1, not selective facts.

**NOT:** "Layer 2 gets only spouse facts from layer 1"
**IS:** "Layer 2 gets everything: all layer 1 identity facts + all layer 2 family facts"

**Why:** Prevents query logic from becoming conditional. Cascade is "layers 1 through N" always.

**Implication:** Layer design must be clean. Mistakes propagate up (if layer 2 has junk, layer 3+ sees it).

## Files to Change

### Schema
- `migrations/XXX_add_layer_columns.sql` — ALTER entities, facts, staged_facts, entity_taxonomies

### Code
- `src/api/main.py`:
  - `_detect_layer_intent()` — map query keywords to entry_layer
  - `_fetch_facts_cascaded()` — cascade through layers 1...N
  - `_classify_entity_layer()` — ingest: determine layer for new entities
  - `_validate_layer_constraints()` — ingest: enforce type + rel_type per layer

- `openwebui/faultline_tool.py`:
  - Remove relevance filtering for layer-matched facts
  - Don't filter by `rel_type in [spouse, parent_of, ...]` if it matches entry_layer_taxonomy

- `src/wgm/gate.py`:
  - Add layer validation: object.layer must match expected layer for rel_type

## Test Scenarios

**Test 1: Identity Query (Layer 1)**
```
Query: "tell me about myself"
Expected: pref_name, also_known_as, age, height, weight (identity facts only)
NOT: spouse, children, pets, species info
```

**Test 2: Family Query (Layer 2)**
```
Query: "tell me about my family"
Expected: identity facts + spouse + children + pets (layers 1 + 2)
NOT: species, location, geographic facts
```

**Test 3: Cascading With Transitive (Layer 2)**
```
Ingest: "I have spouse Marla. Marla has dog Fraggle."
Query: "tell me about my family"
Expected: user (layer 1) + marla (layer 2) + fraggle (layer 2 via household transitive)
Graph traversal finds: user → spouse → marla, marla → has_pet → fraggle
Layer constraint: all three in layers 1-2 ✓ return all
```

**Test 4: Species Query (Layer 3)**
```
Query: "what species am I?"
Expected: user (layer 1) + family (layer 2) + species facts (layer 3)
Shows: "You are human (homo sapiens). Your family are also humans."
```

**Test 5: Geographic Query (Layer 4)**
```
Query: "where do I live on Earth?"
Expected: all facts (layers 1-4) + location/geographic facts
Shows: "You live at 156 Cedar St, Kitchener, Ontario, Canada (North America, Earth)"
```

## Why This Is Awesome

1. **Data-driven, not hardcoded** — taxonomy system determines scope, no signal patterns
2. **Scales infinitely** — add new layers (biological, digital, social, professional) without code change
3. **Fresh and fast** — composite indexes, no refresh latency, immediate memory injection
4. **Semantically correct** — layer membership is deterministic, not probabilistic
5. **Graph + scope unified** — graph traversal finds connections, layers constrain relevance
6. **Cascading scope** — "tell me about" queries naturally expand context as layer increases

This is the architectural payoff of dprompt-20 (taxonomies) + dprompt-22 (LLM-first) + dprompt-23 (taxonomy-driven query) finally coming together. Nested scopes, real-time freshness, infinite extensibility.

## Implementation Checklist

- [ ] Add layer columns to entities, facts, staged_facts, entity_taxonomies
- [ ] Create composite indexes on (layer, user_id, rel_type)
- [ ] Implement `_detect_layer_intent()` for query classification
- [ ] Implement `_fetch_facts_cascaded()` with layer loop
- [ ] Implement `_classify_entity_layer()` in ingest
- [ ] Add layer validation to WGM gate (type + rel_type constraints per layer)
- [ ] Update Filter: don't filter layer-matched facts by relevance
- [ ] Test all 5 scenarios above
- [ ] Verify graph traversal works correctly (unchanged code)
- [ ] Performance test: layer 4 query should be < 10ms

## Timeline

Phase 1 (schema + ingest): 3-4 hours
Phase 2 (query + filter): 2-3 hours
Phase 3 (validation + testing): 2-3 hours

**Total: ~8 hours** for full implementation + testing.

---

**You built a personal knowledge graph that understands nested scopes. That's fucking awesome.**
