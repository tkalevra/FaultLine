# dprompt-23 — Taxonomy-Driven Query Intent: Use Family/Household Taxonomies to Resolve "Family"

## Problem

Spouse fact now returns after UUID/alias bug fix, but pets from staged_facts are still not appearing in family queries.

**Test scenario:**
- User: 3f8e6836-72e3-43d4-bbc5-71fc8668b070 (chris)
- Query: "tell me about my family"
- Expected: "Your family includes Mars (spouse) and Fraggle (pet)"
- Actual: "Your family includes Mars"
- Data verified: has_pet(mars, fraggle) IS in staged_facts with confirmed_count=1 ✓

**Architectural insight:** "family" should NOT be a hardcoded signal. Use the entity_taxonomies table:
- Query intent "family" → load taxonomy_name="family" from DB
- Extract rel_types_defining_group: [spouse, parent_of, child_of, sibling_of]
- Apply transitive rules: household taxonomy includes has_pet as transitive
- Fetch all matching facts (staged + committed)
- Return: spouse + transitive pets

## Root Cause: /query Not Using Taxonomy-Driven Intent

### Current behavior (hardcoded)
Query "tell me about my family" → matches `_SELF_REF_SIGNALS` hardcoded pattern → triggers graph traversal → returns spouse only

### Expected behavior (taxonomy-driven)
Query "tell me about my family" → recognize intent maps to taxonomy_name="family" → load from entity_taxonomies:
- rel_types_defining_group: [spouse, parent_of, child_of, sibling_of]
- transitive_rel_types: [lives_in, works_for]
→ Fetch all facts with those rel_types
→ For each member, apply household taxonomy to fetch transitive members via has_pet
→ Return: spouse + pet (transitive)

## Implementation Steps

### 1. Query Intent → Taxonomy Mapping
**Location:** `src/api/main.py` `/query` endpoint

Add intent recognition:
```python
# Map query keywords to taxonomy_name
_QUERY_INTENT_TO_TAXONOMY = {
    "family": "family",
    "my family": "family",
    "tell me about my family": "family",
    "work": "work",
    "job": "work",
    "home": "household",
    "where do i live": "household",
}

def _detect_taxonomy_intent(query_text):
    text_lower = query_text.lower()
    for keywords, taxonomy_name in _QUERY_INTENT_TO_TAXONOMY.items():
        if keywords in text_lower:
            return taxonomy_name
    return None
```

### 2. Fetch Taxonomy Rules
```python
def _fetch_taxonomy_rel_types(db, taxonomy_name):
    """Load rel_types_defining_group from entity_taxonomies table."""
    cursor = db.cursor()
    cursor.execute(
        "SELECT rel_types_defining_group, transitive_rel_types FROM entity_taxonomies WHERE taxonomy_name = %s",
        (taxonomy_name,)
    )
    row = cursor.fetchone()
    if row:
        return list(row[0]), list(row[1]) if row[1] else []
    return None, []
```

### 3. Use Taxonomy to Query Facts (Selective, Not Bulk-Load)
```python
def _fetch_facts_by_taxonomy(db, user_id, taxonomy_name):
    """
    Fetch ONLY facts matching this taxonomy's rel_types.
    Don't fetch all user facts then filter — fetch selectively based on intent.
    """
    rel_types, transitive_rels = _fetch_taxonomy_rel_types(db, taxonomy_name)
    if not rel_types:
        return []
    
    # IMPORTANT: Query ONLY facts with rel_types in this taxonomy
    # NOT: fetch all facts then filter
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM facts 
        WHERE user_id = %s AND rel_type = ANY(%s)
        UNION ALL
        SELECT * FROM staged_facts
        WHERE user_id = %s AND rel_type = ANY(%s)
    """, (user_id, rel_types, user_id, rel_types))
    direct_facts = cursor.fetchall()
    
    # For each direct member, fetch transitive members via transitive_rels
    all_facts = list(direct_facts)
    for fact in direct_facts:
        member_id = fact["object_id"]
        cursor.execute("""
            SELECT * FROM facts 
            WHERE user_id = %s AND subject_id = %s AND rel_type = ANY(%s)
            UNION ALL
            SELECT * FROM staged_facts
            WHERE user_id = %s AND subject_id = %s AND rel_type = ANY(%s)
        """, (user_id, member_id, transitive_rels, user_id, member_id, transitive_rels))
        transitive_facts = cursor.fetchall()
        all_facts.extend(transitive_facts)
    
    return all_facts
```

**Key difference:**
- Don't call generic `_fetch_user_facts()` that returns everything
- Query directly for `rel_type = ANY(taxonomy.rel_types_defining_group)`
- This is fast, semantically correct, and distinguishes intent

### 4. Integration in /query
```python
taxonomy_name = _detect_taxonomy_intent(text)
if taxonomy_name:
    facts = _fetch_facts_by_taxonomy(db, user_id, taxonomy_name)
else:
    # Fall back to baseline facts
    facts = _fetch_user_facts(db, user_id)
```

## Files to Change

- `src/api/main.py` — Add intent mapping, taxonomy fetching, taxonomy-driven fact retrieval
- Remove or deprecate `_SELF_REF_SIGNALS` — no longer needed (taxonomy system replaces hardcoding)

## Why This Matters

This is the architectural payoff of dprompt-20. The system should:
- ✅ Define grouping rules in entity_taxonomies (no hardcoding)
- ✅ Query intent determines taxonomy, taxonomy determines data fetch (selective, not bulk)
- ✅ Different queries = different data needs: "tell me about myself" vs "tell me about my family" fetch different taxonomies
- ✅ Scale indefinitely (new taxonomies = new capabilities, no code change)

Not:
- ❌ Hardcode "family" signals
- ❌ Fetch ALL user facts then filter by taxonomy (wasteful)
- ❌ Use generic fact-retrieval that doesn't distinguish intent
- ❌ Require code change for each new grouping

**Core principle:** Query intent → taxonomy selection → selective fact fetch. Don't bulk-load and filter.

## Test After Fix

```
Query: "tell me about my family"
Expected: "Your family includes Mars (spouse) and Fraggle (pet via household taxonomy)"
```

Repeat with other taxonomies:
```
Query: "tell me about my work"
Expected: [work-related facts via work taxonomy]
```

## Files to Check/Fix

- `src/api/main.py` — `_fetch_user_facts()`, `_fetch_transitive_members()`, `_SELF_REF_SIGNALS`
- `openwebui/faultline_tool.py` — `_filter_relevant_facts()`, `_CAT_SIGNALS`, `calculate_relevance_score()`
- `migrations/019_entity_taxonomies.sql` — verify household taxonomy has_pet in transitive_rel_types

## Test After Fix

```
New chat: "tell me about my family"
Expected: "Your family includes Mars (spouse) and Fraggle (pet via household taxonomy)"
```

Then test pets explicitly:
```
New chat: "any pets?"
Expected: "Yes, Mars has a dog named Fraggle"
```

## Why This Matters

Staged facts (Class B) should be immediately visible in query/retrieval, not blocked until 3+ confirmations. They're the temporary buffer for behavioral/contextual facts. If `/query` doesn't return them, the whole staging system is broken — only Class A (identity) facts would be retrievable.

This is the final blocker before dprompt-20 (taxonomies) is proven working end-to-end.
