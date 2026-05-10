# dprompt-28 — Hierarchy Traversal: Composition + Classification Expansion

## Purpose

Implement depth-first hierarchy traversal to enrich entities with their taxonomic context. Complements dprompt-27's graph traversal (connectivity) by providing composition and classification details.

## Architecture (from dprompt-26/27)

**HIERARCHY** — Composition + Classification. "What IS each entity? What are its details?"
- Rel_types: `instance_of`, `subclass_of`, `part_of`, `is_a`
- Direction: UP (entity → class → parent class), DOWN (class → members), SIDEWAYS (part_of relationships)
- Purpose: enrich entities with taxonomy context; enable transitive reasoning

## What dprompt-27 expects from _hierarchy_expand()

From dprompt-27 spec (lines 76–89):

```python
def _hierarchy_expand(entity_id, direction="up", max_depth=3) -> set[UUID]:
    """
    Input: entity_id, direction="up", max_depth=3
    Output: set of entity UUIDs in the hierarchy chain
    
    Algorithm:
    1. Start with {entity_id}
    2. For each entity in frontier:
       - Query facts WHERE subject_id = entity AND rel_type IN _REL_TYPE_HIERARCHY
       - direction="up": follow object_ids (entity → instance_of → Person)
       - direction="down": follow subjects (Person → subclass_of → ???)
    3. Return chain from entity to root
    """
```

## Implementation Details

### Upward Traversal (entity → classification)

**Use case:** "Fraggle is a Morkie, Morkie is a Dog, Dog is an Animal"

```
_hierarchy_expand("fraggle-uuid", direction="up", max_depth=3)

Query: SELECT object_id FROM facts 
       WHERE subject_id = 'fraggle-uuid' 
         AND rel_type IN ('instance_of', 'subclass_of')
       
→ fraggle_instance_of_morkie → object_id = 'morkie-uuid'
→ morkie_subclass_of_dog → object_id = 'dog-uuid'
→ dog_subclass_of_animal → object_id = 'animal-uuid'
→ animal_subclass_of_organism → object_id = 'organism-uuid' (stop at max_depth=3)

Return: {fraggle-uuid, morkie-uuid, dog-uuid, animal-uuid}
```

### Downward Traversal (class → members)

**Use case:** "Who are all my family members?" (Family is a class; find all members)

```
_hierarchy_expand("family-uuid", direction="down", max_depth=2)

Query: SELECT subject_id FROM facts 
       WHERE object_id = 'family-uuid' 
         AND rel_type IN ('instance_of', 'subclass_of')

→ person_instance_of_family → subject_id = 'person-uuid'
→ person_subclass_of_family → subject_id = 'person-uuid'

Return: {family-uuid, person-uuid, ...}
```

### Sideways Traversal (composition chains)

**Use case:** "What does Mars contain?" (part_of relationships)

```
_hierarchy_expand("mars-uuid", direction="down", max_depth=2, rel_types={'part_of'})

Query: SELECT subject_id FROM facts 
       WHERE object_id = 'mars-uuid' 
         AND rel_type = 'part_of'

→ arm_part_of_mars → subject_id = 'arm-uuid'
→ hand_part_of_arm → subject_id = 'hand-uuid'

Return: {mars-uuid, arm-uuid, hand-uuid}
```

## Database Queries

### Upward chain (entity to root):

```sql
WITH RECURSIVE hierarchy_chain AS (
  -- Base: start with entity
  SELECT subject_id, object_id, rel_type, 1 AS depth
  FROM facts
  WHERE subject_id = %s
    AND rel_type IN ('instance_of', 'subclass_of', 'part_of')
    AND user_id = %s
    
  UNION ALL
  
  -- Recursive: follow object_id up the chain
  SELECT f.subject_id, f.object_id, f.rel_type, hc.depth + 1
  FROM facts f
  JOIN hierarchy_chain hc ON f.subject_id = hc.object_id
  WHERE f.rel_type IN ('instance_of', 'subclass_of', 'part_of')
    AND f.user_id = %s
    AND hc.depth < %s  -- max_depth
)
SELECT DISTINCT object_id FROM hierarchy_chain
UNION
SELECT %s  -- include the starting entity
```

### Downward chain (class to members):

```sql
WITH RECURSIVE hierarchy_chain AS (
  -- Base: start with class
  SELECT subject_id, object_id, rel_type, 1 AS depth
  FROM facts
  WHERE object_id = %s
    AND rel_type IN ('instance_of', 'subclass_of')
    AND user_id = %s
    
  UNION ALL
  
  -- Recursive: follow subject_id down
  SELECT f.subject_id, f.object_id, f.rel_type, hc.depth + 1
  FROM facts f
  JOIN hierarchy_chain hc ON f.object_id = hc.subject_id
  WHERE f.rel_type IN ('instance_of', 'subclass_of')
    AND f.user_id = %s
    AND hc.depth < %s  -- max_depth
)
SELECT DISTINCT subject_id FROM hierarchy_chain
UNION
SELECT %s  -- include the starting class
```

## Cycle Prevention

Hierarchy chains can have cycles (e.g., `A subclass_of B`, `B subclass_of A`). Use CTE depth tracking to prevent infinite recursion. Stop at `max_depth`.

## Integration with dprompt-27 Query Flow

In `/query`, after graph traversal finds relevant entities:

```python
# dprompt-27 graph traversal finds these entities
relevant_entities = _graph_traverse(user_id, max_hops=1)  # {mars, fraggle}

# dprompt-28 hierarchy expansion enriches each
enriched_entities = set()
for entity in relevant_entities:
    # Upward: find what class it belongs to
    upchain = _hierarchy_expand(entity, direction="up", max_depth=3)
    enriched_entities.update(upchain)
    
    # Downward: if it's a class, find members
    # (optional — depends on query intent)
    downchain = _hierarchy_expand(entity, direction="down", max_depth=2)
    enriched_entities.update(downchain)

# Fetch all facts for enriched entity set
facts = _fetch_user_facts_for_entities(enriched_entities, user_id)
```

## Test Scenarios

### Test 1: Single-entity upchain
```
Given: Fraggle UUID
Query: _hierarchy_expand(fraggle_uuid, direction="up", max_depth=3)
Expected: {fraggle, morkie, dog, animal}
```

### Test 2: Class downchain
```
Given: Family UUID (with members Mars, Cyrus, Desmonde as instance_of Family)
Query: _hierarchy_expand(family_uuid, direction="down", max_depth=2)
Expected: {family, mars, cyrus, desmonde}
```

### Test 3: Integration with graph traversal
```
Given: User ID, query "where do mars and fraggle live?"
Graph traversal: {mars, fraggle}
Hierarchy enrichment: {mars, person, fraggle, morkie, dog, animal}
Fetch facts for all 6 entities
Expected: lives_at facts for mars and fraggle + classification facts
```

## Files to Modify

| File | Change |
|------|--------|
| `src/api/main.py` | Add `_hierarchy_expand()` function; integrate with `/query` after graph traversal (called from dprompt-27's query loop) |

## Success Criteria

- `_hierarchy_expand(entity_id, direction="up", max_depth=N)` returns full classification chain
- `_hierarchy_expand(entity_id, direction="down", max_depth=N)` returns all members of a class
- CTE prevents infinite recursion on cycles
- Integration test: "where do mars and fraggle live?" returns both entities + full hierarchy context
- No performance regressions; indexes on `(user_id, rel_type, subject_id)` and `(user_id, rel_type, object_id)` exist
