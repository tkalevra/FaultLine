# dBug-033: Identity Resolution Selecting Wrong Entity (dBug-031 Regression)

**Status:** OPEN (regression detected during dprompt-92 testing)  
**Priority:** P1 (blocks correct query results, returns wrong user identity)  
**Date:** 2026-05-16

---

## Problem Statement

When user queries "tell me about my family", `/query` endpoint selects wrong canonical identity.

**Observed behavior:**
```
Query: "tell me about my family"
Identity selected: "dog" (unknown-type entity)
Expected: "chris" (Person entity with pref_name)
Result: Query returns facts for "dog", not for "chris"
```

**Logs confirm regression:**
```
2026-05-16 15:00:04 [info] query.user_identity canonical=dog entity_id=d807ffea owui_user_id=10d7d879-63cd-4f31-92ce-f2c9edb760ab
```

---

## Database State

Multiple entities with identity facts (pref_name) for same user:

```sql
SELECT subject_id, entity_type, pref_name FROM facts 
WHERE user_id = '10d7d879-63cd-4f31-92ce-f2c9edb760ab'
AND rel_type = 'pref_name' AND superseded_at IS NULL;

d807ffea-0140-5c9a-b312-930f964d469d | unknown   | dog        ← WRONG (selected)
10d7d879-63cd-4f31-92ce-f2c9edb760ab | Person    | chris      ← CORRECT (should select)
efc8ea62-381a-5859-b7c3-e2588c89bba6 | Person    | chris      ← Also correct
f119510d-8f7a-5843-8d72-bdffea35a538 | Person    | john ← Also correct
```

---

## Root Cause Analysis

This appears to be a regression of dBug-031, which was supposedly fixed by modifying identity resolution to prioritize Person entities. The fix should have been at lines ~3869-3885 in src/api/main.py, ordering results by `entity_type = 'Person'` DESC.

**Hypothesis:** Either:
1. The dBug-031 fix was reverted during rebuild
2. The query logic changed since the fix was applied
3. The identity resolution code path is different now

---

## Related Issues

- **dBug-031:** Wrong canonical identity selected when unknown/Animal types have pref_name
  - Fix applied: Line ~3875 should prioritize Person entities
  - Status: Originally marked FIXED, now showing regression

---

## Affected Code

`src/api/main.py` — `/query` endpoint identity resolution (lines ~3869-3885)

Should be selecting Person entities first, unknown/Animal second.

---

## Impact

- All `/query` responses affected
- Wrong entity context for graph traversal
- User facts not retrieved (facts about "dog", not about "chris")
- Family relationship queries return wrong scope

---

## Success Criteria

- ✓ Query selects "chris" (Person) as canonical identity
- ✓ "tell me about my family" returns facts scoped to "chris"
- ✓ Unknown-type entities NOT selected as canonical identity
- ✓ dBug-031 fix verified in code

---

## Test Case

```bash
curl -X POST "https://docker-host.helpalicekpro.ca/api/chat/completions" \
  -H "Authorization: Bearer <token>" \
  -d '{"model": "faultline-test", "messages": [{"role": "user", "content": "tell me about my family"}]}'

# Expected logs:
# query.user_identity canonical=chris entity_id=10d7d879...

# Actual logs:
# query.user_identity canonical=dog entity_id=d807ffea...
```

---

## Investigation Notes

- Regression detected: 2026-05-16 during dprompt-92 testing (entity scalar return fix)
- dprompt-92 fix is working correctly (scalars visible)
- Issue is separate from scalar deduplication
- Suggests identity resolution code needs review
