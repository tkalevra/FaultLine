# Deepseek Implementation Prompt: Generic Relation Resolver + Conversation State Awareness

**Scope:** Two parts, one file change:

1. **Generic relation resolver** — replace hardcoded `_RELATION_MAP` with dynamic scanning of `rel_index` keys. "My X" resolves across any domain (personal, engineering, science, infrastructure).
2. **Conversation state awareness** — track entity references across conversation turns, resolve pronouns ("she", "it", "they") to previously mentioned entities.

**Why:** Phase 4's hardcoded patterns don't scale. Phase 5 makes the resolver domain-agnostic and adds pronoun resolution for multi-turn context.

---

## Part 1: Generic Relation Resolver

### Current state (Phase 4)
```python
relations_map = {
    "wife": ("spouse", "user"),
    "husband": ("spouse", "user"),
    "pet": ("has_pet", "user"),
    ...  # 22 hardcoded patterns
}
```

### New state (Phase 5)
```python
# Dynamic scanning over rel_index[rel_type]["user"]
# No hardcoding. If (user, manages, team) exists, "my team" resolves.
# If (user, depends_on, database) exists, "my database" resolves.
```

### Implementation

Update `_extract_query_entities()`:

```python
def _extract_query_entities(query: str, preferred_names: dict, facts: list[dict] = None) -> set[str]:
    """
    Extract entity names from query via token matching + dynamic relation resolution.
    """
    entities = set()
    query_lower = query.lower()
    query_tokens = query_lower.split()
    
    # Tier 1a: Direct token match
    for token in query_tokens:
        clean_token = token.strip('.,!?;:')
        if clean_token in preferred_names:
            entities.add(preferred_names[clean_token])
    
    # Tier 1b: Dynamic relation resolution ("my X" patterns)
    if facts:
        # Build rel_index: {rel_type: {subject: [objects]}}
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
        
        # For each query token, check if it matches any entity in rel_index["*"]["user"]
        # Dynamically resolve: "my X" → find X in any (user, rel_type, X) fact
        for token in query_tokens:
            clean_token = token.strip('.,!?;:').lower()
            if clean_token in ("my", "i", "me"):
                # Skip pronouns themselves
                continue
            
            # Check ALL rel_types for user → X relationships
            for rel_type, subjects in rel_index.items():
                if "user" in subjects:
                    for obj_entity in subjects["user"]:
                        # Check if clean_token matches the entity display name or UUID
                        display_name = preferred_names.get(obj_entity, obj_entity).lower()
                        if clean_token in display_name or display_name in clean_token:
                            # Match found: add entity
                            if obj_entity in preferred_names:
                                entities.add(preferred_names[obj_entity])
                            else:
                                entities.add(obj_entity)
    
    return entities
```

