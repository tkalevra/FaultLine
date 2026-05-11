# dprompt-27 — Query Redesign: Graph Traversal + Hierarchy Expansion

## Architecture (from dprompt-26)

Two orthogonal traversal systems replace the scope-layer model:

**GRAPH** — Connectivity. "Who am I connected to?"
- Rel_types: spouse, parent_of, child_of, sibling_of, has_pet, knows, works_for, friend_of, lives_at, lives_in, located_in, owns
- Direction: follows relationship chains 1–2 hops from user
- Purpose: find relevant entities for the query

**HIERARCHY** — Composition + Classification. "What IS each entity? What are its details?"
- Rel_types: instance_of, subclass_of, part_of
- Direction: moves UP taxonomic chains (e.g., Morkie → Dog → Animal)
- Purpose: enrich entities with taxonomy context; enable transitive reasoning

## Query Flow (Revised)

```
/query receives user text
  │
  ├─ 1. Graph traversal: find connected entities
  │     "where do mars and fraggle live?"
  │     → mars (spouse: direct connection to user)
  │     → fraggle (has_pet via mars, then 1 hop from user via mars?)
  │     → lives_at facts for both
  │
  ├─ 2. Hierarchy expansion: enrich with classification
  │     mars instance_of Person → subclass_of FamilyMember
  │     fraggle instance_of Dog → subclass_of Animal → subclass_of Pet
  │
  ├─ 3. Fetch facts for enriched entity set
  │     All facts where subject_id or object_id in enriched_entities
  │
  └─ 4. Deduplicate, relevance-score, return
```

## `_REL_TYPE_SYSTEM` — Replaces `_REL_TYPE_LAYER`

```python
_REL_TYPE_GRAPH = frozenset({
    # Direct connection to user
    "spouse", "parent_of", "child_of", "sibling_of",
    "has_pet", "knows", "friend_of", "met",
    "works_for", "lives_at", "lives_in", "located_in",
    "owns", "educated_at", "member_of",
    # Self-referential
    "pref_name", "also_known_as", "same_as",
    "age", "height", "weight", "born_on", "nationality",
    "has_gender", "occupation",
})

_REL_TYPE_HIERARCHY = frozenset({
    "instance_of", "subclass_of", "part_of", "is_a",
})
```

## Graph Traversal (`_graph_traverse`)

```
Input: user_id, entity_id (the user), max_hops=1
Output: set of connected entity UUIDs

Algorithm:
1. Start with {user_id}
2. For each entity in frontier:
   - Query facts WHERE (subject_id = entity OR object_id = entity)
     AND rel_type IN _REL_TYPE_GRAPH
   - Collect all connected entity IDs
3. Return accumulated set
```

Single-hop by default (covers spouse, children, pets, location).
Optional 2-hop for "tell me about my family" style queries.

## Hierarchy Expansion (`_hierarchy_expand`)

```
Input: entity_id, direction="up", max_depth=3
Output: set of entity UUIDs in the hierarchy chain

Algorithm:
1. Start with {entity_id}
2. For each entity in frontier:
   - Query facts WHERE subject_id = entity AND rel_type IN _REL_TYPE_HIERARCHY
   - direction="up": follow object_ids (entity → instance_of → Person)
   - direction="down": follow subjects (Person → subclass_of → ???)
3. Return chain from entity to root
```

## Query Intent Detection (replaces `_detect_layer_intent`)

Not needed as a separate step — graph traversal naturally surfaces relevant entities for any query.
But we do need to decide:
- **Self-referential** ("tell me about me", "who am i") → 0-hop (just user facts)
- **Family/relationships** ("my family", "where does mars live") → 1-hop graph
- **Domain-agnostic** ("tell me about the system") → named entity lookup + Qdrant

Signal sets remain as-is (`_SELF_REF_SIGNALS`, `_ATTRIBUTE_SIGNALS`, `_GENERIC_SELF_REF_SIGNALS`).

## Implementation Steps

1. Add `_REL_TYPE_GRAPH` and `_REL_TYPE_HIERARCHY` frozensets to main.py
2. Add `_graph_traverse()` function — single-hop graph traversal
3. Add `_hierarchy_expand()` function — up-chain hierarchy expansion
4. Rewrite `/query` loop:
   - Replace cascade logic with: graph traverse → hierarchy expand → fetch facts
   - Keep existing baseline facts, Qdrant search, named entity resolution
5. Keep `_fetch_user_facts()` UNION helper (already handles facts + staged_facts)

## File Impact

| File | Change |
|------|--------|
| `src/api/main.py` | Add `_REL_TYPE_GRAPH`, `_REL_TYPE_HIERARCHY`, `_graph_traverse()`, `_hierarchy_expand()`; rewrite query dispatch logic |
| `dprompt-27.md` | This spec |
| `dprompt-28.md` | Hierarchy traversal implementation details (separate spec for deeper traversal patterns) |
