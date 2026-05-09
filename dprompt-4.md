# Deepseek Implementation Prompt: Relational Reference Resolver

**Scope:** Implement the `rel_index` pattern to resolve "my wife", "my pet", "my son" → entity UUID in Tier 1 entity extraction.

**Why:** Foundation for conversation state awareness (Phase 5). Once relational references resolve, pronouns in turn 3 ("she") can be tracked back to "my wife" in turn 1. Do this first.

---

## The problem

Current Tier 1 only matches entity display names directly:
- Query: "How's my wife?" → extracts nothing (no entity named "my" or "wife" in preferred_names)
- Falls to Tier 2 (identity fallback) instead of returning spouse-specific facts

Solution: walk the facts from `/query` to resolve relations → entities.

---

## Implementation

### Add relation resolver to `_extract_query_entities()`

Modify the function signature and add a relation-resolution step:

```python
def _extract_query_entities(query: str, preferred_names: dict, facts: list[dict] = None) -> set[str]:
    """
    Extract entity names from query via token matching + relational resolution.
    
    preferred_names: {uuid: "display_name"}
    facts: from /query response, used for relation walking
    
    Returns: set of matched entity display names (not UUIDs)
    """
    entities = set()
    query_lower = query.lower()
    query_tokens = query_lower.split()
    
    # Tier 1a: Direct token match against preferred_names
    for token in query_tokens:
        # Strip punctuation for matching
        clean_token = token.strip('.,!?;:')
        if clean_token in preferred_names:
            entities.add(preferred_names[clean_token])
    
    # Tier 1b: Relation resolution ("my wife", "my pet", etc.)
    if facts:
        # Build relation index: rel_type -> {subject: [objects]}
        rel_index = {}
        for fact in facts:
            rel_type = fact.get("rel_type", "")
            subject = fact.get("subject", "")
            obj = fact.get("object", "")
            if rel_type and subject and obj:
                if rel_type not in rel_index:
                    rel_index[rel_type] = {}
                if subject not in rel_index[rel_type]:
                    rel_index[rel_type][subject] = []
                rel_index[rel_type][subject].append(obj)
        
        # Map "my X" patterns to rel_type + subject
        relations_map = {
            "wife": ("spouse", "user"),
            "husband": ("spouse", "user"),
            "spouse": ("spouse", "user"),
            "son": ("parent_of", "user"),
            "daughter": ("parent_of", "user"),
            "child": ("parent_of", "user"),
            "children": ("parent_of", "user"),
            "pet": ("has_pet", "user"),
            "dog": ("has_pet", "user"),
            "cat": ("has_pet", "user"),
            "parent": ("child_of", "user"),
            "mom": ("child_of", "user"),
            "mother": ("child_of", "user"),
            "dad": ("child_of", "user"),
            "father": ("child_of", "user"),
            "sibling": ("sibling_of", "user"),
            "brother": ("sibling_of", "user"),
            "sister": ("sibling_of", "user"),
        }
        
        # Detect "my X" patterns and resolve
        for token in query_tokens:
            clean_token = token.strip('.,!?;:').lower()
            if clean_token in relations_map:
                rel_type, subject = relations_map[clean_token]
                # Lookup: rel_index[rel_type][subject] → list of objects
                if rel_type in rel_index and subject in rel_index[rel_type]:
                    for obj_entity in rel_index[rel_type][subject]:
                        # obj_entity might be UUID or display name; prefer display name
                        if obj_entity in preferred_names:
                            entities.add(preferred_names[obj_entity])
                        else:
                            entities.add(obj_entity)
    
    return entities
```

### Update call site in `inlet()`

Where `_extract_query_entities()` is called (around line 1191), pass the facts:

```python
# OLD:
typed_entities = await self._fetch_entities(clean_text, user_id)
raw_triples = await rewrite_to_triples(...)

# NEW:
typed_entities = await self._fetch_entities(clean_text, user_id)
entities = _extract_query_entities(clean_text, preferred_names, facts=raw_facts_for_extraction)
raw_triples = await rewrite_to_triples(...)
```

Then in `_filter_relevant_facts()`, use the extracted entities for Tier 1 matching.

---

## Test cases

1. **"How's my wife?"** → resolves to "mars" → returns all Mars facts
2. **"Tell me about my pet"** → resolves to "fraggle" → returns all Fraggle facts + species attribute
3. **"How old is my son?"** → resolves to child UUID → returns age facts
4. **"My daughter's birthday?"** → resolves to child UUID → returns born_on facts
5. **"What does my sibling do?"** → resolves to sibling UUID → returns occupation/works_for facts

---

## Constraints

- ✅ Single-file change (faultline_tool.py)
- ✅ No new endpoints or database changes
- ✅ Facts list is bounded (~20-50 items) — O(n) cost is negligible
- ❌ No docker operations
- ❌ No new dependencies

---

## Done when

- ✅ `rel_index` dict built once per call to `_extract_query_entities()`
- ✅ "my X" patterns resolve to entities via relation walking
- ✅ Test 5 relational queries manually in OpenWebUI
- ✅ No regressions on direct entity queries ("How's Mars?")
- ✅ Fallback to Tier 2/3 when relation doesn't exist (graceful)

Ship it.

---

## Note: Foundation for Phase 5

Once this lands, conversation state awareness (Phase 5) can track:
- Turn 1: User says "my wife's name is marla"
- Turn 2: System stores (user, spouse, marla)
- Turn 3: User says "she loves gardening"
- Phase 5 will resolve "she" → marla via relation history

This resolver makes that possible.
