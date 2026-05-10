# dprompt-29 — Comprehensive Validation Suite: Full Pipeline Test

## Purpose

Validate the complete FaultLine pipeline post-dprompt-27/28 without regressions. Test ingest → classify → embed → query with novel types, fact promotion, hierarchy chains, and edge cases.

## Scope: What Gets Tested

### 1. Ingest Pipeline (No Changes Expected)
- LLM-First extraction (Filter LLM extracts edges)
- WGM validation gate (novel types → pending)
- Fact classification (Class A/B/C)
- Entity type inference
- Preferred name handling (8 patterns, auto-synthesis)

### 2. Query Redesign (dprompt-27/28)
- Graph traversal: single-hop connectivity
- Hierarchy expansion: upward classification chains
- Integration: baseline + graph + hierarchy + Qdrant merge
- Deduplication: across all sources
- Entity attributes filtering

### 3. Novel Type Handling
- Unknown rel_type → Class C (staged)
- Ontology evaluation (async, frequency ≥ 3)
- Cosine similarity mapping (> 0.85)
- Rejection on low confidence
- No crashes on novel types

### 4. Fact Promotion
- Class B facts promoted when confirmed_count ≥ 3
- Staged Qdrant points deleted post-promotion
- New facts Qdrant upserted on next cycle
- Promotion doesn't break retrieval

### 5. Hierarchy Edge Cases
- Cycles (e.g., A subclass_of B, B subclass_of A) — depth-limited recursion
- Deep chains (5+ levels deep)
- Mixed entity types (Person, Animal, Organization)
- Downchain members discovery
- Sideways composition (part_of relationships)

### 6. Relevance Scoring
- Query signal match (0.0–0.6)
- Confidence bonus (0.0–0.3)
- Sensitivity penalty (-0.5 for born_on, lives_at, etc.)
- Identity rels bypass scoring
- Threshold 0.4: facts below excluded

### 7. Re-embedder
- Unsynced facts → embed → Qdrant upsert
- Class C expiry after 30 days
- Qdrant reconciliation (facts match Qdrant collection)
- No orphaned points

## Test Scenarios (Detailed)

### Scenario 1: Basic Graph + Hierarchy Query
**Setup:**
```
User: alice (UUID: alice-uuid)
Facts:
  - alice spouse Mars (Class A)
  - Mars has_pet Fraggle (Class A)
  - alice lives_at "156 Cedar St" (Class A)
  - Fraggle instance_of Morkie (Class A)
  - Morkie subclass_of Dog (Class A)
  - Dog subclass_of Animal (Class A)
```

**Query:** "where do mars and fraggle live?"

**Expected:**
- Graph traversal: {mars, fraggle}
- Hierarchy expand: {alice, mars, person, fraggle, morkie, dog, animal}
- Facts returned: spouse, has_pet, lives_at, instance_of, subclass_of
- Response: mentions both mars and fraggle at location

**Validate:**
- All entities found ✓
- No duplicates ✓
- Hierarchy chains included ✓

---

### Scenario 2: Novel Rel_Type Handling
**Setup:**
```
User: bob (UUID: bob-uuid)
Ingest: "Bob mentors Alice in chess"
LLM extraction: (bob, mentors, alice)
WGM gate: "mentors" not in rel_types → "unknown" (novel)
```

**Expected:**
- Edge inserted to staged_facts as Class C
- rel_type marked "unknown"
- confidence=0.4
- expires_at = now() + 30 days
- No crash on unknown type
- Query doesn't return unknown rel_type facts (below relevance threshold)

**Validate:**
- Novel type → Class C ✓
- Staged entry created ✓
- No crashes ✓
- Expiry time set ✓

---

### Scenario 3: Fact Promotion (Class B → facts)
**Setup:**
```
User: charlie (UUID: charlie-uuid)
Ingest: "Charlie lives in Toronto" (Class B: lives_in)
  confirmed_count=1, fact_class='B'
Repeat ingest 2 more times (same fact)
  After 3rd ingest: confirmed_count=3
```

**Expected:**
- After 1st ingest: staged_facts, confirmed_count=1
- After 2nd ingest: staged_facts, confirmed_count=2
- After 3rd ingest + re-embedder cycle: promoted_at set, moved to facts
- Query returns fact
- Qdrant has new upsert for promoted fact
- Staged Qdrant point cleaned up

**Validate:**
- Promotion triggers at confirmed_count≥3 ✓
- Fact moves to permanent table ✓
- Query retrieves promoted fact ✓

---

### Scenario 4: Hierarchy Cycles (Defensive)
**Setup:**
```
User: diana (UUID: diana-uuid)
Facts:
  - Diana instance_of Person
  - Person instance_of HumanBeing
  - HumanBeing instance_of Person (creates cycle)
```

