# dBug-031: Wrong Canonical User Identity Selected When Unknown/Animal Types Have pref_name

**Status:** OPEN (marked as FIXED in earlier context, but code review shows NOT FIXED)  
**Priority:** P1 (blocks user query results)  
**Date:** Created 2026-05-16 (retroactively during dBug-033A investigation)

---

## Problem Statement

When a user asks "What is my name?" or "tell me about my family", the system returns wrong identity. Instead of the user (john/chris), it returns "dog" as the preferred identity.

**Observed behavior:**
```
User: "tell me about my family"
System canonical identity: "dog" (unknown-type entity)
System response: Returns family facts scoped to "dog", not to "chris"
Expected: Returns facts scoped to "chris" (Person entity)
```

**Root cause:** In `/query` endpoint's identity resolution (lines 3821-3838 in src/api/main.py), the code queries for all entities with `pref_name`/`also_known_as` facts for the user, then picks the **first one returned by the database** without filtering by entity_type.

When an unknown/Animal entity accidentally gets a pref_name fact (e.g., "dog"), the database may return it first, and `/query` uses it as the canonical identity instead of the Person entity.

---

## Database State Example

```sql
-- Multiple entities with pref_name for same user:
d807ffea-0140-5c9a-b312-930f964d469d | type=unknown | pref_name='dog'      ← WRONG (picked first)
10d7d879-63cd-4f31-92ce-f2c9edb760ab | type=Person  | pref_name='chris'     ← CORRECT (should pick this)
efc8ea62-381a-5859-b7c3-e2588c89bba6 | type=Person  | pref_name='chris'     ← Also correct
f119510d-8f7a-5843-8d72-bdffea35a538 | type=Person  | pref_name='john'
```

The query at line 3821-3826 returns these in database order (arbitrary), not by entity_type or confidence.

---

## Why This Breaks

1. **Identity is authoritative for graph traversal:** `/query` uses `canonical_identity` to fetch facts about the user
2. **If identity is wrong, all facts are wrong:** Facts about "dog" entity are fetched instead of user facts
3. **User sees fragmented data:** Name queries, family queries, all return wrong context
4. **Graph traversal starts from wrong entity:** Connected entities fetched from "dog" instead of "chris"

---

## Solution

Modify identity resolution in `/query` to **prioritize Person entities** when multiple identity entities exist:

```python
# Line 3821-3826 in src/api/main.py
cur.execute(
    "SELECT DISTINCT subject_id FROM facts f "
    "WHERE f.user_id = %s AND f.rel_type IN ('pref_name', 'also_known_as') "
    "AND f.superseded_at IS NULL "
    "ORDER BY (SELECT entity_type FROM entities WHERE id = f.subject_id) = 'Person' DESC, "  # ← Prioritize Person
    "       f.confidence DESC, f.created_at DESC",  # ← Then confidence/recency
    (user_id,)
)
user_entity_ids_for_query = [row[0] for row in cur.fetchall()]

# Line 3838: Pick the first (now guaranteed to be Person if one exists)
user_entity_id_for_query = user_entity_ids_for_query[0] if user_entity_ids_for_query else None
```

---

## Success Criteria

- ✓ Query returns correct Person entity as canonical identity
- ✓ "tell me about my family" returns user's pref_name (chris/john), not "dog"
- ✓ Graph traversal fetches correct user facts (spouse, children, etc.)
- ✓ Multiple Person entities still supported, but Person entities prioritized over others
- ✓ Identity logs show: `query.user_identity canonical=chris` (not `canonical=dog`)

---

## Files to Modify

- `src/api/main.py` (lines 3821-3838) — modify identity resolution query to prioritize Person entities

---

## Test Case

```bash
# Setup: User entity exists with pref_name='chris' (type=Person)
#        Unknown entity exists with pref_name='dog' (type=unknown)

curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer <token>" \
  -d '{"model": "faultline-test", "messages": [{"role": "user", "content": "tell me about my family"}]}'

# Expected: Response inclualice facts scoped to "chris" Person entity
# Actual (bug): Response inclualice facts scoped to "dog" unknown entity
```

---

## Investigation Notes

- This bug was supposedly fixed in earlier context ("✓ dBug-031 FIXED")
- However, code review reveals the fix was never actually applied
- Re-discovered as dBug-033 regression during dprompt-92 testing (2026-05-16)
- Root cause: Missing ORDER BY clause in identity resolution query (line 3821)
