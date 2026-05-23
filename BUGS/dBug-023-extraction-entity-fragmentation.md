# dBug-023: Extraction Creates Orphaned Entities (Entity Fragmentation)

**Status:** ROOT CAUSE IDENTIFIED (dprompt-88d debugging)

**Severity:** HIGH — Facts created but unreachable by query pipeline

## Problem

Relationship facts (lives_at, spouse) are stored in database but invisible to /query because the subject entities lack identity anchors (pref_name/also_known_as).

### Evidence

**Database state for user ${TEST_USER_ID}:**

Entity a91f8c22-7deb-5d6a-951f-9be50b1b1e07:
- ✓ Has lives_at facts (54931c58, 6860bb69)
- ✓ Has spouse fact (fb0868c4)
- ✗ Has NO pref_name
- ✗ Has NO also_known_as
- ✗ Unreachable by initial_user_facts query

Entity 54931c58-c891-5558-af89-31804663b971 (address):
- ✓ Has instance_of location hierarchy fact
- ✗ Has NO pref_name (orphaned)
- ✗ Unreachable

**Query behavior:**
```sql
SELECT DISTINCT subject_id FROM facts 
WHERE rel_type IN ('pref_name', 'also_known_as') 
-- Returns: 10d7d879, fbb0eca9, fb0868c4, 2e0d4a79
-- Missing: a91f8c22 (has lives_at but no identity)
```

**Result:** initial_user_facts count=15 inclualice spouse facts but NO lives_at, because a91f8c22 never picked up in initial query.

## Root Cause

Extraction creates multiple Person entities during processing:
1. 10d7d879 (OpenWebUI user_id) — gets some identity facts
2. a91f8c22 — gets created for relationship facts, no identity anchor
3. fbb0eca9, fb0868c4, 2e0d4a79 — other entities with pref_name

When extraction sees "I live at X", it should anchor the lives_at fact to the PRIMARY identity entity (the one with pref_name="${USER}"). Instead, it creates a new ephemeral entity a91f8c22 and orphans it.

## Impact

- "where do I live?" query returns instance_of location but not lives_at
- lives_at facts exist in database (confirmed via psql) but never surfaced to LLM
- dprompt-88d (fetch all identity entities) only helps if those entities have identity facts
- Data fragmentation grows with each extraction

## Files to Modify

- `openwebui/faultline_tool.py` — extraction rewrite logic (identify pronoun resolution strategy)
- `src/api/main.py` — /extract endpoint pronoun handling
- Possibly: entity registry integration

## Solution Direction

When extraction processes "I live at X":
1. Query entity_aliases for user_id, find entity with confirmed pref_name
2. Attach lives_at to THAT entity (identity-anchored)
3. Do NOT create new entities for pronouns without giving them identity facts

Alternative: Filter pre-resolves pronouns and passes canonical entity UUID to /extract.

## Related

- dBug-022: Preferred fact exposure (different issue, but related to data structure)
- dprompt-88d: Query fetch all identity entities (symptom relief, not root fix)