**Key robustness points:**
- No hardcoded patterns. Works for any domain.
- Graceful fallback: if no relation match, falls back to Tier 1a (direct token match)
- Returns all matches for ambiguous cases (Deepseek's insight: let Tier 1 scoring sort it)
- Single O(n) pass over facts

---

## Part 2: Conversation State Awareness

Track entity references across turns to resolve pronouns.

### New structure: Conversation context dict

In `inlet()`, maintain a **conversation context** across turns:

```python
# At the top of the Filter class or in inlet scope:
_CONVERSATION_CONTEXT = {
    user_id: {
        "entity_mentions": [],  # [(turn, entity_uuid, entity_name, rel_type), ...]
        "pronoun_map": {}       # {"she": entity_uuid, "it": entity_uuid, ...}
    }
}
```

### Pronoun resolution logic

After entity extraction (Tier 1), resolve pronouns:

```python
def _resolve_pronouns(query: str, context: dict) -> set[str]:
    """
    Resolve pronouns (she, he, it, they) to recently mentioned entities.
    Uses conversation history to map pronouns to UUIDs.
    """
    pronouns = {
        "she": ("female", "Person"),
        "he": ("male", "Person"),
        "it": ("neutral", ["Object", "Concept", "Location"]),
        "they": ("plural", "any"),
    }
    
    resolved = set()
    query_lower = query.lower()
    
    for pronoun, (gender, expected_type) in pronouns.items():
        if pronoun in query_lower:
            # Lookup pronoun_map in context
            if pronoun in context.get("pronoun_map", {}):
                resolved.add(context["pronoun_map"][pronoun])
    
    return resolved
```

### Context update logic

After `_build_memory_block()`, update context:

```python
def _update_conversation_context(user_id: str, entities: set[str], facts: list[dict]):
    """
    Update conversation context: track entity mentions, build pronoun map.
    Called after each inlet turn.
    """
    if user_id not in _CONVERSATION_CONTEXT:
        _CONVERSATION_CONTEXT[user_id] = {"entity_mentions": [], "pronoun_map": {}}
    
    ctx = _CONVERSATION_CONTEXT[user_id]
    
    # Add new entity mentions
    for entity in entities:
        # Find the entity's facts to infer gender/type
        for fact in facts:
            if fact.get("subject") == entity:
                rel_type = fact.get("rel_type", "")
                ctx["entity_mentions"].append((len(ctx["entity_mentions"]), entity, rel_type))
                
                # Simple pronoun mapping: most recent entity becomes "it"
                if rel_type not in ("pref_name", "also_known_as"):
                    ctx["pronoun_map"]["it"] = entity
                break
    
    # Prune old mentions (keep last 10 to avoid memory bloat)
    if len(ctx["entity_mentions"]) > 10:
        ctx["entity_mentions"] = ctx["entity_mentions"][-10:]
```

### Call site in `inlet()`

```python
# After memory injection:
if will_query and entities:
    _update_conversation_context(user_id, entities, facts)

# Before entity extraction in next turn:
pronoun_entities = _resolve_pronouns(clean_text, _CONVERSATION_CONTEXT.get(user_id, {}))
entities = _extract_query_entities(clean_text, preferred_names, facts)
entities.update(pronoun_entities)  # Merge pronoun and direct entity matches
```

---

## Robustness constraints

- ✅ No hardcoded patterns — scales across domains
- ✅ Graceful fallbacks — if pronoun resolution fails, entity extraction still works
- ✅ Bounded memory — keep only last 10 mentions per user
- ✅ Single O(n) pass — no performance degradation
- ✅ No new dependencies or database changes
- ✅ Pronoun map resets per conversation (avoid cross-conversation bleed)

---

## Test cases

### Generic relation resolver:
1. "How's my wife?" → resolves to spouse (if fact exists)
2. "Tell me about my server" → resolves to server entity (if `(user, owns, server)` exists)
3. "Status on my experiment?" → resolves to experiment (if `(user, runs, experiment)` exists)
4. "My foobar?" → no match → falls back to Tier 1a, then Tier 2

### Conversation state:
1. **Turn 1:** "My wife is Marla. She loves gardening."
   - Extract: entities = {marla}
   - Update context: pronoun_map["she"] = marla_uuid
2. **Turn 2:** "What does she do?"
   - Resolve "she" → marla_uuid
   - Extract entities = {marla} + {marla from pronoun resolution}
   - Return facts about marla

---

## Done when

- ✅ `_extract_query_entities()` does dynamic scanning (no hardcoded `_RELATION_MAP`)
- ✅ `_resolve_pronouns()` implemented
- ✅ `_update_conversation_context()` tracks mentions, builds pronoun map
- ✅ Call sites updated in `inlet()`
- ✅ 10/10 relevance tests pass (no regressions)
- ✅ Manual test: "My wife is marla. She loves gardening. What does she do?" → returns marla facts in turn 2
- ✅ Fallback gracefully when pronoun/entity not found
- ✅ Context pruning prevents memory bloat

Ship it.

---

## Notes

- Conversation context is in-memory per session. It resets when the user starts a new chat. This is intentional (fresh conversation, fresh context).
- Pronoun resolution is simple (most recent entity). Can be enhanced later with gender inference from facts.
- If a fact has gender info (has_gender: male), use that for pronoun→entity resolution. Fall back to "most recent" if not.
