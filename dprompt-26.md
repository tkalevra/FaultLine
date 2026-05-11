# dprompt-26 — ARCHITECTURE CLARIFICATION: Graph + Hierarchy Model (NOT Nested Scope Layers)

## 🛑 STOP: This Cancels dprompt-24/25 Code. REVERT FIRST.

**dprompt-24/25 implementations must be DELETED before proceeding.**

See scratch.md for explicit revert instructions:
1. Remove all layer-related code from src/api/main.py
2. Revert FactStoreManager.commit() changes in src/fact_store/store.py
3. Do NOT run migration 020_nested_layers.sql
4. DELETE all _REL_TYPE_LAYER dict usage
5. DELETE all _detect_layer_intent() function
6. DELETE all cascade query logic

**Then read this document to understand the correct architecture.**

---

## CRITICAL: Reframe Before Building dprompt-27/28

**dprompt-24/25 as implemented use a SCOPE LAYER model** (layer 1/2/3/4 constrain which facts appear in which queries). That is **NOT the intended architecture.**

**Intended architecture: GRAPH + HIERARCHY.**

---

## The Two Models (Why They're Different)

### Current Implementation (dprompt-24/25): Nested Scope Layers

```
Query: "tell me about my family"
→ entry_layer = 2
→ fetch facts WHERE layer <= 2
→ return: spouse, children, pets (facts marked layer 2)
```

**Problem:** Family isn't a "scope filter" — it's a **category that entities belong to.**

### Correct Model: Graph + Hierarchy

```
Query: "tell me about my family"
→ GRAPH traversal: find who I'm connected to (spouse, has_pet relations)
→ HIERARCHY expansion: recognize they're all instance_of Person/Animal, subclass_of Family
→ return: entities + their composition + their categories
```

---

## Architecture (Correct)

### Two Orthogonal Traversal Systems

**1. GRAPH (Connectivity) — for RELEVANCE**
- What entities matter for this query?
- Rel_types: spouse, parent_of, child_of, sibling_of, has_pet, knows, works_for, friend_of, etc.
- Direction: follows relationship chains (1-2-3 hops)
- Purpose: "Who am I connected to?"

**2. HIERARCHY (Composition + Classification) — for DETAILS**
- What is each entity made of / classified as?
- Rel_types: part_of, instance_of, subclass_of, composed_of, etc.
- Direction: moves up/down taxonomic chains
- Purpose: "What IS this thing? What are its details?"

### Query Flow (Correct)

```
1. Graph traversal: find relevant entities
   "Where do Mars and Fraggle live?"
   → Mars (spouse relation)
   → Fraggle (has_pet relation via Mars)

2. Hierarchy expansion: get composition + classification
   → Mars: instance_of Person → subclass_of Family
   → Fraggle: instance_of Morkie → subclass_of Dog → subclass_of Animal

3. Graph + hierarchy combine: find details
   → Mars lives_at "156 Cedar St" (graph: location fact)
   → Fraggle lives_at "156 Cedar St" (inherited from Mars or direct)
   
4. Result: "Family at 156 Cedar St: Mars (Person), Fraggle (Dog/Morkie)"
```

**Family is discovered through hierarchy** (instance_of Person/Animal, subclass_of Family), **not imposed as a scope filter.**

---

## Why This Matters

### Current Implementation (Scope Layers):
- Hardcodes "Family is layer 2"
- Query logic: "if layer 2, return layer 2 facts"
- Brittle: adding new categories means changing layer assignments
- Not expansive: Family is a filter, not a rich category

### Correct Implementation (Graph + Hierarchy):
- Family is a category entity that other entities belong to (via instance_of/subclass_of)
- Query logic: "follow graph edges, then enrich with hierarchy edges"
- Extensible: add new categories by creating entities + rel_types, no code change
- Expansive: hierarchy chains give you context (Dog → Canine → Mammal → Animal)

---

## What dprompt-24/25 Did Wrong

**dprompt-24** added `layer` columns to facts/entities and implemented `_detect_layer_intent()` to map queries to scope layers.

**dprompt-25** fixed the bug where facts weren't getting layer assignments.

**Both assume scope layers are the filtering mechanism.**

---

## What Needs to Change

### 1. Redefine `_REL_TYPE_LAYER` as `_REL_TYPE_SYSTEM`

Instead of:
```python
_REL_TYPE_LAYER = {
    "spouse": 2,
    "age": 1,
    ...
}
```

Define:
```python
_REL_TYPE_SYSTEM = {
    # GRAPH edges (connectivity)
    "spouse": "graph",
    "parent_of": "graph",
    "child_of": "graph",
    "has_pet": "graph",
    "knows": "graph",
    "works_for": "graph",
    
    # HIERARCHY edges (composition)
    "part_of": "hierarchy",
    "instance_of": "hierarchy",
    "subclass_of": "hierarchy",
    "composed_of": "hierarchy",
}
```

Plus directionality/steps:
```python
_REL_TYPE_TRAVERSAL = {
    "spouse": {"system": "graph", "direction": "bidirectional", "steps": 1},
    "part_of": {"system": "hierarchy", "direction": "down", "steps": 1},
    "instance_of": {"system": "hierarchy", "direction": "up", "steps": 1},
    "subclass_of": {"system": "hierarchy", "direction": "up", "steps": 1},
}
```

### 2. Rewrite `/query` Logic

Remove `_detect_layer_intent()` + cascade logic.

Implement:
```python
def query(request):
    # 1. Graph traversal: find relevant entities
    relevant_entities = graph_traversal(user_id, request.text, hops=2)
    
    # 2. Hierarchy expansion: get composition + classification
    enriched = []
    for entity in relevant_entities:
        details = hierarchy_expand(entity, direction="up", depth=3)
        enriched.append({entity, details})
    
    # 3. Fetch all facts for enriched entities
    facts = fetch_facts_for_entities(enriched)
    
    return facts
```

### 3. Keep `layer` Columns but Repurpose Them

`layer` can still exist, but it means:
- **layer = graph distance from user** (0 = self, 1 = direct relation, 2 = 2-hops, etc.)
- NOT "scope for filtering"

Or drop them entirely if not needed.

### 4. Don't Deploy dprompt-24/25 Yet

The bug fix (dprompt-25) is correct, but the cascade logic (dprompt-24) needs to be reconceptualized.

---

## Migration Path

1. **Keep dprompt-25 fix** — facts do need layer assignment, but change the meaning
2. **Rethink dprompt-24** — instead of scope layers, implement graph + hierarchy traversal
3. **Rewrite /query** — dual traversal (graph then hierarchy)
4. **Test** — "where do mars and fraggle live?" returns both + family category

---

## The Beautiful Part

Once this is right, the system is:
- **Simpler** (two clear concerns: graph for connectivity, hierarchy for details)
- **Extensible** (add categories as entities, not as code changes)
- **Content-expansive** (hierarchy chains automatically included)
- **Cleaner** (no "scope layer" confusion, just follow edges)

This is the architecture that makes sense. dprompt-24/25 as written don't get there.

---

## Timeline

- **DO NOT deploy dprompt-24/25** as currently implemented
- Redesign query logic (dprompt-27)
- Implement hierarchy traversal (dprompt-28)
- Test + deploy

**Why this matters:** Deploying scope layers now will require ripping out code later. Better to get it right from the start.