**Query:** "tell me about diana"

**Expected:**
- Hierarchy expansion starts at Diana, follows up chain
- Hits cycle (Person → HumanBeing → Person)
- Depth limit (max_depth=3) prevents infinite recursion
- Returns: {Diana, Person, HumanBeing} (no hang, no crash)

**Validate:**
- CTE depth tracking works ✓
- No infinite loops ✓
- Cycle-safe result ✓

---

### Scenario 5: Deep Hierarchy Chains
**Setup:**
```
User: eve (UUID: eve-uuid)
Entity: Felix (cat)
Facts:
  - Felix instance_of Cat (level 1)
  - Cat subclass_of Felidae (level 2)
  - Felidae subclass_of Carnivora (level 3)
  - Carnivora subclass_of Mammalia (level 4)
  - Mammalia subclass_of Animalia (level 5)
```

**Query:** "what is felix?"

**Expected:**
- Hierarchy expand with max_depth=3: {Felix, Cat, Felidae, Carnivora}
- Stops at depth 3 (Carnivora), doesn't fetch Mammalia/Animalia
- Query returns classification facts up to depth 3

**Validate:**
- Deep chains traversed correctly ✓
- max_depth respected ✓
- No performance degradation ✓

---

### Scenario 6: Mixed Entity Types in Hierarchy
**Setup:**
```
User: frank (UUID: frank-uuid)
Facts:
  - Frank instance_of Person
  - Frank works_for ACME (Organization)
  - ACME located_in Toronto (Location)
  - Frank has_pet Spike (Animal)
  - Spike instance_of Dog
  - Dog subclass_of Animal
```

**Query:** "who am i and what's around me?"

**Expected:**
- Graph traversal: {Frank, ACME, Spike, Toronto}
- Hierarchy: {Frank→Person, Spike→Dog→Animal, ACME→?, Toronto→?}
- All entity types handled without type errors
- Results include mixed entity types

**Validate:**
- Entity types don't cause failures ✓
- Mixed types returned ✓
- Traversal handles all types ✓

---

### Scenario 7: Relevance Scoring + Sensitivity
**Setup:**
```
User: grace (UUID: grace-uuid)
Facts:
  - Grace born_on "1990-05-15" (sensitive, Class A)
  - Grace lives_at "123 Main St" (sensitive, Class A)
  - Grace has_pet Whiskers (not sensitive)
```

**Query 1:** "what's my birthday?"
- born_on: signal match 0.6 + confidence 0.3 - sensitivity penalty -0.5 = 0.4 ✓ (passes threshold)
- Expected: birthday fact returned

**Query 2:** "tell me about myself" (generic, no sensitivity terms)
- born_on: signal match 0.0 + confidence 0.3 - sensitivity penalty -0.5 = -0.2 ✗ (fails threshold)
- Expected: birthday fact excluded

**Validate:**
- Sensitivity penalty applied correctly ✓
- Threshold enforcement works ✓
- Explicit requests override penalty ✓

---

### Scenario 8: Re-embedder Reconciliation
**Setup:**
```
User: henry (UUID: henry-uuid)
Ingest 5 new facts
Wait for re-embedder cycle
Check Qdrant collection "faultline-henry-uuid"
```

**Expected:**
- All 5 facts upserted to Qdrant
- Points in Qdrant match facts in PostgreSQL
- Ids, vectors, metadata consistent
- No orphaned Qdrant points
- No missing PostgreSQL facts

**Validate:**
- Qdrant has all facts ✓
- No orphans ✓
- Metadata correct ✓

---

## Forbidden Changes

**DO NOT:**
- Refactor any existing code (even if "cleaner")
- Add optimizations (materialized views, indexes, etc.)
- Modify migrations (all are locked)
- Add new features beyond testing
- Change database schema
- Touch ingest logic, WGM gate, fact classification, or re-embedder
- Rewrite query path (graph + hierarchy already done)

**Testing only:** Add test files, run existing tests, document results.

---

## Test Implementation

**Use existing test framework:** `pytest tests/api/test_query_compound.py` as template.

Create new test file: `tests/api/test_dprompt29_comprehensive.py`

Test structure:
```python
def test_scenario_1_basic_graph_hierarchy():
    # Setup fixtures
    # Ingest test data
    # Query
    # Assert results

def test_scenario_2_novel_rel_type():
    # ...
```

Run suite:
```bash
pytest tests/api/test_dprompt29_comprehensive.py -v
```

Expected: All scenarios pass, no regressions in existing tests.

---

## Success Criteria

- All 8 scenarios pass ✓
- Existing test suite passes (109+) ✓
- No regressions ✓
- No crashes on edge cases ✓
- Performance acceptable (queries < 500ms) ✓
- Code parses cleanly ✓
- Qdrant reconciliation clean ✓
