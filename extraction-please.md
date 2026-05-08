# FaultLine Query Retrieval Failure Analysis

## Problem Statement
User stores "My home address is 156 Cedar St. S, Kitchener, ON" successfully. Later asking "Where do I live?" returns no memory. The fact is ingested (200 OK), but not retrieved.

## Root Cause: Parameter Binding Bug in `_fetch_user_facts()`

**Location**: `src/api/main.py`, lines 1270-1326

**The Bug**: The function builds a UNION query across `facts` and `staged_facts` tables but reuses the same params list for both query parts. 

```
WHERE facts.user_id = %s AND ... rel_type IN (...) AND (subject = %s OR object = %s)
UNION ALL  
WHERE staged_facts.user_id = %s AND ... rel_type IN (...) AND (subject = %s OR object = %s)
```

With only ONE copy of params `[user_id, ...rel_types, entity_id, entity_id]` for TWO queries, psycopg2 either:
1. Runs out of params and fails (caught silently, returns [])
2. Misaligns params to placeholders

**Result**: `baseline_facts` is empty, `lives_at` fact is never returned to the Filter, user's memory is lost.

## The Fix

Split into two separate queries with independent params:

1. Execute facts query with params = `[user_id, ...rel_types, entity_id, entity_id]`
2. Execute staged_facts query with separate params = `[user_id, ...rel_types, entity_id, entity_id]`  
3. Merge results in Python (simple append loop)

This avoids UNION parameter binding issues entirely. The two queries run sequentially but cost is negligible (query is indexed).

## Evidence

- Logs show `/ingest` returns 200 OK (fact stored)
- `/query` returns only 3 identity facts (also_known_as, pref_name, pref_name)
- No `lives_at` fact despite being in `_BASELINE_RELS`
- No `query.fetch_user_facts_failed` warning, so exception is NOT being raised—function returns `[]` without logging
- If params were sufficient, baseline_facts would contain identity facts AND location facts (since both are in `_BASELINE_RELS`)

## Implementation

Replace the UNION query in `_fetch_user_facts()` with two separate queries. Keep all filter logic identical; just deduplicate params construction for each query separately.

### Before (broken):
```python
query = "SELECT ... FROM facts WHERE ... UNION ALL SELECT ... FROM staged_facts WHERE ..."
cur.execute(query, params)  # params only cover first query!
```

### After (fixed):
```python
# First query
cur.execute(facts_query, facts_params)
# Collect results

# Second query  
cur.execute(staged_facts_query, staged_facts_params)
# Append results
```

---
## Testing

After fix:
1. Store "My home address is ..."
2. Query "Where do I live?" 
3. Verify `lives_at` fact appears in `/query` response
4. Verify Filter injects it into memory
5. Verify LLM sees it and answers correctly

---

## Architectural Recommendation: Explicit Fact State for Two-Tier Memory Model

FaultLine implements **tiered memory** by design:
- **Long-term (PostgreSQL)**: Validated Class A facts + promoted Class B facts (confidence >= 3 confirmations)
- **Short-term (Qdrant)**: Staged Class B/C facts, ephemeral context (expires, unpromoted)

However, the **retrieval response does not expose fact state**. The Filter cannot distinguish:
- "This fact is in long-term storage (use it)" 
- "This fact is staged and waiting for confirmation (provisional)"
- "This fact failed validation (ignore it)"
- "This fact expired (discard it)"

### Recommended Response Structure

Modify `/query` response to include `fact_state`:

```python
{
    "status": "ok",
    "facts": [
        {
            "subject": "...",
            "object": "...",
            "rel_type": "lives_at",
            "confidence": 0.6,
            "fact_state": "staged",        # ← NEW: explicitly mark tier
            "staged_confirmations": 1,     # ← NEW: progress toward promotion
            "promoted_at": null,           # ← NEW: None if not yet promoted
            "expires_at": "2026-06-08",    # ← NEW: when this fact expires
            "source": "user_inferred",
            "category": "location"
        }
    ],
    "preferred_names": {...},
    "canonical_identity": "chris"
}
```

### Filter Integration

The Filter can now implement **confidence-aware injection**:

```python
def _inject_memory_with_state(facts):
    long_term = [f for f in facts if f.get("fact_state") == "long_term"]
    staged = [f for f in facts if f.get("fact_state") == "staged"]
    ephemeral = [f for f in facts if f.get("fact_state") == "ephemeral"]
    
    # Always use long-term facts
    injected = long_term
    
    # Use staged facts as provisional (confidence reduced, label applied)
    for f in staged:
        f["confidence"] *= 0.7  # discount unconfirmed facts
        f["_note"] = f"(awaiting {3 - f['staged_confirmations']} more confirmations)"
        injected.append(f)
    
    # Skip ephemeral unless filling a gap
    if not long_term and not staged:
        injected.extend(ephemeral)
    
    return injected
```

### Query Path Implementation

Update `_fetch_user_facts()` and graph traversal to attach state:

```python
# For facts table
for row in facts_cursor:
    fact = {
        "subject": row[0],
        "object": row[1],
        "rel_type": row[2],
        "confidence": row[4],
        "fact_state": "long_term",        # ← All facts are long-term
        "promoted_at": row[5],             # from facts table
        "expires_at": None                 # facts don't expire
    }

# For staged_facts table
for row in staged_cursor:
    fact = {
        "subject": row[0],
        "object": row[1],
        "rel_type": row[2],
        "confidence": row[4],
        "fact_state": "staged",            # ← Explicitly staged
        "staged_confirmations": row[5],    # confirmed_count
        "promoted_at": None,
        "expires_at": row[6]               # from staged_facts table
    }
```

### Benefits

1. **Visibility**: Filter knows exact state of every fact
2. **Confidence**: Can adjust injection strategy based on maturity
3. **Debugging**: User can see why memory isn't "remembering" yet ("awaiting 2 more confirmations")
4. **Correctness**: Ephemeral facts never contaminate long-term memory
5. **Auditability**: Tracks when/if facts were promoted to authoritative status

This keeps the two-tier model's intent (validate before long-term storage) while making it explicit in the data flow.
