# dBug-033A: Identity Resolution Investigation Report

**Status:** COMPLETE (root cause identified)  
**Date:** 2026-05-16  
**Investigator Notes:** dprompt-92 testing uncovered regression

---

## Executive Summary

**Root Cause:** Lines 3821-3826 in `src/api/main.py` query returns entities with identity facts in **arbitrary database order** with NO entity_type filtering. Unknown-type entity "dog" is returned first by the database, and line 3838 selects it without checking entity type.

**Missing Fix:** dBug-031 was supposedly fixed to prioritize Person entities, but the fix is NOT present in the current codebase. The identity resolution query has NO ORDER BY clause to prefer Person entities.

**Impact:** All `/query` responses use wrong user identity when multiple entities have identity facts (common scenario with unknown-type artifacts and Person entities).

---

## Code Analysis

### Current Problematic Code (lines 3821-3838)

```python
# Line 3821-3826: Query identity entities (NO entity_type filter, NO ORDER BY)
cur.execute(
    "SELECT DISTINCT subject_id FROM facts "
    "WHERE user_id = %s AND rel_type IN ('pref_name', 'also_known_as') "
    "AND superseded_at IS NULL",
    (user_id,)
)
user_entity_ids_for_query = [row[0] for row in cur.fetchall()]

# Line 3838: Pick first entity (database order, arbitrary)
user_entity_id_for_query = user_entity_ids_for_query[0] if user_entity_ids_for_query else None

# Line 3840: Get canonical identity
canonical_identity = registry.get_preferred_name(user_id, user_entity_id_for_query)
```

**Issues:**
1. Line 3822: `SELECT DISTINCT subject_id FROM facts` returns entities in PostgreSQL arbitrary order
2. Line 3838: No entity_type check — picks first returned, regardless of type
3. Result: "dog" (unknown-type) returned before "${USER}" (Person)

### Test Case Demonstrating Bug

**Database state:**
```sql
user_id: ${TEST_USER_ID}

Facts with pref_name:
- d807ffea-0140-5c9a-b312-930f964d469d | unknown | pref_name | dog
- ${TEST_USER_ID} | Person  | pref_name | ${USER}
- efc8ea62-381a-5859-b7c3-e2588c89bba6 | Person  | pref_name | ${USER}
```

**Current query result (arbitrary order):**
```
[d807ffea-0140-5c9a-b312-930f964d469d, 10d7d879-..., efc8ea62-...]
                    ↑ picked (wrong!)
```

**Logs from 2026-05-16 15:00:04:**
```
query.user_identity canonical=dog entity_id=d807ffea owui_user_id=10d7d879-...
```

---

## What dBug-031 Should Have Fixed

**dBug-031 Solution (from earlier context):**
> Modify identity resolution in `/query` to **prioritize Person entities** when multiple identity entities exist

**Recommended SQL (never applied):**
```python
cur.execute(
    "SELECT DISTINCT subject_id FROM facts f "
    "WHERE f.user_id = %s AND f.rel_type IN ('pref_name', 'also_known_as') "
    "AND f.superseded_at IS NULL "
    "ORDER BY (SELECT entity_type FROM entities WHERE id = f.subject_id) = 'Person' DESC, "
    "       f.confidence DESC, f.created_at DESC",
    (user_id,)
)
```

**This would ensure:**
1. Person entities sorted first (DESC → Person=TRUE=1, unknown=FALSE=0)
2. Person entities returned before unknown-type
3. First entity picked (line 3838) is always Person if one exists

---

## Why dBug-031 Fix Was Not Applied

**Hypothesis:** dBug-031 was documented but the code change was never committed. Evidence:
- No commit in git history mentioning "dBug-031"
- No BUGS/dBug-031 report file (only dBug-033 exists)
- scratch.md mentions "✓ dBug-031 FIXED" but code review shows it's not actually fixed
- Recent commits (dprompt-88c, dprompt-88d) fetch from multiple entities but don't prioritize Person

**Git history (filtered):**
```
0dfbf58 docs: dBug-013 decision + dprompt-71b — Fix Identity Facts Routing
074c789 fix: initialize db/registry/canonical_identity in /query graph traversal
eb1cf1f fix: Find user entity by identity facts, not registry.resolve (dprompt-88c)
0e14fa5 fix: Fetch facts from ALL user identity entities, not just first one (dprompt-88d)
```

None of these apply the entity_type filtering to prioritize Person.

---

## Correct Fix Required

**Location:** `src/api/main.py`, lines 3821-3826

**Change:** Add ORDER BY clause to prioritize Person entities

```python
with db.cursor() as cur:
    # dprompt-dBug-031-FIX: Prioritize Person entities over unknown-type
    # when multiple identity entities exist (e.g., "dog" created as artifact vs "${USER}" Person)
    cur.execute(
        "SELECT DISTINCT subject_id FROM facts f "
        "WHERE f.user_id = %s AND f.rel_type IN ('pref_name', 'also_known_as') "
        "AND f.superseded_at IS NULL "
        "ORDER BY (SELECT entity_type FROM entities WHERE id = f.subject_id) = 'Person' DESC, "
        "       f.confidence DESC, f.created_at DESC",
        (user_id,)
    )
    user_entity_ids_for_query = [row[0] for row in cur.fetchall()]
```

---

## Expected Behavior After Fix

**Test case same as above:**

**Query result (Person first):**
```
[${TEST_USER_ID}, efc8ea62-..., d807ffea-...]
                    ↑ picked (correct!)
```

**Logs after fix:**
```
query.user_identity canonical=${USER} entity_id=10d7d879-... owui_user_id=10d7d879-...
```

---

## Impact Analysis

**Severity:** P1 (blocks correct query results for most users)

**Scope:** All `/query` endpoint calls when:
- User has multiple entities with identity facts (common with artifact unknown-type entities)
- Unknown-type entity happened to be created/queried before Person entity
- Database returns unknown-type first (arbitrary ordering)

**Affected Systems:**
- Graph traversal (uses wrong entity as anchor)
- Relationship queries (scoped to wrong entity)
- Fact filtering (filters on wrong entity context)

---

## Recommendation

**Implement dBug-031 fix immediately.** It's a simple SQL ORDER BY clause addition that ensures correct identity selection. This should have been applied months ago based on the earlier context mentioning it as "FIXED."

**Files to Modify:**
- `src/api/main.py` (lines 3821-3826)

**Files to Create:**
- `BUGS/dBug-031-wrong-canonical-identity-unknown-types.md` (create the original bug report)

**Testing:**
- Verify canonical identity is Person when multiple identity entities exist
- Check logs: `query.user_identity canonical=${USER}` not `canonical=dog`
- Test `/query` response scope is correct user, not artifact entity
